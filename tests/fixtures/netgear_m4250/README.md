# NETGEAR M4250 live CLI captures

Verbatim, byte-exact CLI output from a real **NETGEAR M4250-40G8XF-PoE+**
(software 13.0.5.14, bootcode 1.0.0.13), captured 2026-06-07 over the switch's
telnet CLI. The driver's parsers were checked against these, so they follow
what the hardware actually emits — not just the CLI-manual layout the synthetic
`netgear_m4250_m4350_outputs.py` fixtures cover.

Each CLI `.txt` is the raw capture: the echoed command on the first line, the
output body, then the trailing CLI prompt. Strip the echo and the prompt to get
exactly what the driver's framing hands a parser.

- `factory/` — read-only baseline at factory defaults (no PoE device attached).
- `with-poe/` — re-captured with a live PoE endpoint (a Dante device, ~6–7 W,
  class 4) on port `0/1`, so `show poe port info` shows real draw and
  `show igmpsnooping group` shows a real multicast subscription table.
- `commands/` — mutating-command transcripts in `===== CMD: <command> =====`
  form: cable diagnostics and the port-description quoting behaviour.
- `discovery/` — what the switch shows a scanner: mDNS and SSDP answers, the
  TLS certificates, the UPnP `rootDesc.xml`, and `legacy-ui-49151-title.txt`,
  the page title the legacy management UI on TCP 49151 returned to a `GET /`
  on the same day (the title element only; the status line and headers were
  not kept). The driver's 49151 `tcp_probe` matches it.

Why this family needed its own captures: the hardware differs from the manual in
ways the synthetic fixtures couldn't show — memory reported in KBytes rather
than bytes, `Bootcode Version` rather than `Boot Code Version`,
`IGMP Snooping Querier Mode` rather than `Admin Mode`, and the trailing
`lag 1`…`lag 24` / `vlan 1` pseudo-interfaces appended to the port tables.
