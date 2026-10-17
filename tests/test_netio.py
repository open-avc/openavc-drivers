"""Driver + simulator tests for netio (NETIO JSON M2M API and web login).

Two kinds of evidence:

  - What NETIO's public demo units sent, parsed byte for byte: the JSON status
    documents of 2026-10-13 (tests/fixtures/netio/: a PowerPDU 8QS, 4PS and
    4KS, a PowerDIN 4PZ, a PowerCable 2PZ and a PowerBOX 4KF on firmware 5.3.1
    and 5.3.2, the shorter answer the 4KS gave to a switch, and the error body
    it gave to a refused write), and the web login's replies of 2026-10-16
    (tests/fixtures/netio/web/: the 4KS, 4KF, 8QS and 4PS).
  - A dual-proof round trip: the real driver wired to the real simulator, over
    an in-memory httpx transport for the JSON API and an in-memory WebSocket
    for the web login; every command, every login, every refusal.

The driver is loaded with the ``openavc.*`` and ``websockets`` imports stubbed
so the community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    ConnectionFaultError,
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "power" / "netio.py"
SIM_PATH = REPO_ROOT / "power" / "netio_sim.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "netio"
WEB_FIXTURES = FIXTURES / "web"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect lifecycle for a driver that owns its
    session; state and children come from the shared stubs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.traffic: list[tuple[str, str]] = []
        self.redacted: list[str] = []
        self.dropped = 0

    async def _verify_reachable(self, host, port, timeout=3.0):
        return True

    async def connect(self):
        await self._close_session()
        await self._create_transport("http")
        try:
            await self._post_connect()
        except Exception:
            await self._close_session()
            raise
        self._connected = True
        self.set_state("connected", True)
        await self._initial_sync()

    async def disconnect(self):
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)

    def redact_in_log(self, value):
        self.redacted.append(value)

    def record_traffic(self, direction, data, *, meta=None):
        self.traffic.append((direction, data if isinstance(data, str) else data.decode()))

    def _handle_transport_disconnect(self):
        self.dropped += 1
        self._connected = False
        self.set_state("connected", False)


class _FakeConnectionClosed(Exception):
    """websockets.exceptions.ConnectionClosed."""


class _FakeInvalidHandshake(Exception):
    """websockets.exceptions.InvalidHandshake."""


class _FakeInvalidStatus(_FakeInvalidHandshake):
    """websockets.exceptions.InvalidStatus: carries the HTTP answer."""

    def __init__(self, status):
        super().__init__(f"server rejected WebSocket connection: HTTP {status}")
        self.response = SimpleNamespace(status_code=status)


_WS_EXC = ModuleType("websockets.exceptions")
_WS_EXC.ConnectionClosed = _FakeConnectionClosed
_WS_EXC.InvalidHandshake = _FakeInvalidHandshake
_WS_EXC.InvalidStatus = _FakeInvalidStatus

install_stubs(
    {"websockets": {"connect": None, "exceptions": _WS_EXC},
     "websockets.exceptions": {"ConnectionClosed": _FakeConnectionClosed,
                               "InvalidHandshake": _FakeInvalidHandshake,
                               "InvalidStatus": _FakeInvalidStatus}},
    base_driver=_FakeBaseDriver,
)
DRV = load_module("netio_under_test", DRIVER_PATH)
SIM = load_module("netio_sim_under_test", SIM_PATH)
INFO = DRV.NetioDriver.DRIVER_INFO


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ── Harness ──────────────────────────────────────────────────────────────────


class _Harness:
    def __init__(self, sim_state=None, driver_config=None):
        self.sim = SIM.NetioSimulator("netio-sim", {})
        for key, value in (sim_state or {}).items():
            self.sim.set_state(key, value)
        self.requests: list[httpx.Request] = []
        cfg = {
            "host": "10.0.0.30", "port": 80, "ssl": False, "verify_ssl": False,
            "username": "netio", "password": "netio", "write_username": "",
            "write_password": "", "poll_interval": 0,
        }
        cfg.update(driver_config or {})
        self.driver = DRV.NetioDriver("pdu1", cfg, StubState(), StubEvents())

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = dict(request.headers)
        result = self.sim.handle_request(
            request.method, request.url.raw_path.decode("ascii"), headers,
            request.content.decode("utf-8"),
        )
        status, body, resp_headers = result if len(result) == 3 else (*result, {})
        if isinstance(body, dict):
            return httpx.Response(status, json=body, headers=resp_headers)
        return httpx.Response(status, text=str(body), headers=resp_headers)

    async def connect(self):
        original = httpx.AsyncClient

        def _mocked(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(self.handler)
            return original(*args, **kwargs)

        httpx.AsyncClient = _mocked  # type: ignore[assignment]
        try:
            await self.driver.connect()
        finally:
            httpx.AsyncClient = original  # type: ignore[assignment]

    def st(self, key):
        return self.driver.state.get(f"device.{self.driver.device_id}.{key}")

    def out(self, n, key):
        return self.st(f"output.{n}.{key}")


async def _connected(sim_state=None, driver_config=None) -> _Harness:
    h = _Harness(sim_state, driver_config)
    await h.connect()
    return h


def _run(coro):
    return asyncio.run(coro)


def _basic(request: httpx.Request) -> tuple[str, str] | None:
    value = request.headers.get("authorization", "")
    if not value.lower().startswith("basic "):
        return None
    user, _, password = base64.b64decode(value[6:]).decode().partition(":")
    return user, password


# ── Metadata ─────────────────────────────────────────────────────────────────


def test_every_declared_command_has_a_dispatch_branch():
    assert set(DRV.NetioDriver._DISPATCH) == set(INFO["commands"])


def test_actions_and_params_point_at_declared_things():
    for action in INFO["actions"]:
        assert action.get("command", action["id"]) in INFO["commands"]
    for name in INFO["quick_actions"]:
        assert name in INFO["commands"]
    for name, cmd in INFO["commands"].items():
        for pname, pdef in cmd["params"].items():
            if pdef.get("child_type"):
                assert pdef["child_type"] in INFO["child_entity_types"], (name, pname)
    assert set(DRV.CHILD_TYPES) == set(INFO["child_entity_types"])


def test_passwords_have_no_default():
    schema = INFO["config_schema"]
    for key in ("password", "write_password"):
        assert schema[key]["secret"] is True and "default" not in schema[key]
        assert INFO["default_config"][key] == ""


def test_whole_unit_commands_ask_first_and_quick_actions_do_not():
    commands = INFO["commands"]
    assert commands["all_outputs_off"]["confirm"] and commands["all_outputs_reboot"]["confirm"]
    assert commands["reset_output_energy"]["confirm"]
    for name in INFO["quick_actions"]:
        assert "confirm" not in commands[name], name


def test_discovery_probe_and_mac_block():
    disc = INFO["discovery"]
    assert disc["oui"] == ["24:a4:2c"]
    probe = disc["tcp_probe"]
    assert probe["port"] == 80 and probe["send_ascii"].startswith("GET /netio.json ")
    # The probe's pattern matches what a live unit sends, and the extracts read it.
    text = (FIXTURES / "powerpdu_4ks.json").read_text(encoding="utf-8")
    assert re.search(probe["expect_regex"], text)
    assert re.search(probe["extract"]["model"]["regex"], text).group(1) == "4KS"
    assert re.search(probe["extract"]["serial"]["regex"], text).group(1) == "24A42C3A392B"


# ── The live demo units' documents ───────────────────────────────────────────


def test_metered_k_model_reads_every_output_with_its_own_voltage():
    s = DRV.summarize(_fixture("powerpdu_4ks.json"))
    dev = s["device"]
    assert dev["model"] == "4KS" and dev["firmware"] == "5.3.2" and dev["serial_number"] == "24A42C3A392B"
    assert dev["output_count"] == 4 and dev["input_count"] == 1 and dev["metered"] is True
    assert dev["frequency_hz"] == 50.02
    # 999.00 is "not available" (no load to measure a power factor from).
    assert dev["total_power_factor"] is None and dev["total_phase_deg"] is None
    assert list(s["output"]) == [1, 2, 3, 4]
    for values in s["output"].values():
        assert values["metered"] is True and values["voltage_v"] and values["frequency_hz"] == 50.0
        assert values["current_a"] == 0.0 and values["power_factor"] == 1.0
    # No sensor on the input: 999 temperature and 999 humidity read as unknown.
    assert s["input"][1] == {
        "name": "Input 1", "closed": False, "s0_counter": 0,
        "temperature_c": None, "temperature_f": None, "humidity": None,
    }


def test_8qs_meters_the_unit_and_output_1_only():
    s = DRV.summarize(_fixture("powerpdu_8qs.json"))
    assert s["device"]["metered"] is True and len(s["output"]) == 8
    # No Frequency in GlobalMeasure on this unit, though the document's listing has one.
    assert s["device"]["frequency_hz"] is None
    assert s["output"][1]["metered"] and s["output"][1]["name"] == "Dream Machine"
    assert s["output"][1]["energy_nr_wh"] == 154263 and s["output"][1]["voltage_v"] is None
    assert not any(s["output"][n]["metered"] for n in range(2, 9))
    assert s["output"][2]["load_w"] is None
    assert s["input"][1]["closed"] is True and s["input"][1]["s0_counter"] == 1246811
    # A rule name with a space becomes a child id with '_'; the name stays as reported.
    assert s["rule"]["2_OFFafter10Sec"]["name"] == "2 OFFafter10Sec"


def test_powerdin_meters_outputs_1_and_2_and_has_two_relays():
    s = DRV.summarize(_fixture("powerdin_4pz.json"))
    assert [s["output"][n]["metered"] for n in (1, 2, 3, 4)] == [True, True, False, False]
    assert s["output"][3]["name"] == "Free Contact 3"
    assert s["device"]["frequency_hz"] is None and len(s["input"]) == 2


def test_unmetered_models_report_no_measurements():
    for name, outputs in (("powerpdu_4ps.json", 4), ("powercable_2pz.json", 2)):
        s = DRV.summarize(_fixture(name))
        assert s["device"]["metered"] is False and s["device"]["voltage_v"] is None
        assert len(s["output"]) == outputs and s["input"] == {}
        assert not any(v["metered"] for v in s["output"].values())


def test_automation_entries():
    s = DRV.summarize(_fixture("powerpdu_4ks.json"))
    assert s["pab"]["CR03_PAB"] == {
        "name": "CR03_PAB", "pab_type": "RANGE", "enabled": True, "in_range": False, "zone": None,
    }
    # The ZONES type reports a Zone where RANGE reports In.
    assert s["pab"]["Falcon"] == {
        "name": "Falcon", "pab_type": "ZONES", "enabled": False, "in_range": None, "zone": 0,
    }
    assert s["rule"]["CR01_RULE"]["result"] is True and s["rule"]["CR02_RULE"]["result"] is None
    s = DRV.summarize(_fixture("powercable_2pz.json"))
    assert s["watchdog"]["CR01_WDT"]["last_ok"] is True
    assert s["watchdog"]["CR01_WDT"]["last_check"] == "2026-10-13T17:52:04"
    assert s["watchdog"]["google_pinger"]["last_check"] is None


def test_powerbox_4kf_is_a_metered_k_model():
    s = DRV.summarize(_fixture("powerbox_4kf.json"))
    assert s["device"]["model"] == "4KF" and s["device"]["input_count"] == 0
    assert s["output"][3]["current_a"] == 0.023 and s["output"][3]["load_w"] == 1.0


def test_error_bodies_in_both_shapes():
    assert DRV.parse_error((FIXTURES / "write_400_body.txt").read_text()) == "Bad request"
    wrapped = '{"errors":[{"code":401,"severity":"fatal","message":"Unauthorized"}]}'
    assert DRV.parse_error(wrapped) == "Unauthorized"
    assert DRV.parse_error("401 Unauthorized") == "401 Unauthorized"
    assert DRV.parse_error("<html>a long page</html>" * 5) == ""


def test_child_keys_are_safe_and_unique():
    taken: set[str] = set()
    assert DRV.child_key("2 OFFafter10Sec", taken) == "2_OFFafter10Sec"
    assert DRV.child_key("2_OFFafter10Sec", taken) == "2_OFFafter10Sec-2"
    assert DRV.child_key("  ", taken) == "entry"


def test_a_switch_answer_does_not_overwrite_metering():
    async def scenario():
        h = await _connected()
        h.sim.set_state("out1_energy_start", "2025-01-01T00:00:00+0000")
        await h.driver.poll()
        assert h.out(1, "energy_start") == "2025-01-01T00:00:00+0000"
        # The live 4KS's answer to a switch: no EnergyStart, power factor 0.
        h.driver._apply_write_reply(_fixture("powerpdu_4ks_switch_reply.json"))
        assert h.out(1, "energy_start") == "2025-01-01T00:00:00+0000"
        assert h.out(1, "state") is True and h.out(1, "name") == "Power output 1"
    _run(scenario())


# ── Connect, roster, polling ─────────────────────────────────────────────────


def test_connect_reads_identity_and_registers_every_child():
    async def scenario():
        h = await _connected()
        assert h.st("connected") is True and h.st("model") == "4KS"
        assert h.st("output_count") == 4 and h.st("metered") is True
        assert h.driver.list_children("output") == [1, 2, 3, 4]
        assert h.driver.list_children("input") == [1]
        assert set(h.driver.list_children("rule")) == {"CR01_RULE", "CR02_RULE", "2_OFFafter10Sec"}
        assert set(h.driver.list_children("pab")) == {"CR03_PAB", "Falcon"}
        assert h.out(1, "load_w") == 40.0 and h.out(1, "current_a") == round(40 / 230, 3)
    _run(scenario())


def test_the_roster_follows_the_unit():
    async def scenario():
        h = await _connected()
        h.sim.set_state("model", "8QS")
        await h.driver.poll()
        assert h.driver.list_children("output") == list(range(1, 9))
        assert h.out(1, "metered") is True and h.out(2, "metered") is False
        h.sim.set_state("model", "2PZ")
        await h.driver.poll()
        assert h.driver.list_children("output") == [1, 2]
        assert h.driver.list_children("input") == []
        assert h.st("metered") is False and h.st("voltage_v") is None
    _run(scenario())


def test_sensor_and_contact_read_through():
    async def scenario():
        h = await _connected({"sensor_connected": True, "in1_temperature_c": 31.5, "in1_closed": True})
        assert h.st("input.1.temperature_c") == 31.5 and h.st("input.1.closed") is True
        assert h.st("input.1.humidity") == 40.0
    _run(scenario())


# ── Commands ─────────────────────────────────────────────────────────────────


def test_switching_reads_back_from_the_answer():
    async def scenario():
        h = await _connected()
        await h.driver.send_command("output_off", {"output": 2})
        assert h.out(2, "state") is False and h.sim.get_state("out2_on") is False
        await h.driver.send_command("output_on", {"output": 2})
        assert h.out(2, "state") is True
        await h.driver.send_command("output_toggle", {"output": 2})
        assert h.out(2, "state") is False
        body = json.loads(h.requests[-1].content)
        assert body == {"Outputs": [{"ID": 2, "Action": 4}]}
    _run(scenario())


def test_reboot_with_a_time_ends_its_session_and_without_one_does_not():
    async def scenario():
        h = await _connected()
        await h.driver.send_command("output_reboot", {"output": 3, "seconds": 0.2})
        sent = h.requests[-1]
        assert json.loads(sent.content) == {"Outputs": [{"ID": 3, "Action": 2, "Delay": 200}]}
        assert sent.headers.get("connection") == "close"
        assert h.out(3, "state") is False
        await asyncio.sleep(0.3)
        await h.driver.poll()
        assert h.out(3, "state") is True
        await h.driver.send_command("output_reboot", {"output": 3})
        sent = h.requests[-1]
        assert json.loads(sent.content) == {"Outputs": [{"ID": 3, "Action": 2}]}
        assert sent.headers.get("connection") != "close"
    _run(scenario())


def test_on_for_a_time_switches_back_off():
    async def scenario():
        h = await _connected({"out4_on": False})
        await h.driver.send_command("output_on_for", {"output": 4, "seconds": 0.2})
        assert h.out(4, "state") is True
        await asyncio.sleep(0.3)
        await h.driver.poll()
        assert h.out(4, "state") is False
    _run(scenario())


def test_all_outputs_go_in_one_write():
    async def scenario():
        h = await _connected()
        await h.driver.send_command("all_outputs_off", {})
        assert json.loads(h.requests[-1].content) == {
            "Outputs": [{"ID": n, "Action": 0} for n in (1, 2, 3, 4)]
        }
        assert [h.out(n, "state") for n in (1, 2, 3, 4)] == [False] * 4
        await h.driver.send_command("all_outputs_on", {})
        assert [h.out(n, "state") for n in (1, 2, 3, 4)] == [True] * 4
    _run(scenario())


def test_rename_reset_and_schedules():
    async def scenario():
        h = await _connected()
        await h.driver.send_command("rename_output", {"output": 1, "name": "Projector"})
        assert h.out(1, "name") == "Projector" and h.sim.get_state("out1_name") == "Projector"
        assert json.loads(h.requests[-1].content) == {
            "Outputs": [{"ID": 1, "Name": "Projector", "Action": 5}]
        }
        h.sim.set_state("out1_energy_wh", 500.0)
        await h.driver.send_command("reset_output_energy", {"output": 1})
        write = [r for r in h.requests if r.method == "POST"][-1]
        assert json.loads(write.content) == {
            "Outputs": [{"ID": 1, "ResetEnergyCounter": True, "Action": 5}]
        }
        assert h.out(1, "energy_wh") == 0 and h.out(1, "energy_start") != "1970-01-01T00:00:00+0000"
        await h.driver.send_command("enable_output_schedule", {"output": 2})
        assert h.sim.get_state("out2_schedule") is True
        await h.driver.send_command("disable_output_schedule", {"output": 2})
        assert h.sim.get_state("out2_schedule") is False
    _run(scenario())


def test_an_output_the_unit_does_not_have_is_refused_before_sending():
    async def scenario():
        h = await _connected()
        before = len(h.requests)
        with pytest.raises(DRV.NetioCommandError, match="no output 7; its outputs are 1 to 4"):
            await h.driver.send_command("output_on", {"output": 7})
        assert len(h.requests) == before
        with pytest.raises(DRV.NetioCommandError, match="more than 0"):
            await h.driver.send_command("output_reboot", {"output": 1, "seconds": 0})
    _run(scenario())


# ── Logins ───────────────────────────────────────────────────────────────────


def test_separate_logins_for_reading_and_writing():
    async def scenario():
        # The demo units' arrangement: reads open, writes with a login.
        h = await _connected(
            {"write_login_required": True},
            {"username": "", "password": "", "write_username": "netio", "write_password": "w1"},
        )
        await h.driver.send_command("output_off", {"output": 1})
        reads = [r for r in h.requests if r.method == "GET"]
        writes = [r for r in h.requests if r.method == "POST"]
        assert all(_basic(r) is None for r in reads)
        assert _basic(writes[-1]) == ("netio", "w1")
    _run(scenario())


def test_blank_write_login_uses_the_reading_one():
    async def scenario():
        h = await _connected()
        await h.driver.send_command("output_off", {"output": 1})
        assert _basic(h.requests[-1]) == ("netio", "netio")
    _run(scenario())


def test_a_refused_reading_login_is_auth_failed():
    async def scenario():
        h = _Harness({"read_login_required": True}, {"password": "invalid"})
        with pytest.raises(ConnectionFaultError) as info:
            await h.connect()
        assert info.value.fault_code == "auth_failed" and "HTTP 401" in str(info.value)
        h = _Harness({"read_login_required": True}, {"username": "netio", "password": ""})
        # A blank password is still a login the unit may accept; the sim takes any but "invalid".
        await h.connect()
        assert h.st("connected") is True
    _run(scenario())


def test_a_refused_writing_login_fails_the_command_and_stays_connected():
    async def scenario():
        h = await _connected(
            {"write_login_required": True},
            {"write_username": "invalid", "write_password": "x"},
        )
        with pytest.raises(DRV.NetioCommandError, match="login for writing") as info:
            await h.driver.send_command("output_off", {"output": 1})
        assert isinstance(info.value, RuntimeError)
        assert "login for writing" in h.st("last_error")
        assert h.st("connected") is True and h.out(1, "state") is True
    _run(scenario())


# ── Refusals and faults ──────────────────────────────────────────────────────


def test_writing_switched_off_answers_403():
    async def scenario():
        h = await _connected({"write_enabled": False})
        with pytest.raises(DRV.NetioCommandError, match="reads only"):
            await h.driver.send_command("output_on", {"output": 1})
    _run(scenario())


def test_json_api_off():
    async def scenario():
        h = _Harness({"json_api_enabled": False})
        with pytest.raises(ConnectionFaultError) as info:
            await h.connect()
        assert info.value.fault_code == "invalid_config" and "JSON API" in str(info.value)
        h = await _connected()
        h.sim.set_state("json_api_enabled", False)
        await h.driver.poll()
        assert "HTTP 404" in h.st("last_error") and h.st("connected") is True
    _run(scenario())


def test_a_restarting_web_server_is_retried_not_paused():
    async def scenario():
        h = _Harness()
        h.sim.inject_error("web_server_restarting")
        with pytest.raises(ConnectionError) as info:
            await h.connect()
        assert not isinstance(info.value, ConnectionFaultError)
        assert "HTTP 500" in str(info.value)
    _run(scenario())


def test_something_other_than_a_netio_is_invalid_config():
    async def scenario():
        h = _Harness()

        async def other(request):
            return httpx.Response(200, text="<html>router</html>")

        h.handler = other  # type: ignore[assignment]
        with pytest.raises(ConnectionFaultError) as info:
            await h.connect()
        assert info.value.fault_code == "invalid_config" and "not with NETIO's JSON status" in str(info.value)
    _run(scenario())


def test_liveness_probe_counts_any_answer_but_a_refused_login():
    async def scenario():
        h = await _connected({"read_login_required": True})
        h.sim.set_state("json_api_enabled", False)
        await h.driver._liveness_probe()           # a 404 is an answer
        h.sim.set_state("json_api_enabled", True)
        h.sim.inject_error("web_server_restarting")
        await h.driver._liveness_probe()           # so is a 500
        h.sim.clear_error("web_server_restarting")
        h.driver.config["password"] = "invalid"
        with pytest.raises(ConnectionFaultError) as info:
            await h.driver._liveness_probe()
        assert info.value.fault_code == "auth_failed"
    _run(scenario())


def test_a_poll_after_the_login_changed_drops_the_connection_typed():
    async def scenario():
        h = await _connected({"read_login_required": True})
        h.driver.config["password"] = "invalid"
        with pytest.raises(ConnectionFaultError) as info:
            await h.driver.poll()
        assert info.value.fault_code == "auth_failed"
    _run(scenario())


# ── Web login: the demo units' replies ───────────────────────────────────────


def _web_fixture(name: str) -> dict:
    return json.loads((WEB_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _data(name: str):
    return _web_fixture(name)["data"]


def test_sign_in_token_is_salted_and_tied_to_the_units_clock():
    args = ("admin", "secret", "KEY", 1792179315)
    token = DRV.web_login_token(*args, salt="ab")
    assert token == SIM._token(*args, "ab")
    assert token.startswith("ab.") and "secret" not in token
    # The same ten-second window gives the same token; the next one does not.
    assert DRV.web_login_token("admin", "secret", "KEY", 1792179319, salt="ab") == token
    assert DRV.web_login_token("admin", "secret", "KEY", 1792179320, salt="ab") != token
    assert DRV.web_login_token(*args) != DRV.web_login_token(*args)


def test_firmware_version_from_the_units_string():
    assert DRV.firmware_version("5.3.2 - 1.57(1.53) - 0  4k") == (5, 3, 2)
    assert DRV._firmware_text("5.3.2 - 1.57(1.53) - 0  4k") == "5.3.2"
    assert DRV.firmware_version("") is None


def test_identity_from_system_info_and_from_the_greeting_alone():
    hello = _data("4ks_helo")["device"]
    info = _data("4ks_system_info")
    dev = DRV.web_identity(info, hello)
    assert dev == {
        "model": "4KS", "device_name": "PowerPDU-2B", "serial_number": "24A42C3A392B",
        "firmware": "5.3.2", "output_count": 4, "input_count": 1,
    }
    # A user who may not read system/info still gets the greeting's identity.
    assert DRV.web_identity(None, hello) == {
        "model": "4KS", "device_name": None, "serial_number": "24A42C3A392B", "firmware": "5.3.2",
    }
    assert DRV.web_measure_mask(info) == 15
    assert DRV.web_measure_mask(_data("8qs_system_info")) == 1
    assert DRV.web_measure_mask(_data("4ps_system_info")) == 0


def test_metering_in_the_units_the_json_api_state_uses():
    items = DRV.web_numbered(_data("4kf_outputs_measure")["items"], 24, lambda i: i)
    out2 = DRV.web_output_metering(items[2])
    # Current comes in amps and power in watts with decimals (the JSON API
    # read beside it said Current 7 mA, Load 0 W).
    assert out2["metered"] is True and out2["current_a"] == 0.007 and out2["load_w"] == 0.154
    assert out2["energy_wh"] == 344 and out2["phase_deg"] == 269.54
    assert out2["energy_start"] == "2025-09-17T00:18:50"
    assert DRV.web_output_metering(items[3])["energy_start"] is None   # 0: never reset
    # Output 1 of the 4KF was off with its schedule on.
    assert DRV.web_output_live(items[1]) == {"state": False, "schedule_enabled": True}


def test_an_unmetered_unit_answers_zeros_and_reads_as_unmetered():
    zeros = _data("4ps_outputs_globalmeasure")["measure"]
    assert zeros["voltage"] == 0 and zeros["powerFactor"] == 999
    glob = DRV.web_global(zeros, metered=False)
    assert glob["metered"] is False and all(v is None for k, v in glob.items() if k != "metered")
    for item in _data("4ps_outputs_measure")["items"]:
        assert DRV.web_output_metering(item)["metered"] is False
    # The 8QS meters output 1 only, and the whole unit.
    items = DRV.web_numbered(_data("8qs_outputs_measure")["items"], 24, DRV.web_output_metering)
    assert [n for n, v in items.items() if v["metered"]] == [1]
    assert DRV.web_global(_data("8qs_outputs_globalmeasure")["measure"], metered=True)["voltage_v"] > 200


def test_lists_inputs_and_an_outputs_settings():
    listed = DRV.web_numbered(_data("4ks_outputs_list")["items"], 24, DRV.web_output_listed)
    assert listed[1] == {"name": "Power output 1", "schedule_name": None}
    inputs = DRV.web_numbered(_data("4ks_inputs_list")["items"], 8, DRV.web_input)
    assert inputs[1]["closed"] is False and inputs[1]["temperature_c"] is None
    assert inputs[1]["humidity"] is None
    config = DRV.web_output_config(_data("4ks_outputs_id_1_config"))
    assert config["delay_ms"] == 5000 and config["schedule_enabled"] is False
    watchdogs = DRV.web_named(_data("4ks_wdtpingers_list")["items"], DRV.WEB_BLANK["watchdog"])
    assert watchdogs["google_pinger"]["name"] == "google_pinger"
    assert watchdogs["google_pinger"]["failed"] is None
    assert watchdogs["google_pinger"]["last_check"] is None
    rules = DRV.web_named(_data("4ks_rules_list")["items"], DRV.WEB_BLANK["rule"])
    assert rules["CR01_RULE"] == {"name": "CR01_RULE", "enabled": True, "result": None}


def test_a_fahrenheit_sensor_is_reported_in_both_units():
    values = DRV.web_input({"id": 1, "name": "Input 1", "state": "on", "s0Counter": 3,
                            "temperature": 72.5, "temperatureUnit": "F", "humidity": 40})
    assert values["closed"] is True and values["temperature_f"] == 72.5
    assert values["temperature_c"] == 22.5 and values["humidity"] == 40


# ── Web login: the round trip ────────────────────────────────────────────────


class _FakeSocket:
    """An in-memory WebSocket wired to the simulator's web-login session. Every
    frame the driver sends is answered by NetioWebSession.handle, and a push
    task sends its events every few milliseconds (a unit: every 0.3 s)."""

    def __init__(self, session, push_s=0.01):
        self.session = session
        self.sent: list[dict] = []
        self.sent_at: list[float] = []
        self.silent = False
        self.state = SimpleNamespace(name="OPEN")
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pusher = asyncio.get_running_loop().create_task(self._push(push_s))

    async def _push(self, period):
        while self.state.name == "OPEN":
            await asyncio.sleep(period)
            if self.session.ended():
                self.drop()                       # the unit restarted
                return
            if not self.silent:
                for frame in self.session.events():
                    self._queue.put_nowait(json.dumps(frame))

    async def send(self, text):
        if self.state.name == "OPEN" and self.session.ended():
            self.drop()
        if self.state.name != "OPEN":
            raise _FakeConnectionClosed()
        self.sent.append(json.loads(text))
        self.sent_at.append(asyncio.get_running_loop().time())
        if not self.silent:
            for reply in self.session.handle(text):
                self._queue.put_nowait(json.dumps(reply))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def _end(self):
        if self.state.name == "OPEN":
            self.state = SimpleNamespace(name="CLOSED")
            self._pusher.cancel()
            self._queue.put_nowait(None)

    async def close(self):
        self._end()

    def drop(self):
        """The unit goes away."""
        self._end()

    def kinds(self):
        return [(f.get("type"), f.get("topic")) for f in self.sent]


class _WebHarness:
    def __init__(self, sim_state=None, driver_config=None):
        self.sim = SIM.NetioSimulator("netio-sim", {})
        for key, value in (sim_state or {}).items():
            self.sim.set_state(key, value)
        self.sockets: list[_FakeSocket] = []
        self.opened: list[tuple[str, dict]] = []
        cfg = {
            "connection": "web", "host": "10.0.0.30", "port": 80, "ssl": False,
            "verify_ssl": False, "username": "admin", "password": "secret",
            "write_username": "", "write_password": "", "poll_interval": 0,
        }
        cfg.update(driver_config or {})
        self.driver = DRV.NetioDriver("pdu1", cfg, StubState(), StubEvents())

    async def fake_connect(self, url, **kwargs):
        self.opened.append((url, kwargs))
        if self.sim.restarting():
            raise _FakeInvalidStatus(503)
        if not self.sim.get_state("web_login_api", True):
            raise _FakeInvalidStatus(404)
        sock = _FakeSocket(SIM.NetioWebSession(self.sim))
        self.sockets.append(sock)
        return sock

    async def connect(self):
        DRV.websockets.connect = self.fake_connect
        await self.driver.connect()

    @property
    def sock(self) -> _FakeSocket:
        return self.sockets[-1]

    def st(self, key):
        return self.driver.state.get(f"device.{self.driver.device_id}.{key}")

    def out(self, n, key):
        return self.st(f"output.{n}.{key}")

    async def settle(self, seconds=0.05):
        await asyncio.sleep(seconds)

    async def until(self, check, timeout=5.0):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not check():
            assert loop.time() < end, "timed out"
            await asyncio.sleep(0.01)

    async def reconnect(self, timeout=5.0):
        """What the platform does after a drop: dial again until the unit
        answers."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            try:
                await self.driver.connect()
                return
            except ConnectionError:
                assert loop.time() < end, "the unit never came back"
                await asyncio.sleep(0.02)


