"""Designer API v12 simulator, from Pharos's HTTP and WebSocket tables.

Fictional project, sparse IDs, two playback groups, three fixture groups and
two controllers. This exercises OpenAVC, not Pharos hardware. Fades complete
at their end; timeline time does not advance automatically. Trigger 7 starts
scene 4 when its simulated condition is true. No fixture output is generated.
Authentication uses opaque synthetic tokens with five-minute expiry; real
controllers use JWTs. Zero binary expiry messages follow the WebSocket guide.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import suppress
from urllib.parse import parse_qs, urlsplit

from aiohttp import web
from openavc.simulator.http_simulator import HTTPSimulator


ROSTERS = {
    'timeline': [
        {'num': 2, 'name': 'Evening Sequence', 'group': 'Public Spaces', 'group_num': 3,
         'length': 120000, 'source_bus': 'internal', 'priority': 'normal'},
        {'num': 9, 'name': 'Event Sequence', 'group': 'Public Spaces', 'group_num': 3,
         'length': 60000, 'source_bus': 'internal', 'priority': 'normal'},
        {'num': 14, 'name': 'Service Sequence', 'group': 'Back of House', 'group_num': 8,
         'length': 30000, 'source_bus': 'internal', 'priority': 'normal'},
    ],
    'scene': [
        {'num': 4, 'name': 'Presentation', 'group': 'Public Spaces', 'group_num': 3},
        {'num': 11, 'name': 'Reception', 'group': 'Public Spaces', 'group_num': 3},
        {'num': 20, 'name': 'Service Lights', 'group': 'Back of House', 'group_num': 8},
    ],
    'group': [{'num': 0, 'name': 'All Fixtures'}, {'num': 3, 'name': 'Audience'},
              {'num': 8, 'name': 'Stage'}],
    'playback_group': [{'num': 3, 'name': 'Public Spaces'}, {'num': 8, 'name': 'Back of House'}],
    'trigger': [{'num': 7, 'name': 'Presentation Mode', 'type': 'Soft', 'group': '#e18383',
                 'description': 'Start the presentation lighting',
                 'conditions': [{'text': 'Presentation permitted'}],
                 'actions': [{'text': 'Start Scene 4'}]}],
    'controller': [
        {'num': 1, 'type': 'LPC X', 'name': 'Main Lighting', 'serial': 'SIM001',
         'ip_address': '', 'is_network_primary': True},
        {'num': 3, 'type': 'LPC', 'name': 'Service Lighting', 'serial': 'SIM003',
         'ip_address': '192.0.2.3', 'is_network_primary': False},
    ],
}


class PharosDesignerSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        'driver_id': 'pharos_designer', 'name': 'Pharos Designer LPC',
        'category': 'lighting', 'transport': 'http', 'default_port': 80,
        'initial_state': {
            'project_loaded': True, 'project_revision': 1, 'security_enabled': False,
            'trigger_condition': True, 'beacon': False,
            'group.0.level': 100, 'group.3.level': 80, 'group.8.level': 65,
            'scene.4.state': 'started', 'scene.4.onstage': True,
            'scene.11.state': 'none', 'scene.11.onstage': False,
            'scene.20.state': 'none', 'scene.20.onstage': False,
            'timeline.2.state': 'running', 'timeline.2.onstage': True,
            'timeline.2.position': 5000, 'timeline.2.rate_adjusted': False,
            'timeline.9.state': 'none', 'timeline.9.onstage': False,
            'timeline.9.position': 0, 'timeline.9.rate_adjusted': False,
            'timeline.14.state': 'none', 'timeline.14.onstage': False,
            'timeline.14.position': 0, 'timeline.14.rate_adjusted': False,
            'controller.1.online': True, 'controller.3.online': True,
        },
        'controls': [
            {'type': 'slider', 'key': 'group.3.level', 'label': 'Audience Master Intensity', 'min': 0, 'max': 100},
            {'type': 'slider', 'key': 'group.8.level', 'label': 'Stage Master Intensity', 'min': 0, 'max': 100},
            {'type': 'select', 'key': 'timeline.2.state', 'label': 'Evening Playback',
             'options': ['none', 'running', 'paused', 'holding_at_end', 'released']},
            {'type': 'toggle', 'key': 'timeline.2.onstage', 'label': 'Evening Affects Output'},
            {'type': 'toggle', 'key': 'controller.3.online', 'label': 'Service Controller Online'},
            {'type': 'toggle', 'key': 'trigger_condition', 'label': 'Presentation Permitted'},
            {'type': 'toggle', 'key': 'project_loaded', 'label': 'Project Loaded'},
        ],
        'error_modes': {
            'refuse_write': {'label': 'Refuse Control', 'description': 'Answer writes with HTTP 403.'},
            'expired_token': {'label': 'Expire Tokens', 'description': 'Reject authenticated API calls with HTTP 401.'},
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.SIMULATOR_INFO = dict(type(self).SIMULATOR_INFO, tls=bool(self.config.get('ssl')))
        self._subscribers = {}
        self._tokens = {}
        self._pending = set()
        self._hidden = set()
        self.requests = []
        if self.config.get('username'):
            self.set_state('security_enabled', True)

    async def stop(self):
        for socket in list(self._subscribers):
            await socket.close()
        for task in self._pending:
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
        await super().stop()

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def set_state(self, key, value):
        if key.endswith(('.level', '.position')):
            value = int(value)
        changed = self.get_state(key) != value
        super().set_state(key, value)
        parts = key.split('.')
        if all((changed, len(parts) == 3, hasattr(self, '_subscribers'))):
            self._publish(parts[0], int(parts[1]))

    def _publish(self, kind, num):
        if kind not in ('timeline', 'scene', 'group') or self._silenced:
            return
        row = self._row(kind, num)
        if row is None:
            return
        fields = {'timeline': ('num', 'state', 'onstage', 'position'),
                  'scene': ('num', 'state', 'onstage'), 'group': ('num', 'name', 'level')}[kind]
        envelope = {'broadcast': kind, 'data': {k: row[k] for k in fields}}
        for socket, channels in list(self._subscribers.items()):
            if kind in channels:
                self._task(self._send(socket, envelope))

    async def _send(self, socket, envelope):
        with suppress(ConnectionError):
            await socket.send_json(envelope)
        self.log_protocol('out', json.dumps(envelope))

    async def respond_http(self, request, method, path, *payload):
        if path == '/query' and request.headers.get('Upgrade', '').lower() == 'websocket':
            return await self._websocket(request)
        return await super().respond_http(request, method, path, *payload)

    async def _websocket(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self._subscribers[socket] = set()
        try:
            async for message in socket:
                if self._silenced or message.type != web.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if not self._authorised(payload.get('token', '')):
                    await socket.send_bytes(bytes(16))
                elif payload.get('subscribe'):
                    self._subscribers[socket].add(payload['subscribe'])
                elif self.get_state('security_enabled'):
                    await socket.send_bytes(b'\x01' * 16)
                else:
                    await socket.send_json({})
        finally:
            self._subscribers.pop(socket, None)
        return socket

    def _authorised(self, token):
        return not self.get_state('security_enabled') or self._tokens.get(token, 0) > time.monotonic()

    def _new_token(self):
        token = secrets.token_urlsafe(24)
        self._tokens[token] = time.monotonic() + 300
        return token

    def _authenticate(self, body):
        try:
            data = json.loads(body)
        except ValueError:
            return 400, {}
        if not all(isinstance(data.get(k), str) for k in ('username', 'password')):
            return 400, {}
        expected = (self.config.get('username', 'admin'), self.config.get('password', 'simulator'))
        supplied = (data['username'], data['password'])
        if supplied != expected or 'invalid' in supplied:
            return 302, '', {'Location': '/default/login.lsp'}
        return 200, {'token': self._new_token()}

    def handle_request(self, method, path, headers, body):
        parsed = urlsplit(path)
        resource = parsed.path.removeprefix('/api/')
        if parsed.path == '/authenticate':
            return self._authenticate(body)
        token = headers.get('Authorization', '').removeprefix('Bearer ')
        if not self._authorised(token) or 'expired_token' in self.active_errors:
            return 401, {}
        self.requests.append((method, path, body))
        if method != 'GET' and 'refuse_write' in self.active_errors:
            return 403, {}
        status, data = self._dispatch_request(method, resource, parsed, body)
        if status == 200 and self.get_state('security_enabled'):
            data['token'] = self._new_token()
        return status, data

    def _dispatch_request(self, method, resource, parsed, body):
        if method == 'GET':
            return self._get_resource(resource, parse_qs(parsed.query))
        try:
            payload = json.loads(body or '{}')
            return self._write_resource(resource, payload)
        except (ValueError, KeyError, TypeError):
            return 400, {}

    def _get_resource(self, resource, query):
        if resource == 'system':
            return 200, {'hardware_type': 'LPC X', 'firmware_version': '2.16.5',
                         'serial_number': 'SIM001', 'channel_capacity': 10240}
        if resource == 'api_version':
            return 200, {'version': 12}
        if not self.get_state('project_loaded'):
            return 404, {}
        if resource == 'project':
            return 200, {'name': 'Auditorium Lighting', 'author': 'Example Integrator',
                         'unique_id': 'simulated-project',
                         'upload_date': f'2026-11-{int(self.get_state("project_revision")):02}T10:00:00'}
        if resource not in ROSTERS:
            return 404, {}
        return 200, {resource + 's': self._get_rows(resource, query)}

    def _get_rows(self, resource, query):
        rows = [self._row(resource, r['num']) for r in ROSTERS[resource]
                if (resource, r['num']) not in self._hidden]
        if 'num' in query:
            rows = [r for r in rows if str(r['num']) == query['num'][0]]
        if resource == 'group':
            rows = [{k: v for k, v in r.items() if k != 'num'} if r['num'] == 0 else r for r in rows]
        return rows

    def _row(self, kind, num):
        source = next((r for r in ROSTERS[kind] if r['num'] == num), None)
        if source is None:
            return None
        row = dict(source)
        prefix = f'{kind}.{num}.'
        for key, value in self.state.items():
            if key.startswith(prefix):
                row[key[len(prefix):]] = value
        return row

    def _write_resource(self, resource, body):
        if resource == 'beacon':
            self.set_state('beacon', bool(body['enable']))
            return 200, {}
        if not self.get_state('project_loaded'):
            return 404, {}
        handlers = {'timeline': self._playback, 'scene': self._playback,
                    'group': self._group, 'trigger': self._trigger}
        if resource not in handlers:
            return 404, {}
        return handlers[resource](resource, body)

    def _targets(self, kind, body):
        rows = [r for r in ROSTERS[kind] if (kind, r['num']) not in self._hidden]
        if 'num' in body:
            return [r for r in rows if r['num'] == body['num']]
        group = body.get('group')
        if group is not None:
            rows = [r for r in rows if group in (r['group'], r['group_num'])]
        return rows

    def _playback(self, kind, body):
        targets = self._targets(kind, body)
        if not targets:
            return 404, {}
        action = body['action']
        if action == 'start_release_others':
            self._release_others(kind, targets[0], body)
            action = 'start'
        for row in targets:
            self._change_playback(kind, row['num'], action, body)
        return 200, {}

    def _release_others(self, kind, selected, body):
        for row in ROSTERS[kind]:
            same_group = not body.get('same_group') or row['group_num'] == selected['group_num']
            if row['num'] != selected['num'] and same_group:
                self._change_playback(kind, row['num'], 'release', body)

    def _change_playback(self, kind, num, action, body):
        prefix = f'{kind}.{num}.'
        started = 'running' if kind == 'timeline' else 'started'
        if action == 'toggle':
            action = 'release' if self.get_state(prefix + 'state') in ('running', 'paused', 'holding_at_end', 'started') else 'start'
        if action == 'set_position':
            return self._set_position(prefix, body)
        if action == 'set_rate':
            self.set_state(prefix + 'rate_adjusted', float(body['rate']) != 1)
            return
        state = {'start': started, 'resume': started, 'pause': 'paused', 'release': 'released'}[action]
        values = {'state': state, 'onstage': action != 'release'}
        self._after(float(body.get('fade', 0)) if action == 'release' else 0, prefix, values)

    def _set_position(self, prefix, body):
        seconds = 10 if body.get('flag') == 'Start sparkle' else body['time']
        self.set_state(prefix + 'position', int(float(seconds) * 1000))

    def _after(self, delay, prefix, values):
        if delay <= 0:
            for key, value in values.items():
                self.set_state(prefix + key, value)
            return
        self._task(self._delayed(delay, prefix, values))

    async def _delayed(self, delay, prefix, values):
        await asyncio.sleep(delay)
        self._after(0, prefix, values)

    def _group(self, kind, body):
        num = body['num']
        if self._row('group', num) is None or body['action'] != 'master_intensity':
            return 400, {}
        value = body['level']
        if isinstance(value, str) and ':' in value:
            numerator, denominator = value.split(':')
            value = float(numerator) / float(denominator)
        level = round(float(value) * 100)
        if not 0 <= level <= 100:
            return 400, {}
        self._after(float(body.get('fade', 0)), f'group.{num}.', {'level': level})
        return 200, {}

    def _trigger(self, kind, body):
        if body['num'] != 7:
            return 404, {}
        if not body.get('conditions', True) or self.get_state('trigger_condition'):
            self._change_playback('scene', 4, 'start', {})
        return 200, {}
