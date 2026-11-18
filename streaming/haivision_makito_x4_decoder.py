"""Makito X4 decoder REST API (1.7+), appliance and blade.

Cookie sessions and device-enumerated streams require Python. Configured routing,
active reception, decoding and fallback output are separate, device-reported
readings. Reconnect rebuilds all rosters. No physical unit has been tested.
Source: https://mx4d.demo.haivision.com/apidoc/
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from html import unescape
import json
import math
import re
from urllib.parse import quote

import httpx

from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError


class HaivisionMakitoX4DecoderDriver(BaseDriver):
    DRIVER_INFO = {'id': 'haivision_makito_x4_decoder',
 'name': 'Haivision Makito X4 Decoder',
 'manufacturer': 'Haivision',
 'category': 'streaming',
 'version': '1.0.0',
 'author': 'OpenAVC',
 'transport': 'http',
 'ports': [443],
 'protocols': ['https'],
 'source_url': 'https://mx4d.demo.haivision.com/apidoc/',
 'description': 'Routes named streams to Makito X4 decoder channels, with SDI assignment, signal, fallback '
                'image, audio and SRT feedback.',
 'simulated': True,
 'verified': False,
 'min_platform_version': '0.37.1',
 'tags': ['decoder', 'srt', 'streaming', 'hevc', 'h264', 'sdi'],
 'compatible_models': [{'manufacturer': 'Haivision',
                        'models': ['Makito X4 Decoder'],
                        'confidence': 'untested',
                        'notes': 'Appliance and blade packaging; REST API firmware 1.7+. SDI output '
                                 'assignment; ST 2110 output provisioning remains in the device web '
                                 'interface. Hardware verification pending.'}],
 'help': {'overview': 'Select device-configured streams by name and route them to decoder channels. Video '
                      'and audio travel together. Selected source, received source, decoding, and fallback '
                      'image are separate readings. Channels can feed multiple SDI outputs. Configure stream '
                      'addresses, encryption and ST 2110 outputs in the device web interface.',
          'setup': 'Use firmware 1.7 or later, HTTPS and an account with control rights. Refresh Status '
                   'discovers channels, streams and presets. Routing preserves whether a decoder is started '
                   'or stopped; use Start Decoder when needed. A stream already assigned to another channel '
                   'must first be unassigned there. Decoder 0 has highest resource priority. Presets may '
                   'change several channels at once. Verify the certificate when it is trusted by this '
                   'computer.'},
 'default_config': {'host': '',
                    'port': 443,
                    'ssl': True,
                    'verify_ssl': False,
                    'username': '',
                    'password': '',
                    'poll_interval': 3,
                    'detail_poll_every': 5},
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
                                         'advanced': True}},
 'state_variables': {'model': {'type': 'string', 'label': 'Model'},
                     'part_number': {'type': 'string', 'label': 'Part Number'},
                     'serial_number': {'type': 'string', 'label': 'Serial Number'},
                     'firmware_version': {'type': 'string', 'label': 'Firmware'},
                     'device_name': {'type': 'string', 'label': 'Device Name'},
                     'personality': {'type': 'string', 'label': 'Output Personality'},
                     'card_status': {'type': 'string', 'label': 'System Status', 'cloud_priority': 'high'},
                     'temperature_c': {'type': 'number',
                                       'label': 'Temperature',
                                       'unit': '°C',
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
                     'stream_count': {'type': 'integer', 'label': 'Configured Streams'},
                     'last_error': {'type': 'string', 'label': 'Last Error', 'cloud_priority': 'high'},
                     'decoder_count': {'type': 'integer', 'label': 'Decoder Channels'},
                     'active_decoders': {'type': 'integer', 'label': 'Active Decoders'},
                     'stream_options': {'type': 'string', 'label': 'Stream Choices'},
                     'cpu_percent': {'type': 'number',
                                     'label': 'CPU Usage',
                                     'unit': '%',
                                     'cloud_priority': 'low'},
                     'ram_percent': {'type': 'number',
                                     'label': 'Memory Usage',
                                     'unit': '%',
                                     'cloud_priority': 'low'},
                     'sdi_available': {'type': 'boolean', 'label': 'SDI Output Assignment Available'}},
 'child_entity_types': {'decoder': {'label': 'Decoder Channel',
                                    'label_plural': 'Decoder Channels',
                                    'id_format': {'type': 'integer', 'min': 0},
                                    'label_field': 'name',
                                    'summary_fields': ['selected_stream_name',
                                                       'status',
                                                       'video_active',
                                                       'fallback_active',
                                                       'sdi_outputs'],
                                    'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                        'selected_stream': {'type': 'integer',
                                                                            'label': 'Selected Stream',
                                                                            'control': True,
                                                                            'cloud_priority': 'high',
                                                                            'help': 'Configured stream ID; '
                                                                                    '-1 means unassigned. '
                                                                                    'This does not indicate '
                                                                                    'successful reception.'},
                                                        'selected_stream_name': {'type': 'string',
                                                                                 'label': 'Selected Stream '
                                                                                          'Name'},
                                                        'active_stream': {'type': 'integer',
                                                                          'label': 'Received Stream',
                                                                          'cloud_priority': 'high',
                                                                          'help': 'Reported stream ID while '
                                                                                  'the decoder reports '
                                                                                  'active reception; '
                                                                                  'otherwise unknown.'},
                                                        'status': {'type': 'string',
                                                                   'label': 'Decoder Status',
                                                                   'cloud_priority': 'high'},
                                                        'state_code': {'type': 'integer',
                                                                       'label': 'Decoder Status Code'},
                                                        'started': {'type': 'boolean',
                                                                    'label': 'Started',
                                                                    'control': True},
                                                        'receiving': {'type': 'boolean',
                                                                      'label': 'Receiving Stream',
                                                                      'cloud_priority': 'high'},
                                                        'video_active': {'type': 'boolean',
                                                                         'label': 'Decoding Video',
                                                                         'cloud_priority': 'high'},
                                                        'video_status': {'type': 'string',
                                                                         'label': 'Video Decoder Status'},
                                                        'trouble': {'type': 'string',
                                                                    'label': 'Decoder Trouble',
                                                                    'cloud_priority': 'high'},
                                                        'stream_status': {'type': 'string',
                                                                          'label': 'Incoming Stream Status'},
                                                        'input_resolution': {'type': 'string',
                                                                             'label': 'Input Resolution'},
                                                        'output_resolution': {'type': 'string',
                                                                              'label': 'Output Resolution'},
                                                        'output_frame_rate': {'type': 'string',
                                                                              'label': 'Actual Output Frame '
                                                                                       'Rate',
                                                                              'unit': 'fps'},
                                                        'codec': {'type': 'string', 'label': 'Video Codec'},
                                                        'audio_status': {'type': 'string',
                                                                         'label': 'Audio Status',
                                                                         'cloud_priority': 'high'},
                                                        'audio_pair_count': {'type': 'integer',
                                                                             'label': 'Audio Pairs'},
                                                        'fallback_active': {'type': 'boolean',
                                                                            'label': 'Showing Fallback Image',
                                                                            'cloud_priority': 'high'},
                                                        'fallback_status': {'type': 'string',
                                                                            'label': 'Fallback Image Status'},
                                                        'fallback_mode': {'type': 'integer',
                                                                          'label': 'Fallback Mode',
                                                                          'control': True},
                                                        'fallback_delay': {'type': 'integer',
                                                                           'label': 'Fallback Delay',
                                                                           'unit': 's',
                                                                           'control': True},
                                                        'buffering_mode': {'type': 'integer',
                                                                           'label': 'Buffering Mode',
                                                                           'control': True},
                                                        'buffering_delay': {'type': 'integer',
                                                                            'label': 'Fixed Buffer Delay',
                                                                            'unit': 'ms',
                                                                            'control': True},
                                                        'multisync_delay': {'type': 'integer',
                                                                            'label': 'MultiSync Delay',
                                                                            'unit': 'ms',
                                                                            'control': True},
                                                        'multisync_status': {'type': 'string',
                                                                             'label': 'MultiSync Status',
                                                                             'cloud_priority': 'high'},
                                                        'video_latency_ms': {'type': 'number',
                                                                             'label': 'Video Latency',
                                                                             'unit': 'ms',
                                                                             'cloud_priority': 'low'},
                                                        'frame_rate_setting': {'type': 'integer',
                                                                               'label': 'Output Frame Rate '
                                                                                        'Setting',
                                                                               'control': True},
                                                        'quad_mode': {'type': 'integer',
                                                                      'label': 'Quad Mode'},
                                                        'sdi_outputs': {'type': 'string',
                                                                        'label': 'SDI Outputs'},
                                                        'drop_corrupted_frames': {'type': 'boolean',
                                                                                  'label': 'Drop Corrupted '
                                                                                           'Frames',
                                                                                  'control': True},
                                                        'skipped_frames': {'type': 'string',
                                                                           'label': 'Skipped Output Frames',
                                                                           'cloud_priority': 'low'},
                                                        'replayed_frames': {'type': 'string',
                                                                            'label': 'Replayed Output Frames',
                                                                            'cloud_priority': 'low'},
                                                        'corrupted_frames': {'type': 'string',
                                                                             'label': 'Corrupted Frames',
                                                                             'cloud_priority': 'low'},
                                                        'clock_status': {'type': 'string',
                                                                         'label': 'Clock Recovery Status'},
                                                        'sdi_1': {'type': 'boolean',
                                                                  'label': 'SDI 1 Assigned',
                                                                  'control': True},
                                                        'sdi_2': {'type': 'boolean',
                                                                  'label': 'SDI 2 Assigned',
                                                                  'control': True},
                                                        'sdi_3': {'type': 'boolean',
                                                                  'label': 'SDI 3 Assigned',
                                                                  'control': True},
                                                        'sdi_4': {'type': 'boolean',
                                                                  'label': 'SDI 4 Assigned',
                                                                  'control': True}}},
                        'stream': {'label': 'Stream',
                                   'label_plural': 'Streams',
                                   'id_format': {'type': 'integer', 'min': 0},
                                   'label_field': 'name',
                                   'summary_fields': ['protocol', 'status', 'bitrate_kbps'],
                                   'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                       'protocol': {'type': 'string', 'label': 'Protocol'},
                                                       'decoder_id': {'type': 'integer',
                                                                      'label': 'Assigned Decoder'},
                                                       'status': {'type': 'string',
                                                                  'label': 'Stream Status',
                                                                  'cloud_priority': 'high'},
                                                       'state_code': {'type': 'integer',
                                                                      'label': 'Status Code'},
                                                       'receiving': {'type': 'boolean',
                                                                     'label': 'Receiving',
                                                                     'cloud_priority': 'high'},
                                                       'bitrate_kbps': {'type': 'number',
                                                                        'label': 'Received Bitrate',
                                                                        'unit': 'kbps',
                                                                        'cloud_priority': 'low'},
                                                       'received_errors': {'type': 'string',
                                                                           'label': 'Receive Errors',
                                                                           'cloud_priority': 'low'},
                                                       'dropped_bytes': {'type': 'integer',
                                                                         'label': 'Dropped Bytes',
                                                                         'cloud_priority': 'low'},
                                                       'last_received': {'type': 'string',
                                                                         'label': 'Last Packet Received',
                                                                         'cloud_priority': 'low'},
                                                       'summary': {'type': 'string',
                                                                   'label': 'Stream Contents'},
                                                       'srt_mode': {'type': 'string', 'label': 'SRT Mode'},
                                                       'srt_redundancy': {'type': 'string',
                                                                          'label': 'SRT Redundancy'},
                                                       'srt_status': {'type': 'string',
                                                                      'label': 'SRT Status',
                                                                      'cloud_priority': 'high'},
                                                       'srt_rtt': {'type': 'string',
                                                                   'label': 'SRT Round-Trip Time',
                                                                   'cloud_priority': 'low'},
                                                       'srt_latency_ms': {'type': 'number',
                                                                          'label': 'SRT Latency',
                                                                          'cloud_priority': 'low',
                                                                          'unit': 'ms'},
                                                       'srt_buffer_ms': {'type': 'number',
                                                                         'label': 'SRT Buffer',
                                                                         'cloud_priority': 'low',
                                                                         'unit': 'ms'},
                                                       'srt_lost_packets': {'type': 'integer',
                                                                            'label': 'SRT Lost Packets',
                                                                            'cloud_priority': 'low'},
                                                       'srt_dropped_packets': {'type': 'integer',
                                                                               'label': 'SRT Dropped Packets',
                                                                               'cloud_priority': 'low'},
                                                       'srt_recovered_packets': {'type': 'integer',
                                                                                 'label': 'SRT Recovered '
                                                                                          'Packets',
                                                                                 'cloud_priority': 'low'},
                                                       'srt_decrypt_status': {'type': 'string',
                                                                              'label': 'SRT Decryption '
                                                                                       'Status',
                                                                              'cloud_priority': 'low'},
                                                       'srt_path2_status': {'type': 'string',
                                                                            'label': 'SRT Second Path Status',
                                                                            'cloud_priority': 'high'},
                                                       'srt_path2_rtt': {'type': 'string',
                                                                         'label': 'SRT Second Path '
                                                                                  'Round-Trip Time',
                                                                         'cloud_priority': 'low'},
                                                       'srt_path2_latency_ms': {'type': 'number',
                                                                                'label': 'SRT Second Path '
                                                                                         'Latency',
                                                                                'cloud_priority': 'low',
                                                                                'unit': 'ms'},
                                                       'srt_path2_buffer_ms': {'type': 'number',
                                                                               'label': 'SRT Second Path '
                                                                                        'Buffer',
                                                                               'cloud_priority': 'low',
                                                                               'unit': 'ms'},
                                                       'srt_path2_lost_packets': {'type': 'integer',
                                                                                  'label': 'SRT Second Path '
                                                                                           'Lost Packets',
                                                                                  'cloud_priority': 'low'},
                                                       'srt_path2_dropped_packets': {'type': 'integer',
                                                                                     'label': 'SRT Second '
                                                                                              'Path Dropped '
                                                                                              'Packets',
                                                                                     'cloud_priority': 'low'},
                                                       'srt_path2_recovered_packets': {'type': 'integer',
                                                                                       'label': 'SRT Second '
                                                                                                'Path '
                                                                                                'Recovered '
                                                                                                'Packets',
                                                                                       'cloud_priority': 'low'},
                                                       'srt_path2_decrypt_status': {'type': 'string',
                                                                                    'label': 'SRT Second '
                                                                                             'Path '
                                                                                             'Decryption '
                                                                                             'Status',
                                                                                    'cloud_priority': 'low'}}},
                        'audio_pair': {'label': 'Audio Pair',
                                       'label_plural': 'Audio Pairs',
                                       'id_format': {'type': 'string'},
                                       'label_field': 'name',
                                       'summary_fields': ['layout', 'left_db', 'right_db'],
                                       'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                           'decoder_id': {'type': 'integer',
                                                                          'label': 'Decoder'},
                                                           'status': {'type': 'string',
                                                                      'label': 'Audio Status'},
                                                           'codec': {'type': 'string', 'label': 'Codec'},
                                                           'layout': {'type': 'string',
                                                                      'label': 'Output Layout'},
                                                           'language': {'type': 'string',
                                                                        'label': 'Language'},
                                                           'left_db': {'type': 'number',
                                                                       'label': 'Left Level',
                                                                       'unit': 'dBFS',
                                                                       'cloud_priority': 'low'},
                                                           'right_db': {'type': 'number',
                                                                        'label': 'Right Level',
                                                                        'unit': 'dBFS',
                                                                        'cloud_priority': 'low'},
                                                           'av_sync_ms': {'type': 'number',
                                                                          'label': 'A/V Sync Difference',
                                                                          'unit': 'ms',
                                                                          'cloud_priority': 'low'},
                                                           'decode_errors': {'type': 'string',
                                                                             'label': 'Decode Errors',
                                                                             'cloud_priority': 'low'}}}},
 'commands': {'refresh_status': {'label': 'Refresh Status',
                                 'help': 'Read the current streams, decoder channels, presets and signal state '
                                         'from the decoder.',
                                 'params': {}},
              'load_preset': {'label': 'Recall Preset',
                              'help': 'Apply a saved decoder configuration. This can change or interrupt '
                                      'active streams.',
                              'params': {'preset': {'type': 'string',
                                                    'label': 'Preset',
                                                    'required': True,
                                                    'options_state': 'preset_options',
                                                    'trim': False}},
                              'sets': {'active_preset': '{preset}'}},
              'save_preset': {'label': 'Save to Preset',
                              'help': 'Replace the selected saved preset with the current decoder '
                                      'configuration.',
                              'params': {'preset': {'type': 'string',
                                                    'label': 'Preset',
                                                    'required': True,
                                                    'options_state': 'preset_options',
                                                    'trim': False}},
                              'confirm': 'Replace this saved preset with the current decoder configuration?'},
              'set_startup_preset': {'label': 'Set Startup Preset',
                                     'help': 'Use the selected saved preset the next time the decoder '
                                             'starts.',
                                     'params': {'preset': {'type': 'string',
                                                           'label': 'Preset',
                                                           'required': True,
                                                           'options_state': 'preset_options',
                                                           'trim': False}},
                                     'sets': {'startup_preset': '{preset}'}},
              'identify': {'label': 'Identify Decoder',
                           'help': 'Blink the decoder status LED for the selected number of minutes.',
                           'params': {'minutes': {'type': 'integer',
                                                  'label': 'Minutes',
                                                  'min': 1,
                                                  'max': 60,
                                                  'default': 1}}},
              'stop_identifying': {'label': 'Stop Identifying',
                                   'help': 'Stop the decoder status LED identification blink.',
                                   'params': {}},
              'route_stream': {'label': 'Select Stream',
                               'params': {'decoder': {'type': 'child_id',
                                                      'child_type': 'decoder',
                                                      'label': 'Decoder Channel',
                                                      'required': True},
                                          'stream': {'type': 'integer',
                                                     'label': 'Stream',
                                                     'required': True,
                                                     'options_state': 'stream_options'}},
                               'sets': {'selected_stream': '{stream}'},
                               'help': 'Assign an existing stream without starting a stopped decoder. Video '
                                       'and audio route together.'},
              'unassign_stream': {'label': 'Unassign Stream',
                                  'params': {'decoder': {'type': 'child_id',
                                                         'child_type': 'decoder',
                                                         'label': 'Decoder Channel',
                                                         'required': True}},
                                  'sets': {'selected_stream': -1},
                                  'help': 'Removes this channel’s stream assignment. It does not delete the '
                                          'stream.'},
              'start_decoder': {'label': 'Start Decoder',
                                'params': {'decoder': {'type': 'child_id',
                                                       'child_type': 'decoder',
                                                       'label': 'Decoder Channel',
                                                       'required': True}},
                                'help': 'Starts the channel. It may wait for a stream; receiving and video '
                                        'feedback report the actual result.'},
              'stop_decoder': {'label': 'Stop Decoder',
                               'params': {'decoder': {'type': 'child_id',
                                                      'child_type': 'decoder',
                                                      'label': 'Decoder Channel',
                                                      'required': True}},
                               'sets': {'started': False},
                               'help': 'Stops decoding this channel. Readback reports the actual output '
                                       'state.'},
              'set_decoder_name': {'label': 'Name Decoder',
                                   'params': {'decoder': {'type': 'child_id',
                                                          'child_type': 'decoder',
                                                          'label': 'Decoder Channel',
                                                          'required': True},
                                              'name': {'type': 'string',
                                                       'label': 'Name',
                                                       'required': True,
                                                       'pattern': '.{1,128}'}},
                                   'sets': {'name': '{name}'}},
              'set_fallback': {'label': 'Set Fallback Image',
                               'params': {'decoder': {'type': 'child_id',
                                                      'child_type': 'decoder',
                                                      'label': 'Decoder Channel',
                                                      'required': True},
                                          'mode': {'type': 'enum',
                                                   'label': 'Image',
                                                   'required': True,
                                                   'values': [{'value': '1', 'label': 'Freeze'},
                                                              {'value': '2', 'label': 'Black'},
                                                              {'value': '3', 'label': 'Blue'},
                                                              {'value': '4', 'label': 'Color Bars'},
                                                              {'value': '5', 'label': 'Mute Output'}]},
                                          'delay': {'type': 'integer',
                                                    'label': 'Delay',
                                                    'required': True,
                                                    'min': 1,
                                                    'max': 1000,
                                                    'unit': 's'}},
                               'sets': {'fallback_mode': '{mode}', 'fallback_delay': '{delay}'},
                               'help': 'Chooses what appears after incoming video is lost. This is not an '
                                       'immediate freeze or mute command.'},
              'set_automatic_buffering': {'label': 'Automatic Buffering',
                                          'params': {'decoder': {'type': 'child_id',
                                                                 'child_type': 'decoder',
                                                                 'label': 'Decoder Channel',
                                                                 'required': True}},
                                          'sets': {'buffering_mode': 3}},
              'set_fixed_buffering': {'label': 'Fixed Buffering',
                                      'params': {'decoder': {'type': 'child_id',
                                                             'child_type': 'decoder',
                                                             'label': 'Decoder Channel',
                                                             'required': True},
                                                 'delay': {'type': 'integer',
                                                           'label': 'Delay',
                                                           'required': True,
                                                           'min': 0,
                                                           'max': 3000,
                                                           'unit': 'ms'}},
                                      'sets': {'buffering_mode': 2, 'buffering_delay': '{delay}'},
                                      'help': 'Must fit the delay range currently reported by the decoder.'},
              'set_multisync_buffering': {'label': 'MultiSync Buffering',
                                          'params': {'decoder': {'type': 'child_id',
                                                                 'child_type': 'decoder',
                                                                 'label': 'Decoder Channel',
                                                                 'required': True},
                                                     'delay': {'type': 'integer',
                                                               'label': 'Delay',
                                                               'required': True,
                                                               'min': 0,
                                                               'max': 10000,
                                                               'unit': 'ms'}},
                                          'sets': {'buffering_mode': 4, 'multisync_delay': '{delay}'},
                                          'help': 'Requires synchronized encoder timecodes and NTP. Check '
                                                  'MultiSync Status after changing the delay.'},
              'set_output_frame_rate': {'label': 'Set Output Frame Rate',
                                        'params': {'decoder': {'type': 'child_id',
                                                               'child_type': 'decoder',
                                                               'label': 'Decoder Channel',
                                                               'required': True},
                                                   'rate': {'type': 'enum',
                                                            'label': 'Frame Rate',
                                                            'required': True,
                                                            'values': [{'value': '0', 'label': 'Automatic'},
                                                                       {'value': '60', 'label': '60 fps'},
                                                                       {'value': '59', 'label': '59.94 fps'},
                                                                       {'value': '50', 'label': '50 fps'},
                                                                       {'value': '30', 'label': '30 fps'},
                                                                       {'value': '29', 'label': '29.97 fps'},
                                                                       {'value': '25', 'label': '25 fps'},
                                                                       {'value': '24', 'label': '24 fps'},
                                                                       {'value': '23',
                                                                        'label': '23.98 fps'}]}},
                                        'sets': {'frame_rate_setting': '{rate}'}},
              'set_drop_corrupted_frames': {'label': 'Drop Corrupted Frames',
                                            'params': {'decoder': {'type': 'child_id',
                                                                   'child_type': 'decoder',
                                                                   'label': 'Decoder Channel',
                                                                   'required': True},
                                                       'enabled': {'type': 'boolean',
                                                                   'label': 'Enabled',
                                                                   'required': True}},
                                            'sets': {'drop_corrupted_frames': '{enabled}'}},
              'set_sdi_outputs': {'label': 'Assign SDI Outputs',
                                  'params': {'decoder': {'type': 'child_id',
                                                         'child_type': 'decoder',
                                                         'label': 'Decoder Channel',
                                                         'required': True},
                                             'sdi_1': {'type': 'boolean', 'label': 'SDI 1', 'required': True},
                                             'sdi_2': {'type': 'boolean', 'label': 'SDI 2', 'required': True},
                                             'sdi_3': {'type': 'boolean', 'label': 'SDI 3', 'required': True},
                                             'sdi_4': {'type': 'boolean',
                                                       'label': 'SDI 4',
                                                       'required': True}},
                                  'sets': {'sdi_1': '{sdi_1}',
                                           'sdi_2': '{sdi_2}',
                                           'sdi_3': '{sdi_3}',
                                           'sdi_4': '{sdi_4}'},
                                  'help': 'Select every SDI output for this channel. Outputs assigned '
                                          'elsewhere must first be released there. Quad mode requires all '
                                          'four outputs.'}},
 'quick_actions': ['refresh_status',
                   'route_stream',
                   'start_decoder',
                   'stop_decoder',
                   'load_preset',
                   'identify'],
 'device_settings': {'preset_autosave': {'type': 'boolean',
                                         'label': 'Preset Auto-Save',
                                         'state_key': 'preset_autosave',
                                         'default': True,
                                         'setup': False,
                                         'help': 'Automatically save configuration changes on the decoder.'}},
 'routing': {'destination_child_type': 'decoder',
             'source_child_type': 'stream',
             'command': 'route_stream',
             'destination_param': 'decoder',
             'source_param': 'stream',
             'planes': [{'label': 'Video and Audio', 'route_property': 'selected_stream'}]}}

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
            self.set_state(key, '[]' if key in ('preset_options', 'stream_options') else None)
        for kind, definition in self.DRIVER_INFO['child_entity_types'].items():
            for ident in self.list_children(kind):
                values = {key: None for key in definition['state_variables'] if key != 'name'}
                values['online'] = False
                self.set_child_state_batch(kind, ident, values)

    async def _create_transport(self, transport_type):
        if not self.config.get('username') or not self.config.get('password'):
            raise ConnectionFaultError('The decoder requires a username and password.', code='auth_failed')
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
        if 'Makito X4' not in model or 'Decoder' not in model:
            raise ConnectionFaultError('This device did not identify as a Makito X4 decoder.', code='invalid_config')
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
            raise ConnectionError('The decoder session is closed.')
        try:
            return await self._client.request(method, path, json=body)
        except httpx.TransportError as exc:
            raise ConnectionError('The decoder stopped answering HTTPS requests.') from exc

    def _reject_login(self, message):
        self._auth_rejected = True
        raise ConnectionFaultError(message, code='auth_failed')

    async def _login(self):
        if self._auth_rejected:
            self._reject_login('The decoder refused the account login.')
        self._client.cookies.clear()
        response = await self._raw('POST', '/apis/authentication', {
            'username': self.config['username'], 'password': self.config['password'],
        })
        if response.status_code in (401, 403):
            self._reject_login('The decoder refused the account login.')
        _check_status(response, 'sign in')
        for cookie in self._client.cookies.jar:
            self.redact_in_log(cookie.value)
        response = await self._raw('GET', '/apis/authentication')
        if response.status_code in (401, 403):
            self._reject_login('The decoder refused the new session.')
        account = _object_response(response, 'read the account session')
        if not account.get('username'):
            raise CommandRefusedError('The decoder returned an incomplete account session.')
        if _flag(account.get('passwordExp')) is True:
            self._reject_login('The decoder account password has expired.')

    async def _call(self, method, path, body=None, *, core=False):
        if self._auth_rejected:
            self._reject_login('The decoder refused the account login.')
        response = await self._raw(method, path, body)
        if response.status_code == 401:
            # Every caller holds the operation lock, so expiry causes one renewal.
            await self._login()
            response = await self._raw(method, path, body)
            if response.status_code == 401:
                self._reject_login('The decoder refused the renewed session.')
        if response.status_code == 403 and core:
            self._reject_login('The decoder account cannot read control status.')
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

    async def _optional(self, path):
        response = await self._call('GET', path)
        if response.status_code in (403, 404, 501):
            self._optional_errors[path] = f'{path}: decoder answered HTTP {response.status_code}.'
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

    async def _control_preset(self, command, params):
        await self._read_presets()
        name = str(params.get('preset', ''))
        if name not in self._presets:
            raise CommandRefusedError('The selected preset is no longer on the decoder. Refresh Status.')
        suffix = _PRESET_COMMANDS[command]
        path = '/apis/presets/' + quote(name, safe='') + suffix
        body = None
        if command == 'save_preset':
            startup = _flag(self._presets[name].get('startup'))
            if startup is None:
                raise CommandRefusedError('The decoder did not report this preset’s startup setting.')
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
        _check_status(await self._call(method, '/apis/leds', body), 'identify the decoder')

    async def _read_system_info(self):
        info = await self._get('/apis/system_info', core=True)
        if _integer(info.get('videoDecoders')) is None:
            raise CommandRefusedError('The decoder returned incomplete hardware capabilities.')
        self._system_info = info
        self.set_state('device_name', _text(info.get('deviceName')))
        self.set_state('personality', _text(info.get('personality')))
        self.set_state('decoder_count', _integer(info.get('videoDecoders')))
        sdi = str(info.get('personality', '')).lower() == 'sdi'
        self.set_state('sdi_available', sdi)
        if sdi:
            self.DRIVER_INFO['commands']['set_sdi_outputs'] = deepcopy(type(self).DRIVER_INFO['commands']['set_sdi_outputs'])
        else:
            self.DRIVER_INFO['commands'].pop('set_sdi_outputs', None)

    def _apply_status(self, data):
        fields = [('model', 'cardType', _text), ('part_number', 'partNumber', _text),
                  ('serial_number', 'serialNumber', _text), ('firmware_version', 'firmwareVersion', _text),
                  ('card_status', 'cardStatus', _text), ('temperature_c', 'temperature', _number),
                  ('cpu_percent', 'cpu', _number), ('ram_percent', 'ram', _number), ('uptime', 'uptime', _text)]
        for key, value in _mapped(data, fields).items():
            self.set_state(key, value)

    async def _refresh(self, *, full):
        try:
            await self._read_snapshot(full)
        except Exception:
            # Keep known objects for project bindings, but do not advertise old
            # routes or signal readings as current when the snapshot failed.
            self._invalidate_readings()
            raise

    async def _read_snapshot(self, full):
        self._apply_status(await self._get('/apis/status', core=True))
        if full:
            await self._read_system_info()
        streams = _rows(await self._get('/apis/streams'), nested=True)
        decoders = _rows(await self._get('/apis/decoders'), nested=True)
        self._rosters = {'stream': streams, 'decoder': decoders}
        self._replace_children('stream', {i: _stream_values(row) for i, row in streams.items()})
        values = {i: _decoder_values(row, streams, self._system_info) for i, row in decoders.items()}
        self._replace_children('decoder', values)
        self._replace_children('audio_pair', _audio_values(decoders))
        self.set_state('stream_options', json.dumps([{'value': i, 'label': _text(r['info'].get('name')) or f'Stream {i}'} for i, r in streams.items()]))
        self.set_state('stream_count', len(streams))
        self.set_state('active_decoders', _active_count(decoders))
        if full:
            await self._read_presets()
        self.set_state('last_error', '; '.join(self._optional_errors.values()))

    async def send_command(self, command, params=None):
        params = params or {}
        async with self._operation_lock:
            if command not in self.DRIVER_INFO['commands']:
                raise CommandRefusedError('This command is not available on this decoder.')
            if command in _PRESET_COMMANDS:
                await self._control_preset(command, params)
            elif command == 'refresh_status':
                await self._refresh(full=True)
            elif command in ('identify', 'stop_identifying'):
                await self._identify(command, params)
            else:
                await self._control_decoder(command, params)

    async def _control_decoder(self, command, params):
        # Fresh assignments prevent an obsolete picker from silently moving an
        # in-use stream or an SDI output belonging to another channel.
        await self._refresh(full=False)
        ident = _target_id(params.get('decoder'))
        if ident not in self._rosters['decoder']:
            raise CommandRefusedError('This channel is no longer reported by the decoder.')
        body = self._decoder_body(command, ident, params)
        suffix = {'start_decoder': '/start', 'stop_decoder': '/stop'}.get(command, '')
        response = await self._call('PUT', f'/apis/decoders/{ident}{suffix}', body)
        _check_status(response, command.replace('_', ' '))
        # Do not use the PUT examples as state: the vendor's stop example
        # repeats an active response. Read the complete current lists instead.
        await self._refresh(full=True)

    def _decoder_body(self, command, ident, params):
        if command == 'route_stream':
            return self._route_body(ident, params)
        if command == 'set_sdi_outputs':
            return self._outputs_body(ident, params)
        if command in ('set_fixed_buffering', 'set_multisync_buffering'):
            return self._buffer_body(command, ident, params)
        builders = {
            'unassign_stream': lambda: {'streamId': -1},
            'set_decoder_name': lambda: {'name': _decoder_name(params['name'])},
            'set_fallback': lambda: {'stillImage': _choice(params['mode'], (1, 2, 3, 4, 5)), 'stillImageDelay': _bounded(params['delay'], 1, 1000)},
            'set_automatic_buffering': lambda: {'bufferingMode': 3},
            'set_output_frame_rate': lambda: {'outputFrameRate': _choice(params['rate'], (0, 60, 59, 50, 30, 29, 25, 24, 23))},
            'set_drop_corrupted_frames': lambda: {'dropCorruptedFrames': _required_flag(params['enabled'])},
        }
        return builders[command]() if command in builders else None

    def _route_body(self, ident, params):
        stream = _target_id(params.get('stream'))
        if stream not in self._rosters['stream']:
            raise CommandRefusedError('This stream is no longer configured on the decoder. Refresh Status.')
        for other, row in self._rosters['decoder'].items():
            if other != ident and _integer(row['info'].get('streamId')) == stream:
                raise CommandRefusedError(f'This stream is assigned to decoder {other}. Unassign it there first.')
        return {'streamId': stream}

    def _outputs_body(self, ident, params):
        outputs = [_required_flag(params[f'sdi_{i}']) for i in range(1, 5)]
        if str(self._system_info.get('personality', '')).lower() != 'sdi':
            raise CommandRefusedError('SDI assignment is only available in SDI personality.')
        current = self._rosters['decoder'][ident]['info']
        _require_quad_outputs(current, outputs)
        for other, row in self._rosters['decoder'].items():
            assigned = _outputs(row['info'])
            if _output_conflict(other, ident, outputs, assigned):
                raise CommandRefusedError(f'An SDI output is assigned to decoder {other}. Release it there first.')
        return {'outputs': outputs}

    def _buffer_body(self, command, ident, params):
        multisync = command == 'set_multisync_buffering'
        delay = _bounded(params['delay'], 0, 10000 if multisync else 3000)
        stats = self._rosters['decoder'][ident]['stats']
        prefix = 'multisyncDelayRange' if multisync else 'delayRange'
        low, high = _integer(stats.get(prefix + 'MinMs')), _integer(stats.get(prefix + 'MaxMs'))
        # Zero/zero is the documented idle example, not a useful live range.
        if _outside_live_range(delay, low, high):
            raise CommandRefusedError(f'The decoder currently permits a delay from {low} to {high} ms.')
        return {'bufferingMode': 4 if multisync else 2,
                'multisyncBufferingDelay' if multisync else 'bufferingDelay': delay}

    async def set_device_setting(self, key, value):
        async with self._operation_lock:
            if key != 'preset_autosave':
                raise CommandRefusedError('This decoder setting is not available.')
            desired = _required_flag(value)
            _check_status(await self._call('PUT', '/apis/presets', {'autosave': desired}), 'set preset auto-save')
            await self._read_presets()


_PRESET_COMMANDS = {'load_preset': '/load', 'save_preset': '', 'set_startup_preset': '/startup'}
_DECODER_STATES = {0: 'Stopped', 1: 'Waiting for Stream', 2: 'Active', -1: 'Not Decoding'}
_STREAM_STATES = {0: 'Unknown', 1: 'Stopped', 2: 'Listening', 3: 'Active', 4: 'Resolving', 5: 'Connecting',
                  6: 'Scrambled', 7: 'Securing', -1: 'Invalid', -2: 'Failed', -3: 'Unlicensed'}
_TROUBLE = {0: 'None', -1: 'Unsupported Stream', -2: 'Unlicensed', -3: 'Oversubscribed', -4: 'Insufficient Memory'}


def _outputs(info):
    values = info.get('outputs')
    if isinstance(values, list) and len(values) == 4:
        return [_flag(value) for value in values]
    # Older response examples report output1..output4 rather than outputs[].
    return [_flag(info.get(f'output{i}')) for i in range(1, 5)]


def _decoder_values(row, streams, system):
    info, stats = row['info'], row['stats']
    values = _mapped(info, [('name', 'name', _text), ('selected_stream', 'streamId', _integer),
                            ('fallback_mode', 'stillImage', _integer), ('fallback_delay', 'stillImageDelay', _integer),
                            ('buffering_mode', 'bufferingMode', _integer), ('buffering_delay', 'bufferingDelay', _integer),
                            ('multisync_delay', 'multisyncBufferingDelay', _integer), ('quad_mode', 'quadMode', _integer),
                            ('frame_rate_setting', 'outputFrameRate', _integer), ('drop_corrupted_frames', 'dropCorruptedFrames', _flag)])
    values.update(_decoder_stats(stats))
    selected = streams.get(values['selected_stream'], {}).get('info', {})
    values['selected_stream_name'] = _text(selected.get('name'))
    outputs = _outputs(info) if str(system.get('personality', '')).lower() == 'sdi' else [None] * 4
    values.update({f'sdi_{i}': flag for i, flag in enumerate(outputs, 1)})
    values['sdi_outputs'] = ', '.join(str(i) for i, flag in enumerate(outputs, 1) if flag) if None not in outputs else None
    return values


def _decoder_stats(stats):
    fields = [('input_resolution', 'videoInputResolution', _text), ('output_resolution', 'videoDisplayResolution', _text),
              ('output_frame_rate', 'videoFrameRate', _text), ('video_status', 'videoDecoderState', _text),
              ('codec', 'videoAlgorithm', _text), ('audio_status', 'audioState', _text), ('audio_pair_count', 'audioPairsAmount', _integer),
              ('fallback_status', 'stillImage', _text), ('video_latency_ms', 'videoLatency', _milliseconds),
              ('multisync_status', 'multisyncStatus', _text), ('skipped_frames', 'skippedOutputFrames', _text),
              ('replayed_frames', 'replayedOutputFrames', _text), ('corrupted_frames', 'corruptedFrames', _text),
              ('clock_status', 'clockStatus', _text), ('started', 'decoderHasBeenStarted', _flag)]
    values = _mapped(stats, fields)
    code, stream = _integer(stats.get('state')), _integer(stats.get('streamId'))
    values.update(state_code=code, status=_status(code, _DECODER_STATES),
                  receiving=code == 2 if code in _DECODER_STATES else None,
                  active_stream=stream if code == 2 and stream is not None and stream >= 0 else None,
                  trouble=_status(_integer(stats.get('troubleCode')), _TROUBLE),
                  stream_status=_status(_integer(stats.get('streamState')), _STREAM_STATES),
                  video_active=_active_text(stats.get('videoDecoderState')),
                  fallback_active=_fallback_active(stats.get('stillImage')))
    return values


def _active_text(value):
    if not isinstance(value, str) or not value:
        return None
    if value.upper() == 'UNKNOWN':
        return None
    return value.upper() == 'ACTIVE'


def _fallback_active(value):
    if not isinstance(value, str):
        return None
    match = re.search(r'\((ACTIVE|INACTIVE)\)', value)
    return match.group(1) == 'ACTIVE' if match else None


def _milliseconds(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*ms\s*', value)
    return _number(match.group(1)) if match else None


def _stream_values(row):
    info, stats = row['info'], row['stats']
    values = _mapped(info, [('name', 'name', _text), ('decoder_id', 'decoderId', _integer)])
    values.update(_mapped(stats, [('bitrate_kbps', 'bitrateInKbps', _number), ('received_errors', 'receivedErrors', _text),
                                 ('dropped_bytes', 'droppedBytes', _integer), ('last_received', 'lastReceived', _text),
                                 ('summary', 'streamSummary', _text)]))
    code = _integer(stats.get('state'))
    values.update(state_code=code, status=_status(code, _STREAM_STATES), receiving=code == 3 if code in _STREAM_STATES and code != 0 else None,
                  protocol=_status(_integer(info.get('encapsulation')), {2: 'UDP', 3: 'RTP', 34: 'SRT'}),
                  srt_mode=_status(_integer(info.get('srtMode')), {0: 'Caller', 1: 'Listener', 2: 'Rendezvous'}))
    srt = stats.get('srt')
    if isinstance(srt, dict):
        values['srt_redundancy'] = _text(srt.get('detectedRedundancyMode'))
        values.update(_srt_values('srt', srt))
        if isinstance(srt.get('path2'), dict):
            values.update(_srt_values('srt_path2', srt['path2']))
    return values


def _srt_values(prefix, data):
    # Decoder RTT is formatted text (e.g. '< 1 ms'), unlike encoder microseconds.
    fields = [('status', 'state', _text), ('rtt', 'rtt', _text), ('latency_ms', 'latency', _number),
              ('buffer_ms', 'buffer', _number), ('lost_packets', 'lostPackets', _integer),
              ('dropped_packets', 'droppedPackets', _integer), ('recovered_packets', 'recoveredPackets', _integer),
              ('decrypt_status', 'decryptState', _text)]
    return {prefix + '_' + key: val for key, val in _mapped(data, fields).items()}


def _audio_values(decoders):
    result = {}
    for ident, row in decoders.items():
        result.update(_decoder_audio_pairs(ident, row))
    return result


def _decoder_audio_pairs(ident, row):
    pairs = row['stats'].get('audioPairs')
    if not isinstance(pairs, list):
        return {}
    return {f'{ident}_{index}': _audio_pair(ident, index, row, pair)
            for index, pair in enumerate(pairs, 1) if isinstance(pair, dict)}


def _active_count(decoders):
    codes = [_integer(row['stats'].get('state')) for row in decoders.values()]
    if not all(code in _DECODER_STATES for code in codes):
        return None
    return sum(code == 2 for code in codes)


def _output_conflict(other, ident, desired, assigned):
    if other == ident:
        return False
    if None in assigned and any(desired):
        raise CommandRefusedError(f'The decoder did not report all SDI assignments for channel {other}.')
    return any(a and b for a, b in zip(desired, assigned))


def _outside_live_range(delay, low, high):
    if low is None or high is None:
        return False
    if high <= 0:
        return False
    return not low <= delay <= high


def _audio_pair(ident, index, row, pair):
    active = _active_text(row['stats'].get('audioState'))
    fields = [('codec', 'compression', _text), ('layout', 'outputLayoutDetailed', _text), ('language', 'language', _text),
              ('av_sync_ms', 'avSyncMs', _number), ('decode_errors', 'decodeErrors', _text)]
    values = _mapped(pair, fields)
    values.update(name=f'{_text(row["info"].get("name")) or f"Decoder {ident}"} / Audio Pair {index}', decoder_id=ident,
                  status=_text(row['stats'].get('audioState')),
                  left_db=_number(pair.get('dbLeft')) if active else None,
                  right_db=_number(pair.get('dbRight')) if active else None)
    return values


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
        raise CommandRefusedError('Choose an object reported by the decoder.')
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
        raise CommandRefusedError(f'The decoder refused to {operation} (HTTP {response.status_code}).')

def _object_response(response, operation):
    _check_status(response, operation)
    try:
        data = response.json()
    except ValueError as exc:
        raise CommandRefusedError('The decoder returned a reply that is not JSON.') from exc
    if not isinstance(data, dict):
        raise CommandRefusedError('The decoder returned an incomplete status reply.')
    return data

def _validate_object(row):
    if not isinstance(row, dict):
        raise CommandRefusedError('The decoder returned an invalid object.')
    if not all(isinstance(row.get(key), dict) for key in ('info', 'stats')):
        raise CommandRefusedError('The decoder returned an incomplete object list. Existing objects were kept.')

def _rows(data, *, nested):
    if not isinstance(data.get('data'), list):
        raise CommandRefusedError('The decoder did not return its object list. Existing objects were kept.')
    rows = {}
    for row in data['data']:
        info = _row_identity(row, nested)
        ident = _target_id(info.get('id'))
        if ident in rows:
            raise CommandRefusedError('The decoder returned duplicate object IDs. Existing objects were kept.')
        rows[ident] = row
    return rows

def _row_identity(row, nested):
    if nested:
        _validate_object(row)
        return row['info']
    if not isinstance(row, dict):
        raise CommandRefusedError('The decoder returned an invalid object.')
    return row

def _preset_rows(data):
    if not isinstance(data.get('data'), list):
        raise CommandRefusedError('The decoder did not return its preset list.')
    result = {}
    for item in data['data']:
        if not _named_preset(item):
            raise CommandRefusedError('The decoder returned an incomplete preset list.')
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
        raise ConnectionFaultError('The decoder address or HTTPS port is invalid.', code='invalid_config') from exc

def _valid_address(host, port):
    if not host or not 1 <= port <= 65535:
        return False
    return not any(c in host for c in '/?#@')

def _mapped(data, fields):
    return {key: convert(data.get(raw)) for key, raw, convert in fields}

def _status(code, mapping):
    return mapping.get(code, f'State {code}') if code is not None else None


def _require_quad_outputs(info, outputs):
    if _integer(info.get('quadMode')) in (0, None):
        return
    if not all(outputs):
        raise CommandRefusedError('Quad mode requires all four SDI outputs.')


def _choice(value, choices):
    result = _integer(value)
    if result not in choices:
        raise CommandRefusedError('Choose one of the offered settings.')
    return result


def _bounded(value, low, high):
    result = _integer(value)
    if result is None or not low <= result <= high:
        raise CommandRefusedError(f'The setting must be from {low} to {high}.')
    return result


def _decoder_name(value):
    result = str(value).strip()
    if not result or len(result) > 128:
        raise CommandRefusedError('Use a decoder name from 1 to 128 characters.')
    return result
