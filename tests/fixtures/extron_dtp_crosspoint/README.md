## From a device audit, 2026-10-29

Extron DTP CrossPoint 84 4K IPCP MA, firmware 2.04, audited with OpenAVC 0.37.0 and extron_dtp_crosspoint 1.0.0. Byte-exact:

- `../discovery/extron_dtp_crosspoint.txt`: the reply to the driver's own discovery probe
- `route_3_1.response.txt`: what the device sent after `route` {"input": "3", "output": "1"} (sent "3*1!"), before the driver sent anything else
- `route_4_1.response.txt`: what the device sent after `route` {"input": "4", "output": "1"} (sent "4*1!"), before the driver sent anything else
- `route_3_2.response.txt`: what the device sent after `route` {"input": "3", "output": "2"} (sent "3*2!"), before the driver sent anything else
- `route_2_2.response.txt`: what the device sent after `route` {"input": "2", "output": "2"} (sent "2*2!"), before the driver sent anything else
- `route_1_1.response.txt`: what the device sent after `route` {"input": "1", "output": "1"} (sent "1*1!"), before the driver sent anything else
- `route_4_2.response.txt`: what the device sent after `route` {"input": "4", "output": "2"} (sent "4*2!"), before the driver sent anything else
