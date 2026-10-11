"""No driver sentence opens with "Connected, but".

A no-response fault's sentence (``HEALTH_FAULT_MESSAGE``, the message given
to ``_force_disconnect`` or a typed ``ConnectionFaultError``) is shown under
the device card's **Offline** heading, where "Connected, but the device
stopped answering" contradicts the heading above it. The sentence says what
the device stopped doing instead: "The projector stopped answering status
queries."
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

CATEGORIES = (
    "projectors", "displays", "switchers", "audio", "cameras", "video",
    "lighting", "power", "streaming", "utility",
)

# A string literal (plain, f-string or raw) whose text opens with the word.
OPENS_WITH_CONNECTED = re.compile(r"""(?<![\w"'])[fFrRbB]{0,2}["']Connected,? but""")


def _driver_files() -> list[Path]:
    files: list[Path] = []
    for category in CATEGORIES:
        files.extend(sorted((REPO_ROOT / category).glob("*.py")))
        files.extend(sorted((REPO_ROOT / category).glob("*.avcdriver")))
    return files


def test_the_corpus_is_found() -> None:
    assert len(_driver_files()) > 50


def test_no_driver_sentence_opens_with_connected() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{number}: {line.strip()}"
        for path in _driver_files()
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1,
        )
        if OPENS_WITH_CONNECTED.search(line)
    ]
    assert offenders == [], "\n".join(offenders)
