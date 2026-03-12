"""
AetherAI — Automation Agent  (Stage 4 — hardened)

WHAT'S NEW vs the previous version
────────────────────────────────────
1. Consistent parameter normalisation
   _normalise() handles every known shape the planner might produce:
     {"action":"type","text":"..."}                  (flat, no inner params key)
     {"action":"type","parameters":{"text":"..."}}   (nested — standard)
     {"action":"type","parameters":"some text"}      (string params — Qwen bug)
   One pass at the top of run() so _single_action and _run_sequence
   always receive a consistent shape.

2. send_to_device() uses SendResult enum
   If the device isn't connected we return a clear message immediately
   instead of a misleading "timed out" after the full timeout window.

3. Sequence step timeouts respect ACTION_TIMEOUTS per action
   Previously a flat default was used for all steps. Now each step looks
   up its own timeout from ACTION_TIMEOUTS.

4. Vision loop: future always unregistered on timeout
   The old vision task didn't call unregister_pending in the timeout path,
   leaking the future. Fixed via the finally block.

5. CancelledError pass-through in all paths.
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Optional

from agents import BaseAgent
from utils.websocket_manager import SendResult

logger = logging.getLogger(__name__)

ACTION_TIMEOUTS: dict[str, float] = {
    "open_app":              25.0,
    "new_file":              25.0,
    "run_command":           30.0,
    "type":                  40.0,
    "type_special":          40.0,
    "screenshot_and_return": 15.0,
    "default":               15.0,
}


def _normalise(parameters: dict) -> dict:
    """
    Canonicalise whatever shape the planner produced into:
      {action, parameters (dict), mode, goal, sequence}
    """
    if "action" not in parameters and isinstance(parameters.get("parameters"), dict):
        inner = parameters["parameters"]
        if "action" in inner:
            parameters = {**inner, **{k: v for k, v in parameters.items()
                                       if k != "parameters"}}

    action   = parameters.get("action", "")
    sequence = parameters.get("sequence", [])
    mode     = parameters.get("mode", "")
    goal     = parameters.get("goal", "") or parameters.get("task", "")

    raw_inner = parameters.get("parameters", {})
    if isinstance(raw_inner, str):
        inner_params = {"text": raw_inner}
    elif isinstance(raw_inner, dict):
        inner_params = dict(raw_inner)
    else:
        inner_params = {}

    RESERVED = {"action", "mode", "goal", "task", "sequence", "parameters",
                "type", "topic", "query", "description", "step", "agent"}
    for k, v in parameters.items():
        if k not in RESERVED and k not in inner_params:
            inner_params[k] = v

    return {
        "action":     action,
        "parameters": inner_params,
        "mode":       mode,
        "goal":       goal,
        "sequence":   sequence,
    }


class AutomationAgent(BaseAgent):
    name        = "automation_agent"
    description = "Controls mouse, keyboard, and screen on the connected PC"

    async def run(self, parameters: dict, task_id: str, context: str = "") -> Optional[str]:
        try:
            return await self._run(parameters, task_id, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[AutomationAgent] Error: {e}", exc_info=True)
            return f"⚠️ AutomationAgent error: {e}"

    async def _run(self, parameters: dict, task_id: str, context: str) -> Optional[str]:
        norm = _normalise(parameters)
        action, inner, mode, goal, sequence = (
            norm["action"], norm["parameters"],
            norm["mode"], norm["goal"], norm["sequence"],
        )

        devices = self.ws_manager.list_devices()
        if not devices:
            return (
                "⚠️ No device connected. To control your PC:\n"
                "1. Go to device_agent/\n"
                "2. Run: python agent.py\n"
                "3. Wait for 'Connected' message, then retry."
            )

        device_id     = devices[0]
        effective_goal = goal or context

        if mode == "vision" or (effective_goal and not action and not sequence):
            return await self._vision_task(device_id, effective_goal, task_id)
        if sequence:
            return await self._run_sequence(device_id, sequence, task_id)
        if action:
            return await self._single_action(device_id, action, inner, task_id)

        return "⚠️ No action, sequence, or goal specified."

    # ── Single action ──────────────────────────────────────────────────────────

    async def _single_action(self, device_id: str, action: str,
                              params: dict, task_id: str) -> str:
        request_id = str(uuid.uuid4())[:8]
        future     = asyncio.get_event_loop().create_future()
        timeout    = ACTION_TIMEOUTS.get(action, ACTION_TIMEOUTS["default"])

        self.ws_manager.register_pending(request_id, future)
        logger.info(f"[AutomationAgent] action={action} params={params} timeout={timeout}s")

        send_result = await self.ws_manager.send_to_device(device_id, {
            "type":       "action",
            "action":     action,
            "parameters": params,
            "request_id": request_id,
            "task_id":    task_id,
        })

        if send_result == SendResult.NO_DEVICE:
            self.ws_manager.unregister_pending(request_id)
            return "⚠️ Device disconnected before action could be sent."
        if send_result == SendResult.SEND_ERROR:
            self.ws_manager.unregister_pending(request_id)
            return f"⚠️ Failed to send action '{action}' to device."

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            return f"✅ {action}: {response.get('result', 'done')}"
        except asyncio.TimeoutError:
            return f"⚠️ Action '{action}' timed out after {timeout}s"
        finally:
            self.ws_manager.unregister_pending(request_id)

    # ── Sequence ───────────────────────────────────────────────────────────────

    async def _run_sequence(self, device_id: str, sequence: list,
                             task_id: str) -> str:
        results = []
        for i, step in enumerate(sequence, 1):
            norm_step = _normalise(step)
            action    = norm_step["action"]
            params    = norm_step["parameters"]
            timeout   = ACTION_TIMEOUTS.get(action, ACTION_TIMEOUTS["default"])

            logger.info(f"[AutomationAgent] Seq {i}/{len(sequence)}: "
                        f"action={action} params={params}")

            request_id = str(uuid.uuid4())[:8]
            future     = asyncio.get_event_loop().create_future()
            self.ws_manager.register_pending(request_id, future)

            send_result = await self.ws_manager.send_to_device(device_id, {
                "type":       "action",
                "action":     action,
                "parameters": params,
                "request_id": request_id,
                "task_id":    task_id,
            })

            if send_result != SendResult.OK:
                self.ws_manager.unregister_pending(request_id)
                results.append(f"Step {i} ({action}): device unavailable")
                break

            try:
                response = await asyncio.wait_for(future, timeout=timeout)
                results.append(f"Step {i} ({action}): {response.get('result', 'done')}")
            except asyncio.TimeoutError:
                results.append(f"Step {i} ({action}): timed out after {timeout}s")
            finally:
                self.ws_manager.unregister_pending(request_id)

            await asyncio.sleep(0.5)

        return "✅ Sequence complete:\n" + "\n".join(results)

    # ── Vision task ────────────────────────────────────────────────────────────

    async def _vision_task(self, device_id: str, goal: str, task_id: str) -> str:
        request_id = str(uuid.uuid4())[:8]
        future     = asyncio.get_event_loop().create_future()

        self.ws_manager.register_vision_task(request_id, future,
                                              self._vision_step_handler)

        send_result = await self.ws_manager.send_to_device(device_id, {
            "type":       "vision_task",
            "goal":       goal,
            "task_id":    task_id,
            "request_id": request_id,
            "max_steps":  15,
        })

        if send_result != SendResult.OK:
            self.ws_manager.unregister_pending(request_id)
            return "⚠️ Device unavailable for vision task."

        try:
            result = await asyncio.wait_for(future, timeout=180.0)
            return f"✅ Vision task complete: {result}"
        except asyncio.TimeoutError:
            return "⚠️ Vision task timed out after 3 minutes"
        finally:
            self.ws_manager.unregister_pending(request_id)

    async def _vision_step_handler(self, device_id: str, request_id: str,
                                    step_data: dict) -> dict:
        goal     = step_data.get("goal", "")
        step_num = step_data.get("step", 1)

        prompt = f"""You are controlling a Windows PC to accomplish: {goal}

