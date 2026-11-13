"""Apple TV simulator with a PIN-paired remote and independently held TV identity."""

import asyncio
import hmac
import importlib.util
import secrets
import uuid
from pathlib import Path

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)

from openavc.simulator.tcp_simulator import TCPSimulator

# Catalog Python drivers are individually loaded files, not importable packages.
_schema = importlib.util.spec_from_file_location("apple_tv_wire", Path(__file__).with_name("apple_tv.py"))
protocol = importlib.util.module_from_spec(_schema)
_schema.loader.exec_module(protocol)

APPS = {
    "com.apple.TVWatchList": "Apple TV",
    "com.apple.TVMusic": "Music",
    "com.apple.TVPhotos": "Photos",
}


class InitialByteReader:
    def __init__(self, reader, first):
        self.reader, self.prefix = reader, first

    async def readexactly(self, size):
        prefix, self.prefix = self.prefix, b""
        return prefix + await self.reader.readexactly(size - len(prefix))


class AirPeer:
    """The simulated TV's HTTP pairing carrier, then its encrypted RTSP connection."""

    def __init__(self, stream):
        self.stream = stream
        self.shared = None
        self.headers = {}

    async def recv_request(self):
        line, self.headers, body = await self.stream.headers()
        method, path, _ = line.split(" ", 2)
        return method, path, body

    async def respond(self, body=b"", pairing=False):
        version = "HTTP/1.1" if pairing else "RTSP/1.0"
        await self.stream.http(f"{version} 200 OK", {"CSeq": self.headers.get("cseq", "0")}, body)

    async def receive(self):
        _, path, body = await self.recv_request()
        return (4 if path == "/pair-setup" else 6), {"_pd": body}

    async def send(self, kind, message):
        await self.respond(message["_pd"], pairing=True)

    def encrypt(self, shared, server=False):
        self.shared = shared
        self.stream.keys(
            shared,
            "Control-Salt",
            "Control-Read-Encryption-Key",
            "Control-Write-Encryption-Key",
        )


