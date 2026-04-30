"""Tests for scripts/build_index.py.

Each test stages a synthetic driver tree under a tmp_path and runs the
build script's `main()` against it. Validation failures are asserted by
catching the non-zero return code and inspecting captured stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make the scripts/ dir importable so tests can call `main()` directly
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import build_index  # noqa: E402


# --- Fixtures and helpers ---------------------------------------------------


MANUFACTURERS_DEFAULT = [
    "Acme",
    "AcmeAudio",
    "Generic",
    "Other",
]


def _write_manufacturers(root: Path, mfrs: list[str] | None = None) -> None:
    (root / "manufacturers.json").write_text(
        json.dumps(mfrs if mfrs is not None else MANUFACTURERS_DEFAULT),
        encoding="utf-8",
    )


def _write_yaml_driver(
    root: Path,
    *,
    category: str = "audio",
    filename: str = "test_driver.avcdriver",
    overrides: dict | None = None,
    extra_yaml: str = "",
) -> Path:
    """Stage a minimally-valid YAML driver. Pass `overrides` to mutate fields."""
    data = {
        "id": "test_driver",
        "name": "Test Driver",
        "manufacturer": "Acme",
        "category": "audio",
        "version": "1.0.0",
        "author": "Tester",
        "transport": "tcp",
        "description": "Fixture driver for tests.",
        "source_url": "https://example.com/test-protocol",
    }
    if overrides:
        data.update(overrides)
    cat_dir = root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    fp = cat_dir / filename
    lines = [f"{k}: {json.dumps(v)}" for k, v in data.items()]
    fp.write_text("\n".join(lines) + "\n" + extra_yaml, encoding="utf-8")
    return fp


def _write_python_driver(
    root: Path,
    *,
    category: str = "projectors",
    filename: str = "test_driver.py",
    info_overrides: dict | None = None,
    raw_class_body: str | None = None,
) -> Path:
    """Stage a minimally-valid Python driver."""
    info = {
        "id": "test_py_driver",
        "name": "Test Python Driver",
        "manufacturer": "Acme",
        "category": "projector",
        "version": "1.0.0",
        "author": "Tester",
        "transport": "tcp",
        "description": "Python fixture.",
        "source_url": "https://example.com/test-protocol",
    }
    if info_overrides:
        info.update(info_overrides)
    cat_dir = root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    fp = cat_dir / filename
    if raw_class_body is not None:
        body = raw_class_body
    else:
        info_repr = json.dumps(info, indent=4)
        body = f"    DRIVER_INFO = {info_repr}\n"
    fp.write_text(
        "class TestDriver:\n"
        + body,
        encoding="utf-8",
    )
    return fp


def _run(root: Path, *args: str) -> tuple[int, str, str]:
    """Run build_index.main against `root` and capture exit code + stderr."""
    import io

    stderr_buf = io.StringIO()
    stdout_buf = io.StringIO()
    real_stderr, real_stdout = sys.stderr, sys.stdout
    sys.stderr, sys.stdout = stderr_buf, stdout_buf
    try:
        rc = build_index.main(["--root", str(root), *args])
    finally:
        sys.stderr, sys.stdout = real_stderr, real_stdout
    return rc, stdout_buf.getvalue(), stderr_buf.getvalue()


# --- Round trip ------------------------------------------------------------


def test_round_trip_valid_drivers(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path)
    _write_python_driver(tmp_path)

    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert "_meta" in index
    assert index["_meta"]["schema_version"] == "1"
    assert index["_meta"]["total_drivers"] == 2
    assert index["_meta"]["shards"] is None

    ids = [d["id"] for d in index["drivers"]]
    assert set(ids) == {"test_driver", "test_py_driver"}

    # Every driver should have file + format set by the collector
    for d in index["drivers"]:
        assert d["file"]
        assert d["format"] in ("avcdriver", "python")


def test_devices_json_emitted_alongside_index(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        extra_yaml=(
            "compatible_models:\n"
            "  - manufacturer: Acme\n"
            "    models: [Model-X, Model-Y]\n"
            "    confidence: full\n"
        ),
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    devices = json.loads((tmp_path / "devices.json").read_text(encoding="utf-8"))
    assert devices["_meta"]["total_devices"] == 2
    by_model = {d["model"]: d for d in devices["devices"]}
    assert "Model-X" in by_model
    assert by_model["Model-X"]["drivers"][0]["id"] == "test_driver"
    assert by_model["Model-X"]["drivers"][0]["confidence"] == "full"


def test_shards_emitted_per_category(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path)
    rc, _, _ = _run(tmp_path)
    assert rc == 0

    audio_shard = tmp_path / "index" / "audio.json"
    assert audio_shard.exists()
    parsed = json.loads(audio_shard.read_text(encoding="utf-8"))
    assert parsed["_meta"]["category"] == "audio"
    assert len(parsed["drivers"]) == 1

    # Empty category still gets a shard with zero drivers
    lighting_shard = tmp_path / "index" / "lighting.json"
    assert lighting_shard.exists()
    assert json.loads(lighting_shard.read_text(encoding="utf-8"))["drivers"] == []


def test_check_mode_does_not_write(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path)
    rc, _, _ = _run(tmp_path, "--check")
    assert rc == 0
    assert not (tmp_path / "index.json").exists()
    assert not (tmp_path / "devices.json").exists()


# --- Field-level validation ------------------------------------------------


@pytest.mark.parametrize(
    "field,bad_value,expected_substring",
    [
        ("id", "My_Driver", "lowercase alphanumeric"),
        ("category", "phasers", "must be one of"),
        ("transport", "bluetooth", "must be one of"),
        ("version", "1.0", "valid semver"),
        ("source_url", "see-the-manual", "http://"),
    ],
)
def test_field_validation_rejects_bad_values(
    tmp_path: Path, field: str, bad_value: str, expected_substring: str
) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path, overrides={field: bad_value})
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert expected_substring in err, err


def test_required_field_missing_fails(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    # Build a driver missing `description`
    cat_dir = tmp_path / "audio"
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / "test_driver.avcdriver").write_text(
        'id: test_driver\n'
        'name: Test\n'
        'manufacturer: Acme\n'
        'category: audio\n'
        'version: 1.0.0\n'
        'author: Tester\n'
        'transport: tcp\n'
        'source_url: https://example.com\n',
        encoding="utf-8",
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "description" in err


def test_tags_must_be_lowercase_hyphenated(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        extra_yaml="tags: [NDI, ceiling-mic]\n",
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "tag" in err.lower()


def test_tags_lowercase_hyphenated_pass(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        extra_yaml="tags: [ndi, ceiling-mic]\n",
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err


def test_help_block_requires_overview_and_setup(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        extra_yaml="help:\n  overview: Just an overview\n",  # missing setup
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "setup" in err


def test_ports_must_be_in_range(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        extra_yaml="ports: [70000]\n",
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "65535" in err


# --- Cross-driver validation -----------------------------------------------


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path, filename="a.avcdriver")
    _write_yaml_driver(tmp_path, filename="b.avcdriver")  # same id "test_driver"
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "duplicate" in err.lower()


def test_unknown_manufacturer_rejected(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path, mfrs=["Acme"])
    _write_yaml_driver(tmp_path, overrides={"manufacturer": "Unknown Co"})
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "manufacturers.json" in err


def test_unknown_compatible_models_manufacturer_rejected(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path, mfrs=["Acme"])
    _write_yaml_driver(
        tmp_path,
        extra_yaml=(
            "compatible_models:\n"
            "  - manufacturer: Mystery\n"
            "    models: [Model-A]\n"
            "    confidence: full\n"
        ),
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "Mystery" in err


def test_replacement_id_required_when_deprecated(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path, extra_yaml="deprecated: true\n")
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "replacement_id" in err


def test_replacement_id_invalid_when_not_deprecated(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path, extra_yaml='replacement_id: other_driver\n')
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "deprecated" in err.lower()


def test_replacement_id_must_resolve(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        extra_yaml="deprecated: true\nreplacement_id: nonexistent\n",
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "nonexistent" in err


def test_replacement_id_resolves_pass(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        filename="old.avcdriver",
        overrides={"id": "old_driver"},
        extra_yaml="deprecated: true\nreplacement_id: new_driver\n",
    )
    _write_yaml_driver(
        tmp_path,
        filename="new.avcdriver",
        overrides={"id": "new_driver", "name": "New Driver"},
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err


def test_overlapping_full_confidence_without_notes_rejected(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        filename="a.avcdriver",
        overrides={"id": "driver_a"},
        extra_yaml=(
            "compatible_models:\n"
            "  - manufacturer: Acme\n"
            "    models: [Model-X]\n"
            "    confidence: full\n"
        ),
    )
    _write_yaml_driver(
        tmp_path,
        filename="b.avcdriver",
        overrides={"id": "driver_b", "name": "Driver B"},
        extra_yaml=(
            "compatible_models:\n"
            "  - manufacturer: Acme\n"
            "    models: [Model-X]\n"
            "    confidence: full\n"
        ),
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "Model-X" in err


def test_overlapping_full_confidence_with_notes_pass(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        filename="a.avcdriver",
        overrides={"id": "driver_a"},
        extra_yaml=(
            "compatible_models:\n"
            "  - manufacturer: Acme\n"
            "    models: [Model-X]\n"
            "    confidence: full\n"
            "    notes: Older firmware\n"
        ),
    )
    _write_yaml_driver(
        tmp_path,
        filename="b.avcdriver",
        overrides={"id": "driver_b", "name": "Driver B"},
        extra_yaml=(
            "compatible_models:\n"
            "  - manufacturer: Acme\n"
            "    models: [Model-X]\n"
            "    confidence: full\n"
            "    notes: Newer firmware\n"
        ),
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err


# --- Python AST extraction -------------------------------------------------


def test_module_level_driver_info_rejected(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    cat = tmp_path / "projectors"
    cat.mkdir(parents=True, exist_ok=True)
    (cat / "bad_driver.py").write_text(
        'DRIVER_INFO = {"id": "bad_driver"}\n',
        encoding="utf-8",
    )
    # Add one valid driver so the build doesn't fail "no drivers"
    _write_yaml_driver(tmp_path)
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "DRIVER_INFO" in err or "no DRIVER_INFO" in err


def test_dict_constructor_call_in_driver_info_rejected(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_python_driver(
        tmp_path,
        raw_class_body='    DRIVER_INFO = dict(id="x", name="y")\n',
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "literal" in err.lower() or "DRIVER_INFO" in err


def test_python_driver_with_nested_compatible_models(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_python_driver(
        tmp_path,
        info_overrides={
            "compatible_models": [
                {
                    "manufacturer": "Acme",
                    "models": ["Model-1", "Model-2"],
                    "confidence": "partial",
                    "notes": "Lacks NDI",
                }
            ],
        },
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err
    devices = json.loads((tmp_path / "devices.json").read_text(encoding="utf-8"))
    by_model = {d["model"]: d for d in devices["devices"]}
    assert by_model["Model-1"]["drivers"][0]["confidence"] == "partial"
    assert by_model["Model-1"]["drivers"][0]["notes"] == "Lacks NDI"


# --- devices-extra ---------------------------------------------------------


def test_devices_extra_merged(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path)
    (tmp_path / "devices-extra.json").write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "manufacturer": "Other",
                        "model": "Untestable-100",
                        "category": "switcher",
                        "drivers": [],
                        "status": "no_driver",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err
    devices = json.loads((tmp_path / "devices.json").read_text(encoding="utf-8"))
    by_model = {d["model"]: d for d in devices["devices"]}
    assert by_model["Untestable-100"]["status"] == "no_driver"


def test_devices_extra_collision_with_driver_rejected(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        extra_yaml=(
            "compatible_models:\n"
            "  - manufacturer: Acme\n"
            "    models: [Model-X]\n"
            "    confidence: full\n"
        ),
    )
    (tmp_path / "devices-extra.json").write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "manufacturer": "Acme",
                        "model": "Model-X",
                        "category": "audio",
                        "drivers": [],
                        "status": "no_driver",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "Model-X" in err
