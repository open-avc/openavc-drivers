"""Every driver runs on every OpenAVC install type.

OpenAVC installs on Windows, macOS, Linux, the Raspberry Pi image, Docker and
appliance hardware, from a source checkout or a frozen build, on Python 3.11
and newer. A driver gets only what all of those share: the Python standard
library, the platform's own packages, and the platform's API. Two things break
that without any other check noticing, because the developer's machine has
them:

* importing a package the platform does not install, a standard-library
  module the frozen builds leave out, one Python 3.13 removed, or one that
  exists on only one operating system;
* running an outside program. Install types differ in which programs they
  have (a slim container has no ``ssh``, a panel no ``ping``). When a driver
  needs one it goes through a platform transport, whose program every install
  type is made to ship.

Applies to drivers, their simulators and their discovery companions: the
simulator and discovery run on every install type too. An import inside a
``try`` whose handler catches ImportError is optional by construction and is
allowed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CATEGORIES = (
    "projectors", "displays", "switchers", "audio", "cameras",
    "video", "lighting", "power", "streaming", "utility",
)
FILES = sorted(p for c in CATEGORIES for p in (ROOT / c).glob("*.py"))

# Import names of the packages the platform installs on every install type
# (openavc's requirements.txt). A driver needing anything else needs the
# platform to ship it first.
PLATFORM_PACKAGES = frozenset({
    "openavc", "httpx", "httpcore", "websockets", "aiohttp", "defusedxml",
    "cryptography", "yaml", "serial", "serial_asyncio", "gmqtt", "croniter",
    "ifaddr", "psutil", "pydantic", "packaging", "certifi", "tzdata",
})

# Standard-library modules a driver cannot rely on:
NOT_IN_FROZEN_BUILDS = frozenset({  # excluded by openavc's installer/openavc.spec
    "tkinter", "unittest", "test", "distutils", "setuptools", "pip",
})
REMOVED_IN_3_13 = frozenset({
    "aifc", "audioop", "cgi", "cgitb", "chunk", "crypt", "imghdr", "mailcap",
    "msilib", "nis", "nntplib", "ossaudiodev", "pipes", "sndhdr", "spwd",
    "sunau", "telnetlib", "uu", "xdrlib",
})
ONE_OS_ONLY = frozenset({
    "fcntl", "termios", "tty", "pty", "pwd", "grp", "resource", "posix",
    "syslog", "msvcrt", "winreg", "winsound", "_winapi", "nt",
})
FORBIDDEN_STDLIB = NOT_IN_FROZEN_BUILDS | REMOVED_IN_3_13 | ONE_OS_ONLY

# Calls that start an outside program.
PROGRAM_CALLS = frozenset({
    ("os", "system"), ("os", "popen"), ("os", "startfile"),
    ("os", "execv"), ("os", "execve"), ("os", "execvp"), ("os", "execvpe"),
    ("os", "execl"), ("os", "execle"), ("os", "execlp"), ("os", "execlpe"),
    ("os", "spawnv"), ("os", "spawnve"), ("os", "spawnl"), ("os", "spawnle"),
    ("os", "posix_spawn"), ("os", "posix_spawnp"),
    ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
    ("shutil", "which"),
})


def _optional_import_nodes(tree: ast.AST) -> set[int]:
    """ids of import nodes inside a try that handles ImportError."""
    optional: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        names = set()
        for handler in node.handlers:
            t = handler.type
            for n in (t.elts if isinstance(t, ast.Tuple) else [t]):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        if names & {"ImportError", "ModuleNotFoundError", "Exception"}:
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        optional.add(id(sub))
    return optional


def _imports(tree: ast.AST):
    optional = _optional_import_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name, id(node) in optional
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node, node.module, id(node) in optional


def _problems(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node, module, optional in _imports(tree):
        top = module.split(".")[0]
        if top == "__future__" or optional:
            continue
        if top in FORBIDDEN_STDLIB:
            found.append(f"line {node.lineno}: imports '{module}', which not every install type has")
        elif top == "subprocess":
            found.append(f"line {node.lineno}: imports subprocess; use a platform transport instead")
        elif top not in sys.stdlib_module_names and top not in PLATFORM_PACKAGES:
            found.append(
                f"line {node.lineno}: imports '{module}', a package OpenAVC does not install"
            )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if isinstance(owner, ast.Name) and (owner.id, node.func.attr) in PROGRAM_CALLS:
                found.append(
                    f"line {node.lineno}: {owner.id}.{node.func.attr}() runs an outside program; "
                    "use a platform transport instead"
                )
    return found


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_driver_needs_nothing_an_install_type_lacks(path):
    problems = _problems(path)
    assert not problems, f"{path.relative_to(ROOT)}:\n  " + "\n  ".join(problems)


def test_the_check_sees_the_driver_library():
    assert len(FILES) > 100


def test_the_check_catches_each_kind(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import subprocess\n"
        "import telnetlib\n"
        "import fcntl\n"
        "import paramiko\n"
        "import shutil, asyncio, os\n"
        "shutil.which('ssh')\n"
        "os.system('ping x')\n"
        "async def f():\n"
        "    await asyncio.create_subprocess_exec('ssh')\n"
        "try:\n"
        "    from openavc.drivers.command_files import CommandFile\n"
        "    import tomli_w\n"
        "except ImportError:\n"
        "    CommandFile = None\n",
        encoding="utf-8",
    )
    problems = "\n".join(_problems(bad))
    for expected in ("subprocess", "telnetlib", "fcntl", "paramiko",
                     "shutil.which", "os.system", "create_subprocess_exec"):
        assert expected in problems
    assert "tomli_w" not in problems  # optional import, allowed
