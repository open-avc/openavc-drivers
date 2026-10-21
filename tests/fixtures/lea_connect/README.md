# LEA Connect captures

## Device audit, 2026-09-30

A LEA Professional Connect Series 352D (a two-channel Dante model), firmware
3.1.7.0, on the bench, audited with OpenAVC 0.35.0. Byte-exact:

- `../discovery/lea_connect.txt`: the reply to the driver's own discovery
  probe, `get /amp/deviceInfo/modelID\n` sent to TCP 4321. The amplifier names
  its model in its own words, quoted, and ends the line with a line feed.
