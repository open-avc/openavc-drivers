"""Every sentence the catalog build can show a contributor reads plainly.

The scan reads what the build loads, not a list of files: it imports
``scripts/build_index.py`` in a fresh interpreter and reads every module of
this repository that comes in with it (the build's own helpers and the
vendored platform checks). A docstring is exempt; any other string literal can
end up in front of a contributor (an error, a warning, a ``--help`` line).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _modules_the_build_loads() -> list[Path]:
    program = (
        "import importlib.util, json, sys\n"
        "path = sys.argv[1]\n"
        "spec = importlib.util.spec_from_file_location('build_index', path)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['build_index'] = module\n"
        "spec.loader.exec_module(module)\n"
        "print(json.dumps(sorted(\n"
        "    m.__file__ for m in list(sys.modules.values())\n"
        "    if getattr(m, '__file__', None)\n"
        ")))\n"
    )
    ran = subprocess.run(
        [sys.executable, "-c", program, str(REPO_ROOT / "scripts" / "build_index.py")],
        capture_output=True, text=True, cwd=REPO_ROOT, check=True,
    )
    loaded = (Path(name).resolve() for name in json.loads(ran.stdout))
    return sorted(path for path in loaded if path.is_relative_to(REPO_ROOT))


def _docstrings(tree: ast.AST) -> set[int]:
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def test_every_message_the_catalog_build_can_show_reads_plainly():
    sources = _modules_the_build_loads()
    names = {path.relative_to(REPO_ROOT).as_posix() for path in sources}
    assert {
        "scripts/build_index.py",
        "scripts/_vendor/avcdriver_semantic.py",
        "scripts/_vendor/python_info.py",
    } <= names, "the scan must read the build and the checks it runs"
    dashed = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstrings(tree)
        dashed += [
            f"{path.relative_to(REPO_ROOT).as_posix()}:{node.lineno}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "—" in node.value
            and id(node) not in docstrings
        ]
    assert dashed == []
