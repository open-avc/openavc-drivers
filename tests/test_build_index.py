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

HERE = Path(__file__).resolve().parent

# Make the scripts/ dir importable so tests can call `main()` directly
SCRIPT_DIR = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import build_index  # noqa: E402

JSON_SCHEMA_PATH = HERE.parent / "avcdriver.schema.json"


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
        # Discovery is optional. The parser warns when no fingerprints
        # or hints are declared; tests that exercise discovery
        # validation supply their own override.
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
        # Use Python repr (not JSON) so True/False/None become valid
        # Python literal nodes that the AST extractor accepts.
        info_repr = repr(info)
        body = f"    DRIVER_INFO = {info_repr}\n"
    fp.write_text(
        "class TestDriver:\n"
        + body,
        encoding="utf-8",
    )
    return fp


@pytest.fixture(
    params=[True, False],
    ids=["with_json_schema", "without_json_schema"],
)
def json_schema(request) -> bool:
    """Fixture that parametrizes tests to run once with JSON Schema validation
    and once without.
    """
    return request.param


def _run(root: Path, *args: str, json_schema_only: bool = False) -> tuple[int, str, str]:
    """Run build_index.main against `root` and capture exit code + stderr.

    By default, the JSON Schema is used to validate drivers.
    Pass `bypass_json_schema=True` to skip that validation and only test the
    script's custom validation.
    """
    import io

    stderr_buf = io.StringIO()
    stdout_buf = io.StringIO()
    real_stderr, real_stdout = sys.stderr, sys.stdout
    sys.stderr, sys.stdout = stderr_buf, stdout_buf
    schema_args = ["--json-schema-file", str(JSON_SCHEMA_PATH)]
    if json_schema_only:
        schema_args.append("--check-json-schema")
    try:
        rc = build_index.main(["--root", str(root), *schema_args, *args])
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
    """--check reports, it never produces the catalog itself."""
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path)
    rc, _, err = _run(tmp_path, "--check")
    # No catalog has been built, so there is nothing matching the drivers —
    # which is exactly the state --check exists to refuse.
    assert rc == 1
    assert "out of date" in err
    assert not (tmp_path / "index.json").exists()
    assert not (tmp_path / "devices.json").exists()


# --- Catalog freshness -----------------------------------------------------
#
# The catalog carries a SHA-256 per driver file and the platform refuses a
# download whose bytes don't match, so a driver edited without regenerating
# the catalog cannot be installed at all. These pin the check that stops one
# landing — it is the only thing standing between "forgot to regenerate" and
# a driver that silently will not install.


def test_check_passes_when_the_catalog_matches_the_drivers(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path)
    assert _run(tmp_path)[0] == 0  # build it
    rc, out, _ = _run(tmp_path, "--check")
    assert rc == 0
    assert "up to date" in out


def test_check_fails_when_a_driver_changed_without_a_rebuild(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    driver = _write_yaml_driver(tmp_path)
    assert _run(tmp_path)[0] == 0  # build it

    # The mistake, exactly as it happens: edit the driver, forget the rebuild.
    driver.write_text(
        driver.read_text(encoding="utf-8") + "\n# a later edit\n", encoding="utf-8"
    )

    rc, _, err = _run(tmp_path, "--check")
    assert rc == 1
    assert "out of date" in err
    assert "index.json" in err
    # The message has to carry the fix, not just the verdict.
    assert "scripts/build_index.py" in err


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
    tmp_path: Path, field: str, bad_value: str, expected_substring: str, json_schema: bool
) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path, overrides={field: bad_value})
    rc, _, err = _run(tmp_path, json_schema_only=json_schema)
    assert rc != 0
    if json_schema:
        assert "JSON Schema validation error" in err, err
    else:
        assert expected_substring in err, err


def test_required_field_missing_fails(tmp_path: Path, json_schema: bool) -> None:
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
    rc, _, err = _run(tmp_path, json_schema_only=json_schema)
    assert rc != 0
    if json_schema:
        assert "JSON Schema validation error" in err, err
    else:
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


def test_discovery_port_open_must_be_list(tmp_path: Path, json_schema: bool) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"port_open": "1710"}},
    )
    rc, _, err = _run(tmp_path, json_schema_only=json_schema)
    assert rc != 0
    if json_schema:
        assert "JSON Schema validation error" in err, err
    else:
        assert "port_open must be a list" in err


def test_discovery_port_open_must_be_int(tmp_path: Path, json_schema: bool) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"port_open": ["1710"]}},
    )
    rc, _, err = _run(tmp_path, json_schema_only=json_schema)
    assert rc != 0
    if json_schema:
        assert "JSON Schema validation error" in err, err
    else:
        assert "must be integers" in err


