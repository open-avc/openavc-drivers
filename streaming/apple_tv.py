"""Direct local Apple TV control. Python handles binary Companion Link framing,
PIN pairing, per-session encryption and asynchronous tvOS events.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import struct
import uuid

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from openavc.discovery.mdns_scanner import MDNSScanner
from openavc.drivers.base import BaseDriver, ConnectionFaultError

SERVICE = "_companion-link._tcp.local."
LIMIT = 1024 * 1024


def packed_integer(value):
    if 0 <= value < 40:
        return bytes([8 + value])
    for tag, width in enumerate((1, 2, 4, 8), 0x30):
        if 0 <= value < 1 << (8 * width):
            return bytes([tag]) + value.to_bytes(width, "little")
    raise ValueError("Integer outside OPACK range")


def packed_data(raw, base, widths):
    size = len(raw)
    if size <= 32:
        return bytes([base + size]) + raw
    for code, width in enumerate(widths, base + 33):
        if size < 1 << (8 * width):
            return bytes([code]) + size.to_bytes(width, "little") + raw
    raise ValueError("OPACK value too large")


def packed_container(base, count, payload):
    return bytes([base + min(count, 15)]) + payload + (bytes([3]) if count >= 15 else b"")


def pack(value):
    """Write literal OPACK values. References are optional for the sender."""
    encoders = {
        type(None): lambda v: bytes([4]),
        bool: lambda v: bytes([1 if v else 2]),
        int: packed_integer,
        float: lambda v: bytes([0x36]) + struct.pack("<d", v),
        str: lambda v: packed_data(v.encode(), 0x40, (1, 2, 3, 4)),
        bytes: lambda v: packed_data(v, 0x70, (1, 2, 4, 8)),
        list: lambda v: packed_container(0xD0, len(v), b"".join(map(pack, v))),
        dict: lambda v: packed_container(0xE0, len(v), b"".join(pack(k) + pack(x) for k, x in v.items())),
    }
    return encoders[type(value)](value)


class OpackReader:
    def __init__(self, raw):
        self.raw = raw
        self.pos = 0
        self.objects = []
        self.seen = set()

    def take(self, size):
        if size > len(self.raw) - self.pos:
            raise ValueError("Truncated OPACK")
        start = self.pos
        self.pos += size
        return self.raw[start : self.pos]

    def number(self, width):
        return int.from_bytes(self.take(width), "little")

    def contents(self, count):
        if count < 15:
            yield from range(count)
            return
        while self.raw[self.pos : self.pos + 1] != bytes([3]):
            yield None
        self.take(1)

    def mapping(self, count, depth):
        result = {}
        for _ in self.contents(count):
            key = self.read(depth + 1)
            if not isinstance(key, str) or key in result:
                raise ValueError("Invalid OPACK dictionary key")
            result[key] = self.read(depth + 1)
        return result

    def reference(self, tag):
        index = tag - 0xA0 if tag <= 0xC0 else self.number(1 << (tag - 0xC1))
        if index >= len(self.objects):
            raise ValueError("Unknown OPACK reference")
        return self.objects[index]

    def blob(self, tag, base, widths):
        code = tag - base
        size = code if code <= 32 else self.number(widths[code - 33])
        return self.take(size)

    def atom(self, tag):
        if 0x30 <= tag <= 0x33:
            return self.number(1 << (tag - 0x30))
        if tag in (0x35, 0x36):
            fmt = "<f" if tag == 0x35 else "<d"
            return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]
        if 0x40 <= tag <= 0x64:
            return self.blob(tag, 0x40, (1, 2, 3, 4)).decode()
        if 0x70 <= tag <= 0x94:
            return self.blob(tag, 0x70, (1, 2, 4, 8))
        if tag == 5:
            return uuid.UUID(bytes=self.take(16))
        if tag == 6:
            return self.number(8)
        raise ValueError("Unknown OPACK type")

    def remember(self, value):
        token = (type(value), value)
        if token not in self.seen:
            self.objects.append(value)
            self.seen.add(token)
        return value

    def read(self, depth):
        if depth > 32:
            raise ValueError("Nested OPACK exceeds limit")
        return self.value(self.number(1), depth)

    def value(self, tag, depth):
        literals = {1: True, 2: False, 4: None, 0x40: "", 0x70: b""}
        if tag in literals:
            return literals[tag]
        if 8 <= tag <= 47:
            return tag - 8
        if 0xD0 <= tag <= 0xDF:
            return [self.read(depth + 1) for _ in self.contents(tag & 15)]
        if 0xE0 <= tag <= 0xEF:
            return self.mapping(tag & 15, depth)
        if 0xA0 <= tag <= 0xC4:
            return self.reference(tag)
        return self.remember(self.atom(tag))


def unpack(raw):
    decoder = OpackReader(raw)
    value = decoder.read(0)
    if decoder.pos != len(raw) or not isinstance(value, dict):
        raise ValueError("Expected one OPACK dictionary")
    return value


def tlv(fields):
    wire = bytearray()
    for tag, data in fields.items():
        for offset in range(0, max(1, len(data)), 255):
            part = data[offset : offset + 255]
            wire.extend((tag, len(part)))
            wire.extend(part)
    return bytes(wire)


def fields(data):
    result = {}
    pos = 0
    while pos < len(data):
        if len(data) - pos < 2:
            raise ValueError("Incomplete pairing record")
        key, size = data[pos : pos + 2]
        pos += 2
        if size > len(data) - pos:
            raise ValueError("Incomplete pairing value")
        result[key] = result.get(key, b"") + data[pos : pos + size]
        pos += size
    return result


def derive(secret, salt, label):
    return HKDF(algorithm=hashes.SHA512(), length=32, salt=salt.encode(), info=label.encode()).derive(secret)


def pairing_cipher(secret, stage):
    purpose = "Pair-" + stage + "-Encrypt-"
    return ChaCha20Poly1305(derive(secret, purpose + "Salt", purpose + "Info"))


def signature_prefix(secret, role):
    purpose = "Pair-Setup-" + role + "-Sign-"
    return derive(secret, purpose + "Salt", purpose + "Info")


def digest(*parts):
    return hashlib.sha512(b"".join(parts)).digest()


def octets(n, width=None):
    return n.to_bytes(width or max(1, (n.bit_length() + 7) // 8), "big")


# RFC 5054 Appendix A, 3072-bit SRP group (generator 5).
GROUP = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF",
    16,
)


def public_challenge(salt, server_key):
    peer = int.from_bytes(server_key, "big")
    if not 0 < len(salt) <= 64 or len(server_key) > 384:
        raise ValueError("Invalid Apple TV pairing challenge size")
    if not 0 < peer < GROUP:
        raise ValueError("Invalid Apple TV pairing challenge")
    return peer


def srp_exchange(pin, salt, server_key):
    peer = public_challenge(salt, server_key)
    private = secrets.randbits(384)
    public = pow(5, private, GROUP)
    a, b = octets(public, 384), octets(peer, 384)
    multiplier = int.from_bytes(digest(octets(GROUP), octets(5, 384)), "big")
    scrambling = int.from_bytes(digest(a, b), "big")
    if not scrambling:
        raise ValueError("Invalid pairing challenge")
    password = int.from_bytes(digest(salt, digest(b"Pair-Setup:", pin.encode("ascii"))), "big")
    base = (peer - multiplier * pow(5, password, GROUP)) % GROUP
    if not base:
        raise ValueError("Invalid pairing public key")
    session = digest(octets(pow(base, private + scrambling * password, GROUP), 384))
    group_hash = bytes(i ^ j for i, j in zip(digest(octets(GROUP)), digest(b"\x05")))
    proof = digest(group_hash, digest(b"Pair-Setup"), salt, a, server_key, session)
    return a, proof, digest(a, proof, session), session


class PeerError(ValueError):
    """The peer returned an application error on an otherwise working link."""


class PairError(ValueError):
    """Apple TV declined the pairing or proof of identity."""


class Wire:
    def __init__(self, reader, writer, trace=None):
        self.trace = trace
        self.reader, self.writer = reader, writer
        self.tx = self.rx = None
        self.sent = self.received = 0
        self.xid = 0
        self.awaiting_late_reply = set()
        self.pending = {}
        self.listener = None

    def encrypt(self, shared, server=False):
        outgoing = derive(shared, "", "ClientEncrypt-main")
        incoming = derive(shared, "", "ServerEncrypt-main")
        if server:
            outgoing, incoming = incoming, outgoing
        self.tx, self.rx = ChaCha20Poly1305(outgoing), ChaCha20Poly1305(incoming)

    async def send(self, kind, message):
        data = pack(message)
        length = len(data) + (16 if self.tx else 0)
        header = bytes([kind]) + length.to_bytes(3, "big")
        if self.tx:
            data = self.tx.encrypt(self.sent.to_bytes(12, "little"), data, header)
            self.sent += 1
        self.writer.write(header + data)
        await self.writer.drain()
        self._report("tx", kind, message)

    def _report(self, direction, kind, message):
        # Pairing frames contain credentials. Only record remote-control operations.
        if not self.trace or kind != 8:
            return
        name = message.get("_i", "Reply")
        if message.get("_t") == 3:
            name = "Refused" if "_em" in message or message.get("_ec") else "Acknowledged"
        self.trace(direction, f"{name} #{message.get('_x')}")

    async def receive(self):
        header = await self.reader.readexactly(4)
        size = int.from_bytes(header[1:], "big")
        if not 0 < size <= LIMIT:
            raise ValueError("Invalid Companion frame size")
        data = await self.reader.readexactly(size)
        if self.rx:
            data = self.rx.decrypt(self.received.to_bytes(12, "little"), data, header)
            self.received += 1
        message = unpack(data)
        self._report("rx", header[0], message)
        return header[0], message

    def transaction_id(self):
        for _ in range(127):
            self.xid = 1 + self.xid % 127
            if self.xid not in self.pending and self.xid not in self.awaiting_late_reply:
                return self.xid
        self.writer.close()
        raise ConnectionError("Apple TV session ran out of available request IDs.")

    async def auth(self, kind, seq, content, **extra):
        self.transaction_id()
        await self.send(kind, {"_x": self.xid, "_pd": tlv({6: bytes([seq]), **content}), **extra})
        reply_kind, reply = await asyncio.wait_for(self.receive(), 8)
        if reply_kind not in (kind, {3: 4, 5: 6}.get(kind)) or not isinstance(reply.get("_pd"), bytes):
            raise PairError("Apple TV returned an unexpected pairing response.")
        record = fields(reply["_pd"])
        if 7 in record:
            raise PairError("Apple TV refused pairing. Request a fresh PIN and check the digits on the TV.")
        if record.get(6) != bytes([seq + 1]):
            raise PairError("Apple TV returned an unexpected pairing step.")
        return record

    async def finish_pin(self, pin, challenge):
        a, proof, expected, secret = srp_exchange(pin, challenge[2], challenge[3])
        reply = await self.auth(4, 3, {3: a, 4: proof}, _pwTy=1)
        if not hmac.compare_digest(expected, reply.get(4, b"")):
            raise PairError("Apple TV's PIN proof did not match.")
        signing = Ed25519PrivateKey.generate()
        controller = str(uuid.uuid4()).upper().encode()
        public = signing.public_key().public_bytes_raw()
        sig = signing.sign(signature_prefix(secret, "Controller") + controller + public)
        identity = tlv({1: controller, 3: public, 10: sig, 17: pack({"name": "OpenAVC"})})
        cipher = pairing_cipher(secret, "Setup")
        reply = await self.auth(4, 5, {5: cipher.encrypt(b"\0\0\0\0PS-Msg05", identity, None)}, _pwTy=1)
        peer = fields(cipher.decrypt(b"\0\0\0\0PS-Msg06", reply[5], None))
        Ed25519PublicKey.from_public_bytes(peer[3]).verify(
            peer[10], signature_prefix(secret, "Accessory") + peer[1] + peer[3]
        )
        return json.dumps(
            [item.hex() for item in (controller, signing.private_bytes_raw(), peer[1], peer[3])]
        )

    async def verify(self, saved):
        controller, seed, peer_id, peer_key = (bytes.fromhex(item) for item in json.loads(saved))
        signing = Ed25519PrivateKey.from_private_bytes(seed)
        ephemeral = X25519PrivateKey.generate()
        ours = ephemeral.public_key().public_bytes_raw()
        response = await self.auth(5, 1, {3: ours}, _auTy=4)
        theirs = response[3]
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(theirs))
        cipher = pairing_cipher(shared, "Verify")
        peer = fields(cipher.decrypt(b"\0\0\0\0PV-Msg02", response[5], None))
        if peer[1] != peer_id:
            raise PairError("The responding Apple TV does not match the saved pairing.")
        Ed25519PublicKey.from_public_bytes(peer_key).verify(peer[10], theirs + peer_id + ours)
        identity = tlv({1: controller, 10: signing.sign(ours + controller + theirs)})
        await self.auth(6, 3, {5: cipher.encrypt(b"\0\0\0\0PV-Msg03", identity, None)})
        self.encrypt(shared)
        return controller

    def deliver(self, msg, on_event):
        if msg.get("_t") == 1:
            on_event(msg.get("_i"), msg.get("_c", {}))
            return
        if msg.get("_t") != 3:
            return
        xid = msg.get("_x")
        self.awaiting_late_reply.discard(xid)
        waiter = self.pending.get(xid)
        if not waiter or waiter.done():
            return
        if "_em" in msg or msg.get("_ec"):
            waiter.set_exception(PeerError(str(msg.get("_em", "Apple TV declined command"))))
        else:
            waiter.set_result(msg.get("_c", {}))

    async def dispatch(self, on_event, dropped):
        try:
            while True:
                kind, msg = await self.receive()
                if kind == 8:
                    self.deliver(msg, on_event)
        except (OSError, EOFError, ValueError, InvalidTag):
            pass
        finally:
            self.writer.close()
            self.abort_waiters()
            dropped()

    def abort_waiters(self):
        for waiter in self.pending.values():
            if not waiter.done():
                waiter.set_exception(ConnectionError("Apple TV connection closed"))

    async def message(self, name, body, event=False, timeout=6):
        xid = self.transaction_id()
        waiter = asyncio.get_running_loop().create_future()
        if not event:
            self.pending[xid] = waiter
        try:
            await self.send(8, {"_i": name, "_t": 1 if event else 2, "_x": xid, "_c": body})
            if not event:
                return await asyncio.wait_for(waiter, timeout)
        except (TimeoutError, asyncio.CancelledError):
            if not event:
                self.awaiting_late_reply.add(xid)
            raise
        finally:
            self.pending.pop(xid, None)
            waiter.cancel()

    async def close(self):
        self.writer.close()
        if self.listener:
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
        try:
            await self.writer.wait_closed()
        except OSError:
            pass


KEYS = {
    "up": 1,
    "down": 2,
    "left": 3,
    "right": 4,
    "back": 5,
    "select": 6,
    "home": 7,
    "volume_up": 8,
    "volume_down": 9,
    "screensaver": 11,
    "sleep": 12,
    "wake": 13,
    "play_pause": 14,
}
MEDIA = {"play": 1, "pause": 2, "next_track": 3, "previous_track": 4}
ATTENTION = {1: "Asleep", 2: "Screensaver", 3: "Awake", 4: "Idle"}


class AppleTVDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "apple_tv",
        "name": "Apple TV",
        "manufacturer": "Apple",
        "category": "streaming",
        "version": "1.0.0",
        "min_platform_version": "0.40.0",
        "author": "OpenAVC",
        "description": "Pair over the local network to navigate tvOS, control playback, launch installed apps and monitor power.",
        "source_url": "https://pyatv.dev/documentation/protocols/#companion",
        "tags": ["apple", "streaming", "media-player", "tvos"],
        "transport": "tcp",
        "protocols": ["companion_link"],
        "verified": False,
        "simulated": True,
        "web_ui": False,
        "compatible_models": [
            {
                "manufacturer": "Apple",
                "models": ["Apple TV HD", "Apple TV 4K"],
                "confidence": "untested",
                "notes": "Requires tvOS with the Companion Link remote service. Simulator and protocol interoperability tested.",
            }
        ],
        "help": {
            "overview": "Controls Apple TV HD and Apple TV 4K directly from OpenAVC. Give each room its own Apple TV device. Apps come from that TV, so an integrator can pick an installed app by name.",
            "setup": "Add the Apple TV by discovery or enter its address. Keep its display visible. Under Actions choose 1. Show pairing PIN, then 2. Enter pairing PIN. Enter all four digits shown on that TV, including leading zeros. OpenAVC saves the verified pairing on this controller and reconnects. To replace a TV or restore onto a different controller, repeat both steps. If Bonjour cannot cross your VLANs, enter the TCP port advertised for _companion-link._tcp in Port override. Apple TV advertises this port dynamically. Playback and volume buttons depend on the active app and audio route. In the built-in simulator the PIN is 1234. Some tvOS versions first report power when it changes.",
        },
        "discovery": {
            "mdns": [
                {"service": "_companion-link._tcp.local.", "txt": {"rpMd": "AppleTV5,3"}},
                {"service": "_companion-link._tcp.local.", "txt": {"rpMd": "AppleTV6,2"}},
                {"service": "_companion-link._tcp.local.", "txt": {"rpMd": "AppleTV11,1"}},
                {"service": "_companion-link._tcp.local.", "txt": {"rpMd": "AppleTV14,1"}},
            ]
        },
        "default_config": {"host": "", "port": 0, "poll_interval": 0},
        "config_schema": {
            "host": {
                "type": "string",
                "label": "Apple TV address",
                "required": True,
                "description": "IP address or Bonjour hostname of the Apple TV.",
            },
            "port": {
                "type": "integer",
                "label": "Port override",
                "default": 0,
                "min": 0,
                "max": 65535,
                "advanced": True,
                "description": "Leave at 0 to look up the TV's current Companion Link port using Bonjour.",
            },
            "pairing_key": {
                "type": "password",
                "secret": True,
                "label": "Saved pairing",
                "advanced": True,
                "description": "Set automatically by the Enter pairing PIN action.",
            },
        },
        "state_variables": {
            "power": {
                "type": "string",
                "label": "Power",
                "cloud_priority": "high",
                "help": "On, Off or Unknown, from Apple TV's attention state.",
            },
            "attention": {
                "type": "string",
                "label": "Apple TV activity",
                "help": "Awake, Asleep, Screensaver, Idle or Unknown.",
            },
            "app_options": {
                "type": "string",
                "label": "Installed apps",
                "help": "Choices for the Launch app command.",
            },
        },
        "commands": {
            "up": {"help": "Move the selection up.", "label": "Up"},
            "down": {"help": "Move the selection down.", "label": "Down"},
            "left": {"help": "Move the selection left.", "label": "Left"},
            "right": {"help": "Move the selection right.", "label": "Right"},
            "select": {"help": "Open the highlighted item.", "label": "Select"},
            "back": {"help": "Go back one screen.", "label": "Back"},
            "home": {
                "help": "Show the TV app or Home Screen according to the TV Button setting in tvOS.",
                "label": "TV / Home",
            },
            "home_hold": {"label": "Hold TV / Home", "help": "Open tvOS Control Center."},
            "wake": {"help": "Wake the Apple TV.", "label": "Wake Apple TV", "sets": {"power": "On"}},
            "sleep": {
                "help": "Put the Apple TV to sleep.",
                "label": "Sleep Apple TV",
                "sets": {"power": "Off"},
            },
            "screensaver": {
                "help": "Display the Apple TV screen saver.",
                "label": "Start screen saver",
                "sets": {"attention": "Screensaver"},
            },
            "volume_up": {"help": "Raise the volume on a supported audio route.", "label": "Volume up"},
            "volume_down": {"help": "Lower the volume on a supported audio route.", "label": "Volume down"},
            "play_pause": {"help": "Pause playing media or resume paused media.", "label": "Play / pause"},
            "play": {"help": "Start or resume playback in the active app.", "label": "Play"},
            "pause": {"help": "Pause playback in the active app.", "label": "Pause"},
            "next_track": {
                "help": "Advance to the next item when the active app supports it.",
                "label": "Next track",
            },
            "previous_track": {
                "help": "Return to the previous item when the active app supports it.",
                "label": "Previous track",
            },
            "skip_forward": {
                "label": "Skip forward",
                "help": "Move forward in the playing item when the app supports seeking.",
                "params": {
                    "seconds": {"type": "integer", "label": "Seconds", "min": 1, "max": 600, "default": 10}
                },
            },
            "skip_back": {
                "label": "Skip back",
                "help": "Move back in the playing item when the app supports seeking.",
                "params": {
                    "seconds": {"type": "integer", "label": "Seconds", "min": 1, "max": 600, "default": 10}
                },
            },
            "launch_app": {
                "label": "Launch app",
                "help": "Open the selected installed app on this TV.",
                "params": {
                    "app": {
                        "type": "string",
                        "label": "App",
                        "required": True,
                        "options_state": "app_options",
                    }
                },
            },
            "refresh_apps": {
                "help": "Rebuild the picker from apps currently installed on this Apple TV.",
                "label": "Refresh installed apps",
                "query_for": "app_options",
            },
            "read_power": {
                "help": "Read the TV’s current attention state on tvOS versions that answer the power query.",
                "label": "Read power",
                "query_for": "power",
            },
        },
        "actions": [
            {
                "id": "start_pairing",
                "kind": "setup",
                "availability": "always",
                "label": "1. Show pairing PIN",
                "icon": "monitor",
            },
            {
                "id": "finish_pairing",
                "kind": "setup",
                "availability": "always",
                "label": "2. Enter pairing PIN",
                "icon": "key",
                "params": {
                    "pin": {
                        "type": "password",
                        "secret": True,
                        "required": True,
                        "label": "Four digits shown on this TV",
                    }
                },
            },
            {"id": "wake"},
            {"id": "home"},
            {"id": "up"},
            {"id": "left"},
            {"id": "select"},
            {"id": "right"},
            {"id": "down"},
            {"id": "back"},
            {"id": "play_pause"},
            {"id": "launch_app"},
            {"id": "sleep"},
            {"id": "refresh_apps"},
        ],
        "quick_actions": ["home", "play_pause", "launch_app"],
    }

    def __init__(self, device_id, config, state, events):
        super().__init__(device_id, config, state, events)
        self._wire = None
        self._pair = None
        self._expiry = None
        self._apps = {}
        self._keys = asyncio.Lock()

    def _simulating(self):
        endpoint = ("127.0.0.1", int(self.config.get("port") or 0))
        return self.config.get("host") == endpoint[0] and getattr(self, "udp_redirect", None) == endpoint

    @staticmethod
    def _advertised(device, target):
        for entry in device.service_list():
            names = (device.ip, device.hostname, device.address_name, entry.target, entry.instance_name)
            kind = (entry.service_type or "").rstrip(".").lower()
            if kind != SERVICE.rstrip("."):
                continue
            if target in {n.rstrip(".").casefold() for n in names if n} and entry.port:
                return entry.port
        return None

    async def _resolve(self, host, local):
        scanner = MDNSScanner(control_ip=local or "", service_types=[SERVICE])
        devices = await scanner.start(duration=2.5)
        target = host.rstrip(".").casefold()
        for device in devices.values():
            port = self._advertised(device, target)
            if port:
                return device.ip, port
        raise ConnectionError(
            "Apple TV's Companion Link port could not be found. Check Bonjour reachability or set Port override."
        )

    async def _open(self):
        host = str(self.config.get("host") or "").strip()
        if not host:
            raise ValueError("Enter the Apple TV address.")
        port = int(self.config.get("port") or 0)
        local = self.local_address(host)
        if not port:
            host, port = await self._resolve(host, local)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, local_addr=(local, 0) if local else None), 5
        )
        return Wire(reader, writer, self.record_traffic)

    async def _pre_connect(self):
        if not self.config.get("pairing_key") and not self._simulating():
            raise ConnectionFaultError(
                code="auth_failed",
                message="Apple TV needs a pairing. Use Show pairing PIN, then Enter pairing PIN.",
            )

    async def _create_transport(self, transport_type):
        self._wire = await self._open()

    async def _simulation_pairing(self):
        # Only used on the platform's redirected loopback endpoint. Each simulator
        # instance owns new keys. Never store them in the physical TV's project.
        wire = self._wire
        challenge = await wire.auth(3, 1, {0: bytes([0])}, _pwTy=1)
        saved = await wire.finish_pin("1234", challenge)
        await wire.close()
        self._wire = await self._open()
        return saved

    async def _post_connect(self):
        saved = await self._simulation_pairing() if self._simulating() else self.config.get("pairing_key")
        wire = self._wire

        try:
            identity = await wire.verify(saved)
        except (ValueError, TypeError, KeyError, InvalidSignature, InvalidTag) as exc:
            raise ConnectionFaultError(
                code="auth_failed", message="Apple TV could not verify the saved pairing. Pair this TV again."
            ) from exc

        def dropped():
            if wire is self._wire:
                self._handle_transport_disconnect()

        wire.listener = asyncio.create_task(wire.dispatch(self._event, dropped))
        fingerprint = hashlib.sha256(identity).digest()
        address = ":".join(f"{b:02X}" for b in bytes([(fingerprint[0] | 2) & 254]) + fingerprint[1:6])
        self._controller_info = {
            "_bf": 0,
            "_cf": 512,
            "_clFl": 128,
            "_i": address.replace(":", "").lower(),
            "_idsID": identity,
            "_pubID": address,
            "_sf": 256,
            "_sv": "170.18",
            "model": "OpenAVC",
            "name": "OpenAVC",
        }
        await wire.message("_systemInfo", self._controller_info)
        await wire.message(
            "_sessionStart", {"_srvT": "com.apple.tvremoteservices", "_sid": secrets.randbits(32)}
        )
        try:
            await wire.message("TVRCSessionStart", {"ProtocolVersionKey": "1.2"}, timeout=2)
        except (PeerError, TimeoutError):
            pass  # Older tvOS does not register this service.
        self.set_states({"power": "Unknown", "attention": "Unknown"})
        await wire.message("_interest", {"_regEvents": ["SystemStatus", "TVSystemStatus"]}, event=True)

    async def _initial_sync(self):
        try:
            await self.poll()
        except TimeoutError:
            pass  # Some versions publish attention changes without answering a snapshot.
        try:
            await self._refresh_apps()
        except (PeerError, TimeoutError):
            self.set_state("app_options", "[]")

    def _event(self, name, body):
        if name in ("TVSystemStatus", "SystemStatus") and isinstance(body, dict):
            state = body.get("state")
            self.set_states(
                {
                    "attention": ATTENTION.get(state, "Unknown"),
                    "power": "Off" if state == 1 else "On" if state in (2, 3, 4) else "Unknown",
                }
            )

    async def poll(self):
        try:
            reply = await self._wire.message("FetchAttentionState", {}, timeout=2)
        except PeerError:
            return  # Newer releases may only send changes through the subscription.
        self._event("SystemStatus", reply)

    async def _refresh_apps(self):
        apps = await self._wire.message("FetchLaunchableApplicationsEvent", {})
        self._apps = {k: v for k, v in apps.items() if isinstance(k, str) and isinstance(v, str)}
        options = [
            {"value": key, "label": label}
            for key, label in sorted(self._apps.items(), key=lambda app: (app[1].casefold(), app[0]))
        ]
        self.set_state("app_options", json.dumps(options, ensure_ascii=False))

    def _link_alive(self):
        return self._wire is not None and not self._wire.writer.is_closing()

    async def _liveness_probe(self):
        await self._wire.message("_systemInfo", self._controller_info)

    async def disconnect(self):
        self._discard_pin()
        await super().disconnect()

    async def _close_session(self):
        wire, self._wire = self._wire, None
        if wire:
            await wire.close()
        self._apps.clear()
        self.set_states({"power": "Unknown", "attention": "Unknown", "app_options": "[]"})

    async def send_command(self, command, params=None):
        queries = {"read_power": self.poll, "refresh_apps": self._refresh_apps}
        if command in queries:
            return await queries[command]()
        if command == "launch_app":
            return await self._launch(params)
        if command in MEDIA:
            await self._wire.message("_mcc", {"_mcc": MEDIA[command]})
        elif command in ("skip_forward", "skip_back"):
            await self._skip(command, params)
        else:
            await self._button(command)

    async def _skip(self, command, params):
        seconds = int((params or {}).get("seconds", 10))
        if not 1 <= seconds <= 600:
            raise ValueError("Choose a skip of 1 to 600 seconds.")
        offset = seconds if command == "skip_forward" else -seconds
        await self._wire.message("_mcc", {"_mcc": 7, "_skpS": float(offset)})

    async def _launch(self, params):
        app = (params or {}).get("app")
        if app not in self._apps:
            raise ValueError("Choose an installed app. Refresh installed apps if the TV has changed.")
        await self._wire.message("_launchApp", {"_bundleID": app})

    async def _button(self, command):
        hold = command == "home_hold"
        key = KEYS.get("home" if hold else command)
        if key is None:
            raise ValueError(f"Unknown Apple TV command: {command}")
        async with self._keys:
            wire = self._wire
            await wire.message("_hidC", {"_hidC": key, "_hBtS": 1})
            try:
                if hold:
                    await asyncio.sleep(1)
            finally:
                await wire.message("_hidC", {"_hidC": key, "_hBtS": 2})

    def _discard_pin(self):
        if self._expiry:
            self._expiry.cancel()
            self._expiry = None
        if self._pair:
            wire, _ = self._pair
            wire.writer.close()
            self._pair = None

    async def run_setup_action(self, action_id, params, progress):
        if action_id == "start_pairing":
            return await self._start_pin(progress)
        if action_id == "finish_pairing":
            return await self._finish_pin(params, progress)
        raise ValueError(f"Unknown Apple TV setup action: {action_id}")

    async def _start_pin(self, progress):
        self._discard_pin()
        await progress("Requesting a PIN on the Apple TV…", 10)
        wire = await self._open()
        try:
            challenge = await wire.auth(3, 1, {0: b"\0"}, _pwTy=1)
        except BaseException:
            await wire.close()
            raise
        self._pair = (wire, challenge)
        self._expiry = asyncio.get_running_loop().call_later(120, self._discard_pin)
        return {
            "message": "Read the PIN on the Apple TV, then choose 2. Enter pairing PIN within two minutes."
        }

    def _claim_pin(self, params):
        pin = str(params.get("pin") or "").strip()
        if not re.fullmatch(r"[0-9]{4}", pin):
            raise ValueError("Enter all four digits displayed on the Apple TV.")
        if not self._pair:
            raise ValueError("Request a fresh PIN with 1. Show pairing PIN.")
        wire, challenge = self._pair
        self._pair = None
        self._discard_pin()
        return pin, wire, challenge

    async def _finish_pin(self, params, progress):
        pin, wire, challenge = self._claim_pin(params)
        try:
            await progress("Verifying the PIN with the Apple TV…", 25)
            saved = await wire.finish_pin(pin, challenge)
        except (InvalidSignature, InvalidTag, KeyError) as exc:
            raise PairError("Apple TV could not confirm its pairing identity. Request a fresh PIN.") from exc
        finally:
            await wire.close()
        wire = await self._open()
        try:
            await wire.verify(saved)
        finally:
            await wire.close()
        await progress("Pairing verified. Saving on this controller…", 85)
        if not self._simulating():
            await self.request_config_update({"pairing_key": saved})
        await self.request_reconnect()
        return {"message": "Paired with Apple TV."}
