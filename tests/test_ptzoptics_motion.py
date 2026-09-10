"""A drive persists until Stop, independently for pan/tilt and zoom.

Packets come from the manufacturer's VISCA movement table:
https://docs.ptzoptics.com/dev/visca-api/movement/
The simulated speed is illustrative; these checks assert direction, continued
movement and stopping, not a physical camera's degrees per second.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from _platform_stubs import install_stubs, load_module

install_stubs()
SIM = load_module(
    'ptzoptics_motion_sim',
    Path(__file__).resolve().parents[1] / 'cameras' / 'ptzoptics_sim.py',
)
ACK = bytes.fromhex('9041ff9051ff')
PT_STOP = '810106010c0a0303ff'
ZOOM_STOP = '8101040700ff'


def send(sim, packet):
    assert sim.handle_command(bytes.fromhex(packet)) == ACK


@pytest_asyncio.fixture
async def camera():
    sim = SIM.PTZOpticsSimulator('camera')
    sim.set_state('zoom_position', 6000)
    try:
        yield sim
    finally:
        await sim.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('packet,stop,axis,direction', [
    ('810106010c0a0301ff', PT_STOP, 'tilt_position', 1),
    ('810106010c0a0302ff', PT_STOP, 'tilt_position', -1),
    ('810106010c0a0103ff', PT_STOP, 'pan_position', -1),
    ('810106010c0a0203ff', PT_STOP, 'pan_position', 1),
    ('8101040723ff', ZOOM_STOP, 'zoom_position', 1),
    ('8101040733ff', ZOOM_STOP, 'zoom_position', -1),
])
async def test_a_single_drive_keeps_moving_until_stop(camera, packet, stop, axis, direction):
    before = camera.get_state(axis)
    send(camera, packet)
    await asyncio.sleep(.13)
    first = camera.get_state(axis)
    await asyncio.sleep(.13)
    second = camera.get_state(axis)
    assert direction * (first - before) > 0
    assert direction * (second - first) > 0
    send(camera, stop)
    stopped = camera.get_state(axis)
    await asyncio.sleep(.15)
    assert camera.get_state(axis) == stopped


@pytest.mark.asyncio
async def test_pan_stop_does_not_stop_zoom_and_reversing_replaces_the_drive(camera):
    send(camera, '810106010c0a0203ff')
    send(camera, '8101040723ff')
    await asyncio.sleep(.13)
    send(camera, PT_STOP)
    pan, zoom = camera.get_state('pan_position'), camera.get_state('zoom_position')
    await asyncio.sleep(.13)
    assert camera.get_state('pan_position') == pan
    assert camera.get_state('zoom_position') > zoom
    send(camera, ZOOM_STOP)
    zoom = camera.get_state('zoom_position')
    send(camera, '810106010c0a0103ff')
    await asyncio.sleep(.13)
    assert camera.get_state('pan_position') < pan
    assert camera.get_state('zoom_position') == zoom


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [
    '81010604ff',
    '81010605ff',
    '810106020c0a0000000000000000ff',
    '810106030c0a0000000000000000ff',
])
async def test_a_position_command_replaces_pan_tilt_drive(camera, replacement):
    send(camera, '810106010c0a0201ff')
    await asyncio.sleep(.13)
    send(camera, replacement)
    stopped = (camera.get_state('pan_position'), camera.get_state('tilt_position'))
    await asyncio.sleep(.15)
    assert (camera.get_state('pan_position'), camera.get_state('tilt_position')) == stopped


@pytest.mark.asyncio
async def test_direct_zoom_and_preset_recall_replace_continuous_moves(camera):
    send(camera, '8101043f0101ff')
    send(camera, '8101040723ff')
    await asyncio.sleep(.13)
    send(camera, '8101044700000000ff')
    await asyncio.sleep(.15)
    assert camera.get_state('zoom_position') == 0
    send(camera, '810106010c0a0201ff')
    send(camera, '8101040723ff')
    await asyncio.sleep(.13)
    send(camera, '8101043f0201ff')
    await asyncio.sleep(.15)
    assert [camera.get_state(k) for k in ('pan_position', 'tilt_position', 'zoom_position')] == [0, 0, 6000]


@pytest.mark.asyncio
async def test_standby_and_simulator_stop_cancel_all_movement(camera):
    send(camera, '810106010c0a0201ff')
    send(camera, '8101040723ff')
    await asyncio.sleep(.13)
    send(camera, '8101040003ff')
    stopped = camera.state
    await asyncio.sleep(.15)
    assert camera.state == stopped
    send(camera, '8101040002ff')
    send(camera, '810106010c0a0201ff')
    send(camera, '8101040723ff')
    await asyncio.sleep(.13)
    await camera.stop()
    stopped = camera.state
    await asyncio.sleep(.15)
    assert camera.state == stopped


@pytest.mark.asyncio
async def test_motion_stays_within_limits_and_can_reverse_at_a_limit(camera):
    camera.set_state('pan_position', 2447)
    camera.set_state('tilt_position', 1295)
    camera.set_state('zoom_position', 16383)
    send(camera, '8101060118140201ff')
    send(camera, '8101040727ff')
    await asyncio.sleep(.15)
    assert [camera.get_state(k) for k in ('pan_position', 'tilt_position', 'zoom_position')] == [2448, 1296, 16384]
    send(camera, '8101060118140102ff')
    send(camera, '8101040737ff')
    await asyncio.sleep(.15)
    assert camera.get_state('pan_position') < 2448
    assert camera.get_state('tilt_position') < 1296
    assert camera.get_state('zoom_position') < 16384
