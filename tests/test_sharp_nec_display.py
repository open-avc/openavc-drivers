"""Driver + simulator tests for sharp_nec_display (NEC External Control).

No Sharp/NEC display hardware on hand, so correctness is proven two ways:
metadata / shape assertions on the driver, and **dual-proof round trips**
that wire the real driver to the real simulator over an in-memory
transport — the simulator renders the SOH/ASCII-hex/BCC packets from the
External Control manual, the driver parses them, and results are
asserted on both sides.

Covers the protocol essentials:
  - packet build/parse with XOR block-check (corrupt packets dropped);
  - Monitor-ID addressing routes values to the right display child;
  - power status/control procedure (01D6 / C203D6) + documented
    post-power/input cooldowns arming the quiet window;
  - the HDMI "set-only alias 4 / readback 17" input quirk;
  - unsupported opcodes (result 01) raising on commands and being
    remembered (and skipped) by the poll;
  - 2's-complement temperature decode, self-diagnosis fault mapping,
    serial/model hex-pair decode;
  - a display missing from the chain times out, and a dead transport
    makes poll raise (never-offline guard).

Loads the driver + simulator with the ``server.*`` / ``simulator.*``
imports stubbed so the community CI stays self-contained (conftest.py
rolls the stubs back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "displays" / "sharp_nec_display.py"
SIM_PATH = REPO_ROOT / "displays" / "sharp_nec_display_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeState:
    def __init__(self) -> None:
        self.data: dict = {}

    def set(self, key, value, **_):
        self.data[key] = value


class _FakeEvents:
    async def emit(self, name, *args, **kwargs):
        pass


class _FakeBaseDriver:
    """Stand-in for the platform BaseDriver surface this driver uses,
    including the child-entity registry."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False
        self._last_transport_error = ""
        self._last_fault = None
        self.children: dict[str, dict[int, dict]] = {}
        self.disconnect_calls = 0

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)

    # Child entity API (mirrors base.py semantics used by the driver).
    def register_child(self, ctype, cid, initial_state=None, schema=None):
        entry = self.children.setdefault(ctype, {}).setdefault(cid, {})
        if initial_state:
            entry.update(initial_state)

    def deregister_child(self, ctype, cid):
        self.children.get(ctype, {}).pop(cid, None)

    def list_children(self, ctype):
        return list(self.children.get(ctype, {}))

    def is_child_registered(self, ctype, cid):
        return cid in self.children.get(ctype, {})

    def set_child_state_batch(self, ctype, cid, updates):
        self.children[ctype][cid].update(updates)

    def _handle_transport_disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False

    def _stash_fault(self, code, message="") -> None:
        pass

    async def start_polling(self, interval) -> None:
        pass

    async def stop_polling(self) -> None:
        pass


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply.
_SWALLOW = False


class _FakeTCPTransport:
    def __init__(self, on_data, frame_parser) -> None:
        self.on_data = on_data
        self.parser = frame_parser
        self.connected = True
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, frame_parser=None, timeout=5.0,
                     name=""):
        return cls(on_data, frame_parser)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        resp = self._sim.handle_command(bytes(data))
        if resp and not _SWALLOW:
            # Dual-proof framing: pipe the sim's raw bytes through the
            # driver's own length-aware frame parser (a reply's BCC byte
            # can equal CR, which broke naive delimiter splitting).
            for frame in self.parser.feed(bytes(resp)):
                if frame:
                    await self.on_data(frame)

    async def close(self) -> None:
        self.connected = False


class _StubCallableFrameParser:
    """Platform-faithful CallableFrameParser: the buffer only persists
    when the parse function returns a message (mirrors frame_parsers.py,
    including the no-forward-progress guard)."""

    def __init__(self, parse_fn, max_buffer=1_048_576) -> None:
        self._parse_fn = parse_fn
        self._buffer = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += bytes(data)
        messages: list[bytes] = []
        while True:
            msg, remaining = self._parse_fn(self._buffer)
            if msg is None:
                break
            messages.append(msg)
            if len(remaining) >= len(self._buffer):
                self._buffer = remaining
                break
            self._buffer = remaining
        return messages

    def reset(self) -> None:
        self._buffer = b""


