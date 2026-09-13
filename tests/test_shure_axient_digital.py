"""Driver + simulator tests for shure_axient_digital (AD4D / AD4Q over Shure
command strings on TCP 2202).

No Axient Digital hardware on hand, so correctness is proven as a dual-proof
round trip: the real driver wired to the real simulator through an in-memory
link that frames on ``>`` the way the platform transport does, plus unit tests
on the value conversions the document is specific about, and a command-surface
check (every declared command has a branch in ``send_command``).

What is specific to THIS device, and so what these tests are mostly about:

  - **Every level is offset on the wire** (gain by 18, audio and RSSI by 120,
    the transmitter offset by 12, temperature by 40) and 255 / 65533..65535 are
    sentinels, so a value read back must be the dB / percent / minutes a person
    expects and an unknown must be None with its state named.
  - **The SAMPLE frame's layout follows the channel's mode**: two antennas, four
    in Quadversity, and a second RF section for an FD-C channel. Each layout is
    parsed, and the overload LED bits become rf_peak / af_peak.
  - **The roster comes from MODEL**: an AD4D added as a four-channel device
    shrinks to two, an AD4Q added as two grows to four and reads the new
    channels.
  - **Slots report presence**: EMPTY is not_fitted, LINKED.INACTIVE is
    not_responding, and a SET on anything but a LINKED.ACTIVE slot is refused
    by the receiver and leaves state alone.
  - **A channel push never satisfies the MODEL liveness probe.**

The driver and simulator are loaded with the ``openavc.*`` imports stubbed so
the community CI stays self-contained (conftest.py rolls the stubs back).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    DelimiterFrameParser,
    StubBaseDriver,
    StubEvents,
    StubState,
    StubTCPSimulator,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "audio" / "shure_axient_digital.py"
SIM_PATH = REPO_ROOT / "audio" / "shure_axient_digital_sim.py"


class _FakeBaseDriver(LifecycleFake, StubBaseDriver):
    """The platform's hook-driven connect for a raw-pipe driver: the transport
    is the in-memory link the test supplies; state, children and the watchdog
    come from the shared stubs."""

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0
        self.transport = None
        self._connected = False
        self.transport_factory = None
        self._bg_tasks = set()

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
        await self._initial_sync()

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
    pass


install_stubs(
    {"openavc.simulator.tcp_simulator": {"TCPSimulator": _FakeTCPSimulator}},
    base_driver=_FakeBaseDriver,
)
DRV = load_module("shure_axient_digital_under_test", DRIVER_PATH)
SIMM = load_module("shure_axient_digital_sim_under_test", SIM_PATH)

Driver = DRV.ShureAxientDigitalDriver
Simulator = SIMM.ShureAxientDigitalSimulator


# ── In-memory link ──────────────────────────────────────────────────────────

class _Link:
    """Stands in for the TCP transport with ``delimiter=b">"``: bytes the
    driver sends reach the simulator's handle_command, and whatever it returns
    (or pushes) is framed on ``>`` and handed to ``on_data_received`` one
    message at a time, delimiter stripped, exactly as the platform transport
    does. ``drip`` delivers the bytes one at a time so the framing is under
    test."""

    def __init__(self, driver, sim, *, silent=False, drip=False):
        self.driver = driver
        self.sim = sim
        self.connected = True
        self.sent: list[str] = []
        self.silent = silent
        self.drip = drip
        self.parser = DelimiterFrameParser(delimiter=b">")
        sim.push_targets.append(self)

    async def send(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("link closed")
        self.sent.append(bytes(data).decode("ascii"))
        if self.silent:
            return
        reply = self.sim.handle_command(bytes(data))
        if reply:
            await self.deliver(reply)

    async def deliver(self, data: bytes) -> None:
        if not self.connected:
            return
        chunks = ([data[i:i + 1] for i in range(len(data))] if self.drip
                  else [data])
        for chunk in chunks:
            for frame in self.parser.feed(chunk):
                await self.driver.on_data_received(frame)

    async def close(self) -> None:
        self.connected = False
        if self in self.sim.push_targets:
            self.sim.push_targets.remove(self)


async def _settle():
    for _ in range(4):
        await asyncio.sleep(0)


def _make(config=None, sim_config=None, *, drip=False):
    cfg = {"host": "10.0.0.5", "port": 2202, "channel_count": 4}
    cfg.update(config or {})
    state, events = StubState(), StubEvents()
    driver = Driver("ad4", cfg, state, events)
    sim = Simulator("sim", {"channel_count": 4, **(sim_config or {})})
    driver.transport_factory = lambda d: _Link(d, sim, drip=drip)
    return driver, sim, state


async def _connected(config=None, sim_config=None, *, drip=False):
    driver, sim, state = _make(config, sim_config, drip=drip)
    await driver.connect()
    await _settle()
    return driver, sim, state


def _ch(driver, n):
    return driver.get_child_state("channel", n)


def _slot(driver, sid):
    return driver.get_child_state("slot", sid)


# ── Value conversions ───────────────────────────────────────────────────────

class TestConversions:
    def test_group_channel_parses_the_braced_pair_and_the_wildcard(self):
        assert DRV._group_channel("{6,100     }") == ("6", "100")
        assert DRV._group_channel("{1,1yyyyyyy}".replace("y", " ")) == ("1", "1")
        assert DRV._group_channel("{--,--     }") == ("", "")
        assert DRV._group_channel("garbage") == ("", "")

    def test_battery_minutes_sentinels_name_their_state(self):
        assert DRV._minutes("00125") == (125, "ok")
        assert DRV._minutes("65533") == (None, "communication_warning")
        assert DRV._minutes("65534") == (None, "calculating")
        assert DRV._minutes("65535") == (None, "unknown")

    def test_three_digit_unknown_is_none(self):
        assert DRV._int3("088") == 88
        assert DRV._int3("255") is None
        assert DRV._level("102") == -18.0
        assert DRV._level("255") is None
        assert DRV._offset_db("012") == 0
        assert DRV._offset_db("033") == 21
        assert DRV._offset_db("255") is None
        assert DRV._input_pad("000") is True
        assert DRV._input_pad("012") is False
        assert DRV._input_pad("255") is None

    def test_model_string_sizes_the_roster(self):
        assert DRV._channel_count_for_model("AD4Q-A") == 4
        assert DRV._channel_count_for_model("AD4D-B") == 2
        assert DRV._channel_count_for_model("MXA920") is None


# ── Connect and seed ────────────────────────────────────────────────────────

class TestConnect:
    @pytest.mark.asyncio
    async def test_topology_and_device_state_seed_on_connect(self):
        driver, sim, state = await _connected()
        assert driver.list_children("channel") == [1, 2, 3, 4]
        assert len(driver.list_children("slot")) == 32
        assert driver.get_state("model") == "AD4Q-A"
        assert driver.get_state("device_name") == "AD4Q-SIM"
        assert driver.get_state("firmware") == "2.0.15.2"
        assert driver.get_state("selftest_failed") is False
        assert driver.get_state("rf_band") == "G55"
        assert driver.get_state("encryption_enabled") is False
        assert driver.get_state("quadversity") is False
        assert driver.get_state("transmission_mode") == "standard"
        assert driver.get_state("channel_count_reported") == 4
        assert driver.get_state("ip_address") == "192.168.1.25"
        assert driver.get_state("mac_address") == "00:0E:DD:45:60:EB"
        link = driver.transport
        assert link.sent[0] == "< GET 0 ALL >"
        assert "< GET NET_SETTINGS SC >" in link.sent
        assert "< GET 1 SLOT_STATUS 0 >" in link.sent
        # Metering is off by default, so nothing armed it.
        assert not any("METER_RATE" in s for s in link.sent)

    @pytest.mark.asyncio
    async def test_channel_one_reads_its_transmitter_and_meters(self):
        driver, sim, state = await _connected()
        ch = _ch(driver, 1)
        assert ch["label"] == "Channel 1"
        assert ch["name"] == "Channel 1"
        assert ch["mute"] is False
        assert ch["gain"] == 12
        assert ch["frequency_khz"] == 606025
        assert (ch["preset_bank"], ch["preset_channel"]) == ("1", "1")
        assert ch["fd_mode"] == "off"
        assert ch["aes256_error"] is False
        assert ch["interference"] is False
        assert ch["unregistered_tx"] is False
        assert ch["metering"] is False and ch["meter_rate_ms"] == 0
        # The decoded ADX1.
        assert ch["tx_linked"] is True and ch["no_link"] is False
        assert ch["tx_model"] == "ADX1"
        assert ch["tx_name"] == "LeadVox"
        assert ch["tx_battery_type"] == "LION"
        assert ch["tx_battery_bars"] == 4
        assert ch["tx_battery_percent"] == 88
        assert ch["tx_battery_minutes"] == 125
        assert ch["tx_battery_state"] == "ok"
        assert ch["tx_battery_health_percent"] == 97
        assert ch["tx_battery_cycles"] == 19
        assert ch["tx_battery_temp_c"] == 22
        assert ch["tx_low_battery"] is False
        assert ch["tx_input_pad"] is False
        assert ch["tx_offset_db"] == 0
        assert ch["tx_polarity"] == "positive"
        assert ch["tx_power_mw"] == 10
        assert ch["tx_lock"] == "none"
        assert ch["tx_mute"] is False
        assert ch["tx_talk_switch"] is False
        # Metered values answered in the GET ALL dump.
        assert ch["chan_quality"] == 5
        assert ch["level_peak_dbfs"] == -18.0
        assert ch["level_dbfs"] == -22.0
        assert ch["antenna_status"] == "BB"
        assert ch["rssi_a_dbm"] == -34.0
        assert ch["rssi_b_dbm"] == -55.0
        assert ch["rssi_dbm"] == -34.0
        assert ch["rssi_c_dbm"] is None
        assert ch["af_peak"] is False

    @pytest.mark.asyncio
    async def test_channel_two_is_fdc_with_no_transmitter(self):
        driver, sim, state = await _connected()
        ch = _ch(driver, 2)
        assert ch["fd_mode"] == "combining"
        assert ch["frequency2_khz"] == 578850
        assert (ch["preset_bank"], ch["preset_channel"]) == ("", "")
        assert ch["interference2"] is False
        assert ch["tx_linked"] is False and ch["no_link"] is True
        assert ch["tx_model"] == ""
        assert ch["tx_name"] == ""
        assert ch["tx_battery_type"] == ""
        assert ch["tx_battery_bars"] is None
        assert ch["tx_battery_percent"] is None
        assert ch["tx_battery_minutes"] is None
        assert ch["tx_battery_state"] == "unknown"
        assert ch["tx_low_battery"] is False
        assert ch["tx_polarity"] is None
        assert ch["tx_lock"] is None
        assert ch["tx_mute"] is None
        assert ch["tx_talk_switch"] is None

    @pytest.mark.asyncio
    async def test_slots_report_presence(self):
        driver, sim, state = await _connected()
        standard = _slot(driver, "1-1")
        assert standard["status"] == "standard"
        assert standard["tx_model"] == "AD2"
        assert standard["online"] is True
        assert standard["offline_reason"] is None
        assert standard["battery_bars"] is None      # not remote-readable
        assert standard["channel"] == 1 and standard["slot_number"] == 1

        active = _slot(driver, "1-2")
        assert active["status"] == "linked_active"
        assert active["online"] is True
        assert active["tx_model"] == "ADX1"
        assert active["tx_name"] == "LeadVox"
        assert active["battery_percent"] == 87
        assert active["battery_minutes"] == 360
        assert active["battery_state"] == "ok"
        assert active["battery_cycles"] == 13
        assert active["input_pad"] is False
        assert active["offset_db"] == 0
        assert active["polarity"] == "positive"
        assert active["rf_muted"] is False
        assert active["rf_power_mw"] == 10
        assert active["rf_power_mode"] == "normal"
        assert active["showlink_quality"] == 5

        inactive = _slot(driver, "1-3")
        assert inactive["status"] == "linked_inactive"
        assert inactive["online"] is False
        assert inactive["offline_reason"] == "not_responding"
        assert inactive["tx_model"] == "ADX2"
        assert inactive["battery_percent"] is None

        empty = _slot(driver, "1-4")
        assert empty["status"] == "empty"
        assert empty["online"] is False
        assert empty["offline_reason"] == "not_fitted"
        assert empty["tx_model"] == "" and empty["tx_name"] == ""
        assert empty["label"] == "Channel 1 Slot 4"

    @pytest.mark.asyncio
    async def test_framing_survives_a_byte_at_a_time(self):
        driver, sim, state = await _connected(drip=True)
        assert _ch(driver, 1)["tx_name"] == "LeadVox"
        assert _slot(driver, "1-2")["tx_name"] == "LeadVox"
        assert _ch(driver, 1)["rssi_b_dbm"] == -55.0


# ── The roster follows MODEL ────────────────────────────────────────────────

class TestRoster:
    @pytest.mark.asyncio
    async def test_an_ad4d_added_as_four_channels_shrinks_to_two(self):
        driver, sim, state = await _connected(
            {"channel_count": 4}, {"channel_count": 2})
        assert driver.get_state("model") == "AD4D-A"
        assert driver.get_state("channel_count_reported") == 2
        assert driver.list_children("channel") == [1, 2]
        assert len(driver.list_children("slot")) == 16
        assert not state.get_namespace("device.ad4.channel.3")

    @pytest.mark.asyncio
    async def test_an_ad4q_added_as_two_channels_grows_and_reads_the_rest(self):
        driver, sim, state = await _connected(
            {"channel_count": 2, "meter_interval_ms": 500}, {"channel_count": 4})
        assert driver.list_children("channel") == [1, 2, 3, 4]
        assert len(driver.list_children("slot")) == 32
        link = driver.transport
        assert "< GET 3 ALL >" in link.sent and "< GET 4 ALL >" in link.sent
        assert "< GET 4 SLOT_STATUS 0 >" in link.sent
        assert "< SET 3 METER_RATE 00500 >" in link.sent
        assert _ch(driver, 3)["frequency_khz"] == 611975
        assert _ch(driver, 3)["metering"] is True
        assert _slot(driver, "4-8")["status"] == "empty"
        for n in range(1, 5):
            await driver.send_command("channel_meters_off", {"channel": n})

    @pytest.mark.asyncio
    async def test_a_report_for_a_channel_off_the_roster_is_ignored(self):
        driver, sim, state = await _connected(
            {"channel_count": 2}, {"channel_count": 2})
        await driver.on_data_received(b"< REP 3 AUDIO_MUTE ON ")
        assert not state.get_namespace("device.ad4.channel.3")

    @pytest.mark.asyncio
    async def test_refresh_children_rereads_everything(self):
        driver, sim, state = await _connected()
        link = driver.transport
        link.sent.clear()
        result = await driver.refresh_children()
        assert result == {"channel": 4, "slot": 32}
        assert link.sent[0] == "< GET 0 ALL >"


# ── Commands round-trip through the receiver's own REP ──────────────────────

class TestChannelCommands:
    @pytest.mark.asyncio
    async def test_mute_set_and_toggle(self):
        driver, sim, state = await _connected()
        await driver.send_command("set_channel_mute", {"channel": 1, "mute": True})
        assert driver.transport.sent[-1] == "< SET 1 AUDIO_MUTE ON >"
        assert _ch(driver, 1)["mute"] is True
        await driver.send_command("toggle_channel_mute", {"channel": 1})
        assert _ch(driver, 1)["mute"] is False
        assert sim.state["ch1_mute"] is False

    @pytest.mark.asyncio
    async def test_gain_is_offset_by_18_on_the_wire(self):
        driver, sim, state = await _connected()
        await driver.send_command("set_channel_gain", {"channel": 1, "gain_db": 22})
        assert driver.transport.sent[-1] == "< SET 1 AUDIO_GAIN 40 >"
        assert _ch(driver, 1)["gain"] == 22
        await driver.send_command("step_channel_gain", {"channel": 1, "delta_db": 10})
        assert driver.transport.sent[-1] == "< SET 1 AUDIO_GAIN INC 10 >"
        assert _ch(driver, 1)["gain"] == 32
        await driver.send_command("step_channel_gain", {"channel": 1, "delta_db": -5})
        assert driver.transport.sent[-1] == "< SET 1 AUDIO_GAIN DEC 5 >"
        assert _ch(driver, 1)["gain"] == 27
        # The receiver clamps at its ends: -18 dB is wire 000.
        await driver.send_command("set_channel_gain", {"channel": 1, "gain_db": -18})
        assert _ch(driver, 1)["gain"] == -18

    @pytest.mark.asyncio
    async def test_name_is_braced_and_read_back_padded(self):
        driver, sim, state = await _connected()
        await driver.send_command("set_channel_name", {"channel": 2, "name": "Lead Vox"})
        assert driver.transport.sent[-1] == "< SET 2 CHAN_NAME {Lead Vox} >"
        assert _ch(driver, 2)["name"] == "Lead Vox"

    @pytest.mark.asyncio
    async def test_frequency_clears_the_preset_and_a_preset_sets_the_frequency(self):
        driver, sim, state = await _connected()
        await driver.send_command("set_channel_frequency",
                                  {"channel": 1, "frequency_khz": 620000})
        assert driver.transport.sent[-1] == "< SET 1 FREQUENCY 620000 >"
        ch = _ch(driver, 1)
        assert ch["frequency_khz"] == 620000
        assert (ch["preset_bank"], ch["preset_channel"]) == ("", "")
        await driver.send_command("set_channel_preset",
                                  {"channel": 1, "group": "6", "preset": "100"})
        assert driver.transport.sent[-1] == "< SET 1 GROUP_CHANNEL {6,100} >"
        ch = _ch(driver, 1)
        assert ch["frequency_khz"] == 652875
        assert (ch["preset_bank"], ch["preset_channel"]) == ("6", "100")

    @pytest.mark.asyncio
    async def test_fdc_second_carrier(self):
        driver, sim, state = await _connected()
        await driver.send_command("set_channel_frequency2",
                                  {"channel": 2, "frequency_khz": 602125})
        assert _ch(driver, 2)["frequency2_khz"] == 602125
        await driver.send_command("set_channel_preset2",
                                  {"channel": 2, "group": "6", "preset": "6"})
        ch = _ch(driver, 2)
        assert ch["frequency2_khz"] == 614650
        assert (ch["preset_bank2"], ch["preset_channel2"]) == ("6", "6")
        # A non-FD-C channel refuses the second carrier; state is untouched.
        await driver.send_command("set_channel_frequency2",
                                  {"channel": 1, "frequency_khz": 602125})
        assert _ch(driver, 1)["frequency2_khz"] is None

    @pytest.mark.asyncio
    async def test_identify_device_and_channel(self):
        driver, sim, state = await _connected()
        await driver.send_command("identify")
        assert driver.transport.sent[-1] == "< SET FLASH ON >"
        assert driver.get_state("identifying") is True
        await driver.send_command("identify_off")
        assert driver.get_state("identifying") is False
        await driver.send_command("identify_channel", {"channel": 3})
        assert driver.transport.sent[-1] == "< SET 3 FLASH ON >"
        assert _ch(driver, 3)["identifying"] is True
        await driver.send_command("identify_channel_off", {"channel": 3})
        assert _ch(driver, 3)["identifying"] is False

    @pytest.mark.asyncio
    async def test_device_name_setting_round_trips(self):
        driver, sim, state = await _connected()
        await driver.set_device_setting("device_name", "Rack 1")
        assert driver.transport.sent[-1] == "< SET DEVICE_ID {Rack 1} >"
        assert driver.get_state("device_name") == "Rack 1"
        with pytest.raises(ValueError):
            await driver.set_device_setting("nope", 1)

    @pytest.mark.asyncio
    async def test_raw_command_goes_out_verbatim(self):
        driver, sim, state = await _connected()
        await driver.send_command("raw_command", {"command": "< GET RF_BAND >"})
        assert driver.transport.sent[-1] == "< GET RF_BAND >"


class TestMeters:
    @pytest.mark.asyncio
    async def test_meters_on_parses_the_sample_and_off_stops(self):
        driver, sim, state = await _connected()
        await driver.send_command("channel_meters_on", {"channel": 1, "rate_ms": 250})
        assert driver.transport.sent[-1] == "< SET 1 METER_RATE 00250 >"
        ch = _ch(driver, 1)
        assert ch["metering"] is True and ch["meter_rate_ms"] == 250
        assert ch["level_dbfs"] == -22.0 and ch["level_peak_dbfs"] == -18.0
        assert ch["chan_quality"] == 5
        assert ch["antenna_status"] == "BB"
        assert ch["rssi_a_dbm"] == -34.0 and ch["rssi_b_dbm"] == -55.0
        assert ch["rssi_dbm"] == -34.0
        assert ch["rf_peak"] is False and ch["af_peak"] is False
        await driver.send_command("channel_meters_off", {"channel": 1})
        assert driver.transport.sent[-1] == "< SET 1 METER_RATE 00000 >"
        assert _ch(driver, 1)["metering"] is False

    @pytest.mark.asyncio
    async def test_meter_interval_config_arms_every_channel_on_connect(self):
        driver, sim, state = await _connected({"meter_interval_ms": 1000})
        sent = driver.transport.sent
        for n in range(1, 5):
            assert f"< SET {n} METER_RATE 01000 >" in sent
            assert _ch(driver, n)["metering"] is True
        for n in range(1, 5):
            await driver.send_command("channel_meters_off", {"channel": n})

    @pytest.mark.asyncio
    async def test_quadversity_sample_carries_four_antennas(self):
        driver, sim, state = await _connected()
        sim.set_state("quadversity", True)
        await _settle()
        assert driver.get_state("quadversity") is True
        await driver.on_data_received(
            b"< SAMPLE 1 ALL 005 031 102 102 BBBB 31 083 31 068 31 069 31 072 ")
        ch = _ch(driver, 1)
        assert ch["antenna_status"] == "BBBB"
        assert ch["rssi_c_dbm"] == -51.0 and ch["rssi_d_dbm"] == -48.0
        assert ch["rssi_dbm"] == -37.0

    @pytest.mark.asyncio
    async def test_fdc_sample_carries_two_rf_sections(self):
        driver, sim, state = await _connected()
        await driver.on_data_received(
            b"< SAMPLE 2 ALL 004 031 102 100 BB 31 082 31 060 BR 31 081 31 059 ")
        ch = _ch(driver, 2)
        assert ch["chan_quality"] == 4
        assert ch["antenna_status"] == "BB" and ch["antenna_status2"] == "BR"
        assert ch["rssi_a_dbm"] == -38.0 and ch["rssi_b_dbm"] == -60.0
        assert ch["rssi2_a_dbm"] == -39.0 and ch["rssi2_b_dbm"] == -61.0
        assert ch["rssi_dbm"] == -38.0

    @pytest.mark.asyncio
    async def test_overload_leds_become_peak_flags(self):
        driver, sim, state = await _connected()
        await driver.on_data_received(
            b"< SAMPLE 1 ALL 005 131 119 110 BB 63 115 31 065 ")
        ch = _ch(driver, 1)
        assert ch["af_peak"] is True
        assert ch["rf_peak"] is True
        assert ch["level_peak_dbfs"] == -1.0
        await driver.on_data_received(
            b"< SAMPLE 1 ALL 005 031 102 102 BB 31 086 31 065 ")
        ch = _ch(driver, 1)
        assert ch["af_peak"] is False and ch["rf_peak"] is False

    @pytest.mark.asyncio
    async def test_the_simulator_streams_at_the_meter_rate(self):
        driver, sim, state = await _connected()
        await driver.send_command("channel_meters_on", {"channel": 1, "rate_ms": 100})
        driver.set_child_state("channel", 1, "level_dbfs", None)
        await asyncio.sleep(0.25)
        assert _ch(driver, 1)["level_dbfs"] == -22.0
        await driver.send_command("channel_meters_off", {"channel": 1})
        await asyncio.sleep(0.15)
        assert not sim._meter_tasks


class TestSlotCommands:
    @pytest.mark.asyncio
    async def test_rf_mute_power_offset_polarity_pad_and_name_on_an_active_slot(self):
        driver, sim, state = await _connected()
        await driver.send_command("set_slot_rf_mute", {"slot": "1-2", "muted": True})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_RF_OUTPUT 2 RF_MUTE >"
        assert _slot(driver, "1-2")["rf_muted"] is True
        await driver.send_command("set_slot_rf_mute", {"slot": "1-2", "muted": False})
        assert _slot(driver, "1-2")["rf_muted"] is False

        await driver.send_command("set_slot_rf_power", {"slot": "1-2", "mode": "high"})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_RF_POWER_MODE 2 HIGH >"
        s = _slot(driver, "1-2")
        assert s["rf_power_mode"] == "high" and s["rf_power_mw"] == 40

        await driver.send_command("set_slot_offset", {"slot": "1-2", "offset_db": 5})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_OFFSET 2 17 >"
        assert _slot(driver, "1-2")["offset_db"] == 5
        await driver.send_command("step_slot_offset", {"slot": "1-2", "delta_db": -2})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_OFFSET 2 DEC 2 >"
        assert _slot(driver, "1-2")["offset_db"] == 3
        await driver.send_command("set_slot_offset", {"slot": "1-2", "offset_db": -12})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_OFFSET 2 0 >"
        assert _slot(driver, "1-2")["offset_db"] == -12

        await driver.send_command("set_slot_polarity", {"slot": "1-2", "polarity": "negative"})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_POLARITY 2 NEGATIVE >"
        assert _slot(driver, "1-2")["polarity"] == "negative"

        await driver.send_command("set_slot_input_pad", {"slot": "1-2", "enabled": True})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_INPUT_PAD 2 0 >"
        assert _slot(driver, "1-2")["input_pad"] is True
        await driver.send_command("set_slot_input_pad", {"slot": "1-2", "enabled": False})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_INPUT_PAD 2 12 >"
        assert _slot(driver, "1-2")["input_pad"] is False

        await driver.send_command("set_slot_tx_name", {"slot": "1-2", "name": "Vox 2"})
        assert driver.transport.sent[-1] == "< SET 1 SLOT_TX_DEVICE_ID 2 {Vox 2} >"
        assert _slot(driver, "1-2")["tx_name"] == "Vox 2"

    @pytest.mark.asyncio
    async def test_a_slot_that_is_not_linked_active_refuses_and_keeps_state(self):
        driver, sim, state = await _connected()
        for sid in ("1-1", "1-3", "1-4"):
            before = _slot(driver, sid)
            await driver.send_command("set_slot_rf_mute", {"slot": sid, "muted": True})
            await driver.send_command("set_slot_offset", {"slot": sid, "offset_db": 3})
            assert _slot(driver, sid) == before
        # The simulator said so on the wire, the way the receiver does.
        assert sim.handle_command(b"< SET 1 SLOT_RF_OUTPUT 1 RF_MUTE >") == b"< REP ERR >"


# ── Push: the receiver reports what changed elsewhere ───────────────────────

class TestPush:
    @pytest.mark.asyncio
    async def test_a_transmitter_going_out_of_range_clears_its_telemetry(self):
        driver, sim, state = await _connected()
        sim.set_state("ch1_tx_linked", False)
        await _settle()
        ch = _ch(driver, 1)
        assert ch["tx_linked"] is False and ch["no_link"] is True
        assert ch["tx_model"] == "" and ch["tx_name"] == ""
        assert ch["tx_battery_percent"] is None
        assert ch["tx_battery_minutes"] is None
        assert ch["tx_battery_state"] == "unknown"
        assert ch["tx_talk_switch"] is None
        sim.set_state("ch1_tx_linked", True)
        await _settle()
        ch = _ch(driver, 1)
        assert ch["tx_linked"] is True and ch["tx_model"] == "AD2"

    @pytest.mark.asyncio
    async def test_warnings_and_switches_push(self):
        driver, sim, state = await _connected()
        sim.set_state("ch1_interference", True)
        sim.set_state("ch1_encryption_error", True)
        sim.set_state("ch1_tx_mute", True)
        sim.set_state("ch1_talk_switch", True)
        sim.set_state("encryption", True)
        await _settle()
        ch = _ch(driver, 1)
        assert ch["interference"] is True
        assert ch["aes256_error"] is True
        assert ch["tx_mute"] is True
        assert ch["tx_talk_switch"] is True
        assert driver.get_state("encryption_enabled") is True

    @pytest.mark.asyncio
    async def test_low_battery_follows_the_bars_threshold(self):
        driver, sim, state = await _connected()
        sim.set_state("ch1_battery_bars", 1)
        await _settle()
        assert _ch(driver, 1)["tx_battery_bars"] == 1
        assert _ch(driver, 1)["tx_low_battery"] is True
        sim.set_state("ch1_battery_bars", 2)
        await _settle()
        assert _ch(driver, 1)["tx_low_battery"] is False
        sim.set_state("ch1_battery_minutes", 30)
        sim.set_state("ch1_battery_percent", 12)
        await _settle()
        ch = _ch(driver, 1)
        assert ch["tx_battery_minutes"] == 30 and ch["tx_battery_percent"] == 12

    @pytest.mark.asyncio
    async def test_a_zero_threshold_never_raises_low_battery(self):
        driver, sim, state = await _connected({"low_battery_bars": 0})
        sim.set_state("ch1_battery_bars", 0)
        await _settle()
        assert _ch(driver, 1)["tx_battery_bars"] == 0
        assert _ch(driver, 1)["tx_low_battery"] is False

    @pytest.mark.asyncio
    async def test_a_slot_emptied_and_refilled(self):
        driver, sim, state = await _connected()
        sim.set_state("slot_1-2_status", "EMPTY")
        await _settle()
        s = _slot(driver, "1-2")
        assert s["status"] == "empty" and s["offline_reason"] == "not_fitted"
        assert s["battery_percent"] is None and s["rf_power_mode"] is None
        sim.set_state("slot_1-2_status", "LINKED.INACTIVE")
        await _settle()
        s = _slot(driver, "1-2")
        assert s["status"] == "linked_inactive"
        assert s["online"] is False and s["offline_reason"] == "not_responding"
        sim.set_state("slot_1-2_status", "LINKED.ACTIVE")
        await _settle()
        s = _slot(driver, "1-2")
        assert s["status"] == "linked_active"
        assert s["online"] is True and s["offline_reason"] is None

    @pytest.mark.asyncio
    async def test_battery_sentinels_from_the_wire(self):
        driver, sim, state = await _connected()
        await driver.on_data_received(b"< REP 1 TX_BATT_MINS 65534 ")
        ch = _ch(driver, 1)
        assert ch["tx_battery_minutes"] is None
        assert ch["tx_battery_state"] == "calculating"
        await driver.on_data_received(b"< REP 1 TX_BATT_MINS 65533 ")
        assert _ch(driver, 1)["tx_battery_state"] == "communication_warning"
        await driver.on_data_received(b"< REP 1 TX_BATT_CHARGE_PERCENT 255 ")
        assert _ch(driver, 1)["tx_battery_percent"] is None
        await driver.on_data_received(b"< REP 1 TX_BATT_TEMP_C 062 ")
        assert _ch(driver, 1)["tx_battery_temp_c"] == 22
        await driver.on_data_received(b"< REP FW_VER {2.0.15.2*               } ")
        assert driver.get_state("selftest_failed") is True
        assert driver.get_state("firmware") == "2.0.15.2"

    @pytest.mark.asyncio
    async def test_rep_err_is_ignored(self):
        driver, sim, state = await _connected()
        before = _ch(driver, 1)
        await driver.on_data_received(b"< REP ERR ")
        assert _ch(driver, 1) == before


# ── Liveness ────────────────────────────────────────────────────────────────

class TestLiveness:
    @pytest.mark.asyncio
    async def test_the_probe_is_answered_by_model_only(self):
        driver, sim, state = await _connected()
        assert driver._health_enabled()
        await asyncio.wait_for(driver._liveness_probe(), 1.0)
        assert driver.transport.sent[-1] == "< GET MODEL >"
        # A silent link: a channel push arrives, but no MODEL. The probe
        # must not be satisfied by it.
        driver.transport.silent = True
        probe = asyncio.ensure_future(driver._liveness_probe())
        await _settle()
        await driver.on_data_received(b"< REP 1 AUDIO_MUTE ON ")
        await _settle()
        assert not probe.done()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(probe, 0.05)
        assert driver._probe_fut is None

    @pytest.mark.asyncio
    async def test_a_dead_link_forces_a_typed_no_response(self):
        driver, sim, state = await _connected()
        driver.HEALTH_INTERVAL_S = 0.01
        driver.HEALTH_TIMEOUT_S = 0.02
        driver.HEALTH_MAX_FAILURES = 2
        driver.transport.silent = True
        driver._start_health_loop()
        await asyncio.sleep(0.2)
        assert driver.stashed_fault is not None
        assert driver.stashed_fault[0] == "no_response"
        assert driver.get_state("connected") is False

    @pytest.mark.asyncio
    async def test_disconnect_unblocks_a_waiting_probe(self):
        driver, sim, state = await _connected()
        driver.transport.silent = True
        probe = asyncio.ensure_future(driver._liveness_probe())
        await _settle()
        await driver.disconnect()
        with pytest.raises(ConnectionError):
            await probe


# ── Command surface ─────────────────────────────────────────────────────────

class TestCommandSurface:
    def test_every_declared_command_has_a_branch(self):
        source = DRIVER_PATH.read_text()
        for name in Driver.DRIVER_INFO["commands"]:
            assert re.search(rf'command == "{name}"', source), name

    @pytest.mark.asyncio
    async def test_an_unknown_command_sends_nothing(self):
        driver, sim, state = await _connected()
        before = len(driver.transport.sent)
        await driver.send_command("no_such_command", {})
        assert len(driver.transport.sent) == before

    def test_child_id_params_name_declared_types(self):
        types = Driver.DRIVER_INFO["child_entity_types"]
        for cname, cmd in Driver.DRIVER_INFO["commands"].items():
            for pname, p in cmd.get("params", {}).items():
                if p.get("type") == "child_id":
                    assert p["child_type"] in types, (cname, pname)

    def test_slot_ids_cover_the_full_roster(self):
        ids = Driver.DRIVER_INFO["child_entity_types"]["slot"]["instances"]["ids"]
        assert ids == [f"{c}-{s}" for c in range(1, 5) for s in range(1, 9)]
        assert Driver.DRIVER_INFO["child_entity_types"]["slot"]["instances"]["presence"] == "reported"
