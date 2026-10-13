"""Driver + simulator tests for netio (NETIO JSON M2M API).

Two kinds of evidence:

  - The status documents NETIO's public demo units served on 2026-10-13
    (tests/fixtures/netio/), parsed byte for byte: a PowerPDU 8QS, 4PS and 4KS,
    a PowerDIN 4PZ, a PowerCable 2PZ and a PowerBOX 4KF on firmware 5.3.1 and
    5.3.2, the shorter answer the 4KS gave to a switch, and the error body it
    gave to a refused write.
  - A dual-proof round trip: the real driver wired to the real simulator over
    an in-memory httpx transport, every command, both logins, every refusal.

The driver is loaded with the ``openavc.*`` imports stubbed so the community
CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

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


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect lifecycle for a driver that owns its
    session; state and children come from the shared stubs."""

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


install_stubs(base_driver=_FakeBaseDriver)
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
