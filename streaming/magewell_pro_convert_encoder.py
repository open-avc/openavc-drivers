"""Magewell Pro Convert encoder HTTP API.

Python handles the MD5/cookie session, device-enumerated backup sources and
capability-dependent settings, including paired automatic/manual video values
and mutually exclusive NDI transport flags. The public encoder API documents
request/response only, so status is polled. Every write is read back; a successful
status code alone does not change feedback. Parameter tables take precedence over
stale video examples (contrast=0, deinterlace=wave, frame-rate-raw and output rgb).

Source: https://www.magewell.com/api-docs/pro-convert-encoder-api/
"""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import hashlib
import ipaddress

import httpx
from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError
from openavc.drivers.command_files import DeviceFile

_FIELDS = {'brightness': ('video', 'brightness'),
 'contrast': ('video', 'contrast'),
 'hue': ('video', 'hue'),
 'saturation': ('video', 'saturation'),
 'bitrate_ratio': ('video', 'bit-rate-ratio'),
 'vertical_flip': ('video', 'out-flip'),
 'horizontal_flip': ('video', 'out-mirror'),
 'low_bandwidth_full_rate': ('video', 'low-res-full-fr'),
 'deinterlace': ('video', 'deinterlace'),
 'aspect_conversion': ('video', 'ar-convertion'),
 'frame_rate_divisor': ('video', 'out-fr-convertion'),
 'input_color': ('video', 'in-color-fmt'),
 'output_color': ('video', 'out-color-fmt'),
 'input_range': ('video', 'in-quant-range'),
 'output_range': ('video', 'out-quant-range'),
 'output_saturation_range': ('video', 'out-sat-range'),
 'ndi_enabled': ('ndi', 'enable'),
 'ndi_web_control': ('ndi', 'enable-web-control'),
 'ndi_ptz_control': ('ndi', 'enable-ptz-control'),
 'failover_enabled': ('ndi', 'enable-fail-over'),
 'discovery_enabled': ('ndi', 'enable-discovery'),
 'source_name': ('ndi', 'source-name'),
 'group_name': ('ndi', 'group-name'),
 'discovery_servers': ('ndi', 'discovery-server'),
 'ndi_transport': ('ndi', ''),
 'multicast_address': ('ndi', 'mcast-addr'),
 'multicast_mask': ('ndi', 'mcast-mask'),
 'multicast_ttl': ('ndi', 'mcast-ttl'),
 'audio_reference': ('ndi', 'reference-level'),
 'smart_edid': ('edid', 'smart-edid'),
 'keep_last_edid': ('edid', 'keep-last'),
 'edid_add_audio': ('edid', 'add-audio'),
 'edid_limit_clock': ('edid', 'limit-pixel-clock'),
 'custom_tally': ('tally', 'ext-tally'),
 'ptz_protocol': ('ptz', 'proto'),
 'ptz_index': ('ptz', 'index'),
 'ptz_baud': ('ptz', 'baudrate'),
 'ptz_invert_pan': ('ptz', 'invert-pan'),
 'ptz_invert_tilt': ('ptz', 'invert-tilt'),
 'ptz_visca_header': ('ptz', 'visca-msg-hdr'),
 'ptz_address': ('ptz', 'ip-addr'),
 'ptz_port': ('ptz', 'port'),
 'ptz_focus_near_limit': ('ptz', 'focus-near-limit'),
 'ptz_focus_far_limit': ('ptz', 'focus-far-limit'),
 'ptz_pan_left_limit': ('ptz', 'pan-left-limit'),
 'ptz_pan_center': ('ptz', 'pan-center'),
 'ptz_pan_right_limit': ('ptz', 'pan-right-limit'),
 'ptz_tilt_top_limit': ('ptz', 'tilt-top-limit'),
 'ptz_tilt_center': ('ptz', 'tilt-center'),
 'ptz_tilt_bottom_limit': ('ptz', 'tilt-bottom-limit'),
 'ptz_zoom_out_limit': ('ptz', 'zoom-out-limit')}

_VIDEO_PAIRS = {'input_color': ('in-auto-color-fmt', 'in-color-fmt'), 'input_range': ('in-auto-quant-range', 'in-quant-range'), 'output_color': ('out-auto-color-fmt', 'out-color-fmt'), 'output_range': ('out-auto-quant-range', 'out-quant-range'), 'output_saturation_range': ('out-auto-sat-range', 'out-sat-range')}

