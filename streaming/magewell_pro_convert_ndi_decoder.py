"""Magewell Pro Convert for NDI decoder APIs v1.1 / v1.3.

Python handles MD5/cookie login, device-enumerated sources and display modes,
paired auto/manual settings, and preserving source URLs during preset edits.
The public API documents request/response only, so status is polled. Writes
are read back before reporting success. Model capabilities and returned fields
narrow the controls. Manufacturer documents establish the wire protocol;
the simulator is not hardware qualification.

Source: https://www.magewell.com/api-docs/pro-convert-decoder-api/
"""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
from copy import deepcopy
import hashlib
import ipaddress
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError
from openavc.drivers.command_files import DeviceFile

_FIELDS = {'show_title': ('video', 'show-title'),
 'show_tally': ('video', 'show-tally'),
 'show_vu': ('video', 'show-vu-meter'),
 'show_cross': ('video', 'show-center-cross'),
 'horizontal_flip': ('video', 'h-flip'),
 'vertical_flip': ('video', 'v-flip'),
 'follow_input': ('video', 'follow-input-mode'),
 'reset_source_on_boot': ('video', 'reset-source-on-boot'),
 'meter_mode': ('video', 'vu-meter-mode'),
 'safe_area': ('video', 'safe-area-mode'),
 'ident_mode': ('video', 'ident-mode'),
 'ident_text': ('video', 'ident-text'),
 'switch_mode': ('video', 'switch-mode'),
 'deinterlace': ('video', 'deinterlace-mode'),
 'input_color': ('video', 'in-color-fmt'),
 'aspect_conversion': ('video', 'ar-convert-mode'),
 'alpha_mode': ('video', 'alpha-disp-mode'),
 'output_color': ('format', 'color-format'),
 'output_range': ('format', 'quant-range'),
 'hdmi_enabled': ('hdmi', 'enabled'),
 'audio_gain': ('audio', 'gain'),
 'audio_sample_rate': ('audio', 'sample-rate'),
 'audio_channel_count': ('audio', 'channels'),
 'audio_conversion': ('audio', 'convert-mode'),
 'audio_check_pts': ('audio', 'check-pts'),
 'discovery_enabled': ('ndi', 'enable-discovery'),
 'low_bandwidth': ('ndi', 'low-bandwidth'),
 'allow_multicast': ('ndi', 'enable-mcast'),
 'allow_rudp': ('ndi', 'enable-rudp'),
 'allow_tcp': ('ndi', 'enable-tcp'),
 'allow_udp': ('ndi', 'enable-udp'),
 'ignore_hx_pts': ('ndi', 'ignore-ndi-hx-video-pts'),
 'prefer_timecode': ('ndi', 'use-timecode-first'),
 'discovery_servers': ('ndi', 'discovery-server'),
 'multicast_subnets': ('ndi', 'mcast-subnets'),
 'ndi_groups': ('ndi', 'group-name'),
 'playback_buffer': ('playback', 'buffer-duration')}

_SUMMARY = {'device': {'name': 'device_name',
            'serial-no': 'serial_number',
            'fw-version': 'firmware_version',
            'hw-revision': 'hardware_revision',
            'output-state': 'output_status',
            'cpu-usage': 'cpu_usage',
            'memory-usage': 'memory_usage',
            'core-temp': 'temperature',
            'up-time': 'uptime',
            'fan-rpm': 'fan_speed',
            'board-id': 'rotary_id'},
 'ethernet': {'state': 'ethernet_link',
              'mac-addr': 'mac_address',
              'ip-addr': 'ip_address',
              'tx-speed-kbps': 'network_tx',
              'rx-speed-kbps': 'network_rx'},
 'ndi': {'name': 'receiving_name',
         'connected': 'stream_connected',
         'tally-preview': 'tally_preview',
         'tally-program': 'tally_program',
         'audio-drop-frames': 'audio_dropped',
         'video-drop-frames': 'video_dropped',
         'video-bit-rate': 'video_bitrate',
         'audio-bit-rate': 'audio_bitrate'}}

_SIGNAL = {'video-info': {'codec': 'video_codec',
                'width': 'video_width',
                'height': 'video_height',
                'scan': 'video_scan',
                'field-rate': 'video_rate',
                'color-depth': 'video_depth',
                'color-format': 'video_color',
                'sampling': 'video_sampling'},
 'audio-info': {'codec': 'audio_codec',
                'num-channels': 'audio_channels',
                'sample-rate': 'audio_rate',
                'bit-count': 'audio_depth'}}