class SimulatedMedia:
    def __init__(self, tv, control):
        self.tv, self.control = tv, control
        self.listeners = []
        self.peers = set()
        self.tasks = set()
        self.sequence = 1

    async def listen(self, name, salt, outgoing, incoming):
        def connected(reader, writer):
            stream = protocol.AirStream(reader, writer)
            stream.keys(self.control.shared, salt, outgoing, incoming)
            task = asyncio.create_task(self.serve(stream, name))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        listener = await asyncio.start_server(connected, "127.0.0.1", 0)
        self.listeners.append(listener)
        return listener.sockets[0].getsockname()[1]

    async def setup(self, body):
        request = protocol.plistlib.loads(body)
        if "streams" not in request:
            port = await self.listen(
                "events",
                "Events-Salt",
                "Events-Write-Encryption-Key",
                "Events-Read-Encryption-Key",
            )
            return {"eventPort": port}
        if request["streams"][0].get("controlType") == 1:
            return {"streams": [{"streamID": 1, "type": 130}]}
        seed = request["streams"][0]["seed"]
        port = await self.listen(
            "data",
            f"DataStream-Salt{seed}",
            "DataStream-Input-Encryption-Key",
            "DataStream-Output-Encryption-Key",
        )
        return {"streams": [{"type": 130, "dataPort": port}]}

    async def run(self):
        try:
            while True:
                method, path, body = await self.control.recv_request()
                if path == "/command":
                    self.play_url_command(body)
                reply = protocol.plist_bytes(await self.setup(body)) if method == "SETUP" else b""
                await self.control.respond(reply)
        finally:
            for listener in self.listeners:
                listener.close()
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    def play_url_command(self, body):
        command = protocol.plistlib.loads(protocol.plistlib.loads(body)["params"]["data"])
        if command["type"] == "insertPlayQueueItem":
            self.tv.set_state("track", "URL playback")
        if command["type"] == "setRate":
            self.tv.set_state("playing", bool(command["rate"]))

    async def serve(self, stream, name):
        try:
            if name == "events":
                await stream.http("POST /command RTSP/1.0", {"CSeq": "1"})
                await stream.headers()
                await stream.reader.read()
                return
            self.peers.add(stream)
            while True:
                for packet in await stream.read_messages():
                    await self.answer(stream, packet)
        except (OSError, EOFError, ValueError, InvalidTag):
            pass
        finally:
            self.peers.discard(stream)
            stream.writer.close()

    def envelope(self, kind, ext, body, identifier=b""):
        return protocol.proto([(1, kind), (ext, body), (2, identifier)])

    def device(self, identifier=b""):
        fields = [(1, "SIM-APPLE-TV"), (2, "Apple TV"), (19, "SIM-APPLE-TV"), (29, 1)]
        if self.tv.get_state("speaker"):
            fields.append((28, protocol.proto([(2, "Room speaker"), (19, "ROOM-SPEAKER")])))
        return self.envelope(15, 20, protocol.proto(fields), identifier)

    def item_messages(self, identifier=b"", art=False):
        title = self.tv.get_state("track")
        app = protocol.proto([(2, "com.apple.TVMusic"), (7, "Music")]) if title else b""
        path = protocol.proto([(2, app), (3, protocol.proto([(1, "MediaRemote-DefaultPlayer")]))])
        client = self.envelope(46, 50, protocol.proto([(1, app)]))
        player = self.envelope(47, 51, protocol.proto([(1, path)]))
        if not title:
            return [client]
        item = self.item(title, art)
        transport = 1 if self.tv.get_state("playing") else 2
        direct = protocol.proto([(6, self.tv.get_state("repeat", 1)), (7, self.tv.get_state("shuffle", 1))])
        state = protocol.proto(
            [
                (9, path),
                (
                    2,
                    protocol.proto(
                        [(1, protocol.proto([(1, cmd), (2, 1)])) for cmd in protocol.ROOM_CONTROLS]
                    ),
                ),
                (1, direct),
                (3, protocol.proto([(1, 0), (2, item)])),
                (6, transport),
            ]
        )
        return [client, player, self.envelope(4, 9, state, identifier)]

    def item(self, title, include_art):
        metadata = protocol.proto(
            [
                (1, title),
                (6, "Room demo"),
                (7, "OpenAVC"),
                (14, 180.0),
                (19, int(self.tv.get_state("cover"))),
                (35, float(self.tv.get_state("position", 0))),
            ]
        )
        fields = [(1, title), (2, metadata)]
        if include_art and self.tv.get_state("cover"):
            # One-pixel PNG generated for the bench; no external artwork required.
            fields.append(
                (
                    3,
                    bytes.fromhex(
                        "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
                    ),
                )
            )
        return protocol.proto(fields)

    async def publish(self):
        for stream in tuple(self.peers):
            await self.output(stream, self.item_messages())

    async def output(self, stream, packets):
        self.sequence += 1
        await stream.messages(self.sequence, packets)

    def transport(self, request):
        command = protocol.first(request, 1)
        options = protocol.embedded(request, 2)
        if command == 4:
            self.tv.set_state("track", "")
        else:
            key, number = {
                45: ("position", 9),
                46: ("repeat", 10),
                47: ("shuffle", 11),
            }[command]
            self.tv.set_state(key, protocol.first(options, number))

    async def answer(self, stream, packet):
        kind = protocol.first(packet, 1)
        identifier = protocol.first(packet, 2)
        if kind == 15:
            await self.output(stream, [self.device(identifier)])
        elif kind in (16, 32):
            request = protocol.embedded(packet, 37)
            await self.output(stream, self.item_messages(identifier, 4 in request))
        elif kind == 1:
            self.transport(protocol.embedded(packet, 6))
            await self.output(stream, [self.envelope(2, 7, protocol.proto([(1, 0)]), identifier)])
        elif kind == 48:
            self.tv.set_state("speaker", 2 in protocol.embedded(packet, 52))
            await self.output(stream, [self.device(identifier)])


class AppleTVSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "apple_tv",
        "name": "Apple TV",
        "category": "streaming",
        "transport": "tcp",
        "default_port": 0,
        "initial_state": {
            "track": "First song",
            "position": 0,
            "cover": True,
            "speaker": False,
            "volume": 40,
            "focus": True,
            "profile": "Room",
            "attention": "Awake",
            "active_app": "Home",
            "last_button": "",
            "playing": False,
            "last_skip_seconds": 0,
        },
        "controls": [
            {
                "key": "track",
                "label": "Now Playing item",
                "type": "select",
                "options": ["First song", "Second song", ""],
            },
            {"key": "cover", "label": "Artwork available", "type": "toggle"},
            {"key": "playing", "label": "Playing", "type": "toggle"},
            {"key": "focus", "label": "Text field focused", "type": "toggle"},
            {
                "key": "attention",
                "type": "select",
                "label": "Apple TV activity",
                "options": ["Asleep", "Screensaver", "Awake", "Idle"],
            },
        ],
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self.identity = str(uuid.uuid4()).encode()
        self.signing = Ed25519PrivateKey.generate()
        self.controllers = {}
        self.subscribers = set()
        self.media_sessions = set()
        self.keyboard_id = uuid.uuid4().bytes

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
        u = int.from_bytes(
            protocol.digest(protocol.octets(a, 384), protocol.octets(public, 384)),
            "big",
        )
        shared = pow(a * pow(verifier, u, n), exponent, n)
        key = protocol.digest(protocol.octets(shared, 384))
        fingerprint = bytes(
            i ^ j for i, j in zip(protocol.digest(protocol.octets(n)), protocol.digest(bytes([g])))
        )
        proof = protocol.digest(
            fingerprint,
            protocol.digest(b"Pair-Setup"),
            salt,
            client[3],
            protocol.octets(public, 384),
            key,
        )
        if not hmac.compare_digest(proof, client[4]):
            await self._auth_reply(wire, 4, 4, {7: b"\x02"})
            return
        await self._auth_reply(wire, 4, 4, {4: protocol.digest(client[3], proof, key)})
        record = await self._next(wire, 4, 5)
        cipher = protocol.pairing_cipher(key, "Setup")
        controller = protocol.fields(cipher.decrypt(b"\0\0\0\0PS-Msg05", record[5], None))
        Ed25519PublicKey.from_public_bytes(controller[3]).verify(
            controller[10],
            protocol.signature_prefix(key, "Controller") + controller[1] + controller[3],
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
            {
                1: self.identity,
                10: self.signing.sign(ours + self.identity + client_public),
            }
        )
        await self._auth_reply(
            wire,
            6,
            2,
            {3: ours, 5: cipher.encrypt(b"\0\0\0\0PV-Msg02", identity, None)},
        )
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
        if key in ("track", "position", "playing", "repeat", "shuffle", "cover"):
            for media in tuple(self.media_sessions):
                asyncio.create_task(media.publish())
        if key in ("attention", "focus", "volume"):
            self.announce_change(key)

    def announce_change(self, key):
        for wire in tuple(self.subscribers):
            asyncio.create_task(self._announce(wire, key))

    async def _announce(self, wire, key="attention"):
        if self._silenced:
            return
        try:
            event, body = {
                "attention": ("SystemStatus", {"state": self._attention()}),
                "volume": ("_iMC", {"_mcF": 256}),
                "focus": (
                    "_tiStarted" if self.get_state("focus") else "_tiStopped",
                    {},
                ),
            }[key]
            await wire.message(event, body, event=True)
        except (OSError, EOFError):
            self.subscribers.discard(wire)

    def _attention(self):
        return next(
            (n for n, name in protocol.ATTENTION.items() if name == self.get_state("attention")),
            0,
        )

    def _button(self, body):
        if body["_hBtS"] == 2:
            button = next(k for k, code in protocol.KEYS.items() if code == body["_hidC"])
            self.set_state("last_button", button)
            if button in ("sleep", "wake", "screensaver"):
                self.set_state(
                    "attention",
                    {"sleep": "Asleep", "wake": "Awake", "screensaver": "Screensaver"}[button],
                )
            elif button == "home":
                self.set_state("active_app", "Home")
            elif button == "play_pause":
                self.set_state("playing", not self.get_state("playing"))

    def _media(self, body):
        if body["_mcc"] == 5:
            return {"_vol": self.get_state("volume") / 100}
        if body["_mcc"] == 6:
            self.set_state("volume", body["_vol"] * 100)
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
            "FetchUserAccountsEvent": {"room": "Room", "guest": "Guest"},
            "_touchStart": {},
            "_touchStop": {},
            "_tiStop": {},
            "_tiStart": self._keyboard(),
            "FetchAttentionState": {"state": self._attention()},
        }
        if name in queries:
            return queries[name]
        handlers = {
            "_launchApp": self._launch,
            "_hidC": self._button,
            "_mcc": self._media,
            "SwitchUserAccountEvent": lambda args: self.set_state("profile", args["SwitchAccountID"]),
        }
        return handlers[name](body) or {}

    def _launch(self, body):
        self.set_state("active_app", body.get("_urlS") or APPS[body["_bundleID"]])

    def _keyboard(self):
        if not self.get_state("focus"):
            return {}
        return {"_tiD": protocol.plist_bytes({"$top": {"sessionUUID": self.keyboard_id}, "$objects": []})}

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
            if "_iMC" in body.get("_regEvents", []):
                await wire.message("_iMC", {"_mcF": 256}, event=True)
            if "SystemStatus" in body.get("_regEvents", []):
                self.subscribers.add(wire)
            return
        if name in ("_hidT", "_tiC"):
            self.set_state("last_button", "Swipe" if name == "_hidT" else "Keyboard edit")
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
            first = await asyncio.wait_for(reader.readexactly(1), 10)
            if first == b"P":
                stream = protocol.AirStream(reader, writer)
                stream.buffer.extend(first)
                await self._air_client(AirPeer(stream))
                return
            # Restore the inspected frame byte using a new bounded reader adapter.
            wire.reader = InitialByteReader(reader, first)
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

    async def _air_client(self, control):
        _, path, body = await control.recv_request()
        if path == "/pair-pin-start":
            await control.respond(pairing=True)
            _, path, body = await control.recv_request()
        if path == "/pair-setup":
            await self._enroll(control)
            return
        if path != "/pair-verify":
            return
        if await self._authenticate(control, protocol.fields(body)[3]):
            media = SimulatedMedia(self, control)
            self.media_sessions.add(media)
            try:
                await media.run()
            finally:
                self.media_sessions.discard(media)
