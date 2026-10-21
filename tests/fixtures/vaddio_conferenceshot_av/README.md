## Telnet failure replies from a ConferenceSHOT AV, 2026-09-15

Vaddio ConferenceSHOT AV on the bench, firmware 1.7.2, over the Telnet API on
port 23 the driver uses, written down while the driver was built against it.
These are the two ways the camera's shell reports a failure.

Each file is one reply block as the driver's parser receives it, not the raw
socket bytes: the Telnet option negotiation, ANSI colour and the BEL the shell
rings are already stripped, and the `\r\n> ` prompt that ends every block is
cut off, as the driver's framing does. Line endings are the shell's CRLF.
Neither reply carries a serial number or MAC address.

- `camera_ccu_scene_recall_factory_1.response.txt`: the reply to
  `camera ccu scene recall factory 1`, a command this camera does not have:
  the echo, then `Syntax error: Unknown or incomplete command` with no `ERROR`
  token. The shell eats the spaces between tokens it cannot complete, so on the
  wire an unknown command's echo comes back as `camera ccu scenerecallfactory1`;
  the echo line here reads as the command was sent and may have been written
  out that way rather than as received. The `Syntax error` line is the camera's.
- `camera_focus_near_4.response.txt`: the reply to `camera focus near 4` sent
  while auto focus was on: the echo, the camera's own sentence
  (`cannot perform manual focus operation while auto focus enabled`), then
  `ERROR`.