This is step {step_num}. Analyse the screenshot and decide the single best next action.

Return ONLY valid JSON — no extra text:

Click:    {{"action":"click","parameters":{{"x":500,"y":300}},"reason":"..."}}
Type:     {{"action":"type","parameters":{{"text":"hello"}},"reason":"..."}}
Hotkey:   {{"action":"hotkey","parameters":{{"keys":["ctrl","t"]}},"reason":"..."}}
Scroll:   {{"action":"scroll","parameters":{{"x":960,"y":540,"clicks":-3}},"reason":"..."}}
Open app: {{"action":"open_app","parameters":{{"app":"notepad"}},"reason":"..."}}
Wait:     {{"action":"wait","parameters":{{"ms":1000}},"reason":"..."}}
Done:     {{"action":"done","message":"Goal accomplished: ...","reason":"finished"}}

Screen is 1920x1080. Be precise with coordinates."""

        try:
            response = await self.qwen.chat(
                system_prompt=(
                    "You are a computer vision agent. "
                    "Analyse the screenshot and return ONLY valid JSON action."
                ),
                user_message=prompt,
                temperature=0.1,
            )
            response    = re.sub(r"```(?:json)?", "", response).strip().rstrip("`").strip()
            action_data = json.loads(response)
            logger.info(f"[Vision] Step {step_num}: "
                        f"{action_data.get('action')} — {action_data.get('reason','')}")
            return action_data
        except Exception as e:
            logger.error(f"[Vision] Step analysis error: {e}")
            return {"action": "done", "message": f"Vision error: {e}"}