class MagewellProConvertNDIDecoderDriver(BaseDriver):
    DRIVER_INFO = {'id': 'magewell_pro_convert_ndi_decoder',
     'name': 'Magewell Pro Convert NDI Decoder',
     'manufacturer': 'Magewell',
     'category': 'streaming',
     'version': '1.0.1',
     'author': 'OpenAVC',
     'transport': 'http',
     'ports': [80, 443],
     'protocols': ['http'],
     'description': 'Controls Pro Convert for NDI decoders with named source routing, '
                    'display-supported output modes, audio mapping, presets and signal diagnostics.',
     'source_url': 'https://www.magewell.com/api-docs/pro-convert-decoder-api/',
     'simulated': True,
     'verified': False,
     'min_platform_version': '0.37.2',
     'tags': ['ndi', 'decoder', 'hdmi', 'sdi', 'srt'],
     'compatible_models': [{'manufacturer': 'Magewell',
                            'models': ['Pro Convert for NDI to HDMI',
                                       'Pro Convert for NDI to HDMI 4K',
                                       'Pro Convert for NDI to AIO',
                                       'Pro Convert for NDI to SDI',
                                       'Pro Convert for NDI to HDMI Module',
                                       'Pro Convert for NDI to HDMI 4K Module',
                                       'Pro Convert for NDI to AIO Module'],
                            'confidence': 'untested',
                            'notes': 'Document-derived from decoder APIs v1.1 and v1.3; not hardware '
                                     'qualified. Newer Pro Convert IP decoders use a separate '
                                     'driver.'}],
     'help': {'overview': 'Select named NDI sources and saved stream presets, choose a '
                          'display-supported resolution, map audio channels and monitor decoded '
                          'signal. Settings and commands read back what the decoder actually reports.',
              'setup': 'Enter the decoder’s IP address and a local administrator account. The username '
                       'is case-sensitive. Select its model: HDMI 4K uses the older login; other '
                       'models use the current firmware login. For older 1.1 firmware on other models, '
                       'enable Legacy Login in Advanced. A connected unit reports its actual model. '
                       'Match HTTP/HTTPS and the web port to the unit. Use its web interface for '
                       'firmware, network addressing and user accounts.',
              'connection': 'Live signal and selection refresh every 3 seconds; settings and source '
                            'lists every 30 seconds by default. Refresh Status updates all lists. '
                            'Sources distinguish discovered NDI from saved presets; a preset’s '
                            'presence does not prove that its stream is online. Discovery offers '
                            'Magewell devices as possible matches. Use this driver only for the '
                            'original Pro Convert for NDI decoder family.'},
     'default_config': {'port': 80,
                        'ssl': False,
                        'verify_ssl': False,
                        'username': 'Admin',
                        'password': '',
                        'model': 'Pro Convert for NDI to HDMI',
                        'legacy_login': False,
                        'poll_interval': 3,
                        'detail_poll_every': 10},
     'config_schema': {'host': {'type': 'string', 'label': 'IP Address', 'required': True},
                       'username': {'type': 'string',
                                    'label': 'Username',
                                    'required': True,
                                    'default': 'Admin'},
                       'password': {'type': 'string',
                                    'label': 'Password',
                                    'required': True,
                                    'secret': True},
                       'model': {'type': 'enum',
                                 'label': 'Model',
                                 'values': ['Pro Convert for NDI to HDMI',
                                            'Pro Convert for NDI to HDMI 4K',
                                            'Pro Convert for NDI to AIO',
                                            'Pro Convert for NDI to SDI',
                                            'Pro Convert for NDI to HDMI Module',
                                            'Pro Convert for NDI to HDMI 4K Module',
                                            'Pro Convert for NDI to AIO Module'],
                                 'default': 'Pro Convert for NDI to HDMI',
                                 'learned_from': 'model',
                                 'help': 'Choose the hardware model for login and simulation; actual '
                                         'equipment corrects this when connected.'},
                       'port': {'type': 'integer',
                                'label': 'Web Port',
                                'min': 1,
                                'max': 65535,
                                'default': 80,
                                'advanced': True},
                       'ssl': {'type': 'boolean',
                               'label': 'Use HTTPS',
                               'default': False,
                               'advanced': True},
                       'verify_ssl': {'type': 'boolean',
                                      'label': 'Verify HTTPS Certificate',
                                      'default': False,
                                      'advanced': True},
                       'legacy_login': {'type': 'boolean',
                                        'label': 'Legacy Login (Firmware 1.1)',
                                        'default': False,
                                        'advanced': True,
                                        'help': 'Enable for 1.1 firmware on non-4K models. HDMI 4K '
                                                'uses the documented older login automatically. No '
                                                'password fallback is attempted.'},
                       'poll_interval': {'type': 'number',
                                         'label': 'Status Poll Interval (seconds)',
                                         'default': 3,
                                         'min': 1,
                                         'max': 60,
                                         'advanced': True},
                       'detail_poll_every': {'type': 'integer',
                                             'label': 'Status Polls Between Setup Refreshes',
                                             'default': 10,
                                             'min': 1,
                                             'max': 100,
                                             'advanced': True}},
     'state_variables': {'show_title': {'type': 'boolean',
                                        'label': 'Source Overlay',
                                        'help': 'Show the source name and resolution on the decoded '
                                                'picture.',
                                        'control': True},
                         'show_tally': {'type': 'boolean',
                                        'label': 'Tally Overlay',
                                        'help': 'Show the selected NDI source’s tally on the picture.',
                                        'control': True},
                         'show_vu': {'type': 'boolean',
                                     'label': 'Audio Meter Overlay',
                                     'help': 'Show audio level meters on the decoded picture.',
                                     'control': True},
                         'show_cross': {'type': 'boolean',
                                        'label': 'Center Cross',
                                        'help': 'Show a center cross for alignment.',
                                        'control': True},
                         'horizontal_flip': {'type': 'boolean',
                                             'label': 'Horizontal Flip',
                                             'help': 'Mirror the decoded picture horizontally.',
                                             'control': True},
                         'vertical_flip': {'type': 'boolean',
                                           'label': 'Vertical Flip',
                                           'help': 'Turn the decoded picture upside down.',
                                           'control': True},
                         'follow_input': {'type': 'boolean',
                                          'label': 'Follow Input Resolution',
                                          'help': 'Match the decoded source resolution. Disable to '
                                                  'select a fixed display mode.',
                                          'control': True},
                         'reset_source_on_boot': {'type': 'boolean',
                                                  'label': 'Reset Source on Boot',
                                                  'help': 'Clear the selected source on boot and '
                                                          'return to automatic NDI discovery (firmware '
                                                          '1.3).',
                                                  'control': True},
                         'meter_mode': {'type': 'enum',
                                        'label': 'Audio Meter Scale',
                                        'help': 'Choose the meter scale before or after output gain.',
                                        'control': True,
                                        'values': ['none',
                                                   'dbu',
                                                   'dbvu',
                                                   'dbfs',
                                                   'post-gain-dbu',
                                                   'post-gain-dbvu',
                                                   'post-gain-dbfs']},
                         'safe_area': {'type': 'enum',
                                       'label': 'Safe Area',
                                       'help': 'Overlay a framing guide.',
                                       'control': True,
                                       'values': ['none', '4:3', '80%', 'square']},
                         'ident_mode': {'type': 'enum',
                                        'label': 'Identification Overlay',
                                        'help': 'Choose a custom label or the device name to identify '
                                                'this output.',
                                        'control': True,
                                        'values': ['none', 'ident-text', 'device-name']},
                         'ident_text': {'type': 'string',
                                        'label': 'Identification Text',
                                        'help': 'Custom overlay text: up to 127 UTF-8 bytes on '
                                                'firmware 1.3; older firmware accepts 1–32 letters, '
                                                'digits, spaces, _, - and +.',
                                        'control': True},
                         'switch_mode': {'type': 'enum',
                                         'label': 'Source Switching Picture',
                                         'help': 'Choose what viewers see while a different source '
                                                 'connects.',
                                         'control': True,
                                         'values': ['blank', 'keep-last']},
                         'deinterlace': {'type': 'enum',
                                         'label': 'Deinterlace Mode',
                                         'help': 'Convert interlaced sources to a progressive picture.',
                                         'control': True,
                                         'values': ['bob', 'weave']},
                         'input_color': {'type': 'enum',
                                         'label': 'Input Color Space',
                                         'help': 'Automatic chooses from the source. Manual choices '
                                                 'follow the firmware API.',
                                         'control': True,
                                         'values': ['auto',
                                                    'rgb',
                                                    'bt.601',
                                                    'bt.709',
                                                    'bt.2020',
                                                    'bt.2020c']},
                         'aspect_conversion': {'type': 'enum',
                                               'label': 'Aspect Conversion',
                                               'help': 'Fit preserves the whole picture, Fill '
                                                       'stretches it, and Zoom crops it.',
                                               'control': True,
                                               'values': ['windowbox', 'full', 'zoom']},
                         'alpha_mode': {'type': 'enum',
                                        'label': 'Alpha Display',
                                        'help': 'Choose the alpha channel background when the decoder '
                                                'reports this capability.',
                                        'control': True,
                                        'values': ['alpha-only',
                                                   'alpha-blend-white',
                                                   'alpha-blend-black',
                                                   'alpha-blend-checkerboard']},
                         'output_color': {'type': 'enum',
                                          'label': 'Output Color Format',
                                          'help': 'Set the output color format.',
                                          'control': True,
                                          'values': ['rgb', 'yuv444', 'yuv422']},
                         'output_range': {'type': 'enum',
                                          'label': 'RGB Output Range',
                                          'help': 'Applies only when Output Color Format is RGB.',
                                          'control': True,
                                          'values': ['limited', 'full']},
                         'hdmi_enabled': {'type': 'boolean',
                                          'label': 'HDMI Output',
                                          'help': 'Enable or disable the HDMI output. This does not '
                                                  'power down the decoder.',
                                          'control': True},
                         'audio_gain': {'type': 'number',
                                        'label': 'Audio Gain',
                                        'help': 'Adjust decoded audio in dB; 0 dB is unchanged.',
                                        'control': True,
                                        'min': -100,
                                        'max': 20,
                                        'unit': 'dB',
                                        'step': 1},
                         'audio_sample_rate': {'type': 'enum',
                                               'label': 'Output Sample Rate',
                                               'help': 'Follow the source or use a fixed output sample '
                                                       'rate.',
                                               'control': True,
                                               'values': [0, 32000, 44100, 48000, 88200, 96000]},
                         'audio_channel_count': {'type': 'enum',
                                                 'label': 'Output Audio Channels',
                                                 'help': 'Follow the source or select 2, 4 or 8 '
                                                         'channels. A fixed count is required for '
                                                         'channel mapping.',
                                                 'control': True,
                                                 'values': [0, 2, 4, 8]},
                         'audio_conversion': {'type': 'enum',
                                              'label': 'Audio Channel Order',
                                              'help': 'Choose the device’s SMPTE or EBU channel '
                                                      'conversion.',
                                              'control': True,
                                              'values': ['smpte', 'ebu']},
                         'audio_check_pts': {'type': 'boolean',
                                             'label': 'Check Audio Timestamps',
                                             'help': 'Use the source’s presentation timestamps for '
                                                     'audio.',
                                             'control': True},
                         'discovery_enabled': {'type': 'boolean',
                                               'label': 'NDI Discovery Server',
                                               'help': 'Use the configured discovery servers. Enter '
                                                       'their addresses before enabling.',
                                               'control': True},
                         'low_bandwidth': {'type': 'boolean',
                                           'label': 'Low Bandwidth NDI',
                                           'help': 'Request the NDI source’s low bandwidth stream.',
                                           'control': True},
                         'allow_multicast': {'type': 'boolean',
                                             'label': 'Allow NDI Multicast',
                                             'help': 'Allow reception over NDI multicast.',
                                             'control': True},
                         'allow_rudp': {'type': 'boolean',
                                        'label': 'Allow NDI Reliable UDP',
                                        'help': 'Allow reception over NDI reliable UDP.',
                                        'control': True},
                         'allow_tcp': {'type': 'boolean',
                                       'label': 'Allow NDI Multi-TCP',
                                       'help': 'Allow reception over NDI multi-connection TCP.',
                                       'control': True},
                         'allow_udp': {'type': 'boolean',
                                       'label': 'Allow NDI UDP',
                                       'help': 'Allow reception over NDI UDP.',
                                       'control': True},
                         'ignore_hx_pts': {'type': 'boolean',
                                           'label': 'Ignore NDI HX Video Timestamps',
                                           'help': 'Use this only when the source’s video timestamps '
                                                   'cause playback problems.',
                                           'control': True},
                         'prefer_timecode': {'type': 'boolean',
                                             'label': 'Prefer NDI Timecode',
                                             'help': 'Prefer source timecode for timing.',
                                             'control': True},
                         'discovery_servers': {'type': 'string',
                                               'label': 'NDI Discovery Servers',
                                               'help': 'Comma-separated IPv4 addresses. Keep at least '
                                                       'one while discovery-server reception is '
                                                       'enabled.',
                                               'control': True},
                         'multicast_subnets': {'type': 'string',
                                               'label': 'NDI Multicast Subnets',
                                               'help': 'Comma-separated IPv4 CIDR subnets. Empty '
                                                       'allows every subnet.',
                                               'control': True},
                         'ndi_groups': {'type': 'string',
                                        'label': 'NDI Groups',
                                        'help': 'NDI group name list; public is the usual default.',
                                        'control': True},
                         'playback_buffer': {'type': 'integer',
                                             'label': 'Playback Buffer',
                                             'help': 'Playback buffer in milliseconds. Saved URLs can '
                                                     'override it with their own buffer; use Set '
                                                     'Preset Buffer for those.',
                                             'control': True,
                                             'min': 20,
                                             'max': 120,
                                             'unit': 'ms',
                                             'step': 1},
                         'device_name': {'type': 'string',
                                         'label': 'Device Name',
                                         'cloud_priority': 'high'},
                         'serial_number': {'type': 'string',
                                           'label': 'Serial Number',
                                           'cloud_priority': 'high'},
                         'firmware_version': {'type': 'string',
                                              'label': 'Firmware Version',
                                              'cloud_priority': 'high'},
                         'hardware_revision': {'type': 'string',
                                               'label': 'Hardware Revision',
                                               'cloud_priority': 'high'},
                         'output_status': {'type': 'string',
                                           'label': 'Output Status',
                                           'cloud_priority': 'high'},
                         'cpu_usage': {'type': 'number',
                                       'label': 'Cpu Usage',
                                       'cloud_priority': 'low',
                                       'unit': '%'},
                         'memory_usage': {'type': 'number',
                                          'label': 'Memory Usage',
                                          'cloud_priority': 'low',
                                          'unit': '%'},
                         'temperature': {'type': 'number',
                                         'label': 'Temperature',
                                         'cloud_priority': 'low',
                                         'unit': '°C'},
                         'uptime': {'type': 'integer',
                                    'label': 'Uptime',
                                    'cloud_priority': 'low',
                                    'unit': 's'},
                         'fan_speed': {'type': 'integer',
                                       'label': 'Fan Speed',
                                       'cloud_priority': 'low',
                                       'unit': 'rpm'},
                         'rotary_id': {'type': 'integer',
                                       'label': 'Rotary Id',
                                       'cloud_priority': 'low'},
                         'ethernet_link': {'type': 'string',
                                           'label': 'Ethernet Link',
                                           'cloud_priority': 'high'},
                         'mac_address': {'type': 'string',
                                         'label': 'Mac Address',
                                         'cloud_priority': 'high'},
                         'ip_address': {'type': 'string',
                                        'label': 'Ip Address',
                                        'cloud_priority': 'high'},
                         'network_tx': {'type': 'number',
                                        'label': 'Network Tx',
                                        'cloud_priority': 'low',
                                        'unit': 'kbps'},
                         'network_rx': {'type': 'number',
                                        'label': 'Network Rx',
                                        'cloud_priority': 'low',
                                        'unit': 'kbps'},
                         'receiving_name': {'type': 'string',
                                            'label': 'Receiving Name',
                                            'cloud_priority': 'high'},
                         'stream_connected': {'type': 'boolean',
                                              'label': 'Stream Connected',
                                              'cloud_priority': 'high'},
                         'tally_preview': {'type': 'boolean',
                                           'label': 'Tally Preview',
                                           'cloud_priority': 'high'},
                         'tally_program': {'type': 'boolean',
                                           'label': 'Tally Program',
                                           'cloud_priority': 'high'},
                         'audio_dropped': {'type': 'integer',
                                           'label': 'Audio Dropped',
                                           'cloud_priority': 'low',
                                           'unit': 'frames/s'},
                         'video_dropped': {'type': 'integer',
                                           'label': 'Video Dropped',
                                           'cloud_priority': 'low',
                                           'unit': 'frames/s'},
                         'video_bitrate': {'type': 'number',
                                           'label': 'Video Bitrate',
                                           'cloud_priority': 'low',
                                           'unit': 'kbps'},
                         'audio_bitrate': {'type': 'number',
                                           'label': 'Audio Bitrate',
                                           'cloud_priority': 'low',
                                           'unit': 'kbps'},
                         'video_codec': {'type': 'string',
                                         'label': 'Video Codec',
                                         'cloud_priority': 'high'},
                         'video_width': {'type': 'integer',
                                         'label': 'Video Width',
                                         'cloud_priority': 'low',
                                         'unit': 'px'},
                         'video_height': {'type': 'integer',
                                          'label': 'Video Height',
                                          'cloud_priority': 'low',
                                          'unit': 'px'},
                         'video_scan': {'type': 'string',
                                        'label': 'Video Scan',
                                        'cloud_priority': 'high'},
                         'video_rate': {'type': 'number',
                                        'label': 'Video Rate',
                                        'cloud_priority': 'low',
                                        'unit': 'Hz'},
                         'video_depth': {'type': 'integer',
                                         'label': 'Video Depth',
                                         'cloud_priority': 'low',
                                         'unit': 'bits'},
                         'video_color': {'type': 'string',
                                         'label': 'Video Color',
                                         'cloud_priority': 'high'},
                         'video_sampling': {'type': 'string',
                                            'label': 'Video Sampling',
                                            'cloud_priority': 'high'},
                         'audio_codec': {'type': 'string',
                                         'label': 'Audio Codec',
                                         'cloud_priority': 'high'},
                         'audio_channels': {'type': 'integer',
                                            'label': 'Audio Channels',
                                            'cloud_priority': 'low'},
                         'audio_rate': {'type': 'integer',
                                        'label': 'Audio Rate',
                                        'cloud_priority': 'low',
                                        'unit': 'Hz'},
                         'audio_depth': {'type': 'integer',
                                         'label': 'Audio Depth',
                                         'cloud_priority': 'low',
                                         'unit': 'bits'},
                         'model': {'type': 'string', 'label': 'Reported Model'},
                         'source': {'type': 'string',
                                    'label': 'Selected Source',
                                    'control': True,
                                    'cloud_priority': 'high'},
                         'source_name': {'type': 'string',
                                         'label': 'Selected Source Name',
                                         'cloud_priority': 'high'},
                         'output_mode': {'type': 'string', 'label': 'Output Mode', 'control': True},
                         'output_mode_name': {'type': 'string', 'label': 'Output Resolution'},
                         'output_edid_available': {'type': 'boolean', 'label': 'Output EDID Available'},
                         'last_error': {'type': 'string',
                                        'label': 'Last Error',
                                        'cloud_priority': 'high'}},
     'device_settings': {'show_title': {'type': 'boolean',
                                        'label': 'Source Overlay',
                                        'help': 'Show the source name and resolution on the decoded '
                                                'picture.',
                                        'state_key': 'show_title',
                                        'setup': False},
                         'show_tally': {'type': 'boolean',
                                        'label': 'Tally Overlay',
                                        'help': 'Show the selected NDI source’s tally on the picture.',
                                        'state_key': 'show_tally',
                                        'setup': False},
                         'show_vu': {'type': 'boolean',
                                     'label': 'Audio Meter Overlay',
                                     'help': 'Show audio level meters on the decoded picture.',
                                     'state_key': 'show_vu',
                                     'setup': False},
                         'show_cross': {'type': 'boolean',
                                        'label': 'Center Cross',
                                        'help': 'Show a center cross for alignment.',
                                        'state_key': 'show_cross',
                                        'setup': False},
                         'horizontal_flip': {'type': 'boolean',
                                             'label': 'Horizontal Flip',
                                             'help': 'Mirror the decoded picture horizontally.',
                                             'state_key': 'horizontal_flip',
                                             'setup': False},
                         'vertical_flip': {'type': 'boolean',
                                           'label': 'Vertical Flip',
                                           'help': 'Turn the decoded picture upside down.',
                                           'state_key': 'vertical_flip',
                                           'setup': False},
                         'follow_input': {'type': 'boolean',
                                          'label': 'Follow Input Resolution',
                                          'help': 'Match the decoded source resolution. Disable to '
                                                  'select a fixed display mode.',
                                          'state_key': 'follow_input',
                                          'setup': False},
                         'reset_source_on_boot': {'type': 'boolean',
                                                  'label': 'Reset Source on Boot',
                                                  'help': 'Clear the selected source on boot and '
                                                          'return to automatic NDI discovery (firmware '
                                                          '1.3).',
                                                  'state_key': 'reset_source_on_boot',
                                                  'setup': False},
                         'meter_mode': {'type': 'enum',
                                        'label': 'Audio Meter Scale',
                                        'help': 'Choose the meter scale before or after output gain.',
                                        'state_key': 'meter_mode',
                                        'setup': False,
                                        'values': [{'value': 'none', 'label': 'None'},
                                                   {'value': 'dbu', 'label': 'dBu'},
                                                   {'value': 'dbvu', 'label': 'VU'},
                                                   {'value': 'dbfs', 'label': 'dBFS'},
                                                   {'value': 'post-gain-dbu',
                                                    'label': 'dBu after gain'},
                                                   {'value': 'post-gain-dbvu',
                                                    'label': 'VU after gain'},
                                                   {'value': 'post-gain-dbfs',
                                                    'label': 'dBFS after gain'}]},
                         'safe_area': {'type': 'enum',
                                       'label': 'Safe Area',
                                       'help': 'Overlay a framing guide.',
                                       'state_key': 'safe_area',
                                       'setup': False,
                                       'values': [{'value': 'none', 'label': 'None'},
                                                  {'value': '4:3', 'label': '4:3'},
                                                  {'value': '80%', 'label': '80%'},
                                                  {'value': 'square', 'label': 'Square'}]},
                         'ident_mode': {'type': 'enum',
                                        'label': 'Identification Overlay',
                                        'help': 'Choose a custom label or the device name to identify '
                                                'this output.',
                                        'state_key': 'ident_mode',
                                        'setup': False,
                                        'values': [{'value': 'none', 'label': 'Off'},
                                                   {'value': 'ident-text', 'label': 'Custom label'},
                                                   {'value': 'device-name', 'label': 'Device name'}]},
                         'ident_text': {'type': 'string',
                                        'label': 'Identification Text',
                                        'help': 'Custom overlay text: up to 127 UTF-8 bytes on '
                                                'firmware 1.3; older firmware accepts 1–32 letters, '
                                                'digits, spaces, _, - and +.',
                                        'state_key': 'ident_text',
                                        'setup': False},
                         'switch_mode': {'type': 'enum',
                                         'label': 'Source Switching Picture',
                                         'help': 'Choose what viewers see while a different source '
                                                 'connects.',
                                         'state_key': 'switch_mode',
                                         'setup': False,
                                         'values': [{'value': 'blank', 'label': 'Black'},
                                                    {'value': 'keep-last',
                                                     'label': 'Hold last picture'}]},
                         'deinterlace': {'type': 'enum',
                                         'label': 'Deinterlace Mode',
                                         'help': 'Convert interlaced sources to a progressive picture.',
                                         'state_key': 'deinterlace',
                                         'setup': False,
                                         'values': [{'value': 'bob', 'label': 'Bob'},
                                                    {'value': 'weave', 'label': 'Weave'}]},
                         'input_color': {'type': 'enum',
                                         'label': 'Input Color Space',
                                         'help': 'Automatic chooses from the source. Manual choices '
                                                 'follow the firmware API.',
                                         'state_key': 'input_color',
                                         'setup': False,
                                         'values': [{'value': 'auto', 'label': 'Automatic'},
                                                    {'value': 'rgb', 'label': 'RGB'},
                                                    {'value': 'bt.601', 'label': 'BT.601'},
                                                    {'value': 'bt.709', 'label': 'BT.709'},
                                                    {'value': 'bt.2020', 'label': 'BT.2020'},
                                                    {'value': 'bt.2020c',
                                                     'label': 'BT.2020 constant luminance'}]},
                         'aspect_conversion': {'type': 'enum',
                                               'label': 'Aspect Conversion',
                                               'help': 'Fit preserves the whole picture, Fill '
                                                       'stretches it, and Zoom crops it.',
                                               'state_key': 'aspect_conversion',
                                               'setup': False,
                                               'values': [{'value': 'windowbox', 'label': 'Fit'},
                                                          {'value': 'full', 'label': 'Fill'},
                                                          {'value': 'zoom', 'label': 'Zoom / crop'}]},
                         'alpha_mode': {'type': 'enum',
                                        'label': 'Alpha Display',
                                        'help': 'Choose the alpha channel background when the decoder '
                                                'reports this capability.',
                                        'state_key': 'alpha_mode',
                                        'setup': False,
                                        'values': [{'value': 'alpha-only', 'label': 'Alpha only'},
                                                   {'value': 'alpha-blend-white', 'label': 'White'},
                                                   {'value': 'alpha-blend-black', 'label': 'Black'},
                                                   {'value': 'alpha-blend-checkerboard',
                                                    'label': 'Checkerboard'}]},
                         'output_color': {'type': 'enum',
                                          'label': 'Output Color Format',
                                          'help': 'Set the output color format.',
                                          'state_key': 'output_color',
                                          'setup': False,
                                          'values': [{'value': 'rgb', 'label': 'RGB'},
                                                     {'value': 'yuv444', 'label': 'YCbCr 4:4:4'},
                                                     {'value': 'yuv422', 'label': 'YCbCr 4:2:2'}]},
                         'output_range': {'type': 'enum',
                                          'label': 'RGB Output Range',
                                          'help': 'Applies only when Output Color Format is RGB.',
                                          'state_key': 'output_range',
                                          'setup': False,
                                          'values': [{'value': 'limited', 'label': 'Limited'},
                                                     {'value': 'full', 'label': 'Full'}]},
                         'hdmi_enabled': {'type': 'boolean',
                                          'label': 'HDMI Output',
                                          'help': 'Enable or disable the HDMI output. This does not '
                                                  'power down the decoder.',
                                          'state_key': 'hdmi_enabled',
                                          'setup': False},
                         'audio_gain': {'type': 'number',
                                        'label': 'Audio Gain',
                                        'help': 'Adjust decoded audio in dB; 0 dB is unchanged.',
                                        'state_key': 'audio_gain',
                                        'setup': False,
                                        'min': -100,
                                        'max': 20},
                         'audio_sample_rate': {'type': 'enum',
                                               'label': 'Output Sample Rate',
                                               'help': 'Follow the source or use a fixed output sample '
                                                       'rate.',
                                               'state_key': 'audio_sample_rate',
                                               'setup': False,
                                               'values': [{'value': 0, 'label': 'Follow source'},
                                                          {'value': 32000, 'label': '32 kHz'},
                                                          {'value': 44100, 'label': '44.1 kHz'},
                                                          {'value': 48000, 'label': '48 kHz'},
                                                          {'value': 88200, 'label': '88.2 kHz'},
                                                          {'value': 96000, 'label': '96 kHz'}]},
                         'audio_channel_count': {'type': 'enum',
                                                 'label': 'Output Audio Channels',
                                                 'help': 'Follow the source or select 2, 4 or 8 '
                                                         'channels. A fixed count is required for '
                                                         'channel mapping.',
                                                 'state_key': 'audio_channel_count',
                                                 'setup': False,
                                                 'values': [{'value': 0, 'label': 'Follow source'},
                                                            {'value': 2, 'label': '2 channels'},
                                                            {'value': 4, 'label': '4 channels'},
                                                            {'value': 8, 'label': '8 channels'}]},
                         'audio_conversion': {'type': 'enum',
                                              'label': 'Audio Channel Order',
                                              'help': 'Choose the device’s SMPTE or EBU channel '
                                                      'conversion.',
                                              'state_key': 'audio_conversion',
                                              'setup': False,
                                              'values': [{'value': 'smpte', 'label': 'SMPTE'},
                                                         {'value': 'ebu', 'label': 'EBU'}]},
                         'audio_check_pts': {'type': 'boolean',
                                             'label': 'Check Audio Timestamps',
                                             'help': 'Use the source’s presentation timestamps for '
                                                     'audio.',
                                             'state_key': 'audio_check_pts',
                                             'setup': False},
                         'discovery_enabled': {'type': 'boolean',
                                               'label': 'NDI Discovery Server',
                                               'help': 'Use the configured discovery servers. Enter '
                                                       'their addresses before enabling.',
                                               'state_key': 'discovery_enabled',
                                               'setup': False},
                         'low_bandwidth': {'type': 'boolean',
                                           'label': 'Low Bandwidth NDI',
                                           'help': 'Request the NDI source’s low bandwidth stream.',
                                           'state_key': 'low_bandwidth',
                                           'setup': False},
                         'allow_multicast': {'type': 'boolean',
                                             'label': 'Allow NDI Multicast',
                                             'help': 'Allow reception over NDI multicast.',
                                             'state_key': 'allow_multicast',
                                             'setup': False},
                         'allow_rudp': {'type': 'boolean',
                                        'label': 'Allow NDI Reliable UDP',
                                        'help': 'Allow reception over NDI reliable UDP.',
                                        'state_key': 'allow_rudp',
                                        'setup': False},
                         'allow_tcp': {'type': 'boolean',
                                       'label': 'Allow NDI Multi-TCP',
                                       'help': 'Allow reception over NDI multi-connection TCP.',
                                       'state_key': 'allow_tcp',
                                       'setup': False},
                         'allow_udp': {'type': 'boolean',
                                       'label': 'Allow NDI UDP',
                                       'help': 'Allow reception over NDI UDP.',
                                       'state_key': 'allow_udp',
                                       'setup': False},
                         'ignore_hx_pts': {'type': 'boolean',
                                           'label': 'Ignore NDI HX Video Timestamps',
                                           'help': 'Use this only when the source’s video timestamps '
                                                   'cause playback problems.',
                                           'state_key': 'ignore_hx_pts',
                                           'setup': False},
                         'prefer_timecode': {'type': 'boolean',
                                             'label': 'Prefer NDI Timecode',
                                             'help': 'Prefer source timecode for timing.',
                                             'state_key': 'prefer_timecode',
                                             'setup': False},
                         'discovery_servers': {'type': 'string',
                                               'label': 'NDI Discovery Servers',
                                               'help': 'Comma-separated IPv4 addresses. Keep at least '
                                                       'one while discovery-server reception is '
                                                       'enabled.',
                                               'state_key': 'discovery_servers',
                                               'setup': False},
                         'multicast_subnets': {'type': 'string',
                                               'label': 'NDI Multicast Subnets',
                                               'help': 'Comma-separated IPv4 CIDR subnets. Empty '
                                                       'allows every subnet.',
                                               'state_key': 'multicast_subnets',
                                               'setup': False},
                         'ndi_groups': {'type': 'string',
                                        'label': 'NDI Groups',
                                        'help': 'NDI group name list; public is the usual default.',
                                        'state_key': 'ndi_groups',
                                        'setup': False},
                         'playback_buffer': {'type': 'integer',
                                             'label': 'Playback Buffer',
                                             'help': 'Playback buffer in milliseconds. Saved URLs can '
                                                     'override it with their own buffer; use Set '
                                                     'Preset Buffer for those.',
                                             'state_key': 'playback_buffer',
                                             'setup': False,
                                             'min': 20,
                                             'max': 120}},
     'commands': {'set_show_title': {'label': 'Set Source Overlay',
                                     'help': 'Show the source name and resolution on the decoded '
                                             'picture.',
                                     'params': {'value': {'type': 'boolean',
                                                          'label': 'Source Overlay',
                                                          'required': True}},
                                     'sets': {'show_title': '{value}'}},
                  'set_show_tally': {'label': 'Set Tally Overlay',
                                     'help': 'Show the selected NDI source’s tally on the picture.',
                                     'params': {'value': {'type': 'boolean',
                                                          'label': 'Tally Overlay',
                                                          'required': True}},
                                     'sets': {'show_tally': '{value}'}},
                  'set_show_vu': {'label': 'Set Audio Meter Overlay',
                                  'help': 'Show audio level meters on the decoded picture.',
                                  'params': {'value': {'type': 'boolean',
                                                       'label': 'Audio Meter Overlay',
                                                       'required': True}},
                                  'sets': {'show_vu': '{value}'}},
                  'set_show_cross': {'label': 'Set Center Cross',
                                     'help': 'Show a center cross for alignment.',
                                     'params': {'value': {'type': 'boolean',
                                                          'label': 'Center Cross',
                                                          'required': True}},
                                     'sets': {'show_cross': '{value}'}},
                  'set_horizontal_flip': {'label': 'Set Horizontal Flip',
                                          'help': 'Mirror the decoded picture horizontally.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Horizontal Flip',
                                                               'required': True}},
                                          'sets': {'horizontal_flip': '{value}'}},
                  'set_vertical_flip': {'label': 'Set Vertical Flip',
                                        'help': 'Turn the decoded picture upside down.',
                                        'params': {'value': {'type': 'boolean',
                                                             'label': 'Vertical Flip',
                                                             'required': True}},
                                        'sets': {'vertical_flip': '{value}'}},
                  'set_follow_input': {'label': 'Set Follow Input Resolution',
                                       'help': 'Match the decoded source resolution. Disable to select '
                                               'a fixed display mode.',
                                       'params': {'value': {'type': 'boolean',
                                                            'label': 'Follow Input Resolution',
                                                            'required': True}},
                                       'sets': {'follow_input': '{value}'}},
                  'set_reset_source_on_boot': {'label': 'Set Reset Source on Boot',
                                               'help': 'Clear the selected source on boot and return '
                                                       'to automatic NDI discovery (firmware 1.3).',
                                               'params': {'value': {'type': 'boolean',
                                                                    'label': 'Reset Source on Boot',
                                                                    'required': True}},
                                               'sets': {'reset_source_on_boot': '{value}'}},
                  'set_meter_mode': {'label': 'Set Audio Meter Scale',
                                     'help': 'Choose the meter scale before or after output gain.',
                                     'params': {'value': {'type': 'enum',
                                                          'label': 'Audio Meter Scale',
                                                          'required': True,
                                                          'values': [{'value': 'none', 'label': 'None'},
                                                                     {'value': 'dbu', 'label': 'dBu'},
                                                                     {'value': 'dbvu', 'label': 'VU'},
                                                                     {'value': 'dbfs', 'label': 'dBFS'},
                                                                     {'value': 'post-gain-dbu',
                                                                      'label': 'dBu after gain'},
                                                                     {'value': 'post-gain-dbvu',
                                                                      'label': 'VU after gain'},
                                                                     {'value': 'post-gain-dbfs',
                                                                      'label': 'dBFS after gain'}]}},
                                     'sets': {'meter_mode': '{value}'}},
                  'set_safe_area': {'label': 'Set Safe Area',
                                    'help': 'Overlay a framing guide.',
                                    'params': {'value': {'type': 'enum',
                                                         'label': 'Safe Area',
                                                         'required': True,
                                                         'values': [{'value': 'none', 'label': 'None'},
                                                                    {'value': '4:3', 'label': '4:3'},
                                                                    {'value': '80%', 'label': '80%'},
                                                                    {'value': 'square',
                                                                     'label': 'Square'}]}},
                                    'sets': {'safe_area': '{value}'}},
                  'set_ident_mode': {'label': 'Set Identification Overlay',
                                     'help': 'Choose a custom label or the device name to identify '
                                             'this output.',
                                     'params': {'value': {'type': 'enum',
                                                          'label': 'Identification Overlay',
                                                          'required': True,
                                                          'values': [{'value': 'none', 'label': 'Off'},
                                                                     {'value': 'ident-text',
                                                                      'label': 'Custom label'},
                                                                     {'value': 'device-name',
                                                                      'label': 'Device name'}]}},
                                     'sets': {'ident_mode': '{value}'}},
                  'set_ident_text': {'label': 'Set Identification Text',
                                     'help': 'Custom overlay text: up to 127 UTF-8 bytes on firmware '
                                             '1.3; older firmware accepts 1–32 letters, digits, '
                                             'spaces, _, - and +.',
                                     'params': {'value': {'type': 'string',
                                                          'label': 'Identification Text',
                                                          'required': True}},
                                     'sets': {'ident_text': '{value}'}},
                  'set_switch_mode': {'label': 'Set Source Switching Picture',
                                      'help': 'Choose what viewers see while a different source '
                                              'connects.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Source Switching Picture',
                                                           'required': True,
                                                           'values': [{'value': 'blank',
                                                                       'label': 'Black'},
                                                                      {'value': 'keep-last',
                                                                       'label': 'Hold last picture'}]}},
                                      'sets': {'switch_mode': '{value}'}},
                  'set_deinterlace': {'label': 'Set Deinterlace Mode',
                                      'help': 'Convert interlaced sources to a progressive picture.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Deinterlace Mode',
                                                           'required': True,
                                                           'values': [{'value': 'bob', 'label': 'Bob'},
                                                                      {'value': 'weave',
                                                                       'label': 'Weave'}]}},
                                      'sets': {'deinterlace': '{value}'}},
                  'set_input_color': {'label': 'Set Input Color Space',
                                      'help': 'Automatic chooses from the source. Manual choices '
                                              'follow the firmware API.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Input Color Space',
                                                           'required': True,
                                                           'values': [{'value': 'auto',
                                                                       'label': 'Automatic'},
                                                                      {'value': 'rgb', 'label': 'RGB'},
                                                                      {'value': 'bt.601',
                                                                       'label': 'BT.601'},
                                                                      {'value': 'bt.709',
                                                                       'label': 'BT.709'},
                                                                      {'value': 'bt.2020',
                                                                       'label': 'BT.2020'},
                                                                      {'value': 'bt.2020c',
                                                                       'label': 'BT.2020 constant '
                                                                                'luminance'}]}},
                                      'sets': {'input_color': '{value}'}},
                  'set_aspect_conversion': {'label': 'Set Aspect Conversion',
                                            'help': 'Fit preserves the whole picture, Fill stretches '
                                                    'it, and Zoom crops it.',
                                            'params': {'value': {'type': 'enum',
                                                                 'label': 'Aspect Conversion',
                                                                 'required': True,
                                                                 'values': [{'value': 'windowbox',
                                                                             'label': 'Fit'},
                                                                            {'value': 'full',
                                                                             'label': 'Fill'},
                                                                            {'value': 'zoom',
                                                                             'label': 'Zoom / crop'}]}},
                                            'sets': {'aspect_conversion': '{value}'}},
                  'set_alpha_mode': {'label': 'Set Alpha Display',
                                     'help': 'Choose the alpha channel background when the decoder '
                                             'reports this capability.',
                                     'params': {'value': {'type': 'enum',
                                                          'label': 'Alpha Display',
                                                          'required': True,
                                                          'values': [{'value': 'alpha-only',
                                                                      'label': 'Alpha only'},
                                                                     {'value': 'alpha-blend-white',
                                                                      'label': 'White'},
                                                                     {'value': 'alpha-blend-black',
                                                                      'label': 'Black'},
                                                                     {'value': 'alpha-blend-checkerboard',
                                                                      'label': 'Checkerboard'}]}},
                                     'sets': {'alpha_mode': '{value}'}},
                  'set_output_color': {'label': 'Set Output Color Format',
                                       'help': 'Set the output color format.',
                                       'params': {'value': {'type': 'enum',
                                                            'label': 'Output Color Format',
                                                            'required': True,
                                                            'values': [{'value': 'rgb', 'label': 'RGB'},
                                                                       {'value': 'yuv444',
                                                                        'label': 'YCbCr 4:4:4'},
                                                                       {'value': 'yuv422',
                                                                        'label': 'YCbCr 4:2:2'}]}},
                                       'sets': {'output_color': '{value}'}},
                  'set_output_range': {'label': 'Set RGB Output Range',
                                       'help': 'Applies only when Output Color Format is RGB.',
                                       'params': {'value': {'type': 'enum',
                                                            'label': 'RGB Output Range',
                                                            'required': True,
                                                            'values': [{'value': 'limited',
                                                                        'label': 'Limited'},
                                                                       {'value': 'full',
                                                                        'label': 'Full'}]}},
                                       'sets': {'output_range': '{value}'}},
                  'set_hdmi_enabled': {'label': 'Set HDMI Output',
                                       'help': 'Enable or disable the HDMI output. This does not power '
                                               'down the decoder.',
                                       'params': {'value': {'type': 'boolean',
                                                            'label': 'HDMI Output',
                                                            'required': True}},
                                       'sets': {'hdmi_enabled': '{value}'}},
                  'set_audio_gain': {'label': 'Set Audio Gain',
                                     'help': 'Adjust decoded audio in dB; 0 dB is unchanged.',
                                     'params': {'value': {'type': 'number',
                                                          'label': 'Audio Gain',
                                                          'required': True,
                                                          'min': -100,
                                                          'max': 20,
                                                          'unit': 'dB'}},
                                     'sets': {'audio_gain': '{value}'}},
                  'set_audio_sample_rate': {'label': 'Set Output Sample Rate',
                                            'help': 'Follow the source or use a fixed output sample '
                                                    'rate.',
                                            'params': {'value': {'type': 'enum',
                                                                 'label': 'Output Sample Rate',
                                                                 'required': True,
                                                                 'values': [{'value': 0,
                                                                             'label': 'Follow source'},
                                                                            {'value': 32000,
                                                                             'label': '32 kHz'},
                                                                            {'value': 44100,
                                                                             'label': '44.1 kHz'},
                                                                            {'value': 48000,
                                                                             'label': '48 kHz'},
                                                                            {'value': 88200,
                                                                             'label': '88.2 kHz'},
                                                                            {'value': 96000,
                                                                             'label': '96 kHz'}]}},
                                            'sets': {'audio_sample_rate': '{value}'}},
                  'set_audio_channel_count': {'label': 'Set Output Audio Channels',
                                              'help': 'Follow the source or select 2, 4 or 8 channels. '
                                                      'A fixed count is required for channel mapping.',
                                              'params': {'value': {'type': 'enum',
                                                                   'label': 'Output Audio Channels',
                                                                   'required': True,
                                                                   'values': [{'value': 0,
                                                                               'label': 'Follow '
                                                                                        'source'},
                                                                              {'value': 2,
                                                                               'label': '2 channels'},
                                                                              {'value': 4,
                                                                               'label': '4 channels'},
                                                                              {'value': 8,
                                                                               'label': '8 '
                                                                                        'channels'}]}},
                                              'sets': {'audio_channel_count': '{value}'}},
                  'set_audio_conversion': {'label': 'Set Audio Channel Order',
                                           'help': 'Choose the device’s SMPTE or EBU channel '
                                                   'conversion.',
                                           'params': {'value': {'type': 'enum',
                                                                'label': 'Audio Channel Order',
                                                                'required': True,
                                                                'values': [{'value': 'smpte',
                                                                            'label': 'SMPTE'},
                                                                           {'value': 'ebu',
                                                                            'label': 'EBU'}]}},
                                           'sets': {'audio_conversion': '{value}'}},
                  'set_audio_check_pts': {'label': 'Set Check Audio Timestamps',
                                          'help': 'Use the source’s presentation timestamps for audio.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Check Audio Timestamps',
                                                               'required': True}},
                                          'sets': {'audio_check_pts': '{value}'}},
                  'set_discovery_enabled': {'label': 'Set NDI Discovery Server',
                                            'help': 'Use the configured discovery servers. Enter their '
                                                    'addresses before enabling.',
                                            'params': {'value': {'type': 'boolean',
                                                                 'label': 'NDI Discovery Server',
                                                                 'required': True}},
                                            'sets': {'discovery_enabled': '{value}'}},
                  'set_low_bandwidth': {'label': 'Set Low Bandwidth NDI',
                                        'help': 'Request the NDI source’s low bandwidth stream.',
                                        'params': {'value': {'type': 'boolean',
                                                             'label': 'Low Bandwidth NDI',
                                                             'required': True}},
                                        'sets': {'low_bandwidth': '{value}'}},
                  'set_allow_multicast': {'label': 'Set Allow NDI Multicast',
                                          'help': 'Allow reception over NDI multicast.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Allow NDI Multicast',
                                                               'required': True}},
                                          'sets': {'allow_multicast': '{value}'}},
                  'set_allow_rudp': {'label': 'Set Allow NDI Reliable UDP',
                                     'help': 'Allow reception over NDI reliable UDP.',
                                     'params': {'value': {'type': 'boolean',
                                                          'label': 'Allow NDI Reliable UDP',
                                                          'required': True}},
                                     'sets': {'allow_rudp': '{value}'}},
                  'set_allow_tcp': {'label': 'Set Allow NDI Multi-TCP',
                                    'help': 'Allow reception over NDI multi-connection TCP.',
                                    'params': {'value': {'type': 'boolean',
                                                         'label': 'Allow NDI Multi-TCP',
                                                         'required': True}},
                                    'sets': {'allow_tcp': '{value}'}},
                  'set_allow_udp': {'label': 'Set Allow NDI UDP',
                                    'help': 'Allow reception over NDI UDP.',
                                    'params': {'value': {'type': 'boolean',
                                                         'label': 'Allow NDI UDP',
                                                         'required': True}},
                                    'sets': {'allow_udp': '{value}'}},
                  'set_ignore_hx_pts': {'label': 'Set Ignore NDI HX Video Timestamps',
                                        'help': 'Use this only when the source’s video timestamps '
                                                'cause playback problems.',
                                        'params': {'value': {'type': 'boolean',
                                                             'label': 'Ignore NDI HX Video Timestamps',
                                                             'required': True}},
                                        'sets': {'ignore_hx_pts': '{value}'}},
                  'set_prefer_timecode': {'label': 'Set Prefer NDI Timecode',
                                          'help': 'Prefer source timecode for timing.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Prefer NDI Timecode',
                                                               'required': True}},
                                          'sets': {'prefer_timecode': '{value}'}},
                  'set_discovery_servers': {'label': 'Set NDI Discovery Servers',
                                            'help': 'Comma-separated IPv4 addresses. Keep at least one '
                                                    'while discovery-server reception is enabled.',
                                            'params': {'value': {'type': 'string',
                                                                 'label': 'NDI Discovery Servers',
                                                                 'required': True}},
                                            'sets': {'discovery_servers': '{value}'}},
                  'set_multicast_subnets': {'label': 'Set NDI Multicast Subnets',
                                            'help': 'Comma-separated IPv4 CIDR subnets. Empty allows '
                                                    'every subnet.',
                                            'params': {'value': {'type': 'string',
                                                                 'label': 'NDI Multicast Subnets',
                                                                 'required': True}},
                                            'sets': {'multicast_subnets': '{value}'}},
                  'set_ndi_groups': {'label': 'Set NDI Groups',
                                     'help': 'NDI group name list; public is the usual default.',
                                     'params': {'value': {'type': 'string',
                                                          'label': 'NDI Groups',
                                                          'required': True}},
                                     'sets': {'ndi_groups': '{value}'}},
                  'set_playback_buffer': {'label': 'Set Playback Buffer',
                                          'help': 'Playback buffer in milliseconds. Saved URLs can '
                                                  'override it with their own buffer; use Set Preset '
                                                  'Buffer for those.',
                                          'params': {'value': {'type': 'integer',
                                                               'label': 'Playback Buffer',
                                                               'required': True,
                                                               'min': 20,
                                                               'max': 120,
                                                               'unit': 'ms'}},
                                          'sets': {'playback_buffer': '{value}'}},
                  'refresh': {'label': 'Refresh Status',
                              'help': 'Refresh signal, settings, sources and supported display modes.',
                              'query_for': 'model'},
                  'select_source': {'label': 'Select Source',
                                    'help': 'Choose a discovered NDI source or saved preset. The '
                                            'Selected Source feedback changes only after the decoder '
                                            'confirms it.',
                                    'sets': {'source': '{source}'},
                                    'params': {'source': {'type': 'child_id',
                                                          'label': 'Source',
                                                          'required': True,
                                                          'child_type': 'source'}}},
                  'set_output_mode': {'label': 'Set Output Resolution',
                                      'help': 'Select a mode reported by the connected display. Turns '
                                              'off Follow Input Resolution, then verifies the current '
                                              'mode.',
                                      'sets': {'output_mode': '{mode}', 'follow_input': False},
                                      'params': {'mode': {'type': 'child_id',
                                                          'label': 'Resolution',
                                                          'required': True,
                                                          'child_type': 'output_mode'}}},
                  'match_input': {'label': 'Match Input Resolution',
                                  'help': 'Let the output follow the decoded source resolution.',
                                  'sets': {'follow_input': True}},
                  'map_audio_channel': {'label': 'Map Audio Channel',
                                        'help': 'Map an output to a source channel. First select a '
                                                'fixed Output Audio Channels count; channels beyond '
                                                'that count are unavailable.',
                                        'sets': {'source_channel': '{source_channel}'},
                                        'params': {'output': {'type': 'child_id',
                                                              'label': 'Output Channel',
                                                              'required': True,
                                                              'child_type': 'audio_output'},
                                                   'source_channel': {'type': 'enum',
                                                                      'label': 'Source Channel',
                                                                      'required': True,
                                                                      'values': [{'value': 1,
                                                                                  'label': 'Channel 1'},
                                                                                 {'value': 2,
                                                                                  'label': 'Channel 2'},
                                                                                 {'value': 3,
                                                                                  'label': 'Channel 3'},
                                                                                 {'value': 4,
                                                                                  'label': 'Channel 4'},
                                                                                 {'value': 5,
                                                                                  'label': 'Channel 5'},
                                                                                 {'value': 6,
                                                                                  'label': 'Channel 6'},
                                                                                 {'value': 7,
                                                                                  'label': 'Channel 7'},
                                                                                 {'value': 8,
                                                                                  'label': 'Channel 8'},
                                                                                 {'value': 9,
                                                                                  'label': 'Channel 9'},
                                                                                 {'value': 10,
                                                                                  'label': 'Channel '
                                                                                           '10'},
                                                                                 {'value': 11,
                                                                                  'label': 'Channel '
                                                                                           '11'},
                                                                                 {'value': 12,
                                                                                  'label': 'Channel '
                                                                                           '12'},
                                                                                 {'value': 13,
                                                                                  'label': 'Channel '
                                                                                           '13'},
                                                                                 {'value': 14,
                                                                                  'label': 'Channel '
                                                                                           '14'},
                                                                                 {'value': 15,
                                                                                  'label': 'Channel '
                                                                                           '15'},
                                                                                 {'value': 16,
                                                                                  'label': 'Channel '
                                                                                           '16'}]}}},
                  'add_stream_source': {'label': 'Add Stream Preset',
                                        'help': 'Save a named stream URL on the decoder.',
                                        'params': {'name': {'type': 'string',
                                                            'label': 'Preset Name',
                                                            'required': True,
                                                            'pattern': '[ -~]{1,120}',
                                                            'help': '1–120 ASCII characters. Use a '
                                                                    'different name from existing '
                                                                    'presets.'},
                                                   'url': {'type': 'string',
                                                           'label': 'Stream URL',
                                                           'required': True,
                                                           'secret': True,
                                                           'help': 'An ntkndi, RTSP, HTTP/HLS, RTMP, '
                                                                   'RTMPS (firmware 1.3), UDP, SRT or '
                                                                   'RTP URL. Credentials remain in the '
                                                                   'preset and its backup file.'}}},
                  'save_ndi_source': {'label': 'Save NDI Preset',
                                      'help': 'Save a discovered NDI source’s name and address as a '
                                              'preset.',
                                      'params': {'source': {'type': 'child_id',
                                                            'label': 'Source',
                                                            'required': True,
                                                            'child_type': 'source'},
                                                 'name': {'type': 'string',
                                                          'label': 'Preset Name',
                                                          'required': True,
                                                          'pattern': '[ -~]{1,120}',
                                                          'help': '1–120 ASCII characters. Use a '
                                                                  'different name from existing '
                                                                  'presets.'}}},
                  'add_ndi_source': {'label': 'Add NDI Preset by Address',
                                     'help': 'Save an NDI source outside local discovery using its '
                                             'exact NDI name and address.',
                                     'params': {'name': {'type': 'string',
                                                         'label': 'Preset Name',
                                                         'required': True,
                                                         'pattern': '[ -~]{1,120}',
                                                         'help': '1–120 ASCII characters. Use a '
                                                                 'different name from existing '
                                                                 'presets.'},
                                                'ndi_name': {'type': 'string',
                                                             'label': 'NDI Source Name',
                                                             'required': True},
                                                'address': {'type': 'string',
                                                            'label': 'NDI Address',
                                                            'required': True,
                                                            'help': 'The NDI sender’s IP address and '
                                                                    'port, for example '
                                                                    '192.0.2.10:5961.'}}},
                  'update_stream_source': {'label': 'Update Preset URL',
                                           'help': 'Replace a saved preset’s URL, preserving its name '
                                                   'and keyboard shortcut.',
                                           'confirm': 'Replace this preset’s stream URL?',
                                           'params': {'source': {'type': 'child_id',
                                                                 'label': 'Source',
                                                                 'required': True,
                                                                 'child_type': 'source'},
                                                      'url': {'type': 'string',
                                                              'label': 'Stream URL',
                                                              'required': True,
                                                              'secret': True,
                                                              'help': 'An ntkndi, RTSP, HTTP/HLS, '
                                                                      'RTMP, RTMPS (firmware 1.3), '
                                                                      'UDP, SRT or RTP URL. '
                                                                      'Credentials remain in the '
                                                                      'preset and its backup file.'}}},
                  'rename_source': {'label': 'Rename Preset',
                                    'help': 'Rename a saved preset without changing its URL or '
                                            'keyboard shortcut.',
                                    'params': {'source': {'type': 'child_id',
                                                          'label': 'Source',
                                                          'required': True,
                                                          'child_type': 'source'},
                                               'name': {'type': 'string',
                                                        'label': 'Preset Name',
                                                        'required': True,
                                                        'pattern': '[ -~]{1,120}',
                                                        'help': '1–120 ASCII characters. Use a '
                                                                'different name from existing '
                                                                'presets.'}}},
                  'delete_source': {'label': 'Delete Preset',
                                    'help': 'Remove a saved preset from the decoder.',
                                    'confirm': 'Delete this saved source preset?',
                                    'params': {'source': {'type': 'child_id',
                                                          'label': 'Source',
                                                          'required': True,
                                                          'child_type': 'source'}}},
                  'set_preset_buffer': {'label': 'Set Preset Buffer',
                                        'help': 'Set this preset’s buffer in milliseconds within the '
                                                'decoder’s reported protocol range. Other URL '
                                                'parameters are preserved.',
                                        'params': {'source': {'type': 'child_id',
                                                              'label': 'Source',
                                                              'required': True,
                                                              'child_type': 'source'},
                                                   'milliseconds': {'type': 'integer',
                                                                    'label': 'Buffer',
                                                                    'required': True,
                                                                    'min': 0,
                                                                    'unit': 'ms'}}},
                  'download_sources': {'label': 'Download Source Presets',
                                       'help': 'Download preset names, stream URLs and shortcuts as '
                                               'JSON. This file can contain stream credentials; keep '
                                               'it with the site’s protected configuration files.',
                                       'returns_file': True},
                  'import_sources': {'label': 'Import Source Presets',
                                     'help': 'Import a Magewell source JSON file on firmware 1.3. '
                                             'Matching names are updated; other presets remain. Every '
                                             'row is validated before uploading, then read back.',
                                     'confirm': 'Import these sources and replace presets with '
                                                'matching names?',
                                     'params': {'file': {'type': 'file',
                                                         'label': 'Source JSON File',
                                                         'required': True,
                                                         'accept': ['.json'],
                                                         'max_bytes': 1048576}}},
                  'download_output_edid': {'label': 'Download Output EDID',
                                           'help': 'Download the connected display’s EDID as a binary '
                                                   'file.',
                                           'returns_file': True},
                  'reset_video': {'label': 'Reset Video Settings',
                                  'help': 'Restore the decoder’s picture, overlay and scaling '
                                          'defaults.',
                                  'confirm': 'Restore all video settings to their defaults?'}},
     'child_entity_types': {'source': {'label': 'Source',
                                       'label_plural': 'Sources',
                                       'id_format': {'type': 'string'},
                                       'label_field': 'label',
                                       'summary_fields': ['kind', 'protocol', 'selected'],
                                       'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                           'kind': {'type': 'string',
                                                                    'label': 'Source Type'},
                                                           'protocol': {'type': 'string',
                                                                        'label': 'Protocol'},
                                                           'address': {'type': 'string',
                                                                       'label': 'NDI Address'},
                                                           'selected': {'type': 'boolean',
                                                                        'label': 'Selected',
                                                                        'cloud_priority': 'high'},
                                                           'ambiguous': {'type': 'boolean',
                                                                         'label': 'Duplicate NDI Name'},
                                                           'label': {'type': 'string',
                                                                     'label': 'Source Label'}}},
                            'output_mode': {'label': 'Output Mode',
                                            'label_plural': 'Output Modes',
                                            'id_format': {'type': 'string'},
                                            'label_field': 'name',
                                            'summary_fields': ['current', 'preferred'],
                                            'state_variables': {'name': {'type': 'string',
                                                                         'label': 'Resolution'},
                                                                'width': {'type': 'integer',
                                                                          'label': 'Width',
                                                                          'unit': 'px'},
                                                                'height': {'type': 'integer',
                                                                           'label': 'Height',
                                                                           'unit': 'px'},
                                                                'rate': {'type': 'number',
                                                                         'label': 'Field Rate',
                                                                         'unit': 'Hz'},
                                                                'current': {'type': 'boolean',
                                                                            'label': 'Current'},
                                                                'preferred': {'type': 'boolean',
                                                                              'label': 'Display '
                                                                                       'Preferred'}}},
                            'audio_output': {'label': 'Audio Output',
                                             'label_plural': 'Audio Outputs',
                                             'id_format': {'type': 'integer', 'min': 1, 'max': 8},
                                             'label_field': 'name',
                                             'summary_fields': ['source_channel'],
                                             'state_variables': {'name': {'type': 'string',
                                                                          'label': 'Output'},
                                                                 'source_channel': {'type': 'integer',
                                                                                    'label': 'Source '
                                                                                             'Channel',
                                                                                    'min': 1,
                                                                                    'max': 16,
                                                                                    'step': 1,
                                                                                    'control': True}}}},
     'quick_actions': ['select_source', 'set_audio_gain', 'match_input', 'set_output_mode'],
     'routing': {'source_child_type': 'source',
                 'source_param': 'source',
                 'command': 'select_source',
                 'planes': [{'label': 'Stream', 'route_property': 'source'}]},
     'discovery': {'oui': ['70:B3:D5:75:D']}}

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._client = None
        self._operations = asyncio.Lock()
        self._rosters = {}
        self._configs = {}
        self._detail_errors = {}
        self._caps = {}
        self._modern = False
        self._poll_count = 0

    async def _create_transport(self, transport_type):
        host = str(self.config.get('host', '')).strip()
        if not host:
            raise ConnectionFaultError('Enter the decoder address.', code='invalid_config')
        if not self.config.get('username') or not self.config.get('password'):
            raise ConnectionFaultError('The decoder username or password is missing.', code='auth_failed')
        scheme = 'https' if self.config.get('ssl') else 'http'
        self._client = httpx.AsyncClient(
            base_url=httpx.URL(scheme=scheme, host=host, port=int(self.config.get('port', 80))),
            verify=bool(self.config.get('verify_ssl', False)),
            timeout=httpx.Timeout(6, connect=4), follow_redirects=False,
        )

    async def _post_connect(self):
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._detail_errors.clear()
        self._configs.clear()
        await self._login()
        await self._read_summary()
        self._caps = await self._call('get-caps', core=True)
        self._apply_capabilities()

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
        # Both API versions document a login-free ping. Any reply proves life.
        await self._request('ping')

    async def _request(self, method, params=None, *, files=None, post_login=False):
        if self._client is None:
            raise ConnectionError('The decoder session is closed.')
        query, body, verb = _request_parts(method, params, bool(files), post_login)
        self.record_traffic('tx', verb + ' /mwapi?' + urlencode(query) +
                            ('\n' + json.dumps(body) if body else ''))
        try:
            reply = await self._client.request(verb, '/mwapi', params=query, json=body, files=files)
        except httpx.TransportError as exc:
            raise ConnectionError('The decoder stopped answering HTTP requests.') from exc
        self._protect_reply(reply)
        self.record_traffic('rx', reply.text)
        return reply

    def _protect_url(self, value):
        self.redact_in_log(value)
        self.redact_in_log(urlencode({'url': value}).partition('=')[2])
        self.redact_in_log(json.dumps(value)[1:-1])

    def _protect_reply(self, reply):
        try:
            data = reply.json()
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        for value in _reply_urls(data):
            self._protect_url(value)

    async def _login(self):
        self._client.cookies.clear()
        # Both published APIs require this digest, including firmware 1.3.
        digest = hashlib.md5(str(self.config['password']).encode(), usedforsecurity=False).hexdigest()
        self.redact_in_log(digest)
        legacy = self.config.get('legacy_login') or 'HDMI 4K' in str(self.config.get('model', ''))
        reply = await self._request('login', {'id': self.config['username'], 'pass': digest},
                                    post_login=not legacy)
        _check_login(reply)
        cookies = [cookie for cookie in self._client.cookies.jar if cookie.name == 'sid']
        if not cookies:
            raise CommandRefusedError('The decoder accepted login but supplied no session cookie.')
        for cookie in cookies:
            self.redact_in_log(cookie.value)

    async def _call(self, method, params=None, *, core=False, files=None):
        for attempt in range(2):
            try:
                return _decode(await self._request(method, params, files=files), method)
            except DecoderError as exc:
                if exc.code in (37, 401) and attempt == 0:
                    await self._login()
                    continue
                _check_access(exc, core)
                raise
        raise ConnectionError('The decoder session could not be renewed.')

    async def _read_summary(self):
        data = await self._call('get-summary-info', core=True)
        device = data.get('device')
        if not isinstance(device, dict):
            raise CommandRefusedError('The decoder returned no device information.')
        model = str(device.get('model', '')).replace('®', '').strip()
        model = model.removeprefix('Pro Convert for ').removeprefix('Pro Convert ')
        model = 'Pro Convert for ' + model
        if model not in type(self).DRIVER_INFO['compatible_models'][0]['models']:
            raise ConnectionFaultError('This is not a supported Pro Convert for NDI decoder.', code='invalid_config')
        self.set_state('model', model)
        self._modern = _firmware_pair(device.get('fw-version', '')) >= (1, 3)
        for group, fields in _SUMMARY.items():
            self._copy_values(data.get(group, {}), fields)

    def _copy_values(self, data, fields):
        for wire, key in fields.items():
            self.set_state(key, data.get(wire))

    def _apply_capabilities(self):
        edid = bool(self._caps.get('has-output-edid'))
        self.set_state('output_edid_available', edid)
        if not edid:
            self.DRIVER_INFO['commands'].pop('download_output_edid', None)
        if not self._caps.get('has-alpha-disp-mode'):
            self._hide_setting('alpha_mode')
        if 'SDI' in self.get_state('model'):
            self._hide_setting('hdmi_enabled')
        if not self._modern:
            self._hide_setting('reset_source_on_boot')
            self.DRIVER_INFO['commands'].pop('import_sources', None)
            self._narrow_values('input_color', {'auto', 'bt.601', 'bt.709'})

    def _narrow_values(self, key, values):
        for spec in (self.DRIVER_INFO['state_variables'][key],
                     self.DRIVER_INFO['device_settings'][key],
                     self.DRIVER_INFO['commands']['set_' + key]['params']['value']):
            spec['values'] = [item for item in spec['values'] if _enum_value(item) in values]

    def _hide_setting(self, key):
        self.DRIVER_INFO['commands'].pop('set_' + key, None)
        self.DRIVER_INFO['device_settings'].pop(key, None)
        self.DRIVER_INFO['state_variables'][key]['control'] = False
        if key == 'follow_input':
            self.DRIVER_INFO['commands'].pop('match_input', None)

    async def poll(self):
        async with self._operations:
            await self._read_live()
            self._poll_count += 1
            if self._poll_count % max(1, int(self.config.get('detail_poll_every', 10))) == 0:
                await self._read_details()
            self._report_errors()

    async def _read_live(self):
        await self._read_summary()
        data = await self._call('get-signal-info')
        # A missing signal block clears old dimensions/rate after stream loss.
        for group, fields in _SIGNAL.items():
            self._copy_values(data.get(group, {}), fields)
        await self._read_selection()

    async def refresh_children(self):
        async with self._operations:
            await self._read_live()
            await self._read_details()
            self._report_errors()
        return {kind: len(rows) for kind, rows in self._rosters.items()}

    async def _read_details(self):
        for group in ('video', 'format', 'audio', 'ndi', 'playback'):
            await self._optional_read(group)
        if 'hdmi_enabled' in self.DRIVER_INFO['device_settings']:
            await self._optional_read('hdmi')
        await self._optional_read('sources')
        await self._optional_read('modes')

    async def _optional_read(self, group):
        readers = {'sources': self._read_sources, 'modes': self._read_modes}
        try:
            if group in readers:
                await readers[group]()
            else:
                await self._read_config(group)
            self._detail_errors.pop(group, None)
        except DecoderError as exc:
            if exc.code not in (5, 9, 16, 31, 403):
                raise
            self._detail_errors[group] = str(exc)

    def _report_errors(self):
        self.set_state('last_error', '; '.join(self._detail_errors.values()))

    async def _read_config(self, group):
        data = await self._call('get-' + _CONFIG_PATHS.get(group, group + '-config'))
        self._configs[group] = data
        for key, (owner, wire) in _FIELDS.items():
            if owner != group:
                continue
            self.set_state(key, _reported_setting(key, wire, data))
            if wire not in data:
                self._hide_setting(key)
        if group == 'audio':
            self._apply_audio(data)
        return data

    def _apply_audio(self, data):
        # The documented output counts are 0 (follow), 2, 4 and 8. Mapping is
        # effective only for a fixed count; do not invent channels in follow mode.
        count = data.get('channels', 0)
        rows = {}
        if count not in (2, 4, 8):
            self._reconcile('audio_output', rows)
            return
        for index in range(count):
            channel = data.get('ch' + str(index))
            values = {'name': 'Output ' + str(index + 1),
                      'source_channel': channel + 1 if isinstance(channel, int) else None}
            rows[index + 1] = ({'index': index}, values)
        self._reconcile('audio_output', rows)

    def _reconcile(self, kind, rows):
        for ident, (_, values) in rows.items():
            self.register_child(kind, ident, initial_state=values)
            self.set_child_state_batch(kind, ident, values)
        for ident in self._rosters.get(kind, {}).keys() - rows.keys():
            self.deregister_child(kind, ident)
        self._rosters[kind] = {ident: row for ident, (row, _) in rows.items()}

    async def _read_selection(self):
        data = await self._call('get-channel', core=True)
        name, ndi = data.get('name'), data.get('ndi-name')
        if not isinstance(name, str) or not isinstance(ndi, bool):
            raise CommandRefusedError('The decoder returned no source selection.')
        ident = _source_id(name, ndi) if name else ''
        self.set_state('source_name', name)
        self.set_state('source', ident)
        for child in self._rosters.get('source', {}):
            self.set_child_state('source', child, 'selected', child == ident)
        return data

    async def _read_sources(self):
        presets = _list(await self._call('list-channels'), 'channels')
        discovered = _list(await self._call('get-ndi-sources'), 'sources')
        rows = self._preset_rows(presets)
        rows.update(self._ndi_rows(discovered))
        self._reconcile('source', rows)

    def _preset_rows(self, presets):
        rows = {}
        for row in presets:
            if not _has_strings(row, 'name', 'url'):
                continue
            self._protect_url(row['url'])
            ident = _source_id(row['name'], False)
            values = _source_values(row['name'], False, self.get_state('source') == ident)
            values['protocol'] = urlsplit(row['url']).scheme.upper()
            rows[ident] = (dict(row, ndi=False), values)
        return rows

    def _ndi_rows(self, discovered):
        counts = Counter(row.get('ndi-name') for row in discovered if isinstance(row, dict))
        rows = {}
        for row in discovered:
            if not _has_strings(row, 'ndi-name', 'ip-addr'):
                continue
            name = row['ndi-name']
            ident = _source_id(name, True)
            values = _source_values(name, True, self.get_state('source') == ident)
            values.update(address=row['ip-addr'], ambiguous=counts[name] > 1)
            rows[ident] = (dict(row, name=name, ndi=True, ambiguous=counts[name] > 1), values)
        return rows

    async def _read_modes(self):
        modes = _list(await self._call('get-supported-video-modes'), 'modes')
        rows, current, current_name = {}, '', ''
        for mode in modes:
            if not isinstance(mode, dict) or not all(key in mode for key in _MODE_FIELDS):
                continue
            ident, name = _mode_identity(mode)
            values = {'name': name, 'width': mode['width'], 'height': mode['height'],
                      'rate': mode['field-rate'] / 100, 'current': bool(mode.get('curr-mode')),
                      'preferred': bool(mode.get('pref-mode'))}
            rows[ident] = (mode, values)
            if values['current']:
                current, current_name = ident, name
        self._reconcile('output_mode', rows)
        self.set_state('output_mode', current)
        self.set_state('output_mode_name', current_name)

    async def set_device_setting(self, key, value):
        if key not in self.DRIVER_INFO['device_settings']:
            raise CommandRefusedError('This setting is not supported by the connected decoder.')
        async with self._operations:
            await self._write_setting(key, value)

    async def send_command(self, command, params=None):
        if command not in self.DRIVER_INFO['commands']:
            raise CommandRefusedError('This command is not supported by the connected decoder.')
        if command == 'refresh':
            return await self.refresh_children()
        async with self._operations:
            try:
                result = await self._dispatch(command, params or {})
                self._report_errors()
                return result
            except CommandRefusedError as exc:
                self.set_state('last_error', str(exc))
                raise

    async def _dispatch(self, command, params):
        key = command.removeprefix('set_')
        if command.startswith('set_') and key in _FIELDS:
            return await self._write_setting(key, params['value'])
        handlers = {'select_source': self._select_source, 'set_output_mode': self._set_output_mode,
                    'match_input': self._match_input, 'map_audio_channel': self._map_audio,
                    'add_stream_source': self._add_stream, 'save_ndi_source': self._save_ndi,
                    'add_ndi_source': self._add_ndi, 'update_stream_source': self._update_stream,
                    'rename_source': self._rename_source, 'delete_source': self._delete_source,
                    'set_preset_buffer': self._preset_buffer, 'download_sources': self._download_sources,
                    'import_sources': self._import_sources, 'download_output_edid': self._download_edid,
                    'reset_video': self._reset_video}
        return await handlers[command](params)

    async def _write_setting(self, key, value):
        group, wire = _FIELDS[key]
        value = _setting_value(key, value, self._modern)
        if key in ('discovery_enabled', 'discovery_servers'):
            config = await self._read_config('ndi')
            _check_discovery(key, value, config)
        payload = {wire: value}
        if key == 'input_color':
            payload = {'in-auto-color-fmt': value == 'auto'}
            if value != 'auto':
                payload[wire] = value
        await self._set_fields(group, payload)

    async def _set_fields(self, group, payload):
        await self._call('set-' + _CONFIG_PATHS.get(group, group + '-config'), payload)
        reported = await self._read_config(group)
        if any(reported.get(key) != value for key, value in payload.items()):
            raise CommandRefusedError('The decoder did not confirm the requested ' + group + ' settings. Refresh Status to see its current values.')
        self._detail_errors.pop(group, None)

    async def _fresh_source(self, ident, *, saved=False):
        await self._read_sources()
        row = self._rosters['source'].get(str(ident))
        if row is None:
            raise CommandRefusedError('That source is no longer listed. Refresh Status and choose another source.')
        if row.get('ambiguous'):
            raise CommandRefusedError('More than one discovered NDI source has this name. Give the senders unique names before selecting one.')
        if saved and row['ndi']:
            raise CommandRefusedError('Choose a saved preset. Save NDI Preset first to edit a discovered source.')
        return row

    async def _select_source(self, params):
        row = await self._fresh_source(params['source'])
        payload = {'ndi-name': row['ndi'], 'name': row['name']}
        await self._call('set-channel', payload)
        reported = await self._read_selection()
        if any(reported.get(key) != value for key, value in payload.items()):
            raise CommandRefusedError('The decoder did not confirm the selected source.')
        await self._read_summary()

    async def _set_output_mode(self, params):
        await self._read_modes()
        row = self._rosters['output_mode'].get(str(params['mode']))
        if row is None:
            raise CommandRefusedError('That resolution is no longer reported by the display. Refresh Status and choose a listed resolution.')
        if 'follow_input' in self.DRIVER_INFO['device_settings']:
            await self._set_fields('video', {'follow-input-mode': False})
        await self._call('set-video-mode', {key: row[key] for key in _MODE_FIELDS})
        await self._read_modes()
        if self.get_state('output_mode') != str(params['mode']):
            raise CommandRefusedError('The decoder did not confirm the selected output resolution.')

    async def _match_input(self, params):
        await self._write_setting('follow_input', True)
        await self._read_modes()

    async def _map_audio(self, params):
        await self._read_config('audio')
        row = self._rosters['audio_output'].get(int(params['output']))
        if row is None:
            raise CommandRefusedError('Choose a fixed Output Audio Channels count, then select an available output channel.')
        await self._set_fields('audio', {'ch' + str(row['index']): int(params['source_channel']) - 1})

    async def _add_stream(self, params):
        row = {'name': params['name'], 'url': params['url']}
        _validate_preset(row, self._modern)
        await self._read_sources()
        if _source_id(row['name'], False) in self._rosters['source']:
            raise CommandRefusedError('A saved preset already uses that name. Choose a different name or update that preset.')
        self._protect_url(row['url'])
        await self._call('add-channel', row)
        await self._verify_presets([row])

    async def _save_ndi(self, params):
        row = await self._fresh_source(params['source'])
        if not row['ndi']:
            raise CommandRefusedError('Choose a discovered NDI source to save as a preset.')
        await self._add_ndi({'name': params['name'], 'ndi_name': row['name'], 'address': row['ip-addr']})

    async def _add_ndi(self, params):
        query = urlencode({'name': params['ndi_name'], 'url': params['address']})
        await self._add_stream({'name': params['name'], 'url': 'ntkndi://ndi?' + query})

    async def _modify_source(self, row, **changes):
        updated = {key: row[key] for key in ('name', 'url', 'hotkey') if key in row}
        updated.update(changes)
        _validate_preset(updated, self._modern)
        self._protect_url(updated['url'])
        payload = dict(updated, name=row['name'], **{'new-name': updated['name']})
        await self._call('modify-channel', payload)
        await self._verify_presets([updated])
        if row['name'] != updated['name'] and _source_id(row['name'], False) in self._rosters['source']:
            raise CommandRefusedError('The decoder did not confirm removal of the old preset name.')
        await self._read_selection()

    async def _update_stream(self, params):
        row = await self._fresh_source(params['source'], saved=True)
        await self._modify_source(row, url=params['url'])

    async def _rename_source(self, params):
        row = await self._fresh_source(params['source'], saved=True)
        new_id = _source_id(params['name'], False)
        if params['name'] != row['name'] and new_id in self._rosters['source']:
            raise CommandRefusedError('A saved preset already uses that name.')
        await self._modify_source(row, name=params['name'])

    async def _delete_source(self, params):
        row = await self._fresh_source(params['source'], saved=True)
        await self._call('del-channel', {'name': row['name']})
        await self._read_sources()
        await self._read_selection()
        if str(params['source']) in self._rosters['source']:
            raise CommandRefusedError('The decoder did not confirm deletion of the preset.')

    async def _preset_buffer(self, params):
        row = await self._fresh_source(params['source'], saved=True)
        parts = urlsplit(row['url'])
        if parts.scheme not in ('ntkndi', 'rtsp', 'http', 'rtmp', 'udp', 'srt', 'rtp'):
            raise CommandRefusedError('This protocol has no documented buffer-range query. Edit its URL in the device web interface.')
        limits = await self._call('get-buffer-limit', {'proto': parts.scheme})
        value = int(params['milliseconds'])
        _check_buffer_range(limits, value)
        query = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
                 if key != 'mw-buffer-duration']
        query.append(('mw-buffer-duration', str(value)))
        await self._modify_source(row, url=urlunsplit(parts._replace(query=urlencode(query))))

    async def _verify_presets(self, expected):
        await self._read_sources()
        for row in expected:
            reported = self._rosters['source'].get(_source_id(row['name'], False), {})
            if any(reported.get(key) != value for key, value in row.items()):
                raise CommandRefusedError('The decoder did not confirm the saved preset. Refresh Status to inspect its source list.')

    async def _download_sources(self, params):
        # list-channels is available on both versions and has exactly the
        # documented import/export shape. Downloading does not expose URL state.
        await self._read_sources()
        rows = [{key: row[key] for key in ('name', 'url', 'hotkey') if key in row}
                for row in self._rosters['source'].values() if not row['ndi']]
        data = json.dumps({'channels': rows}, ensure_ascii=False, indent=2).encode()
        return DeviceFile('pro-convert-sources.json', data, 'application/json')

    async def _import_sources(self, params):
        try:
            data = json.loads(await params['file'].read())
        except (ValueError, UnicodeError) as exc:
            raise CommandRefusedError('Choose a valid Magewell source JSON file.') from exc
        rows = _list(data, 'channels')
        names = set()
        for row in rows:
            _validate_preset(row, self._modern)
            if row['name'] in names:
                raise CommandRefusedError('The file contains duplicate preset names. Give each preset a unique name.')
            names.add(row['name'])
            self._protect_url(row['url'])
        clean = [{key: row[key] for key in ('name', 'url', 'hotkey') if key in row} for row in rows]
        blob = json.dumps({'channels': clean}).encode()
        await self._call('import-channels', files={'file': ('channels.json', blob, 'application/json')})
        await self._verify_presets(clean)

    async def _download_edid(self, params):
        data = await self._call('get-output-edid')
        try:
            blob = base64.b64decode(data['data'], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise CommandRefusedError('The decoder returned no valid output EDID.') from exc
        if not blob or len(blob) > 32768:
            raise CommandRefusedError('The decoder returned no valid output EDID.')
        return DeviceFile('pro-convert-output-edid.bin', blob)

    async def _reset_video(self, params):
        await self._call('reset-video-config')
        await self._read_config('video')
        await self._read_modes()


_CONFIG_PATHS = {'format': 'video-format', 'hdmi': 'hdmi-output'}
_MODE_FIELDS = ('width', 'height', 'interlaced', 'field-rate', 'aspect-ratio')


def _request_parts(method, params, upload, post_login):
    params = params or {}
    query = {'method': method}
    if not post_login:
        query.update({key: _wire(value) for key, value in params.items()})
    return query, params if post_login else None, 'POST' if upload or post_login else 'GET'


def _reply_urls(data):
    rows = data.get('channels', [])
    if not isinstance(rows, list):
        rows = []
    rows = rows + [data.get('ndi', {})]
    return [row['url'] for row in rows if isinstance(row, dict) and isinstance(row.get('url'), str)]


def _check_buffer_range(limits, value):
    lower, upper = limits.get('buffer-duration-min'), limits.get('buffer-duration-max')
    if not isinstance(lower, int) or not isinstance(upper, int):
        raise CommandRefusedError('The decoder returned no buffer range for this protocol.')
    if not lower <= value <= upper:
        raise CommandRefusedError(f'This protocol accepts a buffer from {lower} to {upper} ms.')


def _wire(value):
    return str(value).lower() if isinstance(value, bool) else str(value)


def _firmware_pair(value):
    match = re.match(r'(\d+)\.(\d+)', str(value))
    return tuple(map(int, match.groups())) if match else (0, 0)


def _source_id(name, ndi):
    return ('n_' if ndi else 'p_') + hashlib.sha256(name.encode()).hexdigest()[:24]


def _source_values(name, ndi, selected):
    kind = 'NDI' if ndi else 'Preset'
    return {'name': name, 'label': name + ' (' + kind + ')', 'kind': kind, 'protocol': 'NDI' if ndi else '',
            'address': '', 'selected': selected, 'ambiguous': False}


def _enum_value(value):
    return value['value'] if isinstance(value, dict) else value


def _reported_setting(key, wire, data):
    # Do not briefly publish the manual color on every poll while Auto is on:
    # that would fire state-change triggers with a value the device is not using.
    if key == 'input_color' and data.get('in-auto-color-fmt'):
        return 'auto'
    return data.get(wire)


def _mode_identity(mode):
    raw = json.dumps([mode[key] for key in _MODE_FIELDS])
    ident = hashlib.sha256(raw.encode()).hexdigest()[:24]
    scan = 'i' if mode['interlaced'] else 'p'
    name = f"{mode['width']} × {mode['height']}{scan} {mode['field-rate'] / 100:g} Hz"
    return ident, name


def _has_strings(row, *keys):
    return isinstance(row, dict) and all(isinstance(row.get(key), str) and row[key] for key in keys)


def _list(data, key):
    rows = data.get(key) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise CommandRefusedError('The decoder returned no valid ' + key + ' list.')
    return rows


def _setting_value(key, value, modern):
    if key in ('audio_sample_rate', 'audio_channel_count'):
        return int(value)
    if key == 'ident_text':
        valid = len(str(value).encode()) <= 127 if modern else bool(re.fullmatch(r'[A-Za-z0-9 _+\-]{1,32}', str(value)))
        if not valid:
            raise CommandRefusedError('Identification text exceeds this firmware’s supported length or characters.')
    if key == 'discovery_servers':
        _validate_addresses(value, False)
    if key == 'multicast_subnets':
        _validate_addresses(value, True)
    return value


def _validate_addresses(value, network):
    if not str(value).strip():
        return
    try:
        for part in str(value).split(','):
            if network:
                ipaddress.IPv4Network(part.strip())
            else:
                ipaddress.IPv4Address(part.strip())
    except ValueError as exc:
        detail = 'IPv4 CIDR subnets' if network else 'IPv4 addresses'
        raise CommandRefusedError('Enter comma-separated ' + detail + '.') from exc


def _check_discovery(key, value, config):
    enabled = value if key == 'discovery_enabled' else config.get('enable-discovery')
    servers = value if key == 'discovery_servers' else config.get('discovery-server', '')
    if enabled and not str(servers).strip():
        raise CommandRefusedError('Enter NDI Discovery Servers before enabling discovery-server reception.')


def _validate_preset(row, modern):
    if not _has_strings(row, 'name', 'url'):
        raise CommandRefusedError('Every preset needs a name and stream URL.')
    if not re.fullmatch(r'[ -~]{1,120}', row['name']):
        raise CommandRefusedError('Use a preset name of 1–120 ASCII characters.')
    _validate_url(row['url'], modern)
    if 'hotkey' in row and not re.fullmatch(r'(?:none|ctrl\+[a-z0-9])', str(row['hotkey'])):
        raise CommandRefusedError('Preset shortcuts must be none or ctrl+ followed by a lowercase letter or digit.')


def _validate_url(value, modern):
    schemes = {'ntkndi', 'rtsp', 'http', 'rtmp', 'udp', 'srt', 'rtp'}
    if modern:
        schemes.add('rtmps')
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise CommandRefusedError('Enter a valid stream URL.') from exc
    valid = parts.scheme in schemes and bool(parts.netloc)
    if not valid or re.search(r'[\x00-\x1f]', value):
        raise CommandRefusedError('Use a supported NDI, RTSP, HTTP, RTMP, UDP, SRT or RTP stream URL.')


class DecoderError(CommandRefusedError):
    def __init__(self, operation, code):
        self.code = code
        reasons = {5: 'is not implemented on this decoder', 7: 'contains an invalid value',
                   9: 'is not supported by this decoder', 11: 'cannot run while the decoder is busy',
                   16: 'was not found', 27: 'is outside the supported range',
                   31: 'requires an account with permission', 36: 'was refused during authentication',
                   37: 'requires a new session', 401: 'requires a new session',
                   403: 'requires an account with permission'}
        super().__init__(operation + ' ' + reasons.get(code, f'was refused (status {code})') + '.')


def _check_login(response):
    try:
        _decode(response, 'Login')
    except DecoderError as exc:
        if exc.code in (31, 36, 37, 401, 403):
            raise ConnectionFaultError('The decoder refused the account login.', code='auth_failed') from exc
        if exc.code in (5, 9, 405):
            raise ConnectionFaultError('This firmware does not support the chosen login method. Select the matching model or enable Legacy Login for firmware 1.1.', code='invalid_config') from exc
        raise


def _check_access(error, core):
    fatal = {36, 37, 401}
    if core:
        fatal.update((31, 403))
    if error.code in fatal:
        raise ConnectionFaultError('The decoder account cannot read control status.', code='auth_failed') from error


def _decode(response, operation):
    if response.status_code != 200:
        raise DecoderError(operation, response.status_code)
    data = _json_response(response, operation)
    if data['status'] != 0:
        raise DecoderError(operation, data['status'])
    return data


def _json_response(response, operation):
    try:
        data = response.json()
    except ValueError as exc:
        raise CommandRefusedError('The decoder returned an unreadable response to ' + operation + '.') from exc
    if not isinstance(data, dict) or not isinstance(data.get('status'), int):
        raise CommandRefusedError('The decoder returned no status for ' + operation + '.')
    return data
