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


class Orchestrator:
    def __init__(self, memory: MemoryManager, ws_manager: WebSocketManager):
        self.memory = memory
        self.ws_manager = ws_manager
        self.qwen = QwenClient()
        self.router = AgentRouter(memory=memory, ws_manager=ws_manager, qwen=self.qwen)
        self._running_tasks: dict[str, bool] = {}

    async def run_task(self, task_id: str, command: str):
        """Full lifecycle: classify → chat answer OR plan → execute steps → complete."""
        self._running_tasks[task_id] = True

        try:
            # ── Step 1: Classify the command ──────────────────────────────
            self.memory.update_task_status(task_id, "planning")
            await self.ws_manager.broadcast_task_update(task_id, {
                "status": "planning",
                "message": "AetherAI is analyzing your command...",
            })

            command_type = await self.qwen.classify_command(command)
            logger.info(f"[{task_id}] Command classified as: {command_type}")

            # ── CHAT MODE: answer directly, no agents ─────────────────────
            if command_type == "chat":
                answer = await self.qwen.answer(command)
                self.memory.update_task_status(task_id, "completed", result=answer)
                await self.ws_manager.broadcast_task_update(task_id, {
                    "status": "completed",
                    "message": "Done.",
                    "result": answer,
                    "is_chat": True,
                })
                logger.info(f"[{task_id}] Chat answered directly.")
                return

            # ── TASK MODE: plan + execute ─────────────────────────────────
            plan = await self.qwen.plan_task(command)

            # Deduplicate: if document_agent appears more than once, keep only the LAST call
            # (the last one has the most context from previous research steps)
            doc_indices = [i for i, s in enumerate(plan) if s.get('agent') == 'document_agent']
            if len(doc_indices) > 1:
                keep = doc_indices[-1]
                plan = [s for i, s in enumerate(plan) if s.get('agent') != 'document_agent' or i == keep]
                # Re-number steps
                for idx, step in enumerate(plan, 1):
                    step['step'] = idx
                logger.info(f"[{task_id}] Deduplicated document_agent calls to 1")

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

                step_num   = step.get("step", 0)
                agent_name = step.get("agent", "research_agent")
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
                    self.memory.update_step(task_id, step_num, "completed", output)
                    last_output = output or last_output
                    await self.ws_manager.broadcast_task_update(task_id, {
                        "status": "running",
                        "current_step": step_num,
                        "step_status": "completed",
                        "output": output if output else "",
                    })

                except asyncio.TimeoutError:
                    error_msg = f"Step {step_num} timed out"
                    logger.warning(f"[{task_id}] {error_msg}")
                    self.memory.update_step(task_id, step_num, "failed", error_msg)

                except Exception as e:
                    error_msg = f"Step {step_num} failed: {str(e)}"
                    logger.error(f"[{task_id}] {error_msg}", exc_info=True)
                    self.memory.update_step(task_id, step_num, "failed", error_msg)

            final_status = "completed" if self._running_tasks.get(task_id) else "cancelled"
            self.memory.update_task_status(task_id, final_status, result=last_output)
            await self.ws_manager.broadcast_task_update(task_id, {
                "status": final_status,
                "message": "Task completed." if final_status == "completed" else "Task cancelled.",
                "result": last_output if last_output else "",
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
