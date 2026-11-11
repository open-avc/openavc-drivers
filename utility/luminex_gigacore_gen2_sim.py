"""GigaCore Gen 2 API 1.5.x simulator from Luminex's REST/WS references.

A fictional sparse inventory (ports 1, 3 and 30) exercises enumeration without
claiming a physical model's complete layout. Port 30 is a protected uplink.
LLDP, PoE power, temperature and counters are synthetic, not a hardware capture.
No packets are forwarded and no endpoint is actually powered or rebooted.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import suppress
from copy import deepcopy

from aiohttp import web
from openavc.simulator.http_simulator import HTTPSimulator


CONTROL_PATHS = {
    'device_name': '/device/name', 'description': '/device/description',
    'system_id': '/device/system_id', 'led_brightness': '/led/brightness',
    'poe_capable': '/poe/capable', 'status_message': '/status_message/message',
    'status_level': '/status_message/level', 'port_1_name': '/ports/port/1/legend',
    'port_1_enabled': '/ports/port/1/enabled', 'port_1_link': '/ports/port/1/link_state',
    'port_1_protected': '/ports/port/1/protected', 'port_1_available': '/ports/port/1/available',
    'poe_1_enabled': '/poe/ports/1/enabled', 'poe_1_error': '/poe/ports/1/error',
    'group_3_name': '/groups/group/3/name',
}


def _port(num, legend, protected=False):
    return {'port_number': num, 'legend': legend, 'type': 'sfp' if num == 30 else 'rj45',
            'protected': protected, 'available': True, 'enabled': True,
            'link_state': '10gbps fdx' if num == 30 else '1gbps fdx',
            'member_of': {'type': 'trunk' if num == 30 else 'group', 'id': 1 if num == 30 else 3},
            'rlinkx': {'enabled': True}, 'counters': {'rx_bit_rate': 85000000, 'tx_bit_rate': 4500000,
            'in_discards': 0, 'out_discards': 0, 'link_down_counter': 2}}


def _poe_port(num):
    return {'port_number': num, 'enabled': True, 'priority': 'low',
            'power_limit_type': 'class', 'user_defined_power_limit': 99000,
            'power_up_mode': 'dot3bt', 'capable_mode': 'poe++', 'indication': 'sourcing',
            'error': 'none', 'pd_class': 'class3', 'max_power_limit': 90000,
            'current_power_limit': 15400, 'reserved_power': 15400, 'power_output': 8200,
            'current_output': 158, 'voltage_output': 52, 'temperature': 36}


def _group(num, name, vlan, avb=False):
    return {'group_id': num, 'predefined': True, 'name': name, 'description': 'Example AV network',
            'vid': vlan, 'allow_config': num == 1, 'igmp': {'snooping': True, 'querier': True},
            'avb': {'enabled': avb}, 'ptp_clock_mode': 'transparent'}


def _inventory():
    return {'device': {'name': 'Stage Switch', 'description': 'Auditorium AV network', 'system_id': 7,
            'model': 'GigaCore30i', 'mac_address': '02:00:5e:00:53:01', 'serial': 'SIM-GC30I',
            'firmware': {'active': 'simulated'}, 'api_version': '1.5.0'},
            'config': {'mode': 'luminex'}, 'led': {'brightness': 2},
            'ports': {'port': [_port(1, 'Stage Encoder'), _port(3, 'Audio DSP'), _port(30, 'Core Uplink', True)]},
            'poe': {'capable': True, 'power_budget': 'reserved', 'psu_mode': 'redundant',
                    'pse_status': 'on', 'total_power': 450000, 'consumed_power': 16400,
                    'reserved_power': 30800, 'ports': [_poe_port(1), _poe_port(3)]},
            'groups': {'group': [_group(1, 'Management', 1), _group(3, 'Dante', 103), _group(8, 'Milan', 108, True)]},
            'system': {'uptime': 86400, 'cpu_load': 12, 'temperature': {'current': 42}},
            'status_message': {'level': 'none', 'message': ''},
            'lldp': {'neighbor': [{'port_number': 1, 'sys_name': 'Stage Encoder', 'chassis_id': 'SIM-ENC', 'remote_port': 'eth0'},
                                 {'port_number': 30, 'sys_name': 'Core Switch', 'chassis_id': 'SIM-CORE', 'remote_port': '8'}]},
            'mac': {'mac_table': [{'id': 1, 'mac': '02:00:5e:00:53:11', 'port_number': 1, 'vid': 103},
                                  {'id': 2, 'mac': '02:00:5e:00:53:30', 'port_number': 30, 'vid': 1},
                                  {'id': 3, 'mac': '02:00:5e:00:53:31', 'port_number': 30, 'vid': 1}]}}


class LuminexGigaCoreGen2Simulator(HTTPSimulator):
    SIMULATOR_INFO = {
        'driver_id': 'luminex_gigacore_gen2', 'name': 'Luminex GigaCore Gen 2',
        'category': 'utility', 'transport': 'http', 'default_port': 80,
        'initial_state': {'device_name': 'Stage Switch', 'description': 'Auditorium AV network',
                          'system_id': 7, 'led_brightness': 2, 'poe_capable': True,
                          'status_message': '', 'status_level': 'none', 'security_enabled': False,
                          'port_1_name': 'Stage Encoder', 'port_1_enabled': True,
                          'port_1_link': '1gbps fdx', 'port_1_protected': False, 'port_1_available': True,
                          'poe_1_enabled': True, 'poe_1_error': 'none', 'group_3_name': 'Dante',
                          'port_3_present': True},
        'controls': [
            {'type': 'toggle', 'key': 'port_1_enabled', 'label': 'Port 1 Enabled'},
            {'type': 'select', 'key': 'port_1_link', 'label': 'Port 1 Link', 'options': ['down', '1gbps fdx', '100mbps fdx']},
            {'type': 'toggle', 'key': 'port_1_protected', 'label': 'Port 1 Protected'},
            {'type': 'toggle', 'key': 'port_1_available', 'label': 'Port 1 Available'},
            {'type': 'toggle', 'key': 'poe_1_enabled', 'label': 'Port 1 PoE Enabled'},
            {'type': 'select', 'key': 'poe_1_error', 'label': 'Port 1 PoE Fault', 'options': ['none', 'overload', 'short', 'power_denied']},
            {'type': 'toggle', 'key': 'poe_capable', 'label': 'PoE Available'},
            {'type': 'toggle', 'key': 'port_3_present', 'label': 'Port 3 Present'},
            {'type': 'slider', 'key': 'led_brightness', 'label': 'LED Brightness', 'min': 0, 'max': 4},
        ],
        'error_modes': {
            'refuse_write': {'label': 'Refuse Changes', 'description': 'Answer writes with a documented error envelope.'},
            'ignore_write': {'label': 'Leave Value Unchanged', 'description': 'Acknowledge writes without applying them, to exercise readback.'},
            'reject_subscriptions': {'label': 'Refuse Live Feedback', 'description': 'Return WebSocket subscription errors.'},
            'auth_failed': {'label': 'Reject Credentials', 'description': 'Refuse HTTP and WebSocket sign-in.'},
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.SIMULATOR_INFO = dict(type(self).SIMULATOR_INFO, tls=bool(self.config.get('ssl')))
        self._push_state = True
        self._data = _inventory()
        self._subscribers = {}
        self._pending = set()
        self.requests = []
        self.set_state('security_enabled', bool(self.config.get('username') or self.config.get('password')))
        for key in CONTROL_PATHS:
            self._change(CONTROL_PATHS[key], self.get_state(key))

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def stop(self):
        for socket in list(self._subscribers):
            await socket.close()
        for task in self._pending:
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        await super().stop()

    @staticmethod
    def _step(data, key):
        if isinstance(data, list):
            return next(row for row in data if str(row.get('port_number', row.get('group_id'))) == key)
        return data[key]

    def _get(self, path):
        data = self._data
        for key in path.strip('/').split('/'):
            data = self._step(data, key)
        return data

    def _change(self, path, value):
        parts = path.strip('/').split('/')
        parent = self._data
        for key in parts[:-1]:
            parent = self._step(parent, key)
        if parts[-1] not in parent:
            raise KeyError(path)
        parent[parts[-1]] = deepcopy(value)
        self._derive(path)
        self._publish(path)

    def _derive(self, path):
        parts = path.split('/')
        if path.startswith('/poe/ports/'):
            self._derive_poe(int(parts[3]))
        elif path.startswith('/ports/port/') and parts[-1] == 'enabled':
            row = self._get('/'.join(parts[:-1]))
            row['link_state'] = ('10gbps fdx' if row['type'] == 'sfp' else '1gbps fdx') if row['enabled'] else 'down'
            self._publish('/'.join(parts[:-1]) + '/link_state')

    def _derive_poe(self, num):
        row = self._get(f'/poe/ports/{num}')
        sourcing = row['enabled'] and row['error'] == 'none'
        row.update(indication=('sourcing' if sourcing else 'error') if row['enabled'] else 'disabled',
                   power_output=8200 if sourcing else 0, current_output=158 if sourcing else 0,
                   voltage_output=52 if sourcing else 0)
        row['current_power_limit'] = min(row['user_defined_power_limit'], row['max_power_limit']) if row['power_limit_type'] == 'user_defined' else 15400
        self._data['poe']['consumed_power'] = sum(p['power_output'] for p in self._data['poe']['ports'])
        self._publish(f'/poe/ports/{num}/indication')

    def set_state(self, key, value):
        super().set_state(key, value)
        if not hasattr(self, '_data'):
            return
        if key in CONTROL_PATHS:
            self._change(CONTROL_PATHS[key], value)
        elif key == 'port_3_present':
            self._set_port_presence(bool(value))

    def _set_port_presence(self, present):
        for parent, key, builder in [('ports', 'port', lambda: _port(3, 'Audio DSP')), ('poe', 'ports', lambda: _poe_port(3))]:
            rows = self._data[parent][key]
            rows[:] = [row for row in rows if row['port_number'] != 3]
            if present:
                rows.append(builder())
            self._publish(f'/{parent}/{key}')

    @staticmethod
    def _error(code, reason):
        return {'code': code, 'reason': reason, 'id': '00000000-0000-4000-8000-000000000001', 'fmt': {}}

    def _authorised(self, headers):
        if 'auth_failed' in self.active_errors:
            return False
        if not self.get_state('security_enabled'):
            return True
        raw = f'{self.config.get("username", "admin")}:{self.config.get("password", "simulator")}'
        expected = 'Basic ' + base64.b64encode(raw.encode()).decode()
        return headers.get('Authorization') == expected

    def handle_request(self, method, path, headers, body):
        self.requests.append((method, path, body))
        if not self._authorised(headers):
            return 401, self._error('Unauthorized', 'Check the switch credentials.')
        if not path.startswith('/api/'):
            return 404, self._error('ResourceNotFound', 'Unknown API path.')
        path = path[4:]
        try:
            result = self._get(path) if method == 'GET' else self._put(path, json.loads(body))
            # The HTTP base handles objects or text. Serialize scalar/array
            # JSON explicitly so booleans remain true/false, never Python words.
            return 200, json.dumps(result)
        except PermissionError:
            return 403, self._error('Forbidden', 'This account cannot change switch settings.')
        except (KeyError, StopIteration):
            return 404, self._error('ResourceNotFound', 'The requested port or field does not exist.')
        except (ValueError, TypeError):
            return 400, self._error('InvalidValue', 'The value is not valid for this field.')

    def _put(self, path, value):
        if 'refuse_write' in self.active_errors:
            raise PermissionError('This account cannot change switch settings.')
        if 'ignore_write' in self.active_errors:
            return None
        if path == '/groups/assign_ports':
            return self._assign(value)
        if path.endswith('/counters/reset'):
            return self._reset(path, value)
        current = self._get(path)
        self._validate(path, value, current)
        self._change(path, value)
        for key, location in CONTROL_PATHS.items():
            if location == path:
                super().set_state(key, value)
        return self._get(path)

    @staticmethod
    def _validate(path, value, current):
        if type(current) is not type(value):
            raise ValueError('Wrong JSON type')
        key = path.rsplit('/', 1)[-1]
        LuminexGigaCoreGen2Simulator._validate_number(key, value)
        LuminexGigaCoreGen2Simulator._validate_choice(key, value)
        lengths = {'legend': 16, 'name': 64, 'description': 255}
        if key in lengths and len(value) > lengths[key]:
            raise ValueError('Too long')

    @staticmethod
    def _validate_number(key, value):
        bounds = {'user_defined_power_limit': (3000, 99000), 'brightness': (0, 4), 'system_id': (0, 999999)}
        if key in bounds and not bounds[key][0] <= value <= bounds[key][1]:
            raise ValueError('Outside range')

    @staticmethod
    def _validate_choice(key, value):
        enums = {'priority': ('low', 'high', 'critical'), 'power_limit_type': ('class', 'none', 'user_defined')}
        if key in enums and value not in enums[key]:
            raise ValueError('Unknown value')

    def _assign(self, value):
        group = int(value['id'])
        self._get(f'/groups/group/{group}')
        for num in value['ports']:
            self._change(f'/ports/port/{num}/member_of', {'type': 'group', 'id': group})
        return None

    def _reset(self, path, value):
        if value != {}:
            raise ValueError('Expected an empty object')
        counters = self._get(path.removesuffix('/reset'))
        for key in counters:
            counters[key] = 0
        return None

    async def respond_http(self, request, method, path, *payload):
        if path == '/api/ws' and request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self._websocket(request)
        status, body = self.handle_request(method, path, *payload)
        text = json.dumps(body) if isinstance(body, dict) else body
        return web.Response(status=status, text=text, content_type='application/json')

    async def _websocket(self, request):
        if not self._authorised(request.headers):
            return web.json_response(self._error('Unauthorized', 'Check the switch credentials.'), status=401)
        socket = web.WebSocketResponse(autoping=False)
        await socket.prepare(request)
        self._subscribers[socket] = set()
        try:
            async for message in socket:
                if self._silenced:
                    continue
                if message.type == web.WSMsgType.PING:
                    await socket.pong(message.data)
                elif message.type == web.WSMsgType.TEXT:
                    await self._subscribe(socket, json.loads(message.data))
        finally:
            self._subscribers.pop(socket, None)
        return socket

    async def _subscribe(self, socket, message):
        subscription = message.get('subscription', {})
        path = subscription.get('path', '')
        self.log_protocol('in', json.dumps(message))
        if subscription.get('action') == 'remove':
            self._subscribers[socket].discard(path)
            return
        try:
            self._get(path)
        except (KeyError, StopIteration):
            await self._send(socket, {'error': dict(self._error('ResourceNotFound', 'Unknown subscription path.'), path=path)})
            return
        if 'reject_subscriptions' in self.active_errors:
            await self._send(socket, {'error': dict(self._error('Forbidden', 'Subscriptions refused.'), path=path)})
            return
        self._subscribers[socket].add(path)
        await self._send(socket, {'message': 'Subscription added'})
        await self._notify(socket, path, 'subscription_response')

    def _publish(self, changed):
        if not hasattr(self, '_subscribers') or self._silenced:
            return
        for socket, paths in list(self._subscribers.items()):
            for path in paths:
                if self._overlaps(path, changed):
                    self._task(self._notify(socket, path, 'full_update'))

    @staticmethod
    def _overlaps(path, changed):
        return path == changed or changed.startswith(path + '/') or path.startswith(changed + '/')

    async def _notify(self, socket, path, kind):
        try:
            value = deepcopy(self._get(path))
        except (KeyError, StopIteration):
            value = None
        await self._send(socket, {'api_notification': {'path': path, 'notification_type': kind, 'new_value': value}})

    async def _send(self, socket, message):
        if self._silenced:
            return
        with suppress(ConnectionError):
            await socket.send_json(message)
            self.log_protocol('out', json.dumps(message))
