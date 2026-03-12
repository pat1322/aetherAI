"""
AetherAI — Orchestrator
Receives a command, classifies it (chat vs task), then either
answers directly or routes steps to agents.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from memory import MemoryManager
from utils.qwen_client import QwenClient
from utils.websocket_manager import WebSocketManager
from agent_router import AgentRouter

logger = logging.getLogger(__name__)

# Max characters stored in DB for step output (keeps steps panel clean)
STEP_OUTPUT_PREVIEW = 300


def extract_code_block(output: str) -> tuple[str, str, str]:
    """
    If output contains [CODE_BLOCK:lang]...[/CODE_BLOCK],
    returns (summary, language, code).
    Otherwise returns (output, "", "").
    """
    import re
    m = re.search(r"\[CODE_BLOCK:(\w+)\]\n(.*?)\n\[/CODE_BLOCK\]", output, re.DOTALL)
    if m:
        lang = m.group(1)
        code = m.group(2)
        summary = output[:m.start()].strip()
        return summary, lang, code
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
            logger.info(f"[{task_id}] Command classified as: {command_type}")

            # ── CHAT MODE ─────────────────────────────────────────────────────
            if command_type == "chat":
                answer = await self.qwen.answer(command)
                self.memory.update_task_status(task_id, "completed", result=answer)
                await self.ws_manager.broadcast_task_update(task_id, {
                    "status": "completed",
                    "message": "Done.",
                    "result": answer,
                    "is_chat": True,
                })
                return

            # ── TASK MODE ─────────────────────────────────────────────────────
            plan = await self.qwen.plan_task(command)

            # Deduplicate document_agent — keep only last call
            doc_indices = [i for i, s in enumerate(plan) if s.get("agent") == "document_agent"]
            if len(doc_indices) > 1:
                keep = doc_indices[-1]
                plan = [s for i, s in enumerate(plan) if s.get("agent") != "document_agent" or i == keep]
                for idx, step in enumerate(plan, 1):
                    step["step"] = idx

            logger.info(f"[{task_id}] Plan: {len(plan)} steps")

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
            for step in plan:
                if not self._running_tasks.get(task_id):
                    break

                step_num    = step.get("step", 0)
                agent_name  = step.get("agent", "research_agent")
                description = step.get("description", "")
                parameters  = step.get("parameters", {})

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
                        # Check for code block marker
                        summary, lang, code = extract_code_block(output)

                        # Store only summary in DB (keeps steps panel clean)
                        db_output = summary if summary else output[:STEP_OUTPUT_PREVIEW]
                        if len(output) > STEP_OUTPUT_PREVIEW and not code:
                            db_output = output[:STEP_OUTPUT_PREVIEW] + "…"

                        self.memory.update_step(task_id, step_num, "completed", db_output)
                        last_output = output  # pass full output to next step

                        # Broadcast to UI
                        if code:
                            # Send code block separately for rich rendering in chat
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
                    error_msg = f"Step {step_num} timed out"
                    logger.warning(f"[{task_id}] {error_msg}")
                    self.memory.update_step(task_id, step_num, "failed", error_msg)

                except Exception as e:
                    error_msg = f"Step {step_num} failed: {str(e)}"
                    logger.error(f"[{task_id}] {error_msg}", exc_info=True)
                    self.memory.update_step(task_id, step_num, "failed", error_msg)

            final_status = "completed" if self._running_tasks.get(task_id) else "cancelled"

            # For final result, strip code block markers
            final_summary, _, _ = extract_code_block(last_output)
            display_result = final_summary if final_summary else last_output

            self.memory.update_task_status(task_id, final_status, result=display_result[:500])
            await self.ws_manager.broadcast_task_update(task_id, {
                "status": final_status,
                "message": "Task completed." if final_status == "completed" else "Task cancelled.",
                "result": display_result if display_result else "",
            })

        except Exception as e:
            logger.error(f"[{task_id}] Orchestrator error: {e}", exc_info=True)
            self.memory.update_task_status(task_id, "failed", result=str(e))
            await self.ws_manager.broadcast_task_update(task_id, {
                "status": "failed",
                "message": f"Task failed: {str(e)}",
            })
        finally:
            self._running_tasks.pop(task_id, None)

    def cancel_task(self, task_id: str) -> bool:
        if task_id in self._running_tasks:
            self._running_tasks[task_id] = False
            return True
        return False
