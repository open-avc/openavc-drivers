"""Driver + simulator tests for viewsonic_cde (LFD RS-232 & LAN protocol).

No ViewSonic display hardware on hand, so correctness is proven two
ways: metadata / shape assertions on the driver, and **dual-proof round
trips** that wire the real driver to the real simulator over an
in-memory transport — the simulator renders the framed
len/ID/type/code/value packets from the LFD RS-232 & LAN Protocol
Specification v3.3.2, the driver parses them, and results are asserted
on both sides.

Covers the protocol essentials:
  - packet build against the spec's worked examples (length byte, ID
    field, three-char value field, the backlight 'A'/'a' command-type
    pair);
  - every set command mutating the simulator and every polled get
    landing in the right state variable, including the packed
    Get-Input reply (signal digit + source suffix), the negative
    thermal encoding, and the 32-byte NUL-padded info replies;
  - device-setting writes with immediate read-back (locks are NOT
    inverted on this protocol: wire 001 = locked);
  - the auto-reply push path on both sides: the driver applies
    unsolicited 'r' frames, and the simulator pushes them on
    user-side state changes but not on controller-driven sets;
  - set-only functions (bass/treble/balance, color mode, picture
    size, surround, OSD language, PIP sound/position, key presses)
    fabricating no state;
  - Monitor-ID addressing, reject ('-') handling, the standby gate,
    and IR pass-through ('p') frames being ignored;
  - the Wake-on-LAN setup action's 126-byte spec packet and MAC
    resolution order (param > learned state > config).

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
DRIVER_PATH = REPO_ROOT / "displays" / "viewsonic_cde.py"
SIM_PATH = REPO_ROOT / "displays" / "viewsonic_cde_sim.py"


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
    """Stand-in for the platform BaseDriver surface this driver uses."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self.transport = None
        self._connected = False

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None


class _FakeTransport:
    """In-memory pipe: driver bytes -> sim -> reply frames -> driver,
    split on CR exactly like the platform's delimiter framing."""

    def __init__(self, on_data) -> None:
        self.on_data = on_data
        self.connected = True
        self.sent: list[bytes] = []

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        self.sent.append(bytes(data))
        sim = _CURRENT_SIM
        reply = sim.handle_command(bytes(data)) if sim else None
        if reply:
            for frame in bytes(reply).split(b"\r"):
                if frame:
                    await self.on_data(frame)

    async def close(self) -> None:
        self.connected = False


class _FakeTCPSimulator:
    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))
        self.pushed: list[bytes] = []

    @property
    def state(self) -> dict:
        # Mirrors BaseSimulator: a READ-ONLY COPY. Sim code must write
        # through set_state; tests read through this copy.
        return dict(self._state)

    def set_state(self, key, value) -> None:
        self._state[key] = value

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    async def push(self, data) -> None:
        self.pushed.append(bytes(data))


