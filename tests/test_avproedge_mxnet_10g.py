"""Driver + simulator tests for avproedge_mxnet_10g (AVPro Edge MXNet 10G CBOX).

No 10G hardware on hand, so correctness is proven three ways: unit tests on the
pieces the API document is specific about, a dual-proof round trip wiring the
real driver to the real simulator through an in-memory link that runs the
driver's own frame parser, and command-surface consistency (every declared
command has a branch in ``send_command`` — a Python driver that falls through
returns success indistinguishable from a command that worked, which no gate
catches).

What is specific to THIS box, and so is what these tests are mostly about:

  - **Three reply shapes on one connection.** JSON for `config` / `matrix`,
    a Lua-style table for the video-wall queries, a bare `OK` for the rest of
    the `vwid` family, plus the malformed JSON the multiview writes answer
    with. The framing has to carry all four and only hand a raw one to a
    request that asked for it.
  - **Routes are derived, not queried.** There is no route query on the 10G;
    a decoder's per-plane channel subscriptions are joined against the encoder
    that hosts that channel.
  - **Six planes, not five** (the 10G adds analog audio), and the EDID and HDCP
    argument sets differ from the 1G's for the same physical effect.
  - **An unsolicited frame must never satisfy a request.** Mistaking one for a
    reply shifts every reply after it by one — a bug that already cost the 1G
    driver a release.

The driver and simulator are loaded with the ``openavc.*`` imports stubbed so
the community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubBaseDriver,
    StubEvents,
    StubState,
    StubTCPSimulator,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "avproedge_mxnet_10g.py"
SIM_PATH = REPO_ROOT / "switchers" / "avproedge_mxnet_10g_sim.py"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect for a TCP driver: the transport is
    the in-memory link the test supplies; state, children and the watchdog come
    from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self.transport = None
        self._connected = False
        self.transport_factory = None

    async def _pre_connect(self):
        return None

    async def _post_connect(self):
        return None

    async def _initial_sync(self):
        return None

    async def _close_session(self):
        return None

    async def connect(self):
        await self._close_session()
        await self._pre_connect()
        self.transport = self.transport_factory(self)
        try:
            await self._post_connect()
        except Exception:
            transport, self.transport = self.transport, None
            if transport is not None:
                await transport.close()
            await self._close_session()
            self._connected = False
            raise
        self._connected = True
        self.set_state("connected", True)
        await self.events.emit(f"device.connected.{self.device_id}")
        try:
            await self._initial_sync()
        except Exception:
            self._connected = False
            self.set_state("connected", False)
            raise

    async def disconnect(self):
        self._stop_health_loop()
        await self.stop_polling()
        if self.transport:
            await self.transport.close()
        self.transport = None
        await self._close_session()
        self._connected = False
        self.set_state("connected", False)
        await self.events.emit(f"device.disconnected.{self.device_id}")

    def _handle_transport_disconnect(self):
        self._connected = False
        self.set_state("connected", False)


class _FakeTCPSimulator(StubTCPSimulator):
    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.name = "sim"


install_stubs(
    {"openavc.simulator.tcp_simulator": {"TCPSimulator": _FakeTCPSimulator}},
    base_driver=_FakeBaseDriver,
)
DRV = load_module("avproedge_mxnet_10g_under_test", DRIVER_PATH)
SIMM = load_module("avproedge_mxnet_10g_sim_under_test", SIM_PATH)

Driver = DRV.AVProEdgeMXNet10GDriver
Simulator = SIMM.AVProEdgeMXNet10GSimulator

# The simulator's roster (see the sim's ENDPOINTS table).
TX_CABLE = "188A6ACE87DC"
TX_LAPTOP = "188A6A0F4485"
RX_BAR_L = "188A6A45C4A5"
RX_BAR_R = "188A6A45C4A6"
RX_BOARD = "188A6A1887E3"


# ── In-memory link ──────────────────────────────────────────────────────────

class _Link:
    """Stands in for TCPTransport: bytes the driver sends reach the simulator's
    handle_command, and whatever it returns — or pushes — comes back through
    the driver's OWN frame parser, which is what puts the framing under test
    rather than around it."""

    def __init__(self, driver, sim, *, silent=False, drip=False):
        self.driver = driver
        self.sim = sim
        self.connected = True
        self.sent: list[str] = []
        self.parser = driver._create_frame_parser()
        self.silent = silent
        self.drip = drip
        sim.push_targets.append(self)

    async def send(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("link closed")
        self.sent.append(bytes(data).decode().strip())
        if self.silent:
            return
        reply = self.sim.handle_command(bytes(data))
        if reply:
            await self.deliver(reply)

    async def deliver(self, data: bytes) -> None:
        if not self.connected:
            return
        chunks = [data[i : i + 1] for i in range(len(data))] if self.drip else [data]
        for chunk in chunks:
            for frame in self.parser.feed(chunk):
                await self.driver.on_data_received(frame)

    async def close(self) -> None:
        self.connected = False
        if self in self.sim.push_targets:
            self.sim.push_targets.remove(self)


async def _pair(overrides=None, *, connect=True, silent=False, drip=False):
    sim = Simulator("sim1", {})
    cfg = {"host": "10.0.0.30", "port": 24, "poll_interval": 0}
    cfg.update(overrides or {})
    driver = Driver("mx1", cfg, StubState(), StubEvents())
    link_box: dict = {}

    def _factory(drv):
        link_box["link"] = _Link(drv, sim, silent=silent, drip=drip)
        return link_box["link"]

    driver.transport_factory = _factory
    if connect:
        await driver.connect()
    else:
        driver.transport = _factory(driver)
    return driver, sim, link_box["link"]


def _dev(driver, prop):
    return driver.state.data.get(f"device.mx1.{prop}")


def _child(driver, ctype, cid, prop):
    return driver.state.data.get(f"device.mx1.{ctype}.{cid}.{prop}")


# ── Metadata / shape ────────────────────────────────────────────────────────

def test_metadata():
    info = Driver.DRIVER_INFO
    assert info["id"] == "avproedge_mxnet_10g"
    assert info["manufacturer"] == "AVPro Edge"
    assert info["category"] == "switcher"
    assert info["transport"] == "tcp"
    assert info["ports"] == [24]
    assert info["source_url"].startswith("https://")
    assert set(info["child_entity_types"]) == {"encoder", "decoder"}


def test_it_is_a_separate_driver_from_the_1g_and_says_so():
    """The two ecosystems share a grammar and diverge on the arguments, so a
    project pointed at the wrong one gets a box that accepts every command and
    does something else. The catalog entry has to be unmistakable."""
    info = Driver.DRIVER_INFO
    assert info["compatible_models"][0]["models"] == ["AC-MXNET-10G-CBOX"]
    assert "1G" in info["help"]["overview"]


def test_the_discovery_probe_cannot_claim_a_non_10g_control_box():
    """Every MXNet control box answers `config get name`. A probe matching
    "AC-MXNET" alone would identify a 1G, USP or Dante box as this driver."""
    probe = Driver.DRIVER_INFO["discovery"]["tcp_probe"]
    assert probe["port"] == 24
    assert set(probe) >= {"send_ascii", "expect_regex", "timeout_ms"}
    import re

    pattern = re.compile(probe["expect_regex"])
    assert pattern.search('{"cmd":"config get name","info":"AC-MXNET-10G-CBOX","code":0}')
    for other in ("AC-MXNET-CBOX", "AC-MXNET-CBOX-B", "AC-MXNET-USP-CBOX",
                  "AC-MXNET-DANTE-CBOX"):
        assert not pattern.search(
            '{"cmd":"config get name","info":"%s","code":0}' % other
        ), f"{other} must not match the 10G probe"


def test_six_routing_planes_and_all_streams_is_offered_first():
    """Routing only video and leaving audio on the previous source is silent on
    a panel, so the combined plane has to be the one a matrix picks by default."""
    planes = Driver.DRIVER_INFO["routing"]["planes"]
    assert planes[0]["label"] == "All streams"
    assert planes[0]["params"] == {"stream": "all"}
    labels = [p["label"] for p in planes[1:]]
    assert labels == ["Video", "Audio", "Analog audio", "USB", "IR", "Serial"]
    # Every plane's property must be a declared decoder state variable.
    decoder_props = Driver.DRIVER_INFO["child_entity_types"]["decoder"]["state_variables"]
    for plane in planes:
        assert plane["route_property"] in decoder_props


def test_the_route_command_offers_exactly_the_planes_the_driver_can_send():
    values = {
        v["value"]
        for v in Driver.DRIVER_INFO["commands"]["route"]["params"]["stream"]["values"]
    }
    assert values == {"all", *DRV.ROUTE_COMMANDS}
    assert set(DRV.ROUTE_PROPERTIES) == set(DRV.ROUTE_COMMANDS)
    assert set(DRV.CHANNEL_MEMBERS.values()) == set(DRV.ROUTE_COMMANDS)


def test_the_edid_and_hdcp_argument_sets_are_the_10g_document_not_the_1g():
    """Both are silent when wrong: the box accepts the number and applies a
    different mode, so a list copied from the sibling driver is invisible."""
    edid = Driver.DRIVER_INFO["commands"]["set_edid"]["params"]["edid"]["values"]
    assert [v["value"] for v in edid] == [str(i) for i in range(21)]
    hdcp = Driver.DRIVER_INFO["commands"]["set_hdcp"]["params"]["mode"]["values"]
    assert [v["value"] for v in hdcp] == ["0", "1", "2"]
    assert [v["label"] for v in hdcp] == ["Off", "HDCP 1.4", "HDCP 2.2"]


def test_rebooting_the_control_box_declares_its_restart_window():
    """Without this the platform raises a fault and alerts on a reboot somebody
    just asked for."""
    reboot = Driver.DRIVER_INFO["commands"]["reboot_cbox"]
    # A literal in the driver on purpose: the contract check reads the source,
    # so a named constant would hide the field that sets the platform floor.
    assert reboot["restarts_device_for"] == 120
    assert Driver.DRIVER_INFO["min_platform_version"] == "0.34.0"
    # An endpoint reboot does NOT take the control box away, so it must not
    # declare one -- the platform would stop trusting the box for two minutes.
    assert "restarts_device_for" not in Driver.DRIVER_INFO["commands"]["reboot_endpoint"]


def test_every_declared_command_has_a_branch_in_send_command():
    """A command with no branch answers `{"success":true}` — byte-identical to
    one that worked. Nothing else in the toolchain catches it."""
    source = inspect.getsource(Driver.send_command)
    missing = [
        name for name in Driver.DRIVER_INFO["commands"] if f'"{name}"' not in source
    ]
    assert not missing, f"commands with no send_command branch: {missing}"


def test_declared_actions_and_quick_actions_name_real_commands():
    info = Driver.DRIVER_INFO
    commands = set(info["commands"])
    for action in info["actions"]:
        if action["kind"] == "command":
            assert action["id"] in commands, action["id"]
    assert set(info["quick_actions"]) <= commands


def test_every_device_setting_is_handled():
    source = inspect.getsource(Driver.set_device_setting)
    for name, entry in Driver.DRIVER_INFO["device_settings"].items():
        assert f'"{name}"' in source, name
        assert entry["state_key"] in Driver.DRIVER_INFO["state_variables"]


# ── Framing: the three reply shapes ─────────────────────────────────────────

def test_framing_returns_one_whole_json_object():
    buf = b'{"cmd":"config get name","info":"AC-MXNET-10G-CBOX","code":0}\r\n'
    frame, rest = DRV._json_frame(buf)
    assert json.loads(frame)["info"] == "AC-MXNET-10G-CBOX"
    assert rest == b"\r\n"


def test_framing_splits_two_objects_arriving_in_one_read():
    buf = b'{"a":1}{"b":2}'
    first, rest = DRV._json_frame(buf)
    second, rest = DRV._json_frame(rest)
    assert json.loads(first) == {"a": 1}
    assert json.loads(second) == {"b": 2}
    assert rest == b""


def test_framing_waits_for_an_object_split_across_reads():
    whole = b'{"info":"half"}'
    for cut in range(1, len(whole)):
        frame, rest = DRV._json_frame(whole[:cut])
        assert frame is None
        assert rest == whole[:cut]
    frame, _ = DRV._json_frame(whole)
    assert json.loads(frame)["info"] == "half"


def test_framing_ignores_a_brace_inside_a_string():
    buf = b'{"info":"a } brace \\" and a quote","code":0}'
    frame, rest = DRV._json_frame(buf)
    assert json.loads(frame)["code"] == 0
    assert rest == b""


def test_framing_carries_a_multi_line_lua_table_whole():
    """The video-wall queries answer in the control box's own config syntax,
    which is brace-balanced and is not JSON."""
    table = SIMM.AVProEdgeMXNet10GSimulator("s", {})._wall_table(
        {"BarWall": {"Full": ["1:1:Cable-Box:Bar-Left:1:2:1:1:1:3:102:100:100"]}}
    )
    frame, rest = DRV._json_frame((table + "\r\n").encode())
    assert frame.decode() == table
    assert rest == b"\r\n"


def test_framing_completes_a_bare_ok_on_its_line_ending():
    """Half the `vwid` family answers with a token, not an object. Brace
    balancing alone never completes one, so the request would time out."""
    frame, rest = DRV._json_frame(b"OK\r\n{\"next\":1}")
    assert frame == b"OK"
    assert rest.startswith(b"\r\n")


def test_framing_discards_leading_noise_without_losing_the_reply():
    frame, rest = DRV._json_frame(b'\r\n\r\n{"code":0}')
    assert frame == b""
    frame, _ = DRV._json_frame(rest)
    assert json.loads(frame) == {"code": 0}


# ── The Lua table reader ────────────────────────────────────────────────────

def test_lua_reader_finds_the_walls_and_their_layouts():
    text = """{
    videowall1 = {
     cols = 2,
     layouts = {
      vlayout1 = {
       cols = 2,
       layout = {
        "1:1:TX1:RX1:1:1:1:1:1:3:102:100:100",
        "1:2:TX1:RX2:1:1:1:1:1:3:102:100:100"
       },
       rows = 2
      },
      vlayout2 = {
       cols = 2,
       layout = {},
       rows = 2
      }
     },
     rows = 2
    }
}"""
    tree = DRV._lua_names(text)
    assert list(tree) == ["videowall1"]
    assert sorted(tree["videowall1"]["layouts"]) == ["vlayout1", "vlayout2"]


def test_lua_reader_is_not_fooled_by_a_brace_inside_a_quoted_tile():
    tree = DRV._lua_names('{ wall = { layouts = { one = { layout = { "a{b}c" } } } } }')
    assert sorted(tree["wall"]["layouts"]) == ["one"]


# ── Value normalisation ─────────────────────────────────────────────────────

def test_hdcp_normalises_both_spellings_and_passes_anything_else_through():
    """An encoder says HDCP1 and a decoder says HDCP ON for the same condition,
    so two endpoint cards would otherwise disagree and no trigger could compare
    them."""
    assert DRV._hdcp("HDCP1") == "On"
    assert DRV._hdcp("HDCP ON") == "On"
    assert DRV._hdcp("HDCP0") == "Off"
    assert DRV._hdcp("HDCP OFF") == "Off"
    assert DRV._hdcp("") == ""
    assert DRV._hdcp("HDCP 2.2") == "HDCP 2.2"


def test_resolution_reads_all_three_shapes_the_api_uses():
    assert DRV._resolution(" 3840X2160p/30Hz") == "3840X2160p/30Hz"
    # The pushed AV-info line uses @ for the identical timing.
    assert DRV._resolution("1920X1080p@59Hz") == "1920X1080p/59Hz"
    # `device info` answers with an object for a decoder.
    assert DRV._resolution(
        {"width": "3840", "height": "2160", "frames_per_second": "30"}
    ) == "3840X2160p/30Hz"
    assert DRV._resolution("@,,,") == ""
    assert DRV._resolution(None) == ""


def test_presence_is_the_heartbeat_and_absent_means_gone():
    """`online` is a heartbeat, compared against the newest in the same reply
    rather than this host's clock — the document's examples come from a box
    whose clock has never been set."""
    present = DRV._presence(
        {
            "a": {"online": 17569},
            "b": {"online": 17560},
            "c": {"state": "s_srv_on"},        # no heartbeat at all
            "d": {"online": 100},              # heartbeat hours behind
        }
    )
    assert present == {"a": True, "b": True, "c": False, "d": False}


def test_presence_is_not_read_off_the_service_state():
    """An encoder with no source sits in s_attaching indefinitely while being
    perfectly reachable. Reading `state` as presence is what dropped the 1G
    bench's only encoder out of every source list."""
    present = DRV._presence({"tx": {"online": 17569, "state": "s_attaching"}})
    assert present["tx"] is True


