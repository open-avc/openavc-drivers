"""Every simulator that asks for a password answers the way a driver needs it to.

Two things a driver meets every time it signs in to its own simulator, checked
over a real loopback socket for each Python simulator that prompts for a
password in its default configuration:

* **A blank password gets an answer.** In simulation an integrator usually
  leaves the password empty, so the driver answers the prompt with an empty
  line. The TCP simulator base drops empty lines before ``handle_command``
  sees them, so a sign-in written there never hears that answer: the driver
  waits out its timeout, reports that the device said nothing, and the device
  stays offline for as long as simulation runs. A sign-in written in
  ``authenticate_client`` reads the socket itself and sees the empty line.
  Signing in or refusing are both answers; silence is not.
* **A second connection signs in like the first.** A driver reconnects after
  a drop, a reboot command or a network fault, to the same simulator. Sign-in
  state kept for the simulator as a whole instead of for the connection
  carries over, and the second connection is answered as if already signed in.

Like the connect/disconnect run, this needs the real platform and skips
without it (fails when ``OPENAVC_REQUIRE_PLATFORM`` promised one).
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_PASSWORD_PROMPT = re.compile(rb"password\s*:\s*$", re.I)
_LOGIN_PROMPT = re.compile(rb"(login|user(name)?)\s*:\s*$", re.I)
# Long enough for any simulator's own pacing, far shorter than a driver's
# sign-in timeout, so "nothing within this" is the silence a driver would hit.
_ANSWER_WINDOW = 3.0


def _discover() -> list[Path]:
    """Every simulator whose source carries a password prompt."""
    out: list[Path] = []
    for sim_path in sorted(REPO_ROOT.rglob("*_sim.py")):
        if "tests" in sim_path.parts or "_vendor" in sim_path.parts:
            continue
        if re.search(rb"[Pp]assword\s*:", sim_path.read_bytes()):
            out.append(sim_path)
    return out


_CASES = _discover()

try:
    import openavc  # noqa: F401  (the real platform)
except ModuleNotFoundError:
    from _platform_probe import (  # noqa: E402
        REQUIRE_PLATFORM_ENV,
        platform_required,
    )

    if platform_required():
        raise AssertionError(
            f"{REQUIRE_PLATFORM_ENV}=1 promised the openavc platform, but it is "
            f"not importable, so ALL {len(_CASES)} simulator sign-in check(s) "
            "would have been skipped."
        )
    pytest.skip(
        f"simulator sign-in checks SKIPPED ALL {len(_CASES)} simulator(s): "
        "requires the openavc platform (run from the workspace with openavc "
        f"installed). Set {REQUIRE_PLATFORM_ENV}=1 to make its absence a failure.",
        allow_module_level=True,
    )


def _tcp_simulator_class(sim_path: Path):
    """The TCP simulator class the file defines, or None for another kind.

    By name along the class's bases rather than ``issubclass``, for the reason
    the connect/disconnect run gives: the platform modules can be re-imported
    between collection and the run, so a base class captured here may not be
    the one the simulator subclasses."""
    name = f"_signin_{sim_path.stem}"
    spec = importlib.util.spec_from_file_location(name, sim_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    for value in vars(module).values():
        if (
            isinstance(value, type)
            and value.__module__ == module.__name__
            and getattr(value, "SIMULATOR_INFO", None)
            and any(base.__name__ == "TCPSimulator" for base in value.__mro__)
        ):
            return value
    return None


async def _read_until(reader, pattern, timeout: float) -> bytes:
    """What arrives until ``pattern`` matches its end, the peer closes, or
    ``timeout`` passes."""
    out = b""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        left = deadline - loop.time()
        if left <= 0:
            return out
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=left)
        except asyncio.TimeoutError:
            return out
        if not chunk:
            return out + b"<closed>"
        out += chunk
        if pattern is not None and pattern.search(out.rstrip(b" ")):
            return out


async def _sign_in(port: int, password: bytes) -> tuple[bool, bytes]:
    """Walk one connection's sign-in. Returns (asked for a password, what the
    simulator sent after the password, or ``<closed>`` when it hung up)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        either = re.compile(_LOGIN_PROMPT.pattern + rb"|" + _PASSWORD_PROMPT.pattern, re.I)
        text = await _read_until(reader, either, 1.5)
        if _LOGIN_PROMPT.search(text.rstrip(b" ")):
            writer.write(b"admin\r\n")
            await writer.drain()
            text = await _read_until(reader, _PASSWORD_PROMPT, 1.5)
        if not _PASSWORD_PROMPT.search(text.rstrip(b" ")):
            return False, text
        writer.write(password + b"\r\n")
        await writer.drain()
        return True, await _read_until(reader, None, _ANSWER_WINDOW)
    finally:
        writer.close()


async def _with_simulator(sim_cls, check):
    sim = sim_cls("sign_in_check", {})
    await sim.start(0)
    try:
        return await check(sim.port)
    finally:
        await sim.stop()


@pytest.mark.parametrize("sim_path", _CASES, ids=[p.stem for p in _CASES])
def test_a_blank_password_gets_an_answer(sim_path):
    sim_cls = _tcp_simulator_class(sim_path)
    if sim_cls is None:
        pytest.skip("not a TCP simulator")
    asked, reply = asyncio.run(_with_simulator(sim_cls, lambda port: _sign_in(port, b"")))
    if not asked:
        pytest.skip("asks for no password in its default configuration")
    assert reply, (
        f"{sim_path.name} said nothing to a blank password for "
        f"{_ANSWER_WINDOW:.0f} s; a driver signing in with no password set waits "
        "out its timeout and the device stays offline. Read the credentials in "
        "authenticate_client, which sees an empty line."
    )


@pytest.mark.parametrize("sim_path", _CASES, ids=[p.stem for p in _CASES])
def test_a_second_connection_signs_in_like_the_first(sim_path):
    sim_cls = _tcp_simulator_class(sim_path)
    if sim_cls is None:
        pytest.skip("not a TCP simulator")

    async def twice(port):
        first = await _sign_in(port, b"smokepw")
        await asyncio.sleep(0.2)
        second = await _sign_in(port, b"smokepw")
        return first, second

    (asked, first), (asked_again, second) = asyncio.run(_with_simulator(sim_cls, twice))
    if not asked:
        pytest.skip("asks for no password in its default configuration")
    assert asked_again and second == first, (
        f"{sim_path.name} signed in the first connection ({first[-40:]!r}) but "
        f"answered the second differently ({second[-40:]!r}); keep sign-in state "
        "per connection, so a driver that reconnects signs in again."
    )
