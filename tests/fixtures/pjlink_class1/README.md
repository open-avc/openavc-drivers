## Epson EB-L530U, 2026-10-21

Epson EB-L530U laser projector with PJLink turned on and a PJLink password
set; its firmware version was not recorded. Byte-exact, nothing trimmed:

- `../discovery/pjlink_class1.txt`: what the projector sent on a new TCP
  connection to port 4352, before anything was sent to it: `PJLINK 1`, then
  the 8 hex digits of the random challenge a projector with a password sends
  (new on every connection), then CR.
- `search.response.txt`: the projector's answer to a PJLink Class 2 search
  (`%2SRCH` and CR, broadcast to UDP port 4352): `%2ACKN=` and its MAC address
  written with colons, then CR. It arrived at UDP port 4352 on the computer
  that searched, not at the port the search was sent from, 2.5 to 5.9 s after
  the search.
