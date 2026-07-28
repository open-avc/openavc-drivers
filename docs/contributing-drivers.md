# Contributing Drivers

Guide for contributing device drivers to the OpenAVC community library.

## Quick Checklist

1. **Create your driver** using one of these methods:
   - **Driver Builder UI** in the Programmer IDE (visual wizard, exports `.avcdriver`)
   - **Write a `.avcdriver` file** by hand (YAML, no code — for text-based protocols)
   - **Write a Python driver** (subclass `BaseDriver` — for binary/complex protocols)

2. **Add device settings** if the device has configurable values (hostname, NDI name, video format, etc.) — see Device Settings below

3. **Add simulation support** so your driver works without real hardware (YAML drivers get this automatically, Python drivers need a `_sim.py` companion file) — see [Writing Simulators](writing-simulators.md)

4. **Test thoroughly** against real hardware or the [OpenAVC Simulator](https://github.com/open-avc/openavc) (included in the main repo at `simulator/`)

5. **Fork this repo** and add your driver to the appropriate category folder:
   - `projectors/` — Projectors
   - `displays/` — Commercial displays
   - `switchers/` — Matrix switchers, presentation switchers, scalers
   - `audio/` — DSPs, mixers, amplifiers, microphones
   - `video/` — Video production software (vMix, OBS, etc.)
   - `cameras/` — PTZ cameras
   - `lighting/` — DMX, Art-Net, sACN
   - `utility/` — Wake-on-LAN, relays, bridges

6. **Add metadata fields to your driver file** (NOT `index.json` — see below)

7. **Rebuild the catalog and validate.** The catalog is generated from the driver files, and it belongs in the same commit as the driver:
   ```bash
   pip install -r requirements-dev.txt
   python scripts/build_index.py          # regenerate index.json, devices.json, shards
   python scripts/build_index.py --check  # what CI runs
   ```

8. **Submit a pull request** with your driver file, the regenerated catalog files, and a `manufacturers.json` entry if your manufacturer is new.

## Driver Metadata

`index.json` and `devices.json` are **generated artifacts** — never hand-edit them. The driver file is the single source of truth: add metadata there, then regenerate with `python scripts/build_index.py` and commit the result with your driver.

For YAML drivers, metadata sits at the top level alongside `transport` and `commands`. For Python drivers, it goes inside the `DRIVER_INFO` class attribute.

### Required fields

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique identifier, lowercase + underscores (e.g., `extron_sis`). |
| `name` | string | Human-readable display name. |
| `manufacturer` | string | Must appear in `manufacturers.json`. Add it there first if it's new. |
| `category` | enum | One of: `projector`, `display`, `switcher`, `audio`, `camera`, `video`, `streaming`, `lighting`, `power`, `utility`. |
| `version` | string | Semver. Bump on every change. |
| `author` | string | Your name or GitHub handle. |
| `transport` | enum | One of: `tcp`, `udp`, `http`, `osc`, `serial`. |
| `description` | string | One paragraph for AV integrators. Plain language. |
| `source_url` | URL | The protocol document or canonical implementation you built from. No driver ships without this. |

### Optional fields

| Field | Description |
|-------|-------------|
| `ports` | TCP/UDP ports the device listens on (e.g., `[23]`, `[4352]`). |
| `protocols` | Protocol IDs that auto-discovery probes can identify (e.g., `["pjlink"]`). |
| `simulated` | `true` if a simulator covers this driver. |
| `verified` | `true` only after testing on real hardware. New contributions: leave `false`. |
| `min_platform_version` | Minimum OpenAVC version (semver). Omit when compatible with all versions. |
| `tags` | Lowercase, hyphen-separated keywords for Browse Drivers search (e.g., `["ndi", "ptz"]`). |
| `help` | `{ overview, setup }` block — both non-empty strings. Shown in Browse Drivers. |
| `deprecated` | Mark this driver as superseded. |
| `replacement_id` | Required when `deprecated: true`. Points at the replacement driver's `id`. |
| `compatible_models` | List of `{ manufacturer, models, confidence, notes? }` — see below. |

### `compatible_models` — what makes your driver discoverable

The build script reverse-indexes every `compatible_models` entry into `devices.json`, so AV integrators searching "Sony XBR-65X950H" in Browse Drivers find the right driver. Populate this list from the manufacturer's protocol manual or compatibility chart.

```yaml
compatible_models:
  - manufacturer: Biamp                      # Must match manufacturers.json
    models: [TesiraFORTE AVB, TesiraFORTE CI]
    confidence: full                          # full | partial | untested
    notes: AVB models lack analog I/O         # optional
```

Confidence values (chosen for honesty with AV integrators):

- `full` — driver controls every documented feature of these devices.
- `partial` — driver controls common features only. Some advanced features unsupported.
- `untested` — generic protocol driver expected to work but not specifically verified for these models.

Generic protocol drivers (PJLink, SNMP, ONVIF, Modbus, etc.) leave their own `compatible_models` mostly empty — their device coverage comes from `devices-extra.json` entries that point at them.

## Discovery

Every driver should declare a `discovery:` block. Two kinds of declarations:

- **Fingerprints** identify the driver alone — one match is enough. Examples: an mDNS service type, a TCP probe whose response includes a known string, a sibling Python companion file.
- **Hints** narrow candidates — several together produce a *possible* match with a candidate driver list. Examples: a MAC OUI prefix, a hostname regex, an open port, a manufacturer alias.

A driver with no `discovery:` block at all declares no signals. The build script logs a warning so you notice. The driver is still installable manually.

**Always declare hints alongside any fingerprint.** A fingerprint-only driver is fragile: a single device shows up on the network through several scanner paths — SSDP NOTIFY, mDNS announcements, banner-grab on the control port, or just an ARP-table sweep that captures the OUI. A driver declaring only one path matches that one and silently misses the rest, even when the scan already has the device's manufacturer string and hostname in evidence. Hints (`oui`, `hostname`, `port_open`, `manufacturer_alias`) cost nothing to declare and let the driver claim the device regardless of how it was found.

Recommended minimum for a driver that has a fingerprint:

```yaml
discovery:
  # Fingerprint — any one alone identifies this driver
  ssdp: "urn:schemas-upnp-org:device:ZonePlayer:1"

  # Hints — combine alongside any fingerprint
  oui: ["54:2a:1b", "b8:e9:37"]      # vendor's IEEE MAC blocks
  hostname: ["^Sonos-"]               # default factory hostname pattern
  port_open: [1400]                   # control port (TCP only)
  manufacturer_alias: ["sonos"]       # narrows when the scan captures
                                      # a manufacturer string from any source
```

Or for a device with no fingerprint we can match safely (hints only — surfaces as *possible*):

```yaml
discovery:
  oui:
    - "00:0a:45"
  manufacturer_alias:
    - "audio-technica"
```

The matcher is deterministic — there is no scoring. A signal either fires or it does not. Hints never produce *identified* on their own; they produce *possible (candidate: X)*, which is strictly better than *unknown* because the user gets a one-click choice. See the [Creating Drivers](https://github.com/open-avc/openavc/blob/main/docs/creating-drivers.md) guide for the full schema reference and validation rules.

### Adding a custom probe

If your device announces itself with a wire format that isn't already covered by the platform's passive listeners (mDNS / SSDP / AMX-DDP), declare a custom probe directly in the driver YAML:

1. **`tcp_probe:`** — connect to a port, optionally send a query, match the response. Sub-fields: `port`, exactly one of `send_ascii` / `send_hex` (or omit for connect-only banner reads), exactly one of `expect` (substring) / `expect_regex` / `expect_hex`, optional `tls: true` (TLS-wrap the connection without cert verification before send/read, so an HTTPS-only device can be fingerprinted from its landing page — e.g. `GET /` and `expect` a string from the HTML), optional `timeout_ms` (≤ 10000), optional `cross_vendor` flag, optional `extract_manufacturer:` (lifts a string into the manufacturer-alias enrichment path so peer drivers can claim the device via `manufacturer_alias`), optional free-form `extract:` rules for other metadata (model, version).

2. **`udp_probe:`** — broadcast on a port, listen for replies. Same sub-fields as `tcp_probe`. Useful for devices that respond to a directed broadcast on a vendor-specific port.

3. **`python:`** — sibling Python file. Use this when the wire format genuinely can't be expressed declaratively: multi-step handshakes, encrypted payloads, binary fixed-offset parsing, broadcast-then-per-host TCP follow-ups, or multicast with per-send UUIDs. Ship a sibling `<driver_id>_discovery.py` next to the `.avcdriver` and declare `python: {file: ./<driver_id>_discovery.py, cross_vendor: false}` in the YAML. The companion exposes `async def probe(ctx)`; the platform binds sockets via `ctx.source_ip`, exposes the engine's port-scan results via `ctx.hosts_by_open_port`, runs the probe on its own worker thread, and enforces a hard timeout (default 10 s, capped at 30 s). Use async I/O throughout — a probe that blocks in synchronous calls stalls only itself, but gets cut off at the timeout instead of cancelled cleanly.

The schema parser auto-registers two synthetic probe IDs (`custom_<driver_id>_companion_(udp|tcp)`) for `python:` declarations so the matcher binds the emitted evidence back to your driver — without the `python:` declaration the evidence won't drive identification. See `projectors/pjlink_class1_discovery.py` and `utility/crestron_cip_discovery.py` for canonical examples.

### Cross-vendor anchors

Some discovery signals identify a *protocol class* shared by many vendors (a multi-vendor projector control protocol, a multi-vendor camera discovery beacon, a control-system family beacon). Drivers hosting those signals declare `cross_vendor: true` on the relevant fingerprint. When a `cross_vendor: true` fingerprint matches, the matcher consults peer drivers' hints — a vendor-specific peer matching via `oui`, `hostname`, `manufacturer_alias`, or `port_open` becomes the primary driver, and the cross-vendor anchor moves to `alternatives[0]` in the dropdown on the Discovery card.

When you bump a driver to use a discovery field your platform target may lack, set `min_platform_version` in `index.json` so older OpenAVC instances grey out the driver instead of trying to parse fields they don't understand.

When the device's protocol fits none of these patterns, leave the `discovery:` block empty (or off) and open an issue describing the wire format — or contribute the listener / probe upstream as a generic core capability.

## Help Text

Drivers should include help text to assist users and the AI assistant:

- **Driver-level help** (`help.overview` and `help.setup`): What the driver controls and step-by-step connection instructions. Shown in the Add Device dialog.
- **Command help** (`help` field on each command): What the command does. Shown when selecting commands in the Programmer IDE.
- **Parameter help** (`help` field on each parameter): What values are expected. Shown below parameter input fields.

Example for a `.avcdriver` file:

```yaml
help:
  overview: Controls Extron SIS-compatible switchers over TCP or RS-232.
  setup: >
    1. Connect the device to the network.
    2. Default port is 23 (Extron telnet).

commands:
  set_input:
    label: Set Input
    send: "{input}!"
    help: Route a specific input to all outputs.
    params:
      input:
        type: integer
        required: true
        help: Input number (1-based)
```

For Python drivers, add help to `DRIVER_INFO`:

```python
DRIVER_INFO = {
    # ...
    "help": {
        "overview": "Controls Samsung displays via MDC protocol.",
        "setup": "1. Enable MDC in display settings.\n2. Default port is 1515.",
    },
    "commands": {
        "power_on": {
            "label": "Power On",
            "params": {},
            "help": "Turn on the display.",
        },
    },
}
```

## Device Settings

If the device has configurable values that live **on the hardware** (not just connection config), add a `device_settings` section to your driver. Good candidates: device hostname, NDI source name, video format, tally mode, operation mode.

Each device setting must include:
- **`type`**: `string`, `integer`, `number`, `boolean`, or `enum`
- **`label`**: Human-readable name
- **`help`**: Inline help text explaining what the setting does in context
- **`default`**: A default value
- **`state_key`**: Which state variable provides the current value (defaults to the setting key)

Optional flags:
- **`setup: true`**: Prompt the user to configure this setting when adding the device to a project
- **`unique: true`**: Auto-generate a non-clashing default (e.g., for NDI source names)

For YAML drivers, add a `write` section describing how to push the value to the device. For Python drivers, override `set_device_setting(key, value)`.

See the [Creating Drivers](https://github.com/open-avc/openavc/blob/main/docs/creating-drivers.md) guide for the full device_settings schema and examples.

## Device Log

All transport traffic (TX/RX) is automatically logged in the Programmer IDE's device log — you do not need to add any logging code for protocol communication. If your Python driver overrides `connect()` and creates its own transport, pass `name=self.device_id` so the log entries are tagged with the device name.

Add your own `log.info(f"[{self.device_id}] ...")` calls only for semantic events that interpret protocol data into meaningful state (e.g., "Power: warming" after parsing a status code).

## Testing Requirements

- Test all commands against real hardware or a simulator
- Verify response parsing returns correct state values
- Test connection and disconnection behavior
- For polled drivers, verify polling works at the configured interval

## Reporting Test Results

Many drivers ship at `verified: false`, or with `compatible_models` entries marked `untested` — they are built from the protocol manual and the simulator but have not been confirmed against the specific hardware. If you run a driver against real equipment, please report what you find. There are two ways, depending on what you saw.

**File a test report.** Use the [Driver test report](https://github.com/open-avc/openavc-drivers/issues/new?template=driver-test-report.yml) issue template (it carries the `test-report` label). Fill in the driver, the model(s) and firmware, what worked, and for anything that misbehaved, the command you sent, the response you expected, and the response you actually got. This is the right path when something did not work as documented, and the low-effort path if you would rather not edit YAML. The raw command/response detail lets the result be folded in correctly.

**Open a pull request.** If you are comfortable editing the driver, a test report usually becomes one or two edits in the `.avcdriver` file:

- Update `compatible_models`. It is an array of groups, so different models can carry different confidence. A model whose whole command surface worked goes to `confidence: full`; one with quirks gets its own entry at `confidence: partial` with a `notes:` line describing the deviation.
- Fix any commands or response patterns that did not match, and bump the driver `version`.
- Rebuild the catalog (`python scripts/build_index.py`) and validate (`--check`), committing the regenerated files with the driver.

Link the test-report issue from the pull request so the evidence and the change stay connected.

The `verified` flag stays maintainer-controlled: always submit `false`. A maintainer flips it to `true` once at least one model in `compatible_models` has been confirmed end to end on real hardware. Your report or pull request is what earns that.

## Naming Conventions

- Driver IDs: lowercase, underscores (e.g., `extron_sis`, `biamp_tesira`)
- One driver per device family, not per model
- Name should include manufacturer and protocol (e.g., "Extron SIS Protocol")

## License

All contributed drivers must be released under the **MIT License**. By submitting a pull request, you agree to license your driver under MIT.

## Validation

Validate your driver before submitting:

```bash
python scripts/build_index.py            # Rebuild the catalog from the driver files
python scripts/build_index.py --check    # Validate — this is what CI runs
```

`index.json`, `devices.json`, and the per-category shards under `index/` and `devices/` are generated from the driver files, and they belong in the same commit as the driver that changed them. `--check` fails when they don't match, naming the files and the command that fixes it, so a pull request cannot land a driver whose catalog entry is out of date.

That matters more than tidiness. The catalog records a SHA-256 for your driver file and for any companion that installs alongside it, and OpenAVC refuses a driver whose downloaded bytes don't match the checksum it was given. A stale catalog entry therefore doesn't merely look out of date — it makes that driver impossible to install. Rebuild whenever the files change, including when you only touched a companion.

To catch mistakes as you type, point your editor at the JSON Schema for the `.avcdriver` format. Add this line to the top of your driver file and any editor with YAML Language Server support (VS Code, Neovim, JetBrains, and others) will validate it live:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/open-avc/openavc-drivers/main/avcdriver.schema.json
```

The schema is checked into the repository root as [`avcdriver.schema.json`](../avcdriver.schema.json), generated from the OpenAVC platform's driver contract so it always matches what the platform actually loads. It covers the same rules CI enforces, so a file that validates cleanly against it is well on its way to passing `--check`.

## Using an AI Assistant

If you use an AI coding assistant, point it to [`AGENTS.md`](../AGENTS.md) in the root of this repository. It contains the complete YAML schema, Python driver API, naming conventions, and examples in a format optimized for LLM agents. Have your assistant run `python scripts/build_index.py --check` on its output to catch errors before you submit.

## Driver Creation Reference

For complete documentation on driver formats, the `.avcdriver` YAML schema, Python driver API, and the Driver Builder UI, see the [Creating Drivers](https://github.com/open-avc/openavc/blob/main/docs/creating-drivers.md) guide in the main OpenAVC repo.
