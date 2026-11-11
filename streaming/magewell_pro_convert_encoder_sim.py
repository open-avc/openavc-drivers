"""Document-derived Pro Convert encoder simulator; no hardware captures.

Serves the encoder's GET /mwapi API and sid cookie, independently of the driver.
Settings, live signal and backup sources can change from the Simulator UI.
Capabilities are illustrative configurations for project preparation, not
measurements of a particular unit. No control-state push is documented.
"""
from copy import deepcopy
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

_VIDEO = {
    'show-adv-ui':False, 'in-auto-aspect':True, 'in-aspect-x':16, 'in-aspect-y':9,
    'in-auto-color-fmt':True, 'in-color-fmt':'rgb', 'in-auto-quant-range':True,
    'in-quant-range':'full', 'brightness':0, 'contrast':100, 'hue':0, 'saturation':100,
    'deinterlace':'none', 'ar-convertion':'ignore', 'out-flip':False, 'out-mirror':False,
    'out-cx':1920, 'out-cy':1080, 'out-raw-resolution':True, 'out-aspect-x':16,
    'out-aspect-y':9, 'out-auto-aspect':True, 'out-fr-convertion':'raw',
    'out-auto-color-fmt':True, 'out-color-fmt':'bt.709', 'out-auto-sat-range':True,
    'out-sat-range':'limited', 'out-auto-quant-range':True, 'out-quant-range':'limited',
    'bit-rate-ratio':100, 'low-res-full-fr':False,
}
_NDI = {
    'enable':True, 'source-name':'#%board-id% (%serial-no%)', 'group-name':'public',
    'enable-web-control':True, 'enable-ptz-control':False, 'enable-fail-over':False,
    'fail-over-ndi-name':'', 'fail-over-ip-addr':'', 'enable-mcast':False,
    'enable-rudp':False, 'enable-tcp':False, 'enable-udp':True,
    'mcast-addr':'239.255.0.0', 'mcast-mask':'255.255.0.0', 'mcast-ttl':4,
    'enable-discovery':False, 'discovery-server':'', 'reference-level':20,
}
_EDID = {'smart-edid':True, 'keep-last':False, 'add-audio':True, 'limit-pixel-clock':True}
_PTZ = {'proto':'none', 'index':1, 'baudrate':9600, 'invert-pan':False, 'invert-tilt':False,
        'ip-addr':'192.0.2.20', 'port':52381, 'visca-msg-hdr':True,
        'focus-near-limit':0, 'focus-far-limit':2935, 'pan-left-limit':-2448,
        'pan-center':0, 'pan-right-limit':2448, 'tilt-top-limit':1280,
        'tilt-center':0, 'tilt-bottom-limit':-368, 'zoom-out-limit':16384}
# Synthetic EDID for a file-download exercise, not a captured display profile.
_EDID_BYTES = bytes.fromhex('00ffffffffffff00') + bytes(119) + bytes([6])
_RANGES = {'brightness':(-100,100), 'contrast':(50,200), 'hue':(-90,90),
           'saturation':(0,200), 'bit-rate-ratio':(50,200), 'mcast-ttl':(1,255),
           'index':(1,7), 'port':(1,65535), 'zoom-out-limit':(0,32767),
           'focus-near-limit':(0,65535), 'focus-far-limit':(0,65535)}
_CHOICES = {'deinterlace':('none','top-field','bottom-field'),
            'ar-convertion':('ignore','cropping','padding'),
            'out-fr-convertion':('raw','half','one-third','quarter'),
            'in-color-fmt':('rgb','bt.601','bt.709','bt.2020'),
            'out-color-fmt':('bt.601','bt.709','bt.2020'),
            'reference-level':(14,20), 'baudrate':(2400,4800,9600,19200,38400),
            'proto':('none','visca','visca-udp','visca-udp2rs232','pelco-d','pelco-p')}


