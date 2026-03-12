"""
AetherAI — Orchestrator  (Stage 4 — hardened)

WHAT'S NEW vs the previous version
────────────────────────────────────
1. Real async cancellation
   _tasks stores the live asyncio.Task.  cancel_task() calls task.cancel(),
   which raises CancelledError inside the running await — stopping immediately.

2. Per-agent timeouts
   Each agent type has a tuned timeout instead of a flat 120 s for everything.

3. Automatic step retry  (1 retry, configurable via MAX_STEP_RETRIES)
   A failed step is retried once with a short back-off before being marked
   failed.  Transient network errors and cold-start latency are the main wins.

4. Parallel step execution
   Consecutive steps that are independent (no data dependency, no PC control)
   are grouped and run with asyncio.gather.  Sequential steps run in order.

5. Structured context chaining
   Each step receives a compact summary block of every previous step's output
   instead of the raw last-output string.  Agents can act on earlier results.

6. __RESEARCH_CONTEXT__ placeholder
   Any step whose parameters contain __RESEARCH_CONTEXT__ gets it replaced with
   the output of the most recent research_agent or browser_agent step.

7. Progress percentage broadcast
   Every step update includes a "progress" int (0-100) for the UI progress bar.

8. Duplicate-output guard (fingerprint-based)
   Uses MD5 fingerprinting instead of string prefix comparison.

9. Graceful degradation
   A step that exhausts its retries is skipped with an error message instead of
   aborting the whole task (unless it's the only step).
"""

import asyncio
import hashlib
import logging
import re
from typing import Optional

from memory import MemoryManager
from utils.qwen_client import QwenClient
from utils.websocket_manager import WebSocketManager
from agent_router import AgentRouter

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────

MAX_STEP_RETRIES = 1
STEP_OUTPUT_PREVIEW = 300

AGENT_TIMEOUTS: dict[str, float] = {
    "research_agent":   35.0,
    "browser_agent":    75.0,
    "document_agent":  110.0,
    "coding_agent":     60.0,
    "automation_agent": 30.0,
    "default":          60.0,
}

