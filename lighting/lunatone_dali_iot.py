"""Lunatone DALI-2 IoT / IoT4 lighting control (manufacturer API M0023).

Python: device-enumerated sparse light/zone/sequence rosters, capability-dependent
light schemas and partial WebSocket updates require runtime reconciliation.
HTTP GET snapshots and root WebSocket events supply feedback; commands never
invent state. Reopening the event stream triggers a full snapshot. Scenes are
numbered in the API; project-local labels do not imply programmed scene values
or active-scene feedback. API IDs are not DALI bus addresses.

The common API is linked by both gateway datasheets. No raw DALI, emergency
tests, bus readdressing, firmware updates or guessed sensor layouts are sent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from copy import deepcopy

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import WebSocketException

from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError


class LunatoneDaliIotDriver(BaseDriver):
    HEALTH_INTERVAL_S = 30
    HEALTH_TIMEOUT_S = 10
    HEALTH_FAULT_MESSAGE = "The Lunatone gateway stopped answering status requests."

    DRIVER_INFO = {'id': 'lunatone_dali_iot',
     'name': 'Lunatone DALI-2 IoT',
     'manufacturer': 'Lunatone',
     'category': 'lighting',
     'version': '1.0.0',
     'author': 'OpenAVC',
     'transport': 'http',
     'description': 'Control named DALI lights, zones and stored sequences through a Lunatone DALI-2 IoT or '
                    'IoT4 gateway. Imports registered lights automatically, follows live lighting updates, '
                    'and provides named scene selections.',
     'source_url': 'https://www.lunatone.com/wp-content/uploads/2021/08/89453886_DALI2_IOT_API_Dokumentation_EN_M0023.pdf',
     'min_platform_version': '0.37.1',
     'ports': [80],
     'protocols': ['lunatone_dali_iot'],
     'tags': ['dali', 'dali-2', 'lighting', 'scenes', 'tunable-white'],
     'simulated': True,
     'verified': False,
     'web_ui': 'http://{host}:{port}',
     'compatible_models': [{'manufacturer': 'Lunatone',
                            'models': ['DALI-2 IoT (89453886)',
                                       'DALI-2 IoT Node-RED (89453886-NR)',
                                       'DALI-2 IoT EM (89453886-EM)'],
                            'confidence': 'untested',
                            'notes': 'Routine lighting control through the documented REST/WebSocket API. '
                                     'Emergency-test scheduling and Node-RED administration are not '
                                     'included.'},
                           {'manufacturer': 'Lunatone',
                            'models': ['DALI-2 IoT4 (22176625)',
                                       'DALI-2 IoT4 PS (22176625-PS)',
                                       'DALI-2 IoT4 Node-RED (22176625-NR)',
                                       'DALI-2 IoT4 PS Node-RED (22176625-PS-NR)',
                                       'DALI-2 IoT4 EM (22176625-EM)',
                                       'DALI-2 IoT4 PS EM (22176625-PS-EM)'],
                            'confidence': 'untested',
                            'notes': 'Lights on all four lines use the gateway’s unique device IDs. '
                                     'Configured zones may span lines. Emergency-test scheduling is not '
                                     'included.'}],
     'help': {'overview': 'The gateway’s registered lights, zones and sequences appear automatically. Each '
                          'light offers only the features reported by the gateway. Status is what the '
                          'gateway reports; registering a light does not prove the physical gear is '
                          'responding. DALI scenes use numbers 0–15. Give those numbers meaningful names in '
                          'Scene Names; recalling a scene does not create it or report it as active.',
              'setup': '1. Commission the DALI installation in Lunatone’s tools and enable its local API. '
                       'Enter the gateway IP address and HTTP port. No other controller software needs to '
                       'remain running.\n'
                       '2. Registered lights load automatically. If the list is empty, use Scan Addressed '
                       'Lights to register already addressed gear without changing its addresses. Do '
                       'initial addressing or installation resets in Lunatone’s tools.\n'
                       '3. Use Scene Names to label the 0–15 scene numbers already programmed into your '
                       'lights. Names are saved in this OpenAVC project.\n'
                       '4. Use configured Zones for grouped lighting, especially across IoT4 lines. '
                       'Individual lights retain their own brightness and color readings.\n'
                       '5. Keep Poll Interval above zero for periodic list reconciliation. Live updates '
                       'reconnect automatically and re-read all lists. Refresh from Device also reloads '
                       'them.\n'
                       '6. The documented API uses local HTTP and WebSocket access. Keep access to that '
                       'network restricted to trusted controllers.'},
     'default_config': {'host': '',
                        'port': 80,
                        'poll_interval': 30,
                        'timeout': 5,
                        'scene_names': [],
                        'simulation_lines': 1},
     'config_schema': {'host': {'type': 'string', 'label': 'IP Address', 'required': True},
                       'port': {'type': 'integer',
                                'label': 'HTTP Port',
                                'default': 80,
                                'min': 1,
                                'max': 65535},
                       'poll_interval': {'type': 'number',
                                         'label': 'Poll Interval (seconds)',
                                         'default': 30,
                                         'min': 0,
                                         'max': 3600,
                                         'advanced': True},
                       'timeout': {'type': 'number',
                                   'label': 'Request Timeout (seconds)',
                                   'default': 5,
                                   'min': 1,
                                   'max': 30,
                                   'advanced': True},
                       'scene_names': {'type': 'table',
                                       'label': 'Scene Names',
                                       'default': [],
                                       'row_label': 'scene',
                                       'help': 'Label programmed DALI scenes. This does not change any '
                                               'scene values in the lights.',
                                       'columns': {'scene': {'type': 'integer',
                                                             'label': 'Scene Number',
                                                             'min': 0,
                                                             'max': 15,
                                                             'required': True},
                                                   'name': {'type': 'string',
                                                            'label': 'Name',
                                                            'required': True}}},
                       'simulation_lines': {'type': 'enum',
                                            'label': 'DALI Lines for Simulation',
                                            'default': 1,
                                            'values': [1, 4],
                                            'advanced': True,
                                            'help': 'Used only by the simulator. A connected gateway '
                                                    'reports its actual line count.'}},
     'state_variables': {'gateway_name': {'type': 'string',
                                          'label': 'Gateway Name',
                                          'cloud_priority': 'low'},
                         'firmware_version': {'type': 'string',
                                              'label': 'Software / Firmware',
                                              'cloud_priority': 'low'},
                         'serial_number': {'type': 'string',
                                           'label': 'Serial Number',
                                           'cloud_priority': 'low'},
                         'article_number': {'type': 'string',
                                            'label': 'Article Number',
                                            'cloud_priority': 'low'},
                         'line_count': {'type': 'integer', 'label': 'DALI Lines', 'cloud_priority': 'low'},
                         'light_count': {'type': 'integer',
                                         'label': 'Registered Lights',
                                         'cloud_priority': 'low'},
                         'zone_count': {'type': 'integer', 'label': 'Zones', 'cloud_priority': 'low'},
                         'sequence_count': {'type': 'integer',
                                            'label': 'Sequences',
                                            'cloud_priority': 'low'},
                         'scan_status': {'type': 'string', 'label': 'Light Scan', 'cloud_priority': 'low'},
                         'scan_progress': {'type': 'number',
                                           'label': 'Scan Progress',
                                           'cloud_priority': 'low'},
                         'scan_found': {'type': 'integer', 'label': 'Lights Found', 'cloud_priority': 'low'},
                         'last_error': {'type': 'string', 'label': 'Last Error', 'cloud_priority': 'low'},
                         'live_feedback': {'type': 'boolean',
                                           'label': 'Live Feedback Connected',
                                           'cloud_priority': 'high'},
                         'gateway_errors': {'type': 'string',
                                            'label': 'Gateway Errors',
                                            'cloud_priority': 'high'}},
     'child_entity_types': {'light': {'label': 'Light',
                                      'label_plural': 'Lights',
                                      'id_format': {'type': 'integer', 'min': 0},
                                      'dynamic': True,
                                      'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                          'line': {'type': 'integer',
                                                                   'label': 'DALI Line',
                                                                   'min': 0,
                                                                   'max': 3},
                                                          'address': {'type': 'integer',
                                                                      'label': 'DALI Address',
                                                                      'min': 0,
                                                                      'max': 63},
                                                          'feedback': {'type': 'string',
                                                                       'label': 'Feedback'},
                                                          'power': {'type': 'boolean',
                                                                    'label': 'On',
                                                                    'control': True,
                                                                    'cloud_priority': 'high'},
                                                          'level': {'type': 'number',
                                                                    'label': 'Brightness',
                                                                    'min': 0,
                                                                    'max': 100,
                                                                    'step': 1,
                                                                    'unit': '%',
                                                                    'control': True},
                                                          'color_temperature': {'type': 'number',
                                                                                'label': 'Color Temperature',
                                                                                'min': 100,
                                                                                'max': 20000,
                                                                                'step': 1,
                                                                                'unit': 'K',
                                                                                'control': True},
                                                          'red': {'type': 'number',
                                                                  'label': 'Red',
                                                                  'min': 0,
                                                                  'max': 1,
                                                                  'step': 0.01},
                                                          'green': {'type': 'number',
                                                                    'label': 'Green',
                                                                    'min': 0,
                                                                    'max': 1,
                                                                    'step': 0.01},
                                                          'blue': {'type': 'number',
                                                                   'label': 'Blue',
                                                                   'min': 0,
                                                                   'max': 1,
                                                                   'step': 0.01},
                                                          'white': {'type': 'number',
                                                                    'label': 'White',
                                                                    'min': 0,
                                                                    'max': 1,
                                                                    'step': 0.01},
                                                          'amber': {'type': 'number',
                                                                    'label': 'Amber',
                                                                    'min': 0,
                                                                    'max': 1,
                                                                    'step': 0.01},
                                                          'free_color': {'type': 'number',
                                                                         'label': 'Free Color',
                                                                         'min': 0,
                                                                         'max': 1,
                                                                         'step': 0.01},
                                                          'color_x': {'type': 'number',
                                                                      'label': 'Color X',
                                                                      'min': 0,
                                                                      'max': 1,
                                                                      'step': 0.01},
                                                          'color_y': {'type': 'number',
                                                                      'label': 'Color Y',
                                                                      'min': 0,
                                                                      'max': 1,
                                                                      'step': 0.01}},
                                      'label_field': 'name',
                                      'summary_fields': ['power', 'level', 'color_temperature', 'feedback']},
                            'zone': {'label': 'Zone',
                                     'label_plural': 'Zones',
                                     'id_format': {'type': 'integer', 'min': 0},
                                     'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                         'targets': {'type': 'string', 'label': 'Members'}},
                                     'label_field': 'name'},
                            'sequence': {'label': 'Sequence',
                                         'label_plural': 'Sequences',
                                         'id_format': {'type': 'integer', 'min': 0},
                                         'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                             'active': {'type': 'boolean',
                                                                        'label': 'Running',
                                                                        'control': True,
                                                                        'cloud_priority': 'high'},
                                                             'enabled': {'type': 'boolean',
                                                                         'label': 'Enabled'},
                                                             'loop': {'type': 'boolean', 'label': 'Loops'},
                                                             'repeat': {'type': 'integer',
                                                                        'label': 'Repeat Count',
                                                                        'min': 0}},
                                         'label_field': 'name',
                                         'summary_fields': ['active', 'enabled']},
                            'scene': {'label': 'Scene',
                                      'label_plural': 'Scenes',
                                      'id_format': {'type': 'integer', 'min': 0, 'max': 15},
                                      'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                          'number': {'type': 'integer',
                                                                     'label': 'DALI Scene Number',
                                                                     'min': 0,
                                                                     'max': 15}},
                                      'label_field': 'name'}},
     'commands': {'light_on': {'label': 'Turn Light On',
                               'help': 'Turns the selected light on.',
                               'params': {'light': {'type': 'child_id',
                                                    'child_type': 'light',
                                                    'label': 'Light',
                                                    'required': True}},
                               'sets': {'power': True}},
                  'light_off': {'label': 'Turn Light Off',
                                'help': 'Turns the selected light off.',
                                'params': {'light': {'type': 'child_id',
                                                     'child_type': 'light',
                                                     'label': 'Light',
                                                     'required': True}},
                                'sets': {'power': False}},
                  'set_light_level': {'label': 'Set Light Brightness',
                                      'help': 'Sets the selected light to the requested brightness '
                                              'percentage. Zero turns it off.',
                                      'params': {'light': {'type': 'child_id',
                                                           'child_type': 'light',
                                                           'label': 'Light',
                                                           'required': True},
                                                 'level': {'type': 'number',
                                                           'label': 'Brightness',
                                                           'min': 0,
                                                           'max': 100,
                                                           'required': True,
                                                           'unit': '%'}},
                                      'sets': {'level': '$level'}},
                  'recall_light_scene': {'label': 'Recall Light Scene',
                                         'help': 'Recalls the scene stored in the selected light. Scene '
                                                 'names are configured in OpenAVC; scene values must '
                                                 'already be stored in the lights.',
                                         'params': {'light': {'type': 'child_id',
                                                              'child_type': 'light',
                                                              'label': 'Light',
                                                              'required': True},
                                                    'scene': {'type': 'child_id',
                                                              'child_type': 'scene',
                                                              'label': 'Scene',
                                                              'required': True}}},
                  'zone_on': {'label': 'Turn Zone On',
                              'help': 'Turns the selected zone on.',
                              'params': {'zone': {'type': 'child_id',
                                                  'child_type': 'zone',
                                                  'label': 'Zone',
                                                  'required': True}}},
                  'zone_off': {'label': 'Turn Zone Off',
                               'help': 'Turns the selected zone off.',
                               'params': {'zone': {'type': 'child_id',
                                                   'child_type': 'zone',
                                                   'label': 'Zone',
                                                   'required': True}}},
                  'set_zone_level': {'label': 'Set Zone Brightness',
                                     'help': 'Sets the selected zone to the requested brightness '
                                             'percentage. Zero turns it off.',
                                     'params': {'zone': {'type': 'child_id',
                                                         'child_type': 'zone',
                                                         'label': 'Zone',
                                                         'required': True},
                                                'level': {'type': 'number',
                                                          'label': 'Brightness',
                                                          'min': 0,
                                                          'max': 100,
                                                          'required': True,
                                                          'unit': '%'}}},
                  'recall_zone_scene': {'label': 'Recall Zone Scene',
                                        'help': 'Recalls the scene stored in the selected zone. Scene names '
                                                'are configured in OpenAVC; scene values must already be '
                                                'stored in the lights.',
                                        'params': {'zone': {'type': 'child_id',
                                                            'child_type': 'zone',
                                                            'label': 'Zone',
                                                            'required': True},
                                                   'scene': {'type': 'child_id',
                                                             'child_type': 'scene',
                                                             'label': 'Scene',
                                                             'required': True}}},
                  'restore_light_level': {'label': 'Restore Last Light Level',
                                          'help': 'Restores the light’s last active brightness.',
                                          'params': {'light': {'type': 'child_id',
                                                               'child_type': 'light',
                                                               'label': 'Light',
                                                               'required': True}}},
                  'set_light_temperature': {'label': 'Set Light Color Temperature',
                                            'help': 'Sets the selected tunable-white light to the requested '
                                                    'Kelvin value. The light may limit this to its physical '
                                                    'range.',
                                            'params': {'light': {'type': 'child_id',
                                                                 'child_type': 'light',
                                                                 'label': 'Light',
                                                                 'required': True},
                                                       'kelvin': {'type': 'number',
                                                                  'label': 'Color Temperature',
                                                                  'min': 100,
                                                                  'max': 20000,
                                                                  'required': True,
                                                                  'unit': 'K'}},
                                            'sets': {'color_temperature': '$kelvin'}},
                  'set_light_rgb': {'label': 'Set Light RGB Color',
                                    'help': 'Sets the selected light’s RGB color components. Available only '
                                            'when the gateway reports that color feature.',
                                    'params': {'light': {'type': 'child_id',
                                                         'child_type': 'light',
                                                         'label': 'Light',
                                                         'required': True},
                                               'red': {'type': 'number',
                                                       'label': 'Red',
                                                       'min': 0,
                                                       'max': 1,
                                                       'required': True},
                                               'green': {'type': 'number',
                                                         'label': 'Green',
                                                         'min': 0,
                                                         'max': 1,
                                                         'required': True},
                                               'blue': {'type': 'number',
                                                        'label': 'Blue',
                                                        'min': 0,
                                                        'max': 1,
                                                        'required': True}}},
                  'set_light_waf': {'label': 'Set Light WAF Color',
                                    'help': 'Sets the selected light’s WAF color components. Available only '
                                            'when the gateway reports that color feature.',
                                    'params': {'light': {'type': 'child_id',
                                                         'child_type': 'light',
                                                         'label': 'Light',
                                                         'required': True},
                                               'white': {'type': 'number',
                                                         'label': 'White',
                                                         'min': 0,
                                                         'max': 1,
                                                         'required': True},
                                               'amber': {'type': 'number',
                                                         'label': 'Amber',
                                                         'min': 0,
                                                         'max': 1,
                                                         'required': True},
                                               'free_color': {'type': 'number',
                                                              'label': 'Free Color',
                                                              'min': 0,
                                                              'max': 1,
                                                              'required': True}}},
                  'set_light_xy': {'label': 'Set Light XY Color',
                                   'help': 'Sets the selected light’s XY color components. Available only '
                                           'when the gateway reports that color feature.',
                                   'params': {'light': {'type': 'child_id',
                                                        'child_type': 'light',
                                                        'label': 'Light',
                                                        'required': True},
                                              'x': {'type': 'number',
                                                    'label': 'X',
                                                    'min': 0,
                                                    'max': 1,
                                                    'required': True},
                                              'y': {'type': 'number',
                                                    'label': 'Y',
                                                    'min': 0,
                                                    'max': 1,
                                                    'required': True}}},
                  'set_light_control': {'label': 'Set Light Control',
                                        'help': 'Changes an available on/off, brightness or '
                                                'color-temperature control on the selected light.',
                                        'params': {'light': {'type': 'child_id',
                                                             'child_type': 'light',
                                                             'label': 'Light',
                                                             'required': True},
                                                   'control': {'type': 'string',
                                                               'label': 'Control',
                                                               'required': True,
                                                               'options_from': {'param': 'light',
                                                                                'source': 'child_schema'}},
                                                   'value': {'type': 'string',
                                                             'label': 'Value',
                                                             'required': True,
                                                             'type_from': {'param': 'control'}}}},
                  'start_sequence': {'label': 'Start Sequence',
                                     'help': 'Starts the selected lighting sequence stored in the gateway.',
                                     'params': {'sequence': {'type': 'child_id',
                                                             'child_type': 'sequence',
                                                             'label': 'Sequence',
                                                             'required': True}},
                                     'sets': {'active': True}},
                  'stop_sequence': {'label': 'Stop Sequence',
                                    'help': 'Stops the selected lighting sequence stored in the gateway.',
                                    'params': {'sequence': {'type': 'child_id',
                                                            'child_type': 'sequence',
                                                            'label': 'Sequence',
                                                            'required': True}},
                                    'sets': {'active': False}},
                  'refresh_status': {'label': 'Refresh Status',
                                     'help': 'Reads the gateway’s current light, zone and sequence lists '
                                             'without addressing the DALI bus.',
                                     'params': {}}},
     'device_settings': {'gateway_name': {'type': 'string',
                                          'label': 'Gateway Name',
                                          'state_key': 'gateway_name',
                                          'default': '',
                                          'setup': False,
                                          'help': 'Changes the name reported by the Lunatone gateway.'}},
     'quick_actions': ['light_on', 'light_off', 'set_light_level', 'recall_light_scene', 'start_sequence'],
     'actions': [{'id': 'scan_addressed',
                  'kind': 'setup',
                  'label': 'Scan Addressed Lights',
                  'availability': 'online',
                  'confirm': 'Register lights already addressed on the DALI bus? Existing addresses and '
                             'scene values are preserved.',
                  'params': {}}],
     'discovery': {'manufacturer_alias': ['Lunatone'],
                   'udp_probe': {'port': 5555,
                                 'send_ascii': 'discovery',
                                 'expect_regex': '"type"\\s*:\\s*"dali-2-iot"',
                                 'timeout_ms': 1200}}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._socket = None
        self._owned_tasks = set()
        self._lock = asyncio.Lock()
        self._rows = {kind: {} for kind in ('light', 'zone', 'sequence')}
        self._buffers = {}
        self._stream_problem = ''
        self._scene_labels = {}
        self._stream_generation = 0

    def _url(self, websocket=False):
        host = str(self.config.get('host', '')).strip()
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        return f'{"ws" if websocket else "http"}://{host}:{int(self.config.get("port", 80))}'

    def _spawn(self, coroutine):
        spawn = getattr(self, 'spawn_task', None)
        task = spawn(coroutine) if spawn else asyncio.create_task(coroutine)
        self._owned_tasks.add(task)
        task.add_done_callback(self._owned_tasks.discard)
        return task

    async def _create_transport(self, transport_type):
        if not str(self.config.get('host', '')).strip():
            raise ConnectionFaultError('No gateway address is entered.', code='invalid_config')
        self._read_scene_labels()
        self._client = httpx.AsyncClient(
            base_url=self._url(), timeout=float(self.config.get('timeout', 5)),
            follow_redirects=False, limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )

    async def _post_connect(self):
        self._invalidate()
        self._apply_info(await self._call('GET', '/info'))

    async def _initial_sync(self):
        await self.refresh_children()
        self._spawn(self._stream_loop())

    def _link_alive(self):
        return self._client is not None

    async def _close_session(self):
        self._stream_generation += 1
        tasks = [t for t in self._owned_tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._owned_tasks.clear()
        client, self._client = self._client, None
        if client:
            await client.aclose()
        self._socket = None
        self._buffers.clear()
        self._invalidate()

    def _invalidate(self):
        self.set_state('live_feedback', False)
        for kind in ('light', 'sequence'):
            for ident in self.list_children(kind):
                values = {key: None for key in self.get_child_schema(kind, ident)
                          if key not in ('name', 'line', 'address', 'label', 'online',
                                         'offline_reason', 'offline_detail', 'feedback')}
                if kind == 'light':
                    values['feedback'] = 'Awaiting gateway status'
                self.set_child_state_batch(kind, ident, values)

    async def _call(self, method, path, body=None):
        if self._client is None:
            raise ConnectionError('The gateway connection is closed.')
        text = f'{method} {path}' + (' ' + json.dumps(body) if body is not None else '')
        self.record_traffic('tx', text, meta={'protocol': 'lunatone_http'})
        try:
            response = await self._client.request(method, path, json=body)
        except httpx.TransportError as exc:
            raise ConnectionError('The Lunatone gateway stopped answering HTTP requests.') from exc
        self.record_traffic('rx', response.content, meta={'http_status': response.status_code, 'path': path})
        return self._decode_reply(response, method, path)

    @staticmethod
    def _decode_reply(response, method, path):
        if response.status_code in (401, 403) and path == '/info':
            raise ConnectionFaultError('The gateway refused local API access.', code='auth_failed')
        if not 200 <= response.status_code < 300:
            raise CommandRefusedError(f'The gateway refused {method} {path} (HTTP {response.status_code}).')
        if not response.content and method != 'GET':
            return {}
        return LunatoneDaliIotDriver._json_reply(response)

    @staticmethod
    def _json_reply(response):
        try:
            result = response.json()
        except ValueError as exc:
            raise CommandRefusedError('The gateway returned an unreadable API reply.') from exc
        if not isinstance(result, dict):
            raise CommandRefusedError('The gateway returned an unexpected API reply.')
        return result

    def _apply_info(self, data):
        descriptor = data.get('descriptor', {})
        device = data.get('device', {})
        if not isinstance(descriptor, dict) or not isinstance(device, dict):
            raise CommandRefusedError('The gateway returned invalid system information.')
        if not isinstance(data.get('version'), str) or not isinstance(data.get('name'), str):
            raise ConnectionFaultError('This address did not return Lunatone IoT system information.', code='invalid_config')
        for key, value in {'gateway_name': data['name'], 'firmware_version': data['version'],
                           'serial_number': str(device.get('serial', '')),
                           'article_number': str(device.get('articleNumber', '')),
                           'line_count': descriptor.get('lines')}.items():
            self.set_state(key, value)
        errors = data.get('errors', {})
        self.set_state('gateway_errors', json.dumps(errors, ensure_ascii=False) if errors else '')

    async def _liveness_probe(self):
        async with self._lock:
            self._apply_info(await self._call('GET', '/info'))

    async def poll(self):
        await self.refresh_children()

    async def refresh_children(self):
        async with self._lock:
            self._apply_info(await self._call('GET', '/info'))
            for kind in ('light', 'zone', 'sequence'):
                await self._read_roster(kind)
            if self.get_state('scan_status') not in (None, '', 'done'):
                self._apply_scan(await self._call('GET', '/dali/scan'))
            self._publish_scenes()
            self.set_state('last_error', self._stream_problem)
        return {kind: len(self.list_children(kind)) for kind in self.DRIVER_INFO['child_entity_types']}

    @staticmethod
    def _resource(kind):
        return {'light': ('/devices', 'devices'), 'zone': ('/zones', 'zones'),
                'sequence': ('/automations/sequences', 'sequences')}[kind]

    async def _read_roster(self, kind):
        path, key = self._resource(kind)
        self._buffers[kind] = []
        applied = False
        try:
            data = await self._call('GET', path)
            replacement = _roster_snapshot(data.get(key), kind)
            # A partial event arriving while GET waits must survive its snapshot.
            for deleted, changes in self._buffers[kind]:
                _patch_rows(replacement, changes, deleted)
            self._rows[kind] = replacement
            self._publish_roster(kind)
            applied = True
        finally:
            buffered = self._buffers.pop(kind)
            # Also retain valid push feedback when the snapshot was refused.
            if not applied:
                for deleted, changes in buffered:
                    self._apply_changes(kind, changes, deleted)

    def _publish_roster(self, kind):
        rows = self._rows[kind]
        for ident in set(self.list_children(kind)) - rows.keys():
            self.deregister_child(kind, ident)
        for ident, row in rows.items():
            if kind == 'light':
                self._publish_light(ident, row)
            else:
                self._publish_named(kind, ident, row)
        self.set_state(kind + '_count', len(rows))

    def _publish_light(self, ident, row):
        features = row.get('features')
        if not isinstance(features, dict):
            return  # A partial addition is not a capability declaration.
        schema = self._light_schema(features)
        self._replace_light_schema(ident, schema)
        initial = {key: None for key in schema}
        self.register_child('light', ident, schema=schema, initial_state=initial)
        values = {'name': str(row.get('name') or f'Light {ident}'),
                  'line': row.get('line'), 'address': row.get('address'),
                  'feedback': 'Reported by gateway'}
        values.update(_feature_values(features, schema))
        self.set_child_state_batch('light', ident, values)

    def _light_schema(self, features):
        declared = self.DRIVER_INFO['child_entity_types']['light']['state_variables']
        props = ['name', 'line', 'address', 'feedback']
        for feature, keys in FEATURE_PROPERTIES.items():
            if isinstance(features.get(feature), dict):
                props.extend(keys)
        return {key: declared[key] for key in props}

    def _replace_light_schema(self, ident, schema):
        if not self.is_child_registered('light', ident):
            return
        existing = self.get_child_schema('light', ident)
        existing = {k: v for k, v in existing.items() if k not in RESERVED}
        if existing != schema:
            self.deregister_child('light', ident)

    def _publish_named(self, kind, ident, row):
        self.register_child(kind, ident)
        values = {'name': str(row.get('name') or f'{kind.title()} {ident}')}
        if kind == 'zone':
            targets = row.get('targets') or []
            values['targets'] = ', '.join(_target_label(t) for t in targets if isinstance(t, dict))
        else:
            for key in ('active', 'enabled', 'loop', 'repeat'):
                values[key] = row.get(key)
        self.set_child_state_batch(kind, ident, values)

    def _read_scene_labels(self):
        labels = {}
        for row in self.config.get('scene_names', []):
            ident, name = _scene_label(row)
            if ident in labels:
                raise ConnectionFaultError('A scene number appears more than once in Scene Names.', code='invalid_config')
            labels[ident] = name
        self._scene_labels = labels

    def _publish_scenes(self):
        self._read_scene_labels()
        for ident in range(16):
            self.register_child('scene', ident)
            self.set_child_state_batch('scene', ident, {
                'number': ident, 'name': self._scene_labels.get(ident, f'Scene {ident}'),
            })

    async def _stream_loop(self):
        delay = 1
        while self._client is not None:
            try:
                await self._receive_stream()
                delay = 1
            except (OSError, WebSocketException, ConnectionError, ValueError) as exc:
                self._stream_problem = 'Live updates disconnected; waiting to reconnect.'
                self.set_state('last_error', self._stream_problem)
                logging.getLogger(__name__).warning('Lighting feedback connection ended: %s', type(exc).__name__)
            finally:
                self._socket = None
                self._stream_generation += 1
                self.set_state('live_feedback', False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _receive_stream(self):
        local = self.local_address()
        async with websocket_connect(self._url(True), local_addr=(local, 0) if local else None,
                                     open_timeout=float(self.config.get('timeout', 5)),
                                     ping_interval=20, ping_timeout=10, max_size=2**20) as socket:
            self._socket = socket
            self._stream_generation += 1
            generation = self._stream_generation
            # Opening before the snapshot closes the gap between GET and subscribing.
            self._spawn(self._resync_stream(generation))
            async for message in socket:
                self.record_traffic('rx', message, meta={'protocol': 'lunatone_websocket'})
                self._handle_event(json.loads(message))

    async def _resync_stream(self, generation):
        try:
            await self.refresh_children()
        except (ConnectionError, CommandRefusedError) as exc:
            self._stream_problem = 'Live updates connected, but refreshing the light list failed.'
            self.set_state('last_error', self._stream_problem)
            if self._socket is not None and generation == self._stream_generation:
                await self._socket.close()
            logging.getLogger(__name__).warning('Lighting refresh failed: %s', type(exc).__name__)
            return
        if self._socket is not None and generation == self._stream_generation:
            self._stream_problem = ''
            self.set_state('last_error', '')
            self.set_state('live_feedback', True)

    def _handle_event(self, envelope):
        if not isinstance(envelope, dict) or not isinstance(envelope.get('data'), dict):
            raise ValueError('Invalid gateway event')
        event, data = envelope.get('type'), envelope['data']
        if event in EVENT_ROSTERS:
            self._roster_event(event, data)
        elif event == 'scanProgress':
            self._apply_scan(data)
        elif event == 'info':
            self._apply_info(data)
        elif event == 'messageFlash':
            self.set_state('last_error', str(data.get('message', '')))
        # Bus monitor frames and undisclosed sensor layouts are not light feedback.

    def _roster_event(self, event, data):
        kind, key, deleted = EVENT_ROSTERS[event]
        changes = data.get(key)
        _validate_changes(changes, deleted)
        if kind in self._buffers:
            self._buffers[kind].append((deleted, deepcopy(changes)))
        else:
            self._apply_changes(kind, changes, deleted)

    def _apply_changes(self, kind, changes, deleted):
        _patch_rows(self._rows[kind], changes, deleted)
        for change in changes:
            if deleted:
                self.deregister_child(kind, change)
            elif kind == 'light':
                self._publish_light(change['id'], self._rows[kind][change['id']])
            else:
                self._publish_named(kind, change['id'], self._rows[kind][change['id']])
        self.set_state(kind + '_count', len(self._rows[kind]))

    def _apply_scan(self, data):
        for key, wire in (('scan_status', 'status'), ('scan_progress', 'progress'), ('scan_found', 'found')):
            if wire in data:
                self.set_state(key, data[wire])

    async def run_setup_action(self, action_id, params=None):
        if action_id != 'scan_addressed':
            raise ValueError('Unknown setup action.')
        async with self._lock:
            data = await self._call('POST', '/dali/scan', {'newInstallation': False, 'noAddressing': True})
            self._apply_scan(data)
        return {'message': 'Scanning already addressed lights. Existing addresses are unchanged.'}

    async def set_device_setting(self, name, value):
        if name != 'gateway_name':
            raise ValueError('Unknown gateway setting.')
        async with self._lock:
            await self._call('PUT', '/info', {'name': str(value)})
            self._apply_info(await self._call('GET', '/info'))

    async def send_command(self, command, params=None):
        params = params or {}
        if command == 'refresh_status':
            return await self.refresh_children()
        async with self._lock:
            if command in ('start_sequence', 'stop_sequence'):
                await self._sequence_command(command, params)
            else:
                await self._light_command(command, params)

    async def _sequence_command(self, command, params):
        ident = _child_number(params['sequence'])
        if ident not in self._rows['sequence']:
            raise CommandRefusedError('That sequence is no longer in the gateway.')
        action = 'start' if command == 'start_sequence' else 'stop'
        await self._call('POST', f'/automations/sequence/{ident}/{action}')
        await self._read_roster('sequence')

    async def _light_command(self, command, params):
        kind = 'zone' if 'zone' in params else 'light'
        ident = _child_number(params[kind])
        row = self._rows[kind].get(ident)
        if row is None:
            raise CommandRefusedError(f'That {kind} is no longer in the gateway.')
        body = _control_body(command, params)
        if kind == 'light':
            features = row.get('features', {})
            if not all(isinstance(features.get(key), dict) or features.get(key) is True for key in body):
                raise CommandRefusedError('The selected light does not report that control feature.')
        resource = 'device' if kind == 'light' else 'zone'
        await self._call('POST', f'/{resource}/{ident}/control', body)
        await self._read_roster('light')


RESERVED = {'online', 'offline_reason', 'offline_detail', 'label'}
FEATURE_PROPERTIES = {'switchable': ('power',), 'dimmable': ('level',),
                      'colorKelvin': ('color_temperature',), 'colorRGB': ('red', 'green', 'blue'),
                      'colorWAF': ('white', 'amber', 'free_color'), 'colorXY': ('color_x', 'color_y')}
EVENT_ROSTERS = {
    'devices': ('light', 'devices', False), 'devicesDeleted': ('light', 'deleted', True),
    'zones': ('zone', 'zones', False), 'zonesDeleted': ('zone', 'deleted', True),
    'sequences': ('sequence', 'sequences', False), 'sequencesDeleted': ('sequence', 'deleted', True),
}


def _identified(row):
    return isinstance(row, dict) and type(row.get('id')) is int and row['id'] >= 0


def _merge(target, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def _patch_rows(rows, changes, deleted):
    for change in changes:
        if deleted:
            rows.pop(change, None)
        else:
            _merge(rows.setdefault(change['id'], {'id': change['id']}), change)


def _feature_values(features, schema):
    result = {}
    for feature, props in FEATURE_PROPERTIES.items():
        result.update(_one_feature(feature, props, features.get(feature), schema))
    return result


def _one_feature(feature, props, entry, schema):
    if not isinstance(entry, dict):
        return {}
    components = {'colorRGB': ('r', 'g', 'b'), 'colorWAF': ('w', 'a', 'f'), 'colorXY': ('x', 'y')}
    status = entry.get('status')
    if feature in components:
        status = status if isinstance(status, dict) else {}
        values = [status.get(k) for k in components[feature]]
    else:
        values = [status]
    return {prop: _valid_reading(value, schema[prop]) for prop, value in zip(props, values) if prop in schema}


def _valid_reading(value, schema):
    if schema['type'] == 'boolean':
        return value if type(value) is bool else None
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value if schema.get('min', value) <= value <= schema.get('max', value) else None


def _target_label(target):
    kind = str(target.get('type', 'target'))
    return kind.title() + (f' {target["id"]}' if 'id' in target else '')


def _child_number(value):
    if isinstance(value, bool):
        raise ValueError('Invalid equipment number.')
    number = int(value)
    if number < 0 or str(value).strip() != str(number):
        raise ValueError('Invalid equipment number.')
    return number


def _control_body(command, params):
    direct = {'light_on': ('switchable', True), 'light_off': ('switchable', False),
              'zone_on': ('switchable', True), 'zone_off': ('switchable', False),
              'restore_light_level': ('gotoLastActive', True)}
    if command in direct:
        key, value = direct[command]
        return {key: value}
    if command == 'set_light_control':
        return _selected_control(params)
    numeric = {'set_light_level': ('dimmable', 'level', 0, 100),
               'set_zone_level': ('dimmable', 'level', 0, 100),
               'set_light_temperature': ('colorKelvin', 'kelvin', 100, 20000),
               'recall_light_scene': ('scene', 'scene', 0, 15),
               'recall_zone_scene': ('scene', 'scene', 0, 15)}
    if command in numeric:
        return _numeric_control(numeric[command], params)
    return _color_control(command, params)


def _numeric_control(spec, params):
    key, param, low, high = spec
    value = _number(params[param], low, high)
    if key == 'scene' and value != int(value):
        raise ValueError('Scene Number must be a whole number.')
    return {key: int(value) if key == 'scene' else value}


def _color_control(command, params):
    color = {'set_light_rgb': ('colorRGB', ('r', 'g', 'b'), ('red', 'green', 'blue')),
             'set_light_waf': ('colorWAF', ('w', 'a', 'f'), ('white', 'amber', 'free_color')),
             'set_light_xy': ('colorXY', ('x', 'y'), ('x', 'y'))}
    if command not in color:
        raise ValueError('Unknown lighting command.')
    key, wire, names = color[command]
    return {key: {k: _number(params[p], 0, 1) for k, p in zip(wire, names)}}


def _roster_snapshot(rows, kind):
    _validate_snapshot(rows, kind)
    replacement = {row['id']: deepcopy(row) for row in rows}
    if len(replacement) != len(rows):
        raise CommandRefusedError(f'The gateway returned duplicate {kind} IDs.')
    return replacement


def _validate_snapshot(rows, kind):
    try:
        _validate_changes(rows, False)
    except ValueError as exc:
        raise CommandRefusedError(f'The gateway returned an invalid {kind} list.') from exc
    if kind != 'light':
        return
    if any(not isinstance(row.get('features'), dict) for row in rows):
        raise CommandRefusedError('The gateway omitted light capabilities from its device list.')


def _validate_changes(changes, deleted):
    if not isinstance(changes, list):
        raise ValueError('Invalid roster event')
    valid = all(type(r) is int for r in changes) if deleted else all(_identified(r) for r in changes)
    if not valid:
        raise ValueError('Invalid roster event')


def _scene_label(row):
    if not isinstance(row, dict):
        raise ConnectionFaultError('A Scene Names row is invalid.', code='invalid_config')
    ident, name = row.get('scene'), str(row.get('name') or '').strip()
    valid = type(ident) is int and 0 <= ident <= 15
    if not valid or not name:
        raise ConnectionFaultError('Scene Names requires scene numbers 0–15 and a name for each row.', code='invalid_config')
    return ident, name


def _number(value, low, high):
    if isinstance(value, bool):
        raise ValueError('Use a number for this lighting value.')
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'The lighting value must be between {low} and {high}.')
    return value


def _selected_control(params):
    control, value = params['control'], params['value']
    if control == 'power':
        return {'switchable': _boolean_control(value)}
    mapping = {'level': ('dimmable', 0, 100), 'color_temperature': ('colorKelvin', 100, 20000)}
    if control not in mapping:
        raise ValueError('Select an available on/off, brightness or color-temperature control.')
    key, low, high = mapping[control]
    return {key: _number(value, low, high)}


def _boolean_control(value):
    # The type_from picker submits true/false text on released Programmers.
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    raise ValueError('On must be Yes or No.')
