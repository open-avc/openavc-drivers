"""Native Apple TV remote, AirPlay media feedback and playback for HD and 4K models."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import plistlib
import re
import secrets
import struct
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from openavc.discovery.mdns_scanner import MDNSScanner
from openavc.drivers.base import BaseDriver, ConnectionFaultError
from openavc.drivers.command_files import CommandFile, DeviceFile

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


# Media Remote uses length-delimited protobuf messages inside AirPlay data frames.
# Only the fields this driver consumes are decoded. Unknown fields remain skippable.
AIRPLAY = "_airplay._tcp.local."
MEDIA_LIMIT = 4 * 1024 * 1024
FRAME_HEADER = struct.Struct(">I12s4sQ4x")
PLAY_STATES = {
    0: "Unknown",
    1: "Playing",
    2: "Paused",
    3: "Stopped",
    4: "Interrupted",
    5: "Seeking",
}
REPEAT = {"Off": 1, "One": 2, "All": 3}
SHUFFLE = {"Off": 1, "Albums": 2, "Songs": 3}


def varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def read_varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Invalid media integer")


def proto(entries):
    out = bytearray()
    for number, value in entries:
        if isinstance(value, str):
            value = value.encode()
        if isinstance(value, bytes):
            wire, raw = 2, varint(len(value)) + value
        elif isinstance(value, float):
            wire, raw = 1, struct.pack("<d", value)
        else:
            wire, raw = 0, varint(value)
        out.extend(varint(number * 8 + wire) + raw)
    return bytes(out)


def proto_value(data, pos, wire):
    if wire == 0:
        return read_varint(data, pos)
    if wire == 2:
        size, pos = read_varint(data, pos)
    else:
        size = {1: 8, 5: 4}[wire]
    end = pos + size
    if end > len(data):
        raise ValueError("Truncated media field")
    raw = data[pos:end]
    if wire != 2:
        return struct.unpack("<d" if wire == 1 else "<f", raw)[0], end
    return raw, end


def protobuf(data):
    result = {}
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        value, pos = proto_value(data, pos, tag % 8)
        result.setdefault(tag // 8, []).append(value)
    return result


def first(fields, key, default=b""):
    return fields.get(key, [default])[-1]


def embedded(fields, key):
    return protobuf(first(fields, key))


def string_field(fields, key):
    return first(fields, key).decode("utf-8", errors="replace")


def plist_bytes(body):
    return plistlib.dumps(body, fmt=plistlib.FMT_BINARY)


class AirStream:
    """Incremental stream of authenticated HAP records. Each channel owns both counters."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.buffer = bytearray()
        self.rx = self.tx = None
        self.received = self.sent = 0

    def keys(self, shared, salt, outbound, inbound):
        self.tx = ChaCha20Poly1305(derive(shared, salt, outbound))
        self.rx = ChaCha20Poly1305(derive(shared, salt, inbound))

    async def chunk(self):
        if not self.rx:
            raw = await self.reader.read(8192)
            if not raw:
                raise EOFError("AirPlay connection closed")
            return raw
        header = await self.reader.readexactly(2)
        size = int.from_bytes(header, "little")
        if not 0 < size <= 1024:
            raise ValueError("Invalid AirPlay record length")
        raw = await self.reader.readexactly(size + 16)
        clear = self.rx.decrypt(bytes(4) + self.received.to_bytes(8, "little"), raw, header)
        self.received += 1
        return clear

    async def take(self, count):
        if not 0 <= count <= MEDIA_LIMIT:
            raise ValueError("AirPlay message is too large")
        while len(self.buffer) < count:
            self.buffer.extend(await self.chunk())
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    async def headers(self):
        end = b"\r\n\r\n"
        while end not in self.buffer:
            if len(self.buffer) > 16384:
                raise ValueError("Invalid AirPlay headers")
            self.buffer.extend(await self.chunk())
        lines = (await self.take(self.buffer.index(end) + 4)).decode("latin-1").split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
        return (
            lines[0],
            headers,
            await self.take(int(headers.get("content-length", "0"))),
        )

    async def write(self, data):
        if self.tx:
            for start in range(0, len(data), 1024):
                part = data[start : start + 1024]
                header = len(part).to_bytes(2, "little")
                raw = self.tx.encrypt(bytes(4) + self.sent.to_bytes(8, "little"), part, header)
                self.writer.write(header + raw)
                self.sent += 1
        else:
            self.writer.write(data)
        await self.writer.drain()

    async def http(self, line, headers, body=b""):
        headers = {**headers, "Content-Length": str(len(body))}
        lines = [line, *(f"{key}: {value}" for key, value in headers.items()), "", ""]
        await self.write("\r\n".join(lines).encode("ascii") + body)

    async def frame(self):
        header = await self.take(FRAME_HEADER.size)
        size, kind, _, sequence = FRAME_HEADER.unpack(header)
        return kind.rstrip(b"\0"), sequence, await self.take(size - len(header))

    async def frame_out(self, kind, sequence, body=b""):
        command = b"comm" if body else b""
        header = FRAME_HEADER.pack(FRAME_HEADER.size + len(body), kind, command, sequence)
        await self.write(header + body)

    async def read_messages(self):
        kind, sequence, body = await self.frame()
        if kind == b"sync":
            await self.frame_out(b"rply", sequence)
        return frame_messages(body) if body else ()

    async def messages(self, sequence, envelopes):
        data = b"".join(varint(len(raw)) + raw for raw in envelopes)
        await self.frame_out(b"sync", sequence, plist_bytes({"params": {"data": data}}))


def frame_messages(body):
    packet = plistlib.loads(body)
    data = packet.get("params", {}).get("data", b"")
    pos = 0
    while pos < len(data):
        size, pos = read_varint(data, pos)
        if not 0 < size <= len(data) - pos:
            raise ValueError("Invalid media envelope")
        yield protobuf(data[pos : pos + size])
        pos += size


class AirControl(Wire):
    """Same PIN and signature verification as the remote, over AirPlay's HTTP carrier."""

    def __init__(self, reader, writer, trace=None):
        super().__init__(reader, writer, trace)
        self.stream = AirStream(reader, writer)
        self.secret = None
        self.sequence = 0
        self.instance = secrets.token_hex(8).upper()

    def encrypt(self, shared, server=False):
        if self.stream.buffer:
            raise ValueError("Unexpected data at the AirPlay encryption boundary")
        self.secret = shared
        self.stream.keys(
            shared,
            "Control-Salt",
            "Control-Write-Encryption-Key",
            "Control-Read-Encryption-Key",
        )

    async def request(self, method, target, body=b"", *, extra=None):
        self.sequence += 1
        host, port, *_ = self.writer.get_extra_info("peername")
        address = f"[{host}]" if ":" in host else host
        headers = {
            "Host": f"{address}:{port}",
            "CSeq": self.sequence,
            "User-Agent": "AirPlay/620.14.6",
            "DACP-ID": self.instance,
            "Client-Instance": self.instance,
            "Active-Remote": int(self.instance[:8], 16),
        }
        if body:
            headers["Content-Type"] = "application/x-apple-binary-plist"
        headers.update(extra or {})
        version = "HTTP/1.1" if extra else "RTSP/1.0"
        async with asyncio.timeout(6):
            await self.stream.http(f"{method} {target} {version}", headers, body)
            status, _, result = await self.stream.headers()
        if status.split()[1] != "200":
            raise PeerError(f"Apple TV declined {method} ({status.split()[1]}).")
        return result

    async def auth(self, kind, seq, content, **extra):
        headers = {"X-Apple-HKP": "3", "Content-Type": "application/octet-stream"}
        setup = kind in (3, 4)
        if setup and seq == 1:
            await self.request("POST", "/pair-pin-start", extra=headers)
        path = "/pair-setup" if setup else "/pair-verify"
        raw = await self.request("POST", path, tlv({6: bytes([seq]), **content}), extra=headers)
        record = fields(raw)
        if 7 in record or record.get(6) != bytes([seq + 1]):
            raise PairError("Apple TV declined the media pairing. Request a new media PIN.")
        return record


