# Writing Device Simulators

This guide explains how to add simulation support to your OpenAVC driver. A simulator lets your driver work without real hardware by running a fake protocol server that responds just like the actual device.

Simulation support is optional but strongly recommended. It enables:

- Driver development and testing without hardware
- Demo environments for integrators evaluating OpenAVC
- Automated testing in CI pipelines
- Training for new AV programmers

## How Simulation Works

The [OpenAVC Simulator](https://github.com/open-avc/openavc) (included in the main OpenAVC repo at `simulator/`) is a standalone application that runs alongside OpenAVC. It discovers your driver files, starts fake protocol servers (TCP or HTTP), and responds to commands using the rules you define. OpenAVC drivers connect to these servers instead of real hardware. From the driver's perspective, it's talking to a real device.

```
OpenAVC                          Simulator
┌──────────┐     TCP/HTTP     ┌──────────────┐
│  Driver   │ ──────────────► │  Fake Device  │
│ (PJLink)  │ ◄────────────── │  (PJLink Sim) │
└──────────┘                  └──────────────┘
  Thinks it's                   Behaves like
  real hardware                 real hardware
```

## Effort Levels

| Level | Driver Type | Your Work | What You Get |
|-------|-------------|-----------|-------------|
| **0** | YAML (`.avcdriver`) | Nothing | Auto-generated simulator from your command/response definitions |
| **1** | YAML (`.avcdriver`) | Add a `simulator:` section | Enhanced realism: delays, state machines, error modes |
| **2** | Python (`.py`) | Run scaffold tool, fill in protocol logic | Full simulator with all framework features |
| **3** | Python (`.py`) | Custom connection handling | Advanced: auth handshakes, subscriptions, push messages |

---

## Level 0: YAML Auto-Generation (No Work Required)

If your driver is a YAML `.avcdriver` file, it already has everything the simulator needs. The simulator reads your `commands`, `responses`, and `state_variables` sections and automatically generates a working protocol responder.

**How it works:**

1. Your `commands` section tells the simulator what data to expect (incoming commands)
2. Your `responses` section tells the simulator what format to reply in
3. Your `state_variables` section tells the simulator what state to track

The simulator reverses these definitions: when it receives data matching a command's `send` template, it updates state and responds using the matching response format.

**Example:** Given this driver definition:

```yaml
commands:
  set_volume:
    send: "{level}V"
    params:
      level: { type: integer }

responses:
  - match: 'Vol(\d+)'
    set: { volume: "$1" }
```

The auto-generated simulator will:
1. Receive `60V` from the driver
2. Recognize it as `set_volume` with level=60
3. Update its internal `volume` state to 60
4. Respond with `Vol60` (matching the response format)

**What auto-gen handles:** Commands with parameters, queries, boolean toggles (`mute_on`/`mute_off`), state tracking.

**What auto-gen does NOT handle:** Realistic delays, power warmup/cooldown, error modes, authentication, unsolicited push messages, commands the simulator can't infer from naming conventions. For these, add a `simulator:` section (Level 1).

---

## Level 1: YAML Enhancement (15 Minutes)

Add a `simulator:` section to the end of your `.avcdriver` file to enhance the auto-generated behavior. Everything in this section is optional and merged on top of what the auto-gen produces.

### Initial State

Override the default state values. Without this, the auto-gen uses type defaults (0 for integers, false for booleans, empty string for strings).

```yaml
simulator:
  initial_state:
    input: 1
    volume: 50
    mute: false
    firmware: "2.1.0"
    model: "DXP 168"
```

### Delays

Add realistic response timing:

```yaml
simulator:
  delays:
    command_response: 0.02   # 20ms delay before responding (seconds)
```

### Command Handlers

Define handlers for commands the auto-gen can't infer. These are merged with auto-generated handlers. If a pattern matches both an explicit handler and an auto-generated one, the explicit handler wins.

```yaml
simulator:
  command_handlers:
    # Handle a query that auto-gen can't link to a state variable
    - receive: "N"
      respond: "DXP 168\r\n"

    # Handle a query using current state
    - receive: '\*Q'
      respond: "Ver{state.firmware}\r\n"

    # Handle a command with a capture group
    - receive: '(\d+)\.'
      set_state: { preset: "{1}" }
      respond: "Rpr{1}\r\n"
```

**Template handler fields:**

| Field | Description |
|-------|-------------|
| `receive` | Regex pattern to match incoming data (anchored to full line) |
| `respond` | Response template. `{1}`, `{2}` are regex capture groups. `{state.key}` inserts current state. |
| `set_state` | Dict of state changes to apply when matched |

**Regex escaping in YAML:** Use single quotes for patterns. Within single quotes, backslashes are literal, so `'\d+'` is the regex `\d+`. If you need a literal backslash in the regex, use `'\\'`. Do NOT double-escape: `'\\d+'` would be the regex `\\d+` (literal backslash followed by d), which is wrong.

### Script Handlers

For protocols that need conditional logic, math, or config variable access, use a `match:` + `handler:` pair with inline Python:

```yaml
simulator:
  command_handlers:
    - match: '#ROUTE (\d+),(\d+),(\d+)'
      handler: |
        mn = config.get("machine_number", "01")
        layer = match.group(1)
        output = match.group(2)
        inp = int(match.group(3))
        state["input"] = inp
        respond(f"~{mn}@ROUTE {layer},{output},{inp}\r\n")
```

**Script handler fields:**

| Field | Description |
|-------|-------------|
| `match` | Regex pattern to match incoming data (anchored to full line) |
| `handler` | Inline Python code executed when the pattern matches |

The handler code has access to:

| Variable | Description |
|----------|-------------|
| `match` | The regex match object (use `match.group(1)`, etc.) |
| `state` | Mutable state dict. Writes trigger UI updates. |
| `config` | Device config from the project file |
| `respond(text)` | Send a response to the driver. Include the protocol delimiter. |
| `int`, `float`, `str`, `bool`, `max`, `min`, `round`, `abs`, `len`, `format` | Built-in functions |

State changes made via `state["key"] = value` are reflected in the Simulator UI in real time.

**When to use script vs template handlers:** Use template handlers (`receive:` + `respond:`) for simple command/response patterns. Use script handlers (`match:` + `handler:`) when you need conditionals, math, config access, or complex state logic.

### State Machines

For devices with stateful transitions (projectors warming up, displays going through boot sequences):

```yaml
simulator:
  state_machines:
    power:
      states: [off, warming, on, cooling]
      initial: off
      transitions:
        # trigger matches command names from your commands: section
        - { from: off, trigger: power_on, to: warming }
        - { from: warming, after_seconds: 3.0, to: on }       # auto-transition after delay
        - { from: on, trigger: power_off, to: cooling }
        - { from: cooling, after_seconds: 2.0, to: off }
        - { from: warming, trigger: power_off, to: cooling }   # can interrupt warmup
        - { from: cooling, trigger: "*", reject: true }        # reject commands during cooldown
```

**Transition fields:**

| Field | Description |
|-------|-------------|
| `from` | State this transition applies in |
| `trigger` | Command name that triggers this transition, or `"*"` for any command |
| `to` | State to transition to |
| `after_seconds` | Auto-transition after this delay (no trigger needed) |
| `reject` | If true, commands matching this trigger are rejected (no response) |

The state machine's current state is stored as a state variable with the machine's name (e.g., `power`). When a command triggers a transition, the state updates automatically. Timed transitions (like warmup delays) happen in the background.

### Error Modes

Define error conditions that can be toggled on/off from the Simulator UI:

```yaml
simulator:
  error_modes:
    communication_timeout:
      description: "Device stops responding to commands"
      behavior: no_response

    garbled_response:
      description: "Corrupted data on the wire"
      behavior: corrupt_response

    lamp_warning:
      description: "Lamp approaching end of life"
      set_state: { lamp_hours: 19500 }

    overtemp:
      description: "Device overheating"
      set_state: { error_status: "002000" }
```

**Built-in behaviors:**

| Behavior | Effect |
|----------|--------|
| `no_response` | Simulator stops replying (tests timeout handling) |
| `corrupt_response` | Random byte corruption in responses |
| `disconnect` | Drop the TCP connection (tests reconnection) |

Error modes without a `behavior` field only apply `set_state` changes. This is useful for simulating device-reported errors (lamp warnings, temperature alerts) that change the device's status responses.

### Complete Example

Here's a full `simulator:` section for an Extron SIS switcher:

```yaml
simulator:
  initial_state:
    input: 1
    volume: 50
    mute: false
    firmware: "1.04"
    signal_active: true

  delays:
    command_response: 0.02

  command_handlers:
    - receive: "N"
      respond: "SIS 1616\r\n"
    - receive: '\*Q'
      respond: "Ver{state.firmware}\r\n"
    - receive: '(\d+)\.'
      respond: "Rpr{1}\r\n"
    - receive: '(\d+),\.'
      respond: "Spr{1}\r\n"

  error_modes:
    communication_timeout:
      description: "Device stops responding to commands"
      behavior: no_response
    garbled_response:
      description: "Corrupted serial data on the wire"
      behavior: corrupt_response
```

### Controls Schema

By default, the Simulator UI renders category-based panels (projector gets power/input controls, audio gets level/mute, etc.). For devices that need custom controls (matrix switchers with multiple outputs, multi-channel DSPs, devices with non-standard state), add a `controls` array to your `simulator:` section.

When a `controls` array is present, the UI renders those controls instead of the default category panel.

**Control types:**

| Type | What it renders | Required fields |
|------|----------------|-----------------|
| `power` | Power button with LED indicator | `key` |
| `select` | Button group | `key`, `options`; optional `labels` |
| `slider` | Range slider with value display | `key`, `min`, `max`; optional `step`, `unit` |
| `toggle` | On/off toggle button | `key`, `label` |
| `matrix` | Input x output routing grid | `inputs`, `outputs`, `state_pattern` |
| `meters` | Vertical level meter bars | `channels`, `key_pattern`; optional `mute_pattern` |
| `presets` | Numbered/named preset buttons | `key`; `count` or `names` |
| `group` | Groups other controls with a label | `label`, `controls` (nested array) |
| `indicator` | Read-only status display | `key`, `label`; optional `color_map` |

All control types also accept an optional `label` field.

**Matrix switcher example:**

```yaml
simulator:
  initial_state:
    route_1: 1
    route_2: 1
    route_3: 1
    route_4: 1
    volume: 50
    mute: false

  controls:
    - type: matrix
      label: Video Routing
      inputs: 8
      outputs: 4
      state_pattern: "route_{output}"
    - type: slider
      key: volume
      label: Master Volume
      min: 0
      max: 100
    - type: toggle
      key: mute
      label: Audio Mute
```

The `state_pattern` field uses `{output}` as a placeholder. For output 1, the state key is `route_1`; for output 2, `route_2`, and so on. Each state key holds the currently routed input number.

**Multi-channel DSP example:**

```yaml
simulator:
  controls:
    - type: group
      label: Channel 1
      controls:
        - type: slider
          key: ch1_level
          label: Level
          min: -80
          max: 12
          unit: dB
        - type: toggle
          key: ch1_mute
          label: Mute
    - type: group
      label: Channel 2
      controls:
        - type: slider
          key: ch2_level
          label: Level
          min: -80
          max: 12
          unit: dB
        - type: toggle
          key: ch2_mute
          label: Mute
    - type: presets
      label: Presets
      names: [Meeting, Presentation, Video Call, All Mute]
      key: active_preset
```

**Indicator with color mapping:**

```yaml
- type: indicator
  key: signal_active
  label: Signal
  color_map:
    "true": "#22c55e"
    "false": "#6b7089"
```

**Meters for multi-channel level display:**

```yaml
- type: meters
  label: Channel Levels
  channels: 4
  key_pattern: "level_{ch}"
  mute_pattern: "mute_{ch}"
```

The `key_pattern` and `mute_pattern` use `{ch}` as a placeholder, numbered starting from 1. The meter bars show the level value normalized to 0-100.

Controls are optional. Drivers without a `controls` array continue to use the default category-based panel. Most YAML auto-gen drivers work well with the defaults.

---

## Level 2: Python Simulator (30-60 Minutes)

For Python drivers with binary or complex protocols, write a companion simulator file. The scaffold tool generates a ready-to-edit skeleton.

### Step 1: Generate the Skeleton

```bash
cd openavc
python -m simulator.scaffold ../openavc-drivers/displays/samsung_mdc.py
# Creates: ../openavc-drivers/displays/samsung_mdc_sim.py
```

The skeleton includes:
- Class structure with `SIMULATOR_INFO` populated from your driver's `DRIVER_INFO`
- All state variables with types and default values
- All command names listed as reference comments
- Example code showing the pattern
- The correct base class (`TCPSimulator` or `HTTPSimulator`) chosen by transport type

### Step 2: Fill In the Protocol Logic

**For TCP drivers**, implement `handle_command(data: bytes) -> bytes | None`:

```python
from simulator.tcp_simulator import TCPSimulator


class SamsungMdcSimulator(TCPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "samsung_mdc",
        "name": "Samsung MDC Display Simulator",
        "category": "display",
        "transport": "tcp",
        "default_port": 1515,
        "initial_state": {
            "power": "off",
            "volume": 0,
            "mute": False,
            "input": "hdmi1",
        },
        "delays": {
            "command_response": 0.05,
        },
        "error_modes": {
            "no_signal": {
                "description": "No input signal detected",
            },
        },
    }

    def handle_command(self, data: bytes) -> bytes | None:
        # Samsung MDC uses binary frames: [0xAA][CMD][ID][LEN][DATA...][CHECKSUM]
        if len(data) < 4 or data[0] != 0xAA:
            return None

        cmd = data[1]
        payload_len = data[3]
        payload = data[4:4 + payload_len] if payload_len > 0 else b""

        if cmd == 0x11:  # Power
            if payload_len == 0:  # Query
                val = 0x01 if self.state["power"] == "on" else 0x00
                return self._ack(cmd, bytes([val]))
            else:  # Set
                self.set_state("power", "on" if payload[0] == 0x01 else "off")
                return self._ack(cmd, payload)

        if cmd == 0x12:  # Volume
            if payload_len == 0:
                return self._ack(cmd, bytes([self.state["volume"]]))
            else:
                self.set_state("volume", payload[0])
                return self._ack(cmd, payload)

        return None

    def _ack(self, cmd: int, data: bytes) -> bytes:
        """Build a Samsung MDC ACK response frame."""
        frame = bytearray([0xAA, 0xFF, cmd, len(data)]) + data
        checksum = sum(frame[1:]) & 0xFF
        frame.append(checksum)
        return bytes(frame)
```

**For HTTP drivers**, implement `handle_request(method, path, headers, body) -> (status, body)`:

```python
import json
from simulator.http_simulator import HTTPSimulator


class SonyBraviaSimulator(HTTPSimulator):

    SIMULATOR_INFO = {
        "driver_id": "sony_bravia",
        "name": "Sony Bravia Display Simulator",
        "category": "display",
        "transport": "http",
        "default_port": 80,
        "initial_state": {
            "power": "off",
            "volume": 20,
            "mute": False,
            "input": "hdmi1",
        },
    }

    def handle_request(self, method, path, headers, body):
        if path == "/sony/system" and method == "POST":
            req = json.loads(body)
            rpc_method = req.get("method", "")

            if rpc_method == "setPowerStatus":
                status = req["params"][0].get("status", False)
                self.set_state("power", "active" if status else "off")
                return 200, {"id": req["id"], "result": []}

            if rpc_method == "getPowerStatus":
                return 200, {"id": req["id"], "result": [{"status": self.state["power"]}]}

        if path == "/sony/audio" and method == "POST":
            req = json.loads(body)
            if req.get("method") == "setAudioVolume":
                self.set_state("volume", req["params"][0].get("volume", 0))
                return 200, {"id": req["id"], "result": []}

        return 404, {"error": [{"code": 404}]}
```

### Available Helpers

Inside `handle_command` or `handle_request`, you have access to:

| Helper | Description |
|--------|-------------|
| `self.state` | Dict of current state values (read-only copy) |
| `self.set_state(key, value)` | Update a state value (triggers UI refresh) |
| `self.get_state(key, default)` | Get a state value with default |
| `self.active_errors` | Set of currently active error mode names |
| `self.has_error_behavior(name)` | Check if any active error uses the given behavior |
| `self.config` | Device-specific config passed at startup |

### Custom Controls in Python

Python simulators can also declare custom controls via the `controls` key in `SIMULATOR_INFO`:

```python
SIMULATOR_INFO = {
    ...
    "controls": [
        {"type": "power", "key": "power"},
        {"type": "select", "key": "input", "options": ["hdmi1", "hdmi2", "vga"],
         "labels": {"hdmi1": "HDMI 1", "hdmi2": "HDMI 2", "vga": "VGA"}},
        {"type": "slider", "key": "volume", "min": 0, "max": 100, "label": "Volume"},
        {"type": "toggle", "key": "mute", "label": "Mute"},
    ],
}
```

The control types and fields are the same as for YAML drivers. See the Controls Schema section under Level 1 for the full reference.

### File Naming and Placement

- Simulator files use the `_sim.py` suffix
- Place them alongside the driver file in the same directory
- The file name should match: `my_driver.py` becomes `my_driver_sim.py`
- The simulator's `SIMULATOR_INFO["driver_id"]` must match the driver's `DRIVER_INFO["id"]`

```
openavc-drivers/
├── displays/
│   ├── samsung_mdc.py           # Driver
│   └── samsung_mdc_sim.py       # Simulator
├── projectors/
│   ├── pjlink_class1.py         # Driver
│   └── pjlink_class1_sim.py     # Simulator
```

---

## Level 3: Advanced Python Simulator

For protocols that need custom connection handling, override additional methods.

### Connection Greeting

Some protocols send a banner when a client connects (PJLink, Telnet-based devices):

```python
async def on_client_connected(self, client_id: str) -> bytes | None:
    """Called when a driver connects. Return greeting bytes or None."""
    return b"PJLINK 0\r"
```

### Custom Delimiter

If your protocol uses a non-standard line delimiter, set it in `SIMULATOR_INFO`:

```python
SIMULATOR_INFO = {
    ...
    "delimiter": "\r",      # PJLink uses \r, not \r\n
}
```

Without a delimiter, the simulator reads raw byte chunks (binary mode).

### Push Notifications

For protocols that send unsolicited messages (subscription-based updates):

```python
# Send data to all connected clients
await self.push(b"TALLY 12001\r\n")

# Send to a specific client
await self.push_to(client_id, b"UPDATE power=on\r\n")
```

### State Machines in Python

Trigger state machine transitions manually:

```python
def handle_command(self, data: bytes) -> bytes | None:
    text = data.decode().strip()
    if text == "POWER ON":
        self.set_state("power", "warming")
        self._schedule_warmup()
        return b"OK\r"

async def _schedule_warmup(self):
    await asyncio.sleep(3.0)
    self.set_state("power", "on")
```

### Reference Implementation

The PJLink simulator (`projectors/pjlink_class1_sim.py`) is the reference implementation for Level 3. It demonstrates:

- MD5 authentication handshake on connect
- Power state machine (off/warming/on/cooling with timed transitions)
- Input switching that rejects when power is off
- AV mute with combined video/audio modes
- Lamp hours and error status reporting
- Device info queries (name, manufacturer, class, available inputs)

---

## Testing Your Simulator

### Quick Test

Start the simulator with your driver and connect with a raw TCP client:

```bash
# Start the simulator
cd openavc
python -m simulator --driver-paths ../openavc-drivers

# In another terminal, connect to the simulated device
# (port is logged when the simulator starts)
telnet 127.0.0.1 19000
```

### Test with OpenAVC

1. Start OpenAVC with a project that uses your driver
2. Click the **Simulate** button in the Programmer IDE sidebar
3. The simulator starts and your driver reconnects to the simulated device
4. Open the Simulator UI (auto-opens in a new tab) to see state and protocol traffic
5. Use the Simulator UI controls to change device state and verify the driver reacts correctly
6. Click Simulate again to stop and restore real connections

### What to Verify

- All commands produce the expected responses
- State updates are reflected in the Simulator UI
- Polling queries return current state
- Error injection (timeout, corrupt data) works as expected
- If you have state machines, verify the timing and transitions

---

## Updating index.json

When your simulator is working, add `"simulated": true` to your driver's entry in `index.json`:

```json
{
    "id": "your_driver_id",
    ...
    "simulated": true
}
```

This adds a badge in the Browse Drivers view so users know your driver supports simulation.

---

## Best Practices

1. **Match real device behavior.** Study the device's protocol manual. If the device takes 3 seconds to warm up, your simulator should too. If the device rejects input changes when powered off, your simulator should too.

2. **Include error modes.** Every real device can fail. Add at least a `communication_timeout` error mode so users can test their timeout handling.

3. **Use realistic initial state.** Don't start with everything at zero. A real projector has lamp hours, a real display has a default volume, a real switcher has input 1 selected.

4. **Keep it simple.** You don't need to simulate every feature of the device. Focus on the commands your driver actually uses. A simulator that handles power, input, and volume is more useful than no simulator at all.

5. **Test with the actual driver.** The ultimate test is connecting your driver to your simulator. If the driver works against the simulator the same way it works against real hardware, you're done.
