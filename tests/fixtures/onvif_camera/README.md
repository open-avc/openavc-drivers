## WS-Discovery answer from an AXIS P3265-V, 2026-09-08

AXIS P3265-V on AXIS OS 10.12.165, factory fresh, at 192.168.1.122. This one
is not byte-exact.

- `ws_discovery_probe.response.txt`: the camera's ProbeMatch to the discovery
  companion's WS-Discovery Probe (UDP 3702). **Trimmed** when it was first
  written down, to the elements the companion reads: the SOAP header is left
  empty (a ProbeMatches message carries Action, MessageID, RelatesTo, To and
  AppSequence there), the `tds` prefix used in `Types` has no namespace
  declaration, and the file is one line with no line ending (the camera's own
  layout was not kept). The elements that remain are as the camera sent them:
  the endpoint reference, Types, the scopes (the name percent-encoded, no
  manufacturer or serial-number scope), XAddrs and MetadataVersion. The full
  answer was not kept anywhere.
