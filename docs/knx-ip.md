# KNX/IP

Use a KNX/IP tunnelling interface to connect OpenAVC to an existing KNX
installation. One connection exposes named lights, shades, scenes and sensors
as child entities, available to UI bindings, macros and triggers. This Python
driver handles the binary protocol, encrypted sessions and an installation's
imported controls. It requires OpenAVC 0.40.1 or newer.

## Set up the connection

1. Add **KNX/IP** with the interface's IP address. Leave **Connection** on
   **Automatic**. **Find Interfaces** and **Select Interface** can find and
   choose interfaces on the selected local network. A normal network scan can
   also identify a tunnelling interface before this driver is installed.
2. Ask the KNX programmer for the ETS project (`.knxproj`), and its password
   if protected. Choose **Import ETS Project**. Names, rooms and explicitly
   assigned function roles create supported controls automatically. An ETS
   group-address XML export supplies named address choices instead.
3. For a secure installation, export the interface information/security keys
   from ETS and choose **Import Security Keys** with the `.knxkeys` file and
   its export password. This can differ from the project password. Configure
   a tunnel slot and its group-address associations in ETS before exporting.
4. Review the imported controls. Use **Add or Update Light**, **Shade**,
   **Sensor** or **Scene** for mappings the project does not explicitly define.
   Pick addresses by their imported names. The KNX programmer supplies missing
   datapoint types and command/status relationships; similar names alone do
   not establish those relationships.

Import keeps existing control IDs, names and mappings, so panel bindings survive
another import. A conflicting new datapoint type is refused before it can change
how a saved control writes to the bus. Use an update action to add or replace
addresses; blank fields retain their previous mapping. The advanced **Control
mappings** table also allows an address or a control to be removed.

Protected project import uses the ETS project-schema-21-and-newer password
format. Export an older project from a current ETS version, or use its group
XML export. Import one installation at a time, up to 128 MB, 10,000 addresses
and 512 named controls. Device application programs are not imported or changed.

## Controls and feedback

- **Lights:** on/off, brightness and relative brighter/darker/stop, according
  to the configured mappings. Only configured write properties appear in the
  control picker; the value field uses the property's actual type and range.
- **Shades:** up/down, step/stop, absolute position and slat position where
  mapped. Position is 0% open and 100% closed. Step/stop follows KNX semantics:
  it stops motion, or steps the slats when stationary.
- **Scenes:** named recall of scene 1–64. Recall never sends the learn/store
  bit. There is no invented current-scene feedback.
- **Sensors:** switches, alarms, occupancy, windows, percentages, counts,
  temperature, illuminance, wind speed, pressure, humidity, CO₂ and energy
  using the supported datapoint types shown in the setup action.

Values come from incoming status telegrams. A sent command, acknowledgement or
OpenAVC's own bus echo is not a measurement. Each reading has a companion
`*_valid` flag, plus a last-received time and feedback description on the
control. Configure separate status addresses where the KNX device supplies
them. After disconnect, readings become unknown; reconnect rereads every status
address at a bounded rate. Live telegrams update them immediately, with a periodic
refresh for missed updates. An address that does not answer remains unknown.

Child availability follows the interface connection; it does not prove that a
particular lamp or actuator is physically present. Use the received flags and
feedback description when the UI needs to distinguish a measured value from
one that has not arrived. In ETS, status objects must be set to transmit changes
and respond to group reads. A group that stays silent does not take the whole
interface offline. **Refresh Status** requests the configured status addresses
again without changing any load.

## Security and deployment

Automatic mode selects secure TCP when the interface or imported credentials
require it, otherwise TCP or UDP as advertised. Authentication failure never
falls back to plain traffic. Manual secure setup requires both the tunnel user
password and device authentication password, so the interface is authenticated
as well as the client. Imported credentials are kept in OpenAVC's secret storage.
The project password is used for the import and is not saved.

KNX Data Secure group traffic uses the exported group keys and sender list.
Receive counters are saved before publishing a reading; sending counters are
reserved before use, so a restart or clock rollback does not reuse them.
Retain the server's `driver-state/knx` folder inside its OpenAVC data directory
when moving or restoring the system. A project file alone does not contain this
security history. Missing history fails closed: restore it, or have the KNX
programmer replace the group keys in ETS before importing them again. Reimporting
the same keys cannot reset the replay protection. Simulation uses separate history.

The driver supports KNXnet/IP tunnelling, IP Secure and commissioned Data Secure
group communication. It does not provide multicast routing, ETS programming,
secure point-to-point management or automatic secure-counter commissioning.
Changing keys or commissioning devices remains an ETS task.

Allow outbound TCP/UDP port 3671 and its replies. Interface discovery requires
local multicast; a direct IP address works when multicast is unavailable, including
Docker bridge networking. UDP description requests are also used to identify the
interface and select a free exported tunnel slot. The configured OpenAVC network
adapter is used for the connection. No external bridge service is required.

Use the simulator to check programming and failure behavior. It is an independent
protocol implementation with encrypted sessions and Data Secure group traffic,
plus silent-status, ignored-command and rejected-login modes. Simulation is not
verification against physical KNX equipment.
