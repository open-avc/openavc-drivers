"""Unit tests for the chazy_control (standard, non-Pro) driver.

Loads ``switchers/chazy_control.py`` directly, stubbing the ``openavc.*``
imports it needs (BaseDriver, get_logger) so the community repo's test suite
stays self-contained — mirrors test_chazy_control_pro.py.

The standard Control's command set is a documented strict subset of the Control
Pro. These tests lock that subset in: the shared encoder/decoder/video-wall/
Dante-routing/network/GPIO surface is present, and the Pro-only modules (media,
group, event, schedule, config-preset, Dante-preset, date/NTP) are absent. The
banner parsers are byte-identical to the Pro's (validated against hardware in
test_chazy_control_pro.py); their behaviour on non-Pro-identity banners is
proven by the sim round-trip in test_chazy_control_sim.py.

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
    CHILD_NOT_RESPONDING,
    StubBaseDriver as _StubBaseDriver,
    StubEvents as _FakeEvents,
    StubState as _FakeState,
    install_connection_fault_stub,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "chazy_control.py"

# Commands that exist only on the Control Pro — must NOT appear here.
PRO_ONLY_COMMANDS = {
    # Media Player
    "media_add", "media_delete", "media_addr_list", "media_addr_ping",
    "media_set_id", "media_set_name", "media_set_type", "media_set_addr_file",
    "media_set_user", "media_transparency_on", "media_transparency_off",
    "media_reload",
    # Groups
    "group_create", "group_delete", "group_set_name", "group_add_dec",
    "group_del_dec", "group_switch",
    # Events
    "event_create", "event_delete", "event_set_name", "event_set_type",
    "event_set_addr", "event_set_addr_port", "event_set_data",
    "event_set_data_hex", "event_set_params", "event_set_request",
    "event_set_resend_delay", "event_start", "event_stop",
    # Scheduler
    "schedule_create", "schedule_delete", "schedule_set_name",
    "schedule_set_color", "schedule_set_time_type", "schedule_set_week_type",
    "schedule_set_date", "schedule_set_time", "schedule_action_dec_enc",
    "schedule_action_dec_media", "schedule_action_group_enc",
    "schedule_action_group_media", "schedule_action_dante_preset",
    "schedule_action_event", "schedule_delete_action", "schedule_start",
    "schedule_stop",
    # Configuration + Dante presets
    "config_preset_save", "config_preset_delete", "config_preset_apply",
    "dante_preset_create", "dante_preset_delete", "dante_preset_set_name",
    "dante_preset_apply",
    # Date / time
    "set_date", "set_ntp_server",
}


# ── Platform stand-ins ──────────────────────────────────────────────────────

# Controller-side scripting for the fake transport: the greeting played the
# moment the socket opens (Telnet negotiation + welcome banner + first
# prompt), a switch that keeps the controller silent (banner-timeout path),
# and one that fails every command send (link died right after the banner).
_GREETING = (
    bytes([0xFF, 0xFB, 0x01, 0xFF, 0xFB, 0x03])  # IAC WILL ECHO, IAC WILL SGA
    + b"Welcome to TAV-CHAZY-CLT\r\nFW Version: 1.00.17\r\n\r\nCONTROLLER> "
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
        # All four reserved props, the way the platform injects them —
        # the driver writes the fault pair through child_fault().
        schema.setdefault("online", {"type": "boolean"})
        schema.setdefault("label", {"type": "string"})
        schema.setdefault("offline_reason", {"type": "string"})
        schema.setdefault("offline_detail", {"type": "string"})
        return schema

    # The platform's own rule, so a driver asserting a code this taxonomy
    # does not define fails here the way it would on a real box.
    child_fault = staticmethod(_StubBaseDriver.child_fault)

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
        st["offline_reason"] = ""
        st["offline_detail"] = ""
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
    install_connection_fault_stub()
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="chazy": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_server_stubs()
drv = _load(DRIVER_PATH, "chazy_control_under_test")
INFO = drv.ChazyControlDriver.DRIVER_INFO


# ── Identity ──

def test_driver_identity():
    assert INFO["id"] == "chazy_control"
    assert INFO["manufacturer"] == "TurtleAV"
    assert INFO["transport"] == "tcp"
    assert INFO["version"] == "1.4.0"
    # The floor is the newest platform surface the driver CALLS. That was the
    # 0.25.0 package move until it began asserting a child fault code, which
    # is BaseDriver.child_fault() and arrived in 0.29.0.
    assert INFO["min_platform_version"] == "0.29.0"
    assert INFO["simulated"] is True


# ── Command surface consistency (mirrors the Pro suite) ──

def test_every_command_has_a_handler():
    special = {"search", "add_auto_all"} | set(drv._RESET_CONFIRM)
    for name in INFO["commands"]:
        assert (name in drv._COMMAND_TEMPLATES or name in drv._LIFECYCLE_COMMANDS
                or name in special), f"{name} has no handler"


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
                assert pdef.get("values"), f"{name}.{pname} enum missing values"


def test_reset_commands_present():
    for name in ("reset_system_confirm", "reset_network_confirm", "reset_all_confirm"):
        assert name in INFO["commands"]


# ── Subset correctness: shared surface present, Pro-only absent ──

def test_documented_command_surface_present():
    # The encoder/decoder/video-wall/Dante-routing/network/GPIO/search surface
    # the FW 1.00.17 reference documents must all be present.
    for name in (
        "reboot_controller", "set_rs232_baud",
        "enc_set_name", "enc_static_ip", "enc_preset_apply", "enc_ipmode",
        "enc_guest_config", "enc_switch_arc", "enc_reset",
        "dec_route", "dec_static_ip", "dec_preset_apply", "dec_output_osd",
        "dec_reset",
        "wall_create", "wall_delete", "wall_apply_preset", "wall_preset_class",
        "dante_set_name", "dante_rxchn_subscribe", "dante_txflow_add",
        "dante_search",
        "search", "add_auto_all", "add_dev_enc", "add_dev_reset",
        "gpio_dir", "gpio_level",
        "net_dhcp", "net_telnet_port", "net_hostname",
    ):
        assert name in INFO["commands"], f"missing documented command {name}"


def test_undocumented_pro_carryovers_absent():
    # Commands the FW 1.00.17 reference does NOT document (Pro carryovers) were
    # removed to match the doc (see chazy-control-review-report.md). Re-adding
    # one without a doc to back it should fail this test.
    for name in (
        "net_ssh", "net_ssh_port", "net_telnet",
        "enc_usbmode", "enc_source", "enc_source_auto_priority", "enc_fan",
        "enc_audio_stream", "enc_lanmode", "enc_lan2_ipmode",
        "enc_sendguest_ascii", "enc_sendguest_hex",
        "dec_audio_stream", "dec_output_freeze", "dec_ull",
        "dec_dante_audio_source", "dec_hotkey", "dec_hotkey_del", "dec_lanmode",
        "dec_sendguest_ascii", "dec_sendguest_hex",
        "dante_preferred", "dante_aes67", "dante_reboot", "dante_clear_config",
        "dante_interface_static", "dante_interface_dynamic", "dante_event_clear",
    ):
        assert name not in INFO["commands"], f"undocumented command present: {name}"
        assert name not in drv._COMMAND_TEMPLATES, f"undocumented template: {name}"


def test_pro_only_commands_absent():
    present = set(INFO["commands"])
    leaked = present & PRO_ONLY_COMMANDS
    assert not leaked, f"Pro-only commands leaked into chazy_control: {sorted(leaked)}"
    # And none survive in the wire/lifecycle tables either.
    for name in PRO_ONLY_COMMANDS:
        assert name not in drv._COMMAND_TEMPLATES, f"{name} in templates"
        assert name not in drv._LIFECYCLE_COMMANDS, f"{name} in lifecycle"


# ── Command bounds match the FW 1.00.17 reference ──

def test_dec_route_signals_match_documented_switch_types():
    # FW 1.00.17 §3.3-3.9: ALL/VIDEO/AUDIO/IR/RS232/USB/CEC. No MEDIA — the
    # standard Control has no Media Player module to route from.
    sig = INFO["commands"]["dec_route"]["params"]["signal"]["values"]
    assert sig == ["ALL", "VIDEO", "AUDIO", "IR", "RS232", "USB", "CEC"]
    assert "MEDIA" not in sig


def test_help_ranges_match_fw_1_00_17_tables():
    edid = INFO["commands"]["enc_edid_default"]["params"]["edid"]["help"]
    assert "00-23" in edid and "101" not in edid  # §4.6 EDID table
    res = INFO["commands"]["dec_output_resolution"]["params"]["resolution"]["help"]
    assert "00-13" in res  # §3.14 resolution table


def test_add_dev_allows_auto_assign_zero():
    # §8.5/§8.6: enc/dec id 0 = auto-assign next free ID.
    assert INFO["commands"]["add_dev_enc"]["params"]["encoder_id"]["min"] == 0
    assert INFO["commands"]["add_dev_dec"]["params"]["decoder_id"]["min"] == 0


def test_ss_encoder_state_present():
    # FW 1.00.17 §6 TX SS module read path.
    enc = INFO["child_entity_types"]["encoder"]["state_variables"]
    assert "mainstream_url" in enc and "substream_url" in enc
    # Generic preview convention surfaced to the Video Panel plugin.
    assert "preview_url" in enc and "preview_format" in enc


def test_derive_preview_classifies_stream():
    assert drv._derive_preview("http://169.254.10.1:8080/?action=stream", "") == (
        "http://169.254.10.1:8080/?action=stream", "mjpeg",
    )
    assert drv._derive_preview("", "rtsp://169.254.5.5:554/sub") == (
        "rtsp://169.254.5.5:554/sub", "rtsp",
    )
    assert drv._derive_preview("", "") == ("", "")


# ── child_entity_types schema (subset of the Pro's nine) ──

def test_child_entity_types_are_the_subset():
    types = INFO["child_entity_types"]
    assert set(types) == {"encoder", "decoder", "video_wall"}
    assert types["encoder"]["id_format"] == {
        "type": "integer", "min": 1, "max": 762, "pad_width": 3}
    assert types["decoder"]["id_format"]["max"] == 762
    assert types["video_wall"]["id_format"]["max"] == 9  # FW 1.00.17 §7 hdl [01..09]


def test_child_types_do_not_declare_reserved_props():
    for ctype, cdef in INFO["child_entity_types"].items():
        for prop in cdef.get("state_variables", {}):
            assert prop not in ("online", "label"), f"{ctype} declares {prop}"


def test_cloud_priority_tags_present():
    enc = INFO["child_entity_types"]["encoder"]["state_variables"]
    assert enc["signal_present"]["cloud_priority"] == "high"
    dec = INFO["child_entity_types"]["decoder"]["state_variables"]
    assert dec["source_video"]["cloud_priority"] == "high"
    assert dec["source_ir"]["cloud_priority"] == "low"


def test_lifecycle_only_enc_dec_wall():
    # No group/event/schedule/media/dante_preset/config_preset lifecycle.
    assert set(drv._LIFECYCLE_COMMANDS) == {
        "enc_delete", "enc_set_id", "dec_delete", "dec_set_id",
        "wall_create", "wall_delete",
    }
    for spec in drv._LIFECYCLE_COMMANDS.values():
        assert spec["child_type"] in {"encoder", "decoder", "video_wall"}


# ── System state-variables: Date/NTP module dropped, Network kept ──

def test_state_vars_drop_date_ntp_keep_network():
    sv = INFO["state_variables"]
    assert "date" not in sv and "ntp_server" not in sv
    # The core network/system vars the standard Control reports in GET STATUS.
    for name in ("lan1_ip", "lan2_ip", "hostname", "telnet_port", "ssh",
                 "https", "gpio1_dir"):
        assert name in sv, f"network/system var {name} missing"
    # FW 1.00.17 GET STATUS (§2.2) has no DNS-server block, so these can never
    # populate and are not declared (see report H5).
    for name in ("dns_mode", "dns_preferred", "dns_alternate"):
        assert name not in sv, f"{name} declared but unpopulatable on FW 1.00.17"


# ── Helpers (shared parser primitives) ──

def test_norm_ip():
    assert drv._norm_ip("169.254.010.001") == "169.254.10.1"
    assert drv._norm_ip("255.255.000.000") == "255.255.0.0"
    assert drv._norm_ip("not-an-ip") == "not-an-ip"


def test_split_columns_blank_middle():
    header = "ENC     Type                EDID    IP               NET/Sig"
    data = "001                         DF000   169.254.010.001  Off/Off"
    cols = drv._split_columns(header, data)
    assert cols["ENC"] == "001"
    assert cols["Type"] == ""           # blank middle column preserved
    assert cols["EDID"] == "DF000"
    assert cols["NET/Sig"] == "Off/Off"


# ── Video-wall enumeration (the standard Control's one queryable config type) ──
#
# The parser keys off the VW Col Row CfgSel Name row, so it's identity-agnostic
# (same on the standard Control and the Pro). No standalone Control hardware
# exists, so the banner here is the shared family layout with CHAZY CONTROL
# identity rather than a hardware fixture.

def test_parse_wall_status():
    banner = (
        "================================================================\n"
        "              CHAZY CONTROL Video Wall Info\n"
        "              FW Version: 1.00.17\n"
        "\n"
        "VW  Col    Row    CfgSel  Name\n"
        "01  02     02     01      NULL\n"
        "    OutID\n"
        "    --- --- --- ---\n"
        "    Cfg    Name\n"
        "    01     Preset 1\n"
        "           Class  From    Screen\n"
        "           A      001     H01V01 H02V01 H01V02 H02V02\n"
        "================================================================"
    )
    w = drv._parse_wall_status(banner)
    assert w == {1: {"name": "", "columns": 2, "rows": 2}}


def test_parse_wall_status_empty():
    banner = (
        "================================================================\n"
        "              CHAZY CONTROL Video Wall Info\n"
        "\n"
        "No Video Wall\n"
        "================================================================"
    )
    assert drv._parse_wall_status(banner) == {}


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
    cfg = {"host": "192.168.4.60", "port": 23, "poll_interval": 10}
    cfg.update(config_overrides)
    return drv.ChazyControlDriver("ctl1", cfg, _FakeState(), _FakeEvents())


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

# ── An enrolled endpoint that is not answering ──────────────────────────────
#
# The controller keeps an endpoint in its database once enrolled, so one it
# still lists whose link is down is one it expects and cannot reach. The
# driver detected that all along -- `online` went False, which is the hard
# half -- but said nothing about WHY, so `offline_reason` was blank on the
# device page, automation could not match on it, and a panel bound to the key
# drew nothing. The IDE's "not answering" banner is synthesized from `online`
# alone, so an absent endpoint and a wedged one read identically there too.

def test_an_enrolled_endpoint_that_is_not_answering_says_why():
    d = _make_driver()
    d._reconcile_roster("encoder", {1: {"net": False, "name": "Stage TX"}})
    st = d.get_child_state("encoder", 1)
    assert st["online"] is False
    assert st["offline_reason"] == CHILD_NOT_RESPONDING
    # A code with no sentence behind it reaches a person as a bare token.
    assert st["offline_detail"]


def test_an_endpoint_that_is_answering_claims_nothing():
    d = _make_driver()
    d._reconcile_roster("decoder", {1: {"net": True, "name": "Lobby RX"}})
    st = d.get_child_state("decoder", 1)
    assert st["online"] is True
    assert st["offline_reason"] == "" and st["offline_detail"] == ""


def test_the_fault_clears_when_the_endpoint_comes_back():
    # Clearing matters as much as setting: without it one power cut leaves a
    # fault on a working endpoint for as long as the controller stays up.
    d = _make_driver()
    d._reconcile_roster("encoder", {1: {"net": False, "name": "Stage TX"}})
    assert d.get_child_state("encoder", 1)["offline_reason"] == CHILD_NOT_RESPONDING
    d._reconcile_roster("encoder", {1: {"net": True, "name": "Stage TX"}})
    st = d.get_child_state("encoder", 1)
    assert st["online"] is True
    assert st["offline_reason"] == "" and st["offline_detail"] == ""


def test_a_config_child_is_in_service_and_claims_nothing():
    # A group has no link concept -- the controller listing it IS its
    # presence, so it must never be marked as not answering.
    d = _make_driver()
    d._reconcile_children(
        "video_wall", {1: {"name": "All Displays"}}, online_from_net=False,
    )
    st = d.get_child_state("video_wall", 1)
    assert st["online"] is True
    assert st["offline_reason"] == "" and st["offline_detail"] == ""

