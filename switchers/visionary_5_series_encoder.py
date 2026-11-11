"""Visionary 5 Series encoder control, API 3.x (25-00186-042 Rev 06).

One device per endpoint, one driver per role. Python preserves ordered duplicate
keys in a request, fans variable-length key/value replies into typed state, and
narrows the controls to the reported model. HTTP Basic on port 80; POST writes
form data and GET reads. Poll only: the manufacturer documents no subscription.
Every write with feedback is read back; a queued switch is not an applied switch.
Changes remain volatile until the explicit Save Settings command. A route never
saves settings, changes the AV network or discards video-wall cropping.

Source: manufacturer API supplied for this integration; supplementary E5200/D5200
manual (EDID1/2/3, CEC, STREAM.HOST_LINKED and TCP 6752 serial pass-through):
https://visionary-av.com/wp-content/uploads/2024/10/VISIONARY-ED5200-UserManual-241023.pdf
No hardware qualification yet. The simulator models these documents, not captures.
"""
from copy import deepcopy
import asyncio
import ipaddress
import json
import re
from urllib.parse import urlencode

import httpx

from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError
from openavc.drivers.command_files import DeviceFile

ROLE = 'encoder'
DEFAULT_MODEL = 'E5200'
MODELS = ['E5100', 'E5200', 'DuetE-5']
FIELDS = {'UNIT.MODEL': ('model', 'string'),
 'UNIT.FIRMWARE': ('firmware', 'string'),
 'UNIT.FIRMWARE_DATE': ('firmware_date', 'string'),
 'UNIT.SERIAL': ('serial_number', 'string'),
 'UNIT.MAC_ADDRESS': ('mac_address', 'string'),
 'IP.ADDRESS': ('ip_address', 'string'),
 'IP.NETMASK': ('netmask', 'string'),
 'IP.GATEWAY': ('gateway', 'string'),
 'IP.DNS_SERVER': ('dns_server', 'string'),
 'IP.MODE': ('ip_mode', 'string'),
 'UNIT.DUET_INSTALLED': ('dante_installed', 'boolean'),
 'IR.ENABLED': ('ir_enabled', 'boolean'),
 'UNIT.ID': ('unit_name', 'string'),
 'UNIT.LOCATION': ('location', 'string'),
 'AUDIO.VOLUME': ('volume', 'integer'),
 'STREAM.AUDIO': ('stream_audio_source', 'enum'),
 'STREAM.MODE': ('stream_mode', 'enum'),
 'VIDEO.HDCP_FORCE_ON': ('hdcp_mode', 'enum'),
 'USB.ENABLED': ('usb_enabled', 'boolean'),
 'USB.MODE': ('usb_mode', 'enum'),
 'USB.KVM_FAST': ('fast_kvm', 'boolean'),
 'SERIAL.ENABLED': ('serial_enabled', 'boolean'),
 'SERIAL.BAUDRATE': ('serial_baud', 'integer'),
 'SERIAL.DATABITS': ('serial_data_bits', 'integer'),
 'SERIAL.PARITY': ('serial_parity', 'enum'),
 'SERIAL.STOPBITS': ('serial_stop_bits', 'integer'),
 'DANTE.DEVICE_NAME': ('dante_name', 'string'),
 'STREAM.VIDEO': ('video_input', 'enum'),
 'VIDEO.HDMI_OUT': ('loop_input', 'enum'),
 'STREAM.VIDEO_PRIORITY': ('video_priority', 'enum'),
 'VIDEO.HDMI_OUT_PRIORITY': ('loop_priority', 'enum'),
 'STREAM.BIT_RATE': ('bit_rate', 'enum'),
 'STREAM.FRAME_RATE': ('frame_rate', 'integer'),
 'STREAM.QOS_DSCP': ('qos_dscp', 'integer')}
WRITES = {'unit_name': 'UNIT.SET_ID',
 'location': 'UNIT.LOCATION',
 'volume': 'AUDIO.VOLUME',
 'stream_audio_source': 'STREAM.AUDIO',
 'stream_mode': 'STREAM.MODE',
 'hdcp_mode': 'VIDEO.HDCP_FORCE_ON',
 'usb_enabled': 'USB.ENABLED',
 'usb_mode': 'USB.MODE',
 'fast_kvm': 'USB.KVM_FAST',
 'serial_enabled': 'SERIAL.ENABLED',
 'serial_baud': 'SERIAL.BAUDRATE',
 'serial_data_bits': 'SERIAL.DATABITS',
 'serial_parity': 'SERIAL.PARITY',
 'serial_stop_bits': 'SERIAL.STOPBITS',
 'dante_name': 'DANTE.SET_DEVICE_NAME',
 'video_input': 'STREAM.VIDEO',
 'loop_input': 'VIDEO.HDMI_OUT',
 'video_priority': 'STREAM.VIDEO_PRIORITY',
 'loop_priority': 'VIDEO.HDMI_OUT_PRIORITY',
 'bit_rate': 'STREAM.BIT_RATE',
 'frame_rate': 'STREAM.FRAME_RATE',
 'qos_dscp': 'STREAM.QOS_DSCP'}
FEATURE_GROUPS = {'unit_name': 'common',
 'location': 'common',
 'volume': 'common',
 'stream_audio_source': 'common',
 'stream_mode': 'common',
 'hdcp_mode': 'common',
 'usb_enabled': 'common',
 'usb_mode': 'common',
 'fast_kvm': 'common',
 'serial_enabled': 'common',
 'serial_baud': 'common',
 'serial_data_bits': 'common',
 'serial_parity': 'common',
 'serial_stop_bits': 'common',
 'dante_name': 'dante',
 'video_input': 'multi',
 'loop_input': 'multi',
 'video_priority': 'multi',
 'loop_priority': 'multi',
 'bit_rate': 'common',
 'frame_rate': 'common',
 'qos_dscp': 'common'}
FIXED = {'save_settings': [('UNIT.SAVE', 'TRUE')],
 'identify': [('UNIT.FU', 'TRUE')],
 'reboot': [('UNIT.REBOOT', 'TRUE')],
 'factory_reset': [('UNIT.FACTORY_DEFAULT', 'TRUE')],
 'reboot_dante': [('DANTE.REBOOT', 'TRUE')],
 'display_on': [('VIDEO.SEND_CEC_GENERIC', '10:04')],
 'display_off': [('VIDEO.SEND_CEC_GENERIC', '1F:36')]}
ROUTES = {'linked': ('STREAM.HOST', 'TRUE'),
 'video': ('STREAM.HOST_VIDEO', 'VIDEO'),
 'audio': ('STREAM.HOST_AUDIO', 'AUDIO'),
 'usb': ('STREAM.HOST_USB', 'USB')}
MODEL_FEATURES = {'E5100': [],
 'E5200': ['multi', 'edid', 'ir'],
 'DuetE-5': ['multi', 'duet', 'dante', 'ir'],
 'D5100': [],
 'D5200': ['audio_outputs', 'ir'],
 'DuetD-5': ['audio_outputs', 'duet', 'dante', 'ir']}
