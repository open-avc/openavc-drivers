"""Driver-side dispatch / child-registry tests for darwin_control.

The byte-exact banner parsers are covered in test_darwin_control_sim.py; this
file covers the defect-prone dispatch logic that had no test: send_command wire
formatting + child-id coercion, _send_set's raise-on-[ERROR], the
create/delete/renumber registry mutations (incl. label carry on renumber), the
write-through caching of set-only encoding params, roster reconcile add/drop,
and the reset confirm flow. A dict-backed fake BaseDriver stands in for the
platform registry so no openavc install is needed. Mirrors
test_chazy_control_pro_dispatch.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

import pytest

from _lifecycle_fake import LifecycleFake
from _platform_stubs import (
    StubState as _FakeState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PATH = REPO_ROOT / "switchers" / "darwin_control.py"


class _FakeBaseDriver(LifecycleFake):
    """Minimal functional stand-in for the platform BaseDriver child API."""

    DRIVER_INFO: dict = {}

    def __init__(self, device_id, config, state, events) -> None:
        self.device_id = device_id
        self.config = config
        self.state = state
        self.events = events
        self._children: dict[str, dict[int, dict]] = {}
        self._connected = False

    def _eff_schema(self, ctype: str) -> dict:
        schema = dict(self.DRIVER_INFO["child_entity_types"][ctype]["state_variables"])
        schema.setdefault("online", {"type": "boolean"})
        schema.setdefault("label", {"type": "string"})
        return schema

    def get_child_entity_types(self) -> dict:
        out = {}
        for ct, d in self.DRIVER_INFO.get("child_entity_types", {}).items():
            md = dict(d)
            md["state_variables"] = self._eff_schema(ct)
            out[ct] = md
        return out

    def register_child(self, ctype, lid, initial_state=None) -> None:
        bucket = self._children.setdefault(ctype, {})
        if lid in bucket:
            return
        schema = self._eff_schema(ctype)
        ov = dict(initial_state or {})
        for prop in ov:
            if prop not in schema:
                raise ValueError(f"unknown child prop {prop!r}")
        st = {}
        for prop in schema:
            if prop == "online":
                st[prop] = ov.get("online", True)
            elif prop == "label":
                st[prop] = ov.get("label", "")
            else:
                st[prop] = ov.get(prop)
        bucket[lid] = st

    def deregister_child(self, ctype, lid) -> None:
        self._children.get(ctype, {}).pop(lid, None)

    def is_child_registered(self, ctype, lid) -> bool:
        return lid in self._children.get(ctype, {})

    def list_children(self, ctype) -> list:
        return list(self._children.get(ctype, {}).keys())

    def get_child_state(self, ctype, lid) -> dict:
        return dict(self._children.get(ctype, {}).get(lid, {}))

    def set_child_state(self, ctype, lid, prop, value) -> None:
        self._children[ctype][lid][prop] = value

    def set_child_state_batch(self, ctype, lid, updates) -> None:
        schema = self._eff_schema(ctype)
        for prop in updates:
            if prop not in schema:
                raise ValueError(f"unknown child prop {prop!r}")
        self._children[ctype][lid].update(updates)

    def set_state(self, key, value) -> None:
        self.state.set(key, value)

    def set_states(self, mapping) -> None:
        for k, v in mapping.items():
            self.state.set(k, v)

    def get_state(self, key, default=None):
        return self.state.data.get(key, default)


def _load_driver() -> ModuleType:
    server = ModuleType("openavc")
    server.__path__ = []  # type: ignore[attr-defined]
    sys.modules["openavc"] = server
    for sub in ("drivers", "transport", "utils"):
        m = ModuleType(f"openavc.{sub}")
        m.__path__ = []  # type: ignore[attr-defined]
        sys.modules[f"openavc.{sub}"] = m
    base = ModuleType("openavc.drivers.base")
    base.BaseDriver = _FakeBaseDriver
    sys.modules["openavc.drivers.base"] = base
    tcp = ModuleType("openavc.transport.tcp")
    tcp.TCPTransport = object
    sys.modules["openavc.transport.tcp"] = tcp
    logger = ModuleType("openavc.utils.logger")
    logger.get_logger = lambda name="x": logging.getLogger(name)
    sys.modules["openavc.utils.logger"] = logger
    spec = importlib.util.spec_from_file_location("darwin_dispatch_under_test", DRIVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["darwin_dispatch_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


drv = _load_driver()


def make_driver():
    return drv.DarwinControlDriver("dev1", {"host": "x"}, _FakeState(), None)


# --- send_command wire formatting + child-id coercion ----------------------


def test_send_command_formats_wire_and_coerces_child_id():
    d = make_driver()
    sent = []

    async def fake_send_set(wire, timeout=6.0):
        sent.append(wire)
        return "[SUCCESS]OK."

    d._send_set = fake_send_set
    asyncio.run(d.send_command(
        "dec_switch", {"decoder_id": "001", "encoder_id": 2, "signal": "VIDEO"}
    ))
    assert sent == ["SET DEC 1 SWITCH 2 VIDEO"]


def test_send_set_raises_on_error_line():
    d = make_driver()

    async def fake_send_request(wire, timeout=6.0):
        return "[ERROR]Decoder 001 not assign to video wall."

    d._send_request = fake_send_request
    with pytest.raises(RuntimeError, match="not assign to video wall"):
        asyncio.run(d._send_set("SET DEC 1 MODE VW"))


def test_send_set_passes_success_through():
    d = make_driver()

    async def fake_send_request(wire, timeout=6.0):
        return "[SUCCESS]Set decoder 001 on."

    d._send_request = fake_send_request
    assert "decoder 001 on" in asyncio.run(d._send_set("SET DEC 1 OUTPUT ON"))


# --- lifecycle registry mutation -------------------------------------------


def test_lifecycle_create_and_delete_wall():
    d = make_driver()

    async def ok(wire, timeout=6.0):
        return "[SUCCESS]OK."

    d._send_set = ok
    asyncio.run(d.send_command("wall_create", {"wall_id": 5}))
    assert d.is_child_registered("video_wall", 5)
    asyncio.run(d.send_command("wall_delete", {"wall_id": 5}))
    assert not d.is_child_registered("video_wall", 5)


def test_renumber_carries_label_and_state():
    d = make_driver()

    async def ok(wire, timeout=6.0):
        return "[SUCCESS]OK."

    d._send_set = ok
    d.register_child("encoder", 1, initial_state={
        "name": "Stage TX", "ip": "169.254.10.1", "label": "Custom Label",
    })
    asyncio.run(d.send_command("enc_set_id", {"encoder_id": 1, "new_id": 2}))
    assert not d.is_child_registered("encoder", 1)
    moved = d.get_child_state("encoder", 2)
    assert moved["name"] == "Stage TX"
    assert moved["ip"] == "169.254.10.1"
    assert moved["label"] == "Custom Label"  # user label survives the renumber


# --- write-through caching of set-only encoding params ---------------------


def test_writethrough_caches_encoding_params():
    d = make_driver()

    async def ok(wire, timeout=6.0):
        return "[SUCCESS]OK."

    d._send_set = ok
    # Write-through to an unregistered encoder is a safe no-op (no crash).
    asyncio.run(d.send_command("enc_stream_bitrate", {"encoder_id": 1, "rate": "2"}))
    d.register_child("encoder", 1)
    asyncio.run(d.send_command("enc_stream_bitrate", {"encoder_id": 1, "rate": "2"}))
    assert d.get_child_state("encoder", 1)["bitrate"] == "8Mb"  # index 2 -> label
    asyncio.run(d.send_command(
        "enc_mainstream", {"encoder_id": 1, "type": "1", "audio": "ON"}
    ))
    e = d.get_child_state("encoder", 1)
    assert e["mainstream_codec"] == "h265"  # codec index 1 -> h265
    assert e["mainstream_audio"] is True


# --- roster reconcile add / update / drop ----------------------------------


def test_reconcile_roster_add_update_drop():
    d = make_driver()
    d._reconcile_children("encoder", {
        1: {"net": True, "name": "A"},
        2: {"net": False, "name": "B"},
    }, online_from_net=True)
    assert sorted(d.list_children("encoder")) == [1, 2]
    assert d.get_child_state("encoder", 1)["online"] is True
    assert d.get_child_state("encoder", 2)["online"] is False
    # Next roster drops 1, adds 3, keeps 2 (mirrors the live TX-enrols-as-id-2 case).
    d._reconcile_children("encoder", {
        2: {"net": True, "name": "B"},
        3: {"net": True, "name": "C"},
    }, online_from_net=True)
    assert sorted(d.list_children("encoder")) == [2, 3]


def test_reconcile_config_children_forced_online():
    d = make_driver()
    d._reconcile_children("video_wall", {7: {"name": "Lobby Wall"}},
                          online_from_net=False)
    assert d.get_child_state("video_wall", 7)["online"] is True


# --- reset confirm flow ----------------------------------------------------


def test_reset_confirm_sends_yes():
    d = make_driver()
    sent = []

    async def fake_send_request(wire, timeout=6.0):
        return ('Sure to RESET system to default settings? '
                'Type "Yes" after next prompt to confirm...')

    async def fake_send_set(wire, timeout=6.0):
        sent.append(wire)
        return "[SUCCESS]System reset to default settings."

    d._send_request = fake_send_request
    d._send_set = fake_send_set
    asyncio.run(d.send_command("reset_system_confirm", {}))
    assert sent == ["Yes"]
