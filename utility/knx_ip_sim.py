"""Loopback KNXnet/IP interface, implemented from the KNX V3 wire formats.

Core 03/08/02 §§7.5,7.8; Tunnelling 03/08/04 §§2.6,5.4; cEMI 03/06/03
§4.1.5; IP Secure 03/08/09 §§2.2.1,2.2.3 and Annex A. Wire framing and
session crypto do not import the driver's implementation or XKNX codecs.
This exercises integration behavior; it is not physical KNX certification.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import struct
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from defusedxml import ElementTree as SafeXML
from xknx.secure.keyring import InterfaceType, sync_load_keyring

from openavc.simulator.tcp_simulator import TCPSimulator


def frame(service, body):
    return b"\x06\x10" + struct.pack(">HH", service, len(body) + 6) + body


def hpai(port=0, tcp=False):
    return bytes((8, 2 if tcp else 1)) + (bytes(4) if tcp else b"\x7f\0\0\1") + port.to_bytes(2, "big")


def number(value):
    parts = [int(part) for part in str(value).split("/")]
    if len(parts) == 3:
        return (parts[0] << 11) | (parts[1] << 8) | parts[2]
    if len(parts) == 2:
        return (parts[0] << 11) | parts[1]
    return parts[0]


def cbc_mac(key, associated, payload=b"", first=bytes(16)):
    data = first + len(associated).to_bytes(2, "big") + associated + payload
    data += bytes((-len(data)) % 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
    return (cipher.update(data) + cipher.finalize())[-16:]


def crypt(key, counter, data):
    cipher = Cipher(algorithms.AES(key), modes.CTR(counter)).encryptor()
    return cipher.update(data) + cipher.finalize()


def password_key(password, purpose):
    salt = (purpose + ".1.secure.ip.knx.org").encode("ascii")
    return hashlib.pbkdf2_hmac("sha256", password.encode("latin-1"), salt, 65536, 16)


class Session:
    """One secure TCP connection with independent transmit/receive counters."""
    def __init__(self, device_password, user_password, user_id):
        self.device_key = password_key(device_password, "device-authentication-code")
        self.user_key = password_key(user_password, "user-password")
        self.user_id = user_id
        self.private = X25519PrivateKey.generate()
        self.public = self.private.public_key().public_bytes_raw()
        self.key = None
        self.xor_keys = None
        self.tx = 0
        self.rx = -1
        self.authenticated = False

    def response(self, request):
        if len(request) != 40:
            raise ValueError("Invalid session request")
        client = request[8:]
        self.xor_keys = bytes(a ^ b for a, b in zip(client, self.public))
        self.key = hashlib.sha256(self.private.exchange(X25519PublicKey.from_public_bytes(client))).digest()[:16]
        header = bytes.fromhex("0610095200380001")
        mac = cbc_mac(self.device_key, header + self.xor_keys)
        return frame(0x0952, b"\0\1" + self.public + crypt(self.device_key, bytes(14) + b"\xff\0", mac))

    def authenticate(self, plain, reject=False):
        expected = crypt(self.user_key, bytes(14) + b"\xff\0", cbc_mac(self.user_key, plain[:8] + self.xor_keys))
        valid = len(plain) == 24 and plain[7] == self.user_id and hmac.compare_digest(plain[8:], expected)
        self.authenticated = valid and not reject
        return frame(0x0954, bytes((0 if self.authenticated else 1, 0)))

    def wrap(self, plain):
        nonce = self.tx.to_bytes(6, "big") + bytes.fromhex("00fa00000001") + bytes(2)
        self.tx += 1
        header = bytes.fromhex("06100950") + (len(plain) + 38).to_bytes(2, "big") + b"\0\1"
        mac = cbc_mac(self.key, header, plain, nonce + len(plain).to_bytes(2, "big"))
        encrypted = crypt(self.key, nonce + b"\xff\0", mac + plain)
        return header + nonce + encrypted[16:] + encrypted[:16]

    def unwrap(self, packet):
        if len(packet) < 44 or packet[6:8] != b"\0\1":
            raise ValueError("Invalid secure wrapper")
        sequence = int.from_bytes(packet[8:14], "big")
        if sequence <= self.rx:
            raise ValueError("Repeated secure wrapper")
        nonce = packet[8:22]
        decoded = crypt(self.key, nonce + b"\xff\0", packet[-16:] + packet[22:-16])
        mac, plain = decoded[:16], decoded[16:]
        expected = cbc_mac(self.key, packet[:8], plain, nonce + len(plain).to_bytes(2, "big"))
        if not hmac.compare_digest(mac, expected):
            raise ValueError("Wrong secure wrapper MAC")
        self.rx = sequence
        return plain


def encode(dpt, value):
    if dpt.startswith("1."):
        return int(bool(value)), True
    if dpt == "5.001":
        return bytes((round(float(value) * 255 / 100),)), False
    if dpt.startswith("9."):
        mantissa, exponent = round(float(value) * 100), 0
        while not -2048 <= mantissa <= 2047:
            mantissa = round(mantissa / 2)
            exponent += 1
        encoded = (0x8000 if mantissa < 0 else 0) | (exponent << 11) | (mantissa & 0x7FF)
        return encoded.to_bytes(2, "big"), False
    size = 2 if dpt.startswith("7.") else 4
    return int(value).to_bytes(size, "big", signed=dpt.startswith("13.")), False


def indication(address, dpt, value, source=0x110A):
    data, compact = encode(dpt, value)
    apdu = bytes((0, 0x40 | data)) if compact else b"\0\x40" + data
    return b"\x29\0\xbc\xe0" + source.to_bytes(2, "big") + number(address).to_bytes(2, "big") + bytes((len(apdu) - 1,)) + apdu


def group_cipher(key, header, sequence, plain):
    """Application Layer §5.1.3.2: CCM encryption, four-byte MAC."""
    nonce = sequence + header[4:8]
    first = nonce + b"\0\x80\x03\xf1\0" + bytes((len(plain),))
    mac = cbc_mac(key, b"\x10", plain, first)[:4]
    encrypted = crypt(key, nonce + bytes.fromhex("000000000100"), mac + plain)
    apdu = b"\x03\xf1\x10" + sequence + encrypted[4:] + encrypted[:4]
    return header[:8] + bytes((len(apdu) - 1,)) + apdu


class GroupSecurity:
    def __init__(self, config):
        self.keyring = None
        self.keys = {}
        self.rx = {}
        self.tx = int(time.time() * 1000)
        self.source = 0x110A
        self.interface = None
        encoded = config.get("keyring_data")
        if not encoded:
            return
        data = base64.b64decode(encoded, validate=True)
        SafeXML.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        with tempfile.TemporaryDirectory(prefix="knx-sim-") as folder:
            path = Path(folder) / "keys.knxkeys"
            path.write_bytes(data)
            path.chmod(0o600)
            self.keyring = sync_load_keyring(path, str(config.get("keyring_password", "")))
        self.keys = {address.raw: key for address, key in self.keyring.get_data_secure_group_keys().items()}
        self.interface = next((item for item in self.keyring.interfaces
                               if item.type == InterfaceType.TUNNELING), None)
        senders = self.keyring.get_data_secure_senders()
        if senders:
            self.source = next(iter(senders)).raw
            self.tx = max(self.tx, max(senders.values()) + 1)

    def secure(self, cemi):
        address = int.from_bytes(cemi[6:8], "big")
        if address not in self.keys:
            return cemi
        self.tx += 1
        header = cemi[:4] + self.source.to_bytes(2, "big") + cemi[6:9]
        return group_cipher(self.keys[address], header, self.tx.to_bytes(6, "big"), cemi[9:])

    def plain(self, cemi):
        address = int.from_bytes(cemi[6:8], "big")
        if address not in self.keys:
            return cemi[9:]
        apdu = cemi[9:]
        if apdu[:3] != b"\x03\xf1\x10":
            raise ValueError("Secure group requires encrypted data")
        sequence = apdu[3:9]
        sender = cemi[4:6]
        counter = int.from_bytes(sequence, "big")
        if counter <= self.rx.get(sender, 0):
            raise ValueError("Repeated group telegram")
        nonce = sequence + cemi[4:8]
        decoded = crypt(self.keys[address], nonce + bytes.fromhex("000000000100"), apdu[-4:] + apdu[9:-4])
        first = nonce + b"\0\x80\x03\xf1\0" + bytes((len(decoded) - 4,))
        mac = cbc_mac(self.keys[address], b"\x10", decoded[4:], first)[:4]
        if not hmac.compare_digest(mac, decoded[:4]):
            raise ValueError("Invalid secure group MAC")
        self.rx[sender] = counter
        return decoded[4:]


class DatagramServer(asyncio.DatagramProtocol):
    def __init__(self, owner):
        self.owner = owner

    def datagram_received(self, data, peer):
        if self.owner.has_error_behavior("no_response"):
            return
        self.owner.log_protocol("in", data)
        try:
            response = self.owner.datagram(data, peer)
        except (ValueError, IndexError, struct.error):
            return
        if response:
            for packet in frames(response):
                self.owner._udp.sendto(packet, peer)
                self.owner.log_protocol("out", packet)


class KNXIPSimulator(TCPSimulator):
    SIMULATOR_INFO = {
        "driver_id": "knx_ip", "name": "KNX/IP Interface", "category": "utility", "transport": "tcp",
        "single_client": "reject",
        "initial_state": {"light_on": False, "brightness": 50.0, "shade_position": 25.0,
                          "temperature": 22.5, "occupancy": False},
        "controls": [
            {"key": "light_on", "type": "toggle", "label": "Lights on"},
            {"key": "brightness", "type": "slider", "label": "Brightness", "min": 0, "max": 100},
            {"key": "shade_position", "type": "slider", "label": "Shade position (0 = open)", "min": 0, "max": 100},
            {"key": "temperature", "type": "slider", "label": "Temperature (°C)", "min": -10, "max": 40, "step": 0.5},
            {"key": "occupancy", "type": "toggle", "label": "Occupied"},
        ],
        "error_modes": {
            "silent_status": {"behavior": "custom", "description": "Tunnel works but status objects do not answer"},
            "ignore_commands": {"behavior": "custom", "description": "Confirm commands without changing the load"},
            "reject_credentials": {"behavior": "custom", "description": "Refuse secure authentication"},
        },
    }

    def __init__(self, device_id, config=None):
        super().__init__(device_id, config)
        self._udp = None
        self._udp_peer = None
        self._sequence = 0
        self._secure = None
        self._groups = GroupSecurity(self.config)
        self._push_tasks = set()
        self._bindings = list(self.config.get("bindings", []))
        self._values = {}
        self.requests = []
        for row in self._bindings:
            if row.get("read"):
                self._values[number(row["read"])] = self._starting_value(row)

    def _starting_value(self, row):
        key = control_key(row)
        return self.get_state(key) if key else 0

    def set_state(self, key, value):
        super().set_state(key, value)
        if not hasattr(self, "_bindings") or not self._running:
            return
        updates = []
        for row in self._bindings:
            if row.get("read") and control_key(row) == key:
                self._values[number(row["read"])] = value
                updates.append(indication(row["read"], row["dpt"], value))
        if updates:
            task = asyncio.create_task(self._publish(updates))
            self._push_tasks.add(task)
            task.add_done_callback(self._push_tasks.discard)

    async def _publish(self, updates):
        if self.has_error_behavior("no_response") or "silent_status" in self._active_errors:
            return
        for cemi in updates:
            packet = self._tunnel(self._groups.secure(cemi))
            if self._udp_peer:
                self._udp.sendto(packet, self._udp_peer)
            if self._secure and self._secure.authenticated:
                packet = self._secure.wrap(packet)
            await self.push(packet)

    async def start(self, port):
        await super().start(port)
        try:
            self._udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: DatagramServer(self), local_addr=("127.0.0.1", self.port),
            )
        except OSError:
            await super().stop()
            raise

    async def stop(self):
        for task in list(self._push_tasks):
            task.cancel()
        await asyncio.gather(*self._push_tasks, return_exceptions=True)
        if self._udp:
            self._udp.close()
            self._udp = None
        await super().stop()

    async def on_client_connected(self, client_id):
        self._sequence = 0
        self._secure = None

    async def _read_messages(self, reader, buffer=None):
        try:
            header = await reader.readexactly(6)
            length = int.from_bytes(header[4:6], "big")
            if header[:2] != b"\x06\x10" or not 6 <= length <= 4096:
                raise ConnectionError("Invalid KNX frame length")
            return [header + await reader.readexactly(length - 6)]
        except asyncio.IncompleteReadError:
            return None

    def description(self):
        hardware = bytes.fromhex("360102001101000000fa00000001e000170c020000000001") + b"OpenAVC KNX Simulator".ljust(30, b"\0")
        families = bytes.fromhex("0802020203020402")
        interface = self._groups.interface
        if interface:
            hardware = hardware[:4] + interface.host.raw.to_bytes(2, "big") + hardware[6:]
        requires_secure = any((self.config.get("mode") == "secure", self.config.get("user_password"), interface))
        if requires_secure:
            families = bytes.fromhex("0a02020203020402090104060401")
        slots = bytes.fromhex("080700fe11fa00ff")
        if interface:
            slots = slots[:4] + interface.individual_address.raw.to_bytes(2, "big") + slots[6:]
        return frame(0x0204, hardware + families + slots)

    def datagram(self, packet, peer):
        service, body = split_frame(packet)
        if service == 0x0203:
            return self.description()
        if service == 0x0201:
            return frame(0x0202, hpai(self.port) + self.description()[6:])
        if service == 0x020B:
            return frame(0x020C, hpai(self.port) + self.description()[6:])
        if service == 0x0205:
            self._udp_peer = peer
            self._sequence = 0
        return self._plain(service, body, tcp=False)

    def handle_command(self, packet):
        service, body = split_frame(packet)
        if service == 0x0951:
            interface = self._groups.interface
            self._secure = Session(interface.decrypted_authentication if interface else str(self.config.get("device_password") or "sim-device"),
                                   interface.decrypted_password if interface else str(self.config.get("user_password") or "sim-user"),
                                   interface.user_id if interface else int(self.config.get("user_id", 2)))
            return self._secure.response(body)
        if service == 0x0950:
            return self._secure_command(packet)
        if self._secure is not None:
            return None  # no plaintext commands after secure session setup
        return self._plain(service, body, tcp=True)

    def _secure_command(self, packet):
        if self._secure is None:
            return None
        plain = self._secure.unwrap(packet)
        service, body = split_frame(plain)
        if service == 0x0953:
            reply = self._secure.authenticate(plain, "reject_credentials" in self._active_errors)
        elif service == 0x0954:
            return None
        elif self._secure.authenticated:
            reply = self._plain(service, body, tcp=True)
        else:
            return None
        return b"".join(self._secure.wrap(part) for part in frames(reply or b""))

    def _plain(self, service, body, tcp):
        if service == 0x0205:
            address = self._groups.interface.individual_address.raw if self._groups.interface else 0x11FA
            return frame(0x0206, b"\x01\0" + hpai(0 if tcp else self.port, tcp) + b"\x04\x04" + address.to_bytes(2, "big"))
        if service == 0x0207:
            return frame(0x0208, body[:1] + b"\0")
        if service == 0x0209:
            return frame(0x020A, body[:1] + b"\0")
        if service == 0x0420:
            return self._telegram(body, tcp)
        return None

    def _tunnel(self, cemi):
        packet = frame(0x0420, bytes((4, 1, self._sequence, 0)) + cemi)
        self._sequence = (self._sequence + 1) % 256
        return packet

    def _telegram(self, body, tcp):
        if len(body) < 15 or body[0] != 4:
            raise ValueError("Invalid tunnelling request")
        cemi = body[4:]
        start = 2 + cemi[1]
        destination = int.from_bytes(cemi[start + 4:start + 6], "big")
        apdu = self._groups.plain(cemi)
        if len(apdu) < 2:
            raise ValueError("Invalid group telegram")
        operation = ((apdu[0] & 3) << 8) | (apdu[1] & 0xC0)
        self.requests.append((destination, operation, apdu))
        confirmed = b"\x2e" + cemi[1:]
        result = b"" if tcp else frame(0x0421, body[:3] + b"\0")
        result += self._tunnel(confirmed)
        if operation == 0x80:
            self._apply_write(destination, apdu)
        if "silent_status" not in self._active_errors:
            result += self._status(destination, operation)
        return result

    def _apply_write(self, destination, apdu):
        if "ignore_commands" in self._active_errors:
            return
        for row in self._bindings:
            if row.get("write") and number(row["write"]) == destination:
                value = decode_command(row, apdu)
                if row.get("read") and value is not None:
                    self._values[number(row["read"])] = value

    def _status(self, destination, operation):
        result = b""
        for row in self._bindings:
            if not row.get("read"):
                continue
            address = number(row["read"])
            matches = operation == 0 and address == destination
            writes = operation == 0x80 and row.get("write") and number(row["write"]) == destination
            if matches or writes:
                result += self._tunnel(self._groups.secure(indication(row["read"], row["dpt"], self._values[address])))
        return result


def split_frame(packet):
    if len(packet) < 6:
        raise ValueError("Incomplete KNXnet/IP header")
    if packet[:2] != b"\x06\x10" or int.from_bytes(packet[4:6], "big") != len(packet):
        raise ValueError("Invalid KNXnet/IP frame")
    return int.from_bytes(packet[2:4], "big"), packet[6:]


def frames(data):
    while data:
        size = int.from_bytes(data[4:6], "big")
        if not 6 <= size <= len(data):
            raise ValueError("Invalid response length")
        yield data[:size]
        data = data[size:]


def control_key(row):
    key = (row["kind"], row["property"])
    known = {("light", "on"): "light_on", ("light", "level"): "brightness", ("shade", "position"): "shade_position"}
    if key in known:
        return known[key]
    return {"9.001": "temperature", "1.018": "occupancy"}.get(row["dpt"])


def decode_command(row, apdu):
    if row["dpt"] == "1.001":
        return bool(apdu[1] & 1)
    if row["dpt"] == "5.001" and len(apdu) == 3:
        return apdu[2] * 100 / 255
    return None