# ── Roster ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_enumerates_encoders_and_decoders_from_the_device():
    driver, _sim, _link = await _pair()
    assert _dev(driver, "model") == "AC-MXNET-10G-CBOX"
    assert sorted(driver.list_children("encoder")) == sorted([TX_CABLE, TX_LAPTOP])
    assert sorted(driver.list_children("decoder")) == sorted(
        [RX_BAR_L, RX_BAR_R, RX_BOARD]
    )
    assert _dev(driver, "encoder_count") == 2
    assert _dev(driver, "decoder_count") == 3
    assert _child(driver, "encoder", TX_CABLE, "name") == "Cable-Box"
    assert _child(driver, "encoder", TX_CABLE, "channel") == "0009"
    await driver.disconnect()


@pytest.mark.asyncio
async def test_an_endpoint_with_no_heartbeat_is_offline_but_keeps_its_child():
    """The control box keeps an endpoint in its database after it stops
    answering, so it stays visible with a reason rather than vanishing."""
    driver, _sim, _link = await _pair()
    assert _child(driver, "decoder", RX_BOARD, "online") is False
    assert _dev(driver, "offline_endpoints") == 1
    assert RX_BOARD in driver.list_children("decoder")
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_rename_moves_the_label_and_keeps_the_child_id():
    """The roster is KEYED by the endpoint's current name, so keying children
    off that would deregister every renamed endpoint and orphan its bindings."""
    driver, _sim, _link = await _pair()
    await driver.send_command("rename_endpoint", {"endpoint": TX_CABLE, "name": "Sat-Box"})
    assert TX_CABLE in driver.list_children("encoder")
    assert _child(driver, "encoder", TX_CABLE, "name") == "Sat-Box"
    assert set(driver.list_children("encoder")) == {TX_CABLE, TX_LAPTOP}
    # And the new name still resolves for a later command.
    await driver.send_command("identify", {"endpoint": "Sat-Box"})
    await driver.disconnect()


