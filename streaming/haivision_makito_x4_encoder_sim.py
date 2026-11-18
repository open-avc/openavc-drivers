"""Makito X4 REST simulator from the manufacturer's 1.10.0 endpoint reference.

The object IDs and values are illustrative. Reply layouts, status codes, SRT
microsecond units and the unimplemented recording-list endpoint follow the
published API. Controls allow out-of-band changes, a removed stream, malformed
lists, session expiry and account refusal. No data is a physical-unit capture.
"""

from copy import deepcopy
import json
import secrets
from urllib.parse import unquote, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator


class HaivisionMakitoX4EncoderSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        'driver_id': 'haivision_makito_x4_encoder', 'name': 'Haivision Makito X4 Encoder',
        'category': 'streaming', 'transport': 'http', 'default_port': 443, 'tls': True,
        'initial_state': {
            'signal_present': True, 'stream_3_state': 1, 'stream_11_state': 0,
            'stream_11_present': True, 'stream_3_name': 'Lecture Hall',
            'audio_muted': False, 'recording_status': 'READY', 'recording_enabled': True,
            'srt_rtt_us': 240, 'srt_path2_present': True, 'srt_path2_state': 1,
            'srt_lost_packets': 3427, 'srt_dropped_packets': 0,
            'refuse_login': False, 'expire_session': False, 'read_only': False,
            'malformed_streams': False, 'deny_preview': False,
            'preset_autosave': True, 'active_preset': 'Lecture.cfg', 'preset_modified': False,
            'preview_enabled': True, 'preview_interval': 6,
        },
        'controls': [
            {'type': 'toggle', 'key': 'signal_present', 'label': 'Input Signal'},
            {'type': 'select', 'key': 'stream_3_state', 'label': 'Lecture Stream',
             'options': ['0', '1', '2', '3', '5', '6', '8']},
            {'type': 'select', 'key': 'stream_11_state', 'label': 'Backup Stream', 'options': ['0', '1', '2']},
            {'type': 'toggle', 'key': 'stream_11_present', 'label': 'Backup Stream Configured'},
            {'type': 'toggle', 'key': 'audio_muted', 'label': 'Audio Muted'},
            {'type': 'select', 'key': 'recording_status', 'label': 'Recording Status',
             'options': ['READY', 'INITIALIZING', 'RECORDING', 'STOPPED', 'HALTED', 'PAUSED', 'CLOSING', 'FINISHED']},
            {'type': 'toggle', 'key': 'recording_enabled', 'label': 'Recording Configured'},
            {'type': 'slider', 'key': 'srt_rtt_us', 'label': 'SRT Round-Trip Time (microseconds)', 'min': 0, 'max': 500000},
            {'type': 'toggle', 'key': 'srt_path2_present', 'label': 'SRT Second Path Reported'},
            {'type': 'select', 'key': 'srt_path2_state', 'label': 'SRT Second Path', 'options': ['0', '1', '2', '3', '5']},
            {'type': 'toggle', 'key': 'expire_session', 'label': 'Expire Current Session'},
            {'type': 'toggle', 'key': 'refuse_login', 'label': 'Refuse Login'},
            {'type': 'toggle', 'key': 'read_only', 'label': 'Account Cannot Write'},
            {'type': 'toggle', 'key': 'deny_preview', 'label': 'Account Cannot Read Preview Settings'},
            {'type': 'toggle', 'key': 'malformed_streams', 'label': 'Malformed Stream List'},
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config=config)
        self._sessions = set()
        self.login_count = 0
        self.request_count = 0
        self._inputs = 1 if self.config.get('simulated_model', 'Quad') == 'Single' else 4
        self._storage = bool(self.config.get('simulated_storage', False))
        # The fixture reports its own encoder counts; the driver never derives
        # those counts from the number of physical inputs.
        self._video_count = 2 if self._inputs == 1 else 8
        self._audio_count = 32
        for ident in range(self._video_count):
            self.set_state(f'video_{ident}_state', 7)
        for ident in range(self._audio_count):
            self.set_state(f'audio_{ident}_state', 3)
        self._presets = {'Lecture.cfg': {'startup': True}, 'Backup.cfg': {'startup': False}}
        self._saved_states = {name: self._snapshot() for name in self._presets}
        self._saved_states['Backup.cfg'].update(stream_3_state=0, stream_11_state=1)

    def handle_request(self, method, path, headers, body):
        self.request_count += 1
        path = urlsplit(path).path
        if (method, path) == ('GET', '/apidoc/api_project.js'):
            return 200, 'define({"name":"Makito X4 Encoder","version":"1.10.0"});'
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

    def _dispatch(self, method, path, data):
        reads = {
            '/apis/authentication': lambda: {'username': 'operator', 'uid': 500, 'gid': 511, 'passwordExp': False},
            '/apis/status': self._status, '/apis/system_info': self._system_info,
            '/apis/vidin': self._video_inputs,
            '/apis/videnc': lambda: {'data': [self._video(i) for i in range(self._video_count)]},
            '/apis/audenc': lambda: {'data': [self._audio(i) for i in range(self._audio_count)]},
            '/apis/streams': self._streams,
        }
        if method == 'GET' and path in reads:
            return 200, reads[path]()
        if path.split('/')[2:3] == ['presets']:
            return self._preset_request(method, path, data)
        handlers = {'/apis/services/preview': self._preview_request, '/apis/leds': self._led_request}
        if path in handlers:
            return handlers[path](method, data)
        if path == '/apis/recordings':
            return 501, 'Use /apis/streams to retrieve recording info from the stream->info->recording structure'
        return self._object_request(method, path)

    def _status(self):
        return {'cardStatus': 'OK', 'serialNumber': 'SIM-MX4E-0001',
                'cardType': 'Makito X4 SDI Encoder', 'partNumber': 'B-MX4E-SDI1-UHD' if self._inputs == 1 else 'B-MX4E-SDI4',
                'firmwareVersion': '1.10.0', 'temperature': '53', 'chipsetLoad': '63', 'uptime': '0 days 19:44:14'}

    def _system_info(self):
        return {'videoInputs': self._inputs, 'audioInputs': self._inputs, 'videoEncoders': self._video_count,
                'audioEncoders': self._audio_count, 'storage': self._storage, 'rugged': False, 'hevc': True,
                'personality': 'SDI', 'deviceName': 'Lecture Encoder'}

    def _video_inputs(self):
        return {'data': [{'id': i, 'name': f'BNC-{i + 1}', 'interfaceType': 1, 'state': 'ACTIVE'}
                         for i in range(self._inputs)]}

    def _video(self, ident):
        state = int(self.state[f'video_{ident}_state'])
        signal = self.state['signal_present']
        return {'info': {'id': ident, 'name': f'Video Encoder {ident}', 'interface': ident % self._inputs,
                         'codecAlgorithm': 1, 'bitrate': 6000, 'resolution': '0'},
                'stats': {'state': state if signal or state == 0 else 3, 'inputPresent': signal,
                          'inputFormat': '1920x1080p30' if signal else '', 'resolution': '1920x1080p',
                          'encodedBitRate': '6,000' if state == 7 and signal else '0', 'droppedFrames': '0'}}

    def _audio(self, ident):
        state = int(self.state[f'audio_{ident}_state'])
        if state == 3 and self.state['audio_muted']:
            state = 67
        return {'info': {'id': ident, 'name': f'Audio Encoder {ident}', 'interface': ident % (8 * self._inputs) + 1,
                         'algorithm': 10, 'mode': 0, 'bitRate': 128},
                'stats': {'state': state, 'encodedBitrate': '128' if state in (3, 67) else '0',
                          'maxSampleValuePercentage': 14 if state == 3 else 0, 'encoderErrors': '0'}}

    def _streams(self):
        if self.state['malformed_streams']:
            return {'data': {'unexpected': True}}
        ids = [3, 11] if self.state['stream_11_present'] else [3]
        return {'data': [self._stream(i) for i in ids]}

    def _stream(self, ident):
        state = int(self.state[f'stream_{ident}_state'])
        info = {'id': ident, 'name': self.state['stream_3_name'] if ident == 3 else 'Remote Backup',
                'type': 1, 'encapsulation': 34 if ident == 3 else 2, 'srtMode': 1,
                'video': [{'id': 0, 'name': 'Video Encoder 0'}], 'audio': [{'id': 0, 'name': 'Audio Encoder 0'}]}
        stats = {'state': state, 'stateString': _STREAM_STATES.get(state, 'UNKNOWN'),
                 'bitrateInKbps': 6128 if state == 1 else 0, 'uptime': '19s', 'unsentPackets': '0'}
        if ident == 3:
            stats['srt'] = self._srt_path(state)
            if self.state['srt_path2_present']:
                stats['srt']['path2'] = self._srt_path(int(self.state['srt_path2_state']))
            if self._storage:
                info['recording'] = self._recording()
        return {'info': info, 'stats': stats}

    def _srt_path(self, state):
        return {'state': state, 'rtt': self.state['srt_rtt_us'], 'latency': 250,
                'lostPackets': str(self.state['srt_lost_packets']), 'droppedPackets': str(self.state['srt_dropped_packets']),
                'resentPackets': '3,427'}

    def _recording(self):
        if not self.state['recording_enabled']:
            return {'enabled': False}
        status = self.state['recording_status']
        if status == 'RECORDING' and not self.state['signal_present']:
            status = 'PAUSED'
        return {'enabled': True, 'id': 3, 'name': 'Lecture Recording', 'status': status,
                'upTimeClock': '00:00:12', 'upTime': '12s', 'bytesStored': '12,345', 'percentageDone': 5,
                'autoStart': False, 'filename': '/media/lecture.ts', 'format': 'ts', 'queueOverflows': '0'}

    def _object_request(self, method, path):
        parts = path.strip('/').split('/')
        if len(parts) not in (3, 4) or not parts[2].isdigit():
            return 404, {}
        collection, ident = parts[1], int(parts[2])
        readers = {'videnc': self._video, 'audenc': self._audio, 'streams': self._stream,
                   'recordings': lambda unused: {'recording': self._recording()}}
        if not self._known_object(collection, ident, readers):
            return 404, {}
        if method == 'GET' and len(parts) == 3:
            return 200, readers[collection](ident)
        if not _is_control_request(method, parts):
            return 405, {}
        self._set_running(collection, ident, parts[3] == 'start')
        if collection == 'recordings':
            return 200, {'data': {'recording': {'enabled': True}}}
        return 200, readers[collection](ident)

    def _known_object(self, collection, ident, readers):
        return collection in readers and self._object_exists(collection, ident)

    def _object_exists(self, collection, ident):
        limits = {'videnc': self._video_count, 'audenc': self._audio_count}
        if collection in limits:
            return 0 <= ident < limits[collection]
        if collection == 'streams':
            return ident == 3 or (ident == 11 and self.state['stream_11_present'])
        return self._storage and ident == 3 and self.state['recording_enabled']

    def _set_running(self, collection, ident, started):
        if collection == 'recordings':
            self.set_state('recording_status', 'RECORDING' if started else 'STOPPED')
            return
        prefix, running_code = {'videnc': ('video', 7), 'audenc': ('audio', 3), 'streams': ('stream', 1)}[collection]
        self.set_state(f'{prefix}_{ident}_state', running_code if started else 0)
        self.set_state('preset_modified', True)

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

    def _snapshot(self):
        return {key: value for key, value in self.state.items() if key.endswith('_state')}

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

    def _preview_request(self, method, data):
        if self.state['deny_preview']:
            return 403, {}
        if method == 'PUT':
            if not isinstance(data.get('enabled'), bool) or not 1 <= int(data.get('interval', 0)) <= 60:
                return 400, {}
            self.set_state('preview_enabled', data['enabled'])
            self.set_state('preview_interval', int(data['interval']))
        elif method != 'GET':
            return 405, {}
        return 200, {'enabled': self.state['preview_enabled'], 'interval': self.state['preview_interval'], 'width': 320, 'height': 180}

    def _led_request(self, method, data):
        if method == 'POST':
            return (204, '') if 1 <= int(data.get('duration', 0)) <= 60 else (400, {})
        return (204, '') if method == 'DELETE' else (405, {})


_STREAM_STATES = {0: 'STOPPED', 1: 'STREAMING', 2: 'FAILED', 3: 'CONNECTING', 4: 'SECURING',
                  5: 'LISTENING', 6: 'PAUSED', 7: 'PUBLISHING', 8: 'RESOLVING', 9: 'SCRAMBLED'}


def _is_control_request(method, parts):
    if method != 'PUT' or len(parts) != 4:
        return False
    return parts[3] in ('start', 'stop')
