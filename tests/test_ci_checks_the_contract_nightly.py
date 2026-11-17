"""When CI compares the vendored platform contract against the platform.

scripts/vendor_platform_contract.py --check fetches the platform's current
files and fails when the copies in scripts/_vendor/ differ. Run only on this
repo's own push or pull request, it finds a platform contract change that
nobody vendored on the next contributor's driver change, which then goes red
for a reason the contributor did not cause. A nightly run finds it on this
repo's own schedule instead, the morning after the platform moved.

These tests read .github/workflows/ci.yml and pin that the check runs on that
schedule: the workflow has one, and nothing gates the check off it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CHECK = "vendor_platform_contract.py --check"


def _workflow() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # YAML 1.1 reads a bare `on` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "ci.yml's `on:` block is not a mapping"
    return triggers


def _jobs_running_the_check(workflow: dict) -> list[tuple[str, dict, dict]]:
    found = []
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            if CHECK in str(step.get("run", "")):
                found.append((job_id, job, step))
    return found


def test_ci_runs_on_a_nightly_schedule() -> None:
    schedule = _triggers(_workflow()).get("schedule")
    assert schedule, "ci.yml has no `schedule:` trigger"
    assert all(entry.get("cron") for entry in schedule), (
        "every `schedule:` entry needs a cron line"
    )


def test_ci_can_be_started_by_hand() -> None:
    """A maintainer who vendored a fix re-runs the check without a push."""
    assert "workflow_dispatch" in _triggers(_workflow())


def test_the_contract_check_runs_on_every_trigger() -> None:
    found = _jobs_running_the_check(_workflow())
    assert found, f"no step in ci.yml runs {CHECK!r}"
    for job_id, job, step in found:
        assert "if" not in job, (
            f"job {job_id!r} runs the contract check behind an `if:`, so the "
            "nightly run may skip it"
        )
        assert "if" not in step, (
            f"the contract check in job {job_id!r} has an `if:`, so the "
            "nightly run may skip it"
        )
