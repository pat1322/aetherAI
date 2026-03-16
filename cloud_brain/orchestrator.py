"""
AetherAI — Orchestrator  (Stage 6 — streaming patch)

Streaming addition
──────────────────
STREAMING_AGENTS defines which agents stream their final LLM write step
token-by-token. For these agents the orchestrator:

  1. Sends  stream_event="agent_stream_start"  before execute_step()
     so the UI opens a streaming bubble for this step.
  2. The agent streams chunks internally via stream_llm() / stream_summarize()
     directly into the WebSocket (no orchestrator involvement needed).
  3. Sends  stream_event="agent_stream_end"  after execute_step() so
     the UI finalises the bubble (renders markdown, adds copy button).
  4. Skips broadcasting the step output as a plain logMarkdown entry
     because the streaming bubble already showed it.

The coding_agent is a special case: it streams the code generation call
but then returns a [CODE_BLOCK:...] tagged string. The orchestrator
detects the code block tag and renders it as a syntax-highlighted block
as before — the stream bubble is already finalised by then.

All Stage 5 fixes retained.
All Stage 6 Layer 1 fixes retained (FIX 7, FIX 9).
"""

import asyncio
import logging
import re

from memory import MemoryManager
from utils.qwen_client import QwenClient
from utils.websocket_manager import WebSocketManager
from agent_router import AgentRouter

logger = logging.getLogger(__name__)

STEP_OUTPUT_PREVIEW = 800

# Agents that stream their final LLM write step via stream_llm() /
# stream_summarize(). The orchestrator sends agent_stream_start/end
# around these so the UI can open and close the streaming bubble.
STREAMING_AGENTS = frozenset({
    "research_agent",
    "browser_agent",
    "coding_agent",
    "news_agent",
})


def extract_code_block(output: str) -> tuple[str, str, str]:
    m = re.search(r"\[CODE_BLOCK:(\w+)\]\n(.*?)\n\[/CODE_BLOCK\]", output, re.DOTALL)
    if m:
        return output[:m.start()].strip(), m.group(1), m.group(2)
    return output, "", ""


def is_type_action(agent_name: str, parameters: dict) -> bool:
    if agent_name != "automation_agent":
        return False
    return parameters.get("action") == "type"


def type_action_summary(parameters: dict) -> str:
    return "✅ Content typed into application"


def _load_user_context(memory: MemoryManager) -> str:
    try:
        from agents.memory_agent import MemoryAgent
        return MemoryAgent.load_context(memory)
    except Exception as e:
        logger.debug(f"[Orchestrator] Could not load user context: {e}")
        return ""


