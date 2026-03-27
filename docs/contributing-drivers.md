# Contributing Drivers

Guide for contributing device drivers to the OpenAVC community library.

## Quick Checklist

1. **Create your driver** using one of these methods:
   - **Driver Builder UI** in the Programmer IDE (visual wizard, exports `.avcdriver`)
   - **Write a `.avcdriver` file** by hand (YAML, no code — for text-based protocols)
   - **Write a Python driver** (subclass `BaseDriver` — for binary/complex protocols)

2. **Add device settings** if the device has configurable values (hostname, NDI name, video format, etc.) — see Device Settings below

3. **Test thoroughly** against real hardware or a protocol simulator

4. **Fork this repo** and add your driver to the appropriate category folder:
   - `projectors/` — Projectors
   - `displays/` — Commercial displays
   - `switchers/` — Matrix switchers, presentation switchers, scalers
   - `audio/` — DSPs, mixers, amplifiers, microphones
   - `video/` — Video production software (vMix, OBS, etc.)
   - `cameras/` — PTZ cameras
   - `lighting/` — DMX, Art-Net, sACN
   - `utility/` — Wake-on-LAN, relays, bridges

5. **Update `index.json`** with your driver's metadata entry

6. **Submit a pull request**

## index.json Entry Format

Add an entry to the `drivers` array in `index.json`:

```json
{
    "id": "your_driver_id",
    "name": "Human-Readable Driver Name",
    "file": "category/your_driver_id.avcdriver",
    "format": "avcdriver",
    "category": "switcher",
    "manufacturer": "Manufacturer Name",
    "version": "1.0.0",
    "author": "Your Name",
    "transport": "tcp",
    "verified": false,
    "description": "One-line description of what equipment this controls.",
    "protocols": ["your_protocol_name"],
    "ports": [23]
}
```

| Field | Description |
|-------|-------------|
| `id` | Unique identifier, lowercase with underscores |
| `file` | Path relative to repo root |
| `format` | `"avcdriver"` for YAML, `"python"` for .py |
| `category` | One of: projector, display, switcher, audio, video, camera, lighting, utility |
| `transport` | Primary transport: tcp, serial, udp, http |
| `verified` | Set to `false` for new contributions (maintainers verify) |
| `protocols` | Protocol IDs that discovery probes can identify (e.g., `["pjlink"]`, `["extron_sis"]`). Helps discovery suggest your driver when it detects a matching protocol on the network. Leave as `[]` if your protocol isn't auto-detected. |
| `ports` | TCP ports the device typically listens on (e.g., `[23]`, `[4352]`). Used by discovery to match open ports to drivers. |

## Discovery Hints

If your driver targets a specific device family, add a `discovery` section to the `.avcdriver` file to help OpenAVC's network scanner identify devices and suggest your driver:

```yaml
discovery:
  ports: [23]
  mac_prefixes: ["00:05:a6"]
```

See the [Creating Drivers](https://github.com/open-avc/openavc/blob/main/docs/creating-drivers.md) guide for the full list of discovery hint fields (ports, MAC prefixes, mDNS services, hostname patterns).

Even without explicit discovery hints, the driver's `manufacturer`, `category`, and `default_config.port` are used as basic matching signals. Adding hints makes discovery noticeably more accurate.

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

## Driver Creation Reference

For complete documentation on driver formats, the `.avcdriver` YAML schema, Python driver API, and the Driver Builder UI, see the [Creating Drivers](https://github.com/open-avc/openavc/blob/main/docs/creating-drivers.md) guide in the main OpenAVC repo.
