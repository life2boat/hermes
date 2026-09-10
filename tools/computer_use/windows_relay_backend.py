import os
import sys
import json
import time
import uuid
import subprocess
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .backend import ComputerUseBackend, CaptureResult, UIElement, ActionResult

logger = logging.getLogger(__name__)

class WindowsRelayBackend(ComputerUseBackend):
    def __init__(self):
        self.local_app_data = ""
        self.token = ""
        self.req_dir = None
        self.resp_dir = None
        self.last_elements = []

    def start(self) -> None:
        try:
            # Run powershell.exe to get LOCALAPPDATA from Windows side.
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", "Write-Output $env:LOCALAPPDATA"],
                capture_output=True, text=True, check=True
            )
            windows_appdata = result.stdout.strip()
            
            # Convert Windows path to WSL path
            result = subprocess.run(
                ["wslpath", "-u", windows_appdata],
                capture_output=True, text=True, check=True
            )
            self.local_app_data = result.stdout.strip()
            
            base_dir = Path(self.local_app_data) / "Hermes" / "computer-use"
            self.req_dir = base_dir / "relay_ipc" / "req"
            self.resp_dir = base_dir / "relay_ipc" / "resp"
            
            token_path = base_dir / "relay_token.txt"
            if not token_path.exists():
                raise FileNotFoundError(f"Token file not found: {token_path}")
                
            with open(token_path, "r", encoding="utf-8") as f:
                self.token = f.read().strip()
                
            logger.info("WindowsRelayBackend started successfully.")
            
        except Exception as e:
            logger.error(f"Failed to start WindowsRelayBackend: {e}")
            raise

    def stop(self) -> None:
        pass

    def is_available(self) -> bool:
        return bool(self.token)

    def _send_request(self, verb: str, args: dict = None) -> dict:
        request_id = str(uuid.uuid4())
        req_data = {
            "request_id": request_id,
            "token": self.token,
            "verb": verb,
            "args": args or {}
        }
        
        req_file = self.req_dir / f"{request_id}.json"
        temp_file = self.req_dir / f"{request_id}.tmp"
        
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(req_data, f)
        os.rename(temp_file, req_file)
        
        resp_file = self.resp_dir / f"{request_id}.json"
        timeout = 15.0
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if resp_file.exists():
                try:
                    with open(resp_file, "r", encoding="utf-8") as f:
                        resp_data = json.load(f)
                    resp_file.unlink(missing_ok=True)
                    req_file.unlink(missing_ok=True)
                    
                    if resp_data.get("status") == "error":
                        raise RuntimeError(f"Relay error: {resp_data.get('error')}")
                    return resp_data.get("data", {})
                except json.JSONDecodeError:
                    pass # Maybe file is not fully written yet
            time.sleep(0.1)
            
        req_file.unlink(missing_ok=True)
        raise TimeoutError(f"Request {request_id} timed out")

    def capture(self, mode: str = "som", app: Optional[str] = None) -> CaptureResult:
        data = self._send_request("inspect", {"mode": mode, "app": app})
        
        width = data.get("width", 1920)
        height = data.get("height", 1080)
        png_b64 = data.get("screenshot") or data.get("png_b64")
        
        elements = []
        raw_elements = data.get("elements", [])
        for i, el in enumerate(raw_elements):
            index = el.get("index", i + 1)
            bounds_raw = el.get("bounds", [0, 0, 0, 0])
            if isinstance(bounds_raw, dict):
                bounds = (bounds_raw.get("x", 0), bounds_raw.get("y", 0), bounds_raw.get("w", 0), bounds_raw.get("h", 0))
            else:
                bounds = tuple(bounds_raw)
                
            element = UIElement(
                index=index,
                role=el.get("role", "unknown"),
                label=el.get("label", el.get("name", "")),
                bounds=bounds,
                app=el.get("app", ""),
                pid=el.get("pid", 0),
                window_id=el.get("window_id", 0),
                attributes=el.get("attributes", {})
            )
            elements.append(element)
            
        self.last_elements = elements
        
        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=elements,
            app=data.get("app", ""),
            window_title=data.get("window_title", ""),
            png_bytes_len=len(png_b64) if png_b64 else 0
        )

    def click(
        self,
        *,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        args = {
            "button": button,
            "click_count": click_count,
            "modifiers": modifiers or []
        }
        
        if element is not None:
            args["element_id"] = element
        if x is not None and y is not None:
            args["x"] = x
            args["y"] = y
            
        try:
            self._send_request("click", args)
            return ActionResult(ok=True, action="click", message="Clicked successfully")
        except Exception as e:
            return ActionResult(ok=False, action="click", message=str(e))

    def drag(
        self,
        *,
        from_element: Optional[int] = None,
        to_element: Optional[int] = None,
        from_xy: Optional[Tuple[int, int]] = None,
        to_xy: Optional[Tuple[int, int]] = None,
        button: str = "left",
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        args = {
            "button": button,
            "modifiers": modifiers or [],
            "from_element": from_element,
            "to_element": to_element,
            "from_xy": from_xy,
            "to_xy": to_xy
        }
        try:
            self._send_request("drag", args)
            return ActionResult(ok=True, action="drag", message="Dragged successfully")
        except Exception as e:
            return ActionResult(ok=False, action="drag", message=str(e))

    def scroll(
        self,
        *,
        direction: str,
        amount: int = 3,
        element: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        modifiers: Optional[List[str]] = None,
    ) -> ActionResult:
        args = {
            "direction": direction,
            "amount": amount,
            "element_id": element,
            "x": x,
            "y": y,
            "modifiers": modifiers or []
        }
        try:
            self._send_request("scroll", args)
            return ActionResult(ok=True, action="scroll", message="Scrolled successfully")
        except Exception as e:
            return ActionResult(ok=False, action="scroll", message=str(e))

    def type_text(self, text: str) -> ActionResult:
        try:
            self._send_request("type_text", {"text": text})
            return ActionResult(ok=True, action="type_text", message="Typed text successfully")
        except Exception as e:
            return ActionResult(ok=False, action="type_text", message=str(e))

    def key(self, keys: str) -> ActionResult:
        try:
            self._send_request("key", {"keys": keys})
            return ActionResult(ok=True, action="key", message="Pressed keys successfully")
        except Exception as e:
            return ActionResult(ok=False, action="key", message=str(e))

    def list_apps(self) -> List[Dict[str, Any]]:
        try:
            data = self._send_request("list_windows")
            return data.get("windows", [])
        except Exception:
            return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        try:
            self._send_request("focus_app", {"app": app, "raise_window": raise_window})
            return ActionResult(ok=True, action="focus_app", message="Focused app successfully")
        except Exception as e:
            return ActionResult(ok=False, action="focus_app", message=str(e))

    def set_value(self, value: str, element: Optional[int] = None) -> ActionResult:
        try:
            self._send_request("set_value", {"value": value, "element_id": element})
            return ActionResult(ok=True, action="set_value", message="Set value successfully")
        except Exception as e:
            return ActionResult(ok=False, action="set_value", message=str(e))

