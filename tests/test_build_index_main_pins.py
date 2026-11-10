"""Pins for the catalog build as a whole run, and for its catalog-wide steps.

Each ``main`` case stages a small driver tree in a temporary directory and runs
the build against it, recording the result: the exit code (or the exception
that escaped), everything written to stdout and stderr as one sequence in the
order it was written, and the catalog files the run left behind. The direct
cases run the steps that read the whole catalog (the cross-driver checks, the
devices catalog), reading a Python driver's index fields, and the list of
files an install fetches.

The expected results live in ``fixtures/build_index_main_pins.json``. They
record what the build does today, right or wrong; a result that should change
is changed on purpose, and the fixture regenerated with

    python tests/test_build_index_main_pins.py --regen
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_index  # noqa: E402

FIXTURE = HERE / "fixtures" / "build_index_main_pins.json"
SCHEMA = REPO / "avcdriver.schema.json"
MANUFACTURERS = ["Acme", "AcmeAudio", "Other"]

YAML_BASE: dict[str, Any] = {
    "id": "acme_widget",
    "name": "Acme Widget",
    "manufacturer": "Acme",
    "category": "audio",
    "version": "1.0.0",
    "author": "Tester",
    "transport": "tcp",
    "description": "Fixture driver.",
    "source_url": "https://example.com/protocol",
}
PY_BASE: dict[str, Any] = {**YAML_BASE, "id": "acme_py", "name": "Acme Py", "category": "projector"}


# --- Staging a tree ---------------------------------------------------------


def _yaml(root: Path, rel: str, *, crlf: bool = False, **fields: Any) -> Path:
    data = {**YAML_BASE, **fields}
    data = {k: v for k, v in data.items() if v is not ...}
    text = yaml.safe_dump(data, sort_keys=False)
    return _text(root, rel, text, crlf=crlf)


def _py(root: Path, rel: str, *, extra: str = "", **fields: Any) -> Path:
    data = {**PY_BASE, **fields}
    data = {k: v for k, v in data.items() if v is not ...}
    inner = ", ".join(f"{k!r}: {v!r}" for k, v in data.items())
    if extra:
        inner = f"{inner}, {extra}"
    return _text(root, rel, f"class AcmeDriver:\n    DRIVER_INFO = {{{inner}}}\n")


def _text(root: Path, rel: str, text: str, *, crlf: bool = False) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\n", "\r\n") if crlf else text
    path.write_bytes(data.encode("utf-8"))
    return path


def _manufacturers(root: Path, value: Any = None, *, raw: str | None = None) -> None:
    text = raw if raw is not None else json.dumps(MANUFACTURERS if value is None else value)
    (root / "manufacturers.json").write_text(text, encoding="utf-8")


def _extra(root: Path, value: Any = None, *, raw: str | None = None) -> None:
    text = raw if raw is not None else json.dumps(value)
    (root / "devices-extra.json").write_text(text, encoding="utf-8")


def _built(stage: Callable[[Path], None]) -> Callable[[Path], None]:
    """Stage a tree and build its catalog once, as a committed catalog would be."""
    def run(root: Path) -> None:
        stage(root)
        with _recording([]):
            build_index.main(["--root", str(root), "--json-schema-file", str(SCHEMA)])
    return run


def _then(first: Callable[[Path], None], second: Callable[[Path], None]) -> Callable[[Path], None]:
    def run(root: Path) -> None:
        first(root)
        second(root)
    return run


# --- Trees ------------------------------------------------------------------


def valid_tree(root: Path) -> None:
    """Two YAML drivers, a Python driver with both companions, a deprecated
    driver, a declared discovery companion, devices-extra, and files the walk
    must leave alone."""
    _manufacturers(root)
    _yaml(
        root, "audio/acme_amp.avcdriver", id="acme_amp", name="Acme Amp",
        ports=[23, 4998], protocols=["telnet"], tags=["ceiling-mic", "dsp"],
        simulated=True, verified=False,
        help={"overview": "An amp.", "setup": "Plug it in."},
        compatible_models=[
            {"manufacturer": "Acme", "models": ["AMP-1", "amp-2"], "confidence": "full"},
            {"manufacturer": "AcmeAudio", "models": ["Zed"], "confidence": "untested", "notes": "Rebadge."},
        ],
        discovery={"hostname": ["^acme-amp-"]},
    )
    _yaml(
        root, "switchers/acme_sw.avcdriver", id="acme_sw", name="acme switch",
        category="switcher",
        compatible_models=[{"manufacturer": "Acme", "models": ["SW-8"], "confidence": "partial"}],
        discovery={"python": "./acme_sw_discovery.py", "port_open": [5000]},
    )
    _text(root, "switchers/acme_sw_discovery.py", "def probe(ctx):\n    return None\n")
    _text(root, "switchers/acme_sw_sim.py", "# a YAML driver's sim is never installed\n")
    _yaml(
        root, "displays/acme_old.avcdriver", id="acme_old", name="Acme Old",
        category="display", deprecated=True, replacement_id="acme_amp",
    )
    _py(
        root, "projectors/acme_py.py", verified=True, ports=[4352],
        compatible_models=[
            {"manufacturer": "Acme", "models": ["PJ-1"], "confidence": "partial"},
            {"manufacturer": "Acme", "models": ["AMP-1"], "confidence": "partial"},
        ],
    )
    _text(root, "projectors/acme_py_sim.py", "# simulator\n")
    _text(root, "projectors/acme_py_discovery.py", "# discovery companion\n")
    _text(root, "projectors/README.md", "not a driver\n")
    _text(root, "projectors/notes.json", "{}\n")
    _yaml(root, "devices/stray.avcdriver", id="stray")  # not a category dir
    _yaml(root, "misc/stray2.avcdriver", id="stray2")
    _extra(root, {"devices": [
        {"manufacturer": "Other", "model": "Zeta-9", "category": "switcher", "drivers": [], "status": "no_driver"},
        {"manufacturer": "acme", "model": "aaa", "category": "audio", "drivers": []},
    ]})


def one_yaml(root: Path) -> None:
    _manufacturers(root)
    _yaml(root, "audio/acme_widget.avcdriver")


def one_py(root: Path) -> None:
    _manufacturers(root)
    _py(root, "projectors/acme_py.py")


def edit_driver(root: Path) -> None:
    _yaml(root, "audio/acme_amp.avcdriver", id="acme_amp", name="Acme Amp Renamed")


def schema_failure_only(root: Path) -> None:
    """A driver the JSON Schema refuses but the build's own checks accept."""
    _manufacturers(root)
    _yaml(
        root, "audio/acme_widget.avcdriver",
        state_variables={"p": {"type": "string", "label": 5}}, verified="yes",
    )


