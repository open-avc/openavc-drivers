"""Validate all driver files and index.json in the community driver repository."""

import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

REQUIRED_INDEX_FIELDS = {"id", "name", "file", "format", "category", "manufacturer", "version", "author", "transport", "description"}
VALID_FORMATS = {"avcdriver", "python"}
VALID_TRANSPORTS = {"tcp", "serial", "udp", "http"}

REQUIRED_AVCDRIVER_FIELDS = {"name", "id", "transport"}


def validate_index() -> list[str]:
    """Validate index.json structure and references."""
    errors = []
    index_path = REPO_ROOT / "index.json"

    if not index_path.exists():
        return ["index.json not found"]

    try:
        with open(index_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return [f"index.json is not valid JSON: {e}"]

    if "drivers" not in data:
        return ["index.json missing 'drivers' array"]

    drivers = data["drivers"]
    seen_ids = set()
    seen_files = set()

    for i, driver in enumerate(drivers):
        prefix = f"index.json drivers[{i}]"

        # Check required fields
        missing = REQUIRED_INDEX_FIELDS - set(driver.keys())
        if missing:
            errors.append(f"{prefix}: missing fields: {', '.join(sorted(missing))}")
            continue

        driver_id = driver["id"]
        driver_file = driver["file"]

        # Check for duplicate IDs
        if driver_id in seen_ids:
            errors.append(f"{prefix}: duplicate driver ID '{driver_id}'")
        seen_ids.add(driver_id)

        # Check for duplicate files
        if driver_file in seen_files:
            errors.append(f"{prefix} ({driver_id}): duplicate file '{driver_file}'")
        seen_files.add(driver_file)

        # Check file exists
        file_path = REPO_ROOT / driver_file
        if not file_path.exists():
            errors.append(f"{prefix} ({driver_id}): file not found: {driver_file}")

        # Check format is valid
        fmt = driver["format"]
        if fmt not in VALID_FORMATS:
            errors.append(f"{prefix} ({driver_id}): invalid format '{fmt}', expected one of {VALID_FORMATS}")

        # Check format matches file extension
        if fmt == "avcdriver" and not driver_file.endswith(".avcdriver"):
            errors.append(f"{prefix} ({driver_id}): format is 'avcdriver' but file doesn't end with .avcdriver")
        if fmt == "python" and not driver_file.endswith(".py"):
            errors.append(f"{prefix} ({driver_id}): format is 'python' but file doesn't end with .py")

        # Check transport is valid
        transport = driver["transport"]
        if transport not in VALID_TRANSPORTS:
            errors.append(f"{prefix} ({driver_id}): invalid transport '{transport}', expected one of {VALID_TRANSPORTS}")

    return errors


def validate_avcdriver_files() -> list[str]:
    """Validate all .avcdriver YAML files parse correctly and have required fields."""
    errors = []

    for path in REPO_ROOT.rglob("*.avcdriver"):
        rel = path.relative_to(REPO_ROOT)
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            errors.append(f"{rel}: invalid YAML: {e}")
            continue

        if not isinstance(data, dict):
            errors.append(f"{rel}: expected a YAML mapping, got {type(data).__name__}")
            continue

        missing = REQUIRED_AVCDRIVER_FIELDS - set(data.keys())
        if missing:
            errors.append(f"{rel}: missing required fields: {', '.join(sorted(missing))}")

    return errors


def validate_python_files() -> list[str]:
    """Validate all Python driver files have valid syntax."""
    errors = []

    # Only check .py files in driver category directories (not .github/scripts/)
    driver_dirs = ["projectors", "displays", "switchers", "audio", "cameras", "video", "lighting", "utility"]
    for dir_name in driver_dirs:
        dir_path = REPO_ROOT / dir_name
        if not dir_path.exists():
            continue
        for path in dir_path.glob("*.py"):
            rel = path.relative_to(REPO_ROOT)
            try:
                with open(path) as f:
                    source = f.read()
                compile(source, str(rel), "exec")
            except SyntaxError as e:
                errors.append(f"{rel}: syntax error at line {e.lineno}: {e.msg}")

    return errors


def main():
    print("Validating OpenAVC community drivers...")
    all_errors = []

    print("  Checking index.json...")
    all_errors.extend(validate_index())

    print("  Checking .avcdriver files...")
    all_errors.extend(validate_avcdriver_files())

    print("  Checking Python driver syntax...")
    all_errors.extend(validate_python_files())

    if all_errors:
        print(f"\nFound {len(all_errors)} error(s):")
        for error in all_errors:
            print(f"  - {error}")
        sys.exit(1)
    else:
        print("\nAll checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