class Orchestrator:
    def __init__(self, memory: MemoryManager, ws_manager: WebSocketManager):
        self.memory     = memory
        self.ws_manager = ws_manager
        self.qwen       = QwenClient()
        self.router     = AgentRouter(memory=memory, ws_manager=ws_manager, qwen=self.qwen)
        self._task_handles: dict[str, asyncio.Task] = {}

    async def run_task(self, task_id: str, command: str):
        current = asyncio.current_task()
        if current:
            self._task_handles[task_id] = current

        try:
            self.memory.update_task_status(task_id, "planning")
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":  "planning",
                "message": "AetherAI is analyzing your command...",
            })

            user_context = _load_user_context(self.memory)
            if user_context:
                logger.info(f"[{task_id}] User context loaded ({len(user_context)} chars)")

            command_type = await self.qwen.classify_command(command, user_context=user_context)
            logger.info(f"[{task_id}] Classified: {command_type}")

            # ── CHAT MODE — streamed ──────────────────────────────────────────
            if command_type == "chat":
                await self.ws_manager.broadcast_task_update(task_id, {
                    "status":       "streaming",
                    "message":      "Thinking...",
                    "stream_event": "stream_start",
                    "is_chat":      True,
                })
                self.memory.update_task_status(task_id, "running")

                full_text = ""
                try:
                    async for chunk in self.qwen.stream_answer(
                        command, user_context=user_context
                    ):
                        full_text += chunk
                        await self.ws_manager.stream_chunk_to_ui(task_id, chunk)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"[{task_id}] Stream error: {e}", exc_info=True)
                    error_chunk = f"\n\n⚠️ Error: {e}"
                    full_text += error_chunk
                    await self.ws_manager.stream_chunk_to_ui(task_id, error_chunk)

                self.memory.update_task_status(task_id, "completed", result=full_text)
                await self.ws_manager.broadcast_task_update(task_id, {
                    "status":       "completed",
                    "message":      "Done.",
                    "stream_event": "stream_end",
                    "is_chat":      True,
                })
                return

            # ── TASK MODE ─────────────────────────────────────────────────────
            plan = await self.qwen.plan_task(command, user_context=user_context)

            doc_idx = [i for i, s in enumerate(plan) if s.get("agent") == "document_agent"]
            if len(doc_idx) > 1:
                keep = doc_idx[-1]
                plan = [s for i, s in enumerate(plan)
                        if s.get("agent") != "document_agent" or i == keep]
                for idx, step in enumerate(plan, 1):
                    step["step"] = idx

            logger.info(f"[{task_id}] Plan ({len(plan)} steps): {[s.get('agent') for s in plan]}")

            for step in plan:
                agent  = step.get("agent", "")
                params = step.get("parameters", {})
                if agent == "coding_agent":
                    step["_will_generate_code"] = True
                    continue
                if agent == "automation_agent":
                    inner = params.get("parameters", params)
                    if "__GENERATED_CONTENT__" in str(inner.get("text", "")):
                        step["_needs_content"] = True

            for step in plan:
                self.memory.create_step(
                    task_id=task_id,
                    step_number=step.get("step", 0),
                    agent=step.get("agent", "unknown"),
                    description=step.get("description", ""),
                )

            await self.ws_manager.broadcast_task_update(task_id, {
                "status":  "running",
                "message": f"Plan ready. Executing {len(plan)} steps...",
                "plan":    plan,
            })
            self.memory.update_task_status(task_id, "running")

            last_output            = command
            last_code              = None
            last_meaningful_output = command
            last_step_streamed     = False  # True when final step was a streaming agent

            for step in plan:
                step_num    = step.get("step", 0)
                agent_name  = step.get("agent", "research_agent")
                description = step.get("description", "")
                parameters  = step.get("parameters", {})
                is_streaming_step = agent_name in STREAMING_AGENTS

                if step.get("_needs_content"):
                    if last_code:
                        resolved_text = last_code
                    else:
                        logger.info(f"[{task_id}] Generating content for type step...")
                        await self.ws_manager.broadcast_task_update(task_id, {
                            "status":  "running",
                            "message": "Generating content...",
                        })
                        resolved_text = await self.qwen.generate_content(command)

                    if "parameters" in parameters:
                        parameters["parameters"]["text"] = resolved_text
                    else:
                        parameters["text"] = resolved_text
                    logger.info(f"[{task_id}] Resolved content ({len(resolved_text)} chars)")

                self.memory.update_step(task_id, step_num, "running")
                await self.ws_manager.broadcast_task_update(task_id, {
                    "status":       "running",
                    "current_step": step_num,
                    "agent":        agent_name,
                    "message":      f"Step {step_num}: {description}",
                })

                # ── Open stream bubble for streaming agents ────────────────────
                if is_streaming_step:
                    await self.ws_manager.broadcast_task_update(task_id, {
                        "status":         "running",
                        "stream_event":   "agent_stream_start",
                        "current_step":   step_num,
                        "agent":          agent_name,
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
                        if code:
                            last_code = code

                        if is_type_action(agent_name, parameters):
                            chat_output = type_action_summary(parameters)
                            db_output   = chat_output
                            # type actions are not streaming — broadcast normally
                            is_streaming_step = False
                        else:
                            db_output = summary if summary else output
                            if len(db_output) > STEP_OUTPUT_PREVIEW:
                                db_output = db_output[:STEP_OUTPUT_PREVIEW] + "…"
                            chat_output            = output
                            last_meaningful_output = output

                        self.memory.update_step(task_id, step_num, "completed", db_output)
                        last_output = output

                        if code:
                            # ── Close stream bubble then show code block ───────
                            if is_streaming_step:
                                await self.ws_manager.broadcast_task_update(task_id, {
                                    "status":       "running",
                                    "stream_event": "agent_stream_end",
                                    "current_step": step_num,
                                })
                            await self.ws_manager.broadcast_task_update(task_id, {
                                "status":       "running",
                                "current_step": step_num,
                                "step_status":  "completed",
                                "output":       summary,
                                "code_block":   {"language": lang, "code": code},
                            })
                        elif is_streaming_step:
                            # ── Close stream bubble — content already streamed ─
                            await self.ws_manager.broadcast_task_update(task_id, {
                                "status":       "running",
                                "stream_event": "agent_stream_end",
                                "current_step": step_num,
                            })
                            # Do NOT send chat_output again — already streamed
                            last_step_streamed = True
                        else:
                            # ── Non-streaming agent — broadcast normally ────────
                            last_step_streamed = False
                            await self.ws_manager.broadcast_task_update(task_id, {
                                "status":       "running",
                                "current_step": step_num,
                                "step_status":  "completed",
                                "output":       chat_output,
                            })
                    else:
                        if is_streaming_step:
                            await self.ws_manager.broadcast_task_update(task_id, {
                                "status":       "running",
                                "stream_event": "agent_stream_end",
                                "current_step": step_num,
                            })
                        self.memory.update_step(task_id, step_num, "completed", "")

                except asyncio.CancelledError:
                    logger.info(f"[{task_id}] Step {step_num} cancelled")
                    self.memory.update_step(task_id, step_num, "cancelled", "Cancelled")
                    raise

                except asyncio.TimeoutError:
                    msg = f"Step {step_num} timed out"
                    logger.warning(f"[{task_id}] {msg}")
                    self.memory.update_step(task_id, step_num, "failed", msg)

                except Exception as e:
                    msg = f"Step {step_num} failed: {e}"
                    logger.error(f"[{task_id}] {msg}", exc_info=True)
                    self.memory.update_step(task_id, step_num, "failed", msg)

            final_summary, _, _ = extract_code_block(last_meaningful_output)
            display = (final_summary or last_meaningful_output)[:500]

            self.memory.update_task_status(task_id, "completed", result=display)

            # When the last step was a streaming agent, the content is already
            # rendered in a stream bubble — do NOT send result in the completion
            # broadcast or the UI will render it a second time below the bubble.
            # The full result is saved to the DB for history replay.
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":  "completed",
                "message": "Task completed.",
                "result":  "" if last_step_streamed else display,
            })

        except asyncio.CancelledError:
            logger.info(f"[{task_id}] Task cancelled")
            self.memory.update_task_status(task_id, "cancelled")
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":  "cancelled",
                "message": "Task cancelled.",
            })

        except Exception as e:
            logger.error(f"[{task_id}] Orchestrator error: {e}", exc_info=True)
            self.memory.update_task_status(task_id, "failed", result=str(e))
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":  "failed",
                "message": f"Task failed: {e}",
            })
        finally:
            self._task_handles.pop(task_id, None)
            self.ws_manager.clear_broadcast_cache(task_id)

    def cancel_task(self, task_id: str) -> bool:
        task_handle = self._task_handles.get(task_id)
        if task_handle and not task_handle.done():
            task_handle.cancel()
            return True
        return False