def schema_and_build_failure(root: Path) -> None:
    schema_failure_only(root)
    _yaml(root, "audio/acme_bad.avcdriver", id="Acme-Bad", category="nope")


def per_driver_order(root: Path) -> None:
    """One driver failing its entry, the platform's rules and its discovery block."""
    _manufacturers(root)
    _yaml(
        root, "audio/acme_bad.avcdriver", id="Bad Id", category="nope",
        version="one", source_url="ftp://x", commands={"go": {"sned": "GO"}},
        discovery={"mdns": [""], "nonsense": 1},
    )
    _yaml(root, "audio/acme_ok.avcdriver", id="acme_ok", commands={"go": {"label": "Go", "sned": "GO"}})


def crlf_in_checkout(root: Path) -> None:
    _manufacturers(root)
    (root / ".git").mkdir()
    _yaml(root, "audio/acme_widget.avcdriver", crlf=True, commands={"go": {"sned": "GO"}})
    _yaml(root, "audio/acme_fine.avcdriver", id="acme_fine")


def crlf_companion_in_checkout(root: Path) -> None:
    _manufacturers(root)
    (root / ".git").mkdir()
    _py(root, "projectors/acme_py.py")
    _text(root, "projectors/acme_py_sim.py", "# sim\n", crlf=True)


def crlf_without_checkout(root: Path) -> None:
    _manufacturers(root)
    _yaml(root, "audio/acme_widget.avcdriver", crlf=True)


def companion_outside_root(root: Path) -> None:
    _manufacturers(root)
    outside = root.parent / f"{root.name}_outside_discovery.py"
    outside.write_text("# outside\n", encoding="utf-8")
    _yaml(
        root, "audio/acme_widget.avcdriver",
        discovery={"python": f"../../{outside.name}", "port_open": [5000]},
    )


def companion_shapes(root: Path) -> None:
    _manufacturers(root)
    _text(root, "audio/comp_discovery.py", "# companion\n")
    (root / "audio" / "a_dir_discovery.py").mkdir(parents=True)
    shapes: list[Any] = [
        {"file": "comp_discovery.py"},
        {"file": "comp_discovery.py", "cross_vendor": True, "extra": 1},
        {"file": ""}, {"file": 5}, {}, "", "missing_discovery.py",
        "a_dir_discovery.py", ["comp_discovery.py"], None, 7,
    ]
    for n, shape in enumerate(shapes):
        _yaml(
            root, f"audio/acme_c{n}.avcdriver", id=f"acme_c{n}",
            discovery={"python": shape, "port_open": [5000 + n]},
        )
    _yaml(root, "audio/acme_cx.avcdriver", id="acme_cx", discovery=["python"])
    _py(root, "audio/acme_cy.py", id="acme_cy", category="audio", discovery={"python": "comp_discovery.py"})


def requires_not_text(root: Path) -> None:
    _manufacturers(root)
    _yaml(
        root, "audio/acme_widget.avcdriver",
        discovery={"ssdp": [{"st": "urn:x", "manufacturer": "Acme"}], "requires": 5},
    )


def yaml_reports(root: Path) -> None:
    """A YAML driver the platform warns about, one it refuses, one below its floor."""
    _manufacturers(root)
    _yaml(
        root, "audio/acme_serial.avcdriver", id="acme_serial", transport="serial",
        state_variables={"power": {"type": "string", "label": "Power"}},
        responses=[{"match": "PWR=(\\w+)", "set": {"power": "$1"}}],
    )
    _yaml(root, "audio/acme_typo.avcdriver", id="acme_typo", polling_interval=5)
    _yaml(
        root, "audio/acme_floor.avcdriver", id="acme_floor",
        push={"type": "udp_listener", "port": 9000},
    )
    _yaml(
        root, "audio/acme_low.avcdriver", id="acme_low", min_platform_version="0.1.0",
        liveness={"send": "PING"},
    )