def test_discovery_port_open_out_of_range(tmp_path: Path, json_schema: bool) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"port_open": [70000]}},
    )
    rc, _, err = _run(tmp_path, json_schema_only=json_schema)
    assert rc != 0
    if json_schema:
        assert "JSON Schema validation error" in err, err
    else:
        assert "out of range" in err


@pytest.mark.parametrize("port", [22, 80, 443, 8000, 8080, 8443, 8888])
def test_discovery_port_open_too_generic(tmp_path: Path, port: int, json_schema: bool) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"port_open": [port]}},
    )
    rc, _, err = _run(tmp_path, json_schema_only=json_schema)
    assert rc != 0
    if json_schema:
        assert "JSON Schema validation error" in err, err
    else:
        assert "too generic" in err


def test_discovery_port_open_valid_pass(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"port_open": [1710, 4352]}},
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err


# --- The `requires` catalog-compat gate --------------------------------------
#
# Pre-0.23.0 platform parsers silently ignore SSDP description filters,
# collapsing distinct filtered claims into colliding unfiltered ones —
# which used to knock the ENTIRE catalog out of their scans. The build
# stamps `requires: "0.23.0"` onto any entry using the filters, a
# top-level discovery key old parsers reject, so they skip just that
# driver's hints.


def _index_discovery(tmp_path: Path, driver_id: str = "test_driver") -> dict:
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    (entry,) = [d for d in index["drivers"] if d["id"] == driver_id]
    return entry.get("discovery") or {}


def test_ssdp_filters_stamp_requires_on_catalog_entry(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"ssdp": [
            {"device_type": "urn:acme:device:MixerFamily:1", "model": "Mixer-6"},
        ]}},
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err
    disc = _index_discovery(tmp_path)
    assert disc["requires"] == "0.23.0"
    # The ssdp entries themselves pass through unchanged.
    assert disc["ssdp"] == [
        {"device_type": "urn:acme:device:MixerFamily:1", "model": "Mixer-6"},
    ]


def test_plain_ssdp_emits_no_requires(tmp_path: Path) -> None:
    # Unfiltered entries stay readable by every platform — no gate.
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"ssdp": "urn:acme:device:Widget:1"}},
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err
    assert "requires" not in _index_discovery(tmp_path)


def test_hand_authored_newer_requires_kept(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {
            "requires": "0.30.0",
            "ssdp": [{"device_type": "urn:acme:device:MixerFamily:1",
                      "model": "Mixer-6"}],
        }},
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err
    assert _index_discovery(tmp_path)["requires"] == "0.30.0"


def test_hand_authored_older_requires_raised_to_gate(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {
            "requires": "0.1.0",
            "ssdp": [{"device_type": "urn:acme:device:MixerFamily:1",
                      "model": "Mixer-6"}],
        }},
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err
    assert _index_discovery(tmp_path)["requires"] == "0.23.0"


def test_unparseable_requires_rejected(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"requires": "latest"}},
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "discovery.requires" in err


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


def test_python_driver_may_use_ssh_transport(tmp_path: Path) -> None:
    # ssh is valid for Python drivers (they can drive an SSH CLI session), so
    # the schema check allows it for .py files even though the .avcdriver schema
    # omits it. See _python_driver_schema in build_index.py.
    _write_manufacturers(tmp_path)
    _write_python_driver(tmp_path, info_overrides={"transport": "ssh"})
    rc, _, err = _run(tmp_path, json_schema_only=True)
    assert rc == 0, err


def test_yaml_driver_may_not_use_ssh_transport(tmp_path: Path) -> None:
    # The YAML schema deliberately omits ssh: a declarative driver can't drive
    # an SSH CLI session, so the check keeps catching it at authoring time.
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path, overrides={"transport": "ssh"})
    rc, _, err = _run(tmp_path, json_schema_only=True)
    assert rc != 0
    assert "JSON Schema validation error" in err, err


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


# --- platform validation rules (the vendored copy build_index runs) ---------
# Rejection behavior is pinned case-by-case in test_vendored_contract.py;
# these spot-check the wiring plus the valid shapes the rejection corpus
# can't cover.


def _platform_errors(overrides: dict) -> list[str]:
    base = {"id": "acme_widget", "name": "Acme Widget", "transport": "tcp"}
    base.update(overrides)
    return build_index.validate_driver_definition(base)


def test_frame_parser_valid_header_sizes() -> None:
    for size in (1, 2, 4):
        data = {"frame_parser": {"type": "length_prefix", "header_size": size}}
        assert _platform_errors(data) == []