@pytest.mark.asyncio
async def test_refresh_children_deregisters_an_endpoint_removed_from_the_database():
    driver, sim, _link = await _pair()
    sim._eps.pop(RX_BAR_R)
    sim._routes.pop(RX_BAR_R, None)
    result = await driver.refresh_children()
    assert RX_BAR_R not in driver.list_children("decoder")
    assert result == {"encoders": 2, "decoders": 2}
    await driver.disconnect()


@pytest.mark.asyncio
async def test_the_pickers_list_every_endpoint_by_name():
    driver, _sim, _link = await _pair()
    encoders = json.loads(_dev(driver, "encoder_options"))
    assert {e["value"] for e in encoders} == {TX_CABLE, TX_LAPTOP}
    assert {e["label"] for e in encoders} == {"Cable-Box", "Laptop-HDMI"}
    everything = json.loads(_dev(driver, "endpoint_options"))
    assert len(everything) == 5
    assert any(e["label"].endswith("(Decoder)") for e in everything)
    await driver.disconnect()


# ── Routes: derived, not queried ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_routes_are_derived_by_joining_channels_to_the_hosting_encoder():
    """There is no route query on the 10G. A decoder reports the channel it
    subscribes to per plane; the encoder that hosts that channel is the source."""
    driver, _sim, _link = await _pair()
    assert _child(driver, "decoder", RX_BAR_L, "source_video") == TX_CABLE
    assert _child(driver, "decoder", RX_BAR_L, "source_audio") == TX_CABLE
    # Boardroom is subscribed to a channel no encoder hosts — that is unrouted,
    # without assuming what an idle channel number looks like.
    assert _child(driver, "decoder", RX_BOARD, "source_video") == ""
    await driver.disconnect()


