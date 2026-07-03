"""Driver + simulator tests for lg_sicp (LG SICP text protocol).

No LG signage hardware on hand, so correctness is proven two ways:
byte-exact framing assertions, and a **dual-proof round trip** wiring the
real driver to the real simulator over an in-memory transport that speaks
SICP — the sim renders ack frames, the driver's frame parser scans and
parses them, and results are asserted on both sides (same approach as
test_samsung_mdc.py).

Covers the v2.0.0 Python conversion (from .avcdriver), including
regression tests for the two YAML gaps that forced it:
  - hex numeric read-back (the YAML driver's volume was broken on real
    hardware — a reply of "1e" could never coerce to 30);
  - code<->label input / picture-mode mapping, published as {value, label}
    picker options behind ``options_state``.
And the protocol quirks that are easy to get wrong:
  - Set IDs travel as HEX (display 10 answers to "0A");
  - mute is inverted on the wire (ke 00 = muted);
  - kg (contrast) and mg (backlight) acks are identical ("g ...") —
    only the in-flight correlation queue can tell them apart;
  - a dx (picture mode) ack STARTS with the frame terminator 'x'.

Loads the driver + simulator with the ``server.*`` / ``simulator.*``
imports stubbed so the community CI stays self-contained (conftest.py
rolls the stubs back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "lg_sicp.py"
SIM_PATH = REPO_ROOT / "displays" / "lg_sicp_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value

    def set_batch(self, updates, **_):
        self.data.update(updates)


class _FakeEvents:
    def __init__(self) -> None:
        self.emitted: list[str] = []

    async def emit(self, name, *args, **kwargs):
        self.emitted.append(name)


class _FakeBaseDriver:
    """Functional stand-in for the platform BaseDriver child-entity API + the
    transport-building connect()/disconnect() the driver relies on via super().
    """

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._children: dict[str, dict[int, dict]] = {}
        self._connected = False
        self.disconnect_calls = 0

    # -- transport lifecycle (mirrors real BaseDriver.connect/disconnect) --

    async def connect(self) -> None:
        self.transport = await _FakeTCPTransport.create(
            host=self.config.get("host", ""),
            port=self.config.get("port", 9761),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
        )
        self._connected = True
        self.set_state("connected", True)

    async def disconnect(self) -> None:
        if self.transport:
            await self.transport.close()
            self.transport = None
        self._connected = False
        self.set_state("connected", False)

    # -- child entities --

    def _eff_schema(self, ctype: str) -> dict:
        schema = dict(self.DRIVER_INFO["child_entity_types"][ctype]["state_variables"])
        schema.setdefault("online", {"type": "boolean"})
        schema.setdefault("label", {"type": "string"})
        return schema

    @staticmethod
    def _default_for(var_def: dict):
        """Mirror BaseDriver._default_for_var_def so an unset prop starts at
        the platform's default (enum -> first value, bool -> False, etc.)."""
        vt = var_def.get("type", "string")
        if vt == "boolean":
            return False
        if vt == "integer":
            return int(var_def.get("min", 0) or 0)
        if vt in ("number", "float"):
            return float(var_def.get("min", 0) or 0)
        if vt == "enum":
            values = var_def.get("values", [])
            return values[0] if values else ""
        return ""

    def register_child(self, ctype, lid, initial_state=None) -> None:
        bucket = self._children.setdefault(ctype, {})
        if lid in bucket:
            return  # idempotent
        schema = self._eff_schema(ctype)
        ov = dict(initial_state or {})
        for prop in ov:
            if prop not in schema:
                raise ValueError(f"unknown child prop {prop!r}")
        st: dict = {}
        for prop, var_def in schema.items():
            if prop == "online":
                st[prop] = ov.get("online", True)
            elif prop == "label":
                st[prop] = ov.get("label", "")
            elif prop in ov:
                st[prop] = ov[prop]
            else:
                st[prop] = self._default_for(var_def)
        bucket[lid] = st

    def deregister_child(self, ctype, lid) -> None:
        self._children.get(ctype, {}).pop(lid, None)

    def is_child_registered(self, ctype, lid) -> bool:
        return lid in self._children.get(ctype, {})

    def list_children(self, ctype) -> list:
        return sorted(self._children.get(ctype, {}).keys())

    def get_child_state(self, ctype, lid) -> dict:
        return dict(self._children.get(ctype, {}).get(lid, {}))

    def set_child_state_batch(self, ctype, lid, updates) -> None:
        schema = self._eff_schema(ctype)
        for prop in updates:
            if prop not in schema:
                raise ValueError(f"unknown child prop {prop!r}")
        if lid not in self._children.get(ctype, {}):
            raise ValueError(f"child {ctype}/{lid} not registered")
        self._children[ctype][lid].update(updates)

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)

    def _handle_transport_disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False