BEZEL_KEYS = ['VW.OVERALL_WIDTH', 'VW.OVERALL_HEIGHT', 'VW.VISIBLE_WIDTH', 'VW.VISIBLE_HEIGHT']
BAUD_RATES = [300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
SUCCESS = ['SUCCESS', 'SUCCESS_QUERY', 'SUCCESS_QUERY_X', 'SUCCESS_UTILITY']
READ_KEYS = {key: wire for wire, (key, kind) in FIELDS.items()}


class Visionary5SeriesEncoderDriver(BaseDriver):
    DRIVER_INFO = {'id': 'visionary_5_series_encoder',
     'name': 'Visionary 5 Series Encoder',
     'manufacturer': 'Visionary Solutions',
     'category': 'switcher',
     'version': '1.0.1',
     'author': 'OpenAVC',
     'transport': 'http',
     'ports': [80, 8000],
     'protocols': ['visionary-5-series'],
     'simulated': True,
     'verified': False,
     'source_url': 'https://visionary-av.com/wp-content/uploads/2024/10/VISIONARY-ED5200-UserManual-241023.pdf',
     'min_platform_version': '0.37.2',
     'description': 'Controls one Visionary 5 Series encoder: input and loop output, audio, bandwidth, USB and '
                    'device configuration.',
     'tags': ['av-over-ip', 'visionary', '5-series', 'encoder', 'usb', 'edid'],
     'compatible_models': [{'manufacturer': 'Visionary Solutions',
                            'models': ['E5100', 'E5200', 'DuetE-5'],
                            'confidence': 'untested',
                            'notes': 'API 3.x. Model-specific controls follow the reported model. Hardware '
                                     'qualification is pending.'}],
     'help': {'overview': 'Add each encoder and decoder as its own device. Values shown are read from the '
                          'equipment. Changes are temporary until Save Settings stores all current parameters, '
                          'including routes.',
              'setup': 'Use the endpoint IP address and its web login. Factory login is admin / admin; enter the '
                       'password. Configure the AV network in the Visionary web interface. Choose Model for '
                       'offline design; it updates when the unit reports its model. Use Set Input EDID to change '
                       'an E5200 input’s EDID. Save Settings keeps all current values. Reboot is separate; save '
                       'before rebooting. For serial pass-through on E5200/D5200, configure matching serial '
                       'settings, save and reboot first, then select Through a Bridge on the downstream device.'},
     'default_config': {'host': '',
                        'port': 80,
                        'username': 'admin',
                        'password': '',
                        'model': 'E5200',
                        'poll_interval': 5,
                        'dante_module': False},
     'config_schema': {'host': {'type': 'string', 'label': 'IP Address', 'required': True},
                       'port': {'type': 'integer',
                                'label': 'HTTP Port',
                                'default': 80,
                                'min': 1,
                                'max': 65535,
                                'advanced': True},
                       'username': {'type': 'string', 'label': 'Username', 'default': 'admin', 'required': True},
                       'password': {'type': 'string',
                                    'label': 'Password',
                                    'default': '',
                                    'secret': True,
                                    'required': True},
                       'model': {'type': 'enum',
                                 'label': 'Model',
                                 'values': ['E5100', 'E5200', 'DuetE-5'],
                                 'default': 'E5200',
                                 'learned_from': 'model',
                                 'help': 'Used before connection and in simulation. The connected unit reports '
                                         'its model.'},
                       'dante_module': {'type': 'boolean',
                                        'label': 'Dante Module Installed',
                                        'default': False,
                                        'advanced': True,
                                        'learned_from': 'dante_installed',
                                        'help': 'For offline design and simulation of an optional Dante module. '
                                                'Updated when the endpoint reports its hardware.'},
                       'poll_interval': {'type': 'number',
                                         'label': 'Poll Interval',
                                         'default': 5,
                                         'min': 0,
                                         'advanced': True,
                                         'help': 'Seconds between status reads. Zero pauses status polling; '
                                                 'connection monitoring continues.'}},
     'state_variables': {'model': {'type': 'string', 'label': 'Model', 'default': None, 'cloud_priority': 'low'},
                         'firmware': {'type': 'string',
                                      'label': 'Firmware',
                                      'default': None,
                                      'cloud_priority': 'low'},
                         'firmware_date': {'type': 'string',
                                           'label': 'Firmware Date',
                                           'default': None,
                                           'cloud_priority': 'low'},
                         'serial_number': {'type': 'string',
                                           'label': 'Serial Number',
                                           'default': None,
                                           'cloud_priority': 'low'},
                         'mac_address': {'type': 'string',
                                         'label': 'MAC Address',
                                         'default': None,
                                         'cloud_priority': 'low'},
                         'ip_address': {'type': 'string',
                                        'label': 'IP Address',
                                        'default': None,
                                        'cloud_priority': 'low'},
                         'netmask': {'type': 'string',
                                     'label': 'Netmask',
                                     'default': None,
                                     'cloud_priority': 'low'},
                         'gateway': {'type': 'string',
                                     'label': 'Gateway',
                                     'default': None,
                                     'cloud_priority': 'low'},
                         'dns_server': {'type': 'string',
                                        'label': 'DNS Server',
                                        'default': None,
                                        'cloud_priority': 'low'},
                         'ip_mode': {'type': 'string',
                                     'label': 'IP Mode',
                                     'default': None,
                                     'cloud_priority': 'low'},
                         'dante_installed': {'type': 'boolean',
                                             'label': 'Dante Module Installed',
                                             'default': None},
                         'ir_enabled': {'type': 'boolean', 'label': 'IR Enabled', 'default': None},
                         'last_error': {'type': 'string',
                                        'label': 'Last Error',
                                        'default': None,
                                        'cloud_priority': 'high'},
                         'video_timing': {'type': 'string',
                                          'label': 'Video Timing',
                                          'default': None,
                                          'cloud_priority': 'low'},
                         'video_present': {'type': 'boolean',
                                           'label': 'Video Signal Present',
                                           'default': None,
                                           'cloud_priority': 'high'},
                         'video_resolution': {'type': 'string', 'label': 'Video Resolution', 'default': None},
                         'hdcp': {'type': 'string', 'label': 'HDCP', 'default': None},
                         'unit_name': {'type': 'string', 'label': 'Unit Name', 'default': None, 'control': True},
                         'location': {'type': 'string', 'label': 'Location', 'default': None, 'control': True},
                         'volume': {'type': 'integer',
                                    'label': 'Analog Input Level',
                                    'default': None,
                                    'control': True,
                                    'min': 0,
                                    'step': 1,
                                    'max': 100},
                         'stream_audio_source': {'type': 'enum',
                                                 'label': 'Stream Audio Source',
                                                 'default': None,
                                                 'control': True,
                                                 'values': ['HDMI', 'ANALOG', 'DANTE']},
                         'stream_mode': {'type': 'enum',
                                         'label': 'Stream Mode',
                                         'default': None,
                                         'control': True,
                                         'values': ['multicast', 'unicast'],
                                         'help': 'Selects multicast or unicast operation. The encoder and '
                                                 'decoder must use matching modes.'},
                         'hdcp_mode': {'type': 'enum',
                                       'label': 'HDCP Mode',
                                       'default': None,
                                       'control': True,
                                       'values': ['REJECT', 'SOURCE', '1_X', '2_X']},
                         'usb_enabled': {'type': 'boolean',
                                         'label': 'USB over IP',
                                         'default': None,
                                         'control': True,
                                         'help': 'Enables or disables the USB connection between endpoints.'},
                         'usb_mode': {'type': 'enum',
                                      'label': 'USB Mode',
                                      'default': None,
                                      'control': True,
                                      'values': ['auto', 'link', 'request']},
                         'fast_kvm': {'type': 'boolean',
                                      'label': 'Fast KVM',
                                      'default': None,
                                      'control': True,
                                      'help': 'Speeds switching for keyboards and mice. Turn off for '
                                              'touchscreens. Match both endpoints, then save and reboot.'},
                         'serial_enabled': {'type': 'boolean',
                                            'label': 'Serial Enabled',
                                            'default': None,
                                            'control': True,
                                            'help': 'Enables RS-232. Save and reboot after commissioning the '
                                                    'serial port.'},
                         'serial_baud': {'type': 'integer',
                                         'label': 'Serial Baud Rate',
                                         'default': None,
                                         'control': True,
                                         'min': 300,
                                         'step': 1,
                                         'max': 115200,
                                         'help': 'Sets the RS-232 baud rate. Use a supported rate: 300, 600, '
                                                 '1200, 2400, 4800, 9600, 19200, 38400, 57600 or 115200.'},
                         'serial_data_bits': {'type': 'integer',
                                              'label': 'Serial Data Bits',
                                              'default': None,
                                              'control': True,
                                              'min': 5,
                                              'step': 1,
                                              'max': 8},
                         'serial_parity': {'type': 'enum',
                                           'label': 'Serial Parity',
                                           'default': None,
                                           'control': True,
                                           'values': [{'value': 'n', 'label': 'None'},
                                                      {'value': 'o', 'label': 'Odd'},
                                                      {'value': 'e', 'label': 'Even'}]},
                         'serial_stop_bits': {'type': 'integer',
                                              'label': 'Serial Stop Bits',
                                              'default': None,
                                              'control': True,
                                              'min': 1,
                                              'step': 1,
                                              'max': 2},
                         'dante_name': {'type': 'string',
                                        'label': 'Dante Device Name',
                                        'default': None,
                                        'control': True},
                         'video_input': {'type': 'enum',
                                         'label': 'Stream Video Input',
                                         'default': None,
                                         'control': True,
                                         'values': ['AUTO', 'HDMI1', 'HDMI2', 'USB-C']},
                         'loop_input': {'type': 'enum',
                                        'label': 'Loop Output Input',
                                        'default': None,
                                        'control': True,
                                        'values': ['AUTO', 'HDMI1', 'HDMI2', 'USB-C']},
                         'video_priority': {'type': 'enum',
                                            'label': 'Stream Input Priority',
                                            'default': None,
                                            'control': True,
                                            'values': [{'value': '1', 'label': 'HDMI 1, HDMI 2, USB-C'},
                                                       {'value': '2', 'label': 'HDMI 1, USB-C, HDMI 2'},
                                                       {'value': '3', 'label': 'HDMI 2, HDMI 1, USB-C'},
                                                       {'value': '4', 'label': 'HDMI 2, USB-C, HDMI 1'},
                                                       {'value': '5', 'label': 'USB-C, HDMI 1, HDMI 2'},
                                                       {'value': '6', 'label': 'USB-C, HDMI 2, HDMI 1'}]},
                         'loop_priority': {'type': 'enum',
                                           'label': 'Loop Input Priority',
                                           'default': None,
                                           'control': True,
                                           'values': [{'value': '1', 'label': 'HDMI 1, HDMI 2, USB-C'},
                                                      {'value': '2', 'label': 'HDMI 1, USB-C, HDMI 2'},
                                                      {'value': '3', 'label': 'HDMI 2, HDMI 1, USB-C'},
                                                      {'value': '4', 'label': 'HDMI 2, USB-C, HDMI 1'},
                                                      {'value': '5', 'label': 'USB-C, HDMI 1, HDMI 2'},
                                                      {'value': '6', 'label': 'USB-C, HDMI 2, HDMI 1'}]},
                         'bit_rate': {'type': 'enum',
                                      'label': 'Stream Bit Rate',
                                      'default': None,
                                      'control': True,
                                      'values': [{'value': 'auto', 'label': 'Automatic'},
                                                 {'value': '200M', 'label': '200 Mbps'},
                                                 {'value': '150M', 'label': '150 Mbps'},
                                                 {'value': '100M', 'label': '100 Mbps'},
                                                 {'value': '50M', 'label': '50 Mbps'}]},
                         'frame_rate': {'type': 'integer',
                                        'label': 'Stream Frame Rate',
                                        'default': None,
                                        'control': True,
                                        'min': 1,
                                        'step': 1,
                                        'max': 60},
                         'qos_dscp': {'type': 'integer',
                                      'label': 'Stream DSCP',
                                      'default': None,
                                      'control': True,
                                      'min': 0,
                                      'step': 1,
                                      'max': 63}},
     'commands': {'refresh_status': {'label': 'Refresh Status',
                                     'help': 'Reads the current device settings and video timing.',
                                     'params': {},
                                     'query_for': 'model'},
                  'set_unit_name': {'label': 'Set Unit Name',
                                    'help': 'Sets unit name on this encoder. Use Save Settings to keep it after '
                                            'a reboot.',
                                    'params': {'value': {'type': 'string',
                                                         'label': 'Unit Name',
                                                         'required': True,
                                                         'pattern': '^[^&=\\r\\n\\x00]{0,64}$'}},
                                    'sets': {'unit_name': '{value}'}},
                  'set_location': {'label': 'Set Location',
                                   'help': 'Sets location on this encoder. Use Save Settings to keep it after a '
                                           'reboot.',
                                   'params': {'value': {'type': 'string',
                                                        'label': 'Location',
                                                        'required': True,
                                                        'pattern': '^[^&=\\r\\n\\x00]{0,64}$'}},
                                   'sets': {'location': '{value}'}},
                  'set_volume': {'label': 'Set Analog Input Level',
                                 'help': 'Sets analog input level on this encoder. Use Save Settings to keep it '
                                         'after a reboot.',
                                 'params': {'value': {'type': 'integer',
                                                      'label': 'Analog Input Level',
                                                      'required': True,
                                                      'min': 0,
                                                      'max': 100}},
                                 'sets': {'volume': '{value}'}},
                  'set_stream_audio_source': {'label': 'Set Stream Audio Source',
                                              'help': 'Sets stream audio source on this encoder. Use Save '
                                                      'Settings to keep it after a reboot.',
                                              'params': {'value': {'type': 'enum',
                                                                   'label': 'Stream Audio Source',
                                                                   'required': True,
                                                                   'values': ['HDMI', 'ANALOG', 'DANTE']}},
                                              'sets': {'stream_audio_source': '{value}'}},
                  'set_stream_mode': {'label': 'Set Stream Mode',
                                      'help': 'Selects multicast or unicast operation. The encoder and decoder '
                                              'must use matching modes. Use Save Settings to keep it after a '
                                              'reboot.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Stream Mode',
                                                           'required': True,
                                                           'values': ['multicast', 'unicast']}},
                                      'sets': {'stream_mode': '{value}'}},
                  'set_hdcp_mode': {'label': 'Set HDCP Mode',
                                    'help': 'Sets hdcp mode on this encoder. Use Save Settings to keep it after '
                                            'a reboot.',
                                    'params': {'value': {'type': 'enum',
                                                         'label': 'HDCP Mode',
                                                         'required': True,
                                                         'values': ['REJECT', 'SOURCE', '1_X', '2_X']}},
                                    'sets': {'hdcp_mode': '{value}'}},
                  'set_usb_enabled': {'label': 'Set USB over IP',
                                      'help': 'Enables or disables the USB connection between endpoints. Use '
                                              'Save Settings to keep it after a reboot.',
                                      'params': {'value': {'type': 'boolean',
                                                           'label': 'USB over IP',
                                                           'required': True}},
                                      'sets': {'usb_enabled': '{value}'}},
                  'set_usb_mode': {'label': 'Set USB Mode',
                                   'help': 'Sets usb mode on this encoder. Use Save Settings to keep it after a '
                                           'reboot.',
                                   'params': {'value': {'type': 'enum',
                                                        'label': 'USB Mode',
                                                        'required': True,
                                                        'values': ['auto', 'link', 'request']}},
                                   'sets': {'usb_mode': '{value}'}},
                  'set_fast_kvm': {'label': 'Set Fast KVM',
                                   'help': 'Speeds switching for keyboards and mice. Turn off for touchscreens. '
                                           'Match both endpoints, then save and reboot. Use Save Settings to '
                                           'keep it after a reboot.',
                                   'params': {'value': {'type': 'boolean',
                                                        'label': 'Fast KVM',
                                                        'required': True}},
                                   'sets': {'fast_kvm': '{value}'}},
                  'set_serial_enabled': {'label': 'Set Serial Enabled',
                                         'help': 'Enables RS-232. Save and reboot after commissioning the serial '
                                                 'port. Use Save Settings to keep it after a reboot.',
                                         'params': {'value': {'type': 'boolean',
                                                              'label': 'Serial Enabled',
                                                              'required': True}},
                                         'sets': {'serial_enabled': '{value}'}},
                  'set_serial_baud': {'label': 'Set Serial Baud Rate',
                                      'help': 'Sets the RS-232 baud rate. Use a supported rate: 300, 600, 1200, '
                                              '2400, 4800, 9600, 19200, 38400, 57600 or 115200. Use Save '
                                              'Settings to keep it after a reboot.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Serial Baud Rate',
                                                           'required': True,
                                                           'values': [300,
                                                                      600,
                                                                      1200,
                                                                      2400,
                                                                      4800,
                                                                      9600,
                                                                      19200,
                                                                      38400,
                                                                      57600,
                                                                      115200]}},
                                      'sets': {'serial_baud': '{value}'}},
                  'set_serial_data_bits': {'label': 'Set Serial Data Bits',
                                           'help': 'Sets serial data bits on this encoder. Use Save Settings to '
                                                   'keep it after a reboot.',
                                           'params': {'value': {'type': 'integer',
                                                                'label': 'Serial Data Bits',
                                                                'required': True,
                                                                'min': 5,
                                                                'max': 8}},
                                           'sets': {'serial_data_bits': '{value}'}},
                  'set_serial_parity': {'label': 'Set Serial Parity',
                                        'help': 'Sets serial parity on this encoder. Use Save Settings to keep '
                                                'it after a reboot.',
                                        'params': {'value': {'type': 'enum',
                                                             'label': 'Serial Parity',
                                                             'required': True,
                                                             'values': [{'value': 'n', 'label': 'None'},
                                                                        {'value': 'o', 'label': 'Odd'},
                                                                        {'value': 'e', 'label': 'Even'}]}},
                                        'sets': {'serial_parity': '{value}'}},
                  'set_serial_stop_bits': {'label': 'Set Serial Stop Bits',
                                           'help': 'Sets serial stop bits on this encoder. Use Save Settings to '
                                                   'keep it after a reboot.',
                                           'params': {'value': {'type': 'integer',
                                                                'label': 'Serial Stop Bits',
                                                                'required': True,
                                                                'min': 1,
                                                                'max': 2}},
                                           'sets': {'serial_stop_bits': '{value}'}},
                  'set_dante_name': {'label': 'Set Dante Device Name',
                                     'help': 'Sets dante device name on this encoder. Use Save Settings to keep '
                                             'it after a reboot.',
                                     'params': {'value': {'type': 'string',
                                                          'label': 'Dante Device Name',
                                                          'required': True,
                                                          'pattern': '^[^&=\\r\\n\\x00]{0,64}$'}},
                                     'sets': {'dante_name': '{value}'}},
                  'save_settings': {'label': 'Save Settings',
                                    'help': 'Saves all current parameters, including routes, for the next '
                                            'reboot.',
                                    'params': {},
                                    'confirm': 'Save all current parameters, including source routes, as the '
                                               'startup settings?'},
                  'identify': {'label': 'Identify Unit',
                               'help': 'Flashes the front LEDs for about 10 seconds so you can find this unit.',
                               'params': {}},
                  'reboot': {'label': 'Reboot',
                             'help': 'Restarts the endpoint. Audio, video and USB stop until it is back; unsaved '
                                     'changes are lost.',
                             'params': {},
                             'confirm': 'Restart this endpoint? Unsaved changes will be lost and its streams '
                                        'will stop temporarily.'},
                  'factory_reset': {'label': 'Factory Reset',
                                    'help': 'Erases the endpoint configuration and restores factory settings.',
                                    'params': {},
                                    'confirm': 'Erase this endpoint configuration and restore factory settings?'},
                  'reboot_dante': {'label': 'Reboot Dante Module',
                                   'help': 'Restarts the Dante module. Dante audio stops temporarily.',
                                   'params': {},
                                   'confirm': 'Restart the Dante module and interrupt its audio?'},
                  'download_diagnostics': {'label': 'Download Diagnostics',
                                           'help': 'Downloads the device settings and video timing for '
                                                   'troubleshooting. Login fields are omitted.',
                                           'params': {},
                                           'returns_file': True},
                  'send_ir': {'label': 'Send IR Code',
                              'help': 'Sends a Visionary-format IR code through the IR output. Enable IR in the '
                                      'device web interface first.',
                              'params': {'code': {'type': 'string',
                                                  'label': 'Visionary IR Code',
                                                  'required': True,
                                                  'pattern': '^[0-9]+(?:,[0-9]+)+$',
                                                  'help': 'Comma-separated Visionary IR values. This is not a '
                                                          'Pronto hex string.'}}},
                  'set_gpio': {'label': 'Set GPIO Output',
                               'help': 'Sets a GPIO output high or low. Output 2 is available only with IR '
                                       'disabled.',
                               'params': {'output': {'type': 'child_id',
                                                     'label': 'Output',
                                                     'required': True,
                                                     'child_type': 'gpio_output'},
                                          'high': {'type': 'boolean', 'label': 'High', 'required': True}},
                               'sets': {'high': '{high}'}},
                  'set_video_input': {'label': 'Set Stream Video Input',
                                      'help': 'Sets stream video input on this encoder. Use Save Settings to '
                                              'keep it after a reboot.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Stream Video Input',
                                                           'required': True,
                                                           'values': ['AUTO', 'HDMI1', 'HDMI2', 'USB-C']}},
                                      'sets': {'video_input': '{value}'}},
                  'set_loop_input': {'label': 'Set Loop Output Input',
                                     'help': 'Sets loop output input on this encoder. Use Save Settings to keep '
                                             'it after a reboot.',
                                     'params': {'value': {'type': 'enum',
                                                          'label': 'Loop Output Input',
                                                          'required': True,
                                                          'values': ['AUTO', 'HDMI1', 'HDMI2', 'USB-C']}},
                                     'sets': {'loop_input': '{value}'}},
                  'set_video_priority': {'label': 'Set Stream Input Priority',
                                         'help': 'Sets stream input priority on this encoder. Use Save Settings '
                                                 'to keep it after a reboot.',
                                         'params': {'value': {'type': 'enum',
                                                              'label': 'Stream Input Priority',
                                                              'required': True,
                                                              'values': [{'value': '1',
                                                                          'label': 'HDMI 1, HDMI 2, USB-C'},
                                                                         {'value': '2',
                                                                          'label': 'HDMI 1, USB-C, HDMI 2'},
                                                                         {'value': '3',
                                                                          'label': 'HDMI 2, HDMI 1, USB-C'},
                                                                         {'value': '4',
                                                                          'label': 'HDMI 2, USB-C, HDMI 1'},
                                                                         {'value': '5',
                                                                          'label': 'USB-C, HDMI 1, HDMI 2'},
                                                                         {'value': '6',
                                                                          'label': 'USB-C, HDMI 2, HDMI 1'}]}},
                                         'sets': {'video_priority': '{value}'}},
                  'set_loop_priority': {'label': 'Set Loop Input Priority',
                                        'help': 'Sets loop input priority on this encoder. Use Save Settings to '
                                                'keep it after a reboot.',
                                        'params': {'value': {'type': 'enum',
                                                             'label': 'Loop Input Priority',
                                                             'required': True,
                                                             'values': [{'value': '1',
                                                                         'label': 'HDMI 1, HDMI 2, USB-C'},
                                                                        {'value': '2',
                                                                         'label': 'HDMI 1, USB-C, HDMI 2'},
                                                                        {'value': '3',
                                                                         'label': 'HDMI 2, HDMI 1, USB-C'},
                                                                        {'value': '4',
                                                                         'label': 'HDMI 2, USB-C, HDMI 1'},
                                                                        {'value': '5',
                                                                         'label': 'USB-C, HDMI 1, HDMI 2'},
                                                                        {'value': '6',
                                                                         'label': 'USB-C, HDMI 2, HDMI 1'}]}},
                                        'sets': {'loop_priority': '{value}'}},
                  'set_bit_rate': {'label': 'Set Stream Bit Rate',
                                   'help': 'Sets stream bit rate on this encoder. Use Save Settings to keep it '
                                           'after a reboot.',
                                   'params': {'value': {'type': 'enum',
                                                        'label': 'Stream Bit Rate',
                                                        'required': True,
                                                        'values': [{'value': 'auto', 'label': 'Automatic'},
                                                                   {'value': '200M', 'label': '200 Mbps'},
                                                                   {'value': '150M', 'label': '150 Mbps'},
                                                                   {'value': '100M', 'label': '100 Mbps'},
                                                                   {'value': '50M', 'label': '50 Mbps'}]}},
                                   'sets': {'bit_rate': '{value}'}},
                  'set_frame_rate': {'label': 'Set Stream Frame Rate',
                                     'help': 'Sets stream frame rate on this encoder. Use Save Settings to keep '
                                             'it after a reboot.',
                                     'params': {'value': {'type': 'integer',
                                                          'label': 'Stream Frame Rate',
                                                          'required': True,
                                                          'min': 1,
                                                          'max': 60}},
                                     'sets': {'frame_rate': '{value}'}},
                  'set_qos_dscp': {'label': 'Set Stream DSCP',
                                   'help': 'Sets stream dscp on this encoder. Use Save Settings to keep it after '
                                           'a reboot.',
                                   'params': {'value': {'type': 'integer',
                                                        'label': 'Stream DSCP',
                                                        'required': True,
                                                        'min': 0,
                                                        'max': 63}},
                                   'sets': {'qos_dscp': '{value}'}},
                  'set_edid': {'label': 'Set Input EDID',
                               'help': 'Changes the EDID offered to the source on this E5200 input. Use Save '
                                       'Settings to keep it. The source may need reconnecting to read it.',
                               'params': {'input': {'type': 'child_id',
                                                    'label': 'Input',
                                                    'required': True,
                                                    'child_type': 'input'},
                                          'edid': {'type': 'enum',
                                                   'label': 'EDID',
                                                   'required': True,
                                                   'values': [{'value': '01_DEFAULT', 'label': 'Default'},
                                                              {'value': '02_DEFAULT_LPCM',
                                                               'label': 'Default with PCM Audio'},
                                                              {'value': '03_1080P60_LPCM_2CH',
                                                               'label': '1080p60 — Stereo PCM'},
                                                              {'value': '04_1080P60_LPCM_268CH',
                                                               'label': '1080p60 — 2/6/8-channel PCM'},
                                                              {'value': '05_2160P60_LPCM_2CH',
                                                               'label': '2160p60 — Stereo PCM'},
                                                              {'value': '06_2160P60_LPCM_268CH',
                                                               'label': '2160p60 — 2/6/8-channel PCM'},
                                                              {'value': '07_720P60_LPCM_2CH',
                                                               'label': '720p60 — Stereo PCM'},
                                                              {'value': '08_2160P60_1080P_LPCM_2CH',
                                                               'label': '2160p60 / 1080p — Stereo PCM'},
                                                              {'value': '09_1080P59_LPCM_2CH',
                                                               'label': '1080p59 — Stereo PCM'},
                                                              {'value': '10_1080P50_LPCM_2CH',
                                                               'label': '1080p50 — Stereo PCM'},
                                                              {'value': '11_1920x1200P60_LPCM_2CH',
                                                               'label': '1920 × 1200 at 60 Hz — Stereo PCM'},
                                                              {'value': '12_1920x1024P60_LPCM_2CH',
                                                               'label': '1920 × 1024 at 60 Hz — Stereo PCM'},
                                                              {'value': '13_1024x768P60_LPCM_2CH',
                                                               'label': '1024 × 768 at 60 Hz — Stereo PCM'},
                                                              {'value': '14_3840x1080P60_LPCM_2CH',
                                                               'label': '3840 × 1080 at 60 Hz — Stereo PCM'}]}},
                               'sets': {'edid': '{edid}'}}},
     'device_settings': {'unit_name': {'type': 'string',
                                       'label': 'Unit Name',
                                       'state_key': 'unit_name',
                                       'default': '',
                                       'setup': False,
                                       'help': 'Sets unit name on this encoder. Temporary until Save Settings; '
                                               'that saves all current parameters, including routes.',
                                       'regex': '^[^&=\\r\\n\\x00]{0,64}$'},
                         'location': {'type': 'string',
                                      'label': 'Location',
                                      'state_key': 'location',
                                      'default': '',
                                      'setup': False,
                                      'help': 'Sets location on this encoder. Temporary until Save Settings; '
                                              'that saves all current parameters, including routes.',
                                      'regex': '^[^&=\\r\\n\\x00]{0,64}$'},
                         'stream_audio_source': {'type': 'enum',
                                                 'label': 'Stream Audio Source',
                                                 'state_key': 'stream_audio_source',
                                                 'default': 'HDMI',
                                                 'setup': False,
                                                 'help': 'Sets stream audio source on this encoder. Temporary '
                                                         'until Save Settings; that saves all current '
                                                         'parameters, including routes.',
                                                 'values': ['HDMI', 'ANALOG', 'DANTE']},
                         'stream_mode': {'type': 'enum',
                                         'label': 'Stream Mode',
                                         'state_key': 'stream_mode',
                                         'default': 'multicast',
                                         'setup': False,
                                         'help': 'Selects multicast or unicast operation. The encoder and '
                                                 'decoder must use matching modes. Temporary until Save '
                                                 'Settings; that saves all current parameters, including routes.',
                                         'values': ['multicast', 'unicast']},
                         'hdcp_mode': {'type': 'enum',
                                       'label': 'HDCP Mode',
                                       'state_key': 'hdcp_mode',
                                       'default': 'SOURCE',
                                       'setup': False,
                                       'help': 'Sets hdcp mode on this encoder. Temporary until Save Settings; '
                                               'that saves all current parameters, including routes.',
                                       'values': ['REJECT', 'SOURCE', '1_X', '2_X']},
                         'usb_enabled': {'type': 'boolean',
                                         'label': 'USB over IP',
                                         'state_key': 'usb_enabled',
                                         'default': True,
                                         'setup': False,
                                         'help': 'Enables or disables the USB connection between endpoints. '
                                                 'Temporary until Save Settings; that saves all current '
                                                 'parameters, including routes.'},
                         'usb_mode': {'type': 'enum',
                                      'label': 'USB Mode',
                                      'state_key': 'usb_mode',
                                      'default': 'link',
                                      'setup': False,
                                      'help': 'Sets usb mode on this encoder. Temporary until Save Settings; '
                                              'that saves all current parameters, including routes.',
                                      'values': ['auto', 'link', 'request']},
                         'fast_kvm': {'type': 'boolean',
                                      'label': 'Fast KVM',
                                      'state_key': 'fast_kvm',
                                      'default': True,
                                      'setup': False,
                                      'help': 'Speeds switching for keyboards and mice. Turn off for '
                                              'touchscreens. Match both endpoints, then save and reboot. '
                                              'Temporary until Save Settings; that saves all current parameters, '
                                              'including routes.'},
                         'serial_enabled': {'type': 'boolean',
                                            'label': 'Serial Enabled',
                                            'state_key': 'serial_enabled',
                                            'default': False,
                                            'setup': False,
                                            'help': 'Enables RS-232. Save and reboot after commissioning the '
                                                    'serial port. Temporary until Save Settings; that saves all '
                                                    'current parameters, including routes.'},
                         'serial_baud': {'type': 'enum',
                                         'label': 'Serial Baud Rate',
                                         'state_key': 'serial_baud',
                                         'default': 115200,
                                         'setup': False,
                                         'help': 'Sets the RS-232 baud rate. Use a supported rate: 300, 600, '
                                                 '1200, 2400, 4800, 9600, 19200, 38400, 57600 or 115200. '
                                                 'Temporary until Save Settings; that saves all current '
                                                 'parameters, including routes.',
                                         'min': 300,
                                         'max': 115200,
                                         'values': [300,
                                                    600,
                                                    1200,
                                                    2400,
                                                    4800,
                                                    9600,
                                                    19200,
                                                    38400,
                                                    57600,
                                                    115200]},
                         'serial_data_bits': {'type': 'integer',
                                              'label': 'Serial Data Bits',
                                              'state_key': 'serial_data_bits',
                                              'default': 8,
                                              'setup': False,
                                              'help': 'Sets serial data bits on this encoder. Temporary until '
                                                      'Save Settings; that saves all current parameters, '
                                                      'including routes.',
                                              'min': 5,
                                              'max': 8},
                         'serial_parity': {'type': 'enum',
                                           'label': 'Serial Parity',
                                           'state_key': 'serial_parity',
                                           'default': 'n',
                                           'setup': False,
                                           'help': 'Sets serial parity on this encoder. Temporary until Save '
                                                   'Settings; that saves all current parameters, including '
                                                   'routes.',
                                           'values': [{'value': 'n', 'label': 'None'},
                                                      {'value': 'o', 'label': 'Odd'},
                                                      {'value': 'e', 'label': 'Even'}]},
                         'serial_stop_bits': {'type': 'integer',
                                              'label': 'Serial Stop Bits',
                                              'state_key': 'serial_stop_bits',
                                              'default': 1,
                                              'setup': False,
                                              'help': 'Sets serial stop bits on this encoder. Temporary until '
                                                      'Save Settings; that saves all current parameters, '
                                                      'including routes.',
                                              'min': 1,
                                              'max': 2},
                         'dante_name': {'type': 'string',
                                        'label': 'Dante Device Name',
                                        'state_key': 'dante_name',
                                        'default': '',
                                        'setup': False,
                                        'help': 'Sets dante device name on this encoder. Temporary until Save '
                                                'Settings; that saves all current parameters, including routes.',
                                        'regex': '^[^&=\\r\\n\\x00]{0,64}$'},
                         'video_input': {'type': 'enum',
                                         'label': 'Stream Video Input',
                                         'state_key': 'video_input',
                                         'default': 'HDMI1',
                                         'setup': False,
                                         'help': 'Sets stream video input on this encoder. Temporary until Save '
                                                 'Settings; that saves all current parameters, including routes.',
                                         'values': ['AUTO', 'HDMI1', 'HDMI2', 'USB-C']},
                         'loop_input': {'type': 'enum',
                                        'label': 'Loop Output Input',
                                        'state_key': 'loop_input',
                                        'default': 'AUTO',
                                        'setup': False,
                                        'help': 'Sets loop output input on this encoder. Temporary until Save '
                                                'Settings; that saves all current parameters, including routes.',
                                        'values': ['AUTO', 'HDMI1', 'HDMI2', 'USB-C']},
                         'video_priority': {'type': 'enum',
                                            'label': 'Stream Input Priority',
                                            'state_key': 'video_priority',
                                            'default': '1',
                                            'setup': False,
                                            'help': 'Sets stream input priority on this encoder. Temporary until '
                                                    'Save Settings; that saves all current parameters, including '
                                                    'routes.',
                                            'values': [{'value': '1', 'label': 'HDMI 1, HDMI 2, USB-C'},
                                                       {'value': '2', 'label': 'HDMI 1, USB-C, HDMI 2'},
                                                       {'value': '3', 'label': 'HDMI 2, HDMI 1, USB-C'},
                                                       {'value': '4', 'label': 'HDMI 2, USB-C, HDMI 1'},
                                                       {'value': '5', 'label': 'USB-C, HDMI 1, HDMI 2'},
                                                       {'value': '6', 'label': 'USB-C, HDMI 2, HDMI 1'}]},
                         'loop_priority': {'type': 'enum',
                                           'label': 'Loop Input Priority',
                                           'state_key': 'loop_priority',
                                           'default': '1',
                                           'setup': False,
                                           'help': 'Sets loop input priority on this encoder. Temporary until '
                                                   'Save Settings; that saves all current parameters, including '
                                                   'routes.',
                                           'values': [{'value': '1', 'label': 'HDMI 1, HDMI 2, USB-C'},
                                                      {'value': '2', 'label': 'HDMI 1, USB-C, HDMI 2'},
                                                      {'value': '3', 'label': 'HDMI 2, HDMI 1, USB-C'},
                                                      {'value': '4', 'label': 'HDMI 2, USB-C, HDMI 1'},
                                                      {'value': '5', 'label': 'USB-C, HDMI 1, HDMI 2'},
                                                      {'value': '6', 'label': 'USB-C, HDMI 2, HDMI 1'}]},
                         'bit_rate': {'type': 'enum',
                                      'label': 'Stream Bit Rate',
                                      'state_key': 'bit_rate',
                                      'default': 'auto',
                                      'setup': False,
                                      'help': 'Sets stream bit rate on this encoder. Temporary until Save '
                                              'Settings; that saves all current parameters, including routes.',
                                      'values': [{'value': 'auto', 'label': 'Automatic'},
                                                 {'value': '200M', 'label': '200 Mbps'},
                                                 {'value': '150M', 'label': '150 Mbps'},
                                                 {'value': '100M', 'label': '100 Mbps'},
                                                 {'value': '50M', 'label': '50 Mbps'}]},
                         'frame_rate': {'type': 'integer',
                                        'label': 'Stream Frame Rate',
                                        'state_key': 'frame_rate',
                                        'default': 60,
                                        'setup': False,
                                        'help': 'Sets stream frame rate on this encoder. Temporary until Save '
                                                'Settings; that saves all current parameters, including routes.',
                                        'min': 1,
                                        'max': 60},
                         'qos_dscp': {'type': 'integer',
                                      'label': 'Stream DSCP',
                                      'state_key': 'qos_dscp',
                                      'default': 26,
                                      'setup': False,
                                      'help': 'Sets stream dscp on this encoder. Temporary until Save Settings; '
                                              'that saves all current parameters, including routes.',
                                      'min': 0,
                                      'max': 63}},
     'child_entity_types': {'gpio_output': {'label': 'GPIO Output',
                                            'label_plural': 'GPIO Outputs',
                                            'id_format': {'type': 'integer', 'min': 1, 'max': 2},
                                            'summary_fields': ['high'],
                                            'state_variables': {'high': {'type': 'boolean',
                                                                         'label': 'High',
                                                                         'default': None,
                                                                         'control': True}}},
                            'gpio_input': {'label': 'GPIO Input',
                                           'label_plural': 'GPIO Inputs',
                                           'id_format': {'type': 'integer', 'min': 1, 'max': 1},
                                           'summary_fields': ['high'],
                                           'state_variables': {'high': {'type': 'boolean',
                                                                        'label': 'High',
                                                                        'default': None,
                                                                        'cloud_priority': 'high'}}},
                            'input': {'label': 'Input',
                                      'label_plural': 'Inputs',
                                      'id_format': {'type': 'integer', 'min': 1, 'max': 3},
                                      'label_field': 'name',
                                      'summary_fields': ['edid'],
                                      'state_variables': {'name': {'type': 'string',
                                                                   'label': 'Input',
                                                                   'default': None},
                                                          'edid': {'type': 'string',
                                                                   'label': 'EDID',
                                                                   'default': None,
                                                                   'control': True}}}},
     'bridge': {'ports': [{'id': 'serial:1', 'kind': 'serial', 'passthrough_port': 6752, 'label': 'RS-232'}]},
     'discovery': {'udp_probe': {'port': 8000,
                                 'send_ascii': 'CMD=START&UNIT.ID=ALL&QUERY.KEY=UNIT.MODEL&CMD=END',
                                 'expect_regex': '(?:^|&)UNIT\\.MODEL=(E5100|E5200|DuetE-5)(?:&|\\r|\\n|$)',
                                 'extract': {'model': 'UNIT\\.MODEL=([^&\\r\\n]+)'},
                                 'timeout_ms': 1500}},
     'quick_actions': ['set_video_input', 'set_loop_input', 'set_volume', 'set_stream_audio_source'],
     'routing': {'source_param': 'value',
                 'planes': [{'label': 'Stream', 'route_property': 'video_input', 'command': 'set_video_input'},
                            {'label': 'Loop Output',
                             'route_property': 'loop_input',
                             'command': 'set_loop_input'}]}}

    HEALTH_FAULT_MESSAGE = "The Visionary endpoint stopped answering HTTP requests."
    HEALTH_TIMEOUT_S = 20.0

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._client = None
        self._operations = asyncio.Lock()
        self._diagnostics_due = 0.0
        self._status_error = ''
        self._timing_error = ''
        self._reported = {}
        self._source_names = {}
        self._read_children = set()
        self._configure_menu(str(self.config.get('model') or DEFAULT_MODEL))

    def _configure_menu(self, model):
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._model = model
        self._configure_features(model)
        if ROLE == 'decoder':
            self._configure_sources()
        if model not in ('E5200', 'D5200'):
            self.DRIVER_INFO.pop('bridge', None)
        live = self.DRIVER_INFO['commands']
        self.DRIVER_INFO['quick_actions'] = [key for key in self.DRIVER_INFO['quick_actions'] if key in live]

    def _configure_features(self, model):
        allowed = {'common', *MODEL_FEATURES.get(model, ())}
        module = self._reported.get('UNIT.DUET_INSTALLED', self.config.get('dante_module', False))
        if _typed(module, 'boolean') is True:
            allowed.add('dante')
        self._filter_features(allowed)
        if 'dante' not in allowed:
            self._remove_dante_choices()
        if ROLE == 'encoder' and 'multi' not in allowed:
            self.DRIVER_INFO.pop('routing', None)

    def _filter_features(self, allowed):
        for key, group in FEATURE_GROUPS.items():
            if group not in allowed:
                self.DRIVER_INFO['commands'].pop('set_' + key, None)
                self.DRIVER_INFO['device_settings'].pop(key, None)
        extras = {'set_gpio':'duet', 'reboot_dante':'dante', 'set_edid':'edid', 'send_ir':'ir'}
        self._filter_extra_commands(extras, allowed)
        self._filter_child_features(allowed)

    def _filter_child_features(self, allowed):
        child_features = {'gpio_input': 'duet', 'gpio_output': 'duet', 'input': 'edid'}
        for kind, group in child_features.items():
            if group not in allowed:
                self.DRIVER_INFO['child_entity_types'].pop(kind, None)

    def _filter_extra_commands(self, extras, allowed):
        for command, group in extras.items():
            if group not in allowed:
                self.DRIVER_INFO['commands'].pop(command, None)

    def _remove_dante_choices(self):
        self._without_choice('stream_audio_source', 'DANTE')
        if 'set_analog_source' in self.DRIVER_INFO['commands']:
            self._without_choice('analog_source', 'DANTE')

    def _without_choice(self, key, value):
        specs = [self.DRIVER_INFO['state_variables'][key], self.DRIVER_INFO['device_settings'][key],
                 self.DRIVER_INFO['commands']['set_' + key]['params']['value']]
        for spec in specs:
            spec['values'] = [item for item in spec['values'] if _enum_value(item) != value]

    def _configure_sources(self):
        self._source_names = {}
        for row in self.config.get('sources', []) or []:
            name = str(row.get('name', '')).strip()
            address = _unicast(row.get('address', ''))
            if not name or address in self._source_names:
                raise ConnectionFaultError('Each source needs a name and a different encoder address.', code='invalid_config')
            self._source_names[address] = name
        choices = [{'value': address, 'label': name + ' (' + address + ')'} for address, name in self._source_names.items()]
        for plane in ROUTES:
            self.DRIVER_INFO['commands']['route_' + plane]['params']['source']['values'] = deepcopy(choices)

    async def _create_transport(self, transport_type):
        host = str(self.config.get('host', '')).strip()
        if not host:
            raise ConnectionFaultError('The endpoint address is missing.', code='invalid_config')
        if not self.config.get('username') or not self.config.get('password'):
            raise ConnectionFaultError('The endpoint username or password is missing.', code='auth_failed')
        self._client = httpx.AsyncClient(
            base_url=httpx.URL(scheme='http', host=host, port=int(self.config.get('port', 80))),
            auth=httpx.BasicAuth(str(self.config['username']), str(self.config['password'])),
            timeout=httpx.Timeout(5.0, connect=4.0), follow_redirects=False,
        )

    async def _post_connect(self):
        self._reported.clear()
        async with self._operations:
            data = await self._read_all()
        model = str(self._reported.get('UNIT.MODEL', ''))
        if model not in MODELS:
            raise ConnectionFaultError('This is not a supported Visionary 5 Series ' + ROLE + '.', code='invalid_config')
        self._configure_menu(model)
        self._apply_report(data)
        self._reconcile_children()

    async def _initial_sync(self):
        async with self._operations:
            await self._refresh_child_values(self._reported)
            await self._read_video_timing()

    def _link_alive(self):
        return self._client is not None

    async def _close_session(self):
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self):
        async with self._operations:
            # Any HTTP reply proves life, including an API refusal. A rejected
            # Basic credential remains auth_failed and must not be retried.
            await self._request('GET', [('QUERY.STATUS', 'TRUE')], check=False)

    async def _request(self, method, pairs, *, check=True):
        if self._client is None:
            raise ConnectionError('The Visionary endpoint session is closed.')
        payload = urlencode([('CMD', 'START'), ('UNIT.ID', 'ALL'), *pairs, ('CMD', 'END')])
        self.record_traffic('tx', method + ' /cgi-bin/wapi.cgi\n' + payload)
        reply = await self._http_request(method, payload)
        self._protect_response(reply.text)
        self.record_traffic('rx', reply.text)
        self._check_access(reply.status_code)
        return _decode_reply(reply) if check else {}

    async def _http_request(self, method, payload):
        # httpx QueryParams groups duplicate CMD keys, moving END before the
        # body. Preserve the ordered envelope in the request target instead.
        target = '/cgi-bin/wapi.cgi?' + payload if method == 'GET' else '/cgi-bin/wapi.cgi'
        kwargs = {} if method == 'GET' else {
            'content': payload, 'headers': {'Content-Type': 'application/x-www-form-urlencoded'}}
        try:
            return await self._client.request(method, target, **kwargs)
        except httpx.TransportError as exc:
            raise ConnectionError(self.HEALTH_FAULT_MESSAGE) from exc

    def _check_access(self, status):
        if status == 401:
            raise ConnectionFaultError('The endpoint refused the web login.', code='auth_failed')
        if status == 403 and not self.connected:
            raise ConnectionFaultError('The account cannot access the endpoint API.', code='auth_failed')

    def _protect_response(self, text):
        for key, value in _parse_pairs(text).items():
            if any(part in key.upper() for part in ('PASSWORD', 'TOKEN', 'SECRET')) and value:
                self.redact_in_log(value)

    async def _read_all(self):
        data = await self._request('GET', [('QUERY.ALL', 'TRUE')])
        self._apply_report(data)
        return data

    async def _read_key(self, wire):
        data = await self._request('GET', [('QUERY.KEY', wire)])
        if wire not in data:
            raise CommandRefusedError('The endpoint did not report ' + wire + '.')
        self._apply_report(data)
        return data[wire]

    def _apply_report(self, data):
        self._reported.update(data)
        for wire, (key, kind) in FIELDS.items():
            if wire in data:
                if wire == 'AUDIO.MUTE' and self._model == 'D5200':
                    continue
                self.set_state(key, _typed(data[wire], kind))
        if self._model == 'D5200' and 'AUDIO.HDMI_MUTE' in data:
            self.set_state('hdmi_mute', _typed(data['AUDIO.HDMI_MUTE'], 'boolean'))
        if ROLE == 'decoder':
            self._apply_routes()
        self._update_child_readings(data)

    def _apply_routes(self):
        # The user manual distinguishes stored linked/independent selections.
        # Effective routes follow the mode the DEVICE reports, not our last set.
        linked = _typed(self._reported.get('STREAM.HOST_LINKED'), 'boolean')
        if linked is None:
            return
        host = self._reported.get('STREAM.HOST')
        self.set_state('linked_source', host if linked else '')
        for plane in ('video', 'audio', 'usb'):
            self.set_state(plane + '_source', host if linked else self._reported.get(ROUTES[plane][0]))

    def _reconcile_children(self):
        wanted = set()
        if self._model == 'E5200':
            for ident, name in ((1, 'HDMI 1'), (2, 'HDMI 2'), (3, 'USB-C')):
                wanted.add(('input', ident))
                self.register_child('input', ident, {'name': name})
        if self._model in ('DuetE-5', 'DuetD-5'):
            wanted.add(('gpio_input', 1))
            wanted.update(('gpio_output', ident) for ident in (1, 2))
        for kind, ident in wanted:
            self.register_child(kind, ident)
        for kind, ident in self._read_children - wanted:
            self.deregister_child(kind, ident)
        self._read_children = wanted
        self._update_child_readings(self._reported)

    def _update_child_readings(self, data):
        for kind, ident in self._read_children:
            wire = _child_wire(kind, ident)
            if wire in data:
                key, value = ('edid', data[wire]) if kind == 'input' else ('high', _typed(data[wire], 'boolean'))
                self.set_child_state(kind, ident, key, value)
                self.set_child_state_batch(kind, ident, self.child_fault())
        self._gpio_presence()

    def _gpio_presence(self):
        if ('gpio_output', 2) not in self._read_children:
            return
        ir = _typed(self._reported.get('IR.ENABLED'), 'boolean')
        if ir is True:
            fault = self.child_fault('not_fitted', 'This port is assigned to IR. Disable IR to use GPIO output 2.')
            self.set_child_state_batch('gpio_output', 2, fault)

    async def refresh_children(self):
        async with self._operations:
            await self._read_all()
            self._reconcile_children()
            for kind, ident in self._read_children:
                await self._read_key(_child_wire(kind, ident))
        return {kind: sum(item[0] == kind for item in self._read_children)
                for kind in self.DRIVER_INFO['child_entity_types']}

    async def poll(self):
        async with self._operations:
            try:
                data = await self._read_all()
                await self._refresh_child_values(data)
                self._status_error = ''
            except CommandRefusedError as exc:
                self._status_error = str(exc)
            if asyncio.get_running_loop().time() >= self._diagnostics_due:
                await self._read_video_timing()
            self.set_state('last_error', self._status_error or self._timing_error)

    async def _refresh_child_values(self, data):
        for kind, ident in self._read_children:
            wire = _child_wire(kind, ident)
            if wire not in data:
                await self._read_key(wire)

    async def _read_video_timing(self):
        self._diagnostics_due = asyncio.get_running_loop().time() + 30
        try:
            data = await self._request('GET', [('QUERY.VIDEO_TIMING', 'TRUE')])
        except CommandRefusedError as exc:
            self._set_timing_error(str(exc))
            return
        timing = data.get('VIDEO.TIMING')
        if timing is None:
            self._set_timing_error('The endpoint did not report video timing.')
            return
        self._set_timing_error('')
        self.set_state('video_timing', timing)
        size = re.search(r'\[(\d+)\]X\[(\d+)\]\s*\[([^\]]+)\]Hz', timing)
        absent = 'Not Available' in timing
        self.set_state('video_present', False if absent else True if size else None)
        self.set_state('video_resolution', f'{size[1]} × {size[2]} at {size[3]} Hz' if size else '')
        hdcp = re.search(r'HDCP:\s*\[([^\]]+)\]', timing)
        self.set_state('hdcp', hdcp[1] if hdcp else '')

    def _set_timing_error(self, error):
        self._timing_error = error
        self.set_state('last_error', self._status_error or error)

    async def send_command(self, command, params=None):
        params = params or {}
        if command not in self.DRIVER_INFO['commands']:
            raise CommandRefusedError('This command is not available on this model.')
        async with self._operations:
            return await self._dispatch(command, params)

    async def _dispatch(self, command, params):
        if command.startswith('set_') and command[4:] in WRITES:
            return await self._set_value(command[4:], params['value'])
        if command in FIXED:
            return await self._request('POST', FIXED[command])
        handlers = {'refresh_status': self._refresh, 'download_diagnostics': self._download,
                    'set_gpio': self._gpio, 'set_edid': self._edid, 'send_ir': self._ir,
                    'send_cec': self._cec, 'route_address': self._route_address,
                    'set_wall_position': self._wall_position, 'set_wall_bezel': self._wall_bezel,
                    'disable_bezel': self._disable_bezel}
        if command in handlers:
            return await handlers[command](params)
        return await self._route_named(command, params)

    async def _route_named(self, command, params):
        if not command.startswith('route_') or command[6:] not in ROUTES:
            raise CommandRefusedError('This command has no supported operation.')
        source = _unicast(params['source'])
        if source not in self._source_names:
            raise CommandRefusedError('Add this encoder to Sources or use Route Encoder Address.')
        return await self._route(command[6:], source)

    async def _set_value(self, key, value):
        spec = self.DRIVER_INFO['commands']['set_' + key]['params']['value']
        value = _validated(value, spec)
        if key == 'serial_baud' and int(value) not in BAUD_RATES:
            raise CommandRefusedError('Choose a supported RS-232 baud rate.')
        wire = WRITES[key]
        read_key = READ_KEYS[key]
        if key == 'hdmi_mute' and self._model == 'D5200':
            wire = read_key = 'AUDIO.HDMI_MUTE'
        await self._write_confirm([(wire, _wire_value(value))], {read_key: _wire_value(value)})

    async def set_device_setting(self, key, value):
        if key not in self.DRIVER_INFO['device_settings']:
            raise CommandRefusedError('This setting is not available on this model.')
        return await self.send_command('set_' + key, {'value': value})

    async def _write_confirm(self, pairs, expected):
        await self._request('POST', pairs)
        try:
            async with asyncio.timeout(10):
                await self._confirm_values(expected)
        except TimeoutError:
            raise CommandRefusedError('The endpoint acknowledged the command but did not confirm the requested value within 10 seconds.') from None

    async def _confirm_values(self, expected):
        # Delays are a client retry policy, not a claim about hardware timing.
        # Never replace reported values with a command's requested values.
        for delay in (0, 0.2, 0.5, 1.0, 2.0, 3.0):
            if delay:
                await asyncio.sleep(delay)
            data = await self._read_all()
            missing = [wire for wire in expected if wire not in data]
            for wire in missing:
                data[wire] = await self._read_key(wire)
            if all(_equivalent(data.get(wire), wanted) for wire, wanted in expected.items()):
                return
        raise CommandRefusedError('The endpoint acknowledged the command but did not report the requested value.')

    async def _refresh(self, params):
        await self._read_all()
        await self._read_video_timing()
        for kind, ident in self._read_children:
            await self._read_key(_child_wire(kind, ident))

    async def _download(self, params):
        data = await self._read_all()
        await self._read_video_timing()
        safe = {key: value for key, value in data.items()
                if not any(word in key.upper() for word in ('PASSWORD', 'TOKEN', 'SECRET'))}
        safe['VIDEO.TIMING'] = self.get_state('video_timing')
        return DeviceFile('visionary-' + ROLE + '-diagnostics.json', json.dumps(safe, indent=2).encode(), 'application/json')

    async def _route_address(self, params):
        return await self._route(params['plane'], _unicast(params['address']))

    async def _route(self, plane, address):
        if plane not in ROUTES:
            raise CommandRefusedError('Choose linked, video, audio or USB routing.')
        wall = await self._read_key('VW.ACTIVE')
        if _typed(wall, 'boolean') is None:
            raise CommandRefusedError('The endpoint did not report its video-wall cropping setting.')
        pairs, expected = await self._route_pairs(plane, address)
        pairs.append(('VW.ACTIVE', _wire_value(_typed(wall, 'boolean'))))
        expected['VW.ACTIVE'] = _wire_value(_typed(wall, 'boolean'))
        await self._write_confirm(pairs, expected)

    async def _route_pairs(self, plane, address):
        host, connect = ROUTES[plane]
        if plane == 'linked':
            return self._linked_route_pairs(address)
        linked = _typed(await self._read_key('STREAM.HOST_LINKED'), 'boolean')
        if linked is None:
            raise CommandRefusedError('The endpoint did not report its routing mode.')
        if not linked:
            return [(host, address), ('STREAM.CONNECT', connect)], {host: address, 'STREAM.HOST_LINKED': 'FALSE'}
        # Changing to independent mode must not reactivate old, stored routes
        # on the other planes. The API p.17 explicitly allows repeated CONNECT.
        current = _unicast(await self._read_key('STREAM.HOST'))
        pairs, expected = [], {'STREAM.HOST_LINKED': 'FALSE'}
        for service in ('video', 'audio', 'usb'):
            wire, selector = ROUTES[service]
            value = address if service == plane else current
            pairs.extend([(wire, value), ('STREAM.CONNECT', selector)])
            expected[wire] = value
        return pairs, expected

    def _linked_route_pairs(self, address):
        expected = {'STREAM.HOST': address}
        if 'STREAM.HOST_LINKED' in self._reported or self._model == 'D5200':
            expected['STREAM.HOST_LINKED'] = 'TRUE'
        return [('STREAM.HOST', address), ('STREAM.CONNECT', 'TRUE')], expected

    async def _gpio(self, params):
        ident = int(params['output'])
        if ('gpio_output', ident) not in self._read_children:
            raise CommandRefusedError('Choose a GPIO output on this unit.')
        if ident == 2 and _typed(await self._read_key('IR.ENABLED'), 'boolean') is not False:
            raise CommandRefusedError('GPIO output 2 requires IR to be disabled in the device web interface.')
        value = _validated(params['high'], {'type': 'boolean'})
        wire = _child_wire('gpio_output', ident)
        await self._write_confirm([(wire, _wire_value(value))], {wire: _wire_value(value)})

    async def _edid(self, params):
        ident = int(params['input'])
        if ('input', ident) not in self._read_children:
            raise CommandRefusedError('Choose an input on this E5200.')
        spec = self.DRIVER_INFO['commands']['set_edid']['params']['edid']
        value = _validated(params['edid'], spec)
        wire = _child_wire('input', ident)
        await self._write_confirm([(wire, value)], {wire: value})

    async def _ir(self, params):
        value = _validated(params['code'], self.DRIVER_INFO['commands']['send_ir']['params']['code'])
        if _typed(await self._read_key('IR.ENABLED'), 'boolean') is not True:
            raise CommandRefusedError('Enable IR in the endpoint web interface before sending an IR code.')
        await self._request('POST', [('UNIT.IR_SEND', value)])

    async def _cec(self, params):
        value = _validated(params['code'], self.DRIVER_INFO['commands']['send_cec']['params']['code'])
        await self._request('POST', [('VIDEO.SEND_CEC_GENERIC', value.upper())])

    async def _wall_position(self, params):
        keys = ('wall_rows', 'wall_columns', 'wall_row', 'wall_column')
        values = {key: _validated(params[key], {'type': 'integer', 'min': 1, 'max': 16}) for key in keys}
        if values['wall_row'] > values['wall_rows'] or values['wall_column'] > values['wall_columns']:
            raise CommandRefusedError('The tile row and column must fit inside the wall.')
        # API p.27: total rows/columns MUST precede the tile position.
        pairs = [(READ_KEYS[key], str(values[key])) for key in keys] + [('VW.ACTIVE', 'TRUE')]
        await self._write_confirm(pairs, dict(pairs))

    async def _wall_bezel(self, params):
        keys = ('outer_width', 'outer_height', 'visible_width', 'visible_height')
        values = {key: _millimetres(params[key]) for key in keys}
        if values['visible_width'] > values['outer_width'] or values['visible_height'] > values['outer_height']:
            raise CommandRefusedError('Visible dimensions must not exceed the overall display dimensions.')
        pairs = [(READ_KEYS[key], str(values[key])) for key in keys]
        await self._write_confirm(pairs, dict(pairs))

    async def _disable_bezel(self, params):
        pairs = [(wire, '1') for wire in BEZEL_KEYS]
        await self._write_confirm(pairs, dict(pairs))

    async def prepare_bridge_port(self, port_id, params):
        # The manual requires Save/Reboot after serial commissioning. Never
        # reboot a live AV endpoint as a side effect of connecting a display.
        if port_id != 'serial:1' or not self.DRIVER_INFO.get('bridge'):
            raise CommandRefusedError('This unit has no declared serial pass-through port.')
        wanted = {'SERIAL.ENABLED': 'TRUE', 'SERIAL.BAUDRATE': str(params.get('baudrate', 9600)),
                  'SERIAL.DATABITS': str(params.get('bytesize', 8)),
                  'SERIAL.PARITY': str(params.get('parity', 'N')).lower(),
                  'SERIAL.STOPBITS': str(int(params.get('stopbits', 1)))}
        async with self._operations:
            data = await self._read_all()
        if not all(_equivalent(data.get(key), value) for key, value in wanted.items()):
            raise CommandRefusedError('Match and enable the endpoint serial settings, save and reboot before using RS-232 pass-through.')


