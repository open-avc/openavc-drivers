"""Visionary API 3.x document-derived simulator, not hardware captures.

Models HTTP Basic, UDP discovery, ordered form pairs, queued routing, volatile
settings and Save/Reboot. Fault controls are synthetic. Firmware and network
identity are illustrative. No unsolicited state feed is documented.
"""
import base64
from urllib.parse import parse_qsl, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator
from openavc.simulator.datagram_server import DatagramServerMixin

ROLE = 'encoder'
DEFAULT_MODEL = 'E5200'
COMMON = {'UNIT.ID': 'Visionary Endpoint',
 'UNIT.LOCATION': 'Demo Space',
 'UNIT.MODEL': '',
 'UNIT.FIRMWARE': '3.19.10',
 'UNIT.FIRMWARE_DATE': 'Document-derived simulation',
 'UNIT.MAC_ADDRESS': '02:00:5E:00:53:01',
 'UNIT.SERIAL': 'SIMULATED-001',
 'UNIT.DUET_INSTALLED': False,
 'IP.MODE': 'dhcp',
 'IP.ADDRESS': '192.0.2.100',
 'IP.NETMASK': '255.255.255.0',
 'IP.GATEWAY': '192.0.2.1',
 'IP.DNS_SERVER': '192.0.2.1',
 'AUDIO.VOLUME': 80,
 'STREAM.MODE': 'multicast',
 'STREAM.AUDIO': 'HDMI',
 'VIDEO.HDCP_FORCE_ON': 'SOURCE',
 'USB.ENABLED': True,
 'USB.KVM_FAST': True,
 'USB.MODE': 'link',
 'SERIAL.ENABLED': False,
 'SERIAL.BAUDRATE': 115200,
 'SERIAL.DATABITS': 8,
 'SERIAL.PARITY': 'n',
 'SERIAL.STOPBITS': 1,
 'IR.ENABLED': False}
ENCODER = {'STREAM.BIT_RATE': 'auto',
 'STREAM.FRAME_RATE': 60,
 'STREAM.QOS_DSCP': 26,
 'STREAM.VIDEO': 'HDMI1',
 'STREAM.VIDEO_PRIORITY': '1',
 'VIDEO.HDMI_OUT': 'AUTO',
 'VIDEO.HDMI_OUT_PRIORITY': '1',
 'VIDEO.EDID1': '01_DEFAULT',
 'VIDEO.EDID2': '01_DEFAULT',
 'VIDEO.EDID3': '01_DEFAULT'}
DECODER = {'STREAM.AUDIO': 'STREAM',
 'STREAM.HOST_LINKED': True,
 'STREAM.HOST': '192.0.2.11',
 'STREAM.HOST_VIDEO': '192.0.2.11',
 'STREAM.HOST_AUDIO': '192.0.2.11',
 'STREAM.HOST_USB': '192.0.2.11',
 'VIDEO.OUTPUT': 'NORMAL',
 'VIDEO.FORMAT': '0',
 'VIDEO.GENLOCK': True,
 'VIDEO.POWER_SAVE': False,
 'VIDEO.SOURCE_TIMEOUT': True,
 'VIDEO.INFO_TEXT': False,
 'VIDEO.OSD_TEXT': False,
 'VW.ENABLED': True,
 'VW.ACTIVE': False,
 'VW.MAX_ROWS': 1,
 'VW.MAX_COLUMNS': 1,
 'VW.ROW': 1,
 'VW.COLUMN': 1,
 'VW.NAME': '',
 'VW.OVERALL_WIDTH': 1,
 'VW.OVERALL_HEIGHT': 1,
 'VW.VISIBLE_WIDTH': 1,
 'VW.VISIBLE_HEIGHT': 1,
 'VW.ROTATE': '0',
 'VW.STRETCH': '2'}
BOOLS = ['UNIT.DUET_INSTALLED',
 'IR.ENABLED',
 'USB.ENABLED',
 'USB.KVM_FAST',
 'SERIAL.ENABLED',
 'STREAM.HOST_LINKED',
 'AUDIO.MUTE',
 'AUDIO.HDMI_MUTE',
 'AUDIO.DAC_MUTE',
 'VIDEO.GENLOCK',
 'VIDEO.POWER_SAVE',
 'VIDEO.SOURCE_TIMEOUT',
 'VIDEO.INFO_TEXT',
 'VIDEO.OSD_TEXT',
 'VW.ENABLED',
 'VW.ACTIVE',
 'UNIT.GPIO_IN1',
 'UNIT.GPIO_OUT1',
 'UNIT.GPIO_OUT2']
INTS = ['AUDIO.VOLUME',
 'SERIAL.BAUDRATE',
 'SERIAL.DATABITS',
 'SERIAL.STOPBITS',
 'STREAM.FRAME_RATE',
 'STREAM.QOS_DSCP',
 'VW.MAX_ROWS',
 'VW.MAX_COLUMNS',
 'VW.ROW',
 'VW.COLUMN',
 'VW.OVERALL_WIDTH',
 'VW.OVERALL_HEIGHT',
 'VW.VISIBLE_WIDTH',
 'VW.VISIBLE_HEIGHT']


