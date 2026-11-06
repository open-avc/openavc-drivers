"""
GUDE Expert Power Control 8291 -- Simulator.

Serves the unit's HTTP interface the way the EPC HTTP Interface and HTTP JSON
Sensor Data wiki pages and the 8291-1 manual (v1.5.1) describe it:

  - ``status.json`` (and ``statusjsn.js``) with the ``components`` bits the
    driver asks for: outputs, ethernet, misc, port_summary, hardware,
    sensor_values and sensor_descr, the group sensors (system, banks) only
    with the "extended" bit.
  - The CGI commands on the same request: switch (``cmd=1``, ``s`` 0 / 1 / 2
    toggle), the batch mode (``cmd=5``), cancelling it (``cmd=2``), the Reset
    power cycle (``cmd=12``) and restarting the controller (``cmd=39``).
  - Sign-in, the three Authentication Modes of manual section 4.4: Basic
    Compatible (HTTP Basic or a session), Session (a session only, switching
    only as a POST) and Session Extended (the ``sessionidX`` CSRF token as a
    header too), the session from ``login.json``. Any user and password sign
    in except the designated bad one, ``invalid``. "Firmware without
    sessions" answers ``login.json`` with 404 and takes HTTP Basic, and
    "HTTP redirects to HTTPS" answers every request 301 to ``https://``, as
    the HTTP server's redirect mode does.
  - The ports switch one at a time, at least one second apart (manual 3.1),
    so a burst of commands settles over a few seconds.
  - A DC port's "Overload" trips its eFuse while it is on: the port switches
    off and reports eFuse state 2; switching it on resets the eFuse, which
    trips again at once while the overload lasts (manual 1.5).

The 8291-2's per-port DC metering (sensor type 17) is served only with the
8291-2 model selected, and its layout here (one property per DC port, fields
Voltage / Current / Power) is NOT documented anywhere: GUDE's wiki names the
type and nothing more. It is here so the driver's generic meter path can be
run; it says nothing about the real unit.

Simplified: the "user" account may read and switch and not restart the
controller (manual 3.2.3: switching is a user's, settings the admin's; the
refusal's form is not documented, a 403 here); the power and energy readings
are a fixed efficiency over the port loads; a restart answers 503 for three
seconds.
"""

from __future__ import annotations

import base64
import math
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from openavc.simulator.http_simulator import HTTPSimulator

MODELS = {
    "8291-2": "Expert Power Control 8291-2",
    "8291-1": "Expert Power Control 8291-1",
}
AUTH_MODES = {
    "none": "No password set (factory)",
    "basic_compatible": "Basic Compatible",
    "session": "Session",
    "session_extended": "Session Extended",
}

PORTS = 21
DC_PORTS = 17
# Banks: (id, ports, supply source id, supply name, nominal volts, AC?)
BANKS = (
    ("A", range(1, 6), "S1", "5V", 5.15, False),
    ("B", range(6, 10), "S2", "DC 12V", 12.05, False),
    ("C", range(10, 14), "S3", "DC 12V", 12.02, False),
    ("D", range(14, 18), "S4", "DC 12V", 12.02, False),
    ("E", range(18, 22), "L1", "230V", 232.7, True),
)
# Power sources (type 102): id, name, V_Out, Imax, Imax_out, fed from (index).
SOURCES = (
    ("S1", "DC 5V", 5.15, 11.0, 8.8, 4),
    ("S2", "DC 12V", 12.05, 7.5, 6.0, 4),
    ("S3", "DC 12V", 12.02, 7.5, 6.0, 4),
    ("S4", "DC 12V", 12.02, 7.5, 6.0, 4),
    ("S5", "DC 24V", 24.06, 14.0, 12.5, 5),
    ("L1", "AC 230V", 232.7, 16.0, 16.0, None),
)
SWITCH_GAP_S = 1.0
RESET_DURATION_S = 5
RESTART_S = 3.0
EXTENDED = 0x800000


