"""Pharos Designer 2 LPC / LPC X lighting playback control.

Source: Pharos Designer API v12, HTTP API and WebSocket API (2026-11-10).
Python is needed for device-enumerated sparse child rosters, rotating bearer
tokens, and the authenticated /query WebSocket subscription/keepalive exchange.
These are independent of the platform's declarative WebSocket-push gap.

One HTTP connection and one WebSocket leave room within the LPC's eight-client
limit. HTTP reads refresh the token; WebSocket keepalives use that token every
10 seconds. Nonzero binary keepalive replies are treated only as proof of a
valid session, never decoded into a guessed token representation. A zero reply
forces re-authentication. State comes only from device replies, never writes.

The authenticate endpoint explicitly documents JSON username/password; the
guide's HTML form instead spells it user. This driver uses the endpoint's JSON
definition. The timeline subscription example is copied from scene; its channel
and fields follow the timeline table and channel index. Both need bench checks.
Designer 2 only: no promise about the announced successor or ETC Mosaic.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from contextlib import suppress
from copy import deepcopy
from typing import Any

import httpx
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import WebSocketException

from openavc.drivers.base import BaseDriver, ConnectionFaultError
try:
    from openavc.drivers.base import CommandRefusedError
except ImportError:
    CommandRefusedError = RuntimeError


class PharosResponseError(CommandRefusedError):
    def __init__(self, status, resource):
        self.status = status
        super().__init__(f'The controller refused {resource} (HTTP {status}).')


class PharosDesignerDriver(BaseDriver):
    HEALTH_INTERVAL_S = 30
    HEALTH_TIMEOUT_S = 8
    HEALTH_FAULT_MESSAGE = 'The lighting controller stopped answering status requests.'

    DRIVER_INFO = {'id': 'pharos_designer',
     'name': 'Pharos Designer LPC',
     'manufacturer': 'Pharos',
     'category': 'lighting',
     'version': '1.0.0',
     'author': 'OpenAVC',
     'transport': 'http',
     'description': 'Control Pharos Designer 2 lighting projects on LPC and LPC X controllers. Start '
                    'named scenes and timelines, dim fixture groups, run project triggers, and follow '
                    'live playback and controller status.',
     'source_url': 'https://pharos-controller-api.readthedocs.io/en/latest/',
     'min_platform_version': '0.37.0',
     'ports': [80, 443],
     'protocols': ['pharos_designer_http'],
     'tags': ['architectural-lighting', 'show-control', 'scenes', 'dmx'],
     'simulated': True,
     'verified': False,
     'compatible_models': [{'manufacturer': 'Pharos',
                            'models': ['LPC', 'LPC X', 'LPC X Series 3'],
                            'confidence': 'untested',
                            'notes': 'Designer 2 with project API version 8 or later. Built from '
                                     'Designer API v12 documentation; not tested on hardware. Express '
                                     'and ETC Mosaic are not included.'}],
     'help': {'overview': 'Use the scenes, timelines, fixture groups and triggers programmed in '
                          'Designer. Commands propagate across the Pharos project. Playback state and '
                          'Affecting Output are separate: a running timeline may be hidden by '
                          'higher-priority playback. Group master intensity multiplies programmed '
                          'output; it does not replace the lighting look.',
              'setup': '1. Upload a Designer 2 project to the LPC or LPC X. Select API version 8 or '
                       'later in its project properties (version 12 recommended).\n'
                       '2. Enter the controller management address and web port. For HTTPS, enable Use '
                       'HTTPS and enter the HTTPS port. Enable Verify Certificate only with a '
                       'certificate trusted by this computer.\n'
                       '3. If controller security is enabled, enter an account permitted to read '
                       'status and control playback. Leave both credentials blank only when security '
                       'is disabled.\n'
                       '4. Named controls load automatically. Use Refresh from Device after editing '
                       'the project; the periodic refresh also detects changes. Keep Poll Interval '
                       'above zero to refresh project names and membership.\n'
                       '5. Use Start Scene or Start Timeline to layer playback. Start in Playback '
                       'Group replaces only that group’s playback. Use Release to fade it out.\n'
                       '6. Designer owns fixture programming, networking and user accounts. Open Web '
                       'UI opens the controller’s own pages.'},
     'default_config': {'host': '',
                        'port': 80,
                        'ssl': False,
                        'verify_ssl': False,
                        'username': '',
                        'password': '',
                        'poll_interval': 15,
                        'timeout': 5},
     'config_schema': {'host': {'type': 'string', 'label': 'IP Address', 'required': True},
                       'port': {'type': 'integer',
                                'label': 'Web Port',
                                'default': 80,
                                'min': 1,
                                'max': 65535},
                       'ssl': {'type': 'boolean', 'label': 'Use HTTPS', 'default': False},
                       'verify_ssl': {'type': 'boolean',
                                      'label': 'Verify Certificate',
                                      'default': False,
                                      'advanced': True},
                       'username': {'type': 'string', 'label': 'Username', 'default': ''},
                       'password': {'type': 'string',
                                    'label': 'Password',
                                    'secret': True,
                                    'default': ''},
                       'poll_interval': {'type': 'number',
                                         'label': 'Poll Interval (seconds)',
                                         'default': 15,
                                         'min': 0,
                                         'advanced': True},
                       'timeout': {'type': 'number',
                                   'label': 'Request Timeout (seconds)',
                                   'default': 5,
                                   'min': 1,
                                   'max': 30,
                                   'advanced': True}},
     'state_variables': {'model': {'type': 'string',
                                   'label': 'Controller Type',
                                   'cloud_priority': 'low'},
                         'serial_number': {'type': 'string',
                                           'label': 'Serial Number',
                                           'cloud_priority': 'low'},
                         'firmware_version': {'type': 'string',
                                              'label': 'Firmware Version',
                                              'cloud_priority': 'low'},
                         'project_name': {'type': 'string',
                                          'label': 'Project Name',
                                          'cloud_priority': 'low'},
                         'project_author': {'type': 'string',
                                            'label': 'Project Author',
                                            'cloud_priority': 'low'},
                         'project_uploaded': {'type': 'string',
                                              'label': 'Project Uploaded',
                                              'cloud_priority': 'low'},
                         'last_error': {'type': 'string',
                                        'label': 'Last Error',
                                        'cloud_priority': 'low'},
                         'api_version': {'type': 'integer',
                                         'label': 'Project API Version',
                                         'cloud_priority': 'low'},
                         'channel_capacity': {'type': 'integer',
                                              'label': 'Channel Capacity',
                                              'cloud_priority': 'low'},
                         'live_feedback': {'type': 'boolean',
                                           'label': 'Live Feedback Connected',
                                           'cloud_priority': 'high'},
                         'project_available': {'type': 'boolean',
                                               'label': 'Project Available',
                                               'cloud_priority': 'high'}},
     'child_entity_types': {'timeline': {'label': 'Timeline',
                                         'label_plural': 'Timelines',
                                         'id_format': {'type': 'integer', 'min': 1, 'pad_width': 0},
                                         'label_field': 'name',
                                         'state_variables': {'name': {'type': 'string',
                                                                      'label': 'Name',
                                                                      'cloud_priority': 'low'},
                                                             'state': {'type': 'string',
                                                                       'label': 'Playback State',
                                                                       'cloud_priority': 'high'},
                                                             'onstage': {'type': 'boolean',
                                                                         'label': 'Affecting Output',
                                                                         'cloud_priority': 'high'},
                                                             'group': {'type': 'string',
                                                                       'label': 'Playback Group',
                                                                       'cloud_priority': 'low'},
                                                             'position': {'type': 'number',
                                                                          'label': 'Position',
                                                                          'unit': 's',
                                                                          'min': 0,
                                                                          'cloud_priority': 'low'},
                                                             'length': {'type': 'number',
                                                                        'label': 'Length',
                                                                        'unit': 's',
                                                                        'min': 0,
                                                                        'cloud_priority': 'low'},
                                                             'source_bus': {'type': 'string',
                                                                            'label': 'Time Source',
                                                                            'cloud_priority': 'low'},
                                                             'rate_adjusted': {'type': 'boolean',
                                                                               'label': 'Rate Adjusted',
                                                                               'cloud_priority': 'low'}},
                                         'summary_fields': ['state', 'onstage', 'group']},
                            'scene': {'label': 'Scene',
                                      'label_plural': 'Scenes',
                                      'id_format': {'type': 'integer', 'min': 1, 'pad_width': 0},
                                      'label_field': 'name',
                                      'state_variables': {'name': {'type': 'string',
                                                                   'label': 'Name',
                                                                   'cloud_priority': 'low'},
                                                          'state': {'type': 'string',
                                                                    'label': 'Playback State',
                                                                    'cloud_priority': 'high'},
                                                          'onstage': {'type': 'boolean',
                                                                      'label': 'Affecting Output',
                                                                      'cloud_priority': 'high'},
                                                          'group': {'type': 'string',
                                                                    'label': 'Playback Group',
                                                                    'cloud_priority': 'low'}},
                                      'summary_fields': ['state', 'onstage', 'group']},
                            'group': {'label': 'Fixture Group',
                                      'label_plural': 'Fixture Groups',
                                      'id_format': {'type': 'integer', 'min': 0, 'pad_width': 0},
                                      'label_field': 'name',
                                      'state_variables': {'name': {'type': 'string',
                                                                   'label': 'Name',
                                                                   'cloud_priority': 'low'},
                                                          'level': {'type': 'integer',
                                                                    'label': 'Master Intensity',
                                                                    'unit': '%',
                                                                    'min': 0,
                                                                    'max': 100,
                                                                    'control': True,
                                                                    'cloud_priority': 'high'}},
                                      'summary_fields': ['level']},
                            'playback_group': {'label': 'Playback Group',
                                               'label_plural': 'Playback Groups',
                                               'id_format': {'type': 'integer',
                                                             'min': 1,
                                                             'pad_width': 0},
                                               'label_field': 'name',
                                               'state_variables': {'name': {'type': 'string',
                                                                            'label': 'Name',
                                                                            'cloud_priority': 'low'}},
                                               'summary_fields': []},
                            'trigger': {'label': 'Trigger',
                                        'label_plural': 'Triggers',
                                        'id_format': {'type': 'integer', 'min': 1, 'pad_width': 0},
                                        'label_field': 'name',
                                        'state_variables': {'name': {'type': 'string',
                                                                     'label': 'Name',
                                                                     'cloud_priority': 'low'},
                                                            'type': {'type': 'string',
                                                                     'label': 'Trigger Type',
                                                                     'cloud_priority': 'low'},
                                                            'description': {'type': 'string',
                                                                            'label': 'Description',
                                                                            'cloud_priority': 'low'},
                                                            'conditions': {'type': 'string',
                                                                           'label': 'Conditions',
                                                                           'cloud_priority': 'low'},
                                                            'actions': {'type': 'string',
                                                                        'label': 'Actions',
                                                                        'cloud_priority': 'low'}},
                                        'summary_fields': ['type', 'description']},
                            'controller': {'label': 'Controller',
                                           'label_plural': 'Controllers',
                                           'id_format': {'type': 'integer', 'min': 1, 'pad_width': 0},
                                           'label_field': 'name',
                                           'state_variables': {'name': {'type': 'string',
                                                                        'label': 'Name',
                                                                        'cloud_priority': 'low'},
                                                               'type': {'type': 'string',
                                                                        'label': 'Controller Type',
                                                                        'cloud_priority': 'low'},
                                                               'serial': {'type': 'string',
                                                                          'label': 'Serial Number',
                                                                          'cloud_priority': 'low'},
                                                               'ip_address': {'type': 'string',
                                                                              'label': 'IP Address',
                                                                              'cloud_priority': 'low'},
                                                               'is_network_primary': {'type': 'boolean',
                                                                                      'label': 'Network '
                                                                                               'Primary',
                                                                                      'cloud_priority': 'high'}},
                                           'summary_fields': ['type', 'online', 'is_network_primary']}},
     'commands': {'refresh': {'label': 'Refresh Project',
                              'help': 'Read the current project and update its named controls and '
                                      'playback status.',
                              'params': {}},
                  'start_scene': {'label': 'Start Scene',
                                  'sets': {'state': 'started'},
                                  'help': 'Start the selected scene on all controllers in this Pharos '
                                          'project.',
                                  'params': {'scene': {'type': 'child_id',
                                                       'child_type': 'scene',
                                                       'required': True,
                                                       'label': 'Scene'}}},
                  'start_exclusive_scene': {'label': 'Start Scene in Playback Group',
                                            'help': 'Start the selected scene and release other scenes '
                                                    'in its playback group. Other playback groups keep '
                                                    'running.',
                                            'params': {'scene': {'type': 'child_id',
                                                                 'child_type': 'scene',
                                                                 'required': True,
                                                                 'label': 'Scene'},
                                                       'fade': {'type': 'number',
                                                                'label': 'Fade (seconds)',
                                                                'min': 0,
                                                                'help': 'Leave blank to use the fade '
                                                                        'programmed in Designer.'}}},
                  'release_scene': {'label': 'Release Scene',
                                    'help': 'Release the selected scene on all controllers in this '
                                            'Pharos project.',
                                    'params': {'scene': {'type': 'child_id',
                                                         'child_type': 'scene',
                                                         'required': True,
                                                         'label': 'Scene'},
                                               'fade': {'type': 'number',
                                                        'label': 'Fade (seconds)',
                                                        'min': 0,
                                                        'help': 'Leave blank to use the fade '
                                                                'programmed in Designer.'}}},
                  'toggle_scene': {'label': 'Toggle Scene',
                                   'help': 'Toggle the selected scene on all controllers in this '
                                           'Pharos project.',
                                   'params': {'scene': {'type': 'child_id',
                                                        'child_type': 'scene',
                                                        'required': True,
                                                        'label': 'Scene'},
                                              'fade': {'type': 'number',
                                                       'label': 'Fade (seconds)',
                                                       'min': 0,
                                                       'help': 'Leave blank to use the fade programmed '
                                                               'in Designer.'}}},
                  'release_scene_group': {'label': 'Release Scene Playback Group',
                                          'help': 'Release every scene in the selected playback group.',
                                          'params': {'playback_group': {'type': 'child_id',
                                                                        'child_type': 'playback_group',
                                                                        'required': True,
                                                                        'label': 'Playback Group'},
                                                     'fade': {'type': 'number',
                                                              'label': 'Fade (seconds)',
                                                              'min': 0,
                                                              'help': 'Leave blank to use the fade '
                                                                      'programmed in Designer.'}}},
                  'start_timeline': {'label': 'Start Timeline',
                                     'sets': {'state': 'running'},
                                     'help': 'Start the selected timeline on all controllers in this '
                                             'Pharos project.',
                                     'params': {'timeline': {'type': 'child_id',
                                                             'child_type': 'timeline',
                                                             'required': True,
                                                             'label': 'Timeline'}}},
                  'start_exclusive_timeline': {'label': 'Start Timeline in Playback Group',
                                               'help': 'Start the selected timeline and release other '
                                                       'timelines in its playback group. Other '
                                                       'playback groups keep running.',
                                               'params': {'timeline': {'type': 'child_id',
                                                                       'child_type': 'timeline',
                                                                       'required': True,
                                                                       'label': 'Timeline'},
                                                          'fade': {'type': 'number',
                                                                   'label': 'Fade (seconds)',
                                                                   'min': 0,
                                                                   'help': 'Leave blank to use the '
                                                                           'fade programmed in '
                                                                           'Designer.'}}},
                  'release_timeline': {'label': 'Release Timeline',
                                       'help': 'Release the selected timeline on all controllers in '
                                               'this Pharos project.',
                                       'params': {'timeline': {'type': 'child_id',
                                                               'child_type': 'timeline',
                                                               'required': True,
                                                               'label': 'Timeline'},
                                                  'fade': {'type': 'number',
                                                           'label': 'Fade (seconds)',
                                                           'min': 0,
                                                           'help': 'Leave blank to use the fade '
                                                                   'programmed in Designer.'}}},
                  'toggle_timeline': {'label': 'Toggle Timeline',
                                      'help': 'Toggle the selected timeline on all controllers in this '
                                              'Pharos project.',
                                      'params': {'timeline': {'type': 'child_id',
                                                              'child_type': 'timeline',
                                                              'required': True,
                                                              'label': 'Timeline'},
                                                 'fade': {'type': 'number',
                                                          'label': 'Fade (seconds)',
                                                          'min': 0,
                                                          'help': 'Leave blank to use the fade '
                                                                  'programmed in Designer.'}}},
                  'pause_timeline': {'label': 'Pause Timeline',
                                     'sets': {'state': 'paused'},
                                     'help': 'Pause the selected timeline on all controllers in this '
                                             'Pharos project.',
                                     'params': {'timeline': {'type': 'child_id',
                                                             'child_type': 'timeline',
                                                             'required': True,
                                                             'label': 'Timeline'}}},
                  'resume_timeline': {'label': 'Resume Timeline',
                                      'sets': {'state': 'running'},
                                      'help': 'Resume the selected timeline on all controllers in this '
                                              'Pharos project.',
                                      'params': {'timeline': {'type': 'child_id',
                                                              'child_type': 'timeline',
                                                              'required': True,
                                                              'label': 'Timeline'}}},
                  'release_timeline_group': {'label': 'Release Timeline Playback Group',
                                             'help': 'Release every timeline in the selected playback '
                                                     'group.',
                                             'params': {'playback_group': {'type': 'child_id',
                                                                           'child_type': 'playback_group',
                                                                           'required': True,
                                                                           'label': 'Playback Group'},
                                                        'fade': {'type': 'number',
                                                                 'label': 'Fade (seconds)',
                                                                 'min': 0,
                                                                 'help': 'Leave blank to use the fade '
                                                                         'programmed in Designer.'}}},
                  'set_group_level': {'label': 'Set Group Master Intensity',
                                      'help': 'Fade the selected fixture group to a percentage of its '
                                              'programmed output. Zero darkens the group; 100 restores '
                                              'its programmed level.',
                                      'params': {'group': {'type': 'child_id',
                                                           'child_type': 'group',
                                                           'required': True,
                                                           'label': 'Fixture Group'},
                                                 'level': {'type': 'integer',
                                                           'required': True,
                                                           'label': 'Master Intensity (%)',
                                                           'min': 0,
                                                           'max': 100},
                                                 'fade': {'type': 'number',
                                                          'label': 'Fade (seconds)',
                                                          'min': 0,
                                                          'help': 'Leave blank for the controller '
                                                                  'default.'}},
                                      'sets': {'level': '{level}'}},
                  'set_timeline_rate': {'label': 'Set Timeline Rate',
                                        'help': 'Change the timeline playback speed. A rate of 1 uses '
                                                'its default speed.',
                                        'params': {'timeline': {'type': 'child_id',
                                                                'child_type': 'timeline',
                                                                'required': True,
                                                                'label': 'Timeline'},
                                                   'rate': {'type': 'number',
                                                            'label': 'Playback Rate',
                                                            'required': True}}},
                  'set_timeline_position': {'label': 'Set Timeline Position',
                                            'help': 'Move playback to a time within the selected '
                                                    'timeline.',
                                            'params': {'timeline': {'type': 'child_id',
                                                                    'child_type': 'timeline',
                                                                    'required': True,
                                                                    'label': 'Timeline'},
                                                       'seconds': {'type': 'number',
                                                                   'label': 'Position (seconds)',
                                                                   'required': True,
                                                                   'min': 0}}},
                  'seek_timeline_flag': {'label': 'Go to Timeline Flag',
                                         'help': 'Move playback to a flag named in the selected '
                                                 'Designer timeline.',
                                         'params': {'timeline': {'type': 'child_id',
                                                                 'child_type': 'timeline',
                                                                 'required': True,
                                                                 'label': 'Timeline'},
                                                    'flag': {'type': 'string',
                                                             'label': 'Flag Name',
                                                             'required': True}}},
                  'fire_trigger': {'label': 'Fire Project Trigger',
                                   'help': 'Run the selected trigger. By default its conditions must '
                                           'be met; its actions are programmed in Designer.',
                                   'params': {'trigger': {'type': 'child_id',
                                                          'child_type': 'trigger',
                                                          'required': True,
                                                          'label': 'Trigger'},
                                              'conditions': {'type': 'boolean',
                                                             'label': 'Check Conditions',
                                                             'default': True},
                                              'variables': {'type': 'string',
                                                            'label': 'Variables',
                                                            'help': 'Optional comma-separated values '
                                                                    'in the format expected by the '
                                                                    'Designer trigger.'}}},
                  'identify_on': {'label': 'Identify Controller',
                                  'help': 'Flash this controller’s indicators to identify it.',
                                  'params': {}},
                  'identify_off': {'label': 'Stop Identifying Controller',
                                   'help': 'Stop flashing this controller’s indicators.',
                                   'params': {}},
                  'release_all': {'label': 'Release All Playback',
                                  'help': 'Release every timeline and scene across this Pharos '
                                          'project. Fixture overrides and parked channels are '
                                          'unaffected.',
                                  'params': {'fade': {'type': 'number',
                                                      'label': 'Fade (seconds)',
                                                      'min': 0,
                                                      'help': 'Leave blank to use the fade programmed '
                                                              'in Designer.'}},
                                  'confirm': 'Release all timelines and scenes across the Pharos '
                                             'project?'}},
     'quick_actions': ['start_scene',
                       'start_timeline',
                       'pause_timeline',
                       'resume_timeline',
                       'set_group_level'],
     'discovery': {'hostname': ['^lpc-'], 'manufacturer_alias': ['Pharos', 'Pharos Architectural Controls']}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._token = ''
        self._operation_lock = asyncio.Lock()
        self._stream_task = None
        self._stream_fault = None
        self._project_error = ''
        self._project_stamp = None
        self._poll_count = 0
        self._api_version = 0
        self._roster_dirty = False
        self._push_versions = {}

    def _base_url(self, *, websocket=False):
        scheme = ('wss' if websocket else 'https') if self.config.get('ssl') else ('ws' if websocket else 'http')
        host = str(self.config.get('host', '')).strip()
        if ':' in host and not host.startswith('['):
            host = f'[{host}]'
        return f'{scheme}://{host}:{int(self.config.get("port", 80))}'

    async def _create_transport(self, transport_type):
        if not str(self.config.get('host', '')).strip():
            raise ConnectionFaultError('No lighting controller address is entered.', code='invalid_config')
        self._client = httpx.AsyncClient(
            base_url=self._base_url(), timeout=float(self.config.get('timeout', 5)),
            verify=bool(self.config.get('verify_ssl', False)), follow_redirects=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    async def _post_connect(self):
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        self._token = ''
        self._stream_fault = None
        if self.config.get('username'):
            await self._login()
        identity = await self._call('GET', 'system')
        self._check_identity(identity)
        self._apply_identity(identity)
        await self._read_api_version()

    @staticmethod
    def _check_identity(identity):
        model = str(identity.get('hardware_type', ''))
        if model not in ('LPC', 'LPC X'):
            raise ConnectionFaultError('This driver requires a Pharos Designer LPC or LPC X.', code='invalid_config')
        if not str(identity.get('firmware_version', '')).startswith('2.'):
            raise ConnectionFaultError('This driver requires Pharos Designer 2 firmware.', code='invalid_config')

    async def _read_api_version(self):
        version = await self._call('GET', 'api_version')
        self._api_version = int(version.get('version', 0))
        if self._api_version < 8:
            raise ConnectionFaultError('The Designer project must use API version 8 or later.', code='invalid_config')
        self.set_state('api_version', self._api_version)
        self.DRIVER_INFO = deepcopy(type(self).DRIVER_INFO)
        if self._api_version < 10:
            for command in ('release_scene_group', 'release_timeline_group'):
                self.DRIVER_INFO['commands'].pop(command)
        if self._api_version < 9:
            self.DRIVER_INFO['commands'].pop('set_timeline_position')

    async def _initial_sync(self):
        await self.refresh_children()
        self._stream_task = asyncio.create_task(self._stream_loop())

    def _link_alive(self):
        return self._client is not None

    async def _close_session(self):
        task, self._stream_task = self._stream_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()
        self._token = ''
        self.set_state('live_feedback', False)

    async def _raw_request(self, method, path, **kwargs):
        if self._client is None:
            raise ConnectionError('The lighting controller session is closed.')
        headers = {'Authorization': f'Bearer {self._token}'} if self._token else {}
        try:
            return await self._client.request(method, path, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise ConnectionError('The lighting controller stopped answering HTTP requests.') from exc

    def _decode(self, response, resource):
        if not 200 <= response.status_code < 300:
            raise PharosResponseError(response.status_code, resource)
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise CommandRefusedError('The lighting controller returned an unreadable reply.') from exc
        if not isinstance(data, dict):
            raise CommandRefusedError('The lighting controller returned an unexpected reply.')
        self._save_token(data.get('token'))
        return data

    def _save_token(self, token):
        if isinstance(token, str) and token:
            self.redact_in_log(token)
            self._token = token

    async def _login(self):
        if not self.config.get('username'):
            raise ConnectionFaultError('The lighting controller requires an account login.', code='auth_failed')
        # The endpoint's JSON table names username; do not copy the guide's HTML field.
        response = await self._raw_request('POST', '/authenticate', json={
            'username': self.config['username'], 'password': self.config.get('password', ''),
        })
        if response.status_code in (301, 302, 303, 307, 308, 400, 401, 403):
            raise ConnectionFaultError('The lighting controller refused the account login.', code='auth_failed')
        data = self._decode(response, 'account login')
        if not isinstance(data.get('token'), str) or not data['token']:
            raise ConnectionFaultError('The lighting controller did not return an account token.', code='auth_failed')

    async def _call(self, method, resource, body=None, *, params=None):
        response = await self._raw_request(method, '/api/' + resource, json=body, params=params)
        # Do not follow login redirects into an HTML success page. One renewal only.
        if self._needs_login(response):
            await self._login()
            response = await self._raw_request(method, '/api/' + resource, json=body, params=params)
            if response.status_code in (301, 302, 303, 307, 308, 401):
                raise ConnectionFaultError('The lighting controller refused the renewed session.', code='auth_failed')
        if response.status_code == 403:
            self._permission_refused(method)
        return self._decode(response, resource.replace('_', ' '))

    def _needs_login(self, response):
        if response.status_code in (301, 302, 303, 307, 308, 401):
            return True
        return response.status_code == 403 and not self._token

    @staticmethod
    def _permission_refused(method):
        if method == 'GET':
            raise ConnectionFaultError('The Pharos account cannot read the required project status.', code='auth_failed')
        raise CommandRefusedError('The Pharos account does not have permission to control playback.')

    def _apply_identity(self, data):
        for key, wire in (('model', 'hardware_type'), ('serial_number', 'serial_number'),
                          ('firmware_version', 'firmware_version'), ('channel_capacity', 'channel_capacity')):
            if wire in data:
                self.set_state(key, data[wire])

    async def _liveness_probe(self):
        if self._stream_fault is not None:
            raise self._stream_fault
        async with self._operation_lock:
            # Any HTTP reply proves a live controller. No login from this probe.
            response = await self._raw_request('GET', '/api/system')
            if response.status_code == 200:
                with suppress(CommandRefusedError):
                    self._decode(response, 'system')

    async def refresh_children(self):
        async with self._operation_lock:
            await self._read_project()
        return {kind: len(self.list_children(kind)) for kind in self.DRIVER_INFO['child_entity_types']}

    async def _read_project(self):
        try:
            project = await self._call('GET', 'project')
        except PharosResponseError as exc:
            if exc.status != 404:
                raise
            self._clear_project()
            return
        stamp = (project.get('unique_id'), project.get('upload_date'))
        if stamp != self._project_stamp:
            self._clear_children()
            await self._read_api_version()
        self._project_stamp = stamp
        self._project_error = ''
        self.set_state('project_available', True)
        for key, wire in (('project_name', 'name'), ('project_author', 'author'), ('project_uploaded', 'upload_date')):
            self.set_state(key, str(project.get(wire, '')))
        await self._read_playback()
        for kind in ('trigger', 'controller'):
            await self._read_roster(kind)
        if self._api_version >= 10:
            await self._read_roster('playback_group')
        self._roster_dirty = False
        self.set_state('last_error', '')

    def _clear_children(self):
        for kind in self.DRIVER_INFO['child_entity_types']:
            for num in self.list_children(kind):
                self.deregister_child(kind, num)
        self._push_versions.clear()

    def _clear_project(self):
        self._clear_children()
        self._project_stamp = None
        self._project_error = 'The controller is answering, but its project is unavailable. Upload a Designer project.'
        self.set_state('project_available', False)
        for key in ('project_name', 'project_author', 'project_uploaded'):
            self.set_state(key, '')
        self.set_state('last_error', self._project_error)

    async def _read_playback(self):
        for kind in ('timeline', 'scene', 'group'):
            await self._read_roster(kind)

    async def _read_roster(self, kind):
        versions = self._push_versions.copy()
        data = await self._call('GET', kind)
        rows = data.get(kind + 's')
        if not isinstance(rows, list):
            raise CommandRefusedError(f'The controller did not return its {kind.replace("_", " ")} list.')
        if kind == 'group':
            rows = await self._include_all_fixtures(rows)
        self._reconcile_roster(kind, rows, versions)

    async def _include_all_fixtures(self, rows):
        # Built-in groups omit num. Ask specifically for group 0 before assigning it.
        all_group = await self._call('GET', 'group', params={'num': 0})
        numbered = [r for r in rows if self._numbered_row(r) and r['num'] != 0]
        return numbered + [dict(r, num=0) for r in all_group.get('groups', [])]

    @staticmethod
    def _numbered_row(row):
        return isinstance(row, dict) and type(row.get('num')) is int

    def _reconcile_roster(self, kind, rows, versions):
        seen = set()
        for row in filter(self._numbered_row, rows):
            num = row['num']
            seen.add(num)
            self.register_child(kind, num)
            self._apply_polled_child(kind, row, versions)
        for num in set(self.list_children(kind)) - seen:
            self.deregister_child(kind, num)

    def _apply_polled_child(self, kind, row, versions):
        num = row['num']
        values = self._child_values(kind, row)
        # A newer push received while a GET waited wins for the fields it reports.
        if versions.get((kind, num), 0) != self._push_versions.get((kind, num), 0):
            for key in ('state', 'onstage', 'position', 'level'):
                values.pop(key, None)
        self.set_child_state_batch(kind, num, values)

    def _child_values(self, kind, row):
        schema = self.DRIVER_INFO['child_entity_types'][kind]['state_variables']
        values = {key: row[key] for key in schema if key in row}
        if 'name' in row:
            values['label'] = str(row['name'])
        transform = {'timeline': self._timeline_values, 'trigger': self._trigger_values,
                     'controller': self._controller_values}.get(kind)
        if transform is not None:
            transform(values, row)
        return values

    @staticmethod
    def _timeline_values(values, row):
        for key in ('position', 'length'):
            if key in values:
                values[key] = float(values[key]) / 1000

    @staticmethod
    def _trigger_values(values, row):
        for key in ('conditions', 'actions'):
            items = row.get(key, [])
            values[key] = '; '.join(str(item.get('text', '')) for item in items if isinstance(item, dict))

    @staticmethod
    def _controller_values(values, row):
        if 'online' in row:
            values['online'] = bool(row['online'])
            values['offline_reason'] = '' if row['online'] else 'not_responding'

    async def poll(self):
        if self._stream_fault is not None:
            raise self._stream_fault
        async with self._operation_lock:
            self._apply_identity(await self._call('GET', 'system'))
            self._poll_count += 1
            if self._needs_project_refresh():
                await self._read_project()
            else:
                await self._read_playback()
            self._report_status()

    def _needs_project_refresh(self):
        return any((self._poll_count % 2 == 0, self._roster_dirty, self._project_error))

    def _report_status(self):
        message = self._project_error
        if not message and not self.get_state('live_feedback'):
            message = 'Live feedback is reconnecting. Status is being polled.'
        self.set_state('last_error', message)

    async def _stream_loop(self):
        while self._client is not None:
            try:
                await self._serve_stream()
            except asyncio.CancelledError:
                raise
            except ConnectionFaultError as exc:
                self._stream_fault = exc
                return
            except (ConnectionError, OSError, TimeoutError, WebSocketException, CommandRefusedError):
                self.set_state('live_feedback', False)
                self._report_status()
                await asyncio.sleep(5)

    async def _serve_stream(self):
        address = self.local_address()
        kwargs = {'local_addr': (address, 0) if address else None, 'open_timeout': 5,
                  'ping_interval': None, 'close_timeout': 2, 'proxy': None}
        if self.config.get('ssl'):
            kwargs['ssl'] = ssl.create_default_context() if self.config.get('verify_ssl') else ssl._create_unverified_context()
        async with websocket_connect(self._base_url(websocket=True) + '/query', **kwargs) as socket:
            async with self._operation_lock:
                await self._call('GET', 'system')
                for channel in ('timeline', 'scene', 'group'):
                    await self._send_stream(socket, subscribe=channel)
            heartbeat = asyncio.create_task(self._stream_keepalive(socket))
            try:
                await self._receive_stream(socket, heartbeat)
            finally:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
                self.set_state('live_feedback', False)

    def _stream_message(self, **payload):
        if self._token:
            payload['token'] = self._token
        return payload

    async def _send_stream(self, socket, **payload):
        message = json.dumps(self._stream_message(**payload))
        await socket.send(message)
        self.record_traffic('tx', message, meta={'protocol': 'pharos_websocket'})

    async def _stream_keepalive(self, socket):
        while True:
            async with self._operation_lock:
                # HTTP supplies the documented text JWT, avoiding binary-token assumptions.
                await self._call('GET', 'system')
                await self._send_stream(socket)
            await asyncio.sleep(10)

    async def _receive_stream(self, socket, heartbeat):
        while True:
            if heartbeat.done():
                await heartbeat
            message = await asyncio.wait_for(socket.recv(), 25)
            await self._stream_message_received(message)

    async def _stream_message_received(self, message):
        if isinstance(message, bytes):
            # Binary replies may contain a session key. Never put those keys in an audit.
            return await self._stream_session_reply(message)
        self.record_traffic('rx', message, meta={'protocol': 'pharos_websocket'})
        try:
            envelope = json.loads(message)
        except (ValueError, TypeError):
            return
        if isinstance(envelope, dict):
            self._feedback_alive()
            try:
                self._apply_broadcast(envelope)
            except (ValueError, TypeError):
                self.set_state('last_error', 'The controller sent an unreadable playback update.')

    async def _stream_session_reply(self, message):
        if message and not any(message):
            async with self._operation_lock:
                await self._login()
            raise ConnectionError('The Pharos feedback session expired.')
        self._feedback_alive()

    def _feedback_alive(self):
        if not self.get_state('live_feedback'):
            self.set_state('live_feedback', True)
            self._report_status()

    def _apply_broadcast(self, envelope):
        kind = envelope.get('broadcast')
        row = envelope.get('data')
        if kind not in ('timeline', 'scene', 'group') or not isinstance(row, dict):
            return
        num = row.get('num')
        if type(num) is not int:
            return
        if num not in self.list_children(kind):
            self._roster_dirty = True
            return
        self._push_versions[kind, num] = self._push_versions.get((kind, num), 0) + 1
        self.set_child_state_batch(kind, num, self._child_values(kind, row))

    def _selected_number(self, kind, params):
        num = int(params[kind])
        if num not in self.list_children(kind):
            raise CommandRefusedError(f'The selected {kind.replace("_", " ")} is not in the current project. Refresh the project.')
        return num

    def _playback_body(self, command, params):
        action, _, kind = command.rpartition('_')
        body = {'action': 'start_release_others' if action == 'start_exclusive' else action,
                'num': self._selected_number(kind, params)}
        if action == 'start_exclusive':
            body['same_group'] = True
        if params.get('fade') is not None:
            body['fade'] = float(params['fade'])
        return kind, body

    async def send_command(self, command: str, params: dict[str, Any] | None = None):
        params = params or {}
        if command == 'refresh':
            return await self.refresh_children()
        async with self._operation_lock:
            await self._execute(command, params)

    async def _execute(self, command, params):
        handlers = {
            'set_group_level': self._set_group_level,
            'set_timeline_rate': self._set_timeline_value,
            'set_timeline_position': self._set_timeline_value,
            'seek_timeline_flag': self._set_timeline_value,
            'fire_trigger': self._fire_trigger,
            'identify_on': self._identify,
            'identify_off': self._identify,
            'release_all': self._release_all,
            'release_scene_group': self._release_group,
            'release_timeline_group': self._release_group,
        }
        if command in handlers:
            return await handlers[command](command, params)
        if command not in self.DRIVER_INFO['commands']:
            raise ValueError('Unknown lighting command.')
        kind, body = self._playback_body(command, params)
        await self._call('POST', kind, body)
        await self._read_roster(kind)

    async def _set_group_level(self, command, params):
        body = {'action': 'master_intensity', 'num': self._selected_number('group', params),
                'level': f'{int(params["level"])}:100'}
        if params.get('fade') is not None:
            body['fade'] = float(params['fade'])
        await self._call('POST', 'group', body)
        await self._read_roster('group')

    async def _set_timeline_value(self, command, params):
        action, key, value = {
            'set_timeline_rate': ('set_rate', 'rate', str(params.get('rate', ''))),
            'set_timeline_position': ('set_position', 'time', params.get('seconds')),
            'seek_timeline_flag': ('set_position', 'flag', params.get('flag')),
        }[command]
        body = {'action': action, 'num': self._selected_number('timeline', params), key: value}
        await self._call('POST', 'timeline', body)
        await self._read_roster('timeline')

    async def _fire_trigger(self, command, params):
        body = {'num': self._selected_number('trigger', params), 'conditions': params.get('conditions', True)}
        if params.get('variables'):
            body['var'] = params['variables']
        await self._call('POST', 'trigger', body)
        await self._read_playback()

    async def _identify(self, command, params):
        await self._call('PUT', 'beacon', {'enable': command == 'identify_on'})

    async def _release_all(self, command, params):
        body = {'action': 'release'}
        if params.get('fade') is not None:
            body['fade'] = float(params['fade'])
        for kind in ('timeline', 'scene'):
            await self._call('POST', kind, body)
            await self._read_roster(kind)

    async def _release_group(self, command, params):
        body = {'action': 'release', 'group': self._selected_number('playback_group', params)}
        if params.get('fade') is not None:
            body['fade'] = float(params['fade'])
        kind = command.split('_')[1]
        await self._call('POST', kind, body)
        await self._read_roster(kind)
