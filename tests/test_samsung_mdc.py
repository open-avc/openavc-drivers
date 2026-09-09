"""Driver + simulator tests for samsung_mdc (Samsung MDC binary protocol).

Correctness is proven two ways: byte-exact frame-helper assertions, and a
**dual-proof round trip** wiring the real driver to the real simulator over an
in-memory transport that speaks the MDC binary protocol — the sim renders
response frames, the driver's frame parser strips and parses them, and results
are asserted on both sides (same approach as test_blackmagic_videohub.py /
test_racklink_rlnk.py).

The v1.6.0 tests below come from a DM75E on the bench, and each one covers a
way the previous version looked healthy while doing nothing:

  - a Samsung display answers only its most recently opened connection and
    goes silent on the others without ever closing them, so ``poll`` awaits
    every reply and raises when the whole chain is mute, and
    ``_liveness_probe`` says the same thing when polling is switched off
    (``_SWALLOW`` is the deaf socket);
  - one Set ID that does not answer is a roster entry with no panel behind it,
    not a dead link — that child goes not_responding and the poll continues;
  - a NAK is a model capability gap, not an error: it drops that command from
    the poll (the DM75E refuses colour tone) and it makes a *command* raise
    instead of silently doing nothing;
  - a response frame whose checksum disagrees is dropped rather than written
    to state;
  - setting volume clears mute and makes the display refuse a mute command for
    about two seconds, so the driver re-reads mute instead of assuming, and
    holds a mute until the window closes rather than failing the second step
    of "set the level, then mute".

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

import pytest
from _platform_stubs import (
    CallableFrameParser,
    FrameParser,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
    default_child_fault_message,
    install_connection_fault_stub,
    is_child_fault_code,
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
    LAST_ERROR_PROPERTY = "last_error"

    @staticmethod
    def child_fault(code: str = "", message: str = "") -> dict:
        """Mirror BaseDriver.child_fault: the three reserved presence keys."""
        if not code:
            return {
                "online": True,
                "offline_reason": None,
                "offline_detail": None,
            }
        if not is_child_fault_code(code):
            raise ValueError(f"{code!r} is not a child fault code")
        return {
            "online": False,
            "offline_reason": code,
            "offline_detail": message or default_child_fault_message(code),
        }

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
        # Reserved presence keys the platform adds to every child entity.
        schema.setdefault("offline_reason", {"type": "string"})
        schema.setdefault("offline_detail", {"type": "string"})
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

    # The driver names a child fault code at module scope.
    install_connection_fault_stub()

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
    assert DRV.SamsungMDCDriver.DRIVER_INFO["version"] == "1.6.0"
    assert DRV.SamsungMDCDriver.DRIVER_INFO["min_platform_version"] == "0.25.0"


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
            await driver.send_command("set_sharpness", {"display": 1, "level": 25})
            # A signage panel takes the signage modes; "movie" is one of the
            # TV-style presets it refuses (see test_picture_mode_refusal).
            await driver.send_command(
                "set_picture_mode", {"display": 1, "mode": "shop_mall_video"}
            )
            await driver.poll()
            child = driver.get_child_state("display", 1)
            assert child["brightness"] == 65
            assert child["contrast"] == 55
            assert child["backlight"] == 90
            assert child["sharpness"] == 25
            assert child["picture_mode"] == "shop_mall_video"
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
            # Display 2 is not on the chain. It used to accept the command
            # silently; now the caller is told, because "nothing happened and
            # nobody said so" is the failure this driver exists to stop.
            with pytest.raises(TimeoutError):
                await driver.send_command("power_on", {"display": 2})
            await driver.poll()
            assert driver.get_child_state("display", 1)["power"] == "on"
            # Display 2 never answered — its child stays at the default and is
            # marked absent rather than drawing green.
            two = driver.get_child_state("display", 2)
            assert two["power"] == "off"
            assert two["online"] is False
            assert two["offline_reason"] == "not_responding"
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




# ── v1.6.0: what a real DM75E does (see the module docstring) ───────────────

def test_poll_raises_when_the_whole_chain_goes_silent():
    """A Samsung display stops answering without closing the socket.

    This is the headline failure: another controller, a discovery probe or a
    vendor tool opens port 1515, the display hands service to that connection,
    and this one receives nothing ever again — with no FIN, no RST, and
    transport.connected still True. poll() has to be the thing that notices,
    which it can only do by awaiting replies.
    """
    global _SWALLOW

    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        driver.REPLY_TIMEOUT_S = 0.05
        await driver.connect()
        try:
            await driver.poll()  # healthy
            _SWALLOW = True  # the display goes deaf
            with pytest.raises(ConnectionError):
                await driver.poll()
        finally:
            _SWALLOW = False
            await driver.disconnect()

    asyncio.run(go())


def test_liveness_probe_raises_on_a_deaf_socket():
    """The same signal with polling switched off (poll_interval 0).

    BaseDriver's watchdog force-drops the transport after consecutive misses,
    so this raising is what gets the connection rebuilt — and a reconnect is
    the only thing that makes the display answer this driver again.
    """
    async def go():
        global _SWALLOW
        driver, sim = await _make_pair()
        driver.REPLY_TIMEOUT_S = 0.05
        await driver.connect()
        try:
            await driver._liveness_probe()  # answers, returns normally
            _SWALLOW = True
            with pytest.raises(TimeoutError):
                await driver._liveness_probe()
        finally:
            _SWALLOW = False
            await driver.disconnect()

    asyncio.run(go())


def test_one_absent_set_id_does_not_condemn_the_link():
    """A roster entry with no panel behind it is a config fact, not an outage.

    Someone types "1,2" for a single display all the time. That must mark the
    missing child and carry on, not flap the whole device offline every cycle.
    """
    async def go():
        driver, sim = await _make_pair(
            sim_config={"set_ids": "1"},
            driver_overrides={"display_ids": "1,2"},
        )
        driver.REPLY_TIMEOUT_S = 0.05
        await driver.connect()
        try:
            await driver.poll()  # must NOT raise
            one = driver.get_child_state("display", 1)
            two = driver.get_child_state("display", 2)
            assert one["online"] is True
            assert one["offline_reason"] is None
            assert two["online"] is False
            assert two["offline_reason"] == "not_responding"
            assert "2" in driver.state.data.get("last_error", "")
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_a_naked_get_is_dropped_from_the_poll():
    """Colour tone is not implemented on a DM75E; it NAKs every time.

    Re-asking forever cost a warning per display per cycle and taught people to
    ignore the log. The first NAK retires that command for that Set ID.
    """
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            key = (1, DRV.CMD_COLOR_TONE)
            assert key in driver._unsupported  # learned on the first poll
            sent: list[int] = []
            original = driver._send_to

            async def spy(display, cmd, data=b""):
                sent.append(cmd)
                await original(display, cmd, data)

            driver._send_to = spy
            await driver.poll()
            assert DRV.CMD_COLOR_TONE not in sent
            assert DRV.CMD_STATUS in sent  # everything else still polled
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_a_refused_command_raises_and_is_recorded():
    """A NAK on a command the user issued must not read as success."""
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError, match="does not support"):
                await driver.send_command(
                    "set_picture_mode", {"display": 1, "mode": "movie"}
                )
            assert "picture mode" in driver.state.data.get("last_error", "")
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_an_unknown_enum_value_refuses_instead_of_no_opping():
    """A typo in a macro used to be indistinguishable from a working step."""
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError, match="Unknown input source"):
                await driver.send_command(
                    "set_input", {"display": 1, "input": "hdmi9"}
                )
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_frame_with_a_bad_checksum_is_rejected():
    """0xAA is a legal data and checksum byte, so a resync can land mid-frame.

    Without verification the parser hands on plausible garbage — a volume of
    170, an input that decodes to nothing — and writes it to state.
    """
    good = bytes([0xAA, 0xFF, 0x01, 0x03, 0x41, 0x11, 0x01])
    good += bytes([sum(good[1:]) & 0xFF])
    frame, rest = _parse_mdc_frame(good)
    assert frame is not None and rest == b""

    bad = good[:-1] + bytes([(good[-1] + 1) & 0xFF])
    frame, rest = _parse_mdc_frame(bad)
    assert frame is None  # dropped, not handed on


def test_parser_resyncs_past_a_corrupt_frame_to_a_good_one():
    """A bad frame must not swallow the good one behind it."""
    good = bytes([0xAA, 0xFF, 0x01, 0x03, 0x41, 0x12, 0x2A])
    good += bytes([sum(good[1:]) & 0xFF])
    junk = bytes([0xAA, 0xFF, 0x01, 0x03, 0x41, 0x12, 0x2A, 0x00])  # wrong checksum
    frame, rest = _parse_mdc_frame(junk + good)
    assert frame is not None
    assert frame[4] == 0x12 and frame[5] == 0x2A  # the good one


def test_an_uncorrelated_frame_still_updates_state():
    """While this driver holds the newest socket it receives replies to
    requests another controller made. Those still describe the display, so
    they are applied — they just must not satisfy anybody's wait."""
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            # A volume reply nobody asked for.
            body = bytes([0xFF, 0x01, 0x03, 0x41, DRV.CMD_VOLUME, 42])
            await driver.on_data_received(body)
            assert driver.get_child_state("display", 1)["volume"] == 42
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_identity_is_read_on_connect():
    """Model and firmware answer on a real panel and belong on the card."""
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert driver.state.data.get("model") == "DM75E"
            assert "GFSLE" in driver.state.data.get("firmware", "")
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_settable_picture_modes_include_the_signage_family():
    """The old hand-picked list of eight was wrong for signage panels: six of
    its entries NAK on a DM75E and seven of the modes that work were absent."""
    modes = DRV.PICTURE_MODE_SET
    for name in (
        "shop_mall_video",
        "office_school_video",
        "terminal_station_text",
        "video_wall_text",
        "calibration",
    ):
        assert name in modes, name
    assert "off" not in modes  # a read-back state, refused as a target





