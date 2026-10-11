"""crestron_1beyond_camera: Crestron 1 Beyond IV-CAM cameras over VISCA.

No camera is on hand, so the driver is proven against Crestron's documents
two ways. The packet tables below are copied from the manual's VISCA,
Lightbar and Intelligent Switching pages and compared byte for byte with
what the driver sends. Everything else runs the real driver against the real
simulator over an in-memory connection framed the way the platform frames it
(on the trailing FF, which the parser strips): poll round trips per model,
the reserved presets, both readings of Privacy Mode, the switching host, the
narrowing by model, and the camera's error replies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubBaseDriver,
    StubEvents,
    StubState,
    install_stubs,
    load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Transport:
    """One camera connection: what the driver sends goes to the simulator,
    and its reply comes back split on FF, as the platform's parser does."""

    def __init__(self, driver, sim):
        self.driver = driver
        self.sim = sim
        self.connected = True
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        if not self.connected:
            raise ConnectionError("transport closed")
        self.sent.append(bytes(data))
        reply = self.sim.handle_command(bytes(data))
        if reply:
            for frame in reply.split(b"\xff"):
                if frame:
                    await self.driver.on_data_received(frame)

    async def close(self) -> None:
        self.connected = False


class _FakeBaseDriver(StubBaseDriver, LifecycleFake):
    sim = None

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._health_task = None
        self._health_failures = 0

    async def connect(self) -> None:
        self.transport = _Transport(self, self.sim)
        self._connected = True
        self.set_state("connected", True)
        await self._initial_sync()

    async def disconnect(self) -> None:
        if self.transport:
            await self.transport.close()
        await self._close_session()
        self.set_state("connected", False)

    def _handle_transport_disconnect(self) -> None:
        if self.transport is not None:
            self.transport.connected = False


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("crestron_1beyond_camera_under_test", REPO_ROOT / "cameras" / "crestron_1beyond_camera.py")
SIM = load_module("crestron_1beyond_camera_sim_under_test", REPO_ROOT / "cameras" / "crestron_1beyond_camera_sim.py")