def _web(scenario, sim_state=None, driver_config=None, connect=True):
    """Run a scenario against a connected driver, and close it after."""
    async def run():
        h = _WebHarness(sim_state, driver_config)
        if connect:
            await h.connect()
        try:
            await scenario(h)
        finally:
            await h.driver.disconnect()
    asyncio.run(asyncio.wait_for(run(), timeout=20))


def test_web_connect_reads_everything_and_holds_the_pushed_topics():
    async def scenario(h):
        assert h.opened[0][0] == "ws://10.0.0.30:80/emweb"
        assert "ssl" not in h.opened[0][1] and h.opened[0][1]["ping_interval"] is None
        auth = h.sock.sent[1]
        assert h.sock.sent[0]["type"] == "HELO" and auth["type"] == "AUTH"
        assert auth["username"] == "admin" and "secret" not in auth["token"]
        assert auth["token"] in h.driver.redacted
        assert h.st("model") == "4KS" and h.st("firmware") == "5.3.1"
        assert h.st("serial_number") == "24A42C000001" and h.st("output_count") == 4
        assert h.st("metered") is True and h.st("voltage_v") == 230.0
        assert h.st("mac_address") is None and h.st("json_version") is None
        assert h.out(1, "name") == "Power output 1" and h.out(1, "state") is True
        assert h.out(1, "schedule_name") == "Workdays" and h.out(1, "schedule_enabled") is False
        assert h.out(2, "schedule_name") is None and h.out(1, "delay_ms") == 5000
        assert h.out(1, "load_w") == 40.0 and h.out(1, "current_a") == round(40 / 230, 3)
        assert h.st("input.1.closed") is False
        assert h.st("watchdog.google_pinger.enabled") is True
        assert h.st("watchdog.google_pinger.failed") is None
        assert h.st("rule.2_OFFafter10Sec.name") == "2 OFFafter10Sec"
        assert h.st("pab.CR03_PAB.in_range") is None
        assert h.sock.session.subscribed == {
            "outputs/measure", "outputs/globalmeasure", "outputs/list", "inputs/list",
        }
        # Every frame is reported to the traffic recorder.
        assert ("tx", json.dumps(h.sock.sent[0], ensure_ascii=False)) in h.driver.traffic
        assert any(d == "rx" and '"REPLY"' in t for d, t in h.driver.traffic)
    _web(scenario)