def test_setting_volume_clears_mute_and_the_driver_reads_it_back():
    """A level change unmutes the display as a side effect.

    Nothing in the volume reply says so, so a driver that only tracks what it
    sent shows a muted panel playing audio until the next poll.
    """
    async def go():
        driver, sim = await _make_pair()
        driver.VOLUME_MUTE_GUARD_S = 0.01
        sim._volume_mute_guard_s = 0.0
        await driver.connect()
        try:
            await driver.send_command("mute_on", {"display": 1})
            assert driver.get_child_state("display", 1)["mute"] is True
            await driver.send_command("set_volume", {"display": 1, "level": 33})
            # Read back from the device, not inferred from the command.
            assert driver.get_child_state("display", 1)["mute"] is False
            assert driver.get_child_state("display", 1)["volume"] == 33
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_mute_waits_out_the_post_volume_window_instead_of_failing():
    """"Set the level, then mute" is an ordinary macro, and the display NAKs
    the second step for ~2s after the first. The driver holds instead."""
    async def go():
        driver, sim = await _make_pair()
        driver.VOLUME_MUTE_GUARD_S = 0.30
        sim._volume_mute_guard_s = 0.20
        await driver.connect()
        try:
            await driver.send_command("set_volume", {"display": 1, "level": 25})
            # Would raise ValueError (NAK) without the guard.
            await driver.send_command("mute_on", {"display": 1})
            assert driver.get_child_state("display", 1)["mute"] is True
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_a_refused_set_does_not_retire_the_command():
    """A NAKed SET is usually about the value, not the command.

    A DM75E has one HDMI port and refuses hdmi2; it also refuses any mute
    inside the post-volume window. Retiring input or mute on that evidence
    would stop polling something the display answers perfectly well.
    """
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError):
                await driver.send_command(
                    "set_picture_mode", {"display": 1, "mode": "movie"}
                )
            assert (1, DRV.CMD_PICTURE_MODE) not in driver._unsupported
            await driver.poll()
            assert driver.get_child_state("display", 1)["picture_mode"]
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