def _load(name: str, path: Path) -> ModuleType:
    server = ModuleType("server")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["server"] = server
    for sub in ("drivers", "utils"):
        m = ModuleType(f"server.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"server.{sub}"] = m
    base = ModuleType("server.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["server.drivers.base"] = base
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


DRV = _load("viewsonic_cde_under_test", DRIVER_PATH)
SIM = _load("viewsonic_cde_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

def _make_pair(sim_config=None, driver_overrides=None):
    global _CURRENT_SIM
    sim = SIM.ViewSonicCdeSimulator("sim1", sim_config or {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.61",
        "port": 5000,
        "monitor_id": 1,
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.ViewSonicCdeDriver("lfd1", cfg, _FakeState(), _FakeEvents())
    driver.transport = _FakeTransport(driver.on_data_received)
    return driver, sim


def _dstate(driver, key):
    return driver.get_state(key)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture()
def pair():
    return _make_pair()


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_shape():
    info = DRV.ViewSonicCdeDriver.DRIVER_INFO
    assert info["version"] == "1.0.0"
    assert info["min_platform_version"] == "0.23.0"
    assert info["ports"] == [5000]
    assert info["transports"] == ["tcp", "serial"]
    # Every device setting reads back through a declared state variable.
    for key, setting in info["device_settings"].items():
        assert setting["state_key"] in info["state_variables"], key
    # Quick actions promote declared commands.
    for cid in info["quick_actions"]:
        assert cid in info["commands"], cid
    # The wake action is a setup action available while offline.
    wake = next(a for a in info["actions"] if a["id"] == "wake_display")
    assert wake["kind"] == "setup"
    assert wake["availability"] == "offline"


def test_setting_enums_match_state_enums():
    info = DRV.ViewSonicCdeDriver.DRIVER_INFO
    for key in ("power_lock", "button_lock", "menu_lock", "remote_control_mode"):
        setting = info["device_settings"][key]
        state_var = info["state_variables"][setting["state_key"]]
        setting_values = {v["value"] for v in setting["values"]}
        assert setting_values == set(state_var["values"]), key


def test_packet_build_matches_spec_examples():
    # Spec example 1: Set Brightness 76 for display #02 ->
    # 38 30 32 73 24 30 37 36 0D
    driver, _ = _make_pair(driver_overrides={"monitor_id": 2})
    assert driver._packet("s", "$", "076") == b"802s$076\r"
    # Spec Get example: Get Brightness from display #05 ->
    # 38 30 35 67 62 30 30 30 0D
    driver5, _ = _make_pair(driver_overrides={"monitor_id": 5})
    assert driver5._packet("g", "b", "000") == b"805gb000\r"
    # The backlight level rides its own command-type pair (code 'B').
    driver1, _ = _make_pair()
    assert driver1._packet("A", "B", "055") == b"801AB055\r"
    assert driver1._packet("a", "B", "000") == b"801aB000\r"


def test_probe_answer_coherence():
    """The declared tcp_probe bytes must be a packet the simulator answers,
    and the reply must contain the declared expect substring."""
    probe = DRV.ViewSonicCdeDriver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 5000
    sim = SIM.ViewSonicCdeSimulator("probe_sim", {})
    answer = sim.handle_command(probe["send_ascii"].encode("ascii"))
    assert answer is not None
    assert probe["expect"].encode("ascii") in answer
    # The link test is documented to answer in standby too — a probe can
    # still fingerprint a display whose standby keeps the network alive.
    sim.set_state("power_code", "000")
    answer = sim.handle_command(probe["send_ascii"].encode("ascii"))
    assert probe["expect"].encode("ascii") in answer


# ── Dual-proof round trips ──────────────────────────────────────────────────

def test_power_transitions(pair):
    driver, sim = pair

    async def run():
        await driver.send_command("power_off")
        assert sim.state["power_code"] == "000"
        await driver.poll()
        assert _dstate(driver, "power") == "standby"

        await driver.send_command("power_on")
        assert sim.state["power_code"] == "001"
        await driver.poll()
        assert _dstate(driver, "power") == "on"

    _run(run())


def test_sets_mutate_sim_and_polls_read_back(pair):
    driver, sim = pair

    async def run():
        cases = [
            ("set_source", {"source": "hdmi2"}, "input_code", "014"),
            ("set_volume", {"level": 42}, "volume", 42),
            ("set_brightness", {"level": 62}, "brightness", 62),
            ("set_contrast", {"level": 61}, "contrast", 61),
            ("set_sharpness", {"level": 63}, "sharpness", 63),
            ("set_color", {"level": 64}, "color", 64),
            ("set_tint", {"level": 65}, "tint", 65),
            ("set_backlight", {"level": 66}, "backlight", 66),
            ("mute_on", None, "mute_code", "001"),
            ("freeze_on", None, "freeze_code", "001"),
            ("backlight_off", None, "backlight_on_code", "000"),
            ("set_pip_mode", {"mode": "pip"}, "pip_mode_code", "001"),
            ("set_pip_input", {"source": "dp1"}, "pip_input_code", "009"),
            ("set_tiling_mode", {"mode": "on"}, "tiling_mode_code", "001"),
            ("set_tiling_compensation", {"mode": "on"}, "tiling_comp_code", "001"),
            ("set_tiling_layout", {"horizontal": 3, "vertical": 4}, "tiling_hv", "034"),
            ("set_tiling_position", {"position": 7}, "tiling_pos_code", "007"),
        ]
        for command, params, sim_key, expected in cases:
            await driver.send_command(command, params)
            assert sim.state[sim_key] == expected, command

        # The backlight set must ride the 'A' command type on the wire.
        assert b"801AB066\r" in driver.transport.sent

        await driver.poll()
        expected_states = {
            "power": "on", "source": "hdmi2", "signal_detected": True,
            "volume": 42, "mute": True, "brightness": 62, "backlight": 66,
            "contrast": 61, "sharpness": 63, "color": 64, "tint": 65,
            "backlight_on": False, "freeze": True, "touch_enabled": True,
            "power_lock": "unlocked", "button_lock": "unlocked",
            "menu_lock": "unlocked", "remote_control_mode": "enabled",
            "pip_mode": "pip", "pip_input": "dp1",
            "tiling_mode": True, "tiling_compensation": True,
            "tiling_layout": "3x4", "tiling_position": 7,
            "thermal_c": 42, "operation_hours": 1234,
            "amb_temperature_c": 23.5, "amb_humidity": 45.0,
            "amb_light": 80, "amb_presence": True,
        }
        for key, value in expected_states.items():
            assert _dstate(driver, key) == value, key

    _run(run())


def test_volume_brightness_steps_and_input_cycle(pair):
    driver, sim = pair

    async def run():
        vol = sim.state["volume"]
        await driver.send_command("volume_up")
        assert sim.state["volume"] == vol + 1
        await driver.send_command("volume_down")
        assert sim.state["volume"] == vol

        bri = sim.state["brightness"]
        await driver.send_command("brightness_up")
        assert sim.state["brightness"] == bri + 1
        await driver.send_command("brightness_down")
        assert sim.state["brightness"] == bri

        # 00Z steps the display's own input cycle.
        assert sim.state["input_code"] == "004"
        await driver.send_command("input_cycle")
        assert sim.state["input_code"] == "014"

    _run(run())


def test_device_settings_write_and_read_back(pair):
    driver, sim = pair

    async def run():
        cases = [
            ("backlight", 55, "backlight", 55, "backlight", 55),
            # NOT inverted on this protocol: wire 001 = locked.
            ("power_lock", "locked", "power_lock_code", "001",
             "power_lock", "locked"),
            ("button_lock", "locked", "button_lock_code", "001",
             "button_lock", "locked"),
            ("menu_lock", "locked", "menu_lock_code", "001",
             "menu_lock", "locked"),
            ("remote_control_mode", "passthrough", "rcu_mode_code", "002",
             "remote_control_mode", "passthrough"),
            ("touch", False, "touch_code", "000", "touch_enabled", False),
        ]
        for key, value, sim_key, sim_expected, state_key, state_expected in cases:
            await driver.set_device_setting(key, value)
            assert sim.state[sim_key] == sim_expected, key
            # The write issues an immediate read-back get.
            assert _dstate(driver, state_key) == state_expected, key

        # The backlight read-back must ride the 'a' command type.
        assert b"801aB000\r" in driver.transport.sent

    _run(run())


def test_identity_and_info_decode(pair):
    driver, sim = pair

    async def run():
        await driver._post_connect()
        assert _dstate(driver, "device_name") == "CDE5530"
        assert _dstate(driver, "mac_address") == "04:0e:c2:12:34:56"
        assert _dstate(driver, "ip_address") == "192.168.1.50"
        assert _dstate(driver, "serial_number") == "ABC180212345"
        assert _dstate(driver, "firmware_version") == "3.02.001"
        # The 32-byte format's NUL padding must never leak into state.
        for key in ("device_name", "ip_address", "serial_number", "firmware_version"):
            assert "\x00" not in _dstate(driver, key)

    _run(run())


def test_input_reply_signal_packing(pair):
    driver, sim = pair

    async def run():
        # Signal lost: the same reply carries detect digit + source suffix.
        sim.set_state("signal_code", "0")
        await driver.poll()
        assert _dstate(driver, "signal_detected") is False
        assert _dstate(driver, "source") == "hdmi1"

        # An undocumented source code reads back as source_<suffix>.
        sim.set_state("input_code", "099")
        sim.set_state("signal_code", "1")
        await driver.poll()
        assert _dstate(driver, "signal_detected") is True
        assert _dstate(driver, "source") == "source_99"

    _run(run())


def test_thermal_negative_encoding(pair):
    driver, sim = pair

    async def run():
        sim.set_state("thermal_c", -5)
        await driver.poll()
        assert _dstate(driver, "thermal_c") == -5

    _run(run())


def test_smart_hub_parse(pair):
    driver, sim = pair

    async def run():
        # Sub-zero ambient temperature, individual-field query form.
        sim.set_state("hub_temp", "-05.0")
        reply = sim.handle_command(b"801g:00A\r")
        for frame in reply.split(b"\r"):
            if frame:
                await driver.on_data_received(frame)
        assert _dstate(driver, "amb_temperature_c") == -5.0

    _run(run())


# ── Auto-reply push (*3.2.1) ───────────────────────────────────────────────

def test_driver_applies_unsolicited_frames(pair):
    driver, sim = pair

    async def run():
        await driver.on_data_received(b"801rf042")
        assert _dstate(driver, "volume") == 42
        await driver.on_data_received(b"801rj104")
        assert _dstate(driver, "source") == "hdmi1"
        assert _dstate(driver, "signal_detected") is True
        await driver.on_data_received(b"801rg001")
        assert _dstate(driver, "mute") is True
        await driver.on_data_received(b"801rl000")
        assert _dstate(driver, "power") == "standby"

    _run(run())


def test_sim_pushes_on_user_change_but_not_controller_sets(pair):
    driver, sim = pair

    async def run():
        # A user-side change (Simulator UI) pushes the auto-reply frame.
        sim.set_state("volume", 55)
        await asyncio.sleep(0)
        assert b"801rf055\r" in sim.pushed

        sim.set_state("input_code", "014")
        await asyncio.sleep(0)
        assert b"801rj114\r" in sim.pushed

        # A controller-driven set command must NOT trigger the push.
        sim.pushed.clear()
        sim.handle_command(b"801s5060\r")
        await asyncio.sleep(0)
        assert sim.state["volume"] == 60
        assert sim.pushed == []

    _run(run())


def test_ir_passthrough_frames_ignored(pair):
    driver, sim = pair

    async def run():
        snapshot = dict(driver.state.data)
        # RCU pass-through key event (VOL+ on display #01).
        await driver.on_data_received(b"601p10")
        assert driver.state.data == snapshot

    _run(run())


# ── Addressing / rejects / standby ──────────────────────────────────────────

def test_monitor_id_addressing():
    # Driver addresses ID 2; the sim is ID 1 -> chain silence, no state.
    driver, sim = _make_pair(driver_overrides={"monitor_id": 2})

    async def run():
        await driver.poll()
        assert _dstate(driver, "power") is None
        # A foreign reply frame is ignored by the driver's ID filter.
        await driver.on_data_received(b"801rl001")
        assert _dstate(driver, "power") is None

    _run(run())


def test_rejects_leave_state_untouched(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        assert _dstate(driver, "volume") == 30
        # Out-of-range raw set: the sim answers '-', nothing changes.
        await driver.send_command(
            "raw_command", {"cmd_type": "s", "code": "5", "value": "176"},
        )
        assert sim.state["volume"] == 30
        await driver.poll()
        assert _dstate(driver, "volume") == 30

    _run(run())


def test_no_fabricated_state_for_set_only_functions(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        snapshot = dict(driver.state.data)
        # None of these has a read-back in the protocol; the driver must
        # not synthesize state from the outgoing command.
        await driver.send_command("set_bass", {"level": 40})
        await driver.send_command("set_treble", {"level": 60})
        await driver.send_command("set_balance", {"level": 50})
        await driver.send_command("set_color_mode", {"mode": "warm"})
        await driver.send_command("set_picture_size", {"size": "full"})
        await driver.send_command("set_osd_language", {"language": "english"})
        await driver.send_command("surround_on")
        await driver.send_command("set_pip_sound", {"from_window": "main"})
        await driver.send_command("set_pip_position", {"position": "up"})
        await driver.send_command("nav_key", {"key": "menu"})
        await driver.send_command("press_number", {"number": 5})
        await driver.send_command("custom_hot_key", {"key": 1})
        assert driver.state.data == snapshot

    _run(run())


def test_standby_gate_holds_state(pair):
    driver, sim = pair

    async def run():
        await driver.poll()
        assert _dstate(driver, "volume") == 30

        await driver.send_command("power_off")
        assert sim.state["power_code"] == "000"
        # Nudge the sim's held values; in standby only the power get
        # answers, so the driver's other state must hold.
        sim.set_state("volume", 77)
        await driver.poll()
        assert _dstate(driver, "power") == "standby"
        assert _dstate(driver, "volume") == 30

        # Power-on over the control link brings it back.
        await driver.send_command("power_on")
        await driver.poll()
        assert _dstate(driver, "power") == "on"
        assert _dstate(driver, "volume") == 77

    _run(run())


def test_poll_raises_on_dead_transport(pair):
    driver, _ = pair

    async def run():
        await driver.transport.close()
        with pytest.raises(ConnectionError):
            await driver.poll()

    _run(run())


# ── Wake-on-LAN setup action ────────────────────────────────────────────────

class _FakeSocket:
    sent: list[tuple[bytes, tuple]] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def setsockopt(self, *args) -> None:
        pass

    def sendto(self, data, addr) -> None:
        _FakeSocket.sent.append((bytes(data), addr))


class _FakeSocketModule:
    """Replaces the driver module's ``socket`` attribute only — patching
    the real module would break asyncio's own socketpair plumbing."""

    AF_INET = 2
    SOCK_DGRAM = 2
    SOL_SOCKET = 1
    SO_BROADCAST = 6
    socket = _FakeSocket


def test_wake_display_sends_spec_magic_packet(pair, monkeypatch):
    driver, _ = pair
    monkeypatch.setattr(DRV, "socket", _FakeSocketModule)
    _FakeSocket.sent = []
    driver.set_state("mac_address", "04:0e:c2:12:34:56")
    progress_lines: list[str] = []

    async def progress(step, pct=None):
        progress_lines.append(step)

    async def run():
        result = await driver.run_setup_action("wake_display", {}, progress)
        assert result == {"mac": "04:0e:c2:12:34:56"}

    _run(run())
    mac_bytes = bytes([0x04, 0x0E, 0xC2, 0x12, 0x34, 0x56])
    # The LFD spec's 126-byte WOL frame: sync + 16 MAC repeats + 24-byte
    # zero tail, on UDP port 9.
    magic = b"\xff" * 6 + mac_bytes * 16 + b"\x00" * 24
    assert len(magic) == 126
    # Broadcast plus a direct copy at the configured host.
    assert (magic, ("255.255.255.255", 9)) in _FakeSocket.sent
    assert (magic, ("10.0.0.61", 9)) in _FakeSocket.sent
    assert progress_lines


def test_wake_display_mac_resolution_and_validation(pair, monkeypatch):
    driver, _ = pair
    monkeypatch.setattr(DRV, "socket", _FakeSocketModule)
    _FakeSocket.sent = []

    async def progress(step, pct=None):
        pass

    async def run():
        # No MAC anywhere -> a clear error.
        with pytest.raises(ValueError, match="No MAC address"):
            await driver.run_setup_action("wake_display", {}, progress)
        # Malformed MAC -> a clear error.
        with pytest.raises(ValueError, match="not a valid MAC"):
            await driver.run_setup_action(
                "wake_display", {"mac": "not-a-mac"}, progress,
            )
        # An explicit param wins over config.
        driver.config["mac_address"] = "11:22:33:44:55:66"
        result = await driver.run_setup_action(
            "wake_display", {"mac": "aa:bb:cc:dd:ee:ff"}, progress,
        )
        assert result == {"mac": "aa:bb:cc:dd:ee:ff"}

    _run(run())
