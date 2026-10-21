# ATDM-0604a captures

Byte-exact, from one Audio-Technica ATDM-0604a: serial 22510679, firmware
01.03.01, device ID 42 (the `42` in every answer). Each file ends in the
` \r` the mixer ends every message with; nothing is trimmed. The driver's
framing strips the CR before a reply rule sees a TCP answer.

## From a device audit, 2026-10-06

The unit at 192.168.4.38, audited with OpenAVC 0.37.0 and at_atdm_0604a 2.3.0
over TCP 17300, with its notices on multicast 239.0.0.100:17000. It reported
firmware 01.03.01 to the driver's own query during the audit. Each TCP file is
the raw receive chunk for one answer. Times are the audit's local time
(UTC-04:00).

- `startup_s_network.response.txt`: the answer to the driver's start-up step,
  `s_network S 0000 00 NC ,,,,,,1,,,,,,,,,,,, ` (it arms the IP Control
  Setting Notification flag), 21:10:30
- `identify.response.txt`: after `identify` (sent `identify S 0000 00 NC `),
  21:14:27
- `input_mute_on_2.response.txt`: after `input_mute_on` {"channel": "2"}
  (sent `SICM S 0000 00 NC 1,1 `), 21:20:02
- `rename_input_3_auditprobe.response.txt`: after `rename_input`
  {"channel": "3", "name": "AuditProbe"} (sent
  `s_input_channel_settings S 0000 00 NC 2,,,,,,,,,,,,,,,,,,,"AuditProbe",,,,,,, `),
  21:23:49
- `setting_front_led_dimmer_yes.response.txt`: after the Front Panel LED
  Dimmer setting (`front_led_dimmer`) was written Yes (sent
  `s_front_panel S 0000 00 NC ,1 `), 21:28:56
- `poll_g_input_gain_level_1.response.txt`: the poll's answer for input 2
  (wire channel 1), not muted, max volume off (sent
  `g_input_gain_level O 0000 00 NC 1 `), 21:10:30
- `poll_g_input_gain_level_1_muted.response.txt`: the same query 12 s after
  Input Mute On for input 2, 21:20:15. Mute is the seventh field (`1`); the
  fifth is max-volume enable (`0`). Driver 2.3.0 read the fifth as mute, so
  this poll wrote input 2 back to unmuted.
- `poll_g_input_gain_level_10.response.txt`: the poll's answer for the ST
  input (wire channel 10) (sent `g_input_gain_level O 0000 00 NC 10 `),
  21:10:30
- `output_mute_notice_0_0.multicast.txt`: the multicast datagram after
  `output_mute_off` {"channel": "1"} (sent `SOCM S 0000 00 NC 0,0 `),
  21:18:35. The same bytes were first captured from this unit on 2026-07-10.

## From the bench, 2026-07-11

- `input_gain_level_notice_0.multicast.txt`: a full-form
  `MD input_gain_level_notice` (no omitted fields: channel 0, both gains 0,
  level 400, unmuted), multicast by the unit at 192.168.4.72 during the live
  push check of at_atdm_0604a 1.1.0, after an input fader change sent over
  TCP. It is in neither audit report; the bytes are the ones kept from that
  session, ending ` \r` like every multicast notice the 2026-10-06 audit
  recorded.