def python_reports(root: Path) -> None:
    """Python drivers with a computed value, a computed reference target, an
    unknown key, and one below its floor."""
    _manufacturers(root)
    _text(
        root, "projectors/acme_computed.py",
        "_BUILT = {}\n"
        "class AcmeDriver:\n"
        "    DRIVER_INFO = {" + ", ".join(
            f"{k!r}: {v!r}" for k, v in {**PY_BASE, "id": "acme_computed"}.items()
        ) + ", 'commands': dict(_BUILT), 'config_schema': {'x': {'type': 'string', 'default': str(1)}}}\n",
    )
    _py(
        root, "projectors/acme_refs.py", id="acme_refs",
        extra="'commands': {**_MORE}, 'actions': [{'command': 'go', 'label': 'Go'}]",
    )
    _py(
        root, "projectors/acme_typo.py", id="acme_typo",
        extra="'commands': {'go': {'label': 'Go', 'untis': '%'}}",
    )
    _py(
        root, "projectors/acme_floor.py", id="acme_floor",
        min_platform_version="0.1.0", transport="snmp",
    )
    _py(root, "projectors/acme_push.py", id="acme_push", push={"type": "udp_listener", "port": 9000})


def everything_reports(root: Path) -> None:
    """Notes (stdout), warnings and errors (stderr) in one run."""
    python_reports(root)
    yaml_reports(root)


def cross_driver(root: Path) -> None:
    _manufacturers(root)
    _yaml(root, "audio/a.avcdriver", id="dup", manufacturer="Nobody",
          compatible_models=[{"manufacturer": "Ghost", "models": ["M1"], "confidence": "full"}],
          discovery={"mdns": ["_acme._tcp"]})
    _yaml(root, "audio/b.avcdriver", id="dup", deprecated=True, replacement_id="gone",
          compatible_models=[{"manufacturer": "Acme", "models": ["M1"], "confidence": "full"}],
          discovery={"mdns": ["_other._tcp"]})
    _yaml(root, "audio/c.avcdriver", id="c_drv",
          compatible_models=[{"manufacturer": "Acme", "models": ["M1", "M1"], "confidence": "full"}],
          discovery={"hostname": ["^acme-"], "mdns": ["_acme._tcp"]})
    _yaml(root, "audio/d.avcdriver", id="d_drv",
          discovery={"hostname": ["^acme-"]})
    _yaml(root, "audio/e.avcdriver", id=..., discovery={"mdns": ["_other._tcp"]})
    _yaml(root, "audio/f.avcdriver", id="", discovery={"amx_ddp": [{"make": "Acme"}]})
    _yaml(root, "audio/g.avcdriver", id=None, discovery={"amx_ddp": [{"make": "Acme"}]})


def two_bad(first: Callable[[Path], None], second: Callable[[Path], None]) -> Callable[[Path], None]:
    return _then(first, second)


def bad_manufacturers(root: Path) -> None:
    _manufacturers(root, raw="{not json")


def bad_yaml(root: Path) -> None:
    _text(root, "audio/broken.avcdriver", "id: [unclosed\n")


def bad_extra(root: Path) -> None:
    _extra(root, raw="{not json")


def no_driver_files(root: Path) -> None:
    _manufacturers(root)
    _text(root, "audio/README.md", "nothing here\n")
    _text(root, "audio/x_sim.py", "# sim only\n")