ROOM_CONTROLS = {4: "can_stop", 45: "can_seek", 46: "can_repeat", 47: "can_shuffle"}

MEDIA_EMPTY = {
    **dict.fromkeys(ROOM_CONTROLS.values()),
    "track_genre": "",
    "media_app": "",
    "track_title": "",
    "track_artist": "",
    "track_album": "",
    "series_name": "",
    "season_number": None,
    "episode_number": None,
    "duration": None,
    "position": None,
    "playback_state": "Unknown",
    "queue_count": None,
    "artwork_available": False,
    "repeat_mode": "Unknown",
    "shuffle_mode": "Unknown",
}

DIRECT_FIELDS = {
    1: "track_album",
    2: "track_artist",
    3: "duration",
    4: "position",
    5: "rate",
    8: "timestamp",
    9: "track_title",
}
ITEM_FIELDS = {
    1: "track_title",
    6: "track_album",
    7: "track_artist",
    10: "season_number",
    11: "episode_number",
    14: "duration",
    19: "artwork_available",
    35: "position",
    36: "track_genre",
    39: "rate",
    63: "series_name",
    74: "timestamp",
}


def media_values(fields, mapping):
    updates = {}
    for key in fields.keys() & mapping.keys():
        value = first(fields, key)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, float) and not math.isfinite(value):
            continue
        updates[mapping[key]] = value
    return updates


def path_info(path):
    client, player = embedded(path, 2), embedded(path, 3)
    return (string_field(client, 2), string_field(player, 1)), string_field(client, 7)


class PlayerState:
    def __init__(self):
        self.values = {}
        self.queue = []
        self.location = 0
        self.direct_id = None
        self.item_id = ""
        self.commands = None

    def replace_item(self):
        for key in (*ITEM_FIELDS.values(), *DIRECT_FIELDS.values()):
            self.values.pop(key, None)

    def direct(self, fields):
        identifier = first(fields, 10, None)
        if identifier is not None and self.direct_id not in (None, identifier):
            self.queue_state({})
            self.values.pop("queue_count", None)
        if identifier is not None:
            self.direct_id = identifier
        self.values.update(media_values(fields, DIRECT_FIELDS))
        for number, key, modes in (
            (6, "repeat_mode", REPEAT),
            (7, "shuffle_mode", SHUFFLE),
        ):
            if number in fields:
                self.values[key] = next(
                    (name for name, code in modes.items() if code == first(fields, number)),
                    "Unknown",
                )

    def content(self, item):
        self.item_id = string_field(item, 1)
        self.values.update(media_values(embedded(item, 2), ITEM_FIELDS))
        if first(item, 3):
            self.values["artwork_available"] = True

    def queue_state(self, queue):
        items = [protobuf(raw) for raw in queue.get(2, [])]
        self.location = first(queue, 1, 0)
        self.queue = [string_field(item, 1) for item in items]
        new_id = self.queue[self.location] if self.location < len(items) else ""
        if not new_id or new_id != self.item_id:
            self.direct_id = None
            self.replace_item()
        self.item_id = new_id
        self.values["queue_count"] = len(items)
        if self.location < len(items):
            self.content(items[self.location])

    def update(self, state):
        if 3 in state:
            self.queue_state(embedded(state, 3))
        if 1 in state:
            self.direct(embedded(state, 1))
        if 6 in state:
            self.values["playback_state"] = PLAY_STATES.get(first(state, 6), "Unknown")
        if 2 in state:
            self.commands = supported_commands(embedded(state, 2))

    def items_changed(self, update):
        current = self.queue[self.location] if self.location < len(self.queue) else ""
        for raw in update.get(1, []):
            item = protobuf(raw)
            if current and string_field(item, 1) == current:
                self.content(item)


def supported_commands(supported):
    commands = map(protobuf, supported.get(1, []))
    return {first(item, 1) for item in commands if first(item, 2, 0)}


def apply_playback_clock(values):
    rate = values.pop("rate", None)
    stamp = values.pop("timestamp", None)
    if values["playback_state"] != "Playing":
        return
    if rate == 0:
        values["playback_state"] = "Paused"
        return
    if None in (rate, stamp, values["position"]):
        return
    elapsed = max(0, time.time() - (978307200 + stamp))
    updated = max(0, values["position"] + elapsed * rate)
    limit = values.get("duration")
    values["position"] = round(min(updated, limit) if limit is not None else updated, 1)


class NowPlaying:
    def __init__(self):
        self.players = {}
        self.names = {}
        self.selected = ("", "")
        self.defaults = {}

    def current_path(self):
        app, player = self.selected
        if not app or player:
            return self.selected
        for key in ((app, "MediaRemote-DefaultPlayer"), (app, "")):
            if key in self.players:
                return key
        candidates = [key for key in self.players if key[0] == app]
        return candidates[0] if len(candidates) == 1 else self.selected

    def current(self):
        return self.players.get(self.current_path()) if self.selected[0] else None

    def supported(self):
        current = self.current()
        if current and current.commands is not None:
            return current.commands
        return self.defaults.get(self.selected[0])

    def item_key(self):
        player = self.current()
        return (self.current_path(), player.item_id, player.direct_id) if player else None

    def snapshot(self):
        app, _ = self.selected
        current = self.current()
        values = {**MEDIA_EMPTY, **(current.values if current else {})}
        apply_playback_clock(values)
        values["artwork_available"] = bool(values["artwork_available"])
        supported = self.supported()
        if supported is not None:
            values.update({label: code in supported for code, label in ROOM_CONTROLS.items()})
        values["media_app"] = self.names.get(app) or app
        return values

    def select_client(self, client):
        bundle = string_field(client, 2)
        if bundle != self.selected[0]:
            self.selected = (bundle, "")
        if bundle:
            self.names[bundle] = string_field(client, 7) or self.names.get(bundle, "")

    def route(self, packet):
        kind = first(packet, 1, 0)
        if kind == 46:
            self.select_client(embedded(embedded(packet, 50), 1))
        elif kind == 47:
            self.selected, name = path_info(embedded(embedded(packet, 51), 1))
            if name:
                self.names[self.selected[0]] = name
        elif kind in (53, 54):
            self.remove(kind, packet)
        elif kind in (4, 56, 72):
            self.update_player(kind, packet)
        else:
            return False
        return True

    def remove(self, kind, packet):
        if kind == 54:
            key, _ = path_info(embedded(embedded(packet, 58), 1))
            self.players.pop(key, None)
            if key == self.selected:
                self.selected = (key[0], "")
            return
        self.remove_app(string_field(embedded(embedded(packet, 57), 1), 2))

    def remove_app(self, app):
        self.players = {k: v for k, v in self.players.items() if k[0] != app}
        self.names.pop(app, None)
        self.defaults.pop(app, None)
        if self.selected[0] == app:
            self.selected = ("", "")

    def update_player(self, kind, packet):
        ext, path = {4: (9, 9), 56: (60, 2), 72: (75, 9)}[kind]
        state = embedded(packet, ext)
        key, name = path_info(embedded(state, path))
        if not key[0]:
            key = self.selected
        if name:
            self.names[key[0]] = name
        if kind == 72:
            self.defaults[key[0]] = supported_commands(embedded(state, 2))
            return
        player = self.player_for(key)
        if kind == 4:
            player.update(state)
        else:
            player.items_changed(state)

    def player_for(self, key):
        if len(self.players) >= 64 and key not in self.players:
            del self.players[next(k for k in self.players if k != self.selected)]
        if key not in self.players:
            self.players[key] = PlayerState()
        return self.players[key]