@pytest.mark.asyncio
async def test_no_route_query_is_ever_sent():
    """`config get device routes` is the 1G's query and does not exist here;
    sending it would sit until the request timed out on every poll."""
    driver, _sim, link = await _pair()
    await driver.poll()
    assert not any("routes" in line for line in link.sent)
    await driver.disconnect()


@pytest.mark.asyncio
async def test_routing_all_sends_one_path_command_per_plane():
    driver, sim, link = await _pair()
    link.sent.clear()
    await driver.send_command("route", {"tx": TX_LAPTOP, "rx": RX_BAR_L, "stream": "all"})
    paths = [line for line in link.sent if "path " in line]
    assert len(paths) == 6
    for command in DRV.ROUTE_COMMANDS.values():
        assert any(f"config set device {command} " in line for line in paths)
    assert sim._routes[RX_BAR_L]["analogaudio"] == TX_LAPTOP
    for prop in DRV.ROUTE_PROPERTIES.values():
        assert _child(driver, "decoder", RX_BAR_L, prop) == TX_LAPTOP
    await driver.disconnect()


@pytest.mark.asyncio
async def test_routing_never_uses_matrix_aset():
    """`matrix aset` is acked and then ignored when a destination's plane is
    disabled — the sequence a matrix panel produces (press Off, press a source).
    Reproduced on the 1G box in this family; the simulator models the same no-op.
    This test exists so nobody "simplifies" six commands back into the one-liner
    that reads better and drops the route."""
    driver, sim, link = await _pair()
    await driver.send_command("route_off", {"rx": RX_BAR_L, "stream": "all"})
    link.sent.clear()
    await driver.send_command("route", {"tx": TX_CABLE, "rx": RX_BAR_L, "stream": "all"})
    assert not any(line.lower().startswith("matrix aset") for line in link.sent)
    # And the route actually landed on the device, which aset would not have.
    assert sim._routes[RX_BAR_L]["video"] == TX_CABLE
    await driver.disconnect()