MAIN_CASES: list[tuple[str, Callable[[Path], None], list[str], str]] = [
    # name, stage, extra argv, schema ("repo", "default", "missing", "no_python", "no_package")
    ("build_valid_tree", valid_tree, [], "repo"),
    ("build_valid_tree_no_schema", valid_tree, [], "no_package"),
    ("check_fresh", _built(valid_tree), ["--check"], "repo"),
    ("check_stale", _then(_built(valid_tree), edit_driver), ["--check"], "repo"),
    ("check_never_built", valid_tree, ["--check"], "repo"),
    ("check_json_schema_valid", valid_tree, ["--check-json-schema"], "repo"),
    ("check_json_schema_does_not_ask_freshness", _then(_built(valid_tree), edit_driver), ["--check-json-schema"], "repo"),
    ("both_flags_stale", _then(_built(valid_tree), edit_driver), ["--check", "--check-json-schema"], "repo"),
    ("default_schema_path_absent", one_yaml, [], "default"),
    ("default_schema_path_absent_flag", one_yaml, ["--check-json-schema"], "default"),
    ("schema_file_missing", one_yaml, [], "missing"),
    ("schema_file_missing_flag", one_yaml, ["--check-json-schema"], "missing"),
    ("no_package", one_py, [], "no_package"),
    ("no_package_flag", one_py, ["--check-json-schema"], "no_package"),
    ("no_package_and_no_file", one_py, [], "no_package_missing"),
    ("no_package_and_no_file_flag", one_py, ["--check-json-schema"], "no_package_missing"),
    ("no_python_schema", _then(one_yaml, lambda r: _py(r, "projectors/acme_py.py", polling={"interval": "x"})), [], "no_python"),
    ("no_python_schema_flag", one_py, ["--check-json-schema"], "no_python"),
    ("manufacturers_missing", lambda r: _yaml(r, "audio/acme_widget.avcdriver"), [], "repo"),
    ("manufacturers_not_a_list", lambda r: (_manufacturers(r, {"Acme": 1}), _yaml(r, "audio/acme_widget.avcdriver")), [], "repo"),
    ("manufacturers_bad_json", lambda r: (bad_manufacturers(r), _yaml(r, "audio/acme_widget.avcdriver")), [], "repo"),
    ("extra_bad_json", _then(one_yaml, bad_extra), [], "repo"),
    ("extra_not_a_dict", _then(one_yaml, lambda r: _extra(r, [1, 2])), [], "repo"),
    ("extra_devices_a_string", _then(one_yaml, lambda r: _extra(r, {"devices": "ab"})), [], "repo"),
    ("extra_entry_without_manufacturer", _then(one_yaml, lambda r: _extra(r, {"devices": [{"model": "Q"}]})), [], "repo"),
    ("extra_entries_collide", _then(one_yaml, lambda r: _extra(r, {"devices": [
        {"manufacturer": "Other", "model": "Q", "n": 1}, {"manufacturer": "Other", "model": "Q", "n": 2}]})), [], "repo"),
    ("extra_collides_with_driver", _then(valid_tree, lambda r: _extra(r, {"devices": [
        {"manufacturer": "Acme", "model": "AMP-1"}]})), [], "repo"),
    ("no_driver_files", no_driver_files, [], "repo"),
    ("bad_yaml", _then(lambda r: _manufacturers(r), bad_yaml), [], "repo"),
    ("yaml_not_a_mapping", lambda r: (_manufacturers(r), _text(r, "audio/list.avcdriver", "- 1\n- 2\n")), [], "repo"),
    ("python_syntax_error", lambda r: (_manufacturers(r), _text(r, "projectors/bad.py", "class X(:\n")), [], "repo"),
    ("python_no_driver_info", lambda r: (_manufacturers(r), _text(r, "projectors/bad.py", "DRIVER_INFO = {'id': 'x'}\n")), [], "repo"),
    ("python_non_literal_index_field", lambda r: (_manufacturers(r), _py(r, "projectors/acme_py.py", extra="'tags': sorted(['a'])")), [], "repo"),
    ("python_non_string_key", lambda r: (_manufacturers(r), _py(r, "projectors/acme_py.py", extra="1: 'x'")), [], "repo"),
    # Two failures at once: which one the run reports.
    ("first_manufacturers_then_yaml", lambda r: (bad_manufacturers(r), bad_yaml(r)), [], "repo"),
    ("first_extra_then_yaml", _then(_then(lambda r: _manufacturers(r), bad_extra), bad_yaml), [], "repo"),
    ("first_schema_then_manufacturers", lambda r: (bad_manufacturers(r), bad_yaml(r)), ["--check-json-schema"], "missing"),
    ("first_python_schema_then_manufacturers", lambda r: (bad_manufacturers(r), bad_yaml(r)), ["--check-json-schema"], "no_python"),
    ("first_no_package_warning_then_manufacturers", lambda r: (bad_manufacturers(r), bad_yaml(r)), [], "no_package"),
    ("schema_errors_only", schema_failure_only, [], "repo"),
    ("schema_errors_only_flag", schema_failure_only, ["--check-json-schema"], "repo"),
    ("schema_errors_only_check", schema_failure_only, ["--check"], "repo"),
    ("schema_and_build_errors", schema_and_build_failure, [], "repo"),
    ("schema_and_build_errors_flag", schema_and_build_failure, ["--check-json-schema"], "repo"),
    ("schema_errors_and_extra_collision", _then(schema_failure_only, lambda r: (
        _yaml(r, "audio/acme_cm.avcdriver", id="acme_cm",
              compatible_models=[{"manufacturer": "Acme", "models": ["X1"], "confidence": "full"}]),
        _extra(r, {"devices": [{"manufacturer": "Acme", "model": "X1"}]}))), [], "repo"),
    ("schema_errors_and_python_schema_missing", _then(schema_failure_only, lambda r: _py(r, "projectors/acme_py.py", state_variables={"p": {"type": "string", "label": 5}})), [], "no_python"),
    ("schema_errors_python_only", lambda r: (_manufacturers(r), _py(r, "projectors/acme_py.py", state_variables={"p": {"type": "string", "label": 5}})), [], "repo"),
    ("schema_errors_python_only_flag", lambda r: (_manufacturers(r), _py(r, "projectors/acme_py.py", state_variables={"p": {"type": "string", "label": 5}})), ["--check-json-schema"], "repo"),
    ("schema_errors_and_notes", _then(schema_failure_only, python_reports), [], "repo"),
    # One driver, every per-driver stage.
    ("per_driver_order", per_driver_order, [], "repo"),
    ("per_driver_order_no_schema", per_driver_order, [], "no_package"),
    ("crlf_in_checkout", crlf_in_checkout, [], "repo"),
    ("crlf_companion_in_checkout", crlf_companion_in_checkout, [], "repo"),
    ("crlf_without_checkout", crlf_without_checkout, [], "repo"),
    ("companion_outside_root", companion_outside_root, [], "repo"),
    ("companion_shapes", companion_shapes, [], "no_package"),
    ("requires_not_text", requires_not_text, [], "no_package"),
    ("yaml_reports", yaml_reports, [], "repo"),
    ("python_reports", python_reports, [], "repo"),
    ("everything_reports", everything_reports, [], "repo"),
    ("everything_reports_flag", everything_reports, ["--check-json-schema"], "repo"),
    ("cross_driver", cross_driver, [], "no_package"),
]


# --- Running main -----------------------------------------------------------


class _Recorder(io.TextIOBase):
    """One of the two streams, writing into a log both share."""

    def __init__(self, name: str, log: list[list[str]]) -> None:
        self._name = name
        self._log = log

    def write(self, text: str) -> int:
        if self._log and self._log[-1][0] == self._name:
            self._log[-1][1] += text
        else:
            self._log.append([self._name, text])
        return len(text)