def _parse_pairs(text):
    # Replies are human-readable key/value text, not documented as URL-encoded.
    # Preserve literal '+' and '%' in names; only outgoing HTTP data is encoded.
    pairs = {}
    for item in text.strip().split('&'):
        key, sep, value = item.partition('=')
        if sep:
            pairs[key.strip()] = value.strip()
    return pairs


def _decode_reply(reply):
    if reply.status_code != 200:
        raise CommandRefusedError('The endpoint answered HTTP ' + str(reply.status_code) + '.')
    data = _parse_pairs(reply.text)
    if data.get('API.STATUS') not in SUCCESS:
        raise CommandRefusedError('The endpoint did not confirm the API request. See device traffic for its reply.')
    return data


def _typed(value, kind):
    if kind == 'boolean':
        return {'TRUE': True, 'FALSE': False, '1': True, '0': False}.get(str(value).upper())
    if kind in ('integer', 'millimetres'):
        try:
            number = int(value)
            return number / 10 if kind == 'millimetres' else number
        except (ValueError, TypeError):
            return None
    return str(value)


def _wire_value(value):
    return ('TRUE' if value else 'FALSE') if isinstance(value, bool) else str(value)


def _equivalent(actual, wanted):
    if wanted in ('TRUE', 'FALSE'):
        return _typed(actual, 'boolean') is (wanted == 'TRUE')
    return str(actual) == str(wanted)


