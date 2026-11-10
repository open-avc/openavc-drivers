"""Yamaha DME7/DME5/DME3 Remote Control Protocol V3.1.0.

Python models the processor's discovered parameter dimensions, types and ranges,
correlates replies separately from NOTIFY, and renews optional meter streams.
The standard TCP transport handles the connection. Changes are pushed on that
connection; status polls and a bounded parameter sweep provide reconciliation.
Snapshots and ProVisionaire synchronization cause an immediate parameter reread.

Source: Yamaha DME7/DME5/DME3 Remote Control Protocol Specifications V3.1.0,
sections 3-5. The parameter set must first be exported from ProVisionaire Design.
The simulator is document-derived; device behavior has not been measured.
"""
from __future__ import annotations

import asyncio
import copy
import math
import shlex
from dataclasses import dataclass

from openavc.drivers.base import BaseDriver, CommandRefusedError, ConnectionFaultError


class DeviceRefused(CommandRefusedError):
    def __init__(self, command, reason):
        self.reason = reason
        super().__init__(f"The processor refused {command}: {reason}.")


def quoted(value):
    text = str(value)
    if any(ord(c) < 32 or ord(c) > 126 for c in text):
        raise ValueError("The Yamaha control protocol accepts printable ASCII text only.")
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def meter_value(code):
    """Section 5.1: 0x71 = -13 dBFS; bit 7 is the clipping flag."""
    value = int(code, 16)
    level = value & 127
    return (None if level == 127 else level - 126), bool(value & 128)


@dataclass(frozen=True)
class Parameter:
    index: int
    name: str
    nx: int
    ny: int
    minimum: int
    maximum: int
    unit: str
    kind: str
    ui: str
    writable: bool
    scaling: int
    scale_reported: bool = True

    @classmethod
    def from_reply(cls, tokens):
        # The page 29 example predates the scaling column on page 28.
        return cls(int(tokens[2]), tokens[3], int(tokens[4]), int(tokens[5]),
                   int(tokens[6]), int(tokens[7]), tokens[9], tokens[10], tokens[11],
                   tokens[12] == 'rw', int(tokens[13]) if len(tokens) > 13 else 1,
                   len(tokens) > 13)

    @property
    def is_switch(self):
        return self.ui in {'latchsw', 'unlatchsw'} and (self.minimum, self.maximum) == (0, 1)

    @property
    def has_position(self):
        return self.kind in {'integer', 'freq'} and not self.is_switch

    def value_schema(self):
        if self.is_switch:
            return {'type': 'boolean', 'label': 'Value', 'control': self.writable}
        if self.kind in {'string', 'binary'}:
            return {'type': 'string', 'label': 'Value', 'control': self.writable}
        lo, hi = sorted((self.minimum, self.maximum))
        label = 'Value' if self.scale_reported else 'Raw Value'
        unit = self.unit if self.scale_reported else ''
        return {'type': 'number', 'label': label, 'min': lo / self.scaling,
                'max': hi / self.scaling, 'step': 1 / self.scaling,
                'unit': unit, 'control': self.writable}

    def decode(self, raw):
        if self.kind in {'string', 'binary'}:
            return raw
        number = int(raw)
        if self.is_switch:
            return bool(number)
        # Section 3.1: this fader code is minus infinity, not -138.01 dB.
        if self.unit.lower() == 'db' and number == -13801:
            return None
        return number / self.scaling

    def encode(self, value):
        if self.kind == 'string':
            return self._encode_text(value)
        if self.kind == 'binary':
            return self._encode_binary(value)
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Use Set Display Value with -inf for a silent fader.')
        raw = round(number * self.scaling)
        if not min(self.minimum, self.maximum) <= raw <= max(self.minimum, self.maximum):
            raise ValueError('The value is outside the range reported by the processor.')
        return str(raw)

    def _encode_text(self, value):
        if len(str(value)) > self.maximum:
            raise ValueError('The text exceeds the parameter character limit.')
        return quoted(value)

    def _encode_binary(self, value):
        number = int(str(value), 16)
        if not 0 <= number <= 0xffffffff:
            raise ValueError('The hexadecimal value must fit in four bytes.')
        return f'{number:X}'


INFO_FIELDS = {'productname':'model', 'version':'firmware', 'serialno':'serial_number',
               'devicename':'device_name', 'protocolver':'protocol_version',
               'paramsetver':'parameter_set_version', 'manufacturer':'manufacturer_name',
               'category':'category_name', 'deviceid':'unit_id', 'inputport':'input_ports',
               'outputport':'output_ports', 'gpi':'gpi_ports', 'gpo':'gpo_ports'}
EVENT_FIELDS = {'AudioPlayerSetType':('play_type','type'),
                'AudioPlayerSetPath':('name','path'),
                'AudioPlayerSetPlayMode':('play_mode','mode'),
                'AudioPlayerSetGoToTheTop':('go_to_top','goToTheTop'),
                'AudioPlayerSetInterval':('interval','interval')}
SONG_COMMANDS = {'set_song_type':('AudioPlayerSetType','type'),
                 'set_song_path':('AudioPlayerSetPath','path'),
                 'set_song_mode':('AudioPlayerSetPlayMode','mode'),
                 'set_song_go_to_top':('AudioPlayerSetGoToTheTop','goToTheTop'),
                 'set_song_interval':('AudioPlayerSetInterval','interval')}
OPTIONAL_ERRORS = {'UnknownAddress','UnknownCommand','UnknownEventID','InvalidArgument'}


