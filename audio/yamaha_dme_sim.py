"""Document-derived Yamaha DME7/5/3 RCP V3.1.0 simulator.

The example design has a two-channel fader, mute, matrix, label, read-only
frequency, hexadecimal flags, and a two-channel meter at Remote index 7.
It reports the real 1000-slot list capacity with sparse assignments. This is
a protocol fixture, not an acoustic model or a hardware-verified capture.
Fader normalization uses a linear fixture curve; the driver asks the device
for the actual normalized value rather than deriving one from this curve.
"""

import asyncio
import json
import shlex
import time
import weakref

from openavc.simulator.tcp_simulator import TCPSimulator


def quote(value):
    return json.dumps(str(value), ensure_ascii=False)


def wire(line):
    return (line + '\n').encode('utf-8')


# name, x-count, y-count, minimum, maximum, initial, unit, type, UI, access, scale
PARAMETERS = {
    1: ('Fader/Level', 2, 1, -13801, 1000, -1200, 'dB', 'integer', 'fader', 'rw', 100),
    2: ('Fader/On', 1, 1, 0, 1, 1, '', 'integer', 'latchsw', 'rw', 1),
    3: ('Matrix/Level', 2, 2, -13801, 0, -600, 'dB', 'integer', 'fader', 'rw', 100),
    4: ('Label', 1, 1, 0, 64, 'Room A', '', 'string', 'any', 'rw', 1),
    5: ('Frequency', 1, 1, 20, 20000, 1000, 'Hz', 'freq', 'knob', 'r', 1),
    6: ('Flags', 1, 1, 0, 0xffffffff, 'A5', '', 'binary', 'any', 'rw', 1),
}
EVENT_SONG = {
    'AudioPlayerSetType': ('play_type', 'type'),
    'AudioPlayerSetPath': ('name', 'path'),
    'AudioPlayerSetPlayMode': ('play_mode', 'mode'),
    'AudioPlayerSetGoToTheTop': ('go_to_top', 'goToTheTop'),
    'AudioPlayerSetInterval': ('interval', 'interval'),
}