@pytest.mark.asyncio
async def test_the_simulator_reproduces_the_aset_no_op():
    """Pins the finding itself rather than only the driver's avoidance of it."""
    _driver, sim, _link = await _pair()
    sim._routes[RX_BAR_L]["video"] = ""
    reply = sim.handle_command(b"matrix aset :v Cable-Box Bar-Left")
    assert json.loads(reply)["code"] == 0          # accepted...
    assert sim._routes[RX_BAR_L]["video"] == ""    # ...and not applied


@pytest.mark.asyncio
async def test_routing_one_plane_leaves_the_others_alone():
    driver, sim, _link = await _pair()
    await driver.send_command("route", {"tx": TX_LAPTOP, "rx": RX_BAR_L, "stream": "audio"})
    assert sim._routes[RX_BAR_L]["audio"] == TX_LAPTOP
    assert sim._routes[RX_BAR_L]["video"] == TX_CABLE
    await driver.disconnect()


@pytest.mark.asyncio
async def test_clearing_a_route_empties_the_property():
    driver, sim, _link = await _pair()
    await driver.send_command("route_off", {"rx": RX_BAR_L, "stream": "video"})
    assert sim._routes[RX_BAR_L]["video"] == ""
    assert _child(driver, "decoder", RX_BAR_L, "source_video") == ""
    await driver.poll()
    assert _child(driver, "decoder", RX_BAR_L, "source_video") == ""
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_commanded_route_outranks_a_stale_readback():
    """The box acks a route long before it reports it. Without the hold, a poll
    landing mid-transition shows the old source for a cycle or two, which reads
    as a failed press and gets pressed again."""
    driver, sim, _link = await _pair()
    await driver.send_command("route", {"tx": TX_LAPTOP, "rx": RX_BAR_L, "stream": "video"})
    # The device has not caught up yet.
    sim._routes[RX_BAR_L]["video"] = TX_CABLE
    await driver.poll()
    assert _child(driver, "decoder", RX_BAR_L, "source_video") == TX_LAPTOP
    # Once it agrees, the hold is released and the device is authoritative again.
    sim._routes[RX_BAR_L]["video"] = TX_LAPTOP
    await driver.poll()
    sim._routes[RX_BAR_L]["video"] = TX_CABLE
    await driver.poll()
    assert _child(driver, "decoder", RX_BAR_L, "source_video") == TX_CABLE
    await driver.disconnect()


@pytest.mark.asyncio
async def test_the_route_hold_expires_even_for_a_plane_the_box_never_reports():
    """`ch_l` may not exist on older firmware, so its expectation would
    otherwise be held for the life of the session."""
    driver, _sim, _link = await _pair()
    driver._expect_routes(RX_BAR_L, {"source_analog_audio": TX_LAPTOP})
    driver._route_expect[RX_BAR_L]["source_analog_audio"] = (TX_LAPTOP, 0.0)
    settled = driver._settle_routes(RX_BAR_L, {"source_video": TX_CABLE})
    assert settled == {"source_video": TX_CABLE}
    assert RX_BAR_L not in driver._route_expect
    await driver.disconnect()


@pytest.mark.asyncio
async def test_routing_to_an_endpoint_that_is_not_there_surfaces_the_box_reason():
    driver, _sim, _link = await _pair()
    with pytest.raises(ValueError, match="Device not online"):
        await driver.send_command("route", {"tx": TX_CABLE, "rx": RX_BOARD})
    await driver.disconnect()


@pytest.mark.asyncio
async def test_an_unknown_endpoint_is_refused_before_it_reaches_the_wire():
    driver, _sim, link = await _pair()
    link.sent.clear()
    with pytest.raises(ValueError, match="Unknown encoder"):
        await driver.send_command("route", {"tx": "nope", "rx": RX_BAR_L})
    assert link.sent == []
    await driver.disconnect()


# ── Status fan-out ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_status_reply_fans_out_across_every_endpoint():
    driver, _sim, _link = await _pair()
    await driver.poll()
    assert _child(driver, "encoder", TX_CABLE, "signal_present") is True
    assert _child(driver, "encoder", TX_CABLE, "resolution") == "3840X2160p/30Hz"
    assert _child(driver, "encoder", TX_CABLE, "hdcp") == "On"
    assert _child(driver, "encoder", TX_CABLE, "source_connected") is True
    # The laptop is present with nothing plugged in: HPD low, no timing.
    assert _child(driver, "encoder", TX_LAPTOP, "online") is True
    assert _child(driver, "encoder", TX_LAPTOP, "signal_present") is False
    # A decoder spells HDCP in words; both land normalised.
    assert _child(driver, "decoder", RX_BAR_L, "hdcp") == "On"
    assert _child(driver, "decoder", RX_BAR_L, "display_connected") is True
    await driver.disconnect()