def test_web_switching_reads_back_from_the_pushed_state():
    async def scenario(h):
        await h.driver.send_command("output_off", {"output": 2})
        assert h.out(2, "state") is False and h.sim.get_state("out2_on") is False
        assert h.sock.sent[-1]["data"] == {"request": "off"}
        assert h.sock.sent[-1]["topic"] == "outputs/id/2/ctrl"
        await h.driver.send_command("output_on", {"output": 2})
        assert h.out(2, "state") is True
        await h.driver.send_command("output_toggle", {"output": 2})
        assert h.out(2, "state") is False
        await h.driver.send_command("all_outputs_off", {})
        assert [h.out(n, "state") for n in range(1, 5)] == [False] * 4
        await h.driver.send_command("all_outputs_on", {})
        assert [h.out(n, "state") for n in range(1, 5)] == [True] * 4
    _web(scenario)


def test_web_a_change_at_the_unit_is_pushed_without_a_poll():
    async def scenario(h):
        h.sim.set_state("out3_on", False)
        await h.settle(0.1)
        assert h.out(3, "state") is False
        h.sim.set_state("out3_schedule", True)
        await h.settle(0.1)
        assert h.out(3, "schedule_enabled") is True
    _web(scenario)


def test_web_metering_waits_for_the_interval_and_switching_does_not():
    async def scenario(h):
        h.sim.set_state("out1_load_w", 100.0)
        await h.settle(0.1)
        assert h.out(1, "load_w") == 40.0                    # held to the poll interval
        h.driver._web_meter_next.clear()                     # the interval is up
        await h.settle(0.1)
        assert h.out(1, "load_w") == 100.0
    _web(scenario, driver_config={"poll_interval": 60})


