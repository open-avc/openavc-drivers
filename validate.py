#!/usr/bin/env python3
"""
OpenAVC Driver Validator

Validates .avcdriver YAML files and Python driver files for correctness,
and optionally checks consistency with index.json.

Usage:
    python validate.py                              # Validate all drivers
    python validate.py switchers/my_driver.avcdriver # Validate specific file(s)
    python validate.py --check-index                 # Also validate index.json
    python validate.py --verbose                     # Show passing checks too
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    try:
        # PyYAML might be installed as _yaml
        import _yaml as yaml
    except ImportError:
        print("ERROR: PyYAML is required. Install with: pip install pyyaml")
        sys.exit(1)

VALID_TRANSPORTS = {"tcp", "serial", "http", "udp"}
VALID_CATEGORIES = {
    "projector", "display", "switcher", "scaler", "audio",
    "camera", "lighting", "relay", "utility", "other", "video",
}
VALID_STATE_TYPES = {"string", "integer", "number", "boolean", "enum", "float"}
VALID_PARAM_TYPES = {"string", "integer", "number", "boolean", "enum"}
VALID_CONFIG_TYPES = {"string", "integer", "number", "boolean", "enum", "object"}
VALID_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}
VALID_FRAME_PARSER_TYPES = {"length_prefix", "fixed_length"}

DRIVER_DIRS = [
    "projectors", "displays", "switchers", "audio",
    "cameras", "video", "lighting", "utility",
]


class ValidationResult:
    def __init__(self, file_path):
        self.file_path = file_path
        self.errors = []
        self.warnings = []

    def error(self, msg):
        self.errors.append(msg)

    def warn(self, msg):
        self.warnings.append(msg)

    @property
    def passed(self):
        return len(self.errors) == 0


def validate_id_format(driver_id):
    """Check that ID is lowercase with underscores only."""
    return bool(re.match(r'^[a-z][a-z0-9_]*$', driver_id))


def validate_regex_pattern(pattern):
    """Check that a regex pattern compiles without error."""
    try:
        compiled = re.compile(pattern)
        # Check for nested quantifiers (catastrophic backtracking)
        if re.search(r'\([^)]*[+*][^)]*\)[+*]', pattern):
            return False, "Nested quantifiers detected (potential catastrophic backtracking)"
        return True, None
    except re.error as e:
        return False, str(e)


def validate_yaml_driver(file_path, data, result):
    """Validate a parsed .avcdriver YAML definition."""

    # Required fields
    for field in ("id", "name", "transport"):
        if field not in data:
            result.error(f"Missing required field: {field}")

    # ID format
    if "id" in data:
        if not validate_id_format(data["id"]):
            result.error(f"Invalid driver ID '{data['id']}': must be lowercase letters, numbers, and underscores, starting with a letter")

    # Transport
    if "transport" in data:
        if data["transport"] not in VALID_TRANSPORTS:
            result.error(f"Invalid transport '{data['transport']}': must be one of {sorted(VALID_TRANSPORTS)}")

    # Category
    if "category" in data:
        if data["category"] not in VALID_CATEGORIES:
            result.error(f"Invalid category '{data['category']}': must be one of {sorted(VALID_CATEGORIES)}")

    # Check category matches directory
    rel_path = file_path.relative_to(file_path.parent.parent) if file_path.parent.name in DRIVER_DIRS else None
    if rel_path and "category" in data:
        dir_name = file_path.parent.name
        expected_categories = {
            "projectors": "projector",
            "displays": "display",
            "switchers": {"switcher", "scaler"},
            "audio": "audio",
            "cameras": "camera",
            "video": "video",
            "lighting": "lighting",
            "utility": {"utility", "relay"},
        }
        expected = expected_categories.get(dir_name)
        if expected:
            if isinstance(expected, str):
                expected = {expected}
            if data["category"] not in expected:
                result.warn(f"Category '{data['category']}' doesn't match directory '{dir_name}/' (expected {sorted(expected)})")

    # Version format
    if "version" in data:
        if not re.match(r'^\d+\.\d+\.\d+', str(data["version"])):
            result.warn(f"Version '{data['version']}' doesn't follow semver format (X.Y.Z)")

    # State variables
    if "state_variables" in data:
        if not isinstance(data["state_variables"], dict):
            result.error("state_variables must be a dict")
        else:
            for var_id, var_def in data["state_variables"].items():
                if not isinstance(var_def, dict):
                    result.error(f"State variable '{var_id}' must be a dict")
                    continue
                if "label" not in var_def:
                    result.error(f"State variable '{var_id}' missing required 'label' field")
                if "type" in var_def and var_def["type"] not in VALID_STATE_TYPES:
                    result.error(f"State variable '{var_id}' has invalid type '{var_def['type']}': must be one of {sorted(VALID_STATE_TYPES)}")
                if var_def.get("type") == "enum" and "values" not in var_def:
                    result.error(f"State variable '{var_id}' is type 'enum' but missing 'values' list")

    # Commands
    transport = data.get("transport", "tcp")
    if "commands" in data:
        if not isinstance(data["commands"], dict):
            result.error("commands must be a dict")
        else:
            for cmd_id, cmd_def in data["commands"].items():
                if not isinstance(cmd_def, dict):
                    result.error(f"Command '{cmd_id}' must be a dict")
                    continue
                if "label" not in cmd_def:
                    result.warn(f"Command '{cmd_id}' missing 'label' field")

                if transport in ("tcp", "serial"):
                    has_send = "send" in cmd_def or "string" in cmd_def
                    has_http = "method" in cmd_def or "path" in cmd_def
                    if has_http and not has_send:
                        result.error(f"Command '{cmd_id}': TCP/serial commands should use 'send', not 'method'/'path' (those are for HTTP)")
                elif transport == "http":
                    has_send = "send" in cmd_def or "string" in cmd_def
                    has_http = "method" in cmd_def or "path" in cmd_def
                    if has_send and not has_http:
                        result.error(f"Command '{cmd_id}': HTTP commands should use 'method'/'path', not 'send' (that's for TCP/serial)")
                    if "method" in cmd_def and cmd_def["method"] not in VALID_HTTP_METHODS:
                        result.error(f"Command '{cmd_id}': invalid HTTP method '{cmd_def['method']}'")

                # Validate params
                if "params" in cmd_def and isinstance(cmd_def["params"], dict):
                    for param_id, param_def in cmd_def["params"].items():
                        if isinstance(param_def, dict) and "type" in param_def:
                            if param_def["type"] not in VALID_PARAM_TYPES:
                                result.error(f"Command '{cmd_id}' param '{param_id}' has invalid type '{param_def['type']}'")

    # Responses
    if "responses" in data:
        if not isinstance(data["responses"], list):
            result.error("responses must be a list")
        else:
            for i, resp in enumerate(data["responses"]):
                if not isinstance(resp, dict):
                    result.error(f"Response #{i+1} must be a dict")
                    continue
                pattern = resp.get("match") or resp.get("pattern")
                if not pattern:
                    result.error(f"Response #{i+1} missing 'match' or 'pattern' field")
                    continue

                # Config substitution placeholders won't compile as regex directly
                # Replace {config_key} with a dummy value for validation
                test_pattern = re.sub(r'\{[a-zA-Z_][a-zA-Z0-9_]*\}', 'PLACEHOLDER', str(pattern))
                valid, err = validate_regex_pattern(test_pattern)
                if not valid:
                    result.error(f"Response #{i+1} pattern '{pattern}' is invalid regex: {err}")

                has_set = "set" in resp
                has_mappings = "mappings" in resp
                if not has_set and not has_mappings:
                    result.warn(f"Response #{i+1} has no 'set' or 'mappings' -- it will match but do nothing")

    # Polling
    if "polling" in data:
        polling = data["polling"]
        if isinstance(polling, dict):
            if "queries" in polling and not isinstance(polling["queries"], list):
                result.error("polling.queries must be a list")
            if "interval" in polling:
                try:
                    interval = float(polling["interval"])
                    if interval <= 0:
                        result.error("polling.interval must be positive")
                except (TypeError, ValueError):
                    result.error("polling.interval must be a number")

    # Device settings
    if "device_settings" in data:
        if not isinstance(data["device_settings"], dict):
            result.error("device_settings must be a dict")
        else:
            for setting_id, setting_def in data["device_settings"].items():
                if not isinstance(setting_def, dict):
                    result.error(f"Device setting '{setting_id}' must be a dict")
                    continue
                if "label" not in setting_def:
                    result.error(f"Device setting '{setting_id}' missing 'label'")
                if "write" not in setting_def:
                    result.warn(f"Device setting '{setting_id}' has no 'write' definition -- it won't be writable")

    # Frame parser
    if "frame_parser" in data:
        fp = data["frame_parser"]
        if isinstance(fp, dict):
            if "type" not in fp:
                result.error("frame_parser missing 'type' field")
            elif fp["type"] not in VALID_FRAME_PARSER_TYPES:
                result.error(f"frame_parser type '{fp['type']}' invalid: must be one of {sorted(VALID_FRAME_PARSER_TYPES)}")
            if fp.get("type") == "length_prefix":
                hs = fp.get("header_size", 2)
                if hs not in (1, 2, 4):
                    result.error(f"frame_parser.header_size must be 1, 2, or 4 (got {hs})")

    # Config schema
    if "config_schema" in data:
        if not isinstance(data["config_schema"], dict):
            result.error("config_schema must be a dict")
        else:
            for field_id, field_def in data["config_schema"].items():
                if isinstance(field_def, dict) and "type" in field_def:
                    if field_def["type"] not in VALID_CONFIG_TYPES:
                        result.error(f"Config field '{field_id}' has invalid type '{field_def['type']}'")

    # Delimiter
    if "delimiter" in data:
        delim = data["delimiter"]
        if not isinstance(delim, str):
            result.error(f"delimiter must be a string (got {type(delim).__name__})")

    # Discovery
    if "discovery" in data:
        disc = data["discovery"]
        if isinstance(disc, dict):
            if "ports" in disc and not isinstance(disc["ports"], list):
                result.error("discovery.ports must be a list")
            if "mac_prefixes" in disc and not isinstance(disc["mac_prefixes"], list):
                result.error("discovery.mac_prefixes must be a list")
            if "hostname_patterns" in disc:
                if isinstance(disc["hostname_patterns"], list):
                    for pat in disc["hostname_patterns"]:
                        valid, err = validate_regex_pattern(str(pat))
                        if not valid:
                            result.error(f"discovery.hostname_patterns '{pat}' is invalid regex: {err}")

    return result


def validate_python_driver(file_path, result):
    """Basic validation of a Python driver file."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        result.error(f"Cannot read file: {e}")
        return result

    # Check for DRIVER_INFO
    if "DRIVER_INFO" not in content:
        result.error("Missing DRIVER_INFO class attribute")

    # Check for BaseDriver import
    if "BaseDriver" not in content:
        result.warn("No reference to BaseDriver -- ensure the class inherits from BaseDriver")

    # Check for send_command
    if "send_command" not in content:
        result.error("Missing send_command method (required override)")

    # Check for blocking calls
    if "time.sleep(" in content:
        result.error("Uses time.sleep() which blocks the event loop. Use asyncio.sleep() instead.")

    return result


