"""Apple TV simulator with a PIN-paired remote and independently held TV identity."""

import asyncio
import hmac
import importlib.util
import secrets
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from openavc.simulator.tcp_simulator import TCPSimulator

# Catalog Python drivers are individually loaded files, not importable packages.
_schema = importlib.util.spec_from_file_location("apple_tv_wire", Path(__file__).with_name("apple_tv.py"))
protocol = importlib.util.module_from_spec(_schema)
_schema.loader.exec_module(protocol)

APPS = {"com.apple.TVWatchList": "Apple TV", "com.apple.TVMusic": "Music", "com.apple.TVPhotos": "Photos"}


class AppleTVSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "apple_tv",
        "name": "Apple TV",
        "category": "streaming",
        "transport": "tcp",
        "default_port": 0,
        "initial_state": {
            "attention": "Awake",
            "active_app": "Home",
            "last_button": "",
            "playing": False,
            "last_skip_seconds": 0,
        },
        "controls": [
            {
                "key": "attention",
                "type": "select",
                "label": "Apple TV activity",
                "options": ["Asleep", "Screensaver", "Awake", "Idle"],
            }
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.identity = str(uuid.uuid4()).encode()
        self.signing = Ed25519PrivateKey.generate()
        self.controllers = {}
        self.subscribers = set()

    def handle_command(self, data):
        """Encrypted frames need connection-local keys and nonce counters."""
        return None

    async def _auth_reply(self, wire, kind, seq, record):
        await wire.send(kind, {"_pd": protocol.tlv({6: bytes([seq]), **record})})

    async def _next(self, wire, kind, seq):
        next_kind, message = await asyncio.wait_for(wire.receive(), 130)
        rec = protocol.fields(message["_pd"])
        if next_kind != kind or rec[6] != bytes([seq]):
            raise ValueError("Unexpected pairing step")
        return rec

    async def _enroll(self, wire):
        salt = secrets.token_bytes(16)
        x = int.from_bytes(protocol.digest(salt, protocol.digest(b"Pair-Setup:1234")), "big")
        g, n = 5, protocol.GROUP
        verifier = pow(g, x, n)
        k = int.from_bytes(protocol.digest(protocol.octets(n), protocol.octets(g, 384)), "big")
        exponent = secrets.randbits(384)
        public = (k * verifier + pow(g, exponent, n)) % n
        await self._auth_reply(wire, 4, 2, {2: salt, 3: protocol.octets(public, 384)})
        client = await self._next(wire, 4, 3)
        a = int.from_bytes(client[3], "big")
        if not 0 < a < n:
            raise ValueError("Invalid controller key")
        u = int.from_bytes(protocol.digest(protocol.octets(a, 384), protocol.octets(public, 384)), "big")
        shared = pow(a * pow(verifier, u, n), exponent, n)
        key = protocol.digest(protocol.octets(shared, 384))
        fingerprint = bytes(
            i ^ j for i, j in zip(protocol.digest(protocol.octets(n)), protocol.digest(bytes([g])))
        )
        proof = protocol.digest(
            fingerprint, protocol.digest(b"Pair-Setup"), salt, client[3], protocol.octets(public, 384), key
        )
        if not hmac.compare_digest(proof, client[4]):
            await self._auth_reply(wire, 4, 4, {7: b"\x02"})
            return
        await self._auth_reply(wire, 4, 4, {4: protocol.digest(client[3], proof, key)})
        record = await self._next(wire, 4, 5)
        cipher = protocol.pairing_cipher(key, "Setup")
        controller = protocol.fields(cipher.decrypt(b"\0\0\0\0PS-Msg05", record[5], None))
        Ed25519PublicKey.from_public_bytes(controller[3]).verify(
            controller[10], protocol.signature_prefix(key, "Controller") + controller[1] + controller[3]
        )
        public_key = self.signing.public_key().public_bytes_raw()
        signature = self.signing.sign(
            protocol.signature_prefix(key, "Accessory") + self.identity + public_key
        )
        identity = protocol.tlv({1: self.identity, 3: public_key, 10: signature})
        self.controllers[controller[1]] = controller[3]
        await self._auth_reply(wire, 4, 6, {5: cipher.encrypt(b"\0\0\0\0PS-Msg06", identity, None)})

    async def _authenticate(self, wire, client_public):
        ephemeral = X25519PrivateKey.generate()
        ours = ephemeral.public_key().public_bytes_raw()
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(client_public))
        cipher = protocol.pairing_cipher(shared, "Verify")
        identity = protocol.tlv(
            {1: self.identity, 10: self.signing.sign(ours + self.identity + client_public)}
        )
        await self._auth_reply(wire, 6, 2, {3: ours, 5: cipher.encrypt(b"\0\0\0\0PV-Msg02", identity, None)})
        record = await self._next(wire, 6, 3)
        client = protocol.fields(cipher.decrypt(b"\0\0\0\0PV-Msg03", record[5], None))
        try:
            Ed25519PublicKey.from_public_bytes(self.controllers[client[1]]).verify(
                client[10], client_public + client[1] + ours
            )
        except (KeyError, InvalidSignature):
            await self._auth_reply(wire, 6, 4, {7: b"\x02"})
            return False
        await self._auth_reply(wire, 6, 4, {})
        wire.encrypt(shared, server=True)
        return True

    def set_state(self, key, value):
        super().set_state(key, value)
        if key == "attention":
            for wire in tuple(self.subscribers):
                asyncio.create_task(self._announce(wire))

    async def _announce(self, wire):
        if self._silenced:
            return
        try:
            await wire.message("SystemStatus", {"state": self._attention()}, event=True)
        except (OSError, EOFError):
            self.subscribers.discard(wire)

    def _attention(self):
        return next((n for n, name in protocol.ATTENTION.items() if name == self.get_state("attention")), 0)

    def _button(self, body):
        if body["_hBtS"] == 2:
            button = next(k for k, code in protocol.KEYS.items() if code == body["_hidC"])
            self.set_state("last_button", button)
            if button in ("sleep", "wake", "screensaver"):
                self.set_state(
                    "attention", {"sleep": "Asleep", "wake": "Awake", "screensaver": "Screensaver"}[button]
                )
            elif button == "home":
                self.set_state("active_app", "Home")
            elif button == "play_pause":
                self.set_state("playing", not self.get_state("playing"))

    def _media(self, body):
        if body["_mcc"] == 7:
            self.set_state("last_skip_seconds", body["_skpS"])
        if body["_mcc"] in (1, 2):
            self.set_state("playing", body["_mcc"] == 1)

    def _command(self, name, body):
        queries = {
            "_systemInfo": {},
            "TVRCSessionStart": {},
            "_sessionStop": {},
            "_sessionStart": {"_sid": secrets.randbits(32)},
            "FetchLaunchableApplicationsEvent": APPS,
            "FetchAttentionState": {"state": self._attention()},
        }
        if name in queries:
            return queries[name]
        if name == "_launchApp":
            self.set_state("active_app", APPS[body["_bundleID"]])
        elif name == "_hidC":
            self._button(body)
        elif name == "_mcc":
            self._media(body)
        else:
            raise ValueError("Unsupported command")
        return {}

    async def _serve_remote(self, wire):
        while True:
            kind, msg = await wire.receive()
            if kind != 8:
                return
            await self._reply(wire, msg)

    async def _reply(self, wire, msg):
        if self._silenced:
            return
        name, body = msg["_i"], msg["_c"]
        if name == "_interest":
            if "SystemStatus" in body.get("_regEvents", []):
                self.subscribers.add(wire)
            return
        response = {"_t": 3, "_x": msg["_x"]}
        try:
            response["_c"] = self._command(name, body)
        except (KeyError, ValueError, StopIteration):
            response["_em"] = "Apple TV simulator declined the command."
        await wire.send(8, response)

    async def _handle_client(self, reader, writer):
        conn_id = uuid.uuid4().hex
        wire = protocol.Wire(
            reader,
            writer,
            lambda direction, data: self.log_protocol("in" if direction == "rx" else "out", data, conn_id),
        )
        self._clients[conn_id] = writer
        try:
            kind, message = await asyncio.wait_for(wire.receive(), 10)
            data = protocol.fields(message["_pd"])
            if data[6] != b"\x01":
                return
            if kind == 3:
                await self._enroll(wire)
                return
            if kind != 5 or not await self._authenticate(wire, data[3]):
                return
            await self._serve_remote(wire)
        except (OSError, EOFError, KeyError, ValueError, InvalidSignature, InvalidTag):
            pass
        finally:
            self._clients.pop(conn_id, None)
            self.subscribers.discard(wire)
            await wire.close()
