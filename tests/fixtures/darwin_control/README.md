## Connect greeting from a Darwin Control controller, 2026-05-29/30

TurtleAV Darwin Control (CTL100AL controller), firmware 1.50.02 (welcome
token `Controller(h)`), on the bench during the driver's bring-up on
2026-05-29 and 2026-05-30. Byte-exact:

- `connect.response.txt`: everything the controller sends after a TCP
  connect to port 23, before anything is sent to it: the Telnet option
  negotiation `FF FB 03 FF FB 01 FF FE 01 FF FD 00` (WILL SGA, WILL ECHO,
  DONT ECHO, DO BINARY), then the welcome banner through the first
  `CONTROLLER> ` prompt. The controller sends the negotiation in one TCP
  segment and the banner in a later one; the file joins them in the order
  they arrived. Nothing trimmed.

The banner alone (after the negotiation is stripped) is also `RAW_GREETING` in
`../darwin_control_banners.py`, which holds the rest of this unit's captures.