RESEARCH_AGENTS = {"research_agent", "browser_agent"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_code_block(output: str) -> tuple[str, str, str]:
    m = re.search(r"\[CODE_BLOCK:(\w+)\]\n(.*?)\n\[/CODE_BLOCK\]", output, re.DOTALL)
    if m:
        return output[:m.start()].strip(), m.group(1), m.group(2)
    return output, "", ""


def is_type_action(agent_name: str, parameters: dict) -> bool:
    if agent_name != "automation_agent":
        return False
    return parameters.get("action") == "type"


def _fingerprint(text: str) -> str:
    return hashlib.md5(text.strip()[:400].encode()).hexdigest()[:8]


def _one_liner(text: str, max_chars: int = 120) -> str:
    line = " ".join(text.split())
    return (line[:max_chars] + "…") if len(line) > max_chars else line


def _build_context_block(history: list[dict]) -> str:
    if not history:
        return ""
    lines = []
    for h in history:
        lines.append(f"[Step {h.get('step', '?')} / {h.get('agent', '?')}] {h.get('summary', '')}")
    return "\n".join(lines)


def _are_independent(step_a: dict, step_b: dict) -> bool:
    """True when two steps can safely run in parallel."""
    sequential = {"automation_agent"}
    if step_a.get("agent") in sequential or step_b.get("agent") in sequential:
        return False
    if (step_a.get("agent") in RESEARCH_AGENTS
            and "__RESEARCH_CONTEXT__" in str(step_b.get("parameters", {}))):
        return False
    return True


def _inject_research_context(parameters: dict, research: str) -> dict:
    """Replace __RESEARCH_CONTEXT__ anywhere in the parameters dict."""
    import ast
    serialised = str(parameters).replace("__RESEARCH_CONTEXT__", research[:1500])
    try:
        return ast.literal_eval(serialised)
    except Exception:
        return parameters


# ── Orchestrator ──────────────────────────────────────────────────────────────

class Orchestrator:

    def __init__(self, memory: MemoryManager, ws_manager: WebSocketManager):
        self.memory     = memory
        self.ws_manager = ws_manager
        self.qwen       = QwenClient()
        self.router     = AgentRouter(memory=memory, ws_manager=ws_manager, qwen=self.qwen)
        self._tasks: dict[str, asyncio.Task] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def start_task(self, task_id: str, command: str) -> asyncio.Task:
        """Schedule the task and store the Task handle for real cancellation."""
        t = asyncio.create_task(self._run(task_id, command))
        self._tasks[task_id] = t
        t.add_done_callback(lambda _: self._tasks.pop(task_id, None))
        return t

    def cancel_task(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        if t and not t.done():
            t.cancel()
            logger.info(f"[{task_id}] cancel_task() called — Task.cancel() issued")
            return True
        return False

    # Legacy shim so main.py keeps working without changes
    async def run_task(self, task_id: str, command: str):
        self.start_task(task_id, command)

    # ── Top-level wrapper (catches CancelledError & unexpected exceptions) ─────

    async def _run(self, task_id: str, command: str):
        try:
            await self._orchestrate(task_id, command)
        except asyncio.CancelledError:
            logger.info(f"[{task_id}] CancelledError caught at top level")
            self.memory.update_task_status(task_id, "cancelled")
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":  "cancelled",
                "message": "Task cancelled.",
                "progress": 0,
            })
        except Exception as e:
            logger.error(f"[{task_id}] Unhandled orchestrator error: {e}", exc_info=True)
            self.memory.update_task_status(task_id, "failed", result=str(e))
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":  "failed",
                "message": f"Task failed: {e}",
            })

    # ── Core orchestration ────────────────────────────────────────────────────

    async def _orchestrate(self, task_id: str, command: str):

        # ── 1. Classify ───────────────────────────────────────────────────────
        self.memory.update_task_status(task_id, "planning")
        await self.ws_manager.broadcast_task_update(task_id, {
            "status":   "planning",
            "message":  "AetherAI is analyzing your command…",
            "progress": 0,
        })

        command_type = await self.qwen.classify_command(command)
        logger.info(f"[{task_id}] Classified: {command_type}")

        # ── 2. Chat shortcut ──────────────────────────────────────────────────
        if command_type == "chat":
            answer = await self.qwen.answer(command)
            self.memory.update_task_status(task_id, "completed", result=answer)
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":   "completed",
                "message":  "Done.",
                "result":   answer,
                "is_chat":  True,
                "progress": 100,
            })
            return

        # ── 3. Plan ───────────────────────────────────────────────────────────
        plan = self._sanitise_plan(await self.qwen.plan_task(command))
        logger.info(f"[{task_id}] Plan ({len(plan)} steps): {[s.get('agent') for s in plan]}")

        # Tag steps that need placeholder resolution
        for step in plan:
            params = step.get("parameters", {})
            inner  = params.get("parameters", params)
            if (step.get("agent") == "automation_agent"
                    and "__GENERATED_CONTENT__" in str(inner.get("text", ""))):
                step["_needs_content"] = True
            if "__RESEARCH_CONTEXT__" in str(params):
                step["_needs_research"] = True

        # Persist step records
        for step in plan:
            self.memory.create_step(
                task_id=task_id,
                step_number=step.get("step", 0),
                agent=step.get("agent", "unknown"),
                description=step.get("description", ""),
            )

        await self.ws_manager.broadcast_task_update(task_id, {
            "status":   "running",
            "message":  f"Plan ready — executing {len(plan)} step(s)…",
            "plan":     plan,
            "progress": 5,
        })
        self.memory.update_task_status(task_id, "running")

        # ── 4. Execute ────────────────────────────────────────────────────────
        step_history:         list[dict] = []
        last_research_output: str        = ""
        last_code:            str        = ""
        last_meaningful:      str        = command
        last_fp:              str        = ""
        completed_count:      int        = 0
        total:                int        = len(plan)

        for group in self._group_steps(plan):
            if len(group) > 1:
                results = await self._run_parallel_group(
                    group, task_id, command,
                    step_history, last_research_output, last_code,
                    completed_count, total,
                )
            else:
                results = [await self._run_step(
                    group[0], task_id, command,
                    step_history, last_research_output, last_code,
                    completed_count, total,
                )]

            for step, result in zip(group, results):
                completed_count += 1
                if result is None:
                    continue

                output, agent_name = result

                # Update context chain
                step_history.append({
                    "step":    step.get("step"),
                    "agent":   agent_name,
                    "summary": _one_liner(extract_code_block(output)[0] or output),
                })

                if agent_name in RESEARCH_AGENTS:
                    last_research_output = output

                _, _, code = extract_code_block(output)
                if code:
                    last_code = code

                if not is_type_action(agent_name, step.get("parameters", {})):
                    last_meaningful = output

                # Broadcast (with duplicate guard)
                new_fp = await self._broadcast_step_output(
                    task_id, step, agent_name, output,
                    completed_count, total, last_fp,
                )
                if new_fp:
                    last_fp = new_fp

        # ── 5. Final result ───────────────────────────────────────────────────
        final_summary, _, _ = extract_code_block(last_meaningful)
        display  = (final_summary or last_meaningful)[:500]
        final_fp = _fingerprint(display)

        self.memory.update_task_status(task_id, "completed", result=display)

        payload: dict = {
            "status":   "completed",
            "message":  "Task completed.",
            "progress": 100,
        }
        if final_fp != last_fp:
            payload["result"] = display

        await self.ws_manager.broadcast_task_update(task_id, payload)

    # ── Step execution (with retry) ───────────────────────────────────────────

    async def _run_step(
        self,
        step:             dict,
        task_id:          str,
        command:          str,
        step_history:     list[dict],
        last_research:    str,
        last_code:        str,
        completed_so_far: int,
        total:            int,
    ) -> Optional[tuple[str, str]]:

        step_num    = step.get("step", 0)
        agent_name  = step.get("agent", "research_agent")
        description = step.get("description", "")
        parameters  = dict(step.get("parameters", {}))
        timeout     = AGENT_TIMEOUTS.get(agent_name, AGENT_TIMEOUTS["default"])

        # Resolve placeholders
        if step.get("_needs_content"):
            resolved = last_code if last_code else await self._generate_content(task_id, command)
            inner = parameters.get("parameters", parameters)
            inner["text"] = resolved

        if step.get("_needs_research"):
            parameters = _inject_research_context(parameters, last_research or command)

        context  = _build_context_block(step_history)
        progress = int(5 + 90 * completed_so_far / max(total, 1))

        self.memory.update_step(task_id, step_num, "running")
        await self.ws_manager.broadcast_task_update(task_id, {
            "status":       "running",
            "current_step": step_num,
            "agent":        agent_name,
            "message":      f"Step {step_num}/{total}: {description}",
            "progress":     progress,
        })

        last_error: Optional[str] = None

        for attempt in range(MAX_STEP_RETRIES + 1):
            if attempt > 0:
                wait = 1.5 * attempt
                logger.info(f"[{task_id}] Step {step_num}: retry {attempt} (back-off {wait}s)")
                await self.ws_manager.broadcast_task_update(task_id, {
                    "status":  "running",
                    "message": f"Step {step_num}: retrying… (attempt {attempt + 1})",
                })
                await asyncio.sleep(wait)

            try:
                output = await asyncio.wait_for(
                    self.router.execute_step(
                        agent_name=agent_name,
                        parameters=parameters,
                        task_id=task_id,
                        previous_output=context,
                    ),
                    timeout=timeout,
                )

                summary, _, _ = extract_code_block(output or "")
                db_out = (summary or output or "")[:STEP_OUTPUT_PREVIEW]
                self.memory.update_step(task_id, step_num, "completed", db_out)
                return (output or "", agent_name)

            except asyncio.CancelledError:
                raise  # never swallow cancellation

            except asyncio.TimeoutError:
                last_error = f"timed out after {timeout:.0f}s"
                logger.warning(f"[{task_id}] Step {step_num}: {last_error}")

            except Exception as e:
                last_error = str(e)
                logger.error(
                    f"[{task_id}] Step {step_num} attempt {attempt} error: {e}",
                    exc_info=True,
                )

        # Permanent failure
        self.memory.update_step(task_id, step_num, "failed", last_error or "unknown")
        await self.ws_manager.broadcast_task_update(task_id, {
            "status":  "running",
            "message": f"⚠️ Step {step_num} ({agent_name}) failed after retries — skipping…",
        })
        return None

    # ── Parallel group ────────────────────────────────────────────────────────

    async def _run_parallel_group(
        self,
        group:            list[dict],
        task_id:          str,
        command:          str,
        step_history:     list[dict],
        last_research:    str,
        last_code:        str,
        completed_so_far: int,
        total:            int,
    ) -> list[Optional[tuple[str, str]]]:
        agents = [s.get("agent") for s in group]
        logger.info(f"[{task_id}] Parallel group: {agents}")
        await self.ws_manager.broadcast_task_update(task_id, {
            "status":  "running",
            "message": f"Running in parallel: {', '.join(agents)}…",
        })
        coros = [
            self._run_step(
                step, task_id, command,
                step_history, last_research, last_code,
                completed_so_far + i, total,
            )
            for i, step in enumerate(group)
        ]
        return list(await asyncio.gather(*coros))

    # ── Broadcast helper ──────────────────────────────────────────────────────

    async def _broadcast_step_output(
        self,
        task_id:    str,
        step:       dict,
        agent_name: str,
        output:     str,
        done:       int,
        total:      int,
        last_fp:    str,
    ) -> str:
        """Broadcast step output. Returns new fingerprint (or last_fp if skipped)."""
        if not output:
            return last_fp

        step_num = step.get("step", 0)
        progress = int(5 + 90 * done / max(total, 1))
        summary, lang, code = extract_code_block(output)

        if is_type_action(agent_name, step.get("parameters", {})):
            chat_out = "✅ Content typed into application"
            fp = _fingerprint(chat_out)
            if fp == last_fp:
                return last_fp
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":       "running",
                "current_step": step_num,
                "step_status":  "completed",
                "output":       chat_out,
                "progress":     progress,
            })
            return fp

        fp = _fingerprint(summary or output)
        if fp == last_fp:
            return last_fp

        if code:
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":       "running",
                "current_step": step_num,
                "step_status":  "completed",
                "output":       summary,
                "code_block":   {"language": lang, "code": code},
                "progress":     progress,
            })
        else:
            await self.ws_manager.broadcast_task_update(task_id, {
                "status":       "running",
                "current_step": step_num,
                "step_status":  "completed",
                "output":       output,
                "progress":     progress,
            })
        return fp

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _sanitise_plan(plan: list[dict]) -> list[dict]:
        """Remove duplicate document_agent steps and renumber."""
        doc_idxs = [i for i, s in enumerate(plan) if s.get("agent") == "document_agent"]
        if len(doc_idxs) > 1:
            keep = doc_idxs[-1]
            plan = [s for i, s in enumerate(plan)
                    if s.get("agent") != "document_agent" or i == keep]
        for idx, step in enumerate(plan, 1):
            step["step"] = idx
        return plan

    @staticmethod
    def _group_steps(plan: list[dict]) -> list[list[dict]]:
        """Group consecutive independent steps for parallel execution."""
        if not plan:
            return []
        groups: list[list[dict]] = [[plan[0]]]
        for step in plan[1:]:
            last = groups[-1]
            if len(last) == 1 and _are_independent(last[0], step):
                last.append(step)
            else:
                groups.append([step])
        return groups

    async def _generate_content(self, task_id: str, command: str) -> str:
        await self.ws_manager.broadcast_task_update(task_id, {
            "status":  "running",
            "message": "Generating content…",
        })
        return await self.qwen.generate_content(command)
