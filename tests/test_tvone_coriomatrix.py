"""Driver + simulator tests for tvone_coriomatrix (tvONE CORIOmatrix).

No CORIOmatrix hardware on hand, so correctness is proven two ways: metadata
/ shape assertions on the driver, and **dual-proof round trips** wiring the
real driver to the real simulator over an in-memory transport — the
simulator renders the CORIOmax CLI (long-form property replies, !Done /
!Failed terminals, the documented login confirmation), the driver parses
it, and results are asserted on both sides (same approach as
test_atlona_ome_ms.py / test_blackmagic_videohub.py).

Covers the driver's Python-justified shapes:
  - the login gate: success, rejected credential (typed auth wording),
    and a silent unit (no_response wording);
  - device-enumerated roster: Slots + slot dumps register a child per
    port; refresh_children reconciles a removed card;
  - long-form reply identities folding into compact-alias children
    (poll dumps, property reads, and !Done write echoes in both the long
    and S<n>I<n> alias forms);
  - Preset.PresetList() aggregation into the preset_options picker JSON;
  - the never-offline guard: poll propagates a transport failure.

Loads the driver + simulator with the ``openavc.*`` imports
stubbed so the community CI stays self-contained (conftest.py rolls the
stubs back after this module is collected).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubEvents as _FakeEvents,
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "tvone_coriomatrix.py"
SIM_PATH = REPO_ROOT / "switchers" / "tvone_coriomatrix_sim.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle plus the
    child-entity registry (mirrors base.py semantics: unregistered children
    are skipped, state keys are namespaced)."""

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
        self.disconnect_calls = 0
        self.stashed_fault: tuple[str, str] | None = None
        self._health_task = None
        self._bg_tasks: set = set()
        self._children: dict[str, set] = {}

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)

    def _handle_transport_disconnect(self) -> None:
        # Mirrors the platform: flip the flags synchronously, then schedule
        # the async teardown (stop loops, close transport, _close_session,
        # disconnect event).
        self._connected = False
        self.set_state("connected", False)
        self.disconnect_calls += 1
        if self.transport is not None:
            self.transport.connected = False
        task = asyncio.ensure_future(self._on_disconnect_cleanup())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _on_disconnect_cleanup(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        transport = self.transport
        self.transport = None
        if transport is not None:
            await transport.close()
        await self._close_session()
        await self.events.emit(f"device.disconnected.{self.device_id}")

    # -- liveness watchdog: this driver supplies no probe (steady polling is
    # the keep-alive), so connect() never starts the loop. The raise flags a
    # future probe addition so the loop gets modeled here then. --

    def _start_health_loop(self) -> None:
        raise NotImplementedError(
            "driver grew a liveness probe - model the health loop here")

    def _stop_health_loop(self) -> None:
        self._health_task = None

    # ── Child registry (mirrors BaseDriver) ──

    def register_child(self, child_type, local_id, initial_state=None, schema=None):
        self._children.setdefault(child_type, set())
        if local_id in self._children[child_type]:
            return
        self._children[child_type].add(local_id)
        for prop, value in (initial_state or {}).items():
            self.state.set(
                f"device.{self.device_id}.{child_type}.{local_id}.{prop}", value
            )
        self.state.set(
            f"device.{self.device_id}.{child_type}.{local_id}.online", True
        )

    def deregister_child(self, child_type, local_id):
        self._children.get(child_type, set()).discard(local_id)
        prefix = f"device.{self.device_id}.{child_type}.{local_id}."
        for key in [k for k in self.state.data if k.startswith(prefix)]:
            self.state.delete(key)

    def list_children(self, child_type):
        return sorted(self._children.get(child_type, set()))

    def is_child_registered(self, child_type, local_id):
        return local_id in self._children.get(child_type, set())

    def set_child_state_batch(self, child_type, local_id, updates):
        if not self.is_child_registered(child_type, local_id):
            return
        for prop, value in updates.items():
            self.state.set(
                f"device.{self.device_id}.{child_type}.{local_id}.{prop}", value
            )

    # -- connection lifecycle (mirrors the platform's hook-driven connect) --

    async def _pre_connect(self) -> None:
        pass

    async def _post_connect(self) -> None:
        pass

    async def _initial_sync(self) -> None:
        pass

    async def _close_session(self) -> None:
        pass

    async def _create_transport(self, transport_type) -> None:
        kwargs = dict(
            host=self.config.get("host", ""),
            port=self.config.get("port", 10001),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\r",
            frame_parser=self._create_frame_parser(),
            inter_command_delay=self.config.get("inter_command_delay", 0.0),
            timeout=self.config.get("timeout", 5.0),
            name=self.device_id,
        )
        self.transport = await _FakeTCPTransport.create(
            **self._transport_kwargs(transport_type, kwargs))

    async def connect(self) -> None:
        # 1. Clean slate: reset fault classification, drop a previous
        #    attempt's driver session and stale transport.
        self._last_transport_error = ""
        self._last_fault = None
        self.stashed_fault = None
        await self._close_session()
        if self.transport:
            await self.transport.close()
            self.transport = None
        # 2-3. Establish: pre-connect hook, then the transport.
        await self._pre_connect()
        await self._create_transport("tcp")
        # 4. Handshake: a raise here aborts the connection.
        try:
            await self._post_connect()
        except Exception:
            self._stash_transport_error()
            if self.transport:
                await self.transport.close()
                self.transport = None
            await self._close_session()
            self._connected = False
            raise
        # 5. Declare connected.
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        # 6. Initial sync: a raise here tears the connection back down.
        try:
            await self._initial_sync()
        except Exception:
            self._stash_transport_error()
            transport = self.transport
            self.transport = None
            if transport is not None:
                await transport.close()
            await self._close_session()
            self._connected = False
            self.set_state("connected", False)
            await self.events.emit(f"device.disconnected.{self.device_id}")
            raise
        # 7. Polling + liveness watchdog.
        if self.config.get("poll_interval", 0):
            await self.start_polling(self.config["poll_interval"])
        if self._health_enabled():
            self._start_health_loop()

    async def disconnect(self) -> None:
        self._stop_health_loop()
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
            self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")


class _FakeTCPSimulator:
    """Stand-in for openavc.simulator.tcp_simulator.TCPSimulator. Mirrors the real
    BaseSimulator semantics that matter: ``state`` returns a COPY, writes go
    through set_state."""

    SIMULATOR_INFO: dict = {}

    def __init__(self, device_id, config=None) -> None:
        self.device_id = device_id
        self.config = config or {}
        self._state = dict(self.SIMULATOR_INFO.get("initial_state", {}))

    @property
    def state(self):
        return dict(self._state)

    def set_state(self, key, value) -> None:
        self._state[key] = value


# Set by the pairing harness so the stubbed transport reaches the live sim.
_CURRENT_SIM: object | None = None
# When True, the transport sends to the sim but DROPS the reply — a unit
# that accepts the connection and stays silent.
_SWALLOW = False


class _FakeTCPTransport:
    sent_lines: list[str] = []

    def __init__(self, on_data, on_disconnect) -> None:
        self.on_data = on_data
        self.on_disconnect = on_disconnect
        self.connected = True
        self._sim = _CURRENT_SIM

    @classmethod
    async def create(cls, *, host, port, on_data, on_disconnect,
                     delimiter=None, frame_parser=None,
                     inter_command_delay=0.0, timeout=5.0, name=""):
        return cls(on_data, on_disconnect)

    async def send(self, data) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        line = bytes(data).decode()
        _FakeTCPTransport.sent_lines.append(line.strip())
        resp = self._sim.handle_command(bytes(data))
        if resp and not _SWALLOW:
            # The real transport frames on \n and strips the delimiter —
            # deliver the reply one line per on_data call, like the wire.
            for reply_line in resp.decode().split("\r\n"):
                if reply_line:
                    await self.on_data(reply_line.encode())

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
    tcp = ModuleType("openavc.transport.tcp")
    tcp.TCPTransport = _FakeTCPTransport
    sys.modules["openavc.transport.tcp"] = tcp
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


DRV = _load("tvone_coriomatrix_under_test", DRIVER_PATH)
SIM = _load("tvone_coriomatrix_sim_under_test", SIM_PATH)


# ── Pairing harness ─────────────────────────────────────────────────────────

async def _make_pair(driver_overrides=None, connect=True):
    global _CURRENT_SIM, _SWALLOW
    _SWALLOW = False
    _FakeTCPTransport.sent_lines = []
    sim = SIM.TvoneCoriomatrixSimulator("sim1", {})
    _CURRENT_SIM = sim

    cfg = {
        "host": "10.0.0.20",
        "port": 10001,
        "username": "admin",
        "password": "adminpw",
        "poll_interval": 0,
    }
    cfg.update(driver_overrides or {})
    driver = DRV.TvoneCoriomatrixDriver("mtx1", cfg, _FakeState(), _FakeEvents())
    if connect:
        await driver.connect()
    return driver, sim


def _child(driver, ctype, cid, prop):
    return driver.state.data.get(f"device.mtx1.{ctype}.{cid}.{prop}")


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata_and_platform_gate():
    info = DRV.TvoneCoriomatrixDriver.DRIVER_INFO
    assert info["version"] == "1.0.1"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    assert info["min_platform_version"] == "0.24.0"
    assert info["ports"] == [10001]
    # No active probe on purpose: the CLI answers nothing documented
    # before login.
    assert "tcp_probe" not in info["discovery"]
    assert info["discovery"]["port_open"] == [10001]


def test_actions_reference_real_commands():
    info = DRV.TvoneCoriomatrixDriver.DRIVER_INFO
    commands = info["commands"]
    for action in info["actions"]:
        if action.get("kind") == "command":
            assert action["id"] in commands, action["id"]
    for qa in info["quick_actions"]:
        assert qa in commands, qa


def test_device_setting_reads_back_through_declared_state():
    info = DRV.TvoneCoriomatrixDriver.DRIVER_INFO
    ds = info["device_settings"]
    assert set(ds) == {"device_name"}
    assert ds["device_name"]["state_key"] in info["state_variables"]


def test_password_has_no_prefilled_default():
    info = DRV.TvoneCoriomatrixDriver.DRIVER_INFO
    assert info["default_config"]["password"] == ""
    assert info["config_schema"]["password"]["secret"] is True


# ── Login gate ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_success_registers_roster():
    driver, sim = await _make_pair()
    assert driver._connected
    assert sim.state["logged_in"] is True
    assert driver.list_children("input") == ["s1i1", "s1i2", "s2i1", "s2i2"]
    assert driver.list_children("output") == ["s13o1", "s13o2", "s14o1", "s14o2"]
    # The login line went out first, credentials as configured.
    assert _FakeTCPTransport.sent_lines[0] == "login(admin,adminpw)"


@pytest.mark.asyncio
async def test_login_rejected_raises_auth_wording():
    with pytest.raises(ConnectionError) as exc:
        await _make_pair(driver_overrides={"password": "invalid"})
    assert "authentication failed" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_login_silence_raises_no_response_wording(monkeypatch):
    global _SWALLOW
    monkeypatch.setattr(DRV, "LOGIN_TIMEOUT_S", 0.05)
    driver, sim = await _make_pair(connect=False)
    _SWALLOW = True
    with pytest.raises(ConnectionError) as exc:
        await driver.connect()
    assert "no response" in str(exc.value).lower()


# ── Poll population ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_poll_populates_state():
    driver, sim = await _make_pair()
    # Output children: routing, cut-to-black, status.
    assert _child(driver, "output", "s13o1", "source") == "s1i1"
    assert _child(driver, "output", "s14o2", "source") == "s2i2"
    assert _child(driver, "output", "s13o1", "cut_to_black") is False
    # Input children: signal status.
    assert _child(driver, "input", "s1i1", "status") == "OK"
    assert _child(driver, "input", "s1i2", "status") == "NO_SIGNAL"
    # Device-level state (first cycle reads system info + presets).
    assert driver.get_state("active_preset") == 1
    assert driver.get_state("status") == "Serving"
    assert driver.get_state("api_version") == "3.1.4386"
    assert driver.get_state("device_name") == "CORIOmatrix"


@pytest.mark.asyncio
async def test_preset_options_picker_json():
    driver, sim = await _make_pair()
    options = json.loads(driver.get_state("preset_options"))
    assert {"value": "1", "label": "1: start"} in options
    assert {"value": "2", "label": "2: side_by_side"} in options


# ── Commands ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_sends_long_form_and_updates_child():
    driver, sim = await _make_pair()
    ok = await driver.send_command("route", {"input": "s2i1", "output": "s13o1"})
    assert ok is True
    # Long-form on the wire (immune to renamed aliases, per the doc note).
    assert "Slot2.In1 > Slot13.Out1" in _FakeTCPTransport.sent_lines
    # Both sides agree.
    assert sim.state["route_out_13_1"] == "s2i1"
    assert _child(driver, "output", "s13o1", "source") == "s2i1"


@pytest.mark.asyncio
async def test_alias_form_route_echo_also_parses():
    driver, sim = await _make_pair()
    # A firmware that echoes the alias form must land in the same child.
    await driver.on_data_received(b"!Done S1I2 > S14O1")
    assert _child(driver, "output", "s14o1", "source") == "s1i2"


@pytest.mark.asyncio
async def test_cut_to_black_round_trip():
    driver, sim = await _make_pair()
    ok = await driver.send_command("cut_to_black_on", {"output": "s14o1"})
    assert ok is True
    assert sim.state["ctb_out_14_1"] is True
    # The !Done write echo restates the value — parsed into child state.
    assert _child(driver, "output", "s14o1", "cut_to_black") is True
    await driver.send_command("cut_to_black_off", {"output": "s14o1"})
    assert sim.state["ctb_out_14_1"] is False
    assert _child(driver, "output", "s14o1", "cut_to_black") is False


@pytest.mark.asyncio
async def test_recall_preset_reroutes_and_rereads():
    driver, sim = await _make_pair()
    ok = await driver.send_command("recall_preset", {"preset": 2})
    assert ok is True
    # Preset 2 swaps the card routing on the sim...
    assert sim.state["route_out_13_1"] == "s2i1"
    assert sim.state["route_out_14_1"] == "s1i1"
    # ...and the driver re-read every output.
    assert _child(driver, "output", "s13o1", "source") == "s2i1"
    assert _child(driver, "output", "s14o1", "source") == "s1i1"
    assert driver.get_state("active_preset") == 2


@pytest.mark.asyncio
async def test_recall_unknown_preset_reports_failed():
    driver, sim = await _make_pair()
    ok = await driver.send_command("recall_preset", {"preset": 7})
    assert ok is False


@pytest.mark.asyncio
async def test_save_preset_flow_and_picker_refresh():
    driver, sim = await _make_pair()
    await driver.send_command("route", {"input": "s1i1", "output": "s14o2"})
    ok = await driver.send_command("save_preset", {"preset": 5, "name": "lecture"})
    assert ok is True
    sent = _FakeTCPTransport.sent_lines
    assert "Preset.Read = 5" in sent
    assert "Preset.NameRead = lecture" in sent
    assert "Preset.SaveRead()" in sent
    assert sim._presets[5]["name"] == "lecture"
    assert sim._presets[5]["routing"]["route_out_14_2"] == "s1i1"
    options = json.loads(driver.get_state("preset_options"))
    assert {"value": "5", "label": "5: lecture"} in options


@pytest.mark.asyncio
async def test_device_name_setting_round_trip():
    driver, sim = await _make_pair()
    await driver.set_device_setting("device_name", "Lecture Hall Matrix")
    assert sim.state["unit_description"] == "Lecture Hall Matrix"
    assert driver.get_state("device_name") == "Lecture Hall Matrix"


@pytest.mark.asyncio
async def test_raw_command_returns_reply_text():
    driver, sim = await _make_pair()
    reply = await driver.send_command("raw_command", {"command": "System.API_Version"})
    assert "System.API_Version = 3.1.4386" in reply
    assert "!Done" in reply


# ── Roster reconcile ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_children_reconciles_removed_card():
    driver, sim = await _make_pair()
    del sim._cards[2]  # pull the HDMI input card
    result = await driver.refresh_children()
    assert result == {"inputs": 2, "outputs": 4}
    assert driver.list_children("input") == ["s1i1", "s1i2"]
    # The removed ports' state keys are gone.
    assert _child(driver, "input", "s2i1", "status") is None


# ── Never-offline guard ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_propagates_transport_failure():
    driver, sim = await _make_pair()
    driver.transport.connected = False
    with pytest.raises(ConnectionError):
        await driver.poll()