class _recording:
    def __init__(self, log: list[list[str]]) -> None:
        self._log = log

    def __enter__(self) -> None:
        self._saved = (sys.stdout, sys.stderr)
        sys.stdout = _Recorder("out", self._log)
        sys.stderr = _Recorder("err", self._log)

    def __exit__(self, *exc: Any) -> None:
        sys.stdout, sys.stderr = self._saved


def _schema_argv(root: Path, schema: str) -> list[str]:
    if schema in ("repo", "no_package"):
        return ["--json-schema-file", str(SCHEMA)]
    if schema in ("missing", "no_package_missing"):
        return ["--json-schema-file", str(root / "absent.schema.json")]
    if schema == "no_python":
        alone = root / "schemas" / "avcdriver.schema.json"
        alone.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SCHEMA, alone)
        return ["--json-schema-file", str(alone)]
    assert schema == "default"
    return []


def _catalog(root: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in ("index.json", "devices.json"):
        path = root / name
        if path.is_file():
            out[name] = json.loads(path.read_text(encoding="utf-8"))
    for shard_dir in ("index", "devices"):
        for path in sorted((root / shard_dir).glob("*.json")):
            if path.is_file() and path.parent.name == shard_dir:
                rel = path.relative_to(root).as_posix()
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _scrub(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return (
            value.replace(str(root), "<root>")
            .replace(str(root.parent), "<tmp>")
            .replace(str(REPO), "<repo>")
        )
    if isinstance(value, list):
        return [_scrub(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, root) for k, v in value.items()}
    return value


def _run_main(stage: Callable[[Path], None], argv: list[str], schema: str) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="bi_pin_")).resolve()
    try:
        stage(root)
        before = _catalog(root)
        args = ["--root", str(root), *_schema_argv(root, schema), *argv]
        log: list[list[str]] = []
        saved = build_index.jsonschema_validator_for
        if schema.startswith("no_package"):
            build_index.jsonschema_validator_for = None
        raised = None
        rc = None
        try:
            with _recording(log):
                rc = build_index.main(args)
        except Exception as exc:  # noqa: BLE001 - the escaped exception is the result
            raised = f"{type(exc).__name__}: {exc}"
        finally:
            build_index.jsonschema_validator_for = saved
        after = _catalog(root)
        return _scrub({
            "rc": rc,
            "raised": raised,
            "transcript": log,
            "catalog": after if after != before else "unchanged",
        }, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        for stray in root.parent.glob(f"{root.name}_outside*"):
            stray.unlink()


# --- The catalog-wide steps, called directly ---------------------------------


def _entry(**fields: Any) -> build_index.DriverEntry:
    base = {
        "id": "acme_widget", "name": "Acme Widget", "manufacturer": "Acme",
        "category": "audio", "version": "1.0.0", "author": "T", "transport": "tcp",
        "description": "D", "source_url": "https://example.com", "file": "audio/x.avcdriver",
        "format": "avcdriver",
    }
    return build_index.DriverEntry(**{**base, **fields})


def _cm(manufacturer: str, models: list[str], confidence: str = "full", notes: str | None = None) -> dict:
    out: dict[str, Any] = {"manufacturer": manufacturer, "models": models, "confidence": confidence}
    if notes is not None:
        out["notes"] = notes
    return out


ENTRY_SETS: list[tuple[str, list[dict[str, Any]]]] = [
    ("empty", []),
    ("clean", [dict(id="a", file="a"), dict(id="b", file="b", compatible_models=[_cm("Acme", ["M"])])]),
    ("three_same_id", [dict(id="a", file="f1"), dict(id="a", file="f2"), dict(id="a", file="f3"), dict(id="b", file="f4")]),
    ("manufacturers", [
        dict(id="a", file="f1", manufacturer="Nobody", compatible_models=[_cm("Ghost", ["M"]), _cm("Acme", ["N"]), _cm("Other2", ["O"], "partial")]),
        dict(id="b", file="f2", manufacturer="Other2"),
    ]),
    ("replacements", [
        dict(id="a", file="f1", deprecated=True, replacement_id="a"),
        dict(id="b", file="f2", deprecated=True, replacement_id="zzz"),
        dict(id="c", file="f3", deprecated=True, replacement_id="b"),
    ]),
    ("full_three_ways", [
        dict(id="a", file="f1", compatible_models=[_cm("Acme", ["M"])]),
        dict(id="b", file="f2", compatible_models=[_cm("Acme", ["M"])]),
        dict(id="c", file="f3", compatible_models=[_cm("Acme", ["M"])]),
    ]),
    ("full_notes_alternate", [
        dict(id="a", file="f1", compatible_models=[_cm("Acme", ["M"], notes="why")]),
        dict(id="b", file="f2", compatible_models=[_cm("Acme", ["M"])]),
        dict(id="c", file="f3", compatible_models=[_cm("Acme", ["M"], notes="also")]),
        dict(id="d", file="f4", compatible_models=[_cm("Acme", ["M"], notes="and")]),
    ]),
    ("full_notes_empty_string", [
        dict(id="a", file="f1", compatible_models=[_cm("Acme", ["M"], notes="")]),
        dict(id="b", file="f2", compatible_models=[_cm("Acme", ["M"], notes="x")]),
    ]),
    ("full_two_models_each", [
        dict(id="a", file="f1", compatible_models=[_cm("Acme", ["M", "N", "O"])]),
        dict(id="b", file="f2", compatible_models=[_cm("Acme", ["O", "M"]), _cm("Acme", ["N"], "partial")]),
    ]),
    ("full_self_overlap", [
        dict(id="a", file="f1", compatible_models=[_cm("Acme", ["M", "M"]), _cm("Acme", ["M"], notes="n")]),
    ]),
    ("partial_and_untested_ignored", [
        dict(id="a", file="f1", compatible_models=[_cm("Acme", ["M"], "partial")]),
        dict(id="b", file="f2", compatible_models=[_cm("Acme", ["M"], "untested")]),
        dict(id="c", file="f3", compatible_models=[_cm("Acme", ["M"])]),
        dict(id="d", file="f4", compatible_models=[_cm("Acme", ["m"]), _cm("Other", ["M"])]),
    ]),
    ("every_check_interleaved", [
        dict(id="a", file="f1", manufacturer="Nobody", compatible_models=[_cm("Ghost", ["M"])]),
        dict(id="a", file="f2", deprecated=True, replacement_id="q", compatible_models=[_cm("Ghost", ["M"])]),
        dict(id="b", file="f3", manufacturer="Nobody2", deprecated=True, replacement_id="r"),
    ]),
    ("devices_order", [
        dict(id="zed", file="f1", category="video", compatible_models=[_cm("Acme", ["M", "b"], "untested"), _cm("acme", ["a"], "partial", notes="n")]),
        dict(id="alpha", file="f2", category="audio", compatible_models=[_cm("Acme", ["M"], "untested"), _cm("Acme", ["B"], "full", notes="")]),
        dict(id="mid", file="f3", category="power", compatible_models=[_cm("Acme", ["M"], "full")]),
        dict(id="beta", file="f4", category="power", compatible_models=[_cm("Acme", ["M"], "untested", notes="x")]),
    ]),
]

EXTRA_SETS: list[tuple[str, list[Any]]] = [
    ("none", []),
    ("merged_and_sorted", [
        {"manufacturer": "acme", "model": "0", "category": "audio", "drivers": []},
        {"manufacturer": "Zulu", "model": "a"},
    ]),
    ("collides_with_driver", [{"manufacturer": "Acme", "model": "M"}]),
    ("collide_with_each_other", [
        {"manufacturer": "Other", "model": "Q", "n": 1},
        {"manufacturer": "Other", "model": "Q", "n": 2},
    ]),
    ("missing_keys", [{"model": "Q"}, {"manufacturer": "Other"}]),
    ("non_string_manufacturer", [{"manufacturer": 5, "model": "Q"}]),
    ("not_a_mapping", ["x"]),
]


def _run_cross(name: str) -> dict[str, Any]:
    entries = [_entry(**fields) for fields in dict(ENTRY_SETS)[name]]
    return {"errors": build_index.cross_validate(entries, {"Acme", "Other"})}


def _run_devices(entry_set: str, extra_set: str) -> dict[str, Any]:
    entries = [_entry(**fields) for fields in dict(ENTRY_SETS)[entry_set]]
    extras = json.loads(json.dumps(dict(EXTRA_SETS)[extra_set]))
    try:
        result: Any = build_index.build_devices(entries, extras)
    except Exception as exc:  # noqa: BLE001 - the exception is the result
        return {"raised": f"{type(exc).__name__}: {exc}"}
    return {"devices": json.loads(json.dumps(result))}


DEVICE_CASES = [
    (e, x) for e in ("empty", "clean", "devices_order", "full_self_overlap", "manufacturers")
    for x, _ in EXTRA_SETS
]


# --- Reading a Python driver's index fields ----------------------------------

INFO_CASES: list[tuple[str, str]] = [
    ("literals", "{'id': 'x', 'ports': [1, -2, (3, 4)], 'help': {'overview': 'o', 'setup': None}, 'verified': True}"),
    ("negative_numbers", "{'ports': [-1, -2.5, -True, -0]}"),
    ("other_constants", "{'tags': [1j, b'raw', ..., None, 'é']}"),
    ("nested_lists_and_tuples", "{'compatible_models': [{'models': ('a', ['b', ('c',)])}]}"),
    ("runtime_field_left_alone", "{'id': 'x', 'commands': dict(_BUILT), 'state_variables': {1: f'{x}'}}"),
    ("discovery_kept", "{'id': 'x', 'discovery': {'port_open': [1, 2], 'python': {'file': 'a.py'}}}"),
    ("uplus", "{'ports': [+1]}"),
    ("negated_name", "{'ports': [-X]}"),
    ("negated_string", "{'ports': [-'a']}"),
    ("not_constant", "{'verified': not 1}"),
    ("nested_non_string_key", "{'help': {1: 'x'}}"),
    ("nested_spread", "{'help': {**OTHER}}"),
    ("set_literal", "{'tags': {'a', 'b'}}"),
    ("f_string", "{'name': f'{X}'}"),
    ("call", "{'tags': sorted(['a'])}"),
    ("name", "{'id': DRIVER_ID}"),
    ("comprehension", "{'tags': [t for t in 'ab']}"),
    ("binop", "{'ports': [1 + 2]}"),
    ("top_level_non_string_key", "{1: 'x', 'id': 'y'}"),
    ("top_level_spread", "{**BASE, 'id': 'y'}"),
    ("top_level_non_string_key_runtime_value", "{'id': 'y', 2: sorted([])}"),
]


def _run_info(source: str, annotated: bool) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="bi_pin_")).resolve()
    try:
        target = "DRIVER_INFO: dict = " if annotated else "DRIVER_INFO = "
        path = _text(root, "drv.py", f"class AcmeDriver:\n    {target}{source}\n")
        try:
            return {"value": repr(build_index.extract_python_driver_info(path))}
        except build_index.ExtractError as exc:
            return {"raised": str(exc)}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --- One entry's fields ---------------------------------------------------------

