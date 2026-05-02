"""
Atlona AT-OME-MS Series Simulator.

Implements a TCP server that mimics the AT-OME-MS family's Telnet protocol:

  - Optionally prompts for `Login: ` / `Password: ` (Telnet Login Mode)
    when the simulator's ``require_auth`` config is true.
  - Accepts the ``OutputMode h|j|p`` switch and emits compact single-line
    JSON when ``OutputMode j`` is active.
  - Implements the subset of Display:* / Audio:* / USBRouting:* /
    Instruments:* / Misc:* commands that the driver issues.
  - Returns ``Unknown command`` for anything it doesn't recognize.

Compact JSON only: the simulator always emits compact JSON regardless of
the OutputMode setting because the driver always sends ``OutputMode j``
on connect. Pretty / human modes aren't exercised by the driver, so they
aren't faithfully simulated.
"""

from __future__ import annotations

import json
import logging
import re

from simulator.tcp_simulator import TCPSimulator

logger = logging.getLogger(__name__)


class AtlonaOmeMsSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "atlona_ome_ms",
        "name": "Atlona AT-OME-MS Series Simulator",
        "category": "switcher",
        "transport": "tcp",
        "default_port": 23,
        # Lines end in \r on the wire, but the simulator's flexible line
        # mode strips \r / \n / \r\n indistinguishably.
        "delimiter": "\r",
        "initial_state": {
            "model": "AT-OME-MS52W",
            "firmware": "1.2.05",
            "matrix_mode": True,
            "active_input": 0,
            "route_0": 0,    # HDBaseT out follows USB-C input
            "route_1": 2,    # HDMI out follows HDMI input
            "display_power": True,
            "volume": 0,
            "mute_hdmi": False,
            "mute_analog": False,
            "audio_source": "digital",
            "usb_mode": "follow",
            "usb_route": 0,
            "input_signal_0": True,
            "input_signal_1": False,
            "input_signal_2": True,
            "input_signal_3": False,
            "input_signal_4": False,
            "temperature": 38.5,
        },
        "controls": [
            {"type": "indicator", "key": "model", "label": "Model"},
            {"type": "indicator", "key": "firmware", "label": "Firmware"},
            {"type": "indicator", "key": "temperature", "label": "Temperature (°C)"},
            {"type": "toggle", "key": "matrix_mode", "label": "Matrix Mode"},
            {"type": "toggle", "key": "display_power", "label": "Display (CEC)"},
            {"type": "toggle", "key": "mute_hdmi", "label": "HDMI Mute"},
            {"type": "toggle", "key": "mute_analog", "label": "Analog Mute"},
            {
                "type": "slider",
                "key": "volume",
                "label": "Volume (dB)",
                "min": -80,
                "max": 0,
            },
        ],
        "delays": {"command_response": 0.005},
    }

    def __init__(self, device_id: str, config: dict | None = None):
        super().__init__(device_id, config)
        self._line_mode = True
        self._auth_required = bool(self.config.get("require_auth", False))
        # Per-shared-client auth state: 0=expect username, 1=expect password,
        # 2=authenticated. The TCPSimulator harness uses a shared slot.
        self._auth_state = 0 if self._auth_required else 2
        self._output_mode = "p"  # device default; driver flips to j on connect

    async def on_client_connected(self, client_id: str) -> bytes | None:
        if self._auth_required:
            self._auth_state = 0
            return b"Login: "
        return None

    def handle_command(self, data: bytes) -> bytes | None:
        line = data.decode("utf-8", errors="replace").strip()

        if self._auth_state == 0:
            self._auth_state = 1
            return b"Password: "
        if self._auth_state == 1:
            self._auth_state = 2
            return b"Welcome!\r\n"

        if not line:
            return None

        # Match dispatch on the canonical (lowercase, colons-only) form.
        method_key = re.sub(r"\s*:\s*", ":", line.lower())
        return self._dispatch(line, method_key)

    # ── Helpers ──

    def _ok(self, method: str, extra: dict | None = None) -> bytes:
        result = {"success": True}
        if extra:
            result.update(extra)
        payload = {
            "result": result,
            "methodreturn": method.lower(),
        }
        return (json.dumps(payload, separators=(",", ":")) + "\r\n").encode("utf-8")

    def _result(self, method: str, result: dict) -> bytes:
        payload = {
            "result": result,
            "methodreturn": method.lower(),
        }
        return (json.dumps(payload, separators=(",", ":")) + "\r\n").encode("utf-8")

    def _unknown(self) -> bytes:
        return b"Unknown command\r\n"

    # ── Dispatch ──

    def _dispatch(self, original: str, method_key: str) -> bytes | None:
        # OutputMode switch — the driver sends `OutputMode j` on connect.
        m = re.match(r"^outputmode\s+([hjp])$", method_key)
        if m:
            self._output_mode = m.group(1)
            return f"OutputMode is {self._output_mode}\r\n".encode("utf-8")

        # ── Misc ──
        if method_key == "misc:model:get":
            return self._result(original, {"model": self._state.get("model")})
        m = re.match(r"^misc:version:get(?:\s+(master|mcu))?$", method_key)
        if m:
            kind = m.group(1) or "master"
            fw = self._state.get("firmware", "1.0.00")
            return self._result(original, {"version": {kind: fw}})

        # ── Display:Matrix:Mode ──
        if method_key == "display:matrix:mode:get":
            return self._result(
                original,
                {
                    "mode": bool(self._state.get("matrix_mode")),
                    "subtype": "matrix" if self._state.get("matrix_mode") else "single",
                },
            )
        m = re.match(r"^display:matrix:mode:set\s+([012])$", method_key)
        if m:
            mode = int(m.group(1))
            self._state["matrix_mode"] = mode != 0
            return self._ok(original)

        # ── Display:Matrix:Set / Get ──
        m = re.match(r"^display:matrix:set\s+(\d+)\s+(\d+)$", method_key)
        if m:
            inp, out = int(m.group(1)), int(m.group(2))
            if not self._state.get("matrix_mode"):
                return b"Command Failure\r\n"
            if 0 <= inp <= 4 and 0 <= out <= 1:
                self._state[f"route_{out}"] = inp
                return self._ok(original)
            return self._unknown()
        m = re.match(r"^display:matrix:get\s+(\d+)$", method_key)
        if m:
            out = int(m.group(1))
            if 0 <= out <= 1:
                inp = int(self._state.get(f"route_{out}", 0))
                return self._result(original, {"input": inp})
            return self._unknown()

        # ── Display:Input ──
        if method_key == "display:input:get":
            inp = int(self._state.get("active_input", 0))
            type_map = {0: "usb-c", 1: "displayport", 2: "hdmi", 3: "hdmi", 4: "airplay"}
            return self._result(
                original,
                {"input": inp, "type": type_map.get(inp, "unknown")},
            )
        m = re.match(r"^display:input:set\s+(\d+)$", method_key)
        if m:
            inp = int(m.group(1))
            self._state["active_input"] = inp
            return self._result(original, {"activeinput": inp})

        # ── Display:Minimal (display power via CEC) ──
        if method_key == "display:minimal:get":
            return self._result(
                original, {"state": bool(self._state.get("display_power"))}
            )
        m = re.match(r"^display:minimal:set\s+([01])$", method_key)
        if m:
            self._state["display_power"] = m.group(1) == "1"
            return self._ok(original)

        # ── Display:InputState:Get ──
        m = re.match(r"^display:inputstate:get\s+(\d+)$", method_key)
        if m:
            inp = int(m.group(1))
            if 0 <= inp <= 4:
                return self._result(
                    original,
                    {
                        "input": inp,
                        "state": bool(self._state.get(f"input_signal_{inp}")),
                    },
                )
            return self._unknown()

        # ── Audio:Volume ──
        if method_key == "audio:volume:get":
            return self._result(
                original,
                {"volume": {"units": "dB", "value": int(self._state.get("volume", 0))}},
            )
        m = re.match(r"^audio:volume:set\s+(-?\d+)$", method_key)
        if m:
            level = max(-80, min(0, int(m.group(1))))
            self._state["volume"] = level
            return self._ok(original)
        m = re.match(r"^audio:volume:(increase|decrease)\s+(\d+)$", method_key)
        if m:
            sign = 1 if m.group(1) == "increase" else -1
            cur = int(self._state.get("volume", 0))
            new = max(-80, min(0, cur + sign * int(m.group(2))))
            self._state["volume"] = new
            return self._result(original, {"volume": new, "success": True})

        # ── Audio:Mute ──
        if method_key == "audio:mute:get":
            return self._result(
                original,
                {
                    "outputmute": {
                        "hdmi": bool(self._state.get("mute_hdmi")),
                        "analog": bool(self._state.get("mute_analog")),
                    }
                },
            )
        m = re.match(r"^audio:mute:set\s+(hdmi|analog)\s+(true|false)$", method_key)
        if m:
            ch = m.group(1)
            val = m.group(2) == "true"
            self._state[f"mute_{ch}"] = val
            return self._ok(original)

        # ── Audio source ──
        if method_key == "audio:getsource":
            return self._result(
                original,
                {"audiosource": self._state.get("audio_source", "digital")},
            )
        m = re.match(r"^audio:setsource\s+(digital|analog)$", method_key)
        if m:
            self._state["audio_source"] = m.group(1)
            return self._ok(original)

        # ── USB routing ──
        if method_key == "usbrouting:mode:get":
            return self._result(
                original, {"mode": self._state.get("usb_mode", "follow")}
            )
        m = re.match(r"^usbrouting:mode:set\s+(follow|manual)$", method_key)
        if m:
            self._state["usb_mode"] = m.group(1)
            return self._ok(original)
        if method_key == "usbrouting:input:get":
            return self._result(
                original, {"input": int(self._state.get("usb_route", 0))}
            )
        m = re.match(r"^usbrouting:input:set\s+(\d+)$", method_key)
        if m:
            inp = int(m.group(1))
            if 0 <= inp <= 4:
                self._state["usb_route"] = inp
                return self._ok(original)
            return self._unknown()

        # ── Instruments ──
        if method_key == "instruments:temperature:get":
            return self._result(
                original,
                {"temperature": float(self._state.get("temperature", 35.0))},
            )

        # ── Platform (no return value modeled) ──
        if method_key in ("platform:restart", "platform:shutdown"):
            return self._ok(original)
        if method_key.startswith("platform:reset"):
            return self._ok(original)

        # ── Quit ──
        if method_key == "quit":
            return b"Goodbye\r\n"

        return self._unknown()