class YamahaDMEDriver(BaseDriver):
    DRIVER_INFO = {'id': 'yamaha_dme',
     'name': 'Yamaha DME7/DME5/DME3',
     'manufacturer': 'Yamaha',
     'category': 'audio',
     'version': '1.0.0',
     'author': 'OpenAVC',
     'description': 'Controls Yamaha DME installation processors, importing their Remote Control Setup '
                    'List with live feedback, snapshots, optional metering, audio playback, and public '
                    'scheduler events.',
     'source_url': 'https://usa.yamaha.com/files/download/other_assets/0/2935190/DME7_5_3_remote_control_specs_v310_en.pdf',
     'transport': 'tcp',
     'delimiter': '\n',
     'ports': [49280],
     'protocols': ['yamaha_rcp'],
     'tags': ['dsp', 'dante', 'audio-processor', 'snapshots'],
     'simulated': True,
     'verified': False,
     'min_platform_version': '0.37.1',
     'compatible_models': [{'manufacturer': 'Yamaha',
                            'models': ['DME7', 'DME5', 'DME3'],
                            'confidence': 'untested',
                            'notes': 'Protocol V3.1.0, device firmware V3.10 or later. Configure the '
                                     'Remote Control Setup List in ProVisionaire Design. No hardware '
                                     'validation yet.'}],
     'help': {'overview': 'Imports the parameters assigned in the processor Remote Control Setup List. '
                          'Controls use the ranges and units reported by the processor. Changes from '
                          'other controllers update OpenAVC automatically.',
              'setup': 'Create and synchronize a Remote Control Setup List in ProVisionaire Design, '
                       'then enter the processor NETWORK address. Control uses TCP 49280. Initial '
                       'control import checks the available list slots and can take several minutes. '
                       'Add parameter set IDs for snapshots beyond the current set. Live Meters are '
                       'optional. Use a unique list index for each parameter: Yamaha reports duplicate '
                       'assignments only at the lowest index.'},
     'default_config': {'host': '',
                        'port': 49280,
                        'poll_interval': 15,
                        'inter_command_delay': 0.01,
                        'model': 'DME7',
                        'parameter_sets': [],
                        'meters': []},
     'config_schema': {'host': {'type': 'string', 'label': 'IP Address', 'required': True},
                       'port': {'type': 'integer',
                                'label': 'Port',
                                'default': 49280,
                                'min': 1,
                                'max': 65535},
                       'poll_interval': {'type': 'number',
                                         'label': 'Status Poll Interval',
                                         'default': 15,
                                         'min': 0},
                       'inter_command_delay': {'type': 'number',
                                               'label': 'Command Interval',
                                               'default': 0.01,
                                               'min': 0.01,
                                               'advanced': True},
                       'model': {'type': 'enum',
                                 'label': 'Model',
                                 'default': 'DME7',
                                 'values': ['DME7', 'DME5', 'DME3'],
                                 'learned_from': 'model'},
                       'parameter_sets': {'type': 'table',
                                          'label': 'Additional Parameter Sets',
                                          'default': [],
                                          'row_label': 'parameter set',
                                          'columns': {'id': {'type': 'integer',
                                                             'label': 'Parameter Set ID',
                                                             'required': True,
                                                             'min': 1}},
                                          'help': 'The current parameter set is imported '
                                                  'automatically. Add other parameter set IDs from '
                                                  'ProVisionaire Design to make their snapshots '
                                                  'available.'},
                       'meters': {'type': 'table',
                                  'label': 'Live Meters',
                                  'default': [],
                                  'row_label': 'meter',
                                  'columns': {'index': {'type': 'integer',
                                                        'label': 'Remote List Index',
                                                        'required': True,
                                                        'min': 1,
                                                        'max': 1000},
                                              'peak_hold': {'type': 'boolean',
                                                            'label': 'Include Peak Hold',
                                                            'default': False}},
                                  'help': 'Optional meter indexes from the Remote Control Setup List. '
                                          'Each selected meter reports twice per second. Leave empty '
                                          'to disable meter traffic.'}},
     'state_variables': {'model': {'type': 'string', 'label': 'Model'},
                         'firmware': {'type': 'string', 'label': 'Firmware'},
                         'serial_number': {'type': 'string', 'label': 'Serial Number'},
                         'device_name': {'type': 'string', 'label': 'Device Name'},
                         'protocol_version': {'type': 'string', 'label': 'Protocol Version'},
                         'parameter_set_version': {'type': 'string', 'label': 'Parameter Set Version'},
                         'manufacturer_name': {'type': 'string', 'label': 'Manufacturer'},
                         'category_name': {'type': 'string', 'label': 'Device Category'},
                         'unit_id': {'type': 'string', 'label': 'Unit ID'},
                         'run_mode': {'type': 'string', 'label': 'Run Mode', 'cloud_priority': 'high'},
                         'alert': {'type': 'string', 'label': 'Device Alert', 'cloud_priority': 'high'},
                         'power1': {'type': 'string',
                                    'label': 'Power Supply A',
                                    'cloud_priority': 'high'},
                         'power2': {'type': 'string',
                                    'label': 'Power Supply B',
                                    'cloud_priority': 'high'},
                         'input_ports': {'type': 'integer', 'label': 'Audio Inputs'},
                         'output_ports': {'type': 'integer', 'label': 'Audio Outputs'},
                         'gpi_ports': {'type': 'integer', 'label': 'GPI Inputs'},
                         'gpo_ports': {'type': 'integer', 'label': 'GPI Outputs'},
                         'import_status': {'type': 'string', 'label': 'Control Import'},
                         'parameter_slots': {'type': 'integer', 'label': 'Parameter Slots'},
                         'meter_slots': {'type': 'integer', 'label': 'Meter Slots'},
                         'current_parameter_set': {'type': 'integer', 'label': 'Current Parameter Set'},
                         'current_snapshot': {'type': 'integer', 'label': 'Current Snapshot'},
                         'snapshot_status': {'type': 'string', 'label': 'Snapshot Status'},
                         'player_status': {'type': 'string', 'label': 'Playback'},
                         'current_song': {'type': 'integer', 'label': 'Current Song'},
                         'media': {'type': 'string', 'label': 'Media'},
                         'synchronizing': {'type': 'boolean', 'label': 'Synchronizing'},
                         'last_error': {'type': 'string',
                                        'label': 'Last Error',
                                        'cloud_priority': 'high'}},
     'child_entity_types': {'parameter': {'label': 'Parameter',
                                          'label_plural': 'Parameters',
                                          'dynamic': True,
                                          'id_format': {'type': 'string', 'max_length': 64},
                                          'label_field': 'name',
                                          'summary_fields': ['name', 'display'],
                                          'state_variables': {'name': {'type': 'string',
                                                                       'label': 'Name'},
                                                              'display': {'type': 'string',
                                                                          'label': 'Display Value'},
                                                              'address': {'type': 'string',
                                                                          'label': 'Address'},
                                                              'writable': {'type': 'boolean',
                                                                           'label': 'Writable'}}},
                            'meter': {'label': 'Meter',
                                      'label_plural': 'Meters',
                                      'dynamic': True,
                                      'id_format': {'type': 'string', 'max_length': 64},
                                      'label_field': 'name',
                                      'summary_fields': ['name', 'value'],
                                      'state_variables': {'name': {'type': 'string', 'label': 'Name'},
                                                          'meter_type': {'type': 'string',
                                                                         'label': 'Meter Type'}}},
                            'snapshot': {'label': 'Snapshot',
                                         'label_plural': 'Snapshots',
                                         'id_format': {'type': 'string', 'max_length': 64},
                                         'label_field': 'name',
                                         'summary_fields': ['name', 'attribute'],
                                         'state_variables': {'name': {'type': 'string',
                                                                      'label': 'Name'},
                                                             'parameter_set': {'type': 'integer',
                                                                               'label': 'Parameter '
                                                                                        'Set'},
                                                             'number': {'type': 'integer',
                                                                        'label': 'Number'},
                                                             'attribute': {'type': 'string',
                                                                           'label': 'Availability'}}},
                            'song': {'label': 'Song',
                                     'label_plural': 'Songs',
                                     'id_format': {'type': 'integer', 'min': 1},
                                     'label_field': 'name',
                                     'summary_fields': ['name', 'play_mode'],
                                     'state_variables': {'name': {'type': 'string',
                                                                  'label': 'File or Folder'},
                                                         'play_type': {'type': 'string',
                                                                       'label': 'Playback Type'},
                                                         'play_mode': {'type': 'string',
                                                                       'label': 'Play Mode'},
                                                         'go_to_top': {'type': 'boolean',
                                                                       'label': 'Go to Top'},
                                                         'interval': {'type': 'number',
                                                                      'label': 'Interval',
                                                                      'unit': 's'}}},
                            'schedule': {'label': 'Scheduled Event',
                                         'label_plural': 'Scheduled Events',
                                         'id_format': {'type': 'integer', 'min': 1},
                                         'label_field': 'name',
                                         'summary_fields': ['name', 'enabled', 'time'],
                                         'state_variables': {'name': {'type': 'string',
                                                                      'label': 'Name'},
                                                             'enabled': {'type': 'boolean',
                                                                         'label': 'Enabled',
                                                                         'control': True},
                                                             'time': {'type': 'string',
                                                                      'label': 'Time',
                                                                      'control': True}}}},
     'commands': {'query_status': {'label': 'Read Status',
                                   'help': 'Reads the processor status, current snapshot, and playback '
                                           'status.',
                                   'params': {}},
                  'refresh_controls': {'label': 'Refresh Controls',
                                       'help': 'Reads the Remote Control Setup List and refreshes the '
                                               'available controls.',
                                       'params': {}},
                  'set_control': {'label': 'Set Control',
                                  'help': 'Sets a discovered control in its displayed units and reads '
                                          'back the result.',
                                  'params': {'parameter': {'type': 'child_id',
                                                           'label': 'Parameter',
                                                           'required': True,
                                                           'child_type': 'parameter'},
                                             'property': {'type': 'string',
                                                          'label': 'Control',
                                                          'required': True,
                                                          'options_from': {'param': 'parameter',
                                                                           'source': 'child_schema'}},
                                             'value': {'type': 'string',
                                                       'label': 'Value',
                                                       'required': True,
                                                       'type_from': {'param': 'property'}}}},
                  'set_text': {'label': 'Set Display Value',
                               'help': 'Sets a parameter using Yamaha display text, such as -6.50, ON, '
                                       'or -inf.',
                               'params': {'parameter': {'type': 'child_id',
                                                        'label': 'Parameter',
                                                        'required': True,
                                                        'child_type': 'parameter'},
                                          'value': {'type': 'string',
                                                    'label': 'Display Value',
                                                    'required': True,
                                                    'trim': False}}},
                  'set_raw': {'label': 'Set Raw Value',
                              'help': 'Sets a parameter using the raw value defined by Yamaha.',
                              'params': {'parameter': {'type': 'child_id',
                                                       'label': 'Parameter',
                                                       'required': True,
                                                       'child_type': 'parameter'},
                                         'value': {'type': 'string',
                                                   'label': 'Raw Value',
                                                   'required': True}}},
                  'step_fader': {'label': 'Step Fader',
                                 'help': 'Changes a fader by the specified number of Yamaha steps.',
                                 'params': {'parameter': {'type': 'child_id',
                                                          'label': 'Parameter',
                                                          'required': True,
                                                          'child_type': 'parameter'},
                                            'steps': {'type': 'integer',
                                                      'label': 'Steps',
                                                      'required': True}}},
                  'read_parameter': {'label': 'Read Parameter',
                                     'help': 'Reads the selected parameter from the processor.',
                                     'params': {'parameter': {'type': 'child_id',
                                                              'label': 'Parameter',
                                                              'required': True,
                                                              'child_type': 'parameter'}}},
                  'recall_snapshot': {'label': 'Recall Snapshot',
                                      'help': 'Recalls the selected snapshot and refreshes the '
                                              'affected control values.',
                                      'params': {'snapshot': {'type': 'child_id',
                                                              'label': 'Snapshot',
                                                              'required': True,
                                                              'child_type': 'snapshot'}}},
                  'identify': {'label': 'Identify',
                               'help': 'Flashes the processor identification display for the selected '
                                       'duration.',
                               'params': {'seconds': {'type': 'integer',
                                                      'label': 'Duration',
                                                      'required': True,
                                                      'min': 0,
                                                      'max': 60,
                                                      'default': 3,
                                                      'unit': 's'}}},
                  'set_run_mode': {'label': 'Set Run Mode',
                                   'help': 'Changes the processor between normal and emergency '
                                           'operation.',
                                   'params': {'mode': {'type': 'enum',
                                                       'label': 'Mode',
                                                       'required': True,
                                                       'values': ['normal', 'emergency']}},
                                   'confirm': 'Change the operating mode of the entire audio '
                                              'processor?'},
                  'player_transport': {'label': 'Playback Control',
                                       'help': 'Plays, stops, or changes the selected audio item.',
                                       'params': {'operation': {'type': 'enum',
                                                                'label': 'Operation',
                                                                'required': True,
                                                                'values': ['play',
                                                                           'stop',
                                                                           'prev',
                                                                           'next']}}},
                  'select_song': {'label': 'Select Song',
                                  'help': 'Selects an audio player item.',
                                  'params': {'song': {'type': 'child_id',
                                                      'label': 'Song',
                                                      'required': True,
                                                      'child_type': 'song'}}},
                  'set_song_type': {'label': 'Set Playback Type',
                                    'help': 'Assigns a song, folder, or no item to the selected player '
                                            'entry.',
                                    'params': {'song': {'type': 'child_id',
                                                        'label': 'Song',
                                                        'required': True,
                                                        'child_type': 'song'},
                                               'value': {'type': 'enum',
                                                         'label': 'Type',
                                                         'required': True,
                                                         'values': ['noAssign', '1song', 'folder']}}},
                  'set_song_path': {'label': 'Set Song or Folder',
                                    'help': 'Sets the media path for the selected player entry.',
                                    'params': {'song': {'type': 'child_id',
                                                        'label': 'Song',
                                                        'required': True,
                                                        'child_type': 'song'},
                                               'value': {'type': 'string',
                                                         'label': 'Path',
                                                         'required': True,
                                                         'trim': False}}},
                  'set_song_mode': {'label': 'Set Play Mode',
                                    'help': 'Sets normal, repeat, or shuffle repeat playback for the '
                                            'selected entry.',
                                    'params': {'song': {'type': 'child_id',
                                                        'label': 'Song',
                                                        'required': True,
                                                        'child_type': 'song'},
                                               'value': {'type': 'enum',
                                                         'label': 'Mode',
                                                         'required': True,
                                                         'values': ['normal',
                                                                    'repeat',
                                                                    'shuffleRepeat']}}},
                  'set_song_go_to_top': {'label': 'Set Go to Top',
                                         'help': 'Sets whether the selected entry starts at its '
                                                 'beginning.',
                                         'params': {'song': {'type': 'child_id',
                                                             'label': 'Song',
                                                             'required': True,
                                                             'child_type': 'song'},
                                                    'value': {'type': 'boolean',
                                                              'label': 'Go to Top',
                                                              'required': True}}},
                  'set_song_interval': {'label': 'Set Song Interval',
                                        'help': 'Sets the interval between songs for the selected '
                                                'entry.',
                                        'params': {'song': {'type': 'child_id',
                                                            'label': 'Song',
                                                            'required': True,
                                                            'child_type': 'song'},
                                                   'value': {'type': 'number',
                                                             'label': 'Interval',
                                                             'required': True,
                                                             'min': 0,
                                                             'max': 10,
                                                             'unit': 's'}}},
                  'set_schedule_enabled': {'label': 'Enable Scheduled Event',
                                           'help': 'Enables or disables a public scheduler event.',
                                           'params': {'schedule': {'type': 'child_id',
                                                                   'label': 'Scheduled Event',
                                                                   'required': True,
                                                                   'child_type': 'schedule'},
                                                      'value': {'type': 'boolean',
                                                                'label': 'Enabled',
                                                                'required': True}},
                                           'sets': {'enabled': '{value}'}},
                  'set_schedule_time': {'label': 'Set Scheduled Time',
                                        'help': 'Changes the time of a public scheduler event.',
                                        'params': {'schedule': {'type': 'child_id',
                                                                'label': 'Scheduled Event',
                                                                'required': True,
                                                                'child_type': 'schedule'},
                                                   'value': {'type': 'string',
                                                             'label': 'Time',
                                                             'required': True,
                                                             'pattern': '^(?:[01]?\\d|2[0-3]):[0-5]\\d$'}},
                                        'sets': {'time': '{value}'}},
                  'set_display_font': {'label': 'Set Display Font',
                                       'help': 'Selects the character set used by the processor '
                                               'display.',
                                       'params': {'font': {'type': 'enum',
                                                           'label': 'Font',
                                                           'required': True,
                                                           'values': [{'value': 'type1',
                                                                       'label': 'Japanese and '
                                                                                'European'},
                                                                      {'value': 'type2',
                                                                       'label': 'Chinese, Japanese, '
                                                                                'and European'}]}}}},
     'quick_actions': ['set_control', 'recall_snapshot', 'player_transport'],
     'discovery': {'tcp_probe': {'port': 49280,
                                 'send_ascii': 'devstatus runmode\n',
                                 'expect_regex': 'OK devstatus runmode "normal"',
                                 'extract_manufacturer': 'Yamaha',
                                 'then': {'send_ascii': 'devinfo productname\n',
                                           'expect_regex': 'OK devinfo productname "DME(?:7|5|3)"'}}}}
    HEALTH_INTERVAL_S = 15
    HEALTH_TIMEOUT_S = 5
    HEALTH_MAX_FAILURES = 2
    HEALTH_FAULT_MESSAGE = 'The processor stopped answering status queries.'

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self.DRIVER_INFO = copy.deepcopy(self.DRIVER_INFO)
        self._request_lock = asyncio.Lock()
        self._import_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._pending = None
        self._wanted = ()
        self._jobs = {}
        self._parameters = {}
        self._addresses = {}
        self._meter_kinds = {}
        self._cursor = 0
        self._reading = False
        self._needs_sync = False
        self._changed = set()

    def _job(self, name, operation):
        current = self._jobs.get(name)
        if current and not current.done():
            return
        task = asyncio.create_task(self._run_job(operation))
        self._jobs[name] = task

    async def _run_job(self, operation):
        try:
            await operation()
        except (ConnectionError, asyncio.TimeoutError):
            self._force_disconnect('no_response', self.HEALTH_FAULT_MESSAGE)
        except (ValueError, DeviceRefused) as exc:
            self.set_state('last_error', str(exc))
            self.set_state('import_status', 'Import incomplete. Use Refresh Controls to retry.')

    async def _close_session(self):
        current = asyncio.current_task()
        tasks = [t for t in self._jobs.values() if t is not current]
        self._jobs.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._pending and not self._pending.done():
            self._pending.set_exception(ConnectionError('The processor connection closed.'))
        self._pending = None
        self._reading = False

    async def _request(self, line):
        async with self._request_lock:
            if not self.transport or not self.transport.connected:
                raise ConnectionError('The processor is disconnected.')
            future = asyncio.get_running_loop().create_future()
            self._wanted = tuple(shlex.split(line))
            self._pending = future
            try:
                await self.transport.send((line + '\n').encode('ascii'))
                reply = await asyncio.wait_for(future, timeout=3)
                await asyncio.sleep(max(0.01, float(self.config.get('inter_command_delay', 0.01))))
            except asyncio.TimeoutError:
                # A late, untagged ERROR must never answer the next request.
                self._force_disconnect('no_response', self.HEALTH_FAULT_MESSAGE)
                raise
            finally:
                self._pending = None
            if reply[0] == 'ERROR':
                raise DeviceRefused(reply[1], ' '.join(reply[2:]))
            return reply

    async def _optional(self, line):
        try:
            return await self._request(line)
        except DeviceRefused as exc:
            if exc.reason not in OPTIONAL_ERRORS:
                raise
            return None

    def _matches(self, tokens):
        if len(tokens) < 2 or not self._wanted:
            return False
        verb = self._wanted[0]
        aliases = {'sscurrent_ex':'sscurrent', 'ssrecall_ex':'ssrecall'}
        if tokens[1] not in {verb, aliases.get(verb)}:
            return False
        if tokens[0] == 'ERROR':
            return True
        if verb == 'devstatus':
            return tokens[2].lower() == self._wanted[1].lower()
        widths = {'devinfo':1,'devstatus':1,'scpmode':1,'prminfo':1,'mtrinfo':1,
                  'get':3,'getn':3,'gett':3,'set':3,'setn':3,'sett':3,'setr':3,
                  'mtrstart':1,'mtrstop':1,'ssinfo_ex':2,'listitem':2,'listitemnum':1}
        width = widths.get(verb, 0)
        if verb == 'event':
            expected = self._wanted[1]
            choices = {expected, expected.replace('GetCurrentSong','SetCurrentSong')}
            return len(tokens) > 2 and tokens[2] in choices
        return tuple(tokens[2:2+width]) == self._wanted[1:1+width]

    async def on_data_received(self, data):
        try:
            tokens = shlex.split(data.decode('utf-8').strip())
            if not tokens:
                return
            self._apply(tokens)
            if tokens[0] in {'OK','OKm','ERROR'} and self._matches(tokens):
                if self._pending and not self._pending.done():
                    self._pending.set_result(tokens)
        except (ValueError, IndexError, KeyError) as exc:
            self.set_state('last_error', f'Cannot read a processor reply: {exc}')

    def _apply(self, tokens):
        if len(tokens) < 2 or tokens[0] == 'ERROR':
            return
        dispatch = {'devinfo':self._info, 'devstatus':self._status,
                    'get':self._parameter_reply, 'set':self._parameter_reply,
                    'setr':self._parameter_reply, 'getn':self._parameter_reply,
                    'setn':self._parameter_reply, 'gett':self._parameter_reply,
                    'sett':self._parameter_reply, 'mtr':self._meter_reply,
                    'sscurrent_ex':self._snapshot_reply, 'sscurrent':self._snapshot_reply,
                    'ssrecall_ex':self._recall_reply, 'ssrecall':self._recall_reply,
                    'event':self._event_reply}
        handler = dispatch.get(tokens[1])
        if handler:
            handler(tokens)

    def _info(self, t):
        key = INFO_FIELDS.get(t[2])
        if key:
            value = int(t[3]) if key.endswith('_ports') else t[3]
            self.set_state(key, value)

    def _status(self, t):
        key = {'runmode':'run_mode','error':'alert','power1':'power1','power2':'power2'}.get(t[2].lower())
        if key:
            self.set_state(key, t[3])

    async def _post_connect(self):
        for _ in range(60):
            reply = await self._request('devstatus runmode')
            if reply[-1] == 'normal':
                return
            await asyncio.sleep(1)
        raise ConnectionFaultError('The processor has not entered normal operation.', code='no_response')

    async def _initial_sync(self):
        for line in ('scpmode encoding utf8','scpmode valuetype raw','scpmode resolution 1000'):
            await self._request(line)
        for name in INFO_FIELDS:
            await self._optional('devinfo ' + name)
        model = self.get_state('model')
        if model not in {'DME7','DME5','DME3'}:
            raise ConnectionFaultError('The device did not identify as a DME7, DME5, or DME3.', code='invalid_config')
        await self._read_status()
        self.set_state('import_status', 'Reading the Remote Control Setup List')
        self._job('import', self.refresh_children)
        self._job('meters', self._meter_loop)

    async def _liveness_probe(self):
        try:
            await self._request('devstatus runmode')
        except DeviceRefused:
            return  # An explicit refusal proves that the link answers.

    async def poll(self):
        await self._read_status()
        keys = list(self._addresses)
        for _ in range(min(8, len(keys))):
            key = keys[self._cursor % len(keys)]
            self._cursor += 1
            await self._read_parameter(key)

    async def _read_status(self):
        for line in ('devstatus runmode','devstatus error','devstatus power1','devstatus power2',
                     'sscurrent_ex parameter_set','event PROC:AudioPlayerGetStatus ""',
                     'event PROC:AudioPlayerGetCurrentSong ""'):
            await self._optional(line)
        bank = self.get_state('current_parameter_set')
        if bank:
            await self._optional(f'sscurrent_ex {bank}')

    async def refresh_children(self):
        async with self._import_lock:
            self.set_state('import_status', 'Reading the Remote Control Setup List')
            reply = await self._request('prmnum')
            count = int(reply[2])
            if not 0 <= count <= 1000:
                raise ValueError('The processor reported an unsupported Remote Control Setup List size.')
            self.set_state('parameter_slots', count)
            seen = set()
            for index in range(1, count + 1):
                reply = await self._optional(f'prminfo {index}')
                if reply:
                    seen.update(self._register_parameter(Parameter.from_reply(reply)))
            for key in set(self._addresses) - seen:
                self.deregister_child('parameter', key)
                del self._addresses[key]
            await self._import_meters()
            await self._import_snapshots()
            await self._import_list('AudioPlayer','song')
            await self._import_list('Scheduler','schedule')
            await self._sync_parameters()
            self.set_state('import_status', 'Ready')
            return len(self._addresses)

    def _register_parameter(self, spec):
        if min(spec.nx, spec.ny, spec.scaling) < 1:
            raise ValueError(f'Invalid parameter dimensions or scaling at index {spec.index}.')
        self._parameters[spec.index] = spec
        keys = set()
        for x in range(spec.nx):
            for y in range(spec.ny):
                key = f'{spec.index}_{x}_{y}'
                keys.add(key)
                self._parameter_child(key, spec, x, y)
                self._addresses[key] = (spec.index, x, y)
        return keys

    def _parameter_child(self, key, spec, x, y):
        schema = copy.deepcopy(self.DRIVER_INFO['child_entity_types']['parameter']['state_variables'])
        schema.update(value=spec.value_schema(), raw={'type':'string','label':'Raw Value'})
        if spec.has_position:
            schema['position'] = {'type':'integer','label':'Position','min':0,'max':1000,'control':spec.writable}
        if self.is_child_registered('parameter', key):
            self.deregister_child('parameter', key)
        name = f'{spec.index}: {spec.name} [{x},{y}]'
        self.register_child('parameter', key, schema=schema, initial_state={
            'name':name, 'address':f'PROC:Remote/{spec.index} {x} {y}', 'writable':spec.writable})

    def _parameter_reply(self, t):
        index = int(t[2].split('/')[-1])
        key = f'{index}_{t[3]}_{t[4]}'
        if key not in self._addresses:
            return
        spec = self._parameters[index]
        form = t[1]
        if form in {'gett','sett'}:
            values = {'display':t[5]}
        elif form in {'getn','setn'}:
            values = {'position':int(t[5])}
        else:
            values = {'raw':t[5], 'value':spec.decode(t[5])}
        if form in {'set','setn'} and len(t) > 6:
            values['display'] = t[6]
        self.set_child_state_batch('parameter', key, values)
        if t[0] == 'NOTIFY':
            self._changed.add(key)
            self._job('changes', self._read_changes)

    async def _read_changes(self):
        while self._changed:
            key = self._changed.pop()
            if key in self._addresses:
                await self._read_parameter(key)

    async def _read_parameter(self, key):
        index, x, y = self._addresses[key]
        spec = self._parameters[index]
        for verb in ('get','gett') + (('getn',) if spec.has_position else ()):
            await self._optional(f'{verb} PROC:Remote/{index} {x} {y}')

    def _schedule_sync(self):
        self._needs_sync = True
        if self.connected:
            self._job('sync', self._sync_parameters)

    async def _sync_parameters(self):
        async with self._sync_lock:
            await self._sync_all()

    async def _sync_all(self):
        self._reading = True
        try:
            while True:
                self._needs_sync = False
                for key in list(self._addresses):
                    await self._read_parameter(key)
                if not self._needs_sync:
                    return
        finally:
            self._reading = False

    async def _import_meters(self):
        reply = await self._request('mtrnum')
        self.set_state('meter_slots', int(reply[2]))
        wanted = set()
        for row in self.config.get('meters', []):
            index = int(row['index'])
            reply = await self._request(f'mtrinfo {index}')
            dimensions = [int(n) for n in reply[4:-1]]
            size = math.prod(dimensions)
            kind = reply[-1]
            self._meter_kinds[index] = kind
            for n in range(size):
                key = f'{index}_{n}'
                wanted.add(key)
                self._register_meter(key, reply[3], kind)
        for key in set(self.list_children('meter')) - wanted:
            self.deregister_child('meter', key)

    def _register_meter(self, key, name, kind):
        schema = copy.deepcopy(self.DRIVER_INFO['child_entity_types']['meter']['state_variables'])
        schema.update({p:{'type':typ,'label':label,'cloud_priority':'low'} for p,typ,label in
                  [('raw','string','Raw Value'),('peak_raw','string','Peak Raw Value'),
                   ('value','number','Value'),('peak','number','Peak Hold'),('clip','boolean','Clipping')]})
        if kind in {'level','hold','gr'}:
            for p in ('value','peak'):
                schema[p].update(unit='dBFS' if kind != 'gr' else 'dB', min=-126, max=1)
        if self.is_child_registered('meter', key):
            self.deregister_child('meter', key)
        self.register_child('meter', key, schema=schema, initial_state={'name':f'{name} [{key}]','meter_type':kind})

    def _meter_reply(self, t):
        path = t[2].split('/')[-1]
        index = int(path.split('>')[0])
        peak = '>PeakHold' in path
        kind = t[3]
        for n, raw in enumerate(t[4:]):
            key = f'{index}_{n}'
            if not self.is_child_registered('meter', key):
                continue
            values = self._meter_values(raw, kind, peak)
            self.set_child_state_batch('meter', key, values)

    @staticmethod
    def _meter_values(raw, kind, peak):
        number = int(raw, 16)
        values = {'peak_raw' if peak else 'raw':raw}
        if kind in {'level','hold','gr'}:
            number, clip = meter_value(raw)
            values['clip'] = clip
        if kind == 'gr' and int(raw, 16) >= 127:
            number = None
        values['peak' if peak else 'value'] = number
        return values

    async def _meter_loop(self):
        while self.connected:
            for row in self.config.get('meters', []):
                index = int(row['index'])
                if index not in self._meter_kinds:
                    continue
                await self._request(f'mtrstart PROC:Remote/{index} 500')
                if row.get('peak_hold'):
                    await self._request(f'mtrstart PROC:Remote/{index}>PeakHold 500')
            await asyncio.sleep(2)

    async def _import_snapshots(self):
        banks = {int(row['id']) for row in self.config.get('parameter_sets', [])}
        current = self.get_state('current_parameter_set')
        if current:
            banks.add(int(current))
        wanted = set()
        for bank in sorted(banks):
            wanted.update(await self._snapshot_bank(bank))
        for key in set(self.list_children('snapshot')) - wanted:
            self.deregister_child('snapshot', key)

    async def _snapshot_bank(self, bank):
        reply = await self._optional(f'ssnum_ex {bank}')
        wanted = set()
        if not reply:
            return wanted
        for n in range(1, min(100, int(reply[-1])) + 1):
            row = await self._optional(f'ssinfo_ex {bank} {n}')
            if not row or row[-1] not in {'user','preinst'}:
                continue
            key = f'{bank}_{n}'
            wanted.add(key)
            self.register_child('snapshot', key)
            self.set_child_state_batch('snapshot', key, {'name':f'{bank} / {row[4]} {row[5]}',
                'parameter_set':bank,'number':n,'attribute':row[-1]})
        return wanted

    def _snapshot_reply(self, t):
        values = t[2:]
        if values[0] == 'parameter_set':
            self.set_state('current_parameter_set', int(values[1]))
        elif len(values) == 2 and values[1] in {'modified','unmodified'}:
            self.set_state('current_parameter_set', int(values[0]))
        else:
            self.set_state('current_parameter_set', int(values[0]))
            self.set_state('current_snapshot', int(values[1]))
            if len(values) > 2:
                self.set_state('snapshot_status', values[2])
        if t[0] == 'NOTIFY':
            self._schedule_sync()

    def _recall_reply(self, t):
        if t[0] == 'NOTIFY':
            self._schedule_sync()

    async def _import_list(self, name, child_type):
        reply = await self._optional(f'listitemnum PROC:{name}')
        if not reply or reply[0] == 'OKm':
            return
        wanted = set()
        for index in range(int(reply[4]), int(reply[5]) + 1):
            row = await self._optional(f'listitem PROC:{name} {index}')
            if row:
                wanted.add(index)
                self._list_reply(row, child_type)
        for key in set(self.list_children(child_type)) - wanted:
            self.deregister_child(child_type, key)

    def _list_reply(self, t, child_type):
        index = int(t[3])
        if child_type == 'song':
            mode, play, top, interval, name = t[4].split(' ', 4)
            values = {'name':name,'play_type':mode,'play_mode':play,
                      'go_to_top':top.lower() == 'on','interval':float(interval)}
        else:
            enabled, clock, name = t[4].split(' ', 2)
            values = {'name':name,'enabled':enabled == 'enable','time':clock}
        self.register_child(child_type, index)
        self.set_child_state_batch(child_type, index, values)

    def _event_reply(self, t):
        event = t[2].removeprefix('PROC:')
        args = dict(v.split('=', 1) for v in t[3:] if '=' in v)
        if t[0] == 'OKm':
            self.set_state('last_error', ' '.join(t[3:]))
            return
        simple = {'AudioPlayerTransport':('player_status','operation'),
                  'AudioPlayerGetStatus':('player_status','status'),
                  'AudioPlayerGetCurrentSong':('current_song','index'),
                  'AudioPlayerSetCurrentSong':('current_song','index')}
        if event in simple:
            key, arg = simple[event]
            self._player_event(key, args.get(arg))
        elif event == 'Media':
            self.set_state('media', t[3])
        elif event == 'SynchronizationSetStatus':
            self._synchronization(t[3])
        elif event in EVENT_FIELDS:
            self._song_event(event, args)
        elif event in {'SchedulerSetEnable', 'SchedulerSetTime'}:
            self._schedule_event(event, args)

    def _player_event(self, key, value):
        if value in {'prev', 'next'}:
            self._job('player', self._read_status)
            return
        if value is None:
            return
        self.set_state(key, int(value) if key == 'current_song' else value)

    def _synchronization(self, status):
        self.set_state('synchronizing', status == 'active')
        if status == 'inactive':
            self._job('synchronized_import', self.refresh_children)

    def _schedule_event(self, event, args):
        index = int(args['index'])
        if not self.is_child_registered('schedule', index):
            return
        if event == 'SchedulerSetEnable':
            self.set_child_state('schedule', index, 'enabled', args['enable'] == 'true')
        else:
            self.set_child_state('schedule', index, 'time', args['time'])

    def _song_event(self, event, args):
        index = int(args['index'])
        if not self.is_child_registered('song', index):
            return
        key, arg = EVENT_FIELDS[event]
        value = args[arg]
        conversions = {'go_to_top':lambda v:v == 'on','interval':float}
        self.set_child_state('song', index, key, conversions.get(key, str)(value))

    async def send_command(self, command, params=None):
        p = params or {}
        handlers = {'query_status':self._command_status, 'refresh_controls':self._command_refresh,
                    'set_control':self._set_control, 'set_text':self._set_text,
                    'set_raw':self._set_raw, 'step_fader':self._step_fader,
                    'read_parameter':self._command_read, 'recall_snapshot':self._recall,
                    'identify':self._identify, 'set_run_mode':self._run_mode,
                    'player_transport':self._player_transport,'select_song':self._select_song,
                    'set_schedule_enabled':self._schedule_enabled,'set_schedule_time':self._schedule_time,
                    'set_display_font':self._display_font}
        if command in SONG_COMMANDS:
            return await self._write_song(command, p)
        handler = handlers.get(command)
        if handler is None:
            raise ValueError(f'Unknown command: {command}')
        return await handler(p)

    async def _command_status(self, p):
        await self._read_status()

    async def _command_refresh(self, p):
        return await self.refresh_children()

    async def _command_read(self, p):
        await self._read_parameter(str(p['parameter']))

    def _writable_address(self, p):
        key = str(p['parameter'])
        if key not in self._addresses:
            raise ValueError('Select a parameter imported from the processor.')
        index, x, y = self._addresses[key]
        spec = self._parameters[index]
        if not spec.writable:
            raise CommandRefusedError('This parameter is read only.')
        return key, spec, f'PROC:Remote/{index} {x} {y}'

    async def _write_parameter(self, p, verb, value):
        key, _, address = self._writable_address(p)
        reply = await self._request(f'{verb} {address} {value}')
        await self._read_parameter(key)
        return 'Value adjusted by the processor.' if reply[0] == 'OKm' else None

    async def _set_control(self, p):
        _, spec, _ = self._writable_address(p)
        prop = p['property']
        if prop == 'value':
            return await self._write_parameter(p, 'set', spec.encode(p['value']))
        if prop == 'position' and spec.has_position:
            value = int(p['value'])
            if 0 <= value <= 1000:
                return await self._write_parameter(p, 'setn', str(value))
        raise ValueError('Select a writable control and a value inside its range.')

    async def _set_text(self, p):
        return await self._write_parameter(p, 'sett', quoted(p['value']))

    async def _set_raw(self, p):
        _, spec, _ = self._writable_address(p)
        value = quoted(p['value']) if spec.kind == 'string' else str(p['value'])
        if spec.kind != 'string':
            int(value, 16 if spec.kind == 'binary' else 10)
        return await self._write_parameter(p, 'set', value)

    async def _step_fader(self, p):
        _, spec, _ = self._writable_address(p)
        if spec.ui != 'fader':
            raise CommandRefusedError('Relative steps are available only for fader parameters.')
        return await self._write_parameter(p, 'setr', str(int(p['steps'])))

    async def _recall(self, p):
        bank, number = str(p['snapshot']).split('_')
        reply = await self._request(f'ssrecall_ex {int(bank)} {int(number)}')
        await self._sync_parameters()
        await self._optional(f'sscurrent_ex {int(bank)}')
        return reply[0]

    async def _identify(self, p):
        await self._request(f'identify {int(p["seconds"])}')

    async def _run_mode(self, p):
        await self._request('devmode ' + p['mode'])
        await self._request('devstatus runmode')

    async def _event_write(self, name, arguments):
        reply = await self._request('event PROC:' + name + ' ' + ' '.join(quoted(v) for v in arguments))
        if reply[0] == 'OKm':
            raise DeviceRefused(name, ' '.join(reply[3:]))

    async def _player_transport(self, p):
        await self._event_write('AudioPlayerTransport', ['operation=' + p['operation']])
        await self._read_status()

    async def _select_song(self, p):
        await self._event_write('AudioPlayerSetCurrentSong', [f'index={int(p["song"])}'])
        await self._read_status()

    async def _write_song(self, command, p):
        event, key = SONG_COMMANDS[command]
        value = p['value']
        if key == 'goToTheTop':
            value = 'on' if value else 'off'
        index = int(p['song'])
        await self._event_write(event, [f'index={index}', f'{key}={value}'])
        self._list_reply(await self._request(f'listitem PROC:AudioPlayer {index}'), 'song')

    async def _schedule_enabled(self, p):
        value = 'true' if p['value'] else 'false'
        await self._write_schedule(p, 'SchedulerSetEnable', 'enable=' + value)

    async def _schedule_time(self, p):
        await self._write_schedule(p, 'SchedulerSetTime', 'time=' + p['value'])

    async def _write_schedule(self, p, event, value):
        index = int(p['schedule'])
        await self._event_write(event, [f'index={index}', value])
        self._list_reply(await self._request(f'listitem PROC:Scheduler {index}'), 'schedule')

    async def _display_font(self, p):
        await self._event_write('Language', [p['font']])