INFO = DRV.CrestronOneBeyondCameraDriver.DRIVER_INFO


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch):
    monkeypatch.setattr(DRV, "REPLY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(DRV, "OPTIONAL_REPLY_TIMEOUT_S", 0.02)


def make(model="i12", sim_model=None, **config):
    cfg = {"host": "10.0.0.20", "port": 5500, "model": model}
    cfg.update(config)
    sim_cfg = {"model": sim_model or model, "camera_address": cfg.get("camera_address", 1)}
    sim = SIM.CrestronOneBeyondCameraSimulator("cam", sim_cfg)
    driver = DRV.CrestronOneBeyondCameraDriver("cam", cfg, StubState(), StubEvents())
    driver.sim = sim
    return driver, sim


async def connected(model="i12", sim_model=None, **config):
    driver, sim = make(model, sim_model, **config)
    await driver.connect()
    driver.transport.sent.clear()
    return driver, sim


def h(text: str) -> bytes:
    return bytes.fromhex(text)


def state(driver, key):
    return driver.get_state(key)


# --------------------------------------------------------------------------
# Packets, byte for byte against the manual (camera address 1)
# --------------------------------------------------------------------------

PACKETS = [
    ("power_on", {}, "81 01 04 00 02 FF"),
    ("power_off", {}, "81 01 04 00 03 FF"),
    ("pt_up", {}, "81 01 06 01 0C 0A 03 01 FF"),
    ("pt_down", {"pan_speed": 1, "tilt_speed": 20}, "81 01 06 01 01 14 03 02 FF"),
    ("pt_left", {"pan_speed": 24}, "81 01 06 01 18 0A 01 03 FF"),
    ("pt_right", {}, "81 01 06 01 0C 0A 02 03 FF"),
    ("pt_up_left", {}, "81 01 06 01 0C 0A 01 01 FF"),
    ("pt_up_right", {}, "81 01 06 01 0C 0A 02 01 FF"),
    ("pt_down_left", {}, "81 01 06 01 0C 0A 01 02 FF"),
    ("pt_down_right", {}, "81 01 06 01 0C 0A 02 02 FF"),
    ("pt_stop", {}, "81 01 06 01 0C 0A 03 03 FF"),
    ("pt_home", {}, "81 01 06 04 FF"),
    ("pt_reset", {}, "81 01 06 05 FF"),
    ("pt_absolute", {"pan": 256, "tilt": -1}, "81 01 06 02 0C 0A 00 01 00 00 0F 0F 0F 0F FF"),
    ("pt_relative", {"pan": -144, "tilt": 72}, "81 01 06 03 0C 0A 0F 0F 07 00 00 00 04 08 FF"),
    ("set_pan_tilt_limit", {"corner": "up_right", "pan": 100, "tilt": 50},
     "81 01 06 07 00 01 00 00 06 04 00 00 03 02 FF"),
    ("set_pan_tilt_limit", {"corner": "down_left", "pan": -100, "tilt": -50},
     "81 01 06 07 00 00 0F 0F 09 0C 0F 0F 0C 0E FF"),
    ("zoom_in", {}, "81 01 04 07 02 FF"),
    ("zoom_out", {}, "81 01 04 07 03 FF"),
    ("zoom_in", {"speed": 5}, "81 01 04 07 25 FF"),
    ("zoom_out", {"speed": 7}, "81 01 04 07 37 FF"),
    ("zoom_stop", {}, "81 01 04 07 00 FF"),
    ("zoom_direct", {"position": 0x4000}, "81 01 04 47 04 00 00 00 FF"),
    ("zoom_focus_direct", {"zoom": 0x1982, "focus": 0x1234},
     "81 01 04 47 01 09 08 02 01 02 03 04 FF"),
    ("focus_far", {}, "81 01 04 08 02 FF"),
    ("focus_near", {}, "81 01 04 08 03 FF"),
    ("focus_far", {"speed": 0}, "81 01 04 08 20 FF"),
    ("focus_near", {"speed": 3}, "81 01 04 08 33 FF"),
    ("focus_stop", {}, "81 01 04 08 00 FF"),
    ("focus_direct", {"position": 0xABCD}, "81 01 04 48 0A 0B 0C 0D FF"),
    ("focus_auto", {}, "81 01 04 38 02 FF"),
    ("focus_manual", {}, "81 01 04 38 03 FF"),
    ("focus_one_push", {}, "81 01 04 18 01 FF"),
    ("set_wb_mode", {"mode": "auto"}, "81 01 04 35 00 FF"),
    ("set_wb_mode", {"mode": "indoor"}, "81 01 04 35 01 FF"),
    ("set_wb_mode", {"mode": "outdoor"}, "81 01 04 35 02 FF"),
    ("set_wb_mode", {"mode": "one_push"}, "81 01 04 35 03 FF"),
    ("set_wb_mode", {"mode": "manual"}, "81 01 04 35 05 FF"),
    ("wb_one_push_trigger", {}, "81 01 04 10 05 FF"),
    ("r_gain_reset", {}, "81 01 04 03 00 FF"),
    ("r_gain_up", {}, "81 01 04 03 02 FF"),
    ("r_gain_down", {}, "81 01 04 03 03 FF"),
    ("set_r_gain", {"value": 0xA5}, "81 01 04 43 00 00 0A 05 FF"),
    ("b_gain_reset", {}, "81 01 04 04 00 FF"),
    ("b_gain_up", {}, "81 01 04 04 02 FF"),
    ("b_gain_down", {}, "81 01 04 04 03 FF"),
    ("set_b_gain", {"value": 0x5A}, "81 01 04 44 00 00 05 0A FF"),
    ("set_ae_mode", {"mode": "full_auto"}, "81 01 04 39 00 FF"),
    ("set_ae_mode", {"mode": "manual"}, "81 01 04 39 03 FF"),
    ("set_ae_mode", {"mode": "shutter"}, "81 01 04 39 0A FF"),
    ("set_ae_mode", {"mode": "iris"}, "81 01 04 39 0B FF"),
    ("set_ae_mode", {"mode": "bright"}, "81 01 04 39 0D FF"),
    ("shutter_reset", {}, "81 01 04 0A 00 FF"),
    ("shutter_up", {}, "81 01 04 0A 02 FF"),
    ("shutter_down", {}, "81 01 04 0A 03 FF"),
    ("set_shutter", {"value": 0x12}, "81 01 04 4A 00 00 01 02 FF"),
    ("iris_reset", {}, "81 01 04 0B 00 FF"),
    ("iris_up", {}, "81 01 04 0B 02 FF"),
    ("iris_down", {}, "81 01 04 0B 03 FF"),
    ("set_iris", {"value": 0x0C}, "81 01 04 4B 00 00 00 0C FF"),
    ("gain_reset", {}, "81 01 04 0C 00 FF"),
    ("gain_up", {}, "81 01 04 0C 02 FF"),
    ("gain_down", {}, "81 01 04 0C 03 FF"),
    ("set_gain", {"value": 0x0E}, "81 01 04 4C 00 00 00 0E FF"),
    ("bright_reset", {}, "81 01 04 0D 00 FF"),
    ("bright_up", {}, "81 01 04 0D 02 FF"),
    ("bright_down", {}, "81 01 04 0D 03 FF"),
    ("set_bright", {"value": 0x1F}, "81 01 04 4D 00 00 01 0F FF"),
    ("set_exp_comp", {"enabled": True}, "81 01 04 3E 02 FF"),
    ("set_exp_comp", {"enabled": False}, "81 01 04 3E 03 FF"),
    ("exp_comp_reset", {}, "81 01 04 0E 00 FF"),
    ("exp_comp_up", {}, "81 01 04 0E 02 FF"),
    ("exp_comp_down", {}, "81 01 04 0E 03 FF"),
    ("set_exp_comp_level", {"level": 7}, "81 01 04 4E 00 00 00 0E FF"),
    ("set_exp_comp_level", {"level": 0}, "81 01 04 4E 00 00 00 07 FF"),
    ("set_exp_comp_level", {"level": -7}, "81 01 04 4E 00 00 00 00 FF"),
    ("set_backlight", {"enabled": True}, "81 01 04 33 02 FF"),
    ("set_backlight", {"enabled": False}, "81 01 04 33 03 FF"),
    ("recall_preset", {"number": 10}, "81 01 04 3F 02 0A FF"),
    ("save_preset", {"number": 254}, "81 01 04 3F 01 FE FF"),
    ("delete_preset", {"number": 10}, "81 01 04 3F 00 0A FF"),
    ("set_freeze", {"enabled": True}, "81 01 04 62 02 FF"),
    ("set_freeze", {"enabled": False}, "81 01 04 62 03 FF"),
    ("set_preset_freeze", {"enabled": True}, "81 01 04 62 22 FF"),
    ("set_preset_freeze", {"enabled": False}, "81 01 04 62 23 FF"),
    # Reserved presets: "convert the camera preset value to hexadecimal".
    # 80 / 81 also match the 2024 edition's own packets (3F 02 50 / 51).
    ("start_tracking", {}, "81 01 04 3F 02 50 FF"),
    ("pause_tracking", {}, "81 01 04 3F 02 51 FF"),
    ("recall_home_shot", {}, "81 01 04 3F 02 00 FF"),
    ("set_home_shot", {}, "81 01 04 3F 01 00 FF"),
    ("set_tracking_shot", {}, "81 01 04 3F 01 01 FF"),
    ("toggle_osd_menu", {}, "81 01 04 3F 02 5F FF"),
    ("set_ir_receiver", {"enabled": True}, "81 01 06 08 02 FF"),
    ("set_ir_receiver", {"enabled": False}, "81 01 06 08 03 FF"),
    ("set_lightbar", {"width": "full", "color": "green", "brightness": "bright"}, "81 C1 0C 0C 0C 0C FF"),
    ("set_lightbar_segments",
     {"segment_1": "off", "segment_2": "green_bright", "segment_3": "green_bright", "segment_4": "off"},
     "81 C1 00 0C 0C 00 FF"),
    ("clear_interface", {}, "88 01 00 01 FF"),
]


@pytest.mark.parametrize("command,params,expected", PACKETS)
def test_each_packet_matches_the_manual(command, params, expected):
    async def run():
        driver, sim = await connected("i12")
        if command in ("focus_far", "focus_near", "focus_direct"):
            sim.set_state("focus_mode", "manual")
        await driver.send_command(command, params)
        assert driver.transport.sent[-1] == h(expected)
    asyncio.run(run())


MODEL_PACKETS = [
    ("i20", "start_group_tracking", {}, "81 01 04 3F 02 52 FF"),
    ("i20", "pause_group_tracking", {}, "81 01 04 3F 02 53 FF"),
    ("i20", "recall_preset_zone", {"zone": 1}, "81 01 04 3F 02 65 FF"),
    ("i20", "recall_preset_zone", {"zone": 4}, "81 01 04 3F 02 68 FF"),
    ("i20", "select_tracking_profile", {"profile": 1}, "81 01 04 3F 02 69 FF"),
    ("i20", "select_tracking_profile", {"profile": 4}, "81 01 04 3F 02 6C FF"),
    ("i12d", "enable_group_framing", {}, "81 01 04 3F 02 57 FF"),
    ("i12d", "enable_speaker_tracking", {}, "81 01 04 3F 02 59 FF"),
    ("p12", "set_mount_mode", {"mode": "stand"}, "81 01 04 A4 02 FF"),
    ("p20", "set_mount_mode", {"mode": "ceiling"}, "81 01 04 A4 03 FF"),
]


@pytest.mark.parametrize("model,command,params,expected", MODEL_PACKETS)
def test_model_packets_match_the_manual(model, command, params, expected):
    async def run():
        driver, _ = await connected(model)
        await driver.send_command(command, params)
        assert driver.transport.sent[-1] == h(expected)
    asyncio.run(run())


SWITCHING_PACKETS = [
    ("set_switching_camera", {"camera": 2, "ip": "192.168.1.10"},
     "81 C2 01 09 02 0C 00 0A 08 00 01 00 0A FF"),
    ("clear_switching_cameras", {}, "81 C2 01 0A 00 FF"),
    ("switch_to_camera", {"camera": 3}, "81 C2 01 08 03 FF"),
    ("resume_switching", {}, "81 C2 01 08 00 FF"),
    ("pause_switching", {}, "81 C2 01 0B 00 FF"),
    ("show_group_framing_feed", {}, "81 01 04 3F 02 55 FF"),
    ("show_presenter_feed", {}, "81 01 04 3F 02 56 FF"),
]


@pytest.mark.parametrize("command,params,expected", SWITCHING_PACKETS)
def test_switching_packets_match_the_manual(command, params, expected):
    async def run():
        driver, _ = await connected("i12", switching_host=True)
        await driver.send_command(command, params)
        assert driver.transport.sent[-1] == h(expected)
    asyncio.run(run())


def test_reboot_is_preset_99_and_opens_a_restart_window():
    async def run():
        driver, sim = await connected("p20")
        await driver.send_command("reboot")
        assert driver.transport.sent[-1] == h("81 01 04 3F 02 63 FF")
        assert sim.get_state("reboots") == 1
    asyncio.run(run())
    assert INFO["commands"]["reboot"]["restarts_device_for"] == 90


def test_the_camera_address_is_in_every_packet_and_in_the_replies_read():
    async def run():
        driver, sim = await connected("i12", camera_address=3)
        await driver.send_command("power_on")
        assert driver.transport.sent[-1] == h("83 01 04 00 02 FF")
        await driver.poll()
        assert state(driver, "power") == "on"
        # A reply from another address on the chain is not this camera's.
        await driver.on_data_received(h("90 50 03"))
        assert state(driver, "power") == "on"
    asyncio.run(run())


def test_a_camera_at_another_address_answers_nothing_and_the_command_says_so():
    async def run():
        driver, sim = make("i12", camera_address=2)
        sim._address, sim._head = 1, 0x90
        await driver.connect()
        with pytest.raises(RuntimeError, match="Camera Address"):
            await driver.send_command("pt_home")
    asyncio.run(run())


# --------------------------------------------------------------------------
# Lightbar: the manual's table of common commands
# --------------------------------------------------------------------------

LIGHTBAR_ROWS = [
    ("off", "green", "bright", "00 00 00 00"),
    ("full", "green", "bright", "0C 0C 0C 0C"),
    ("full", "green", "medium", "08 08 08 08"),
    ("full", "green", "dim", "04 04 04 04"),
    ("full", "yellow", "bright", "0F 0F 0F 0F"),
    ("full", "yellow", "medium", "0B 0B 0B 0B"),
    ("full", "yellow", "dim", "07 07 07 07"),
    ("full", "red", "bright", "0D 0D 0D 0D"),
    ("full", "red", "medium", "09 09 09 09"),
    ("full", "red", "dim", "05 05 05 05"),
    ("half", "green", "bright", "00 0C 0C 00"),
    ("half", "green", "medium", "00 08 08 00"),
    ("half", "green", "dim", "00 04 04 00"),
    ("half", "yellow", "bright", "03 0F 0F 03"),
    ("half", "yellow", "medium", "03 0B 0B 03"),
    ("half", "yellow", "dim", "03 07 07 03"),
    ("half", "red", "bright", "01 0D 0D 01"),
    ("half", "red", "medium", "01 09 09 01"),
    ("half", "red", "dim", "01 05 05 01"),
]


@pytest.mark.parametrize("width,color,level,expected", LIGHTBAR_ROWS)
def test_lightbar_rows_match_the_manuals_table(width, color, level, expected):
    assert DRV.lightbar_bytes(width, color, level) == h(expected)


def test_the_default_status_colours_are_rows_of_the_same_table():
    # Full green (tracking), half green (live), full yellow (update), half red (privacy).
    assert DRV.lightbar_bytes("full", "green", "bright") == h("0C 0C 0C 0C")
    assert DRV.lightbar_bytes("half", "green", "bright") == h("00 0C 0C 00")
    assert DRV.lightbar_bytes("full", "yellow", "bright") == h("0F 0F 0F 0F")
    assert DRV.lightbar_bytes("half", "red", "bright") == h("01 0D 0D 01")


def test_a_custom_segment_choice_is_brightness_then_colour():
    assert DRV.segment_from_choice("green_bright") == 0x0C  # 11 00
    assert DRV.segment_from_choice("red_dim") == 0x05       # 01 01
    assert DRV.segment_from_choice("yellow_medium") == 0x0B  # 10 11
    assert DRV.segment_from_choice("off") == 0x00
    with pytest.raises(ValueError):
        DRV.segment_from_choice("blue_bright")


def test_a_lightbar_command_needs_no_reply():
    async def run():
        driver, sim = await connected("i12")
        sim.inject_error("no_response")
        await driver.send_command("set_lightbar", {"width": "half", "color": "red", "brightness": "dim"})
        assert driver.transport.sent[-1] == h("81 C1 01 05 05 01 FF")
    asyncio.run(run())


# --------------------------------------------------------------------------
# Intelligent Switching addresses, zoom tables, exposure compensation
# --------------------------------------------------------------------------

def test_an_ip_address_is_eight_hex_digits_high_first():
    assert DRV.encode_ip("192.168.1.10") == h("0C 00 0A 08 00 01 00 0A")
    assert DRV.decode_ip(h("0C 00 0A 08 00 01 00 0A")) == "192.168.1.10"
    assert DRV.encode_ip(" 10.0.0.255 ") == h("00 0A 00 00 00 00 0F 0F")
    with pytest.raises(ValueError):
        DRV.encode_ip("10.0.0.256")


@pytest.mark.parametrize("lens", ["12x", "20x"])
def test_every_row_of_the_zoom_table_reads_back_as_its_ratio(lens):
    for ratio, position in DRV._ZOOM_TABLES[lens]:
        assert DRV.zoom_ratio_for(position, lens) == float(ratio)
        assert DRV.zoom_position_for(ratio, lens) == position


def test_zoom_ratio_between_table_rows_and_past_the_ends():
    assert DRV.zoom_ratio_for((0x1982 + 0x24E2) // 2, "12x") == 2.5
    assert DRV.zoom_ratio_for(0x5000, "12x") == 12.0
    assert DRV.zoom_position_for(30, "20x") == 0x4000
    assert DRV.zoom_position_for(0.5, "20x") == 0x0000


def test_zoom_to_ratio_uses_the_models_lens_and_is_bounded_by_it():
    async def run():
        driver, _ = await connected("i20")
        await driver.send_command("zoom_to_ratio", {"ratio": 10})
        assert driver.transport.sent[-1] == h("81 01 04 47 03 08 0B 03 FF")  # 0x38B3
        assert driver.DRIVER_INFO["commands"]["zoom_to_ratio"]["params"]["ratio"]["max"] == 20
        p12, _ = await connected("p12")
        await p12.send_command("zoom_to_ratio", {"ratio": 10})
        assert p12.transport.sent[-1] == h("81 01 04 47 03 0D 04 03 FF")  # 0x3D43
        assert p12.DRIVER_INFO["commands"]["zoom_to_ratio"]["params"]["ratio"]["max"] == 12
    asyncio.run(run())


def test_exposure_compensation_reads_00_to_0E_as_minus_7_to_plus_7():
    async def run():
        driver, sim = await connected("i12")
        for code, level in ((0x00, -7), (0x07, 0), (0x0E, 7)):
            sim.set_state("exp_comp_code", code)
            await driver.poll()
            assert state(driver, "exp_comp_level") == level
    asyncio.run(run())


# --------------------------------------------------------------------------
# Poll round trips
# --------------------------------------------------------------------------

def test_an_intelligent_camera_polls_into_every_reading():
    async def run():
        driver, sim = await connected("i12")
        for key, value in {
            "pan_position": 144, "tilt_position": -72, "zoom_position": 0x2BC9,
            "focus_position": 0x1234, "focus_mode": "manual", "ae_mode": "iris",
            "wb_mode": "outdoor", "r_gain": 0x91, "b_gain": 0x22, "shutter": 0x15,
            "iris": 0x0A, "gain": 0x03, "bright": 0x09, "exp_comp": True,
            "backlight": True, "last_preset": 12, "tracking": "active",
            "ir_receiver": False, "video_format": "720p50",
        }.items():
            sim.set_state(key, value)
        await driver.poll()
        assert state(driver, "power") == "on"
        assert state(driver, "pan_position") == 144
        assert state(driver, "pan_angle") == 10.0
        assert state(driver, "tilt_position") == -72
        assert state(driver, "tilt_angle") == -5.0
        assert state(driver, "zoom_position") == 0x2BC9
        assert state(driver, "zoom_ratio") == 4.0
        assert state(driver, "focus_position") == 0x1234
        assert state(driver, "focus_mode") == "manual"
        assert state(driver, "ae_mode") == "iris"
        assert state(driver, "wb_mode") == "outdoor"
        assert state(driver, "r_gain") == 0x91
        assert state(driver, "b_gain") == 0x22
        assert state(driver, "shutter_position") == 0x15
        assert state(driver, "iris_position") == 0x0A
        assert state(driver, "gain_position") == 0x03
        assert state(driver, "bright_position") == 0x09
        assert state(driver, "exp_comp") is True
        assert state(driver, "backlight") is True
        assert state(driver, "last_preset") == 12
        assert state(driver, "tracking") == "active"
        assert state(driver, "ir_receiver") is False
        assert state(driver, "video_format") == "720p50"
    asyncio.run(run())


def test_connect_reads_the_version_and_max_speeds_once():
    async def run():
        driver, _ = await connected("i20")
        assert state(driver, "model_code") == "0002"
        assert state(driver, "firmware_version") == "0100"
        assert state(driver, "pan_max_speed") == 0x18
        assert state(driver, "tilt_max_speed") == 0x14
    asyncio.run(run())


def test_a_ptz_camera_reads_mount_mode_and_never_asks_about_tracking():
    async def run():
        driver, sim = await connected("p20")
        sim.set_state("mount_mode", "ceiling")
        await driver.poll()
        assert state(driver, "mount_mode") == "ceiling"
        assert state(driver, "tracking") is None
        assert h("81 09 08 01 FF") not in driver.transport.sent
        assert h("81 09 06 08 FF") not in driver.transport.sent
    asyncio.run(run())


def test_the_i12d_reports_no_zoom_ratio_and_offers_no_ratio_command():
    async def run():
        driver, sim = await connected("i12d")
        sim.set_state("zoom_position", 0x4000)
        await driver.poll()
        assert state(driver, "zoom_position") == 0x4000
        assert state(driver, "zoom_ratio") is None
        assert "zoom_to_ratio" not in driver.DRIVER_INFO["commands"]
    asyncio.run(run())


def test_an_inquiry_refused_as_a_syntax_error_is_not_asked_again():
    async def run():
        # The device is set to I12 but the camera is a P12, which refuses the
        # tracking and IR inquiries.
        driver, _ = await connected("i12", sim_model="p12")
        await driver.poll()
        assert {"tracking", "ir_receiver"} <= driver._unsupported
        driver.transport.sent.clear()
        await driver.poll()
        assert h("81 09 08 01 FF") not in driver.transport.sent
        assert state(driver, "zoom_position") == 0
    asyncio.run(run())


def test_a_poll_stops_at_the_first_inquiry_nothing_answers():
    async def run():
        driver, sim = await connected("i12")
        real = sim.handle_command
        sim.handle_command = lambda data: None if data == h("81 09 06 12 FF") else real(data)
        driver.transport.sent.clear()
        await driver.poll()
        assert driver.transport.sent[-1] == h("81 09 06 12 FF")
    asyncio.run(run())


def test_commands_read_back_through_the_poll():
    async def run():
        driver, sim = await connected("i12")
        await driver.send_command("set_ae_mode", {"mode": "shutter"})
        await driver.send_command("set_shutter", {"value": 0x20})
        await driver.send_command("pause_tracking")
        await driver.send_command("start_tracking")
        await driver.send_command("pt_absolute", {"pan": 720, "tilt": 288})
        await driver.send_command("recall_preset", {"number": 7})
        await driver.poll()
        assert state(driver, "ae_mode") == "shutter"
        assert state(driver, "shutter_position") == 0x20
        assert state(driver, "tracking") == "active"
        assert state(driver, "pan_angle") == 50.0
        assert state(driver, "tilt_angle") == 20.0
        assert state(driver, "last_preset") == 7
    asyncio.run(run())


def test_a_saved_preset_is_recalled_by_the_simulator():
    async def run():
        driver, sim = await connected("p12")
        await driver.send_command("pt_absolute", {"pan": 500, "tilt": 100})
        await driver.send_command("save_preset", {"number": 20})
        await driver.send_command("pt_home")
        await driver.send_command("recall_preset", {"number": 20})
        await driver.poll()
        assert state(driver, "pan_position") == 500
        assert state(driver, "tilt_position") == 100
    asyncio.run(run())


# --------------------------------------------------------------------------
# Reserved presets
# --------------------------------------------------------------------------

def test_preset_255_is_out_of_range_because_FF_ends_a_packet():
    # The manual says 0 to 255, but 0xFF is the VISCA terminator: the camera
    # would read "81 01 04 3F 01 FF" as a cut-off packet.
    for name in ("recall_preset", "save_preset", "delete_preset"):
        assert INFO["commands"][name]["params"]["number"]["max"] == 254


def test_recalling_99_is_refused_in_favour_of_reboot():
    async def run():
        driver, _ = await connected("i12")
        with pytest.raises(ValueError, match="Reboot"):
            await driver.send_command("recall_preset", {"number": 99})
    asyncio.run(run())


@pytest.mark.parametrize("model,number", [
    ("i12", 80), ("i12", 85), ("i20", 82), ("i20", 105), ("i12d", 89), ("p12", 95), ("p20", 99),
])
def test_a_reserved_preset_cannot_be_saved_over_or_deleted(model, number):
    async def run():
        driver, _ = await connected(model)
        with pytest.raises(ValueError, match="reserved"):
            await driver.send_command("save_preset", {"number": number})
        with pytest.raises(ValueError, match="reserved"):
            await driver.send_command("delete_preset", {"number": number})
    asyncio.run(run())


def test_a_ptz_camera_may_store_a_preset_where_an_intelligent_one_reserves_it():
    async def run():
        driver, _ = await connected("p12")
        await driver.send_command("save_preset", {"number": 80})
        assert driver.transport.sent[-1] == h("81 01 04 3F 01 50 FF")
    asyncio.run(run())


def test_the_home_and_tracking_shots_save_but_never_delete_on_an_intelligent_camera():
    async def run():
        driver, _ = await connected("i20")
        await driver.send_command("save_preset", {"number": 0})
        for number in (0, 1):
            with pytest.raises(ValueError, match="tracking needs"):
                await driver.send_command("delete_preset", {"number": number})
        p12, _ = await connected("p12")
        await p12.send_command("delete_preset", {"number": 0})
    asyncio.run(run())


def test_the_simulator_refuses_storing_over_a_reserved_preset():
    async def run():
        driver, _ = await connected("i12")
        with pytest.raises(RuntimeError, match="cannot run"):
            await driver._command("save_preset", h("01 04 3F 01 50"))
    asyncio.run(run())


def test_group_tracking_zones_profiles_and_modes_reach_the_camera():
    async def run():
        i20, sim = await connected("i20")
        await i20.send_command("start_group_tracking")
        assert sim.get_state("group_tracking") is True
        await i20.send_command("select_tracking_profile", {"profile": 3})
        assert sim.get_state("tracking_profile") == 3
        i12d, dsim = await connected("i12d")
        await i12d.send_command("enable_speaker_tracking")
        assert dsim.get_state("intelligent_mode") == "speaker_tracking"
        await i12d.send_command("toggle_osd_menu")
        assert dsim.get_state("osd_menu") is True
    asyncio.run(run())


# --------------------------------------------------------------------------
# Privacy Mode, both readings
# --------------------------------------------------------------------------

def test_privacy_mode_reads_standby_and_the_poll_asks_nothing_else():
    async def run():
        driver, sim = await connected("i12")
        await driver.send_command("power_off")
        assert sim.get_state("power") == "standby"
        driver.transport.sent.clear()
        await driver.poll()
        assert state(driver, "power") == "standby"
        assert driver.transport.sent == [h("81 09 04 00 FF")]
    asyncio.run(run())


def test_a_command_in_privacy_mode_says_to_send_power_on():
    async def run():
        driver, sim = await connected("i12")
        await driver.send_command("power_off")
        with pytest.raises(RuntimeError, match="Privacy Mode. Send Power On first"):
            await driver.send_command("pt_home")
        await driver.send_command("power_on")
        await driver.send_command("pt_home")
        assert state(driver, "power") == "on"
    asyncio.run(run())


def test_a_silent_privacy_mode_keeps_the_camera_online_and_power_on_wakes_it():
    async def run():
        driver, sim = await connected("i12")
        sim.inject_error("privacy_silent")
        await driver.send_command("power_off")
        await driver.poll()  # no answer: returns, does not raise
        await driver._liveness_probe()  # standby: silence is not a failure
        with pytest.raises(RuntimeError, match="Privacy Mode"):
            await driver.send_command("pt_home")
        await driver.send_command("power_on")
        await driver.poll()
        assert state(driver, "power") == "on"
    asyncio.run(run())


def test_power_off_from_the_remote_is_read_by_the_next_poll():
    async def run():
        driver, sim = await connected("i12")
        sim.set_state("power", "standby")
        await driver.poll()
        assert state(driver, "power") == "standby"
    asyncio.run(run())


# --------------------------------------------------------------------------
# Liveness and error replies
# --------------------------------------------------------------------------

def test_the_liveness_probe_raises_when_a_powered_camera_goes_silent():
    async def run():
        driver, sim = await connected("i12")
        sim.inject_error("no_response")
        with pytest.raises(ConnectionError, match="not responding"):
            await driver._liveness_probe()
    asyncio.run(run())


def test_an_error_reply_counts_as_alive():
    async def run():
        driver, sim = await connected("i12")
        sim.handle_command = lambda data: h("90 60 02 FF")
        await driver._liveness_probe()
    asyncio.run(run())


def test_a_silent_camera_that_never_answered_fails_the_probe():
    async def run():
        driver, sim = make("i12")
        sim.inject_error("no_response")
        await driver.connect()
        assert state(driver, "power") is None
        with pytest.raises(ConnectionError):
            await driver._liveness_probe()
    asyncio.run(run())


def test_a_busy_camera_asks_for_the_command_again():
    async def run():
        driver, sim = await connected("i12")
        sim.inject_error("busy")
        with pytest.raises(RuntimeError, match="Send it again"):
            await driver.send_command("zoom_stop")
    asyncio.run(run())


def test_manual_focus_during_auto_focus_names_the_reason():
    async def run():
        driver, _ = await connected("i12")
        with pytest.raises(RuntimeError, match="manual focus command while auto focus is on"):
            await driver.send_command("focus_near")
    asyncio.run(run())


def test_a_syntax_error_reply_is_a_refusal():
    async def run():
        driver, _ = await connected("i12", sim_model="i20")
        with pytest.raises(RuntimeError, match="syntax error"):
            await driver.send_command("set_ir_receiver", {"enabled": True})
    asyncio.run(run())


def test_a_late_completion_or_another_sockets_error_does_not_answer_an_inquiry():
    async def run():
        driver, sim = await connected("i12")
        real = sim.handle_command

        def noisy(data):
            reply = real(data)
            if data == h("81 09 04 00 FF"):
                return h("90 51 FF 90 61 41 FF") + reply
            return reply
        sim.handle_command = noisy
        await driver.poll()
        assert state(driver, "power") == "on"
    asyncio.run(run())


# --------------------------------------------------------------------------
# Narrowing by model
# --------------------------------------------------------------------------

def commands_of(model, **config):
    driver, _ = make(model, **config)
    return set(driver.DRIVER_INFO["commands"])


def test_each_model_offers_its_own_surface():
    assert {"start_tracking", "set_home_shot", "set_ir_receiver"} <= commands_of("i12")
    assert "start_group_tracking" not in commands_of("i12")
    assert {"start_group_tracking", "recall_preset_zone", "select_tracking_profile"} <= commands_of("i20")
    assert "set_ir_receiver" not in commands_of("i20")
    assert {"enable_group_framing", "enable_speaker_tracking", "set_ir_receiver"} <= commands_of("i12d")
    for model in ("p12", "p20"):
        offered = commands_of(model)
        assert "set_mount_mode" in offered
        assert not {"start_tracking", "set_home_shot", "set_ir_receiver"} & offered
    assert "set_mount_mode" not in commands_of("i12")


def test_the_switching_commands_need_a_host_model_with_the_setting_on():
    switching = DRV._SWITCHING_COMMANDS
    assert not switching & commands_of("i12")
    assert switching <= commands_of("i12", switching_host=True)
    assert switching <= commands_of("i12d", switching_host=True)
    assert not switching & commands_of("i20", switching_host=True)
    assert not switching & commands_of("p12", switching_host=True)


def test_quick_actions_and_settings_follow_the_model():
    driver, _ = make("i12")
    assert driver.DRIVER_INFO["quick_actions"] == [
        "power_on", "power_off", "start_tracking", "pause_tracking", "recall_home_shot",
    ]
    assert set(driver.DRIVER_INFO["device_settings"]) >= {"ir_receiver", "ae_mode"}
    assert "mount_mode" not in driver.DRIVER_INFO["device_settings"]
    p12, _ = make("p12")
    assert p12.DRIVER_INFO["quick_actions"] == ["power_on", "power_off", "pt_home"]
    assert "mount_mode" in p12.DRIVER_INFO["device_settings"]
    assert "ir_receiver" not in p12.DRIVER_INFO["device_settings"]
    # The class declaration is untouched.
    assert "start_tracking" in INFO["commands"] and "set_mount_mode" in INFO["commands"]


def test_a_command_another_model_has_is_refused_by_name():
    async def run():
        driver, _ = await connected("p12")
        with pytest.raises(ValueError, match="not available on this camera model"):
            await driver.send_command("start_tracking")
        with pytest.raises(ValueError, match="Unknown command"):
            await driver.send_command("no_such_command")
    asyncio.run(run())


def _sample(pdef):
    if pdef["type"] == "enum":
        first = pdef["values"][0]
        return first["value"] if isinstance(first, dict) else first
    if pdef["type"] == "boolean":
        return True
    if pdef["type"] == "string":
        return "192.168.1.20"
    return pdef.get("min", 0) if pdef.get("min", 0) > 0 else max(pdef.get("min", 0), 2)


@pytest.mark.parametrize("model", ["i12", "i20", "p12", "p20", "i12d"])
def test_every_offered_command_reaches_the_simulator(model):
    async def run():
        driver, sim = await connected(model, switching_host=True)
        sim.set_state("focus_mode", "manual")
        # Reboot is covered on its own; Power Off goes last, since Privacy
        # Mode refuses what follows it.
        names = [c for c in driver.DRIVER_INFO["commands"] if c not in ("power_off", "reboot")]
        names.append("power_off")
        for name in names:
            cdef = driver.DRIVER_INFO["commands"][name]
            params = {p: _sample(d) for p, d in cdef["params"].items()}
            if name in ("recall_preset", "save_preset", "delete_preset"):
                params["number"] = 10
            if name in ("focus_auto",):
                continue  # keeps the focus commands that follow it executable
            sent_before = len(driver.transport.sent)
            await driver.send_command(name, params)
            assert len(driver.transport.sent) == sent_before + 1, name
    asyncio.run(run())


def test_every_declared_command_has_a_branch():
    async def run():
        for model in ("i12", "i20", "p12", "i12d"):
            driver, sim = await connected(model, switching_host=True)
            sim.set_state("focus_mode", "manual")
            for name, cdef in driver.DRIVER_INFO["commands"].items():
                params = {p: _sample(d) for p, d in cdef["params"].items()}
                if "number" in params:
                    params["number"] = 10
                try:
                    await driver.send_command(name, params)
                except ValueError as exc:
                    assert "Unknown command" not in str(exc), name
                except RuntimeError:
                    pass
                sim._booting_until = 0.0
                sim.set_state("power", "on")
    asyncio.run(run())


# --------------------------------------------------------------------------
# Intelligent Switching host
# --------------------------------------------------------------------------

def test_a_switching_host_reads_its_cameras_and_output():
    async def run():
        driver, sim = await connected("i12", switching_host=True)
        assert driver.list_children("switch_camera") == [2, 3, 4, 5]
        await driver.send_command("set_switching_camera", {"camera": 2, "ip": "192.168.1.10"})
        await driver.send_command("set_switching_camera", {"camera": 4, "ip": "10.1.2.3"})
        assert sim.get_state("camera_2_ip") == "192.168.1.10"
        await driver.send_command("switch_to_camera", {"camera": 4})
        await driver.send_command("resume_switching")
        await driver.poll(slow=True)
        assert driver.get_child_state("switch_camera", 2)["ip"] == "192.168.1.10"
        assert driver.get_child_state("switch_camera", 2)["connected"] is True
        assert driver.get_child_state("switch_camera", 3)["connected"] is False
        assert driver.get_child_state("switch_camera", 4)["ip"] == "10.1.2.3"
        assert state(driver, "switching_active") is True
        assert state(driver, "switching_output") == 4
        await driver.send_command("pause_switching")
        await driver.send_command("clear_switching_cameras")
        await driver.poll(slow=True)
        assert state(driver, "switching_active") is False
        assert driver.get_child_state("switch_camera", 2)["connected"] is False
        assert driver.get_child_state("switch_camera", 2)["ip"] == ""
    asyncio.run(run())


def test_the_camera_ips_are_re_read_only_on_slow_polls():
    async def run():
        driver, _ = await connected("i12", switching_host=True)
        driver.transport.sent.clear()
        await driver.poll(slow=False)
        assert h("81 C2 09 09 02 FF") not in driver.transport.sent
        assert h("81 C2 09 0D 02 FF") in driver.transport.sent
        driver.transport.sent.clear()
        await driver.poll(slow=True)
        assert h("81 C2 09 09 02 FF") in driver.transport.sent
    asyncio.run(run())


def test_a_camera_that_is_not_a_host_registers_no_switching_cameras():
    async def run():
        driver, _ = await connected("i12")
        assert driver.list_children("switch_camera") == []
        driver.transport.sent.clear()
        await driver.poll()
        assert not any(p[1] == 0xC2 for p in driver.transport.sent)
    asyncio.run(run())


# --------------------------------------------------------------------------
# Video feeds
# --------------------------------------------------------------------------

def feeds(driver):
    return {
        fid: driver.get_child_state("feed", fid)["preview_url"]
        for fid in driver.list_children("feed")
    }


def test_each_model_publishes_its_feeds_in_rtsp_path_order():
    async def run():
        i12, _ = await connected("i12")
        assert feeds(i12) == {
            "main": "rtsp://10.0.0.20:554/1.h264",
            "reference": "rtsp://10.0.0.20:554/2.h264",
        }
        assert i12.get_child_state("feed", "reference")["name"] == "Wide Reference"
        assert i12.get_child_state("feed", "main")["preview_format"] == "rtsp"
        p20, _ = await connected("p20")
        assert feeds(p20) == {"main": "rtsp://10.0.0.20:554/1.h264"}
        i12d, _ = await connected("i12d")
        assert feeds(i12d) == {
            "ptz1": "rtsp://10.0.0.20:554/1.h264",
            "ptz2": "rtsp://10.0.0.20:554/2.h264",
            "reference": "rtsp://10.0.0.20:554/3.h264",
        }
    asyncio.run(run())


def test_the_login_goes_in_the_stream_address_only_when_asked():
    async def run():
        plain, _ = await connected("p12", camera_password="p@ss word", rtsp_port=3500,
                                   stream_encoding="h265")
        assert feeds(plain)["main"] == "rtsp://10.0.0.20:3500/1.h265"
        login, _ = await connected("p12", camera_password="p@ss word",
                                   credentials_in_stream_url=True)
        assert feeds(login)["main"] == "rtsp://admin:p%40ss%20word@10.0.0.20:554/1.h264"
        no_password, _ = await connected("p12", credentials_in_stream_url=True)
        assert feeds(no_password)["main"] == "rtsp://10.0.0.20:554/1.h264"
    asyncio.run(run())


# --------------------------------------------------------------------------
# Device settings
# --------------------------------------------------------------------------

@pytest.mark.parametrize("model,key,value,reading", [
    ("i12", "ae_mode", "manual", "manual"),
    ("i12", "wb_mode", "indoor", "indoor"),
    ("i12", "backlight", True, True),
    ("i12", "exp_comp", True, True),
    ("i12", "exp_comp_level", -3, -3),
    ("i12", "ir_receiver", False, False),
    ("p12", "mount_mode", "ceiling", "ceiling"),
])
def test_each_setting_writes_and_reads_back(model, key, value, reading):
    async def run():
        driver, _ = await connected(model)
        await driver.set_device_setting(key, value)
        await driver.poll()
        state_key = driver.DRIVER_INFO["device_settings"][key]["state_key"]
        assert state(driver, state_key) == reading
    asyncio.run(run())


def test_a_setting_another_model_has_is_refused():
    async def run():
        driver, _ = await connected("i20")
        with pytest.raises(ValueError, match="Unknown device setting"):
            await driver.set_device_setting("mount_mode", "ceiling")
    asyncio.run(run())


def test_every_setting_names_a_declared_state_variable():
    for key, sdef in INFO["device_settings"].items():
        assert sdef["state_key"] in INFO["state_variables"], key