def test_frame_parser_bad_header_size() -> None:
    for bad in (3, 8, 0):
        data = {"frame_parser": {"type": "length_prefix", "header_size": bad}}
        assert any("header_size" in e for e in _platform_errors(data)), bad


def test_frame_parser_negative_offset_ok() -> None:
    data = {"frame_parser": {"type": "length_prefix", "header_size": 2,
                             "header_offset": -2}}
    assert _platform_errors(data) == []


def test_frame_parser_bad_offset_type() -> None:
    data = {"frame_parser": {"type": "length_prefix", "header_offset": "two"}}
    assert any("header_offset" in e for e in _platform_errors(data))


def test_frame_parser_fixed_length() -> None:
    ok = {"frame_parser": {"type": "fixed_length", "length": 8}}
    assert _platform_errors(ok) == []
    bad = {"frame_parser": {"type": "fixed_length", "length": 0}}
    assert any("length" in e for e in _platform_errors(bad))


def test_frame_parser_unknown_type() -> None:
    data = {"frame_parser": {"type": "crc16"}}
    errs = _platform_errors(data)
    assert any("crc16" in e or "type" in e for e in errs)


def test_frame_parser_absent_is_ok() -> None:
    assert _platform_errors({}) == []


def test_child_entity_types_valid() -> None:
    data = {"child_entity_types": {"encoder": {}, "decoder": {}, "zone_1": {}}}
    assert _platform_errors(data) == []


def test_child_entity_types_must_be_mapping() -> None:
    data = {"child_entity_types": ["encoder", "decoder"]}
    assert any("mapping" in e for e in _platform_errors(data))


def test_child_entity_types_rejects_dots() -> None:
    # A dot would corrupt device.<id>.<child_type>.<local_id>.<prop> keys.
    data = {"child_entity_types": {"enc.oder": {}}}
    assert any("dots" in e for e in _platform_errors(data))


def test_child_entity_types_rejects_glob() -> None:
    # Glob metachars break the fnmatch dispatch routing per-child state changes.
    for name in ("enc*", "dec?", "zone["):
        data = {"child_entity_types": {name: {}}}
        assert any("glob" in e for e in _platform_errors(data)), name


def test_child_entity_types_rejects_empty_name() -> None:
    data = {"child_entity_types": {"": {}}}
    assert any("non-empty" in e for e in _platform_errors(data))


def test_invalid_yaml_driver_fails_check_with_platform_message(tmp_path: Path) -> None:
    # End-to-end: a YAML driver the platform loader would reject must fail
    # the catalog check with the same rule's message, prefixed by the file.
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        overrides={"polling": {"interval": 5, "queries": ["PWR?\r"]}},
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "audio/test_driver.avcdriver" in err
    assert "polling.interval" in err


