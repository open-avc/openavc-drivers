"""Driver + simulator tests for samsung_mdc (Samsung MDC binary protocol).

No Samsung MDC hardware on hand, so correctness is proven two ways: byte-exact
frame-helper assertions, and a **dual-proof round trip** wiring the real driver
to the real simulator over an in-memory transport that speaks the MDC binary
protocol — the sim renders response frames, the driver's frame parser strips
and parses them, and results are asserted on both sides (same approach as
test_blackmagic_videohub.py / test_racklink_rlnk.py).

Covers the v1.5.0 first-class adoption:
  - each Set ID is a ``display`` child entity, sized from the ``display_ids``
    config (a single display or a daisy-chained wall);
  - per-display power / volume / mute / input plus the picture settings
    (brightness, contrast, backlight, picture mode, color tone) as child props,
    each set by a child_id command and read back on poll;
  - child_id command params + coercion of a zero-padded picker value;
  - the whole-chain all_on / all_off quick actions;
  - the byte-exact request framing and streaming response parser (kept from the
    original suite).

Loads the driver + simulator with the ``openavc.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the stubs
back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType
from _platform_stubs import (
    CallableFrameParser,
    FrameParser,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "samsung_mdc.py"
SIM_PATH = REPO_ROOT / "displays" / "samsung_mdc_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

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
            port=self.config.get("port", 1515),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
        )
        self._connected = True
        self.set_state("connected", True)
        await self._initial_sync()

    async def _initial_sync(self) -> None:
        pass

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

    def get_child_entity_types(self) -> dict:
        out = {}
        for ct, d in self.DRIVER_INFO.get("child_entity_types", {}).items():
            md = dict(d)
            md["state_variables"] = self._eff_schema(ct)
            out[ct] = md
        return out

    @staticmethod
    def _default_for(var_def: dict):
        """Mirror BaseDriver._default_for_var_def so an unset prop starts at the
        platform's default (enum -> first value, bool -> False, etc.)."""
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

    def set_child_state(self, ctype, lid, prop, value) -> None:
        schema = self._eff_schema(ctype)
        if prop not in schema:
            raise ValueError(f"unknown child prop {prop!r}")
        if lid not in self._children.get(ctype, {}):
            raise ValueError(f"child {ctype}/{lid} not registered")
        self._children[ctype][lid][prop] = value

    def set_child_state_batch(self, ctype, lid, updates) -> None:
        schema = self._eff_schema(ctype)
        for prop in updates:
            if prop not in schema:
                raise ValueError(f"unknown child prop {prop!r}")
        if lid not in self._children.get(ctype, {}):
            raise ValueError(f"child {ctype}/{lid} not registered")
        self._children[ctype][lid].update(updates)

    def set_children_state_batch(self, updates) -> None:
        for ctype, lid, child_updates in updates:
            schema = self._eff_schema(ctype)
            for prop in child_updates:
                if prop not in schema:
                    raise ValueError(f"unknown child prop {prop!r}")
            if lid not in self._children.get(ctype, {}):
                raise ValueError(f"child {ctype}/{lid} not registered")
        for ctype, lid, child_updates in updates:
            self._children[ctype][lid].update(child_updates)

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
    """Stand-in for openavc.simulator.tcp_simulator.TCPSimulator."""

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
# The driver's MDC frame parser, wired after the driver module loads so the
# fake transport strips header/checksum exactly as the real TCPTransport does.
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
        # Mirror the real transport: apply the driver's frame parser, delivering
        # each complete frame (header + checksum stripped) to on_data.
        buf = bytes(resp)
        while True:
            frame, buf = _PARSE(buf)
            if frame is None:
                break
            await self.on_data(bytes(frame))

    async def close(self) -> None:
        self.connected = False


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"openavc.{sub}"] = m
    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["openavc.drivers.base"] = base

    binary_helpers = ModuleType("openavc.transport.binary_helpers")
    binary_helpers.checksum_sum = lambda data, mask=0xFF: sum(data) & mask
    sys.modules["openavc.transport.binary_helpers"] = binary_helpers

    frame_parsers = ModuleType("openavc.transport.frame_parsers")

    frame_parsers.CallableFrameParser = CallableFrameParser
    frame_parsers.FrameParser = FrameParser
    sys.modules["openavc.transport.frame_parsers"] = frame_parsers

    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger

    sim_pkg = ModuleType("openavc.simulator")
    sim_pkg.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc.simulator"] = sim_pkg
    sim_tcp = ModuleType("openavc.simulator.tcp_simulator")
    sim_tcp.TCPSimulator = _FakeTCPSimulator
    sys.modules["openavc.simulator.tcp_simulator"] = sim_tcp

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


