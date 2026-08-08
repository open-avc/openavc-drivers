"""Test isolation for stubbed modules in ``sys.modules``.

Most driver / simulator / discovery tests in this repo load their driver by
importing it with the real ``openavc`` packages (and the odd
third-party lib such as ``websockets``) replaced by lightweight stubs in
``sys.modules`` — the community CI has no ``openavc`` install, so the drivers'
``from openavc.* import ...`` lines need something to resolve against. Those
stubs are installed at module-import time and never removed, so they leak into
the shared ``sys.modules`` table and shadow the real module for any later test
module that needs it (e.g. ``test_vmix_driver.py`` / ``test_connect_lifecycle.py``,
which run the real driver against a simulator when ``openavc`` *is* installed —
a leaked partial ``openavc.transport`` stub with no ``__path__`` hides the real
``openavc.transport.frame_parsers`` it imports; a leaked ``websockets`` stub with
``connect = None`` breaks a real WebSocket driver's ``connect()``).

This brackets each test module's import with a snapshot/restore of those
``sys.modules`` entries: whatever a module installs while importing is rolled
back once it's collected. Each module captures the driver class it needs at its
own import time, so removing the stubs afterward doesn't affect it — it only
stops the leak from reaching the next module.
"""

import os
import sys

import pytest

# Strict driver state, on for the whole suite.
#
# The platform reports a write to a state variable the driver never declared in
# DRIVER_INFO["state_variables"]: a warning at runtime, a raise under this
# variable. openavc's own conftest sets it so the platform suite runs strict.
# It has to be set HERE too, because this is where driver authors actually run
# tests -- a driver's own suite is the loop where an undeclared write is
# actionable, and leaving it unset makes strict mode a no-op in the one repo
# that matters most for it. ``setdefault``, so a deliberate
# OPENAVC_STRICT_DRIVER_STATE=0 on the command line still wins.
#
# It bites for any test whose fake inherits ``_platform_stubs.StubBaseDriver``,
# which carries the platform's check. A fake that still writes state its own
# way is unaffected until it moves onto the shared stub.
os.environ.setdefault("OPENAVC_STRICT_DRIVER_STATE", "1")

# Package roots the tests stub: the platform packages, plus the third-party
# libraries a driver imports at module load and its fake-based test replaces
# (``websockets`` for the WebSocket drivers). Bracketing these keeps a stub from
# outliving the module that installed it.
_BRACKETED_ROOTS = ("openavc", "websockets")


def _is_platform_module(name: str) -> bool:
    return name.split(".", 1)[0] in _BRACKETED_ROOTS


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # Only bracket module imports — that's where the stubs get installed. The
    # module's import runs inside collector.collect(), i.e. during the yield.
    if not isinstance(collector, pytest.Module):
        yield
        return

    before = {n: m for n, m in sys.modules.items() if _is_platform_module(n)}
    before_keys = set(sys.modules)

    yield  # <-- the test module is imported here

    # Drop platform modules this import added (the leaked stubs)...
    for name in list(sys.modules):
        if _is_platform_module(name) and name not in before_keys:
            del sys.modules[name]
    # ...and restore any real/original entries the import overwrote in place.
    for name, module in before.items():
        sys.modules[name] = module