def test_python_driver_index_fields_only_skips_platform_rules(tmp_path: Path) -> None:
    # Python drivers contribute only index fields to the catalog; the
    # platform validates their full DRIVER_INFO at load. A Python-only
    # capability in DRIVER_INFO must not trip the YAML rules here.
    _write_manufacturers(tmp_path)
    _write_python_driver(
        tmp_path,
        info_overrides={"transport": "ssh"},
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err


# --- Artifact hashes (`files`) ----------------------------------------------
#
# The platform hashes every file it downloads during an install and compares it
# against this map before writing anything to driver_repo/. So the map has to
# describe exactly the set the installer fetches: a hash that is wrong, missing,
# or for a file the installer never asks for all break that check.


def _sha256_of(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _entry_by_id(root: Path, driver_id: str) -> dict:
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    return next(d for d in index["drivers"] if d["id"] == driver_id)


def test_files_map_hashes_the_driver_file(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    yaml_path = _write_yaml_driver(tmp_path)
    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    entry = _entry_by_id(tmp_path, "test_driver")
    assert entry["files"] == {"audio/test_driver.avcdriver": _sha256_of(yaml_path)}
    # The map is keyed by the same repo-relative path the entry advertises,
    # so the installer can look up what it just downloaded.
    assert entry["file"] in entry["files"]


def test_files_map_tracks_edits_to_the_driver(tmp_path: Path) -> None:
    # A real hash of real bytes, not a placeholder: editing the driver must
    # move the hash, which is what makes a stale catalog detectable.
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path)
    rc, _, _ = _run(tmp_path)
    assert rc == 0
    before = _entry_by_id(tmp_path, "test_driver")["files"]

    _write_yaml_driver(tmp_path, overrides={"description": "Edited description."})
    rc, _, _ = _run(tmp_path)
    assert rc == 0
    after = _entry_by_id(tmp_path, "test_driver")["files"]

    assert before != after


def test_files_map_includes_declared_yaml_companion(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    companion = tmp_path / "audio" / "test_driver_discovery.py"
    companion.parent.mkdir(parents=True, exist_ok=True)
    companion.write_text("def probe(ctx):\n    return None\n", encoding="utf-8")
    _write_yaml_driver(
        tmp_path,
        overrides={"discovery": {"python": "./test_driver_discovery.py"}},
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    files = _entry_by_id(tmp_path, "test_driver")["files"]
    assert files["audio/test_driver_discovery.py"] == _sha256_of(companion)


def test_files_map_includes_declared_companion_in_mapping_form(tmp_path: Path) -> None:
    # `discovery.python` accepts a bare path or a {file, cross_vendor} mapping.
    # Both declare the same companion, so both must be hashed.
    _write_manufacturers(tmp_path)
    companion = tmp_path / "audio" / "test_driver_discovery.py"
    companion.parent.mkdir(parents=True, exist_ok=True)
    companion.write_text("def probe(ctx):\n    return None\n", encoding="utf-8")
    _write_yaml_driver(
        tmp_path,
        overrides={
            "discovery": {
                "python": {"file": "./test_driver_discovery.py", "cross_vendor": True}
            }
        },
    )
    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    files = _entry_by_id(tmp_path, "test_driver")["files"]
    assert files["audio/test_driver_discovery.py"] == _sha256_of(companion)


def test_files_map_includes_python_convention_companions(tmp_path: Path) -> None:
    _write_manufacturers(tmp_path)
    main = _write_python_driver(tmp_path)
    sim = tmp_path / "projectors" / "test_driver_sim.py"
    disco = tmp_path / "projectors" / "test_driver_discovery.py"
    sim.write_text("SIM = True\n", encoding="utf-8")
    disco.write_text("def probe(ctx):\n    return None\n", encoding="utf-8")

    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    files = _entry_by_id(tmp_path, "test_py_driver")["files"]
    assert files == {
        "projectors/test_driver.py": _sha256_of(main),
        "projectors/test_driver_discovery.py": _sha256_of(disco),
        "projectors/test_driver_sim.py": _sha256_of(sim),
    }


def test_files_map_omits_python_companions_that_do_not_exist(tmp_path: Path) -> None:
    # The platform fetches these by naming convention and treats a 404 as
    # "ships without one". Listing an absent file would make every install of
    # this driver look truncated.
    _write_manufacturers(tmp_path)
    main = _write_python_driver(tmp_path)
    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    files = _entry_by_id(tmp_path, "test_py_driver")["files"]
    assert files == {"projectors/test_driver.py": _sha256_of(main)}


def test_files_map_omits_yaml_sim_companion(tmp_path: Path) -> None:
    # A YAML driver simulates from its inline `simulator:` block, so the
    # install path never fetches a sibling _sim.py even when the repo ships
    # one. Listing it would be a hash the installer can never satisfy.
    _write_manufacturers(tmp_path)
    yaml_path = _write_yaml_driver(tmp_path)
    (tmp_path / "audio" / "test_driver_sim.py").write_text("SIM = True\n", encoding="utf-8")

    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    files = _entry_by_id(tmp_path, "test_driver")["files"]
    assert files == {"audio/test_driver.avcdriver": _sha256_of(yaml_path)}


def test_files_map_cannot_be_declared_by_the_driver(tmp_path: Path) -> None:
    # `files` is computed from bytes on disk. A driver that declares its own
    # must not be able to pin a hash of its choosing into the catalog — and it
    # is now refused outright rather than quietly ignored, because `files` is
    # not a key the driver format has. (The computed map is asserted by the
    # two tests above; this one is about an author who tries to supply it.)
    _write_manufacturers(tmp_path)
    _write_yaml_driver(
        tmp_path,
        extra_yaml='files: {"audio/test_driver.avcdriver": "deadbeef"}\n',
    )
    rc, _, err = _run(tmp_path)
    assert rc != 0
    assert "unknown key 'files'" in err, err


def test_shard_entries_carry_the_same_hashes(tmp_path: Path) -> None:
    # Installs can be driven from a category shard as well as the monolith.
    _write_manufacturers(tmp_path)
    _write_yaml_driver(tmp_path)
    rc, _, err = _run(tmp_path)
    assert rc == 0, err

    shard = json.loads((tmp_path / "index" / "audio.json").read_text(encoding="utf-8"))
    assert shard["drivers"][0]["files"] == _entry_by_id(tmp_path, "test_driver")["files"]
