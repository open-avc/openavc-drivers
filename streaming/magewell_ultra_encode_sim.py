"""Ultra Encode HTTP simulator, derived from Magewell's Plus/AIO API documents.

Models POST JSON login with an MD5 password, the documented session cookie,
expired sessions, permission errors, global live/record controls and independent
channel enables. Sparse example IDs exercise roster identity. Simulator values
are examples, not captures from hardware. The default model is AIO; the driver's
model setting chooses another member for simulation. Controls change the same
state that API reads return, including a front-panel change while connected.
"""

from copy import deepcopy
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator


_VIDEO = {'is-auto': 0, 'codec': 0, 'cx': 1920, 'cy': 1080, 'duration': 333333,
          'kbps': 8192, 'gop': 60, 'fourcc': 0, 'profile': 2, 'cbrstat': 60,
          'fullrange': 0, 'is-vbr': 0, 'min-vbr-qp': 0, 'max-vbr-qp': 51,
          'is-time-code-sei': 0, 'is-closed-caption-sei': 0, 'ar-convert-mode': 2,
          'rotation': 0}
_FLAGS = {'enable-deinterlace': 'deinterlace', 'set-enable-stream1': 'sub_enabled',
          'enable-ndi-hx3': 'ndi_hx3', 'use-nosignal-file': 'no_signal_image',
          'set-analog-mic-bias': 'microphone_bias'}
_FLAG_PARAMS = {'enable-deinterlace': 'enable-deinterlace', 'set-enable-stream1': 'enable',
                'enable-ndi-hx3': 'enable-ndi-hx3', 'use-nosignal-file': 'use-nosignal-file',
                'set-analog-mic-bias': 'enable-mic-bias'}
_GLOBALS = {'start-live': ('streaming', True), 'stop-live': ('streaming', False),
            'start-rec': ('recording', True), 'stop-rec': ('recording', False),
            'clear-live': ('streaming', False), 'clear-rec': ('recording', False)}


def _choice(value, name):
    return {'value': value, 'name': name}


class MagewellUltraEncodeSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        'driver_id': 'magewell_ultra_encode', 'name': 'Magewell Ultra Encode Simulator',
        'category': 'streaming', 'transport': 'http', 'default_port': 80,
        'initial_state': {
            'model': 'Ultra Encode AIO', 'device_name': 'Ultra Encode Simulator',
            'streaming': False, 'recording': False, 'signal_present': True,
            'stream_0_enabled': True, 'stream_7_enabled': True,
            'rec_1_enabled': True, 'rec_6_enabled': False,
            'spi_mute': False, 'spi_gain': 0, 'linein_mute': False, 'linein_gain': 0,
            'lineout_mute': False, 'lineout_gain': 0,
            'input_source': 2, 'audio_offset': 0, 'deinterlace': True,
            'sub_enabled': True, 'ndi_hx3': False, 'no_signal_image': True,
            'microphone_bias': False, 'main_overlays': True, 'sub_overlays': False,
            'overlay_enabled': True, 'schedule_0_enabled': True,
            'schedule_1_enabled': False, 'schedule_2_enabled': False,
            'auto_record': False, 'auto_record_trigger': 0,
            'refuse_login': False, 'expire_session': False, 'deny_overlays': False,
        },
        'controls': [
            {'type': 'toggle', 'key': 'streaming', 'label': 'Live Function'},
            {'type': 'toggle', 'key': 'recording', 'label': 'Record Function'},
            {'type': 'toggle', 'key': 'signal_present', 'label': 'Input Signal'},
            {'type': 'toggle', 'key': 'stream_0_enabled', 'label': 'NDI Destination Enabled'},
            {'type': 'toggle', 'key': 'stream_7_enabled', 'label': 'RTSP Destination Enabled'},
            {'type': 'toggle', 'key': 'rec_1_enabled', 'label': 'SD Recorder Enabled'},
            {'type': 'toggle', 'key': 'spi_mute', 'label': 'HDMI / SDI Audio Mute'},
            {'type': 'slider', 'key': 'spi_gain', 'label': 'Native Input Gain', 'min': -10000, 'max': 600},
            {'type': 'toggle', 'key': 'expire_session', 'label': 'Expire Current Sessions'},
            {'type': 'toggle', 'key': 'refuse_login', 'label': 'Refuse Login'},
            {'type': 'toggle', 'key': 'deny_overlays', 'label': 'Deny Overlay Access'},
        ],
    }

    def __init__(self, device_id, config=None):
        config = dict(config or {})
        if config.get('ssl'):
            config['tls'] = True
        super().__init__(device_id, config=config)
        self._sessions = set()
        self._video_settings = [deepcopy(_VIDEO), {**_VIDEO, 'kbps': 4096}]
        self._input_mixer = {'input-device': 2, 'is-hdmi-top': 1, 'type': 0, 'location': 2}
        self._selected_image = 1
        self.login_count = 0
        self.request_count = 0
        self.last_login = None
        if self.config.get('model'):
            self.set_state('model', self.config['model'])

    def _flag(self, key):
        return int(bool(self.state.get(key)))

    def _is_aio(self):
        return self.state['model'] == 'Ultra Encode AIO'

    def handle_request(self, method, path, headers, body):
        self.request_count += 1
        url = urlsplit(path)
        if url.path != '/usapi':
            return 404, {'result': -14}, {}
        params = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        action = params.pop('method', '')
        if action == 'login':
            return self._login(method, params, body)
        if action == 'ping':
            return 200, {'result': 0, 'micro-sec': 1234567000, 'cur-status': 65536}, {}
        if not self._authenticated(headers):
            return 200, {'result': -17}, {}
        if method != 'GET':
            return 405, {'result': -10}, {}
        try:
            result = self._dispatch(action, params)
        except (KeyError, ValueError, TypeError):
            result = {'result': -10}
        return 200, result, {}

    def _login(self, method, params, body):
        if method == 'POST':
            try:
                params = json.loads(body)
            except (ValueError, TypeError):
                return 400, {'result': -10}, {}
        elif method != 'GET':
            return 405, {'result': -10}, {}
        if not isinstance(params, dict):
            return 400, {'result': -10}, {}
        self.login_count += 1
        self.last_login = {'method': method, 'body': dict(params)}
        if self._login_refused(params):
            return 200, {'result': 36}, {}
        sid = secrets.token_hex(16)
        self._sessions.add(sid)
        return 200, {'result': 0, 'sid': sid, 'type': 1, 'theme': 0}, {'Set-Cookie': f'sid={sid}; Path=/; HttpOnly'}

    def _login_refused(self, params):
        password = self.config.get('password', '')
        expected = hashlib.md5(str(password).encode()).hexdigest() if password else None
        invalid = hashlib.md5(b'invalid').hexdigest()
        refused = self.state['refuse_login'] or params.get('pass') == invalid
        refused = refused or (expected is not None and params.get('pass') != expected)
        refused = refused or params.get('id') in (None, '', 'invalid')
        return refused

    def _authenticated(self, headers):
        if self.state['expire_session']:
            self._sessions.clear()
            self.set_state('expire_session', False)
        cookie = next((v for k, v in headers.items() if k.lower() == 'cookie'), '')
        pairs = dict(piece.strip().split('=', 1) for piece in cookie.split(';') if '=' in piece)
        return pairs.get('sid') in self._sessions

    def _dispatch(self, method, params):
        reads = {'get-info': self._info, 'get-status': self._status, 'get-settings': self._settings,
                 'get-rec-channels': self._recorders, 'get-surfaces': self._overlays}
        if method in reads:
            return reads[method]()
        if method == 'get-schedulers':
            return self._schedules(int(params['chn-type']))
        if method in _GLOBALS:
            key, value = _GLOBALS[method]
            self.set_state(key, value)
            return {'result': 0}
        if method in _FLAGS:
            self.set_state(_FLAGS[method], bool(int(params[_FLAG_PARAMS[method]])))
            return {'result': 0}
        writes = {'set-volume': self._volume, 'set-video': self._video,
                  'enable-server': self._enable_stream, 'enable-rec-channel': self._enable_rec,
                  'enable-surface': self._enable_overlay, 'enable-scheduler': self._enable_schedule,
                  'set-enable-surface': self._overlay_master, 'set-auto-rec': self._auto_record,
                  'set-name': self._name, 'set-sync-offset': self._offset,
                  'select-input-source': self._input, 'set-video-mixer-config': self._mixer,
                  'set-nosignal-file': self._image, 'reboot': self._reboot,
                  'reset-all-settings': self._reset}
        handler = writes.get(method)
        return handler(params) if handler else {'result': -14}

    def _info(self):
        inputs = [_choice(1, 'SDI'), _choice(2, 'HDMI'), _choice(3, 'Automatic'), _choice(4, 'Mixer')] if self._is_aio() else []
        return {'result': 0,
                'product': {'module-name': self.state['model'], 'sn': 'SIM-ULTRA-001',
                            'firmware-ver-s': '2.5.62', 'record-support': 1, 'scheduler-support': 1},
                'input-source': {'sources': inputs, 'video-mixer': {
                    'pip': [_choice(i, f'Corner {i}') for i in range(1, 5)],
                    'sbs': [_choice(i, f'Layout {i}') for i in range(1, 6)]}},
                'audio-range': {'spi': {'min': -10000, 'max': 600},
                                'linein': {'min': -13900, 'max': 5525},
                                'lineout': {'min': -5700, 'max': 600}},
                'codec-cap': {'main-stream': {'max-video-kbps': 32768},
                              'sub-stream': {'max-video-kbps': 32768},
                              'durations': [_choice(333333, '30 FPS'), _choice(400000, '25 FPS')],
                              'video-codec': [_choice(0, 'H.264'), _choice(1, 'HEVC')],
                              'profile': [_choice(0, 'Baseline'), _choice(1, 'Main'), _choice(2, 'High')],
                              'hevc-profile': [_choice(0, 'Main')],
                              'gop-sec': [_choice(i, f'{i} sec') for i in (1, 2, 5, 10, 30, 60)],
                              'video-rotation': [_choice(i, str(i)) for i in (0, 90, 180, 270)]},
                'rec': {'trigger-mode': [_choice(0, 'Power On'), _choice(1, 'Signal Detected')]}}

    def _stream_rows(self):
        return [{'id': 0, 'type': 130, 'name': 'NDI Program', 'is-use': self._flag('stream_0_enabled')},
                {'id': 7, 'type': 100, 'name': 'RTSP Preview', 'is-use': self._flag('stream_7_enabled'),
                 'port': 554, 'is-main': 1, 'main-stream-name': 'main', 'is-auth': 0}]

    def _recorders(self):
        return {'result': 0, 'rec-channels': [
            {'id': 1, 'type': 1, 'is-use': self._flag('rec_1_enabled'), 'stream-index': 0},
            {'id': 6, 'type': 2, 'is-use': self._flag('rec_6_enabled'), 'stream-index': 1}]}

    def _settings(self):
        volume = {'enable-mic-bias': self._flag('microphone_bias')}
        for name in ('spi', 'linein', 'lineout'):
            volume['is-'+name] = int(not self.state[name+'_mute'])
            volume[name+'-gain'] = int(self.state[name+'_gain'])
        return {'result': 0, 'name': self.state['device_name'], 'volume': volume,
                'audio-sync-offset': int(self.state['audio_offset']),
                'enable-deinterlace': self._flag('deinterlace'), 'enable-ndi-hx3': self._flag('ndi_hx3'),
                'use-nosignal-file': self._flag('no_signal_image'),
                'input-source': {'source': self.state['input_source'], 'mixer': self._input_mixer},
                'main-stream': dict(self._video_settings[0]),
                'sub-stream': {**self._video_settings[1], 'enable': self._flag('sub_enabled')},
                'stream-server': self._stream_rows(),
                'rec': {'is-auto': self._flag('auto_record'), 'trigger-mode': self.state['auto_record_trigger']},
                'nosignal-files': [{'id': i, 'is-use': int(i == self._selected_image)} for i in (0, 1, 2)]}

    def _status(self):
        live = [self._channel_status(row, 'streaming', 22) for row in self._stream_rows()]
        rec = [self._channel_status(row, 'recording', 2) for row in self._recorders()['rec-channels']]
        signal = self.state['signal_present']
        return {'result': 0, 'cur-status': 65536, 'box-name': self.state['device_name'],
                'input-source': self.state['input_source'],
                'input-signal': {'status': 0 if signal else -35, 'cx': 1920 if signal else 0,
                                 'cy': 1080 if signal else 0, 'frame-rate': 30.0 if signal else 0},
                'live-status': {'live': live}, 'rec-status': {'rec': rec}}

    def _channel_status(self, row, master, running):
        active = bool(row['is-use'] and self.state[master])
        code = running if active else 27
        if active and not self.state['signal_present']:
            code = -35
        result = {**row, 'result': code, 'run-ms': 1000 if code == running else 0}
        if master == 'streaming':
            result.update({'main-inst-bps': 8192000 if code == running else 0,
                           'sub-inst-bps': 4096000 if code == running else 0})
        else:
            result.update({'video-frame-count': 30 if code == running else 0,
                           'audio-frame-count': 47 if code == running else 0, 'parted-num': 1})
        return result

    def _overlays(self):
        if self.state['deny_overlays']:
            return {'result': -26}
        return {'result': 0, 'main-surface': self._flag('main_overlays'), 'second-surface': self._flag('sub_overlays'),
                'surfaces': [{'id': 3, 'text': 'Program Title', 'stream-index': 0, 'is-use': self._flag('overlay_enabled')}]}

    def _schedules(self, kind):
        return {'result': 0, 'schedulers': [{'id': 4, 'chn-id': 0,
                 'is-use': self._flag(f'schedule_{kind}_enabled'), 'name': f'Schedule {kind}'}]}

    def _enable_stream(self, params):
        ident = int(params['id'])
        if ident not in (0, 7):
            return {'result': -14}
        self.set_state(f'stream_{ident}_enabled', bool(int(params['is-use'])))
        return {'result': 0}

    def _enable_rec(self, params):
        ident = int(params['id'])
        if ident not in (1, 6):
            return {'result': -14}
        self.set_state(f'rec_{ident}_enabled', bool(int(params['is-use'])))
        return {'result': 0}

    def _enable_overlay(self, params):
        if int(params['id']) != 3:
            return {'result': -14}
        self.set_state('overlay_enabled', bool(int(params['is-use'])))
        return {'result': 0}

    def _enable_schedule(self, params):
        if int(params['id']) != 4 or int(params['chn-id']) != -1:
            return {'result': -14}
        self.set_state(f'schedule_{int(params["chn-type"])}_enabled', bool(int(params['is-use'])))
        return {'result': 0}

    def _overlay_master(self, params):
        key = ('main_overlays', 'sub_overlays')[int(params['stream-index'])]
        self.set_state(key, bool(int(params['enable'])))
        return {'result': 0}

    def _volume(self, params):
        for name in ('spi', 'linein', 'lineout'):
            if 'is-'+name in params:
                self.set_state(name+'_mute', not bool(int(params['is-'+name])))
            if name+'-gain' in params:
                self.set_state(name+'_gain', int(params[name+'-gain']))
        return {'result': 0}

    def _video(self, params):
        index = int(params['stream'])
        updates = {key: int(value) for key, value in params.items() if key in _VIDEO}
        if not 1 <= updates.get('kbps', 8192) <= 32768:
            return {'result': -10}
        self._video_settings[index].update(updates)
        return {'result': 0}

    def _auto_record(self, params):
        self.set_state('auto_record', bool(int(params['is-auto'])))
        self.set_state('auto_record_trigger', int(params['trigger-mode']))
        return {'result': 0}

    def _name(self, params):
        self.set_state('device_name', params['name'])
        return {'result': 0}

    def _offset(self, params):
        self.set_state('audio_offset', int(params['audio-sync-offset']))
        return {'result': 0}

    def _input(self, params):
        if not self._is_aio():
            return {'result': -4}
        self.set_state('input_source', int(params['input-source']))
        return {'result': 0}

    def _mixer(self, params):
        if not self._is_aio():
            return {'result': -4}
        self._input_mixer.update({k: int(v) for k, v in params.items()})
        return {'result': 0}

    def _image(self, params):
        self._selected_image = int(params['id'])
        return {'result': 0}

    def _reboot(self, params):
        self._sessions.clear()
        self.set_state('streaming', False)
        self.set_state('recording', False)
        return {'result': 0}

    def _reset(self, params):
        model = self.state['model']
        for key, value in self.SIMULATOR_INFO['initial_state'].items():
            self.set_state(key, value)
        self.set_state('model', model)
        self._video_settings = [deepcopy(_VIDEO), {**_VIDEO, 'kbps': 4096}]
        self._input_mixer = {'input-device': 2, 'is-hdmi-top': 1, 'type': 0, 'location': 2}
        self._selected_image = 1
        self._sessions.clear()
        return {'result': 0}