def test_web_reboot_uses_the_outputs_own_time_and_refuses_one_of_its_own():
    async def scenario(h):
        await h.driver.send_command("output_reboot", {"output": 2})
        assert h.out(2, "state") is False and h.sock.sent[-1]["data"] == {"request": "soff"}
        await h.settle(0.4)
        assert h.out(2, "state") is True
        await h.driver.send_command("output_off", {"output": 3})
        await h.driver.send_command("output_on_for", {"output": 3})
        assert h.out(3, "state") is True
        await h.settle(0.4)
        assert h.out(3, "state") is False
        sent = len(h.sock.sent)
        with pytest.raises(DRV.NetioCommandError, match="own short on/off time"):
            await h.driver.send_command("output_reboot", {"output": 2, "seconds": 5})
        assert len(h.sock.sent) == sent
    _web(scenario, sim_state={"out2_delay_ms": 200, "out3_delay_ms": 200})


def test_web_rename_schedule_and_energy_reset_write_the_outputs_settings():
    async def scenario(h):
        await h.driver.send_command("rename_output", {"output": 1, "name": "Codec"})
        assert h.out(1, "name") == "Codec" and h.sim.get_state("out1_name") == "Codec"
        written = [f for f in h.sock.sent
                   if f.get("type") == "SET" and f["topic"] == "outputs/id/1/config"]
        assert set(written[-1]["data"]) == set(DRV.WEB_OUTPUT_CONFIG_KEYS)
        assert written[-1]["data"]["resetDelay"] == 5000                  # the rest written back
        await h.driver.send_command("enable_output_schedule", {"output": 1})
        assert h.out(1, "schedule_enabled") is True and h.sim.get_state("out1_schedule") is True
        await h.driver.send_command("disable_output_schedule", {"output": 1})
        assert h.out(1, "schedule_enabled") is False
        h.sim.set_state("out1_energy_wh", 50.0)
        await h.driver.send_command("reset_output_energy", {"output": 1})
        # Zeroed; the simulated output keeps drawing 40 W, so a push between
        # the reset and this read may already have counted a fraction of a mWh.
        assert h.sim.get_state("out1_energy_wh") < 0.001
        await h.settle(0.1)
        assert h.out(1, "energy_start") is not None
    _web(scenario)