FIELD_CASES: list[tuple[str, dict[str, Any]]] = [
    ("ports_edges", {"ports": [1, 65535]}),
    ("ports_zero", {"ports": [0]}),
    ("ports_too_high", {"ports": [65536]}),
    ("ports_negative", {"ports": [-1]}),
    ("ports_bool", {"ports": [True, False]}),
    ("ports_coerced", {"ports": ["80", 81.0]}),
    ("ports_fraction", {"ports": [1.5]}),
    ("ports_none", {"ports": [None]}),
    ("protocols_blank", {"protocols": ["telnet", " "]}),
    ("protocols_non_string", {"protocols": [5]}),
    ("models_empty", {"compatible_models": [{"manufacturer": "Acme", "models": [], "confidence": "full"}]}),
    ("models_blank", {"compatible_models": [{"manufacturer": "Acme", "models": ["A", "  "], "confidence": "full"}]}),
    ("confidence_unknown", {"compatible_models": [{"manufacturer": "Acme", "models": ["A"], "confidence": "most"}]}),
    ("cm_extra_key", {"compatible_models": [{"manufacturer": "Acme", "models": ["A"], "confidence": "full", "x": 1}]}),
    ("min_platform_bad", {"min_platform_version": "one"}),
    ("min_platform_ok", {"min_platform_version": "0.1.0"}),
    ("tags_bad", {"tags": ["ok", "Not-OK"]}),
    ("help_missing_setup", {"help": {"overview": "o"}}),
    ("help_connection_blank", {"help": {"overview": "o", "setup": "s", "connection": ""}}),
    ("deprecated_alone", {"deprecated": True}),
    ("replacement_alone", {"replacement_id": "b"}),
    ("several_at_once", {"id": "Bad", "category": "nope", "transport": "x", "version": "1", "source_url": "x", "ports": [0]}),
    ("unknown_fields_dropped", {"commands": {"x": 1}, "flavour": "sweet"}),
]