DRV = _load("samsung_mdc_under_test", DRIVER_PATH)
SIM = _load("samsung_mdc_sim_under_test", SIM_PATH)
_PARSE = DRV._parse_mdc_frame

_build_mdc_frame = DRV._build_mdc_frame
_parse_mdc_frame = DRV._parse_mdc_frame


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.SamsungMdcSimulator("sim1", sim_config or {"set_ids": "1"})
    _CURRENT_SIM = sim

    cfg = {"host": "10.0.0.9", "port": 1515, "display_ids": "1", "poll_interval": 0}
    cfg.update(driver_overrides or {})
    driver = DRV.SamsungMDCDriver("mdc1", cfg, _FakeState(), _FakeEvents())
    return driver, sim


# ── Request framing (kept from the original suite) ──────────────────────────

def test_build_frame_power_on():
    frame = _build_mdc_frame(0x11, 1, bytes([1]))
    assert frame[0] == 0xAA  # header
    assert frame[1] == 0x11  # command
    assert frame[2] == 1     # display id
    assert frame[3] == 1     # data length
    assert frame[4] == 1     # data: power on


def test_build_frame_checksum():
    frame = _build_mdc_frame(0x11, 1, bytes([1]))
    # checksum = sum of every byte after the header, masked to 0xFF
    expected_cs = (0x11 + 0x01 + 0x01 + 0x01) & 0xFF
    assert frame[-1] == expected_cs


def test_parse_frame_complete():
    frame = _build_mdc_frame(0x11, 1, bytes([1]))
    result, remaining = _parse_mdc_frame(frame)
    assert result is not None
    assert result[0] == 0x11  # command (header + checksum stripped)
    assert remaining == b""


def test_parse_frame_incomplete():
    result, remaining = _parse_mdc_frame(b"\xAA\x11")
    assert result is None
    assert remaining == b"\xAA\x11"


def test_parse_frame_no_header():
    # No 0xAA marker -> parser discards the garbage.
    result, remaining = _parse_mdc_frame(b"\x00\x01\x02")
    assert result is None
    assert remaining == b""


def test_parse_frame_skips_garbage_before_header():
    frame = _build_mdc_frame(0x11, 1, bytes([1]))
    result, remaining = _parse_mdc_frame(b"\x00\xFF" + frame)
    assert result is not None
    assert result[0] == 0x11
    assert remaining == b""


def test_parse_frame_multiple():
    frame1 = _build_mdc_frame(0x11, 1, bytes([1]))
    frame2 = _build_mdc_frame(0x12, 1, bytes([50]))
    msg1, rest = _parse_mdc_frame(frame1 + frame2)
    assert msg1 is not None and msg1[0] == 0x11
    msg2, rest = _parse_mdc_frame(rest)
    assert msg2 is not None and msg2[0] == 0x12
    assert rest == b""


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_bumped():
    assert DRV.SamsungMDCDriver.DRIVER_INFO["version"] == "1.5.1"
    assert DRV.SamsungMDCDriver.DRIVER_INFO["min_platform_version"] == "0.24.0"


def test_child_entity_type_declared():
    types = DRV.SamsungMDCDriver.DRIVER_INFO["child_entity_types"]
    assert set(types) == {"display"}
    disp = types["display"]
    assert disp["id_format"]["type"] == "integer"
    assert disp["id_format"]["min"] == 0
    assert disp["id_format"]["max"] == 254
    sv = disp["state_variables"]
    # reserved props must NOT be declared by the driver.
    assert "online" not in sv and "label" not in sv
    # power / volume / mute / input are the hot operational props.
    for hot in ("power", "volume", "mute", "input"):
        assert sv[hot]["cloud_priority"] == "high"
    # picture settings are the low-priority ones.
    for cold in ("brightness", "contrast", "backlight", "picture_mode", "color_tone"):
        assert sv[cold]["cloud_priority"] == "low"


