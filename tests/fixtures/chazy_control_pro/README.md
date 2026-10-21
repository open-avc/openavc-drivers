# Chazy Control Pro captures

Replies from the bench TurtleAV Chazy Control Pro (TAV-CHAZY-CLTPRO, controller
firmware 1.10.11) over its telnet API on TCP 23. More captures from the same
unit are in `../chazy_control_pro_banners.py` and
`../chazy_control_pro_child_banners.py`.

Except where a file says otherwise, each `.response.txt` is the reply as the
driver's parser receives it, the same form as the `BANNER_*` values in
`../chazy_control_pro_banners.py`: the echoed command and the trailing
`CONTROLLER> ` prompt removed, CRLF folded to LF, and leading and trailing line
feeds stripped (what `ChazyControlProDriver._strip_echo` returns). Nothing else
is trimmed.

## Live read-only probe, 2026-05-30

The controller had one encoder and one decoder enrolled and no groups, events,
video walls or Dante presets. The controller's clock was on Sydney time, so the
date it reports is 2026-05-31.

- `get_group_status_empty.response.txt`: the reply to `GET GROUP STATUS` with no
  groups. The firmware titles it "Dante Preset Info" and sends no closing
  `====` line.
- `get_event_status_empty.response.txt`: the reply to `GET EVENT STATUS` with no
  events. Also titled "Dante Preset Info", also no closing line.
- `get_wall_status_empty.response.txt`: the reply to `GET WALL STATUS` with no
  video walls. Titled "Video Wall Info", and it does end with a closing `====`
  line.
- `get_dante_preset_status_empty.response.txt`: the reply to
  `GET DANTE PRESET STATUS` with no Dante presets. Titled "Dante Preset Info",
  with no closing line.
- `get_date.response.txt`: the reply to `GET DATE`: a `[SUCCESS]` prefix, the
  time zone in parentheses, and a closing full stop. (`GET NTP SERVER` answered
  `[SUCCESS]time.nist.gov.`, kept as `LINE_GET_NTP` in
  `../chazy_control_pro_banners.py`.)

## Live re-test, 2026-05-31

A live HDMI source on the encoder (TAV-CHAZY4K-TX) and a display on the
decoder.

- `get_enc_1_ss_status.response.txt`: the reply to `GET ENC 1 SS STATUS`, the
  encoder's secondary stream: an MJPEG main stream and no substream (`NA`).
  Fetching the main-stream URL returned live JPEG frames.

## Device audit, 2026-09-30

OpenAVC 0.35.0's device audit of the same controller, reading what port 23
sends on its own when a client connects.

- `connect_greeting.response.bin`: the connect greeting exactly as it arrives on
  the wire, nothing removed. It opens with twelve bytes of Telnet option
  negotiation, `FF FB 03 FF FB 01 FF FE 01 FF FD 00` (WILL SUPPRESS-GO-AHEAD,
  WILL ECHO, DONT ECHO, DO BINARY), followed by the welcome banner and the
  first `CONTROLLER> ` prompt, byte for byte the `RAW_GREETING` in
  `../chazy_control_pro_banners.py`. The driver's discovery probe, which sends
  nothing and reads this greeting, matched these same bytes in that audit.
