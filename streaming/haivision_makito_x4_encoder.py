"""Haivision Makito X4 Single/Quad operational REST API driver.

Python is required for device-enumerated sparse rosters and cookie-session
renewal. The published REST API documents request/response polling, with no
operational-state subscription. Bulk snapshots and command readback report
actual state; a missing measurement remains unknown. All rosters are read
again after reconnect. Configured recorders are enumerated from streams,
not GET /apis/recordings, which the manufacturer documents as unimplemented.

Destination and codec administration remain in the encoder web interface.
SRT round-trip time is reported in microseconds by the API and converted to ms.
The source includes both numeric strings and JSON numbers in its examples.
No physical unit has been tested.
Source: https://mx4e.demo.haivision.com/apidoc/ (1.10.0).
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from html import unescape
import json
import math
from urllib.parse import quote

import httpx

from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError


class HaivisionMakitoX4EncoderDriver(BaseDriver):
    DRIVER_INFO = {'id': 'haivision_makito_x4_encoder',
     'name': 'Haivision Makito X4 Encoder',
     'manufacturer': 'Haivision',
     'category': 'streaming',
     'version': '1.0.0',
     'author': 'OpenAVC',
     'transport': 'http',
     'ports': [443],
     'protocols': ['https'],
     'source_url': 'https://mx4e.demo.haivision.com/apidoc/',
     'description': 'Controls Makito X4 streams, video and audio encoders, named presets and configured '
                    'recordings, with input signal and SRT link feedback.',
     'simulated': True,
     'verified': False,
     'min_platform_version': '0.37.1',
     'tags': ['encoder', 'srt', 'streaming', 'recording', 'hevc', 'h264'],
     'compatible_models': [{'manufacturer': 'Haivision',
                            'models': ['Makito X4 Encoder Single', 'Makito X4 Encoder Quad'],
                            'confidence': 'untested',
                            'notes': 'REST API on firmware 1.7 or later. Recording requires storage-equipped '
                                     'hardware and firmware 1.10. Hardware testing is pending.'}],
     'help': {'overview': 'Controls configured streams and recordings independently, recalls actual saved '
                          'presets, and reports input signals, encoder state and both SRT paths. Configure '
                          'stream destinations and encoding profiles in the encoder web interface.',
              'setup': 'Enter the encoder address and an account permitted to control it. HTTPS is required. Use '
                       'firmware 1.7 or later; configured recording controls require firmware 1.10 and on-device '
                       'storage. Refresh Status updates the available objects and presets. A stopped encoder or '
                       'stream remains listed. Certificate verification requires a certificate trusted by this '
                       'computer.'},
     'default_config': {'host': '',
                        'port': 443,
                        'ssl': True,
                        'verify_ssl': False,
                        'username': '',
                        'password': '',
                        'poll_interval': 3,
                        'detail_poll_every': 5,
                        'simulated_storage': False,
                        'simulated_model': 'Quad'},
     'config_schema': {'host': {'type': 'string', 'label': 'IP Address or Hostname', 'required': True},
                       'port': {'type': 'integer', 'label': 'HTTPS Port', 'default': 443, 'min': 1, 'max': 65535},
                       'ssl': {'type': 'boolean', 'label': 'HTTPS', 'default': True},
                       'verify_ssl': {'type': 'boolean', 'label': 'Verify HTTPS Certificate', 'default': False},
                       'username': {'type': 'string', 'label': 'Username', 'required': True, 'default': ''},
                       'password': {'type': 'string',
                                    'label': 'Password',
                                    'required': True,
                                    'secret': True,
                                    'default': ''},
                       'poll_interval': {'type': 'number',
                                         'label': 'Poll Interval (seconds)',
                                         'default': 3,
                                         'min': 0},
                       'detail_poll_every': {'type': 'integer',
                                             'label': 'Refresh Settings Every N Polls',
                                             'default': 5,
                                             'min': 1,
                                             'max': 100,
                                             'advanced': True},
                       'simulated_storage': {'type': 'boolean',
                                             'label': 'Recording Hardware in Simulation',
                                             'default': False,
                                             'advanced': True},
                       'simulated_model': {'type': 'enum',
                                           'label': 'Model in Simulation',
                                           'default': 'Quad',
                                           'values': ['Single', 'Quad'],
                                           'advanced': True,
                                           'help': 'Used only for simulation. Connected hardware supplies its '
                                                   'own inputs and encoders.'}},
     'state_variables': {'model': {'type': 'string', 'label': 'Model'},
                         'part_number': {'type': 'string', 'label': 'Part Number'},
                         'serial_number': {'type': 'string', 'label': 'Serial Number'},
                         'firmware_version': {'type': 'string', 'label': 'Firmware'},
                         'device_name': {'type': 'string', 'label': 'Device Name'},
                         'personality': {'type': 'string', 'label': 'Input Mode'},
                         'input_count': {'type': 'integer', 'label': 'Video Inputs'},
                         'storage_available': {'type': 'boolean', 'label': 'Recording Hardware'},
                         'card_status': {'type': 'string', 'label': 'System Status', 'cloud_priority': 'high'},
                         'temperature_c': {'type': 'number',
                                           'label': 'Temperature',
                                           'unit': '°C',
                                           'cloud_priority': 'low'},
                         'chipset_load_percent': {'type': 'number',
                                                  'label': 'Chipset Load',
                                                  'unit': '%',
                                                  'cloud_priority': 'low'},
                         'uptime': {'type': 'string', 'label': 'Uptime', 'cloud_priority': 'low'},
                         'active_preset': {'type': 'string',
                                           'label': 'Active Preset',
                                           'control': True,
                                           'cloud_priority': 'high'},
                         'preset_modified': {'type': 'boolean',
                                             'label': 'Unsaved Preset Changes',
                                             'cloud_priority': 'high'},
                         'startup_preset': {'type': 'string', 'label': 'Startup Preset', 'control': True},
                         'preset_options': {'type': 'string', 'label': 'Preset Choices'},
                         'preset_autosave': {'type': 'boolean', 'label': 'Preset Auto-Save', 'control': True},
                         'preview_enabled': {'type': 'boolean', 'label': 'Preview Thumbnails', 'control': True},
                         'preview_interval': {'type': 'integer',
                                              'label': 'Thumbnail Interval',
                                              'unit': 's',
                                              'control': True},
                         'stream_count': {'type': 'integer', 'label': 'Configured Streams'},
                         'running_streams': {'type': 'integer',
                                             'label': 'Streaming Destinations',
                                             'cloud_priority': 'high'},
                         'last_error': {'type': 'string', 'label': 'Last Error', 'cloud_priority': 'high'}},
     'child_entity_types': {'input': {'label': 'Video Input',
                                      'label_plural': 'Video Inputs',
                                      'id_format': {'type': 'integer', 'min': 0},
                                      'label_field': 'name',
                                      'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                          'online': {'type': 'boolean', 'label': 'Available'},
                                                          'status': {'type': 'string',
                                                                     'label': 'Input Status',
                                                                     'cloud_priority': 'high'},
                                                          'interface_type': {'type': 'integer',
                                                                             'label': 'Interface Type'}},
                                      'summary_fields': ['name', 'status']},
                            'video_encoder': {'label': 'Video Encoder',
                                              'label_plural': 'Video Encoders',
                                              'id_format': {'type': 'integer', 'min': 0},
                                              'label_field': 'name',
                                              'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                                  'online': {'type': 'boolean',
                                                                             'label': 'Available'},
                                                                  'input_id': {'type': 'integer',
                                                                               'label': 'Input ID'},
                                                                  'input_name': {'type': 'string',
                                                                                 'label': 'Input'},
                                                                  'signal_present': {'type': 'boolean',
                                                                                     'label': 'Input Signal',
                                                                                     'cloud_priority': 'high'},
                                                                  'input_format': {'type': 'string',
                                                                                   'label': 'Input Format'},
                                                                  'resolution': {'type': 'string',
                                                                                 'label': 'Encoded Resolution'},
                                                                  'codec': {'type': 'string', 'label': 'Codec'},
                                                                  'bitrate_kbps': {'type': 'number',
                                                                                   'label': 'Configured Bit Rate',
                                                                                   'unit': 'kbps'},
                                                                  'actual_bitrate_kbps': {'type': 'number',
                                                                                          'label': 'Encoded Bit '
                                                                                                   'Rate',
                                                                                          'unit': 'kbps',
                                                                                          'cloud_priority': 'low'},
                                                                  'state_code': {'type': 'integer',
                                                                                 'label': 'Encoder State Code'},
                                                                  'status': {'type': 'string',
                                                                             'label': 'Encoder Status',
                                                                             'cloud_priority': 'high'},
                                                                  'running': {'type': 'boolean',
                                                                              'label': 'Encoding',
                                                                              'control': True,
                                                                              'cloud_priority': 'high'},
                                                                  'dropped_frames': {'type': 'integer',
                                                                                     'label': 'Dropped Frames',
                                                                                     'cloud_priority': 'low'}},
                                              'summary_fields': ['name', 'status', 'input_name', 'input_format']},
                            'audio_encoder': {'label': 'Audio Encoder',
                                              'label_plural': 'Audio Encoders',
                                              'id_format': {'type': 'integer', 'min': 0},
                                              'label_field': 'name',
                                              'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                                  'online': {'type': 'boolean',
                                                                             'label': 'Available'},
                                                                  'input_id': {'type': 'integer',
                                                                               'label': 'Audio Input ID'},
                                                                  'codec': {'type': 'string', 'label': 'Codec'},
                                                                  'mode': {'type': 'string',
                                                                           'label': 'Channel Mode'},
                                                                  'bitrate_kbps': {'type': 'number',
                                                                                   'label': 'Configured Bit Rate',
                                                                                   'unit': 'kbps'},
                                                                  'actual_bitrate_kbps': {'type': 'number',
                                                                                          'label': 'Encoded Bit '
                                                                                                   'Rate',
                                                                                          'unit': 'kbps',
                                                                                          'cloud_priority': 'low'},
                                                                  'state_code': {'type': 'integer',
                                                                                 'label': 'Encoder State Code'},
                                                                  'status': {'type': 'string',
                                                                             'label': 'Encoder Status',
                                                                             'cloud_priority': 'high'},
                                                                  'running': {'type': 'boolean',
                                                                              'label': 'Encoding',
                                                                              'control': True,
                                                                              'cloud_priority': 'high'},
                                                                  'muted': {'type': 'boolean',
                                                                            'label': 'Muted',
                                                                            'cloud_priority': 'high'},
                                                                  'level_percent': {'type': 'number',
                                                                                    'label': 'Peak Input Level',
                                                                                    'unit': '%',
                                                                                    'cloud_priority': 'low'},
                                                                  'encoder_errors': {'type': 'integer',
                                                                                     'label': 'Encoder Errors',
                                                                                     'cloud_priority': 'high'}},
                                              'summary_fields': ['name', 'status', 'muted']},
                            'stream': {'label': 'Stream',
                                       'label_plural': 'Streams',
                                       'id_format': {'type': 'integer', 'min': 0},
                                       'label_field': 'name',
                                       'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                           'online': {'type': 'boolean', 'label': 'Available'},
                                                           'protocol': {'type': 'string', 'label': 'Protocol'},
                                                           'state_code': {'type': 'integer',
                                                                          'label': 'Stream State Code'},
                                                           'status': {'type': 'string',
                                                                      'label': 'Stream Status',
                                                                      'cloud_priority': 'high'},
                                                           'running': {'type': 'boolean',
                                                                       'label': 'Streaming',
                                                                       'control': True,
                                                                       'cloud_priority': 'high'},
                                                           'bitrate_kbps': {'type': 'number',
                                                                            'label': 'Stream Bit Rate',
                                                                            'unit': 'kbps',
                                                                            'cloud_priority': 'low'},
                                                           'video_encoders': {'type': 'string',
                                                                              'label': 'Video Encoders'},
                                                           'audio_encoders': {'type': 'string',
                                                                              'label': 'Audio Encoders'},
                                                           'srt_mode': {'type': 'string', 'label': 'SRT Mode'},
                                                           'uptime': {'type': 'string',
                                                                      'label': 'Stream Uptime',
                                                                      'cloud_priority': 'low'},
                                                           'unsent_packets': {'type': 'integer',
                                                                              'label': 'Unsent Packets',
                                                                              'cloud_priority': 'high'},
                                                           'srt_status': {'type': 'string',
                                                                          'label': 'SRT Status',
                                                                          'cloud_priority': 'high'},
                                                           'srt_rtt_ms': {'type': 'number',
                                                                          'label': 'SRT Round-Trip Time',
                                                                          'unit': 'ms',
                                                                          'cloud_priority': 'low'},
                                                           'srt_latency_ms': {'type': 'number',
                                                                              'label': 'SRT Latency',
                                                                              'unit': 'ms',
                                                                              'cloud_priority': 'low'},
                                                           'srt_lost_packets': {'type': 'integer',
                                                                                'label': 'SRT Lost Packets',
                                                                                'cloud_priority': 'low'},
                                                           'srt_dropped_packets': {'type': 'integer',
                                                                                   'label': 'SRT Dropped Packets',
                                                                                   'cloud_priority': 'high'},
                                                           'srt_resent_packets': {'type': 'integer',
                                                                                  'label': 'SRT Retransmitted '
                                                                                           'Packets',
                                                                                  'cloud_priority': 'low'},
                                                           'srt_path2_status': {'type': 'string',
                                                                                'label': 'SRT Path 2 Status',
                                                                                'cloud_priority': 'high'},
                                                           'srt_path2_rtt_ms': {'type': 'number',
                                                                                'label': 'SRT Path 2 Round-Trip '
                                                                                         'Time',
                                                                                'unit': 'ms',
                                                                                'cloud_priority': 'low'},
                                                           'srt_path2_latency_ms': {'type': 'number',
                                                                                    'label': 'SRT Path 2 Latency',
                                                                                    'unit': 'ms',
                                                                                    'cloud_priority': 'low'},
                                                           'srt_path2_lost_packets': {'type': 'integer',
                                                                                      'label': 'SRT Path 2 Lost '
                                                                                               'Packets',
                                                                                      'cloud_priority': 'low'},
                                                           'srt_path2_dropped_packets': {'type': 'integer',
                                                                                         'label': 'SRT Path 2 '
                                                                                                  'Dropped '
                                                                                                  'Packets',
                                                                                         'cloud_priority': 'high'},
                                                           'srt_path2_resent_packets': {'type': 'integer',
                                                                                        'label': 'SRT Path 2 '
                                                                                                 'Retransmitted '
                                                                                                 'Packets',
                                                                                        'cloud_priority': 'low'}},
                                       'summary_fields': ['name', 'protocol', 'status', 'bitrate_kbps']},
                            'recording': {'label': 'Recording',
                                          'label_plural': 'Recordings',
                                          'id_format': {'type': 'integer', 'min': 0},
                                          'label_field': 'name',
                                          'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                              'online': {'type': 'boolean', 'label': 'Available'},
                                                              'status': {'type': 'string',
                                                                         'label': 'Recording Status',
                                                                         'cloud_priority': 'high'},
                                                              'running': {'type': 'boolean',
                                                                          'label': 'Recording',
                                                                          'control': True,
                                                                          'cloud_priority': 'high'},
                                                              'duration': {'type': 'string',
                                                                           'label': 'Recorded Duration',
                                                                           'cloud_priority': 'low'},
                                                              'bytes_stored': {'type': 'integer',
                                                                               'label': 'Recorded Bytes',
                                                                               'unit': 'bytes',
                                                                               'cloud_priority': 'low'},
                                                              'progress_percent': {'type': 'number',
                                                                                   'label': 'Recording Limit '
                                                                                            'Used',
                                                                                   'unit': '%',
                                                                                   'cloud_priority': 'low'},
                                                              'auto_start': {'type': 'boolean',
                                                                             'label': 'Start with Stream'},
                                                              'queue_overflows': {'type': 'integer',
                                                                                  'label': 'Recording Queue '
                                                                                           'Overflows',
                                                                                  'cloud_priority': 'high'},
                                                              'filename': {'type': 'string',
                                                                           'label': 'Current File'},
                                                              'format': {'type': 'string', 'label': 'Format'}},
                                          'summary_fields': ['name', 'status', 'duration']}},
     'commands': {'refresh_status': {'label': 'Refresh Status',
                                     'help': 'Read the current streams, encoders, presets and recording state '
                                             'from the encoder.',
                                     'params': {}},
                  'start_stream': {'label': 'Start Stream',
                                   'help': 'Start the selected stream. The reported status follows the encoder.',
                                   'params': {'target': {'type': 'child_id',
                                                         'child_type': 'stream',
                                                         'label': 'Stream',
                                                         'required': True}}},
                  'stop_stream': {'label': 'Stop Stream',
                                  'help': 'Stop the selected stream. The reported status follows the encoder.',
                                  'params': {'target': {'type': 'child_id',
                                                        'child_type': 'stream',
                                                        'label': 'Stream',
                                                        'required': True}},
                                  'sets': {'running': False}},
                  'start_video_encoder': {'label': 'Start Video Encoder',
                                          'help': 'Start the selected video encoder. The reported status follows '
                                                  'the encoder.',
                                          'params': {'target': {'type': 'child_id',
                                                                'child_type': 'video_encoder',
                                                                'label': 'Video Encoder',
                                                                'required': True}}},
                  'stop_video_encoder': {'label': 'Stop Video Encoder',
                                         'help': 'Stop the selected video encoder. The reported status follows '
                                                 'the encoder.',
                                         'params': {'target': {'type': 'child_id',
                                                               'child_type': 'video_encoder',
                                                               'label': 'Video Encoder',
                                                               'required': True}},
                                         'sets': {'running': False}},
                  'start_audio_encoder': {'label': 'Start Audio Encoder',
                                          'help': 'Start the selected audio encoder. The reported status follows '
                                                  'the encoder.',
                                          'params': {'target': {'type': 'child_id',
                                                                'child_type': 'audio_encoder',
                                                                'label': 'Audio Encoder',
                                                                'required': True}}},
                  'stop_audio_encoder': {'label': 'Stop Audio Encoder',
                                         'help': 'Stop the selected audio encoder. The reported status follows '
                                                 'the encoder.',
                                         'params': {'target': {'type': 'child_id',
                                                               'child_type': 'audio_encoder',
                                                               'label': 'Audio Encoder',
                                                               'required': True}},
                                         'sets': {'running': False}},
                  'start_recording': {'label': 'Start Recording',
                                      'help': 'Start the selected recording. The reported status follows the '
                                              'encoder.',
                                      'params': {'target': {'type': 'child_id',
                                                            'child_type': 'recording',
                                                            'label': 'Recording',
                                                            'required': True}}},
                  'stop_recording': {'label': 'Stop Recording',
                                     'help': 'Stop the selected recording. The reported status follows the '
                                             'encoder.',
                                     'params': {'target': {'type': 'child_id',
                                                           'child_type': 'recording',
                                                           'label': 'Recording',
                                                           'required': True}},
                                     'sets': {'running': False}},
                  'load_preset': {'label': 'Recall Preset',
                                  'help': 'Apply a saved encoder configuration. This can change or interrupt '
                                          'active streams.',
                                  'params': {'preset': {'type': 'string',
                                                        'label': 'Preset',
                                                        'required': True,
                                                        'options_state': 'preset_options',
                                                        'trim': False}},
                                  'sets': {'active_preset': '{preset}'}},
                  'save_preset': {'label': 'Save to Preset',
                                  'help': 'Replace the selected saved preset with the current encoder '
                                          'configuration.',
                                  'params': {'preset': {'type': 'string',
                                                        'label': 'Preset',
                                                        'required': True,
                                                        'options_state': 'preset_options',
                                                        'trim': False}},
                                  'confirm': 'Replace this saved preset with the current encoder configuration?'},
                  'set_startup_preset': {'label': 'Set Startup Preset',
                                         'help': 'Use the selected saved preset the next time the encoder '
                                                 'starts.',
                                         'params': {'preset': {'type': 'string',
                                                               'label': 'Preset',
                                                               'required': True,
                                                               'options_state': 'preset_options',
                                                               'trim': False}},
                                         'sets': {'startup_preset': '{preset}'}},
                  'identify': {'label': 'Identify Encoder',
                               'help': 'Blink the encoder status LED for the selected number of minutes.',
                               'params': {'minutes': {'type': 'integer',
                                                      'label': 'Minutes',
                                                      'min': 1,
                                                      'max': 60,
                                                      'default': 1}}},
                  'stop_identifying': {'label': 'Stop Identifying',
                                       'help': 'Stop the encoder status LED identification blink.',
                                       'params': {}}},
     'quick_actions': ['start_stream', 'stop_stream', 'load_preset', 'start_recording', 'stop_recording'],
     'device_settings': {'preset_autosave': {'type': 'boolean',
                                             'label': 'Preset Auto-Save',
                                             'state_key': 'preset_autosave',
                                             'default': True,
                                             'setup': False,
                                             'help': 'Automatically save configuration changes on the encoder.'},
                         'preview_enabled': {'type': 'boolean',
                                             'label': 'Preview Thumbnails',
                                             'state_key': 'preview_enabled',
                                             'default': True,
                                             'setup': False,
                                             'help': 'Generate preview thumbnails in the encoder web interface.'},
                         'preview_interval': {'type': 'integer',
                                              'label': 'Thumbnail Interval (seconds)',
                                              'state_key': 'preview_interval',
                                              'default': 6,
                                              'setup': False,
                                              'min': 1,
                                              'max': 60}}}

    HEALTH_FAULT_MESSAGE = 'The encoder stopped answering HTTPS requests.'

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._client = None
        self._operation_lock = asyncio.Lock()
        self._auth_rejected = False
        self._rosters = {}
        self._presets = {}
        self._poll_number = 0
        self._optional_errors = {}
        self._system_info = {}

    async def _pre_connect(self):
        self._auth_rejected = False
        self._optional_errors.clear()
        self._poll_number = 0
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._invalidate_readings()

    def _invalidate_readings(self):
        for key in self.DRIVER_INFO['state_variables']:
            self.set_state(key, '[]' if key == 'preset_options' else None)
        for kind, definition in self.DRIVER_INFO['child_entity_types'].items():
            for ident in self.list_children(kind):
                values = {key: None for key in definition['state_variables'] if key != 'name'}
                values['online'] = False
                self.set_child_state_batch(kind, ident, values)

    async def _create_transport(self, transport_type):
        if not self.config.get('username') or not self.config.get('password'):
            raise ConnectionFaultError('The encoder requires a username and password.', code='auth_failed')
        if not self.config.get('ssl', True):
            raise ConnectionFaultError('The Makito X4 API requires HTTPS.', code='invalid_config')
        self._client = httpx.AsyncClient(
            base_url=_connection_url(self.config), verify=bool(self.config.get('verify_ssl', False)),
            timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=False,
        )

    async def _post_connect(self):
        await self._login()
        status = await self._get('/apis/status', core=True)
        model = _text(status.get('cardType')) or ''
        if 'Makito X4' not in model or 'Encoder' not in model:
            raise ConnectionFaultError('This device did not identify as a Makito X4 encoder.', code='invalid_config')
        self._apply_status(status)
        await self._read_system_info()

    async def _initial_sync(self):
        await self.refresh_children()

    def _link_alive(self):
        return self._client is not None and not self._auth_rejected

    async def _close_session(self):
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._invalidate_readings()

    async def _raw(self, method, path, body=None):
        if self._client is None:
            raise ConnectionError('The encoder session is closed.')
        try:
            return await self._client.request(method, path, json=body)
        except httpx.TransportError as exc:
            raise ConnectionError('The encoder stopped answering HTTPS requests.') from exc

    def _reject_login(self, message):
        self._auth_rejected = True
        raise ConnectionFaultError(message, code='auth_failed')

    async def _login(self):
        if self._auth_rejected:
            self._reject_login('The encoder refused the account login.')
        self._client.cookies.clear()
        response = await self._raw('POST', '/apis/authentication', {
            'username': self.config['username'], 'password': self.config['password'],
        })
        if response.status_code in (401, 403):
            self._reject_login('The encoder refused the account login.')
        _check_status(response, 'sign in')
        for cookie in self._client.cookies.jar:
            self.redact_in_log(cookie.value)
        response = await self._raw('GET', '/apis/authentication')
        if response.status_code in (401, 403):
            self._reject_login('The encoder refused the new session.')
        account = _object_response(response, 'read the account session')
        if not account.get('username'):
            raise CommandRefusedError('The encoder returned an incomplete account session.')
        if _flag(account.get('passwordExp')) is True:
            self._reject_login('The encoder account password has expired.')

    async def _call(self, method, path, body=None, *, core=False):
        if self._auth_rejected:
            self._reject_login('The encoder refused the account login.')
        response = await self._raw(method, path, body)
        if response.status_code == 401:
            # Every caller holds the operation lock, so expiry causes one renewal.
            await self._login()
            response = await self._raw(method, path, body)
            if response.status_code == 401:
                self._reject_login('The encoder refused the renewed session.')
        if response.status_code == 403 and core:
            self._reject_login('The encoder account cannot read control status.')
        return response

    async def _get(self, path, *, core=False):
        response = await self._call('GET', path, core=core)
        return _object_response(response, 'read ' + path)

    async def _liveness_probe(self):
        # Any HTTP answer proves presence; an expired session is renewed by the
        # next operation, not by the watchdog. No configured stream is required.
        await self._raw('GET', '/apis/authentication')

    async def refresh_children(self):
        async with self._operation_lock:
            await self._refresh(full=True)
        return {kind: len(self.list_children(kind)) for kind in self.DRIVER_INFO['child_entity_types']}

    async def poll(self):
        async with self._operation_lock:
            self._poll_number += 1
            every = max(1, int(self.config.get('detail_poll_every', 5)))
            await self._refresh(full=self._poll_number % every == 0)

    async def _refresh(self, *, full):
        self._apply_status(await self._get('/apis/status', core=True))
        if full:
            await self._read_system_info()
            await self._read_inputs()
        for kind, path in _COLLECTION_PATHS.items():
            data = await self._get(path)
            rows = _rows(data, nested=True)
            self._rosters[kind] = rows
            self._replace_children(kind, {ident: self._child_values(kind, row) for ident, row in rows.items()})
        self._apply_recordings()
        streams = self._rosters['stream']
        self.set_state('stream_count', len(streams))
        states = [_integer(row['stats'].get('state')) for row in streams.values()]
        self.set_state('running_streams', sum(code == 1 for code in states) if all(code in _STREAM_STATES for code in states) else None)
        if full:
            await self._read_presets()
            await self._read_preview()
        self.set_state('last_error', '; '.join(self._optional_errors.values()))

    async def _read_system_info(self):
        info = await self._get('/apis/system_info', core=True)
        if _integer(info.get('videoInputs')) is None or _flag(info.get('storage')) is None:
            raise CommandRefusedError('The encoder returned incomplete hardware capabilities.')
        self._system_info = info
        self.set_state('device_name', _text(info.get('deviceName')))
        self.set_state('personality', _text(info.get('personality')))
        self.set_state('input_count', _integer(info.get('videoInputs')))
        storage = _flag(info.get('storage'))
        self.set_state('storage_available', storage)
        for name in ('start_recording', 'stop_recording'):
            if storage:
                self.DRIVER_INFO['commands'][name] = deepcopy(type(self).DRIVER_INFO['commands'][name])
            else:
                self.DRIVER_INFO['commands'].pop(name, None)
        self.DRIVER_INFO['quick_actions'] = [
            name for name in type(self).DRIVER_INFO['quick_actions'] if name in self.DRIVER_INFO['commands']
        ]

    async def _read_inputs(self):
        rows = _rows(await self._get('/apis/vidin'), nested=False)
        self._rosters['input'] = rows
        values = {ident: {'name': _text(row.get('name')), 'status': _text(row.get('state')),
                          'interface_type': _integer(row.get('interfaceType'))} for ident, row in rows.items()}
        self._replace_children('input', values)

    async def _optional(self, path):
        response = await self._call('GET', path)
        if response.status_code in (403, 404, 501):
            self._optional_errors[path] = f'{path}: encoder answered HTTP {response.status_code}.'
            return None
        data = _object_response(response, 'read ' + path)
        self._optional_errors.pop(path, None)
        return data

    async def _read_presets(self):
        data = await self._optional('/apis/presets')
        if data is None:
            self._presets = {}
            for key in ('active_preset', 'startup_preset', 'preset_modified', 'preset_autosave'):
                self.set_state(key, None)
            self.set_state('preset_options', '[]')
            return
        presets = _preset_rows(data)
        self._presets = presets
        options = [{'value': name, 'label': unescape(name)} for name in presets]
        self.set_state('preset_options', json.dumps(options))
        self.set_state('active_preset', data.get('active'))
        self.set_state('preset_modified', _flag(data.get('activeWasModified')))
        self.set_state('preset_autosave', _flag(data.get('autosave')))
        startup = [name for name, row in presets.items() if _flag(row.get('startup')) is True]
        self.set_state('startup_preset', startup[0] if len(startup) == 1 else None)

    async def _read_preview(self):
        data = await self._optional('/apis/services/preview')
        self.set_state('preview_enabled', _flag(data.get('enabled')) if data else None)
        self.set_state('preview_interval', _integer(data.get('interval')) if data else None)

    def _apply_status(self, data):
        fields = {'model': ('cardType', _text), 'part_number': ('partNumber', _text),
                  'serial_number': ('serialNumber', _text), 'firmware_version': ('firmwareVersion', _text),
                  'card_status': ('cardStatus', _text), 'temperature_c': ('temperature', _number),
                  'chipset_load_percent': ('chipsetLoad', _number), 'uptime': ('uptime', _text)}
        for key, (raw, convert) in fields.items():
            self.set_state(key, convert(data.get(raw)))

    def _replace_children(self, kind, rows):
        schema = self.DRIVER_INFO['child_entity_types'][kind]['state_variables']
        for ident, values in rows.items():
            # Clear optional fields when a subsequent complete snapshot omits them.
            complete = {key: values.get(key) for key in schema}
            complete['online'] = True  # These are virtual objects, including stopped ones.
            self.register_child(kind, ident, initial_state=complete)
            self.set_child_state_batch(kind, ident, complete)
        for ident in set(self.list_children(kind)) - set(rows):
            self.deregister_child(kind, ident)

    def _child_values(self, kind, row):
        if kind == 'stream':
            return _stream_values(row)
        if kind == 'video_encoder':
            values = _video_values(row)
            source = self._rosters.get('input', {}).get(values['input_id'], {})
            values['input_name'] = _text(source.get('name'))
            return values
        return _audio_values(row)

    def _apply_recordings(self):
        rows = {}
        if _flag(self._system_info.get('storage')) is True:
            for ident, row in self._rosters.get('stream', {}).items():
                recording = row['info'].get('recording', {})
                if isinstance(recording, dict) and _flag(recording.get('enabled')) is True:
                    rows[ident] = _recording_values(recording)
        self._replace_children('recording', rows)

    async def send_command(self, command, params=None):
        params = params or {}
        async with self._operation_lock:
            if command not in self.DRIVER_INFO['commands']:
                raise CommandRefusedError('This command is not available on this encoder.')
            if command in _CONTROL_COMMANDS:
                await self._control_object(command, params)
            elif command in _PRESET_COMMANDS:
                await self._control_preset(command, params)
            elif command == 'refresh_status':
                await self._refresh(full=True)
            else:
                await self._identify(command, params)

    async def _control_object(self, command, params):
        kind, path, verb = _CONTROL_COMMANDS[command]
        ident = _target_id(params.get('target'))
        if ident not in self.list_children(kind):
            raise CommandRefusedError('The selected object is no longer reported by the encoder. Refresh Status.')
        response = await self._call('PUT', f'{path}/{ident}/{verb}')
        _check_status(response, command.replace('_', ' '))
        if kind == 'recording':
            await self._read_recording(ident)
        else:
            row = await self._get(f'{path}/{ident}')
            _validate_object(row)
            self.set_child_state_batch(kind, ident, self._child_values(kind, row))
        # Read every resource a start/stop may affect, including dependent streams.
        await self._refresh(full=False)

    async def _read_recording(self, ident):
        data = await self._get(f'/apis/recordings/{ident}')
        recording = data.get('recording')
        if not isinstance(recording, dict):
            raise CommandRefusedError('The encoder returned incomplete recording status.')
        if _flag(recording.get('enabled')) is not True:
            self.deregister_child('recording', ident)
            raise CommandRefusedError('The recording is no longer configured on the encoder.')
        self.set_child_state_batch('recording', ident, _recording_values(recording))

    async def _control_preset(self, command, params):
        await self._read_presets()
        name = str(params.get('preset', ''))
        if name not in self._presets:
            raise CommandRefusedError('The selected preset is no longer on the encoder. Refresh Status.')
        suffix = _PRESET_COMMANDS[command]
        path = '/apis/presets/' + quote(name, safe='') + suffix
        body = None
        if command == 'save_preset':
            startup = _flag(self._presets[name].get('startup'))
            if startup is None:
                raise CommandRefusedError('The encoder did not report this preset’s startup setting.')
            body = {'startup': startup}
        _check_status(await self._call('PUT', path, body), command.replace('_', ' '))
        await self._refresh(full=True)

    async def _identify(self, command, params):
        method, body = 'DELETE', None
        if command == 'identify':
            minutes = _target_id(params.get('minutes', 1))
            if not 1 <= minutes <= 60:
                raise CommandRefusedError('Identification duration must be from 1 to 60 minutes.')
            method, body = 'POST', {'duration': minutes}
        _check_status(await self._call(method, '/apis/leds', body), 'identify the encoder')

    async def set_device_setting(self, key, value):
        async with self._operation_lock:
            if key == 'preset_autosave':
                desired = _required_flag(value)
                _check_status(await self._call('PUT', '/apis/presets', {'autosave': desired}), 'set preset auto-save')
                await self._read_presets()
            elif key in ('preview_enabled', 'preview_interval'):
                await self._write_preview(key, value)
            else:
                raise CommandRefusedError('This encoder setting is not available.')

    async def _write_preview(self, key, value):
        data = await self._get('/apis/services/preview')
        body = {'enabled': _flag(data.get('enabled')), 'interval': _integer(data.get('interval'))}
        if None in body.values():
            raise CommandRefusedError('The encoder did not report its current thumbnail settings.')
        if key == 'preview_enabled':
            body['enabled'] = _required_flag(value)
        else:
            interval = _target_id(value)
            if not 1 <= interval <= 60:
                raise CommandRefusedError('Thumbnail interval must be from 1 to 60 seconds.')
            body['interval'] = interval
        _check_status(await self._call('PUT', '/apis/services/preview', body), 'set thumbnail options')
        await self._read_preview()


_COLLECTION_PATHS = {'video_encoder': '/apis/videnc', 'audio_encoder': '/apis/audenc', 'stream': '/apis/streams'}
_CONTROL_COMMANDS = {
    'start_stream': ('stream', '/apis/streams', 'start'),
    'stop_stream': ('stream', '/apis/streams', 'stop'),
    'start_video_encoder': ('video_encoder', '/apis/videnc', 'start'),
    'stop_video_encoder': ('video_encoder', '/apis/videnc', 'stop'),
    'start_audio_encoder': ('audio_encoder', '/apis/audenc', 'start'),
    'stop_audio_encoder': ('audio_encoder', '/apis/audenc', 'stop'),
    'start_recording': ('recording', '/apis/recordings', 'start'),
    'stop_recording': ('recording', '/apis/recordings', 'stop'),
}
_PRESET_COMMANDS = {'load_preset': '/load', 'save_preset': '', 'set_startup_preset': '/startup'}
_STREAM_STATES = {0: 'Stopped', 1: 'Streaming', 2: 'Failed', 3: 'Connecting', 4: 'Securing',
                  5: 'Listening', 6: 'Paused', 7: 'Publishing', 8: 'Resolving', 9: 'Scrambled'}
_VIDEO_STATES = {0: 'Stopped', 3: 'Waiting for Input', 5: 'Not Encoding', 7: 'Encoding', 8: 'Resetting', 128: 'Failed'}
_AUDIO_STATES = {0: 'Stopped', 3: 'Encoding', 67: 'Muted', 128: 'Failed'}


def _text(value):
    return unescape(str(value)) if value is not None else None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(',', ''))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _integer(value):
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _target_id(value):
    ident = _integer(value)
    if ident is None or ident < 0:
        raise CommandRefusedError('Choose an object reported by the encoder.')
    return ident


def _flag(value):
    if str(value).lower() in ('true', '1'):
        return True
    if str(value).lower() in ('false', '0'):
        return False
    return None


def _required_flag(value):
    result = _flag(value)
    if result is None:
        raise CommandRefusedError('Choose on or off.')
    return result


def _check_status(response, operation):
    if not 200 <= response.status_code < 300:
        raise CommandRefusedError(f'The encoder refused to {operation} (HTTP {response.status_code}).')


def _object_response(response, operation):
    _check_status(response, operation)
    try:
        data = response.json()
    except ValueError as exc:
        raise CommandRefusedError('The encoder returned a reply that is not JSON.') from exc
    if not isinstance(data, dict):
        raise CommandRefusedError('The encoder returned an incomplete status reply.')
    return data


def _validate_object(row):
    if not isinstance(row, dict):
        raise CommandRefusedError('The encoder returned an invalid object.')
    if not all(isinstance(row.get(key), dict) for key in ('info', 'stats')):
        raise CommandRefusedError('The encoder returned an incomplete object list. Existing objects were kept.')


def _rows(data, *, nested):
    if not isinstance(data.get('data'), list):
        raise CommandRefusedError('The encoder did not return its object list. Existing objects were kept.')
    rows = {}
    for row in data['data']:
        info = _row_identity(row, nested)
        ident = _target_id(info.get('id'))
        if ident in rows:
            raise CommandRefusedError('The encoder returned duplicate object IDs. Existing objects were kept.')
        rows[ident] = row
    return rows


def _row_identity(row, nested):
    if nested:
        _validate_object(row)
        return row['info']
    if not isinstance(row, dict):
        raise CommandRefusedError('The encoder returned an invalid object.')
    return row


def _preset_rows(data):
    if not isinstance(data.get('data'), list):
        raise CommandRefusedError('The encoder did not return its preset list.')
    result = {}
    for item in data['data']:
        if not _named_preset(item):
            raise CommandRefusedError('The encoder returned an incomplete preset list.')
        result[item['name']] = item
    return result


def _named_preset(item):
    if not isinstance(item, dict):
        return False
    name = item.get('name')
    return isinstance(name, str) and bool(name)


def _connection_url(config):
    host = str(config.get('host', '')).strip()
    try:
        port = int(config.get('port', 443))
        if not _valid_address(host, port):
            raise ValueError
        return httpx.URL(scheme='https', host=host.strip('[]'), port=port)
    except (ValueError, httpx.InvalidURL) as exc:
        raise ConnectionFaultError('The encoder address or HTTPS port is invalid.', code='invalid_config') from exc


def _valid_address(host, port):
    if not host or not 1 <= port <= 65535:
        return False
    return not any(c in host for c in '/?#@')


def _mapped(data, fields):
    return {key: convert(data.get(raw)) for key, raw, convert in fields}


def _status(code, mapping):
    return mapping.get(code, f'State {code}') if code is not None else None


def _video_values(row):
    info, stats = row['info'], row['stats']
    code = _integer(stats.get('state'))
    values = _mapped(info, [('name', 'name', _text), ('input_id', 'interface', _integer),
                            ('bitrate_kbps', 'bitrate', _number)])
    values.update(_mapped(stats, [('signal_present', 'inputPresent', _flag), ('input_format', 'inputFormat', _text),
                                 ('resolution', 'resolution', _text), ('actual_bitrate_kbps', 'encodedBitRate', _number),
                                 ('dropped_frames', 'droppedFrames', _integer)]))
    codec = _integer(info.get('codecAlgorithm'))
    values.update(codec=_status(codec, {0: 'H.264', 1: 'H.265'}), state_code=code,
                  status=_status(code, _VIDEO_STATES), running=code == 7 if code is not None else None)
    return values


def _audio_values(row):
    info, stats = row['info'], row['stats']
    code = _integer(stats.get('state'))
    values = _mapped(info, [('name', 'name', _text), ('input_id', 'interface', _integer),
                            ('bitrate_kbps', 'bitRate', _number)])
    values.update(_mapped(stats, [('actual_bitrate_kbps', 'encodedBitrate', _number),
                                 ('level_percent', 'maxSampleValuePercentage', _number),
                                 ('encoder_errors', 'encoderErrors', _integer)]))
    values.update(state_code=code, status=_status(code, _AUDIO_STATES),
                  running=code in (3, 67) if code is not None else None,
                  muted=code == 67 if code is not None else None,
                  codec=_status(_integer(info.get('algorithm')), {1: 'PCM Pass-Through', 10: 'AAC ADTS', 21: 'AAC LOAS/LATM', 50: 'Opus'}),
                  mode=_status(_integer(info.get('mode')), {0: 'Stereo', 1: 'Mono Left', 2: 'Mono Right', 3: '3.0', 4: '4.0', 5: '4.1', 6: '5.1', 7: '7.0', 8: '7.1'}))
    return values


def _stream_values(row):
    info, stats = row['info'], row['stats']
    code = _integer(stats.get('state'))
    values = _mapped(stats, [('bitrate_kbps', 'bitrateInKbps', _number), ('uptime', 'uptime', _text),
                             ('unsent_packets', 'unsentPackets', _integer)])
    protocol = _integer(info.get('encapsulation'))
    values.update(name=_text(info.get('name')), state_code=code,
                  status=_text(stats.get('stateString')) or _status(code, _STREAM_STATES),
                  running=code == 1 if code is not None else None,
                  protocol=_status(protocol, {2: 'UDP', 3: 'RTP', 34: 'SRT', 128: 'WHIP'}),
                  srt_mode=_status(_integer(info.get('srtMode')), {0: 'Caller', 1: 'Listener', 2: 'Rendezvous'}) if protocol == 34 else None,
                  video_encoders=_source_names(info.get('video')),
                  audio_encoders=_source_names(info.get('audio')))
    srt = stats.get('srt') or {}
    values.update(_srt_values(srt, 'srt'))
    values.update(_srt_values(srt.get('path2', {}), 'srt_path2'))
    return values


def _source_names(rows):
    if not isinstance(rows, list):
        return None
    names = [_text(row.get('name', row.get('id'))) for row in rows if isinstance(row, dict)]
    return ', '.join(names) if len(names) == len(rows) and None not in names else None


def _srt_values(data, prefix):
    fields = [('latency_ms', 'latency', _number), ('lost_packets', 'lostPackets', _integer),
              ('dropped_packets', 'droppedPackets', _integer), ('resent_packets', 'resentPackets', _integer)]
    values = {prefix + '_' + key: value for key, value in _mapped(data, fields).items()}
    rtt = _number(data.get('rtt'))
    values[prefix + '_rtt_ms'] = rtt / 1000 if rtt is not None else None
    values[prefix + '_status'] = _status(_integer(data.get('state')), _STREAM_STATES)
    return values


def _recording_values(data):
    status = _text(data.get('status'))
    values = _mapped(data, [('name', 'name', _text), ('bytes_stored', 'bytesStored', _integer),
                            ('progress_percent', 'percentageDone', _number), ('auto_start', 'autoStart', _flag),
                            ('queue_overflows', 'queueOverflows', _integer), ('filename', 'filename', _text),
                            ('format', 'format', _text)])
    values.update(status=status.upper() if status else None,
                  running=status.upper() == 'RECORDING' if status else None,
                  duration=_text(data.get('upTimeClock', data.get('upTime'))))
    return values
