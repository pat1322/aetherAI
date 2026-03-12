"""
AetherAI — Automation Agent (Stage 3)
Sends actions to the connected Device Agent.
Supports: single actions, multi-step sequences, and vision loops.
"""

import asyncio
import json
import logging
import uuid
from typing import Optional

from agents import BaseAgent

logger = logging.getLogger(__name__)


class AutomationAgent(BaseAgent):
    name = "automation_agent"
    description = "Controls mouse, keyboard, and screen on the connected PC"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        # Normalize flat parameters from Qwen into nested form
        # e.g. {"action": "open_app", "app": "notepad"}
        #   -> {"action": "open_app", "parameters": {"app": "notepad"}}
        action_name = parameters.get("action", "")
        if action_name and "parameters" not in parameters:
            inner = {
                k: v for k, v in parameters.items()
                if k not in ("action", "mode", "goal", "task", "sequence")
            }
            parameters = {
                "action": action_name,
                "parameters": inner,
                **{k: v for k, v in parameters.items() if k in ("mode", "goal", "task", "sequence")},
            }

        # Check a device is connected
        devices = self.ws_manager.list_devices()
        if not devices:
            return ("⚠️ No device connected. To control your PC:\n"
                    "1. Go to C:\\Users\\patri\\aetherAI\\device_agent\\\n"
                    "2. Run: python agent.py\n"
                    "3. Wait for 'Connected' message, then retry.")

        device_id = devices[0]
        action    = parameters.get("action", "")
        goal      = parameters.get("goal", "") or parameters.get("task", "") or context
        sequence  = parameters.get("sequence", [])

        # Vision loop mode
        if parameters.get("mode") == "vision" or (goal and not action and not sequence):
            return await self._vision_task(device_id, goal, task_id)

        # Sequence mode
        if sequence:
            return await self._run_sequence(device_id, sequence, task_id)

        # Single action mode
        if action:
            inner_params = parameters.get("parameters", {})
            return await self._single_action(device_id, action, inner_params, task_id)

        return "⚠️ No action, sequence, or goal specified."

    # ── Single action ─────────────────────────────────────────────────────────

    async def _single_action(self, device_id: str, action: str,
                              params: dict, task_id: str) -> str:
        request_id = str(uuid.uuid4())[:8]
        future = asyncio.get_event_loop().create_future()

        self.ws_manager.register_pending(request_id, future)

        await self.ws_manager.send_to_device(device_id, {
            "type":       "action",
            "action":     action,
            "parameters": params,       # clean nested dict — no extra keys
            "request_id": request_id,
            "task_id":    task_id,
        })

        logger.info(f"[AutomationAgent] Sent action={action} params={params}")

        try:
            result = await asyncio.wait_for(future, timeout=15.0)
            return f"✅ {action}: {result.get('result', 'done')}"
        except asyncio.TimeoutError:
            return f"⚠️ Action '{action}' timed out"
        finally:
            self.ws_manager.unregister_pending(request_id)

    # ── Sequence of actions ───────────────────────────────────────────────────

    async def _run_sequence(self, device_id: str, sequence: list,
                             task_id: str) -> str:
        results = []
        for i, step in enumerate(sequence, 1):
            action = step.get("action", "")
            # Support both nested {"parameters": {...}} and flat params
            params = step.get("parameters") or {
                k: v for k, v in step.items() if k != "action"
            }
            logger.info(f"[AutomationAgent] Sequence step {i}/{len(sequence)}: {action} params={params}")

            request_id = str(uuid.uuid4())[:8]
            future     = asyncio.get_event_loop().create_future()
            self.ws_manager.register_pending(request_id, future)

            await self.ws_manager.send_to_device(device_id, {
                "type":       "action",
                "action":     action,
                "parameters": params,
                "request_id": request_id,
                "task_id":    task_id,
            })

            try:
                result = await asyncio.wait_for(future, timeout=20.0)
                results.append(f"Step {i} ({action}): {result.get('result','done')}")
            except asyncio.TimeoutError:
                results.append(f"Step {i} ({action}): timed out")
            finally:
                self.ws_manager.unregister_pending(request_id)

            await asyncio.sleep(0.5)

        return "✅ Sequence complete:\n" + "\n".join(results)

    # ── Vision loop ───────────────────────────────────────────────────────────

    async def _vision_task(self, device_id: str, goal: str, task_id: str) -> str:
        request_id = str(uuid.uuid4())[:8]
        future     = asyncio.get_event_loop().create_future()

        self.ws_manager.register_vision_task(request_id, future, self._vision_step_handler)

        await self.ws_manager.send_to_device(device_id, {
            "type":       "vision_task",
            "goal":       goal,
            "task_id":    task_id,
            "request_id": request_id,
            "max_steps":  15,
        })

        try:
            result = await asyncio.wait_for(future, timeout=180.0)
            return f"✅ Vision task complete: {result}"
        except asyncio.TimeoutError:
            return "⚠️ Vision task timed out after 3 minutes"
        finally:
            self.ws_manager.unregister_pending(request_id)

    async def _vision_step_handler(self, device_id: str, request_id: str,
                                    step_data: dict) -> dict:
        goal      = step_data.get("goal", "")
        step_num  = step_data.get("step", 1)
        img_b64   = step_data.get("image_base64", "")
        screen_w, screen_h = 1920, 1080

        prompt = f"""You are controlling a Windows PC to accomplish this goal: {goal}

This is step {step_num}. Look at the screenshot and decide the single best next action.

Return ONLY valid JSON with one of these formats:

To click somewhere:
{{"action": "click", "parameters": {{"x": 500, "y": 300}}, "reason": "clicking the button"}}

To type text:
{{"action": "type", "parameters": {{"text": "hello world"}}, "reason": "typing the search query"}}

To press a hotkey:
{{"action": "hotkey", "parameters": {{"keys": ["ctrl", "t"]}}, "reason": "opening new tab"}}

To scroll:
{{"action": "scroll", "parameters": {{"x": 960, "y": 540, "clicks": -3}}, "reason": "scrolling down"}}

To open an app:
{{"action": "open_app", "parameters": {{"app": "notepad"}}, "reason": "opening notepad"}}

To wait:
{{"action": "wait", "parameters": {{"ms": 1000}}, "reason": "waiting for page to load"}}

When the goal is fully complete:
{{"action": "done", "message": "Goal accomplished: ...", "reason": "task finished"}}

Screen is {screen_w}x{screen_h} pixels. Be precise with coordinates."""

        try:
            response = await self.qwen.chat(
                system_prompt="You are a computer vision agent. Analyze the screenshot and return ONLY valid JSON action.",
                user_message=prompt,
                temperature=0.1,
            )

            import re
            response = re.sub(r"```(?:json)?", "", response).strip().rstrip("`").strip()
            action_data = json.loads(response)
            logger.info(f"[Vision] Step {step_num}: {action_data.get('action')} — {action_data.get('reason','')}")
            return action_data

        except Exception as e:
            logger.error(f"[Vision] Step analysis error: {e}")
            return {"action": "done", "message": f"Vision error: {e}"}
            