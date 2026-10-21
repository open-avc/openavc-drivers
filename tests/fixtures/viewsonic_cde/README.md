## From a CDE8631-1C, 2026-10-09

ViewSonic CDE8631-1C on TCP 5000, Monitor ID 01, firmware 20250609234259, no
Smart Hub accessory, with viewsonic_cde 2.1.0. Each file is one frame as the
display sent it, CR included, byte-exact except where noted:

- `get_device_name.response.txt`: the reply to Get-Device name
  (`801g4000\r`). The value is padded with spaces, where the protocol
  document pads with NULs. After a later update to firmware 20260526125505
  the same display reported `CDE8631`.
- `get_mac_address.response.txt`: the reply to Get-MAC address
  (`801g5000\r`). The last six hex digits of the MAC are replaced with
  `123456`; the length and the padding are the display's.
- `get_operation_hours.response.txt`: the reply to Get-Operation hours
  (`801g1000\r`).
- `get_ambient_sensors_standby.response.txt`: the reply to Get-Smart hub, all
  fields (`801g:000\r`), with the display in standby. The display answers
  from its own sensors without the accessory; the fields are not fixed-width,
  and light `-1` means no reading.
- `get_ambient_sensors_on.response.txt`: the same get with the display on.
  Firmware 20260526125505 pads the light value (`C00003`, here `C3`).
- `get_ambient_single_field.response.txt`: the answer to a single-field Smart
  hub get (`801g:00A\r` to `801g:00D\r`) with no accessory.
- `get_input_android.response.txt`: the input frame while the embedded
  Android source is showing (signal detected, source code `0A`), as the
  display reports it when it switches to that source.
- `freeze_on.response.txt`: what the display pushes after Freeze Picture
  (`801s*001\r`) and its ack, in the Function On_Off form the protocol
  document does not list for freeze.
- `set_ack.response.txt`: the display's acknowledgement of a set it accepts.

Not a file: the reply to Get-FW version. The only copy of it was put together
from the firmware string and a padding count that gives the protocol
document's reply size (31 bytes before the CR), not the 32 this display sends
in every other information reply above, so it is not the display's bytes. The
firmware string itself is the one named at the top.
