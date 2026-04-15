# OpenAVC Driver Development Guide for AI Agents

This file is a self-contained reference for LLM-based coding agents helping users create device drivers for OpenAVC. It contains the complete YAML schema, Python driver API, naming conventions, validation instructions, and examples needed to produce working drivers without reading the full platform source code.

**What is OpenAVC?** An open-source (MIT) AV room control platform that replaces Crestron, Extron, and AMX. Drivers translate device protocols (TCP, serial, HTTP, UDP) into a unified state and command model.

**Repository:** `github.com/open-avc/openavc-drivers`
**Platform source:** `github.com/open-avc/openavc`

---

## Table of Contents

1. [Driver Formats](#1-driver-formats)
2. [YAML Driver Schema (.avcdriver)](#2-yaml-driver-schema-avcdriver)
3. [Python Driver API](#3-python-driver-api)
4. [Transport Layer](#4-transport-layer)
5. [Simulator Support](#5-simulator-support)
6. [Repository Structure and Naming](#6-repository-structure-and-naming)
7. [index.json Catalog Entry](#7-indexjson-catalog-entry)
8. [Validation](#8-validation)
9. [Complete Examples](#9-complete-examples)
10. [Common Mistakes](#10-common-mistakes)

---

## 1. Driver Formats

OpenAVC supports two driver formats. Both produce identical runtime behavior.

| Format | Extension | Best For |
|--------|-----------|----------|
| YAML definition | `.avcdriver` | Text-based protocols (TCP, serial, HTTP). No code needed. |
| Python class | `.py` | Binary protocols, authentication handshakes, UDP, complex state logic. |

**Decision guide:**

- Text commands over TCP or serial (e.g., `POWR ON\r`)? Use `.avcdriver`.
- HTTP/REST API? Use `.avcdriver` with `transport: http`.
- Binary protocol with checksums or length headers? Use Python.
- UDP broadcast/multicast? Use Python.
- Authentication handshake before commands? Use Python (custom `connect()`).

---

## 2. YAML Driver Schema (.avcdriver)

YAML driver definitions are interpreted at runtime by the `ConfigurableDriver` class. The file extension must be `.avcdriver`.

### 2.1 Top-Level Fields

#### Required

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique identifier. Lowercase, underscores only. (e.g., `extron_sis`) |
| `name` | string | Human-readable display name. |
| `transport` | string | One of: `tcp`, `serial`, `http`, `udp` |

#### Optional

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `manufacturer` | string | `"Generic"` | Manufacturer name. |
| `category` | string | `"utility"` | One of: `projector`, `display`, `switcher`, `scaler`, `audio`, `camera`, `lighting`, `relay`, `utility`, `other` |
| `version` | string | `"1.0.0"` | Semantic version of the driver. |
| `author` | string | `"Community"` | Driver author. |
| `description` | string | `""` | Brief description. |
| `delimiter` | string | `"\r"` | Message delimiter. Supports escape sequences: `\r`, `\n`, `\r\n`, or a literal character. |
| `help` | object | `{}` | `{overview: "...", setup: "..."}` shown in the Add Device dialog. |
| `protocols` | list | `[]` | Protocol names for device discovery. (e.g., `["pjlink"]`, `["extron_sis"]`) |
| `discovery` | object | `{}` | Network discovery hints (see below). |

### 2.2 discovery

Optional hints that help the discovery engine match detected devices to this driver.

```yaml
discovery:
  ports: [23, 9761]                      # TCP ports the device listens on
  mac_prefixes: ["00:05:a6", "00:e0:91"] # IEEE OUI prefixes
  mdns_services: ["_pjlink._tcp.local."] # mDNS/Bonjour service types
  upnp_types: ["urn:schemas-upnp-org:device:MediaServer:1"]
  hostname_patterns: ["^DTP-.*", "^NEC-.*"]  # Regex patterns for hostnames
```

### 2.3 default_config

Default values for device connection settings. These pre-fill the Add Device dialog.

```yaml
default_config:
  # TCP
  host: ""
  port: 23
  poll_interval: 10          # Seconds between status polls (0 = no polling)
  inter_command_delay: 0.1   # Seconds to wait between sequential commands

  # Serial
  baudrate: 9600
  parity: "N"                # "N", "E", or "O"
  bytesize: 8
  stopbits: 1

  # HTTP
  ssl: false
  verify_ssl: false
  auth_type: "none"          # "none", "basic", "digest", "bearer", "api_key"
  username: ""
  password: ""
  token: ""
  api_key: ""
  timeout: 10.0
```

### 2.4 config_schema

Defines the fields shown in the Add Device dialog. Each key is a config field name.

```yaml
config_schema:
  host:
    type: string             # string | integer | number | boolean | enum | object
    required: true
    default: ""
    label: "IP Address"
    description: "Device IP address or hostname"
  port:
    type: integer
    required: true
    default: 23
    label: "Port"
    min: 1
    max: 65535
  display_id:
    type: integer
    required: false
    default: 1
    label: "Display ID"
    description: "Monitor ID for multi-display setups"
    min: 0
    max: 255
  input_mode:
    type: enum
    label: "Input Mode"
    values: ["auto", "manual"]
    default: "auto"
  password:
    type: string
    label: "Password"
    secret: true             # Masks the value in the UI
```

### 2.5 state_variables

Properties read from the device and exposed to the system. State keys are automatically namespaced as `device.<device_id>.<variable_id>`.

```yaml
state_variables:
  power:
    type: enum               # string | integer | number | float | boolean | enum
    values: ["off", "on", "warming", "cooling"]
    label: "Power State"
    help: "Current power state of the projector"
  volume:
    type: integer
    label: "Volume"
    help: "Audio volume level (0-100)"
  mute:
    type: boolean
    label: "Audio Mute"
  lamp_hours:
    type: integer
    label: "Lamp Hours"
    help: "Total lamp operating hours"
```

**Rules:**
- `label` is required.
- `type` must be one of: `string`, `integer`, `number`, `boolean`, `enum`, `float`.
- `enum` type requires a `values` list.
- Values must be flat primitives (str, int, float, bool, None). No nested objects.

### 2.6 commands

Actions the driver can send to the device.

#### TCP / Serial Commands

```yaml
commands:
  power_on:
    label: "Power On"
    send: "POWR ON\r"           # The raw string to send. "string" is an alias for "send".
    help: "Turn on the projector"
  set_input:
    label: "Set Input"
    send: "{input}!\r"           # {param_name} is substituted at runtime
    help: "Route a specific input"
    params:
      input:
        type: integer            # string | integer | number | boolean | enum
        required: true
        label: "Input Number"
        min: 1
        max: 8
        help: "Source input number (1-based)"
  set_volume:
    label: "Set Volume"
    send: "{level:03d}AU\r"      # Python format spec: zero-padded 3-digit integer
    params:
      level:
        type: integer
        required: true
        label: "Volume Level"
        min: 0
        max: 100
```

#### HTTP Commands

```yaml
commands:
  power_on:
    label: "Power On"
    method: POST                 # GET | POST | PUT | DELETE | PATCH (default: GET)
    path: "/api/power"           # Supports {param} substitution
    body: '{"power": "on"}'      # Optional. For POST/PUT.
    help: "Turn on the device"
  set_volume:
    label: "Set Volume"
    method: PUT
    path: "/api/audio"
    body: '{"level": {level}}'   # {param} substituted with actual value
    params:
      level:
        type: integer
        required: true
        label: "Volume"
        min: 0
        max: 100
  get_status:
    label: "Get Status"
    method: GET
    path: "/api/status"
    # Response text is matched against response patterns
```

HTTP commands also support `query_params` (a dict of URL query parameters with `{param}` substitution) and the config field `api_key_header` (default: `"X-API-Key"`) for customizing the API key auth header name.

**Config substitution:** `{config_key}` placeholders (e.g., `{display_id}`) are replaced with the device's config values. This works in `send` strings, HTTP `path`/`body`/`query_params` fields.

### 2.7 responses

Regex patterns for parsing device responses and mapping captured values to state variables.

#### Shorthand Format (recommended)

```yaml
responses:
  - match: 'In(\d+) All'          # Regex pattern. "pattern" is an alias for "match".
    set: { input: "$1" }           # $1, $2, etc. = capture groups
  - match: 'Vol(\d+)'
    set: { volume: "$1" }
  - match: 'Amt(\d+)'
    set: { mute: "$1" }           # Values are strings; type coercion happens in state store
  - match: 'POWR=ON'
    set: { power: "on" }          # Literal values (no capture group needed)
```

#### Verbose Format (with type conversion and value mapping)

```yaml
responses:
  - match: 'In(\d+)'
    mappings:
      - group: 1                  # Which capture group
        state: input              # State variable to update
        type: integer             # Cast to this type: integer | float | boolean | string
  - match: 'Pwr(\d)'
    mappings:
      - group: 1
        state: power
        map:                      # Value mapping (raw value -> state value)
          "0": "off"
          "1": "on"
          "2": "warming"
          "3": "cooling"
```

**Config substitution in patterns:** Use `{config_key}` in patterns. For example, if a DSP uses configurable instance tags:

```yaml
responses:
  - match: '"{level_instance_tag}" value (-?[\d.]+)'
    set: { level: "$1" }
```

The `{level_instance_tag}` is replaced with the device's config value when the driver connects.

**Important:** The first matching pattern wins. Order your patterns from most specific to most general.

### 2.8 polling

Periodic status queries sent to the device.

```yaml
polling:
  interval: 10                   # Seconds between polls (overridden by device config poll_interval)
  queries:
    # TCP/Serial: raw protocol strings
    - "I\r"                      # Query current input
    - "V\r"                      # Query volume
    - "Z\r"                      # Query mute

    # HTTP: command names or paths
    - "get_status"               # Executes the command named "get_status"
    - "/api/status"              # GET request to this path; response matched against patterns
```

### 2.9 device_settings

Configurable values that live on the device hardware (not in the project file). These are writable and polled. The system queues writes for offline devices and sends them when the device reconnects.

```yaml
device_settings:
  hostname:
    type: string                 # string | integer | number | float | boolean | enum
    label: "Device Hostname"
    help: "Network hostname of the device"
    default: "DEVICE"
    state_key: "hostname"        # Which state variable reflects current value
    setup: true                  # Show in Add Device dialog
    unique: true                 # Auto-generate non-clashing default
    regex: "^[A-Za-z0-9_-]+$"   # Optional validation pattern
    write:
      # TCP/Serial write
      send: 'SET HOSTNAME {value}\r'
      # OR HTTP write
      # method: POST
      # path: /api/settings
      # body: '{"hostname": "{value}"}'
  ndi_name:
    type: string
    label: "NDI Source Name"
    help: "Name visible to NDI receivers"
    default: "DEVICE_NAME"
    state_key: "ndi_name"
    setup: true
    unique: true
    write:
      method: PUT
      path: /api/ndi/name
      body: '{"name": "{value}"}'
```

### 2.10 frame_parser (Advanced)

For binary protocols that don't use text delimiters. Overrides the default delimiter-based framing.

```yaml
frame_parser:
  type: length_prefix            # length_prefix | fixed_length
  header_size: 2                 # 1, 2, or 4 bytes (big-endian)
  header_offset: 0               # Offset added to decoded length value
  include_header: false          # Include the length header in the returned message

# OR
frame_parser:
  type: fixed_length
  length: 10                     # Exact message length in bytes
```

---

## 3. Python Driver API

For complex protocols that YAML can't express. Python drivers subclass `BaseDriver`. They can be created and edited directly in the Programmer IDE's **Code** view with hot-reload support, or placed manually in `driver_repo/`.

**Source reference:** [`server/drivers/base.py`](https://github.com/open-avc/openavc/blob/main/server/drivers/base.py)

### 3.1 DRIVER_INFO (Required Class Attribute)

Every Python driver must define `DRIVER_INFO` as a class-level dict. It uses the same schema as the YAML top-level fields:

```python
class MyDriver(BaseDriver):
    DRIVER_INFO = {
        # Required
        "id": "my_driver",
        "name": "My Device",
        "transport": "tcp",  # tcp | serial | http | udp

        # Metadata
        "manufacturer": "Acme",
        "category": "switcher",
        "version": "1.0.0",
        "author": "Your Name",
        "description": "Controls Acme switchers via binary protocol.",

        # Connection defaults
        "default_config": {
            "host": "",
            "port": 5000,
            "poll_interval": 10,
        },

        # Config UI fields (same schema as YAML config_schema)
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "required": True, "default": 5000, "label": "Port"},
        },

        # State (same schema as YAML state_variables)
        "state_variables": {
            "power": {"type": "boolean", "label": "Power"},
            "input": {"type": "integer", "label": "Active Input"},
        },

        # Commands (params same schema as YAML)
        "commands": {
            "power_on": {"label": "Power On", "params": {}, "help": "Turn on"},
            "set_input": {
                "label": "Set Input",
                "params": {
                    "input": {"type": "integer", "required": True, "label": "Input", "min": 1, "max": 8}
                },
            },
        },

        # Optional
        "help": {
            "overview": "Controls Acme matrix switchers.",
            "setup": "Connect via Ethernet. Default port 5000.",
        },
        "protocols": ["acme_binary"],
        "discovery": {"ports": [5000]},
        "device_settings": {},
        "delimiter": "\r",  # Can be overridden by _resolve_delimiter()
    }
```

### 3.2 Constructor

```python
def __init__(self, device_id: str, config: dict, state: StateStore, events: EventBus):
```

The base class constructor sets:
- `self.device_id` -- Assigned device ID
- `self.config` -- Device configuration dict
- `self.state` -- StateStore instance
- `self.events` -- EventBus instance
- `self.transport` -- Set during `connect()` (None initially)
- `self.connected` -- Boolean, True after successful connect

### 3.3 Required Override

```python
async def send_command(self, command: str, params: dict | None = None) -> Any:
    """Execute a named command. Called when a user, macro, or script
    triggers a command on this device.

    Args:
        command: Command name (key from DRIVER_INFO["commands"])
        params: Parameter dict (keys match command's params schema)

    Returns:
        Command result (driver-specific, often None)
    """
```

### 3.4 Optional Overrides

#### Connection Lifecycle

```python
async def connect(self) -> None:
    """Establish connection. Default implementation:
    1. Creates transport from DRIVER_INFO["transport"] and self.config
    2. Sets self._connected = True
    3. Starts polling if poll_interval > 0

    Override for: authentication handshakes, greeting parsing,
    custom transport setup.
    """

async def disconnect(self) -> None:
    """Close connection. Default implementation:
    1. Stops polling
    2. Closes transport
    3. Sets self._connected = False
    """
```

#### Data Handling

```python
async def on_data_received(self, data: bytes) -> None:
    """Called when a complete message arrives from the device.
    For delimiter-based transports, the delimiter is stripped.
    Default: no-op. Override to parse responses and update state.
    """

async def poll(self) -> None:
    """Called periodically (every poll_interval seconds).
    Default: no-op. Override to send status query commands.
    """
```

#### Device Settings

```python
async def set_device_setting(self, key: str, value: Any) -> Any:
    """Write a device setting to the hardware.
    Default: raises NotImplementedError.
    """
```

#### Transport Customization

```python
def _create_frame_parser(self) -> FrameParser | None:
    """Return a custom frame parser for binary protocols.
    Default: None (uses delimiter-based framing).

    Options:
    - LengthPrefixFrameParser(header_size, header_offset, include_header)
    - FixedLengthFrameParser(length)
    - CallableFrameParser(parse_fn)
    """

def _resolve_delimiter(self) -> bytes | None:
    """Return the message delimiter as bytes.
    Default: checks DRIVER_INFO["delimiter"], then config["delimiter"], then b"\\r".
    Return None for raw (no-framing) mode.
    """
```

### 3.5 State Management Methods

```python
# Set a single device state value
self.set_state("power", True)
# Internally: self.state.set(f"device.{self.device_id}.power", True)

# Set multiple state values atomically
self.set_states({"power": True, "input": 3})

# Read a state value
value = self.get_state("power")
```

### 3.6 Polling Control

```python
await self.start_polling(interval=10.0)  # Start background polling
await self.stop_polling()                 # Cancel polling task
```

### 3.7 Transport Usage

The default `connect()` creates the transport automatically. If you override `connect()`, create the transport yourself:

```python
# TCP
from server.transport.tcp import TCPTransport

self.transport = await TCPTransport.create(
    host=self.config["host"],
    port=self.config["port"],
    on_data=self._handle_data,       # async callback for complete messages
    on_disconnect=self._handle_disconnect,
    delimiter=b"\r",
    timeout=5.0,
    ssl=False,
)

# Then send data:
await self.transport.send(b"POWR ON\r")
response = await self.transport.send_and_wait(b"POWR?\r", timeout=3.0)
```

```python
# Serial
from server.transport.serial_transport import SerialTransport

self.transport = await SerialTransport.create(
    port=self.config.get("port", "COM3"),
    baudrate=self.config.get("baudrate", 9600),
    on_data=self._handle_data,
    on_disconnect=self._handle_disconnect,
    delimiter=b"\r",
    bytesize=8, parity="N", stopbits=1,
)
```

```python
# HTTP
from server.transport.http_client import HTTPClientTransport

self.transport = HTTPClientTransport(
    base_url=f"http://{self.config['host']}",
    auth_type="basic",  # "none", "basic", "digest", "bearer", "api_key"
    credentials={"username": self.config["username"], "password": self.config["password"]},
    verify_ssl=False,
    timeout=10.0,
)
await self.transport.open()

# Then make requests:
resp = await self.transport.get("/api/status")
resp = await self.transport.post("/api/power", json_body={"power": "on"})
# resp.status, resp.text, resp.json_data
```

```python
# UDP (no auto-transport, always manual)
from server.transport.udp import UDPTransport

udp = UDPTransport(name=self.device_id)
await udp.open(allow_broadcast=True)
await udp.send(magic_packet, "255.255.255.255", 9)
udp.close()
```

### 3.8 Frame Parsers (Binary Protocols)

```python
from server.transport.frame_parsers import (
    LengthPrefixFrameParser,
    FixedLengthFrameParser,
    CallableFrameParser,
)

# Length-prefix: first N bytes encode payload length (big-endian)
parser = LengthPrefixFrameParser(header_size=2, header_offset=0, include_header=False)

# Fixed-length: every message is exactly N bytes
parser = FixedLengthFrameParser(length=12)

# Custom: provide a function (buffer: bytes) -> (message | None, remaining_buffer)
def my_parser(buf):
    if len(buf) < 4:
        return None, buf
    length = buf[2]
    total = 3 + length + 1  # header + payload + checksum
    if len(buf) < total:
        return None, buf
    return buf[:total], buf[total:]

parser = CallableFrameParser(my_parser)
```

### 3.9 Binary Helpers

```python
from server.transport.binary_helpers import checksum_xor, checksum_sum, crc16, hex_dump
```

---

## 4. Transport Layer

| Transport | Config Fields | Use Case |
|-----------|---------------|----------|
| `tcp` | `host`, `port`, `ssl`, `verify_ssl` | Network devices (most AV equipment) |
| `serial` | `serial_port`, `baudrate`, `bytesize`, `parity`, `stopbits` | RS-232/RS-485 devices |
| `http` | `host`, `port`, `ssl`, `verify_ssl`, `auth_type`, `username`, `password`, `token`, `api_key` | REST API devices |
| `udp` | `host`, `port` | Broadcast protocols (Wake-on-LAN, Art-Net) |

**Common config fields (all transports):**
- `poll_interval` -- Seconds between polls (0 = disabled)
- `inter_command_delay` -- Seconds to wait between sequential commands

---

## 5. Simulator Support

Drivers can include simulation support so users can test without real hardware. The simulator runs as a separate process.

### 5.1 YAML Drivers: Inline `simulator` Section

Add a `simulator` section to your `.avcdriver` file. Without it, auto-generation still creates basic Level 0 simulation (accepts connections, echoes).

```yaml
simulator:
  initial_state:
    power: "off"
    volume: 50
    mute: false
    input: 1

  delays:
    command_response: 0.02       # Seconds of simulated response latency

  controls:                       # UI controls in the Simulator web interface
    - type: power                 # Power button (toggles on/off)
      key: power
    - type: toggle                # On/off toggle switch
      key: mute
      label: "Mute"
    - type: slider                # Range control
      key: volume
      label: "Volume"
      min: 0
      max: 100
      step: 1
    - type: select                # Dropdown
      key: input
      label: "Input"
      options: ["HDMI 1", "HDMI 2", "VGA"]
    - type: indicator             # Read-only display
      key: lamp_hours
      label: "Lamp Hours"
      color_map:
        "ok": "#22c55e"
        "error": "#ef4444"
    - type: matrix                # Routing matrix grid
      label: "Video Routing"
      inputs: 8
      outputs: 4
      state_pattern: "route_{output}"

  command_handlers:
    # Simple: exact match with static response
    - receive: 'POWR ON'
      set_state: { power: "on" }
      respond: "POWR=ON\r\n"

    # Regex match with Python handler
    - match: '(\d+)\*(\d+)!'
      handler: |
        inp = int(match.group(1))
        out = int(match.group(2))
        state[f"route_{out}"] = inp
        respond(f"In{inp} Out{out}\r\n")

    # Query handler
    - receive: 'POWR?'
      handler: |
        val = "ON" if state["power"] == "on" else "OFF"
        respond(f"POWR={val}\r")

  error_modes:
    communication_timeout:
      description: "Device stops responding"
      behavior: no_response       # no_response | corrupt_response | custom_state
```

### 5.2 Python Drivers: Separate `_sim.py` File

Python drivers need a companion simulator file. Place it alongside the driver with a `_sim.py` suffix.

```
projectors/
├── pjlink_class1.py           # Driver
└── pjlink_class1_sim.py       # Simulator
```

**Source reference:** [`simulator/base.py`](https://github.com/open-avc/openavc/blob/main/simulator/base.py), [`simulator/tcp_simulator.py`](https://github.com/open-avc/openavc/blob/main/simulator/tcp_simulator.py)

You can scaffold a simulator from a Python driver:
```bash
python -m simulator.scaffold path/to/my_driver.py
```

**Simulator documentation:** [`docs/simulator.md`](https://github.com/open-avc/openavc/blob/main/docs/simulator.md), [`openavc-drivers/docs/writing-simulators.md`](https://github.com/open-avc/openavc-drivers/blob/main/docs/writing-simulators.md)

---

## 6. Repository Structure and Naming

### Directory Layout

```
openavc-drivers/
├── projectors/          # PJLink, Sony ADCP, Sharp NEC
├── displays/            # Samsung MDC, LG SICP, Sony Bravia
├── switchers/           # Extron SIS, Kramer P3000
├── audio/               # Biamp Tesira, QSC Q-SYS, Shure, Sonos
├── cameras/             # VISCA, BirdDog PTZ
├── video/               # vMix, NDI codecs
├── lighting/            # DMX, Art-Net, sACN
├── utility/             # Wake-on-LAN, relays, bridges
├── docs/                # Contributing guide, writing simulators
├── index.json           # Driver catalog
├── validate.py          # Validation script
└── AGENTS.md            # This file
```

### Naming Conventions

- **Driver ID:** Lowercase with underscores. (e.g., `extron_sis`, `samsung_mdc`, `biamp_tesira_ttp`)
- **File name:** Same as driver ID. (e.g., `extron_sis.avcdriver`, `samsung_mdc.py`)
- **One driver per device family,** not per model. A single `extron_sis.avcdriver` covers all Extron SIS products.
- **Simulator files:** `_sim.py` suffix alongside their driver. (e.g., `pjlink_class1_sim.py`)

### Category Selection

| Category | Directory | When to Use |
|----------|-----------|-------------|
| `projector` | `projectors/` | Projectors (PJLink, NEC, Sony ADCP) |
| `display` | `displays/` | Commercial displays, TVs, LED walls |
| `switcher` | `switchers/` | Matrix switchers, presentation switchers, scalers |
| `audio` | `audio/` | DSPs, mixers, amplifiers, microphones, speakers |
| `camera` | `cameras/` | PTZ cameras, webcams |
| `video` | `video/` | Video production software, NDI encoders/decoders |
| `lighting` | `lighting/` | DMX controllers, Art-Net nodes, sACN |
| `utility` | `utility/` | Wake-on-LAN, relays, power controllers, bridges |

---

## 7. index.json Catalog Entry

Every driver must have an entry in `index.json`. The catalog is used by the Programmer IDE's "Browse Drivers" feature.

```json
{
    "id": "my_driver",
    "name": "My Device",
    "file": "category/my_driver.avcdriver",
    "format": "avcdriver",
    "category": "switcher",
    "manufacturer": "Acme",
    "version": "1.0.0",
    "author": "Your Name",
    "transport": "tcp",
    "verified": false,
    "description": "Controls Acme matrix switchers via TCP.",
    "protocols": ["acme_protocol"],
    "ports": [5000],
    "simulated": true
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `id` | Yes | Must match driver's `id` field exactly. |
| `name` | Yes | Must match driver's `name` field. |
| `file` | Yes | Path relative to repo root. |
| `format` | Yes | `"avcdriver"` or `"python"`. |
| `category` | Yes | Must match driver's `category`. |
| `manufacturer` | Yes | Must match driver's `manufacturer`. |
| `version` | Yes | Must match driver's `version`. |
| `author` | Yes | Must match driver's `author`. |
| `transport` | Yes | Must match driver's `transport`. |
| `verified` | Yes | Always `false` for community contributions. |
| `description` | Yes | Must match driver's `description`. |
| `protocols` | No | List of protocol names (from driver). |
| `ports` | No | TCP ports the device listens on. |
| `simulated` | No | `true` if the driver has simulator support. |
| `min_platform_version` | No | Minimum OpenAVC version required (e.g., `"0.5.13"`). Set when the driver uses platform features not available in all releases. Older versions block installation with a clear error. |

---

## 8. Validation

Run the validation script before submitting:

```bash
python validate.py                              # Validate all drivers
python validate.py switchers/my_driver.avcdriver # Validate a specific driver
python validate.py --check-index                 # Also validate index.json consistency
```

The validator checks:
- Required fields present
- Field types correct
- Driver ID format (lowercase, underscores only)
- Category is valid
- State variable types valid
- Command structure valid (TCP/serial vs HTTP)
- Response patterns compile as valid regex
- No nested quantifiers in regex (causes backtracking)
- Delimiter is valid
- index.json entry matches driver fields
- File exists at declared path

---

## 9. Complete Examples

### 9.1 YAML: TCP Text Protocol (Switcher)

```yaml
id: acme_matrix
name: Acme Matrix Switcher
manufacturer: Acme
category: switcher
version: 1.0.0
author: Your Name
transport: tcp
description: Controls Acme matrix switchers via TCP text protocol.
delimiter: "\r\n"

help:
  overview: Controls Acme 8x8 and 16x16 matrix switchers.
  setup: Connect via Ethernet to port 5000. No authentication required.

discovery:
  ports: [5000]
  hostname_patterns: ["^ACME-"]

default_config:
  host: ""
  port: 5000
  poll_interval: 15

config_schema:
  host:
    type: string
    required: true
    label: IP Address
  port:
    type: integer
    required: true
    default: 5000
    label: Port

state_variables:
  input:
    type: integer
    label: Active Input
    help: Currently selected input
  volume:
    type: integer
    label: Volume
    help: Volume level 0-100
  mute:
    type: boolean
    label: Mute
    help: Audio mute state

commands:
  set_input:
    label: Set Input
    send: "IN{input}OUT1\r\n"
    help: Route an input to output 1
    params:
      input:
        type: integer
        required: true
        label: Input
        min: 1
        max: 16
  set_volume:
    label: Set Volume
    send: "VOL{level}\r\n"
    params:
      level:
        type: integer
        required: true
        label: Volume
        min: 0
        max: 100
  mute_on:
    label: Mute On
    send: "MUTE ON\r\n"
  mute_off:
    label: Mute Off
    send: "MUTE OFF\r\n"

responses:
  - match: 'IN(\d+)OUT1'
    set: { input: "$1" }
  - match: 'VOL(\d+)'
    set: { volume: "$1" }
  - match: 'MUTE (ON|OFF)'
    mappings:
      - group: 1
        state: mute
        map:
          "ON": true
          "OFF": false

polling:
  interval: 15
  queries:
    - "STA\r\n"

simulator:
  initial_state:
    input: 1
    volume: 50
    mute: false
  controls:
    - type: select
      key: input
      label: Input
      options: ["1", "2", "3", "4", "5", "6", "7", "8"]
    - type: slider
      key: volume
      label: Volume
      min: 0
      max: 100
    - type: toggle
      key: mute
      label: Mute
  command_handlers:
    - match: 'IN(\d+)OUT1'
      handler: |
        inp = int(match.group(1))
        state["input"] = inp
        respond(f"IN{inp}OUT1\r\n")
    - match: 'VOL(\d+)'
      handler: |
        vol = int(match.group(1))
        state["volume"] = vol
        respond(f"VOL{vol}\r\n")
    - receive: 'MUTE ON'
      set_state: { mute: true }
      respond: "MUTE ON\r\n"
    - receive: 'MUTE OFF'
      set_state: { mute: false }
      respond: "MUTE OFF\r\n"
    - receive: 'STA'
      handler: |
        respond(f"IN{state['input']}OUT1\r\n")
        respond(f"VOL{state['volume']}\r\n")
        mute_str = "ON" if state["mute"] else "OFF"
        respond(f"MUTE {mute_str}\r\n")
```

### 9.2 YAML: HTTP REST API (Display)

```yaml
id: acme_display
name: Acme Smart Display
manufacturer: Acme
category: display
version: 1.0.0
author: Your Name
transport: http
description: Controls Acme smart displays via REST API.

default_config:
  host: ""
  port: 443
  ssl: true
  verify_ssl: false
  auth_type: basic
  username: admin
  password: ""
  poll_interval: 10

config_schema:
  host:
    type: string
    required: true
    label: IP Address
  username:
    type: string
    label: Username
    default: admin
  password:
    type: string
    label: Password
    secret: true

state_variables:
  power:
    type: boolean
    label: Power
  input:
    type: string
    label: Active Input
  brightness:
    type: integer
    label: Brightness

commands:
  power_on:
    label: Power On
    method: POST
    path: /api/power
    body: '{"state": "on"}'
  power_off:
    label: Power Off
    method: POST
    path: /api/power
    body: '{"state": "off"}'
  set_input:
    label: Set Input
    method: PUT
    path: /api/input
    body: '{"input": "{input}"}'
    params:
      input:
        type: enum
        required: true
        label: Input
        values: ["HDMI1", "HDMI2", "DP1", "USB-C"]
  get_status:
    label: Get Status
    method: GET
    path: /api/status

responses:
  - match: '"power":\s*"(on|off)"'
    mappings:
      - group: 1
        state: power
        map:
          "on": true
          "off": false
  - match: '"input":\s*"(\w+)"'
    set: { input: "$1" }
  - match: '"brightness":\s*(\d+)'
    set: { brightness: "$1" }

polling:
  interval: 10
  queries:
    - "get_status"
```

### 9.3 YAML: DSP with Config Substitution

```yaml
id: acme_dsp
name: Acme DSP
manufacturer: Acme
category: audio
version: 1.0.0
author: Your Name
transport: tcp
description: Controls Acme DSP audio processors via text protocol.
delimiter: "\r\n"

default_config:
  host: ""
  port: 23
  poll_interval: 5
  level_tag: "Main_Level"
  mute_tag: "Main_Mute"

config_schema:
  host:
    type: string
    required: true
    label: IP Address
  level_tag:
    type: string
    required: true
    label: Level Instance Tag
    description: "DSP block instance tag for level control"
  mute_tag:
    type: string
    required: true
    label: Mute Instance Tag
    description: "DSP block instance tag for mute control"

state_variables:
  level:
    type: number
    label: Level
    help: Audio level in dB
  mute:
    type: boolean
    label: Mute

commands:
  set_level:
    label: Set Level
    send: '{level_tag} set level 1 {level}\r\n'
    params:
      level:
        type: number
        required: true
        label: Level (dB)
        min: -100
        max: 12
  mute_on:
    label: Mute On
    send: '{mute_tag} set mute 1 true\r\n'
  mute_off:
    label: Mute Off
    send: '{mute_tag} set mute 1 false\r\n'

responses:
  - match: '"{level_tag}" value (-?[\d.]+)'
    set: { level: "$1" }
  - match: '"{mute_tag}" value (true|false)'
    mappings:
      - group: 1
        state: mute
        map:
          "true": true
          "false": false

polling:
  interval: 5
  queries:
    - '{level_tag} get level 1\r\n'
    - '{mute_tag} get mute 1\r\n'
```

### 9.4 Python: Binary Protocol

```python
"""
Acme Binary Protocol Driver

Protocol: 4-byte header + payload + XOR checksum
  [0xAA] [CMD] [LEN] [DATA...] [XOR]

Source reference for BaseDriver API:
  https://github.com/open-avc/openavc/blob/main/server/drivers/base.py
"""

from server.drivers.base import BaseDriver
from server.transport.frame_parsers import CallableFrameParser


def _parse_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Extract one complete frame from buffer."""
    if len(buf) < 4:
        return None, buf
    if buf[0] != 0xAA:
        # Scan for start byte
        idx = buf.find(b"\xaa", 1)
        return None, buf[idx:] if idx >= 0 else b""
    length = buf[2]
    total = 3 + length + 1  # header(3) + payload + checksum(1)
    if len(buf) < total:
        return None, buf
    return buf[:total], buf[total:]


def _checksum(data: bytes) -> int:
    result = 0
    for b in data:
        result ^= b
    return result


class AcmeBinaryDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "acme_binary",
        "name": "Acme Binary Device",
        "manufacturer": "Acme",
        "category": "switcher",
        "version": "1.0.0",
        "author": "Your Name",
        "description": "Controls Acme devices via binary protocol.",
        "transport": "tcp",
        "default_config": {"host": "", "port": 5000, "poll_interval": 10},
        "config_schema": {
            "host": {"type": "string", "required": True, "label": "IP Address"},
            "port": {"type": "integer", "required": True, "default": 5000, "label": "Port"},
        },
        "state_variables": {
            "power": {"type": "boolean", "label": "Power"},
            "input": {"type": "integer", "label": "Active Input"},
        },
        "commands": {
            "power_on": {"label": "Power On", "params": {}},
            "power_off": {"label": "Power Off", "params": {}},
            "set_input": {
                "label": "Set Input",
                "params": {"input": {"type": "integer", "required": True, "min": 1, "max": 8, "label": "Input"}},
            },
        },
    }

    CMD_POWER = 0x01
    CMD_INPUT = 0x02
    CMD_STATUS = 0x10

    def _create_frame_parser(self):
        return CallableFrameParser(_parse_frame)

    def _build_packet(self, cmd: int, data: bytes = b"") -> bytes:
        header = bytes([0xAA, cmd, len(data)])
        payload = header + data
        return payload + bytes([_checksum(payload)])

    async def send_command(self, command: str, params: dict | None = None):
        params = params or {}
        if command == "power_on":
            await self.transport.send(self._build_packet(self.CMD_POWER, b"\x01"))
        elif command == "power_off":
            await self.transport.send(self._build_packet(self.CMD_POWER, b"\x00"))
        elif command == "set_input":
            inp = params["input"]
            await self.transport.send(self._build_packet(self.CMD_INPUT, bytes([inp])))

    async def on_data_received(self, data: bytes):
        if len(data) < 4:
            return
        cmd = data[1]
        payload = data[3:-1]
        if cmd == self.CMD_POWER and len(payload) >= 1:
            self.set_state("power", payload[0] == 1)
        elif cmd == self.CMD_INPUT and len(payload) >= 1:
            self.set_state("input", payload[0])

    async def poll(self):
        await self.transport.send(self._build_packet(self.CMD_STATUS))
```

---

## 10. Common Mistakes

These are common errors that produce drivers that fail validation or don't work at runtime.

### YAML Drivers

| Mistake | Fix |
|---------|-----|
| Missing `label` on state variables | Every state variable requires a `label` field. |
| Using `send` in HTTP commands | HTTP commands use `method` + `path` + `body`, not `send`. |
| Using `method`/`path` in TCP commands | TCP/serial commands use `send` (or `string`), not HTTP fields. |
| Nested objects in state values | State values must be flat primitives: str, int, float, bool, None. |
| Invalid regex in response patterns | Test your regex. Avoid nested quantifiers like `(a+)+` which cause catastrophic backtracking. |
| Wrong delimiter for protocol | Check the device's protocol manual. Most AV devices use `\r`, not `\n` or `\r\n`. |
| Forgetting config substitution syntax | Use `{config_key}` (curly braces) for config values in commands and patterns. |
| Putting command parameters in `default_config` | `default_config` is for connection settings. Command parameters go in `commands.<cmd>.params`. |
| Category doesn't match directory | A driver in `audio/` must have `category: audio`. |
| YAML single-quote escaping for regex | In YAML, use `'\*Q'` not `'\\*Q'` for regex special chars in simulator command_handlers. |

### Python Drivers

| Mistake | Fix |
|---------|-----|
| Using `asyncio.create_task()` | Use the framework's task management. Keep async operations in lifecycle methods. |
| Not calling `super().__init__()` | Always call the parent constructor in `__init__`. |
| Blocking the event loop | Use `await` for I/O. Never use `time.sleep()` -- use `asyncio.sleep()`. |
| Writing to state outside device namespace | Use `self.set_state("key", val)` which auto-prefixes with `device.<id>.`. |
| Missing DRIVER_INFO | Required class attribute. Without it, the driver won't load. |
| Missing `send_command` override | Required method. The base class raises `NotImplementedError`. |

### index.json

| Mistake | Fix |
|---------|-----|
| `id` doesn't match driver file | Must be identical. |
| `file` path wrong | Path is relative to repo root (e.g., `audio/biamp_tesira_ttp.avcdriver`). |
| `format` wrong | Use `"avcdriver"` for YAML files, `"python"` for `.py` files. |
| Missing entry entirely | Every driver must have an index.json entry for the Browse Drivers UI. |
| `verified` set to `true` | Only OpenAVC maintainers mark drivers as verified. Always submit with `false`. |

---

## License

All drivers in this repository must be MIT licensed. All dependencies (if any, for Python drivers) must use MIT-compatible licenses: MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0, ISC, PSF, Unlicense, 0BSD, CC0-1.0.

No GPL, LGPL, or AGPL licensed code or dependencies.
