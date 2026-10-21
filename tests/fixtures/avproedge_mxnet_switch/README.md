## PoE replies from an AC-MXNET-SW8P, 2026-08-17

AVPro Edge AC-MXNET-SW8P on the bench, software V705R002C013 (BootRom 7.5.15,
hardware 1.0.2), over the CLI the driver uses. Each file is the switch's output
for one command, without the echoed command or the prompt after it. Column
alignment, spacing and the leading blank line are the switch's; line endings
are LF, as stored in the capture these were copied from. Nothing else is edited
(these three replies carry no serial number or MAC address).

- `show_power_inline_interface.response.txt`: `show power inline interface`,
  the full PoE table: ports 1 and 3 delivering power (port 3 is the encoder the
  PoE measurements were made against), 2 and 4-8 idle, and the four SFP+ ports
  answering with a sentence instead of a row
- `show_power_inline.response.txt`: `show power inline`, the switch-wide PoE
  state and the 125 W budget
- `show_power_inline_interface_ethernet_1_0_3.response.txt`:
  `show power inline interface ethernet 1/0/3`, the same table for one port
