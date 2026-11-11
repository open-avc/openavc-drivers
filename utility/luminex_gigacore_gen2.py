"""Luminex GigaCore Gen 2, REST and WebSocket API 1.5.x.

Python supports device-enumerated ports and groups plus selective WebSocket
subscriptions. REST reads reconcile inventory and telemetry; commands read back
actual values. No hardware qualification is claimed. The official v192 generated
WebSocket examples incorrectly include /api in data paths; Luminex's published
Q-SYS example confirms /ports/port/<id>, primitive new_value and HTTP Basic auth.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import ssl
from collections import Counter
from contextlib import suppress
from copy import deepcopy

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import InvalidStatus

from openavc.core.connection_fault import ConnectionFaultError
from openavc.drivers.base import BaseDriver, CommandPartialError, CommandRefusedError


class LuminexGigaCoreGen2Driver(BaseDriver):
    DRIVER_INFO = {'id': 'luminex_gigacore_gen2',
     'name': 'Luminex GigaCore Gen 2',
     'manufacturer': 'Luminex',
     'category': 'utility',
     'version': '1.0.0',
     'author': 'OpenAVC',
     'transport': 'http',
     'min_platform_version': '0.37.1',
     'description': 'Monitor GigaCore Gen 2 AV switches, manage named ports and group membership, and '
                    'control PoE endpoints with live status, power readings and fault feedback.',
     'source_url': 'https://support.luminex.be/portal/en/kb/articles/gigacore-api-documentation',
     'ports': [80],
     'protocols': ['luminex_gigacore_rest'],
     'tags': ['network', 'switch', 'poe', 'av-over-ip', 'dante', 'milan'],
     'simulated': True,
     'verified': False,
     'compatible_models': [{'manufacturer': 'Luminex',
                            'models': ['GigaCore 10i',
                                       'GigaCore 16i',
                                       'GigaCore 30i',
                                       'GigaCore 16t',
                                       'GigaCore 18t',
                                       'GigaCore 20t',
                                       'GigaCore 10t',
                                       'GigaCore 10t-IP',
                                       'GigaCore 16tf',
                                       'GigaCore O26-I',
                                       'GigaCore S30-I'],
                            'confidence': 'untested',
                            'notes': 'Generation 2 REST/WebSocket API 1.5.x. PoE ports are discovered from '
                                     'the switch. Not hardware verified. Generation 1 models are not '
                                     'supported.'}],
     'help': {'overview': 'Add one device for the switch. Its named ports, PoE ports and groups appear under '
                          'Child Entities and are available in command pickers and panel bindings. Ports '
                          'shows physical link and group membership. PoE Ports lists only ports currently '
                          'offered by the switch’s PoE subsystem. Groups reports multicast and timing '
                          'settings. Enable Traffic Counters to publish per-port rates and discard counters. '
                          'Advanced configuration mode can contain CLI settings that the API does not '
                          'represent. Use the switch web UI for those settings.',
              'setup': 'Enter the management IP address. Leave credentials blank only when authentication is '
                       'disabled on the switch; otherwise enter its username and password. Use firmware '
                       'exposing API 1.5.x. Mark management and uplink ports Protected in the switch web UI '
                       'before operating port controls.',
              'connection': 'Check the switch address, HTTP port and credentials. REST and WebSocket access '
                            'must both be allowed. A rejected sign-in requires corrected credentials. This '
                            'driver supports Generation 2 only.'},
     'default_config': {'port': 80,
                        'ssl': False,
                        'verify_ssl': True,
                        'username': '',
                        'password': '',
                        'timeout': 5,
                        'poll_interval': 15,
                        'enable_traffic_counters': False},
     'config_schema': {'host': {'type': 'string',
                                'label': 'Switch Address',
                                'required': True,
                                'help': 'Management IP address or hostname.'},
                       'port': {'type': 'integer',
                                'label': 'HTTP Port',
                                'default': 80,
                                'min': 1,
                                'max': 65535},
                       'ssl': {'type': 'boolean',
                               'label': 'Use HTTPS',
                               'default': False,
                               'help': 'Use HTTPS and secure WebSocket when enabled on the switch. Set HTTP '
                                       'Port to match.'},
                       'verify_ssl': {'type': 'boolean',
                                      'label': 'Verify Certificate',
                                      'default': True,
                                      'help': 'Verify the switch certificate when using HTTPS.'},
                       'username': {'type': 'string', 'label': 'Username', 'default': ''},
                       'password': {'type': 'string', 'label': 'Password', 'secret': True, 'default': ''},
                       'enable_traffic_counters': {'type': 'boolean',
                                                   'label': 'Traffic Counters',
                                                   'default': False,
                                                   'help': 'Publish per-port traffic rates, discards and '
                                                           'link-drop counters at the poll interval.'},
                       'poll_interval': {'type': 'number',
                                         'label': 'Status Refresh Interval',
                                         'default': 15,
                                         'min': 0,
                                         'help': 'Seconds between inventory, power and connected-device '
                                                 'reads. Zero disables these refreshes; live subscriptions '
                                                 'continue.'},
                       'timeout': {'type': 'number',
                                   'label': 'Response Timeout',
                                   'default': 5,
                                   'min': 1,
                                   'max': 60,
                                   'help': 'Seconds to wait for an API response.'}},
     'state_variables': {'model': {'type': 'string', 'label': 'Model'},
                         'serial_number': {'type': 'string', 'label': 'Serial Number'},
                         'firmware_version': {'type': 'string', 'label': 'Firmware'},
                         'api_version': {'type': 'string', 'label': 'API Version'},
                         'device_name': {'type': 'string', 'label': 'Switch Name'},
                         'description': {'type': 'string', 'label': 'Description'},
                         'system_id': {'type': 'integer', 'label': 'System ID'},
                         'mac_address': {'type': 'string', 'label': 'MAC Address'},
                         'config_mode': {'type': 'string', 'label': 'Configuration Mode'},
                         'led_brightness': {'type': 'integer',
                                            'label': 'LED Brightness',
                                            'min': 0,
                                            'max': 4,
                                            'control': True},
                         'live_feedback': {'type': 'boolean',
                                           'label': 'Live Feedback',
                                           'cloud_priority': 'high'},
                         'last_error': {'type': 'string', 'label': 'Last Error', 'cloud_priority': 'high'},
                         'port_count': {'type': 'integer', 'label': 'Ports'},
                         'ports_up': {'type': 'integer',
                                      'label': 'Ports With Link',
                                      'cloud_priority': 'high'},
                         'poe_capable': {'type': 'boolean', 'label': 'PoE Available'},
                         'poe_status': {'type': 'string',
                                        'label': 'PoE Supply Status',
                                        'cloud_priority': 'high'},
                         'poe_total_w': {'type': 'number',
                                         'label': 'PoE Power Budget',
                                         'unit': 'W',
                                         'min': 0},
                         'poe_consumed_w': {'type': 'number',
                                            'label': 'PoE Power Used',
                                            'unit': 'W',
                                            'min': 0,
                                            'cloud_priority': 'low'},
                         'poe_reserved_w': {'type': 'number',
                                            'label': 'PoE Power Reserved',
                                            'unit': 'W',
                                            'min': 0,
                                            'cloud_priority': 'low'},
                         'uptime_s': {'type': 'integer',
                                      'label': 'Uptime',
                                      'unit': 's',
                                      'min': 0,
                                      'cloud_priority': 'low'},
                         'cpu_load': {'type': 'integer',
                                      'label': 'CPU Load',
                                      'unit': '%',
                                      'min': 0,
                                      'cloud_priority': 'low'},
                         'temperature_c': {'type': 'number',
                                           'label': 'CPU Temperature',
                                           'unit': '°C',
                                           'cloud_priority': 'low'},
                         'status_level': {'type': 'string',
                                          'label': 'Status Severity',
                                          'cloud_priority': 'high'},
                         'status_message': {'type': 'string',
                                            'label': 'Switch Status',
                                            'cloud_priority': 'high'},
                         'group_options': {'type': 'string', 'label': 'Group Choices'}},
     'device_settings': {'device_name': {'type': 'string',
                                         'label': 'Switch Name',
                                         'state_key': 'device_name',
                                         'regex': '^[a-zA-Z0-9_ -]{0,64}$',
                                         'help': 'Change the name stored on the switch.'},
                         'description': {'type': 'string',
                                         'label': 'Description',
                                         'regex': '(?s)^.{0,255}$',
                                         'help': 'Describe the switch’s location or purpose.'},
                         'system_id': {'type': 'integer',
                                       'label': 'System ID',
                                       'min': 0,
                                       'max': 999999,
                                       'help': 'Set the switch’s identification number.'},
                         'led_brightness': {'type': 'integer',
                                            'label': 'LED Brightness',
                                            'min': 0,
                                            'max': 4,
                                            'help': 'Set the front-panel LEDs from 0 to 4.'}},
     'child_entity_types': {'port': {'label': 'Port',
                                     'label_plural': 'Ports',
                                     'id_format': {'type': 'integer', 'min': 1, 'pad_width': 0},
                                     'state_variables': {'name': {'type': 'string', 'label': 'Port Name'},
                                                         'port_number': {'type': 'integer',
                                                                         'label': 'Port Number'},
                                                         'port_type': {'type': 'string',
                                                                       'label': 'Connector'},
                                                         'available': {'type': 'boolean',
                                                                       'label': 'Available',
                                                                       'cloud_priority': 'high'},
                                                         'protected': {'type': 'boolean',
                                                                       'label': 'Protected',
                                                                       'cloud_priority': 'high'},
                                                         'enabled': {'type': 'boolean',
                                                                     'label': 'Port Enabled',
                                                                     'control': True,
                                                                     'cloud_priority': 'high'},
                                                         'link_state': {'type': 'string',
                                                                        'label': 'Link',
                                                                        'cloud_priority': 'high'},
                                                         'rlinkx_enabled': {'type': 'boolean',
                                                                            'label': 'RLinkX Enabled',
                                                                            'control': True},
                                                         'membership_type': {'type': 'string',
                                                                             'label': 'Membership Type'},
                                                         'group_id': {'type': 'integer',
                                                                      'label': 'Group ID',
                                                                      'control': True},
                                                         'group_name': {'type': 'string', 'label': 'Group'},
                                                         'neighbor': {'type': 'string',
                                                                      'label': 'Connected Device'},
                                                         'neighbor_port': {'type': 'string',
                                                                           'label': 'Remote Port'},
                                                         'rx_mbps': {'type': 'number',
                                                                     'label': 'Receive Rate',
                                                                     'unit': 'Mbps',
                                                                     'min': 0,
                                                                     'cloud_priority': 'low'},
                                                         'tx_mbps': {'type': 'number',
                                                                     'label': 'Transmit Rate',
                                                                     'unit': 'Mbps',
                                                                     'min': 0,
                                                                     'cloud_priority': 'low'},
                                                         'in_discards': {'type': 'integer',
                                                                         'label': 'Receive Discards',
                                                                         'min': 0,
                                                                         'cloud_priority': 'low'},
                                                         'out_discards': {'type': 'integer',
                                                                          'label': 'Transmit Discards',
                                                                          'min': 0,
                                                                          'cloud_priority': 'low'},
                                                         'link_down_counter': {'type': 'integer',
                                                                               'label': 'Link Drops',
                                                                               'min': 0,
                                                                               'cloud_priority': 'low'}},
                                     'label_field': 'name',
                                     'summary_fields': ['link_state', 'group_name', 'protected']},
                            'poe_port': {'label': 'PoE Port',
                                         'label_plural': 'PoE Ports',
                                         'id_format': {'type': 'integer', 'min': 1, 'pad_width': 0},
                                         'state_variables': {'name': {'type': 'string', 'label': 'Port Name'},
                                                             'port_number': {'type': 'integer',
                                                                             'label': 'Port Number'},
                                                             'capable_mode': {'type': 'string',
                                                                              'label': 'PoE Capability'},
                                                             'enabled': {'type': 'boolean',
                                                                         'label': 'PoE Enabled',
                                                                         'control': True,
                                                                         'cloud_priority': 'high'},
                                                             'priority': {'type': 'enum',
                                                                          'label': 'Priority',
                                                                          'values': ['low',
                                                                                     'high',
                                                                                     'critical'],
                                                                          'control': True},
                                                             'power_limit_type': {'type': 'enum',
                                                                                  'label': 'Power Limit Mode',
                                                                                  'values': ['class',
                                                                                             'none',
                                                                                             'user_defined'],
                                                                                  'control': True},
                                                             'power_limit_w': {'type': 'number',
                                                                               'label': 'User Power Limit',
                                                                               'unit': 'W',
                                                                               'min': 3,
                                                                               'max': 99,
                                                                               'step': 0.001,
                                                                               'control': True},
                                                             'current_limit_w': {'type': 'number',
                                                                                 'label': 'Effective Power '
                                                                                          'Limit',
                                                                                 'unit': 'W',
                                                                                 'min': 0},
                                                             'max_limit_w': {'type': 'number',
                                                                             'label': 'Maximum Power Limit',
                                                                             'unit': 'W',
                                                                             'min': 0},
                                                             'indication': {'type': 'string',
                                                                            'label': 'PoE Status',
                                                                            'cloud_priority': 'high'},
                                                             'error': {'type': 'string',
                                                                       'label': 'PoE Fault',
                                                                       'cloud_priority': 'high'},
                                                             'pd_class': {'type': 'string',
                                                                          'label': 'Powered Device Class'},
                                                             'power_w': {'type': 'number',
                                                                         'label': 'Power Draw (W)',
                                                                         'unit': 'W',
                                                                         'min': 0,
                                                                         'cloud_priority': 'low'},
                                                             'reserved_w': {'type': 'number',
                                                                            'label': 'Reserved Power',
                                                                            'unit': 'W',
                                                                            'min': 0,
                                                                            'cloud_priority': 'low'},
                                                             'voltage_v': {'type': 'number',
                                                                           'label': 'Voltage',
                                                                           'unit': 'V',
                                                                           'min': 0,
                                                                           'cloud_priority': 'low'},
                                                             'current_ma': {'type': 'number',
                                                                            'label': 'Current',
                                                                            'unit': 'mA',
                                                                            'min': 0,
                                                                            'cloud_priority': 'low'},
                                                             'temperature_c': {'type': 'number',
                                                                               'label': 'PoE Temperature',
                                                                               'unit': '°C',
                                                                               'cloud_priority': 'low'}},
                                         'label_field': 'name',
                                         'summary_fields': ['indication', 'power_w', 'error']},
                            'group': {'label': 'Group',
                                      'label_plural': 'Groups',
                                      'id_format': {'type': 'integer', 'min': 1, 'pad_width': 0},
                                      'state_variables': {'name': {'type': 'string', 'label': 'Group Name'},
                                                          'description': {'type': 'string',
                                                                          'label': 'Description'},
                                                          'vlan_id': {'type': 'integer', 'label': 'VLAN ID'},
                                                          'allow_config': {'type': 'boolean',
                                                                           'label': 'Management Access'},
                                                          'igmp_snooping': {'type': 'boolean',
                                                                            'label': 'IGMP Snooping'},
                                                          'igmp_querier': {'type': 'boolean',
                                                                           'label': 'IGMP Querier'},
                                                          'avb_enabled': {'type': 'boolean',
                                                                          'label': 'AVB Enabled'},
                                                          'ptp_clock_mode': {'type': 'string',
                                                                             'label': 'PTP Clock Mode'}},
                                      'label_field': 'name',
                                      'summary_fields': ['vlan_id', 'igmp_snooping', 'avb_enabled']}},
     'commands': {'refresh_status': {'label': 'Refresh Status',
                                     'help': 'Read the switch, ports, PoE, groups and connected devices '
                                             'again.'},
                  'get_port_status': {'label': 'Read Port Status',
                                      'help': 'Read the selected port’s link, name, protection and group '
                                              'membership.',
                                      'params': {'port': {'type': 'child_id',
                                                          'child_type': 'port',
                                                          'required': True,
                                                          'label': 'Port'}},
                                      'query_for': 'link_state'},
                  'get_poe_status': {'label': 'Read PoE Status',
                                     'help': 'Read the selected port’s PoE setting, power draw and fault '
                                             'status.',
                                     'params': {'port': {'type': 'child_id',
                                                         'child_type': 'poe_port',
                                                         'required': True,
                                                         'label': 'PoE Port'}},
                                     'query_for': 'enabled'},
                  'set_port_name': {'label': 'Set Port Name',
                                    'help': 'Change the selected port’s legend. Protected or unavailable '
                                            'ports refuse changes.',
                                    'params': {'port': {'type': 'child_id',
                                                        'child_type': 'port',
                                                        'required': True,
                                                        'label': 'Port'},
                                               'name': {'type': 'string',
                                                        'label': 'Port Name',
                                                        'required': True,
                                                        'pattern': '(?s).{0,16}'}},
                                    'sets': {'name': '{name}'}},
                  'set_port_enabled': {'label': 'Set Port Enabled',
                                       'help': 'Enable or disable the selected network port. Disabling a '
                                               'control-path port can disconnect OpenAVC.',
                                       'params': {'port': {'type': 'child_id',
                                                           'child_type': 'port',
                                                           'required': True,
                                                           'label': 'Port'},
                                                  'enabled': {'type': 'boolean',
                                                              'label': 'Enabled',
                                                              'required': True},
                                                  'force': {'type': 'boolean',
                                                            'label': 'Allow Uplink Change',
                                                            'default': False,
                                                            'help': 'Allow a port with multiple learned '
                                                                    'devices or a trunk assignment to be '
                                                                    'disabled. Protected ports remain '
                                                                    'protected.'}},
                                       'sets': {'enabled': '{enabled}'}},
                  'set_rlinkx_enabled': {'label': 'Set RLinkX Enabled',
                                         'help': 'Enable or disable RLinkX on the selected port. This '
                                                 'changes redundancy behavior on that port.',
                                         'params': {'port': {'type': 'child_id',
                                                             'child_type': 'port',
                                                             'required': True,
                                                             'label': 'Port'},
                                                    'enabled': {'type': 'boolean',
                                                                'label': 'Enabled',
                                                                'required': True}},
                                         'sets': {'rlinkx_enabled': '{enabled}'}},
                  'assign_port_group': {'label': 'Assign Port to Group',
                                        'help': 'Move the selected port into an existing group and apply '
                                                'that group’s settings. This can interrupt traffic and '
                                                'management access.',
                                        'params': {'port': {'type': 'child_id',
                                                            'child_type': 'port',
                                                            'required': True,
                                                            'label': 'Port'},
                                                   'group': {'type': 'integer',
                                                             'label': 'Group',
                                                             'required': True,
                                                             'options_state': 'group_options',
                                                             'min': 1,
                                                             'max': 254},
                                                   'force': {'type': 'boolean',
                                                             'label': 'Allow Uplink Change',
                                                             'default': False,
                                                             'help': 'Allow a port with multiple learned '
                                                                     'devices or a trunk assignment to '
                                                                     'change group. Protected ports remain '
                                                                     'protected.'}},
                                        'sets': {'group_id': '{group}'}},
                  'set_poe_enabled': {'label': 'Set PoE Enabled',
                                      'help': 'Turn PoE on or off for the selected port. Turning it off '
                                              'removes power from the attached endpoint.',
                                      'params': {'port': {'type': 'child_id',
                                                          'child_type': 'poe_port',
                                                          'required': True,
                                                          'label': 'PoE Port'},
                                                 'value': {'type': 'boolean',
                                                           'label': 'Enabled',
                                                           'required': True},
                                                 'force': {'type': 'boolean',
                                                           'label': 'Allow Uplink Change',
                                                           'default': False,
                                                           'help': 'Allow PoE to be disabled on a port with '
                                                                   'multiple learned devices. Protected '
                                                                   'ports remain protected.'}},
                                      'sets': {'enabled': '{value}'}},
                  'set_poe_priority': {'label': 'Set PoE Priority',
                                       'help': 'Set which PoE devices take priority if the switch runs short '
                                               'of power.',
                                       'params': {'port': {'type': 'child_id',
                                                           'child_type': 'poe_port',
                                                           'required': True,
                                                           'label': 'PoE Port'},
                                                  'value': {'type': 'enum',
                                                            'label': 'Priority',
                                                            'required': True,
                                                            'values': ['low', 'high', 'critical']}},
                                       'sets': {'priority': '{value}'}},
                  'set_poe_power_limit_type': {'label': 'Set PoE Power Limit Mode',
                                               'help': 'Choose the selected port’s PoE limit mode. User '
                                                       'Defined uses its stored User Power Limit.',
                                               'params': {'port': {'type': 'child_id',
                                                                   'child_type': 'poe_port',
                                                                   'required': True,
                                                                   'label': 'PoE Port'},
                                                          'value': {'type': 'enum',
                                                                    'label': 'Power Limit Mode',
                                                                    'required': True,
                                                                    'values': [{'value': 'class',
                                                                                'label': 'Device Class'},
                                                                               {'value': 'none',
                                                                                'label': 'No User Limit'},
                                                                               {'value': 'user_defined',
                                                                                'label': 'User Defined'}]}},
                                               'sets': {'power_limit_type': '{value}'}},
                  'set_poe_power_limit': {'label': 'Set PoE Power Limit',
                                          'help': 'Store a PoE power limit in watts. It takes effect only '
                                                  'when Power Limit Mode is User Defined; the effective '
                                                  'limit may be lower for the port’s PoE mode.',
                                          'params': {'port': {'type': 'child_id',
                                                              'child_type': 'poe_port',
                                                              'required': True,
                                                              'label': 'PoE Port'},
                                                     'value': {'type': 'number',
                                                               'label': 'Power Limit',
                                                               'required': True,
                                                               'min': 3,
                                                               'max': 99,
                                                               'decimals': 3,
                                                               'unit': 'W'}},
                                          'sets': {'power_limit_w': '{value}'}},
                  'cycle_poe': {'label': 'Power Cycle PoE',
                                'help': 'Turn the selected port’s PoE off, wait, then restore it. The '
                                        'endpoint reboots. Requires PoE to be enabled initially; a lost '
                                        'connection may prevent restoration.',
                                'params': {'port': {'type': 'child_id',
                                                    'child_type': 'poe_port',
                                                    'required': True,
                                                    'label': 'PoE Port'},
                                           'off_seconds': {'type': 'integer',
                                                           'label': 'Seconds Without Power',
                                                           'default': 4,
                                                           'min': 1,
                                                           'max': 60,
                                                           'unit': 's'},
                                           'force': {'type': 'boolean',
                                                     'label': 'Allow Uplink Change',
                                                     'default': False,
                                                     'help': 'Allow a port with multiple learned devices to '
                                                             'be cycled. Protected ports remain protected.'}},
                                'confirm': 'Interrupt power to the selected endpoint and restart it?'},
                  'reset_port_counters': {'label': 'Reset Port Counters',
                                          'help': 'Clear the selected port’s traffic and link counters. '
                                                  'Traffic continues, so active counters may rise again '
                                                  'immediately.',
                                          'params': {'port': {'type': 'child_id',
                                                              'child_type': 'port',
                                                              'required': True,
                                                              'label': 'Port'}},
                                          'confirm': 'Clear the counters for this port?'}},
     'quick_actions': ['get_port_status', 'get_poe_status'],
     'discovery': {'manufacturer_alias': ['Luminex', 'Luminex Network Intelligence']}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._socket = None
        self._reader = None
        self._lock = asyncio.Lock()
        self._fault = None
        self._cache = {}
        self._versions = Counter()
        self._subscriptions = set()
        self._feedback_error = ''

    def _url(self, websocket=False):
        secure = bool(self.config.get('ssl', False))
        scheme = ('wss' if secure else 'ws') if websocket else ('https' if secure else 'http')
        host = str(self.config.get('host', '')).strip()
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        return f'{scheme}://{host}:{int(self.config.get("port", 80))}'

    def _auth_header(self):
        username = str(self.config.get('username', ''))
        password = str(self.config.get('password', ''))
        if not username and not password:
            return {}
        token = base64.b64encode(f'{username}:{password}'.encode()).decode()
        self.redact_in_log(token)
        return {'Authorization': 'Basic ' + token}

    async def _create_transport(self, transport_type):
        if not str(self.config.get('host', '')).strip():
            raise ConnectionFaultError('Enter the switch management address.', code='invalid_config')
        self._client = httpx.AsyncClient(
            base_url=self._url(), headers=self._auth_header(),
            timeout=float(self.config.get('timeout', 5)), follow_redirects=False,
            verify=bool(self.config.get('verify_ssl', True)),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    async def _post_connect(self):
        self._fault = None
        self._feedback_error = ''
        self._cache.clear()
        self._versions.clear()
        data = await self._call('GET', '/device')
        self._check_identity(data)
        self._cache['/device'] = data
        self._apply_device(data)

    def _check_identity(self, data):
        model = re.sub(r'[^a-z0-9]', '', str(data.get('model', '')).lower())
        models = self.DRIVER_INFO['compatible_models'][0]['models']
        accepted = {re.sub(r'[^a-z0-9]', '', name.lower()) for name in models}
        if model not in accepted:
            raise ConnectionFaultError('This driver requires a supported GigaCore Gen 2 switch.', code='invalid_config')
        if not re.fullmatch(r'1\.5\.\d+', str(data.get('api_version', ''))):
            raise ConnectionFaultError('This driver requires GigaCore API 1.5.x. Check the switch firmware.', code='invalid_config')

    async def _initial_sync(self):
        await self.refresh_children()
        options = {'additional_headers': self._auth_header(), 'proxy': None,
                   'open_timeout': float(self.config.get('timeout', 5)),
                   'close_timeout': 2, 'ping_interval': None}
        local = self.local_address()
        options['local_addr'] = (local, 0) if local else None
        if self.config.get('ssl'):
            context = ssl.create_default_context()
            if not self.config.get('verify_ssl', True):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            options['ssl'] = context
        try:
            self._socket = await websocket_connect(self._url(True) + '/api/ws', **options)
        except InvalidStatus as exc:
            if exc.response.status_code in (401, 403):
                raise ConnectionFaultError('The switch refused WebSocket sign-in.', code='auth_failed') from exc
            raise ConnectionError('The switch refused its live status connection.') from exc
        self._reader = asyncio.create_task(self._receive_updates())
        await self._sync_subscriptions()

    def _link_alive(self):
        return self._client is not None and self._fault is None

    async def _close_session(self):
        task, self._reader = self._reader, None
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        socket, self._socket = self._socket, None
        if socket:
            await socket.close()
        client, self._client = self._client, None
        if client:
            await client.aclose()
        self._subscriptions.clear()
        self.set_state('live_feedback', False)

    async def _call(self, method, path, body=None):
        if self._client is None:
            raise ConnectionError('The switch session is closed.')
        kwargs = {'json': body} if method == 'PUT' else {}
        response = await self._client.request(method, '/api' + path, **kwargs)
        self._check_response(response, method)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise CommandRefusedError('The switch returned an unreadable response. Check the address and API version.') from exc

    @staticmethod
    def _check_response(response, method):
        code = response.status_code
        if code == 401 or 300 <= code < 400:
            raise ConnectionFaultError('The switch refused the username or password.', code='auth_failed')
        if code == 403 and method == 'GET':
            raise ConnectionFaultError('The account cannot read switch status.', code='auth_failed')
        if 200 <= code < 300:
            return
        LuminexGigaCoreGen2Driver._refused_response(response)

    @staticmethod
    def _refused_response(response):
        reason = ''
        with suppress(ValueError, AttributeError):
            reason = str(response.json().get('reason', ''))
        raise CommandRefusedError(reason or f'The switch refused the request (HTTP {response.status_code}).')

    async def _read(self, path):
        versions = dict(self._versions)
        data = await self._call('GET', path)
        # A live value received while the GET waited must not be replaced by
        # an older snapshot. Overlay only those fields, retaining fresh telemetry.
        for changed, revision in self._versions.items():
            if revision != versions.get(changed, 0) and changed.startswith(path + '/'):
                self._patch(data, changed[len(path) + 1:].split('/'), self._cached(changed))
        if self._versions[path] != versions.get(path, 0):
            data = self._cache.get(path, data)
        self._cache_read(path, data)
        return data

    @staticmethod
    def _step(data, key):
        if isinstance(data, list):
            return next((row for row in data if str(row.get('port_number', row.get('group_id'))) == key), None)
        return data.get(key) if isinstance(data, dict) else None

    @staticmethod
    def _patch(data, parts, value):
        if not parts:
            return
        parent = data
        for key in parts[:-1]:
            parent = LuminexGigaCoreGen2Driver._step(parent, key)
        if isinstance(parent, dict):
            parent[parts[-1]] = deepcopy(value)
        elif isinstance(parent, list):
            LuminexGigaCoreGen2Driver._replace_row(parent, parts[-1], value)

    @staticmethod
    def _replace_row(rows, key, value):
        for index, row in enumerate(rows):
            if str(row.get('port_number', row.get('group_id'))) == key:
                rows[index] = deepcopy(value)
                return

    def _cached(self, path):
        roots = [root for root in self._cache if path == root or path.startswith(root + '/')]
        if not roots:
            return None
        root = max(roots, key=len)
        data = self._cache[root]
        for key in path[len(root):].strip('/').split('/') if root != path else []:
            data = self._step(data, key)
        return data

    def _cache_read(self, path, data):
        # Keep a row read and its containing roster consistent. A full roster
        # replaces old individual snapshots, including rows that disappeared.
        for root in list(self._cache):
            if root.startswith(path + '/'):
                del self._cache[root]
            elif path.startswith(root + '/'):
                self._patch(self._cache[root], path[len(root) + 1:].split('/'), data)
        self._cache[path] = deepcopy(data)

    async def _refresh(self):
        for path, apply in [('/device', self._apply_device), ('/groups/group', self._apply_groups),
                            ('/ports/port', self._apply_ports), ('/poe', self._apply_poe),
                            ('/system', self._apply_system), ('/status_message', self._apply_status),
                            ('/lldp/neighbor', self._apply_neighbors)]:
            apply(await self._read(path))
        self.set_state('config_mode', await self._call('GET', '/config/mode'))
        self.set_state('led_brightness', await self._call('GET', '/led/brightness'))
        if self._socket:
            await self._sync_subscriptions()
        self.set_state('last_error', self._feedback_error)

    async def refresh_children(self):
        async with self._lock:
            await self._refresh()
        return {kind: len(self.list_children(kind)) for kind in ('port', 'poe_port', 'group')}

    async def poll(self):
        if self._fault:
            raise self._fault
        await self.refresh_children()

    async def _liveness_probe(self):
        if self._fault:
            raise self._fault
        if self._socket is None:
            raise ConnectionError('The live status connection is closed.')
        pong = await self._socket.ping()
        await asyncio.wait_for(pong, float(self.config.get('timeout', 5)))

    def _apply_device(self, data):
        for prop, key in [('device_name', 'name'), ('description', 'description'), ('model', 'model'),
                          ('serial_number', 'serial'), ('api_version', 'api_version'),
                          ('mac_address', 'mac_address'), ('system_id', 'system_id')]:
            self.set_state(prop, data.get(key))
        self.set_state('firmware_version', (data.get('firmware') or {}).get('active'))

    @staticmethod
    def _valid_row(row, key):
        return isinstance(row, dict) and type(row.get(key)) is int

    @staticmethod
    def _rows(data, key):
        if not isinstance(data, list):
            raise CommandRefusedError('The switch returned an unreadable inventory. Check its API version.')
        if not all(LuminexGigaCoreGen2Driver._valid_row(row, key) for row in data):
            raise CommandRefusedError('The switch returned an unreadable inventory ID.')
        LuminexGigaCoreGen2Driver._check_ids([row[key] for row in data])
        return data

    @staticmethod
    def _check_ids(ids):
        if len(set(ids)) != len(ids) or min(ids, default=1) < 1:
            raise CommandRefusedError('The switch returned duplicate or invalid inventory IDs.')

    def _reconcile_children(self, kind, rows, key, values):
        seen = set()
        for row in self._rows(rows, key):
            num = row[key]
            seen.add(num)
            self.register_child(kind, num)
            self.set_child_state_batch(kind, num, values(row))
        for num in set(self.list_children(kind)) - seen:
            self.deregister_child(kind, num)

    def _apply_groups(self, rows):
        self._reconcile_children('group', rows, 'group_id', self._group_values)
        options = [{'value': row['group_id'], 'label': row.get('name') or f'Group {row["group_id"]}'} for row in rows]
        self.set_state('group_options', json.dumps(options))
        self._refresh_port_labels()

    @staticmethod
    def _group_values(row):
        return {'online': True, 'name': row.get('name'), 'description': row.get('description'),
                'vlan_id': row.get('vid'), 'allow_config': row.get('allow_config'),
                'igmp_snooping': (row.get('igmp') or {}).get('snooping'),
                'igmp_querier': (row.get('igmp') or {}).get('querier'),
                'avb_enabled': (row.get('avb') or {}).get('enabled'), 'ptp_clock_mode': row.get('ptp_clock_mode')}

    def _apply_ports(self, rows):
        self._reconcile_children('port', rows, 'port_number', self._port_values)
        self.set_state('port_count', len(rows))
        self.set_state('ports_up', sum(bool(row.get('link_state') not in (None, '', 'down', 'unknown')) for row in rows))
        self._refresh_port_labels()

    def _port_values(self, row):
        member = row.get('member_of') or {}
        group = member.get('id') if member.get('type') == 'group' else None
        values = {'online': True, 'name': row.get('legend'),
                  'port_number': row['port_number'], 'port_type': row.get('type'),
                  'available': row.get('available'), 'protected': row.get('protected'), 'enabled': row.get('enabled'),
                  'link_state': row.get('link_state'), 'rlinkx_enabled': (row.get('rlinkx') or {}).get('enabled'),
                  'membership_type': member.get('type'), 'group_id': group,
                  'group_name': self.get_child_state('group', group).get('name') if group in self.list_children('group') else ''}
        counters = row.get('counters') or {}
        for prop, wire, scale in [('rx_mbps', 'rx_bit_rate', 1000000), ('tx_mbps', 'tx_bit_rate', 1000000),
                                  ('in_discards', 'in_discards', 1), ('out_discards', 'out_discards', 1),
                                  ('link_down_counter', 'link_down_counter', 1)]:
            value = counters.get(wire) if self.config.get('enable_traffic_counters', False) else None
            values[prop] = self._scaled(value, scale)
        return values

    def _refresh_port_labels(self):
        rows = self._cache.get('/ports/port') or []
        for row in rows:
            num = row.get('port_number')
            if num in self.list_children('port'):
                values = self._port_values(row)
                self.set_child_state_batch('port', num, {key: values[key] for key in ('name', 'group_name')})
            if num in self.list_children('poe_port'):
                self.set_child_state('poe_port', num, 'name', row.get('legend') or f'Port {num}')

    def _apply_poe(self, data):
        capable = data.get('capable') is True
        self.set_state('poe_capable', capable)
        self.set_state('poe_status', data.get('pse_status'))
        for prop, wire in [('poe_total_w', 'total_power'), ('poe_consumed_w', 'consumed_power'), ('poe_reserved_w', 'reserved_power')]:
            self.set_state(prop, self._scaled(data.get(wire), 1000))
        self._reconcile_children('poe_port', data.get('ports', []) if capable else [], 'port_number', self._poe_values)

    @staticmethod
    def _scaled(value, divisor):
        if type(value) not in (float, int):
            return None
        return value if divisor == 1 else value / divisor

    def _poe_values(self, row):
        num = row['port_number']
        port = self.get_child_state('port', num) if num in self.list_children('port') else {}
        values = {'online': True, 'name': port.get('name') or f'Port {num}', 'port_number': num}
        for key in ('enabled', 'priority', 'power_limit_type', 'capable_mode', 'indication', 'error', 'pd_class'):
            values[key] = row.get(key)
        for prop, wire, scale in [('power_limit_w', 'user_defined_power_limit', 1000), ('current_limit_w', 'current_power_limit', 1000),
                                  ('max_limit_w', 'max_power_limit', 1000), ('power_w', 'power_output', 1000),
                                  ('reserved_w', 'reserved_power', 1000), ('voltage_v', 'voltage_output', 1),
                                  ('current_ma', 'current_output', 1), ('temperature_c', 'temperature', 1)]:
            values[prop] = self._scaled(row.get(wire), scale)
        return values

    def _apply_system(self, data):
        self.set_state('uptime_s', data.get('uptime'))
        self.set_state('cpu_load', data.get('cpu_load'))
        self.set_state('temperature_c', (data.get('temperature') or {}).get('current'))

    def _apply_status(self, data):
        self.set_state('status_level', (data or {}).get('level'))
        self.set_state('status_message', (data or {}).get('message'))

    @staticmethod
    def _neighbor_values(matches):
        names = [row.get('sys_name') or row.get('chassis_id') or 'Unnamed device' for row in matches]
        ports = [str(row.get('remote_port', '')) for row in matches]
        return {'neighbor': ', '.join(names), 'neighbor_port': ', '.join(ports)}

    def _apply_neighbors(self, rows):
        if not isinstance(rows, list):
            raise CommandRefusedError('The switch returned an unreadable connected-device list.')
        for num in self.list_children('port'):
            matches = [row for row in rows if row.get('port_number') == num]
            self.set_child_state_batch('port', num, self._neighbor_values(matches))

    def _wanted_subscriptions(self):
        paths = {'/device', '/groups/group', '/poe/capable', '/status_message', '/led/brightness', '/config/mode'}
        for num in self.list_children('port'):
            paths.update(f'/ports/port/{num}/{key}' for key in ('legend', 'enabled', 'protected', 'available', 'link_state', 'member_of', 'rlinkx'))
        for num in self.list_children('poe_port'):
            paths.update(f'/poe/ports/{num}/{key}' for key in ('enabled', 'priority', 'power_limit_type', 'user_defined_power_limit', 'indication', 'error'))
        return paths

    async def _sync_subscriptions(self):
        wanted = self._wanted_subscriptions()
        changes = [('remove', self._subscriptions - wanted), ('add', wanted - self._subscriptions)]
        self._subscriptions = wanted
        for action, paths in changes:
            for path in sorted(paths):
                message = json.dumps({'subscription': {'path': path, 'action': action, 'method': 'full'}})
                await self._socket.send(message)
                self.record_traffic('tx', message)
        self._subscriptions = wanted

    async def _receive_updates(self):
        try:
            async for raw in self._socket:
                self.record_traffic('rx', raw)
                self._notification(json.loads(raw))
            raise ConnectionError('The switch closed its live status connection.')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fault = exc
            self._feedback_error = 'Live feedback stopped. Reconnect the switch after checking its API access.'
            self.set_state('last_error', self._feedback_error)
            self.set_state('live_feedback', False)

    def _notification(self, message):
        if message.get('error'):
            reason = str(message['error'].get('reason', 'Subscription refused'))
            raise CommandRefusedError('The switch refused live feedback: ' + reason)
        notice = message.get('api_notification')
        if notice is not None:
            self._apply_notice(notice)
        elif message.get('message') == 'Subscription added':
            self.set_state('live_feedback', True)

    def _apply_notice(self, notice):
        path = notice.get('path')
        if path not in self._subscriptions:
            return
        if notice.get('notification_type') not in ('subscription_response', 'full_update'):
            return
        self._versions[path] += 1
        value = notice.get('new_value')
        self._update_cached_notice(path, value)
        self._apply_notification(path, value)
        self.set_state('live_feedback', True)

    def _update_cached_notice(self, path, value):
        for root in list(self._cache):
            if root == path:
                self._cache[root] = deepcopy(value)
            elif path.startswith(root + '/'):
                self._patch(self._cache[root], path[len(root) + 1:].split('/'), value)

    def _apply_notification(self, path, value):
        direct = {'/device': self._apply_device, '/groups/group': self._apply_groups, '/status_message': self._apply_status}
        if path in direct:
            direct[path](value)
        elif path.startswith('/ports/port/'):
            self._apply_ports(self._cache['/ports/port'])
        elif path.startswith('/poe/'):
            self._apply_poe(self._cache['/poe'])
        elif path in ('/led/brightness', '/config/mode'):
            self.set_state('led_brightness' if path.startswith('/led') else 'config_mode', value)

    def _port_id(self, params, kind='port'):
        num = int(params['port'])
        if num not in self.list_children(kind):
            raise CommandRefusedError('Choose a port currently listed by the switch.')
        return num

    async def _writable_port(self, num):
        row = await self._call('GET', f'/ports/port/{num}')
        if row.get('protected') is not False:
            raise CommandRefusedError('This port is protected. Remove protection in the switch web UI before changing it.')
        if row.get('available') is not True:
            raise CommandRefusedError('This port is unavailable. Check its license and hardware in the switch web UI.')
        return row

    async def _guard_uplink(self, num, row, params):
        if params.get('force'):
            return
        entries = await self._call('GET', '/mac/mac_table')
        macs = {entry.get('mac') for entry in entries if entry.get('port_number') == num}
        trunk = (row.get('member_of') or {}).get('type') == 'trunk'
        if len(macs) > 1 or trunk:
            raise CommandRefusedError('This port appears to be an uplink. Choose an endpoint port, or enable Allow Uplink Change after checking the control path.')

    async def _write_confirmed(self, path, value):
        try:
            await self._call('PUT', path, value)
        except httpx.TransportError as exc:
            raise CommandPartialError('The connection was lost while sending the change. Check the switch; the setting may have changed.') from exc
        try:
            actual = await self._call('GET', path)
        except Exception as exc:
            raise CommandPartialError('The switch accepted the change, but its resulting value could not be read. Check the switch before retrying.') from exc
        if actual != value:
            raise CommandRefusedError(f'The switch reports {actual!r} after the change. Check port protection and configuration in its web UI.')
        return actual

    async def send_command(self, command, params=None):
        params = dict(params or {})
        handlers = {'refresh_status': self._command_refresh, 'get_port_status': self._get_port,
                    'get_poe_status': self._get_poe, 'set_port_name': self._port_write,
                    'set_port_enabled': self._port_write, 'set_rlinkx_enabled': self._port_write,
                    'assign_port_group': self._assign_group, 'cycle_poe': self._cycle_poe,
                    'reset_port_counters': self._reset_counters}
        if command.startswith('set_poe_') and command in self.DRIVER_INFO['commands']:
            handler = self._poe_write
        else:
            handler = handlers.get(command)
        if handler is None:
            raise CommandRefusedError('Unknown switch command.')
        async with self._lock:
            result = await handler(command, params)
            self.set_state('last_error', self._feedback_error)
            return result

    async def _command_refresh(self, command, params):
        await self._refresh()

    async def _read_port(self, num):
        row = await self._read(f'/ports/port/{num}')
        self.set_child_state_batch('port', num, self._port_values(row))
        return row

    async def _read_poe(self, num):
        row = await self._read(f'/poe/ports/{num}')
        self.set_child_state_batch('poe_port', num, self._poe_values(row))
        return row

    async def _get_port(self, command, params):
        return await self._read_port(self._port_id(params))

    async def _get_poe(self, command, params):
        return await self._read_poe(self._port_id(params, 'poe_port'))

    async def _port_write(self, command, params):
        num = self._port_id(params)
        row = await self._writable_port(num)
        key, value = {'set_port_name': ('legend', params.get('name')),
                      'set_port_enabled': ('enabled', params.get('enabled')),
                      'set_rlinkx_enabled': ('rlinkx/enabled', params.get('enabled'))}[command]
        if command == 'set_port_enabled' and value is False:
            await self._guard_uplink(num, row, params)
        await self._write_confirmed(f'/ports/port/{num}/{key}', value)
        await self._read_port(num)

    async def _poe_write(self, command, params):
        num = self._port_id(params, 'poe_port')
        row = await self._writable_port(num)
        key = command.removeprefix('set_poe_')
        value = params['value']
        if key == 'enabled' and value is False:
            await self._guard_uplink(num, row, params)
        if key == 'power_limit':
            key, value = 'user_defined_power_limit', round(float(value) * 1000)
        await self._write_confirmed(f'/poe/ports/{num}/{key}', value)
        await self._read_poe(num)

    async def _assign_group(self, command, params):
        num = self._port_id(params)
        row = await self._writable_port(num)
        group = int(params['group'])
        groups = await self._read('/groups/group')
        self._apply_groups(groups)
        if group not in self.list_children('group'):
            raise CommandRefusedError('Choose a group currently listed by the switch.')
        await self._guard_uplink(num, row, params)
        try:
            await self._call('PUT', '/groups/assign_ports', {'id': group, 'ports': [num]})
            actual = await self._read_port(num)
        except httpx.TransportError as exc:
            raise CommandPartialError('Group assignment may have changed this port’s network. Check the switch through another management path.') from exc
        if actual.get('member_of') != {'type': 'group', 'id': group}:
            raise CommandRefusedError('The port did not report the requested group. Check its configuration in the switch web UI.')

    async def _cycle_poe(self, command, params):
        num = self._port_id(params, 'poe_port')
        row = await self._writable_port(num)
        await self._guard_uplink(num, row, params)
        path = f'/poe/ports/{num}/enabled'
        if await self._call('GET', path) is not True:
            raise CommandRefusedError('PoE must already be enabled before it can be power-cycled.')
        try:
            await self._write_confirmed(path, False)
            await self._read_poe(num)
            await asyncio.sleep(int(params.get('off_seconds', 4)))
        finally:
            # Restore even if the readback, hold or caller is cancelled. A
            # disconnected control path must never look like an unchanged port.
            try:
                await self._write_confirmed(path, True)
                await self._read_poe(num)
            except Exception as exc:
                raise CommandPartialError('PoE restoration could not be confirmed. The endpoint may still be off. Reach the switch another way and enable PoE on this port.') from exc
        return 'PoE was switched off and restored. Allow the endpoint time to start.'

    async def _reset_counters(self, command, params):
        num = self._port_id(params)
        await self._writable_port(num)
        await self._call('PUT', f'/ports/port/{num}/counters/reset', {})
        await self._read_port(num)

    async def set_device_setting(self, key, value):
        paths = {'device_name': '/device/name', 'description': '/device/description',
                 'system_id': '/device/system_id', 'led_brightness': '/led/brightness'}
        if key not in paths:
            raise CommandRefusedError('Unknown switch setting.')
        async with self._lock:
            actual = await self._write_confirmed(paths[key], value)
            self.set_state(key, actual)