def test_web_settings_refused_for_this_user_fail_the_command_and_stay_connected():
    async def scenario(h):
        with pytest.raises(DRV.NetioCommandError, match="not allowed"):
            await h.driver.send_command("rename_output", {"output": 1, "name": "Codec"})
        assert "rename the output" in h.st("last_error")
        with pytest.raises(DRV.NetioCommandError, match="not allowed"):
            await h.driver.send_command("reset_output_energy", {"output": 1})
        assert h.sim.get_state("out1_name") == "Power output 1"
        assert h.driver._link_alive() is True
        await h.driver.send_command("output_off", {"output": 1})     # switching still works
        assert h.out(1, "state") is False
    _web(scenario, sim_state={"web_settings_allowed": False})


@pytest.mark.parametrize("login", [{"password": "invalid"}, {"username": "invalid"}])
def test_web_refused_sign_ins_are_auth_failed(login):
    async def scenario(h):
        with pytest.raises(ConnectionFaultError) as info:
            await h.connect()
        assert info.value.fault_code == "auth_failed"
        assert "refused the web login" in str(info.value)
    _web(scenario, driver_config=login, connect=False)


def test_web_no_password_is_never_sent():
    async def scenario(h):
        with pytest.raises(ConnectionFaultError) as info:
            await h.connect()
        assert info.value.fault_code == "auth_failed" and h.opened == []
    _web(scenario, driver_config={"password": ""}, connect=False)


