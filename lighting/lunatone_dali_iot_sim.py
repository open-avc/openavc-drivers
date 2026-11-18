"""Lunatone M0023 HTTP/WebSocket simulator with fictional lighting data.

Models sparse API IDs, per-light capabilities, one or four DALI lines, partial
updates, list removal, a refused write and a stream outage with HTTP still up.
Scene levels and the example sequence are fictional, not factory defaults.
UDP discovery uses the simulated HTTP port number for isolated local probes.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from urllib.parse import urlsplit

from aiohttp import web
from openavc.simulator.datagram_server import DatagramServerMixin
from openavc.simulator.http_simulator import HTTPSimulator


class LunatoneDaliIotSimulator(DatagramServerMixin, HTTPSimulator):
    CAPACITY_KIND = None
    _push_state = True
    SIMULATOR_INFO = {
        'driver_id': 'lunatone_dali_iot', 'name': 'Lunatone DALI-2 IoT',
        'category': 'lighting', 'transport': 'http', 'default_port': 80,
        'initial_state': {
            'gateway_name': 'Training Room Lighting', 'feedback_enabled': True,
            'light_9_present': True, 'extra_light': False, 'omit_level': False,
            'light.2.name': 'Audience Lights', 'light.2.power': True, 'light.2.level': 65,
            'light.2.color_temperature': 4000,
            'light.9.name': 'Stage Color', 'light.9.power': True, 'light.9.level': 40,
            'light.9.red': 1.0, 'light.9.green': 0.4, 'light.9.blue': 0.1,
            'light.9.white': 0.2, 'light.9.amber': 0.3, 'light.9.free_color': 0.4,
            'light.9.color_x': 0.432, 'light.9.color_y': 0.15,
            'light.17.name': 'Entry Relay', 'light.17.power': False,
            'light.42.name': 'Added Light', 'light.42.power': True, 'light.42.level': 80,
            'sequence.7.active': False, 'sequence.7.enabled': True,
            'scan_status': 'done', 'scan_progress': 100,
        },
        'controls': [
            {'type': 'slider', 'key': 'light.2.level', 'label': 'Audience Brightness', 'min': 0, 'max': 100},
            {'type': 'toggle', 'key': 'light.2.power', 'label': 'Audience On'},
            {'type': 'slider', 'key': 'light.2.color_temperature', 'label': 'Audience Color Temperature', 'min': 2700, 'max': 6500},
            {'type': 'slider', 'key': 'light.9.level', 'label': 'Stage Brightness', 'min': 0, 'max': 100},
            {'type': 'toggle', 'key': 'light.17.power', 'label': 'Entry Relay On'},
            {'type': 'toggle', 'key': 'feedback_enabled', 'label': 'Live Event Connection'},
            {'type': 'toggle', 'key': 'light_9_present', 'label': 'Stage Listed in Gateway'},
            {'type': 'toggle', 'key': 'extra_light', 'label': 'Add Another Light'},
            {'type': 'toggle', 'key': 'omit_level', 'label': 'Gateway Omits Brightness Reading'},
        ],
        'error_modes': {
            'refuse_write': {'label': 'Refuse Control', 'description': 'Return HTTP 409 for lighting writes.'},
            'malformed_devices': {'label': 'Invalid Light List', 'description': 'Return a malformed device list.'},
        },
    }

    def __init__(self, device_id, config=None):
        self._clients = set()
        self._jobs = set()
        self._revision = 0
        self._initialized = False
        super().__init__(device_id, config)
        self.requests = []
        self._lines = int(self.config.get('simulation_lines', 1))
        self._initialized = True

    async def start(self, port):
        await super().start(port)
        try:
            await self.start_datagram_server(self.port)
        except OSError:
            await super().stop()
            raise

    async def stop(self):
        for socket in list(self._clients):
            await socket.close()
        jobs = list(self._jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        await self.stop_datagram_server()
        await super().stop()

    def _schedule(self, coroutine):
        job = asyncio.create_task(coroutine)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    def _signature(self):
        return {'timestamp': time.time(), 'counter': self._revision}

    def _envelope(self, kind, data):
        return {'type': kind, 'data': data, 'timeSignature': self._signature()}

    async def _publish(self, envelope):
        if self._silenced or not self.get_state('feedback_enabled'):
            return
        for socket in list(self._clients):
            with suppress(ConnectionError, RuntimeError):
                await socket.send_json(envelope)
        self.log_protocol('out', json.dumps(envelope))

    def _notify(self, kind, data):
        if self._initialized and self._clients:
            self._schedule(self._publish(self._envelope(kind, data)))

    def set_state(self, key, value):
        if key.startswith('light.') and key.split('.')[-1] not in ('name', 'power'):
            value = float(value)
        previous = self.get_state(key)
        super().set_state(key, value)
        if not self._initialized or previous == value:
            return
        self._revision += 1
        self._changed(key, value)

    def _changed(self, key, value):
        if key.startswith('light.'):
            self._changed_light(key, value)
        elif key.startswith('sequence.'):
            self._notify('sequences', {'sequences': [self._sequence()]})
        elif key == 'feedback_enabled':
            self._change_feedback(value)
        elif key in ('light_9_present', 'extra_light'):
            self._change_presence(9 if key == 'light_9_present' else 42, value)
        elif key == 'omit_level':
            self._notify('devices', {'devices': [self._device(2)]})
        elif key == 'gateway_name':
            self._notify('info', self._info())

    def _change_presence(self, ident, present):
        if present:
            self._notify('devices', {'devices': [self._device(ident)]})
        else:
            self._notify('devicesDeleted', {'deleted': [ident]})

    def _change_feedback(self, enabled):
        if not enabled:
            for socket in list(self._clients):
                self._schedule(socket.close())

    def _changed_light(self, key, value):
        _, number, prop = key.split('.')
        ident = int(number)
        row = self._device(ident)
        if prop == 'level':
            self.set_state(f'light.{ident}.power', value > 0)
        if prop == 'name':
            self._notify('devices', {'devices': [{'id': ident, 'name': value}]})
            return
        feature = PROPERTY_FEATURE.get(prop)
        if feature:
            self._notify('devices', {'devices': [{'id': ident, 'features': {feature: row['features'][feature]}}]})

    async def dispatch_datagram(self, data, addr):
        if data == b'discovery':
            self.send_datagram(json.dumps({'type': 'dali-2-iot', 'name': self.get_state('gateway_name')}).encode(), addr)

    async def respond_http(self, request, method, path, *payload):
        if path == '/' and request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self._events(request)
        return await super().respond_http(request, method, path, *payload)

    async def _events(self, request):
        if not self.get_state('feedback_enabled'):
            return web.Response(status=503)
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self._clients.add(socket)
        await socket.send_json(self._envelope('info', self._info()))
        try:
            async for message in socket:
                if message.type == web.WSMsgType.TEXT:
                    self.log_protocol('in', message.data)
        finally:
            self._clients.discard(socket)
        return socket

    def handle_request(self, method, path, headers, body):
        path = urlsplit(path).path
        self.requests.append((method, path, body))
        if method == 'GET':
            return self._get(path)
        if 'refuse_write' in self.active_errors:
            return 409, {'detail': 'Control refused'}
        try:
            return self._write(method, path, json.loads(body or '{}'))
        except (ValueError, TypeError, KeyError):
            return 422, {'detail': 'Invalid request'}

    def _get(self, path):
        resources = {'/info': self._info, '/devices': self._devices,
                     '/zones': lambda: {'zones': [self._zone()]},
                     '/automations/sequences': lambda: {'sequences': [self._sequence()]},
                     '/dali/scan': self._scan}
        reader = resources.get(path)
        if reader is None:
            return 404, {'detail': 'Not found'}
        data = reader()
        data['timeSignature'] = self._signature()
        return 200, data

    def _info(self):
        return {'name': self.get_state('gateway_name'), 'version': 'v1.18.0/1.0.9',
                'tier': 'plus', 'emergencyLight': False, 'errors': {},
                'descriptor': {'lines': self._lines, 'bufferSize': 32, 'protocolVersion': '1.0'},
                'device': {'serial': 1234567890, 'articleNumber': 22176625 if self._lines == 4 else 89453886}}

    def _ids(self):
        ids = [2, 17]
        if self.get_state('light_9_present'):
            ids.append(9)
        if self.get_state('extra_light'):
            ids.append(42)
        return ids

    def _devices(self):
        if 'malformed_devices' in self.active_errors:
            return {'devices': 'invalid'}
        return {'devices': [self._device(ident) for ident in self._ids()]}

    def _device(self, ident):
        features = {'switchable': {'status': self.get_state(f'light.{ident}.power')},
                    'scene': True, 'saveToScene': True, 'gotoLastActive': {}}
        if ident != 17:
            level = self.get_state(f'light.{ident}.level')
            features['dimmable'] = {'status': None if self.get_state('omit_level') else level}
        if ident == 2:
            features['colorKelvin'] = {'status': self.get_state('light.2.color_temperature')}
        if ident == 9:
            for name, pairs in COLOR_PROPERTIES.items():
                features[name] = {'status': {k: self.get_state(f'light.9.{p}') for k, p in pairs}}
        line = {2: 0, 9: 2, 17: 3, 42: 1}[ident] if self._lines == 4 else 0
        return {'id': ident, 'name': self.get_state(f'light.{ident}.name'),
                'address': {2: 0, 9: 0 if self._lines == 4 else 7, 17: 3, 42: 9}[ident],
                'line': line, 'type': 'default', 'features': features,
                'scenes': [], 'groups': [0] if ident in (2, 9) else [],
                'daliTypes': [8] if ident in (2, 9) else [6]}

    @staticmethod
    def _zone():
        return {'id': 3, 'name': 'Presentation Area',
                'targets': [{'type': 'device', 'id': 2}, {'type': 'device', 'id': 9}]}

    def _sequence(self):
        return {'id': 7, 'name': 'Welcome', 'active': self.get_state('sequence.7.active'),
                'enabled': self.get_state('sequence.7.enabled'), 'loop': False, 'repeat': 1,
                'steps': [{'type': 'features', 'data': {'targets': [{'type': 'device', 'id': 2}],
                                                    'features': {'dimmable': 75}}}]}

    def _scan(self):
        return {'id': 'simulated-scan', 'status': self.get_state('scan_status'),
                'progress': self.get_state('scan_progress'), 'found': len(self._ids())}

    def _write(self, method, path, body):
        if method == 'PUT' and path == '/info':
            self.set_state('gateway_name', str(body['name']))
            return 200, self._info()
        if method != 'POST':
            return 405, {}
        return self._post(path, body)

    def _post(self, path, body):
        if path == '/dali/scan':
            if body != {'newInstallation': False, 'noAddressing': True}:
                return 422, {'detail': 'Simulator accepts non-addressing registration only'}
            self._notify('scanProgress', self._scan())
            return 200, self._scan()
        parts = path.strip('/').split('/')
        if self._control_path(parts):
            return self._control(parts[0], int(parts[1]), body)
        if len(parts) == 4 and parts[:2] == ['automations', 'sequence']:
            return self._sequence_action(int(parts[2]), parts[3])
        return 404, {}

    @staticmethod
    def _control_path(parts):
        return len(parts) == 3 and parts[0] in ('device', 'zone') and parts[2] == 'control'

    def _sequence_action(self, ident, action):
        if ident != 7 or action not in ('start', 'stop'):
            return 404, {}
        self.set_state('sequence.7.active', action == 'start')
        if action == 'start':
            self.set_state('light.2.level', 75)
        return 200, {}

    def _control(self, kind, ident, body):
        targets = [ident] if kind == 'device' else [2, 9]
        if kind == 'zone' and ident != 3:
            return 404, {}
        if any(target not in self._ids() for target in targets):
            return 404, {}
        if kind == 'device' and not set(body).issubset(self._device(ident)['features']):
            return 422, {}
        for target in targets:
            self._apply_control(target, body)
        return 200, {}

    def _apply_control(self, ident, body):
        for key, value in body.items():
            if key in self._device(ident)['features']:
                self._apply_feature(ident, key, value)

    def _apply_feature(self, ident, key, value):
        fields = {'switchable': 'power', 'dimmable': 'level', 'colorKelvin': 'color_temperature'}
        if key in fields:
            self.set_state(f'light.{ident}.{fields[key]}', value)
        elif key in COLOR_PROPERTIES:
            for wire, prop in COLOR_PROPERTIES[key]:
                self.set_state(f'light.{ident}.{prop}', value[wire])
        elif key == 'scene':
            self._scene(ident, int(value))
        elif key == 'gotoLastActive':
            self._restore(ident)

    def _scene(self, ident, scene):
        # Fictional programmed looks; each light may store a different level.
        level = {0: 100, 1: 20, 2: 60}.get(scene, 35)
        self.set_state(f'light.{ident}.power', level > 0)
        if ident != 17:
            self.set_state(f'light.{ident}.level', level)

    def _restore(self, ident):
        self.set_state(f'light.{ident}.power', True)
        if ident != 17 and not self.get_state(f'light.{ident}.level'):
            self.set_state(f'light.{ident}.level', 100)


COLOR_PROPERTIES = {'colorRGB': [('r', 'red'), ('g', 'green'), ('b', 'blue')],
                    'colorWAF': [('w', 'white'), ('a', 'amber'), ('f', 'free_color')],
                    'colorXY': [('x', 'color_x'), ('y', 'color_y')]}
PROPERTY_FEATURE = {'power': 'switchable', 'level': 'dimmable', 'color_temperature': 'colorKelvin',
                    **{prop: feature for feature, pairs in COLOR_PROPERTIES.items() for _, prop in pairs}}
