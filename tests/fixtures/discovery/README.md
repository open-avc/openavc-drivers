# Discovery probe response fixtures

Captured response bytes for a driver's declared `tcp_probe` / `udp_probe`
block, used to confirm the declared matcher and extract rules hold against a
real device's wire format.

One fixture per driver, named by driver id:

- `<driver_id>.bin` — raw captured bytes (binary protocols).
- `<driver_id>.txt` — ASCII responses (line endings preserved — see
  `.gitattributes`, which keeps these byte-exact).

`tests/test_discovery_probe_fixtures.py` reads each driver's probe block from
`index.json` and, **for every driver that has a fixture here**, replays it
through a small stdlib matcher (mirroring openavc's
`server/discovery/probe_runner`) to confirm the declaration matches the capture
and each extract rule pulls a value.

A driver may declare a probe without shipping a fixture (e.g. no hardware to
capture from) — it simply isn't replayed. Capturing one is how you add
coverage; no test-code change is needed, the runner picks it up automatically.

Probe *engine* behavior (parsing, matcher/extract semantics) is tested
separately and generically in the openavc platform repo, with synthetic
devices — not here.
