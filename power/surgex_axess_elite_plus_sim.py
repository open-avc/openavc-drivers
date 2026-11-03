"""SurgeX Axess ELITE+ — Simulator.

An SNMP agent serving the ESP Axess Elite MIB (``axesselite_v10.mib``,
enterprise 40079) the way an Axess ELITE+ would: the device scalars, the
outlet table, two outlet groups, two sequences, two triggers and an AutoPing
host. Pick the model in the Simulator UI: the 120 V units have nine outlets,
the 240 V units eleven, and the outlet table follows.

What is modelled because a round trip alone would miss it:

  - Switching writes the outlet's status column, and a reboot reads
    ``rebooting`` for the outlet's Reboot Time before it reads ``on``.
  - The outlet Initial States the manual says ignore a command do ignore it:
    outlet 1 is Reboot Only (it will not switch off) and outlet 2 is Always On
    (it will neither switch off nor reboot). The SET is answered either way,
    so a driver that trusts the answer reports a switch that did not happen.
  - An outlet that is off draws nothing, so the per-outlet and total current
    and power fall when outlets switch off.
  - The temperature object is in whichever unit temperatureUnit says, so a
    driver that ignores the unit reports 88 degrees Celsius.

The outlet states, the input fault, the line voltage and the trigger states
are Simulator UI controls, standing in for the unit's own web interface and
for the mains.

What this simulator assumes where SurgeX's documents are silent (each is in
the driver's shipped record as a hardware check): the per-outlet metering
scales (taken from the user manual's CLI screenshots), that the outlet table
holds the outlets and not the two auxiliary relays, what a group's status
reads while its outlets differ (``off`` unless every one is on), and that
writing ``on`` to a sequence's status runs it.

Driver side: ``power/surgex_axess_elite_plus.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time

from openavc.simulator.snmp_simulator import SNMPSimulator

logger = logging.getLogger(__name__)

SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"

AXESSELITE = "1.3.6.1.4.1.40079.1000"
DEVICE = f"{AXESSELITE}.1"
OUTLETS = f"{AXESSELITE}.2.1"
GROUPS = f"{AXESSELITE}.3.1"
SEQUENCES = f"{AXESSELITE}.4.1"
TRIGGERS = f"{AXESSELITE}.5.1"
PING_HOSTS = f"{AXESSELITE}.6.1"

MODELS = {
    # model: (outlets, nominal voltage, voltage limit, line voltage)
    "SX-AEP-159": (9, 120, 130, 121.4),
    "SX-AEP-209": (9, 120, 130, 120.6),
    "SX-AEP-1011i": (11, 230, 240, 229.8),
    "SX-AEP-1611i": (11, 230, 240, 231.2),
}
DEFAULT_MODEL = "SX-AEP-209"
MAX_OUTLETS = 11

INPUT_STATES = [
    "no_fault", "no_ground", "reverse_polarity",
    "over_voltage_line_neutral", "under_voltage_line_neutral",
    "over_voltage_neutral_ground", "under_voltage_neutral_ground",
    "overload", "over_temperature", "under_temperature", "over_humidity",
    "under_humidity", "over_frequency", "under_frequency",
]
STATUS_CODE = {"on": 1, "off": 2, "rebooting": 3}
CODE_STATUS = {code: word for word, code in STATUS_CODE.items()}

# outletInitialState codes. Outlet 1 Reboot Only, outlet 2 Always On, the rest
# Last (the factory default).
REBOOT_ONLY, ALWAYS_ON, ALWAYS_OFF, LAST = 6, 0, 1, 5
INITIAL_STATE = {1: REBOOT_ONLY, 2: ALWAYS_ON}

# What each outlet draws while on, in hundredths of an amp at 120 V. Uneven so
# a driver that reads the wrong row notices. Outlet 1's 0.11 A is the manual's
# CLI screenshot reading.
DRAW_CENTIAMPS = {1: 11, 2: 46, 3: 182, 4: 175, 5: 168, 6: 92, 7: 88,
                  8: 23, 9: 7, 10: 31, 11: 54}
POWER_FACTOR_MILLI = 920
CREST_MILLI = 1414

GROUP_MEMBERS = {1: ("Amplifiers", [3, 4, 5]), 2: ("Displays", [6, 7])}
SEQUENCE_STEPS = {
    0: ("System On", "on", [3, 4, 5, 6, 7]),
    1: ("System Off", "off", [7, 6, 5, 4, 3]),
}
SEQUENCE_STEP_SECONDS = 0.5
TRIGGER_DEFS = {0: ("Rack Temp High", "Threshold"), 1: ("Codec Ping", "AutoPing")}


def _initial_state() -> dict:
    state: dict = {
        "model": DEFAULT_MODEL,
        "input_state": "no_fault",
        "line_voltage": MODELS[DEFAULT_MODEL][3],
        "temperature_unit": "F",
        "trigger_0_triggered": False,
        "trigger_1_triggered": False,
    }
    for n in range(1, MAX_OUTLETS + 1):
        state[f"outlet_{n}"] = "on" if n <= 8 else "off"
    return state


def _controls() -> list:
    controls: list = [
        {"type": "select", "key": "model", "label": "Model",
         "options": list(MODELS)},
        {"type": "select", "key": "input_state", "label": "Input Fault",
         "options": INPUT_STATES},
        {"type": "slider", "key": "line_voltage", "label": "Line Voltage",
         "min": 80, "max": 280, "step": 0.1, "unit": "V"},
        {"type": "select", "key": "temperature_unit", "label": "Temperature Unit",
         "options": ["F", "C"]},
        {"type": "toggle", "key": "trigger_0_triggered",
         "label": "Rack Temp High triggered"},
        {"type": "toggle", "key": "trigger_1_triggered",
         "label": "Codec Ping triggered"},
    ]
    outlets = [
        {"type": "select", "key": f"outlet_{n}", "label": f"Outlet {n}",
         "options": ["on", "off", "rebooting"]}
        for n in range(1, MAX_OUTLETS + 1)
    ]
    controls.append({"type": "group", "label": "Outlets", "controls": outlets})
    return controls


class SurgeXAxessElitePlusSimulator(SNMPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "surgex_axess_elite_plus",
        "name": "SurgeX Axess ELITE+ Simulator",
        "category": "power",
        "transport": "snmp",
        "default_port": 161,
        "initial_state": _initial_state(),
        "controls": _controls(),
        "delays": {"command_response": 0.002},
    }

    READ_COMMUNITY = "public"
    WRITE_COMMUNITY = "private"

    def __init__(self, device_id: str, config: dict | None = None):
        self._ready = False
        super().__init__(device_id, config)
        self._booted = time.monotonic()
        self._energy_wh = 1834.0
        self._energy_at = time.monotonic()
        self._energy_since = "11/03/2026 08:00:00"
        self._temperature_c = 31.4
        self._handles: list[asyncio.TimerHandle] = []
        self._ping_successes = 0
        self.oids = self._build_mib()
        self._ready = True

    # ── The MIB ──────────────────────────────────────────────────────────

    def _model(self) -> str:
        model = self.get_state("model", DEFAULT_MODEL)
        return model if model in MODELS else DEFAULT_MODEL

    def _outlet_count(self) -> int:
        return MODELS[self._model()][0]

    def _build_mib(self) -> dict[str, tuple]:
        """Every OID the unit serves. Values that move are computed in
        read_oid; an entry here is what makes an OID exist and be writable."""
        model = self._model()
        count, nominal, limit, _line = MODELS[model]
        oids: dict[str, tuple] = {
            SYS_DESCR: ("string", f"SurgeX Axess ELITE+ {model}"),
            SYS_OBJECT_ID: ("oid", AXESSELITE),
            SYS_UPTIME: ("timeticks", 0),
            SYS_NAME: ("string", "axess-elite-plus"),
            f"{DEVICE}.1.0": ("string", "Axess Elite+"),
            f"{DEVICE}.2.0": ("string", "1.01.27844"),
            f"{DEVICE}.3.0": ("integer", nominal, True),
            f"{DEVICE}.4.0": ("string", "SurgeX"),
            f"{DEVICE}.5.0": ("string", model),
            f"{DEVICE}.6.0": ("string", "11/03/2026 14:05:12"),
            f"{DEVICE}.7.0": ("integer", 0),
            f"{DEVICE}.8.0": ("integer", 5),
            f"{DEVICE}.9.0": ("integer", 1),
            f"{DEVICE}.10.0": ("integer", 0),
            f"{DEVICE}.11.0": ("integer", 3450),
            f"{DEVICE}.12.0": ("integer", 0),
            f"{DEVICE}.13.0": ("integer", 0),
            f"{DEVICE}.14.0": ("integer", POWER_FACTOR_MILLI),
            f"{DEVICE}.15.0": ("integer", 0),
            f"{DEVICE}.16.0": ("integer", CREST_MILLI),
            f"{DEVICE}.17.0": ("integer", 0),
            f"{DEVICE}.18.0": ("integer", 59989 if nominal == 120 else 49989),
            f"{DEVICE}.19.0": ("integer", 0),
            f"{DEVICE}.20.0": ("string", ""),
            f"{DEVICE}.21.0": ("integer", 1, True),
            f"{DEVICE}.22.0": ("integer", 1, True),
            f"{DEVICE}.23.0": ("integer", 1, True),
            f"{DEVICE}.24.0": ("integer", 1, True),
            f"{DEVICE}.25.0": ("integer", 60 if nominal == 120 else 50, True),
            f"{DEVICE}.26.0": ("integer", limit, True),
            f"{DEVICE}.27.0": ("string", "Axess ELITE+"),
        }
        for n in range(1, count + 1):
            oids.update({
                f"{OUTLETS}.1.{n}": ("integer", n),
                f"{OUTLETS}.2.{n}": ("string", str(n)),
                f"{OUTLETS}.3.{n}": ("string", f"Outlet {n}"),
                f"{OUTLETS}.4.{n}": ("integer", 1, True),
                f"{OUTLETS}.5.{n}": ("integer", INITIAL_STATE.get(n, LAST)),
                f"{OUTLETS}.6.{n}": ("integer", 1),
                f"{OUTLETS}.7.{n}": ("integer", 5),
                f"{OUTLETS}.8.{n}": ("integer", 0),
                f"{OUTLETS}.9.{n}": ("integer", 0),
                f"{OUTLETS}.10.{n}": ("integer", 0),
                f"{OUTLETS}.11.{n}": ("integer", 0),
                f"{OUTLETS}.12.{n}": ("integer", 0),
            })
        for g, (name, _members) in GROUP_MEMBERS.items():
            oids.update({
                f"{GROUPS}.1.{g}": ("integer", g),
                f"{GROUPS}.2.{g}": ("string", f"G{g}"),
                f"{GROUPS}.3.{g}": ("string", name),
                f"{GROUPS}.4.{g}": ("integer", 1, True),
            })
        for s, (name, _action, _steps) in SEQUENCE_STEPS.items():
            oids.update({
                f"{SEQUENCES}.1.{s}": ("integer", s),
                f"{SEQUENCES}.2.{s}": ("string", f"S{s}"),
                f"{SEQUENCES}.3.{s}": ("string", name),
                f"{SEQUENCES}.4.{s}": ("integer", 0, True),
            })
        for t, (name, kind) in TRIGGER_DEFS.items():
            oids.update({
                f"{TRIGGERS}.1.{t}": ("integer", t),
                f"{TRIGGERS}.2.{t}": ("string", f"T{t}"),
                f"{TRIGGERS}.3.{t}": ("string", name),
                f"{TRIGGERS}.4.{t}": ("string", kind),
                f"{TRIGGERS}.5.{t}": ("integer", 1),
                f"{TRIGGERS}.6.{t}": ("integer", 0),
            })
        oids.update({
            f"{PING_HOSTS}.1.0": ("integer", 0),
            f"{PING_HOSTS}.2.0": ("string", "192.168.1.50"),
            f"{PING_HOSTS}.3.0": ("integer", 0),
            f"{PING_HOSTS}.4.0": ("integer", 0),
            f"{PING_HOSTS}.5.0": ("string", ""),
            f"{PING_HOSTS}.6.0": ("string", ""),
        })
        return oids

    def set_state(self, key, value) -> None:
        super().set_state(key, value)
        if key == "model" and getattr(self, "_ready", False):
            # A different model has a different outlet table, and a 240 V
            # model sits on 240 V mains.
            self.oids = self._build_mib()
            super().set_state("line_voltage", MODELS[self._model()][3])

    # ── Computed values ──────────────────────────────────────────────────

    def read_oid(self, oid: str):
        if oid == SYS_UPTIME:
            return "timeticks", int((time.monotonic() - self._booted) * 100)
        if oid.startswith(DEVICE + "."):
            value = self._device_value(int(oid.split(".")[-2]))
            if value is not None and oid in self.oids:
                return self.oids[oid][0], value
        elif oid.startswith(OUTLETS + "."):
            column, index = (int(p) for p in oid.rsplit(".", 2)[-2:])
            value = self._outlet_value(column, index)
            if value is not None and oid in self.oids:
                return "integer", value
        elif oid.startswith(GROUPS + ".4."):
            if oid in self.oids:
                return "integer", self._group_status(int(oid.rsplit(".", 1)[-1]))
        elif oid.startswith(TRIGGERS + ".6."):
            if oid in self.oids:
                index = oid.rsplit(".", 1)[-1]
                return "integer", int(bool(self.get_state(f"trigger_{index}_triggered")))
        elif oid.startswith(PING_HOSTS + "."):
            value = self._ping_value(int(oid.split(".")[-2]))
            if value is not None and oid in self.oids:
                return self.oids[oid][0], value
        return super().read_oid(oid)

    def _status(self, n: int) -> str:
        status = self.get_state(f"outlet_{n}", "off")
        return status if status in STATUS_CODE else "off"

    def _draw(self, n: int) -> int:
        """Hundredths of an amp, scaled to the line voltage the model runs on."""
        if self._status(n) != "on":
            return 0
        nominal = MODELS[self._model()][1]
        return int(round(DRAW_CENTIAMPS.get(n, 10) * 120 / nominal))

    def _watts(self, n: int) -> int:
        line = float(self.get_state("line_voltage", 120.0))
        return int(round(self._draw(n) / 100 * line * POWER_FACTOR_MILLI / 1000))

    def _total_watts(self) -> int:
        return sum(self._watts(n) for n in range(1, self._outlet_count() + 1))

    def _device_value(self, number: int):
        if number == 7:
            name = self.get_state("input_state", "no_fault")
            return INPUT_STATES.index(name) if name in INPUT_STATES else 0
        if number == 10:
            return int(round(float(self.get_state("line_voltage", 120.0)) * 1000))
        if number == 12:
            return sum(self._draw(n) for n in range(1, self._outlet_count() + 1))
        if number == 13:
            return self._total_watts()
        if number == 17:
            celsius = self._temperature_c
            if self.get_state("temperature_unit", "F") == "F":
                return int(round((celsius * 9 / 5 + 32) * 10))
            return int(round(celsius * 10))
        if number == 19:
            now = time.monotonic()
            self._energy_wh += self._total_watts() * (now - self._energy_at) / 3600
            self._energy_at = now
            return int(self._energy_wh)
        if number == 20:
            return self._energy_since
        if number == 24:
            return 2 if self.get_state("temperature_unit", "F") == "C" else 1
        return None

    def _outlet_value(self, column: int, n: int):
        if n > self._outlet_count():
            return None
        if column == 4:
            return STATUS_CODE[self._status(n)]
        if column == 6:
            return 0 if self._status(n) == "off" else 1
        on = self._status(n) == "on"
        if column == 8:
            return self._draw(n)
        if column == 9:
            return self._watts(n)
        if column == 10:
            return POWER_FACTOR_MILLI if on else 0
        if column == 11:
            return CREST_MILLI if on else 0
        if column == 12:
            return int(self._draw(n) * CREST_MILLI / 1000)
        return None

    def _group_status(self, g: int) -> int:
        members = [n for n in GROUP_MEMBERS[g][1] if n <= self._outlet_count()]
        states = [self._status(n) for n in members]
        if any(s == "rebooting" for s in states):
            return STATUS_CODE["rebooting"]
        if states and all(s == "on" for s in states):
            return STATUS_CODE["on"]
        return STATUS_CODE["off"]

    def _ping_value(self, column: int):
        # The unit pings its host on its own clock; one answered ping per
        # read keeps the count moving in the Simulator UI.
        if column == 3:
            self._ping_successes += 1
            return self._ping_successes
        if column == 5 and self._ping_successes:
            return time.strftime("%m/%d/%Y %H:%M:%S")
        return None

    # ── Writes ───────────────────────────────────────────────────────────

    def write_oid(self, oid: str, type_name: str, value):
        status = super().write_oid(oid, type_name, value)
        if status != 0:
            return status
        code = int(value)
        if oid.startswith(OUTLETS + ".4."):
            self._switch(int(oid.rsplit(".", 1)[-1]), code)
        elif oid.startswith(GROUPS + ".4."):
            g = int(oid.rsplit(".", 1)[-1])
            for n in GROUP_MEMBERS[g][1]:
                if n <= self._outlet_count():
                    self._switch(n, code)
        elif oid.startswith(SEQUENCES + ".4.") and code == 1:
            self._run_sequence(int(oid.rsplit(".", 1)[-1]))
        elif oid == f"{DEVICE}.21.0" and code == 2:
            self._energy_wh = 0.0
            self._energy_at = time.monotonic()
            self._energy_since = time.strftime("%m/%d/%Y %H:%M:%S")
            self.oids[oid] = ("integer", 1, True)
        elif oid == f"{DEVICE}.23.0" and code == 2:
            logger.info("Axess ELITE+ controller restart requested (simulated)")
            self.oids[oid] = ("integer", 1, True)
        elif oid == f"{DEVICE}.24.0" and code in (1, 2):
            self.set_state("temperature_unit", "C" if code == 2 else "F")
        return 0

    def _switch(self, n: int, code: int) -> None:
        """Apply on(1), off(2) or rebooting(3) to one outlet, the way its
        Initial State allows (B01-00014 Rev A, Outlet Configuration)."""
        initial = INITIAL_STATE.get(n, LAST)
        word = CODE_STATUS.get(code)
        if word is None:
            return
        if initial == ALWAYS_ON and word in ("off", "rebooting"):
            return
        if initial == REBOOT_ONLY and word == "off":
            return
        if initial == ALWAYS_OFF and word in ("on", "rebooting"):
            return
        if word != "rebooting":
            self.set_state(f"outlet_{n}", word)
            return
        self.set_state(f"outlet_{n}", "rebooting")
        reboot_time = int(self.oids.get(f"{OUTLETS}.7.{n}", ("integer", 5))[1])
        self._later(reboot_time, lambda: self.set_state(f"outlet_{n}", "on"))

    def _run_sequence(self, s: int) -> None:
        _name, action, steps = SEQUENCE_STEPS[s]
        code = STATUS_CODE[action]
        for position, n in enumerate(steps, start=1):
            if n <= self._outlet_count():
                self._later(position * SEQUENCE_STEP_SECONDS,
                            lambda n=n: self._switch(n, code))

    def _later(self, delay: float, callback) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            callback()
            return
        self._handles.append(loop.call_later(delay, callback))

    async def stop(self) -> None:
        for handle in self._handles:
            handle.cancel()
        self._handles.clear()
        await super().stop()
