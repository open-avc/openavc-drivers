"""Unit tests for the darwin_control driver.

Loads ``switchers/darwin_control.py`` directly, stubbing the ``openavc.*``
imports it needs (BaseDriver, get_logger) so the community repo's test suite
stays self-contained — mirrors test_chazy_control_pro.py.

The banner parsers are exercised against byte-exact captures from real
hardware (Controller(h) FW 1.50.02, one TX + one RX enrolled) in
fixtures/darwin_control_banners.py.

Also covers the connection lifecycle against a functional fake BaseDriver
mirroring the platform's hook-driven connect()/disconnect(): the
controller-speaks-first banner consumption, the banner-timeout teardown, the
best-effort initial sync, and the receive-state clears on every teardown path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import string
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
DRIVER_PATH = REPO_ROOT / "switchers" / "darwin_control.py"
FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "darwin_control_banners.py"


# ── Platform stand-ins ──────────────────────────────────────────────────────

# Controller-side scripting for the fake transport: the greeting played the
# moment the socket opens (Telnet negotiation + welcome banner + first
# prompt), a switch that keeps the controller silent (banner-timeout path),
# and one that fails every command send (link died right after the banner).
_GREETING = (
    bytes([0xFF, 0xFB, 0x01, 0xFF, 0xFB, 0x03])  # IAC WILL ECHO, IAC WILL SGA
    + b"Welcome to Darwin Control\r\nFW Version: 1.50.02\r\n\r\nCONTROLLER> "
)
_SILENT = False
_FAIL_SENDS = False


class _FakeTransport:
    """In-memory stand-in for the platform TCP transport: records what the
    driver sends and answers each line the way the controller CLI does
    (echo, an ack line, the prompt)."""

    created_kwargs: dict = {}
    sent_lines: list[str] = []
    last: "_FakeTransport | None" = None

    def __init__(self, on_data) -> None:
        self.on_data = on_data
        self.connected = True

    @classmethod
    async def create(cls, **kwargs):
        cls.created_kwargs = dict(kwargs)
        transport = cls(kwargs["on_data"])
        cls.last = transport
        # The controller talks first: negotiation and the banner land before
        # the driver has sent a byte.
        if not _SILENT:
            await transport.on_data(_GREETING)
        return transport

    async def send(self, data: bytes) -> None:
        if _FAIL_SENDS or not self.connected:
            raise ConnectionError("transport closed")
        line = bytes(data).decode("ascii", errors="replace").strip()
        _FakeTransport.sent_lines.append(line)
        if not _SILENT:
            await self.on_data(
                (line + "\r\n[SUCCESS].\r\nCONTROLLER> ").encode("ascii")
            )

    async def close(self) -> None:
        self.connected = False


class _FakeBaseDriver(LifecycleFake):
    """Functional stand-in for the platform BaseDriver surface this driver
    uses: the hook-driven connect()/disconnect() lifecycle (including the
    _post_connect / _initial_sync failure teardowns and _close_session on
    every teardown path) plus the child-entity registry."""

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
        self._health_task = None
        self._bg_tasks: set = set()
        self._children: dict[str, dict[int, dict]] = {}
        self.polling: float | None = None

    # -- state --

    def set_state(self, key, value) -> None:
        self.state.set(f"device.{self.device_id}.{key}", value)

    def set_states(self, mapping) -> None:
        for key, value in mapping.items():
            self.set_state(key, value)

    def get_state(self, key, default=None):
        return self.state.data.get(f"device.{self.device_id}.{key}", default)

    # -- child registry (dict-backed, mirrors the platform semantics the
    # driver relies on: idempotent register, unregistered writes dropped) --

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

    def register_child(self, ctype, lid, initial_state=None) -> None:
        bucket = self._children.setdefault(ctype, {})
        if lid in bucket:
            return
        st = {prop: None for prop in self._eff_schema(ctype)}
        st["online"] = True
        st["label"] = ""
        st.update(initial_state or {})
        bucket[lid] = st

    def deregister_child(self, ctype, lid) -> None:
        self._children.get(ctype, {}).pop(lid, None)

    def is_child_registered(self, ctype, lid) -> bool:
        return lid in self._children.get(ctype, {})

    def list_children(self, ctype) -> list:
        return list(self._children.get(ctype, {}).keys())

    def get_child_state(self, ctype, lid) -> dict:
        return dict(self._children.get(ctype, {}).get(lid, {}))

    def set_child_state_batch(self, ctype, lid, updates) -> None:
        if not self.is_child_registered(ctype, lid):
            return
        self._children[ctype][lid].update(updates)

    async def poll_children(self, child_type, fetch, batch_size=50,
                            inter_batch_delay=0.1) -> None:
        ids = self.list_children(child_type)
        if not ids:
            return
        results = await fetch(ids)
        for lid, props in results.items():
            self.set_child_state_batch(child_type, lid, props)

    # -- polling --

    async def start_polling(self, interval) -> None:
        self.polling = interval

    async def stop_polling(self) -> None:
        self.polling = None

    # -- liveness watchdog: this driver supplies no probe (steady polling is
    # the keep-alive), so connect() never starts the loop. The raise flags a
    # future probe addition so the loop gets modeled here then. --

    def _start_health_loop(self) -> None:
        raise NotImplementedError(
            "driver grew a liveness probe - model the health loop here")

    def _stop_health_loop(self) -> None:
        self._health_task = None

    def _handle_transport_disconnect(self) -> None:
        # Mirrors the platform: flip the flags synchronously, then schedule
        # the async teardown (stop loops, close transport, _close_session,
        # disconnect event).
        self._connected = False
        self.set_state("connected", False)
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
            port=self.config.get("port", 23),
            on_data=self.on_data_received,
            on_disconnect=self._handle_transport_disconnect,
            delimiter=b"\r",
            frame_parser=self._create_frame_parser(),
            inter_command_delay=self.config.get("inter_command_delay", 0.0),
            timeout=self.config.get("timeout", 5.0),
            name=self.device_id,
        )
        self.transport = await _FakeTransport.create(
            **self._transport_kwargs(transport_type, kwargs))

    async def connect(self) -> None:
        # 1. Clean slate: reset fault classification, drop a previous
        #    attempt's driver session and stale transport.
        self._last_transport_error = ""
        self._last_fault = None
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


def _install_server_stubs() -> None:
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc"] = server
    for sub in ("drivers", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"openavc.{sub}"] = m
    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["openavc.drivers.base"] = base
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="darwin": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_server_stubs()
drv = _load(DRIVER_PATH, "darwin_control_under_test")
fx = _load(FIXTURES_PATH, "darwin_fixtures_under_test")
INFO = drv.DarwinControlDriver.DRIVER_INFO

_SPECIAL = {"search", "add_auto_all", "exit_guest"} | set(drv._RESET_CONFIRM)


# ── Identity ──

def test_driver_identity():
    assert INFO["id"] == "darwin_control"
    assert INFO["manufacturer"] == "TurtleAV"
    assert INFO["transport"] == "tcp"
    assert INFO["version"] == "1.1.9"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    # The 0.25.0 floor is the package move: this file imports openavc.*.
    assert INFO["min_platform_version"] == "0.25.0"


# ── Command surface consistency ──

def test_every_command_has_a_handler():
    for name in INFO["commands"]:
        assert (name in drv._COMMAND_TEMPLATES or name in drv._LIFECYCLE_COMMANDS
                or name in _SPECIAL), f"{name} has no handler"


def test_no_orphan_templates_or_lifecycle():
    for name in drv._COMMAND_TEMPLATES:
        assert name in INFO["commands"], f"template {name} not declared"
    for name in drv._LIFECYCLE_COMMANDS:
        assert name in INFO["commands"], f"lifecycle {name} not declared"


def test_template_placeholders_have_params():
    fmt = string.Formatter()
    sources = list(drv._COMMAND_TEMPLATES.items())
    sources += [(k, v["template"]) for k, v in drv._LIFECYCLE_COMMANDS.items()]
    for name, tpl in sources:
        placeholders = {f for _, f, _, _ in fmt.parse(tpl) if f}
        params = set(INFO["commands"][name].get("params", {}))
        missing = placeholders - params
        assert not missing, f"{name}: placeholders without params: {missing}"


def test_child_id_params_reference_declared_types():
    types = set(INFO["child_entity_types"])
    for name, cdef in INFO["commands"].items():
        for pname, pdef in cdef.get("params", {}).items():
            if pdef.get("type") == "child_id":
                assert pdef.get("child_type") in types, f"{name}.{pname}"


def test_enum_params_have_values():
    for name, cdef in INFO["commands"].items():
        for pname, pdef in cdef.get("params", {}).items():
            if pdef.get("type") == "enum":
                assert pdef.get("values"), f"{name}.{pname} enum has no values"


def test_writethrough_targets_are_real_commands_and_state():
    enc_state = INFO["child_entity_types"]["encoder"]["state_variables"]
    for cmd, (ctype, id_param, fn) in drv._WRITETHROUGH.items():
        assert cmd in INFO["commands"], f"write-through {cmd} not a command"
        assert ctype == "encoder"
        # the produced keys must all be declared encoder state vars
        sample = {id_param: 1, "rate": "2", "type": "1", "audio": "ON",
                  "sh": "640", "sv": "540", "format": "AAC"}
        for key in fn(sample):
            assert key in enc_state, f"{cmd} writes undeclared state {key}"


# ── GET STATUS parser ──

def test_parse_status_empty_roster():
    ps = drv._parse_status(fx.BANNER_STATUS_EMPTY)
    assert ps["encoders"] == {}
    assert ps["decoders"] == {}
    s = ps["system"]
    assert s["firmware"] == "1.50.02"
    assert s["power"] is True and s["ir"] is True
    assert s["web"] is True and s["https"] is False and s["ssh"] is False
    assert s["telnet_port"] == "23"
    assert s["lan2_ip"] == "192.168.4.33"
    assert s["lan2_subnet_mask"] == "255.255.252.0"
    assert s["lan1_mac"] == "18:66:96:11:11:48"
    assert s["hostname"] == "Controller.local"


def test_parse_status_populated_roster():
    ps = drv._parse_status(fx.BANNER_STATUS_POPULATED)
    assert sorted(ps["encoders"]) == [1]
    e = ps["encoders"][1]
    assert e["ip"] == "169.254.10.1" and e["net"] is True and e["signal_present"] is False
    assert e["edid"] == "DF000"
    assert e["audio_format"] == "PCM"  # AudioFormat column is device-reported
    assert sorted(ps["decoders"]) == [1]
    de = ps["decoders"][1]
    assert de["source"] == 1 and de["net"] is True and de["hpd"] is False
    assert de["resolution"] == "1080p@60" and de["mode"] == "MX" and de["hdcp"] == "SNK"


# ── GET ENC/DEC detail parsers ──

def test_parse_encoder_detail():
    e = drv._parse_encoder_detail(fx.BANNER_ENC)
    assert e["net"] is True and e["signal_present"] is False
    assert e["firmware"] == "3.12.02"
    assert e["edid"] == "DF000" and e["audio_input"] == "HDMI"
    assert e["multicast"] is True and e["name"] == "Encoder 001"
    assert e["fpled"] == "9"
    assert e["guest_enabled"] is False and e["guest_baud"] == "9" and e["guest_framing"] == "8n1"
    assert e["mac"] == "18:66:96:11:0D:EB"
    assert e["ip"] == "169.254.10.1" and e["gateway"] == "169.254.8.1"
    assert e["subnet_mask"] == "255.255.0.0"


def test_parse_decoder_detail():
    x = drv._parse_decoder_detail(fx.BANNER_DEC)
    assert x["net"] is True and x["hpd"] is False and x["firmware"] == "3.12.01"
    assert x["mode"] == "MX" and x["resolution"] == "1080p@60" and x["rotate"] == "0"
    assert x["name"] == "Decoder 001"
    assert x["source"] == 1
    assert x["lock_video"] == 0 and x["lock_ir"] == 0 and x["lock_rs232"] == 0 and x["lock_usb"] == 0
    assert x["multicast"] is True
    assert x["aspect"] == "Maintain" and x["hdcp"] == "SNK"
    assert x["ir_enabled"] is True and x["button"] is True and x["fpled"] == "9"
    assert x["guest_baud"] == "9" and x["guest_framing"] == "8n1"
    assert x["video_output"] is True and x["video_mute"] is False and x["pause"] is False
    assert x["auto"] is True and x["video_lost_timeout"] == 0
    assert x["mac"] == "18:66:96:11:0D:1D" and x["ip"] == "169.254.20.1"


def test_parse_gpio():
    g = drv._parse_gpio(fx.BANNER_GPIO)
    assert g["gpio1_dir"] == "In" and g["gpio1_level"] == 1
    assert g["gpio4_dir"] == "In"


def test_parse_wall_empty():
    assert drv._parse_wall_status(fx.BANNER_WALL_EMPTY) == {}


def test_routing_signals_have_no_audio_or_cec():
    # Darwin routes VIDEO/IR/RS232/USB only (audio embedded, no CEC).
    assert "AUDIO" not in drv.SIGNAL_TYPES
    assert "CEC" not in drv.SIGNAL_TYPES
    assert set(drv.SIGNAL_TYPES) == {"ALL", "VIDEO", "IR", "RS232", "USB"}


def test_hotkey_k0_is_unpadded():
    """The KVM hotkey modifier (k0) must be an UNPADDED 1-9 enum.

    Live hardware (FW 1.50.02) rejects a zero-padded modifier with
    ``[ERROR]OUT parameter out of range`` (9/9 rejected) and accepts the
    unpadded form (9/9 accepted). The IDE enum once shipped padded
    ("01".."09"), which made the hotkey command always fail when driven from
    the picker. Guard against regressing to the padded form, and confirm the
    rendered wire carries a single-digit modifier.
    """
    k0 = INFO["commands"]["dec_hotkey_set"]["params"]["k0"]
    assert k0["values"] == [str(x) for x in range(1, 10)]
    assert all(not v.startswith("0") for v in k0["values"])
    wire = drv._COMMAND_TEMPLATES["dec_hotkey_set"].format(
        decoder_id=1, nn=1, k0="1", k1=65, action="PULL", encoder_id=2)
    assert wire == "SET DEC 1 HOTKEY 1 KEY 1 65 ACTION PULL SRC 2"


# ── Connection lifecycle ────────────────────────────────────────────────────
#
# The controller speaks first (Telnet negotiation + banner), the driver
# consumes the banner before the platform declares connected, primes state
# best-effort, and clears its receive machinery on every teardown path.

def _make_driver(**config_overrides):
    global _SILENT, _FAIL_SENDS
    _SILENT = False
    _FAIL_SENDS = False
    _FakeTransport.sent_lines = []
    _FakeTransport.created_kwargs = {}
    _FakeTransport.last = None
    cfg = {"host": "192.168.4.33", "port": 23, "poll_interval": 10}
    cfg.update(config_overrides)
    return drv.DarwinControlDriver("ctl1", cfg, _FakeState(), _FakeEvents())


@pytest.mark.asyncio
async def test_connect_consumes_banner_then_syncs():
    d = _make_driver()
    await d.connect()
    assert d._connected is True
    assert d.state.data["device.ctl1.connected"] is True
    assert "device.connected.ctl1" in d.events.emitted
    # The transport was opened raw — prompt framing is the driver's job.
    assert _FakeTransport.created_kwargs["delimiter"] is None
    # The greeting (IAC bytes included) was consumed, and the initial status
    # sync went out on the wire.
    assert d._responses.empty()
    assert "GET STATUS" in _FakeTransport.sent_lines
    assert "GET WALL STATUS" in _FakeTransport.sent_lines
    # Polling was started by the platform lifecycle from config.
    assert d.polling == 10


@pytest.mark.asyncio
async def test_connect_banner_timeout_is_a_clean_teardown(monkeypatch):
    global _SILENT
    monkeypatch.setattr(drv, "BANNER_TIMEOUT_S", 0.05)
    d = _make_driver()
    _SILENT = True
    closes = []
    orig_close = d._close_session

    async def spying_close_session():
        closes.append(True)
        await orig_close()

    d._close_session = spying_close_session
    with pytest.raises(ConnectionError, match="No banner/prompt from controller"):
        await d.connect()
    assert d._connected is False
    assert d.transport is None
    assert _FakeTransport.last.connected is False  # closed, not leaked
    # _close_session ran twice: the pre-attempt clean slate, then the
    # handshake-failure teardown.
    assert len(closes) == 2
    assert "device.connected.ctl1" not in d.events.emitted


@pytest.mark.asyncio
async def test_connect_survives_failed_initial_sync():
    global _FAIL_SENDS
    d = _make_driver()
    _FAIL_SENDS = True  # banner arrives, then every command send fails
    await d.connect()
    # The status/roster priming is best-effort: the device still comes up
    # and steady-state polling retries on the next cycle.
    assert d._connected is True
    assert "device.disconnected.ctl1" not in d.events.emitted
    assert d.polling == 10


@pytest.mark.asyncio
async def test_reconnect_starts_from_a_clean_slate():
    d = _make_driver()
    d._rx_buffer = b"half a banner from a dead session"
    d._iac_state = "sb"
    d._responses.put_nowait("stale response from a dead session")
    await d.connect()
    # The stale response was drained and the IAC filter reset before the
    # socket opened, so the real greeting parsed and was consumed.
    assert d._connected is True
    assert d._responses.empty()


@pytest.mark.asyncio
async def test_disconnect_clears_receive_state():
    d = _make_driver()
    await d.connect()
    d._rx_buffer = b"partial line"
    d._iac_state = "iac"
    d._responses.put_nowait("unclaimed")
    await d.disconnect()
    assert d._connected is False
    assert d.transport is None
    assert d._rx_buffer == b""
    assert d._iac_state == "normal"
    assert d._responses.empty()
    assert "device.disconnected.ctl1" in d.events.emitted


@pytest.mark.asyncio
async def test_transport_drop_also_clears_receive_state():
    d = _make_driver()
    await d.connect()
    d._rx_buffer = b"partial line"
    transport = d.transport
    d._handle_transport_disconnect()
    for _ in range(3):
        await asyncio.sleep(0)
    assert d._connected is False
    assert d.transport is None
    assert transport.connected is False
    assert d._rx_buffer == b""
    assert "device.disconnected.ctl1" in d.events.emitted
