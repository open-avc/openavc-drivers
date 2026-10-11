"""No driver sentence opens with "Connected, but".

A no-response fault's sentence (``HEALTH_FAULT_MESSAGE``, the message given
to ``_force_disconnect`` or a typed ``ConnectionFaultError``) is shown under
the device card's **Offline** heading, where "Connected, but the device
stopped answering" contradicts the heading above it. The sentence says what
the device stopped doing instead: "The projector stopped answering status
queries."
"""

from __future__ import annotations

import ast
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


# ── A refused login says what was refused, never where to fix it ──
#
# A device that refuses its credentials is marked offline with auth_failed, and
# the device page's banner under it gives the next step itself (edit the
# credentials, then Reconnect). A driver sentence that also says where to fix
# them ("in this device's settings", "under Edit Device", "press Reconnect")
# puts two instructions on one card. Device-side facts stay ("set in the
# Nureva App"): the banner cannot say those.
#
# "Device settings" is also a section of the device page that help text points
# at, and Edit Device is the right pointer in a command's own error (a wake
# with no MAC address), so this reads only what a driver says when it raises
# auth_failed: every string in a function that raises it, the module constants
# that function names, and the messages of the same class's methods it calls.

WHERE_TO_FIX = re.compile(
    r"press Reconnect|Edit Device|device'?s? settings|in the device settings"
    r"|check (the|them|your) (admin |nvx )?(username|password|credentials|login)",
    re.IGNORECASE,
)

# A community contribution, left as its author wrote it.
CONTRIBUTED = {"audio/soundcorehero.py"}


def _strings(node: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _raises_auth_failed(func: ast.AST) -> bool:
    for call in ast.walk(func):
        if not isinstance(call, ast.Call):
            continue
        name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
        if name == "ConnectionFaultError" and any(
            kw.arg == "code" and isinstance(kw.value, ast.Constant)
            and kw.value.value == "auth_failed" for kw in call.keywords
        ):
            return True
        if name in ("_force_disconnect", "_stash_fault") and call.args and (
            isinstance(call.args[0], ast.Constant) and call.args[0].value == "auth_failed"
        ):
            return True
    return False


def _auth_sentences(tree: ast.Module) -> list[str]:
    constants = {
        target.id: node.value
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }
    methods: dict[str, ast.AST] = {}
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods[item.name] = item
    found: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _raises_auth_failed(func):
            continue
        found.extend(_strings(func))
        for ref in ast.walk(func):
            if isinstance(ref, ast.Name) and ref.id in constants:
                found.extend(_strings(constants[ref.id]))
            if (isinstance(ref, ast.Call) and isinstance(ref.func, ast.Attribute)
                    and ref.func.attr in methods):
                found.extend(_strings(methods[ref.func.attr]))
    return found


def test_the_auth_reader_sees_a_refusal() -> None:
    sample = ast.parse(
        "REFUSED = 'The amp refused the password. Enter it under Edit Device.'\n"
        "class D:\n"
        "    def _why(self):\n"
        "        return 'press Reconnect'\n"
        "    async def connect(self):\n"
        "        raise ConnectionFaultError(REFUSED + self._why(), code='auth_failed')\n"
    )
    assert len([s for s in _auth_sentences(sample) if WHERE_TO_FIX.search(s)]) == 2


def test_a_refused_login_says_what_was_refused_not_where_to_fix_it() -> None:
    offenders = []
    for path in _driver_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if path.suffix != ".py" or rel in CONTRIBUTED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(
            f"{rel}: {text.strip()}" for text in _auth_sentences(tree)
            if WHERE_TO_FIX.search(text)
        )
    assert offenders == [], "\n".join(offenders)
