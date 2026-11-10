"""Document-derived Pro Convert IP decoder API v1.3 simulator.

Examples are invented, not hardware captures. The HTTP session, source/profile
rosters and live-window replies follow the manufacturer's API. Settings writes
mutate the same data subsequently read; simulator controls represent changes
made outside OpenAVC. Sparse source IDs and a serial-number-specific cookie
exercise those documented protocol details. No import of the driver is used.
"""
from copy import deepcopy
import hashlib
import json
import secrets
from urllib.parse import urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

_VIDEO = {'follow-input':False, 'show-title':True, 'show-tally':True, 'screen-debug':False,
          'enable-webrtc':True, 'ident-mode':0, 'ident-text':'', 'vu-meter-mode':0,
          'vu-display-mode':'global', 'show-center-cross':False, 'safe-area':0,
          'h-flip':False, 'v-flip':False, 'deinterlace-mode':0, 'force-deint':False,
          'ar-convert':0, 'hdr-output-mode':0, 'color-format':'yuv444',
          'quant-range':'limited', 'switch-mode':0, 'in-color-fmt':0,
          'alpha-mode':'alpha-only', 'alpha-color-key':0}
_AUDIO = {'gain':0.0, 'channels':0, 'sample-rate':0, 'check-pts':True,
          'convert-mode':0, 'channel-map':[0,1,2,3,4,5,6,7]}
_NDI = {'enable-d':False, 'd-server':'', 'groups':'public', 'extra-ips':'',
        'transport':'auto', 'mcast-subnets':'', 'pts-mode':'auto', 'low-bw':False, 'buffer-ms':50}
_MODES = [{'width':1920, 'height':1080, 'interlaced':False, 'frame-rate':6000,
           'aspect-x':16, 'aspect-y':9, 'pref-mode':True, 'curr-mode':True},
          {'width':1280, 'height':720, 'interlaced':False, 'frame-rate':5994,
           'aspect-x':16, 'aspect-y':9, 'pref-mode':False, 'curr-mode':False}]


class MagewellProConvertIPDecoderSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        'driver_id':'magewell_pro_convert_ip_decoder', 'name':'Magewell Pro Convert IP Decoder Simulator',
        'category':'streaming', 'transport':'http', 'default_port':80,
        'initial_state':{'model':'Pro Convert IP to HDMI 4K', 'device_name':'IP Decoder Simulator',
                         'mode':1, 'selected_source':10017, 'current_profile':7,
                         'signal_present':True, 'ptz_available':True, 'hdmi_connected':True,
                         'audio_gain':0, 'panel_locked':False, 'refuse_login':False,
                         'expire_session':False, 'deny_video':False},
        'controls':[
            {'type':'toggle', 'key':'signal_present', 'label':'Source Signal'},
            {'type':'toggle', 'key':'ptz_available', 'label':'Source Supports PTZ'},
            {'type':'toggle', 'key':'hdmi_connected', 'label':'HDMI Connected'},
            {'type':'slider', 'key':'audio_gain', 'label':'Audio Gain (dB)', 'min':-100, 'max':20},
            {'type':'toggle', 'key':'panel_locked', 'label':'Front Panel Locked'},
            {'type':'toggle', 'key':'expire_session', 'label':'Expire Current Sessions'},
            {'type':'toggle', 'key':'refuse_login', 'label':'Refuse Login'},
            {'type':'toggle', 'key':'deny_video', 'label':'Deny Video Settings'},
        ],
    }

    def __init__(self, device_id, config=None):
        config = dict(config or {})
        if config.get('ssl'):
            config['tls'] = True
        super().__init__(device_id, config=config)
        self._sessions = set()
        self._video = deepcopy(_VIDEO)
        self._audio = deepcopy(_AUDIO)
        self._ndi = deepcopy(_NDI)
        self._modes = deepcopy(_MODES)
        self._sources = {
            3:{'id':3, 'protocol':16, 'config':{'type':1, 'name':'Lectern', 'url':'rtsp://camera.example/live'}},
            42:{'id':42, 'protocol':2, 'config':{'type':1, 'name':'Remote Feed', 'url':'srt://feed.example:9000'}},
            10017:{'id':10017, 'protocol':64, 'config':{'type':2, 'name':'NDI Camera', 'ndi':{'name':'Camera (Main)', 'url':'192.0.2.10:5961'}}}}
        self._profiles = {
            0:{'id':0, 'name':'Default', 'mode':1, 'audio-idx':0, 'screens':[{'id':10017}]},
            7:{'id':7, 'name':'Presentation', 'mode':2, 'audio-idx':0,
               'pip':{'mode':6, 'scale':.3}, 'screens':[{'id':3}, {'id':10017}]},
            19:{'id':19, 'name':'Quad View', 'mode':4, 'audio-idx':1,
                'screens':[{'id':3}, {'id':42}, {'id':10017}, {'id':3}]}}
        self._images = [{'id':0, 'type':'img_none', 'use':True, 'edit':False},
                        {'id':1, 'type':'img_no_signal', 'use':True, 'edit':False},
                        {'id':3, 'type':'img_no_signal', 'use':False, 'edit':True}]
        self.login_count = 0
        self.requests = []
        self.last_ptz = None
        if self.config.get('model'):
            self.set_state('model', self.config['model'])

    def handle_request(self, method, path, headers, body):
        path = urlsplit(path).path
        try:
            params = json.loads(body) if body else {}
        except (ValueError, TypeError):
            return 400, {'status':7}, {}
        if not isinstance(params, dict):
            return 400, {'status':7}, {}
        self.requests.append((method, path, deepcopy(params)))
        if path == '/api/user/login':
            return self._login(method, params)
        if path == '/api/ping':
            return 200, {'status':0}, {}
        if not self._authenticated(headers):
            return 200, {'status':37}, {}
        try:
            result = self._dispatch(method, path, params)
        except (ValueError, TypeError, KeyError, IndexError):
            result = {'status':7}
        return 200, result, {}

    def _login(self, method, params):
        self.login_count += 1
        if method != 'POST':
            return 405, {'status':7}, {}
        if self._login_refused(params):
            return 200, {'status':36}, {}
        sid = secrets.token_hex(16)
        self._sessions.add(sid)
        return 200, {'status':0, 'sid':sid, 'group':'Admin', 'role':'Admin'}, {
            'Set-Cookie':f'sid-A443SIM000001={sid}; Path=/; HttpOnly'}

    def _login_refused(self, params):
        digest = params.get('password', '')
        if self.state['refuse_login'] or params.get('username') in ('', None, 'invalid'):
            return True
        if len(digest) != 64 or digest == hashlib.sha256(b'invalid').hexdigest():
            return True
        password = self.config.get('password')
        return bool(password and digest != hashlib.sha256(str(password).encode()).hexdigest())

    def _authenticated(self, headers):
        if self.state['expire_session']:
            self._sessions.clear()
            self.set_state('expire_session', False)
        cookie = next((value for key,value in headers.items() if key.lower() == 'cookie'), '')
        pairs = dict(part.strip().split('=',1) for part in cookie.split(';') if '=' in part)
        return pairs.get('sid-A443SIM000001') in self._sessions

    def _dispatch(self, method, path, params):
        if self.state['deny_video'] and path.startswith('/api/video/config/'):
            return {'status':31}
        if method == 'GET':
            return self._read(path)
        if method != 'POST':
            return {'status':7}
        return self._write(path, params)

    def _read(self, path):
        readers = {'/api/system/summary':self._summary, '/api/source/list':self._source_list,
                   '/api/profile/list':self._profile_list, '/api/audio/config/get':self._audio_config,
                   '/api/settings/get':self._settings}
        if path in readers:
            return readers[path]()
        values = {'/api/video/config/get':self._video, '/api/video/mode/get':{'modes':self._modes},
                  '/api/resource/image/list':{'data':self._images}}
        data = values.get(path)
        return {'status':16} if data is None else {'status':0, **deepcopy(data)}

    def _summary(self):
        profile = deepcopy(self._profiles[int(self.state['current_profile'])])
        if int(self.state['mode']) == 1:
            profile = deepcopy(self._profiles[0])
            profile['screens'] = [{'id':int(self.state['selected_source'])}]
        profile['streams'] = [self._stream(index, row['id']) for index,row in enumerate(profile.pop('screens'))]
        return {'status':0, 'mode':int(self.state['mode']), 'product-name':self.state['model'],
                'device-name':self.state['device_name'], 'firmware-ver':'1.3.83', 'serial-number':'A443SIM000001',
                'core-temp':52.5, 'fan-speed':0, 'uptime':1000,
                'hdmi-state':int(bool(self.state['hdmi_connected'])),
                'ptz':{'enabled':bool(self.state['ptz_available'])}, 'profile':profile}

    def _stream(self, index, source_id):
        source = self._sources.get(source_id, {'config':{'name':'Missing Source'}})
        receiving = bool(self.state['signal_present']) and source_id in self._sources
        return {'index':index, 'id':source_id, 'name':source['config']['name'], 'state':2 if receiving else 4,
                'protocol':'ndi' if source.get('protocol') == 64 else 'rtsp', 'buffer-ms':50,
                'video':{'width':1920, 'height':1080, 'field-rate':'59.94', 'codec':'H264', 'kbps':6430, 'drops':0},
                'audio':{'codec':'AAC', 'num-channels':8, 'sample-rate':48000, 'kbps':260, 'drops':0},
                'extra':{'tally-program':False, 'tally-preview':False}}

    def _source_list(self):
        rows = list(deepcopy(self._sources).values())
        return {'status':0, 'mode':int(self.state['mode']), 'selected':int(self.state['selected_source']),
                'static':[r for r in rows if r['config']['type'] != 2],
                'ndi':[r for r in rows if r['config']['type'] == 2]}

    def _profile_list(self):
        rows = list(deepcopy(self._profiles).values())
        for row in rows:
            row['selected'] = row['id'] == int(self.state['current_profile'])
        return {'status':0, 'current-id':int(self.state['current_profile']), 'profiles':rows,
                'limit':{'max-profile-num':20}}

    def _audio_config(self):
        return {'status':0, **deepcopy(self._audio), 'gain':float(self.state['audio_gain'])}

    def _settings(self):
        return {'status':0, 'mode':int(self.state['mode']), 'ndi':deepcopy(self._ndi),
                'panel':{'menu-knob-locked':bool(self.state['panel_locked'])}, 'gui':{'lang':'en'}}

    def _write(self, path, params):
        stores = {'/api/video/config/set':self._video, '/api/audio/config/set':self._audio,
                  '/api/settings/ndi/set':self._ndi}
        if path in stores:
            stores[path].update(deepcopy(params))
            if path == '/api/audio/config/set' and 'gain' in params:
                self.set_state('audio_gain', params['gain'])
            return {'status':0}
        handlers = {'/api/settings/mode/set':self._set_mode, '/api/settings/panel/set':self._set_panel,
                    '/api/system/set-device-name':self._set_name, '/api/source/select':self._select_source,
                    '/api/source/add':self._add_source, '/api/source/set':self._set_source,
                    '/api/source/del':self._delete_source, '/api/profile/select':self._select_profile,
                    '/api/profile/add':self._add_profile, '/api/profile/set':self._set_profile,
                    '/api/profile/del':self._delete_profile, '/api/profile/screen/select':self._profile_screen,
                    '/api/video/mode/set':self._set_output, '/api/resource/image/set/default':self._set_image}
        if path in handlers:
            return handlers[path](params)
        return self._simple_write(path, params)

    def _simple_write(self, path, params):
        if path.startswith('/api/ptz/'):
            if not self.state['ptz_available']:
                return {'status':9}
            self.last_ptz = (path, deepcopy(params))
            return {'status':0}
        if path in ('/api/source/restart', '/api/video/screen/wakeup'):
            return {'status':0}
        return {'status':16}

    def _set_mode(self, params):
        if params['mode'] not in (1,2):
            return {'status':7}
        self.set_state('mode', params['mode'])
        return {'status':0}

    def _set_panel(self, params):
        self.set_state('panel_locked', params['menu-knob-locked'])
        return {'status':0}

    def _set_name(self, params):
        self.set_state('device_name', params['name'])
        return {'status':0}

    def _select_source(self, params):
        if int(self.state['mode']) != 1 or params['id'] not in self._sources:
            return {'status':7}
        self.set_state('selected_source', params['id'])
        return {'status':0}

    def _add_source(self, params):
        ident = max(self._sources)+7
        self._sources[ident] = {'id':ident, 'protocol':64 if params['type'] == 3 else 16, 'config':deepcopy(params)}
        return {'status':0}

    def _set_source(self, params):
        source = self._sources[params['id']]
        if source['config']['type'] == 2:
            return {'status':26}
        source['config'].update(deepcopy(params['config']))
        return {'status':0}

    def _delete_source(self, params):
        if self._sources[params['id']]['config']['type'] == 2:
            return {'status':26}
        del self._sources[params['id']]
        return {'status':0}

    def _select_profile(self, params):
        if int(self.state['mode']) != 2 or params['id'] not in self._profiles:
            return {'status':7}
        self.set_state('current_profile', params['id'])
        return {'status':0}

    def _add_profile(self, params):
        if len(params['screens']) != params['mode']:
            return {'status':7}
        ident = max(self._profiles)+3
        self._profiles[ident] = {'id':ident, 'audio-idx':0, **deepcopy(params)}
        return {'status':0}

    def _set_profile(self, params):
        self._profiles[params['id']].update(deepcopy(params['config']))
        return {'status':0}

    def _delete_profile(self, params):
        ident = params['id']
        if ident == 0:
            return {'status':26}
        del self._profiles[ident]
        if int(self.state['current_profile']) == ident:
            self.set_state('current_profile', 0)
        return {'status':0}

    def _profile_screen(self, params):
        if params['source-id'] not in self._sources:
            return {'status':16}
        self._profiles[params['id']]['screens'][params['screen-index']] = {'id':params['source-id']}
        return {'status':0}

    def _set_output(self, params):
        fields = ('width','height','interlaced','frame-rate','aspect-x','aspect-y')
        matches = [mode for mode in self._modes if all(mode[k] == params[k] for k in fields)]
        if not matches:
            return {'status':9}
        for mode in self._modes:
            mode['curr-mode'] = mode is matches[0]
        return {'status':0}

    def _set_image(self, params):
        if not any(row['id'] == params['id'] and row['type'] == params['type'] for row in self._images):
            return {'status':16}
        for row in self._images:
            if row['type'] == params['type']:
                row['use'] = row['id'] == params['id']
        return {'status':0}
