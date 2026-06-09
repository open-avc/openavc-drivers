# NETGEAR M4250 live CLI captures

Verbatim, byte-exact CLI output from a real **NETGEAR M4250-40G8XF-PoE+**
(software 13.0.5.14, bootcode 1.0.0.13), captured 2026-06-07 over the switch's
telnet CLI. `tests/test_netgear_m4250_live.py` replays these through the
driver's parsers, so the parsers are validated against what the hardware
actually emits — not just the CLI-manual layout the synthetic
`netgear_m4250_m4350_outputs.py` fixtures cover.

Each `.txt` is the raw capture: the echoed command on the first line, the
output body, then the trailing CLI prompt. The replay loader strips the echo
and the prompt, leaving exactly what the driver's framing hands a parser.

- `factory/` — read-only baseline at factory defaults (no PoE device attached).
- `with-poe/` — re-captured with a live PoE endpoint (a Dante device, ~6–7 W,
  class 4) on port `0/1`, so `show poe port info` shows real draw and
  `show igmpsnooping group` shows a real multicast subscription table.
- `commands/` — mutating-command transcripts in `===== CMD: <command> =====`
  form: cable diagnostics and the port-description quoting behaviour.

Why this family needed its own captures: the hardware differs from the manual in
ways the synthetic fixtures couldn't show — memory reported in KBytes rather
than bytes, `Bootcode Version` rather than `Boot Code Version`,
`IGMP Snooping Querier Mode` rather than `Admin Mode`, and the trailing
`lag 1`…`lag 24` / `vlan 1` pseudo-interfaces appended to the port tables.
