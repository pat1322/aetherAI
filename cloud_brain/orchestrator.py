"""
AetherAI — Orchestrator
Receives a command, classifies it, then plans and executes steps.

KEY FIX: When a type step contains __GENERATED_CONTENT__ placeholder,
the orchestrator generates the actual content FIRST (separate Qwen call),
then replaces the placeholder before sending to the device.
This prevents text truncation caused by Qwen writing long content inline in JSON.
"""

import asyncio
import logging
import re

from memory import MemoryManager
from utils.qwen_client import QwenClient
from utils.websocket_manager import WebSocketManager
from agent_router import AgentRouter

logger = logging.getLogger(__name__)

STEP_OUTPUT_PREVIEW = 300


def extract_code_block(output: str) -> tuple[str, str, str]:
    m = re.search(r"\[CODE_BLOCK:(\w+)\]\n(.*?)\n\[/CODE_BLOCK\]", output, re.DOTALL)
    if m:
        return output[:m.start()].strip(), m.group(1), m.group(2)
    return output, "", ""


class Orchestrator:
    def __init__(self, memory: MemoryManager, ws_manager: WebSocketManager):
        self.memory = memory
        self.ws_manager = ws_manager
        self.qwen = QwenClient()
        self.router = AgentRouter(memory=memory, ws_manager=ws_manager, qwen=self.qwen)
        self._running_tasks: dict[str, bool] = {}

    async def run_task(self, task_id: str, command: str):
        self._running_tasks[task_id] = True

        try:
            self.memory.update_task_status(task_id, "planning")
            await self.ws_manager.broadcast_task_update(task_id, {
                "status": "planning",
                "message": "AetherAI is analyzing your command...",
            })

            command_type = await self.qwen.classify_command(command)
            logger.info(f"[{task_id}] Classified: {command_type}")

            # ── CHAT MODE ─────────────────────────────────────────────────────
            if command_type == "chat":
                answer = await self.qwen.answer(command)
                self.memory.update_task_status(task_id, "completed", result=answer)
                await self.ws_manager.broadcast_task_update(task_id, {
                    "status": "completed", "message": "Done.",
                    "result": answer, "is_chat": True,
                })
                return

            # ── TASK MODE ─────────────────────────────────────────────────────
            plan = await self.qwen.plan_task(command)

            # Deduplicate document_agent
            doc_idx = [i for i, s in enumerate(plan) if s.get("agent") == "document_agent"]
            if len(doc_idx) > 1:
                keep = doc_idx[-1]
                plan = [s for i, s in enumerate(plan) if s.get("agent") != "document_agent" or i == keep]
                for idx, step in enumerate(plan, 1):
                    step["step"] = idx

            logger.info(f"[{task_id}] Plan ({len(plan)} steps): {[s.get('agent') for s in plan]}")

            # ── Pre-resolve __GENERATED_CONTENT__ placeholders ────────────────
            # Find all type steps that need content generated
            # Content comes from: previous coding_agent output, or fresh Qwen generation
            generated_content = None  # will hold code or text from previous step

            for step in plan:
                agent = step.get("agent", "")
                params = step.get("parameters", {})

                # Track coding_agent output as content source
                if agent == "coding_agent":
                    # Mark that next type step should use coding_agent output
                    step["_will_generate_code"] = True
                    continue

                # Resolve __GENERATED_CONTENT__ in type steps
                if agent == "automation_agent":
                    inner = params.get("parameters", params)
                    text = inner.get("text", "")
                    if "__GENERATED_CONTENT__" in str(text):
                        # Will be resolved at runtime after previous step
                        step["_needs_content"] = True

            for step in plan:
                self.memory.create_step(
                    task_id=task_id,
                    step_number=step.get("step", 0),
                    agent=step.get("agent", "unknown"),
                    description=step.get("description", ""),
                )

            await self.ws_manager.broadcast_task_update(task_id, {
                "status": "running",
                "message": f"Plan ready. Executing {len(plan)} steps...",
                "plan": plan,
            })
            self.memory.update_task_status(task_id, "running")

            last_output = command
            last_code   = None   # holds code from coding_agent

            for step in plan:
                if not self._running_tasks.get(task_id):
                    break

                step_num    = step.get("step", 0)
                agent_name  = step.get("agent", "research_agent")
                description = step.get("description", "")
                parameters  = step.get("parameters", {})

                # ── Resolve __GENERATED_CONTENT__ right before execution ───────
                if step.get("_needs_content"):
                    if last_code:
                        # Use code from previous coding_agent step
                        resolved_text = last_code
                    else:
                        # Generate the content now (story, letter, essay, etc.)
                        logger.info(f"[{task_id}] Generating content for type step...")
                        await self.ws_manager.broadcast_task_update(task_id, {
                            "status": "running",
                            "message": "Generating content...",
                        })
                        resolved_text = await self.qwen.generate_content(command)

                    # Inject resolved text into parameters
                    if "parameters" in parameters:
                        parameters["parameters"]["text"] = resolved_text
                    else:
                        parameters["text"] = resolved_text

                    logger.info(f"[{task_id}] Resolved content ({len(resolved_text)} chars)")

                self.memory.update_step(task_id, step_num, "running")
                await self.ws_manager.broadcast_task_update(task_id, {
                    "status": "running",
                    "current_step": step_num,
                    "agent": agent_name,
                    "message": f"Step {step_num}: {description}",
                })

                try:
                    output = await asyncio.wait_for(
                        self.router.execute_step(
                            agent_name=agent_name,
                            parameters=parameters,
                            task_id=task_id,
                            previous_output=last_output,
                        ),
                        timeout=120.0,
                    )

                    if output:
                        summary, lang, code = extract_code_block(output)

                        # Save code for later type steps
                        if code:
                            last_code = code

                        # DB: store summary only (keep steps panel clean)
                        db_output = summary if summary else output
                        if len(db_output) > STEP_OUTPUT_PREVIEW:
                            db_output = db_output[:STEP_OUTPUT_PREVIEW] + "…"

                        self.memory.update_step(task_id, step_num, "completed", db_output)
                        last_output = output

                        # Broadcast
                        if code:
                            await self.ws_manager.broadcast_task_update(task_id, {
                                "status": "running",
                                "current_step": step_num,
                                "step_status": "completed",
                                "output": summary,
                                "code_block": {"language": lang, "code": code},
                            })
                        else:
                            await self.ws_manager.broadcast_task_update(task_id, {
                                "status": "running",
                                "current_step": step_num,
                                "step_status": "completed",
                                "output": output,
                            })
                    else:
                        self.memory.update_step(task_id, step_num, "completed", "")

                except asyncio.TimeoutError:
                    msg = f"Step {step_num} timed out"
                    logger.warning(f"[{task_id}] {msg}")
                    self.memory.update_step(task_id, step_num, "failed", msg)

                except Exception as e:
                    msg = f"Step {step_num} failed: {e}"
                    logger.error(f"[{task_id}] {msg}", exc_info=True)
                    self.memory.update_step(task_id, step_num, "failed", msg)

            final_status = "completed" if self._running_tasks.get(task_id) else "cancelled"
            final_summary, _, _ = extract_code_block(last_output)
            display = (final_summary or last_output)[:500]

            self.memory.update_task_status(task_id, final_status, result=display)
            await self.ws_manager.broadcast_task_update(task_id, {
                "status": final_status,
                "message": "Task completed." if final_status == "completed" else "Task cancelled.",
                "result": display,
            })

        except Exception as e:
            logger.error(f"[{task_id}] Orchestrator error: {e}", exc_info=True)
            self.memory.update_task_status(task_id, "failed", result=str(e))
            await self.ws_manager.broadcast_task_update(task_id, {
                "status": "failed",
                "message": f"Task failed: {e}",
            })
        finally:
            self._running_tasks.pop(task_id, None)

    def cancel_task(self, task_id: str) -> bool:
        if task_id in self._running_tasks:
            self._running_tasks[task_id] = False
            return True
        return False