def _enum_value(item):
    return item.get('value') if isinstance(item, dict) else item


def _validated(value, spec):
    kind = spec['type']
    if kind == 'boolean':
        parsed = _typed(value, kind)
        if parsed is None:
            raise CommandRefusedError('Choose true or false.')
        return parsed
    if kind == 'integer':
        return _integer(value, spec.get('min'), spec.get('max'))
    return _validated_text(value, spec)


def _validated_text(value, spec):
    text = str(value)
    if spec['type'] == 'enum' and text not in [str(_enum_value(item)) for item in spec.get('values', [])]:
        raise CommandRefusedError('Choose one of the available values.')
    if spec.get('pattern') and re.fullmatch(spec['pattern'], text) is None:
        raise CommandRefusedError('The value contains unsupported characters or has the wrong format.')
    return text


def _integer(value, low, high):
    try:
        number = int(value)
        exact = not isinstance(value, bool) and float(value) == number
    except (ValueError, TypeError, OverflowError):
        exact = False
        number = 0
    if not exact or not _in_range(number, low, high):
        raise CommandRefusedError('Choose a whole number in the supported range.')
    return number


def _in_range(value, low, high):
    if low is not None and value < low:
        return False
    return high is None or value <= high


def _millimetres(value):
    if isinstance(value, bool):
        raise CommandRefusedError('Enter display dimensions in millimetres.')
    try:
        number = float(value)
        tenths = round(number * 10)
    except (ValueError, TypeError, OverflowError):
        raise CommandRefusedError('Enter display dimensions in millimetres.') from None
    if not 1 <= tenths <= 99999 or abs(number * 10 - tenths) > 0.00001:
        raise CommandRefusedError('Use dimensions from 0.1 to 9999.9 mm, with at most one decimal place.')
    return tenths


def _unicast(value):
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ipaddress.AddressValueError:
        raise CommandRefusedError('Enter an encoder IPv4 address.') from None
    forbidden = (address.is_multicast, address.is_unspecified, str(address) == '255.255.255.255')
    if any(forbidden):
        raise CommandRefusedError('Use the encoder unicast address, not a multicast or broadcast address.')
    return str(address)


def _child_wire(kind, ident):
    if kind == 'input':
        return 'VIDEO.EDID' + str(ident)
    return ('UNIT.GPIO_IN' if kind == 'gpio_input' else 'UNIT.GPIO_OUT') + str(ident)