def _run_fields(fields: dict[str, Any]) -> dict[str, Any]:
    try:
        entry = _entry(**fields)
    except build_index.ValidationError as exc:
        return {"errors": build_index._format_validation_errors("f", exc)}
    return {"entry": repr(build_index._entry_dict(entry))}


# --- The files an install fetches --------------------------------------------

COMPANION_SHAPES: list[tuple[str, Any]] = [
    ("string", "comp_discovery.py"),
    ("string_dot_slash", "./comp_discovery.py"),
    ("string_missing", "missing_discovery.py"),
    ("string_empty", ""),
    ("string_a_directory", "a_dir_discovery.py"),
    ("string_outside", "../outside_discovery.py"),
    ("mapping", {"file": "comp_discovery.py"}),
    ("mapping_extra_keys", {"file": "comp_discovery.py", "cross_vendor": "x", "other": 1}),
    ("mapping_empty_file", {"file": ""}),
    ("mapping_int_file", {"file": 5}),
    ("mapping_no_file", {}),
    ("list", ["comp_discovery.py"]),
    ("none", None),
    ("int", 7),
    ("no_python_key", ...),
    ("discovery_not_a_mapping", "<list>"),
    ("no_discovery", "<none>"),
]


def _run_files(shape: Any, suffix: str) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="bi_pin_")).resolve()
    try:
        _text(root, "outside_discovery.py", "# outside the category dir\n")
        _text(root, "audio/comp_discovery.py", "# companion\n")
        (root / "audio" / "a_dir_discovery.py").mkdir(parents=True)
        _text(root, "audio/drv_discovery.py", "# convention companion\n")
        _text(root, "audio/drv_sim.py", "# convention sim\n")
        driver = _text(root, f"audio/drv{suffix}", "# driver\n")
        if shape == "<list>":
            raw: dict[str, Any] = {"discovery": ["python"]}
        elif shape == "<none>":
            raw = {}
        elif shape is ...:
            raw = {"discovery": {"port_open": [1]}}
        else:
            raw = {"discovery": {"python": shape}}
        files = build_index._installed_file_set(driver, raw)
        return {"files": [_scrub(str(f), root) for f in files]}
    finally:
        shutil.rmtree(root, ignore_errors=True)


FILE_CASES = [(name, suffix) for name, _ in COMPANION_SHAPES for suffix in (".avcdriver", ".py")]


# --- Observing everything -----------------------------------------------------


def _observe() -> dict[str, Any]:
    return {
        "main": {name: _run_main(stage, argv, schema) for name, stage, argv, schema in MAIN_CASES},
        "cross": {name: _run_cross(name) for name, _ in ENTRY_SETS},
        "devices": {f"{e}+{x}": _run_devices(e, x) for e, x in DEVICE_CASES},
        "info": {
            f"{name}{'_annotated' if annotated else ''}": _run_info(source, annotated)
            for name, source in INFO_CASES for annotated in (False, True)
        },
        "files": {
            f"{name}{suffix}": _run_files(dict(COMPANION_SHAPES)[name], suffix)
            for name, suffix in FILE_CASES
        },
        "fields": {name: _run_fields(fields) for name, fields in FIELD_CASES},
    }