def test_web_login_missing_is_invalid_config():
    async def scenario(h):
        with pytest.raises(ConnectionFaultError) as info:
            await h.connect()
        assert info.value.fault_code == "invalid_config" and "5.1.3" in str(info.value)
    _web(scenario, sim_state={"web_login_api": False}, connect=False)


def test_web_firmware_too_old_is_invalid_config_before_signing_in():
    async def scenario(h):
        with pytest.raises(ConnectionFaultError) as info:
            await h.connect()
        assert info.value.fault_code == "invalid_config" and "5.1.2" in str(info.value)
        assert [f["type"] for f in h.sock.sent] == ["HELO"]
    _web(scenario, sim_state={"firmware": "5.1.2"}, connect=False)


def test_web_firmware_5_4_reads_with_get_and_takes_states_from_outputs_state():
    async def scenario(h):
        assert "outputs/state" in h.sock.session.subscribed
        assert h.out(2, "state") is True
        start = len(h.sock.sent)
        await h.driver.poll()
        assert {kind for kind, _topic in h.sock.kinds()[start:]} == {"GET"}
        await h.driver.send_command("output_off", {"output": 2})
        assert h.out(2, "state") is False
    _web(scenario, sim_state={"firmware": "5.4.0"})


def test_web_liveness_pings_from_5_2():
    async def scenario(h):
        await h.driver._liveness_probe()
        assert h.sock.kinds()[-1] == ("PING", None)
    _web(scenario)


