# BACnet/IP

Use this driver to expose a controller's analog, binary and multistate inputs,
outputs and values to OpenAVC. Each configured point has its own name, value
and status for controls, macros, triggers and monitoring.

The driver requires the OpenAVC release that includes BACpypes3. OpenAVC 0.40.0
and earlier do not include it. No separate application or Python installation
is required on supported releases.

## Connect and choose points

1. Get the controller's IPv4 address, UDP port and approved point list from the
   facilities team. The usual port is 47808. The controller must be reachable
   from OpenAVC's control network.
2. Add **BACnet/IP** and enter that address and port. The controller's name,
   manufacturer, model, firmware and BACnet device instance appear when it answers.
3. Run **Import Points**. Enter a room name or prefix under **Name contains** to
   narrow the import. New points are read-only. Existing entries, labels and
   write permissions stay as they are. For a large controller, use the next
   object index shown by the result to continue.
4. Review **Points** in Edit Device. Remove points the room does not need, or
   add approved objects manually, such as `analog-value,1`. Up to 128 points
   can be configured. An empty display name uses the controller's current name.
5. Bind the point's **Value** and status fields to the room's controls or
   automation. **Set Point** offers the writable point, its Value property and
   the appropriate number, Yes/No or named-state input. A point's numeric unit,
   range and step come from the controller when available. **Refresh from
   Device** re-reads names and metadata for the configured points.

The client does not reserve or advertise a BACnet device instance of its own.
Multiple configured controllers can run together. Direct IP communication can
cross IP subnets when the network permits UDP; it does not require a BBMD.
Discovery uses a local Who-Is broadcast and identifies direct I-Am replies on
the standard port. Enter custom ports manually. BACnet routing to MS/TP,
foreign-device registration and BACnet/SC are outside this driver's scope.

## Enable control deliberately

Leave **Allow writes** off for monitoring points. To control a point, enable
it in its table row and agree the write method and priority with facilities.
Inputs remain read-only.

**Priority** is the default write method for commandable outputs and values.
Priority 1 is highest and 16 is lowest; priority 6 is reserved. OpenAVC defaults
to 16. **Release Point** sends NULL at that one configured priority so another
priority or the controller's default can take over. It never releases every
priority or changes the relinquish default.

Select **Direct** only for a writable value that facilities confirms does not
use a priority array. Direct writes have no priority to release. If a priority
array cannot be read, the driver refuses a priority write rather than changing
the write method on its own.

After a write or release, OpenAVC reads the actual value. A higher-priority
command may keep that value unchanged. **Active priority**, **Configured
priority has a value** and **Higher priority in control** explain what the
controller reports. A slot belongs to a priority, not to an individual client;
another system may write the same slot.

Writes can remain in the controller after OpenAVC disconnects or restarts.
They are not automatically released. Arrange an explicit release in the room's
automation where appropriate. Commands are not queued to run later while offline.

## Feedback and troubleshooting

The driver subscribes to changes where the controller supports COV. It renews
subscriptions before they expire and reconciles readings every five minutes.
When a subscription is refused, the point uses polling and retries COV later.
Each point shows **Feedback: cov** or **poll**. The poll interval defaults to
30 seconds. Controllers with a limited subscription pool can use both modes.

**In alarm**, **Fault**, **Overridden**, **Out of service**, **Reliability**,
**Value in service** and the last valid value's timestamp are available for
monitoring. A missing or unreadable point reports its own error. Device
connection status is checked separately, so a mistyped point does not make a
reachable controller appear offline.

Allow UDP replies and change notifications from the controller to OpenAVC's
outgoing UDP port. Docker host networking is simplest for local discovery;
with bridge networking, enter the controller address manually and ensure its
notifications can return through the NAT mapping. Local firewalls and Wi-Fi
client isolation can also block replies. If necessary, turn off **Use change
notifications** and use polling. BACnet/IP does not provide an encrypted login;
use the building's approved controls network.

The included simulator has temperature, setpoint, occupancy, occupied-mode and
named HVAC-mode points. Its controls generate actual BACnet changes. The
facilities override demonstrates a higher priority taking control; error modes
exercise refused writes, unavailable COV, individual-property reads and silence.
Simulator testing is complete; physical controller verification remains pending.