class _FakeTCPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self._error_modes = dict(self.SIMULATOR_INFO.get("error_modes", {}))
        self._active_errors: set = set()

    @property
    def state(self) -> dict:
        # Mirrors BaseSimulator: a READ-ONLY COPY. Sim code must write
        # through set_state; tests read through this copy.
        return dict(self._state)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def inject_error(self, mode: str) -> None:
        self._active_errors.add(mode)
        for key, value in self._error_modes[mode].get(
                "set_state", {}).items():
            self.set_state(key, value)

    def clear_error(self, mode: str) -> None:
        self._active_errors.discard(mode)
        self._state.pop("force_diag_code", None)


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
    tcp = ModuleType("server.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["server.transport.tcp"] = tcp
    parsers = ModuleType("server.transport.frame_parsers")
    parsers.CallableFrameParser = _StubCallableFrameParser
    sys.modules["server.transport.frame_parsers"] = parsers
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


DRV = _load("sharp_nec_display_under_test", DRIVER_PATH)
SIM = _load("sharp_nec_display_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(sim_config=None, driver_overrides=None, monkeypatch=None):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    sim = SIM.SharpNECDisplaySimulator(
        "sim1", {"display_ids": "1", **(sim_config or {})},
    )
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.50",
        "port": 7142,
        "display_ids": "1",
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.SharpNECDisplayDriver(
        "dsp1", cfg, _FakeState(), _FakeEvents(),
    )
    return driver, sim


def _child(driver, monitor_id):
    return driver.children["display"][monitor_id]


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_version_and_platform_gate():
    info = DRV.SharpNECDisplayDriver.DRIVER_INFO
    assert info["version"] == "1.0.0"
    assert info["min_platform_version"] == "0.13.0"
    assert info["ports"] == [7142]
    assert info["manufacturer"] == "Sharp NEC"


def test_probe_answer_coherence():
    """The declared tcp_probe bytes must be a valid packet the simulator
    answers, and the reply must start with the declared expect_hex."""
    probe = DRV.SharpNECDisplayDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 7142
    send = bytes.fromhex(probe["send_hex"])
    sim = SIM.SharpNECDisplaySimulator("probe_sim", {"display_ids": "1"})
    answer = sim.handle_command(send)
    assert answer is not None
    assert answer.startswith(bytes.fromhex(probe["expect_hex"]))


def test_actions_and_child_schema():
    info = DRV.SharpNECDisplayDriver.DRIVER_INFO
    for entry in info["actions"]:
        assert entry["id"] in info["commands"], entry["id"]
    child_vars = info["child_entity_types"]["display"]["state_variables"]
    # Every polled opcode must land in a declared child prop.
    for prop in DRV.POLL_PROPS:
        assert prop in child_vars, prop
    for extra in ("power", "temperature", "diagnosis", "model", "serial"):
        assert extra in child_vars, extra
    # Doc-verified bounds.
    assert child_vars["sharpness"]["max"] == 24
    assert child_vars["volume"]["max"] == 100


def test_packet_roundtrip_and_bcc():
    packet = DRV._build_packet(1, b"A", bytes([DRV.STX]) + b"01D6" + bytes([DRV.ETX]))
    # The manual's own worked example ends with CR and carries the XOR
    # of everything between SOH and the check code.
    assert packet[0] == DRV.SOH and packet.endswith(b"\r")
    body = packet[1:-2]
    bcc = 0
    for b in body:
        bcc ^= b
    assert packet[-2] == bcc
    # Monitor ID encoding table anchors: 1='A', 26='Z', 100=0xA4.
    assert DRV._encode_monitor_id(1) == b"A"
    assert DRV._encode_monitor_id(26) == b"Z"
    assert DRV._encode_monitor_id(100) == bytes([0xA4])


def test_frame_with_cr_valued_bcc_survives_framing():
    """Regression: a packet whose check-code byte equals CR (0x0D) must
    not be split by the framing layer — this is why the driver uses a
    length-aware parser instead of CR-delimiter framing."""
    sim = SIM.SharpNECDisplaySimulator("sim_cr", {"display_ids": "1"})
    cr_reply = None
    for prop in DRV.POLL_PROPS:
        page, code = DRV.OPCODES[prop]
        for value in range(101):
            request = DRV._build_packet(
                1, DRV.TYPE_SET,
                bytes([DRV.STX]) + DRV._op_bytes(page, code)
                + DRV._hex_word(value) + bytes([DRV.ETX]),
            )
            if request[-2] == 0x0D:  # request-side BCC collision
                cr_reply = request
            reply = sim.handle_command(request)
            if reply and 0x0D in reply[:-1]:  # reply-side collision
                cr_reply = reply
            if cr_reply is not None:
                break
        if cr_reply is not None:
            break
    assert cr_reply is not None, "no CR-colliding packet found in the sweep"

    parser = _StubCallableFrameParser(DRV._extract_nec_frame)
    frames = [f for f in parser.feed(cr_reply) if f]
    assert len(frames) == 1
    frame = frames[0]
    bcc = 0
    for b in frame[1:-1]:
        bcc ^= b
    assert bcc == frame[-1]

    # Same packet delivered in three chunks still yields one frame.
    parser.reset()
    assert [f for f in parser.feed(cr_reply[:5]) if f] == []
    assert [f for f in parser.feed(cr_reply[5:9]) if f] == []
    assert [f for f in parser.feed(cr_reply[9:]) if f] == [frame]


def test_corrupt_bcc_gets_no_reply():
    sim = SIM.SharpNECDisplaySimulator("sim_bcc", {"display_ids": "1"})
    packet = bytearray(
        DRV._build_packet(1, b"A", bytes([DRV.STX]) + b"01D6" + bytes([DRV.ETX]))
    )
    packet[-2] ^= 0xFF  # break the check code
    assert sim.handle_command(bytes(packet)) is None


# ── Connect / poll population ───────────────────────────────────────────────

def test_connect_populates_identity_and_standby_state():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            child = _child(driver, 1)
            assert child["model"] == "P554-1"
            assert child["serial"] == "8Z000001"
            assert child["power"] == "standby"
            assert child["diagnosis"] == "normal"
            # Parameter reads are gated off while not on.
            assert "volume" not in child
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_poll_reads_parameters_when_on():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("d1_power", "on")
            await driver.poll()
            child = _child(driver, 1)
            assert child["power"] == "on"
            assert child["input"] == "HDMI"        # code 17 readback
            assert child["volume"] == 30
            assert child["mute"] is False           # 2 = unmuted
            assert child["video_mute"] is False
            assert child["backlight"] == 70
            assert child["brightness"] == 50
            assert child["contrast"] == 50
            assert child["sharpness"] == 12
            assert child["aspect"] == "FULL"
            assert child["picture_mode"] == "STANDARD"
            assert child["temperature"] == 38.5
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_negative_temperature_decodes_twos_complement():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            sim.set_state("d1_power", "on")
            sim.set_state("d1_temperature", -5.0)
            await driver.poll()
            assert _child(driver, 1)["temperature"] == -5.0
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Power + cooldowns ───────────────────────────────────────────────────────

def test_power_round_trip_arms_cooldown(monkeypatch):
    async def go():
        driver, sim = await _make_pair()
        monkeypatch.setattr(DRV, "POWER_COOLDOWN_S", 0.05)
        await driver.connect()
        try:
            loop = asyncio.get_event_loop()
            await driver.send_command("power_on", {"display": 1})
            assert sim.state["d1_power"] == "on"
            assert _child(driver, 1)["power"] == "on"
            # The manual's post-power quiet window is armed…
            assert driver._quiet_until > loop.time()
            # …and the next transaction waits it out, then succeeds.
            await driver.send_command("power_off", {"display": 1})
            assert sim.state["d1_power"] == "off"
            assert _child(driver, 1)["power"] == "off"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_all_on_and_off_loop_the_roster(monkeypatch):
    async def go():
        driver, sim = await _make_pair(
            sim_config={"display_ids": "1,2"},
            driver_overrides={"display_ids": "1,2"},
        )
        monkeypatch.setattr(DRV, "POWER_COOLDOWN_S", 0.0)
        await driver.connect()
        try:
            await driver.send_command("all_on")
            assert sim.state["d1_power"] == "on"
            assert sim.state["d2_power"] == "on"
            await driver.send_command("all_off")
            assert sim.state["d1_power"] == "off"
            assert sim.state["d2_power"] == "off"
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Input quirk + setters ───────────────────────────────────────────────────

def test_select_input_uses_hdmi_set_alias(monkeypatch):
    async def go():
        driver, sim = await _make_pair()
        monkeypatch.setattr(DRV, "INPUT_COOLDOWN_S", 0.0)
        await driver.connect()
        try:
            sim.set_state("d1_input", 3)  # DVI
            await driver.send_command(
                "select_input", {"display": 1, "input": "HDMI"},
            )
            # The sim stored the canonical readback code…
            assert sim.state["d1_input"] == 17
            # …and the driver mapped it back to the same name it set.
            assert _child(driver, 1)["input"] == "HDMI"

            await driver.send_command(
                "select_input", {"display": 1, "input": "DPORT"},
            )
            assert sim.state["d1_input"] == 15
            assert _child(driver, 1)["input"] == "DPORT"
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_setter_round_trips():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            cases = [
                ("set_volume", {"level": 55}, "d1_volume", 55, "volume", 55),
                ("mute_on", {}, "d1_mute", 1, "mute", True),
                ("mute_off", {}, "d1_mute", 2, "mute", False),
                ("screen_mute_on", {}, "d1_video_mute", 1, "video_mute", True),
                ("screen_mute_off", {}, "d1_video_mute", 2, "video_mute", False),
                ("set_backlight", {"level": 40}, "d1_backlight", 40,
                 "backlight", 40),
                ("set_brightness", {"level": 60}, "d1_brightness", 60,
                 "brightness", 60),
                ("set_contrast", {"level": 65}, "d1_contrast", 65,
                 "contrast", 65),
                ("set_sharpness", {"level": 20}, "d1_sharpness", 20,
                 "sharpness", 20),
                ("set_aspect", {"aspect": "1:1"}, "d1_aspect", 7,
                 "aspect", "1:1"),
                ("set_picture_mode", {"mode": "CINEMA"}, "d1_picture_mode", 5,
                 "picture_mode", "CINEMA"),
            ]
            for command, extra, sim_key, sim_val, prop, prop_val in cases:
                await driver.send_command(
                    command, {"display": 1, **extra},
                )
                assert sim.state[sim_key] == sim_val, command
                assert _child(driver, 1)[prop] == prop_val, command
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_save_settings_and_raw_opcodes():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            await driver.send_command("save_settings", {"display": 1})
            assert sim.state["d1_saved"] is True

            # Raw set on a mapped opcode also updates the child prop.
            value = await driver.send_command(
                "set_opcode",
                {"display": 1, "page": "02", "code": "1A", "value": 8},
            )
            assert value == 8
            assert sim.state["d1_picture_mode"] == 8
            assert _child(driver, 1)["picture_mode"] == "CUSTOM1"

            value = await driver.send_command(
                "get_opcode", {"display": 1, "page": "00", "code": "62"},
            )
            assert value == 30
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Unsupported opcodes ─────────────────────────────────────────────────────

def test_unsupported_opcode_raises_on_command():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            with pytest.raises(ValueError) as excinfo:
                await driver.send_command(
                    "get_opcode", {"display": 1, "page": "07", "code": "99"},
                )
            assert "unsupported" in str(excinfo.value)
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_poll_remembers_unsupported_opcodes(monkeypatch):
    async def go():
        driver, sim = await _make_pair()
        # This model has no sharpness control: answer result 01.
        monkeypatch.delitem(SIM.OPCODE_KEYS, (0x00, 0x8C))
        await driver.connect()
        try:
            sim.set_state("d1_power", "on")
            await driver.poll()
            page, code = DRV.OPCODES["sharpness"]
            assert (1, page, code) in driver._unsupported
            assert "sharpness" not in _child(driver, 1)
            # Everything else still populated.
            assert _child(driver, 1)["volume"] == 30
            await driver.poll()  # skips the unsupported opcode quietly
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Multi-display routing ───────────────────────────────────────────────────

def test_monitor_id_routing_isolates_displays():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"display_ids": "1,2"},
            driver_overrides={"display_ids": "1,2"},
        )
        await driver.connect()
        try:
            sim.set_state("d1_power", "on")
            sim.set_state("d2_power", "on")
            await driver.poll()
            await driver.send_command(
                "set_volume", {"display": 2, "level": 90},
            )
            assert sim.state["d2_volume"] == 90
            assert sim.state["d1_volume"] == 30
            assert _child(driver, 2)["volume"] == 90
            assert _child(driver, 1)["volume"] == 30
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_missing_display_times_out():
    async def go():
        driver, sim = await _make_pair()  # chain only has ID 1
        await driver.connect()
        try:
            with pytest.raises(TimeoutError):
                await driver._transact(
                    5, DRV.TYPE_COMMAND,
                    bytes([DRV.STX]) + b"01D6" + bytes([DRV.ETX]),
                    timeout=0.05,
                )
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Diagnosis ───────────────────────────────────────────────────────────────