class YamahaDMESimulator(TCPSimulator):
    SIMULATOR_INFO = {
        'driver_id': 'yamaha_dme', 'name': 'Yamaha DME Simulator',
        'category': 'audio', 'transport': 'tcp', 'default_port': 49280,
        'delimiter': '\n',
        'initial_state': {
            'model': 'DME7', 'run_mode': 'normal', 'gain': -12.0, 'on': True,
            'meter_db': -13, 'clip': False, 'snapshot': 1, 'player_status': 'stop',
            'current_song': 1, 'media': True, 'synchronizing': False,
        },
        'controls': [
            {'type': 'indicator', 'key': 'model', 'label': 'Model'},
            {'type': 'slider', 'key': 'gain', 'label': 'Channel 1 Gain (dB)',
             'min': -138, 'max': 10, 'step': 0.1},
            {'type': 'toggle', 'key': 'on', 'label': 'Channel On'},
            {'type': 'slider', 'key': 'meter_db', 'label': 'Meter (dBFS)',
             'min': -126, 'max': 0, 'step': 1},
            {'type': 'toggle', 'key': 'clip', 'label': 'Clipping'},
            {'type': 'slider', 'key': 'snapshot', 'label': 'Recall Snapshot',
             'min': 1, 'max': 2, 'step': 1},
            {'type': 'toggle', 'key': 'media', 'label': 'Media Inserted'},
            {'type': 'toggle', 'key': 'synchronizing', 'label': 'Synchronizing'},
        ],
        'error_modes': {'busy': {'description': 'Reject commands as Busy'}},
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._sessions = weakref.WeakKeyDictionary()
        self._leases = {}
        self._tasks = set()
        self._sender = None
        self._values = {(i, x, y): p[5] for i, p in PARAMETERS.items()
                        for x in range(p[1]) for y in range(p[2])}
        self._songs = {i: {'name': f'Announcement {i}.wav', 'play_type': '1song',
                          'play_mode': 'normal', 'go_to_top': 'on', 'interval': '1.0'}
                       for i in (1, 2)}
        self._schedules = {i: {'name': f'Event {i}', 'enabled': True, 'time': '9:00'}
                           for i in (1, 2)}
        super().set_state('model', self.config.get('model', 'DME7'))

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self, port):
        await super().start(port)
        self._spawn(self._meter_clock())

    async def stop(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._leases.clear()
        await super().stop()

    async def on_client_connected(self, client_id):
        # Both hooks execute in TCPSimulator's per-connection reader task.
        # Weak keys disappear when that reader finishes; no framework override.
        self._sessions[asyncio.current_task()] = client_id

    def handle_command(self, data):
        tokens = shlex.split(data.decode('ascii').strip())
        if not tokens:
            return None
        self._sender = self._sessions.get(asyncio.current_task())
        try:
            if 'busy' in self.active_errors:
                return wire(f'ERROR {tokens[0]} Busy')
            return wire(self._dispatch(tokens))
        except (ValueError, IndexError):
            return wire(f'ERROR {tokens[0]} InvalidArgument')
        except KeyError:
            return wire(f'ERROR {tokens[0]} UnknownAddress')
        finally:
            self._sender = None

    def _dispatch(self, t):
        handlers = {'devstatus': self._status, 'devinfo': self._info,
                    'prminfo': self._parameter_info, 'mtrinfo': self._meter_info,
                    'mtrstart': self._meter_start, 'mtrstop': self._meter_stop,
                    'sscurrent_ex': self._snapshot_current, 'ssnum_ex': self._snapshot_count,
                    'ssinfo_ex': self._snapshot_info, 'ssrecall_ex': self._snapshot_recall,
                    'listitemnum': self._list_count, 'listitem': self._list_item,
                    'event': self._event, 'devmode': self._mode}
        if t[0] in {'get', 'gett', 'getn', 'set', 'sett', 'setn', 'setr'}:
            return self._parameter(t)
        if t[0] in {'prmnum', 'mtrnum'}:
            return f'OK {t[0]} 1000'
        if t[0] in {'scpmode', 'identify'}:
            return 'OK ' + ' '.join(t)
        handler = handlers.get(t[0])
        return handler(t) if handler else f'ERROR {t[0]} UnknownCommand'

    def _status(self, t):
        values = {'runmode': self.get_state('run_mode'), 'error': 'no_error',
                  'power1': 'on', 'power2': 'off'}
        return f'OK devstatus {t[1]} {quote(values[t[1].lower()])}'

    def _info(self, t):
        values = {'productname': self.get_state('model'), 'manufacturer': 'Yamaha',
                  'version': '3.10', 'protocolver': '1.0.0', 'paramsetver': '3.10',
                  'serialno': 'SIMULATOR', 'devicename': 'Simulated DME',
                  'category': 'Processor', 'deviceid': '1', 'inputport': 64,
                  'outputport': 64, 'gpi': 16, 'gpo': 8}
        return f'OK devinfo {t[1]} {quote(values[t[1]])}'

    def _parameter_info(self, t):
        index = int(t[1])
        p = PARAMETERS[index]
        fields = [quote(p[0]), *map(str, p[1:5]), quote(p[5]), quote(p[6]), *map(str, p[7:])]
        return f'OK prminfo {index} ' + ' '.join(fields)

    def _parameter(self, t):
        key = (int(t[1].split('/')[-1]), int(t[2]), int(t[3]))
        spec = PARAMETERS[key[0]]
        old = self._values[key]
        status = 'OK'
        if t[0].startswith('set'):
            if spec[9] == 'r':
                return f'ERROR {t[0]} ReadOnly'
            value, adjusted = self._requested_value(t, spec, old)
            status = 'OKm' if adjusted else 'OK'
            self._values[key] = value
            self._parameter_change(key)
        value = self._values[key]
        address = ' '.join(t[:4])
        if t[0] in {'gett', 'sett'}:
            return f'{status} {address} {quote(self._display(key))}'
        if t[0] in {'getn', 'setn'}:
            value = round((float(value) - spec[3]) / (spec[4] - spec[3]) * 1000)
        formatted = quote(value) if spec[7] == 'string' else str(value)
        suffix = ' ' + quote(self._display(key)) if t[0] in {'set', 'setn'} else ''
        return f'{status} {address} {formatted}{suffix}'

    def _requested_value(self, t, spec, old):
        value = t[4]
        if spec[7] == 'string':
            return value[:spec[4]], len(value) > spec[4]
        if spec[7] == 'binary':
            return f'{int(value, 16):X}', False
        transforms = {'set': lambda: int(value), 'sett': lambda: round(float(value) * spec[10]),
                      'setn': lambda: round(spec[3] + int(value) / 1000 * (spec[4] - spec[3])),
                      'setr': lambda: int(old) + int(value)}
        number = -13801 if value == '-inf' else transforms[t[0]]()
        limited = max(spec[3], min(spec[4], number))
        return limited, limited != number

    def _display(self, key):
        value = self._values[key]
        p = PARAMETERS[key[0]]
        if p[7] in {'string', 'binary'}:
            return str(value)
        return '-inf' if value == -13801 else str(value / p[10])

    def _parameter_change(self, key):
        value = self._values[key]
        ui = {(1, 0, 0): ('gain', lambda v: v / 100), (2, 0, 0): ('on', bool)}
        if key in ui:
            name, convert = ui[key]
            super().set_state(name, convert(value))
        i, x, y = key
        raw = quote(value) if PARAMETERS[i][7] == 'string' else str(value)
        self._notify(f'NOTIFY set PROC:Remote/{i} {x} {y} {raw} {quote(self._display(key))}')

    def _notify(self, line):
        if self.running:
            self._spawn(self._notify_others(wire(line), self._sender))

    async def _notify_others(self, data, sender):
        for client in list(self._clients):
            if client != sender:
                await self.push_to(client, data)

    def set_state(self, key, value):
        previous = self.get_state(key)
        super().set_state(key, value)
        if previous == value or not hasattr(self, '_values'):
            return
        handlers = {'gain': self._ui_gain, 'on': self._ui_on, 'snapshot': self._ui_snapshot,
                    'media': self._ui_media, 'synchronizing': self._ui_sync}
        handler = handlers.get(key)
        if handler:
            handler(value)

    def _ui_gain(self, value):
        self._values[1, 0, 0] = round(float(value) * 100)
        self._parameter_change((1, 0, 0))

    def _ui_on(self, value):
        self._values[2, 0, 0] = int(bool(value))
        self._parameter_change((2, 0, 0))

    def _ui_snapshot(self, value):
        self._values[1, 0, 0] = -600 if int(value) == 1 else -2000
        super().set_state('gain', self._values[1, 0, 0] / 100)
        # Yamaha explicitly does not send parameter notifications for recalls.
        self._notify(f'NOTIFY sscurrent_ex 5000 {int(value)} unmodified')

    def _ui_media(self, value):
        medium = 'sdcard' if self.get_state('model') == 'DME7' else 'usbmemory'
        self._notify(f'NOTIFY event PROC:Media {quote(medium + "=" + ("inserted" if value else "extracted"))}')

    def _ui_sync(self, value):
        self._notify('NOTIFY event PROC:SynchronizationSetStatus ' + quote('active' if value else 'inactive'))

    def _meter_info(self, t):
        if int(t[1]) != 7:
            raise KeyError(t[1])
        return 'OK mtrinfo 7 "Level" 2 1 level'

    def _meter_start(self, t):
        self._meter_info(['mtrinfo', t[1].split('/')[-1].split('>')[0]])
        self._leases[self._sender, t[1]] = (time.monotonic() + 10, 0, max(0.1, int(t[2]) / 1000))
        return 'OK mtrstart ' + t[1]

    def _meter_stop(self, t):
        self._leases.pop((self._sender, t[1]), None)
        return 'OK mtrstop ' + t[1]

    async def _meter_clock(self):
        while self.running:
            now = time.monotonic()
            for key, (expires, last, interval) in list(self._leases.items()):
                client, address = key
                if expires <= now or client not in self._clients:
                    self._leases.pop(key, None)
                elif now - last >= interval:
                    raw = max(0, min(126, int(self.get_state('meter_db')) + 126))
                    raw |= 128 if self.get_state('clip') else 0
                    await self.push_to(client, wire(f'NOTIFY mtr {address} level {raw:02X} {raw:02X}'))
                    self._leases[key] = (expires, now, interval)
            await asyncio.sleep(0.05)

    def _snapshot_current(self, t):
        if t[1] == 'parameter_set':
            return 'OK sscurrent_ex parameter_set 5000 unmodified'
        return f'OK sscurrent_ex {int(t[1])} {self.get_state("snapshot")} unmodified'

    def _snapshot_count(self, t):
        if int(t[1]) != 5000:
            raise KeyError(t[1])
        return 'OK ssnum_ex 100'

    def _snapshot_info(self, t):
        self._snapshot_count(t)
        n = int(t[2])
        attribute = 'user' if n in (1, 2) else 'empty'
        return f'OK ssinfo_ex {t[1]} {n} "{n}" "Snapshot {n}" "" {attribute}'

    def _snapshot_recall(self, t):
        self._snapshot_count(t)
        if int(t[2]) not in (1, 2):
            raise KeyError(t[2])
        self.set_state('snapshot', int(t[2]))
        return 'OK ssrecall_ex ' + ' '.join(t[1:])

    def _list_count(self, t):
        if t[1] not in {'PROC:AudioPlayer', 'PROC:Scheduler'}:
            raise KeyError(t[1])
        return f'OK listitemnum {t[1]} 2 1 2'

    def _list_item(self, t):
        index = int(t[2])
        if t[1] == 'PROC:AudioPlayer':
            p = self._songs[index]
            text = ' '.join(p[k] for k in ('play_type', 'play_mode', 'go_to_top', 'interval', 'name'))
        else:
            p = self._schedules[index]
            text = f'{"enable" if p["enabled"] else "disable"} {p["time"]} {p["name"]}'
        return f'OK listitem {t[1]} {index} {quote(text)}'

    def _mode(self, t):
        if t[1] not in {'normal', 'emergency'}:
            raise ValueError(t[1])
        super().set_state('run_mode', t[1])
        return 'OK devmode ' + t[1]

    def _event(self, t):
        event = t[1].removeprefix('PROC:')
        args = self._event_arguments(t)
        if event.startswith('AudioPlayer') and not self.get_state('media'):
            return f'OKm event {t[1]} "sdcard/usbmemory is not inserted"'
        handlers = {'AudioPlayerGetStatus': self._player_status,
                    'AudioPlayerGetCurrentSong': self._current_song,
                    'AudioPlayerSetCurrentSong': self._select_song,
                    'AudioPlayerTransport': self._transport,
                    'SchedulerSetEnable': self._schedule_enable,
                    'SchedulerSetTime': self._schedule_time,
                    'Language': lambda _: t[2:]}
        if event in EVENT_SONG:
            self._song_edit(event, args)
            result = t[2:]
        elif event in handlers:
            result = handlers[event](args)
        else:
            return 'ERROR event UnknownEventID'
        body = f'event {t[1]} ' + ' '.join(quote(v) for v in result)
        if not event.startswith('AudioPlayerGet'):
            self._notify('NOTIFY ' + body)
        return 'OK ' + body

    @staticmethod
    def _event_arguments(t):
        return dict(v.split('=', 1) for v in t[2:] if '=' in v)

    def _player_status(self, args):
        return ['status=' + self.get_state('player_status')]

    def _current_song(self, args):
        return ['index=' + str(self.get_state('current_song'))]

    def _select_song(self, args):
        index = int(args['index'])
        if index not in self._songs:
            raise KeyError(index)
        super().set_state('current_song', index)
        return self._current_song(args)

    def _transport(self, args):
        operation = args['operation']
        if operation in {'prev', 'next'}:
            super().set_state('current_song', 3 - self.get_state('current_song'))
        elif operation in {'play', 'stop'}:
            super().set_state('player_status', operation)
        else:
            raise ValueError(operation)
        return ['operation=' + operation]

    def _song_edit(self, event, args):
        field, argument = EVENT_SONG[event]
        self._songs[int(args['index'])][field] = args[argument]

    def _schedule_enable(self, args):
        self._schedules[int(args['index'])]['enabled'] = args['enable'] == 'true'
        return [f'{k}={v}' for k, v in args.items()]

    def _schedule_time(self, args):
        self._schedules[int(args['index'])]['time'] = args['time']
        return [f'{k}={v}' for k, v in args.items()]