def test_commands_use_child_id():
    cmds = DRV.SamsungMDCDriver.DRIVER_INFO["commands"]
    for cmd in (
        "power_on", "power_off", "set_volume", "mute_on", "mute_off",
        "set_input", "set_brightness", "set_contrast", "set_backlight",
        "set_picture_mode", "set_color_tone",
    ):
        assert cmds[cmd]["params"]["display"]["type"] == "child_id"
        assert cmds[cmd]["params"]["display"]["child_type"] == "display"
    # whole-chain actions take no target.
    for cmd in ("all_on", "all_off", "refresh"):
        assert cmds[cmd]["params"] == {}


def test_discovery_probe_and_actions_present():
    info = DRV.SamsungMDCDriver.DRIVER_INFO
    probe = info["discovery"]["tcp_probe"]
    assert probe["port"] == 1515
    assert probe["expect_hex"] == "AAFF"
    assert probe["extract_manufacturer"] == "Samsung"
    action_ids = {a["id"] for a in info["actions"]}
    assert {"all_on", "all_off", "refresh"} <= action_ids


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
    driver, _ = asyncio.run(_make_pair(driver_overrides={"display_ids": "abc,,999"}))
    # 999 is out of range, "abc"/"" are junk -> falls back to [1].
    assert driver._parse_display_ids() == [1]


# ── Round trips: command mutates the sim, poll updates the driver's child ────

def test_power_volume_input_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("power_on", {"display": 1})
            await driver.send_command("set_volume", {"display": 1, "level": 42})
            await driver.send_command("set_input", {"display": 1, "input": "hdmi3"})
            await driver.send_command("mute_on", {"display": 1})
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["power"] == "on"
            assert child["volume"] == 42
            assert child["input"] == "hdmi3"
            assert child["mute"] is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_picture_settings_round_trip():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("set_brightness", {"display": 1, "level": 65})
            await driver.send_command("set_contrast", {"display": 1, "level": 55})
            await driver.send_command("set_backlight", {"display": 1, "level": 90})
            await driver.send_command("set_picture_mode", {"display": 1, "mode": "movie"})
            await driver.send_command("set_color_tone", {"display": 1, "tone": "warm2"})
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["brightness"] == 65
            assert child["contrast"] == 55
            assert child["backlight"] == 90
            assert child["picture_mode"] == "movie"
            assert child["color_tone"] == "warm2"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_child_id_padded_string_coerced():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # The IDE child picker can hand back a zero-padded id ("001").
            await driver.send_command("power_on", {"display": "001"})
            await driver.send_command("set_volume", {"display": "001", "level": 7})
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["power"] == "on"
            assert child["volume"] == 7
        finally:
            await driver.disconnect()

    asyncio.run(go())


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


def test_all_on_all_off():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1,2,3"},
            driver_overrides={"display_ids": "1,2,3"},
        )
        await driver.connect()
        try:
            await driver.send_command("all_on", {})
            await driver.poll()
            for sid in (1, 2, 3):
                assert driver.get_child_state("display", sid)["power"] == "on"
            await driver.send_command("all_off", {})
            await driver.poll()
            for sid in (1, 2, 3):
                assert driver.get_child_state("display", sid)["power"] == "off"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_absent_display_does_not_update():
    async def go():
        # Driver expects displays 1 and 2, but the chain only has display 1.
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1"},
            driver_overrides={"display_ids": "1,2"},
        )
        await driver.connect()
        try:
            await driver.send_command("power_on", {"display": 1})
            await driver.send_command("power_on", {"display": 2})  # no ACK, silent
            await driver.poll()
            assert driver.get_child_state("display", 1)["power"] == "on"
            # Display 2 never answered — its child stays at the default.
            assert driver.get_child_state("display", 2)["power"] == "off"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_refresh_children_repolls_roster():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1,2"},
            driver_overrides={"display_ids": "1,2"},
        )
        await driver.connect()
        try:
            # An out-of-band change on display 2 (e.g. the sim UI / front panel).
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
            # Simulate an out-of-band change on the display (e.g. the sim UI).
            sim.set_state("power", "on")
            sim.set_state("volume", 15)
            sim.set_state("mute", True)
            sim.set_state("input", "dp1")
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["power"] == "on"
            assert child["volume"] == 15
            assert child["mute"] is True
            assert child["input"] == "dp1"
        finally:
            await driver.disconnect()

    asyncio.run(go())
