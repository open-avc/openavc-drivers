"""Magewell Pro Convert IP decoder API v1.3, firmware 1.3+.

Python handles device-enumerated sources/profiles, variable window feedback,
read/modify/write audio mapping, and the serial-number-specific session cookie.
No control-state push is documented; live status is polled. Source credentials
are never copied into feedback. SHA-256 login follows the v1.3 parameter table
and 64-digit curl example, not the stale MD5 example elsewhere in the PDF.
No claims are made for the older NDI decoder or encoder APIs, or for hardware
qualification. Network, firmware, accounts and file uploads remain in the web UI.
Source: https://www.magewell.com/api-docs/pro-convert-ip-decoder-api/latest/
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
from urllib.parse import urlsplit

import httpx
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError

_VIDEO_FIELDS = {'follow_input': 'follow-input',
 'show_title': 'show-title',
 'show_tally': 'show-tally',
 'screen_debug': 'screen-debug',
 'enable_webrtc': 'enable-webrtc',
 'identifier_mode': 'ident-mode',
 'identifier_text': 'ident-text',
 'vu_meter_mode': 'vu-meter-mode',
 'vu_display_mode': 'vu-display-mode',
 'center_cross': 'show-center-cross',
 'safe_area': 'safe-area',
 'horizontal_flip': 'h-flip',
 'vertical_flip': 'v-flip',
 'deinterlace': 'deinterlace-mode',
 'force_deinterlace': 'force-deint',
 'aspect_conversion': 'ar-convert',
 'hdr_mode': 'hdr-output-mode',
 'output_color': 'color-format',
 'output_range': 'quant-range',
 'no_signal_mode': 'switch-mode',
 'input_color': 'in-color-fmt',
 'alpha_mode': 'alpha-mode',
 'alpha_color': 'alpha-color-key'}
_AUDIO_FIELDS = {'audio_gain': 'gain',
 'audio_channels': 'channels',
 'audio_sample_rate': 'sample-rate',
 'audio_check_pts': 'check-pts',
 'audio_conversion': 'convert-mode'}
_NDI_FIELDS = {'ndi_discovery_enabled': 'enable-d',
 'ndi_discovery_server': 'd-server',
 'ndi_groups': 'groups',
 'ndi_extra_ips': 'extra-ips',
 'ndi_transport': 'transport',
 'ndi_multicast_subnets': 'mcast-subnets',
 'ndi_pts_mode': 'pts-mode',
 'ndi_low_bandwidth': 'low-bw',
 'ndi_buffer_ms': 'buffer-ms'}

class MagewellProConvertIPDecoderDriver(BaseDriver):
    DRIVER_INFO = {'id': 'magewell_pro_convert_ip_decoder',
     'name': 'Magewell Pro Convert IP Decoder',
     'manufacturer': 'Magewell',
     'category': 'streaming',
     'version': '1.0.0',
     'author': 'OpenAVC',
     'transport': 'http',
     'ports': [80, 443],
     'protocols': ['http'],
     'source_url': 'https://www.magewell.com/api-docs/pro-convert-ip-decoder-api/latest/',
     'description': 'Controls IP decoder sources, single/PiP/quad profiles, screen routing, audio, video '
                    'and source-camera PTZ.',
     'simulated': True,
     'verified': False,
     'min_platform_version': '0.37.1',
     'tags': ['decoder', 'ndi', 'srt', 'streaming', 'multiview'],
     'compatible_models': [{'manufacturer': 'Magewell',
                            'models': ['Pro Convert IP to HDMI',
                                       'Pro Convert IP to HDMI 4K',
                                       'Pro Convert IP to AIO 4K'],
                            'confidence': 'untested',
                            'notes': 'SKUs 644300000, 644100000 and 644000000. Firmware 1.3 or newer; API '
                                     'v1.3. Older Pro Convert NDI decoders and encoders use different '
                                     'APIs.'}],
     'help': {'overview': 'Select a discovered or saved source, recall a saved profile, and route sources '
                          'to the active windows. Window feedback reflects the decoder’s actual stream '
                          'status. Sources and profiles are discovered automatically. No hardware '
                          'qualification has been performed.',
              'setup': 'Use firmware 1.3 or newer. Enter a local account with control permission; username '
                       'is case-sensitive. Match HTTP/HTTPS and the port to the decoder’s web interface. '
                       'Simple mode uses Select Source; Profile mode uses Select Profile. Changing modes is '
                       'explicit. Set up network access, certificates and firmware in the device web '
                       'interface. NDI alpha controls require a 4K model. PTZ requires a supporting active '
                       'source. On loss of signal, Receiving becomes false; the decoder may still display '
                       'its configured fallback image.'},
     'default_config': {'host': '',
                        'port': 80,
                        'ssl': False,
                        'verify_ssl': False,
                        'username': 'Admin',
                        'poll_interval': 3,
                        'detail_poll_every': 10},
     'config_schema': {'host': {'type': 'string', 'label': 'IP Address', 'required': True},
                       'port': {'type': 'integer',
                                'label': 'Web Port',
                                'default': 80,
                                'min': 1,
                                'max': 65535},
                       'ssl': {'type': 'boolean', 'label': 'HTTPS', 'default': False},
                       'verify_ssl': {'type': 'boolean',
                                      'label': 'Verify HTTPS Certificate',
                                      'default': False},
                       'username': {'type': 'string',
                                    'label': 'Username',
                                    'default': 'Admin',
                                    'required': True},
                       'password': {'type': 'string', 'label': 'Password', 'secret': True, 'required': True},
                       'poll_interval': {'type': 'number',
                                         'label': 'Poll Interval (seconds)',
                                         'min': 0,
                                         'default': 3},
                       'detail_poll_every': {'type': 'integer',
                                             'label': 'Refresh Settings Every N Polls',
                                             'min': 1,
                                             'max': 100,
                                             'default': 10}},
     'state_variables': {'follow_input': {'type': 'boolean', 'label': 'Match Input Timing', 'control': True},
                         'show_title': {'type': 'boolean', 'label': 'Show Source Name', 'control': True},
                         'show_tally': {'type': 'boolean', 'label': 'Show Tally', 'control': True},
                         'screen_debug': {'type': 'boolean',
                                          'label': 'Show Stream Information',
                                          'control': True},
                         'enable_webrtc': {'type': 'boolean',
                                           'label': 'Enable Web Preview',
                                           'control': True},
                         'identifier_mode': {'type': 'enum',
                                             'label': 'Identifier',
                                             'values': [0, 1, 2],
                                             'control': True},
                         'identifier_text': {'type': 'string', 'label': 'Identifier Text', 'control': True},
                         'vu_meter_mode': {'type': 'enum',
                                           'label': 'Audio Meter',
                                           'values': [0, 1, 2],
                                           'control': True},
                         'vu_display_mode': {'type': 'enum',
                                             'label': 'Meter Placement',
                                             'values': ['global', 'per-window'],
                                             'control': True},
                         'center_cross': {'type': 'boolean', 'label': 'Show Center Cross', 'control': True},
                         'safe_area': {'type': 'enum',
                                       'label': 'Safe Area',
                                       'values': [0, 1, 2, 3],
                                       'control': True},
                         'horizontal_flip': {'type': 'boolean', 'label': 'Horizontal Flip', 'control': True},
                         'vertical_flip': {'type': 'boolean', 'label': 'Vertical Flip', 'control': True},
                         'deinterlace': {'type': 'enum',
                                         'label': 'Deinterlace Method',
                                         'values': [0, 1, 2],
                                         'control': True},
                         'force_deinterlace': {'type': 'boolean',
                                               'label': 'Force Deinterlace',
                                               'control': True},
                         'aspect_conversion': {'type': 'enum',
                                               'label': 'Aspect Ratio',
                                               'values': [0, 1, 2],
                                               'control': True},
                         'hdr_mode': {'type': 'enum',
                                      'label': 'HDR Output',
                                      'values': [0, 1],
                                      'control': True},
                         'output_color': {'type': 'enum',
                                          'label': 'Output Color Format',
                                          'values': ['rgb', 'yuv444', 'yuv422'],
                                          'control': True},
                         'output_range': {'type': 'enum',
                                          'label': 'Output Range',
                                          'values': ['limited', 'full'],
                                          'control': True},
                         'no_signal_mode': {'type': 'enum',
                                            'label': 'When Signal Is Lost',
                                            'values': [0, 1, 2, 3],
                                            'control': True},
                         'input_color': {'type': 'enum',
                                         'label': 'Input Colorimetry',
                                         'values': [0, 1, 2, 3],
                                         'control': True},
                         'alpha_mode': {'type': 'enum',
                                        'label': 'NDI Alpha Display',
                                        'values': ['alpha-only', 'alpha-color-key', 'alpha-checkerboard'],
                                        'help': '4K models only.',
                                        'control': True},
                         'alpha_color': {'type': 'integer',
                                         'label': 'Alpha Key Color (RGB)',
                                         'min': 0,
                                         'max': 16777215,
                                         'help': '4K models only.',
                                         'control': True},
                         'audio_gain': {'type': 'number',
                                        'label': 'Audio Gain (dB)',
                                        'min': -100,
                                        'max': 20,
                                        'control': True,
                                        'cloud_priority': 'high'},
                         'audio_channels': {'type': 'enum',
                                            'label': 'Output Audio Channels',
                                            'values': [0, 2, 4, 8],
                                            'control': True},
                         'audio_sample_rate': {'type': 'enum',
                                               'label': 'Audio Sample Rate',
                                               'values': [0, 32000, 44100, 48000, 88200, 96000],
                                               'control': True},
                         'audio_check_pts': {'type': 'boolean',
                                             'label': 'Check Audio Timestamps',
                                             'control': True},
                         'audio_conversion': {'type': 'enum',
                                              'label': 'Audio Conversion',
                                              'values': [0, 1],
                                              'control': True},
                         'ndi_discovery_enabled': {'type': 'boolean',
                                                   'label': 'Use NDI Discovery Server',
                                                   'control': True},
                         'ndi_discovery_server': {'type': 'string',
                                                  'label': 'NDI Discovery Server',
                                                  'control': True},
                         'ndi_groups': {'type': 'string', 'label': 'NDI Groups', 'control': True},
                         'ndi_extra_ips': {'type': 'string',
                                           'label': 'Extra NDI IP Addresses',
                                           'help': 'Comma-separated addresses.',
                                           'control': True},
                         'ndi_transport': {'type': 'enum',
                                           'label': 'NDI Transport',
                                           'values': ['auto',
                                                      'rudp',
                                                      'udp',
                                                      'multicast',
                                                      'multi-tcp',
                                                      'tcp'],
                                           'control': True},
                         'ndi_multicast_subnets': {'type': 'string',
                                                   'label': 'NDI Multicast Subnets',
                                                   'help': 'Comma-separated CIDR subnets.',
                                                   'control': True},
                         'ndi_pts_mode': {'type': 'enum',
                                          'label': 'NDI Timestamps',
                                          'values': ['auto', 'timecode', 'timestamp'],
                                          'control': True},
                         'ndi_low_bandwidth': {'type': 'boolean',
                                               'label': 'Use NDI Low Bandwidth Stream',
                                               'control': True},
                         'ndi_buffer_ms': {'type': 'integer',
                                           'label': 'NDI Buffer (ms)',
                                           'min': 0,
                                           'control': True},
                         'panel_locked': {'type': 'boolean', 'label': 'Lock Front Panel', 'control': True},
                         'device_name': {'type': 'string', 'label': 'Device Name', 'control': True},
                         'model': {'type': 'string', 'label': 'Model'},
                         'serial_number': {'type': 'string', 'label': 'Serial Number'},
                         'firmware_version': {'type': 'string', 'label': 'Firmware'},
                         'last_error': {'type': 'string', 'label': 'Last Error', 'cloud_priority': 'high'},
                         'mode': {'type': 'integer',
                                  'label': 'Configuration Mode',
                                  'cloud_priority': 'high'},
                         'current_profile': {'type': 'string',
                                             'label': 'Current Profile ID',
                                             'cloud_priority': 'high'},
                         'layout': {'type': 'integer', 'label': 'View Layout', 'cloud_priority': 'high'},
                         'audio_screen': {'type': 'integer', 'label': 'Audio Window (0–3)'},
                         'hdmi_state': {'type': 'integer', 'label': 'HDMI Status Code'},
                         'temperature': {'type': 'number', 'label': 'Core Temperature (°C)'},
                         'fan_speed': {'type': 'integer', 'label': 'Fan Speed'},
                         'uptime': {'type': 'integer', 'label': 'Uptime (seconds)'},
                         'ptz_available': {'type': 'boolean', 'label': 'Source Supports PTZ'},
                         'output_mode': {'type': 'string', 'label': 'Output Timing'},
                         'output_mode_options': {'type': 'string', 'label': 'Available Output Timings'}},
     'child_entity_types': {'source': {'label': 'Source',
                                       'label_plural': 'Sources',
                                       'id_format': {'type': 'string'},
                                       'label_field': 'name',
                                       'summary_fields': ['name'],
                                       'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                           'protocol': {'type': 'string',
                                                                        'label': 'Protocol'},
                                                           'source_type': {'type': 'integer',
                                                                           'label': 'Source Type'},
                                                           'selected': {'type': 'boolean',
                                                                        'label': 'Selected',
                                                                        'cloud_priority': 'high'},
                                                           'online': {'type': 'boolean',
                                                                      'label': 'In Source List'}}},
                            'profile': {'label': 'Profile',
                                        'label_plural': 'Profiles',
                                        'id_format': {'type': 'string'},
                                        'label_field': 'name',
                                        'summary_fields': ['name'],
                                        'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                            'layout': {'type': 'integer',
                                                                       'label': 'View Layout'},
                                                            'audio_screen': {'type': 'integer',
                                                                             'label': 'Audio Window'},
                                                            'selected': {'type': 'boolean',
                                                                         'label': 'Selected',
                                                                         'cloud_priority': 'high'},
                                                            'online': {'type': 'boolean',
                                                                       'label': 'In Profile List'}}},
                            'screen': {'label': 'Window',
                                       'label_plural': 'Windows',
                                       'id_format': {'type': 'string'},
                                       'label_field': 'name',
                                       'summary_fields': ['name'],
                                       'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                           'source': {'type': 'string',
                                                                      'label': 'Source ID',
                                                                      'cloud_priority': 'high'},
                                                           'status': {'type': 'string',
                                                                      'label': 'Stream Status',
                                                                      'cloud_priority': 'high'},
                                                           'status_code': {'type': 'integer',
                                                                           'label': 'Status Code'},
                                                           'receiving': {'type': 'boolean',
                                                                         'label': 'Receiving',
                                                                         'cloud_priority': 'high'},
                                                           'protocol': {'type': 'string',
                                                                        'label': 'Protocol'},
                                                           'width': {'type': 'integer',
                                                                     'label': 'Video Width'},
                                                           'height': {'type': 'integer',
                                                                      'label': 'Video Height'},
                                                           'frame_rate': {'type': 'string',
                                                                          'label': 'Field Rate'},
                                                           'video_codec': {'type': 'string',
                                                                           'label': 'Video Codec'},
                                                           'audio_codec': {'type': 'string',
                                                                           'label': 'Audio Codec'},
                                                           'audio_channels': {'type': 'integer',
                                                                              'label': 'Audio Channels'},
                                                           'audio_sample_rate': {'type': 'integer',
                                                                                 'label': 'Audio Sample '
                                                                                          'Rate'},
                                                           'video_bitrate': {'type': 'integer',
                                                                             'label': 'Video Bit Rate '
                                                                                      '(kbps)',
                                                                             'cloud_priority': 'low'},
                                                           'audio_bitrate': {'type': 'integer',
                                                                             'label': 'Audio Bit Rate '
                                                                                      '(kbps)',
                                                                             'cloud_priority': 'low'},
                                                           'video_drops': {'type': 'integer',
                                                                           'label': 'Dropped Video Frames',
                                                                           'cloud_priority': 'low'},
                                                           'audio_drops': {'type': 'integer',
                                                                           'label': 'Dropped Audio Frames',
                                                                           'cloud_priority': 'low'},
                                                           'buffer_ms': {'type': 'integer',
                                                                         'label': 'Buffer (ms)'},
                                                           'tally_program': {'type': 'boolean',
                                                                             'label': 'Program Tally'},
                                                           'tally_preview': {'type': 'boolean',
                                                                             'label': 'Preview Tally'}}},
                            'image': {'label': 'Image',
                                      'label_plural': 'Images',
                                      'id_format': {'type': 'string'},
                                      'label_field': 'name',
                                      'summary_fields': ['name'],
                                      'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                          'image_type': {'type': 'string', 'label': 'Use'},
                                                          'selected': {'type': 'boolean',
                                                                       'label': 'Selected'}}}},
     'commands': {'refresh': {'label': 'Refresh Status', 'params': {}},
                  'select_source': {'label': 'Select Source',
                                    'params': {'source': {'type': 'child_id',
                                                          'label': 'Source',
                                                          'required': True,
                                                          'child_type': 'source'}},
                                    'help': 'Simple mode only. Use Configuration Mode to change modes '
                                            'explicitly.'},
                  'select_profile': {'label': 'Select Profile',
                                     'params': {'profile': {'type': 'child_id',
                                                            'label': 'Profile',
                                                            'required': True,
                                                            'child_type': 'profile'}},
                                     'help': 'Profile mode only.'},
                  'set_mode': {'label': 'Set Configuration Mode',
                               'params': {'mode': {'type': 'enum',
                                                   'label': 'Mode',
                                                   'values': [{'value': 1, 'label': 'Simple'},
                                                              {'value': 2, 'label': 'Profiles'}],
                                                   'required': True}}},
                  'route_screen': {'label': 'Route Source to Window',
                                   'params': {'screen': {'type': 'child_id',
                                                         'label': 'Screen',
                                                         'required': True,
                                                         'child_type': 'screen'},
                                              'source': {'type': 'child_id',
                                                         'label': 'Source',
                                                         'required': True,
                                                         'child_type': 'source'}}},
                  'set_profile_screen': {'label': 'Set Saved Profile Window',
                                         'params': {'profile': {'type': 'child_id',
                                                                'label': 'Profile',
                                                                'required': True,
                                                                'child_type': 'profile'},
                                                    'screen': {'type': 'integer',
                                                               'label': 'Window Index',
                                                               'required': True,
                                                               'min': 0,
                                                               'max': 3},
                                                    'source': {'type': 'child_id',
                                                               'label': 'Source',
                                                               'required': True,
                                                               'child_type': 'source'}}},
                  'set_profile_audio': {'label': 'Set Profile Audio Window',
                                        'params': {'profile': {'type': 'child_id',
                                                               'label': 'Profile',
                                                               'required': True,
                                                               'child_type': 'profile'},
                                                   'screen': {'type': 'integer',
                                                              'label': 'Window Index',
                                                              'required': True,
                                                              'min': 0,
                                                              'max': 3}}},
                  'rename_profile': {'label': 'Rename Profile',
                                     'params': {'profile': {'type': 'child_id',
                                                            'label': 'Profile',
                                                            'required': True,
                                                            'child_type': 'profile'},
                                                'name': {'type': 'string',
                                                         'label': 'Name',
                                                         'required': True}}},
                  'delete_profile': {'label': 'Delete Profile',
                                     'params': {'profile': {'type': 'child_id',
                                                            'label': 'Profile',
                                                            'required': True,
                                                            'child_type': 'profile'}},
                                     'confirm': 'Delete this saved profile? The default profile cannot be '
                                                'deleted.'},
                  'add_url_source': {'label': 'Add URL Source',
                                     'params': {'name': {'type': 'string',
                                                         'label': 'Name',
                                                         'required': True},
                                                'url': {'type': 'string',
                                                        'label': 'Stream URL',
                                                        'required': True}},
                                     'help': 'Use the complete stream URL, including any required protocol '
                                             'options. It is not exposed as feedback.'},
                  'update_url_source': {'label': 'Change Source URL',
                                        'params': {'source': {'type': 'child_id',
                                                              'label': 'Source',
                                                              'required': True,
                                                              'child_type': 'source'},
                                                   'url': {'type': 'string',
                                                           'label': 'Stream URL',
                                                           'required': True}}},
                  'rename_source': {'label': 'Rename Saved Source',
                                    'params': {'source': {'type': 'child_id',
                                                          'label': 'Source',
                                                          'required': True,
                                                          'child_type': 'source'},
                                               'name': {'type': 'string',
                                                        'label': 'Name',
                                                        'required': True}}},
                  'delete_source': {'label': 'Delete Saved Source',
                                    'params': {'source': {'type': 'child_id',
                                                          'label': 'Source',
                                                          'required': True,
                                                          'child_type': 'source'}},
                                    'confirm': 'Delete this saved source from the decoder?'},
                  'restart_source': {'label': 'Restart Current Source', 'params': {}},
                  'wake_output': {'label': 'Wake Video Output',
                                  'params': {},
                                  'help': 'Restores an output disabled by the no-signal setting. Does not '
                                          'turn the decoder on.'},
                  'set_output_mode': {'label': 'Set Output Timing',
                                      'params': {'mode': {'type': 'string',
                                                          'label': 'Timing',
                                                          'required': True,
                                                          'options_state': 'output_mode_options'}}},
                  'select_image': {'label': 'Select Status Image',
                                   'params': {'image': {'type': 'child_id',
                                                        'label': 'Image',
                                                        'required': True,
                                                        'child_type': 'image'}}},
                  'ptz_move': {'label': 'Move Source Camera',
                               'params': {'pan': {'type': 'number',
                                                  'label': 'Pan (−1 Right, +1 Left)',
                                                  'required': True,
                                                  'min': -1,
                                                  'max': 1},
                                          'tilt': {'type': 'number',
                                                   'label': 'Tilt (−1 Down, +1 Up)',
                                                   'required': True,
                                                   'min': -1,
                                                   'max': 1}}},
                  'ptz_stop': {'label': 'Stop Source Camera Movement', 'params': {}},
                  'ptz_zoom_speed': {'label': 'Set Camera Zoom Speed',
                                     'params': {'speed': {'type': 'number',
                                                          'label': 'Speed (−1 Out, +1 In, 0 Stop)',
                                                          'required': True,
                                                          'min': -1,
                                                          'max': 1}}},
                  'ptz_focus_speed': {'label': 'Set Camera Focus Speed',
                                      'params': {'speed': {'type': 'number',
                                                           'label': 'Signed Speed (0 Stop)',
                                                           'required': True,
                                                           'min': -1,
                                                           'max': 1}}},
                  'ptz_zoom_position': {'label': 'Set Camera Zoom Position',
                                        'params': {'value': {'type': 'number',
                                                             'label': 'Position (0 Maximum Zoom, 1 Minimum)',
                                                             'required': True,
                                                             'min': 0,
                                                             'max': 1}}},
                  'ptz_focus_position': {'label': 'Set Camera Focus Position',
                                         'params': {'value': {'type': 'number',
                                                              'label': 'Position (0 Far, 1 Near)',
                                                              'required': True,
                                                              'min': 0,
                                                              'max': 1}}},
                  'ptz_autofocus': {'label': 'Autofocus Source Camera', 'params': {}},
                  'ptz_recall': {'label': 'Recall Camera Preset',
                                 'params': {'number': {'type': 'integer',
                                                       'label': 'Preset',
                                                       'required': True,
                                                       'min': 1,
                                                       'max': 255}}},
                  'ptz_store': {'label': 'Store Camera Preset',
                                'params': {'number': {'type': 'integer',
                                                      'label': 'Preset',
                                                      'required': True,
                                                      'min': 1,
                                                      'max': 255}},
                                'confirm': 'Replace this camera preset with the current camera position?'},
                  'create_profile': {'label': 'Create Profile',
                                     'params': {'name': {'type': 'string',
                                                         'label': 'Name',
                                                         'required': True},
                                                'layout': {'type': 'enum',
                                                           'label': 'Layout',
                                                           'values': [{'value': 1, 'label': 'Single'},
                                                                      {'value': 2,
                                                                       'label': 'Picture in Picture'},
                                                                      {'value': 4, 'label': 'Quad View'}],
                                                           'required': True},
                                                'source_1': {'type': 'child_id',
                                                             'label': 'Window 1',
                                                             'required': True,
                                                             'child_type': 'source'},
                                                'source_2': {'type': 'child_id',
                                                             'label': 'Window 2',
                                                             'required': False,
                                                             'child_type': 'source'},
                                                'source_3': {'type': 'child_id',
                                                             'label': 'Window 3',
                                                             'required': False,
                                                             'child_type': 'source'},
                                                'source_4': {'type': 'child_id',
                                                             'label': 'Window 4',
                                                             'required': False,
                                                             'child_type': 'source'}},
                                     'help': 'Supply one, two or four sources to match the selected '
                                             'layout.'},
                  'set_pip': {'label': 'Set Picture in Picture',
                              'params': {'profile': {'type': 'child_id',
                                                     'label': 'Profile',
                                                     'required': True,
                                                     'child_type': 'profile'},
                                         'position': {'type': 'enum',
                                                      'label': 'Position',
                                                      'values': [{'value': 0, 'label': 'Custom'},
                                                                 {'value': 1, 'label': 'Top Left'},
                                                                 {'value': 2, 'label': 'Top Center'},
                                                                 {'value': 3, 'label': 'Top Right'},
                                                                 {'value': 4, 'label': 'Bottom Left'},
                                                                 {'value': 5, 'label': 'Bottom Center'},
                                                                 {'value': 6, 'label': 'Bottom Right'},
                                                                 {'value': 7, 'label': 'Center'}],
                                                      'required': True},
                                         'scale': {'type': 'number',
                                                   'label': 'Size (fraction of screen)',
                                                   'required': True,
                                                   'min': 0.3,
                                                   'max': 0.8},
                                         'x': {'type': 'number',
                                               'label': 'Custom X',
                                               'min': 0,
                                               'max': 1,
                                               'default': 0},
                                         'y': {'type': 'number',
                                               'label': 'Custom Y',
                                               'min': 0,
                                               'max': 1,
                                               'default': 0}}},
                  'add_ndi_source': {'label': 'Add Manual NDI Source',
                                     'params': {'name': {'type': 'string',
                                                         'label': 'Name',
                                                         'required': True},
                                                'ndi_name': {'type': 'string', 'label': 'NDI Source Name'},
                                                'ndi_url': {'type': 'string', 'label': 'NDI Source URL'}},
                                     'help': 'Supply the NDI name, URL, or both as provided by the source.'},
                  'set_audio_channel_pair': {'label': 'Map Audio Channel Pair',
                                             'params': {'output_pair': {'type': 'integer',
                                                                        'label': 'Output Pair (1–4)',
                                                                        'required': True,
                                                                        'min': 1,
                                                                        'max': 4},
                                                        'input_pair': {'type': 'integer',
                                                                       'label': 'Input Pair (1–4)',
                                                                       'required': True,
                                                                       'min': 1,
                                                                       'max': 4}},
                                             'help': 'Requires a fixed audio channel count. Preserves the '
                                                     'other channel mappings.'},
                  'set_follow_input': {'label': 'Set Match Input Timing',
                                       'params': {'value': {'type': 'boolean',
                                                            'label': 'Match Input Timing',
                                                            'required': True}}},
                  'set_show_title': {'label': 'Set Show Source Name',
                                     'params': {'value': {'type': 'boolean',
                                                          'label': 'Show Source Name',
                                                          'required': True}}},
                  'set_show_tally': {'label': 'Set Show Tally',
                                     'params': {'value': {'type': 'boolean',
                                                          'label': 'Show Tally',
                                                          'required': True}}},
                  'set_screen_debug': {'label': 'Set Show Stream Information',
                                       'params': {'value': {'type': 'boolean',
                                                            'label': 'Show Stream Information',
                                                            'required': True}}},
                  'set_enable_webrtc': {'label': 'Set Enable Web Preview',
                                        'params': {'value': {'type': 'boolean',
                                                             'label': 'Enable Web Preview',
                                                             'required': True}}},
                  'set_identifier_mode': {'label': 'Set Identifier',
                                          'params': {'value': {'type': 'enum',
                                                               'label': 'Identifier',
                                                               'values': [{'value': 0, 'label': 'Hidden'},
                                                                          {'value': 1,
                                                                           'label': 'Custom Text'},
                                                                          {'value': 2,
                                                                           'label': 'Device Name'}],
                                                               'required': True}}},
                  'set_identifier_text': {'label': 'Set Identifier Text',
                                          'params': {'value': {'type': 'string',
                                                               'label': 'Identifier Text',
                                                               'required': True}}},
                  'set_vu_meter_mode': {'label': 'Set Audio Meter',
                                        'params': {'value': {'type': 'enum',
                                                             'label': 'Audio Meter',
                                                             'values': [{'value': 0, 'label': 'Hidden'},
                                                                        {'value': 1,
                                                                         'label': 'Post Gain (dBu)'},
                                                                        {'value': 2,
                                                                         'label': 'Post Gain (dBFS)'}],
                                                             'required': True}}},
                  'set_vu_display_mode': {'label': 'Set Meter Placement',
                                          'params': {'value': {'type': 'enum',
                                                               'label': 'Meter Placement',
                                                               'values': [{'value': 'global',
                                                                           'label': 'Global'},
                                                                          {'value': 'per-window',
                                                                           'label': 'Each Window'}],
                                                               'required': True}}},
                  'set_center_cross': {'label': 'Set Show Center Cross',
                                       'params': {'value': {'type': 'boolean',
                                                            'label': 'Show Center Cross',
                                                            'required': True}}},
                  'set_safe_area': {'label': 'Set Safe Area',
                                    'params': {'value': {'type': 'enum',
                                                         'label': 'Safe Area',
                                                         'values': [{'value': 0, 'label': 'None'},
                                                                    {'value': 1, 'label': '4:3'},
                                                                    {'value': 2, 'label': 'Center 80%'},
                                                                    {'value': 3, 'label': 'Square'}],
                                                         'required': True}}},
                  'set_horizontal_flip': {'label': 'Set Horizontal Flip',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Horizontal Flip',
                                                               'required': True}}},
                  'set_vertical_flip': {'label': 'Set Vertical Flip',
                                        'params': {'value': {'type': 'boolean',
                                                             'label': 'Vertical Flip',
                                                             'required': True}}},
                  'set_deinterlace': {'label': 'Set Deinterlace Method',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Deinterlace Method',
                                                           'values': [{'value': 0, 'label': 'Bob'},
                                                                      {'value': 1, 'label': 'Weave'},
                                                                      {'value': 2, 'label': 'Blend'}],
                                                           'required': True}}},
                  'set_force_deinterlace': {'label': 'Set Force Deinterlace',
                                            'params': {'value': {'type': 'boolean',
                                                                 'label': 'Force Deinterlace',
                                                                 'required': True}}},
                  'set_aspect_conversion': {'label': 'Set Aspect Ratio',
                                            'params': {'value': {'type': 'enum',
                                                                 'label': 'Aspect Ratio',
                                                                 'values': [{'value': 0,
                                                                             'label': 'Fit with Borders'},
                                                                            {'value': 1,
                                                                             'label': 'Fill and Crop'},
                                                                            {'value': 2,
                                                                             'label': 'Stretch'}],
                                                                 'required': True}}},
                  'set_hdr_mode': {'label': 'Set HDR Output',
                                   'params': {'value': {'type': 'enum',
                                                        'label': 'HDR Output',
                                                        'values': [{'value': 0, 'label': 'Automatic'},
                                                                   {'value': 1, 'label': 'Disabled'}],
                                                        'required': True}}},
                  'set_output_color': {'label': 'Set Output Color Format',
                                       'params': {'value': {'type': 'enum',
                                                            'label': 'Output Color Format',
                                                            'values': [{'value': 'rgb', 'label': 'RGB'},
                                                                       {'value': 'yuv444',
                                                                        'label': 'YUV444'},
                                                                       {'value': 'yuv422',
                                                                        'label': 'YUV422'}],
                                                            'required': True}}},
                  'set_output_range': {'label': 'Set Output Range',
                                       'params': {'value': {'type': 'enum',
                                                            'label': 'Output Range',
                                                            'values': [{'value': 'limited',
                                                                        'label': 'Limited'},
                                                                       {'value': 'full', 'label': 'Full'}],
                                                            'required': True}}},
                  'set_no_signal_mode': {'label': 'Set When Signal Is Lost',
                                         'params': {'value': {'type': 'enum',
                                                              'label': 'When Signal Is Lost',
                                                              'values': [{'value': 0,
                                                                          'label': 'No-Signal Image'},
                                                                         {'value': 1, 'label': 'Black'},
                                                                         {'value': 2,
                                                                          'label': 'Hold Last Frame'},
                                                                         {'value': 3,
                                                                          'label': 'Disable Output after 30 '
                                                                                   'Seconds'}],
                                                              'required': True}}},
                  'set_input_color': {'label': 'Set Input Colorimetry',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Input Colorimetry',
                                                           'values': [{'value': 0, 'label': 'Automatic'},
                                                                      {'value': 1, 'label': 'BT.601'},
                                                                      {'value': 2, 'label': 'BT.709'},
                                                                      {'value': 3, 'label': 'BT.2020'}],
                                                           'required': True}}},
                  'set_alpha_mode': {'label': 'Set NDI Alpha Display',
                                     'params': {'value': {'type': 'enum',
                                                          'label': 'NDI Alpha Display',
                                                          'values': [{'value': 'alpha-only',
                                                                      'label': 'Alpha Only'},
                                                                     {'value': 'alpha-color-key',
                                                                      'label': 'Color Key'},
                                                                     {'value': 'alpha-checkerboard',
                                                                      'label': 'Checkerboard'}],
                                                          'help': '4K models only.',
                                                          'required': True}}},
                  'set_alpha_color': {'label': 'Set Alpha Key Color (RGB)',
                                      'params': {'value': {'type': 'integer',
                                                           'label': 'Alpha Key Color (RGB)',
                                                           'min': 0,
                                                           'max': 16777215,
                                                           'help': '4K models only.',
                                                           'required': True}}},
                  'set_audio_gain': {'label': 'Set Audio Gain (dB)',
                                     'params': {'value': {'type': 'number',
                                                          'label': 'Audio Gain (dB)',
                                                          'min': -100,
                                                          'max': 20,
                                                          'required': True}}},
                  'set_audio_channels': {'label': 'Set Output Audio Channels',
                                         'params': {'value': {'type': 'enum',
                                                              'label': 'Output Audio Channels',
                                                              'values': [{'value': 0,
                                                                          'label': 'Match Input'},
                                                                         {'value': 2, 'label': '2 Channels'},
                                                                         {'value': 4, 'label': '4 Channels'},
                                                                         {'value': 8,
                                                                          'label': '8 Channels'}],
                                                              'required': True}}},
                  'set_audio_sample_rate': {'label': 'Set Audio Sample Rate',
                                            'params': {'value': {'type': 'enum',
                                                                 'label': 'Audio Sample Rate',
                                                                 'values': [{'value': 0,
                                                                             'label': 'Match Input'},
                                                                            {'value': 32000,
                                                                             'label': '32000 Hz'},
                                                                            {'value': 44100,
                                                                             'label': '44100 Hz'},
                                                                            {'value': 48000,
                                                                             'label': '48000 Hz'},
                                                                            {'value': 88200,
                                                                             'label': '88200 Hz'},
                                                                            {'value': 96000,
                                                                             'label': '96000 Hz'}],
                                                                 'required': True}}},
                  'set_audio_check_pts': {'label': 'Set Check Audio Timestamps',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Check Audio Timestamps',
                                                               'required': True}}},
                  'set_audio_conversion': {'label': 'Set Audio Conversion',
                                           'params': {'value': {'type': 'enum',
                                                                'label': 'Audio Conversion',
                                                                'values': [{'value': 0, 'label': 'SMPTE'},
                                                                           {'value': 1, 'label': 'EBU'}],
                                                                'required': True}}},
                  'set_ndi_discovery_enabled': {'label': 'Set Use NDI Discovery Server',
                                                'params': {'value': {'type': 'boolean',
                                                                     'label': 'Use NDI Discovery Server',
                                                                     'required': True}}},
                  'set_ndi_discovery_server': {'label': 'Set NDI Discovery Server',
                                               'params': {'value': {'type': 'string',
                                                                    'label': 'NDI Discovery Server',
                                                                    'required': True}}},
                  'set_ndi_groups': {'label': 'Set NDI Groups',
                                     'params': {'value': {'type': 'string',
                                                          'label': 'NDI Groups',
                                                          'required': True}}},
                  'set_ndi_extra_ips': {'label': 'Set Extra NDI IP Addresses',
                                        'params': {'value': {'type': 'string',
                                                             'label': 'Extra NDI IP Addresses',
                                                             'help': 'Comma-separated addresses.',
                                                             'required': True}}},
                  'set_ndi_transport': {'label': 'Set NDI Transport',
                                        'params': {'value': {'type': 'enum',
                                                             'label': 'NDI Transport',
                                                             'values': [{'value': 'auto', 'label': 'AUTO'},
                                                                        {'value': 'rudp', 'label': 'RUDP'},
                                                                        {'value': 'udp', 'label': 'UDP'},
                                                                        {'value': 'multicast',
                                                                         'label': 'MULTICAST'},
                                                                        {'value': 'multi-tcp',
                                                                         'label': 'MULTI-TCP'},
                                                                        {'value': 'tcp', 'label': 'TCP'}],
                                                             'required': True}}},
                  'set_ndi_multicast_subnets': {'label': 'Set NDI Multicast Subnets',
                                                'params': {'value': {'type': 'string',
                                                                     'label': 'NDI Multicast Subnets',
                                                                     'help': 'Comma-separated CIDR subnets.',
                                                                     'required': True}}},
                  'set_ndi_pts_mode': {'label': 'Set NDI Timestamps',
                                       'params': {'value': {'type': 'enum',
                                                            'label': 'NDI Timestamps',
                                                            'values': [{'value': 'auto', 'label': 'Auto'},
                                                                       {'value': 'timecode',
                                                                        'label': 'Timecode'},
                                                                       {'value': 'timestamp',
                                                                        'label': 'Timestamp'}],
                                                            'required': True}}},
                  'set_ndi_low_bandwidth': {'label': 'Set Use NDI Low Bandwidth Stream',
                                            'params': {'value': {'type': 'boolean',
                                                                 'label': 'Use NDI Low Bandwidth Stream',
                                                                 'required': True}}},
                  'set_ndi_buffer_ms': {'label': 'Set NDI Buffer (ms)',
                                        'params': {'value': {'type': 'integer',
                                                             'label': 'NDI Buffer (ms)',
                                                             'min': 0,
                                                             'required': True}}},
                  'set_panel_locked': {'label': 'Set Lock Front Panel',
                                       'params': {'value': {'type': 'boolean',
                                                            'label': 'Lock Front Panel',
                                                            'required': True}}},
                  'set_device_name': {'label': 'Set Device Name',
                                      'params': {'value': {'type': 'string',
                                                           'label': 'Device Name',
                                                           'required': True}}}},
     'device_settings': {'follow_input': {'type': 'boolean',
                                          'label': 'Match Input Timing',
                                          'state_key': 'follow_input',
                                          'setup': False},
                         'show_title': {'type': 'boolean',
                                        'label': 'Show Source Name',
                                        'state_key': 'show_title',
                                        'setup': False},
                         'show_tally': {'type': 'boolean',
                                        'label': 'Show Tally',
                                        'state_key': 'show_tally',
                                        'setup': False},
                         'screen_debug': {'type': 'boolean',
                                          'label': 'Show Stream Information',
                                          'state_key': 'screen_debug',
                                          'setup': False},
                         'enable_webrtc': {'type': 'boolean',
                                           'label': 'Enable Web Preview',
                                           'state_key': 'enable_webrtc',
                                           'setup': False},
                         'identifier_mode': {'type': 'enum',
                                             'label': 'Identifier',
                                             'values': [{'value': 0, 'label': 'Hidden'},
                                                        {'value': 1, 'label': 'Custom Text'},
                                                        {'value': 2, 'label': 'Device Name'}],
                                             'state_key': 'identifier_mode',
                                             'setup': False},
                         'identifier_text': {'type': 'string',
                                             'label': 'Identifier Text',
                                             'state_key': 'identifier_text',
                                             'setup': False},
                         'vu_meter_mode': {'type': 'enum',
                                           'label': 'Audio Meter',
                                           'values': [{'value': 0, 'label': 'Hidden'},
                                                      {'value': 1, 'label': 'Post Gain (dBu)'},
                                                      {'value': 2, 'label': 'Post Gain (dBFS)'}],
                                           'state_key': 'vu_meter_mode',
                                           'setup': False},
                         'vu_display_mode': {'type': 'enum',
                                             'label': 'Meter Placement',
                                             'values': [{'value': 'global', 'label': 'Global'},
                                                        {'value': 'per-window', 'label': 'Each Window'}],
                                             'state_key': 'vu_display_mode',
                                             'setup': False},
                         'center_cross': {'type': 'boolean',
                                          'label': 'Show Center Cross',
                                          'state_key': 'center_cross',
                                          'setup': False},
                         'safe_area': {'type': 'enum',
                                       'label': 'Safe Area',
                                       'values': [{'value': 0, 'label': 'None'},
                                                  {'value': 1, 'label': '4:3'},
                                                  {'value': 2, 'label': 'Center 80%'},
                                                  {'value': 3, 'label': 'Square'}],
                                       'state_key': 'safe_area',
                                       'setup': False},
                         'horizontal_flip': {'type': 'boolean',
                                             'label': 'Horizontal Flip',
                                             'state_key': 'horizontal_flip',
                                             'setup': False},
                         'vertical_flip': {'type': 'boolean',
                                           'label': 'Vertical Flip',
                                           'state_key': 'vertical_flip',
                                           'setup': False},
                         'deinterlace': {'type': 'enum',
                                         'label': 'Deinterlace Method',
                                         'values': [{'value': 0, 'label': 'Bob'},
                                                    {'value': 1, 'label': 'Weave'},
                                                    {'value': 2, 'label': 'Blend'}],
                                         'state_key': 'deinterlace',
                                         'setup': False},
                         'force_deinterlace': {'type': 'boolean',
                                               'label': 'Force Deinterlace',
                                               'state_key': 'force_deinterlace',
                                               'setup': False},
                         'aspect_conversion': {'type': 'enum',
                                               'label': 'Aspect Ratio',
                                               'values': [{'value': 0, 'label': 'Fit with Borders'},
                                                          {'value': 1, 'label': 'Fill and Crop'},
                                                          {'value': 2, 'label': 'Stretch'}],
                                               'state_key': 'aspect_conversion',
                                               'setup': False},
                         'hdr_mode': {'type': 'enum',
                                      'label': 'HDR Output',
                                      'values': [{'value': 0, 'label': 'Automatic'},
                                                 {'value': 1, 'label': 'Disabled'}],
                                      'state_key': 'hdr_mode',
                                      'setup': False},
                         'output_color': {'type': 'enum',
                                          'label': 'Output Color Format',
                                          'values': [{'value': 'rgb', 'label': 'RGB'},
                                                     {'value': 'yuv444', 'label': 'YUV444'},
                                                     {'value': 'yuv422', 'label': 'YUV422'}],
                                          'state_key': 'output_color',
                                          'setup': False},
                         'output_range': {'type': 'enum',
                                          'label': 'Output Range',
                                          'values': [{'value': 'limited', 'label': 'Limited'},
                                                     {'value': 'full', 'label': 'Full'}],
                                          'state_key': 'output_range',
                                          'setup': False},
                         'no_signal_mode': {'type': 'enum',
                                            'label': 'When Signal Is Lost',
                                            'values': [{'value': 0, 'label': 'No-Signal Image'},
                                                       {'value': 1, 'label': 'Black'},
                                                       {'value': 2, 'label': 'Hold Last Frame'},
                                                       {'value': 3,
                                                        'label': 'Disable Output after 30 Seconds'}],
                                            'state_key': 'no_signal_mode',
                                            'setup': False},
                         'input_color': {'type': 'enum',
                                         'label': 'Input Colorimetry',
                                         'values': [{'value': 0, 'label': 'Automatic'},
                                                    {'value': 1, 'label': 'BT.601'},
                                                    {'value': 2, 'label': 'BT.709'},
                                                    {'value': 3, 'label': 'BT.2020'}],
                                         'state_key': 'input_color',
                                         'setup': False},
                         'alpha_mode': {'type': 'enum',
                                        'label': 'NDI Alpha Display',
                                        'values': [{'value': 'alpha-only', 'label': 'Alpha Only'},
                                                   {'value': 'alpha-color-key', 'label': 'Color Key'},
                                                   {'value': 'alpha-checkerboard', 'label': 'Checkerboard'}],
                                        'help': '4K models only.',
                                        'state_key': 'alpha_mode',
                                        'setup': False},
                         'alpha_color': {'type': 'integer',
                                         'label': 'Alpha Key Color (RGB)',
                                         'min': 0,
                                         'max': 16777215,
                                         'help': '4K models only.',
                                         'state_key': 'alpha_color',
                                         'setup': False},
                         'audio_gain': {'type': 'number',
                                        'label': 'Audio Gain (dB)',
                                        'min': -100,
                                        'max': 20,
                                        'state_key': 'audio_gain',
                                        'setup': False},
                         'audio_channels': {'type': 'enum',
                                            'label': 'Output Audio Channels',
                                            'values': [{'value': 0, 'label': 'Match Input'},
                                                       {'value': 2, 'label': '2 Channels'},
                                                       {'value': 4, 'label': '4 Channels'},
                                                       {'value': 8, 'label': '8 Channels'}],
                                            'state_key': 'audio_channels',
                                            'setup': False},
                         'audio_sample_rate': {'type': 'enum',
                                               'label': 'Audio Sample Rate',
                                               'values': [{'value': 0, 'label': 'Match Input'},
                                                          {'value': 32000, 'label': '32000 Hz'},
                                                          {'value': 44100, 'label': '44100 Hz'},
                                                          {'value': 48000, 'label': '48000 Hz'},
                                                          {'value': 88200, 'label': '88200 Hz'},
                                                          {'value': 96000, 'label': '96000 Hz'}],
                                               'state_key': 'audio_sample_rate',
                                               'setup': False},
                         'audio_check_pts': {'type': 'boolean',
                                             'label': 'Check Audio Timestamps',
                                             'state_key': 'audio_check_pts',
                                             'setup': False},
                         'audio_conversion': {'type': 'enum',
                                              'label': 'Audio Conversion',
                                              'values': [{'value': 0, 'label': 'SMPTE'},
                                                         {'value': 1, 'label': 'EBU'}],
                                              'state_key': 'audio_conversion',
                                              'setup': False},
                         'ndi_discovery_enabled': {'type': 'boolean',
                                                   'label': 'Use NDI Discovery Server',
                                                   'state_key': 'ndi_discovery_enabled',
                                                   'setup': False},
                         'ndi_discovery_server': {'type': 'string',
                                                  'label': 'NDI Discovery Server',
                                                  'state_key': 'ndi_discovery_server',
                                                  'setup': False},
                         'ndi_groups': {'type': 'string',
                                        'label': 'NDI Groups',
                                        'state_key': 'ndi_groups',
                                        'setup': False},
                         'ndi_extra_ips': {'type': 'string',
                                           'label': 'Extra NDI IP Addresses',
                                           'help': 'Comma-separated addresses.',
                                           'state_key': 'ndi_extra_ips',
                                           'setup': False},
                         'ndi_transport': {'type': 'enum',
                                           'label': 'NDI Transport',
                                           'values': [{'value': 'auto', 'label': 'AUTO'},
                                                      {'value': 'rudp', 'label': 'RUDP'},
                                                      {'value': 'udp', 'label': 'UDP'},
                                                      {'value': 'multicast', 'label': 'MULTICAST'},
                                                      {'value': 'multi-tcp', 'label': 'MULTI-TCP'},
                                                      {'value': 'tcp', 'label': 'TCP'}],
                                           'state_key': 'ndi_transport',
                                           'setup': False},
                         'ndi_multicast_subnets': {'type': 'string',
                                                   'label': 'NDI Multicast Subnets',
                                                   'help': 'Comma-separated CIDR subnets.',
                                                   'state_key': 'ndi_multicast_subnets',
                                                   'setup': False},
                         'ndi_pts_mode': {'type': 'enum',
                                          'label': 'NDI Timestamps',
                                          'values': [{'value': 'auto', 'label': 'Auto'},
                                                     {'value': 'timecode', 'label': 'Timecode'},
                                                     {'value': 'timestamp', 'label': 'Timestamp'}],
                                          'state_key': 'ndi_pts_mode',
                                          'setup': False},
                         'ndi_low_bandwidth': {'type': 'boolean',
                                               'label': 'Use NDI Low Bandwidth Stream',
                                               'state_key': 'ndi_low_bandwidth',
                                               'setup': False},
                         'ndi_buffer_ms': {'type': 'integer',
                                           'label': 'NDI Buffer (ms)',
                                           'min': 0,
                                           'state_key': 'ndi_buffer_ms',
                                           'setup': False},
                         'panel_locked': {'type': 'boolean',
                                          'label': 'Lock Front Panel',
                                          'state_key': 'panel_locked',
                                          'setup': False},
                         'device_name': {'type': 'string',
                                         'label': 'Device Name',
                                         'state_key': 'device_name',
                                         'setup': False}},
     'quick_actions': ['refresh', 'restart_source', 'wake_output'],
     'routing': {'destination_child_type': 'screen',
                 'source_child_type': 'source',
                 'destination_param': 'screen',
                 'source_param': 'source',
                 'planes': [{'label': 'Stream', 'route_property': 'source', 'command': 'route_screen'}]},
     'discovery': {'oui': ['d0:c8:57:8', '70:b3:d5:75:d']}}

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._client = None
        self._operation_lock = asyncio.Lock()
        self._rosters = {}
        self._summary = {}
        self._output_modes = {}
        self._detail_errors = {}
        self._poll_count = 0

    async def _create_transport(self, transport_type):
        host = str(self.config.get('host', '')).strip()
        if not host:
            raise ConnectionFaultError('Enter the decoder address.', code='invalid_config')
        if not self.config.get('username') or not self.config.get('password'):
            raise ConnectionFaultError('Enter the decoder username and password.', code='auth_failed')
        port = int(self.config.get('port', 80))
        if not await self._verify_reachable(host, port):
            raise ConnectionError('The decoder is not responding.')
        scheme = 'https' if self.config.get('ssl') else 'http'
        self._client = httpx.AsyncClient(base_url=f'{scheme}://{host}:{port}',
            verify=bool(self.config.get('verify_ssl', False)),
            timeout=httpx.Timeout(8, connect=4), follow_redirects=False)

    async def _post_connect(self):
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._detail_errors.clear()
        await self._login()
        await self._read_summary()
        self._check_identity()

    async def _initial_sync(self):
        self._poll_count = 0
        await self.refresh_children()

    def _link_alive(self):
        return self._client is not None

    async def _close_session(self):
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _liveness_probe(self):
        response = await self._request('POST', '/api/ping', {})
        try:
            _decode(response, '/api/ping')
        except DecoderError:
            return  # Even a documented refusal proves the device answered.

    async def _request(self, verb, path, body=None):
        if self._client is None:
            raise ConnectionError('The decoder session is closed.')
        try:
            return await self._client.request(verb, path, json=body)
        except httpx.TransportError as exc:
            raise ConnectionError('The decoder stopped answering HTTP requests.') from exc

    async def _login(self):
        self._client.cookies.clear()
        digest = hashlib.sha256(str(self.config['password']).encode('utf-8')).hexdigest()
        self.redact_in_log(digest)
        response = await self._request('POST', '/api/user/login',
            {'username': self.config['username'], 'password': digest})
        try:
            _decode(response, 'Login')
        except DecoderError as exc:
            if exc.code in (16, 31, 36, 37):
                raise ConnectionFaultError('The decoder refused the account login.', code='auth_failed') from exc
            raise
        # The device chooses the cookie name (for example sid-A506220808450).
        cookies = list(self._client.cookies.jar)
        if not cookies:
            raise CommandRefusedError('Login succeeded but the decoder did not supply its session cookie.')
        for cookie in cookies:
            self.redact_in_log(cookie.value)

    async def _call(self, path, body=None, *, core=False):
        verb = 'GET' if body is None else 'POST'
        for attempt in range(2):
            try:
                return _decode(await self._request(verb, path, body), path)
            except DecoderError as exc:
                self._check_auth_error(exc, core, attempt)
                if exc.code != 37:
                    raise
            await self._login()
        raise ConnectionError('The decoder session could not be renewed.')

    @staticmethod
    def _check_auth_error(exc, core, attempt):
        fatal = {36}
        if core:
            fatal.add(31)
        if attempt:
            fatal.add(37)
        if exc.code in fatal:
            raise ConnectionFaultError('The decoder account cannot read control status.', code='auth_failed') from exc

    def _check_identity(self):
        model = str(self._summary.get('product-name', ''))
        # Accept both the full product name and its unprefixed family name.
        supported = ('IP to HDMI', 'IP to HDMI 4K', 'IP to AIO 4K')
        if model.removeprefix('Pro Convert ') not in supported:
            raise ConnectionFaultError('This is not a supported Pro Convert IP decoder.', code='invalid_config')
        version = str(self._summary.get('firmware-ver', ''))
        if not _supported_firmware(version):
            raise ConnectionFaultError('This driver requires decoder firmware 1.3 or newer.', code='invalid_config')
        if '4K' not in model:
            for key in ('alpha_mode', 'alpha_color'):
                self.DRIVER_INFO['commands'].pop('set_'+key, None)
                self.DRIVER_INFO['device_settings'].pop(key, None)
                self.DRIVER_INFO['state_variables'].pop(key, None)

    async def _read_summary(self):
        self._summary = await self._call('/api/system/summary', core=True)
        fields = {'product-name':'model', 'serial-number':'serial_number', 'firmware-ver':'firmware_version',
                  'device-name':'device_name', 'mode':'mode', 'hdmi-state':'hdmi_state',
                  'core-temp':'temperature', 'fan-speed':'fan_speed', 'uptime':'uptime'}
        self._copy_state(self._summary, fields)
        profile = self._summary.get('profile', {})
        self.set_state('current_profile', str(profile.get('id', '')))
        self._copy_state(profile, {'mode':'layout', 'audio-idx':'audio_screen'})
        self.set_state('ptz_available', bool(self._summary.get('ptz', {}).get('enabled', False)))
        self._apply_screens(profile.get('streams', []))
        self._mark_selections()

    def _copy_state(self, data, fields):
        for raw, key in fields.items():
            if raw in data and key in self.DRIVER_INFO['state_variables']:
                self.set_state(key, data[raw])

    def _apply_screens(self, streams):
        rows = {}
        for stream in streams:
            ident = str(stream['index'])
            values = _screen_values(stream)
            rows[ident] = (stream, values)
        self._reconcile('screen', rows)

    def _mark_selections(self):
        sources = {str(s['id']) for s in self._summary.get('profile', {}).get('streams', [])}
        current = str(self._summary.get('profile', {}).get('id', ''))
        for ident in self._rosters.get('source', {}):
            self.set_child_state('source', ident, 'selected', ident in sources)
        for ident in self._rosters.get('profile', {}):
            self.set_child_state('profile', ident, 'selected', ident == current)

    def _reconcile(self, kind, rows):
        for ident, (_, values) in rows.items():
            self.register_child(kind, ident, initial_state=values)
            self.set_child_state_batch(kind, ident, values)
        for stale in set(self._rosters.get(kind, {})) - rows.keys():
            self.deregister_child(kind, stale)
        self._rosters[kind] = {ident: raw for ident, (raw, _) in rows.items()}

    async def refresh_children(self):
        async with self._operation_lock:
            await self._read_details()
            await self._read_summary()
        return {kind:len(rows) for kind, rows in self._rosters.items()}

    async def poll(self):
        async with self._operation_lock:
            await self._read_summary()
            self._poll_count += 1
            if self._poll_count % max(1, int(self.config.get('detail_poll_every', 10))) == 0:
                await self._read_details()
                self._mark_selections()

    async def _read_details(self):
        readers = [('/api/source/list?type=all', self._apply_sources),
                   ('/api/profile/list', self._apply_profiles),
                   ('/api/video/config/get', self._apply_video),
                   ('/api/audio/config/get', self._apply_audio),
                   ('/api/video/mode/get', self._apply_output_modes),
                   ('/api/settings/get', self._apply_settings),
                   ('/api/resource/image/list', self._apply_images)]
        for path, apply in readers:
            await self._read_optional(path, apply)
        self.set_state('last_error', '; '.join(self._detail_errors.values()))

    async def _read_optional(self, path, apply):
        try:
            apply(await self._call(path))
            self._detail_errors.pop(path, None)
        except DecoderError as exc:
            if exc.code not in (5, 9, 16, 31):
                raise
            self._detail_errors[path] = str(exc)

    def _apply_sources(self, data):
        rows = {}
        for item in data.get('static', []) + data.get('ndi', []):
            ident = str(item['id'])
            config = item.get('config', {})
            values = {'name':str(config.get('name') or f'Source {ident}'), 'online':True,
                      'source_type':config.get('type', 0),
                      'protocol':_PROTOCOLS.get(item.get('protocol'), 'Unknown')}
            # Preserve only the fields needed for commands; no passwords in feedback.
            rows[ident] = ({'id':item['id'], 'type':config.get('type')}, values)
        self._reconcile('source', rows)

    def _apply_profiles(self, data):
        rows = {}
        for item in data.get('profiles', []):
            values = {'name':str(item.get('name', '')), 'layout':item['mode'],
                      'audio_screen':item.get('audio-idx', 0), 'online':True,
                      'selected':bool(item.get('selected', False))}
            rows[str(item['id'])] = (item, values)
        self._reconcile('profile', rows)

    def _apply_video(self, data):
        self._copy_state(data, {v:k for k,v in _VIDEO_FIELDS.items()})

    def _apply_audio(self, data):
        self._copy_state(data, {v:k for k,v in _AUDIO_FIELDS.items()})

    def _apply_settings(self, data):
        self._copy_state(data.get('ndi', {}), {v:k for k,v in _NDI_FIELDS.items()})
        self._copy_state(data.get('panel', {}), {'menu-knob-locked':'panel_locked'})

    def _apply_output_modes(self, data):
        self._output_modes = {}
        choices = []
        for mode in data.get('modes', []):
            key = _mode_key(mode)
            self._output_modes[key] = mode
            choices.append({'value':key, 'label':_mode_label(mode)})
            if mode.get('curr-mode'):
                self.set_state('output_mode', key)
        self.set_state('output_mode_options', json.dumps(choices))

    def _apply_images(self, data):
        rows = {}
        for item in data.get('data', []):
            ident = f"{item['type']}_{item['id']}"
            values = {'name':f"{item['type']} — {item['id']}", 'image_type':item['type'],
                      'selected':bool(item.get('use', False))}
            rows[ident] = (item, values)
        self._reconcile('image', rows)

    async def set_device_setting(self, key, value):
        return await self.send_command('set_'+key, {'value':value})

    async def send_command(self, command, params=None):
        spec = self.DRIVER_INFO['commands'].get(command)
        if spec is None:
            raise CommandRefusedError('This command is not available on this decoder.')
        values = _validate_params(params or {}, spec.get('params', {}))
        async with self._operation_lock:
            await self._dispatch(command, values)
            # Motion commands must return promptly so a stop can follow immediately.
            if not command.startswith('ptz_'):
                await self._read_details()
                await self._read_summary()

    async def _dispatch(self, command, params):
        setting = command.removeprefix('set_')
        if setting in self.DRIVER_INFO['device_settings']:
            await self._write_setting(setting, params['value'])
            return
        if command.startswith('ptz_'):
            await self._ptz(command, params)
            return
        simple = {'restart_source':'/api/source/restart', 'wake_output':'/api/video/screen/wakeup'}
        if command in simple:
            await self._call(simple[command], {})
            return
        handlers = {'refresh':self._refresh_command, 'set_mode':self._set_mode,
                    'select_source':self._select_source, 'select_profile':self._select_profile,
                    'route_screen':self._route_screen, 'set_profile_screen':self._profile_screen,
                    'set_profile_audio':self._profile_audio, 'create_profile':self._create_profile,
                    'rename_profile':self._rename_profile, 'delete_profile':self._delete_profile,
                    'set_pip':self._set_pip, 'add_url_source':self._add_url_source,
                    'add_ndi_source':self._add_ndi_source, 'update_url_source':self._update_url_source,
                    'rename_source':self._rename_source, 'delete_source':self._delete_source,
                    'set_output_mode':self._set_output_mode, 'select_image':self._select_image,
                    'set_audio_channel_pair':self._audio_pair}
        await handlers[command](params)

    async def _refresh_command(self, params):
        pass  # The common readback refreshes every roster and control.

    async def _write_setting(self, key, value):
        paths = [(_VIDEO_FIELDS, '/api/video/config/set'),
                 (_AUDIO_FIELDS, '/api/audio/config/set'), (_NDI_FIELDS, '/api/settings/ndi/set'),
                 ({'panel_locked':'menu-knob-locked'}, '/api/settings/panel/set'),
                 ({'device_name':'name'}, '/api/system/set-device-name')]
        for fields, path in paths:
            if key in fields:
                await self._call(path, {fields[key]:value})
                return

    async def _set_mode(self, params):
        await self._call('/api/settings/mode/set', {'mode':params['mode']})

    async def _fresh_target(self, kind, ident):
        paths = {'source':('/api/source/list?type=all', self._apply_sources),
                 'profile':('/api/profile/list', self._apply_profiles),
                 'image':('/api/resource/image/list', self._apply_images)}
        path, apply = paths[kind]
        apply(await self._call(path))
        target = self._rosters[kind].get(str(ident))
        if target is None:
            raise CommandRefusedError('The selected item is no longer on the decoder. Refresh the choices.')
        return target

    async def _require_mode(self, mode):
        await self._read_summary()
        if self._summary.get('mode') != mode:
            raise CommandRefusedError('Select the matching configuration mode first.')

    async def _select_source(self, params):
        await self._require_mode(1)
        source = await self._fresh_target('source', params['source'])
        await self._call('/api/source/select', {'id':source['id']})

    async def _select_profile(self, params):
        await self._require_mode(2)
        profile = await self._fresh_target('profile', params['profile'])
        await self._call('/api/profile/select', {'id':profile['id']})

    async def _route_screen(self, params):
        await self._read_summary()
        screen = self._rosters.get('screen', {}).get(str(params['screen']))
        if screen is None:
            raise CommandRefusedError('This window is no longer active.')
        if self._summary['mode'] == 1:
            await self._select_source(params)
            return
        await self._profile_screen({'profile':str(self._summary['profile']['id']),
                                    'screen':screen['index'], 'source':params['source']})

    async def _profile_window(self, params):
        profile = await self._fresh_target('profile', params['profile'])
        if params['screen'] >= len(profile.get('screens', [])):
            raise CommandRefusedError('This profile does not contain that window.')
        return profile

    async def _profile_screen(self, params):
        profile = await self._profile_window(params)
        source = await self._fresh_target('source', params['source'])
        await self._call('/api/profile/screen/select', {'id':profile['id'],
            'screen-index':params['screen'], 'source-id':source['id']})

    async def _profile_audio(self, params):
        profile = await self._profile_window(params)
        await self._call('/api/profile/set', {'id':profile['id'], 'config':{'audio-idx':params['screen']}})

    async def _create_profile(self, params):
        screens = []
        for number in range(1, params['layout']+1):
            source = await self._fresh_target('source', params.get(f'source_{number}', ''))
            screens.append({'id':source['id']})
        await self._call('/api/profile/add', {'name':params['name'], 'mode':params['layout'], 'screens':screens})

    async def _rename_profile(self, params):
        profile = await self._fresh_target('profile', params['profile'])
        await self._call('/api/profile/set', {'id':profile['id'], 'config':{'name':params['name']}})

    async def _delete_profile(self, params):
        profile = await self._fresh_target('profile', params['profile'])
        if profile['id'] == 0:
            raise CommandRefusedError('The default profile cannot be deleted.')
        await self._call('/api/profile/del', {'id':profile['id']})

    async def _set_pip(self, params):
        profile = await self._fresh_target('profile', params['profile'])
        if profile['mode'] != 2:
            raise CommandRefusedError('Select a picture-in-picture profile.')
        pip = {'mode':params['position'], 'scale':params['scale']}
        if params['position'] == 0:
            pip.update(x=params.get('x', 0), y=params.get('y', 0))
            if pip['x']+pip['scale'] > 1 or pip['y']+pip['scale'] > 1:
                raise CommandRefusedError('The inset must fit within the screen.')
        await self._call('/api/profile/set', {'id':profile['id'], 'config':{'pip':pip}})

    async def _add_url_source(self, params):
        url = self._stream_url(params['url'])
        await self._call('/api/source/add', {'type':1, 'name':params['name'], 'url':url})

    def _stream_url(self, value):
        self.redact_in_log(value)
        if urlsplit(value).scheme.lower() not in ('rtsp', 'rtmp', 'http', 'https', 'udp', 'rtp', 'srt', 'rist', 'zixi'):
            raise CommandRefusedError('Enter a supported stream URL.')
        return value

    async def _add_ndi_source(self, params):
        ndi = {key:params[param] for key,param in [('name','ndi_name'), ('url','ndi_url')] if params.get(param)}
        if not ndi:
            raise CommandRefusedError('Enter the NDI source name or URL.')
        if 'url' in ndi:
            self.redact_in_log(ndi['url'])
        await self._call('/api/source/add', {'type':3, 'name':params['name'], 'ndi':ndi})

    async def _saved_source(self, params):
        source = await self._fresh_target('source', params['source'])
        if source['type'] not in (1, 3):
            raise CommandRefusedError('This command applies to saved sources, not automatically discovered NDI sources.')
        return source

    async def _update_url_source(self, params):
        source = await self._saved_source(params)
        if source['type'] != 1:
            raise CommandRefusedError('Select a saved URL source.')
        await self._call('/api/source/set', {'id':source['id'], 'config':{'url':self._stream_url(params['url'])}})

    async def _rename_source(self, params):
        source = await self._saved_source(params)
        await self._call('/api/source/set', {'id':source['id'], 'config':{'name':params['name']}})

    async def _delete_source(self, params):
        source = await self._saved_source(params)
        await self._call('/api/source/del', {'id':source['id']})

    async def _set_output_mode(self, params):
        self._apply_output_modes(await self._call('/api/video/mode/get'))
        mode = self._output_modes.get(params['mode'])
        if mode is None:
            raise CommandRefusedError('Choose an output timing currently reported by the decoder.')
        await self._call('/api/video/mode/set', {key:mode[key] for key in _MODE_FIELDS})

    async def _select_image(self, params):
        item = await self._fresh_target('image', params['image'])
        await self._call('/api/resource/image/set/default', {'type':item['type'], 'id':item['id']})

    async def _audio_pair(self, params):
        audio = await self._call('/api/audio/config/get')
        offset = (params['output_pair']-1)*2
        mapping = list(audio.get('channel-map', []))
        if audio.get('channels', 0) < offset+2 or len(mapping) < offset+2:
            raise CommandRefusedError('Choose a fixed output channel count that includes this pair.')
        first = (params['input_pair']-1)*2
        mapping[offset:offset+2] = [first, first+1]
        await self._call('/api/audio/config/set', {'channel-map':mapping})

    async def _ptz(self, command, params):
        await self._read_summary()
        if not self.get_state('ptz_available'):
            raise CommandRefusedError('The active source does not report PTZ support.')
        routes = {'ptz_move':('move', params), 'ptz_stop':('move', {'pan':0, 'tilt':0}),
                  'ptz_zoom_speed':('zoom/set', params), 'ptz_zoom_position':('zoom/set', params),
                  'ptz_focus_speed':('focus/set', params), 'ptz_focus_position':('focus/set', params),
                  'ptz_autofocus':('focus/auto', {}), 'ptz_recall':('preset/recall', params),
                  'ptz_store':('preset/store', params)}
        path, body = routes[command]
        await self._call('/api/ptz/'+path, body)


_PROTOCOLS = {1:'RTMP', 2:'SRT', 4:'UDP', 8:'RTP', 16:'RTSP', 32:'HTTP/HLS', 64:'NDI', 128:'ZIXI', 256:'RIST'}
_STREAM_STATES = {0:'Waiting', 1:'Connecting', 2:'Connected', 3:'Connection failed', 4:'Connection broken',
                  5:'Rejected', 6:'DNS failure', 7:'TCP failure', 8:'Handshake failure', 9:'Stream error',
                  10:'Authentication failed', 11:'Create failure', 12:'Bind failure'}
_ERRORS = {5:'Not implemented', 7:'Invalid parameters', 9:'Unsupported operation', 11:'Device busy',
           16:'Not found', 26:'Read only', 27:'Out of range', 31:'Permission denied',
           36:'Authentication failed', 37:'Not logged in', 41:'Constraint failed'}
_MODE_FIELDS = ('width', 'height', 'interlaced', 'frame-rate', 'aspect-x', 'aspect-y')


class DecoderError(CommandRefusedError):
    def __init__(self, path, code):
        self.code = code
        super().__init__(f'{path}: {_ERRORS.get(code, "Device error")} ({code}).')


def _decode(response, path):
    if response.status_code in (401, 403):
        raise DecoderError(path, 37 if response.status_code == 401 else 31)
    if response.status_code != 200:
        raise CommandRefusedError(f'The decoder answered HTTP {response.status_code} to {path}.')
    return _decode_body(response, path)


def _decode_body(response, path):
    try:
        data = response.json()
    except ValueError as exc:
        raise CommandRefusedError(f'The decoder returned invalid JSON to {path}.') from exc
    if not _has_status(data):
        raise CommandRefusedError(f'The decoder returned an incomplete reply to {path}.')
    if data['status']:
        raise DecoderError(path, data['status'])
    return data


def _has_status(data):
    return isinstance(data, dict) and type(data.get('status')) is int


def _supported_firmware(version):
    parts = version.lstrip('Vv').split('.')[:2]
    if len(parts) != 2 or not all(x.isdigit() for x in parts):
        return False
    return tuple(map(int, parts)) >= (1, 3)


def _validate_params(params, schema):
    values = {}
    for key, spec in schema.items():
        value = params.get(key, spec.get('default'))
        if value is None:
            if spec.get('required'):
                raise CommandRefusedError(f'Enter {spec["label"]}.')
            continue
        values[key] = _validate_value(value, spec)
    return values


def _validate_value(value, spec):
    kind = spec['type']
    if kind == 'boolean':
        return _boolean(value)
    if kind == 'enum':
        return _enum(value, spec)
    if kind in ('number', 'integer'):
        return _number(value, spec)
    return str(value)


def _boolean(value):
    if str(value).lower() not in ('true', 'false', '0', '1'):
        raise CommandRefusedError('Choose on or off.')
    return str(value).lower() in ('true', '1')


def _enum(value, spec):
    choices = {str(x['value']):x['value'] for x in spec['values']}
    if str(value) not in choices:
        raise CommandRefusedError('Choose a listed value.')
    return choices[str(value)]


def _number(value, spec):
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise CommandRefusedError('Enter a number.') from exc
    _check_range(number, spec)
    if spec['type'] == 'integer':
        if not number.is_integer():
            raise CommandRefusedError('Enter a whole number.')
        return int(number)
    return number


def _check_range(number, spec):
    in_range = spec.get('min', -math.inf) <= number <= spec.get('max', math.inf)
    if not math.isfinite(number) or not in_range:
        raise CommandRefusedError('The value is outside the supported range.')


def _mode_key(mode):
    return ':'.join(str(mode[key]) for key in _MODE_FIELDS)


def _mode_label(mode):
    scan = 'i' if mode['interlaced'] else 'p'
    return f"{mode['width']}×{mode['height']}{scan} {mode['frame-rate']/100:g} Hz ({mode['aspect-x']}:{mode['aspect-y']})"


def _screen_values(stream):
    code = stream.get('state', 0)
    values = {'name':str(stream.get('name') or f"Window {stream['index']+1}"),
              'source':str(stream['id']), 'status_code':code,
              'status':_STREAM_STATES.get(code, f'Status {code}'), 'receiving':code == 2,
              'protocol':str(stream.get('protocol', '')), 'buffer_ms':stream.get('buffer-ms', 0)}
    fields = {'video':{'width':'width', 'height':'height', 'field-rate':'frame_rate', 'codec':'video_codec',
                       'kbps':'video_bitrate', 'drops':'video_drops'},
              'audio':{'codec':'audio_codec', 'num-channels':'audio_channels', 'sample-rate':'audio_sample_rate',
                       'kbps':'audio_bitrate', 'drops':'audio_drops'},
              'extra':{'tally-program':'tally_program', 'tally-preview':'tally_preview'}}
    for section, mapping in fields.items():
        data = stream.get(section, {})
        for raw,key in mapping.items():
            if raw in data:
                values[key] = data[raw]
    return values
