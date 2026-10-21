## Kramer SWT3-41-H by hand, 2026-10-12

Kramer SWT3-41-H (4x1 auto switcher), firmware 1.0.65739 (`#VERSION?`),
Protocol 3000 version 3000:02.11 (`#PROT-VER?`), over TCP port 5000, one
command at a time, each sent with a trailing CR. Each file is everything the
unit sent back to one command, byte-exact with its CRLF line endings; nothing
trimmed. Commands joined with `|` were sent as one line and answered one line
per command, in the order sent.

The unit speaks only the `ROUTE` routing dialect, so the legacy `VID`, `AUD`
and `AV` forms, and `#MUTE`, answer ERR 002. Error codes, from the Protocol
3000 reference: 001 syntax, 002 not available, 003 out of range, 005 internal,
031 device specific.

| File | Sent | What came back |
|---|---|---|
| `handshake.response.txt` | `#` | `~01@ OK ` (capital, trailing space) |
| `model_query.response.txt` | `#MODEL?` | the model |
| `version_query.response.txt` | `#VERSION?` | the firmware version |
| `info_io_query.response.txt` | `#INFO-IO?` | ERR 002: it does not report a matrix size |
| `vid_query_1.response.txt` | `#VID? 1` | ERR 002 |
| `vid_query_all.response.txt` | `#VID? *` | ERR 002 |
| `aud_query_1.response.txt` | `#AUD? 1` | ERR 002 |
| `route_query_output_2.response.txt` | `#ROUTE? 2,1` | ERR 003: it has one output |
| `mute_query_0_and_1.response.txt` | `#MUTE? 0\|MUTE? 1` | ERR 002 twice (its mute is `#AUD-MUTE`) |
| `aud_lvl_query_0_and_1.response.txt` | `#AUD-LVL? 1,0\|AUD-LVL? 1,1` | ERR 003 for channel 0, then output 1 at 0.0 dB |
| `signal_query_1.response.txt` | `#SIGNAL? 1` | no signal on input 1 |
| `route_input_2.response.txt` | `#ROUTE 1,1,2` | the route, echoed |
| `route_av_3_to_1.response.txt` | `#AV 3>1\|ROUTE 1,1,3` | ERR 002 for the `AV` form, then the route |
| `route_vid_4_to_1.response.txt` | `#VID 4>1\|ROUTE 1,1,4` | ERR 002 for the `VID` form, then the route |
| `route_all_input_2.response.txt` | `#AV 2>*\|ROUTE 1,*,2` | ERR 002, then ERR 001 for the all-outputs route |
| `route_input_0.response.txt` | `#ROUTE 1,1,0` (input 0, untie) | ERR 031 |
| `vmute_on.response.txt` | `#VMUTE 1,1` | video mute on, echoed |
| `vmute_blank.response.txt` | `#VMUTE 1,2` (blank) | ERR 005 |
| `set_volume_minus_12_5.response.txt` | `#AUD-LVL 1,1,-12.5` | the level in dB with one decimal |