def _socket(port: int) -> str:
    for bank_id, ports, *_rest in BANKS:
        if port in ports:
            return f"{bank_id}{list(ports).index(port) + 1}"
    return str(port)


def _dew_point(temperature: float, humidity: float) -> float:
    """Magnus formula, the usual way a climate sensor reports it."""
    humidity = max(1.0, min(100.0, humidity))
    gamma = math.log(humidity / 100.0) + 17.62 * temperature / (243.12 + temperature)
    return round(243.12 * gamma / (17.62 - gamma), 1)


def _port_controls():
    groups = []
    for port in range(1, PORTS + 1):
        controls = [
            {"type": "toggle", "key": f"port{port}_on", "label": "On"},
            {"type": "slider", "key": f"port{port}_load", "label": "Load when on",
             "min": 0, "max": 4 if port <= DC_PORTS else 10, "step": 0.05,
             "unit": "A"},
        ]
        if port <= DC_PORTS:
            controls.append({"type": "toggle", "key": f"port{port}_overload",
                             "label": "Overload (trips the eFuse)"})
            controls.append({"type": "indicator", "key": f"port{port}_efuse",
                             "label": "eFuse state"})
        groups.append({"type": "group", "label": f"Port {port} ({_socket(port)})",
                       "controls": controls})
    return groups


def _initial_state():
    state = {
        "model": "8291-2",
        "auth_mode": "none",
        "no_sessions": False,
        "https_redirect": False,
        "ovp_failed": False,
        "session_timeout_s": 0,
        "ext1_present": True,
        "ext1_temperature": 24.5,
        "ext1_humidity": 41.0,
        "ext1_pressure": 1006.5,
        "ext2_present": False,
        "ext2_temperature": 22.0,
        "ext2_humidity": 45.0,
        "ext2_pressure": 1006.0,
    }
    for port in range(1, PORTS + 1):
        state[f"port{port}_on"] = True
        state[f"port{port}_load"] = 0.4 if port <= DC_PORTS else 0.3
        if port <= DC_PORTS:
            state[f"port{port}_overload"] = False
            state[f"port{port}_efuse"] = 1
    return state


