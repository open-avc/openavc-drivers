"""Unit tests for the chazy_control_pro driver.

Loads ``switchers/chazy_control_pro.py`` directly, stubbing the ``openavc.*``
imports it needs (BaseDriver, get_logger) so the community repo's test suite
stays self-contained — mirrors test_crestron_cip_discovery.py.

The banner parsers are exercised against byte-exact captures from real
hardware (FW 1.10.11) in fixtures/chazy_control_pro_banners.py, in both the
offline (just-added) and online (linked) device states.

Also covers the connection lifecycle against a functional fake BaseDriver
mirroring the platform's hook-driven connect()/disconnect(): the
controller-speaks-first banner consumption, the banner-timeout teardown, the
best-effort initial sync (including the system-clock read), and the
receive-state clears on every teardown path.
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
DRIVER_PATH = REPO_ROOT / "switchers" / "chazy_control_pro.py"
FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "chazy_control_pro_banners.py"
CHILD_FIXTURES_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "chazy_control_pro_child_banners.py"
)


# ── Platform stand-ins ──────────────────────────────────────────────────────

# Controller-side scripting for the fake transport: the greeting played the
# moment the socket opens (Telnet negotiation + welcome banner + first
# prompt), a switch that keeps the controller silent (banner-timeout path),
# and one that fails every command send (link died right after the banner).
_GREETING = (
    bytes([0xFF, 0xFB, 0x01, 0xFF, 0xFB, 0x03])  # IAC WILL ECHO, IAC WILL SGA
    + b"Welcome to TAV-CHAZY-CLTPRO\r\nFW Version: 1.10.11\r\n\r\nCONTROLLER> "
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
drv = _load(DRIVER_PATH, "chazy_control_pro_under_test")
fx = _load(FIXTURES_PATH, "chazy_fixtures_under_test")
fxc = _load(CHILD_FIXTURES_PATH, "chazy_child_fixtures_under_test")
INFO = drv.ChazyControlProDriver.DRIVER_INFO


# ── Command surface consistency ──

def test_every_command_has_a_handler():
    special = {"search", "add_auto_all", "discover_add_all"} | set(drv._RESET_CONFIRM)
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


def test_full_module_coverage_present():
    # Spot-check that the Pro-only modules and the per-endpoint network/preset
    # commands all made it into the surface (guards against scope regressions).
    for name in (
        "media_add", "group_create", "event_create", "schedule_create",
        "config_preset_save", "dante_preset_create", "set_date", "set_ntp_server",
        "enc_static_ip", "enc_preset_apply", "enc_lan2_ipmode", "enc_guest_config",
        "dec_static_ip", "dec_preset_apply", "dec_hotkey",
        "net_dns", "dante_rxchn_subscribe", "wall_create",
    ):
        assert name in INFO["commands"], f"missing {name}"


# ── child_entity_types schema ──

def test_child_entity_types_declared():
    types = INFO["child_entity_types"]
    assert set(types) == {
        "encoder", "decoder", "video_wall", "group", "event",
        "schedule", "media", "dante_preset", "config_preset",
    }
    assert types["encoder"]["id_format"] == {
        "type": "integer", "min": 1, "max": 762, "pad_width": 3}
    assert types["decoder"]["id_format"]["max"] == 762
    assert types["config_preset"]["id_format"]["max"] == 10


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


# ── Helpers ──

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


# ── GET STATUS parsing ──

def test_parse_status_empty():
    p = drv._parse_status(fx.BANNER_STATUS_EMPTY)
    assert p["encoders"] == {} and p["decoders"] == {}
    assert p["system"]["firmware"] == "1.10.11"
    assert p["system"]["telnet_port"] == "23"


@pytest.mark.parametrize("banner", ["BANNER_STATUS_POPULATED", "BANNER_STATUS_ONLINE"])
def test_parse_status_roster(banner):
    p = drv._parse_status(getattr(fx, banner))
    assert set(p["encoders"]) == {1}
    assert set(p["decoders"]) == {1}
    assert p["encoders"][1]["edid"] == "DF000"
    assert p["encoders"][1]["ip"] == "169.254.10.1"
    assert p["decoders"][1]["source_video"] == 1
    assert p["decoders"][1]["mode"] == "MX"
    assert p["decoders"][1]["resolution"] == "02"
    assert p["system"]["lan2_ip"] == "192.168.4.188"
    assert p["system"]["lan1_mac"] == "18:66:96:11:11:E0"
    assert p["system"]["dns_preferred"] == "192.168.4.1"
    assert p["system"]["hostname"] == "controller.local"


def test_parse_status_online_flags():
    p = drv._parse_status(fx.BANNER_STATUS_ONLINE)
    assert p["encoders"][1]["net"] is True            # "On /Off"
    assert p["encoders"][1]["signal_present"] is False
    assert p["encoders"][1]["gen"] == "TAV-CHAZY4K-TX"
    assert p["decoders"][1]["net"] is True


# ── GET ENC/DEC STATUS detail parsing ──

def test_parse_encoder_detail_offline():
    e = drv._parse_encoder_detail(fx.BANNER_ENC_DETAIL)
    assert e["edid"] == "DF000" and e["audio_input"] == "HDMI"
    assert e["multicast"] is True and e["sac"] == "ARC"
    assert e["guest_enabled"] is False and e["guest_baud"] == "9"
    assert e["ip_mode"] == "Static" and e["mac"] == "18:66:96:11:0A:27"
    assert e["ip"] == "169.254.10.1" and e["subnet_mask"] == "255.255.0.0"
    assert "arc_fix" not in e and "arc_source" not in e  # NA skipped


def test_parse_encoder_detail_online():
    e = drv._parse_encoder_detail(fx.BANNER_ENC_DETAIL_ONLINE)
    assert e["gen"] == "TAV-CHAZY4K-TX"
    assert e["net"] is True and e["firmware"] == "1.10.03"
    assert e["name"] == "Encoder 001"
    assert e["arc_fix"] == 0 and e["arc_source"] == 0
    # Pin rows: port 1 full, port 2 omits IRVOL/PHY (relay still found)
    assert e["io1_dir"] == "Out" and e["io1_relay"] == "Open" and e["io1_phy"] == "Copper"
    assert e["io2_dir"] == "Out" and e["io2_relay"] == "Open"


def test_parse_decoder_detail_online():
    d = drv._parse_decoder_detail(fx.BANNER_DEC_DETAIL_ONLINE)
    assert d["gen"] == "TAV-CHAZY4K-RX" and d["net"] is True
    assert d["mode"] == "MX" and d["resolution"] == "02" and d["rotate"] == "0"
    assert d["name"] == "Decoder 001"
    assert [d["source_video"], d["source_cec"]] == [1, 1]
    assert d["fix_video"] == 0
    assert d["multicast"] is True and d["video_output"] is True and d["video_mute"] is False
    assert d["sac"] == "ARC" and d["osp"] == 4
    assert d["mac"] == "18:66:96:11:08:C0" and d["ip"] == "169.254.20.1"


def test_parse_gpio():
    g = drv._parse_gpio(fx.BANNER_GPIO)
    assert g["gpio1_dir"] == "In" and g["gpio1_level"] == 1
    assert g["gpio4_dir"] == "In" and g["gpio4_level"] == 1


# ── Pre-existing-child enumeration parsers (group/event/wall/dante_preset) ──
#
# Validated against the byte-exact populated banners in
# fixtures/chazy_control_pro_child_banners.py. Both the ALL (list) and DETAIL
# (per-handle) captures parse to the same single-instance view.

@pytest.mark.parametrize("banner", ["BANNER_GROUP_ALL", "BANNER_GROUP_DETAIL"])
def test_parse_group_status(banner):
    g = drv._parse_group_status(getattr(fxc, banner))
    assert set(g) == {1}
    assert g[1]["name"] == "TestGroup"
    assert g[1]["member_count"] == 1


@pytest.mark.parametrize("banner", ["BANNER_EVENT_ALL", "BANNER_EVENT_DETAIL"])
def test_parse_event_status(banner):
    e = drv._parse_event_status(getattr(fxc, banner))
    assert set(e) == {1}
    assert e[1]["name"] == "TestEvent"
    assert e[1]["event_type"] == "TCP"
    assert e[1]["address"] == ""
    assert "running" not in e[1]  # no banner field for it; defaults on register


@pytest.mark.parametrize("banner", ["BANNER_WALL_ALL", "BANNER_WALL_DETAIL"])
def test_parse_wall_status(banner):
    w = drv._parse_wall_status(getattr(fxc, banner))
    assert set(w) == {1}
    assert w[1]["columns"] == 2
    assert w[1]["rows"] == 2
    assert w[1]["name"] == ""  # "NULL" sentinel normalised to empty


@pytest.mark.parametrize(
    "banner", ["BANNER_DANTE_PRESET_ALL", "BANNER_DANTE_PRESET_DETAIL"]
)
def test_parse_dante_preset_status(banner):
    p = drv._parse_dante_preset_status(getattr(fxc, banner))
    assert set(p) == {1}
    assert p[1]["name"] == "TestDP"


def test_config_child_parsers_handle_empty():
    # Real empty banners (FW 1.10.11, from the live probe): GROUP and EVENT are
    # firmware-mislabeled "Dante Preset Info" and have NO closing sentinel;
    # WALL and DANTE PRESET carry their own title and a closing sentinel. All
    # must parse to {}.
    sentinel = "=" * 64
    cases = (
        ("Dante Preset Info", "No Group", drv._parse_group_status, False),
        ("Dante Preset Info", "No Event", drv._parse_event_status, False),
        ("Video Wall Info", "No Video Wall", drv._parse_wall_status, True),
        ("Dante Preset Info", "No Dante Preset", drv._parse_dante_preset_status, True),
    )
    for title, body, parser, closed in cases:
        banner = (
            f"{sentinel}\n"
            f"              TAV-CHAZY-CLTPRO {title}\n"
            f"              FW Version: 1.10.11\n\n"
            f"{body}\n"
        )
        if closed:
            banner += f"{sentinel}\n"
        assert parser(banner) == {}, f"{body!r} should parse to empty"


def test_parse_success_line():
    # Live GET DATE / GET NTP SERVER replies (FW 1.10.11): [SUCCESS] prefix +
    # trailing period, date carries a (TZ) suffix that must be preserved.
    assert (
        drv._parse_success_line("[SUCCESS]2026-05-31 03:55:35 (Australia/Sydney).")
        == "2026-05-31 03:55:35 (Australia/Sydney)"
    )
    assert drv._parse_success_line("[SUCCESS]time.nist.gov.") == "time.nist.gov"
    # An IP NTP server keeps its dotted form (only the sentence period drops).
    assert drv._parse_success_line("[SUCCESS]192.168.4.1.") == "192.168.4.1"
    # Error / empty replies yield None so the state key isn't overwritten.
    assert drv._parse_success_line("[ERROR]Unknown parameter.") is None
    assert drv._parse_success_line("") is None


def test_event_schedule_schema_drops_unpopulated_keys():
    # Keys with no GET read-back were removed so the IDE doesn't show them
    # permanently blank (review findings #7, #8).
    ce = INFO["child_entity_types"]
    assert "running" not in ce["event"]["state_variables"]
    assert ce["event"]["summary_fields"] == ["name", "event_type", "address"]
    assert set(ce["schedule"]["state_variables"]) == {"name"}
    assert ce["schedule"]["summary_fields"] == ["name"]


def test_parse_ss_status():
    # Byte-exact GET ENC 1 SS STATUS from FW 1.10.11 (Gen-2 MJPEG mainstream,
    # no substream). Mainstream URL uses the de-padded encoder IP.
    banner = (
        "=" * 64 + "\n"
        "              TAV-CHAZY-CLTPRO Secondary Stream Info\n"
        "\n"
        "ID    WorkMode    Version\n"
        "001   NA\n"
        "    >>MainStream URL\n"
        "      http://169.254.10.1:8080/?action=stream\n"
        "    >>SubStream URL\n"
        "      NA\n"
        "\n" + "=" * 64
    )
    ss = drv._parse_ss_status(banner)
    assert ss["mainstream_url"] == "http://169.254.10.1:8080/?action=stream"
    assert ss["substream_url"] == ""  # NA normalises to empty


def test_derive_preview_classifies_stream():
    # Gen-2 MJPEG mainstream is the preview source.
    assert drv._derive_preview("http://169.254.10.1:8080/?action=stream", "") == (
        "http://169.254.10.1:8080/?action=stream", "mjpeg",
    )
    # A Gen-1 RTSP substream classifies as rtsp and is used when no mainstream.
    assert drv._derive_preview("", "rtsp://169.254.5.5:554/sub") == (
        "rtsp://169.254.5.5:554/sub", "rtsp",
    )
    # Offline / NA -> empty (no preview advertised).
    assert drv._derive_preview("", "") == ("", "")


def test_encoder_declares_stream_urls():
    enc_vars = INFO["child_entity_types"]["encoder"]["state_variables"]
    assert "mainstream_url" in enc_vars and "substream_url" in enc_vars
    # Generic preview convention surfaced to the Video Panel plugin.
    assert "preview_url" in enc_vars and "preview_format" in enc_vars
    assert enc_vars["preview_format"]["values"] == ["mjpeg", "rtsp"]


# ── Identity ────────────────────────────────────────────────────────────────

def test_driver_identity():
    assert INFO["id"] == "chazy_control_pro"
    assert INFO["transport"] == "tcp"
    assert INFO["version"] == "1.4.11"
    # The connection lifecycle hooks this driver overrides ship in 0.24.0.
    # The 0.25.0 floor is the package move: this file imports openavc.*.
    assert INFO["min_platform_version"] == "0.25.0"


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
    cfg = {"host": "192.168.4.188", "port": 23, "poll_interval": 10}
    cfg.update(config_overrides)
    return drv.ChazyControlProDriver("ctl1", cfg, _FakeState(), _FakeEvents())


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
    # sync — including the system-clock read — went out on the wire.
    assert d._responses.empty()
    assert "GET STATUS" in _FakeTransport.sent_lines
    assert "GET DATE" in _FakeTransport.sent_lines
    assert "GET NTP SERVER" in _FakeTransport.sent_lines
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