class MagewellProConvertEncoderSimulator(HTTPSimulator):
    SIMULATOR_INFO = {
        'driver_id':'magewell_pro_convert_encoder', 'name':'Magewell Pro Convert Encoder Simulator',
        'category':'streaming', 'transport':'http', 'default_port':80,
        'initial_state':{'model':'Pro Convert HDMI 4K Plus', 'signal_present':True,
                         'tally_program':False, 'tally_preview':False, 'clients':2,
                         'temperature':46.5, 'backup_available':True, 'refuse_login':False,
                         'expire_session':False, 'deny_settings':False, 'ignore_write':False,
                         'video.brightness':0, 'ndi.enable':True},
        'controls':[
            {'type':'toggle','key':'signal_present','label':'Input Signal'},
            {'type':'toggle','key':'tally_program','label':'On Program'},
            {'type':'toggle','key':'tally_preview','label':'On Preview'},
            {'type':'slider','key':'clients','label':'NDI Receivers','min':0,'max':20},
            {'type':'slider','key':'temperature','label':'Temperature (°C)','min':20,'max':95},
            {'type':'toggle','key':'backup_available','label':'Backup Sources Available'},
            {'type':'slider','key':'video.brightness','label':'Brightness','min':-100,'max':100},
            {'type':'toggle','key':'ndi.enable','label':'NDI Output'},
            {'type':'toggle','key':'expire_session','label':'Expire Sessions'},
            {'type':'toggle','key':'refuse_login','label':'Refuse Login'},
            {'type':'toggle','key':'deny_settings','label':'Deny Settings Permission'},
            {'type':'toggle','key':'ignore_write','label':'Acknowledge without Applying Write'},
        ],
    }

    def __init__(self, device_id, config=None):
        config = dict(config or {})
        if config.get('ssl'):
            config['tls'] = True
        super().__init__(device_id, config=config)
        self._sessions = set()
        self._settings = {'video':deepcopy(_VIDEO), 'ndi':deepcopy(_NDI), 'edid':deepcopy(_EDID),
                          'ptz':deepcopy(_PTZ), 'tally':{'ext-tally':False}}
        for group, values in self._settings.items():
            for key, value in values.items():
                self.set_state(group + '.' + key, value)
        if self.config.get('model'):
            self.set_state('model', self.config['model'])
        self.login_count = 0
        self.requests = []
        self.arrange_count = 0

    def handle_request(self, method, path, headers, body):
        parts = urlsplit(path)
        if parts.path != '/mwapi' or method != 'GET':
            return 404, {'status':16}, {}
        query = {key:values[-1] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}
        operation = query.pop('method', '')
        self.requests.append(operation)
        if operation == 'ping':
            return 200, {'status':0}, {}
        if operation == 'login':
            return self._login(query)
        if not self._has_session(headers):
            return 200, {'status':37}, {}
        if operation == 'logout':
            self._sessions.clear()
            return 200, {'status':0}, {}
        return 200, self._dispatch(operation, query), {}

    def _has_session(self, headers):
        if self.state.get('expire_session'):
            self._sessions.clear()
            self.set_state('expire_session', False)
        cookie = next((value for key,value in headers.items() if key.lower() == 'cookie'), '')
        return any('sid=' + session in cookie.split('; ') for session in self._sessions)

    def _login(self, query):
        self.login_count += 1
        rejected = self.state.get('refuse_login') or query.get('id') == 'invalid'
        rejected = rejected or query.get('pass') == _digest('invalid')
        if self.config.get('password'):
            rejected = rejected or query.get('pass') != _digest(self.config['password'])
        if rejected or not all(query.get(key) for key in ('id', 'pass')):
            return 200, {'status':36}, {}
        sid = secrets.token_hex(16)
        self._sessions.add(sid)
        return 200, {'status':0}, {'Set-Cookie':f'sid={sid}; Path=/; HttpOnly'}

    def _dispatch(self, operation, query):
        readers = {'get-summary-info':self._summary, 'get-caps':self._capabilities,
                   'get-signal-info':self._signal, 'get-ndi-sources':self._sources,
                   'get-output-edid':self._output_edid}
        if operation in readers:
            return readers[operation]()
        resets = {'reset-video-config':('video', _VIDEO), 'set-default-edid':('edid', _EDID)}
        if operation in resets:
            group, defaults = resets[operation]
            return self._write(group, {key:_text(value) for key,value in defaults.items()})
        if operation == 'arrange-ptz-cameras':
            self.arrange_count += 1
            return {'status':0}
        if operation == 'reboot':
            self._sessions.clear()
            return {'status':0}
        return self._config_request(operation, query)

    def _config_request(self, operation, query):
        group = operation.removeprefix('get-').removeprefix('set-').removesuffix('-config')
        if group not in self._settings:
            return {'status':5}
        if self.state.get('deny_settings'):
            return {'status':31}
        if not self._group_supported(group):
            return {'status':9}
        if operation.startswith('get-'):
            return {'status':0, **self._read(group)}
        if operation.startswith('set-'):
            return self._write(group, query)
        return {'status':5}

    def _capabilities(self):
        model = str(self.state['model'])
        hdmi = 'HDMI' in model
        plus = 'Plus' in model
        return {'status':0, 'max-input-width':4096, 'max-input-height':2160,
                'max-output-width':4096 if '4K' in model else 1920,
                'max-output-height':2160 if '4K' in model else 1080,
                'has-input':True, 'has-output':plus, 'has-loop-through':plus,
                'has-fan':plus, 'has-sdcard':False, 'has-ptz':plus,
                'has-input-edid':hdmi, 'has-output-edid':hdmi and plus}

    def _group_supported(self, group):
        cap = {'edid':'has-input-edid', 'ptz':'has-ptz'}.get(group)
        return cap is None or self._capabilities()[cap]

    def _read(self, group):
        values = {key:self.state.get(group + '.' + key, value) for key,value in self._settings[group].items()}
        if group == 'edid':
            values['data'] = base64.b64encode(_EDID_BYTES).decode()
        return values

    def _write(self, group, query):
        if self.state.get('deny_settings'):
            return {'status':31}
        if not self._group_supported(group):
            return {'status':9}
        try:
            values = {key:_coerce(value, self._settings[group][key]) for key,value in query.items()}
        except (ValueError, KeyError):
            return {'status':7}
        if not self._valid_values(group, values):
            return {'status':27}
        if not self.state.get('ignore_write'):
            for key,value in values.items():
                self.set_state(group + '.' + key, value)
        return {'status':0}

    def _valid_values(self, group, values):
        if not all(_valid_value(key, value) for key,value in values.items()):
            return False
        merged = {**self._read(group), **values}
        if group == 'video':
            return self._valid_video(merged)
        if group == 'ndi':
            return sum(bool(merged[key]) for key in ('enable-mcast','enable-rudp','enable-tcp','enable-udp')) <= 1
        return True

    def _valid_video(self, values):
        caps = self._capabilities()
        dimensions = [(values['out-cx'], caps['max-output-width'], 4),
                      (values['out-cy'], caps['max-output-height'], 2)]
        return all(0 < value <= maximum and value % step == 0 for value,maximum,step in dimensions)

    def _summary(self):
        ndi, video = self._read('ndi'), self._read('video')
        signal = bool(self.state['signal_present'])
        name = ndi['source-name'].replace('%board-id%', '00').replace('%serial-no%', 'SIM0001')
        return {'status':0,
                'device':{'name':'Pro Convert SIM0001', 'model':self.state['model'].removeprefix('Pro Convert '),
                          'serial-no':'SIM0001', 'fw-version':'1.1.376', 'hw-revision':'B',
                          'input-state':'1920x1080p60' if signal else 'no-signal',
                          'output-state':'active' if self._capabilities()['has-output'] else 'unconnected',
                          'ptz-state':'disconnected', 'ptz-proto':self._read('ptz')['proto'],
                          'cpu-usage':5.0, 'memory-usage':58.3, 'core-temp':float(self.state['temperature']),
                          'up-time':8006, 'fan-rpm':1200, 'board-id':0},
                'ethernet':{'state':'1000m', 'mac-addr':'70:B3:D5:75:D0:01', 'ip-addr':'192.0.2.1',
                            'tx-speed-kbps':80000, 'rx-speed-kbps':100},
                'ndi':{'name':name, 'enabled':ndi['enable'], 'num-clients':int(self.state['clients']),
                       'tally-preview':bool(self.state['tally_preview']), 'tally-program':bool(self.state['tally_program']),
                       'audio-drop-frames':0, 'video-drop-frames':0, 'audio-bit-rate':1536,
                       'video-bit-rate':80000 if signal and ndi['enable'] else 0,
                       'video-width':1920 if video['out-raw-resolution'] else video['out-cx'],
                       'video-height':1080 if video['out-raw-resolution'] else video['out-cy'],
                       'video-scan':'progressive', 'video-field-rate':60.0,
                       'audio-num-channels':2, 'audio-sample-rate':48000, 'audio-bit-count':16}}

    def _signal(self):
        if not self.state['signal_present']:
            return {'status':0, 'signal-info-types':[]}
        data = {'status':0, 'signal-info-types':['video-info','audio-info'],
                'video-info':{'codec':'uncompressed', 'width':1920, 'height':1080,
                              'scan':'progressive', 'field-rate':60.0, 'color-depth':8,
                              'color-format':'rgb', 'aspect-ratio':'16:9', 'sampling':'4:4:4',
                              'quant-range':'full'},
                'audio-info':{'codec':'lpcm','num-channels':2,'sample-rate':48000,'bit-count':16}}
        if 'HDMI' in self.state['model']:
            data['hdmi-info'] = {'hdcp':'none'}
        else:
            data['sdi-info'] = {'link-speed':'3g', 'link-type':'single-link'}
        return data

    def _sources(self):
        rows = [{'ndi-name':'Backup Camera (Main)', 'ip-addr':'192.0.2.10:5961'},
                {'ndi-name':'Lectern & Slides', 'ip-addr':'192.0.2.11:5962'}]
        return {'status':0, 'sources':rows if self.state['backup_available'] else []}

    def _output_edid(self):
        if not self._capabilities()['has-output-edid']:
            return {'status':9}
        return {'status':0, 'data':base64.b64encode(_EDID_BYTES).decode()}


def _digest(value):
    return hashlib.md5(str(value).encode(), usedforsecurity=False).hexdigest()


def _valid_value(key, value):
    bounds = _RANGES.get(key)
    if bounds and not bounds[0] <= value <= bounds[1]:
        return False
    choices = _CHOICES.get(key)
    return not choices or value in choices


def _text(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def _coerce(value, prototype):
    if isinstance(prototype, bool):
        if value not in ('true', 'false'):
            raise ValueError('boolean')
        return value == 'true'
    if isinstance(prototype, int):
        return int(value)
    return value