def validate_index_json(repo_root, results):
    """Validate index.json and cross-reference with driver files."""
    index_path = repo_root / "index.json"
    if not index_path.exists():
        r = ValidationResult(index_path)
        r.error("index.json not found")
        results.append(r)
        return

    result = ValidationResult(index_path)

    try:
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    except json.JSONDecodeError as e:
        result.error(f"Invalid JSON: {e}")
        results.append(result)
        return

    if "drivers" not in index:
        result.error("Missing 'drivers' array")
        results.append(result)
        return

    seen_ids = set()
    for entry in index["drivers"]:
        driver_id = entry.get("id", "<missing>")

        # Required fields
        for field in ("id", "name", "file", "format", "category", "manufacturer",
                      "version", "author", "transport", "verified", "description"):
            if field not in entry:
                result.error(f"Driver '{driver_id}': missing required field '{field}'")

        # Duplicate IDs
        if driver_id in seen_ids:
            result.error(f"Duplicate driver ID '{driver_id}' in index.json")
        seen_ids.add(driver_id)

        # File exists
        if "file" in entry:
            driver_file = repo_root / entry["file"]
            if not driver_file.exists():
                result.error(f"Driver '{driver_id}': file '{entry['file']}' does not exist")

        # Format matches extension
        if "file" in entry and "format" in entry:
            ext = Path(entry["file"]).suffix
            if entry["format"] == "avcdriver" and ext != ".avcdriver":
                result.error(f"Driver '{driver_id}': format is 'avcdriver' but file extension is '{ext}'")
            elif entry["format"] == "python" and ext != ".py":
                result.error(f"Driver '{driver_id}': format is 'python' but file extension is '{ext}'")

        # Valid category
        if "category" in entry and entry["category"] not in VALID_CATEGORIES:
            result.error(f"Driver '{driver_id}': invalid category '{entry['category']}'")

        # Valid transport
        if "transport" in entry and entry["transport"] not in VALID_TRANSPORTS:
            result.error(f"Driver '{driver_id}': invalid transport '{entry['transport']}'")

        # Cross-reference with driver file for YAML drivers
        if entry.get("format") == "avcdriver" and "file" in entry:
            driver_file = repo_root / entry["file"]
            if driver_file.exists():
                try:
                    with open(driver_file, encoding="utf-8") as f:
                        driver_data = yaml.safe_load(f)
                    if driver_data:
                        # Check key fields match
                        for field in ("id", "name", "transport", "category"):
                            if field in driver_data and field in entry:
                                if str(driver_data[field]) != str(entry[field]):
                                    result.error(
                                        f"Driver '{driver_id}': index.json {field}='{entry[field]}' "
                                        f"doesn't match driver file {field}='{driver_data[field]}'"
                                    )
                except Exception:
                    pass  # Driver file validation will catch parse errors

    # Check for driver files not in index
    for dir_name in DRIVER_DIRS:
        dir_path = repo_root / dir_name
        if not dir_path.exists():
            continue
        for f in dir_path.iterdir():
            if f.suffix in (".avcdriver", ".py") and not f.name.startswith("_") and not f.name.endswith("_sim.py"):
                file_rel = f"{dir_name}/{f.name}"
                if not any(e.get("file") == file_rel for e in index["drivers"]):
                    result.warn(f"Driver file '{file_rel}' exists but has no index.json entry")

    results.append(result)


