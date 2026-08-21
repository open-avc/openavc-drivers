"""Real-platform connect/disconnect lifecycle smoke for every Python driver that
ships a simulator.

Unlike the per-driver unit tests (which stand in a *fake* BaseDriver so the
community CI can run without an ``openavc`` install), this drives the REAL
platform end to end: a real ``StateStore`` / ``EventBus``, the driver's own
``BaseDriver`` subclass, and the driver's own simulator, connected over a real
loopback socket. It is the one thing the fake-based suite structurally cannot
check — that a driver's ``connect()`` actually runs the platform's connection
hooks and emits the canonical ``device.connected.<id>`` /
``device.disconnected.<id>`` lifecycle.

For each driver it asserts the whole arc:
    connect  -> ``device.connected.<id>`` event + ``connected`` state True
    one command (the first parameter-free one, if any) runs without raising
    disconnect -> ``device.disconnected.<id>`` event + ``connected`` state False

Because it runs the real platform (``openavc.*``, simulator included), the whole
module skips when ``openavc`` is not importable: in this repo's isolated CI
(stdlib + pyyaml + pydantic) it skips cleanly; it runs in the workspace where
``openavc`` is installed alongside.

Adding a driver is automatic: ship a ``<driver>_sim.py`` next to ``<driver>.py``
and it is picked up. Drivers whose simulator needs specific config get a
``_CONFIG`` entry; drivers not yet green get a ``_KNOWN_GAPS`` entry whose reason
prints in the skip output (never a silent drop).
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent




# --- Per-driver config the simulator expects (nothing here = generic path) ---
#
# host/port are always injected. These add what a driver needs to complete its
# handshake against its own simulator: credentials the sim checks, the identifier
# it answers under, or a switch to the plain-text scheme a sim serves. A driver
# whose device is HTTPS-only keeps its own scheme — its simulator terminates TLS
# with a self-signed cert instead (``"tls": True`` in SIMULATOR_INFO), so the
# driver only needs verification turned off, exactly as against real hardware.
_CONFIG: dict[str, dict] = {
    "philips_hue": {"app_key": "smoke", "ssl": False},
    "lg_webos": {"ssl": False},
    "tvone_coriomatrix": {"password": "adminpw"},
    "tvone_coriomaster": {"password": "adminpw"},
    "crestron_nvx": {"password": "smokepw"},
    "dante_ddm": {
        "api_key": "smoke",
        "verify_ssl": False,
        "domain_name": "OpenAVC Domain",  # the domain the simulator publishes
    },
    # These two default to a transport their own simulator doesn't speak — the
    # NETGEAR switch to ssh, the TOA mixer to serial — while both simulators are
    # TCP servers. The platform's `transport` config override exists for exactly
    # this (see BaseDriver.connect), so the smoke uses it. That means it covers
    # each driver's lifecycle and protocol over the wire, NOT its ssh/serial
    # transport, which is what this harness is for.
    "netgear_m4250_m4350": {"transport": "tcp"},
    "toa_9000m2": {"transport": "tcp"},
}


# --- Drivers whose smoke is not green yet — skipped LOUDLY with the reason. ---
#
# Empty, and worth keeping that way: every Python driver that ships a simulator
# is covered. Anything added here must carry a reason that prints in the skip
# output, so a gap is tracked rather than silently dropped.
_KNOWN_GAPS: dict[str, str] = {}


# Budget for a single connect(). Matched to what the platform itself allows a
# driver (DeviceManager waits 30 s), because a device whose protocol mandates
# pacing between commands legitimately spends seconds in its handshake and
# initial sync — the Atlona MS-series requires 500 ms per command, so its
# identity read alone is ~9 s. A tighter budget here reports healthy drivers as
# broken rather than measuring anything the product cares about.
_CONNECT_TIMEOUT = 30


def _discover() -> list[tuple[str, str, int]]:
    """Every ``<driver>_sim.py`` that has a sibling ``<driver>.py`` Python
    driver, as ``(driver_id, relative_driver_path, port)``. Port is assigned
    from a fixed pool because the HTTP/UDP/OSC sims bind the port they are given
    without reading an ephemeral one back."""
    out: list[tuple[str, str, int]] = []
    port = 19000
    for sim_path in sorted(REPO_ROOT.rglob("*_sim.py")):
        if "tests" in sim_path.parts or "_vendor" in sim_path.parts:
            continue
        driver_id = sim_path.name[: -len("_sim.py")]
        driver_path = sim_path.with_name(driver_id + ".py")
        if not driver_path.exists():
            continue  # YAML-paired auto-sim: no Python driver to drive
        out.append((driver_id, str(driver_path.relative_to(REPO_ROOT)), port))
        port += 1
    return out


_CASES = _discover()


# --- Platform gate: skip the whole module unless openavc is importable ---

try:
    # One import covers both halves now: the driver sims import
    # ``openavc.simulator.tcp_simulator``, which ships inside the same package
    # the install exposes. This used to need the checkout root added to
    # ``sys.path`` by hand, because ``simulator`` was a second top-level package
    # that no install carried.
    import openavc  # noqa: F401  (real platform; not the conftest stub)

    from openavc.core.event_bus import EventBus
    from openavc.core.state_store import StateStore
except ModuleNotFoundError:
    # Name the number. A sweep covering zero drivers reads exactly like one
    # covering forty otherwise -- which is how a genuinely broken driver sat
    # green in this repo's isolated CI (stdlib + pyyaml + pydantic, no
    # platform) for as long as anyone cared to look.
    pytest.skip(
        f"connect-lifecycle smoke SKIPPED ALL {len(_CASES)} driver(s): "
        "requires the openavc platform (run from the workspace with openavc "
        "installed). NO connect/disconnect coverage ran.",
        allow_module_level=True,
    )


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _class_with(module, marker: str):
    """The class defined in ``module`` carrying ``marker`` (``DRIVER_INFO`` for a
    driver, ``SIMULATOR_INFO`` for a simulator).

    Detection is by attribute, not ``issubclass``: conftest rolls the platform
    modules out of ``sys.modules`` after this test module is collected, so the
    ``BaseDriver`` / ``BaseSimulator`` captured at import time is a stale class
    object by the time the driver module is re-imported here — ``issubclass``
    against it would spuriously return False. The runtime uses the instances we
    build, so class identity never matters; only picking the right class does.
    The ``__module__`` guard skips the imported base classes."""
    found = [
        v
        for v in vars(module).values()
        if isinstance(v, type)
        and getattr(v, marker, None)
        and v.__module__ == module.__name__
    ]
    return found[0] if found else None


def _required_config(driver_cls) -> dict:
    """Fill config fields that are required and have no default, so the driver
    passes its own config validation. Never touches fields with defaults (that
    would clobber the credentials/scheme the paired sim expects)."""
    cfg: dict = {}
    schema = driver_cls.DRIVER_INFO.get("config", {}) or {}
    for key, spec in schema.items():
        if not isinstance(spec, dict) or key in ("host", "port"):
            continue
        if not spec.get("required") or spec.get("default") is not None:
            continue
        t = spec.get("type")
        cfg[key] = 1 if t in ("int", "number") else (False if t == "bool" else "x")
    return cfg


async def _run_smoke(driver_id: str, driver_rel: str, port: int) -> None:
    driver_mod = _load(f"_lc_{driver_id}", REPO_ROOT / driver_rel)
    sim_mod = _load(f"_lc_{driver_id}_sim", (REPO_ROOT / driver_rel).with_name(f"{driver_id}_sim.py"))
    driver_cls = _class_with(driver_mod, "DRIVER_INFO")
    sim_cls = _class_with(sim_mod, "SIMULATOR_INFO")
    assert driver_cls is not None, f"{driver_rel}: no BaseDriver subclass"
    assert sim_cls is not None, f"{driver_id}_sim.py: no BaseSimulator subclass"

    sim = sim_cls(device_id="smoke")
    await sim.start(port)
    bound = getattr(sim, "port", None) or port

    state = StateStore()
    events = EventBus()
    state.set_event_bus(events)
    seen: list[str] = []
    events.on("device.connected.*", lambda name, payload=None: seen.append(name))
    events.on("device.disconnected.*", lambda name, payload=None: seen.append(name))

    config = _required_config(driver_cls)
    config.update({"host": "127.0.0.1", "port": bound, "poll_interval": 0, "verify_timeout": 2.0})
    config.update(_CONFIG.get(driver_id, {}))
    driver = driver_cls(device_id="smoke", config=config, state=state, events=events)

    try:
        await asyncio.wait_for(driver.connect(), timeout=_CONNECT_TIMEOUT)
        await asyncio.sleep(0.25)  # let any on-connect sync / first push settle

        assert "device.connected.smoke" in seen, f"{driver_id}: no device.connected event"
        assert state.get("device.smoke.connected") is True, f"{driver_id}: connected state not True"

        commands = driver_cls.DRIVER_INFO.get("commands", {}) or {}
        param_free = [
            cid
            for cid, cmd in commands.items()
            if not any((p or {}).get("required") for p in (cmd.get("params") or {}).values())
        ]
        if param_free:
            # One command through the real send path — must not raise.
            await asyncio.wait_for(driver.send_command(param_free[0]), timeout=5)

        await asyncio.wait_for(driver.disconnect(), timeout=5)
        await asyncio.sleep(0.1)
        assert "device.disconnected.smoke" in seen, f"{driver_id}: no device.disconnected event"
        assert state.get("device.smoke.connected") is False, f"{driver_id}: connected state not False"
    finally:
        try:
            await driver.disconnect()
        except Exception:
            pass
        try:
            await sim.stop()
        except Exception:
            pass


@pytest.mark.parametrize(
    "driver_id,driver_rel,port",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_connect_lifecycle(driver_id, driver_rel, port):
    """Drive the real connect -> command -> disconnect arc against the driver's
    own simulator and assert the platform lifecycle events + connected state."""
    if driver_id in _KNOWN_GAPS:
        pytest.skip(_KNOWN_GAPS[driver_id])
    asyncio.run(_run_smoke(driver_id, driver_rel, port))