class PlaybackClock(asyncio.DatagramProtocol):
    """Answer AirPlay timing requests only from the TV that owns the stream."""

    def __init__(self, peer):
        self.peer = peer
        self.port = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if addr[0] != self.peer or self.port not in (None, addr[1]):
            return
        if len(data) != 32 or data[1] & 127 != 82:
            return
        ntp = int((time.time() + 2208988800) * (1 << 32))
        response = struct.pack(">BBH4x8sQQ", data[0], 211, 7, data[24:32], ntp, ntp)
        self.transport.sendto(response, addr)


class LocalMedia:
    """A temporary, seekable copy served only to the paired Apple TV for this playback."""

    def __init__(self, peer):
        self.peer = peer
        self.folder = tempfile.TemporaryDirectory(prefix="openavc-apple-tv-")
        self.runner = None
        self.path = None

    async def open(self, file, local):
        suffix = Path(file.name).suffix.lower()
        if suffix not in (".mp4", ".m4v", ".mov", ".mp3", ".m4a", ".aac", ".wav"):
            raise ValueError("Choose an MP4, M4V, MOV, MP3, M4A, AAC or WAV file.")
        self.path = Path(self.folder.name) / ("media" + suffix)
        with self.path.open("wb") as dest:
            async for chunk in file.chunks(256 * 1024):
                await asyncio.to_thread(dest.write, chunk)
        route = "/" + secrets.token_urlsafe(32) + suffix
        app = web.Application()
        app.router.add_get(route, self.fetch)
        self.runner = web.AppRunner(app, access_log=None, handle_signals=False, shutdown_timeout=2)
        await self.runner.setup()
        site = web.TCPSite(self.runner, local, 0)
        await site.start()
        port = self.runner.addresses[0][1]
        host = f"[{local}]" if ":" in local else local
        return f"http://{host}:{port}{route}"

    async def fetch(self, request):
        if request.remote != self.peer:
            raise web.HTTPNotFound()
        return web.FileResponse(self.path, headers={"Cache-Control": "no-store"})

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
        await asyncio.to_thread(self.folder.cleanup)


class URLPlayback:
    def __init__(self, control):
        self.control = control
        self.file = None
        self.timing = None
        self.events = None
        self.jobs = []
        self.session = str(uuid.uuid4()).upper()

    async def open(self, credential, source):
        controller = await self.control.verify(credential)
        sock = self.control.writer
        local = sock.get_extra_info("sockname")[0]
        peer = sock.get_extra_info("peername")[0]
        url = source
        if isinstance(source, CommandFile):
            self.file = LocalMedia(peer)
            url = await self.file.open(source, local)
        self.timing, clock = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: PlaybackClock(peer), local_addr=(local, 0)
        )
        mac = hashlib.sha256(controller).digest()[:6].hex(":")
        authority = f"[{local}]" if ":" in local else local
        target = f"rtsp://{authority}/{secrets.randbits(32)}"
        hello = {
            "deviceID": mac,
            "macAddress": mac,
            "name": "OpenAVC",
            "model": "OpenAVC",
            "sessionUUID": self.session,
            "timingProtocol": "NTP",
            "timingPort": self.timing.get_extra_info("sockname")[1],
            "isMultiSelectAirPlay": True,
            "sourceVersion": "690.7.1",
            "osName": "OpenAVC",
            "osVersion": "1.0",
        }
        reply = plistlib.loads(await self.control.request("SETUP", target, plist_bytes(hello)))
        clock.port = reply.get("timingPort")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(peer, reply["eventPort"], local_addr=(local, 0)), 5
        )
        self.events = AirStream(reader, writer)
        self.events.keys(
            self.control.secret,
            "Events-Salt",
            "Events-Read-Encryption-Key",
            "Events-Write-Encryption-Key",
        )
        self.jobs.append(asyncio.create_task(answer_events(self.events)))
        await self.control.request("POST", "/feedback")
        await self.control.request("RECORD", target)
        await self.queue(target, url)
        self.jobs.append(asyncio.create_task(keep_airplay_alive(self.control)))

    async def queue(self, target, url):
        stream = {
            "type": 130,
            "controlType": 1,
            "channelID": str(uuid.uuid4()).upper(),
            "clientUUID": str(uuid.uuid4()).upper(),
            "clientTypeUUID": "A6B27562-B43A-4F2D-B75F-82391E250194",
        }
        response = plistlib.loads(
            await self.control.request("SETUP", target, plist_bytes({"streams": [stream]}))
        )
        stream_id = str(response["streams"][0]["streamID"])
        headers = {
            "X-Apple-ProtocolVersion": "1",
            "X-Apple-Session-ID": self.session,
            "X-Apple-StreamID": stream_id,
        }
        item = {"uuid": str(uuid.uuid4()).upper(), "mediaType": "file", "Content-Location": url}
        for action in (
            {"type": "insertPlayQueueItem", "item": item},
            {"type": "setProperty", "property": "actionAtItemEnd", "value": 1},
            {"type": "setRate", "rate": 1.0},
        ):
            body = plist_bytes({"params": {"data": plist_bytes(action)}})
            await self.control.request("POST", "/command", body, extra=headers)

    async def closed(self):
        done, _ = await asyncio.wait(self.jobs, return_when=asyncio.FIRST_COMPLETED)
        for job in done:
            job.result()

    async def close(self):
        for job in self.jobs:
            job.cancel()
        if self.file:
            await self.file.close()
        if self.timing:
            self.timing.close()
        if self.events:
            self.events.writer.close()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        await self.control.close()


async def answer_events(stream):
    while True:
        _, headers, _ = await stream.headers()
        await stream.http("RTSP/1.0 200 OK", {"CSeq": headers.get("cseq", "0")})


async def keep_airplay_alive(control):
    while True:
        await asyncio.sleep(2)
        await control.request("POST", "/feedback")


