"""Update-notification replay tests for panasonic_awhe.

The AW-HE/UE/UN cameras push state changes by dialing back to a registered
TCP port with container-framed notifications (push: {type: tcp_listener}).
These tests build spec-derived container frames (HD/4K Integrated Camera
Interface Specifications v1.12, section 4: 22-byte reserve + 2-byte
big-endian size + 4-byte reserve + [CR][LF]<response>[CR][LF] + 24-byte
reserve, size = payload + 8), unwrap them with a minimal reimplementation of
the platform's struct_frame parser driven by the driver's own frame_parser
declaration, and replay the payloads against the declared response rules —
keeping the repo CI self-contained per the driver-test policy.

Frame provenance: spec-derived (v1.12, Apr 2020; the AW-UE160 spec of
Mar 2025 documents the identical container). The size-field byte order is
big-endian, confirmed against a hardware-observed open-source implementation
of this protocol.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_driver() -> dict:
    with open(REPO_ROOT / "cameras" / "panasonic_awhe.avcdriver", encoding="utf-8") as f:
        return yaml.safe_load(f)


def coerce(raw: str, var_type: str):
    if var_type == "integer":
        return int(raw)
    if var_type in ("float", "number"):
        return float(raw)
    if var_type == "boolean":
        return raw.lower() in ("1", "true", "yes", "on")
    return raw


def wrap_frame(response: str, cfg: dict) -> bytes:
    """Build the camera's notification container per the spec (and the
    driver's own frame_parser declaration, so a driver edit that breaks the
    contract fails here)."""
    payload = b"\r\n" + response.encode() + b"\r\n"
    length_value = len(payload) - cfg["length_adjust"]
    return (
        bytes(cfg["header_reserve"])
        + length_value.to_bytes(cfg["length_size"], cfg["length_endian"])
        + bytes(cfg["mid_reserve"])
        + payload
        + bytes(cfg["trailer_reserve"])
    )


def unwrap_frame(frame: bytes, cfg: dict) -> bytes:
    """Minimal struct_frame parse: extract the payload the platform would."""
    start = cfg["header_reserve"]
    length_field = frame[start : start + cfg["length_size"]]
    payload_len = (
        int.from_bytes(length_field, cfg["length_endian"]) + cfg["length_adjust"]
    )
    payload_start = start + cfg["length_size"] + cfg["mid_reserve"]
    total = payload_start + payload_len + cfg["trailer_reserve"]
    assert len(frame) == total, (len(frame), total)
    return frame[payload_start : payload_start + payload_len]


def dispatch(driver: dict, text: str) -> dict:
    """Apply the first matching response rule; return {state: value}."""
    state_vars = driver.get("state_variables", {})
    for resp in driver.get("responses", []):
        pattern = resp.get("match") or resp.get("pattern")
        if not pattern:
            continue
        m = re.search(pattern, text)
        if not m:
            continue
        out = {}
        for state_key, expr in (resp.get("set") or {}).items():
            var_type = state_vars.get(state_key, {}).get("type", "string")
            if isinstance(expr, str) and expr.startswith("$"):
                raw = m.group(int(expr[1:]))
                if raw is None:
                    continue
                out[state_key] = coerce(raw, var_type)
            else:
                out[state_key] = coerce(str(expr), var_type)
        return out
    return {}


def dispatch_frame(driver: dict, response: str) -> dict:
    """Full pipeline: container-wrap per the driver's frame_parser, unwrap
    (platform framing), split/strip (platform dispatch), match rules."""
    cfg = driver["push"]["frame_parser"]
    payload = unwrap_frame(wrap_frame(response, cfg), cfg)
    # Platform dispatch splits pushed data on the driver delimiter (default
    # CR) and strips each part before matching.
    results: dict = {}
    for part in payload.split(b"\r"):
        text = part.decode().strip()
        if text:
            results.update(dispatch(driver, text))
    return results


# Spec-derived notification payloads (each is the documented response token
# the camera relays in an update notification).
CASES = [
    ("p1", {"power": 1}),
    ("p0", {"power": 0}),
    ("p3", {"power": 3}),  # transitioning
    ("aPC80008000", {"pan_position": "8000", "tilt_position": "8000"}),
    ("gz555", {"zoom_position": "555"}),
    ("axz80A", {"zoom_position": "80A"}),
    ("gfAAA", {"focus_position": "AAA"}),
    ("d11", {"focus_auto": True}),
    ("giFFF", {"iris_position": "FFF"}),
    ("d30", {"iris_auto": False}),
    ("s07", {"preset_last": 7}),
    ("q07", {"preset_last": 7}),  # recall completion notice
    ("uPVS250", {"preset_speed": 250}),
    ("dA1", {"r_tally": True}),
    ("tAE0", {"tally_input_enabled": False}),
    ("wLC1", {"wireless_remote_enabled": True}),
    ("sWZ0", {"zoom_linked_pt_speed": False}),
    ("Event session:2", {"event_sessions": 2}),
    # Notifications the driver deliberately doesn't track (image-quality CGI
    # tokens) fall through without writing anything.
    ("OSJ:56:1", {}),
]


@pytest.mark.parametrize("response, expected", CASES)
def test_awhe_notification_frame(response, expected):
    driver = load_driver()
    result = dispatch_frame(driver, response)
    for key, want in expected.items():
        assert result.get(key) == want, (response, key, result)
    assert set(result) == set(expected), (response, result)


def test_awhe_push_block():
    driver = load_driver()
    push = driver["push"]
    assert push["type"] == "tcp_listener"
    assert push["port"] == "{notify_port}"
    assert driver["default_config"]["notify_port"] == 31004
    # Spec section 4.2 container: 22 + 2 + 4 + payload + 24, size = payload+8
    frame = push["frame_parser"]
    assert frame["type"] == "struct_frame"
    assert frame["header_reserve"] == 22
    assert frame["length_size"] == 2
    assert frame["length_endian"] == "big"
    assert frame["length_adjust"] == -8
    assert frame["mid_reserve"] == 4
    assert frame["trailer_reserve"] == 24


def test_awhe_registration_commands():
    driver = load_driver()
    push = driver["push"]
    commands = driver["commands"]
    start = commands[push["register"]]["path"]
    stop = commands[push["unregister"]]["path"]
    # Spec section 4.1.1 registration CGI, port templated to the listener.
    assert start == "/cgi-bin/event?connect=start&my_port={listener_port}&uid=0"
    assert stop == "/cgi-bin/event?connect=stop&my_port={listener_port}&uid=0"
    # Session-count read-back (spec 4.1.2).
    assert commands["query_event_sessions"]["path"] == (
        "/cgi-bin/man_session?command=get"
    )


def test_awhe_gates_on_platform_with_tcp_listener():
    driver = load_driver()
    assert driver["min_platform_version"] == "0.23.0"
