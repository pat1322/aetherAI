"""
AetherAI — Automation Agent  (Stage 5 — patch 11)

FIX 6  Vision handler memory leak: when send_to_device() fails after
       register_vision_task(), the _vision_handlers dict entry was not
       cleaned up. unregister_pending() only clears _pending and
       _pending_ts. Added explicit pop of _vision_handlers on early exit.
"""

import asyncio
import base64
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from agents import BaseAgent
from utils.websocket_manager import SendResult

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

ACTION_TIMEOUTS: dict[str, float] = {
    "open_app":              25.0,
    "new_file":              25.0,
    "run_command":           30.0,
    "type":                  40.0,
    "type_special":          40.0,
    "screenshot_and_return": 15.0,
    "navigate_chrome":       15.0,
    "calculator_input":      10.0,
    "open_folder":           10.0,
    "list_files":            10.0,
    "find_and_open_file":    20.0,
    "click_button":          10.0,
    "window_type":           15.0,
    "close_window":          10.0,
    "focus_window":           8.0,
    "wait":                   5.0,
    "hotkey":                 8.0,
    "click":                  8.0,
    "default":               15.0,
}


def _normalise(parameters: dict) -> dict:
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

        device_id      = devices[0]
        effective_goal = goal or context

        if mode == "vision" or (effective_goal and not action and not sequence and len(effective_goal) > 5):
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
        future     = asyncio.get_running_loop().create_future()
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

            if action == "screenshot_and_return":
                img_b64 = response.get("image_base64", "")
                if img_b64:
                    try:
                        img_bytes = base64.b64decode(img_b64)
                        fname     = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                        fpath     = OUTPUT_DIR / fname
                        fpath.write_bytes(img_bytes)
                        logger.info(f"[AutomationAgent] Screenshot saved: {fpath}")
                        return f"✅ screenshot_and_return: done\nSaved as output/{fname}"
                    except Exception as e:
                        logger.error(f"[AutomationAgent] Screenshot save failed: {e}")
                        return f"✅ screenshot_and_return: done (save failed: {e})"
                return "✅ screenshot_and_return: done (no image data received)"

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
            future     = asyncio.get_running_loop().create_future()
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
        future     = asyncio.get_running_loop().create_future()

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
            # FIX 6: unregister_pending only clears _pending/_pending_ts,
            # NOT _vision_handlers — must explicitly clean that up too.
            self.ws_manager.unregister_pending(request_id)
            self.ws_manager._vision_handlers.pop(request_id, None)
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
        goal      = step_data.get("goal", "")
        step_num  = step_data.get("step", 1)
        img_b64   = step_data.get("image_base64", "")

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
            if img_b64:
                response = await self.qwen.chat_with_image(
                    system_prompt=(
                        "You are a computer vision agent. "
                        "Analyse the screenshot and return ONLY valid JSON action."
                    ),
                    user_message=prompt,
                    image_base64=img_b64,
                    temperature=0.1,
                )
            else:
                logger.warning(f"[Vision] Step {step_num}: no image in step_data")
                response = await self.qwen.chat(
                    system_prompt=(
                        "You are a computer vision agent. "
                        "No screenshot available. Return a safe wait action."
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