class MediaSession:
    def __init__(self, control, update, trace):
        self.control = control
        self.update = update
        self.trace = trace
        self.data = None
        self.channels = []
        self.tasks = []
        self.pending = {}
        self.art_requests = deque(maxlen=128)
        self.outputs = {}
        self.route_update = asyncio.Event()
        self.state = NowPlaying()
        self.info = asyncio.Event()
        self.sequence = secrets.randbits(48)
        self.ready = False

    async def channel(self, port, salt, outbound, inbound):
        host = self.control.writer.get_extra_info("peername")[0]
        local = self.control.writer.get_extra_info("sockname")[0]
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, local_addr=(local, 0)), 5)
        stream = AirStream(reader, writer)
        self.channels.append(stream)
        stream.keys(self.control.secret, salt, outbound, inbound)
        return stream

    async def setup(self, controller):
        machine = hashlib.sha256(controller).digest()[:6].hex(":")
        local = self.control.writer.get_extra_info("sockname")[0]
        authority = f"[{local}]" if ":" in local else local
        target = f"rtsp://{authority}/{secrets.randbits(40)}"
        hello = {
            "isRemoteControlOnly": True,
            "sessionUUID": str(uuid.uuid4()).upper(),
            "timingProtocol": "None",
            "deviceID": machine,
            "macAddress": machine,
            "name": "OpenAVC",
            "model": "OpenAVC",
            "sourceVersion": "550.10",
            "osName": "OpenAVC",
            "osVersion": "1.0",
        }
        reply = plistlib.loads(await self.control.request("SETUP", target, plist_bytes(hello)))
        if reply.get("eventPort"):
            events = await self.channel(
                reply["eventPort"],
                "Events-Salt",
                "Events-Read-Encryption-Key",
                "Events-Write-Encryption-Key",
            )
            self.tasks.append(asyncio.create_task(answer_events(events)))
        await self.control.request("RECORD", target)
        seed = secrets.randbits(31)
        spec = {
            "type": 130,
            "controlType": 2,
            "seed": seed,
            "channelID": str(uuid.uuid4()).upper(),
            "clientUUID": str(uuid.uuid4()).upper(),
            "wantsDedicatedSocket": True,
            "clientTypeUUID": "1910A70F-DBC0-4242-AF95-115DB30604E1",
        }
        reply = plistlib.loads(await self.control.request("SETUP", target, plist_bytes({"streams": [spec]})))
        port = reply["streams"][0]["dataPort"]
        self.data = await self.channel(
            port,
            f"DataStream-Salt{seed}",
            "DataStream-Output-Encryption-Key",
            "DataStream-Input-Encryption-Key",
        )

    async def run(self, saved):
        try:
            controller = await self.control.verify(saved)
        except (InvalidTag, InvalidSignature, KeyError, ValueError) as exc:
            raise PairError("Use Show media PIN to renew the media pairing.") from exc
        await self.setup(controller)
        self.tasks.extend(
            [
                asyncio.create_task(self.listen()),
                asyncio.create_task(keep_airplay_alive(self.control)),
            ]
        )
        hello = proto(
            [
                (1, controller),
                (2, "OpenAVC"),
                (3, "Room controller"),
                (5, "dev.openavc.remote"),
                (6, "1"),
                (7, 1),
                (8, 108),
                (9, 1),
                (11, 1),
                (14, 1),
                (21, 1),
                (49, 1),
            ]
        )
        await self.send(15, 20, hello)
        await asyncio.wait_for(self.info.wait(), 5)
        await self.send(38, 42, proto([(1, 2)]))
        await self.send(16, 21, proto([(1, 1), (2, 1), (3, 1), (5, 1)]))
        self.credential = saved
        self.ready = True
        self.tasks.append(asyncio.create_task(self.track_position()))
        self.update({"media_status": "Connected", **self.state.snapshot()})
        await self.send(32, 37, proto([(1, 0), (2, 1), (3, 1)]))
        done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()

    async def track_position(self):
        while True:
            await asyncio.sleep(2)
            values = self.state.snapshot()
            if values["playback_state"] == "Playing":
                self.update({"position": values["position"]})

    async def send(self, kind, extension, body, identifier=None):
        envelope = [(1, kind), (extension, body)]
        if identifier:
            envelope.append((2, identifier))
        self.sequence += 1
        await self.data.messages(self.sequence, [proto(envelope)])
        self.trace("tx", f"Media Remote message {kind}")

    async def listen(self):
        while True:
            for packet in await self.data.read_messages():
                self.handle(packet)

    def handle(self, packet):
        kind = first(packet, 1, 0)
        self.trace("rx", f"Media Remote message {kind}")
        if kind in (15, 37):
            self.output_info(embedded(packet, 20), kind == 37)
        if kind == 15:
            self.info.set()
        identifier = string_field(packet, 2)
        if identifier not in self.art_requests and self.state.route(packet):
            self.update(self.state.snapshot())
        waiter = self.pending.get(identifier)
        if waiter and not waiter.done():
            waiter.set_result(packet)

    def output_info(self, device, partial):
        if partial and not {28, 29}.intersection(device):
            return
        members = [protobuf(raw) for raw in device.get(28, [])]
        self.outputs = {string_field(d, 19): string_field(d, 2) for d in members if first(d, 19)}
        if first(device, 29, 0) and not first(device, 24, 0):
            self.outputs[string_field(device, 1)] = string_field(device, 2)
        self.update({"audio_output": ", ".join(self.outputs.values())})
        self.route_update.set()

    async def command(self, command, options):
        supported = self.state.supported()
        if supported is not None and command not in supported:
            raise PeerError("The current player does not offer that control.")
        response = await self.request(1, 6, proto([(1, command), (2, proto(options))]))
        if first(embedded(response, 7), 1, 0):
            raise PeerError("Apple TV declined the playback command.")

    async def request(self, kind, extension, body, *, artwork=False):
        identifier = str(uuid.uuid4()).upper()
        if artwork:
            self.art_requests.append(identifier)
        waiter = asyncio.get_running_loop().create_future()
        self.pending[identifier] = waiter
        try:
            await self.send(kind, extension, body, identifier)
            response = await asyncio.wait_for(waiter, 6)
            if first(response, 4, 0):
                raise PeerError("The current player could not fulfill the request.")
            return response
        finally:
            self.pending.pop(identifier, None)
            waiter.cancel()

    async def artwork(self):
        key = self.state.item_key()
        if not key:
            raise PeerError("No active item to fetch artwork for.")
        app, player = key[0]
        path = proto(
            [
                (2, proto([(2, app)])),
                (3, proto([(1, player or "MediaRemote-DefaultPlayer")])),
            ]
        )
        current = self.state.current()
        request = [
            (1, current.location),
            (2, 1),
            (3, 1),
            (4, 400.0),
            (5, 400.0),
            (14, path),
        ]
        reply = await self.request(32, 37, proto(request), artwork=True)
        if key != self.state.item_key():
            raise PeerError("The item changed while its artwork was loading. Try again.")
        return current_artwork(reply, key[1])

    async def close(self):
        self.ready = False
        for task in self.tasks:
            task.cancel()
        for stream in self.channels:
            stream.writer.close()
        for waiter in self.pending.values():
            if not waiter.done():
                waiter.set_exception(ConnectionError("Media session disconnected"))
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.control.close()