def _expected() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_case_names_are_unique() -> None:
    for names in (
        [c[0] for c in MAIN_CASES], [c[0] for c in ENTRY_SETS],
        [c[0] for c in EXTRA_SETS], [c[0] for c in INFO_CASES],
        [c[0] for c in COMPANION_SHAPES], [c[0] for c in FIELD_CASES],
    ):
        assert len(names) == len(set(names))


def test_fixture_covers_exactly_the_cases() -> None:
    expected = _expected()
    assert sorted(expected["main"]) == sorted(c[0] for c in MAIN_CASES)
    assert sorted(expected["cross"]) == sorted(c[0] for c in ENTRY_SETS)
    assert sorted(expected["devices"]) == sorted(f"{e}+{x}" for e, x in DEVICE_CASES)
    assert len(expected["info"]) == 2 * len(INFO_CASES)
    assert sorted(expected["files"]) == sorted(f"{n}{s}" for n, s in FILE_CASES)
    assert sorted(expected["fields"]) == sorted(n for n, _ in FIELD_CASES)


@pytest.mark.parametrize(
    ("name", "stage", "argv", "schema"), MAIN_CASES, ids=[c[0] for c in MAIN_CASES],
)
def test_main(name: str, stage: Callable[[Path], None], argv: list[str], schema: str) -> None:
    assert _run_main(stage, argv, schema) == _expected()["main"][name]


@pytest.mark.parametrize("name", [c[0] for c in ENTRY_SETS])
def test_cross_driver_checks(name: str) -> None:
    assert _run_cross(name) == _expected()["cross"][name]


@pytest.mark.parametrize(("entry_set", "extra_set"), DEVICE_CASES, ids=[f"{e}+{x}" for e, x in DEVICE_CASES])
def test_devices_catalog(entry_set: str, extra_set: str) -> None:
    assert _run_devices(entry_set, extra_set) == _expected()["devices"][f"{entry_set}+{extra_set}"]


@pytest.mark.parametrize(("name", "source"), INFO_CASES, ids=[c[0] for c in INFO_CASES])
@pytest.mark.parametrize("annotated", [False, True])
def test_index_fields_of_a_python_driver(name: str, source: str, annotated: bool) -> None:
    key = f"{name}{'_annotated' if annotated else ''}"
    assert _run_info(source, annotated) == _expected()["info"][key]


@pytest.mark.parametrize(("name", "fields"), FIELD_CASES, ids=[c[0] for c in FIELD_CASES])
def test_entry_fields(name: str, fields: dict[str, Any]) -> None:
    assert _run_fields(fields) == _expected()["fields"][name]


@pytest.mark.parametrize(("name", "suffix"), FILE_CASES, ids=[f"{n}{s}" for n, s in FILE_CASES])
def test_files_an_install_fetches(name: str, suffix: str) -> None:
    shape = dict(COMPANION_SHAPES)[name]
    assert _run_files(shape, suffix) == _expected()["files"][f"{name}{suffix}"]


# --- What only a whole process shows -----------------------------------------


def test_the_default_schema_sits_under_the_resolved_root(tmp_path: Path) -> None:
    """--root reached through a link names the schema under the real directory."""
    real = (tmp_path / "real").resolve()
    real.mkdir()
    one_yaml(real)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as exc:  # Windows without Developer Mode or admin rights
        pytest.skip(f"cannot create a symbolic link here: {exc}")
    log: list[list[str]] = []
    with _recording(log):
        rc = build_index.main(["--root", str(link)])
    err = "".join(text for stream, text in log if stream == "err")
    assert rc == 0
    assert f"JSON Schema file not found at {real / 'avcdriver.schema.json'}," in err, err


def test_a_stale_catalog_reports_after_the_validation_line(tmp_path: Path) -> None:
    """stdout is buffered in a pipe and stderr is not: the stale report still
    prints after the line before it when both share one pipe."""
    root = tmp_path.resolve()
    _then(_built(valid_tree), edit_driver)(root)
    script = REPO / "scripts" / "build_index.py"
    result = subprocess.run(
        [sys.executable, "-B", str(script), "--root", str(root),
         "--json-schema-file", str(SCHEMA), "--check"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"},
    )
    assert result.returncode == 1
    validated = result.stdout.index("Validated 4 driver(s)")
    stale = result.stdout.index("FAILED: the catalog is out of date")
    assert validated < stale, result.stdout


def test_the_file_loads_without_being_registered(tmp_path: Path) -> None:
    """The golden master loads build_index.py by path and never registers it in
    sys.modules; everything defined at import must survive that."""
    spec = importlib.util.spec_from_file_location(
        "build_index_unregistered", REPO / "scripts" / "build_index.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "build_index_unregistered" not in sys.modules
    path = _py(tmp_path, "projectors/acme_py.py", ports=[-1, 2])
    assert module.extract_python_driver_info(path)["ports"] == [-1, 2]


if __name__ == "__main__":
    if sys.argv[1:] != ["--regen"]:
        sys.exit("usage: python tests/test_build_index_main_pins.py --regen")
    FIXTURE.write_text(json.dumps(_observe(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE}")