def test_web_liveness_reads_the_uptime_before_5_2():
    async def scenario(h):
        await h.driver._liveness_probe()
        assert h.sock.kinds()[-2:] == [
            ("SUBSCRIBE", "system/uptime"), ("UNSUBSCRIBE", "system/uptime"),
        ]
    _web(scenario, sim_state={"firmware": "5.1.3"})


def test_web_a_silent_unit_fails_the_probe(monkeypatch):
    async def scenario(h):
        monkeypatch.setattr(DRV, "WEB_REPLY_TIMEOUT_S", 0.2)
        h.sock.silent = True
        with pytest.raises(ConnectionError, match="did not answer PING"):
            await h.driver._liveness_probe()
    _web(scenario)


def test_web_a_dropped_socket_takes_the_unit_offline():
    async def scenario(h):
        h.sock.drop()
        await h.settle(0.05)
        assert h.driver.dropped == 1 and h.st("connected") is False
        assert h.driver.stashed_fault[0] == "transport_disconnected"
        assert h.driver._link_alive() is False
        with pytest.raises(ConnectionError):
            await h.driver.send_command("output_on", {"output": 1})
    _web(scenario)


def test_web_a_refused_read_is_named_and_identity_comes_from_the_greeting():
    async def scenario(h):
        original = h.sim.web_topic
        h.sim.web_topic = lambda topic: None if topic == "system/info" else original(topic)
        await h.connect()
        assert h.st("model") == "4KS" and h.st("device_name") is None
        assert "system/info (TopicNotFonud)" in h.st("last_error")
    _web(scenario, connect=False)


def test_web_no_outputs_over_the_web_login_at_connect_is_invalid_config():
    async def scenario(h):
        original = h.sim.web_topic
        h.sim.web_topic = lambda topic: None if topic == "outputs/measure" else original(topic)
        with pytest.raises(ConnectionFaultError) as info:
            await h.connect()
        assert info.value.fault_code == "invalid_config"
    _web(scenario, connect=False)


def test_web_frames_are_paced():
    async def scenario(h):
        gaps = [b - a for a, b in zip(h.sock.sent_at, h.sock.sent_at[1:])]
        assert gaps and min(gaps) >= DRV.WEB_SEND_GAP_S * 0.9
    _web(scenario)


def test_web_https_uses_wss_with_the_units_own_certificate():
    async def scenario(h):
        url, kwargs = h.opened[0]
        assert url == "wss://10.0.0.30:443/emweb"
        assert kwargs["ssl"].verify_mode == DRV.ssl.CERT_NONE
    _web(scenario, driver_config={"ssl": True, "port": 443})


def test_web_login_runs_on_a_platform_without_reach_devices_through():
    """0.36.0 has no BaseDriver.local_address: the computer picks the adapter."""
    async def scenario(h):
        h.driver.local_address = None
        await h.connect()
        assert h.st("model") == "4KS" and "local_addr" not in h.opened[0][1]
    _web(scenario, connect=False)


def test_web_login_follows_reach_devices_through_where_the_platform_has_it():
    async def scenario(h):
        h.driver.local_address = lambda host=None: "192.168.10.5"
        await h.connect()
        assert h.opened[0][1]["local_addr"] == ("192.168.10.5", 0)
    _web(scenario, connect=False)


def test_the_web_login_needs_nothing_newer_than_0_36_0():
    assert INFO["min_platform_version"] == "0.36.0"


# ── Web login: online firmware update ────────────────────────────────────────


def _fast(monkeypatch):
    """The update's clocks, shortened: the simulated unit's, and the driver's
    waits and message spacing to match (on a unit an attempt takes minutes,
    far longer than a reconnect's reads)."""
    for name, value in {"FW_CHECK_S": 0.1, "FW_REBUILD_S": 0.02, "FW_DOWNLOAD_S": 0.5,
                        "FW_RESTART_S": 0.2, "FW_RETRY_DELAY_S": 0.05}.items():
        monkeypatch.setattr(SIM, name, value)
    for name, value in {"FW_LIST_INTERVAL_S": 0.01, "FW_LIST_SETTLE_S": 0.15,
                        "FW_RETRY_INTERVAL_S": 0.01, "FW_RETRY_WAIT_S": 1.0,
                        "FW_RESTART_SLACK_S": 0.1, "WEB_SEND_GAP_S": 0.001}.items():
        monkeypatch.setattr(DRV, name, value)


def _sets(h, topic="system/firmwarelist"):
    return [f["data"] for s in h.sockets for f in s.sent if f.get("type") == "SET" and f["topic"] == topic]


def test_the_update_list_before_any_check_and_the_answers_it_can_hold():
    blank = _data("4ks_fwupdate_list")["items"]        # never checked since the unit started
    assert _data("8qs_fwupdate_list")["items"] == blank
    assert DRV.firmware_list_answered(blank) is False and DRV.firmware_offers(blank) == []
    nothing = [{**blank[0], "name": "No new firmware available"}]
    assert DRV.firmware_list_answered(nothing) is True and DRV.firmware_offers(nothing) == []
    offered = [{"name": "Firmware 5.4.0", "version": "5.4.0 beta", "downloadUrl": "https://f/full",
                "downloadUrlNoMcu": "https://f/app", "releaseNoteEn": ""}]
    assert DRV.firmware_offers(offered) == [
        {"name": "Firmware 5.4.0", "version": "5.4.0", "full": "https://f/full", "app": "https://f/app"},
    ]
    assert _data("4ks_system_lock")["op"] == "unlock"


