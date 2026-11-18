"""Makito X4 decoder simulator from the public 1.7.0 REST reference.

Illustrative streams and values, not hardware captures. Models documented
selected/received stream differences, fallback images, SDI assignments,
SRT path loss, missing fields, out-of-band changes and session refusal.
"""
from copy import deepcopy
import json
import secrets
from urllib.parse import unquote, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator


class HaivisionMakitoX4DecoderSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        'driver_id': 'haivision_makito_x4_decoder', 'name': 'Haivision Makito X4 Decoder',
        'category': 'streaming', 'transport': 'http', 'default_port': 443, 'tls': True,
        'initial_state': {
            'stream_3_name': 'Lecture Hall', 'stream_3_state': 3, 'stream_11_state': 2,
            'stream_11_present': True, 'video_present': True, 'audio_present': True,
            'srt_path2_present': True, 'srt_path2_state': 'ACTIVE', 'omit_measurements': False,
            'decoder_0_stream': 3, 'decoder_0_trouble': 0,
            'refuse_login': False, 'expire_session': False, 'read_only': False,
            'malformed_streams': False, 'deny_presets': False,
            'preset_autosave': True, 'active_preset': 'Lecture.cfg', 'preset_modified': False,
            'personality': 'SDI',
        },
        'controls': [
            {'type': 'select', 'key': 'stream_3_state', 'label': 'Lecture Stream', 'options': ['1', '2', '3', '4', '5', '6', '-2', '-3']},
            {'type': 'select', 'key': 'stream_11_state', 'label': 'Backup Stream', 'options': ['1', '2', '3', '-2']},
            {'type': 'toggle', 'key': 'stream_11_present', 'label': 'Backup Stream Configured'},
            {'type': 'select', 'key': 'decoder_0_stream', 'label': 'Decoder 0 Source', 'options': ['3', '11', '-1']},
            {'type': 'select', 'key': 'decoder_0_trouble', 'label': 'Decoder 0 Trouble', 'options': ['0', '-1', '-2', '-3', '-4']},
            {'type': 'toggle', 'key': 'video_present', 'label': 'Video Arriving'},
            {'type': 'toggle', 'key': 'audio_present', 'label': 'Audio Arriving'},
            {'type': 'toggle', 'key': 'srt_path2_present', 'label': 'Second SRT Path Reported'},
            {'type': 'select', 'key': 'srt_path2_state', 'label': 'Second SRT Path', 'options': ['ACTIVE', 'CONNECTING', 'FAILED']},
            {'type': 'toggle', 'key': 'omit_measurements', 'label': 'Omit Optional Readings'},
            {'type': 'toggle', 'key': 'expire_session', 'label': 'Expire Current Session'},
            {'type': 'toggle', 'key': 'refuse_login', 'label': 'Refuse Login'},
            {'type': 'toggle', 'key': 'read_only', 'label': 'Account Cannot Write'},
            {'type': 'toggle', 'key': 'deny_presets', 'label': 'Account Cannot Read Presets'},
            {'type': 'toggle', 'key': 'malformed_streams', 'label': 'Malformed Stream List'},
            {'type': 'select', 'key': 'personality', 'label': 'Output Personality', 'options': ['SDI', '2110']},
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config=config)
        self._sessions = set()
        self.login_count = 0
        self.request_count = 0
        self.writes = []
        for ident in range(4):
            defaults = {'stream': 3 if ident == 0 else -1, 'started': ident == 0,
                        'name': f'Decoder {ident}', 'outputs': [i == ident for i in range(4)],
                        'bufferingMode': 3, 'bufferingDelay': 0, 'multisyncBufferingDelay': 1000,
                        'stillImage': 1, 'stillImageDelay': 3, 'outputFrameRate': 0,
                        'quadMode': 0, 'dropCorruptedFrames': False}
            for key, value in defaults.items():
                self.set_state(f'decoder_{ident}_{key}', value)
        self._presets = {'Lecture.cfg': {'startup': True}, 'Backup.cfg': {'startup': False}}
        self._saved_states = {name: self._snapshot() for name in self._presets}
        self._saved_states['Backup.cfg']['decoder_0_stream'] = 11

    def _dispatch(self, method, path, data):
        reads = {
            '/apis/authentication': lambda: {'username': 'operator', 'uid': 500, 'gid': 511, 'passwordExp': 0},
            '/apis/status': self._status, '/apis/system_info': self._system_info,
            '/apis/streams': self._streams,
            '/apis/decoders': lambda: {'data': [self._decoder(i) for i in range(4)]},
        }
        if method == 'GET' and path in reads:
            return 200, reads[path]()
        if method != 'GET':
            self.writes.append((method, path, deepcopy(data)))
        if path.split('/')[2:3] == ['presets']:
            if self.state['deny_presets']:
                return 403, {}
            return self._preset_request(method, path, data)
        if path == '/apis/leds':
            return self._led_request(method, data)
        return self._object_request(method, path, data)

    def _status(self):
        return {'cardStatus': 'OK', 'serialNumber': 'SIM-MX4D-0001',
                'cardType': 'Makito X4 SDI Decoder', 'partNumber': 'S-MX4D-SDI',
                'firmwareVersion': '1.7.1', 'temperature': '52', 'cpu': 15, 'ram': 48, 'uptime': '0 days 19:44:14'}

    def _system_info(self):
        return {'videoDecoders': 4, 'audioDecoders': 4, 'hasSRT': True, 'hasHDR': True,
                'hasSrtRedundancy': True, 'personality': self.state['personality'], 'deviceName': 'Lecture Decoder'}

    def _streams(self):
        if self.state['malformed_streams']:
            return {'data': {'unexpected': True}}
        ids = [3, 11] if self.state['stream_11_present'] else [3]
        return {'data': [self._stream(i) for i in ids]}

    def _stream(self, ident):
        state = int(self.state[f'stream_{ident}_state'])
        assigned = [i for i in range(4) if int(self.state[f'decoder_{i}_stream']) == ident]
        info = {'id': ident, 'name': self.state['stream_3_name'] if ident == 3 else 'Remote Backup',
                'decoderId': assigned[0] if assigned else -1, 'encapsulation': 34 if ident == 3 else 2,
                'srtMode': 0, 'address': 'example.invalid', 'port': 9000, 'passphraseSet': False}
        stats = {'state': state, 'bitrateInKbps': 6128 if state == 3 else 0,
                 'receivedErrors': '0 (Last: Never)', 'droppedBytes': '1,557,545',
                 'lastReceived': '0s ago' if state == 3 else 'Never', 'streamSummary': '1 video, 2 audio'}
        if ident == 3:
            stats['srt'] = self._srt_path('ACTIVE' if state == 3 else 'CONNECTING')
            stats['srt']['detectedRedundancyMode'] = 'active'
            if self.state['srt_path2_present']:
                stats['srt']['path2'] = self._srt_path(self.state['srt_path2_state'])
        return {'info': info, 'stats': stats}

    def _srt_path(self, state):
        return {'state': state, 'rtt': '< 1 ms', 'latency': 250, 'buffer': 242,
                'lostPackets': '3,427', 'droppedPackets': '0', 'recoveredPackets': '3,427', 'decryptState': 'Secured'}

    def _decoder(self, ident):
        keys = ['bufferingMode', 'bufferingDelay', 'multisyncBufferingDelay', 'stillImage', 'stillImageDelay',
                'outputFrameRate', 'quadMode', 'dropCorruptedFrames', 'name', 'outputs']
        info = {key: self.state[f'decoder_{ident}_{key}'] for key in keys}
        source = int(self.state[f'decoder_{ident}_stream'])
        info.update(id=ident, streamId=source, altStreamId=-1, state=int(self.state[f'decoder_{ident}_started']))
        return {'info': info, 'stats': self._decoder_stats(ident, source)}

    def _decoder_stats(self, ident, source):
        started = self.state[f'decoder_{ident}_started']
        state = int(self.state.get(f'stream_{source}_state', 0))
        receiving = bool(started and state == 3)
        trouble = int(self.state.get(f'decoder_{ident}_trouble', 0))
        video = receiving and self.state['video_present'] and not trouble
        audio = receiving and self.state['audio_present']
        code = (2 if receiving else 1) if started else 0
        stats = {'id': ident, 'state': -1 if trouble else code, 'troubleCode': trouble, 'streamState': state,
                 'streamId': source if receiving else 0, 'decoderHasBeenStarted': started,
                 'videoDecoderState': 'ACTIVE' if video else 'STOPPED', 'audioState': 'ACTIVE' if audio else 'IDLE',
                 'stillImage': f'FREEZE ({"INACTIVE" if video else "ACTIVE"}) [Count=1]',
                 'videoInputResolution': '1920x1080p59.94' if video else '',
                 'videoDisplayResolution': '1920x1080p', 'videoFrameRate': '59', 'videoAlgorithm': 'H.265',
                 'videoLatency': '211 ms', 'multisyncStatus': 'WORKING', 'clockStatus': 'LOCKED (0.0PPM)',
                 'delayRangeMinMs': 100, 'delayRangeMaxMs': 3000,
                 'multisyncDelayRangeMinMs': 300, 'multisyncDelayRangeMaxMs': 10000,
                 'skippedOutputFrames': '0 [0.00%] (Last: Never)', 'replayedOutputFrames': '39 [0.00%]',
                 'corruptedFrames': '0 (Last: Never)', 'audioPairsAmount': 2 if audio else 0}
        if audio:
            stats['audioPairs'] = [{'compression': 'AAC-ADTS', 'outputLayoutDetailed': f'{i}L {i}R',
                                    'language': 'eng', 'dbLeft': '-23.2', 'dbRight': '-23.3',
                                    'avSyncMs': 15, 'decodeErrors': '0 (Last: Never)'} for i in (1, 2)]
        return self._reported_measurements(stats)

    def _reported_measurements(self, stats):
        if self.state['omit_measurements']:
            for key in ['videoDecoderState', 'stillImage', 'videoLatency', 'audioPairs']:
                stats.pop(key, None)
        return stats

    def _object_request(self, method, path, data):
        parts = path.strip('/').split('/')
        if not _object_path(parts):
            return 404, {}
        kind, ident = parts[1], int(parts[2])
        if kind == 'streams':
            return self._stream_request(method, ident)
        if kind != 'decoders' or not 0 <= ident < 4:
            return 404, {}
        if method == 'GET':
            return 200, {'data': self._decoder(ident)}
        if method != 'PUT':
            return 405, {}
        if len(parts) == 4:
            return self._decoder_operation(ident, parts[3])
        return self._update_decoder(ident, data)

    def _stream_request(self, method, ident):
        if method != 'GET' or ident not in (3, 11):
            return 404, {}
        return 200, {'data': self._stream(ident)}

    def _update_decoder(self, ident, data):
        for key, value in data.items():
            key = 'stream' if key == 'streamId' else key
            if f'decoder_{ident}_{key}' not in self.state:
                return 400, {}
            self.set_state(f'decoder_{ident}_{key}', value)
        self.set_state('preset_modified', True)
        return 200, {}

    def _decoder_operation(self, ident, operation):
        if operation not in ('start', 'stop'):
            return 404, {}
        self.set_state(f'decoder_{ident}_started', operation == 'start')
        self.set_state('preset_modified', True)
        return 200, self._decoder(ident)

    def _snapshot(self):
        return {key: deepcopy(value) for key, value in self.state.items() if key.startswith('decoder_')}


    def handle_request(self, method, path, headers, body):
        self.request_count += 1
        path = urlsplit(path).path
        if (method, path) == ('GET', '/apidoc/api_project.js'):
            return 200, 'define({"name":"Makito X4 Decoder","version":"1.7.0"});'
        if (method, path) == ('POST', '/apis/authentication'):
            return self._login(body)
        if not self._authenticated(headers):
            return 401, {}
        if self._write_refused(method):
            return 403, {}
        try:
            data = json.loads(body) if body else {}
            return self._dispatch(method, path, data)
        except (ValueError, TypeError, KeyError):
            return 400, {}

    def _write_refused(self, method):
        return method != 'GET' and self.state['read_only']

    def _login(self, body):
        self.login_count += 1
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            return 400, {}
        if not isinstance(data, dict):
            return 400, {}
        if self.state['refuse_login'] or any(data.get(k) in (None, '', 'invalid') for k in ('username', 'password')):
            return 401, {}
        token = secrets.token_hex(16)
        self._sessions.add(token)
        return 200, {'data': {'username': data['username'], 'uid': 500, 'gid': 511, 'passwordExp': False}}, {
            'Set-Cookie': f'SessionID={token}; Path=/; Secure; HttpOnly',
        }

    def _authenticated(self, headers):
        if self.state['expire_session']:
            self._sessions.clear()
            self.set_state('expire_session', False)
        cookie = next((v for k, v in headers.items() if k.lower() == 'cookie'), '')
        pairs = dict(part.strip().split('=', 1) for part in cookie.split(';') if '=' in part)
        return pairs.get('SessionID') in self._sessions

    def _preset_request(self, method, path, data):
        if path == '/apis/presets':
            return self._preset_collection(method, data)
        parts = path.removeprefix('/apis/presets/').split('/')
        name = unquote(parts[0])
        if name not in self._presets:
            return 404, {}
        if method != 'PUT':
            return 405, {}
        operation = parts[1] if len(parts) == 2 else 'save'
        handlers = {'load': self._load_preset, 'save': self._save_preset, 'startup': self._startup_preset}
        if operation not in handlers:
            return 404, {}
        handlers[operation](name, data)
        return 200, {'name': name, 'startup': self._presets[name]['startup']}

    def _preset_collection(self, method, data):
        if method == 'PUT' and isinstance(data.get('autosave'), bool):
            self.set_state('preset_autosave', data['autosave'])
            return 200, {'autosave': data['autosave'], 'message': 'Success'}
        if method != 'GET':
            return 405, {}
        return 200, {'data': [{'name': name, **row} for name, row in self._presets.items()],
                     'active': self.state['active_preset'], 'activeWasModified': self.state['preset_modified'],
                     'autosave': self.state['preset_autosave']}

    def _load_preset(self, name, data):
        for key, value in self._saved_states[name].items():
            self.set_state(key, value)
        self.set_state('active_preset', name)
        self.set_state('preset_modified', False)

    def _save_preset(self, name, data):
        self._saved_states[name] = deepcopy(self._snapshot())
        if not isinstance(data.get('startup'), bool):
            raise ValueError('startup')
        self._presets[name]['startup'] = data['startup']
        self.set_state('preset_modified', False)

    def _startup_preset(self, name, data):
        for key in self._presets:
            self._presets[key]['startup'] = key == name

    def _led_request(self, method, data):
        if method == 'POST':
            return (204, '') if 1 <= int(data.get('duration', 0)) <= 60 else (400, {})
        return (204, '') if method == 'DELETE' else (405, {})


def _object_path(parts):
    return len(parts) in (3, 4) and parts[2].isdigit()