@pytest.mark.asyncio
async def test_the_whole_install_costs_two_broad_queries_per_poll():
    """Poll cost has to be flat in the number of endpoints, not one query each."""
    driver, _sim, link = await _pair()
    driver._poll_cycle = 1          # skip the slow system cycle
    link.sent.clear()
    await driver.poll()
    assert link.sent == [
        "config get devicelist",
        "config get device status ALL",
    ]
    await driver.disconnect()


@pytest.mark.asyncio
async def test_system_info_and_the_pickers_refresh_on_the_slow_cycle():
    driver, _sim, _link = await _pair()
    await driver.poll()
    assert _dev(driver, "firmware") == "3.01"
    assert _dev(driver, "timezone") == "UTC+0"
    assert _dev(driver, "lan_ip") == "192.168.1.239"
    assert _dev(driver, "dns_servers") == "8.8.8.8 8.8.4.4"
    assert _dev(driver, "system_date").startswith("2026-")
    matrices = json.loads(_dev(driver, "matrix_options"))
    assert {m["value"] for m in matrices} == {"Bar", "AllHands"}
    await driver.disconnect()


@pytest.mark.asyncio
async def test_an_ipsetting_of_plain_autoip_does_not_invent_an_address():
    """LAN1 answers `autoip` with no address at all on a box that has not been
    given one; splitting on / would otherwise index past the end."""
    driver, _sim, _link = await _pair()
    await driver.poll()
    assert _dev(driver, "av_ip") == ""
    await driver.disconnect()


# ── Video walls: the non-JSON replies ───────────────────────────────────────

@pytest.mark.asyncio
async def test_the_wall_picker_is_read_out_of_the_lua_reply():
    driver, _sim, _link = await _pair()
    await driver.poll()
    walls = json.loads(_dev(driver, "videowall_options"))
    assert {w["value"] for w in walls} == {"BarWall", "Boardroom"}
    pairs = json.loads(_dev(driver, "videowall_layout_options"))
    assert {p["value"] for p in pairs} == {
        "BarWall|Full", "BarWall|Split", "Boardroom|Single"
    }
    assert any(p["label"] == "BarWall — Full" for p in pairs)
    await driver.disconnect()


@pytest.mark.asyncio
async def test_recalling_a_wall_layout_applies_its_routes():
    driver, sim, _link = await _pair()
    await driver.poll()
    sim._routes[RX_BAR_L]["video"] = ""
    sim._routes[RX_BAR_R]["video"] = ""
    await driver.send_command("recall_videowall_layout", {"layout": "BarWall|Full"})
    assert sim._routes[RX_BAR_L]["video"] == TX_CABLE
    assert sim._routes[RX_BAR_R]["video"] == TX_CABLE
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_layout_that_belongs_to_another_wall_is_refused_with_the_real_list():
    driver, _sim, _link = await _pair()
    await driver.poll()
    with pytest.raises(ValueError, match="has no layout"):
        await driver.send_command(
            "recall_videowall_layout", {"layout": "Boardroom|Full"}
        )
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_layout_value_that_is_not_a_pair_is_refused():
    driver, _sim, _link = await _pair()
    with pytest.raises(ValueError, match="not a wall and layout"):
        await driver.send_command("recall_videowall_layout", {"layout": "BarWall"})
    await driver.disconnect()


@pytest.mark.asyncio
async def test_multiview_survives_the_malformed_json_the_box_answers_with():
    """The document's own examples show these replies carrying unescaped quotes
    inside `cmd`, which no parser can read. The command still has to work."""
    driver, _sim, _link = await _pair()
    await driver.poll()
    assert await driver.send_command(
        "activate_multiview", {"layout": "BarWall|Full", "index": "2:1"}
    ) is True
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_raw_frame_cannot_satisfy_a_request_that_expects_json():
    """A banner or a stray line must not be handed to a `config` query — that
    is how a driver starts answering every request with the previous one. The
    request has to time out instead, which is recoverable; being answered with
    the wrong thing is not."""
    driver, _sim, link = await _pair()
    link.silent = True          # the box never answers this one

    async def _noise():
        await asyncio.sleep(0)
        await link.deliver(b"Welcome to MXNet\r\n")

    task = asyncio.ensure_future(_noise())
    result = await driver._request("config get version", timeout=0.3)
    await task
    assert result is None

    # A `vwid` request, which is the one kind that DOES answer in raw text,
    # takes the same frame.
    task = asyncio.ensure_future(_noise())
    result = await driver._request("vwid list", timeout=0.3)
    await task
    assert result == {"cmd": "vwid list", "info": "Welcome to MXNet", "code": 0}


# ── Unsolicited frames ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_event_frame_is_never_handed_to_the_waiting_request():
    """An empty `cmd` plus a `source` is an event. Consuming one as a reply
    hands this request the event and shifts every reply after it by one."""
    driver, sim, link = await _pair()

    async def _interleave():
        await asyncio.sleep(0)
        await link.deliver(sim._event_frame(TX_CABLE, "IN1 HPD 1"))

    task = asyncio.ensure_future(_interleave())
    doc = await driver._request("config get version")
    await task
    assert doc["cmd"] == "config get version"
    assert doc["info"] == "3.01"
    await driver.disconnect()