def find_driver_files(repo_root, targets=None):
    """Find driver files to validate."""
    files = []
    if targets:
        for target in targets:
            path = Path(target)
            if not path.is_absolute():
                path = repo_root / path
            if path.exists():
                files.append(path)
            else:
                print(f"WARNING: File not found: {target}")
    else:
        for dir_name in DRIVER_DIRS:
            dir_path = repo_root / dir_name
            if not dir_path.exists():
                continue
            for f in sorted(dir_path.iterdir()):
                if f.suffix == ".avcdriver":
                    files.append(f)
                elif f.suffix == ".py" and not f.name.startswith("_") and not f.name.endswith("_sim.py"):
                    files.append(f)
    return files


def main():
    parser = argparse.ArgumentParser(description="Validate OpenAVC driver files")
    parser.add_argument("files", nargs="*", help="Specific driver files to validate (default: all)")
    parser.add_argument("--check-index", action="store_true", help="Also validate index.json consistency")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show passing checks")
    args = parser.parse_args()

    repo_root = Path(__file__).parent

    files = find_driver_files(repo_root, args.files if args.files else None)
    results = []

    for file_path in files:
        result = ValidationResult(file_path)

        if file_path.suffix == ".avcdriver":
            try:
                with open(file_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    result.error("File does not contain a valid YAML mapping")
                else:
                    validate_yaml_driver(file_path, data, result)
            except yaml.YAMLError as e:
                result.error(f"YAML parse error: {e}")
        elif file_path.suffix == ".py":
            validate_python_driver(file_path, result)

        results.append(result)

    if args.check_index:
        validate_index_json(repo_root, results)

    # Print results
    total_errors = 0
    total_warnings = 0
    total_files = len(results)
    passed_files = 0

    for result in results:
        rel_path = result.file_path
        try:
            rel_path = result.file_path.relative_to(repo_root)
        except ValueError:
            pass

        if result.passed:
            passed_files += 1
            if args.verbose:
                print(f"  PASS  {rel_path}")
        else:
            print(f"  FAIL  {rel_path}")
            for err in result.errors:
                print(f"        ERROR: {err}")
                total_errors += 1

        for warn in result.warnings:
            if result.passed and not args.verbose:
                # Print file header for warnings on passing files
                print(f"  WARN  {rel_path}")
            print(f"        WARNING: {warn}")
            total_warnings += 1

    # Summary
    print()
    print(f"Validated {total_files} file(s): {passed_files} passed, {total_files - passed_files} failed")
    if total_errors:
        print(f"  {total_errors} error(s)")
    if total_warnings:
        print(f"  {total_warnings} warning(s)")

    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
