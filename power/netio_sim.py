"""NETIO (JSON M2M API) simulator.

Serves ``/netio.json`` the way a NETIO unit does:

  GET  /netio.json   the status document (Agent, GlobalMeasure, Outputs,
                     Inputs, PAB, Watchdogs, Rules)
  POST /netio.json   a partial document with output changes; answered with
                     the whole status document again

The model is a choice in the Simulator UI. It sets how many outputs and inputs
there are and which of them are metered, laid out the way NETIO's public demo
units answered on firmware 5.3.1 and 5.3.2 (a K model meters every output and
reports each one's voltage and frequency; an 8QS meters the whole unit and
output 1; a PowerDIN 4PZ outputs 1 and 2; a P model none).

Logins: off by default, so a simulated unit answers whatever is entered (a
real unit can read and write with no login, and the demo units read with
none). Switch "Login required to read" or "to write" on to model NETIO's
factory setup; then no login, or the login ``invalid``, is refused with 401.

Refusals: writing switched off answers 403; the JSON API switched off
answers 404; a malformed or impossible write answers 400 with the error list
and no enclosing braces, as the demo units do. The web server restart that
follows saving the JSON API settings is the ``web_server_restarting`` error
mode (500 to everything).
"""

import asyncio
import base64
import json
import time
from datetime import datetime, timezone

from openavc.simulator.http_simulator import HTTPSimulator

# model -> (outputs, inputs, metering)
#   "k"    every output metered, with its own voltage and frequency
#   "q"    the whole unit and output 1
#   "din"  the whole unit and outputs 1-2; 3-4 are relay contacts
#   "none" no metering at all
MODELS = {
    "4KS": (4, 1, "k"),
    "4KB": (4, 1, "k"),
    "8KS": (8, 1, "k"),
    "8KB": (8, 1, "k"),
    "4KF": (4, 0, "k"),
    "FK6": (6, 2, "k"),
    "8QS": (8, 1, "q"),
    "4PS": (4, 0, "none"),
    "2PZ": (2, 0, "none"),
    "4PZ": (4, 2, "din"),
}

VALID_ACTIONS = {0, 1, 2, 3, 4, 5, 6, 20, 21}
MAX_OUTPUTS = 8
SENTINEL = 999.0


def _output_controls():
    groups = []
    for n in range(1, MAX_OUTPUTS + 1):
        groups.append({
            "type": "group",
            "label": f"Output {n}",
            "controls": [
                {"type": "toggle", "key": f"out{n}_on", "label": "On"},
                {"type": "slider", "key": f"out{n}_load_w", "label": "Load when on",
                 "min": 0, "max": 2000, "step": 5, "unit": "W"},
            ],
        })
    return groups


class NetioSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "netio",
        "name": "NETIO Simulator",
        "category": "power",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "model": "4KS",
            "device_name": "PowerPDU-SIM",
            "firmware": "5.3.1",
            "voltage": 230.0,
            "json_api_enabled": True,
            "read_login_required": False,
            "write_login_required": False,
            "write_enabled": True,
            "sensor_connected": False,
            "in1_temperature_c": 22.5,
            "in1_humidity": 40.0,
            "watchdog_failed": False,
            "rule_result": True,
            "out1_on": True,
            "out1_name": "Power output 1",
            "out1_load_w": 40.0,
            "out1_delay_ms": 5000,
            "out1_energy_wh": 0.0,
            "out1_energy_nr_wh": 100.0,
            "out1_energy_start": "1970-01-01T00:00:00+0000",
            "out1_schedule": False,
            "out2_on": True,
            "out2_name": "Power output 2",
            "out2_load_w": 40.0,
            "out2_delay_ms": 5000,
            "out2_energy_wh": 0.0,
            "out2_energy_nr_wh": 200.0,
            "out2_energy_start": "1970-01-01T00:00:00+0000",
            "out2_schedule": False,
            "out3_on": True,
            "out3_name": "Power output 3",
            "out3_load_w": 0.0,
            "out3_delay_ms": 5000,
            "out3_energy_wh": 0.0,
            "out3_energy_nr_wh": 300.0,
            "out3_energy_start": "1970-01-01T00:00:00+0000",
            "out3_schedule": False,
            "out4_on": True,
            "out4_name": "Power output 4",
            "out4_load_w": 0.0,
            "out4_delay_ms": 5000,
            "out4_energy_wh": 0.0,
            "out4_energy_nr_wh": 400.0,
            "out4_energy_start": "1970-01-01T00:00:00+0000",
            "out4_schedule": False,
            "out5_on": True,
            "out5_name": "Power output 5",
            "out5_load_w": 0.0,
            "out5_delay_ms": 5000,
            "out5_energy_wh": 0.0,
            "out5_energy_nr_wh": 500.0,
            "out5_energy_start": "1970-01-01T00:00:00+0000",
            "out5_schedule": False,
            "out6_on": True,
            "out6_name": "Power output 6",
            "out6_load_w": 0.0,
            "out6_delay_ms": 5000,
            "out6_energy_wh": 0.0,
            "out6_energy_nr_wh": 600.0,
            "out6_energy_start": "1970-01-01T00:00:00+0000",
            "out6_schedule": False,
            "out7_on": True,
            "out7_name": "Power output 7",
            "out7_load_w": 0.0,
            "out7_delay_ms": 5000,
            "out7_energy_wh": 0.0,
            "out7_energy_nr_wh": 700.0,
            "out7_energy_start": "1970-01-01T00:00:00+0000",
            "out7_schedule": False,
            "out8_on": True,
            "out8_name": "Power output 8",
            "out8_load_w": 0.0,
            "out8_delay_ms": 5000,
            "out8_energy_wh": 0.0,
            "out8_energy_nr_wh": 800.0,
            "out8_energy_start": "1970-01-01T00:00:00+0000",
            "out8_schedule": False,
            "in1_closed": False,
            "in1_s0": 0,
            "in2_closed": False,
            "in2_s0": 0,
        },
        "delays": {"command_response": 0.02},
        "controls": [
            {
                "type": "select", "key": "model", "label": "Model",
                "options": list(MODELS),
            },
            {"type": "toggle", "key": "json_api_enabled", "label": "JSON API enabled"},
            {"type": "toggle", "key": "write_enabled", "label": "Writing enabled"},
            {"type": "toggle", "key": "read_login_required", "label": "Login required to read"},
            {"type": "toggle", "key": "write_login_required", "label": "Login required to write"},
            {
                "type": "group",
                "label": "Inputs",
                "controls": [
                    {"type": "toggle", "key": "in1_closed", "label": "Input 1 closed"},
                    {"type": "toggle", "key": "in2_closed", "label": "Input 2 closed"},
                    {"type": "toggle", "key": "sensor_connected", "label": "Sensor on input 1"},
                    {"type": "slider", "key": "in1_temperature_c", "label": "Temperature",
                     "min": -20, "max": 80, "step": 0.5, "unit": "°C"},
                    {"type": "slider", "key": "in1_humidity", "label": "Humidity",
                     "min": 0, "max": 100, "step": 1, "unit": "%"},
                ],
            },
            {"type": "toggle", "key": "watchdog_failed", "label": "Watchdog check failing"},
            {"type": "toggle", "key": "rule_result", "label": "Rule result"},
            *_output_controls(),
        ],
        "error_modes": {
            "web_server_restarting": {
                "description": "The web server restarts after the JSON API settings are saved and "
                               "answers HTTP 500 meanwhile",
                "behavior": "custom",
            },
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._started = time.monotonic()
        self._metered_at = time.monotonic()
        # output -> (monotonic deadline, on afterwards) for a short on/off in progress
        self._pending = {}

    # ── Model ──

    def _model(self):
        return MODELS.get(str(self.get_state("model", "4KS")), MODELS["4KS"])

    def _output_metered(self, n):
        outputs, _inputs, metering = self._model()
        if metering == "k":
            return True
        if metering == "q":
            return n == 1
        if metering == "din":
            return n in (1, 2)
        return False

    # ── Requests ──

    def handle_request(self, method, path, headers, body):
        if "web_server_restarting" in self.active_errors:
            return 500, '"errors":[{"code":500,"message":"Internal Server Error"}]'
        clean = path.split("?")[0]
        if method == "HEAD":
            return 200, ""
        if clean != "/netio.json":
            if method == "GET" and clean in ("", "/"):
                return 200, "<!DOCTYPE html><title>PDU Web Config</title>"
            return 404, "Not Found"
        if not self.get_state("json_api_enabled", True):
            return 404, '"errors":[{"code":404,"message":"Not found"}]'
        login = self._login(headers)
        self._settle()
        if method == "GET":
            if self.get_state("read_login_required", False) and not login:
                return self._unauthorized()
            return 200, self._document()
        if method == "POST":
            if self.get_state("write_login_required", False) and not login:
                return self._unauthorized()
            if not self.get_state("write_enabled", True):
                return 403, '"errors":[{"code":403,"message":"Forbidden"}]'
            return self._write(body)
        return 405, "Method Not Allowed"

    @staticmethod
    def _unauthorized():
        # As the demo units answer (text/html there).
        return 401, "401 Unauthorized", {"WWW-Authenticate": 'Basic realm="Login Required"'}

    @staticmethod
    def _login(headers):
        """True for a Basic login other than the designated bad one, ``invalid``."""
        value = ""
        for key, val in headers.items():
            if key.lower() == "authorization":
                value = str(val)
        if not value.lower().startswith("basic "):
            return False
        try:
            user, _, password = base64.b64decode(value[6:].strip()).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return user != "invalid" and password != "invalid"

    def _write(self, body):
        bad = (400, '"errors":[{"code":400,"message":"Bad request"}]')
        try:
            doc = json.loads(body or "")
        except ValueError:
            return bad
        entries = doc.get("Outputs") if isinstance(doc, dict) else None
        if not isinstance(entries, list) or not entries:
            return bad
        outputs = self._model()[0]
        checked = []
        for entry in entries:
            if not isinstance(entry, dict):
                return bad
            n = entry.get("ID")
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= outputs:
                return bad
            action = entry.get("Action", 6)
            if isinstance(action, bool) or not isinstance(action, int) or action not in VALID_ACTIONS:
                return bad
            delay = entry.get("Delay")
            if delay is not None and (isinstance(delay, bool) or not isinstance(delay, int) or delay <= 0):
                return bad
            checked.append((n, action, delay, entry))
        for n, action, delay, entry in checked:
            self._apply(n, action, delay, entry)
        # A unit answers a switch with a shorter status than a read (see
        # _document); a rename or counter reset gets the full one.
        switched = any(action in (0, 1, 2, 3, 4) for _n, action, _d, _e in checked)
        return 200, self._document(after_switch=switched)

    def _apply(self, n, action, delay, entry):
        if "Name" in entry and str(entry["Name"]).strip():
            self.set_state(f"out{n}_name", str(entry["Name"]))
        if entry.get("ResetEnergyCounter") is True:
            self._meter()
            self.set_state(f"out{n}_energy_wh", 0.0)
            self.set_state(f"out{n}_energy_start", self._now())
        key = f"out{n}_on"
        if action == 0:
            self.set_state(key, False)
        elif action == 1:
            self.set_state(key, True)
        elif action == 4:
            self.set_state(key, not self.get_state(key, False))
        elif action in (2, 3):
            # Short off / short on: switch now, switch back after the time
            # given with the command, else the output's own.
            ms = delay or int(self.get_state(f"out{n}_delay_ms", 5000))
            self.set_state(key, action == 3)
            self._schedule_back(n, action == 2, ms / 1000.0)
        elif action == 20:
            self.set_state(f"out{n}_schedule", False)
        elif action == 21:
            self.set_state(f"out{n}_schedule", True)
        elif action == 6 and "State" in entry:
            self.set_state(key, entry["State"] == 1)

    # ── Short on/off timing ──

    def _schedule_back(self, n, final_on, seconds):
        self._pending[n] = (time.monotonic() + seconds, final_on)
        try:
            asyncio.get_running_loop().call_later(seconds, self._settle)
        except RuntimeError:
            pass  # no loop running: the next request settles it

    def _settle(self):
        now = time.monotonic()
        for n, (deadline, final_on) in list(self._pending.items()):
            if now >= deadline:
                del self._pending[n]
                self.set_state(f"out{n}_on", bool(final_on))

    # ── Document ──

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

    def _meter(self):
        """Count energy since the last reading: each metered output on draws its load."""
        now = time.monotonic()
        hours = (now - self._metered_at) / 3600.0
        self._metered_at = now
        if hours <= 0:
            return
        for n in range(1, MAX_OUTPUTS + 1):
            if self.get_state(f"out{n}_on") and self._output_metered(n):
                wh = float(self.get_state(f"out{n}_load_w", 0.0)) * hours
                self._state[f"out{n}_energy_wh"] = float(self.get_state(f"out{n}_energy_wh", 0.0)) + wh
                self._state[f"out{n}_energy_nr_wh"] = float(self.get_state(f"out{n}_energy_nr_wh", 0.0)) + wh

    def _load(self, n):
        if not self.get_state(f"out{n}_on"):
            return 0.0
        return float(self.get_state(f"out{n}_load_w", 0.0))

    def _document(self, after_switch=False):
        """The status document. ``after_switch`` renders the answer to a switch
        the way the 4KS demo (5.3.2) sends it: each metered output without its
        own Voltage, Frequency, EnergyNR, ReverseEnergyNR and EnergyStart, and
        PowerFactor 0 where a read says 1.0 for an idle output."""
        self._meter()
        outputs, inputs, metering = self._model()
        voltage = float(self.get_state("voltage", 230.0))
        model = str(self.get_state("model", "4KS"))
        doc = {
            "Agent": {
                "Model": model,
                "DeviceName": str(self.get_state("device_name", "PowerPDU-SIM")),
                "MAC": "24:A4:2C:00:00:01",
                "SerialNumber": "24A42C000001",
                "JSONVer": "2.6",
                "Time": self._now(),
                "Uptime": int(time.monotonic() - self._started),
                "Version": str(self.get_state("firmware", "5.3.1")),
                "OemID": 700,
                "VendorID": 0,
                "NumOutputs": outputs,
                "NumInputs": inputs,
            },
        }
        if metering != "none":
            if metering == "q":
                total_load = sum(self._load(n) for n in range(1, outputs + 1))
            else:
                total_load = sum(self._load(n) for n in range(1, outputs + 1) if self._output_metered(n))
            total_ma = int(round(total_load / voltage * 1000)) if voltage else 0
            pf = 0.95 if total_load else SENTINEL
            measure = {
                "Voltage": round(voltage, 2),
                "TotalCurrent": total_ma,
                "OverallPowerFactor": pf,
                "TotalPowerFactor": pf,
                "OverallPhase": 0.0 if total_load else SENTINEL,
                "TotalPhase": 0.0 if total_load else SENTINEL,
            }
            # The 8QS and PowerDIN demo units leave Frequency out of this block.
            if metering == "k":
                measure["Frequency"] = 50.0
            energy = sum(float(self.get_state(f"out{n}_energy_wh", 0.0)) for n in range(1, outputs + 1)
                         if self._output_metered(n))
            energy_nr = sum(float(self.get_state(f"out{n}_energy_nr_wh", 0.0)) for n in range(1, outputs + 1)
                            if self._output_metered(n))
            measure.update({
                "TotalEnergy": int(energy),
                "TotalReverseEnergy": 0,
                "TotalEnergyNR": int(energy_nr),
                "TotalReverseEnergyNR": 0,
                "TotalLoad": int(round(total_load)),
                "EnergyStart": "1970-01-01T00:00:00+0000",
            })
            doc["GlobalMeasure"] = measure
        out_list = []
        for n in range(1, outputs + 1):
            entry = {
                "ID": n,
                "Name": str(self.get_state(f"out{n}_name", f"Power output {n}")),
                "State": 1 if self.get_state(f"out{n}_on") else 0,
                "Action": 6,
                "Delay": int(self.get_state(f"out{n}_delay_ms", 5000)),
            }
            if self._output_metered(n):
                load = self._load(n)
                if after_switch:
                    entry.update({
                        "Current": int(round(load / voltage * 1000)) if voltage else 0,
                        "Load": int(round(load)),
                        "PowerFactor": 0.95 if load else 0,
                        "Phase": 0,
                        "ReverseEnergy": 0,
                        "Energy": int(float(self.get_state(f"out{n}_energy_wh", 0.0))),
                    })
                    out_list.append(entry)
                    continue
                if metering == "k":
                    entry["Voltage"] = round(voltage, 2)
                entry.update({
                    "Current": int(round(load / voltage * 1000)) if voltage else 0,
                    "PowerFactor": 0.95 if load else 1.0,
                    "Phase": 0.0,
                    "Energy": int(float(self.get_state(f"out{n}_energy_wh", 0.0))),
                    "ReverseEnergy": 0,
                    "EnergyNR": int(float(self.get_state(f"out{n}_energy_nr_wh", 0.0))),
                    "ReverseEnergyNR": 0,
                    "Load": int(round(load)),
                })
                if metering == "k":
                    entry["Frequency"] = 50.0
                entry["EnergyStart"] = str(self.get_state(f"out{n}_energy_start", "1970-01-01T00:00:00+0000"))
            out_list.append(entry)
        doc["Outputs"] = out_list
        if inputs:
            in_list = []
            for n in range(1, inputs + 1):
                sensor = n == 1 and bool(self.get_state("sensor_connected"))
                temp_c = float(self.get_state("in1_temperature_c", 22.5)) if sensor else SENTINEL
                in_list.append({
                    "ID": n,
                    "Name": f"Input {n}",
                    "State": 1 if self.get_state(f"in{n}_closed") else 0,
                    "S0Counter": int(self.get_state(f"in{n}_s0", 0)),
                    "TemperatureC": round(temp_c, 1),
                    "TemperatureF": round(temp_c * 9 / 5 + 32, 1) if sensor else SENTINEL,
                    "Humidity": int(self.get_state("in1_humidity", 40.0)) if sensor else 999,
                })
            doc["Inputs"] = in_list
        failed = bool(self.get_state("watchdog_failed"))
        doc["PAB"] = [
            {"Type": "RANGE", "Name": "CR03_PAB", "Enabled": True, "In": self._load(1) > 0},
            {"Type": "ZONES", "Name": "Falcon", "Enabled": False, "Zone": 0},
        ]
        doc["Watchdogs"] = [
            {"Name": "google_pinger", "Enabled": True, "Fail": failed, "LastStatus": not failed,
             "Timestamp": int(time.time())},
            {"Name": "CR01_WDT", "Enabled": False, "Fail": False, "LastStatus": False, "Timestamp": 0},
        ]
        doc["Rules"] = [
            {"Name": "CR01_RULE", "Enabled": True, "Result": bool(self.get_state("rule_result"))},
            {"Name": "CR02_RULE", "Enabled": False, "Result": None},
            {"Name": "2 OFFafter10Sec", "Enabled": False, "Result": None},
        ]
        return doc