class Visionary5SeriesEncoderSimulator(DatagramServerMixin, HTTPSimulator):
    SIMULATOR_INFO = {'driver_id': 'visionary_5_series_encoder',
     'name': 'Visionary 5 Series Encoder',
     'transport': 'http',
     'initial_state': {'video_present': True,
                       'refuse_login': False,
                       'deny_requests': False,
                       'ignore_write': False,
                       'route_delay_reads': 0,
                       'last_utility': ''},
     'controls': [{'type': 'toggle', 'key': 'video_present', 'label': 'Video Signal Present'},
                  {'type': 'slider', 'key': 'AUDIO.VOLUME', 'label': 'Analog Level', 'min': 0, 'max': 100},
                  {'type': 'toggle', 'key': 'USB.ENABLED', 'label': 'USB Enabled'},
                  {'type': 'toggle', 'key': 'refuse_login', 'label': 'Refuse Login'},
                  {'type': 'toggle', 'key': 'deny_requests', 'label': 'Refuse HTTP Requests'},
                  {'type': 'toggle', 'key': 'ignore_write', 'label': 'Acknowledge without Applying Write'}]}

    CAPACITY_KIND = None

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config=config)
        self.requests = []
        self._pending_route = []
        self._route_reads_left = 0
        self._seed_device()
        self._factory = self._device_values()
        self._saved = dict(self._factory)

    def _seed_device(self):
        model = str(self.config.get('model') or DEFAULT_MODEL)
        data = dict(COMMON)
        data.update(ENCODER if ROLE == 'encoder' else DECODER)
        data['UNIT.MODEL'] = model
        data['UNIT.ID'] = model + ' Demo'
        if model in ('DuetE-5','DuetD-5'):
            data.update({'UNIT.DUET_INSTALLED':True,'DANTE.DEVICE_NAME':model + '-001',
                         'UNIT.GPIO_IN1':False,'UNIT.GPIO_OUT1':False,'UNIT.GPIO_OUT2':False})
        if self.config.get('dante_module'):
            data.update({'UNIT.DUET_INSTALLED':True,'DANTE.DEVICE_NAME':model + '-001'})
        if model in ('DuetD-5','D5200'):
            data.update({'AUDIO.DAC_MUTE':False,'AUDIO.DAC_MUX':'STREAM'})
            data['AUDIO.MUTE' if model=='DuetD-5' else 'AUDIO.HDMI_MUTE'] = False
        if model == 'E5100':
            for key in ('STREAM.VIDEO','STREAM.VIDEO_PRIORITY','VIDEO.HDMI_OUT','VIDEO.HDMI_OUT_PRIORITY',
                        'VIDEO.EDID1','VIDEO.EDID2','VIDEO.EDID3'):
                data.pop(key, None)
        for key, value in data.items():
            self.set_state(key, value)

    async def start(self, port):
        await super().start(port)
        try:
            await self.start_datagram_server(self.port)
        except Exception:
            await super().stop()
            raise

    async def stop(self):
        await self.stop_datagram_server()
        await super().stop()

    async def respond_http(self, request, method, path, headers, body):
        return await super().respond_http(request, method, request.raw_path, headers, body)

    def handle_request(self, method, path, headers, body):
        parts = urlsplit(path)
        if parts.path != '/cgi-bin/wapi.cgi':
            return 404, 'Not found', {}
        if not self._authenticated(headers):
            return 401, 'Unauthorized', {'WWW-Authenticate':'Basic realm="Visionary"'}
        if self.state.get('deny_requests'):
            return 503, 'Synthetic HTTP refusal', {}
        if method not in ('GET','POST'):
            return 405, 'Method not allowed', {}
        payload = parts.query if method == 'GET' else body
        pairs = parse_qsl(payload, keep_blank_values=True)
        self.requests.append((method, pairs))
        return 200, self._dispatch(pairs), {}

    def handle_command(self, data):
        # UDP carries the same human-readable API envelope. This simulator
        # answers request/reply discovery; it sends no unsolicited feedback.
        pairs = parse_qsl(data.decode('utf-8'), keep_blank_values=True)
        self.requests.append(('UDP', pairs))
        return self._dispatch(pairs).encode()

    def _authenticated(self, headers):
        raw = next((v for k,v in headers.items() if k.lower()=='authorization'), '')
        if self.state.get('refuse_login') or not raw.startswith('Basic '):
            return False
        credentials = _credentials(raw[6:])
        if credentials is None:
            return False
        configured = self.config.get('password')
        return not configured or credentials[1] == configured

    def _dispatch(self, pairs):
        if pairs[:2] != [('CMD','START'),('UNIT.ID','ALL')] or pairs[-1:] != [('CMD','END')]:
            return _refusal()
        body = pairs[2:-1]
        if not body:
            return _refusal()
        if body[0][0].startswith('QUERY.'):
            return self._query(*body[0])
        if self.state.get('ignore_write'):
            return _reply({}, 'SUCCESS')
        return self._write(body)

    def _query(self, key, value):
        self._advance_route()
        if key in ('QUERY.ALL','QUERY.STATUS'):
            return _reply(self._device_values(), 'SUCCESS_QUERY')
        if key == 'QUERY.VIDEO_TIMING':
            return _reply({'VIDEO.TIMING': self._timing(), 'QUERY.VIDEO_TIMING':'OK'}, 'SUCCESS_QUERY')
        if key == 'QUERY.KEY' and value in self.state:
            raw = self.state[value]
            if value.startswith('UNIT.GPIO_'):
                raw = '1' if _boolean(raw) else '0'
            return _reply({value: raw}, 'SUCCESS_QUERY_X')
        return _refusal()

    def _timing(self):
        if not self.state.get('video_present'):
            return 'Not Available'
        return ('Timing Table: Serial Number[0x006B] [3840]X[2160] [30]Hz\n'
                '\tPixel Rate: 296703KHz, Htotal: 4400, Vtotal: 2250\n'
                'HDCP: [On], version 1.x\nSignal Type: HDMI 16:9')

    def _device_values(self):
        return {key: value for key,value in self.state.items() if '.' in key}

    def _advance_route(self):
        if self._route_reads_left:
            self._route_reads_left -= 1
            return
        for key,value in self._pending_route:
            self.set_state(key,value)
        self._pending_route.clear()

    def _write(self, pairs):
        operations = {'UNIT.SAVE': self._save, 'UNIT.REBOOT': self._reboot,
                      'UNIT.FACTORY_DEFAULT': self._reset}
        if len(pairs)==1 and pairs[0][0] in operations:
            operations[pairs[0][0]]()
            return _reply({}, 'SUCCESS_UTILITY')
        if len(pairs)==1 and pairs[0][0] in ('UNIT.FU','DANTE.REBOOT','UNIT.IR_SEND','VIDEO.SEND_CEC_GENERIC'):
            self.set_state('last_utility', pairs[0][0])
            return _reply({}, 'SUCCESS_UTILITY')
        if any(key == 'STREAM.CONNECT' for key,value in pairs):
            return self._queue_route(pairs)
        return self._write_parameters(pairs)

    def _write_parameters(self, pairs):
        for key,value in pairs:
            target = {'UNIT.SET_ID':'UNIT.ID','DANTE.SET_DEVICE_NAME':'DANTE.DEVICE_NAME'}.get(key,key)
            if target not in self.state:
                return _refusal()
            converted = _convert(target, value)
            self.set_state(target, converted)
        return _reply({}, 'SUCCESS')

    def _queue_route(self, pairs):
        pending = []
        host = None
        replies = []
        for key,value in pairs:
            if key.startswith('STREAM.HOST'):
                host = (key,value)
            elif key == 'STREAM.CONNECT':
                _append_route(pending, replies, host, value)
            elif key == 'VW.ACTIVE':
                pending.append((key,_boolean(value)))
        self._pending_route = pending
        self._route_reads_left = int(self.state.get('route_delay_reads',0))
        if self._route_reads_left == 0:
            self._advance_route()
        return '&' + '&'.join(key+'='+str(value) for key,value in replies) + '&API.STATUS=SUCCESS\n'

    def _save(self):
        self._saved = self._device_values()

    def _reboot(self):
        self._pending_route.clear()
        for key,value in self._saved.items():
            self.set_state(key,value)

    def _reset(self):
        self._saved = dict(self._factory)
        self._reboot()


