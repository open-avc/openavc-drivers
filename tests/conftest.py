"""Test isolation for stubbed modules in ``sys.modules``.

Most driver / simulator / discovery tests in this repo load their driver by
importing it with the real ``server`` / ``simulator`` packages (and the odd
third-party lib such as ``websockets``) replaced by lightweight stubs in
``sys.modules`` — the community CI has no ``openavc`` install, so the drivers'
``from server.* import ...`` lines need something to resolve against. Those
stubs are installed at module-import time and never removed, so they leak into
the shared ``sys.modules`` table and shadow the real module for any later test
module that needs it (e.g. ``test_vmix_driver.py`` / ``test_connect_lifecycle.py``,
which run the real driver against a simulator when ``openavc`` *is* installed —
a leaked partial ``server.transport`` stub with no ``__path__`` hides the real
``server.transport.frame_parsers`` it imports; a leaked ``websockets`` stub with
``connect = None`` breaks a real WebSocket driver's ``connect()``).

This brackets each test module's import with a snapshot/restore of those
``sys.modules`` entries: whatever a module installs while importing is rolled
back once it's collected. Each module captures the driver class it needs at its
own import time, so removing the stubs afterward doesn't affect it — it only
stops the leak from reaching the next module.
"""

import sys

import pytest

# Package roots the tests stub: the platform packages, plus the third-party
# libraries a driver imports at module load and its fake-based test replaces
# (``websockets`` for the WebSocket drivers). Bracketing these keeps a stub from
# outliving the module that installed it.
_BRACKETED_ROOTS = ("server", "simulator", "websockets")


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