def test_self_diagnosis_fault_and_recovery():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            assert _child(driver, 1)["diagnosis"] == "normal"
            sim.inject_error("fan_fault")
            # Diagnosis reads on a 4-cycle rotation starting at cycle 1.
            driver._poll_cycle = 4
            await driver.poll()
            assert _child(driver, 1)["diagnosis"] == (
                "80: cooling fan 1 abnormality"
            )
            sim.clear_error("fan_fault")
            driver._poll_cycle = 4
            await driver.poll()
            assert _child(driver, 1)["diagnosis"] == "normal"
        finally:
            await driver.disconnect()

    asyncio.run(go())


# ── Never-offline guard ─────────────────────────────────────────────────────

def test_poll_raises_when_transport_down():
    async def go():
        driver, sim = await _make_pair()
        await driver.connect()
        try:
            driver.transport.connected = False
            with pytest.raises(ConnectionError):
                await driver.poll()
        finally:
            await driver.disconnect()

    asyncio.run(go())


def test_roster_reconcile_add_and_drop():
    async def go():
        driver, sim = await _make_pair(
            sim_config={"display_ids": "1,2"},
            driver_overrides={"display_ids": "1,2"},
        )
        await driver.connect()
        try:
            assert sorted(driver.list_children("display")) == [1, 2]
            driver.config["display_ids"] = "2"
            await driver.refresh_children()
            assert driver.list_children("display") == [2]
        finally:
            await driver.disconnect()

    asyncio.run(go())