class GudeEpc8291Simulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "gude_epc8291",
        "name": "GUDE Expert Power Control 8291 Simulator",
        "category": "power",
        "transport": "http",
        "default_port": 80,
        "initial_state": _initial_state(),
        "delays": {"command_response": 0.01},
        "controls": [
            {"type": "select", "key": "model", "label": "Model",
             "options": list(MODELS)},
            {"type": "select", "key": "auth_mode", "label": "Authentication Mode",
             "options": list(AUTH_MODES)},
            {"type": "toggle", "key": "no_sessions",
             "label": "Firmware without session sign-in (HTTP Basic only)"},
            {"type": "toggle", "key": "https_redirect",
             "label": "HTTP redirects to HTTPS"},
            {"type": "slider", "key": "session_timeout_s",
             "label": "Session timeout (0 = never)", "min": 0, "max": 600,
             "step": 5, "unit": "s"},
            {"type": "toggle", "key": "ovp_failed",
             "label": "Overvoltage protection failed"},
            {
                "type": "group", "label": "External sensor 1",
                "controls": [
                    {"type": "toggle", "key": "ext1_present", "label": "Plugged in"},
                    {"type": "slider", "key": "ext1_temperature", "label": "Temperature",
                     "min": -20, "max": 80, "step": 0.5, "unit": "°C"},
                    {"type": "slider", "key": "ext1_humidity", "label": "Humidity",
                     "min": 0, "max": 100, "step": 1, "unit": "%"},
                    {"type": "slider", "key": "ext1_pressure", "label": "Air pressure",
                     "min": 900, "max": 1100, "step": 0.5, "unit": "hPa"},
                ],
            },
            {
                "type": "group", "label": "External sensor 2",
                "controls": [
                    {"type": "toggle", "key": "ext2_present", "label": "Plugged in"},
                    {"type": "slider", "key": "ext2_temperature", "label": "Temperature",
                     "min": -20, "max": 80, "step": 0.5, "unit": "°C"},
                    {"type": "slider", "key": "ext2_humidity", "label": "Humidity",
                     "min": 0, "max": 100, "step": 1, "unit": "%"},
                    {"type": "slider", "key": "ext2_pressure", "label": "Air pressure",
                     "min": 900, "max": 1100, "step": 0.5, "unit": "hPa"},
                ],
            },
            *_port_controls(),
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        now = time.monotonic()
        self._booted = now - 5 * 86400
        self._metered_at = now
        self._energy_kwh = 127.418
        self._energy_reset_kwh = 12.425
        self._switch_counts = {port: 3 for port in range(1, PORTS + 1)}
        # Switches waiting for the gap between ports: [(due, port, on)].
        self._queue = []
        self._last_switch = 0.0
        # port -> batch: {"steps": [(due, on)], "total": s, "reason": n}
        self._batches = {}
        # session id -> (csrf token, user, last use)
        self._sessions = {}
        self._down_until = 0.0
        # The UI's last view of each port, to count a UI switch as one.
        self._seen_on = {port: True for port in range(1, PORTS + 1)}

    # ── Model ────────────────────────────────────────────────────────────

    def _on(self, port):
        return bool(self.get_state(f"port{port}_on", False))

    def _load(self, port):
        try:
            return float(self.get_state(f"port{port}_load", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _efuse(self, port):
        if port > DC_PORTS:
            return int(self._on(port))
        return int(self.get_state(f"port{port}_efuse", int(self._on(port))))

    def _set_port(self, port, on, now):
        if self._on(port) != on:
            self._switch_counts[port] += 1
        self.set_state(f"port{port}_on", on)
        self._seen_on[port] = on
        self._last_switch = now
        if port <= DC_PORTS:
            if on and self.get_state(f"port{port}_overload", False):
                # Switching on resets the eFuse; the overload trips it again.
                self.set_state(f"port{port}_on", False)
                self._seen_on[port] = False
                self.set_state(f"port{port}_efuse", 2)
            else:
                self.set_state(f"port{port}_efuse", int(on))

    def _request_switch(self, port, on, now):
        """At least SWITCH_GAP_S after the switch before it, queued or not."""
        last = max([self._last_switch] + [q[0] for q in self._queue])
        due = max(now, last + SWITCH_GAP_S)
        if due <= now:
            self._set_port(port, on, now)
        else:
            self._queue.append((due, port, on))

    def _settle(self):
        now = time.monotonic()
        # Switches the UI made count like any other.
        for port in range(1, PORTS + 1):
            if self._on(port) != self._seen_on[port]:
                self._switch_counts[port] += 1
                self._seen_on[port] = self._on(port)
                if port <= DC_PORTS:
                    self.set_state(f"port{port}_efuse", int(self._on(port)))
        # An overload on a port that is on trips its eFuse.
        for port in range(1, DC_PORTS + 1):
            if self._on(port) and self.get_state(f"port{port}_overload", False):
                self.set_state(f"port{port}_on", False)
                self._seen_on[port] = False
                self.set_state(f"port{port}_efuse", 2)
        for item in sorted(self._queue):
            if item[0] <= now:
                self._queue.remove(item)
                self._set_port(item[1], item[2], item[0])
        for port, batch in list(self._batches.items()):
            steps = batch["steps"]
            while steps and steps[0][0] <= now:
                _due, on = steps.pop(0)
                self._set_port(port, on, now)
            if not steps:
                del self._batches[port]
        # Energy at the line power since the last look.
        elapsed = now - self._metered_at
        self._metered_at = now
        kwh = self._line_power() * elapsed / 3_600_000.0
        self._energy_kwh += kwh
        self._energy_reset_kwh += kwh

    def _dc_watts(self):
        total = 0.0
        for _bank_id, ports, _src, _name, volts, ac in BANKS:
            if ac:
                continue
            total += volts * sum(self._load(p) for p in ports if self._on(p))
        return total

    def _ac_amps(self):
        return sum(self._load(p) for p in BANKS[4][1] if self._on(p))

    def _line_power(self):
        # The power supply at 90 %, the AC loads at a 0.95 power factor, and
        # the unit's own 8 W.
        return round(self._dc_watts() / 0.9 + 232.7 * self._ac_amps() * 0.95 + 8.0, 1)

    # ── HTTP ─────────────────────────────────────────────────────────────

    def handle_request(self, method, path, headers, body):
        now = time.monotonic()
        if now < self._down_until:
            return 503, "Service Unavailable"
        if self.get_state("https_redirect", False):
            # The HTTP server set to HTTPS_REDIR: every plain request is sent on.
            return 301, "Moved Permanently", {
                "Location": f"https://127.0.0.1:{self.port}{path}"}
        self._settle()
        parts = urlsplit(path)
        route = parts.path
        query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
        if method == "HEAD":
            return 200, ""
        if route == "/login.json" and method == "POST":
            return self._login(body)
        if route in ("/status.json", "/statusjsn.js"):
            form = {}
            if method == "POST":
                form = {k: v[-1] for k, v in parse_qs(body).items()}
            args = {**query, **form}
            who, how = self._authorise(headers)
            if who is None:
                return 401, {"error": "authentication required"}, {
                    "WWW-Authenticate": 'Basic realm="GUDE"'}
            if "cmd" in args:
                if how == "session" and method != "POST":
                    # Manual 4.4: with a session, switching is a POST.
                    return 403, {"error": "use POST"}
                refused = self._run(args, who, now)
                if refused is not None:
                    return refused
            try:
                components = int(args.get("components", "1"))
            except ValueError:
                components = 1
            return 200, self._status(components)
        if route in ("/", "/index.html", "/ov.html"):
            return 200, "<html><title>GUDE Expert Power Control</title></html>"
        return 404, "Not Found"

    def _login(self, body):
        mode = self.get_state("auth_mode", "none")
        if self.get_state("no_sessions", False):
            return 404, "Not Found"
        form = {k: v[-1] for k, v in parse_qs(body).items()}
        user = form.get("username", "")
        password = form.get("password", "")
        if mode != "none" and (not user or password == "invalid" or user == "invalid"):
            return 401, {"login": 0}
        session = secrets.token_hex(8)
        csrf = secrets.token_hex(16)
        self._sessions[session] = (csrf, user or "admin", time.monotonic())
        return 200, {"login": 1, "sessionidX": csrf, "user_id": 1,
                     "href": "dashboard.html"}, {
            "Set-Cookie": f"GUDESESSION={session}; Path=/; HttpOnly"}

    def _authorise(self, headers):
        """(user, how) for a request the unit's mode lets through, else
        (None, None)."""
        mode = self.get_state("auth_mode", "none")
        if mode == "none":
            return "admin", "none"
        lower = {k.lower(): v for k, v in headers.items()}
        if self.get_state("no_sessions", False) or mode == "basic_compatible":
            user = self._basic_user(lower.get("authorization", ""))
            if user is not None:
                return user, "basic"
            if self.get_state("no_sessions", False):
                return None, None
        session = self._session_cookie(lower.get("cookie", ""))
        entry = self._sessions.get(session) if session else None
        if entry is None:
            return None, None
        csrf, user, last = entry
        timeout = float(self.get_state("session_timeout_s", 0) or 0)
        now = time.monotonic()
        if timeout > 0 and now - last > timeout:
            del self._sessions[session]
            return None, None
        if mode == "session_extended" and lower.get("sessionidx", "") != csrf:
            return None, None
        self._sessions[session] = (csrf, user, now)
        return user, "session"

    @staticmethod
    def _basic_user(value):
        if not value.lower().startswith("basic "):
            return None
        try:
            user, _, password = base64.b64decode(value[6:].strip()).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        if not user or user == "invalid" or password == "invalid":
            return None
        return user

    @staticmethod
    def _session_cookie(cookie):
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "GUDESESSION":
                return value
        return None

    def _run(self, args, who, now):
        try:
            cmd = int(args.get("cmd", "0"))
        except ValueError:
            return 400, {"error": "bad cmd"}
        if cmd == 39:
            if who == "user":
                return 403, {"error": "admin only"}
            self._down_until = now + RESTART_S
            self._sessions.clear()
            return None
        try:
            port = int(args.get("p", "0"))
        except ValueError:
            port = 0
        if not 1 <= port <= PORTS:
            return 400, {"error": "bad port"}
        if cmd == 1:
            s = args.get("s", "")
            if s == "2":
                on = not self._on(port)
            elif s in ("0", "1"):
                on = s == "1"
            else:
                return 400, {"error": "bad state"}
            self._batches.pop(port, None)
            self._request_switch(port, on, now)
        elif cmd == 2:
            self._batches.pop(port, None)
        elif cmd == 5:
            try:
                first = args["a1"] == "1"
                then = args["a2"] == "1"
                wait = int(args["s"])
            except (KeyError, ValueError):
                return 400, {"error": "bad batch"}
            if not 1 <= wait <= 65535:
                return 400, {"error": "bad wait"}
            self._set_port(port, first, now)
            self._batches[port] = {"steps": [(now + wait, then)], "total": wait,
                                   "reason": 1}
        elif cmd == 12:
            if self._on(port):
                self._set_port(port, False, now)
                self._batches[port] = {"steps": [(now + RESET_DURATION_S, True)],
                                       "total": RESET_DURATION_S, "reason": 2}
        else:
            return 400, {"error": "unknown cmd"}
        self._settle()
        return None

    # ── Status JSON ──────────────────────────────────────────────────────

    def _status(self, components):
        reply = {}
        if components & 0x1:
            reply["outputs"] = [self._output(p) for p in range(1, PORTS + 1)]
        if components & 0x8:
            reply["ethernet"] = {"mac": "00:19:32:01:2a:5c", "crc": 0}
        if components & 0x10:
            model = self.get_state("model", "8291-2")
            reply["misc"] = {
                "product_name": MODELS.get(model, MODELS["8291-2"]),
                "vendor_name": "GUDE Systems",
                "prodid": "8291",
                "firm_v": "1.5.3",
                "def_hostname": "EPC-8291",
                "hostname": "epc8291",
                "uptime": int(time.monotonic() - self._booted),
            }
        if components & 0x100:
            reply["port_summary"] = {
                "num_outputs": PORTS, "num_inputs": 0,
                "num_sensors": sum(1 for n in (1, 2)
                                   if self.get_state(f"ext{n}_present", False)),
                "num_adc": 0, "peak_period": 1,
                "batches": len(self._batches),
            }
        if components & 0x200:
            reply["hardware"] = self._hardware()
        layout = self._sensors(extended=bool(components & EXTENDED))
        if components & 0x10000:
            reply["sensor_descr"] = [descr for descr, _values in layout]
        if components & 0x4000:
            reply["sensor_values"] = [values for _descr, values in layout]
        return reply

    def _output(self, port):
        batch = self._batches.get(port)
        now = time.monotonic()
        if batch and batch["steps"]:
            remaining = max(0, math.ceil(batch["steps"][0][0] - now))
            batch_field = [batch["total"], remaining, 1, 2, 1, batch["reason"]]
        else:
            batch_field = [0, 0, 0, 0, 0, 0]
        return {
            "name": self.get_state(f"port{port}_name", "Power Port"),
            "state": int(self._on(port)),
            "ph_state": int(self._on(port)),
            "sw_cnt": self._switch_counts[port],
            "type": 1,
            "batch": batch_field,
            "wdog": [0, 3, None, 32],
        }

    def _hardware(self):
        banks = []
        for bank_id, ports, _src, _name, _volts, ac in BANKS:
            fuse = None
            if ac:
                fuse = 0 if self.get_state("ovp_failed", False) else 1
            banks.append({
                "fuse": fuse,
                "name": f"Bank {bank_id}",
                "power": 1,
                "size": len(ports),
                "ports": list(ports),
                "port_names": [_socket(p) for p in ports],
                "sensor": [101, len(banks)],
            })
        return {"num_powerports": PORTS, "num_inputs": 0, "outlet_name": "Power Port",
                "banks": banks}

    def _sensors(self, extended):
        """[(descr, values)] in the order the unit lists them."""
        out = [self._line_sensor()]
        if extended:
            out.append(self._system_sensor())
            out.append(self._bank_sensor())
        out.append(self._source_sensor())
        if self.get_state("model", "8291-2") == "8291-2":
            out.append(self._dc_outlet_sensor())
        for n in (1, 2):
            if self.get_state(f"ext{n}_present", False):
                out.append(self._ext_sensor(n))
        return out

    def _line_sensor(self):
        names = [("Voltage", "V", 3), ("Current", "A", 3), ("Frequency", "Hz", 2),
                 ("PhaseIU", "deg", 2), ("ActivePower", "W", 1),
                 ("ReactivePower", "VAR", 1), ("ApparentPower", "VA", 1),
                 ("PowerFactor", "", 3), ("Residual Current", "A", 4),
                 ("AbsActEnergyNonRes", "kWh", 3), ("AbsActEnergyRes", "kWh", 3),
                 ("RelativeTime", "s", None)]
        power = self._line_power()
        apparent = round(power / 0.93, 1)
        values = [232.7, round(apparent / 232.7, 3), 49.98, -21.5, power,
                  round(math.sqrt(max(0.0, apparent ** 2 - power ** 2)), 1),
                  apparent, 0.93, 0.0012, round(self._energy_kwh, 3),
                  round(self._energy_reset_kwh, 3), 61461881]
        fields = []
        for name, unit, precision in names:
            field = {"name": name, "unit": unit}
            if precision is not None:
                field["decPrecision"] = precision
            fields.append(field)
        descr = {"type": 9, "num": 1, "options": {"sumRowIndex": None},
                 "fields": fields,
                 "properties": [{"id": "L1", "n_ref": 0, "name": "Meter1", "state": 1}]}
        return descr, {"type": 9, "num": 1, "index": 0,
                       "values": [[{"v": v} for v in values]]}

    def _system_sensor(self):
        descr = {
            "type": 20, "num": 1,
            "groups": [
                {"fields": [{"name": "Vsystem", "unit": "V", "decPrecision": 3},
                            {"name": "Vaux", "unit": "V", "decPrecision": 3},
                            {"name": "Vmain", "unit": "V", "decPrecision": 3},
                            {"name": "Tcpu", "unit": "deg C", "decPrecision": 1},
                            {"name": "Fan", "unit": ""}]},
                {"fields": [{"name": "Alert", "unit": ""}]},
            ],
            "properties": [{"id": "System", "name": "", "groups": [
                [{"id": "I1", "name": "Internal"}],
                [{"id": "Port Switch", "name": None}, {"id": "eFuse Fail", "name": None}],
            ]}],
        }
        values = [[[[{"v": 3.295}, {"v": 4.013}, {"v": 11.664}, {"v": 40.5}, {"v": 0}]],
                   [[{"v": 0}], [{"v": 0}]]]]
        return descr, {"type": 20, "num": 1, "index": 0, "values": values}

    def _bank_sensor(self):
        props = []
        values = []
        for bank_id, ports, src, name, volts, ac in BANKS:
            port_props = [{"id": str(p), "name": _socket(p)} for p in ports]
            props.append({"id": bank_id, "name": f"Bank {bank_id}",
                          "groups": [port_props, [{"id": src, "name": name}]]})
            port_rows = []
            amps = 0.0
            for p in ports:
                on = self._on(p)
                port_rows.append([{"v": int(on)}, {"v": int(on)}, {"v": self._efuse(p)}])
                if on:
                    amps += self._load(p)
            current = None if ac else round(amps, 3)
            values.append([port_rows, [[{"v": "AC" if ac else "DC"}, {"v": volts},
                                        {"v": current}]]])
        descr = {
            "type": 101, "num": len(BANKS),
            "groups": [
                {"fields": [{"name": "Enabled", "unit": ""}, {"name": "Power", "unit": ""},
                            {"name": "State", "unit": ""}]},
                {"fields": [{"name": "AC/DC", "unit": ""},
                            {"name": "Voltage", "unit": "V", "decPrecision": 3},
                            {"name": "Current", "unit": "A", "decPrecision": 3}]},
            ],
            "properties": props,
        }
        return descr, {"type": 101, "num": len(BANKS), "index": 0, "values": values}

    def _source_sensor(self):
        bank_amps = {}
        for _bank_id, ports, src, _name, _volts, ac in BANKS:
            bank_amps[src] = sum(self._load(p) for p in ports if self._on(p))
        dc_watts = self._dc_watts()
        rows = []
        for source_id, _name, volts, imax, imax_out, fed in SOURCES:
            if source_id == "S5":
                amps = round(dc_watts / volts, 3)
            else:
                amps = round(bank_amps.get(source_id, 0.0), 3)
            rows.append([{"v": volts}, {"v": imax}, {"v": imax_out}, {"v": amps},
                         {"v": "AC" if source_id == "L1" else "DC"}, {"v": fed}])
        descr = {
            "type": 102, "num": len(SOURCES),
            "fields": [{"name": "V_Out", "unit": "V", "decPrecision": 3},
                       {"name": "Imax", "unit": "A", "decPrecision": 3},
                       {"name": "Imax_out", "unit": "A", "decPrecision": 3},
                       {"name": "I_out", "unit": "A", "decPrecision": 3},
                       {"name": "AC/DC", "unit": ""}, {"name": "src", "unit": ""}],
            "properties": [{"id": s[0], "name": s[1], "state": 1} for s in SOURCES],
        }
        return descr, {"type": 102, "num": len(SOURCES), "index": 0, "values": rows}

    def _dc_outlet_sensor(self):
        # NOT documented: an illustrative layout for the driver's generic
        # meter path (see the module docstring).
        volts = {}
        for _bank_id, ports, _src, _name, v, ac in BANKS:
            for p in ports:
                volts[p] = v
        rows = []
        for p in range(1, DC_PORTS + 1):
            amps = self._load(p) if self._on(p) else 0.0
            rows.append([{"v": volts[p] if self._on(p) else 0.0}, {"v": round(amps, 3)},
                         {"v": round(volts[p] * amps, 2)}])
        descr = {
            "type": 17, "num": DC_PORTS,
            "fields": [{"name": "Voltage", "unit": "V", "decPrecision": 3},
                       {"name": "Current", "unit": "A", "decPrecision": 3},
                       {"name": "Power", "unit": "W", "decPrecision": 2}],
            "properties": [{"id": str(p), "name": _socket(p)}
                           for p in range(1, DC_PORTS + 1)],
        }
        return descr, {"type": 17, "num": DC_PORTS, "index": 0, "values": rows}

    def _ext_sensor(self, n):
        temperature = float(self.get_state(f"ext{n}_temperature", 22.0))
        humidity = float(self.get_state(f"ext{n}_humidity", 40.0))
        pressure = float(self.get_state(f"ext{n}_pressure", 1006.0))
        dew = _dew_point(temperature, humidity)
        descr = {
            "type": 53, "num": 1,
            "fields": [{"name": "Temperature", "unit": "deg C", "decPrecision": 1},
                       {"name": "Humidity", "unit": "%", "decPrecision": 1},
                       {"name": "Dew Point", "unit": "deg C", "decPrecision": 1},
                       {"name": "Dew Diff", "unit": "deg C", "decPrecision": 1},
                       {"name": "Pressure", "unit": "hPa", "decPrecision": 1}],
            "properties": [{"id": f"{n}: 7206", "real_id": n - 1, "name": "7206",
                            "state": 1}],
        }
        values = [[{"v": temperature}, {"v": humidity}, {"v": dew},
                   {"v": round(temperature - dew, 1)}, {"v": pressure}]]
        return descr, {"type": 53, "num": 1, "index": 0, "values": values}
