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

7. **Run the build script** to regenerate `index.json` and `devices.json`:
   ```bash
   pip install pyyaml pydantic
   python scripts/build_index.py
   ```

8. **Submit a pull request** — CI will fail if you forgot to run the build script.

## Driver Metadata

`index.json` and `devices.json` are **generated artifacts**. Do not edit them by hand. The driver file is the single source of truth — add metadata there, then regenerate.

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

## Discovery Hints

Every driver should declare a `discovery:` block. A driver with no signals at all will silently never match anything; CI emits a warning at build time so you notice. Set `manual_only: true` to document that the device expects manual IP entry — that flag is documentation, not a matcher filter, so you should still declare any soft signals (OUI, hostname, vendor aliases) the device exposes.

**Always declare soft signals alongside any strong signal.** A strong signal alone is fragile: if the SSDP scanner misses the device's NOTIFY, if mDNS multicast is filtered between VLANs, if ONVIF is disabled in the camera menu, the strong signal never fires and the driver claims nothing — even when the discovery scan has the device's manufacturer string, hostname, and MAC address in hand. Soft signals (`oui_prefixes`, `vendor_aliases`, `hostname_patterns`, `open_ports`) are the safety net that makes the driver claim the device regardless of which scanner found it.

Required minimum for every driver with a strong signal:

```yaml
discovery:
  # Strong signal (any one of mdns_services / ssdp_device_types /
  # amx_ddp / onvif / active_probes / udp_broadcast_probe /
  # tcp_active_probe / companion).
  ssdp_device_types: ["urn:schemas-upnp-org:device:ZonePlayer:1"]

  # Soft fallback — REQUIRED alongside any strong signal:
  oui_prefixes: ["54:2a:1b", "b8:e9:37"]      # vendor's IEEE OUI blocks
  hostname_patterns: ["^Sonos-"]               # default factory hostname
  open_ports: [1400]                           # control port (TCP only)
  vendor_aliases: ["sonos"]                    # narrows when discovery
                                               # captures manufacturer
                                               # string from any source
```

Or for a device with no fingerprint we can match safely:

```yaml
discovery:
  manual_only: true
  oui_prefixes:
    - "00:0a:45"
  vendor_aliases:
    - "audio-technica"
```

**Why soft signals matter for "I already have a strong signal" drivers:** the same Sonos speaker can show up in a discovery scan via SSDP NOTIFY, mDNS `_spotify-connect._tcp.local`, mDNS `_sonos._tcp.local`, banner-grab on TCP 1400, or just an ARP-table sweep that captures the OUI. A driver declaring only the SSDP URN matches one of those five paths and silently misses the rest. A driver declaring SSDP plus OUI plus hostname plus port matches all five.

The matcher is deterministic — there is no scoring. A signal either fires (the device is identified) or it does not. Soft signals never produce `identified` on their own; they produce `possible (candidate: X)` which is strictly better than `unknown` because the user gets a one-click choice. See the [Creating Drivers](https://github.com/open-avc/openavc/blob/main/docs/creating-drivers.md) guide for the full schema (Tier 1 mDNS / SSDP / AMX DDP, Tier 2 broadcast probes, Tier 3 active probes, Tier 4 enrichment hints).

### Adding discovery support

If your device announces itself on the network and the wire format isn't covered by a built-in opt-in, you have two options before falling back to `manual_only: true`:

1. **Declarative probe** — for "send these bytes, look for this in the response" protocols, declare a `udp_broadcast_probe:` or `tcp_active_probe:` block directly in the `.avcdriver`. Parameters: `port`, `send: {hex|ascii}`, `response_match: {starts_with_hex, contains, regex}`, optional `timeout_ms` (≤10000), `generic` flag, and `extract:` rules. Reserved extract keys `manufacturer` / `make` feed the Tier 4 vendor_string path so peer drivers can claim the device by `vendor_aliases`. Built-in handler ports are reserved (mDNS/SSDP/PJLink/Crestron CIP/ONVIF/AMX DDP for UDP; the active-probe handler ports for TCP).

2. **Python companion** — for multi-step handshakes, binary parsers, or broadcast-then-per-host TCP follow-ups too dynamic for the declarative block, ship a sibling `<driver_id>_discovery.py` next to the `.avcdriver` *and* declare `discovery.companion: {generic: bool}` in the driver. The schema declaration auto-registers two synthetic probe IDs (`custom_<driver_id>_companion_(udp|tcp)`) so the matcher binds companion-emitted evidence back to your driver — without it, the evidence won't drive identification. The companion exposes `async def probe(ctx)`; `ctx.source_ip` is the binding the companion **must** use, and `ctx.emit_broadcast / emit_active / emit_oui` produce evidence routed back to the right device record. Hard wall-clock timeout enforced by the platform (default 10s, capped at 30s).

   Set `companion.generic: true` for cross-vendor anchor drivers (PJLink, Crestron CIP, etc.) — this lets the matcher demote the anchor to an alternative when a vendor-specific peer driver matches via `vendor_aliases` / OUI / hostname soft signals. See `projectors/pjlink_class1_discovery.py` and `utility/crestron_cip_discovery.py` for canonical examples.

When you adopt either Phase 9 / 9.7 schema, bump the driver's `min_platform_version` in `index.json` so older OpenAVC instances don't try to match against fields they can't parse.

When the device's protocol fits neither pattern, ship `manual_only: true` and open an issue describing the wire format — or contribute the listener / probe upstream.

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

## Naming Conventions

- Driver IDs: lowercase, underscores (e.g., `extron_sis`, `biamp_tesira`)
- One driver per device family, not per model
- Name should include manufacturer and protocol (e.g., "Extron SIS Protocol")

## License

All contributed drivers must be released under the **MIT License**. By submitting a pull request, you agree to license your driver under MIT.

## Validation

Run the validator before submitting:

```bash
python validate.py                              # Validate all drivers
python validate.py switchers/my_driver.avcdriver # Validate a specific driver
python validate.py --check-index                 # Also check index.json consistency
```

## Using an AI Assistant

If you use an AI coding assistant, point it to [`AGENTS.md`](../AGENTS.md) in the root of this repository. It contains the complete YAML schema, Python driver API, naming conventions, and examples in a format optimized for LLM agents. Have your assistant run `python validate.py` on its output to catch errors before you submit.

## Driver Creation Reference

For complete documentation on driver formats, the `.avcdriver` YAML schema, Python driver API, and the Driver Builder UI, see the [Creating Drivers](https://github.com/open-avc/openavc/blob/main/docs/creating-drivers.md) guide in the main OpenAVC repo.
