# OpenAVC Driver Development Guide for AI Agents

This file is a self-contained reference for LLM-based coding agents helping users create device drivers for OpenAVC. It contains the complete YAML schema, Python driver API, naming conventions, validation instructions, and examples needed to produce working drivers without reading the full platform source code.

**What is OpenAVC?** An open-source (MIT) AV room control platform that replaces Crestron, Extron, and AMX. Drivers translate device protocols (TCP, serial, HTTP, UDP, OSC) into a unified state and command model.

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
10. [Runtime and Simulator Mechanics (Gotchas)](#10-runtime-and-simulator-mechanics-gotchas)
11. [Common Mistakes](#11-common-mistakes)

---

## 1. Driver Formats

OpenAVC supports two driver formats. Both produce identical runtime behavior.

| Format | Extension | Best For |
|--------|-----------|----------|
| YAML definition | `.avcdriver` | Text-based and OSC protocols (TCP, serial, HTTP, OSC). No code needed. Includes Telnet-style login handshake support. |
| Python class | `.py` | Binary protocols, UDP, custom auth schemes (non-prompt), complex state logic. |

**Decision guide:**

- Text commands over TCP or serial (e.g., `POWR ON\r`)? Use `.avcdriver`.
- HTTP/REST API? Use `.avcdriver` with `transport: http`.
- Binary protocol with checksums or length headers? Use Python.
- UDP broadcast/multicast? Use Python.
- Telnet-style `Username:` / `Password:` prompt handshake? Use `.avcdriver` with the `auth:` block (see §2.8).
- Other auth schemes (LOGIN command, JSON-RPC `login` method, OAuth, challenge-response)? Use Python.

---

## 2. YAML Driver Schema (.avcdriver)

YAML driver definitions are interpreted at runtime by the `ConfigurableDriver` class. The file extension must be `.avcdriver`.

A machine-readable JSON Schema for this format lives at the repository root: [`avcdriver.schema.json`](avcdriver.schema.json), published at `https://raw.githubusercontent.com/open-avc/openavc-drivers/main/avcdriver.schema.json`. Add this line to the top of a `.avcdriver` file to get live validation and autocompletion in editors with YAML Language Server support:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/open-avc/openavc-drivers/main/avcdriver.schema.json
```

The sections below remain the authoritative field-by-field reference; the schema mirrors them and the catalog rules enforced by `scripts/build_index.py`.

### 2.1 Top-Level Fields

#### Required

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique identifier. Lowercase, underscores only. (e.g., `extron_sis`) |
| `name` | string | Human-readable display name. |
| `transport` | string | One of: `tcp`, `serial`, `http`, `udp`, `osc` |

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

The `discovery:` block declares the network signals that point at
this driver. Two kinds of declarations:

- **Fingerprints** identify the driver alone — one match is enough.
  Result state: *identified*.
- **Hints** narrow candidates — several together produce a *possible*
  match with a candidate driver list. Result state: *possible*.

The matcher is deterministic — a rule either fires or it does not,
and a fingerprint match always beats a hint accumulation. A driver
with no `discovery:` block declares no signals; the loader logs a
warning and the driver becomes invisible to the matcher (still
installable manually).

```yaml
discovery:
  # ─── Fingerprints — any one alone identifies this driver ──────────

  mdns: "_pjlink._tcp.local."
  # OR list:
  #   mdns:
  #     - "_pjlink._tcp.local."
  #     - service: "_http._tcp.local."
  #       txt: { manufacturer: "Shure" }   # TXT-record filter

  ssdp: "urn:schemas-upnp-org:device:MediaRenderer:1"
  # OR list

  amx_ddp:
    make: "Polycom"
    model_pattern: "SoundStructure*"   # optional, default "*"

  tcp_probe:
    port: 4352
    send_ascii: "%1POWR ?\r"           # exactly one of: send_ascii, send_hex,
                                        # (omit for connect-only banner read)
    expect: "%1POWR=[01]"               # exactly one of: expect (substring),
                                        # expect_regex, expect_hex
    cross_vendor: false                 # default false; see §2.2.1
    extract_manufacturer: "PJLink"      # optional — feeds manufacturer_alias path
    extract:                             # optional — free-form metadata
      model:
        regex: "model=(.+)"
        group: 1
    timeout_ms: 3000                    # optional, default 3000, max 10000

  udp_probe:
    port: 6454
    send_hex: "417274..."
    expect_regex: "NovaStar"
    cross_vendor: false
    extract_manufacturer: "NovaStar"

  python:
    file: ./pjlink_class1_discovery.py  # path relative to driver YAML
    cross_vendor: true                  # see §2.2.1
  # The module must export `async def probe(ctx) -> None`. See §2.2.2.

  # ─── Hints — combine to narrow candidates ─────────────────────────

  oui: ["00:0e:dd", "d8:34:ee"]              # MAC vendor blocks
  hostname: ["^MXA", "^ANI"]                  # regex patterns
  port_open: [2202]                           # vendor-specific TCP ports
  manufacturer_alias: ["NEC", "Sharp NEC"]   # case-insensitive exact match
  snmp_pen: 17049                             # IANA Private Enterprise Number
```

Every field is optional. Mix and match — a driver with only hints can
still surface as *possible* with a candidate list; a driver with one
fingerprint identifies on a single match.

**Always declare hints alongside any fingerprint.** A fingerprint-only
driver is fragile: a single device shows up via several different
scanner paths — an SSDP NOTIFY, an mDNS announcement, a banner-grab
on the control port, or just an ARP-table sweep that captures the
OUI. A driver claiming only one path silently misses the rest, even
when the discovery scan already has the device's manufacturer string
and hostname in evidence. Hints (`oui`, `hostname`, `port_open`,
`manufacturer_alias`) cost nothing to declare and let the driver
claim the device regardless of how it was found. Hints never produce
*identified* alone, but they turn an *unknown* into a *possible
(candidate: <your driver>)* — strictly better, since the user gets a
one-click choice.

**Validation rules (enforced at load time by `parse_driver_discovery`,
mirrored at catalog-build time by `build_index.py`):**

1. **`port_open` rejects `{22, 80, 443, 8000, 8080, 8443, 8888}`** —
   too generic. The runtime keeps the disallowed set in
   `server/discovery/hints.py:DISALLOWED_OPEN_PORTS`; declaring any of
   these as a vendor-specific hint fails validation at load time.
   Other ports are accepted; vendor-specific port allocation is the
   driver author's responsibility.
2. **`tcp_probe` and `udp_probe` accept exactly one of `send_ascii` /
   `send_hex`.** Both is an error; omitting both is allowed for TCP
   connect-only banner reads.
3. **Probes declare exactly one of `expect` / `expect_regex` /
   `expect_hex`.** Required for both `tcp_probe` and `udp_probe`.
   Regex patterns are compiled at load time — invalid patterns fail
   validation.
4. **`timeout_ms` ≤ 10000.** Hard cap so a slow probe can't stretch
   the scan budget.
5. **`extract_manufacturer:`** is sugar for the manufacturer-alias
   enrichment path. The probe runner lifts the value into the evidence
   response so the matcher can pick a vendor-specific peer when this
   driver carries `cross_vendor: true`.
6. **`manufacturer_alias`** is case-insensitive and de-duplicated at
   parse time. Multiple drivers may declare the same alias.
7. **Fingerprint collisions raise.** Two drivers cannot claim the same
   fingerprint (same kind, same source ID, same TXT filter) without
   explicit cross-vendor framing. The signal index raises
   `ValueError` at build time.
8. **Template drivers exempt.** Drivers whose ID starts with
   `generic_` skip discovery validation entirely — they are project
   starting points, not discoverable devices.

### 2.2.1 Cross-vendor demotion

Some discovery signals identify a *protocol class*, not a specific
vendor — a multi-vendor projector control protocol, a multi-vendor
camera discovery beacon, a control-system family beacon. Drivers
hosting those signals declare `cross_vendor: true` on the relevant
fingerprint:

```yaml
discovery:
  python:
    file: ./pjlink_class1_discovery.py
    cross_vendor: true
```

When a `cross_vendor: true` fingerprint wins the match:

1. The matcher checks every peer driver's hints against the same
   device's evidence.
2. If a peer matches via `oui`, `hostname`, `manufacturer_alias`, or
   `port_open`, the peer becomes the primary `driver_id` and the
   cross-vendor driver moves to `alternatives[0]` (surfaced via
   `IdentificationMatch.alternatives` and the dropdown on the
   Discovery card).
3. If no peer matches, the cross-vendor driver remains primary.

This is per-fingerprint, not per-driver — a driver may carry several
fingerprints, some cross-vendor and some not. Only the fingerprint
that won the match is consulted for demotion.

If your driver targets a device that also responds to a generic
cross-vendor probe, **declare every brand string the firmware
actually emits** in `manufacturer_alias`. The exact string varies by
vendor and model; list every variant you've seen (e.g. `["NEC",
"Sharp NEC", "Sharp"]`, `["EPSON", "Seiko Epson"]`). You don't opt
into the cross-vendor probe yourself — the anchor driver hosts it.
Your `manufacturer_alias` is what makes your driver win the "best
fit" pick.

The same applies to `oui`: list every OUI block the manufacturer
ships under. Vendor-specific drivers that share an OUI (post-merger
entities, OEM rebranding) can both claim the prefix — they appear
together in the alternatives list.

### 2.2.2 Python escape-hatch

When the wire format can't be expressed as a single send/expect
exchange — multi-step handshakes, encrypted payloads, big-endian
bitfield parsing, broadcast-then-per-host TCP follow-ups, multicast
with per-send UUIDs — declare a sibling Python file:

```yaml
discovery:
  python:
    file: ./<driver_id>_discovery.py
    cross_vendor: false                # see §2.2.1
```

Convention: filename ends `_discovery.py` and lives next to the
driver:

```
projectors/pjlink_class1.avcdriver
projectors/pjlink_class1_discovery.py

utility/crestron_cip.avcdriver
utility/crestron_cip_discovery.py
```

The schema parser auto-registers two synthetic `SignalRule` records
under canonical IDs `custom_<driver_id>_companion_udp` (broadcast)
and `custom_<driver_id>_companion_tcp` (active); the companion's
`emit_broadcast()` / `emit_active()` calls default to those IDs.
**Without the `python:` declaration in YAML the evidence won't bind
back to your driver** — it'll land in `evidence_log` for the "Why?"
reveal but no rule will fire.

#### Companion API

```python
# pjlink_class1_discovery.py
from server.discovery.companion import ProbeContext


async def probe(ctx: ProbeContext) -> None:
    """Run discovery for this driver. Emit evidence via ctx."""
    # ctx.driver_id            — derived from filename stem
    # ctx.source_ip            — control adapter IP. Bind every socket.
    # ctx.target_subnets       — tuple of CIDR strings under scan
    # ctx.hosts_by_open_port   — dict[port, tuple[host, ...]] from the
    #                            engine's port-scan results — consult
    #                            this instead of iterating subnets
    # ctx.timeout_seconds      — overall budget (capped 30 s)
    # ctx.log                  — logger
    # ctx.emit_broadcast(host, *, response, txt, port, matched_pattern)
    #                                — emit broadcast evidence; pass
    #                                  port + matched_pattern so the
    #                                  scan-results "Why?" reveal can
    #                                  render "UDP probe on port <p>
    #                                  matched <kind:value>"
    # ctx.emit_active(host, response, *, port, matched_pattern)
    #                                — emit active-probe evidence; pass
    #                                  port + matched_pattern so the
    #                                  reveal renders "TCP probe on
    #                                  port <p> returned <excerpt>"
    #                                  for readable text, or
    #                                  "TCP probe on port <p> matched
    #                                  <kind:value>" for binary protocols
    # ctx.emit_oui(mac, host, *, vendor)
    #                                — emit OUI evidence (mac first)

    for host in ctx.hosts_by_open_port.get(4352, ()):
        await ctx.emit_active(
            host=host,
            response={"manufacturer": "BSS"},   # 'manufacturer' is
                                                 # reserved — feeds the
                                                 # manufacturer_alias path
            port=4352,                           # TCP port for the
                                                 # "TCP probe on port
                                                 # 4352 returned ..." UI
        )
```

#### Port-scan reuse

By the time companions run, the engine has already discovered which
IPs answer on which TCP ports. Companions whose protocol has no
native discovery layer should consult
`ctx.hosts_by_open_port.get(port, ())` instead of iterating
`target_subnets` and re-running the port scan themselves. The
engine's existing scan covers it; the companion stays small.

#### Safety

- The runtime wraps every `probe()` invocation in `asyncio.wait_for`
  with a hard cap (default 10 s, max 30 s). Hung companions are
  logged and cut off.
- The companion **must** bind every socket to `ctx.source_ip`. The
  runner doesn't sandbox Python — the contract is explicit through
  the `source_ip` argument and the community-trust model that already
  applies to driver code.
- Install / uninstall / update of any driver that declares `python:`
  fetches and removes the sibling `_discovery.py` file alongside the
  YAML, atomically.

#### When to use

The `python:` field is the escape-hatch — try declaring `tcp_probe:`
or `udp_probe:` first. Reach for `python:` when the wire format
genuinely needs Python: multi-step handshakes, binary fixed-offset
parsing, broadcast-then-per-host TCP follow-ups, or multicast with
per-send UUIDs. See `projectors/pjlink_class1_discovery.py` and
`utility/crestron_cip_discovery.py` for canonical examples.

If your driver targets older deployments, set `min_platform_version`
in `index.json` to the OpenAVC release that contains the discovery
schema fields you depend on — older platforms fail to parse new
fields and the catalog will grey out the driver for them.

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
    type: string             # string | text | integer | number | boolean | enum | object
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
  blocks:
    type: text               # Multi-line textarea — for declarative block lists,
                             # channel maps, scripted patterns. The Add Device
                             # dialog renders a 6-row monospace textarea. The
                             # raw string is preserved (no JSON / number coercion
                             # on save). Driver parses the string at __init__.
    label: "DSP Block List"
    description: "One block per line: <TAG> <TYPE> [CHANNELS]"
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

### 2.5.1 child_entity_types

Optional. Declare this when one physical device manages many addressable sub-units — a video matrix with hundreds of encoders/decoders, a DSP with many zones, a switcher with video-wall presets. Each registered sub-unit becomes a **child entity** with its own state keyed `device.<device_id>.<child_type>.<local_id_padded>.<property>`, its own row in the device's Child Entities tab, and per-property cloud relay cadence. Drivers that omit this stay flat single-unit devices.

```yaml
child_entity_types:
  encoder:
    label: "Encoder"
    label_plural: "Encoders"
    id_format:
      type: integer       # only integer IDs are supported in v1
      min: 1
      max: 762
      pad_width: 3        # encoder 5 -> "005" in state keys
    state_variables:
      name: { type: string }
      ip: { type: string }
      signal_present: { type: boolean, cloud_priority: high }
      edid_block: { type: string, cloud_priority: low }
    summary_fields: ["name", "ip", "signal_present"]
    label_field: name
```

**Rules:**
- `id_format.type` must be `integer`. `min` defaults to 1; `max` is optional (unbounded if omitted); `pad_width` zero-pads the ID in state keys (0 = no padding).
- `state_variables` uses the same schema as device `state_variables` (types: `string`, `integer`, `number`, `float`, `boolean`, `enum`). The platform always injects a boolean `online` and a string `label` per child — do not declare those.
- `cloud_priority` (optional, per state variable): `high` relays at the fast top-level cadence, `low` at the slow verbose cadence, omitted uses the default per-child cadence.
- `summary_fields` lists which fields appear as columns in the list view; `label_field` names the field carrying the controller's own name for the unit (the user-set label is separate and lives in the project file).
- A YAML driver only declares the types here; it has no way to register instances at runtime. Use a **Python driver** when the controller actually enumerates and updates children (`register_child` / `set_children_state_batch` / `deregister_child` — see §3.5).

### 2.6 commands

Actions the driver can send to the device.

#### TCP / Serial Commands

```yaml
commands:
  power_on:
    label: "Power On"
    send: "POWR ON\r"           # The raw string to send.
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

**Custom request headers:** HTTP commands accept an optional `headers:` map. Use it when the device requires a specific `Content-Type` (e.g. `text/xml` for SOAP / Cisco RoomOS xAPI), or any other custom header — `Accept`, `X-Device-Auth`, etc. Values support `{param}` substitution like the rest of the command. Headers declared on the command merge with (and override) any defaults the transport layer sets.

```yaml
commands:
  putxml_command:
    method: POST
    path: "/putxml"
    headers: { Content-Type: "text/xml" }
    body: "<Command><Audio><Volume><Set><Level>{level}</Level></Set></Volume></Audio></Command>"
    params:
      level: { type: integer, required: true, default: 50, min: 0, max: 100 }
```

If you don't declare `headers:`, the transport sets `Content-Type: application/json` for JSON bodies (i.e. ones that parse cleanly as JSON) and sends raw bodies (e.g. XML, plain text) with no `Content-Type` header at all — fine for many devices, but not for ones that strictly check the header. The `headers:` field is also valid on `device_settings` write definitions.

**Config substitution:** `{config_key}` placeholders (e.g., `{display_id}`) are replaced with the device's config values. This works in `send` strings, HTTP `path`/`body`/`query_params`/`headers` fields.

### 2.7 responses

Regex patterns for parsing device responses and mapping captured values to state variables.

#### Shorthand Format (recommended)

```yaml
responses:
  - match: 'In(\d+) All'          # Regex with capture groups.
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

### 2.8 auth

Login handshake for Telnet-style devices that present `Username:` / `Password:` prompts before accepting commands. Runs after the TCP connection is established and before `on_connect` commands are sent.

```yaml
auth:
  type: telnet_login                      # only type supported today
  username_prompt: "login: "              # regex matched against incoming bytes
  password_prompt: "Password: "           # regex matched against incoming bytes
  success_pattern: "GNET> "               # optional regex; if omitted, success is assumed
  failure_pattern: "Login incorrect"      # optional regex; matches => fail fast
  username_field: username                # config field holding the username (default: "username")
  password_field: password                # config field holding the password (default: "password")
  skip_if_empty: true                     # if true and username is blank, the handshake is skipped (default: true)
  timeout_seconds: 10                     # per-prompt timeout (default: 10)
  line_ending: "\r\n"                     # appended after username/password (default: "\r\n")
```

Add `username` and `password` fields to `default_config` and `config_schema` so they show up in the Add Device dialog (mark `password` with `secret: true`).

The framework drops the transport's frame parser to raw mode for the duration of the handshake so partial prompts (e.g., `Login: ` without trailing newline) are visible. Each prompt is a regex matched against the buffered bytes, decoded as UTF-8 with replacement. The original parser is restored before `on_connect` runs.

If the device's auth scheme is not prompt-and-response Telnet (e.g., `LOGIN <password>` command, JSON-RPC `login` method, OAuth, challenge-response), `type: telnet_login` does not fit — use a Python driver. New auth types may be added as the framework grows; declare the new `type:` value in your driver and check `server/drivers/configurable.py` for support.

### 2.9 on_connect

Commands sent once immediately after connection (and after the `auth:` handshake completes, if any), before polling starts. Use for enabling feedback/verbose mode or requesting initial state.

```yaml
on_connect:
  - "\x1b3CV\r\n"    # Extron: enable verbose mode 3 (push all state changes)
  - "< GET ALL >"    # Shure: request all current state values
```

This enables real-time push notifications from devices that support it. Without `on_connect`, the driver relies entirely on polling.

### 2.10 polling

Periodic status queries sent to the device. The poll cadence is set by
`default_config.poll_interval` (and overridden per-device by the project's
`config.poll_interval`) — `polling:` only declares the queries to run.

```yaml
default_config:
  poll_interval: 10              # Seconds between polls. Single canonical
                                  # field for poll cadence — DO NOT add a
                                  # top-level `polling.interval` (runtime
                                  # ignores it; the build script rejects it).

polling:
  queries:
    # TCP/Serial: raw protocol strings
    - "I\r"                      # Query current input
    - "V\r"                      # Query volume
    - "Z\r"                      # Query mute

    # HTTP: command names or paths
    - "get_status"               # Executes the command named "get_status"
    - "/api/status"              # GET request to this path; response matched against patterns
```

### 2.11 device_settings

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

### 2.12 frame_parser (Advanced)

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
        "transport": "tcp",  # tcp | serial | http | udp | osc

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
        "discovery": {"port_open": [5000]},
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

    CONTRACT: poll() MUST propagate transport-level errors
    (ConnectionError, TimeoutError, OSError, httpx.ConnectError,
    httpx.TimeoutException). The platform polling loop catches them
    and counts each toward a missed-poll watchdog; after 3 consecutive
    dry polls, device.<id>.connected is flipped to False.

    Swallowing transport errors here causes device.<id>.connected to
    lie. Wrap any HTTP client work in poll() like this:

        try:
            await self._refresh_state()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise ConnectionError(
                f"Device not responding: {exc}"
            ) from exc

    Protocol-level errors (ValueError, expected device "in standby"
    responses) MAY be handled inside poll() — the device is still
    reachable.
    """
```

#### Reachability check for custom-transport drivers

If your driver creates its own HTTP / websocket client instead of using
a platform transport class, call `_verify_reachable(host, port, timeout)`
in `connect()` before setting `connected=True`. Without this, loading the
project against an unreachable device reports `connected=True` for one
or more poll cycles before the watchdog catches up.

```python
async def connect(self) -> None:
    host = self.config.get("host", "")
    port = self.config.get("port", 1400)
    if not await self._verify_reachable(host, port, timeout=3.0):
        raise ConnectionError(f"Device at {host}:{port} not responding")
    # ... rest of setup
    self._connected = True
    self.set_state("connected", True)
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

#### Child entities

When the device declares `child_entity_types` (see §2.5.1), register and update its sub-units through these helpers. The platform owns the key formatting — never assemble `device.<id>.<type>.<id>.<prop>` strings yourself. All writes validate the property against the declared schema and raise on unknown props.

```python
# Tell the platform a child exists; creates its state keys. Idempotent —
# a repeat call with the same (type, id) is a no-op, so it's safe to call
# from a poll loop. initial_state overrides per-prop defaults; the
# synthetic `online` defaults to True.
self.register_child("encoder", 5, initial_state={"name": "Lobby TX"})

# Update one or many fields for one child (atomic per child).
self.set_child_state("encoder", 5, "signal_present", True)
self.set_child_state_batch("encoder", 5, {"ip": "10.0.0.9", "online": True})

# Atomic update across many children — preferred for poll responses that
# touch dozens/hundreds at once. Each entry is (type, local_id, {prop: val}).
self.set_children_state_batch([
    ("encoder", 5, {"signal_present": True}),
    ("encoder", 6, {"signal_present": False}),
])

# Currently-registered local IDs of a type, in registration order.
ids = self.list_children("encoder")

# Remove a child and delete all its state keys (e.g. unit deleted on the
# controller). For a unit that's merely offline, set online=False instead.
self.deregister_child("encoder", 5)

# Paginated polling helper: splits registered IDs into batches, calls
# fetch(batch_ids) per batch, applies results atomically per batch.
await self.poll_children("encoder", fetch=self._fetch_encoder_state)
```

Override `async def refresh_children(self)` to support the IDE's "Refresh from Device" button (re-enumerate the controller's children). Without an override it returns HTTP 501.

#### Controller pattern: enumerate and reconcile children

A controller that manages many children follows one shape every time (worked example: `switchers/chazy_control_pro.py`):

1. **Declare** the child types in `DRIVER_INFO["child_entity_types"]` (§2.5.1).
2. **On connect, enumerate the roster.** Override `connect()`; after the transport is up, run one cheap "list everything" command, parse it, and `register_child(type, id, initial_state=...)` per unit. `register_child` is idempotent, so the same call is safe to repeat every poll.
3. **Fill in detail** with `await self.poll_children(type, fetch=...)` — it batches the registered IDs (50/batch) instead of one request per unit.
4. **On poll, reconcile.** Re-read the roster, `register_child` newly-seen units (idempotent → updates existing), `deregister_child` ones the controller no longer reports. Run the cheap roster query at the normal poll interval and the expensive per-unit detail refresh on a **slower** cadence (e.g. a `detail_poll_interval` config knob) so large controllers don't flood the wire every cycle.
5. **Pick the right `online` model per type.** Link-style children (encoders/decoders/endpoints that physically come and go) derive `online` from the unit's link/net flag and stay registered while offline (`online=False`, do **not** deregister). Config-style children (groups, video walls, presets — virtual objects) are forced `online=True` whenever the controller lists them and deregistered only when actually deleted on the device.
6. **Refresh.** Override `async def refresh_children(self)` to re-run the enumeration for the IDE button; return a count summary.
7. **Commands** that act on a child take a `child_id` param (`child_type: <type>`, §2.5.1 / commands) — the platform substitutes the integer local ID. There is no separate per-child command surface.

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
| `serial` | `port`, `baudrate`, `bytesize`, `parity`, `stopbits` | RS-232/RS-485 devices |
| `http` | `host`, `port`, `ssl`, `verify_ssl`, `auth_type`, `username`, `password`, `token`, `api_key` | REST API devices |
| `udp` | `host`, `port` | Broadcast protocols (Wake-on-LAN, Art-Net) |
| `osc` | `host`, `port`, `listen_port` | OSC (Open Sound Control) devices — mixing consoles, show control, lighting |

**Common config fields (all transports):**
- `poll_interval` -- Seconds between polls (0 = disabled)
- `inter_command_delay` -- Seconds to wait between sequential commands

### Reachability detection by transport

The runtime decides "is this device actually online?" differently per transport. If your driver omits the right signal, `device.<id>.connected` can report `True` indefinitely against an unreachable host.

| Transport | How `connected` becomes `False` |
|-----------|----------------------------------|
| `tcp` | Socket open fails or the connection drops. |
| `serial` | The OS rejects the port open. |
| `http` | Pre-connect `verify()` HEAD probe; periodic poll on `poll_interval`. |
| `osc` | Pre-connect `verify()` probe (send + listen); periodic poll on `poll_interval`. |
| `udp` | **No transport-level probe.** UDP is purely connectionless and has no `verify()` method. The only signal the runtime gets is your driver's `poll()` succeeding or failing. Set `poll_interval > 0` and implement a `poll()` that round-trips a status query — otherwise the device will sit at `connected: True` forever no matter what's happening on the network. |

For UDP, picking a poll interval is a tradeoff: too tight wastes wire traffic on a connectionless protocol; too loose delays failure detection. 10–30 seconds is reasonable for most AV equipment.

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

  notifications:
    # Push unsolicited messages when state changes (simulates real device behavior)
    volume:
      '*': 'Vol{value}'            # {value} replaced with new state value
    mute:
      'true': 'Amt1'              # Value-specific messages
      'false': 'Amt0'

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
      behavior: no_response       # no_response | corrupt_response
                                  # (omit `behavior` to only apply this mode's `set_state`)
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
├── audio/               # Biamp Tesira, QSC Q-SYS, Shure, Audio-Technica
├── cameras/             # VISCA, BirdDog PTZ
├── video/               # vMix, NDI codecs
├── streaming/           # NDI / RTSP encoders, streaming endpoints, Sonos
├── lighting/            # DMX, Art-Net, sACN
├── power/               # PDUs, power sequencers
├── devices/             # Miscellaneous AV gear that doesn't fit elsewhere
├── utility/             # Wake-on-LAN, relays, bridges
├── docs/                # Contributing guide, writing simulators
├── scripts/             # Build + validation scripts (build_index.py)
├── index/               # Generated per-category index files
├── tests/               # Driver tests
├── index.json           # Generated driver catalog
├── devices.json         # Generated device catalog
├── manufacturers.json   # Manufacturer registry
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
| `video` | `video/` | Video production software, recorders |
| `streaming` | `streaming/` | NDI / RTSP encoders, streaming endpoints, Sonos |
| `lighting` | `lighting/` | DMX controllers, Art-Net nodes, sACN |
| `power` | `power/` | PDUs, power sequencers, relay controllers |
| `utility` | `utility/` | Wake-on-LAN, bridges, miscellaneous helpers |

---

## 7. Driver Metadata (powers index.json and devices.json)

**`index.json` and `devices.json` are generated artifacts. Do NOT edit them by hand.** They are produced by `scripts/build_index.py` from the metadata declared in each driver file. CI verifies they're in sync.

Add metadata to the driver file itself: top-level YAML keys for `.avcdriver`, or inside the `DRIVER_INFO` class attribute for `.py` drivers. Run `python scripts/build_index.py` to regenerate the catalog.

### Required fields

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Lowercase alphanumeric + underscores. Unique across the repo. |
| `name` | string | Human-readable display name. |
| `manufacturer` | string | Must appear in `manufacturers.json`. Add new manufacturers there first. |
| `category` | enum | One of: `projector`, `display`, `switcher`, `audio`, `camera`, `video`, `streaming`, `lighting`, `power`, `utility`. |
| `version` | string | Semver (e.g., `1.0.0`). Bump on every change. |
| `author` | string | Your name or GitHub handle. |
| `transport` | enum | One of: `tcp`, `udp`, `http`, `osc`, `serial`. |
| `description` | string | One paragraph for AV integrators. Plain language, no marketing fluff. |
| `source_url` | URL | The protocol document or canonical implementation you built from. No driver ships without this. |

### Optional fields

| Field | Type | Notes |
|-------|------|-------|
| `ports` | list[int] | Default network ports the device listens on (1-65535). |
| `protocols` | list[string] | Protocol family identifiers (e.g., `["pjlink"]`). |
| `simulated` | bool | `true` if a simulator covers this driver. |
| `verified` | bool | `true` only after testing on real hardware. |
| `min_platform_version` | string | Minimum OpenAVC version (semver). Omit when compatible with all platform versions. |
| `tags` | list[string] | Lowercase, hyphen-separated keywords for Browse Drivers search. Examples: `["ndi", "ptz"]`, `["ceiling-mic"]`. |
| `help` | object | `{ "overview": "...", "setup": "..." }`. Both strings non-empty. |
| `deprecated` | bool | Mark superseded drivers. |
| `replacement_id` | string | Required when `deprecated: true`. Must reference another driver's `id`. |
| `compatible_models` | list | List of `{ manufacturer, models, confidence, notes? }` entries — see below. |

### `compatible_models` entry shape

```yaml
compatible_models:
  - manufacturer: Biamp                              # Must be in manufacturers.json
    models: [TesiraFORTE AVB, TesiraFORTE CI]
    confidence: full                                  # full | partial | untested
    notes: AVB models lack analog I/O                 # optional, required when two drivers claim 'full' for the same model
```

Confidence values:

- `full` — driver controls every documented feature of these devices.
- `partial` — driver controls common features only. Some advanced features unsupported.
- `untested` — generic protocol driver expected to work but not specifically verified for these models.

### How models become discoverable

The build script reverse-indexes every `compatible_models` entry into `devices.json`, so a user searching "Epson EB-L1075U" in Browse Drivers finds the right driver. Generic protocol drivers (PJLink, SNMP, ONVIF, etc.) leave their own `compatible_models` mostly empty — their device coverage comes from `devices-extra.json` entries that point at them.

---

## 8. Validation

Run the build script before submitting. It validates the schema and regenerates `index.json` and `devices.json`:

```bash
python scripts/build_index.py            # Validate + regenerate
python scripts/build_index.py --check    # Validate only (does not write outputs)
```

CI runs `--check` and fails the PR if the generated artifacts differ from what's checked in.

The validator checks:
- Required fields present
- Field types correct
- Driver ID format (lowercase, underscores only)
- Category is valid
- State variable types valid
- Command structure valid (TCP/serial vs HTTP vs OSC)
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
  port_open: [5000]
  hostname: ["^ACME-"]

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

## 10. Runtime and Simulator Mechanics (Gotchas)

These are behaviors of the runtime and the auto-simulator that aren't obvious from the schema. Each one has caused a silent driver bug at least once.

### Simulator handler dispatch order

`command_handlers:` entries in your `simulator:` section are sorted into two lists by shape:

- `match: + respond:` (or `match: + set_state:`) — explicit handlers, tried **first**.
- `match: + handler:` (inline Python) — script handlers, tried **second**.

Anything not matched by those falls through to handlers auto-generated from your `commands:` block. A catch-all (`match: '.+'`) belongs **last** — but if you write it as `match: + respond:` it ends up in the explicit list and intercepts everything ahead of more specific entries written below it. Write catch-alls and other "always last" entries as `match: + handler:` so they sit in the script-handler list, which preserves YAML order.

### Simulator `match:` patterns are anchored to the full line

Every simulator `match:` is compiled as `^{pattern}$` — the pattern must match the **entire** synthesized command line, not a prefix. For HTTP simulators the wire format is `GET /path?query` or `POST /path|<body>`. So a pattern like `^POST /putxml\|<Command><Standby><Activate` will **not** match `POST /putxml|<Command><Standby><Activate/></Standby></Command>` — there's body left over and `$` requires end-of-string. End HTTP `match:` patterns with `.*` (or another consumer) so trailing bytes don't sink the match. Symptom: handler returns `None`, simulator emits 404.

### Response dispatch returns after the first match

Your `responses:` list is tried in order; the first regex that matches the response wins, the rest are skipped. To pull multiple values out of one response line, use **one** regex with multiple capture groups and **one** `set:` block with multiple keys — not two separate response entries.

### Multi-line responses fan out per line

Some protocols answer a single bulk query with one key/value per line. The TCP/Telnet `delimiter` framing splits incoming bytes into one frame per line **before** response matching, so each line is matched independently against `responses:`. Write one `responses:` entry per key (`^iris\s+(\d+)$`, `^gain\s+(\d+)$`, ...) and one bulk query populates many state vars from one round-trip. Don't try to match the whole multi-line block as a single regex with `[\s\S]+` — that fights the per-line dispatch.

### HTTP body Content-Type behavior

When you set `body:` on an HTTP command, the runtime tries to parse it as JSON. If parsing succeeds, the request goes out as `Content-Type: application/json` with that JSON body. If parsing fails (e.g. XML, plain text), the body is sent as raw bytes with **no** `Content-Type` header. If your device strictly checks Content-Type for non-JSON bodies, set the right one explicitly via the `headers:` field documented in section 2.6.

### Don't fabricate state from outgoing commands

If the protocol has no query for a value, don't synthesize that state by tracking the last command you sent. The "state" you'd be reporting wouldn't reflect the actual device — it'd reflect the last command issued, which diverges the moment another control surface (front panel, IR remote, scheduled task) acts. Mirror only what the device tells you. If users want a "last sent" value, that belongs in macros / variables on the project side, not in the driver's state surface.

---

## 11. Common Mistakes

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
