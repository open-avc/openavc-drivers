# Lunatone DALI-2 IoT lighting

This driver connects OpenAVC directly to a Lunatone DALI-2 IoT or IoT4 gateway.
It imports the gateway's registered lights, zones and stored sequences, using their
names in command pickers and feedback. No additional controller application needs
to remain running. Requires OpenAVC 0.37.1 or later.

## Supported gateways

- DALI-2 IoT: 89453886, 89453886-NR and 89453886-EM.
- DALI-2 IoT4: 22176625, 22176625-PS, 22176625-NR, 22176625-PS-NR,
  22176625-EM and 22176625-PS-EM.

These models share the manufacturer's documented HTTP/WebSocket API. The driver
covers routine lighting control; Node-RED administration and emergency-light
testing are not included. Compatibility is documentation-based and has not yet
been verified on physical gateways. DALI Display products are not included.

## Connect and name the lights

1. Commission and address the DALI gear with Lunatone's tools. Make the gateway's
   local API reachable from the OpenAVC computer.
2. Run OpenAVC's network discovery, or add **Lunatone DALI-2 IoT** with its IP
   address. The default HTTP port is 80.
3. Connect. The registered light list loads automatically. Light, zone and sequence
   names come from the gateway. Rename them in Lunatone's tools and use **Refresh
   from Device** to reload them immediately.
4. If commissioned lights are missing from the gateway's list, use **Scan Addressed
   Lights**. This registers gear that already has DALI addresses. It does not
   assign new addresses or reset the installation. Initial addressing remains a
   Lunatone commissioning task.
5. Add meaningful names under **Scene Names**, such as scene 1 = Presentation.
   The numbers are the API's DALI scene numbers, **0–15**. These labels are saved
   in the OpenAVC project; they do not program or overwrite a scene in a light.

UDP discovery uses port 5555 on the local network. Routed networks and Docker
bridge networking may block discovery broadcasts; entering the IP address works
when the gateway is reachable. HTTP requests and WebSocket updates are outbound
connections to the gateway's HTTP port. The documented API has no authentication
or encrypted-transport procedure, so use a trusted control network.

## Control and feedback

Each light exposes only the capabilities the gateway reports: on/off, brightness,
color temperature, RGB, WAF or xy color. **Set Light Control** narrows its Control
picker to that light's on/off, brightness and color-temperature controls. Color
components use the manufacturer's normalized 0–1 values, not 0–255.

Use **Zones** for configured groups of lights, including groups spanning IoT4
lines. Zone commands read the individual lights back; there is no invented
combined zone brightness. **Sequences** use the gateway's names and report their
running and enabled states. **Scenes** are recall selections, not current-state
indicators: the API does not document active-scene feedback. All 16 scene numbers
are offered; a local label does not prove that every targeted light has that scene
programmed.

IoT4 lights are addressed using the gateway's unique device IDs. Their displayed
DALI line and short address are informational; identical short addresses on
different lines remain separate lights. Line numbers follow the API, starting at
zero. The driver deliberately uses configured zones instead of guessing how the
older single-line group and broadcast endpoints address multiple lines.

Live WebSocket updates keep readings current. Commands read status back rather
than assuming success. Reopening the live connection reloads the full lists and
readings. The default 30-second poll also reconciles changes; keep it enabled for
a periodic status check. **Live Feedback Connected** distinguishes the event
connection from HTTP reachability. Unknown or omitted readings are shown as
unknown. A full disconnect clears changing light and sequence readings until
fresh status arrives.

Feedback describes the gateway's reported state. A registered light is not proof
that the physical gear is responding; the driver does not invent a per-light
presence signal. Gateway error information is exposed separately.

## Simulation and qualification

The companion simulator provides fictional named lights, a relay, a zone and a
sequence. **DALI Lines for Simulation** selects one or four lines; real gateways
report their own line count. Simulator controls exercise live brightness changes,
light additions/removals, missing readings and an interrupted event connection.
Its example scene values are fictional, not factory defaults.

OpenAVC's Device Audit includes everyday lighting actions, expected feedback,
and HTTP/WebSocket traffic. Physical qualification should confirm discovery,
commissioned names and capabilities, scene numbering, external-controller changes,
multi-line selection and recovery after a gateway restart. Successful simulation
checks the implementation against the documentation; it does not replace this
hardware qualification.

Protocol references: [M0023 API guide](https://www.lunatone.com/wp-content/uploads/2021/08/89453886_DALI2_IOT_API_Dokumentation_EN_M0023.pdf),
[IoT datasheet](https://www.lunatone.com/wp-content/uploads/2021/08/89453886_DALI2_IOT_EN_D0099.pdf),
[IoT4 datasheet](https://www.lunatone.com/wp-content/uploads/2024/11/22176625_DALI2_IOT4_EN_D0133.pdf).