def test_firmware_commands_ask_first_and_hold_the_offline_fault_back():
    commands = INFO["commands"]
    assert "confirm" not in commands["check_firmware_update"]
    assert "restarts_device_for" not in commands["check_firmware_update"]
    for name in ("update_firmware_full", "update_firmware_keep_outlets"):
        assert commands[name]["params"] == {}                 # nothing is chosen for the person
        assert commands[name]["confirm"] and commands[name]["restarts_device_for"] == 300
    assert "switches off" in commands["update_firmware_full"]["confirm"]
    assert "stay as they are" in commands["update_firmware_keep_outlets"]["confirm"]


def test_firmware_needs_the_web_login():
    async def scenario():
        h = await _connected()
        sent = len(h.requests)
        for command in ("check_firmware_update", "update_firmware_full", "update_firmware_keep_outlets"):
            with pytest.raises(DRV.NetioCommandError, match="needs the web login"):
                await h.driver.send_command(command, {})
        assert len(h.requests) == sent
        await h.driver.disconnect()
    _run(scenario())


def test_web_check_for_a_firmware_update(monkeypatch):
    _fast(monkeypatch)

    async def scenario(h):
        h.sim.set_state("update_server", SIM.UPDATE_SERVER[2])
        with pytest.raises(DRV.NetioCommandError, match="got no answer"):
            await h.driver.send_command("check_firmware_update", {})
        h.sim.set_state("update_server", SIM.UPDATE_SERVER[0])
        await h.driver.send_command("check_firmware_update", {})
        assert h.st("firmware_update_available") is True and h.st("firmware_update_version") == "5.4.0"
        h.sim.set_state("update_server", SIM.UPDATE_SERVER[1])
        await h.driver.send_command("check_firmware_update", {})      # asked again, not reused
        assert h.st("firmware_update_available") is False and h.st("firmware_update_version") == ""
        assert _sets(h) == [{"getFirmwareList": True, "updateFw": ""}] * 3
        assert h.driver._link_alive() is True
    _web(scenario)


def test_web_firmware_needs_a_login_that_may_change_settings(monkeypatch):
    _fast(monkeypatch)

    async def scenario(h):
        for command in ("check_firmware_update", "update_firmware_full"):
            with pytest.raises(DRV.NetioCommandError, match="not allowed"):
                await h.driver.send_command(command, {})
        assert "check for a firmware update" in h.st("last_error")
        assert h.sim.get_state("last_update") == "" and h.driver._link_alive() is True
    _web(scenario, sim_state={"web_settings_allowed": False})


def test_web_full_update_follows_the_unit_through_its_restart(monkeypatch):
    _fast(monkeypatch)

    async def scenario(h):
        await h.driver.send_command("update_firmware_full", {})
        # It returns once the unit has started; one check, then the full link.
        assert h.sim.get_state("last_update") == "full"
        assert _sets(h) == [
            {"getFirmwareList": True, "updateFw": ""},
            {"getFirmwareList": False, "updateFw": "https://firmware.invalid/netio/5.4.0/full.package"},
        ]
        assert h.st("firmware_update_status") == "Updating to 5.4.0"
        reads = len(h.sock.sent)
        await h.driver.poll()                          # the unit is left to its download
        assert len(h.sock.sent) == reads
        with pytest.raises(DRV.NetioCommandError, match="updating to firmware 5.4.0"):
            await h.driver.send_command("check_firmware_update", {})
        await h.until(lambda: h.driver.dropped == 1)   # the unit restarts
        await h.reconnect()
        assert h.st("firmware") == "5.4.0" and h.st("firmware_update_status") == "Updated to 5.4.0"
        assert h.st("firmware_update_available") is False and h.driver._fw_update is None
    _web(scenario)


def test_web_keep_outlets_on_sends_the_link_without_the_outlet_controller(monkeypatch):
    _fast(monkeypatch)

    async def scenario(h):
        await h.driver.send_command("check_firmware_update", {})
        await h.driver.send_command("update_firmware_keep_outlets", {})
        assert h.sim.get_state("last_update") == "keep outlets on"
        # The check a moment ago is the answer used; nothing is asked twice.
        assert _sets(h)[1:] == [
            {"getFirmwareList": False, "updateFw": "https://firmware.invalid/netio/5.4.0/app.package"},
        ]
        await h.until(lambda: h.driver.dropped == 1)
        await h.reconnect()
        assert h.st("firmware_update_status") == "Updated to 5.4.0"
    _web(scenario)


def test_web_nothing_newer_installs_nothing(monkeypatch):
    _fast(monkeypatch)

    async def scenario(h):
        with pytest.raises(DRV.NetioCommandError, match=r"nothing newer than .* \(5\.4\.0\)"):
            await h.driver.send_command("update_firmware_full", {})
        assert _sets(h) == [{"getFirmwareList": True, "updateFw": ""}]
        assert h.sim.get_state("last_update") == "" and h.st("firmware_update_status") is None
    _web(scenario, sim_state={"firmware": "5.4.0"})


def test_web_a_download_that_keeps_failing_ends_after_three_attempts(monkeypatch):
    _fast(monkeypatch)

    async def scenario(h):
        await h.driver.send_command("update_firmware_full", {})
        for attempt in (1, 2):
            await h.until(lambda: h.driver.dropped == attempt)
            await h.reconnect()                        # back on the old firmware
            want = f"Updating to 5.4.0: attempt {attempt + 1} of 3"
            await h.until(lambda: h.st("firmware_update_status") == want)
        await h.until(lambda: h.driver.dropped == 3)
        await h.reconnect()
        failed = "The update to 5.4.0 failed: the NETIO restarted on firmware 5.3.1 after 3 attempts."
        assert h.st("firmware_update_status") == failed and h.st("last_error") == failed
        assert h.st("firmware") == "5.3.1" and h.driver._fw_update is None
    _web(scenario, sim_state={"update_download_fails": True})


def test_web_a_unit_that_does_not_try_again_has_failed(monkeypatch):
    _fast(monkeypatch)

    async def scenario(h):
        await h.driver.send_command("update_firmware_keep_outlets", {})
        await h.until(lambda: h.driver.dropped == 1)
        await h.reconnect()
        await h.until(lambda: h.driver._fw_update is None)
        assert h.st("firmware_update_status") == (
            "The update to 5.4.0 failed: the NETIO restarted on firmware 5.3.1 and did not try again."
        )
    _web(scenario, sim_state={"update_download_fails": True, "update_retries": False})


def test_web_an_update_that_never_finishes_fails_in_time(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(SIM, "FW_DOWNLOAD_S", 60.0)

    async def scenario(h):
        await h.driver.send_command("update_firmware_full", {})
        monkeypatch.setattr(DRV, "FW_UPDATE_TIMEOUT_S", 0.0)
        await h.driver.poll()
        assert h.st("firmware_update_status").startswith(
            "The update to 5.4.0 did not finish: the NETIO still runs firmware 5.3.1"
        )
        assert h.driver._fw_update is None
    _web(scenario)