@pytest.mark.asyncio
async def test_an_av_info_event_updates_the_endpoint_it_names():
    driver, sim, link = await _pair()
    await link.deliver(
        sim._event_frame(
            RX_BAR_L, "OUT1 AV INF 1920X1080p@59Hz,RGB,8Bit,HDR ON,HDCP ON,PCM"
        )
    )
    assert _child(driver, "decoder", RX_BAR_L, "resolution") == "1920X1080p/59Hz"
    assert _child(driver, "decoder", RX_BAR_L, "chroma") == "RGB"
    assert _child(driver, "decoder", RX_BAR_L, "hdr") is True
    assert _child(driver, "decoder", RX_BAR_L, "hdcp") == "On"
    assert _child(driver, "decoder", RX_BAR_L, "audio_format") == "PCM"
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_signal_less_av_info_event_clears_rather_than_parses():
    driver, sim, link = await _pair()
    await link.deliver(sim._event_frame(TX_CABLE, "IN1 AV INF @,,,HDR OFF,HDCP ON,"))
    assert _child(driver, "encoder", TX_CABLE, "resolution") == ""
    assert _child(driver, "encoder", TX_CABLE, "signal_present") is False
    await driver.disconnect()


@pytest.mark.asyncio
async def test_an_unrecognised_event_is_kept_rather_than_dropped():
    """The channel is undocumented on this box, so a shape we cannot parse has
    to stay visible instead of disappearing."""
    driver, sim, link = await _pair()
    await link.deliver(sim._event_frame(TX_CABLE, "SOMETHING NEW 42"))
    assert _child(driver, "encoder", TX_CABLE, "last_event") == "SOMETHING NEW 42"
    await driver.disconnect()


@pytest.mark.asyncio
async def test_serial_data_lands_on_the_endpoint_that_received_it():
    driver, _sim, _link = await _pair()
    await driver.send_command(
        "send_serial", {"endpoint": RX_BAR_L, "data": "PWR ON", "append_cr": True}
    )
    assert _child(driver, "decoder", RX_BAR_L, "serial_data") == "PWR ON"
    assert _child(driver, "decoder", RX_BAR_R, "serial_data") in (None, "")
    await driver.disconnect()


@pytest.mark.asyncio
async def test_base64_serial_feedback_is_decoded_when_that_is_the_configured_format():
    driver, sim, link = await _pair({"serial_feedback_format": "base64"})
    await link.deliver(sim._serial_frame(RX_BAR_L, "UFdSIE9O"))
    assert _child(driver, "decoder", RX_BAR_L, "serial_data") == "PWR ON"
    await driver.disconnect()


@pytest.mark.asyncio
async def test_serial_feedback_that_is_not_valid_base64_is_kept_raw():
    driver, sim, link = await _pair({"serial_feedback_format": "base64"})
    await link.deliver(sim._serial_frame(RX_BAR_L, "not base64 !!"))
    assert _child(driver, "decoder", RX_BAR_L, "serial_data") == "not base64 !!"
    await driver.disconnect()


# ── Commands ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,params,expected",
    [
        ("set_edid", {"tx": TX_CABLE, "edid": "17"},
         "config set device edid 17 188A6ACE87DC"),
        ("copy_edid", {"rx": RX_BAR_L, "tx": TX_CABLE},
         "config set device copyedid 188A6A45C4A5 188A6ACE87DC"),
        ("set_encoder_volume", {"tx": TX_CABLE, "level": 45},
         "config set device exaudio volume 45 188A6ACE87DC"),
        ("set_encoder_stream", {"tx": TX_CABLE, "state": "off"},
         "config set device stream off 188A6ACE87DC"),
        ("set_hdmi_input", {"tx": TX_CABLE, "input": "1", "state": "off"},
         "config set device hdmi 1 off 188A6ACE87DC"),
        ("set_downmix", {"tx": TX_CABLE, "mode": "5"},
         "config set device exmxmode 5 188A6ACE87DC"),
        ("set_output_resolution", {"rx": RX_BAR_L, "timing": "1920 1080 60"},
         "config set device video 1920 1080 60 188A6A45C4A5"),
        ("set_hdcp", {"rx": RX_BAR_L, "mode": "2"},
         "config set device hdcp 2 188A6A45C4A5"),
        ("set_hdr", {"rx": RX_BAR_L, "state": "0"},
         "config set device hdrmode 0 188A6A45C4A5"),
        ("identify", {"endpoint": TX_CABLE, "mode": "flash"},
         "config set device light flash 188A6ACE87DC"),
        ("reboot_endpoint", {"endpoint": RX_BAR_L},
         "config set device reboot 188A6A45C4A5"),
        ("hpd_reset", {"endpoint": RX_BAR_L},
         "config set device hpdrst 188A6A45C4A5"),
        ("cec_power", {"endpoint": RX_BAR_L, "state": "on"},
         "config set device cec poweron 188A6A45C4A5"),
        ("cec_power", {"endpoint": RX_BAR_L, "state": "off"},
         "config set device cec poweroff 188A6A45C4A5"),
        ("send_cec", {"endpoint": RX_BAR_L, "data": "0036"},
         "config set device cec 0036 188A6A45C4A5"),
        ("set_serial_settings",
         {"endpoint": RX_BAR_L, "baud": "19200", "data_bits": "8",
          "parity": "1", "stop_bits": "2"},
         "config set device rs232setting 19200 8 1 2 188A6A45C4A5"),
        ("describe_endpoint", {"endpoint": RX_BAR_L, "description": "BAR LEFT TV"},
         "config set device description BAR LEFT TV 188A6A45C4A5"),
        ("rename_avdm", {"tx": TX_LAPTOP, "name": "AVDM1"},
         "config set device avdmid AVDM1 188A6A0F4485"),
        ("describe_avdm", {"tx": TX_LAPTOP, "description": "Stage feed"},
         "config set device avdmdes Stage feed 188A6A0F4485"),
        ("reboot_cbox", {}, "config set reboot"),
    ],
)
async def test_commands_put_the_documented_line_on_the_wire(command, params, expected):
    driver, _sim, link = await _pair()
    link.sent.clear()
    await driver.send_command(command, params)
    assert expected in link.sent
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_written_edid_is_mirrored_back_into_child_state():
    driver, sim, _link = await _pair()
    await driver.send_command("set_edid", {"tx": TX_CABLE, "edid": "17"})
    assert _child(driver, "encoder", TX_CABLE, "edid") == "17"
    assert sim._eps[TX_CABLE]["edid"] == "17"
    await driver.disconnect()