_SUMMARY = {'device': {'name': 'device_name',
            'model': 'model',
            'serial-no': 'serial_number',
            'fw-version': 'firmware_version',
            'hw-revision': 'hardware_revision',
            'input-state': 'input_signal',
            'output-state': 'loopthrough_status',
            'ptz-state': 'camera_status',
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
 'ndi': {'name': 'ndi_name',
         'enabled': 'ndi_enabled',
         'num-clients': 'ndi_clients',
         'tally-preview': 'tally_preview',
         'tally-program': 'tally_program',
         'audio-drop-frames': 'audio_dropped',
         'video-drop-frames': 'video_dropped',
         'video-bit-rate': 'video_bitrate',
         'audio-bit-rate': 'audio_bitrate',
         'video-width': 'ndi_width',
         'video-height': 'ndi_height',
         'video-scan': 'ndi_scan',
         'video-field-rate': 'ndi_frame_rate',
         'audio-num-channels': 'ndi_audio_channels',
         'audio-sample-rate': 'ndi_audio_rate',
         'audio-bit-count': 'ndi_audio_depth'}}

_SIGNAL = {'video-info': {'width': 'input_width',
                'height': 'input_height',
                'scan': 'input_scan',
                'field-rate': 'input_frame_rate',
                'color-format': 'input_color_reported',
                'color-depth': 'input_color_depth',
                'quant-range': 'input_range_reported',
                'sampling': 'input_sampling',
                'aspect-ratio': 'input_aspect'},
 'audio-info': {'codec': 'audio_codec',
                'num-channels': 'audio_channels',
                'sample-rate': 'audio_sample_rate',
                'bit-count': 'audio_bit_depth'},
 'hdmi-info': {'hdcp': 'hdcp'},
 'sdi-info': {'link-speed': 'sdi_link_speed', 'link-type': 'sdi_link_type'}}

class MagewellProConvertEncoderDriver(BaseDriver):
    DRIVER_INFO = {'id': 'magewell_pro_convert_encoder',
     'name': 'Magewell Pro Convert Encoder',
     'manufacturer': 'Magewell',
     'category': 'streaming',
     'version': '1.0.0',
     'author': 'OpenAVC',
     'transport': 'http',
     'ports': [80, 443],
     'protocols': ['http'],
     'description': 'Controls Pro Convert HDMI and SDI NDI encoders, with signal diagnostics, discovered '
                    'backup sources, video settings, EDID and camera-link setup.',
     'source_url': 'https://www.magewell.com/api-docs/pro-convert-encoder-api/',
     'simulated': True,
     'verified': False,
     'min_platform_version': '0.37.2',
     'tags': ['ndi', 'encoder', 'hdmi', 'sdi', 'failover'],
     'compatible_models': [{'manufacturer': 'Magewell',
                            'models': ['Pro Convert HDMI 4K Plus',
                                       'Pro Convert HDMI Plus',
                                       'Pro Convert HDMI TX',
                                       'Pro Convert 12G SDI 4K Plus',
                                       'Pro Convert SDI 4K Plus',
                                       'Pro Convert SDI Plus',
                                       'Pro Convert SDI TX',
                                       'Pro Convert HDMI 4K Plus Module',
                                       'Pro Convert HDMI Plus Module',
                                       'Pro Convert 12G SDI 4K Plus Module',
                                       'Pro Convert SDI Plus Module'],
                            'confidence': 'untested',
                            'notes': 'Document-derived; not tested on hardware. Encoder API only. Pro '
                                     'Convert decoders and Ultra Encode use separate drivers.'}],
     'help': {'overview': 'Enable the NDI output, choose a backup source by name, and monitor input signal, '
                          'receiver count, tally and dropped frames. Video and NDI settings are read back '
                          'from the encoder. Controls follow the capabilities it reports.',
              'setup': 'Enter the encoder’s IP address and local username and password. The username is '
                       'case-sensitive. Use an administrator account for all settings. Match HTTP/HTTPS and '
                       'the web port to the device. Select a model to prepare a simulation; a connected '
                       'encoder reports its actual model and capabilities. Use the device web interface for '
                       'firmware, accounts and network addressing. PTZ settings configure the camera link '
                       'used by NDI receivers; they do not move a camera.',
              'connection': 'Polling updates live status every 3 seconds and configuration every 30 seconds '
                            'by default. Refresh Status also refreshes backup sources. Discovery offers '
                            'Magewell devices as possible matches; select the encoder driver for encoder '
                            'models.'},
     'default_config': {'port': 80,
                        'ssl': False,
                        'verify_ssl': False,
                        'username': 'Admin',
                        'password': '',
                        'model': 'Pro Convert HDMI 4K Plus',
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
                                      'default': False,
                                      'advanced': True},
                       'username': {'type': 'string',
                                    'label': 'Username',
                                    'default': 'Admin',
                                    'required': True},
                       'password': {'type': 'string', 'label': 'Password', 'secret': True, 'required': True},
                       'model': {'type': 'enum',
                                 'label': 'Model',
                                 'default': 'Pro Convert HDMI 4K Plus',
                                 'values': ['Pro Convert HDMI 4K Plus',
                                            'Pro Convert HDMI Plus',
                                            'Pro Convert HDMI TX',
                                            'Pro Convert 12G SDI 4K Plus',
                                            'Pro Convert SDI 4K Plus',
                                            'Pro Convert SDI Plus',
                                            'Pro Convert SDI TX',
                                            'Pro Convert HDMI 4K Plus Module',
                                            'Pro Convert HDMI Plus Module',
                                            'Pro Convert 12G SDI 4K Plus Module',
                                            'Pro Convert SDI Plus Module'],
                                 'learned_from': 'model',
                                 'help': 'Used for project preparation and simulation. Replaced with the '
                                         'connected encoder’s reported model.'},
                       'poll_interval': {'type': 'number',
                                         'label': 'Poll Interval (seconds)',
                                         'default': 3,
                                         'min': 0},
                       'detail_poll_every': {'type': 'integer',
                                             'label': 'Refresh Settings Every N Polls',
                                             'default': 10,
                                             'min': 1,
                                             'max': 100,
                                             'advanced': True}},
     'state_variables': {'brightness': {'type': 'integer',
                                        'label': 'Brightness',
                                        'control': True,
                                        'min': -100,
                                        'max': 100},
                         'contrast': {'type': 'integer',
                                      'label': 'Contrast',
                                      'control': True,
                                      'min': 50,
                                      'max': 200},
                         'hue': {'type': 'integer', 'label': 'Hue', 'control': True, 'min': -90, 'max': 90},
                         'saturation': {'type': 'integer',
                                        'label': 'Saturation',
                                        'control': True,
                                        'min': 0,
                                        'max': 200},
                         'bitrate_ratio': {'type': 'integer',
                                           'label': 'NDI Bitrate Ratio',
                                           'control': True,
                                           'min': 50,
                                           'max': 200},
                         'vertical_flip': {'type': 'boolean', 'label': 'Vertical Flip', 'control': True},
                         'horizontal_flip': {'type': 'boolean', 'label': 'Horizontal Flip', 'control': True},
                         'low_bandwidth_full_rate': {'type': 'boolean',
                                                     'label': 'Full Frame Rate for Low Bandwidth',
                                                     'control': True},
                         'deinterlace': {'type': 'enum',
                                         'label': 'Deinterlace',
                                         'control': True,
                                         'values': [{'value': 'none', 'label': 'None'},
                                                    {'value': 'top-field', 'label': 'Top Field'},
                                                    {'value': 'bottom-field', 'label': 'Bottom Field'}]},
                         'aspect_conversion': {'type': 'enum',
                                               'label': 'Aspect Ratio Conversion',
                                               'control': True,
                                               'values': [{'value': 'ignore', 'label': 'Stretch'},
                                                          {'value': 'cropping', 'label': 'Fill and Crop'},
                                                          {'value': 'padding',
                                                           'label': 'Fit with Borders'}]},
                         'frame_rate_divisor': {'type': 'enum',
                                                'label': 'Output Frame Rate',
                                                'control': True,
                                                'values': [{'value': 'raw', 'label': 'Match Input'},
                                                           {'value': 'half', 'label': 'Half'},
                                                           {'value': 'one-third', 'label': 'One Third'},
                                                           {'value': 'quarter', 'label': 'One Quarter'}]},
                         'input_color': {'type': 'enum',
                                         'label': 'Input Color Space',
                                         'control': True,
                                         'values': [{'value': 'auto', 'label': 'Automatic'},
                                                    {'value': 'rgb', 'label': 'RGB'},
                                                    {'value': 'bt.601', 'label': 'BT.601'},
                                                    {'value': 'bt.709', 'label': 'BT.709'},
                                                    {'value': 'bt.2020', 'label': 'BT.2020'}]},
                         'output_color': {'type': 'enum',
                                          'label': 'Output Color Space',
                                          'control': True,
                                          'values': [{'value': 'auto', 'label': 'Automatic'},
                                                     {'value': 'bt.601', 'label': 'BT.601'},
                                                     {'value': 'bt.709', 'label': 'BT.709'},
                                                     {'value': 'bt.2020', 'label': 'BT.2020'}]},
                         'input_range': {'type': 'enum',
                                         'label': 'Input Quantization Range',
                                         'control': True,
                                         'values': [{'value': 'auto', 'label': 'Automatic'},
                                                    {'value': 'full', 'label': 'Full'},
                                                    {'value': 'limited', 'label': 'Limited'}]},
                         'output_range': {'type': 'enum',
                                          'label': 'Output Quantization Range',
                                          'control': True,
                                          'values': [{'value': 'auto', 'label': 'Automatic'},
                                                     {'value': 'full', 'label': 'Full'},
                                                     {'value': 'limited', 'label': 'Limited'}]},
                         'output_saturation_range': {'type': 'enum',
                                                     'label': 'Output Saturation Range',
                                                     'control': True,
                                                     'values': [{'value': 'auto', 'label': 'Automatic'},
                                                                {'value': 'full', 'label': 'Full'},
                                                                {'value': 'limited', 'label': 'Limited'},
                                                                {'value': 'extended', 'label': 'Extended'}]},
                         'ndi_enabled': {'type': 'boolean',
                                         'label': 'NDI Output',
                                         'control': True,
                                         'cloud_priority': 'high'},
                         'ndi_web_control': {'type': 'boolean', 'label': 'NDI Web Control', 'control': True},
                         'ndi_ptz_control': {'type': 'boolean',
                                             'label': 'NDI Camera Control',
                                             'control': True},
                         'failover_enabled': {'type': 'boolean', 'label': 'NDI Failover', 'control': True},
                         'discovery_enabled': {'type': 'boolean',
                                               'label': 'NDI Discovery Server Enabled',
                                               'control': True},
                         'source_name': {'type': 'string', 'label': 'NDI Source Name', 'control': True},
                         'group_name': {'type': 'string', 'label': 'NDI Groups', 'control': True},
                         'discovery_servers': {'type': 'string',
                                               'label': 'NDI Discovery Servers',
                                               'control': True},
                         'ndi_transport': {'type': 'enum',
                                           'label': 'NDI Transport',
                                           'control': True,
                                           'values': [{'value': 'tcp', 'label': 'TCP Unicast'},
                                                      {'value': 'tcp_multi',
                                                       'label': 'TCP Multi-connection'},
                                                      {'value': 'udp', 'label': 'UDP Unicast'},
                                                      {'value': 'rudp', 'label': 'Reliable UDP Unicast'},
                                                      {'value': 'multicast', 'label': 'UDP Multicast'}]},
                         'multicast_address': {'type': 'string',
                                               'label': 'Multicast Address',
                                               'control': True},
                         'multicast_mask': {'type': 'string',
                                            'label': 'Multicast Subnet Mask',
                                            'control': True},
                         'multicast_ttl': {'type': 'integer',
                                           'label': 'Multicast TTL',
                                           'control': True,
                                           'min': 1,
                                           'max': 255},
                         'audio_reference': {'type': 'enum',
                                             'label': 'Audio Reference Level',
                                             'control': True,
                                             'values': [{'value': 20, 'label': 'SMPTE (20 dB)'},
                                                        {'value': 14, 'label': 'EBU (14 dB)'}]},
                         'smart_edid': {'type': 'boolean', 'label': 'SmartEDID', 'control': True},
                         'keep_last_edid': {'type': 'boolean',
                                            'label': 'Keep Last Loop-through EDID',
                                            'control': True},
                         'edid_add_audio': {'type': 'boolean',
                                            'label': 'EDID Audio Support',
                                            'control': True},
                         'edid_limit_clock': {'type': 'boolean',
                                              'label': 'Limit EDID Pixel Clock',
                                              'control': True},
                         'custom_tally': {'type': 'boolean',
                                          'label': 'Custom Tally Lights',
                                          'control': True,
                                          'help': 'Enables the encoder’s user-customized tally option. '
                                                  'Program and preview feedback still comes from NDI '
                                                  'receivers.'},
                         'ptz_protocol': {'type': 'enum',
                                          'label': 'Camera Control Protocol',
                                          'control': True,
                                          'values': [{'value': 'none', 'label': 'Disabled'},
                                                     {'value': 'visca', 'label': 'VISCA'},
                                                     {'value': 'visca-udp', 'label': 'VISCA-UDP'},
                                                     {'value': 'visca-udp2rs232',
                                                      'label': 'VISCA-UDP2RS232'},
                                                     {'value': 'pelco-d', 'label': 'PELCO-D'},
                                                     {'value': 'pelco-p', 'label': 'PELCO-P'}]},
                         'ptz_index': {'type': 'integer',
                                       'label': 'Camera Address',
                                       'control': True,
                                       'min': 1,
                                       'max': 7},
                         'ptz_baud': {'type': 'enum',
                                      'label': 'Camera Baud Rate',
                                      'control': True,
                                      'values': [{'value': 2400, 'label': '2400'},
                                                 {'value': 4800, 'label': '4800'},
                                                 {'value': 9600, 'label': '9600'},
                                                 {'value': 19200, 'label': '19200'},
                                                 {'value': 38400, 'label': '38400'}]},
                         'ptz_invert_pan': {'type': 'boolean',
                                            'label': 'Reverse Camera Pan',
                                            'control': True},
                         'ptz_invert_tilt': {'type': 'boolean',
                                             'label': 'Reverse Camera Tilt',
                                             'control': True},
                         'ptz_visca_header': {'type': 'boolean',
                                              'label': 'VISCA UDP Header',
                                              'control': True},
                         'ptz_address': {'type': 'string', 'label': 'Camera IP Address', 'control': True},
                         'ptz_port': {'type': 'integer',
                                      'label': 'Camera UDP Port',
                                      'control': True,
                                      'min': 1,
                                      'max': 65535},
                         'ptz_focus_near_limit': {'type': 'integer',
                                                  'label': 'Camera Focus Near Limit',
                                                  'control': True,
                                                  'min': 0,
                                                  'max': 65535},
                         'ptz_focus_far_limit': {'type': 'integer',
                                                 'label': 'Camera Focus Far Limit',
                                                 'control': True,
                                                 'min': 0,
                                                 'max': 65535},
                         'ptz_pan_left_limit': {'type': 'integer',
                                                'label': 'Camera Pan Left Limit',
                                                'control': True,
                                                'min': -32768,
                                                'max': 32767},
                         'ptz_pan_center': {'type': 'integer',
                                            'label': 'Camera Pan Center',
                                            'control': True,
                                            'min': -32768,
                                            'max': 32767},
                         'ptz_pan_right_limit': {'type': 'integer',
                                                 'label': 'Camera Pan Right Limit',
                                                 'control': True,
                                                 'min': -32768,
                                                 'max': 32767},
                         'ptz_tilt_top_limit': {'type': 'integer',
                                                'label': 'Camera Tilt Top Limit',
                                                'control': True,
                                                'min': -32768,
                                                'max': 32767},
                         'ptz_tilt_center': {'type': 'integer',
                                             'label': 'Camera Tilt Center',
                                             'control': True,
                                             'min': -32768,
                                             'max': 32767},
                         'ptz_tilt_bottom_limit': {'type': 'integer',
                                                   'label': 'Camera Tilt Bottom Limit',
                                                   'control': True,
                                                   'min': -32768,
                                                   'max': 32767},
                         'ptz_zoom_out_limit': {'type': 'integer',
                                                'label': 'Camera Zoom Out Limit',
                                                'control': True,
                                                'min': 0,
                                                'max': 32767},
                         'device_name': {'type': 'string', 'label': 'Device Name'},
                         'model': {'type': 'string', 'label': 'Model'},
                         'serial_number': {'type': 'string', 'label': 'Serial Number'},
                         'firmware_version': {'type': 'string', 'label': 'Firmware Version'},
                         'hardware_revision': {'type': 'string', 'label': 'Hardware Revision'},
                         'input_signal': {'type': 'string',
                                          'label': 'Input Signal',
                                          'cloud_priority': 'high'},
                         'loopthrough_status': {'type': 'string', 'label': 'Loopthrough Status'},
                         'camera_status': {'type': 'string', 'label': 'Camera Status'},
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
                         'uptime': {'type': 'integer', 'label': 'Uptime', 'unit': 's'},
                         'fan_speed': {'type': 'integer', 'label': 'Fan Speed', 'unit': 'RPM'},
                         'rotary_id': {'type': 'integer', 'label': 'Rotary Id'},
                         'ethernet_link': {'type': 'string', 'label': 'Ethernet Link'},
                         'mac_address': {'type': 'string', 'label': 'Mac Address'},
                         'ip_address': {'type': 'string', 'label': 'Ip Address'},
                         'network_tx': {'type': 'number',
                                        'label': 'Network Tx',
                                        'cloud_priority': 'low',
                                        'unit': 'kbps'},
                         'network_rx': {'type': 'number',
                                        'label': 'Network Rx',
                                        'cloud_priority': 'low',
                                        'unit': 'kbps'},
                         'ndi_name': {'type': 'string', 'label': 'Ndi Name'},
                         'ndi_clients': {'type': 'integer', 'label': 'Ndi Clients'},
                         'tally_preview': {'type': 'boolean',
                                           'label': 'Tally Preview',
                                           'cloud_priority': 'high'},
                         'tally_program': {'type': 'boolean',
                                           'label': 'Tally Program',
                                           'cloud_priority': 'high'},
                         'audio_dropped': {'type': 'integer', 'label': 'Audio Dropped'},
                         'video_dropped': {'type': 'integer', 'label': 'Video Dropped'},
                         'video_bitrate': {'type': 'number',
                                           'label': 'Video Bitrate',
                                           'cloud_priority': 'low',
                                           'unit': 'kbps'},
                         'audio_bitrate': {'type': 'number',
                                           'label': 'Audio Bitrate',
                                           'cloud_priority': 'low',
                                           'unit': 'kbps'},
                         'ndi_width': {'type': 'integer', 'label': 'Ndi Width'},
                         'ndi_height': {'type': 'integer', 'label': 'Ndi Height'},
                         'ndi_scan': {'type': 'string', 'label': 'Ndi Scan'},
                         'ndi_frame_rate': {'type': 'number',
                                            'label': 'Ndi Frame Rate',
                                            'cloud_priority': 'low',
                                            'unit': 'Hz'},
                         'ndi_audio_channels': {'type': 'integer', 'label': 'Ndi Audio Channels'},
                         'ndi_audio_rate': {'type': 'integer', 'label': 'Ndi Audio Rate', 'unit': 'Hz'},
                         'ndi_audio_depth': {'type': 'integer', 'label': 'Ndi Audio Depth'},
                         'input_width': {'type': 'integer', 'label': 'Input Width'},
                         'input_height': {'type': 'integer', 'label': 'Input Height'},
                         'input_scan': {'type': 'string', 'label': 'Input Scan'},
                         'input_frame_rate': {'type': 'number', 'label': 'Input Frame Rate', 'unit': 'Hz'},
                         'input_color_reported': {'type': 'string', 'label': 'Input Color Reported'},
                         'input_color_depth': {'type': 'integer', 'label': 'Input Color Depth'},
                         'input_range_reported': {'type': 'string', 'label': 'Input Range Reported'},
                         'input_sampling': {'type': 'string', 'label': 'Input Sampling'},
                         'input_aspect': {'type': 'string', 'label': 'Input Aspect'},
                         'audio_codec': {'type': 'string', 'label': 'Audio Codec'},
                         'audio_channels': {'type': 'integer', 'label': 'Audio Channels'},
                         'audio_sample_rate': {'type': 'integer',
                                               'label': 'Audio Sample Rate',
                                               'unit': 'Hz'},
                         'audio_bit_depth': {'type': 'integer', 'label': 'Audio Bit Depth'},
                         'hdcp': {'type': 'string', 'label': 'Hdcp'},
                         'sdi_link_speed': {'type': 'string', 'label': 'Sdi Link Speed'},
                         'sdi_link_type': {'type': 'string', 'label': 'Sdi Link Type'},
                         'output_width': {'type': 'integer', 'label': 'Configured Output Width'},
                         'output_height': {'type': 'integer', 'label': 'Configured Output Height'},
                         'match_input_resolution': {'type': 'boolean', 'label': 'Match Input Resolution'},
                         'failover_name': {'type': 'string', 'label': 'Selected Backup Source'},
                         'failover_address': {'type': 'string', 'label': 'Selected Backup Address'},
                         'last_error': {'type': 'string', 'label': 'Last Error'},
                         'input_edid_available': {'type': 'boolean', 'label': 'Input EDID Supported'},
                         'output_edid_available': {'type': 'boolean', 'label': 'Output EDID Supported'},
                         'ptz_available': {'type': 'boolean', 'label': 'Camera Control Supported'},
                         'max_output_width': {'type': 'integer', 'label': 'Maximum Output Width'},
                         'max_output_height': {'type': 'integer', 'label': 'Maximum Output Height'}},
     'device_settings': {'brightness': {'type': 'integer',
                                        'label': 'Brightness',
                                        'min': -100,
                                        'max': 100,
                                        'state_key': 'brightness'},
                         'contrast': {'type': 'integer',
                                      'label': 'Contrast',
                                      'min': 50,
                                      'max': 200,
                                      'state_key': 'contrast'},
                         'hue': {'type': 'integer',
                                 'label': 'Hue',
                                 'min': -90,
                                 'max': 90,
                                 'state_key': 'hue'},
                         'saturation': {'type': 'integer',
                                        'label': 'Saturation',
                                        'min': 0,
                                        'max': 200,
                                        'state_key': 'saturation'},
                         'bitrate_ratio': {'type': 'integer',
                                           'label': 'NDI Bitrate Ratio',
                                           'min': 50,
                                           'max': 200,
                                           'state_key': 'bitrate_ratio'},
                         'vertical_flip': {'type': 'boolean',
                                           'label': 'Vertical Flip',
                                           'state_key': 'vertical_flip'},
                         'horizontal_flip': {'type': 'boolean',
                                             'label': 'Horizontal Flip',
                                             'state_key': 'horizontal_flip'},
                         'low_bandwidth_full_rate': {'type': 'boolean',
                                                     'label': 'Full Frame Rate for Low Bandwidth',
                                                     'state_key': 'low_bandwidth_full_rate'},
                         'deinterlace': {'type': 'enum',
                                         'label': 'Deinterlace',
                                         'values': [{'value': 'none', 'label': 'None'},
                                                    {'value': 'top-field', 'label': 'Top Field'},
                                                    {'value': 'bottom-field', 'label': 'Bottom Field'}],
                                         'state_key': 'deinterlace'},
                         'aspect_conversion': {'type': 'enum',
                                               'label': 'Aspect Ratio Conversion',
                                               'values': [{'value': 'ignore', 'label': 'Stretch'},
                                                          {'value': 'cropping', 'label': 'Fill and Crop'},
                                                          {'value': 'padding', 'label': 'Fit with Borders'}],
                                               'state_key': 'aspect_conversion'},
                         'frame_rate_divisor': {'type': 'enum',
                                                'label': 'Output Frame Rate',
                                                'values': [{'value': 'raw', 'label': 'Match Input'},
                                                           {'value': 'half', 'label': 'Half'},
                                                           {'value': 'one-third', 'label': 'One Third'},
                                                           {'value': 'quarter', 'label': 'One Quarter'}],
                                                'state_key': 'frame_rate_divisor'},
                         'input_color': {'type': 'enum',
                                         'label': 'Input Color Space',
                                         'values': [{'value': 'auto', 'label': 'Automatic'},
                                                    {'value': 'rgb', 'label': 'RGB'},
                                                    {'value': 'bt.601', 'label': 'BT.601'},
                                                    {'value': 'bt.709', 'label': 'BT.709'},
                                                    {'value': 'bt.2020', 'label': 'BT.2020'}],
                                         'state_key': 'input_color'},
                         'output_color': {'type': 'enum',
                                          'label': 'Output Color Space',
                                          'values': [{'value': 'auto', 'label': 'Automatic'},
                                                     {'value': 'bt.601', 'label': 'BT.601'},
                                                     {'value': 'bt.709', 'label': 'BT.709'},
                                                     {'value': 'bt.2020', 'label': 'BT.2020'}],
                                          'state_key': 'output_color'},
                         'input_range': {'type': 'enum',
                                         'label': 'Input Quantization Range',
                                         'values': [{'value': 'auto', 'label': 'Automatic'},
                                                    {'value': 'full', 'label': 'Full'},
                                                    {'value': 'limited', 'label': 'Limited'}],
                                         'state_key': 'input_range'},
                         'output_range': {'type': 'enum',
                                          'label': 'Output Quantization Range',
                                          'values': [{'value': 'auto', 'label': 'Automatic'},
                                                     {'value': 'full', 'label': 'Full'},
                                                     {'value': 'limited', 'label': 'Limited'}],
                                          'state_key': 'output_range'},
                         'output_saturation_range': {'type': 'enum',
                                                     'label': 'Output Saturation Range',
                                                     'values': [{'value': 'auto', 'label': 'Automatic'},
                                                                {'value': 'full', 'label': 'Full'},
                                                                {'value': 'limited', 'label': 'Limited'},
                                                                {'value': 'extended', 'label': 'Extended'}],
                                                     'state_key': 'output_saturation_range'},
                         'ndi_enabled': {'type': 'boolean',
                                         'label': 'NDI Output',
                                         'state_key': 'ndi_enabled'},
                         'ndi_web_control': {'type': 'boolean',
                                             'label': 'NDI Web Control',
                                             'state_key': 'ndi_web_control'},
                         'ndi_ptz_control': {'type': 'boolean',
                                             'label': 'NDI Camera Control',
                                             'state_key': 'ndi_ptz_control'},
                         'failover_enabled': {'type': 'boolean',
                                              'label': 'NDI Failover',
                                              'state_key': 'failover_enabled'},
                         'discovery_enabled': {'type': 'boolean',
                                               'label': 'NDI Discovery Server Enabled',
                                               'state_key': 'discovery_enabled'},
                         'source_name': {'type': 'string',
                                         'label': 'NDI Source Name',
                                         'state_key': 'source_name',
                                         'help': 'Name advertised to NDI receivers. %board-id% and '
                                                 '%serial-no% insert the rotary switch number and serial '
                                                 'number.'},
                         'group_name': {'type': 'string',
                                        'label': 'NDI Groups',
                                        'state_key': 'group_name',
                                        'help': 'Separate multiple NDI groups with commas.'},
                         'discovery_servers': {'type': 'string',
                                               'label': 'NDI Discovery Servers',
                                               'state_key': 'discovery_servers',
                                               'help': 'Separate multiple discovery server IP addresses '
                                                       'with commas.'},
                         'ndi_transport': {'type': 'enum',
                                           'label': 'NDI Transport',
                                           'values': [{'value': 'tcp', 'label': 'TCP Unicast'},
                                                      {'value': 'tcp_multi',
                                                       'label': 'TCP Multi-connection'},
                                                      {'value': 'udp', 'label': 'UDP Unicast'},
                                                      {'value': 'rudp', 'label': 'Reliable UDP Unicast'},
                                                      {'value': 'multicast', 'label': 'UDP Multicast'}],
                                           'state_key': 'ndi_transport'},
                         'multicast_address': {'type': 'string',
                                               'label': 'Multicast Address',
                                               'state_key': 'multicast_address'},
                         'multicast_mask': {'type': 'string',
                                            'label': 'Multicast Subnet Mask',
                                            'state_key': 'multicast_mask'},
                         'multicast_ttl': {'type': 'integer',
                                           'label': 'Multicast TTL',
                                           'min': 1,
                                           'max': 255,
                                           'state_key': 'multicast_ttl'},
                         'audio_reference': {'type': 'enum',
                                             'label': 'Audio Reference Level',
                                             'values': [{'value': 20, 'label': 'SMPTE (20 dB)'},
                                                        {'value': 14, 'label': 'EBU (14 dB)'}],
                                             'state_key': 'audio_reference'},
                         'smart_edid': {'type': 'boolean', 'label': 'SmartEDID', 'state_key': 'smart_edid'},
                         'keep_last_edid': {'type': 'boolean',
                                            'label': 'Keep Last Loop-through EDID',
                                            'state_key': 'keep_last_edid'},
                         'edid_add_audio': {'type': 'boolean',
                                            'label': 'EDID Audio Support',
                                            'state_key': 'edid_add_audio'},
                         'edid_limit_clock': {'type': 'boolean',
                                              'label': 'Limit EDID Pixel Clock',
                                              'state_key': 'edid_limit_clock'},
                         'custom_tally': {'type': 'boolean',
                                          'label': 'Custom Tally Lights',
                                          'help': 'Enables the encoder’s user-customized tally option. '
                                                  'Program and preview feedback still comes from NDI '
                                                  'receivers.',
                                          'state_key': 'custom_tally'},
                         'ptz_protocol': {'type': 'enum',
                                          'label': 'Camera Control Protocol',
                                          'values': [{'value': 'none', 'label': 'Disabled'},
                                                     {'value': 'visca', 'label': 'VISCA'},
                                                     {'value': 'visca-udp', 'label': 'VISCA-UDP'},
                                                     {'value': 'visca-udp2rs232',
                                                      'label': 'VISCA-UDP2RS232'},
                                                     {'value': 'pelco-d', 'label': 'PELCO-D'},
                                                     {'value': 'pelco-p', 'label': 'PELCO-P'}],
                                          'state_key': 'ptz_protocol'},
                         'ptz_index': {'type': 'integer',
                                       'label': 'Camera Address',
                                       'min': 1,
                                       'max': 7,
                                       'state_key': 'ptz_index'},
                         'ptz_baud': {'type': 'enum',
                                      'label': 'Camera Baud Rate',
                                      'values': [{'value': 2400, 'label': '2400'},
                                                 {'value': 4800, 'label': '4800'},
                                                 {'value': 9600, 'label': '9600'},
                                                 {'value': 19200, 'label': '19200'},
                                                 {'value': 38400, 'label': '38400'}],
                                      'state_key': 'ptz_baud'},
                         'ptz_invert_pan': {'type': 'boolean',
                                            'label': 'Reverse Camera Pan',
                                            'state_key': 'ptz_invert_pan'},
                         'ptz_invert_tilt': {'type': 'boolean',
                                             'label': 'Reverse Camera Tilt',
                                             'state_key': 'ptz_invert_tilt'},
                         'ptz_visca_header': {'type': 'boolean',
                                              'label': 'VISCA UDP Header',
                                              'state_key': 'ptz_visca_header'},
                         'ptz_address': {'type': 'string',
                                         'label': 'Camera IP Address',
                                         'state_key': 'ptz_address'},
                         'ptz_port': {'type': 'integer',
                                      'label': 'Camera UDP Port',
                                      'min': 1,
                                      'max': 65535,
                                      'state_key': 'ptz_port'},
                         'ptz_focus_near_limit': {'type': 'integer',
                                                  'label': 'Camera Focus Near Limit',
                                                  'min': 0,
                                                  'max': 65535,
                                                  'state_key': 'ptz_focus_near_limit'},
                         'ptz_focus_far_limit': {'type': 'integer',
                                                 'label': 'Camera Focus Far Limit',
                                                 'min': 0,
                                                 'max': 65535,
                                                 'state_key': 'ptz_focus_far_limit'},
                         'ptz_pan_left_limit': {'type': 'integer',
                                                'label': 'Camera Pan Left Limit',
                                                'min': -32768,
                                                'max': 32767,
                                                'state_key': 'ptz_pan_left_limit'},
                         'ptz_pan_center': {'type': 'integer',
                                            'label': 'Camera Pan Center',
                                            'min': -32768,
                                            'max': 32767,
                                            'state_key': 'ptz_pan_center'},
                         'ptz_pan_right_limit': {'type': 'integer',
                                                 'label': 'Camera Pan Right Limit',
                                                 'min': -32768,
                                                 'max': 32767,
                                                 'state_key': 'ptz_pan_right_limit'},
                         'ptz_tilt_top_limit': {'type': 'integer',
                                                'label': 'Camera Tilt Top Limit',
                                                'min': -32768,
                                                'max': 32767,
                                                'state_key': 'ptz_tilt_top_limit'},
                         'ptz_tilt_center': {'type': 'integer',
                                             'label': 'Camera Tilt Center',
                                             'min': -32768,
                                             'max': 32767,
                                             'state_key': 'ptz_tilt_center'},
                         'ptz_tilt_bottom_limit': {'type': 'integer',
                                                   'label': 'Camera Tilt Bottom Limit',
                                                   'min': -32768,
                                                   'max': 32767,
                                                   'state_key': 'ptz_tilt_bottom_limit'},
                         'ptz_zoom_out_limit': {'type': 'integer',
                                                'label': 'Camera Zoom Out Limit',
                                                'min': 0,
                                                'max': 32767,
                                                'state_key': 'ptz_zoom_out_limit'}},
     'commands': {'refresh': {'label': 'Refresh Status',
                              'help': 'Reads signal, NDI status, settings and available backup sources from '
                                      'the encoder.'},
                  'set_brightness': {'label': 'Set Brightness',
                                     'help': 'Changes brightness and reads the value back from the encoder.',
                                     'params': {'value': {'type': 'integer',
                                                          'label': 'Brightness',
                                                          'min': -100,
                                                          'max': 100,
                                                          'required': True}},
                                     'sets': {'brightness': '{value}'}},
                  'set_contrast': {'label': 'Set Contrast',
                                   'help': 'Changes contrast and reads the value back from the encoder.',
                                   'params': {'value': {'type': 'integer',
                                                        'label': 'Contrast',
                                                        'min': 50,
                                                        'max': 200,
                                                        'required': True}},
                                   'sets': {'contrast': '{value}'}},
                  'set_hue': {'label': 'Set Hue',
                              'help': 'Changes hue and reads the value back from the encoder.',
                              'params': {'value': {'type': 'integer',
                                                   'label': 'Hue',
                                                   'min': -90,
                                                   'max': 90,
                                                   'required': True}},
                              'sets': {'hue': '{value}'}},
                  'set_saturation': {'label': 'Set Saturation',
                                     'help': 'Changes saturation and reads the value back from the encoder.',
                                     'params': {'value': {'type': 'integer',
                                                          'label': 'Saturation',
                                                          'min': 0,
                                                          'max': 200,
                                                          'required': True}},
                                     'sets': {'saturation': '{value}'}},
                  'set_bitrate_ratio': {'label': 'Set NDI Bitrate Ratio',
                                        'help': 'Changes ndi bitrate ratio and reads the value back from '
                                                'the encoder.',
                                        'params': {'value': {'type': 'integer',
                                                             'label': 'NDI Bitrate Ratio',
                                                             'min': 50,
                                                             'max': 200,
                                                             'required': True}},
                                        'sets': {'bitrate_ratio': '{value}'}},
                  'set_vertical_flip': {'label': 'Set Vertical Flip',
                                        'help': 'Changes vertical flip and reads the value back from the '
                                                'encoder.',
                                        'params': {'value': {'type': 'boolean',
                                                             'label': 'Vertical Flip',
                                                             'required': True}},
                                        'sets': {'vertical_flip': '{value}'}},
                  'set_horizontal_flip': {'label': 'Set Horizontal Flip',
                                          'help': 'Changes horizontal flip and reads the value back from '
                                                  'the encoder.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Horizontal Flip',
                                                               'required': True}},
                                          'sets': {'horizontal_flip': '{value}'}},
                  'set_low_bandwidth_full_rate': {'label': 'Set Full Frame Rate for Low Bandwidth',
                                                  'help': 'Changes full frame rate for low bandwidth and '
                                                          'reads the value back from the encoder.',
                                                  'params': {'value': {'type': 'boolean',
                                                                       'label': 'Full Frame Rate for Low '
                                                                                'Bandwidth',
                                                                       'required': True}},
                                                  'sets': {'low_bandwidth_full_rate': '{value}'}},
                  'set_deinterlace': {'label': 'Set Deinterlace',
                                      'help': 'Changes deinterlace and reads the value back from the '
                                              'encoder.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Deinterlace',
                                                           'values': [{'value': 'none', 'label': 'None'},
                                                                      {'value': 'top-field',
                                                                       'label': 'Top Field'},
                                                                      {'value': 'bottom-field',
                                                                       'label': 'Bottom Field'}],
                                                           'required': True}},
                                      'sets': {'deinterlace': '{value}'}},
                  'set_aspect_conversion': {'label': 'Set Aspect Ratio Conversion',
                                            'help': 'Changes aspect ratio conversion and reads the value '
                                                    'back from the encoder.',
                                            'params': {'value': {'type': 'enum',
                                                                 'label': 'Aspect Ratio Conversion',
                                                                 'values': [{'value': 'ignore',
                                                                             'label': 'Stretch'},
                                                                            {'value': 'cropping',
                                                                             'label': 'Fill and Crop'},
                                                                            {'value': 'padding',
                                                                             'label': 'Fit with Borders'}],
                                                                 'required': True}},
                                            'sets': {'aspect_conversion': '{value}'}},
                  'set_frame_rate_divisor': {'label': 'Set Output Frame Rate',
                                             'help': 'Changes output frame rate and reads the value back '
                                                     'from the encoder.',
                                             'params': {'value': {'type': 'enum',
                                                                  'label': 'Output Frame Rate',
                                                                  'values': [{'value': 'raw',
                                                                              'label': 'Match Input'},
                                                                             {'value': 'half',
                                                                              'label': 'Half'},
                                                                             {'value': 'one-third',
                                                                              'label': 'One Third'},
                                                                             {'value': 'quarter',
                                                                              'label': 'One Quarter'}],
                                                                  'required': True}},
                                             'sets': {'frame_rate_divisor': '{value}'}},
                  'set_input_color': {'label': 'Set Input Color Space',
                                      'help': 'Changes input color space and reads the value back from the '
                                              'encoder.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Input Color Space',
                                                           'values': [{'value': 'auto',
                                                                       'label': 'Automatic'},
                                                                      {'value': 'rgb', 'label': 'RGB'},
                                                                      {'value': 'bt.601', 'label': 'BT.601'},
                                                                      {'value': 'bt.709', 'label': 'BT.709'},
                                                                      {'value': 'bt.2020',
                                                                       'label': 'BT.2020'}],
                                                           'required': True}},
                                      'sets': {'input_color': '{value}'}},
                  'set_output_color': {'label': 'Set Output Color Space',
                                       'help': 'Changes output color space and reads the value back from '
                                               'the encoder.',
                                       'params': {'value': {'type': 'enum',
                                                            'label': 'Output Color Space',
                                                            'values': [{'value': 'auto',
                                                                        'label': 'Automatic'},
                                                                       {'value': 'bt.601',
                                                                        'label': 'BT.601'},
                                                                       {'value': 'bt.709',
                                                                        'label': 'BT.709'},
                                                                       {'value': 'bt.2020',
                                                                        'label': 'BT.2020'}],
                                                            'required': True}},
                                       'sets': {'output_color': '{value}'}},
                  'set_input_range': {'label': 'Set Input Quantization Range',
                                      'help': 'Changes input quantization range and reads the value back '
                                              'from the encoder.',
                                      'params': {'value': {'type': 'enum',
                                                           'label': 'Input Quantization Range',
                                                           'values': [{'value': 'auto',
                                                                       'label': 'Automatic'},
                                                                      {'value': 'full', 'label': 'Full'},
                                                                      {'value': 'limited',
                                                                       'label': 'Limited'}],
                                                           'required': True}},
                                      'sets': {'input_range': '{value}'}},
                  'set_output_range': {'label': 'Set Output Quantization Range',
                                       'help': 'Changes output quantization range and reads the value back '
                                               'from the encoder.',
                                       'params': {'value': {'type': 'enum',
                                                            'label': 'Output Quantization Range',
                                                            'values': [{'value': 'auto',
                                                                        'label': 'Automatic'},
                                                                       {'value': 'full', 'label': 'Full'},
                                                                       {'value': 'limited',
                                                                        'label': 'Limited'}],
                                                            'required': True}},
                                       'sets': {'output_range': '{value}'}},
                  'set_output_saturation_range': {'label': 'Set Output Saturation Range',
                                                  'help': 'Changes output saturation range and reads the '
                                                          'value back from the encoder.',
                                                  'params': {'value': {'type': 'enum',
                                                                       'label': 'Output Saturation Range',
                                                                       'values': [{'value': 'auto',
                                                                                   'label': 'Automatic'},
                                                                                  {'value': 'full',
                                                                                   'label': 'Full'},
                                                                                  {'value': 'limited',
                                                                                   'label': 'Limited'},
                                                                                  {'value': 'extended',
                                                                                   'label': 'Extended'}],
                                                                       'required': True}},
                                                  'sets': {'output_saturation_range': '{value}'}},
                  'set_ndi_enabled': {'label': 'Set NDI Output',
                                      'help': 'Changes ndi output and reads the value back from the '
                                              'encoder.',
                                      'params': {'value': {'type': 'boolean',
                                                           'label': 'NDI Output',
                                                           'required': True}},
                                      'sets': {'ndi_enabled': '{value}'}},
                  'set_ndi_web_control': {'label': 'Set NDI Web Control',
                                          'help': 'Changes ndi web control and reads the value back from '
                                                  'the encoder.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'NDI Web Control',
                                                               'required': True}},
                                          'sets': {'ndi_web_control': '{value}'}},
                  'set_ndi_ptz_control': {'label': 'Set NDI Camera Control',
                                          'help': 'Changes ndi camera control and reads the value back from '
                                                  'the encoder.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'NDI Camera Control',
                                                               'required': True}},
                                          'sets': {'ndi_ptz_control': '{value}'}},
                  'set_failover_enabled': {'label': 'Set NDI Failover',
                                           'help': 'Changes ndi failover and reads the value back from the '
                                                   'encoder.',
                                           'params': {'value': {'type': 'boolean',
                                                                'label': 'NDI Failover',
                                                                'required': True}},
                                           'sets': {'failover_enabled': '{value}'}},
                  'set_discovery_enabled': {'label': 'Set NDI Discovery Server Enabled',
                                            'help': 'Changes ndi discovery server enabled and reads the '
                                                    'value back from the encoder.',
                                            'params': {'value': {'type': 'boolean',
                                                                 'label': 'NDI Discovery Server Enabled',
                                                                 'required': True}},
                                            'sets': {'discovery_enabled': '{value}'}},
                  'set_source_name': {'label': 'Set NDI Source Name',
                                      'help': 'Changes ndi source name and reads the value back from the '
                                              'encoder.',
                                      'params': {'value': {'type': 'string',
                                                           'label': 'NDI Source Name',
                                                           'required': True}},
                                      'sets': {'source_name': '{value}'}},
                  'set_group_name': {'label': 'Set NDI Groups',
                                     'help': 'Changes ndi groups and reads the value back from the encoder.',
                                     'params': {'value': {'type': 'string',
                                                          'label': 'NDI Groups',
                                                          'required': True}},
                                     'sets': {'group_name': '{value}'}},
                  'set_discovery_servers': {'label': 'Set NDI Discovery Servers',
                                            'help': 'Changes ndi discovery servers and reads the value back '
                                                    'from the encoder.',
                                            'params': {'value': {'type': 'string',
                                                                 'label': 'NDI Discovery Servers',
                                                                 'required': True}},
                                            'sets': {'discovery_servers': '{value}'}},
                  'set_ndi_transport': {'label': 'Set NDI Transport',
                                        'help': 'Changes ndi transport and reads the value back from the '
                                                'encoder.',
                                        'params': {'value': {'type': 'enum',
                                                             'label': 'NDI Transport',
                                                             'values': [{'value': 'tcp',
                                                                         'label': 'TCP Unicast'},
                                                                        {'value': 'tcp_multi',
                                                                         'label': 'TCP Multi-connection'},
                                                                        {'value': 'udp',
                                                                         'label': 'UDP Unicast'},
                                                                        {'value': 'rudp',
                                                                         'label': 'Reliable UDP Unicast'},
                                                                        {'value': 'multicast',
                                                                         'label': 'UDP Multicast'}],
                                                             'required': True}},
                                        'sets': {'ndi_transport': '{value}'}},
                  'set_multicast_address': {'label': 'Set Multicast Address',
                                            'help': 'Changes multicast address and reads the value back '
                                                    'from the encoder.',
                                            'params': {'value': {'type': 'string',
                                                                 'label': 'Multicast Address',
                                                                 'required': True}},
                                            'sets': {'multicast_address': '{value}'}},
                  'set_multicast_mask': {'label': 'Set Multicast Subnet Mask',
                                         'help': 'Changes multicast subnet mask and reads the value back '
                                                 'from the encoder.',
                                         'params': {'value': {'type': 'string',
                                                              'label': 'Multicast Subnet Mask',
                                                              'required': True}},
                                         'sets': {'multicast_mask': '{value}'}},
                  'set_multicast_ttl': {'label': 'Set Multicast TTL',
                                        'help': 'Changes multicast ttl and reads the value back from the '
                                                'encoder.',
                                        'params': {'value': {'type': 'integer',
                                                             'label': 'Multicast TTL',
                                                             'min': 1,
                                                             'max': 255,
                                                             'required': True}},
                                        'sets': {'multicast_ttl': '{value}'}},
                  'set_audio_reference': {'label': 'Set Audio Reference Level',
                                          'help': 'Changes audio reference level and reads the value back '
                                                  'from the encoder.',
                                          'params': {'value': {'type': 'enum',
                                                               'label': 'Audio Reference Level',
                                                               'values': [{'value': 20,
                                                                           'label': 'SMPTE (20 dB)'},
                                                                          {'value': 14,
                                                                           'label': 'EBU (14 dB)'}],
                                                               'required': True}},
                                          'sets': {'audio_reference': '{value}'}},
                  'set_smart_edid': {'label': 'Set SmartEDID',
                                     'help': 'Changes smartedid and reads the value back from the encoder.',
                                     'params': {'value': {'type': 'boolean',
                                                          'label': 'SmartEDID',
                                                          'required': True}},
                                     'sets': {'smart_edid': '{value}'}},
                  'set_keep_last_edid': {'label': 'Set Keep Last Loop-through EDID',
                                         'help': 'Changes keep last loop-through edid and reads the value '
                                                 'back from the encoder.',
                                         'params': {'value': {'type': 'boolean',
                                                              'label': 'Keep Last Loop-through EDID',
                                                              'required': True}},
                                         'sets': {'keep_last_edid': '{value}'}},
                  'set_edid_add_audio': {'label': 'Set EDID Audio Support',
                                         'help': 'Changes edid audio support and reads the value back from '
                                                 'the encoder.',
                                         'params': {'value': {'type': 'boolean',
                                                              'label': 'EDID Audio Support',
                                                              'required': True}},
                                         'sets': {'edid_add_audio': '{value}'}},
                  'set_edid_limit_clock': {'label': 'Set Limit EDID Pixel Clock',
                                           'help': 'Changes limit edid pixel clock and reads the value back '
                                                   'from the encoder.',
                                           'params': {'value': {'type': 'boolean',
                                                                'label': 'Limit EDID Pixel Clock',
                                                                'required': True}},
                                           'sets': {'edid_limit_clock': '{value}'}},
                  'set_custom_tally': {'label': 'Set Custom Tally Lights',
                                       'help': 'Enables the encoder’s user-customized tally option. Program '
                                               'and preview feedback still comes from NDI receivers.',
                                       'params': {'value': {'type': 'boolean',
                                                            'label': 'Custom Tally Lights',
                                                            'required': True}},
                                       'sets': {'custom_tally': '{value}'}},
                  'set_ptz_protocol': {'label': 'Set Camera Control Protocol',
                                       'help': 'Changes camera control protocol and reads the value back '
                                               'from the encoder.',
                                       'params': {'value': {'type': 'enum',
                                                            'label': 'Camera Control Protocol',
                                                            'values': [{'value': 'none',
                                                                        'label': 'Disabled'},
                                                                       {'value': 'visca', 'label': 'VISCA'},
                                                                       {'value': 'visca-udp',
                                                                        'label': 'VISCA-UDP'},
                                                                       {'value': 'visca-udp2rs232',
                                                                        'label': 'VISCA-UDP2RS232'},
                                                                       {'value': 'pelco-d',
                                                                        'label': 'PELCO-D'},
                                                                       {'value': 'pelco-p',
                                                                        'label': 'PELCO-P'}],
                                                            'required': True}},
                                       'sets': {'ptz_protocol': '{value}'}},
                  'set_ptz_index': {'label': 'Set Camera Address',
                                    'help': 'Changes camera address and reads the value back from the '
                                            'encoder.',
                                    'params': {'value': {'type': 'integer',
                                                         'label': 'Camera Address',
                                                         'min': 1,
                                                         'max': 7,
                                                         'required': True}},
                                    'sets': {'ptz_index': '{value}'}},
                  'set_ptz_baud': {'label': 'Set Camera Baud Rate',
                                   'help': 'Changes camera baud rate and reads the value back from the '
                                           'encoder.',
                                   'params': {'value': {'type': 'enum',
                                                        'label': 'Camera Baud Rate',
                                                        'values': [{'value': 2400, 'label': '2400'},
                                                                   {'value': 4800, 'label': '4800'},
                                                                   {'value': 9600, 'label': '9600'},
                                                                   {'value': 19200, 'label': '19200'},
                                                                   {'value': 38400, 'label': '38400'}],
                                                        'required': True}},
                                   'sets': {'ptz_baud': '{value}'}},
                  'set_ptz_invert_pan': {'label': 'Set Reverse Camera Pan',
                                         'help': 'Changes reverse camera pan and reads the value back from '
                                                 'the encoder.',
                                         'params': {'value': {'type': 'boolean',
                                                              'label': 'Reverse Camera Pan',
                                                              'required': True}},
                                         'sets': {'ptz_invert_pan': '{value}'}},
                  'set_ptz_invert_tilt': {'label': 'Set Reverse Camera Tilt',
                                          'help': 'Changes reverse camera tilt and reads the value back '
                                                  'from the encoder.',
                                          'params': {'value': {'type': 'boolean',
                                                               'label': 'Reverse Camera Tilt',
                                                               'required': True}},
                                          'sets': {'ptz_invert_tilt': '{value}'}},
                  'set_ptz_visca_header': {'label': 'Set VISCA UDP Header',
                                           'help': 'Changes visca udp header and reads the value back from '
                                                   'the encoder.',
                                           'params': {'value': {'type': 'boolean',
                                                                'label': 'VISCA UDP Header',
                                                                'required': True}},
                                           'sets': {'ptz_visca_header': '{value}'}},
                  'set_ptz_address': {'label': 'Set Camera IP Address',
                                      'help': 'Changes camera ip address and reads the value back from the '
                                              'encoder.',
                                      'params': {'value': {'type': 'string',
                                                           'label': 'Camera IP Address',
                                                           'required': True}},
                                      'sets': {'ptz_address': '{value}'}},
                  'set_ptz_port': {'label': 'Set Camera UDP Port',
                                   'help': 'Changes camera udp port and reads the value back from the '
                                           'encoder.',
                                   'params': {'value': {'type': 'integer',
                                                        'label': 'Camera UDP Port',
                                                        'min': 1,
                                                        'max': 65535,
                                                        'required': True}},
                                   'sets': {'ptz_port': '{value}'}},
                  'set_ptz_focus_near_limit': {'label': 'Set Camera Focus Near Limit',
                                               'help': 'Changes camera focus near limit and reads the value '
                                                       'back from the encoder.',
                                               'params': {'value': {'type': 'integer',
                                                                    'label': 'Camera Focus Near Limit',
                                                                    'min': 0,
                                                                    'max': 65535,
                                                                    'required': True}},
                                               'sets': {'ptz_focus_near_limit': '{value}'}},
                  'set_ptz_focus_far_limit': {'label': 'Set Camera Focus Far Limit',
                                              'help': 'Changes camera focus far limit and reads the value '
                                                      'back from the encoder.',
                                              'params': {'value': {'type': 'integer',
                                                                   'label': 'Camera Focus Far Limit',
                                                                   'min': 0,
                                                                   'max': 65535,
                                                                   'required': True}},
                                              'sets': {'ptz_focus_far_limit': '{value}'}},
                  'set_ptz_pan_left_limit': {'label': 'Set Camera Pan Left Limit',
                                             'help': 'Changes camera pan left limit and reads the value '
                                                     'back from the encoder.',
                                             'params': {'value': {'type': 'integer',
                                                                  'label': 'Camera Pan Left Limit',
                                                                  'min': -32768,
                                                                  'max': 32767,
                                                                  'required': True}},
                                             'sets': {'ptz_pan_left_limit': '{value}'}},
                  'set_ptz_pan_center': {'label': 'Set Camera Pan Center',
                                         'help': 'Changes camera pan center and reads the value back from '
                                                 'the encoder.',
                                         'params': {'value': {'type': 'integer',
                                                              'label': 'Camera Pan Center',
                                                              'min': -32768,
                                                              'max': 32767,
                                                              'required': True}},
                                         'sets': {'ptz_pan_center': '{value}'}},
                  'set_ptz_pan_right_limit': {'label': 'Set Camera Pan Right Limit',
                                              'help': 'Changes camera pan right limit and reads the value '
                                                      'back from the encoder.',
                                              'params': {'value': {'type': 'integer',
                                                                   'label': 'Camera Pan Right Limit',
                                                                   'min': -32768,
                                                                   'max': 32767,
                                                                   'required': True}},
                                              'sets': {'ptz_pan_right_limit': '{value}'}},
                  'set_ptz_tilt_top_limit': {'label': 'Set Camera Tilt Top Limit',
                                             'help': 'Changes camera tilt top limit and reads the value '
                                                     'back from the encoder.',
                                             'params': {'value': {'type': 'integer',
                                                                  'label': 'Camera Tilt Top Limit',
                                                                  'min': -32768,
                                                                  'max': 32767,
                                                                  'required': True}},
                                             'sets': {'ptz_tilt_top_limit': '{value}'}},
                  'set_ptz_tilt_center': {'label': 'Set Camera Tilt Center',
                                          'help': 'Changes camera tilt center and reads the value back from '
                                                  'the encoder.',
                                          'params': {'value': {'type': 'integer',
                                                               'label': 'Camera Tilt Center',
                                                               'min': -32768,
                                                               'max': 32767,
                                                               'required': True}},
                                          'sets': {'ptz_tilt_center': '{value}'}},
                  'set_ptz_tilt_bottom_limit': {'label': 'Set Camera Tilt Bottom Limit',
                                                'help': 'Changes camera tilt bottom limit and reads the '
                                                        'value back from the encoder.',
                                                'params': {'value': {'type': 'integer',
                                                                     'label': 'Camera Tilt Bottom Limit',
                                                                     'min': -32768,
                                                                     'max': 32767,
                                                                     'required': True}},
                                                'sets': {'ptz_tilt_bottom_limit': '{value}'}},
                  'set_ptz_zoom_out_limit': {'label': 'Set Camera Zoom Out Limit',
                                             'help': 'Changes camera zoom out limit and reads the value '
                                                     'back from the encoder.',
                                             'params': {'value': {'type': 'integer',
                                                                  'label': 'Camera Zoom Out Limit',
                                                                  'min': 0,
                                                                  'max': 32767,
                                                                  'required': True}},
                                             'sets': {'ptz_zoom_out_limit': '{value}'}},
                  'ndi_on': {'label': 'Enable NDI Output',
                             'help': 'Starts advertising and sending the NDI source.',
                             'sets': {'ndi_enabled': True}},
                  'ndi_off': {'label': 'Disable NDI Output',
                              'help': 'Stops the NDI output. Receivers lose this source.',
                              'sets': {'ndi_enabled': False}},
                  'match_input': {'label': 'Match Input Resolution',
                                  'help': 'Sets the encoded video resolution to follow the input signal.',
                                  'sets': {'match_input_resolution': True}},
                  'set_resolution': {'label': 'Set Output Resolution',
                                     'help': 'Scales the encoded video to the chosen dimensions. Width must '
                                             'be a multiple of 4; height a multiple of 2.',
                                     'params': {'width': {'type': 'integer',
                                                          'label': 'Width',
                                                          'required': True,
                                                          'min': 4},
                                                'height': {'type': 'integer',
                                                           'label': 'Height',
                                                           'required': True,
                                                           'min': 2}}},
                  'select_failover_source': {'label': 'Select Backup Source',
                                             'help': 'Uses a source discovered by the encoder as its NDI '
                                                     'failover source and enables failover.',
                                             'params': {'source': {'type': 'child_id',
                                                                   'label': 'Backup Source',
                                                                   'required': True,
                                                                   'child_type': 'source'}}},
                  'set_failover_source': {'label': 'Set Backup Source by Address',
                                          'help': 'Sets a named NDI backup source when discovery does not '
                                                  'find it, and enables failover.',
                                          'params': {'name': {'type': 'string',
                                                              'label': 'NDI Source Name',
                                                              'required': True},
                                                     'address': {'type': 'string',
                                                                 'label': 'Source Address',
                                                                 'required': True,
                                                                 'help': 'The source address reported by '
                                                                         'NDI, including its port, for '
                                                                         'example 192.0.2.10:5961.'}}},
                  'disable_failover': {'label': 'Disable Failover',
                                       'help': 'Stops using the backup source when the input signal is '
                                               'lost.',
                                       'sets': {'failover_enabled': False}},
                  'reset_video': {'label': 'Reset Video Settings',
                                  'help': 'Restores the encoder’s picture and scaling settings to their '
                                          'defaults.',
                                  'confirm': 'Restore all picture and scaling settings to their defaults?'},
                  'reset_edid': {'label': 'Restore Default EDID',
                                 'help': 'Restores the default input EDID. The connected source may '
                                         'renegotiate its output.',
                                 'confirm': 'Replace the current input EDID with the factory EDID?'},
                  'download_input_edid': {'label': 'Download Input EDID',
                                          'help': 'Downloads the input EDID as a binary file.',
                                          'returns_file': True},
                  'download_output_edid': {'label': 'Download Loop-through EDID',
                                           'help': 'Downloads the loop-through EDID as a binary file.',
                                           'returns_file': True},
                  'reboot': {'label': 'Restart Encoder',
                             'help': 'Restarts the encoder and interrupts its video and audio until it '
                                     'returns.',
                             'confirm': 'Restart the encoder and interrupt its streams?'}},
     'quick_actions': ['ndi_on', 'ndi_off', 'select_failover_source', 'match_input'],
     'child_entity_types': {'source': {'label': 'Backup Source',
                                       'label_plural': 'Backup Sources',
                                       'id_format': {'type': 'string'},
                                       'label_field': 'name',
                                       'summary_fields': ['address', 'selected'],
                                       'state_variables': {'name': {'type': 'string',
                                                                    'label': 'NDI Source Name'},
                                                           'address': {'type': 'string',
                                                                       'label': 'Source Address'},
                                                           'selected': {'type': 'boolean',
                                                                        'label': 'Selected for Failover',
                                                                        'cloud_priority': 'high'}}}},
     'discovery': {'oui': ['70:B3:D5:75:D']}}

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._client = None
        self._operations = asyncio.Lock()
        self._sources = {}
        self._configs = {}
        self._detail_errors = {}
        self._caps = {}
        self._poll_count = 0

    async def _create_transport(self, transport_type):
        host = str(self.config.get('host', '')).strip()
        if not host:
            raise ConnectionFaultError('Enter the encoder address.', code='invalid_config')
        if not self.config.get('username') or not self.config.get('password'):
            raise ConnectionFaultError('The encoder username or password is missing.', code='auth_failed')
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
        # ping is documented as login-free. A refusal is still an answer.
        await self._request('ping')

    async def _request(self, method, params=None):
        if self._client is None:
            raise ConnectionError('The encoder session is closed.')
        query = {'method': method}
        query.update({key: _wire(value) for key, value in (params or {}).items()})
        try:
            return await self._client.get('/mwapi', params=query)
        except httpx.TransportError as exc:
            raise ConnectionError('The encoder stopped answering HTTP requests.') from exc

    async def _login(self):
        self._client.cookies.clear()
        digest = hashlib.md5(str(self.config['password']).encode(), usedforsecurity=False).hexdigest()
        self.redact_in_log(digest)
        reply = await self._request('login', {'id': self.config['username'], 'pass': digest})
        _check_login(reply)
        cookies = [cookie for cookie in self._client.cookies.jar if cookie.name == 'sid']
        if not cookies:
            raise CommandRefusedError('The encoder accepted login but supplied no session cookie.')
        for cookie in cookies:
            self.redact_in_log(cookie.value)

    async def _call(self, method, params=None, *, core=False):
        for attempt in range(2):
            try:
                return _decode(await self._request(method, params), method)
            except EncoderError as exc:
                if exc.code in (37, 401) and attempt == 0:
                    await self._login()
                    continue
                _check_access(exc, core)
                raise
        raise ConnectionError('The encoder session could not be renewed.')

    async def _read_summary(self):
        data = await self._call('get-summary-info', core=True)
        device = data.get('device')
        if not isinstance(device, dict):
            raise CommandRefusedError('The encoder returned no device information.')
        model = str(device.get('model', '')).removeprefix('Pro Convert ')
        supported = type(self).DRIVER_INFO['compatible_models'][0]['models']
        if 'Pro Convert ' + model not in supported:
            raise ConnectionFaultError('This is not a supported Pro Convert encoder.', code='invalid_config')
        device['model'] = 'Pro Convert ' + model
        for group, fields in _SUMMARY.items():
            self._copy_values(data.get(group, {}), fields)

    def _copy_values(self, data, fields, *, clear_missing=False):
        for wire, key in fields.items():
            if wire in data:
                self.set_state(key, data[wire])
            elif clear_missing:
                self.set_state(key, None)

    def _apply_capabilities(self):
        for cap, key in [('has-input-edid', 'input_edid_available'),
                         ('has-output-edid', 'output_edid_available'), ('has-ptz', 'ptz_available')]:
            self.set_state(key, bool(self._caps.get(cap, False)))
        self._hide_unsupported_controls()
        self._resolution_limits()

    def _resolution_limits(self):
        params = self.DRIVER_INFO['commands']['set_resolution']['params']
        for param, wire in [('width', 'max-output-width'), ('height', 'max-output-height')]:
            if self._caps.get(wire):
                params[param]['max'] = int(self._caps[wire])
                self.set_state('max_output_' + param, int(self._caps[wire]))

    def _hide_unsupported_controls(self):
        if not self._caps.get('has-input-edid'):
            self._hide_group('edid')
            self.DRIVER_INFO['commands'].pop('reset_edid', None)
            self.DRIVER_INFO['commands'].pop('download_input_edid', None)
        if not self._caps.get('has-output-edid'):
            self.DRIVER_INFO['commands'].pop('download_output_edid', None)
        if not self._caps.get('has-ptz'):
            self._hide_group('ptz')
            self._hide_setting('ndi_ptz_control')

    def _hide_group(self, group):
        for key, (owner, _) in _FIELDS.items():
            if owner == group:
                self._hide_setting(key)

    def _hide_setting(self, key):
        self.DRIVER_INFO['commands'].pop('set_' + key, None)
        self.DRIVER_INFO['device_settings'].pop(key, None)
        self.DRIVER_INFO['state_variables'][key]['control'] = False

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
        # Signal blocks can disappear on a cable pull. Old dimensions are not current readings.
        for group, fields in _SIGNAL.items():
            self._copy_values(data.get(group, {}), fields, clear_missing=True)

    async def refresh_children(self):
        async with self._operations:
            await self._read_live()
            await self._read_details()
            self._report_errors()
        return {'source': len(self._sources)}

    async def _read_details(self):
        groups = ['video', 'ndi', 'tally']
        if self._caps.get('has-input-edid'):
            groups.append('edid')
        if self._caps.get('has-ptz'):
            groups.append('ptz')
        for group in groups:
            await self._optional_read(group)
        await self._optional_read('sources')

    async def _optional_read(self, group):
        try:
            if group == 'sources':
                await self._read_sources()
            else:
                await self._read_config(group)
            self._detail_errors.pop(group, None)
        except EncoderError as exc:
            if exc.code not in (5, 9, 16, 31, 403):
                raise
            self._detail_errors[group] = str(exc)

    def _report_errors(self):
        self.set_state('last_error', '; '.join(self._detail_errors.values()))

    async def _read_config(self, group):
        method = 'get-tally' if group == 'tally' else f'get-{group}-config'
        data = await self._call(method)
        self._configs[group] = data
        fields = {wire:key for key,(owner,wire) in _FIELDS.items() if owner == group}
        for wire, key in fields.items():
            if key in _VIDEO_PAIRS:
                continue
            if wire in data:
                self.set_state(key, data[wire])
        if group == 'video':
            self._apply_video(data)
        if group == 'ndi':
            self._apply_ndi(data)
        return data

    def _apply_video(self, data):
        self._copy_values(data, {'out-cx':'output_width', 'out-cy':'output_height',
                                 'out-raw-resolution':'match_input_resolution'})
        for key, (automatic, wire) in _VIDEO_PAIRS.items():
            if automatic in data:
                self.set_state(key, 'auto' if data[automatic] else data.get(wire))

    def _apply_ndi(self, data):
        self._copy_values(data, {'fail-over-ndi-name':'failover_name', 'fail-over-ip-addr':'failover_address'})
        enabled = [name for name, wire in _TRANSPORT_FLAGS.items() if data.get(wire)]
        self.set_state('ndi_transport', enabled[0] if len(enabled) == 1 else 'tcp' if not enabled else None)
        for ident, row in self._sources.items():
            self.set_child_state('source', ident, 'selected', _selected(row, data))

    async def _read_sources(self):
        data = await self._call('get-ndi-sources')
        rows = data.get('sources')
        if not isinstance(rows, list):
            raise CommandRefusedError('The encoder returned no backup source list.')
        sources = {}
        for row in rows:
            if not _source_valid(row):
                continue
            ident = hashlib.sha256((row['ndi-name'] + '\0' + row['ip-addr']).encode()).hexdigest()[:24]
            values = {'name':row['ndi-name'], 'address':row['ip-addr'], 'online':True,
                      'selected':_selected(row, self._configs.get('ndi', {}))}
            self.register_child('source', ident, initial_state=values)
            self.set_child_state_batch('source', ident, values)
            sources[ident] = row
        for ident in self._sources.keys() - sources.keys():
            self.deregister_child('source', ident)
        self._sources = sources

    async def set_device_setting(self, key, value):
        if key not in self.DRIVER_INFO['device_settings']:
            raise CommandRefusedError('This setting is not supported by the connected encoder.')
        async with self._operations:
            await self._write_setting(key, value)

    async def send_command(self, command, params=None):
        if command not in self.DRIVER_INFO['commands']:
            raise CommandRefusedError('This command is not supported by the connected encoder.')
        if command == 'refresh':
            return await self.refresh_children()
        async with self._operations:
            try:
                return await self._dispatch(command, params or {})
            except CommandRefusedError as exc:
                self.set_state('last_error', str(exc))
                raise

    async def _dispatch(self, command, params):
        setting = command.removeprefix('set_')
        if command.startswith('set_') and setting in _FIELDS:
            return await self._write_setting(setting, params['value'])
        switches = {'ndi_on':('ndi', {'enable':True}), 'ndi_off':('ndi', {'enable':False}),
                    'disable_failover':('ndi', {'enable-fail-over':False}),
                    'match_input':('video', {'out-raw-resolution':True})}
        if command in switches:
            return await self._set_fields(*switches[command])
        handlers = {'set_resolution':self._set_resolution, 'select_failover_source':self._select_source,
                    'set_failover_source':self._manual_source, 'reset_video':self._reset_video,
                    'reset_edid':self._reset_edid, 'reboot':self._reboot,
                    'download_input_edid':self._input_edid, 'download_output_edid':self._output_edid}
        return await handlers[command](params)

    async def _write_setting(self, key, value):
        group, wire = _FIELDS[key]
        # Enum editors send strings; these two API fields report JSON integers.
        if key in ('audio_reference', 'ptz_baud'):
            value = int(value)
        if key in ('multicast_address', 'multicast_mask', 'ptz_address'):
            _validate_address(key, value)
        payload = {wire:value}
        if key in _VIDEO_PAIRS:
            automatic, wire = _VIDEO_PAIRS[key]
            payload = {automatic:value == 'auto'}
            if value != 'auto':
                payload[wire] = value
        if key == 'ndi_transport':
            payload = _transport_fields(value)
        await self._set_fields(group, payload)

    async def _set_fields(self, group, payload):
        method = 'set-tally' if group == 'tally' else f'set-{group}-config'
        await self._call(method, payload)
        if group == 'ptz':
            await self._call('arrange-ptz-cameras')
        reported = await self._read_config(group)
        mismatches = [key for key, value in payload.items() if reported.get(key) != value]
        if mismatches:
            raise CommandRefusedError('The encoder did not confirm the requested ' + group + ' settings. Refresh Status to check its current values.')
        self._detail_errors.pop(group, None)
        await self._read_summary()
        self._report_errors()

    async def _set_resolution(self, params):
        width, height = int(params['width']), int(params['height'])
        _validate_dimension(width, 4, self._caps.get('max-output-width'), 'width')
        _validate_dimension(height, 2, self._caps.get('max-output-height'), 'height')
        await self._set_fields('video', {'out-raw-resolution':False, 'out-cx':width, 'out-cy':height})

    async def _select_source(self, params):
        await self._read_sources()
        row = self._sources.get(str(params['source']))
        if row is None:
            raise CommandRefusedError('That backup source is no longer available. Refresh Status and choose another source.')
        await self._manual_source({'name':row['ndi-name'], 'address':row['ip-addr']})

    async def _manual_source(self, params):
        await self._set_fields('ndi', {'enable-fail-over':True, 'fail-over-ndi-name':params['name'],
                                      'fail-over-ip-addr':params['address']})

    async def _reset_video(self, params):
        await self._call('reset-video-config')
        await self._read_config('video')

    async def _reset_edid(self, params):
        await self._call('set-default-edid')
        await self._read_config('edid')

    async def _reboot(self, params):
        await self._call('reboot')
        # No invented grace period: a real unit's reboot duration is unmeasured.

    async def _input_edid(self, params):
        return await self._download_edid('get-edid-config', 'input')

    async def _output_edid(self, params):
        return await self._download_edid('get-output-edid', 'loopthrough')

    async def _download_edid(self, method, label):
        data = await self._call(method)
        try:
            blob = base64.b64decode(data['data'], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise CommandRefusedError('The encoder returned no valid EDID data.') from exc
        if not blob or len(blob) > 32768:
            raise CommandRefusedError('The encoder returned no valid EDID data.')
        return DeviceFile('pro-convert-' + label + '-edid.bin', blob)


_TRANSPORT_FLAGS = {'multicast':'enable-mcast', 'rudp':'enable-rudp',
                    'tcp_multi':'enable-tcp', 'udp':'enable-udp'}


def _transport_fields(value):
    if value not in ('tcp', *_TRANSPORT_FLAGS):
        raise CommandRefusedError('Choose one of the listed NDI transport modes.')
    return {wire:name == value for name, wire in _TRANSPORT_FLAGS.items()}


def _selected(source, config):
    return (source['ndi-name'] == config.get('fail-over-ndi-name')
            and source['ip-addr'] == config.get('fail-over-ip-addr'))


def _wire(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _validate_address(key, value):
    try:
        address = ipaddress.IPv4Address(str(value))
    except ipaddress.AddressValueError as exc:
        raise CommandRefusedError('Enter a valid IPv4 address.') from exc
    if key == 'multicast_address' and not address.is_multicast:
        raise CommandRefusedError('The multicast address must be between 224.0.0.0 and 239.255.255.255.')
    if key == 'multicast_mask':
        _validate_mask(value)


def _validate_mask(value):
    try:
        ipaddress.IPv4Network('0.0.0.0/' + str(value))
    except ValueError as exc:
        raise CommandRefusedError('Enter a contiguous IPv4 subnet mask.') from exc


def _validate_dimension(value, step, maximum, label):
    if value < step or value % step:
        raise CommandRefusedError(f'Use a positive {label} divisible by {step}.')
    if maximum is None:
        raise CommandRefusedError('The encoder did not report its maximum output resolution.')
    if value > maximum:
        raise CommandRefusedError('That resolution exceeds this encoder’s reported capability.')


def _source_valid(row):
    if not isinstance(row, dict):
        return False
    return all(isinstance(row.get(key), str) and row[key] for key in ('ndi-name', 'ip-addr'))


def _check_login(response):
    try:
        _decode(response, 'Login')
    except EncoderError as exc:
        if exc.code in (31, 36, 37, 401, 403):
            raise ConnectionFaultError('The encoder refused the account login.', code='auth_failed') from exc
        raise


def _check_access(error, core):
    fatal = {36, 37, 401}
    if core:
        fatal.update((31, 403))
    if error.code in fatal:
        raise ConnectionFaultError('The encoder account cannot read control status.', code='auth_failed') from error


class EncoderError(CommandRefusedError):
    def __init__(self, operation, code):
        self.code = code
        reasons = {5:'is not implemented on this encoder', 7:'contains an invalid value',
                   9:'is not supported by this encoder', 11:'cannot run while the encoder is busy',
                   16:'was not found', 27:'is outside the supported range',
                   31:'requires an account with permission', 36:'was refused during authentication',
                   37:'requires a new session', 401:'requires a new session', 403:'requires an account with permission'}
        super().__init__(operation + ' ' + reasons.get(code, f'was refused (status {code})') + '.')


def _decode(response, operation):
    if response.status_code != 200:
        raise EncoderError(operation, response.status_code)
    data = _json_response(response, operation)
    if data['status'] != 0:
        raise EncoderError(operation, data['status'])
    return data


def _json_response(response, operation):
    try:
        data = response.json()
    except ValueError as exc:
        raise CommandRefusedError('The encoder returned an unreadable response to ' + operation + '.') from exc
    if not isinstance(data, dict) or not isinstance(data.get('status'), int):
        raise CommandRefusedError('The encoder returned no status for ' + operation + '.')
    return data