KEYS = {
    "channel_up": 15,
    "channel_down": 16,
    "guide": 17,
    "page_up": 18,
    "page_down": 19,
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


def current_artwork(reply, identifier):
    queue = embedded(embedded(reply, 9), 3)
    for item in map(protobuf, queue.get(2, [])):
        if not identifier or string_field(item, 1) == identifier:
            return artwork_file(first(item, 3))
    raise PeerError("The current app did not return cover artwork.")


def artwork_file(data):
    signatures = [
        (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
        (b"\xff\xd8\xff", "jpg", "image/jpeg"),
        (b"GIF8", "gif", "image/gif"),
    ]
    for signature, extension, mime in signatures:
        if data.startswith(signature):
            return DeviceFile("apple-tv-cover." + extension, data, mime)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return DeviceFile("apple-tv-cover.webp", data, "image/webp")
    raise PeerError("The current app did not return supported cover artwork.")


def picker(names):
    pairs = sorted(names.items(), key=lambda item: (item[1].casefold(), item[0]))
    return json.dumps([{"label": label, "value": value} for value, label in pairs], ensure_ascii=False)


def volume_percent(value):
    if isinstance(value, (float, int)) and 0 <= value <= 1:
        return round(value * 100, 1)
    return None


def receiver_names(found):
    choices = {}
    for device in found.values():
        choices.update(receiver_options(device))
    return choices


def receiver_options(device):
    for entry in device.service_list():
        txt = {k.lower(): v for k, v in entry.txt_records.items()}
        service = (entry.service_type or "").rstrip(".").lower()
        if service == AIRPLAY.rstrip(".") and txt.get("psi"):
            yield txt["psi"], entry.instance_name or device.hostname or device.ip


def playback_command(command, params):
    if command == "seek":
        seconds = float(params["seconds"])
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Enter a nonnegative playback position.")
        return 45, [(9, seconds)]
    if command == "set_repeat":
        return 46, [(10, REPEAT[params["mode"]])]
    if command == "set_shuffle":
        return 47, [(11, SHUFFLE[params["mode"]])]
    return 4, []


def text_session_id(data):
    archive = plistlib.loads(data)
    session = archive["$top"]["sessionUUID"]
    if isinstance(session, plistlib.UID):
        session = archive["$objects"][session.data]
    if isinstance(session, dict):
        session = session.get("NS.uuidbytes")
    if not isinstance(session, bytes) or len(session) != 16:
        raise PeerError("Apple TV did not provide a writable keyboard session.")
    return session


def text_archive(session, text):
    objects = ["$null"]

    def ref(value):
        objects.append(value)
        return plistlib.UID(len(objects) - 1)

    def instance(name, attrs):
        kind = ref({"$classname": name, "$classes": [name, "NSObject"]})
        return ref({"$class": kind, **attrs})

    target = instance("NSUUID", {"NS.uuidbytes": session})
    output = instance("TIKeyboardOutput", {"insertionText": ref(text)} if text is not None else {})
    attrs = {"targetSessionUUID": target, "keyboardOutput": output}
    if text is None:
        attrs["textToAssert"] = ref("")
    top = instance("RTITextOperations", attrs)
    return plist_bytes(
        {
            "$version": 100000,
            "$archiver": "RTIKeyedArchiver",
            "$objects": objects,
            "$top": {"textOperations": top},
        }
    )


class AppleTVDriver(BaseDriver):
    DRIVER_INFO = {
        "id": "apple_tv",
        "name": "Apple TV",
        "manufacturer": "Apple",
        "category": "streaming",
        "version": "1.1.0",
        "min_platform_version": "0.40.0",
        "author": "OpenAVC",
        "description": "Pair directly with tvOS for room control, text entry, app and profile selection. "
        "Optional media pairing adds live Now Playing feedback and playback controls.",
        "source_url": "https://pyatv.dev/documentation/protocols/#companion",
        "tags": ["apple", "streaming", "media-player", "tvos"],
        "transport": "tcp",
        "protocols": ["companion_link", "airplay", "media_remote"],
        "verified": False,
        "simulated": True,
        "web_ui": False,
        "compatible_models": [
            {
                "manufacturer": "Apple",
                "models": ["Apple TV HD", "Apple TV 4K"],
                "confidence": "untested",
                "notes": "Requires tvOS with the Companion Link remote service. Simulator and "
                "protocol interoperability tested.",
            }
        ],
        "help": {
            "overview": "Controls Apple TV HD and Apple TV 4K directly from OpenAVC. Give each room its "
            "own Apple TV device. Apps come from that TV, so an integrator can pick an "
            "installed app by name.",
            "setup": "Add the Apple TV by discovery or enter its address. Keep its display visible. Under "
            "Actions choose 1. Show pairing PIN, then 2. Enter pairing PIN. Enter all four "
            "digits shown on that TV, including leading zeros. OpenAVC saves the verified "
            "pairing on this controller and reconnects. To replace a TV or restore onto a "
            "different controller, repeat both steps. If Bonjour cannot cross your VLANs, enter "
            "the TCP port advertised for _companion-link._tcp in Port override. Apple TV "
            "advertises this port dynamically. Playback and volume buttons depend on the active "
            "app and audio route. In the built-in simulator the PIN is 1234. Some tvOS versions "
            "first report power when it changes. For Now Playing, leave the remote paired and "
            "run Show media PIN, then Enter media PIN. The second pairing uses the AirPlay "
            "service on the same TV. Leave Media port at 0 for Bonjour or enter its advertised "
            "port across VLANs. Bind Track title, Playing app, Playback state and Position to "
            "panel elements. Apps decide which fields, commands and cover images they supply.",
        },
        "discovery": {
            "mdns": [
                {
                    "service": "_companion-link._tcp.local.",
                    "txt": {"rpMd": "AppleTV5,3"},
                },
                {
                    "service": "_companion-link._tcp.local.",
                    "txt": {"rpMd": "AppleTV6,2"},
                },
                {
                    "service": "_companion-link._tcp.local.",
                    "txt": {"rpMd": "AppleTV11,1"},
                },
                {
                    "service": "_companion-link._tcp.local.",
                    "txt": {"rpMd": "AppleTV14,1"},
                },
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
            "media_key": {
                "type": "password",
                "label": "Saved media pairing",
                "advanced": True,
                "secret": True,
                "description": "Filled by Enter media PIN on this controller.",
            },
            "media_port": {
                "type": "integer",
                "default": 0,
                "min": 0,
                "max": 65535,
                "advanced": True,
                "label": "Media port override",
                "description": "AirPlay TCP port. Leave at 0 to resolve the current port through Bonjour.",
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
            "track_title": {"type": "string", "label": "Track title"},
            "track_artist": {"type": "string", "label": "Artist"},
            "track_album": {"type": "string", "label": "Album"},
            "media_app": {"type": "string", "label": "Playing app"},
            "playback_state": {"type": "string", "label": "Playback state"},
            "series_name": {"type": "string", "label": "Series"},
            "media_status": {"type": "string", "label": "Media connection"},
            "repeat_mode": {"type": "string", "label": "Repeat mode"},
            "shuffle_mode": {"type": "string", "label": "Shuffle mode"},
            "profile_options": {"type": "string", "label": "Available profiles"},
            "keyboard_focus": {"type": "string", "label": "Keyboard focus"},
            "audio_output": {"type": "string", "label": "Selected audio outputs"},
            "output_options": {"type": "string", "label": "Available audio outputs"},
            "position": {
                "type": "number",
                "label": "Playback position",
                "unit": "s",
                "cloud_priority": "low",
            },
            "duration": {
                "type": "number",
                "label": "Item duration",
                "unit": "s",
                "cloud_priority": "low",
            },
            "volume_level": {
                "type": "number",
                "label": "Volume level",
                "unit": "%",
                "cloud_priority": "low",
            },
            "queue_count": {"type": "integer", "label": "Items in reported queue"},
            "episode_number": {"type": "integer", "label": "Episode"},
            "season_number": {"type": "integer", "label": "Season"},
            "track_genre": {"type": "string", "label": "Genre"},
            "can_stop": {"type": "boolean", "label": "Stop available"},
            "can_seek": {"type": "boolean", "label": "Seek available"},
            "can_repeat": {"type": "boolean", "label": "Repeat control available"},
            "can_shuffle": {"type": "boolean", "label": "Shuffle control available"},
            "artwork_available": {
                "type": "boolean",
                "label": "Cover artwork available",
            },
            "absolute_volume": {
                "type": "boolean",
                "label": "Absolute volume available",
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
            "home_hold": {
                "label": "Hold TV / Home",
                "help": "Open tvOS Control Center.",
            },
            "wake": {
                "help": "Wake the Apple TV.",
                "label": "Wake Apple TV",
                "sets": {"power": "On"},
            },
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
            "volume_up": {
                "help": "Raise the volume on a supported audio route.",
                "label": "Volume up",
            },
            "volume_down": {
                "help": "Lower the volume on a supported audio route.",
                "label": "Volume down",
            },
            "play_pause": {
                "help": "Pause playing media or resume paused media.",
                "label": "Play / pause",
            },
            "play": {
                "help": "Start or resume playback in the active app.",
                "label": "Play",
            },
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
                    "seconds": {
                        "type": "integer",
                        "label": "Seconds",
                        "min": 1,
                        "max": 600,
                        "default": 10,
                    }
                },
            },
            "skip_back": {
                "label": "Skip back",
                "help": "Move back in the playing item when the app supports seeking.",
                "params": {
                    "seconds": {
                        "type": "integer",
                        "label": "Seconds",
                        "min": 1,
                        "max": 600,
                        "default": 10,
                    }
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
            "stop": {
                "label": "Stop playback",
                "help": "Stop playback if the active app offers Stop.",
            },
            "seek": {
                "label": "Seek to position",
                "help": "Move to an absolute position in the current item.",
                "params": {
                    "seconds": {
                        "type": "number",
                        "label": "Seconds",
                        "required": True,
                        "min": 0,
                    }
                },
            },
            "set_repeat": {
                "label": "Set repeat",
                "help": "Change repeat mode in the active player.",
                "params": {
                    "mode": {
                        "type": "enum",
                        "label": "Repeat",
                        "required": True,
                        "values": ["Off", "One", "All"],
                    }
                },
            },
            "set_shuffle": {
                "label": "Set shuffle",
                "help": "Change shuffle mode in the active player.",
                "params": {
                    "mode": {
                        "type": "enum",
                        "label": "Shuffle",
                        "required": True,
                        "values": ["Off", "Albums", "Songs"],
                    }
                },
            },
            "play_file": {
                "label": "Play a local media file",
                "help": "Choose a movie or audio file on this computer. Apple TV retrieves it from the controller. The TV must be able to reach the controller on the control network.",
                "params": {
                    "file": {
                        "type": "file",
                        "label": "Movie or audio file",
                        "required": True,
                        "max_bytes": 1073741824,
                    }
                },
            },
            "play_url": {
                "label": "Play media URL",
                "help": "Ask the TV to play a receiver-accessible HTTP(S) media URL. HLS is the best documented format.",
                "params": {"url": {"type": "string", "label": "Media URL", "required": True}},
            },
            "download_artwork": {
                "label": "Download current artwork",
                "help": "Save the cover supplied by the currently playing app to this computer.",
                "returns_file": True,
            },
            "refresh_media": {
                "label": "Read Now Playing",
                "help": "Request the selected player’s current item and refresh the panel fields.",
                "query_for": "track_title",
            },
            "switch_profile": {
                "label": "Switch profile",
                "help": "Switch to a tvOS user profile listed by this TV.",
                "params": {
                    "profile": {
                        "type": "string",
                        "label": "Profile",
                        "required": True,
                        "options_state": "profile_options",
                    }
                },
            },
            "refresh_profiles": {
                "label": "Refresh profiles",
                "help": "Read the available user profiles from the TV.",
                "query_for": "profile_options",
            },
            "open_link": {
                "label": "Open app link",
                "help": "Open a link registered to an installed app, such as a link to a particular show.",
                "params": {"url": {"type": "string", "label": "App link", "required": True}},
            },
            "set_volume": {
                "label": "Set volume level",
                "help": "Set a volume percentage on an audio route that reports absolute volume.",
                "params": {
                    "level": {
                        "type": "number",
                        "label": "Volume (%)",
                        "required": True,
                        "min": 0,
                        "max": 100,
                    }
                },
            },
            "read_volume": {
                "label": "Read volume",
                "help": "Read the absolute volume of the current audio route when supported.",
                "query_for": "volume_level",
            },
            "channel_up": {
                "label": "Channel up",
                "help": "Send channel up to an app that supports this remote key.",
            },
            "channel_down": {
                "label": "Channel down",
                "help": "Send channel down to an app that supports this remote key.",
            },
            "guide": {
                "label": "Program guide",
                "help": "Send program guide to an app that supports this remote key.",
            },
            "page_up": {
                "label": "Page up",
                "help": "Send page up to an app that supports this remote key.",
            },
            "page_down": {
                "label": "Page down",
                "help": "Send page down to an app that supports this remote key.",
            },
            "type_text": {
                "label": "Insert text",
                "help": "Insert text in the field currently focused on the Apple TV.",
                "params": {
                    "text": {
                        "type": "string",
                        "label": "Text",
                        "required": True,
                        "trim": False,
                        "secret": True,
                    }
                },
            },
            "replace_text": {
                "label": "Replace text",
                "help": "Replace text in the field currently focused on the Apple TV.",
                "params": {
                    "text": {
                        "type": "string",
                        "label": "Text",
                        "required": True,
                        "trim": False,
                        "secret": True,
                    }
                },
            },
            "clear_text": {
                "label": "Clear text",
                "confirm": "Clear all text in the current Apple TV field?",
                "help": "Clear text in the field currently focused on the Apple TV.",
            },
            "swipe": {
                "label": "Swipe touch surface",
                "confirm": False,
                "help": "Swipe once across the remote touch surface.",
                "params": {
                    "direction": {
                        "type": "enum",
                        "label": "Direction",
                        "required": True,
                        "values": ["Up", "Down", "Left", "Right"],
                    }
                },
            },
            "press_key": {
                "label": "Press remote key",
                "help": "Tap, double-tap or hold a remote button.",
                "params": {
                    "key": {
                        "type": "enum",
                        "label": "Button",
                        "required": True,
                        "values": [
                            "Up",
                            "Down",
                            "Left",
                            "Right",
                            "Select",
                            "Back",
                            "Home",
                            "Play/Pause",
                        ],
                    },
                    "gesture": {
                        "type": "enum",
                        "label": "Gesture",
                        "required": True,
                        "values": ["Tap", "Double tap", "Hold"],
                    },
                },
            },
            "refresh_outputs": {
                "label": "Find audio outputs",
                "help": "Discover named AirPlay receivers on the control network.",
                "query_for": "output_options",
            },
            "add_output": {
                "label": "Add audio output",
                "help": "Add audio output for Apple TV playback using a discovered receiver.",
                "params": {
                    "output": {
                        "type": "string",
                        "label": "Audio output",
                        "required": True,
                        "options_state": "output_options",
                    }
                },
            },
            "remove_output": {
                "label": "Remove audio output",
                "confirm": False,
                "help": "Remove audio output for Apple TV playback using a discovered receiver.",
                "params": {
                    "output": {
                        "type": "string",
                        "label": "Audio output",
                        "required": True,
                        "options_state": "output_options",
                    }
                },
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
            {
                "id": "start_media_pairing",
                "kind": "setup",
                "availability": "always",
                "label": "3. Show media PIN (optional)",
                "icon": "monitor",
            },
            {
                "id": "finish_media_pairing",
                "kind": "setup",
                "availability": "always",
                "label": "4. Enter media PIN",
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
            {"id": "refresh_media"},
            {"id": "switch_profile"},
            {"id": "type_text"},
            {"id": "set_volume"},
            {"id": "refresh_outputs"},
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
        self._profiles = {}
        self._outputs = {}
        self._media = None
        self._media_job = None
        self._url_job = None
        self._url_lock = asyncio.Lock()
        self._volume_job = None
        self._flags = 0
        self._touch_origin = None

    def _simulating(self):
        endpoint = ("127.0.0.1", int(self.config.get("port") or 0))
        return self.config.get("host") == endpoint[0] and getattr(self, "udp_redirect", None) == endpoint

    @staticmethod
    def _advertised(device, target, service=SERVICE):
        for entry in device.service_list():
            names = (
                device.ip,
                device.hostname,
                device.address_name,
                entry.target,
                entry.instance_name,
            )
            kind = (entry.service_type or "").rstrip(".").lower()
            if kind != service.rstrip("."):
                continue
            if target in {n.rstrip(".").casefold() for n in names if n} and entry.port:
                return entry.port
        return None

    async def _resolve(self, host, local, service=SERVICE):
        scanner = MDNSScanner(control_ip=local or "", service_types=[service])
        devices = await scanner.start(duration=2.5)
        target = host.rstrip(".").casefold()
        for device in devices.values():
            port = self._advertised(device, target, service)
            if port:
                return device.ip, port
        raise ConnectionError(
            f"Could not find {service} for this Apple TV. Check Bonjour or enter its advertised port."
        )

    async def _open(self, media=False):
        host = str(self.config.get("host") or "").strip()
        if not host:
            raise ValueError("Enter the Apple TV address.")
        setting = "media_port" if media and not self._simulating() else "port"
        port = int(self.config.get(setting) or 0)
        local = self.local_address(host)
        if not port:
            host, port = await self._resolve(host, local, AIRPLAY if media else SERVICE)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, local_addr=(local, 0) if local else None),
            5,
        )
        carrier = AirControl if media else Wire
        return carrier(reader, writer, self.record_traffic)

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
                code="auth_failed",
                message="Apple TV could not verify the saved pairing. Pair this TV again.",
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
            "_sessionStart",
            {"_srvT": "com.apple.tvremoteservices", "_sid": secrets.randbits(32)},
        )
        try:
            await wire.message("TVRCSessionStart", {"ProtocolVersionKey": "1.2"}, timeout=2)
        except (PeerError, TimeoutError):
            pass  # Older tvOS does not register this service.
        self.set_states({"power": "Unknown", "attention": "Unknown"})
        await wire.message(
            "_interest",
            {"_regEvents": ["SystemStatus", "TVSystemStatus", "_iMC"]},
            event=True,
        )

    async def _initial_sync(self):
        try:
            await self.poll()
        except TimeoutError:
            pass  # Some versions publish attention changes without answering a snapshot.
        try:
            await self._refresh_apps()
        except (PeerError, TimeoutError):
            self.set_state("app_options", "[]")

        await self._optional(self._refresh_profiles)
        await self._optional(self._read_focus)
        self._media_job = asyncio.create_task(self._maintain_media())

    async def _optional(self, query):
        try:
            await query()
        except (PeerError, TimeoutError):
            pass

    async def _media_attempt(self, saved):
        control = await self._open(media=True)
        media = MediaSession(control, self.set_states, self.record_traffic)
        self._media = media
        try:
            if self._simulating():
                challenge = await control.auth(3, 1, {0: bytes([0])})
                saved = await control.finish_pin("1234", challenge)
                await control.close()
                media.control = await self._open(media=True)
            await media.run(saved)
        finally:
            self._media = None
            await media.close()

    async def _maintain_media(self):
        saved = self.config.get("media_key")
        if not saved and not self._simulating():
            self.set_state("media_status", "Not paired")
            return
        delay = 2
        while True:
            self.set_state("media_status", "Connecting")
            try:
                await self._media_attempt(saved)
            except (PairError, InvalidSignature):
                self.set_states({**MEDIA_EMPTY, "audio_output": "", "media_status": "Pair again"})
                return
            except (
                OSError,
                EOFError,
                ValueError,
                KeyError,
                IndexError,
                InvalidTag,
                plistlib.InvalidFileException,
            ):
                self.set_states({**MEDIA_EMPTY, "audio_output": "", "media_status": "Reconnecting"})
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    def _event(self, name, body):
        if name == "_iMC":
            self._volume_flags(body.get("_mcF", 0))
        elif name in ("_tiStarted", "_tiStopped"):
            self.set_state("keyboard_focus", "Focused" if name == "_tiStarted" else "Not focused")
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
        tasks = [job for job in (self._media_job, self._volume_job, self._url_job) if job]
        for job in tasks:
            job.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._media_job = self._volume_job = self._url_job = self._touch_origin = None
        self._flags = 0
        self._profiles.clear()
        self._outputs.clear()
        self.set_states(
            {
                **MEDIA_EMPTY,
                "audio_output": "",
                "media_status": "Offline",
                "keyboard_focus": "Unknown",
                "volume_level": None,
                "absolute_volume": False,
                "profile_options": "[]",
                "output_options": "[]",
            }
        )
        wire, self._wire = self._wire, None
        if wire:
            await wire.close()
        self._apps.clear()
        self.set_states({"power": "Unknown", "attention": "Unknown", "app_options": "[]"})

    async def send_command(self, command, params=None):
        params = params or {}
        queries = {
            "read_power": self.poll,
            "refresh_apps": self._refresh_apps,
            "refresh_profiles": self._refresh_profiles,
            "read_volume": self._read_volume,
            "refresh_media": self._refresh_media,
            "download_artwork": self._artwork,
            "refresh_outputs": self._refresh_outputs,
        }
        if command in queries:
            return await queries[command]()
        if command in MEDIA:
            return await self._wire.message("_mcc", {"_mcc": MEDIA[command]})
        handlers = {
            "launch_app": self._launch,
            "switch_profile": self._profile,
            "open_link": self._link,
            "play_url": self._play_url,
            "play_file": self._play_file,
            "set_volume": self._set_volume,
            "press_key": self._press,
            "swipe": self._swipe,
        }
        if command in handlers:
            return await handlers[command](params)
        groups = {
            "skip_forward": self._skip,
            "skip_back": self._skip,
            "type_text": self._text,
            "replace_text": self._text,
            "clear_text": self._text,
            "stop": self._playback,
            "seek": self._playback,
            "set_repeat": self._playback,
            "set_shuffle": self._playback,
            "add_output": self._route_output,
            "remove_output": self._route_output,
        }
        if command in groups:
            return await groups[command](command, params)
        return await self._button(command)

    async def _refresh_profiles(self):
        accounts = await self._wire.message("FetchUserAccountsEvent", {}, timeout=2)
        self._profiles = {k: v for k, v in accounts.items() if isinstance(v, str)}
        self.set_state("profile_options", picker(self._profiles))

    async def _profile(self, params):
        identifier = params.get("profile")
        if identifier not in self._profiles:
            raise ValueError("Refresh profiles, then choose a profile on this TV.")
        await self._wire.message("SwitchUserAccountEvent", {"SwitchAccountID": identifier})

    async def _link(self, params):
        link = str(params.get("url", "")).strip()
        if not urlsplit(link).scheme or len(link) > 4096:
            raise ValueError("Enter a complete app link.")
        await self._wire.message("_launchApp", {"_urlS": link})

    def _volume_flags(self, flags):
        self._flags = flags if isinstance(flags, int) else 0
        available = bool(self._flags & 256)
        self.set_state("absolute_volume", available)
        if not available:
            self.set_state("volume_level", None)
        elif not self._volume_job or self._volume_job.done():
            self._volume_job = asyncio.create_task(self._background_volume())

    async def _background_volume(self):
        try:
            await self._read_volume()
        except (OSError, ValueError, EOFError):
            self.set_state("volume_level", None)

    async def _read_volume(self):
        if not self._flags & 256:
            raise PeerError("This audio route has not advertised absolute volume.")
        reply = await self._wire.message("_mcc", {"_mcc": 5}, timeout=2)
        level = reply.get("_vol")
        if self._flags & 256:
            self.set_state("volume_level", volume_percent(level))

    async def _set_volume(self, params):
        level = float(params["level"])
        if not 0 <= level <= 100:
            raise ValueError("Choose a volume from 0 to 100.")
        if not self._flags & 256:
            raise PeerError("This audio route does not offer absolute volume. Use the volume buttons.")
        await self._wire.message("_mcc", {"_mcc": 6, "_vol": level / 100})
        await self._read_volume()

    async def _read_focus(self):
        reply = await self._wire.message("_tiStart", {}, timeout=2)
        value = reply.get("_tiD")
        self.set_state("keyboard_focus", "Focused" if value else "Not focused")
        return value

    async def _text(self, command, params):
        text = str(params.get("text", ""))
        if len(text.encode()) > 8192:
            raise ValueError("Enter a shorter text value.")
        async with self._keys:
            await self._wire.message("_tiStop", {})
            archive = await self._read_focus()
            if not archive:
                raise PeerError("Select a text field on the Apple TV first.")
            session = text_session_id(archive)
            if command in ("clear_text", "replace_text"):
                await self._text_operation(session, None)
            if command != "clear_text" and text:
                await self._text_operation(session, text)

    async def _text_operation(self, session, text):
        await self._wire.message("_tiC", {"_tiV": 1, "_tiD": text_archive(session, text)}, event=True)

    async def _press(self, params):
        key = params["key"].lower().replace("/", "_")
        gesture = params["gesture"]
        if gesture not in ("Tap", "Double tap", "Hold"):
            raise ValueError("Choose a supported press gesture.")
        await self._button(key, gesture)

    async def _swipe(self, params):
        dx, dy = {"Up": (0, -1), "Down": (0, 1), "Left": (-1, 0), "Right": (1, 0)}[params["direction"]]
        async with self._keys:
            wire = self._wire
            if self._touch_origin is None:
                await wire.message("_touchStart", {"_height": 1000, "_width": 1000, "_tFl": 0})
                self._touch_origin = time.monotonic_ns()

            async def touch(phase, offset):
                await wire.message(
                    "_hidT",
                    {
                        "_cx": 500 + dx * offset,
                        "_cy": 500 + dy * offset,
                        "_tFg": 1,
                        "_tPh": phase,
                        "_ns": time.monotonic_ns() - self._touch_origin,
                    },
                    event=True,
                )

            await touch(1, -300)
            try:
                for offset in range(-250, 300, 50):
                    await asyncio.sleep(0.016)
                    await touch(3, offset)
            finally:
                await touch(4, 300)

    def _media_ready(self):
        if not self._media or not self._media.ready:
            raise PeerError("Now Playing is unavailable. Check Media connection or pair with Show media PIN.")
        return self._media

    async def _play_url(self, params):
        raw = str(params.get("url", "")).strip()
        parsed = urlsplit(raw)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            raise ValueError("Enter a complete HTTP or HTTPS media URL that the Apple TV can reach.")
        await self._start_playback(raw)

    async def _play_file(self, params):
        file = params.get("file")
        if not isinstance(file, CommandFile):
            raise ValueError("Choose a media file on the device page.")
        await self._start_playback(file)

    async def _start_playback(self, source):
        credential = self._media_ready().credential
        async with self._url_lock:
            if self._url_job:
                self._url_job.cancel()
                await asyncio.gather(self._url_job, return_exceptions=True)
            player = URLPlayback(await self._open(media=True))
            try:
                await player.open(credential, source)
            except BaseException:
                await player.close()
                raise
            self._url_job = asyncio.create_task(self._end_playback(player))

    async def _end_playback(self, player):
        try:
            async with asyncio.timeout(4 * 3600 if player.file else None):
                await player.closed()
        except (OSError, EOFError, ValueError, InvalidTag):
            pass
        finally:
            await player.close()

    async def _artwork(self):
        return await self._media_ready().artwork()

    async def _refresh_media(self):
        await self._media_ready().send(32, 37, proto([(1, 0), (2, 1), (3, 1)]))

    async def _playback(self, command, params):
        session = self._media_ready()
        code, options = playback_command(command, params)
        await session.command(code, options)
        if command == "stop" and self._url_job:
            self._url_job.cancel()
            await asyncio.gather(self._url_job, return_exceptions=True)

    async def _refresh_outputs(self):
        session = self._media_ready()
        if self._simulating():
            self._outputs = {"ROOM-SPEAKER": "Room speaker"}
        else:
            local = self.local_address(self.config["host"])
            found = await MDNSScanner(control_ip=local or "", service_types=[AIRPLAY]).start(duration=2.5)
            self._outputs = receiver_names(found)
        self._outputs.update(session.outputs)
        self.set_state("output_options", picker(self._outputs))

    async def _route_output(self, command, params):
        session = self._media_ready()
        output = params.get("output")
        if output not in self._outputs:
            raise ValueError("Find audio outputs and choose a receiver by name.")
        adding = command == "add_output"
        async with self._keys:
            if (output in session.outputs) == adding:
                return
            fields = (2, 5) if adding else (3, 6)
            await session.send(
                48,
                52,
                proto([(1, 1), *((n, output) for n in fields)]),
                str(uuid.uuid4()).upper(),
            )
            async with asyncio.timeout(6):
                while (output in session.outputs) != adding:
                    session.route_update.clear()
                    await session.route_update.wait()

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

    async def _button(self, command, gesture="Tap"):
        hold = command == "home_hold" or gesture == "Hold"
        key = KEYS.get("home" if command == "home_hold" else command)
        if key is None:
            raise ValueError(f"Unknown Apple TV command: {command}")
        async with self._keys:
            wire = self._wire
            await self._tap(wire, key, hold)
            if gesture == "Double tap":
                await asyncio.sleep(0.1)
                await self._tap(wire, key, False)

    async def _tap(self, wire, key, hold):
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
            wire, _, _ = self._pair
            wire.writer.close()
            self._pair = None

    async def run_setup_action(self, action_id, params, progress):
        media = action_id in ("start_media_pairing", "finish_media_pairing")
        if action_id in ("start_pairing", "start_media_pairing"):
            return await self._start_pin(progress, media)
        if action_id in ("finish_pairing", "finish_media_pairing"):
            return await self._finish_pin(params, progress, media)
        raise ValueError(f"Unknown Apple TV setup action: {action_id}")

    async def _start_pin(self, progress, media=False):
        self._discard_pin()
        await progress("Requesting a PIN on the Apple TV…", 10)
        wire = await self._open(media=media)
        try:
            challenge = await wire.auth(3, 1, {0: b"\0"}, _pwTy=1)
        except BaseException:
            await wire.close()
            raise
        self._pair = (wire, challenge, media)
        self._expiry = asyncio.get_running_loop().call_later(120, self._discard_pin)
        return {
            "message": "Read the PIN on the TV, then choose Enter media PIN within two minutes."
            if media
            else "Read the PIN on the TV, then choose Enter pairing PIN within two minutes."
        }

    def _claim_pin(self, params, media):
        pin = str(params.get("pin") or "").strip()
        if not re.fullmatch(r"[0-9]{4}", pin):
            raise ValueError("Enter all four digits displayed on the Apple TV.")
        if not self._pair:
            raise ValueError("Request a fresh PIN using the matching Show PIN action.")
        wire, challenge, requested_media = self._pair
        if requested_media != media:
            raise ValueError("Choose the Enter PIN action for the pairing you started.")
        self._pair = None
        self._discard_pin()
        return pin, wire, challenge

    async def _finish_pin(self, params, progress, media=False):
        pin, wire, challenge = self._claim_pin(params, media)
        try:
            await progress("Verifying the PIN with the Apple TV…", 25)
            saved = await wire.finish_pin(pin, challenge)
        except (InvalidSignature, InvalidTag, KeyError) as exc:
            raise PairError("Apple TV could not confirm its pairing identity. Request a fresh PIN.") from exc
        finally:
            await wire.close()
        wire = await self._open(media=media)
        try:
            await wire.verify(saved)
        finally:
            await wire.close()
        await progress("Pairing verified. Saving on this controller…", 85)
        if not self._simulating():
            await self.request_config_update({"media_key" if media else "pairing_key": saved})
        await self.request_reconnect()
        return {"message": "Paired with Apple TV."}