@pytest.mark.asyncio
async def test_the_clock_is_set_from_the_server_and_read_back():
    driver, _sim, link = await _pair()
    link.sent.clear()
    await driver.send_command("sync_clock", {})
    assert any(line.startswith("config set date ") for line in link.sent)
    assert _dev(driver, "system_date") != "2026-09-12 09:15:00"
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_rejected_command_raises_with_the_boxs_own_reason():
    driver, _sim, _link = await _pair()
    with pytest.raises(ValueError, match="invalid edid value"):
        # 21 is one past the documented range; the box refuses it.
        await driver._write("config set device edid 21 188A6ACE87DC")
    await driver.disconnect()


@pytest.mark.asyncio
async def test_the_raw_command_escape_hatch_returns_the_reply():
    driver, _sim, _link = await _pair()
    out = json.loads(await driver.send_command("raw_command", {"command": "config get name"}))
    assert out["info"] == "AC-MXNET-10G-CBOX"
    await driver.disconnect()


# ── Device settings ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_settings_write_through_and_mirror_back():
    driver, sim, _link = await _pair()
    await driver.set_device_setting("timezone", "UTC-5")
    assert _dev(driver, "timezone") == "UTC-5"
    assert sim.state["timezone"] == "UTC-5"

    await driver.set_device_setting("ntp_servers", "10.0.0.1 10.0.0.2")
    assert _dev(driver, "ntp_servers") == "10.0.0.1 10.0.0.2"
    assert sim._ntp == ["10.0.0.1", "10.0.0.2"]

    await driver.set_device_setting("dns_servers", "1.1.1.1")
    assert sim._dns == ["1.1.1.1"]
    await driver.disconnect()


@pytest.mark.asyncio
async def test_a_bad_setting_is_refused_before_it_reaches_the_device():
    driver, _sim, link = await _pair()
    link.sent.clear()
    with pytest.raises(ValueError, match="UTC-5"):
        await driver.set_device_setting("timezone", "Eastern")
    with pytest.raises(ValueError, match="five NTP servers"):
        await driver.set_device_setting("ntp_servers", "a b c d e f")
    with pytest.raises(ValueError, match="two DNS servers"):
        await driver.set_device_setting("dns_servers", "1.1.1.1 2.2.2.2 3.3.3.3")
    assert link.sent == []
    await driver.disconnect()


# ── Connection lifecycle ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connecting_to_a_1g_control_box_says_which_driver_to_use():
    """Both boxes answer the same query on the same port and accept many of the
    same commands with different meanings, so a wrong pairing looks like broken
    hardware rather than a wrong driver."""
    sim = Simulator("sim1", {})
    sim.set_state("cbox_name", "AC-MXNET-CBOX-B")
    driver = Driver("mx1", {"host": "10.0.0.30", "port": 24, "poll_interval": 0},
                    StubState(), StubEvents())
    driver.transport_factory = lambda drv: _Link(drv, sim)
    with pytest.raises(ConnectionError, match="not a 10G control box"):
        await driver.connect()
    assert _dev(driver, "connected") is not True


@pytest.mark.asyncio
async def test_a_silent_box_fails_the_connection_rather_than_reporting_connected():
    driver, _sim, _link = await _pair(connect=False, silent=True)
    with pytest.raises(ConnectionError, match="No answer from the MXNet API"):
        await driver.connect()


@pytest.mark.asyncio
async def test_poll_propagates_a_box_that_stops_answering():
    """The platform's watchdog only flips the device offline if poll raises."""
    driver, _sim, link = await _pair()
    link.silent = True
    for _ in range(DRV.MAX_POLL_MISSES - 1):
        await driver.poll()
    with pytest.raises(ConnectionError, match="stopped answering"):
        await driver.poll()


@pytest.mark.asyncio
async def test_a_reply_split_byte_by_byte_still_reassembles():
    driver, _sim, _link = await _pair(drip=True)
    assert _dev(driver, "model") == "AC-MXNET-10G-CBOX"
    assert len(driver.list_children("encoder")) == 2
    await driver.disconnect()


@pytest.mark.asyncio
async def test_disconnect_clears_the_route_holds_and_poll_bookkeeping():
    driver, _sim, _link = await _pair()
    await driver.send_command("route", {"tx": TX_LAPTOP, "rx": RX_BAR_L, "stream": "video"})
    assert driver._route_expect
    await driver.disconnect()
    assert driver._route_expect == {}
    assert driver._poll_cycle == 0
