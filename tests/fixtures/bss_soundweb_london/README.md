## From a device audit, 2026-10-01

BSS Audio BLU-100, firmware 86.4.2, audited by an unnamed tester with OpenAVC 0.35.0 and bss_soundweb_london 1.1.3. Byte-exact:

- `../discovery/bss_soundweb_london.txt`: the reply to the driver's own discovery probe
- `recall_parameter_preset_0.response.txt`: what the device sent after `recall_parameter_preset` {"preset": 0} (sent hex 02 8c 00 00 00 00 8c 03), before the driver sent anything else
