"""How scripts/vendor_platform_contract.py reads the platform's files.

The sync check compares this repo's vendored copies against the platform
repo's current ones, fetched over HTTP. It used to fetch them from the
mutable ``main`` URL, which looked fine and was quietly wrong: that host
serves through a CDN with a cache measured in minutes, so a run starting
shortly after the platform half of a paired change landed compared the new
vendored copies against the *old* platform bytes and failed with drift that
did not exist. Paired changes land minutes apart by nature, so that window
is the normal case.

The fix is to resolve the branch to a commit SHA once and fetch every file
at that SHA, since a SHA's URL cannot go stale. These tests pin the three
things that has to keep doing, without touching the network.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import vendor_platform_contract as vendor  # noqa: E402


def _fake_ref_response(sha: str):
    body = json.dumps({"object": {"sha": sha, "type": "commit"}}).encode()
    return io.BytesIO(body)


def test_the_branch_is_resolved_to_a_commit_sha(monkeypatch):
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(
        vendor.urllib.request, "urlopen", lambda *a, **k: _fake_ref_response(sha)
    )
    vendor._upstream_ref.cache_clear()
    try:
        assert vendor._upstream_ref() == sha
    finally:
        vendor._upstream_ref.cache_clear()


def test_an_unreachable_api_falls_back_to_the_branch(monkeypatch):
    """Degrade, never crash: without the network this has to keep working the
    way it always did, just without the staleness guarantee."""
    def _boom(*args, **kwargs):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(vendor.urllib.request, "urlopen", _boom)
    vendor._upstream_ref.cache_clear()
    try:
        assert vendor._upstream_ref() == vendor.UPSTREAM_BRANCH
    finally:
        vendor._upstream_ref.cache_clear()


def test_files_are_fetched_at_the_resolved_ref(monkeypatch):
    """The whole point: the file URL carries the SHA, not the branch name."""
    sha = "89abcdef0123456789abcdef0123456789abcdef"
    seen: list[str] = []

    def _capture(url, *args, **kwargs):
        seen.append(url)
        return io.BytesIO(b"contents")

    monkeypatch.setattr(vendor, "_upstream_ref", lambda: sha)
    monkeypatch.setattr(vendor.urllib.request, "urlopen", _capture)

    assert vendor._fetch_upstream("server/drivers/spec.py") == b"contents"
    assert seen == [f"{vendor.RAW_BASE}{sha}/server/drivers/spec.py"]
    assert vendor.UPSTREAM_BRANCH not in seen[0].rsplit("/", 3)[0]
