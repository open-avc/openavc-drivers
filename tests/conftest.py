"""Suite-wide settings.

Strict driver state, on for the whole suite.

The platform reports a write to a state variable the driver never declared in
DRIVER_INFO["state_variables"]: a warning at runtime, a raise under this
variable. openavc's own conftest sets it so the platform suite runs strict.
It is set HERE too because the checks that load a driver on the real platform
(the connect/disconnect run over every simulator, the protocol-math tests)
are where an undeclared write in a driver gets caught before it ships.
``setdefault``, so a deliberate OPENAVC_STRICT_DRIVER_STATE=0 on the command
line still wins.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("OPENAVC_STRICT_DRIVER_STATE", "1")

# The catalog's own modules, imported by name across the suite: which files are
# the catalog's drivers is catalog_files' rule.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
