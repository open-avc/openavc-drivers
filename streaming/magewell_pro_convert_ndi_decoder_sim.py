"""Document-derived Magewell NDI decoder simulator (APIs v1.1 and v1.3).

Independent wire fixtures, not imports from the driver. Firmware/model variants
and fault controls exercise OpenAVC; capabilities are illustrative, not captures.
No control-state push is documented. HTTP and HTTPS use the same API.
"""
from copy import deepcopy
from email.parser import Parser
import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

_VIDEO = {
    'show-title': False, 'show-tally': False, 'show-vu-meter': False,
    'vu-meter-mode': 'dbu', 'show-center-cross': False, 'safe-area-mode': 'none',
    'ident-mode': 'none', 'ident-text': 'Display', 'h-flip': False, 'v-flip': False,
    'switch-mode': 'blank', 'reset-source-on-boot': False, 'deinterlace-mode': 'weave',
    'in-auto-color-fmt': True, 'in-color-fmt': 'bt.709', 'ar-convert-mode': 'windowbox',
    'alpha-disp-mode': 'alpha-blend-checkerboard', 'follow-input-mode': True,
}
_AUDIO = {'gain': 0.0, 'sample-rate': 0, 'channels': 2, 'bit-count': 0,
          'convert-mode': 'smpte', 'check-pts': True,
          'ch0': 0, 'ch1': 1, 'ch2': 2, 'ch3': 3, 'ch4': 4, 'ch5': 5, 'ch6': 6, 'ch7': 7,
          'ch8': 8, 'ch9': 9, 'ch10': 10, 'ch11': 11, 'ch12': 12, 'ch13': 13, 'ch14': 14, 'ch15': 15}
_NDI = {'enable-discovery': False, 'discovery-server': '', 'mcast-subnets': '',
        'group-name': 'public', 'low-bandwidth': False, 'enable-mcast': True,
        'enable-rudp': True, 'enable-tcp': True, 'enable-udp': True,
        'ignore-ndi-hx-video-pts': False, 'use-timecode-first': False}
_MODES = [
    {'width': 1920, 'height': 1080, 'interlaced': False, 'field-rate': 6000,
     'aspect-ratio': 1.77777779, 'pref-mode': True, 'curr-mode': True},
    {'width': 1280, 'height': 720, 'interlaced': False, 'field-rate': 5000,
     'aspect-ratio': 1.77777779, 'pref-mode': False, 'curr-mode': False},
    {'width': 1920, 'height': 1080, 'interlaced': True, 'field-rate': 5994,
     'aspect-ratio': 1.77777779, 'pref-mode': False, 'curr-mode': False},
]
_CHOICES = {'vu-meter-mode': ('none', 'dbu', 'dbvu', 'dbfs', 'post-gain-dbu', 'post-gain-dbvu', 'post-gain-dbfs'),
            'safe-area-mode': ('none', '4:3', '80%', 'square'),
            'ident-mode': ('none', 'ident-text', 'device-name'), 'switch-mode': ('blank', 'keep-last'),
            'deinterlace-mode': ('bob', 'weave'), 'in-color-fmt': ('rgb', 'bt.601', 'bt.709', 'bt.2020', 'bt.2020c'),
            'ar-convert-mode': ('windowbox', 'full', 'zoom'),
            'alpha-disp-mode': ('alpha-only', 'alpha-blend-white', 'alpha-blend-black', 'alpha-blend-checkerboard'),
            'color-format': ('rgb', 'yuv444', 'yuv422'), 'quant-range': ('limited', 'full'),
            'sample-rate': (0, 32000, 44100, 48000, 88200, 96000), 'channels': (0, 2, 4, 8),
            'convert-mode': ('smpte', 'ebu')}
_CONFIG_PATHS = {'video-config': 'video', 'audio-config': 'audio', 'ndi-config': 'ndi',
                 'playback-config': 'playback', 'video-format': 'format', 'hdmi-output': 'hdmi'}
# Synthetic display EDID, never represented as a real display capture.
_EDID = bytes.fromhex('00ffffffffffff00') + bytes(119) + bytes([6])


class MagewellProConvertNDIDecoderSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        'driver_id': 'magewell_pro_convert_ndi_decoder',
        'name': 'Magewell Pro Convert NDI Decoder',
        'transport': 'http',
        'initial_state': {'model': 'Pro Convert for NDI to HDMI', 'stream_connected': True,
                          'display_connected': True, 'sources_available': True, 'duplicate_names': False,
                          'tally_program': False, 'tally_preview': False, 'temperature': 48.0,
                          'video_dropped': 0, 'refuse_login': False, 'expire_session': False,
                          'deny_settings': False, 'ignore_write': False,
                          'audio.gain': 0.0, 'video.show-title': False},
        'controls': [
            {'type': 'toggle', 'key': 'stream_connected', 'label': 'Stream Connected'},
            {'type': 'toggle', 'key': 'display_connected', 'label': 'Display Connected'},
            {'type': 'toggle', 'key': 'sources_available', 'label': 'NDI Sources Available'},
            {'type': 'toggle', 'key': 'duplicate_names', 'label': 'Duplicate NDI Names'},
            {'type': 'toggle', 'key': 'tally_program', 'label': 'On Program'},
            {'type': 'toggle', 'key': 'tally_preview', 'label': 'On Preview'},
            {'type': 'slider', 'key': 'temperature', 'label': 'Temperature (°C)', 'min': 20, 'max': 95},
            {'type': 'slider', 'key': 'video_dropped', 'label': 'Dropped Video Frames/s', 'min': 0, 'max': 60},
            {'type': 'slider', 'key': 'audio.gain', 'label': 'Audio Gain (dB)', 'min': -100, 'max': 20},
            {'type': 'toggle', 'key': 'video.show-title', 'label': 'Source Overlay'},
            {'type': 'toggle', 'key': 'expire_session', 'label': 'Expire Sessions'},
            {'type': 'toggle', 'key': 'refuse_login', 'label': 'Refuse Login'},
            {'type': 'toggle', 'key': 'deny_settings', 'label': 'Deny Settings Permission'},
            {'type': 'toggle', 'key': 'ignore_write', 'label': 'Acknowledge without Applying Write'},
        ],
    }

    def __init__(self, device_id, config=None):
        config = dict(config or {})
        if config.get('ssl'):
            config['tls'] = True
        super().__init__(device_id, config=config)
        if self.config.get('model'):
            self.set_state('model', self.config['model'])
        self._legacy = 'HDMI 4K' in self.state['model'] or bool(self.config.get('legacy_login'))
        self._sessions = set()
        self._init_settings()
        self._presets = [{'name': 'Lectern Backup', 'url': 'srt://192.0.2.40:9000?mode=caller&latency=120', 'hotkey': 'ctrl+1'}]
        self._selection = {'name': 'Studio Camera', 'ndi-name': True}
        self._modes = self._initial_modes()
        self.requests = []
        self.login_count = 0

    def _init_settings(self):
        self._settings = {'video': deepcopy(_VIDEO), 'audio': deepcopy(_AUDIO), 'ndi': deepcopy(_NDI),
                          'playback': {'buffer-duration': 60}, 'format': {'color-format': 'rgb', 'quant-range': 'full'},
                          'hdmi': {'enabled': True}}
        if self._legacy:
            self._settings['video'].pop('reset-source-on-boot')
        for group, values in self._settings.items():
            for key, value in values.items():
                self.set_state(group + '.' + key, value)

    def _initial_modes(self):
        modes = deepcopy(_MODES)
        if '4K' in self.state['model']:
            modes.append({'width': 3840, 'height': 2160, 'interlaced': False, 'field-rate': 6000,
                          'aspect-ratio': 1.77777779, 'pref-mode': False, 'curr-mode': False})
        return modes

    async def respond_http(self, request, method, path, headers, body):
        # A source URL is itself a percent-encoded query value. aiohttp's
        # query_string has already decoded one layer; parse the actual request
        # target once, preserving %26 / %2B inside NDI names and passphrases.
        return await super().respond_http(request, method, request.raw_path, headers, body)

    def handle_request(self, method, path, headers, body):
        parts = urlsplit(path)
        if parts.path != '/mwapi':
            return 404, {'status': 16}, {}
        query = {key: values[-1] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
        operation = query.pop('method', '')
        self.requests.append((method, operation))
        if operation == 'ping':
            return 200, {'status': 0}, {}
        if operation == 'login':
            return self._login_request(method, query, body)
        if not self._has_session(headers):
            return 200, {'status': 37}, {}
        return self._authenticated_request(operation, query, (method, headers, body))

    def _authenticated_request(self, operation, query, request):
        method, headers, body = request
        if operation == 'import-channels' and method == 'POST':
            return 200, self._import_channels(headers, body), {}
        if method != 'GET':
            return 405, {'status': 9}, {}
        return 200, self._dispatch(operation, query), {}

    def _has_session(self, headers):
        if self.state['expire_session']:
            self._sessions.clear()
            self.set_state('expire_session', False)
        cookie = next((value for key, value in headers.items() if key.lower() == 'cookie'), '')
        return any('sid=' + session in cookie.split('; ') for session in self._sessions)

    def _login_request(self, method, query, body):
        if method == 'POST':
            if self._legacy:
                return 405, {'status': 9}, {}
            try:
                query = json.loads(body)
            except ValueError:
                return 200, {'status': 7}, {}
        self.login_count += 1
        if self._login_rejected(query):
            return 200, {'status': 36}, {}
        sid = secrets.token_hex(16)
        self._sessions.add(sid)
        return 200, {'status': 0}, {'Set-Cookie': f'sid={sid}; Path=/; HttpOnly'}

    def _login_rejected(self, query):
        rejected = self.state['refuse_login'] or query.get('id') == 'invalid'
        rejected = rejected or query.get('pass') == _digest('invalid')
        return rejected or not self._credentials_match(query)

    def _credentials_match(self, query):
        if not all(query.get(key) for key in ('id', 'pass')):
            return False
        expected = self.config.get('password')
        if expected and query.get('pass') != _digest(expected):
            return False
        return query.get('id') == self.config.get('username', query.get('id'))

    def _dispatch(self, operation, query):
        readers = {'get-summary-info': self._summary, 'get-signal-info': self._signal,
                   'get-caps': self._capabilities, 'get-ndi-sources': self._sources,
                   'get-supported-video-modes': self._get_modes, 'get-output-edid': self._output_edid}
        if operation in readers:
            return readers[operation]()
        handlers = {'list-channels': self._list_presets, 'get-channel': self._get_channel,
                    'set-channel': self._set_channel, 'add-channel': self._add_channel,
                    'modify-channel': self._modify_channel, 'del-channel': self._delete_channel,
                    'set-video-mode': self._set_mode, 'get-buffer-limit': self._buffer_limit,
                    'reset-video-config': self._reset_video}
        if operation in handlers:
            return handlers[operation](query)
        return self._config_request(operation, query)

    def _config_request(self, operation, query):
        verb, _, suffix = operation.partition('-')
        group = _CONFIG_PATHS.get(suffix)
        if group is None or verb not in ('get', 'set'):
            return {'status': 5}
        if self.state['deny_settings']:
            return {'status': 31}
        if group == 'hdmi' and 'SDI' in self.state['model']:
            return {'status': 9}
        if verb == 'get':
            return {'status': 0, **self._read(group)}
        return self._write(group, query)

    def _read(self, group):
        return {key: self.state.get(group + '.' + key, value) for key, value in self._settings[group].items()}

    def _write(self, group, query):
        try:
            values = {key: _coerce(value, self._settings[group][key]) for key, value in query.items()}
        except (KeyError, ValueError):
            return {'status': 7}
        if not all(_valid_value(key, value) for key, value in values.items()):
            return {'status': 27}
        if not self.state['ignore_write']:
            for key, value in values.items():
                self.set_state(group + '.' + key, value)
        return {'status': 0}

    def _capabilities(self):
        hdmi = 'SDI' not in self.state['model']
        return {'status': 0, 'has-input': False, 'has-output': True, 'has-loop-through': False,
                'has-output-edid': hdmi, 'has-output-state': hdmi, 'has-input-edid': False,
                'has-ndi': True, 'has-ptz': False, 'has-fan': False, 'has-sdcard': False,
                'has-alpha-disp-mode': '4K' in self.state['model'],
                'max-output-width': 4096 if '4K' in self.state['model'] else 1920,
                'max-output-height': 2160 if '4K' in self.state['model'] else 1080}

    def _summary(self):
        connected = bool(self.state['stream_connected'])
        device = {'name': 'Pro Convert SIM0001', 'model': self.state['model'].removeprefix('Pro Convert for '),
                  'serial-no': 'SIM0001', 'fw-version': '1.1.1059' if self._legacy else '1.3.26',
                  'hw-revision': 'B', 'cpu-usage': 15.5, 'memory-usage': 58.3,
                  'core-temp': float(self.state['temperature']), 'up-time': 8006, 'board-id': 0}
        if self._capabilities()['has-output-state']:
            device['output-state'] = 'connected' if self.state['display_connected'] else 'unconnected'
        return {'status': 0, 'device': device,
                'ethernet': {'state': '1000m', 'mac-addr': '70:B3:D5:75:D0:01', 'ip-addr': '192.0.2.1',
                             'tx-speed-kbps': 50, 'rx-speed-kbps': 80000 if connected else 0},
                'ndi': {'name': self._selection['name'], 'url': '', 'connected': connected,
                        'tally-preview': bool(self.state['tally_preview']), 'tally-program': bool(self.state['tally_program']),
                        'audio-drop-frames': 0, 'video-drop-frames': int(self.state['video_dropped']),
                        'video-bit-rate': 80000 if connected else 0, 'audio-bit-rate': 1536 if connected else 0}}

    def _signal(self):
        if not self.state['stream_connected']:
            return {'status': 0, 'signal-info-types': []}
        return {'status': 0, 'signal-info-types': ['video-info', 'audio-info'],
                'video-info': {'codec': 'shq2', 'width': 1920, 'height': 1080, 'scan': 'progressive',
                               'field-rate': 60.0, 'color-depth': 8, 'color-format': 'bt.709', 'sampling': '4:2:2'},
                'audio-info': {'codec': 'ndi', 'num-channels': 8, 'sample-rate': 48000, 'bit-count': 16}}

    def _sources(self):
        rows = [{'ndi-name': 'Studio Camera', 'ip-addr': '192.0.2.10:5961'},
                {'ndi-name': 'Lectern & Slides', 'ip-addr': '192.0.2.11:5962'}]
        if self.state['duplicate_names']:
            rows.append({'ndi-name': 'Studio Camera', 'ip-addr': '192.0.2.12:5961'})
        return {'status': 0, 'sources': rows if self.state['sources_available'] else []}

    def _list_presets(self, query):
        return {'status': 0, 'channels': deepcopy(self._presets)}

    def _get_channel(self, query):
        return {'status': 0, **self._selection}

    def _set_channel(self, query):
        ndi = query.get('ndi-name') == 'true'
        names = [row['ndi-name'] for row in self._sources()['sources']] if ndi else [row['name'] for row in self._presets]
        if query.get('name') not in names:
            return {'status': 16}
        if not self.state['ignore_write']:
            self._selection = {'name': query['name'], 'ndi-name': ndi}
        return {'status': 0}

    def _add_channel(self, query):
        if not _valid_preset(query):
            return {'status': 7}
        if any(row['name'] == query['name'] for row in self._presets):
            return {'status': 7}
        if not self.state['ignore_write']:
            self._presets.append({'name': query['name'], 'url': query['url'], 'hotkey': query.get('hotkey', 'none')})
        return {'status': 0}

    def _modify_channel(self, query):
        for row in self._presets:
            if row['name'] == query.get('name'):
                updated = dict(row, name=query.get('new-name', ''), url=query.get('url', ''))
                if 'hotkey' in query:
                    updated['hotkey'] = query['hotkey']
                if not _valid_preset(updated):
                    return {'status': 7}
                if not self.state['ignore_write']:
                    row.update(updated)
                return {'status': 0}
        return {'status': 16}

    def _delete_channel(self, query):
        if not self.state['ignore_write']:
            self._presets = [row for row in self._presets if row['name'] != query.get('name')]
        return {'status': 0}

    def _get_modes(self):
        return {'status': 0, 'modes': deepcopy(self._modes) if self.state['display_connected'] else []}

    def _set_mode(self, query):
        fields = ('width', 'height', 'interlaced', 'field-rate', 'aspect-ratio')
        matches = [mode for mode in self._modes if query == {key: _text(mode[key]) for key in fields}]
        if not matches or not self.state['display_connected']:
            return {'status': 7}
        if not self.state['ignore_write']:
            self._mark_mode(matches[0])
        return {'status': 0, **matches[0]}

    def _mark_mode(self, selected):
        for row in self._modes:
            row['curr-mode'] = row is selected

    def _buffer_limit(self, query):
        if query.get('proto') not in ('ntkndi', 'rtsp', 'http', 'rtmp', 'udp', 'srt', 'rtp'):
            return {'status': 9}
        return {'status': 0, 'buffer-duration-def': 60, 'buffer-duration-min': 20, 'buffer-duration-max': 120}

    def _output_edid(self):
        if not self._capabilities()['has-output-edid'] or not self.state['display_connected']:
            return {'status': 9}
        return {'status': 0, 'data': base64.b64encode(_EDID).decode()}

    def _reset_video(self, query):
        if self.state['deny_settings']:
            return {'status': 31}
        defaults = self._settings['video']
        return self._write('video', {key: _text(value) for key, value in defaults.items()})

    def _import_channels(self, headers, body):
        if self._legacy:
            return {'status': 9}
        if self.state['deny_settings']:
            return {'status': 31}
        try:
            rows = _import_rows(headers, body)
        except (ValueError, KeyError, TypeError):
            return {'status': 7}
        self._merge_presets(rows)
        return {'status': 0, 'channels': deepcopy(self._presets)}

    def _merge_presets(self, rows):
        for row in rows:
            if not _valid_preset(row):
                continue  # v1.3 explicitly skips invalid rows.
            existing = next((item for item in self._presets if item['name'] == row['name']), None)
            if self.state['ignore_write']:
                continue
            if existing is not None:
                existing.update(row)
            else:
                self._presets.append(dict(row))


def _import_rows(headers, body):
    content_type = next((v for k, v in headers.items() if k.lower() == 'content-type'), '')
    message = Parser().parsestr('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n' + body)
    for part in message.walk():
        if part.get_param('name', header='content-disposition') == 'file':
            return json.loads(part.get_payload(decode=True))['channels']
    raise ValueError('No file')


def _digest(value):
    return hashlib.md5(str(value).encode(), usedforsecurity=False).hexdigest()


def _text(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def _coerce(value, prototype):
    if isinstance(prototype, bool):
        if value not in ('true', 'false'):
            raise ValueError('boolean')
        return value == 'true'
    if isinstance(prototype, (float, int)):
        return type(prototype)(value)
    return value


def _valid_value(key, value):
    ranges = {'gain': (-100, 20), 'buffer-duration': (20, 120)}
    if key.startswith('ch') and key[2:].isdigit():
        return 0 <= value <= 15
    if key in ranges:
        low, high = ranges[key]
        return low <= value <= high
    return key not in _CHOICES or value in _CHOICES[key]


def _valid_preset(row):
    if not isinstance(row, dict):
        return False
    if not all(isinstance(row.get(key), str) and row[key] for key in ('name', 'url')):
        return False
    return 'hotkey' not in row or bool(re.fullmatch(r'(?:none|ctrl\+[a-z0-9])', row['hotkey']))