def _boolean(value):
    return value is True or str(value).upper() in ('TRUE','1')


def _credentials(encoded):
    try:
        user, sep, password = base64.b64decode(encoded, validate=True).decode().partition(':')
    except (ValueError, UnicodeDecodeError):
        return None
    if not all((user, sep, password)) or 'invalid' in (user, password):
        return None
    return user, password


def _append_route(pending, replies, host, selector):
    if host is None:
        raise ValueError('A route needs an encoder address before CONNECT.')
    pending.extend([host, ('STREAM.HOST_LINKED', selector == 'TRUE')])
    replies.extend([host, ('STREAM.CONNECT', 'queue_next_switch' if selector=='TRUE' else selector+'_OK')])


def _convert(key, value):
    if key in BOOLS:
        return _boolean(value)
    if key in INTS:
        return int(value)
    return str(value)


def _reply(data, status):
    pairs = []
    for key,value in data.items():
        if isinstance(value, bool):
            value = 'TRUE' if value else 'FALSE'
        pairs.append(key+'='+str(value))
    return '&' + '&'.join(pairs + ['API.STATUS='+status]) + '\n'


def _refusal():
    # The document gives no refusal vocabulary. This is deliberately named
    # synthetic, not presented as a device-observed error code.
    return '&API.STATUS=SIMULATED_REFUSAL\n'