class _FakeSimState:
    def __init__(self, initial) -> None:
        self.data = dict(initial)

    def get(self, key, default=None):
        return self.data.get(key, default)


class _FakeTCPSimulator:
    """Stand-in for simulator.tcp_simulator.TCPSimulator."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self.state = _FakeSimState(self.SIMULATOR_INFO.get("initial_state", {}))

    def set_state(self, key, value) -> None:
        self.state.data[key] = value


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport processes the request but DROPS the reply.
_SWALLOW = False
# The driver's SICP frame parser, wired after the driver module loads so the
# fake transport delivers complete ack frames exactly as the real TCPTransport
# does.
_PARSE = None


class _FakeTCPTransport:
    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect, **_):
        return cls(on_data, on_disconnect)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if _SWALLOW or not resp:
            return
        # Mirror the real transport: apply the driver's frame parser,
        # delivering each complete ack frame to on_data.
        buf = bytes(resp)
        while True:
            frame, buf = _PARSE(buf)
            if frame is None:
                break
            await self.on_data(bytes(frame))

    async def close(self) -> None:
        self.connected = False


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["server.drivers.base"] = base

    frame_parsers = ModuleType("server.transport.frame_parsers")

    class CallableFrameParser:  # referenced in the class body, never called here
        def __init__(self, *a, **k):
            pass

    class FrameParser:
        pass

    frame_parsers.CallableFrameParser = CallableFrameParser
    frame_parsers.FrameParser = FrameParser
    sys.modules["server.transport.frame_parsers"] = frame_parsers

    logger = ModuleType("server.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["server.utils.logger"] = logger

    sim_pkg = ModuleType("simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["simulator"] = sim_pkg
    sim_tcp = ModuleType("simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    sys.modules["simulator.tcp_simulator"] = sim_tcp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("lg_sicp_under_test", DRIVER_PATH)
SIM = _load("lg_sicp_sim_under_test", SIM_PATH)
_PARSE = DRV._parse_sicp_frame

_build_sicp_command = DRV._build_sicp_command
_parse_sicp_frame = DRV._parse_sicp_frame


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.LgSicpSimulator("sim1", sim_config or {"set_ids": "1"})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.9", "port": 9761, "display_ids": "1", "poll_interval": 0}
    cfg.update(driver_overrides or {})
    driver = DRV.LGSICPDriver("lg1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Request framing ─────────────────────────────────────────────────────────

def test_build_command_basic():
    assert _build_sicp_command("ka", 1, "01") == b"ka 01 01\r"
    assert _build_sicp_command("kf", 1, "FF") == b"kf 01 FF\r"


def test_build_command_set_id_is_hex():
    # OSD Set ID 10 travels as hex 0A (manual: 1..1000 = 01H..3E8H) — a
    # driver that sent the decimal literal "10" would address Set ID 16.
    assert _build_sicp_command("ka", 10, "01") == b"ka 0A 01\r"
    assert _build_sicp_command("ka", 255, "01") == b"ka FF 01\r"
    assert _build_sicp_command("ka", 1000, "01") == b"ka 3E8 01\r"


def test_parse_frame_complete():
    frame, rest = _parse_sicp_frame(b"a 01 OK01x")
    assert frame == b"a 01 OK01x"
    assert rest == b""


def test_parse_frame_partial_kept():
    frame, rest = _parse_sicp_frame(b"a 01 OK0")
    assert frame is None
    assert rest == b"a 01 OK0"


def test_parse_frame_dx_ack_leads_with_x():
    # The dx ack's Cmd2 is itself an 'x' — split-on-'x' framing (what the
    # old YAML driver's delimiter did) would lose it.
    frame, rest = _parse_sicp_frame(b"x 01 OK01x")
    assert frame == b"x 01 OK01x"
    assert rest == b""


def test_parse_frame_multiple_and_garbage():
    buf = b"\x00noise a 01 OK01xf 01 OK1ex"
    frame1, rest = _parse_sicp_frame(buf)
    assert frame1 == b"a 01 OK01x"
    frame2, rest = _parse_sicp_frame(rest)
    assert frame2 == b"f 01 OK1ex"
    assert rest == b""


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_and_platform_floor():
    info = DRV.LGSICPDriver.DRIVER_INFO
    assert info["version"] == "2.0.0"
    # Child entities + child-prop cloud tiers are the hard runtime need.
    assert info["min_platform_version"] == "0.13.0"


def test_child_entity_type_declared():
    types = DRV.LGSICPDriver.DRIVER_INFO["child_entity_types"]
    assert set(types) == {"display"}
    disp = types["display"]
    assert disp["id_format"]["type"] == "integer"
    assert disp["id_format"]["min"] == 1
    assert disp["id_format"]["max"] == 1000
    sv = disp["state_variables"]
    # reserved props must NOT be declared by the driver.
    assert "online" not in sv and "label" not in sv
    # operationally-hot props ride the high cloud tier.
    for hot in ("power", "input", "volume", "mute", "screen_off", "signal"):
        assert sv[hot]["cloud_priority"] == "high"
    # picture + health props ride the low tier.
    for cold in (
        "brightness", "contrast", "sharpness", "color", "tint",
        "color_temperature", "backlight", "picture_mode", "aspect_ratio",
        "energy_saving", "key_lock", "temperature", "usage_hours",
        "serial_number", "software_version",
    ):
        assert sv[cold]["cloud_priority"] == "low"


def test_commands_use_child_id():
    cmds = DRV.LGSICPDriver.DRIVER_INFO["commands"]
    for cmd in (
        "power_on", "power_off", "set_input", "set_volume", "mute_on",
        "mute_off", "screen_off", "screen_on", "set_brightness",
        "set_contrast", "set_sharpness", "set_color", "set_tint",
        "set_color_temperature", "set_backlight", "set_picture_mode",
        "set_aspect_ratio", "set_energy_saving", "remote_lock_on",
        "remote_lock_off", "send_key", "raw_command",
    ):
        assert cmds[cmd]["params"]["display"]["type"] == "child_id"
        assert cmds[cmd]["params"]["display"]["child_type"] == "display"
    # whole-chain actions take no target.
    for cmd in ("all_on", "all_off", "refresh"):
        assert cmds[cmd]["params"] == {}


def test_param_bounds_match_manual():
    cmds = DRV.LGSICPDriver.DRIVER_INFO["commands"]
    # Volume/brightness/etc are 00-64 hex = 0-100; sharpness is 00-32 = 0-50.
    for cmd in (
        "set_volume", "set_brightness", "set_contrast", "set_color",
        "set_tint", "set_color_temperature", "set_backlight",
    ):
        assert cmds[cmd]["params"]["level"]["min"] == 0
        assert cmds[cmd]["params"]["level"]["max"] == 100
    assert cmds["set_sharpness"]["params"]["level"]["max"] == 50
    assert cmds["send_key"]["params"]["code"]["pattern"] == "^[0-9A-Fa-f]{2}$"
    assert cmds["raw_command"]["params"]["command"]["pattern"] == "^[a-z]{2}$"


def test_pickers_declared():
    cmds = DRV.LGSICPDriver.DRIVER_INFO["commands"]
    assert cmds["set_input"]["params"]["input"]["options_state"] == "input_options"
    assert (
        cmds["set_picture_mode"]["params"]["mode"]["options_state"]
        == "picture_mode_options"
    )


def test_discovery_probe_and_actions_present():
    info = DRV.LGSICPDriver.DRIVER_INFO
    probe = info["discovery"]["tcp_probe"]
    assert probe["port"] == 9761
    assert probe["send_ascii"] == "ka 01 FF\r"
    assert probe["expect"] == "a 01 OK"
    assert probe["extract_manufacturer"] == "LG"
    assert 9761 in info["discovery"]["port_open"]
    action_ids = {a["id"] for a in info["actions"]}
    assert {"all_on", "all_off", "refresh"} <= action_ids


def test_picker_options_published_on_connect():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            inputs = json.loads(driver.get_state("input_options"))
            assert {"value": "90", "label": "HDMI 1"} in inputs
            assert {"value": "C0", "label": "DisplayPort"} in inputs
            modes = json.loads(driver.get_state("picture_mode_options"))
            assert {"value": "01", "label": "Standard"} in modes
            assert {"value": "00", "label": "Vivid"} in modes
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Roster from config ──────────────────────────────────────────────────────

def test_roster_from_config():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1,3,5"},
            driver_overrides={"display_ids": "1,3,5"},
        )
        await driver.connect()
        try:
            assert driver.list_children("display") == [1, 3, 5]
            assert driver.get_state("display_count") == 3
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_roster_reconcile_drops_removed():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1,2,3"},
            driver_overrides={"display_ids": "1,2,3"},
        )
        await driver.connect()
        try:
            assert driver.list_children("display") == [1, 2, 3]
            # Operator edits the config down to a single display.
            driver.config["display_ids"] = "1"
            driver._reconcile_displays()
            assert driver.list_children("display") == [1]
            assert driver.get_state("display_count") == 1
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_bad_display_ids_fall_back_to_one():
    driver, _ = asyncio.run(_make_pair(driver_overrides={"display_ids": "abc,,2000"}))
    # 2000 is out of range (max 1000), "abc"/"" are junk -> falls back to [1].
    assert driver._parse_display_ids() == [1]


# ── Round trips: command mutates the sim, ack/poll updates the child ────────

def test_power_input_volume_mute_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("power_on", {"display": 1})
            await driver.send_command("set_input", {"display": 1, "input": "90"})
            await driver.send_command("set_volume", {"display": 1, "level": 30})
            await driver.send_command("mute_on", {"display": 1})
            await driver.send_command("screen_off", {"display": 1})
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["power"] == "on"
            assert child["input"] == "HDMI 1"
            assert child["volume"] == 30
            assert child["mute"] is True
            assert child["screen_off"] is True
            # the sim really took the values (0x1E went over the wire)
            assert sim.state.get("volume") == 30
            assert sim.state.get("mute") is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_hex_volume_readback_regression():
    # THE bug that forced the Python conversion: a volume reply of "1e"
    # (= 30) could never coerce in the YAML driver's base-10 parsing. The
    # sim reports its state in protocol-accurate hex; the driver must
    # store the decimal value.
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("power", "on")
            sim.set_state("volume", 30)     # reads back as "1E"
            sim.set_state("brightness", 100)  # reads back as "64"
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["volume"] == 30
            assert child["brightness"] == 100
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_set_ack_updates_state_without_poll():
    # An OK ack echoes the applied value, so a set alone must update the
    # child (polling only covers out-of-band changes).
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_volume", {"display": 1, "level": 42})
            assert driver.get_child_state("display", 1)["volume"] == 42
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_set_input_by_label_and_code():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_input", {"display": 1, "input": "HDMI 1"})
            assert sim.state.get("input") == "HDMI 1"
            await driver.send_command(
                "set_input", {"display": 1, "input": "displayport"}
            )
            assert sim.state.get("input") == "DisplayPort"
            await driver.send_command("set_input", {"display": 1, "input": "A1"})
            assert sim.state.get("input") == "HDMI 2 / OPS (PC)"
            # unresolvable value: warn, send nothing
            await driver.send_command(
                "set_input", {"display": 1, "input": "component 3"}
            )
            assert sim.state.get("input") == "HDMI 2 / OPS (PC)"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_unsupported_input_code_gets_ng_and_no_state():
    # 55 is a valid two-hex-digit code (forgiving free-text) but this
    # model doesn't support it -> the display answers NG -> no state change.
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_input", {"display": 1, "input": "90"})
            await driver.send_command("set_input", {"display": 1, "input": "55"})
            assert sim.state.get("input") == "HDMI 1"
            assert driver.get_child_state("display", 1)["input"] == "HDMI 1"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_mute_is_inverted_on_the_wire():
    # ke 00 = muted, 01 = unmuted — the exact inversion that made the YAML
    # capture workaround impossible.
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("power", "on")
            sim.set_state("mute", True)
            await driver.poll()
            assert driver.get_child_state("display", 1)["mute"] is True
            await driver.send_command("mute_off", {"display": 1})
            assert sim.state.get("mute") is False
            assert driver.get_child_state("display", 1)["mute"] is False
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_picture_settings_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("power_on", {"display": 1})
            await driver.send_command("set_brightness", {"display": 1, "level": 65})
            await driver.send_command("set_contrast", {"display": 1, "level": 55})
            await driver.send_command("set_sharpness", {"display": 1, "level": 40})
            await driver.send_command("set_color", {"display": 1, "level": 45})
            await driver.send_command("set_tint", {"display": 1, "level": 60})
            await driver.send_command(
                "set_color_temperature", {"display": 1, "level": 35}
            )
            await driver.send_command("set_backlight", {"display": 1, "level": 90})
            await driver.send_command(
                "set_picture_mode", {"display": 1, "mode": "Cinema"}
            )
            await driver.send_command(
                "set_aspect_ratio", {"display": 1, "aspect": "Just Scan"}
            )
            await driver.send_command(
                "set_energy_saving", {"display": 1, "mode": "Maximum"}
            )
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["brightness"] == 65
            assert child["contrast"] == 55
            assert child["sharpness"] == 40
            assert child["color"] == 45
            assert child["tint"] == 60
            assert child["color_temperature"] == 35
            assert child["backlight"] == 90
            assert child["picture_mode"] == "Cinema"
            assert child["aspect_ratio"] == "Just Scan"
            assert child["energy_saving"] == "Maximum"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_picture_mode_by_raw_code_and_dx_ack_framing():
    # Setting by raw dx code exercises the ack whose Cmd2 is 'x' end-to-end
    # ("x 01 OK00x" must survive framing and correlation).
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_picture_mode", {"display": 1, "mode": "00"})
            assert sim.state.get("picture_mode") == "Vivid"
            assert driver.get_child_state("display", 1)["picture_mode"] == "Vivid"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_contrast_backlight_ack_collision():
    # kg (contrast) and mg (backlight) both ack as "g <id> OK<data>x" —
    # only the in-flight correlation queue can route them to the right
    # prop. A naive Cmd2 dispatch would cross them.
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_contrast", {"display": 1, "level": 11})
            await driver.send_command("set_backlight", {"display": 1, "level": 99})
            child = driver.get_child_state("display", 1)
            assert child["contrast"] == 11
            assert child["backlight"] == 99
            # and read-back keeps them apart too
            sim.set_state("power", "on")
            sim.set_state("contrast", 22)
            sim.set_state("backlight", 88)
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["contrast"] == 22
            assert child["backlight"] == 88
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_cinema_zoom_readback():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("power", "on")
            sim.set_state("aspect_ratio", "Cinema Zoom 3")  # reads back as 12
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["aspect_ratio"] == "Cinema Zoom 3"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_key_lock_and_send_key():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("remote_lock_on", {"display": 1})
            assert sim.state.get("key_lock") is True
            assert driver.get_child_state("display", 1)["key_lock"] is True
            await driver.send_command("remote_lock_off", {"display": 1})
            assert driver.get_child_state("display", 1)["key_lock"] is False
            # IR key passthrough acks but models no state.
            await driver.send_command("send_key", {"display": 1, "code": "7C"})
            await driver.send_command("send_key", {"display": 1, "code": "zz"})  # bad
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_raw_command_ack_updates_tracked_state():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("brightness", 77)
            await driver.send_command(
                "raw_command", {"display": 1, "command": "kh", "data": "FF"}
            )
            assert driver.get_child_state("display", 1)["brightness"] == 77
            # An untracked raw command gets NG'd by this sim — no crash,
            # no state change, and correlation stays intact.
            await driver.send_command(
                "raw_command", {"display": 1, "command": "dd", "data": "FF"}
            )
            await driver.send_command(
                "raw_command", {"display": 1, "command": "kh", "data": "FF"}
            )
            assert driver.get_child_state("display", 1)["brightness"] == 77
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_health_block():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # identity reads happen on connect (fy/fz)
            child = driver.get_child_state("display", 1)
            assert child["serial_number"] == "SIM0LG0000001"
            assert child["software_version"] == "03.11.20"
            # temperature / hours ride the powered-on poll (hex payloads)
            await driver.send_command("power_on", {"display": 1})
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["temperature"] == 38     # sim replies "26"
            assert child["usage_hours"] == 1234   # sim replies "4D2"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_signal_check():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("power_on", {"display": 1})
            await driver.poll()
            assert driver.get_child_state("display", 1)["signal"] == "present"
            sim.set_state("signal", "none")
            await driver.poll()
            assert driver.get_child_state("display", 1)["signal"] == "none"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_standby_skips_full_surface():
    # While a display reports "off", only the power query goes out — the
    # picture/health surface refreshes once it comes back on.
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("brightness", 90)
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["power"] == "off"
            assert child["brightness"] == 0  # untouched default
            sim.set_state("power", "on")
            await driver.poll()
            assert driver.get_child_state("display", 1)["brightness"] == 90
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Multi-display routing ───────────────────────────────────────────────────

def test_multiple_displays_are_independent():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1,2"},
            driver_overrides={"display_ids": "1,2"},
        )
        await driver.connect()
        try:
            await driver.send_command("set_volume", {"display": 1, "level": 30})
            await driver.send_command("set_volume", {"display": 2, "level": 80})
            await driver.send_command("power_on", {"display": 2})
            await driver.poll()
            assert driver.get_child_state("display", 1)["volume"] == 30
            assert driver.get_child_state("display", 1)["power"] == "off"
            assert driver.get_child_state("display", 2)["volume"] == 80
            assert driver.get_child_state("display", 2)["power"] == "on"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_set_id_ten_routes_as_hex():
    # The sim keys displays by the wire Set ID parsed as hex — a driver
    # that sent decimal "10" would address Set ID 16, get silence, and
    # this round trip would fail.
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "10"},
            driver_overrides={"display_ids": "10"},
        )
        await driver.connect()
        try:
            await driver.send_command("power_on", {"display": 10})
            await driver.send_command("set_volume", {"display": 10, "level": 25})
            await driver.poll()
            child = driver.get_child_state("display", 10)
            assert child["power"] == "on"
            assert child["volume"] == 25
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_child_id_padded_string_coerced():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # The IDE child picker can hand back a zero-padded id ("0001").
            await driver.send_command("power_on", {"display": "0001"})
            await driver.send_command("set_volume", {"display": "0001", "level": 7})
            child = driver.get_child_state("display", 1)
            assert child["power"] == "on"
            assert child["volume"] == 7
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_all_on_all_off():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1,2,3"},
            driver_overrides={"display_ids": "1,2,3"},
        )
        await driver.connect()
        try:
            await driver.send_command("all_on", {})
            for sid in (1, 2, 3):
                assert driver.get_child_state("display", sid)["power"] == "on"
            await driver.send_command("all_off", {})
            for sid in (1, 2, 3):
                assert driver.get_child_state("display", sid)["power"] == "off"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_absent_display_is_silent_and_pruned():
    async def go():
        # Driver expects displays 1 and 2, but the chain only has display 1.
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1"},
            driver_overrides={"display_ids": "1,2"},
        )
        await driver.connect()
        try:
            # Address the absent display FIRST so its unanswered entry sits
            # at the head of the correlation queue — the next ack must
            # prune past it and still route to display 1.
            await driver.send_command("power_on", {"display": 2})
            await driver.send_command("power_on", {"display": 1})
            assert driver.get_child_state("display", 1)["power"] == "on"
            assert driver.get_child_state("display", 2)["power"] == "off"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_stray_ack_does_not_break_correlation():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # A frame nothing asked for: ignored, queue left intact.
            await driver.on_data_received(b"g 01 OK50x")
            child = driver.get_child_state("display", 1)
            assert child["contrast"] == 0 and child["backlight"] == 0
            await driver.send_command("set_contrast", {"display": 1, "level": 33})
            assert driver.get_child_state("display", 1)["contrast"] == 33
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_broadcast_applies_to_all_with_no_ack():
    # Direct wire-level check of the sim: Set ID 00 writes every display
    # and returns no acknowledgement, per the manual.
    sim = SIM.LgSicpSimulator("sim1", {"set_ids": "1,2"})
    assert sim.handle_command(b"ka 00 01\r") is None
    assert sim.state.get("power") == "on"
    assert sim._displays[2]["power"] == "on"


def test_refresh_children_repolls_roster():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1,2"},
            driver_overrides={"display_ids": "1,2"},
        )
        await driver.connect()
        try:
            await driver.send_command("power_on", {"display": 2})
            # An out-of-band change on display 2 (e.g. the sim UI / remote).
            sim._displays[2]["volume"] = 88
            result = await driver.refresh_children()
            assert result == {"displays": 2}
            assert driver.get_child_state("display", 2)["volume"] == 88
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_status_reflects_ui_driven_change():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # Simulate an out-of-band change on the display (sim UI / remote).
            sim.set_state("power", "on")
            sim.set_state("volume", 15)
            sim.set_state("mute", True)
            sim.set_state("input", "DisplayPort")
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["power"] == "on"
            assert child["volume"] == 15
            assert child["mute"] is True
            assert child["input"] == "DisplayPort"
        finally:
            await driver.disconnect()

    asyncio.run(go())
