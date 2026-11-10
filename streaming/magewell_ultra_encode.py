"""Magewell Ultra Encode operational HTTP API driver.

Python is required for device-enumerated stream, recording, overlay and schedule
rosters, capability-dependent encoder controls, read/modify/write operations,
and a cookie session. No control-state push is documented; status is polled.
POST login JSON and the retained MD5 password format were confirmed by Magewell
support for V2.5.62. The Plus API applies to current Rev B HDMI/SDI as well as
Plus products; AIO-specific controls follow the AIO API and model report.

Gain is normalized against the reported native min/max. The API calls its integer
gain dB but does not explain its scale; no unverified dB conversion is made.
Preview URLs are supplied explicitly because the API documents stream keys and
ports, but not a complete playback URL. Account, network, file and firmware
administration remain in the manufacturer's web UI. No hardware qualification.

Sources: https://www.magewell.com/api-docs/ultra-encode-plus-api/
         https://www.magewell.com/api-docs/ultra-encode-aio-api/
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math

import httpx

from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError

_PROTOCOLS = {0: 'RTMP', 1: 'Twitch', 2: 'YouTube', 3: 'Facebook', 4: 'Wowza RTMP',
              100: 'RTSP', 120: 'SRT Caller', 121: 'SRT Listener', 122: 'Wowza SRT',
              130: 'NDI HX', 131: 'HLS', 132: 'TS UDP', 133: 'TS RTP', 140: 'TVU',
              143: 'YouTube HLS', 144: 'ZIXI', 150: 'RIST Caller', 151: 'RIST Listener'}
_STATUS = {0: 'Succeeded', 1: 'Repeated', 2: 'Running', 5: 'Cancelled',
           22: 'Connected', 23: 'Connecting', 24: 'Waiting', 25: 'Authorizing',
           27: 'Initial', 28: 'Resolving DNS', 29: 'Address not set', 30: 'Stream login refused'}
_ERRORS = {-1: 'Password refused', -2: 'Device occupied', -4: 'Unsupported device',
           -5: 'Storage error', -9: 'Device busy', -10: 'Invalid parameters',
           -14: 'Not found', -15: 'File error', -17: 'Session expired', -18: 'System error',
           -25: 'Unknown user', -26: 'Permission denied', -29: 'Simultaneous stream limit',
           -35: 'No input signal', -39: 'SD card full', -40: 'SD card permission denied',
           -44: 'Stream port occupied', -54: 'Protocol session limit', -58: 'NAS mount error'}
_AUDIO = {'spi': 'HDMI / SDI Audio', 'linein': 'Line In', 'lineout': 'Line Out'}
_SIMPLE = {'start_streaming': 'start-live', 'stop_streaming': 'stop-live',
           'start_recording': 'start-rec', 'stop_recording': 'stop-rec',
           'clear_stream_status': 'clear-live', 'clear_record_status': 'clear-rec',
           'reboot': 'reboot', 'factory_reset': 'reset-all-settings'}
_SETTINGS = {
    'device_name': ('set-name', 'name'),
    'audio_sync_offset_ms': ('set-sync-offset', 'audio-sync-offset'),
    'deinterlace': ('enable-deinterlace', 'enable-deinterlace'),
    'substream_enabled': ('set-enable-stream1', 'enable'),
    'ndi_hx3': ('enable-ndi-hx3', 'enable-ndi-hx3'),
    'no_signal_image': ('use-nosignal-file', 'use-nosignal-file'),
    'microphone_bias': ('set-analog-mic-bias', 'enable-mic-bias'),
}
# Only fields named by the manufacturer's set-video parameter table.
_VIDEO_FIELDS = {
    'is-auto': ('follow_input', 'Follow Input', 'boolean'),
    'cx': ('width', 'Width', 'integer'), 'cy': ('height', 'Height', 'integer'),
    'duration': ('frame_interval', 'Frame Interval (100 ns)', 'integer'),
    'kbps': ('bitrate_kbps', 'Bit Rate (kbps)', 'integer'),
    'gop': ('gop', 'Keyframe Interval', 'integer'),
    'fourcc': ('fourcc', 'Reserved Format', 'integer'),
    'codec': ('codec', 'Codec', 'integer'), 'profile': ('profile', 'Profile', 'integer'),
    'cbrstat': ('cbrstat', 'Reserved Rate Window', 'integer'),
    'fullrange': ('fullrange', 'Quantization Range', 'integer'),
    'is-vbr': ('vbr', 'Variable Bit Rate', 'boolean'),
    'min-vbr-qp': ('min_qp', 'Minimum Quantizer', 'integer'),
    'max-vbr-qp': ('max_qp', 'Maximum Quantizer', 'integer'),
    'is-time-code-sei': ('timecode', 'Timecode Source', 'integer'),
    'is-closed-caption-sei': ('closed_caption', 'Closed Captions', 'boolean'),
    'ar-convert-mode': ('aspect_mode', 'Aspect Ratio Conversion', 'integer'),
    'rotation': ('rotation', 'Rotation', 'integer'),
}
_CAP_CHOICES = {'duration': 'durations', 'codec': 'video-codec', 'profile': 'profile',
                'gop': 'gop-sec',
                'fullrange': 'video-range', 'ar-convert-mode': 'video-ar-convert-mode',
                'rotation': 'video-rotation'}
_VIDEO_ENUMS = {'is-time-code-sei': [(0, 'Off'), (1, 'System Time'), (2, 'Input Timecode')],
                'ar-convert-mode': [(0, 'Ignore'), (1, 'Cropping'), (2, 'Padding')],
                'fullrange': [(0, 'Limited'), (1, 'Full')]}


def _flag(value):
    return value is True or str(value).lower() in ('1', 'true', 'yes', 'on')


def _code_name(value):
    return _STATUS.get(value, _ERRORS.get(value, f'Status {value}'))


def _choices(items):
    return [{'value': item['value'], 'label': str(item.get('name', item['value']))}
            for item in items if isinstance(item, dict) and 'value' in item]


def _valid_value(value, schema):
    kind = schema.get('type')
    if kind == 'boolean':
        return _boolean_value(value)
    if kind == 'enum':
        return _enum_value(value, schema)
    return _integer_value(value, schema)


def _boolean_value(value):
    if str(value).lower() not in ('0', '1', 'true', 'false'):
        raise ValueError('Choose on or off.')
    return int(_flag(value))


def _enum_value(value, schema):
    allowed = {str(v.get('value')) for v in schema.get('values', [])}
    if str(value) not in allowed:
        raise ValueError('Choose a value reported by the encoder.')
    return int(value)


def _integer_value(value, schema):
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError('Enter a whole number.')
    if number < schema.get('min', -math.inf) or number > schema.get('max', math.inf):
        raise ValueError('The value is outside the encoder’s reported range.')
    return int(number)


def _present_values(data, fields):
    return {key: data[raw] for raw, key in fields if raw in data}


def _channel_values(item, kind, running_code):
    code = int(item.get('result', 27))
    values = {'status_code': code, 'status': _code_name(code), 'running': code == running_code}
    mapping = {'run-ms': 'run_ms'}
    detail = {'stream': {'main-inst-bps': 'main_bitrate_bps', 'sub-inst-bps': 'sub_bitrate_bps'},
              'recorder': {'video-frame-count': 'video_frames', 'audio-frame-count': 'audio_frames', 'parted-num': 'parts'}}
    mapping.update(detail[kind])
    values.update(_present_values(item, mapping.items()))
    if 'is-use' in item:
        values['enabled'] = _flag(item['is-use'])
    return values


class UltraEncodeError(CommandRefusedError):
    def __init__(self, method, code):
        self.code = code
        super().__init__(f'{method}: {_code_name(code)} ({code}).')


class MagewellUltraEncodeDriver(BaseDriver):
    DRIVER_INFO = {'id': 'magewell_ultra_encode',
     'discovery': {'oui': ['d0:c8:57:8', '70:b3:d5:75:d']},
     'name': 'Magewell Ultra Encode',
     'manufacturer': 'Magewell',
     'category': 'streaming',
     'version': '1.0.1',
     'author': 'OpenAVC',
     'transport': 'http',
     'ports': [80, 443],
     'protocols': ['http'],
     'source_url': 'https://www.magewell.com/api-docs/ultra-encode-plus-api/',
     'description': 'Controls Ultra Encode streaming and recording, individual destinations, audio paths, video '
                    'encoding, overlays and schedules.',
     'simulated': True,
     'verified': False,
     'min_platform_version': '0.37.1',
     'tags': ['encoder', 'streaming', 'recording', 'ndi', 'srt', 'rtsp', 'rtmp'],
     'compatible_models': [{'manufacturer': 'Magewell',
                            'models': ['Ultra Encode AIO',
                                       'Ultra Encode HDMI Plus',
                                       'Ultra Encode SDI Plus',
                                       'Ultra Encode HDMI Plus Module',
                                       'Ultra Encode SDI Plus Module'],
                            'confidence': 'untested',
                            'notes': 'HTTP API with JSON POST login on firmware V2.5.62. Input switching and '
                                     'composition are AIO-only.'},
                           {'manufacturer': 'Magewell',
                            'models': ['Ultra Encode HDMI', 'Ultra Encode SDI'],
                            'confidence': 'untested',
                            'notes': 'Current Rev B hardware only: SKUs 531100000 and 531200000, firmware '
                                     'V2.5.62. Discontinued Rev A models are not supported.'}],
     'help': {'overview': 'Controls configured streaming and recording sessions, reports their actual progress '
                          'and errors, and exposes audio and video controls. Configure destination addresses, '
                          'storage and schedules in the encoder’s web interface; they appear here after Refresh '
                          'Status.',
              'setup': 'Use firmware V2.5.62 and enter an encoder account with control permission. Username is '
                       'case-sensitive. HTTP uses port 80; if HTTPS is enabled, select HTTPS and its configured '
                       'port. Configure stream destinations and recording channels in the web interface. Start '
                       'Streaming/Recording controls the global function; each desired destination/channel must '
                       'also be enabled. An optional RTSP or SRT preview URL can be copied from the encoder’s '
                       'Live page. No hardware qualification has been performed.'},
     'default_config': {'host': '',
                        'port': 80,
                        'ssl': False,
                        'verify_ssl': False,
                        'username': 'Admin',
                        'password': '',
                        'poll_interval': 3,
                        'detail_poll_every': 5,
                        'model': 'Ultra Encode AIO',
                        'preview_url': ''},
     'config_schema': {'host': {'type': 'string', 'label': 'IP Address', 'required': True},
                       'port': {'type': 'integer', 'label': 'Web Port', 'default': 80, 'min': 1, 'max': 65535},
                       'ssl': {'type': 'boolean', 'label': 'HTTPS', 'default': False},
                       'verify_ssl': {'type': 'boolean', 'label': 'Verify HTTPS Certificate', 'default': False},
                       'username': {'type': 'string', 'label': 'Username', 'default': 'Admin', 'required': True},
                       'password': {'type': 'string',
                                    'label': 'Password',
                                    'default': '',
                                    'secret': True,
                                    'required': True},
                       'poll_interval': {'type': 'number',
                                         'label': 'Poll Interval (seconds)',
                                         'default': 3,
                                         'min': 0},
                       'detail_poll_every': {'type': 'integer',
                                             'label': 'Refresh Settings Every N Polls',
                                             'default': 5,
                                             'min': 1,
                                             'max': 100},
                       'model': {'type': 'enum',
                                 'label': 'Model',
                                 'default': 'Ultra Encode AIO',
                                 'learned_from': 'model',
                                 'values': ['Ultra Encode AIO',
                                            'Ultra Encode HDMI Plus',
                                            'Ultra Encode SDI Plus',
                                            'Ultra Encode HDMI Plus Module',
                                            'Ultra Encode SDI Plus Module',
                                            'Ultra Encode HDMI',
                                            'Ultra Encode SDI']},
                       'preview_url': {'type': 'string',
                                       'label': 'Preview URL',
                                       'default': '',
                                       'help': 'Optional RTSP or SRT playback URL copied from the encoder’s Live '
                                               'page. The API does not document the complete RTSP URL form.',
                                       'secret': True}},
     'state_variables': {'model': {'type': 'string', 'label': 'Model'},
                         'selected_no_signal_image': {'type': 'integer', 'label': 'Selected No-Signal Image'},
                         'mixer_format_source': {'type': 'integer', 'label': 'Mixer Format Source'},
                         'mixer_hdmi_on_top': {'type': 'boolean', 'label': 'Mixer HDMI on Top'},
                         'mixer_layout': {'type': 'integer', 'label': 'Mixer Layout'},
                         'mixer_location': {'type': 'integer', 'label': 'Mixer Location'},
                         'serial_number': {'type': 'string', 'label': 'Serial Number'},
                         'firmware_version': {'type': 'string', 'label': 'Firmware'},
                         'device_name': {'type': 'string', 'label': 'Device Name'},
                         'last_error': {'type': 'string', 'label': 'Last Error', 'cloud_priority': 'high'},
                         'streaming': {'type': 'boolean', 'label': 'Streaming', 'cloud_priority': 'high'},
                         'recording': {'type': 'boolean', 'label': 'Recording', 'cloud_priority': 'high'},
                         'status_mask': {'type': 'integer', 'label': 'Device Status Code'},
                         'upgrading': {'type': 'boolean', 'label': 'Updating Firmware'},
                         'rebooting': {'type': 'boolean', 'label': 'Rebooting'},
                         'input_source': {'type': 'integer', 'label': 'Input Source'},
                         'input_options': {'type': 'string', 'label': 'Input Choices'},
                         'signal_width': {'type': 'integer', 'label': 'Input Width', 'unit': 'px'},
                         'signal_height': {'type': 'integer', 'label': 'Input Height', 'unit': 'px'},
                         'signal_frame_rate': {'type': 'number', 'label': 'Input Frame Rate', 'unit': 'fps'},
                         'signal_status': {'type': 'integer', 'label': 'Input Signal Status Code'},
                         'audio_sync_offset_ms': {'type': 'integer',
                                                  'label': 'Audio Offset',
                                                  'min': -200,
                                                  'max': 200,
                                                  'unit': 'ms',
                                                  'control': True},
                         'deinterlace': {'type': 'boolean', 'label': 'Deinterlace', 'control': True},
                         'substream_enabled': {'type': 'boolean', 'label': 'Substream Encoding', 'control': True},
                         'ndi_hx3': {'type': 'boolean', 'label': 'NDI HX3', 'control': True},
                         'no_signal_image': {'type': 'boolean', 'label': 'Show No-Signal Image', 'control': True},
                         'main_overlays': {'type': 'boolean', 'label': 'Main Stream Overlays', 'control': True},
                         'sub_overlays': {'type': 'boolean', 'label': 'Substream Overlays', 'control': True},
                         'microphone_bias': {'type': 'boolean', 'label': 'Microphone Bias', 'control': True},
                         'auto_record': {'type': 'boolean', 'label': 'Automatic Recording', 'control': True},
                         'auto_record_trigger': {'type': 'integer', 'label': 'Automatic Recording Trigger'},
                         'no_signal_options': {'type': 'string', 'label': 'No-Signal Image Choices'},
                         'preview_url': {'type': 'string', 'label': 'Preview URL'},
                         'preview_format': {'type': 'string', 'label': 'Preview Format'},
                         'auto_record_options': {'type': 'string',
                                                 'label': 'Automatic Recording Trigger Choices'}},
     'child_entity_types': {'stream': {'label': 'Stream',
                                       'label_plural': 'Streams',
                                       'id_format': {'type': 'string'},
                                       'label_field': 'name',
                                       'summary_fields': ['name'],
                                       'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                           'online': {'type': 'boolean', 'label': 'Available'},
                                                           'enabled': {'type': 'boolean',
                                                                       'label': 'Enabled',
                                                                       'control': True},
                                                           'status_code': {'type': 'integer',
                                                                           'label': 'Status Code'},
                                                           'status': {'type': 'string',
                                                                      'label': 'Status',
                                                                      'cloud_priority': 'high'},
                                                           'running': {'type': 'boolean',
                                                                       'label': 'Running',
                                                                       'cloud_priority': 'high'},
                                                           'run_ms': {'type': 'integer',
                                                                      'label': 'Running Time',
                                                                      'unit': 'ms',
                                                                      'cloud_priority': 'low'},
                                                           'protocol': {'type': 'string', 'label': 'Protocol'},
                                                           'main_bitrate_bps': {'type': 'integer',
                                                                                'label': 'Main Bit Rate',
                                                                                'unit': 'bps',
                                                                                'cloud_priority': 'low'},
                                                           'sub_bitrate_bps': {'type': 'integer',
                                                                               'label': 'Substream Bit Rate',
                                                                               'unit': 'bps',
                                                                               'cloud_priority': 'low'}}},
                            'recorder': {'label': 'Recorder',
                                         'label_plural': 'Recorders',
                                         'id_format': {'type': 'string'},
                                         'label_field': 'name',
                                         'summary_fields': ['name'],
                                         'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                             'online': {'type': 'boolean', 'label': 'Available'},
                                                             'enabled': {'type': 'boolean',
                                                                         'label': 'Enabled',
                                                                         'control': True},
                                                             'status_code': {'type': 'integer',
                                                                             'label': 'Status Code'},
                                                             'status': {'type': 'string',
                                                                        'label': 'Status',
                                                                        'cloud_priority': 'high'},
                                                             'running': {'type': 'boolean',
                                                                         'label': 'Running',
                                                                         'cloud_priority': 'high'},
                                                             'run_ms': {'type': 'integer',
                                                                        'label': 'Running Time',
                                                                        'unit': 'ms',
                                                                        'cloud_priority': 'low'},
                                                             'storage': {'type': 'string', 'label': 'Storage'},
                                                             'video_frames': {'type': 'integer',
                                                                              'label': 'Video Frames',
                                                                              'cloud_priority': 'low'},
                                                             'audio_frames': {'type': 'integer',
                                                                              'label': 'Audio Frames',
                                                                              'cloud_priority': 'low'},
                                                             'parts': {'type': 'integer',
                                                                       'label': 'File Parts'}}},
                            'audio': {'label': 'Audio Channel',
                                      'label_plural': 'Audio Channels',
                                      'id_format': {'type': 'string'},
                                      'label_field': 'name',
                                      'summary_fields': ['name'],
                                      'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                          'online': {'type': 'boolean', 'label': 'Available'},
                                                          'mute': {'type': 'boolean',
                                                                   'label': 'Mute',
                                                                   'control': True,
                                                                   'cloud_priority': 'high'},
                                                          'gain_percent': {'type': 'number',
                                                                           'label': 'Gain',
                                                                           'min': 0,
                                                                           'max': 100,
                                                                           'unit': '%',
                                                                           'control': True},
                                                          'gain_raw': {'type': 'integer',
                                                                       'label': 'Native Gain Value'}}},
                            'encoder': {'label': 'Video Encoder',
                                        'label_plural': 'Video Encoders',
                                        'id_format': {'type': 'string'},
                                        'label_field': 'name',
                                        'summary_fields': ['name'],
                                        'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                            'online': {'type': 'boolean', 'label': 'Available'}},
                                        'dynamic': True},
                            'overlay': {'label': 'Overlay',
                                        'label_plural': 'Overlays',
                                        'id_format': {'type': 'string'},
                                        'label_field': 'name',
                                        'summary_fields': ['name'],
                                        'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                            'online': {'type': 'boolean', 'label': 'Available'},
                                                            'enabled': {'type': 'boolean',
                                                                        'label': 'Enabled',
                                                                        'control': True},
                                                            'stream': {'type': 'integer',
                                                                       'label': 'Stream Index'}}},
                            'schedule': {'label': 'Schedule',
                                         'label_plural': 'Schedules',
                                         'id_format': {'type': 'string'},
                                         'label_field': 'name',
                                         'summary_fields': ['name'],
                                         'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                             'online': {'type': 'boolean', 'label': 'Available'},
                                                             'enabled': {'type': 'boolean',
                                                                         'label': 'Enabled',
                                                                         'control': True},
                                                             'channel_type': {'type': 'string',
                                                                              'label': 'Channel Type'},
                                                             'channel_id': {'type': 'integer',
                                                                            'label': 'Channel ID'}}}},
     'commands': {'refresh': {'label': 'Refresh Status',
                              'help': 'Reads current device status and refreshes configured channels.',
                              'params': {}},
                  'start_streaming': {'label': 'Start Streaming',
                                      'help': 'Starts the global live function for enabled stream destinations.',
                                      'params': {}},
                  'stop_streaming': {'sets': {'streaming': False},
                                     'label': 'Stop Streaming',
                                     'help': 'Stops the global live function; configured destinations are '
                                             'retained.',
                                     'params': {}},
                  'start_recording': {'label': 'Start Recording',
                                      'help': 'Starts the global recording function for enabled recording '
                                              'channels.',
                                      'params': {}},
                  'stop_recording': {'sets': {'recording': False},
                                     'label': 'Stop Recording',
                                     'help': 'Stops the global recording function; recorded files are retained.',
                                     'params': {}},
                  'clear_stream_status': {'label': 'Clear Streaming Status',
                                          'help': 'Resets the completed or failed live task to its initial '
                                                  'status.',
                                          'params': {}},
                  'clear_record_status': {'label': 'Clear Recording Status',
                                          'help': 'Resets the completed or failed recording task to its initial '
                                                  'status.',
                                          'params': {}},
                  'reboot': {'label': 'Reboot',
                             'help': 'Restarts the encoder; current streams and recordings are interrupted.',
                             'params': {},
                             'confirm': 'Restart the encoder and interrupt its streams and recordings?'},
                  'factory_reset': {'label': 'Factory Reset',
                                    'help': 'Erases the encoder configuration and restarts it with factory '
                                            'settings.',
                                    'params': {},
                                    'confirm': 'Erase all encoder settings and restart with factory settings?'},
                  'set_stream_enabled': {'sets': {'enabled': '{enabled}'},
                                         'label': 'Enable or Disable Stream',
                                         'help': 'Changes whether the selected stream is enabled. Running status '
                                                 'is read back separately.',
                                         'params': {'target': {'type': 'child_id',
                                                               'label': 'Stream',
                                                               'required': True,
                                                               'child_type': 'stream'},
                                                    'enabled': {'type': 'boolean',
                                                                'label': 'Enabled',
                                                                'required': True}}},
                  'set_recorder_enabled': {'sets': {'enabled': '{enabled}'},
                                           'label': 'Enable or Disable Recorder',
                                           'help': 'Changes whether the selected recorder is enabled. Running '
                                                   'status is read back separately.',
                                           'params': {'target': {'type': 'child_id',
                                                                 'label': 'Recorder',
                                                                 'required': True,
                                                                 'child_type': 'recorder'},
                                                      'enabled': {'type': 'boolean',
                                                                  'label': 'Enabled',
                                                                  'required': True}}},
                  'set_overlay_enabled': {'sets': {'enabled': '{enabled}'},
                                          'label': 'Enable or Disable Overlay',
                                          'help': 'Enables or disables the selected overlay. It is visible when '
                                                  'its stream overlay master is enabled.',
                                          'params': {'target': {'type': 'child_id',
                                                                'label': 'Overlay',
                                                                'required': True,
                                                                'child_type': 'overlay'},
                                                     'enabled': {'type': 'boolean',
                                                                 'label': 'Enabled',
                                                                 'required': True}}},
                  'set_schedule_enabled': {'sets': {'enabled': '{enabled}'},
                                           'label': 'Enable or Disable Schedule',
                                           'help': 'Enables or disables the selected stored schedule. Its '
                                                   'configured actions run at the scheduled times while enabled.',
                                           'params': {'target': {'type': 'child_id',
                                                                 'label': 'Schedule',
                                                                 'required': True,
                                                                 'child_type': 'schedule'},
                                                      'enabled': {'type': 'boolean',
                                                                  'label': 'Enabled',
                                                                  'required': True}}},
                  'set_audio_mute': {'sets': {'mute': '{mute}'},
                                     'label': 'Set Audio Mute',
                                     'help': 'Mutes or unmutes the selected audio path without changing the '
                                             'other paths.',
                                     'params': {'target': {'type': 'child_id',
                                                           'label': 'Audio Channel',
                                                           'required': True,
                                                           'child_type': 'audio'},
                                                'mute': {'type': 'boolean', 'label': 'Mute', 'required': True}}},
                  'set_audio_gain': {'label': 'Set Audio Gain',
                                     'help': 'Sets gain within the device-reported range: 0% is its minimum and '
                                             '100% its maximum. This is not a dB or loudness percentage.',
                                     'params': {'target': {'type': 'child_id',
                                                           'label': 'Audio Channel',
                                                           'required': True,
                                                           'child_type': 'audio'},
                                                'level': {'type': 'number',
                                                          'label': 'Gain (%)',
                                                          'required': True,
                                                          'min': 0,
                                                          'max': 100}}},
                  'select_input': {'sets': {'input_source': '{source}'},
                                   'label': 'Select Input',
                                   'help': 'Selects HDMI, SDI, automatic selection or the mixer on an Ultra '
                                           'Encode AIO.',
                                   'params': {'source': {'type': 'integer',
                                                         'label': 'Input',
                                                         'required': True,
                                                         'options_state': 'input_options'}}},
                  'set_video_parameter': {'label': 'Set Video Encoder Parameter',
                                          'help': 'Changes one encoding parameter, preserving the other current '
                                                  'parameters and reading the result back.',
                                          'params': {'target': {'type': 'child_id',
                                                                'label': 'Video Encoder',
                                                                'required': True,
                                                                'child_type': 'encoder'},
                                                     'control': {'type': 'string',
                                                                 'label': 'Parameter',
                                                                 'required': True,
                                                                 'options_from': {'param': 'target',
                                                                                  'source': 'child_schema'}},
                                                     'value': {'type': 'string',
                                                               'label': 'Value',
                                                               'required': True,
                                                               'type_from': {'param': 'control'}}}},
                  'set_mixer': {'sets': {'mixer_format_source': '{format_source}',
                                         'mixer_hdmi_on_top': '{hdmi_on_top}',
                                         'mixer_layout': '{layout}',
                                         'mixer_location': '{location}'},
                                'label': 'Set Video Mixer',
                                'help': 'Sets the AIO picture-in-picture or side-by-side composition. Select '
                                        'Mixer as the input to use it.',
                                'params': {'format_source': {'type': 'enum',
                                                             'label': 'Follow Input',
                                                             'required': True,
                                                             'values': [{'value': 1, 'label': 'SDI'},
                                                                        {'value': 2, 'label': 'HDMI'}]},
                                           'hdmi_on_top': {'type': 'boolean',
                                                           'label': 'HDMI on Top',
                                                           'required': True},
                                           'layout': {'type': 'enum',
                                                      'label': 'Layout',
                                                      'required': True,
                                                      'values': [{'value': 0, 'label': 'Picture in Picture'},
                                                                 {'value': 1, 'label': 'Side by Side'}]},
                                           'location': {'type': 'integer',
                                                        'label': 'Location',
                                                        'required': True,
                                                        'min': 1,
                                                        'max': 5}}},
                  'select_no_signal_image': {'sets': {'selected_no_signal_image': '{image}'},
                                             'label': 'Select No-Signal Image',
                                             'help': 'Selects an image already stored on the encoder.',
                                             'params': {'image': {'type': 'integer',
                                                                  'label': 'Image',
                                                                  'required': True,
                                                                  'options_state': 'no_signal_options'}}},
                  'set_auto_record': {'sets': {'auto_record': '{enabled}', 'auto_record_trigger': '{trigger}'},
                                      'label': 'Set Automatic Recording',
                                      'help': 'Sets automatic recording and its trigger from the encoder’s '
                                              'supported choices.',
                                      'params': {'enabled': {'type': 'boolean',
                                                             'label': 'Enabled',
                                                             'required': True},
                                                 'trigger': {'type': 'integer',
                                                             'label': 'Trigger',
                                                             'required': True,
                                                             'options_state': 'auto_record_options'}}},
                  'set_device_name': {'sets': {'device_name': '{value}'},
                                      'label': 'Set Device Name',
                                      'help': 'Changes the encoder name reported in device status.',
                                      'params': {'value': {'type': 'string',
                                                           'label': 'Device Name',
                                                           'required': True}}},
                  'set_audio_sync_offset_ms': {'sets': {'audio_sync_offset_ms': '{value}'},
                                               'label': 'Set Audio Offset',
                                               'help': 'Changes the audio synchronization offset in '
                                                       'milliseconds.',
                                               'params': {'value': {'type': 'integer',
                                                                    'label': 'Audio Offset',
                                                                    'required': True,
                                                                    'min': -200,
                                                                    'max': 200}}},
                  'set_deinterlace': {'sets': {'deinterlace': '{value}'},
                                      'label': 'Set Deinterlace',
                                      'help': 'Enables or disables deinterlacing for interlaced input video.',
                                      'params': {'value': {'type': 'boolean',
                                                           'label': 'Deinterlace',
                                                           'required': True}}},
                  'set_substream_enabled': {'sets': {'substream_enabled': '{value}'},
                                            'label': 'Set Substream Encoding',
                                            'help': 'Enables or disables encoding of the secondary video stream.',
                                            'params': {'value': {'type': 'boolean',
                                                                 'label': 'Substream Encoding',
                                                                 'required': True}}},
                  'set_ndi_hx3': {'sets': {'ndi_hx3': '{value}'},
                                  'label': 'Set NDI HX3',
                                  'help': 'Enables or disables NDI HX3 output.',
                                  'params': {'value': {'type': 'boolean', 'label': 'NDI HX3', 'required': True}}},
                  'set_no_signal_image': {'sets': {'no_signal_image': '{value}'},
                                          'label': 'Set Show No-Signal Image',
                                          'help': 'Enables or disables displaying the selected stored image when '
                                                  'input signal is absent.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Show No-Signal Image',
                                                               'required': True}}},
                  'set_main_overlays': {'sets': {'main_overlays': '{value}'},
                                        'label': 'Set Main Stream Overlays',
                                        'help': 'Shows or hides enabled overlays on the main stream.',
                                        'params': {'value': {'type': 'boolean',
                                                             'label': 'Main Stream Overlays',
                                                             'required': True}}},
                  'set_sub_overlays': {'sets': {'sub_overlays': '{value}'},
                                       'label': 'Set Substream Overlays',
                                       'help': 'Shows or hides enabled overlays on the secondary stream.',
                                       'params': {'value': {'type': 'boolean',
                                                            'label': 'Substream Overlays',
                                                            'required': True}}},
                  'set_microphone_bias': {'sets': {'microphone_bias': '{value}'},
                                          'label': 'Set Microphone Bias',
                                          'help': 'Enables or disables bias power for the microphone input.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Microphone Bias',
                                                               'required': True}}}},
     'device_settings': {'device_name': {'type': 'string',
                                         'label': 'Device Name',
                                         'state_key': 'device_name',
                                         'default': 'Ultra Encode',
                                         'setup': False,
                                         'help': 'Writes this setting on the encoder and reads back the value it '
                                                 'reports.',
                                         'regex': "[A-Za-z0-9._+\\[\\]()' -]{1,32}"},
                         'audio_sync_offset_ms': {'type': 'integer',
                                                  'label': 'Audio Offset',
                                                  'min': -200,
                                                  'max': 200,
                                                  'state_key': 'audio_sync_offset_ms',
                                                  'default': 0,
                                                  'setup': False,
                                                  'help': 'Writes this setting on the encoder and reads back the '
                                                          'value it reports.'},
                         'deinterlace': {'type': 'boolean',
                                         'label': 'Deinterlace',
                                         'state_key': 'deinterlace',
                                         'default': True,
                                         'setup': False,
                                         'help': 'Writes this setting on the encoder and reads back the value it '
                                                 'reports.'},
                         'substream_enabled': {'type': 'boolean',
                                               'label': 'Substream Encoding',
                                               'state_key': 'substream_enabled',
                                               'default': True,
                                               'setup': False,
                                               'help': 'Writes this setting on the encoder and reads back the '
                                                       'value it reports.'},
                         'ndi_hx3': {'type': 'boolean',
                                     'label': 'NDI HX3',
                                     'state_key': 'ndi_hx3',
                                     'default': False,
                                     'setup': False,
                                     'help': 'Writes this setting on the encoder and reads back the value it '
                                             'reports.'},
                         'no_signal_image': {'type': 'boolean',
                                             'label': 'Show No-Signal Image',
                                             'state_key': 'no_signal_image',
                                             'default': True,
                                             'setup': False,
                                             'help': 'Writes this setting on the encoder and reads back the '
                                                     'value it reports.'},
                         'main_overlays': {'type': 'boolean',
                                           'label': 'Main Stream Overlays',
                                           'state_key': 'main_overlays',
                                           'default': True,
                                           'setup': False,
                                           'help': 'Writes this setting on the encoder and reads back the value '
                                                   'it reports.'},
                         'sub_overlays': {'type': 'boolean',
                                          'label': 'Substream Overlays',
                                          'state_key': 'sub_overlays',
                                          'default': False,
                                          'setup': False,
                                          'help': 'Writes this setting on the encoder and reads back the value '
                                                  'it reports.'},
                         'microphone_bias': {'type': 'boolean',
                                             'label': 'Microphone Bias',
                                             'state_key': 'microphone_bias',
                                             'default': False,
                                             'setup': False,
                                             'help': 'Writes this setting on the encoder and reads back the '
                                                     'value it reports.'}},
     'quick_actions': ['start_streaming', 'stop_streaming', 'start_recording', 'stop_recording']}

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._client = None
        self._operation_lock = asyncio.Lock()
        self._info = {}
        self._settings = {}
        self._rosters = {}
        self._encoder_schemas = {}
        self._poll_count = 0
        self._detail_errors = {}

    async def _create_transport(self, transport_type):
        host = str(self.config.get('host', '')).strip()
        if not host:
            raise ConnectionFaultError('No encoder address is entered.', code='invalid_config')
        if not self.config.get('username') or not self.config.get('password'):
            raise ConnectionFaultError('The encoder requires a username and password.', code='auth_failed')
        port = int(self.config.get('port', 80))
        if not await self._verify_reachable(host, port):
            raise ConnectionError('The encoder is not responding.')
        scheme = 'https' if self.config.get('ssl') else 'http'
        self._client = httpx.AsyncClient(
            base_url=f'{scheme}://{host}:{port}',
            verify=bool(self.config.get('verify_ssl', False)),
            timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=False,
        )

    async def _post_connect(self):
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._encoder_schemas.clear()
        self._detail_errors.clear()
        await self._login()
        self._info = await self._call('get-info')
        self._apply_identity()
        # Establish that this account can read the indispensable control state.
        await self._call('get-status', core=True)

    async def _initial_sync(self):
        self._poll_count = 0
        await self.refresh_children()
        preview = str(self.config.get('preview_url', '')).strip()
        scheme = preview.partition(':')[0].lower()
        if preview and scheme not in ('rtsp', 'srt'):
            raise ConnectionFaultError('Preview URL must use RTSP or SRT.', code='invalid_config')
        self.set_state('preview_url', preview)
        self.set_state('preview_format', scheme if preview else '')

    def _link_alive(self):
        return self._client is not None

    async def _close_session(self):
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self):
        # ping is documented as unauthenticated, and does not require configured streams.
        response = await self._request('ping')
        try:
            self._decode(response, 'ping')
        except UltraEncodeError:
            return  # A documented error reply still proves that the encoder is answering.

    async def _request(self, method, params=None, *, body=None):
        if self._client is None:
            raise ConnectionError('The encoder session is closed.')
        query = {'method': method, **(params or {})}
        verb = 'POST' if body is not None else 'GET'
        try:
            return await self._client.request(verb, '/usapi', params=query, json=body)
        except httpx.TransportError as exc:
            # Keep credentials and query strings out of a surfaced exception.
            raise ConnectionError('The encoder stopped answering HTTP requests.') from exc

    @staticmethod
    def _decode(response, method):
        if response.status_code in (401, 403):
            raise UltraEncodeError(method, -17 if response.status_code == 401 else -26)
        if response.status_code != 200:
            raise CommandRefusedError(f'The encoder answered HTTP {response.status_code} to {method}.')
        return MagewellUltraEncodeDriver._decode_body(response, method)

    @staticmethod
    def _decode_body(response, method):
        try:
            data = response.json()
        except ValueError as exc:
            raise CommandRefusedError(f'The encoder returned an invalid JSON reply to {method}.') from exc
        if not isinstance(data, dict) or type(data.get('result')) is not int:
            raise CommandRefusedError(f'The encoder returned an incomplete reply to {method}.')
        if data['result'] != 0:
            raise UltraEncodeError(method, data['result'])
        return data

    async def _login(self):
        digest = hashlib.md5(str(self.config['password']).encode('utf-8')).hexdigest()
        self.redact_in_log(digest)
        response = await self._request('login', body={'id': self.config['username'], 'pass': digest})
        try:
            data = self._decode(response, 'login')
        except UltraEncodeError as exc:
            self._login_error(exc)
        self._save_session(data)

    @staticmethod
    def _login_error(exc):
        if exc.code in (-1, -17, -25, -26, 36):
            raise ConnectionFaultError('The encoder refused the account login.', code='auth_failed') from exc
        raise exc

    def _save_session(self, data):
        # The PDF promises the cookie; the vendor's POST example also returns sid.
        sid = data.get('sid') or self._client.cookies.get('sid')
        if not isinstance(sid, str) or not sid:
            raise CommandRefusedError('The encoder accepted login but did not provide a session ID.')
        self.redact_in_log(sid)
        self._client.cookies.clear()
        self._client.cookies.set('sid', sid)

    async def _call(self, method, params=None, *, core=False):
        try:
            return self._decode(await self._request(method, params), method)
        except UltraEncodeError as exc:
            if exc.code == -26 and core:
                raise ConnectionFaultError('The encoder account cannot read control status.', code='auth_failed') from exc
            if exc.code != -17:
                raise
        # Polls and commands hold _operation_lock, so an expired session logs in once.
        await self._login()
        return await self._after_login(method, params, core=core)

    async def _after_login(self, method, params, *, core):
        try:
            return self._decode(await self._request(method, params), method)
        except UltraEncodeError as exc:
            fatal_codes = (-17, -26) if core else (-17,)
            if exc.code in fatal_codes:
                raise ConnectionFaultError('The encoder refused the renewed session.', code='auth_failed') from exc
            raise

    def _apply_identity(self):
        product = self._info.get('product', {})
        model = str(product.get('module-name', ''))
        known = self.DRIVER_INFO['config_schema']['model']['values']
        if model not in known or not str(product.get('firmware-ver-s', '')).startswith('2.'):
            raise ConnectionFaultError('This is not a supported Ultra Encode model on 2.x firmware.', code='invalid_config')
        for key, raw in [('model', 'module-name'), ('serial_number', 'sn'), ('firmware_version', 'firmware-ver-s')]:
            self.set_state(key, str(product.get(raw, '')))
        inputs = _choices(self._info.get('input-source', {}).get('sources', []))
        self.set_state('input_options', json.dumps(inputs))
        self.set_state('auto_record_options', json.dumps(_choices(self._info.get('rec', {}).get('trigger-mode', []))))
        self._narrow_commands(model, product)

    def _narrow_commands(self, model, product):
        if model != 'Ultra Encode AIO':
            self._remove_commands(('select_input', 'set_mixer'))
        if not product.get('record-support'):
            self._remove_commands(('start_recording', 'stop_recording', 'clear_record_status', 'set_recorder_enabled', 'set_auto_record'))
        self.DRIVER_INFO['quick_actions'] = [x for x in self.DRIVER_INFO['quick_actions'] if x in self.DRIVER_INFO['commands']]

    def _remove_commands(self, keys):
        for key in keys:
            self.DRIVER_INFO['commands'].pop(key, None)

    async def refresh_children(self):
        async with self._operation_lock:
            await self._read_details()
            self._apply_status(await self._call('get-status', core=True))
            self._report_detail_errors()
        return {kind: len(self.list_children(kind)) for kind in self.DRIVER_INFO['child_entity_types']}

    async def poll(self):
        async with self._operation_lock:
            self._apply_status(await self._call('get-status', core=True))
            self._poll_count += 1
            if self._poll_count % max(1, int(self.config.get('detail_poll_every', 5))) == 0:
                await self._read_details()
            self._report_detail_errors()

    def _report_detail_errors(self):
        self.set_state('last_error', '; '.join(self._detail_errors.values()))

    async def _read_details(self):
        self._settings = await self._call('get-settings')
        self._apply_settings()
        readers = [('get-surfaces', {}, self._apply_overlays)]
        product = self._info.get('product', {})
        if product.get('record-support'):
            readers.append(('get-rec-channels', {}, self._apply_recorders))
        if product.get('scheduler-support'):
            for kind in (0, 1, 2):
                readers.append(('get-schedulers', {'id': -1, 'chn-type': kind, 'chn-id': -1},
                                lambda data, kind=kind: self._apply_schedules(data, kind)))
        for method, params, apply in readers:
            await self._read_optional(method, params, apply)

    async def _read_optional(self, method, params, apply):
        key = method + str(params.get('chn-type', ''))
        try:
            apply(await self._call(method, params))
            self._detail_errors.pop(key, None)
        except UltraEncodeError as exc:
            if exc.code not in (-4, -14, -26):
                raise
            self._detail_errors[key] = str(exc)

    def _reconcile(self, kind, rows, prefix=''):
        roster = self._rosters.setdefault(kind, {})
        seen = set()
        for local, (raw, values) in rows.items():
            local = str(local)
            seen.add(local)
            roster[local] = raw
            self.register_child(kind, local, initial_state=values)
            self.set_child_state_batch(kind, local, values)
        for stale in set(roster) - seen:
            if str(stale).startswith(prefix):
                self.deregister_child(kind, stale)
                del roster[stale]

    def _apply_settings(self):
        data = self._settings
        self._copy_fields(data, [('name', 'device_name'), ('audio-sync-offset', 'audio_sync_offset_ms')])
        flags = _present_values(data, [('enable-deinterlace', 'deinterlace'), ('enable-ndi-hx3', 'ndi_hx3'), ('use-nosignal-file', 'no_signal_image')])
        for key, value in flags.items():
            self.set_state(key, _flag(value))
        if 'enable' in data.get('sub-stream', {}):
            self.set_state('substream_enabled', _flag(data['sub-stream']['enable']))
        self._apply_record_settings(data.get('rec', {}))
        self._apply_input_settings(data.get('input-source', {}))
        images = [{'value': x['id'], 'label': f'Image {x["id"]}'} for x in data.get('nosignal-files', []) if 'id' in x]
        self.set_state('no_signal_options', json.dumps(images))
        selected = next((x['id'] for x in data.get('nosignal-files', []) if x.get('is-use')), None)
        self.set_state('selected_no_signal_image', selected)
        self._apply_streams(data.get('stream-server', []))
        self._apply_audio(data.get('volume', {}))
        self._apply_encoders()

    def _copy_fields(self, data, fields):
        for key, value in _present_values(data, fields).items():
            self.set_state(key, value)

    def _apply_input_settings(self, data):
        mixer = data.get('mixer', {})
        self._copy_fields(mixer, [('input-device', 'mixer_format_source'), ('type', 'mixer_layout'), ('location', 'mixer_location')])
        if 'is-hdmi-top' in mixer:
            self.set_state('mixer_hdmi_on_top', _flag(mixer['is-hdmi-top']))

    def _apply_record_settings(self, data):
        if 'is-auto' in data:
            self.set_state('auto_record', _flag(data['is-auto']))
        self._copy_fields(data, [('trigger-mode', 'auto_record_trigger')])

    def _apply_streams(self, items):
        rows = {}
        for item in items:
            ident = str(item['id'])
            code = int(item['type'])
            rows[ident] = (item, {'name': str(item.get('name') or f'Stream {ident}'), 'online': True,
                                  'enabled': _flag(item.get('is-use')), 'protocol': _PROTOCOLS.get(code, f'Type {code}')})
        self._reconcile('stream', rows)

    def _apply_recorders(self, data):
        rows = {}
        for item in data.get('rec-channels', []):
            ident = str(item['id'])
            storage = {0: 'USB', 1: 'SD', 2: 'NAS'}.get(item.get('type'), str(item.get('type', '')))
            rows[ident] = (item, {'name': f'{storage} Recording {ident}', 'online': True,
                                  'enabled': _flag(item.get('is-use')), 'storage': storage})
        self._reconcile('recorder', rows)

    def _apply_audio(self, volume):
        rows = {}
        for raw, label in _AUDIO.items():
            limits = self._info.get('audio-range', {}).get(raw, {})
            if raw+'-gain' not in volume:
                continue
            if not {'min', 'max'} <= limits.keys():
                continue
            low, high = limits['min'], limits['max']
            gain = volume[raw+'-gain']
            percent = 100 * (gain-low) / (high-low) if high > low else 0
            rows[raw] = (limits, {'name': label, 'online': True, 'mute': not _flag(volume.get('is-'+raw)),
                                  'gain_raw': gain, 'gain_percent': round(percent, 2)})
        self._reconcile('audio', rows)
        if 'enable-mic-bias' in volume:
            self.set_state('microphone_bias', _flag(volume['enable-mic-bias']))

    def _encoder_schema(self, raw, value, index):
        prop, label, kind = _VIDEO_FIELDS[raw]
        schema = {'type': kind, 'label': label, 'control': raw not in ('fourcc', 'cbrstat')}
        caps = self._info.get('codec-cap', {})
        choice_key = self._choice_key(raw, index)
        choices = _choices(caps.get(choice_key, [])) if choice_key else []
        if not choices:
            choices = [{'value': v, 'label': label} for v, label in _VIDEO_ENUMS.get(raw, [])]
        if choices:
            schema.update(type='enum', values=choices)
        schema.update(self._video_limits(raw, index, caps))
        if raw in ('min-vbr-qp', 'max-vbr-qp'):
            schema.update(min=0, max=51)
        if raw in ('is-auto', 'codec') and index == 1:
            schema['control'] = False
        return prop, schema

    def _choice_key(self, raw, index):
        if raw == 'duration' and self._settings.get('enable-ndi-hx3'):
            return 'ndi-hx3-durations'
        hevc = self._settings.get('main-stream', {}).get('codec') == 1
        if (raw, index, hevc) == ('profile', 0, True):
            return 'hevc-profile'
        return _CAP_CHOICES.get(raw)

    def _video_limits(self, raw, index, caps):
        keys = {'kbps': 'video-kbps', 'cx': 'width', 'cy': 'height', 'duration': 'duration'}
        if raw not in keys:
            return {}
        stream = 'main-stream' if index == 0 else 'sub-stream'
        if index == 0 and self._settings.get('enable-ndi-hx3'):
            stream = 'ndi-hx3-main-stream'
        limits = caps.get(stream, {})
        values = {'min': limits.get('min-'+keys[raw], 1)}
        if 'max-'+keys[raw] in limits:
            values['max'] = limits['max-'+keys[raw]]
        if raw == 'kbps':
            values['unit'] = 'kbps'
        return values

    def _apply_encoders(self):
        for index, name in enumerate(('main-stream', 'sub-stream')):
            data = self._settings.get(name, {})
            schema = deepcopy(self.DRIVER_INFO['child_entity_types']['encoder']['state_variables'])
            values = {'name': 'Main Stream' if index == 0 else 'Substream', 'online': True}
            for raw in _VIDEO_FIELDS.keys() & data.keys():
                prop, field = self._encoder_schema(raw, data[raw], index)
                schema[prop] = field
                values[prop] = _flag(data[raw]) if field['type'] == 'boolean' else data[raw]
            ident = str(index)
            if self._encoder_schemas.get(ident) != schema:
                self.deregister_child('encoder', ident)
                self.register_child('encoder', ident, initial_state=values, schema=schema)
                self._encoder_schemas[ident] = schema
            else:
                self.set_child_state_batch('encoder', ident, values)

    def _apply_overlays(self, data):
        rows = {}
        for item in data.get('surfaces', []):
            ident = str(item['id'])
            rows[ident] = (item, {'name': str(item.get('text') or f'Overlay {ident}'), 'online': True,
                                  'enabled': _flag(item.get('is-use')), 'stream': int(item.get('stream-index', 0))})
        self._reconcile('overlay', rows)
        for key, raw in [('main_overlays', 'main-surface'), ('sub_overlays', 'second-surface')]:
            if raw in data:
                self.set_state(key, _flag(data[raw]))

    def _apply_schedules(self, data, kind):
        rows = {}
        for item in data.get('schedulers', []):
            ident = f'{kind}_{item["id"]}'
            raw = {**item, 'chn-type': kind}
            rows[ident] = (raw, {'name': str(item.get('name') or f'Schedule {item["id"]}'), 'online': True,
                                  'enabled': _flag(item.get('is-use')), 'channel_type': ('Live', 'Recording', 'Reboot')[kind],
                                  'channel_id': int(item.get('chn-id', -1))})
        self._reconcile('schedule', rows, prefix=f'{kind}_')

    def _apply_status(self, data):
        mask = int(data.get('cur-status', 0))
        self.set_state('status_mask', mask)
        self.set_state('upgrading', bool(mask & 0x4000))
        self.set_state('rebooting', bool(mask & 0x20000000))
        self._copy_fields(data, [('box-name', 'device_name'), ('input-source', 'input_source')])
        signal = data.get('input-signal', {})
        self._copy_fields(signal, [('cx', 'signal_width'), ('cy', 'signal_height'), ('frame-rate', 'signal_frame_rate'), ('status', 'signal_status')])
        live = data.get('live-status', {}).get('live', [])
        rec = data.get('rec-status', {}).get('rec', [])
        self._apply_channel_status('stream', live, 22)
        self._apply_channel_status('recorder', rec, 2)
        self.set_state('streaming', any(x.get('result') == 22 for x in live))
        self.set_state('recording', any(x.get('result') == 2 for x in rec))

    def _apply_channel_status(self, kind, items, running_code):
        updates = []
        seen = set()
        for item in items:
            ident = str(item['id'])
            if ident not in self._rosters.get(kind, {}):
                continue
            values = _channel_values(item, kind, running_code)
            updates.append((kind, ident, values))
            seen.add(ident)
        for ident in self._rosters.get(kind, {}).keys() - seen:
            schema = self.DRIVER_INFO['child_entity_types'][kind]['state_variables']
            values = {key: None for key in schema if key not in ('name', 'online', 'enabled', 'protocol', 'storage')}
            values['status'] = 'Not reported'
            updates.append((kind, ident, values))
        self.set_children_state_batch(updates)

    async def set_device_setting(self, key, value):
        return await self.send_command('set_'+key, {'value': value})

    async def send_command(self, command, params=None):
        if command not in self.DRIVER_INFO['commands']:
            raise CommandRefusedError('This command is not available on this encoder.')
        params = params or {}
        async with self._operation_lock:
            await self._dispatch(command, params)
            if command not in ('reboot', 'factory_reset'):
                await self._read_details()
                self._apply_status(await self._call('get-status', core=True))
                self._report_detail_errors()

    async def _dispatch(self, command, params):
        if command == 'refresh':
            return
        if command in _SIMPLE:
            await self._call(_SIMPLE[command])
            return
        setting = command.removeprefix('set_')
        if setting in self.DRIVER_INFO['device_settings']:
            await self._write_setting(setting, params['value'])
            return
        handlers = {'set_audio_mute': self._write_audio, 'set_audio_gain': self._write_audio,
                    'select_input': self._select_input, 'set_video_parameter': self._write_video,
                    'set_mixer': self._write_mixer, 'select_no_signal_image': self._write_image,
                    'set_auto_record': self._write_auto_record}
        if command.endswith('_enabled'):
            await self._write_enabled(command, params)
            return
        handler = handlers.get(command)
        if handler is None:
            raise CommandRefusedError('The encoder command has no implementation.')
        await handler(params)

    async def _write_setting(self, key, value):
        if key in ('main_overlays', 'sub_overlays'):
            await self._call('set-enable-surface', {'stream-index': int(key == 'sub_overlays'), 'enable': int(_flag(value))})
            return
        method, field = _SETTINGS[key]
        if self.DRIVER_INFO['device_settings'][key]['type'] == 'boolean':
            value = int(_flag(value))
        await self._call(method, {field: value})

    async def _write_enabled(self, command, params):
        kind = command[len('set_'):-len('_enabled')]
        raw = self._target(kind, params['target'])
        methods = {'stream': 'enable-server', 'recorder': 'enable-rec-channel',
                   'overlay': 'enable-surface', 'schedule': 'enable-scheduler'}
        values = {'id': raw['id'], 'is-use': int(_flag(params['enabled']))}
        if kind == 'schedule':
            values.update({'chn-type': raw['chn-type'], 'chn-id': -1})
        await self._call(methods[kind], values)

    def _target(self, kind, target):
        raw = self._rosters.get(kind, {}).get(str(target))
        if raw is None:
            raise CommandRefusedError('The selected item is no longer present on the encoder.')
        return raw

    async def _write_audio(self, params):
        target = str(params['target'])
        limits = self._target('audio', target)
        fresh = await self._call('get-settings')
        volume = fresh.get('volume', {})
        values = {key: value for key, value in volume.items()
                  if key in {x for name in _AUDIO for x in ('is-'+name, name+'-gain')}}
        if 'mute' in params:
            values['is-'+target] = int(not _flag(params['mute']))
        else:
            values[target+'-gain'] = self._gain_value(params['level'], limits)
        await self._call('set-volume', values)

    @staticmethod
    def _gain_value(level, limits):
        percent = float(level)
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            raise ValueError('Gain must be between 0% and 100%.')
        return round(limits['min'] + percent/100*(limits['max']-limits['min']))

    async def _select_input(self, params):
        options = self._info.get('input-source', {}).get('sources', [])
        source = int(params['source'])
        if source not in {x['value'] for x in options}:
            raise ValueError('Choose an input reported by the encoder.')
        await self._call('select-input-source', {'input-source': source})

    async def _write_video(self, params):
        index = str(params['target'])
        if index not in ('0', '1'):
            raise ValueError('Choose the main stream or substream.')
        self._settings = await self._call('get-settings')
        self._apply_encoders()
        schema = self._encoder_schemas.get(index, {}).get(params['control'], {})
        if not schema.get('control'):
            raise CommandRefusedError('This encoding parameter cannot be changed.')
        raw = next(key for key, desc in _VIDEO_FIELDS.items() if desc[0] == params['control'])
        source = self._settings['main-stream' if index == '0' else 'sub-stream']
        values = {key: source[key] for key in _VIDEO_FIELDS.keys() & source.keys()}
        values[raw] = _valid_value(params['value'], schema)
        self._validate_video_combination(values)
        values['stream'] = int(index)
        await self._call('set-video', values)

    def _validate_video_combination(self, values):
        if values.get('min-vbr-qp', 0) > values.get('max-vbr-qp', 51):
            raise ValueError('Minimum quantizer cannot exceed maximum quantizer.')
        key = 'hevc-profile' if values.get('codec') == 1 else 'profile'
        profiles = self._info.get('codec-cap', {}).get(key, [])
        if profiles and values.get('profile') not in {x['value'] for x in profiles}:
            raise ValueError('Choose a profile supported by the new codec before changing the codec.')

    async def _write_mixer(self, params):
        mode = int(params['layout'])
        key = 'pip' if mode == 0 else 'sbs'
        choices = self._info.get('input-source', {}).get('video-mixer', {}).get(key, [])
        if int(params['location']) not in {x['value'] for x in choices}:
            raise ValueError('Choose a location supported by this mixer layout.')
        await self._call('set-video-mixer-config', {'input-device': int(params['format_source']),
                         'is-hdmi-top': int(_flag(params['hdmi_on_top'])), 'type': mode, 'location': int(params['location'])})

    async def _write_image(self, params):
        ident = int(params['image'])
        if ident not in {x['id'] for x in self._settings.get('nosignal-files', [])}:
            raise ValueError('Choose an image stored on the encoder.')
        await self._call('set-nosignal-file', {'id': ident})

    async def _write_auto_record(self, params):
        options = self._info.get('rec', {}).get('trigger-mode', [])
        trigger = int(params['trigger'])
        if trigger not in {x['value'] for x in options}:
            raise ValueError('Choose a recording trigger reported by the encoder.')
        await self._call('set-auto-rec', {'is-auto': int(_flag(params['enabled'])), 'trigger-mode': trigger})
