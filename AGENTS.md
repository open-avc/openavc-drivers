# OpenAVC Driver Development Guide for AI Agents

This file is the repository-specific reference for LLM-based coding agents helping users create device drivers for OpenAVC. It covers what is true *here*: how a driver is laid out and named, the metadata that powers the generated catalog, how to validate and test what you wrote, worked examples, and the runtime behaviors that have each caused a silent driver bug at least once.

**The field-by-field driver contract is not repeated in this file.** It lives in two places that cannot drift out of sync with the platform:

- **[`avcdriver.schema.json`](avcdriver.schema.json)** and **[`pythondriver.schema.json`](pythondriver.schema.json)** at this repository's root — the machine-readable contract. Every field, type, enum, default and cross-field rule, generated from the platform's own driver-contract definition and byte-copied here by `scripts/vendor_platform_contract.py`. These are authoritative.
- **[Creating Drivers](https://docs.openavc.com/creating-drivers/)** — the written guide to the same fields: what each one is for, when to reach for it, what it pairs with, with worked examples per transport.

Read the schema for *what exists*, the guide for *what to use*, and this file for *what it takes to land a driver in this repository*.

**What is OpenAVC?** An open-source (MIT) AV room control platform that replaces Crestron, Extron, and AMX. Drivers translate device protocols (TCP, serial, HTTP, UDP, OSC) into a unified state and command model.

**Repository:** `github.com/open-avc/openavc-drivers`
**Platform source:** `github.com/open-avc/openavc`

---

## Table of Contents

1. [Driver Formats](#1-driver-formats)
2. [YAML Drivers (.avcdriver)](#2-yaml-drivers-avcdriver)
3. [Python Drivers](#3-python-drivers)
4. [Transport Layer](#4-transport-layer)
5. [Simulator Support](#5-simulator-support)
6. [Repository Structure and Naming](#6-repository-structure-and-naming)
7. [Driver Metadata](#7-driver-metadata-powers-indexjson-and-devicesjson)
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
- Telnet-style `Username:` / `Password:` prompt handshake? Use `.avcdriver` with an `auth:` block (see section 2).
- Other auth schemes (LOGIN command, JSON-RPC `login` method, OAuth, challenge-response)? Use Python.

---

## 2. YAML Drivers (.avcdriver)

YAML driver definitions are interpreted at runtime by the platform's `ConfigurableDriver` class. The file extension must be `.avcdriver`. No code is involved: the file declares the connection, the commands, and the patterns that turn replies into state.

**Where the fields are documented:**

- **[`avcdriver.schema.json`](avcdriver.schema.json)** (this repository's root) — the complete machine-readable contract, including the `discovery:`, `child_entity_types:`, `push:`, `liveness:`, `auth:`, `bridge:`, `polling:` and `simulator:` blocks. Also published at `https://raw.githubusercontent.com/open-avc/openavc-drivers/main/avcdriver.schema.json`.
- **[Creating Drivers](https://docs.openavc.com/creating-drivers/)**, section "Method 2: Driver Definition File (.avcdriver)" and its "Definition Reference" — the same contract in prose, with complete examples for TCP/serial text protocols, HTTP/REST, OSC, discovery, child entities, command and response framing, polling, device-initiated push, liveness, Telnet login handshakes, multi-transport drivers, bridges, and IR code sets.

Put this line at the top of every `.avcdriver` file to get live validation and autocompletion in any editor with YAML Language Server support — it is the fastest feedback loop available for this format, and it catches the mistakes below as you type:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/open-avc/openavc-drivers/main/avcdriver.schema.json
```

**Every block is closed — an unrecognized key is a hard error, not an ignored one.** This is deliberate: a misspelled section is the quietest possible failure. `state_varibles` used to load a driver with zero state variables and no warning anywhere, so the device connected and simply had nothing to show. `build_index.py` now rejects any key the contract doesn't declare (usually naming the spelling you meant), the schema above flags it live in your editor, and the platform's Driver Builder and import paths refuse it too. Don't invent fields: if a value has nowhere to go in the contract, it does nothing, and now it says so. The one place that stays lenient is the platform's *runtime loader*, which downgrades this to a log warning so a driver written for a newer platform still runs rather than dropping offline over a field that release doesn't know yet.

Section 9.1 is a complete working `.avcdriver`; sections 10 and 11 cover the behaviors the schema cannot express.

---

## 3. Python Drivers

A Python driver is a class that subclasses the platform's `BaseDriver`, carries a `DRIVER_INFO` class attribute, and implements `send_command()`. Reach for it when the protocol is binary, when the transport is UDP, MQTT or SSH, when authentication is anything other than a Telnet-style prompt handshake, or when the device is a controller that enumerates its own child entities at runtime.

**Where the API is documented:**

- **[`pythondriver.schema.json`](pythondriver.schema.json)** (this repository's root) — the machine-readable contract for the `DRIVER_INFO` dict. Same generator and same vendoring as the YAML schema; it additionally allows the Python-only `ssh` and `mqtt` transports and `kind: "setup"` actions.
- **[Creating Drivers](https://docs.openavc.com/creating-drivers/)**, section "Method 3: Python Driver" onward — `BaseDriver` hooks, the `poll()` contract, lifecycle hooks (`_post_connect` and friends, rather than overriding `connect()`), driver-owned HTTP sessions, controller drivers and the child-entity helpers, frame parsers, binary helpers, the device log, declaring every state variable you write, and the full `DRIVER_INFO` reference.

A `DRIVER_INFO` dict gets none of the live editor feedback a `.avcdriver` gets from its schema line, so a misspelled key there is invisible until the block it belongs to silently does nothing at runtime. Check it explicitly with `python -m openavc.drivers.check path/to/my_driver.py` from an OpenAVC checkout — see section 8.

Every Python driver ships a test (section 8.1) and a companion simulator (section 5). Section 9.2 is a complete worked example of a binary protocol.

---

## 4. Transport Layer

| Transport | Config Fields | Use Case |
|-----------|---------------|----------|
| `tcp` | `host`, `port`, `ssl`, `verify_ssl` | Network devices (most AV equipment) |
| `serial` | `port`, `baudrate`, `bytesize`, `parity`, `stopbits` | RS-232/RS-485 devices |
| `http` | `host`, `port`, `ssl`, `verify_ssl`, `auth_type`, `username`, `password`, `token`, `api_key` | REST API devices |
| `udp` | `host`, `port` | Broadcast protocols (Wake-on-LAN, Art-Net) |
| `osc` | `host`, `port`, `listen_port`, `transport_mode` | OSC (Open Sound Control) devices — mixing consoles, show control, lighting |

**OSC over UDP or TCP.** `transport: osc` defaults to UDP. To use OSC over TCP (reliable, large replies — e.g. QLab), add a `transport_mode` config field with values `udp`/`tcp` (default `udp`); when set to `tcp` the platform frames OSC with SLIP (RFC 1055) over a TCP connection and replies arrive on the same socket (`listen_port` is unused in TCP mode). OSC drivers that don't declare `transport_mode` stay UDP-only and are unaffected.

**`ssh` and `mqtt` are Python-driver-only transports.** There is no `.avcdriver`/YAML surface for them — an SSH CLI session and MQTT pub/sub don't fit the declarative request/response model, so they're not in the table above or in `avcdriver.schema.json`. Use a Python driver: build the transport in `connect()` (e.g. `MQTTTransport.create(...)` / `SSHTransport.create(...)`) and drive it yourself. For MQTT, register `self.transport.on_message` and use `publish`/`subscribe` rather than `send`/`send_and_wait`.

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
| `udp` | **No transport-level probe.** UDP is purely connectionless and has no `verify()` method. Give the runtime a liveness signal or the device will sit at `connected: True` forever no matter what's happening on the network. YAML drivers: declare a `liveness:` block (see section 2) -- a YAML driver's UDP poll queries are fire-and-forget, so polling alone proves nothing. Python drivers: override `_liveness_probe()` (see section 3), or implement a `poll()` that round-trips a status query **and raises when the reply doesn't come back** (a fire-and-forget send never fails). |

For UDP, picking a poll interval is a tradeoff: too tight wastes wire traffic on a connectionless protocol; too loose delays failure detection. 10–30 seconds is reasonable for most AV equipment.

---

## 5. Simulator Support

Drivers can include simulation support so users can test without real hardware. The simulator runs as a separate process. A `.avcdriver` adds an inline `simulator:` section; a Python driver ships a companion file with a `_sim.py` suffix alongside it (`pjlink_class1.py` -> `pjlink_class1_sim.py`).

**Auto-generation covers YAML only.** A `.avcdriver` with no `simulator:` section still simulates — the generator builds handlers from its declared commands, so the device accepts connections and answers them. **A Python driver with no `_sim.py` gets nothing at all**, and the failure does not name itself: the device simply never connects, and its card reports `connection_refused` and asks whether the port is right. The port is fine; there is no simulator listening on it. The server log is where it says so (`No simulator available for driver '<id>'`). Ship the `_sim.py` — scaffold it with the command below.

**Where this is documented:** [`docs/writing-simulators.md`](docs/writing-simulators.md) in this repository is the reference — the effort levels, the `simulator:` block section by section (initial state, delays, command and script handlers, state machines, error modes, controls, push and notifications), the Python simulator base class per transport, children in a simulator, and the validator. The `simulator:` block's fields are also in [`avcdriver.schema.json`](avcdriver.schema.json) like every other block, and the platform-side view of how simulation runs is [Device Simulator](https://docs.openavc.com/simulator/).

Scaffold a Python driver's simulator from its `DRIVER_INFO`:

```bash
python -m openavc.simulator.scaffold path/to/my_driver.py
```

Check that a driver and its simulator still agree with `python -m openavc.simulator.validate` (section 8). Section 10 lists the simulator dispatch behaviors that are not obvious from the schema and have each caused a silent bug.

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
├── utility/             # Wake-on-LAN, relays, bridges, miscellaneous helpers
├── docs/                # Contributing guide, writing simulators
├── scripts/             # Build + validation scripts (build_index.py)
│   └── _vendor/         # Generated copies of the platform's validation rules — never edit
├── index/               # Generated per-category index shards
├── devices/             # Generated per-category device shards
├── tests/               # Driver tests
├── index.json           # Generated driver catalog
├── devices.json         # Generated device catalog
├── manufacturers.json   # Manufacturer registry
└── AGENTS.md            # This file
```

**A driver only counts if it sits in one of the ten category directories above.**
`build_index.py` scans exactly those, so a driver file anywhere else — including
`devices/`, which holds generated shards — is skipped without a word: the
catalog builds clean, CI passes, and the driver never appears in Browse
Drivers. There is no "miscellaneous" directory; gear that fits nowhere else
goes in `utility/`.

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

**`index.json` and `devices.json` are generated artifacts — never hand-edit them.** They are produced by `scripts/build_index.py` from the metadata declared in each driver file, and they belong in the same commit as the driver that changed them. `--check` fails when they are out of date, and a stale entry is not cosmetic: the catalog carries a SHA-256 per driver file, and OpenAVC refuses a download whose bytes don't match, so a driver with a stale entry cannot be installed at all.

Add metadata to the driver file itself: top-level YAML keys for `.avcdriver`, or inside the `DRIVER_INFO` class attribute for `.py` drivers. Then run `python scripts/build_index.py` to regenerate, and `python scripts/build_index.py --check` to validate.

Each generated entry also carries a `files` map — repo-relative path to SHA-256 — covering the driver file and any companion an install fetches with it. OpenAVC hashes what it downloads and compares before writing it to disk, so a driver whose bytes don't match the catalog is refused rather than installed. Two consequences worth knowing: the hashes are computed from the files, never read from them (declaring your own `files` key does nothing), and editing only a companion still changes the catalog, so CI's post-merge rebuild is what keeps the hashes true.

**`scripts/_vendor/` is also generated — never edit it.** It holds copies of the platform's own driver-validation rules, produced by `scripts/vendor_platform_contract.py` from the OpenAVC platform repo, so a driver that passes catalog review is exactly a driver the platform will load. CI fails when the copies drift from the platform's current files; only a maintainer regenerating after a platform contract change should ever touch that directory.

**The JSON Schemas at the repository root are generated too — never edit them.** `avcdriver.schema.json` (YAML drivers) and `pythondriver.schema.json` (the `DRIVER_INFO` dict of Python drivers, which additionally allows the `ssh`/`mqtt` transports and `kind: "setup"` actions) are rendered by the platform repo from its driver-contract definition and byte-copied here by the same `scripts/vendor_platform_contract.py`. A field change starts on the platform side; the vendor script brings it here.

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
| `min_platform_version` | string | Minimum OpenAVC version (semver). Omit only when the driver uses no field that carries a floor — the build computes the floor and rejects a driver declaring less than it, or nothing at all. Don't guess it; see section 8. |
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

When real-hardware results come in (via a [Driver test report](https://github.com/open-avc/openavc-drivers/issues/new?template=driver-test-report.yml) issue or a PR), raise a model's confidence from `untested` to `partial` or `full` and split it into its own `compatible_models` entry if its results differ from the rest of the family. Never set `verified: true` yourself — that flag is maintainer-controlled and always submitted as `false`.

### How models become discoverable

The build script reverse-indexes every `compatible_models` entry into `devices.json`, so a user searching "Epson EB-L1075U" in Browse Drivers finds the right driver. Generic protocol drivers (PJLink, SNMP, ONVIF, etc.) leave their own `compatible_models` mostly empty — their device coverage comes from `devices-extra.json` entries that point at them.

---

## 8. Validation

Validate the driver before submitting:

```bash
python scripts/build_index.py            # Rebuild the catalog from the driver files
python scripts/build_index.py --check    # Validate — this is what CI runs
```

Commit `index.json`, `devices.json`, and the shards under `index/` and `devices/` alongside the driver change that produced them. They are generated, so never hand-edit them — regenerate instead. `--check` fails when what is committed no longer matches the driver files, and names the command that fixes it.

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
- `min_platform_version` covers every field the driver uses

That last one is computed, not looked up. Fields the platform grew in a
particular release say so in their schema description ("Requires platform
0.23.0"), and the check reads the same annotation: declare less than the
highest floor your driver reaches — or nothing at all — and the build fails
naming the field that set it. Don't guess the number and don't raise it "to be
safe": the version is exactly what stops the driver installing on older
releases, so an inflated floor locks out users who could have run it, and a low
one installs on a release that reads the file, ignores the fields it doesn't
know, and runs the driver wrong.

Those are all checks of what the driver **declares**. Nothing above reads a
Python driver's code. That half is covered by the test you ship with it.

`build_index.py` speaks for the whole catalog, so it needs this repo's layout,
its `manufacturers.json`, and a rebuilt index. To check **one file**, at any
path, with none of that present — a driver mid-write, or one built for a single
job that will never be contributed — use the platform's checker from an OpenAVC
checkout:

```bash
python -m openavc.drivers.check path/to/my_driver.py
python -m openavc.drivers.check path/to/my_driver.avcdriver
python -m openavc.drivers.check path/to/a/folder/
```

It defines no rules of its own: the verdicts come from the same functions
`build_index.py --check`, the Driver Builder's save, and the runtime loader
call, so all four say the same sentence. It prints nothing when a single file
is clean, exits non-zero when anything is wrong, and prints one line per
problem:

```
my_driver.py: error: commands.power_on: unknown key 'labl' (did you mean 'label'?)
```

Reach for it first on a **Python** driver. A `.avcdriver` gets live editor
feedback from its `# yaml-language-server:` schema line; a `DRIVER_INFO` dict
has no equivalent, so a misspelled key there is invisible until the section it
belongs to silently does nothing at runtime. `python -m openavc.simulator.validate`
runs the same check before its own parity checks.

It also checks that a driver's declarations agree with each other. Anything in
a driver that names something else in the same driver has to resolve:

| Reference | Must name |
|---|---|
| `commands.*.params.*.child_type` | a key of `child_entity_types` |
| `actions[].command` (or the entry's `id` when there is no `command`) | a key of `commands` |
| `quick_actions[]` | a key of `commands` |
| a `child_id` param's `min` / `max` | a range inside that type's `id_format` |

None of these announces itself at runtime. A typo'd `child_type` falls back to
plain integer handling at dispatch, so the failure blames the value the user
typed rather than the driver; an `actions` entry naming no command still
renders a live button on the device page that fails when pressed.

```
my_driver.py: error: commands.set_zone_level.params.zone: child_type 'zne' is not a declared child_entity_type (declared: zone)
my_driver.py: error: actions[3]: command 'query_evrything' is not a declared command
```

It reports its own coverage too. A `DRIVER_INFO` value built by code rather
than written as a literal cannot be read from the source, and the keys under it
go unchecked — those spots are named in a note line rather than passed over, as
are the two structural checks that need the loaded driver class
(`run_setup_action` for a `kind: "setup"` action, `set_device_setting` for
`device_settings`).

The same honesty applies to the references above. When the thing a reference
points **at** is built at runtime — a driver merging its `commands` in from a
module constant, or filling them in once the device says what it supports —
the check cannot decide that reference, so it skips it and names the skip:

```
my_driver.py: note: cross-reference not checked — 6 action/quick_action reference(s) into commands — commands is only partly visible (15 key(s) read, the rest merged or built at runtime)
```

That is deliberate. A subset of the target set proves nothing about what is
missing from it, so reporting a working driver as broken would be worse than
saying plainly that the check could not run. The skip is always per reference,
never per driver — everything else in the file is still checked. Write your
`DRIVER_INFO` as literals wherever it is reasonable and you get the checks;
compute it and you get a named gap instead.

Catalog CI treats these as **errors**; the runtime loader logs them as
**warnings**, so a driver already installed keeps working and reports the
problem rather than vanishing and taking its devices offline with it.

### 8.1 Writing the test

`tests/` is a pytest suite and CI runs it (`python -m pytest tests/ -v`). Ship
a test with every Python driver. A YAML driver has no code of its own to
unit-test — the platform that interprets it is tested in the OpenAVC repo — so
it normally needs none.

**CI installs `requirements-dev.txt` and nothing else, so there is no `openavc`
package in this repo.** A driver's `from openavc.drivers.base import BaseDriver`
has nothing to resolve against, and every test here puts stand-ins into
`sys.modules` before loading the driver.

**Do not write those stand-ins.** Import them from `tests/_platform_stubs.py`:

```python
from pathlib import Path

from _platform_stubs import (
    StubBaseDriver, StubEvents, StubState, install_stubs, load_module,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeBaseDriver(StubBaseDriver):
    """Only this driver's connect() ceremony. State handling — the
    device.<id>. namespace, the undeclared-state check, the whole
    child-entity registry — comes from StubBaseDriver."""

    async def connect(self):
        self.transport = _FakeTransport(self)
        self.set_state("connected", True)


install_stubs(base_driver=_FakeBaseDriver)
DRV = load_module("acme_under_test", REPO_ROOT / "devices" / "acme_widget.py")
```

What the module exports: `StubBaseDriver`, `StubState`, `StubEvents`,
`ConnectionFaultError`, `CommandParamError`, `UndeclaredStateError`,
`FrameParser`, `CallableFrameParser`, `DelimiterFrameParser`,
`StubBaseSimulator`, `StubTCPSimulator`, `StubHTTPSimulator`,
`StubUDPSimulator`, `StubProbeContext`, plus `install_stubs()`,
`stub_modules()` and `load_module()`.

`install_stubs()` takes per-module overrides for anything else the driver
imports. `stub_modules()` returns the same tree without installing it, for a
driver that resolves a transport lazily at call time and so needs the stubs
re-installed per test:

```python
install_stubs(
    {"openavc.transport.ir_codec": {"IRCode": _FakeIRCode}},
    base_driver=_FakeBaseDriver,
)
```

`tests/_lifecycle_fake.py` carries the connect/disconnect/liveness half that
several fakes share; inherit from both when a driver uses the platform's
lifecycle hooks.

### 8.2 Why the stand-ins are shared

A stand-in cannot disagree with whoever wrote it — it *is* their belief about
the platform, so a test passing against it confirms the belief, not the
platform. Drivers shipped that way: a resync path tested against a frame parser
that in reality wedged after one corrupt frame, and connection-fault tests
asserting an attribute the platform does not have.

`tests/test_platform_stub_fidelity.py` is what stops that. It signature-compares
every stubbed method against the real class and replays behaviour side by side,
whenever the OpenAVC checkout is present:

```bash
OPENAVC_PLATFORM_ROOT=../openavc OPENAVC_REQUIRE_PLATFORM=1 \
    python -m pytest tests/test_platform_stub_fidelity.py -v
```

It skips when the platform is absent, so a run with only this repo cloned stays
green, and fails loudly when `OPENAVC_REQUIRE_PLATFORM=1` promised it. If you
need a stand-in the shared module lacks, add it there **and** to that test's
`PAIRS` list. Never write a private one.

### 8.3 Strict driver state is on

`tests/conftest.py` sets `OPENAVC_STRICT_DRIVER_STATE=1`, so a write to a state
variable the driver never declared (see section 3) is a test failure, not a runtime
warning. It applies to any fake that inherits `StubBaseDriver`. Declare the
variable or stop writing it — a key built in a loop is the usual culprit.

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
  queries:
    - '{level_tag} get level 1\r\n'
    - '{mute_tag} get mute 1\r\n'
```

### 9.4 Python: Binary Protocol

```python
"""
Acme Binary Protocol Driver

Protocol: 3-byte header + payload + XOR checksum
  [0xAA] [CMD] [LEN] [DATA...] [XOR]

Source reference for BaseDriver API:
  https://github.com/open-avc/openavc/blob/main/openavc/drivers/base.py
"""

from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser


def _parse_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Extract one complete frame from buffer.

    The buffer returned is always what the parser keeps, message or not:
    unchanged means "wait for more data", shorter means "discard these bytes".
    The discard branch is what lets this driver resync after a bad packet.
    """
    if len(buf) < 4:
        return None, buf                    # unchanged: wait for more data
    if buf[0] != 0xAA:
        # Scan for the start byte and drop everything before it.
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

Your `responses:` list is tried in order; the first **regex** that matches the response wins, the rest are skipped. To pull multiple values out of one non-JSON response line, use **one** regex with multiple capture groups and **one** `set:` block with multiple keys — not two separate response entries. For a JSON reply body don't fight this with a mega-regex: use a `json: true` response (see section 2), which parses the body and applies every field mapping, so the first-match rule does not apply.

### Multi-line responses fan out per line

Some protocols answer a single bulk query with one key/value per line. The TCP/Telnet `delimiter` framing splits incoming bytes into one frame per line **before** response matching, so each line is matched independently against `responses:`. Write one `responses:` entry per key (`^iris\s+(\d+)$`, `^gain\s+(\d+)$`, ...) and one bulk query populates many state vars from one round-trip. Don't try to match the whole multi-line block as a single regex with `[\s\S]+` — that fights the per-line dispatch.

### HTTP body Content-Type behavior

When you set `body:` on an HTTP command, the runtime tries to parse it as JSON. If parsing succeeds, the request goes out as `Content-Type: application/json` with that JSON body. If parsing fails (e.g. XML, plain text), the body is sent as raw bytes with **no** `Content-Type` header. If your device strictly checks Content-Type for non-JSON bodies, set the right one explicitly via the command's `headers:` field (see section 2).

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
| Using `method`/`path` in TCP commands | TCP/serial commands use `send`, not HTTP fields. |
| Nested objects in state values | State values must be flat primitives: str, int, float, bool, None. |
| Invalid regex in response patterns | Test your regex. Avoid nested quantifiers like `(a+)+` which cause catastrophic backtracking. |
| Wrong delimiter for protocol | Check the device's protocol manual. Most AV devices use `\r`, not `\n` or `\r\n`. |
| Forgetting config substitution syntax | Use `{config_key}` (curly braces) for config values in commands and patterns. |
| Putting command parameters in `default_config` | `default_config` is for connection settings. Command parameters go in `commands.<cmd>.params`. |
| Category doesn't match directory | A driver in `audio/` must have `category: audio`. |
| YAML single-quote escaping for regex | In YAML, use `'\*Q'` not `'\\*Q'` for regex special chars in simulator command_handlers. |
| Simulator handler pattern ending in a space | The simulator strips whitespace off an incoming frame before matching, so a `command_handlers` pattern that ends in a literal space can never fire -- and it fails silently, with the simulated device answering an error to every such command while the driver works fine on hardware. If the protocol puts a space before its terminator, end the pattern `NC ?` (optional) rather than `NC `. |

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
