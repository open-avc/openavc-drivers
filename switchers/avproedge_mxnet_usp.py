"""AVPro Edge MXNet USP — AV-over-IP ecosystem controlled through an MXNet control box.

The control box is the head end: encoders (TX) and decoders (RX) never take
control connections of their own, so the control box is the device you add and
every endpoint appears beneath it as a child entity. Two control boxes run the
USP ecosystem: the original AC-MXNET-USP-CBOX, and today's AC-MXNET-CBOX-HA
when it was set up in its MXnet USP mode (the HA hardware runs one of four
modes, chosen at first boot: USP, 1G, 10G or Dante controller). The endpoints
are the AC-MXNET-USP-ES, -PL and -PR.

USP is its own ecosystem with its own API document ("IPCBox USP Commands"),
not a variant of MXNet 1G or 10G. The grammar (`config get/set device`, TCP 24,
no login, the endpoint roster) is shared; the arguments are not, and the box
accepts a wrong number and does something else with it:

    ======================  =====================  ===================  =====================
    What                    USP (this driver)      MXNet 1G             MXNet 10G
    ======================  =====================  ===================  =====================
    Routing planes          video, audio, USB,     the same five        six (adds analog
                            IR, RS-232                                  audio)
    Route readback          `config get device     `config get device   none; joined from the
                            routes`, answering a   routes`, answering   devicelist's channels
                            Lua table              JSON
    EDID index              0-20                   1-15                 0-20
    Decoder HDCP argument   0/1/2/3 (3 = follow    2/3/4                0/1/2
                            the source)
    Stream gate             on the decoder         on the decoder       on the encoder
    Endpoint names          txid / rxid and        id / description     id / description
                            txdescription /
                            rxdescription
    Audio                   decoder audio-out      encoder and decoder  encoder analog
                            source and volume,     volume, audio-input  volume, AVDM downmix
                            HDMI audio source,     select
                            Dante (PL / PR)
    Also                    AVPro Flow open        blackout, rotate,    dual-HDMI input gate,
                            stream, USB-C input    scaling, scenes,     CEC power keywords,
                            select (PR), the mvid  mosaic, KVM          multiview
                            multiview, the media
                            player, CEC power
                            keywords
    ======================  =====================  ===================  =====================

Python rather than YAML for four reasons, none of which ConfigurableDriver can
express today:

  1. The endpoint roster is enumerated FROM the device (`config get
     devicelist`), not declared by the integrator. YAML's `instances:` roster
     covers fixed and config-driven rosters only.
  2. Routes, status and the roster arrive on one connection in at least six
     reply shapes (see Framing), and the driver has to decide per reply how to
     read it.
  3. The route query answers with a Lua table, not JSON, and the video-wall
     queries with another Lua table.
  4. Route values come back as an encoder's lowercase MAC or its custom name,
     keyed by a decoder's lowercase MAC or its custom name, and each has to be
     joined back to the child id (the uppercase MAC the roster reports).

Protocol
    Telnet-style ASCII on TCP 24 with no login. The API document states no line
    terminator for either direction; sends are CRLF-terminated, which is what a
    PuTTY session does with Enter.

Framing
    These reply shapes, all in IPCBox USP Commands V1.08's examples, share one
    connection:

      1. JSON with `cmd` and `code`: most commands.
      2. JSON whose `cmd` is itself an escaped JSON object and which carries no
         `code`: `config set device hdcprequest`, `play pl stop` (no `info` at
         all) and `play getstorage`. The echo is read from the inner object.
      3. JSON with `info` and `cmd` and no `code`: `mvid layout rx`,
         `play pl start`, `play pl upload`. A missing `code` is acceptance.
      4. A bare token, "OK": the `vwid` writes.
      5. A Lua table: `vwid list` / `get` / `layout list` / `layout get`, and
         `config get device routes`.
      6. JSON with unescaped quotes inside `cmd`, which nothing can parse: the
         `vwid layout multiview` writes.

    Framing brace-balances anything that opens with `{` (right for the JSON and
    the Lua shapes whatever the terminator turns out to be) and takes a line
    otherwise. A frame that is not JSON is raw text and only answers a request
    that expects one (the `vwid` family and the route query; see
    _expects_raw), so a banner or a stray line can never satisfy a real query.

    A seventh shape is a refusal that says `code: 0`: `config set device
    sourceinput` answers an endpoint that is not an AC-MXNET-USP-PR with
    `{"info":"Only AC-MXNET-USP-PR supports this command","code":0}`. An `info`
    that says only some model supports the command is read as a refusal
    (_refusal); ordinary sentences ("Set EDID success", "Copy success", "The IP
    setting was successful.The Dante device will be restarted.") are not.

    The document prints each Lua reply under the command line that asked for
    it. Whether the box echoes that line on the wire is not stated, so a raw
    line that is only the request read back is ignored rather than taken as
    the answer.

Push vs poll
    Poll. The API document has no subscription, notification or event section.
    The control box family does interleave unsolicited frames on the control
    connection all the same: a frame with an EMPTY `cmd` and a `source` member,
    which is never a reply (found on a 1G control box; undocumented for USP).
    Any such frame is diverted whatever its source, because mistaking one for a
    reply hands it to the waiting request and shifts every reply after it by
    one. Their content is parsed defensively (an unrecognised shape only lands
    on `last_event`), and polling stays the authority.

    Three broad queries cover the whole system, so poll cost is flat in the size
    of the install: `config get devicelist` (roster and config), `config get
    device status ALL` (AV status of every endpoint) and `config get device
    routes vaurs ALLRX` (every decoder's five routes). The document's own
    example spells the target `ALLRX`; its contents page says `ALL_RX`.

Reading the routes
    The route reply keys each decoder, and names each source, by the
    endpoint's current id: its MAC (lowercase in this reply, uppercase in the
    device list) until somebody renames it, and its custom name afterwards. An
    endpoint has two custom names on USP, one as an encoder (`txid`) and one as
    a decoder (`rxid`). Both are resolved back to the child id without regard
    to case; a value the roster cannot resolve (a third-party stream source)
    is published as reported, and `none` in any case is unrouted.

    A route the control box accepted moments ago is held at its commanded value
    until the box agrees or a ceiling lapses (_expect_routes), because the box
    acknowledges a route long before it reports it.

Preview stream
    Every USP encoder has a live MJPEG preview that the control box proxies on
    its control port: http://<control box>:10085/?ip=<encoder AV address>&port=10004
    (AVPro's MXnet Endpoints User Manual). Each encoder publishes it as
    `preview_url` / `preview_format`, so the Video Panel plugin lists it.

Not covered
    * Factory resets of the control box and of an endpoint: they erase the
      system's configuration and have no control-system use.
    * `config set device rm`: removes an endpoint from the database.
    * `config set device ip`, `config set ip4addr`, `config set ip4addr2`:
      network commissioning. A wrong static address strands an endpoint or the
      control box itself (ip4addr2 is the address this driver connects through)
      and undoing it needs physical access.
    * `config set rs232baudrate`: the control box's own serial port. Changing it
      cuts off a control system on that port, and there is no command to read
      it back.
    * Third-party stream sources (`addexdevice`, `exdevice add`, `rmexdevice`,
      `setexid`): commissioning, and two of them take a camera's password on
      the command line.
    * `msdstream`: it switches an AC-MXNET-10G-MSD's output, not a USP
      endpoint's.
    * Authoring video walls, multiview layouts and templates, matrices and
      playlists (`vwid add|setrowcol|rm|layout add|set|rm|copy|setrowcol`, the
      `vwid layout multiview` writes other than `active`, `mvid layout
      add|rm|window|rmwindow|layer|resolution|template`, `mvid template *`,
      `matrix add|set|aset|rm`, `play pl add|rm|setname|set`, `play getfiles`).
      These are built at commissioning; a control system recalls them.
    * `config set device rs232mode`: whether serial data an endpoint receives
      is relayed back to the control box at all (TYPE2, which the document's
      roster example shows on every endpoint) or not (TYPE1). It is left as
      commissioned; Serial Data needs TYPE2. `rs232responsetype` sets only how
      relayed data is encoded.

Source: IPCBox USP Commands V1.08 (AVPro Global Holdings), published at
https://support.avproglobal.com/kb/15141900/
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from typing import Any

from openavc.core.connection_fault import CHILD_NOT_RESPONDING
from openavc.drivers.base import BaseDriver
from openavc.transport.frame_parsers import CallableFrameParser
from openavc.utils.logger import get_logger

log = get_logger(__name__)

REQUEST_TIMEOUT_S = 6.0
ROSTER_TIMEOUT_S = 15.0
MAX_POLL_MISSES = 2
FULL_REFRESH_EVERY = 6

# How far behind the newest heartbeat in the same reply an endpoint may fall
# before it is called offline. See _presence() for why it is relative.
HEARTBEAT_STALE_S = 90

# How long a commanded route outranks the control box's own answer. Carried
# over from the 1G driver, where convergence was measured at ~16s on a
# CBOX-B; not measured on a USP box. It is a ceiling, and agreement clears it
# sooner.
ROUTE_SETTLE_S = 30

# Where the control box proxies each encoder's MJPEG preview (MXnet Endpoints
# User Manual): http://<control box>:10085/?ip=<encoder>&port=10004
PREVIEW_PORT = 10085
PREVIEW_ENCODER_PORT = 10004

# Routing is done with the per-plane `*path` commands, never with `matrix aset`.
# `aset` looks like the natural "route everything" call and the API document
# presents it that way, but on the 1G control box in this firmware family it
# is acknowledged and then IGNORED whenever the destination's plane is
# currently disabled -- reproduced deterministically on an AC-MXNET-CBOX-B,
# where an accepted `aset` left the route unset twenty seconds later while the
# per-plane `videopath` applied in about six. That combination is exactly what
# a matrix panel produces (press Off on a destination, then press a source),
# and the boxes share the command grammar, so the same discipline applies here
# until a USP box says otherwise.
ROUTE_COMMANDS = {
    "video": "videopath",
    "audio": "audiopath",
    "usb": "usbpath",
    "infrared": "irpath",
    "serial": "rs232path",
}

UNROUTE_COMMANDS = {name: cmd + "disable" for name, cmd in ROUTE_COMMANDS.items()}

# The decoder child property each plane's current source lands on (the 1G
# driver's names, so a project moves between the two without rebinding).
ROUTE_PROPERTIES = {
    "video": "source_video",
    "audio": "source_audio",
    "usb": "source_usb",
    "infrared": "source_infrared",
    "serial": "source_serial",
}

# The route reply's member for each plane -> the decoder property it lands on.
WIRE_ROUTES = {
    "video": "source_video",
    "audio": "source_audio",
    "usb": "source_usb",
    "ir": "source_infrared",
    "rs232": "source_serial",
}

# RS-232 feedback encapsulation (`config set device rs232responsetype`):
# 1 Base64 (the endpoint's default), 2 ASCII, 3 HEX.
SERIAL_FORMATS = {"base64": "1", "ascii": "2", "hex": "3"}

_RE_MAC = re.compile(r"^[0-9A-Fa-f]{12}$")

# An `info` that says only some model accepts the command. The control box
# sends it with code 0, so it has to be read as the refusal it is.
_RE_ONLY_MODEL = re.compile(r"^\s*only\b.*\bsupports?\b", re.IGNORECASE)

# Endpoint names: no colon, no comma, not ALL / ALLRX / ALLTX (IPCBox USP
# Commands V1.08, `config set device txid`), and one token, because the target
# is the last word of the line.
_RE_ENDPOINT_NAME = re.compile(r"^(?!(?:ALL|ALLRX|ALLTX)$)[^\s:,]{1,32}$", re.IGNORECASE)

_RE_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

# Unsolicited event lines. The port label is IN<n> on an encoder and OUT<n> on a
# decoder; the number is captured rather than assumed.
_RE_EVENT_HPD = re.compile(r"^(?:IN|OUT)(\d+)\s+HPD\s+([01])\s*$", re.IGNORECASE)
_RE_EVENT_AVINF = re.compile(r"^(?:IN|OUT)(\d+)\s+AV\s+INF\s+(.*)$", re.IGNORECASE)

# The `vwid` queries answer with a Lua-style table. `<name> = {` is the only
# piece of that grammar the wall reader needs.
_RE_LUA_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*\{")

# The route reply: `{ ["188a6a02c0ab"] = {audio = "none", ir = "none", ...}, ... }`.
# A key is a quoted string in brackets (a bare name is accepted too); each
# entry holds flat `plane = "value"` pairs.
_RE_LUA_ROUTE_ENTRY = re.compile(
    r'(?:\[\s*"((?:[^"\\]|\\.)*)"\s*\]|\b([A-Za-z_][\w.-]*))\s*=\s*\{([^{}]*)\}'
)
_RE_LUA_PAIR = re.compile(r'([A-Za-z_]\w*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s}]+))')


def _json_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Frame the control box's reply stream, which is not all one shape.

    A frame that opens with `{` is brace-balanced (string/escape aware), which
    is correct for a JSON object AND for the Lua-style tables the route and
    video-wall queries answer with, whichever line terminator the firmware
    uses. Anything else is framed on a line ending, which is the only thing
    that completes a bare `OK`.

    Leading whitespace is consumed by returning an EMPTY frame with the
    trimmed remainder; CallableFrameParser keeps whatever buffer a parse
    function returns on either branch.
    """
    lead = 0
    while lead < len(buf) and buf[lead] in (0x20, 0x09, 0x0D, 0x0A):
        lead += 1
    if lead:
        return b"", buf[lead:]
    if not buf:
        return None, buf

    if buf[0] != 0x7B:  # not "{" -- a bare token, framed on its line ending
        end = -1
        for i, byte in enumerate(buf):
            if byte in (0x0D, 0x0A):
                end = i
                break
        if end < 0:
            return None, buf
        return buf[:end], buf[end:]

    depth = 0
    in_string = False
    escape = False
    for i, byte in enumerate(buf):
        if escape:
            escape = False
            continue
        if in_string:
            if byte == 0x5C:  # backslash
                escape = True
            elif byte == 0x22:  # "
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte == 0x7B:  # {
            depth += 1
        elif byte == 0x7D:  # }
            depth -= 1
            if depth == 0:
                return buf[: i + 1], buf[i + 1 :]
    return None, buf


def _lua_names(text: str) -> dict[str, dict[str, Any]]:
    """Read the names out of a Lua-style table, keeping the nesting.

    The video-wall queries answer with the control box's own config syntax
    rather than JSON, and the only thing this driver wants from it is which
    walls exist and which layouts each one holds. Values are ignored entirely,
    so the reader needs no grammar beyond `<name> = {` and a brace depth.
    """
    tree: dict[str, dict[str, Any]] = {}
    stack: list[dict[str, Any]] = [tree]
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char == '"' or char == "'":
            quote = char
            i += 1
            while i < length:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    break
                i += 1
            i += 1
            continue
        if char == "{":
            # The reply is wrapped in one anonymous table, so the first brace
            # opens the root rather than a level inside it; deeper anonymous
            # tables (a list of layout strings) still have to be tracked so
            # their closing brace does not pop a named level.
            stack.append(tree if len(stack) == 1 else {})
            i += 1
            continue
        if char == "}":
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        match = _RE_LUA_KEY.match(text, i)
        if match:
            node: dict[str, Any] = {}
            stack[-1][match.group(1)] = node
            stack.append(node)
            i = match.end()
            continue
        i += 1
    return tree


def _lua_routes(text: str) -> dict[str, dict[str, str]]:
    """Read `config get device routes` as IPCBox USP Commands V1.08 shows it.

        { ["188a6a02c0ab"] = {audio = "none", ir = "none",rs232 = "none",
          usb = "none",video = "none" }, ["188a6a02c0b6"] = {audio =
          "188a11223368", ...} }

    Returns {decoder key: {plane: value}} with the plane names lowercased. The
    spacing in the document's example is irregular, so nothing here depends
    on it.
    """
    table: dict[str, dict[str, str]] = {}
    for match in _RE_LUA_ROUTE_ENTRY.finditer(text):
        key = match.group(1) if match.group(1) is not None else match.group(2)
        if not key:
            continue
        pairs: dict[str, str] = {}
        for pair in _RE_LUA_PAIR.finditer(match.group(3)):
            value = pair.group(2) if pair.group(2) is not None else pair.group(3)
            pairs[pair.group(1).lower()] = value or ""
        table[key] = pairs
    return table


def _txt(value: Any) -> str:
    """Coerce a JSON member to a trimmed string ('' for absent/null)."""
    if value is None:
        return ""
    return str(value).strip()


def _flat(text: str) -> str:
    """A command line with its whitespace collapsed, for comparing echoes."""
    return " ".join(text.split())


def _echo(doc: dict[str, Any]) -> str:
    """The command a reply echoes, unwrapping the escaped-JSON form.

    `{"info":"844M","cmd":"{\\"cmd\\":\\"play getstorage\\"}"}` carries its echo
    one level down, as a JSON object inside the `cmd` string.
    """
    cmd = _txt(doc.get("cmd"))
    if cmd.startswith("{"):
        try:
            inner = json.loads(cmd)
        except (ValueError, TypeError):
            return cmd
        if isinstance(inner, dict):
            return _txt(inner.get("cmd"))
    return cmd


def _refusal(doc: dict[str, Any]) -> str | None:
    """The control box's reason for refusing a command, or None if it accepted.

    A missing `code` is acceptance (shapes 2 and 3 in the module docstring
    carry none). A non-zero code is a refusal. And so is code 0 with an `info`
    saying only some model supports the command, which is how `sourceinput`
    answers an endpoint that is not an AC-MXNET-USP-PR.
    """
    raw = doc.get("code", 0)
    try:
        code = int(str(raw).strip() or "0")
    except (TypeError, ValueError):
        code = 0
    info = doc.get("info")
    info_text = _txt(info) if isinstance(info, str) else ""
    if code != 0:
        return _txt(doc.get("error")) or info_text or "rejected"
    if info_text and _RE_ONLY_MODEL.search(info_text):
        return info_text
    return None


def _model_refusal(model: str) -> str | None:
    """Why a control box with this name is not one this driver controls.

    None means go ahead. The sentence names the driver that fits.
    """
    upper = model.strip().upper()
    if not upper:
        return None
    # The HA hardware runs USP, 1G, 10G or Dante controller mode, and no AVPro
    # document says which name it reports in USP mode. It is accepted on its
    # name, and the USP replies decide the rest.
    if upper.startswith("AC-MXNET-CBOX-HA"):
        return None
    if "USP" in upper:
        return None
    if "10G" in upper:
        return (
            f"reports itself as {model}, an MXNet 10G control box. Use the AVPro Edge "
            f"MXNet 10G driver (avproedge_mxnet_10g) for it."
        )
    if "DANTE" in upper:
        return (
            f"reports itself as {model}, an MXNet control box running as a Dante "
            f"controller. This driver controls an MXNet USP system, and there is no "
            f"driver for the Dante controller mode yet."
        )
    if upper in ("AC-MXNET-CBOX", "AC-MXNET-CBOX-B"):
        return (
            f"reports itself as {model}, an MXNet 1G control box. Use the AVPro Edge "
            f"MXNet 1G driver (avproedge_mxnet_1g) for it."
        )
    return None


def _flag(value: Any, token: str) -> bool:
    """MXNet reports several booleans as a token plus a digit: HDR1 / HPD0."""
    return _txt(value).upper() == f"{token}1"


def _hdcp(value: Any) -> str:
    """Normalise the control box's two spellings of the same HDCP state.

    An encoder reports `HDCP0` / `HDCP1` while a decoder reports `HDCP OFF` /
    `HDCP ON` for the same condition on the same firmware (measured on the 1G
    control box; the USP document shows the encoder spelling). Left raw, one
    endpoint's card reads "HDCP0" and its neighbour's "HDCP OFF" and no trigger
    can compare them. Anything unrecognised passes through.
    """
    text = _txt(value)
    if not text:
        return ""
    flat = text.upper().replace(" ", "")
    if flat in ("HDCP0", "HDCPOFF", "OFF", "0"):
        return "Off"
    if flat in ("HDCP1", "HDCPON", "ON", "1"):
        return "On"
    return text


def _resolution(value: Any) -> str:
    """Normalise a timing to one spelling, whichever shape the control box used.

    Three forms reach this: the status reply's string (`3840X2160p/30Hz`), the
    AV-info event's string (`3840X2160p@30Hz` for the identical timing), and
    the decoder `device info` reply's object (`{"width": "3840", "height":
    "2160", "frames_per_second": "30"}`).
    """
    if isinstance(value, dict):
        width = _txt(value.get("width"))
        height = _txt(value.get("height"))
        fps = _txt(value.get("frames_per_second"))
        if not width or not height:
            return ""
        return f"{width}X{height}p" + (f"/{fps}Hz" if fps else "")
    text = _txt(value)
    if not text or text.startswith("@"):
        return ""
    return text.replace("@", "/")


def _has_signal(video: Any) -> bool:
    """A signal-less port reports an empty, 'none' or zero-by-zero timing."""
    text = _resolution(video).lower()
    if not text or text in ("none", "no signal"):
        return False
    return not text.startswith("0x0")


def _heartbeat(entry: dict[str, Any]) -> int | None:
    """The endpoint's `online` member, which is a heartbeat or absent.

    Absent is the control box's way of saying "this is a database record for
    something that is not here" -- it is not zero and not false.
    """
    if "online" not in entry:
        return None
    try:
        beat = int(str(entry["online"]).strip())
    except (TypeError, ValueError):
        return None
    return beat if beat > 0 else None


def _presence(entries: dict[str, dict[str, Any]]) -> dict[str, bool]:
    """Decide which endpoints are actually present, from one roster reply.

    `online` is a heartbeat, and the comparison is made against the NEWEST
    heartbeat in the same reply rather than against this host's clock, which
    makes it correct without either clock agreeing with the other. The USP
    document's examples carry values around 14000-17500 (a box counting from
    the epoch because its clock was never set), and its roster example reports
    the identical value for every endpoint at once, which is one clock sampled
    once rather than per-endpoint uptimes.

    Not `state`: that is the streaming service state (`s_srv_on`,
    `s_attaching`), and an encoder with no source sits in `s_attaching`
    indefinitely while being perfectly present.
    """
    beats = {key: _heartbeat(e) for key, e in entries.items() if isinstance(e, dict)}
    live = [b for b in beats.values() if b is not None]
    if not live:
        return {key: False for key in beats}
    newest = max(live)
    return {
        key: beat is not None and (newest - beat) <= HEARTBEAT_STALE_S
        for key, beat in beats.items()
    }


class AVProEdgeMXNetUSPDriver(BaseDriver):
    """AVPro Edge MXNet USP ecosystem, via an MXNet control box in USP mode."""

    DRIVER_INFO = {
        "id": "avproedge_mxnet_usp",
        "name": "AVPro Edge MXNet USP",
        "manufacturer": "AVPro Edge",
        "category": "switcher",
        "version": "1.0.0",
        # Computed, not chosen: `routing:` needs 0.27.0, sharing a route
        # property between the All-streams and Video planes needs 0.28.0,
        # BaseDriver.child_fault() needs 0.29.0, `restarts_device_for`
        # needs 0.34.0, and BaseDriver.local_address(), which the Test
        # Connection socket binds to, needs 0.37.0. child_fault() and
        # local_address() are method calls the contract check cannot see, so
        # the floor is carried by hand here the way the 1G and 10G drivers
        # carry it.
        "min_platform_version": "0.37.0",
        "author": "OpenAVC",
        "description": (
            "Controls an AVPro Edge MXNet USP AV-over-IP system through its control "
            "box. Encoders and decoders appear as child entities with per-plane "
            "routing (video, audio, USB, IR, RS-232), live encoder previews, EDID, "
            "CEC/IR/RS-232 passthrough, Dante and AVPro Flow settings, and video "
            "wall, multiview, matrix and playlist recall."
        ),
        "source_url": "https://support.avproglobal.com/kb/15141900/",
        "tags": [
            "av-over-ip",
            "mxnet",
            "usp",
            "matrix-switcher",
            "video-wall",
            "multiview",
            "encoder",
            "decoder",
            "dante",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["mxnet_api"],
        "ports": [24],
        "transport": "tcp",
        # Five independently routable planes per decoder, all switched by the
        # one route command and told apart by its stream parameter -- plus the
        # combined mode that moves all five, which is what a room wants and so
        # is offered first. It watches source_video because that is what a
        # tile should read; the Video plane below watches the same property and
        # sends a different stream, which is a different control rather than
        # the same one twice.
        "routing": {
            "destination_child_type": "decoder",
            "source_child_type": "encoder",
            "command": "route",
            "planes": [
                {"label": "All streams", "route_property": "source_video",
                 "params": {"stream": "all"}},
                {"label": "Video", "route_property": "source_video",
                 "params": {"stream": "video"}},
                {"label": "Audio", "route_property": "source_audio",
                 "params": {"stream": "audio"}},
                {"label": "USB", "route_property": "source_usb",
                 "params": {"stream": "usb"}},
                {"label": "IR", "route_property": "source_infrared",
                 "params": {"stream": "infrared"}},
                {"label": "Serial", "route_property": "source_serial",
                 "params": {"stream": "serial"}},
            ],
        },
        "discovery": {
            "port_open": [24],
            "manufacturer_alias": ["avpro edge", "avpro global", "avproedge"],
            # The control box answers `config get name` with its model, e.g.
            # {"cmd":"config get name","info":"AC-MXNET-USP-CBOX","code":0}
            # The regex names the USP box specifically: every MXNet control box
            # answers this query, and a probe matching "AC-MXNET" alone would
            # claim all four ecosystems for whichever driver was asked first.
            # An AC-MXNET-CBOX-HA in USP mode that answers with its hardware
            # name is not matched here and is found by its port and maker only.
            "tcp_probe": {
                "port": 24,
                "send_ascii": "config get name\r\n",
                "expect_regex": r'"info"\s*:\s*"AC-MXNET-USP[^"]*"',
                "extract_manufacturer": "AVPro Edge",
                "extract": {
                    "model": {"regex": r'"info"\s*:\s*"(AC-MXNET-USP[^"]*)"'},
                },
                "timeout_ms": 2000,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "AVPro Edge",
                "models": ["AC-MXNET-USP-CBOX"],
                "confidence": "untested",
                "notes": (
                    "Add the CONTROL BOX as the device; the AC-MXNET-USP-ES, -PL and "
                    "-PR encoders and decoders it manages appear as children. The 1G, "
                    "10G and Dante control boxes speak different command sets and have "
                    "their own drivers."
                ),
            },
            {
                "manufacturer": "AVPro Edge",
                "models": ["AC-MXNET-CBOX-HA"],
                "confidence": "untested",
                "notes": (
                    "In its MXnet USP mode only (the mode is chosen when the control "
                    "box is first set up). In MXnet 1G mode use the AVPro Edge MXNet 1G "
                    "driver, in MXnet 10G mode the MXNet 10G driver."
                ),
            },
        ],
        "help": {
            "overview": (
                "The control box is the head end of an MXNet USP system. Add the control "
                "box (not the individual encoders and decoders) and every endpoint it "
                "knows about shows up as a child you can route, preview and pass CEC, IR "
                "and RS-232 through.\n\n"
                "Routing works per plane: video, audio, USB, IR and RS-232 can each "
                "follow a different source, or move together with the Route command's "
                "Media setting of 'All'. Video walls, multiview layouts, matrices and "
                "media playlists built in the MXNet management interface are recalled "
                "by name. Each encoder's live preview is offered to the Video Panel "
                "plugin.\n\n"
                "This is not the same product as MXNet 1G or 10G. The EDID numbers, the "
                "HDCP numbers and the stream gate differ between them, so use the driver "
                "that matches the system you have."
            ),
            "setup": (
                "1. Connect to the control box's control network port (PC Control / "
                "LAN2), the one used for its web interface, not the AV network port.\n"
                "2. Enter that port's IP address. The API listens on TCP 24 and needs no "
                "login.\n"
                "3. An AC-MXNET-CBOX-HA has to be running in MXnet USP mode.\n"
                "4. Commission the system first (endpoints adopted, names assigned). The "
                "driver reads the roster from the control box; it does not adopt "
                "endpoints.\n"
                "5. Endpoints are listed by their custom name. Renaming one changes its "
                "label here on the next poll; its identity is its MAC address, so "
                "renaming never breaks a button that points at it.\n"
                "6. RS-232 passthrough needs the endpoint's serial port set for the "
                "attached device: use Set Serial Port Settings."
            ),
        },
        "default_config": {
            "host": "",
            "port": 24,
            "poll_interval": 10,
            "serial_feedback_format": "ascii",
        },
        "config_schema": {
            "host": {
                "type": "string",
                "required": True,
                "label": "IP Address",
                "description": "IP address of the control box's control network (LAN2) port.",
            },
            "port": {
                "type": "integer",
                "required": True,
                "default": 24,
                "label": "Port",
                "description": "MXNet API port. Fixed at 24 on the control box.",
            },
            "poll_interval": {
                "type": "integer",
                "default": 10,
                "min": 0,
                "label": "Poll Interval (sec)",
                "description": (
                    "Refreshes the endpoint roster, every decoder's routes and each "
                    "endpoint's AV status. The MXNet API has no notifications, so this is "
                    "the only source of state. 0 disables polling."
                ),
            },
            "serial_feedback_format": {
                "type": "enum",
                "default": "ascii",
                "values": ["ascii", "hex", "base64"],
                "label": "Serial Feedback Format",
                "description": (
                    "How the control box relays RS-232 data coming back from an "
                    "endpoint. Applied to every endpoint on connect, so inbound serial "
                    "data can be read. The endpoints' own default is Base64."
                ),
            },
        },
        "state_variables": {
            "connected": {"type": "boolean", "label": "Connected"},
            "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
            "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
            "av_ip": {
                "type": "string",
                "label": "AV Network IP",
                "help": "Address of the port that manages the MXNet endpoints (LAN1).",
                "cloud_priority": "low",
            },
            "lan_ip": {
                "type": "string",
                "label": "Control Network IP",
                "help": "Address of the control port this driver talks to (LAN2).",
                "cloud_priority": "low",
            },
            "encoder_count": {
                "type": "integer",
                "label": "Encoders",
                "cloud_priority": "low",
            },
            "decoder_count": {
                "type": "integer",
                "label": "Decoders",
                "cloud_priority": "low",
            },
            "offline_endpoints": {
                "type": "integer",
                "label": "Endpoints Offline",
                "help": "Endpoints in the control box database that are not reachable.",
                "cloud_priority": "high",
            },
            "system_date": {
                "type": "string",
                "label": "Clock",
                "help": "The control box's own date and time.",
                "cloud_priority": "low",
            },
            "timezone": {"type": "string", "label": "Timezone", "cloud_priority": "low"},
            "ntp_servers": {
                "type": "string",
                "label": "NTP Servers",
                "cloud_priority": "low",
            },
            "dns_servers": {
                "type": "string",
                "label": "DNS Servers",
                "cloud_priority": "low",
            },
            "media_storage_used": {
                "type": "string",
                "label": "Media Storage Used",
                "help": "Space the playlist media files take on the control box (16 GB "
                "in all), e.g. 844M.",
                "cloud_priority": "low",
            },
            "encoder_options": {
                "type": "string",
                "label": "Encoder Options",
                "help": "JSON list of encoders. Feeds the source dropdowns.",
                "cloud_priority": "low",
            },
            "decoder_options": {
                "type": "string",
                "label": "Decoder Options",
                "help": "JSON list of decoders. Feeds the display dropdowns.",
                "cloud_priority": "low",
            },
            "endpoint_options": {
                "type": "string",
                "label": "Endpoint Options",
                "help": "JSON list of every endpoint. Feeds the endpoint dropdowns.",
                "cloud_priority": "low",
            },
            "matrix_options": {
                "type": "string",
                "label": "Matrix Options",
                "cloud_priority": "low",
            },
            "videowall_options": {
                "type": "string",
                "label": "Video Wall Options",
                "cloud_priority": "low",
            },
            "videowall_layout_options": {
                "type": "string",
                "label": "Video Wall Layout Options",
                "help": "JSON list of every wall-and-layout pair. Feeds the recall dropdowns.",
                "cloud_priority": "low",
            },
            "multiview_layout_options": {
                "type": "string",
                "label": "Multiview Layout Options",
                "help": "JSON list of the multiview layouts. Feeds the layout dropdowns.",
                "cloud_priority": "low",
            },
            "playlist_options": {
                "type": "string",
                "label": "Playlist Options",
                "help": "JSON list of the media playlists. Feeds the playlist dropdowns.",
                "cloud_priority": "low",
            },
        },
        "child_entity_types": {
            "encoder": {
                "label": "Encoder",
                "label_plural": "Encoders",
                "id_format": {"type": "string", "max_length": 12},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "online": {
                        "type": "boolean",
                        "label": "Online",
                        "help": "Endpoint is reachable on the AV network.",
                        "cloud_priority": "high",
                    },
                    "signal_present": {
                        "type": "boolean",
                        "label": "Signal",
                        "help": "A source is connected and sending a valid signal.",
                        "cloud_priority": "high",
                    },
                    "source_connected": {
                        "type": "boolean",
                        "label": "Source Connected",
                        "help": "Hot-plug detect from the attached source. True with no "
                        "Signal means the cable is in but the source is not sending.",
                        "cloud_priority": "high",
                    },
                    "resolution": {
                        "type": "string",
                        "label": "Input Resolution",
                        "cloud_priority": "high",
                    },
                    "audio_format": {
                        "type": "string",
                        "label": "Audio Format",
                        "cloud_priority": "low",
                    },
                    "hdcp": {"type": "string", "label": "HDCP", "cloud_priority": "low"},
                    "hdr": {"type": "boolean", "label": "HDR", "cloud_priority": "low"},
                    "chroma": {"type": "string", "label": "Chroma", "cloud_priority": "low"},
                    "color_depth": {
                        "type": "string",
                        "label": "Color Depth",
                        "cloud_priority": "low",
                    },
                    "edid": {
                        "type": "string",
                        "label": "EDID",
                        "help": "Active EDID preset presented to the source.",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "channel": {
                        "type": "string",
                        "label": "Stream Channel",
                        "help": "Channel decoders subscribe to for this encoder's streams.",
                        "cloud_priority": "low",
                    },
                    "open_stream": {
                        "type": "boolean",
                        "label": "Open Stream",
                        "help": "The AVPro Flow open stream (RTSP or RTMP for third-party "
                        "players) is on. Shown when the control box reports it.",
                        "cloud_priority": "low",
                    },
                    "preview_url": {
                        "type": "string",
                        "label": "Preview URL",
                        "help": "Live MJPEG preview of this encoder's input, served by the "
                        "control box. Empty when the encoder's address is not known.",
                        "cloud_priority": "low",
                    },
                    "preview_format": {
                        "type": "string",
                        "label": "Preview Format",
                        "cloud_priority": "low",
                    },
                    "dante_ip_mode": {
                        "type": "string",
                        "label": "Dante IP Mode",
                        "help": "dhcp or static. Read with Read Dante IP Settings; "
                        "AC-MXNET-USP-PL and -PR only.",
                        "cloud_priority": "low",
                    },
                    "dante_ip": {
                        "type": "string",
                        "label": "Dante IP Address",
                        "cloud_priority": "low",
                    },
                    "dante_netmask": {
                        "type": "string",
                        "label": "Dante Subnet Mask",
                        "cloud_priority": "low",
                    },
                    "dante_gateway": {
                        "type": "string",
                        "label": "Dante Gateway",
                        "cloud_priority": "low",
                    },
                    "serial_data": {
                        "type": "string",
                        "label": "Serial Data",
                        "help": "Most recent RS-232 data received on this endpoint's serial port.",
                        "cloud_priority": "low",
                    },
                    "mac": {"type": "string", "label": "MAC", "cloud_priority": "low"},
                    "ip": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
                    "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
                    "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
                    "link_speed": {
                        "type": "string",
                        "label": "Link Speed",
                        "cloud_priority": "low",
                    },
                    "service_state": {
                        "type": "string",
                        "label": "Service State",
                        "help": (
                            "The endpoint's streaming service state as the control box "
                            "reports it (s_srv_on, s_attaching...). This is not presence: "
                            "an endpoint with no source sits in s_attaching while being "
                            "perfectly reachable. Use Online for presence."
                        ),
                        "cloud_priority": "low",
                    },
                    "last_event": {
                        "type": "string",
                        "label": "Last Event",
                        "help": "Most recent unsolicited AV-info line the control box sent "
                        "for this endpoint.",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["online", "signal_present", "resolution"],
                "label_field": "name",
            },
            "decoder": {
                "label": "Decoder",
                "label_plural": "Decoders",
                "id_format": {"type": "string", "max_length": 12},
                "state_variables": {
                    "name": {"type": "string", "label": "Name", "cloud_priority": "low"},
                    "online": {
                        "type": "boolean",
                        "label": "Online",
                        "help": "Endpoint is reachable on the AV network.",
                        "cloud_priority": "high",
                    },
                    "source_video": {
                        "type": "string",
                        "label": "Video Source",
                        "help": "Encoder whose video this decoder is showing; empty when unrouted.",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "source_audio": {
                        "type": "string",
                        "label": "Audio Source",
                        "control": True,
                        "cloud_priority": "high",
                    },
                    "source_usb": {
                        "type": "string",
                        "label": "USB Source",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "source_infrared": {
                        "type": "string",
                        "label": "IR Source",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "source_serial": {
                        "type": "string",
                        "label": "Serial Source",
                        "control": True,
                        "cloud_priority": "low",
                    },
                    "display_connected": {
                        "type": "boolean",
                        "label": "Display Connected",
                        "help": "Hot-plug detect from the attached display.",
                        "cloud_priority": "high",
                    },
                    "resolution": {
                        "type": "string",
                        "label": "Output Resolution",
                        "cloud_priority": "high",
                    },
                    "audio_format": {
                        "type": "string",
                        "label": "Audio Format",
                        "cloud_priority": "low",
                    },
                    "hdcp": {"type": "string", "label": "HDCP", "cloud_priority": "low"},
                    "hdr": {"type": "boolean", "label": "HDR", "cloud_priority": "low"},
                    "chroma": {"type": "string", "label": "Chroma", "cloud_priority": "low"},
                    "color_depth": {
                        "type": "string",
                        "label": "Color Depth",
                        "cloud_priority": "low",
                    },
                    "dante_ip_mode": {
                        "type": "string",
                        "label": "Dante IP Mode",
                        "help": "dhcp or static. Read with Read Dante IP Settings; "
                        "AC-MXNET-USP-PL and -PR only.",
                        "cloud_priority": "low",
                    },
                    "dante_ip": {
                        "type": "string",
                        "label": "Dante IP Address",
                        "cloud_priority": "low",
                    },
                    "dante_netmask": {
                        "type": "string",
                        "label": "Dante Subnet Mask",
                        "cloud_priority": "low",
                    },
                    "dante_gateway": {
                        "type": "string",
                        "label": "Dante Gateway",
                        "cloud_priority": "low",
                    },
                    "serial_data": {
                        "type": "string",
                        "label": "Serial Data",
                        "help": "Most recent RS-232 data received on this endpoint's serial port.",
                        "cloud_priority": "low",
                    },
                    "mac": {"type": "string", "label": "MAC", "cloud_priority": "low"},
                    "ip": {"type": "string", "label": "IP Address", "cloud_priority": "low"},
                    "model": {"type": "string", "label": "Model", "cloud_priority": "low"},
                    "firmware": {"type": "string", "label": "Firmware", "cloud_priority": "low"},
                    "link_speed": {
                        "type": "string",
                        "label": "Link Speed",
                        "cloud_priority": "low",
                    },
                    "service_state": {
                        "type": "string",
                        "label": "Service State",
                        "help": (
                            "The endpoint's streaming service state as the control box "
                            "reports it (s_srv_on, s_attaching...). This is not presence: "
                            "an endpoint with no source sits in s_attaching while being "
                            "perfectly reachable. Use Online for presence."
                        ),
                        "cloud_priority": "low",
                    },
                    "last_event": {
                        "type": "string",
                        "label": "Last Event",
                        "help": "Most recent unsolicited AV-info line the control box sent "
                        "for this endpoint.",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["online", "source_video", "display_connected"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "timezone": {
                "type": "string",
                "label": "Timezone",
                "help": "UTC offset, e.g. UTC-5. Range UTC-12 to UTC+12.",
                "state_key": "timezone",
                "default": "UTC+0",
                "setup": False,
            },
            "ntp_servers": {
                "type": "string",
                "label": "NTP Servers",
                "help": "Up to five NTP servers, separated by spaces.",
                "state_key": "ntp_servers",
                "default": "",
                "setup": False,
            },
            "dns_servers": {
                "type": "string",
                "label": "DNS Servers",
                "help": "Up to two DNS servers, separated by spaces.",
                "state_key": "dns_servers",
                "default": "",
                "setup": False,
            },
        },
        "quick_actions": ["route", "route_off", "recall_matrix", "refresh"],
        "actions": [
            {"id": "route", "kind": "command", "icon": "route"},
            {"id": "route_off", "kind": "command", "icon": "circle-off"},
            {"id": "recall_matrix", "kind": "command", "icon": "bookmark"},
            {"id": "recall_videowall_layout", "kind": "command", "icon": "layout-grid"},
            {"id": "recall_multiview_layout", "kind": "command", "icon": "layout-dashboard"},
            {"id": "identify", "kind": "command", "icon": "lightbulb"},
            {"id": "refresh", "kind": "command", "icon": "refresh-cw"},
            {"id": "reboot_cbox", "kind": "command", "icon": "power", "confirm": True},
            {
                "id": "test_connection",
                "kind": "setup",
                "label": "Test Connection",
                "icon": "plug-zap",
                "availability": "always",
            },
        ],
        "commands": {
            # ── Routing ────────────────────────────────────────────────
            "route": {
                "label": "Route Source to Display",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "stream": {
                        "type": "enum",
                        "required": False,
                        "label": "Media",
                        "default": "all",
                        "values": [
                            {"value": "all", "label": "All"},
                            {"value": "video", "label": "Video"},
                            {"value": "audio", "label": "Audio"},
                            {"value": "usb", "label": "USB"},
                            {"value": "infrared", "label": "Infrared"},
                            {"value": "serial", "label": "RS-232"},
                        ],
                        "help": "Route one plane on its own, or all of them together.",
                    },
                },
                "help": "The display shows the chosen source. With Media set to All, its "
                "audio, USB, IR and RS-232 follow the same source too.",
            },
            "route_off": {
                "label": "Clear Route",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "stream": {
                        "type": "enum",
                        "required": False,
                        "label": "Media",
                        "default": "all",
                        "values": [
                            {"value": "all", "label": "All"},
                            {"value": "video", "label": "Video"},
                            {"value": "audio", "label": "Audio"},
                            {"value": "usb", "label": "USB"},
                            {"value": "infrared", "label": "Infrared"},
                            {"value": "serial", "label": "RS-232"},
                        ],
                    },
                },
                "help": "The display's source is disconnected: no picture (or no audio, "
                "USB, IR or RS-232 for that plane alone).",
            },
            "recall_matrix": {
                "label": "Recall Matrix",
                "params": {
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "Matrix",
                        "options_state": "matrix_options",
                    },
                    "force": {
                        "type": "boolean",
                        "required": False,
                        "label": "Force Resubscribe",
                        "default": False,
                        "help": "Re-apply every route even where the control box thinks it "
                        "already matches.",
                    },
                },
                "help": "Every display in the saved matrix switches to the source the "
                "matrix gives it.",
            },
            # ── Video walls ───────────────────────────────────────────
            "recall_videowall_layout": {
                "label": "Recall Video Wall Layout",
                "params": {
                    "layout": {
                        "type": "string",
                        "required": True,
                        "label": "Layout",
                        "options_state": "videowall_layout_options",
                        "help": "A wall and one of its layouts. Walls and layouts are built "
                        "in the MXNet management interface; this activates one.",
                    },
                },
                "help": "The video wall changes to the chosen layout.",
            },
            "set_videowall_source": {
                "label": "Set Video Wall Source",
                "params": {
                    "layout": {
                        "type": "string",
                        "required": True,
                        "label": "Layout",
                        "options_state": "videowall_layout_options",
                    },
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "New Source (Encoder)",
                    },
                    "position": {
                        "type": "string",
                        "required": False,
                        "label": "Screen",
                        "default": "ALL",
                        "pattern": r"^(?:\d{1,2}:\d{1,2}|[Aa][Ll][Ll])$",
                        "help": "Row and column of one screen, top left is 1:1, or ALL for "
                        "every screen in the layout.",
                    },
                    "replace": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": False,
                        "label": "Instead, Replace Source",
                        "help": "Optional. Every screen in the layout showing this encoder "
                        "switches to the new source, and Screen is ignored.",
                    },
                },
                "help": "The chosen screens of the video wall layout show the new source. "
                "Needs control box firmware 3.42.5 or later.",
            },
            "activate_multiview": {
                "label": "Activate Video Wall Multiview",
                "params": {
                    "layout": {
                        "type": "string",
                        "required": True,
                        "label": "Layout",
                        "options_state": "videowall_layout_options",
                    },
                    "index": {
                        "type": "string",
                        "required": True,
                        "label": "Position",
                        "pattern": r"^\d{1,3}:\d{1,3}$",
                        "help": "Row and column of the display in the layout, e.g. 2:1.",
                    },
                },
                "help": "The display at that position in the video wall layout switches to "
                "its multiview arrangement.",
            },
            # ── Multiview ─────────────────────────────────────────────
            "recall_multiview_layout": {
                "label": "Recall Multiview Layout",
                "params": {
                    "layout": {
                        "type": "string",
                        "required": True,
                        "label": "Layout",
                        "options_state": "multiview_layout_options",
                    },
                },
                "help": "The decoders assigned to the multiview layout show it.",
            },
            "assign_multiview_layout": {
                "label": "Assign Multiview Layout to Display",
                "params": {
                    "layout": {
                        "type": "string",
                        "required": True,
                        "label": "Layout",
                        "options_state": "multiview_layout_options",
                    },
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "more": {
                        "type": "string",
                        "required": False,
                        "label": "More Displays",
                        "help": "Optional. Other decoders to assign at the same time, by "
                        "name or MAC, separated by colons.",
                    },
                },
                "help": "The display (or displays) take the multiview layout as theirs. "
                "Recall the layout to show it.",
            },
            "set_multiview_window_source": {
                "label": "Set Multiview Window Source",
                "params": {
                    "layout": {
                        "type": "string",
                        "required": True,
                        "label": "Layout",
                        "options_state": "multiview_layout_options",
                    },
                    "window": {
                        "type": "integer",
                        "required": True,
                        "label": "Window",
                        "min": 1,
                        "max": 16,
                        "help": "Window number. An AC-MXNET-USP-ES shows up to 4 windows, "
                        "a -PL 7 and a -PR 16.",
                    },
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                },
                "help": "That window of the multiview layout shows the chosen source.",
            },
            "set_multiview_audio": {
                "label": "Set Multiview Audio",
                "params": {
                    "layout": {
                        "type": "string",
                        "required": True,
                        "label": "Layout",
                        "options_state": "multiview_layout_options",
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "label": "Audio",
                        "values": [
                            {"value": "0", "label": "Follow a window"},
                            {"value": "1", "label": "Follow a source"},
                            {"value": "2", "label": "No audio"},
                        ],
                    },
                    "window": {
                        "type": "integer",
                        "required": False,
                        "label": "Window",
                        "min": 1,
                        "max": 16,
                        "help": "For Follow a window: the window whose audio is heard.",
                    },
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": False,
                        "label": "Source (Encoder)",
                        "help": "For Follow a source: the encoder whose audio is heard.",
                    },
                },
                "help": "The multiview layout plays the audio of one window, of one source, "
                "or none.",
            },
            # ── Media player ──────────────────────────────────────────
            "upload_playlist": {
                "label": "Upload Playlist to Display",
                "params": {
                    "playlist": {
                        "type": "string",
                        "required": True,
                        "label": "Playlist",
                        "options_state": "playlist_options",
                    },
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "more": {
                        "type": "string",
                        "required": False,
                        "label": "More Displays",
                        "help": "Optional. Other decoders to upload to at the same time, by "
                        "name or MAC, separated by commas.",
                    },
                },
                "help": "The playlist's media files are copied from the control box to the "
                "decoder, ready to play. A decoder needs this before Start Playlist.",
            },
            "apply_playlist": {
                "label": "Apply Playlist to Display",
                "params": {
                    "playlist": {
                        "type": "string",
                        "required": True,
                        "label": "Playlist",
                        "options_state": "playlist_options",
                    },
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                },
                "help": "The decoder is given the playlist as the one it plays.",
            },
            "start_playlist": {
                "label": "Start Playlist",
                "params": {
                    "playlist": {
                        "type": "string",
                        "required": True,
                        "label": "Playlist",
                        "options_state": "playlist_options",
                    },
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                },
                "help": "The display starts playing the playlist. Upload it to that decoder "
                "first.",
            },
            "stop_playlist": {
                "label": "Stop Playlist",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                },
                "help": "The display stops playing its playlist.",
            },
            # ── Decoder (display side) ────────────────────────────────
            "set_output_resolution": {
                "label": "Set Display Output Timing",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "timing": {
                        "type": "enum",
                        "required": True,
                        "label": "Timing",
                        "default": "0 0 0",
                        "values": [
                            {"value": "0 0 0", "label": "Pass through (follow the source)"},
                            {"value": "1280 720 50", "label": "1280x720 50Hz"},
                            {"value": "1280 720 60", "label": "1280x720 60Hz"},
                            {"value": "1920 1080 24", "label": "1920x1080 24Hz"},
                            {"value": "1920 1080 50", "label": "1920x1080 50Hz"},
                            {"value": "1920 1080 60", "label": "1920x1080 60Hz"},
                            {"value": "3840 2160 30", "label": "3840x2160 30Hz"},
                            {"value": "3840 2160 60", "label": "3840x2160 60Hz"},
                        ],
                    },
                },
                "help": (
                    "The decoder sends its display this resolution. Pass through follows "
                    "the source and is the right answer unless a display refuses it."
                ),
            },
            "set_hdr": {
                "label": "Set HDR Mode",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "HDR",
                        "values": [
                            {"value": "1", "label": "Enabled"},
                            {"value": "0", "label": "Disabled"},
                        ],
                    },
                },
                "help": "The decoder passes HDR through to its display, or turns it off for "
                "a display that mishandles it.",
            },
            "set_hdcp": {
                "label": "Set Display HDCP Mode",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "label": "HDCP",
                        "values": [
                            {"value": "3", "label": "Follow the source"},
                            {"value": "0", "label": "No HDCP"},
                            {"value": "1", "label": "HDCP 1.x"},
                            {"value": "2", "label": "HDCP 2.x"},
                        ],
                    },
                },
                "help": (
                    "The decoder presents this HDCP version to its display. These numbers "
                    "are not the MXNet 1G's: the same value means a different mode there."
                ),
            },
            "set_decoder_stream": {
                "label": "Decoder Stream",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "Stream",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": "Off stops the decoder taking its source's stream, so its display "
                "goes blank; On starts it again.",
            },
            "set_audio_out_source": {
                "label": "Set Decoder Audio Out Source",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "source": {
                        "type": "enum",
                        "required": True,
                        "label": "Audio",
                        "values": [
                            {"value": "1", "label": "The routed encoder's audio"},
                            {"value": "2", "label": "Dante audio"},
                            {"value": "4", "label": "This endpoint's own input (loop out)"},
                        ],
                    },
                },
                "help": (
                    "The decoder's audio output plays the chosen audio. Dante audio needs "
                    "an AC-MXNET-USP-PL or -PR."
                ),
            },
            "set_audio_out_volume": {
                "label": "Set Decoder Audio Out Volume",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "level": {
                        "type": "integer",
                        "required": True,
                        "label": "Volume",
                        "min": 0,
                        "max": 100,
                        "unit": "%",
                    },
                },
                "help": "The decoder's audio output gets louder or quieter. The control box "
                "does not report the level back.",
            },
            "set_hdmi_audio_source": {
                "label": "Set Decoder HDMI Audio Source",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "source": {
                        "type": "enum",
                        "required": True,
                        "label": "Audio",
                        "values": [
                            {"value": "1", "label": "The routed encoder's audio"},
                            {"value": "2", "label": "Dante audio"},
                        ],
                    },
                },
                "help": "The display hears the chosen audio on its HDMI input. Dante audio "
                "needs an AC-MXNET-USP-PL or -PR.",
            },
            # ── Encoder (source side) ─────────────────────────────────
            "set_edid": {
                "label": "Set Encoder EDID",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "edid": {
                        "type": "enum",
                        "required": True,
                        "label": "EDID",
                        "values": [
                            {"value": "0", "label": "1080p, 2-channel audio"},
                            {"value": "1", "label": "1080p, 6-channel audio"},
                            {"value": "2", "label": "1080p 3D, 2-channel audio"},
                            {"value": "3", "label": "1080p 3D, 6-channel audio"},
                            {"value": "4", "label": "4K30 3D, 2-channel audio"},
                            {"value": "5", "label": "4K30 3D, 6-channel audio"},
                            {"value": "6", "label": "4K30 3D, 8-channel audio"},
                            {"value": "7", "label": "4K60 3D, 2-channel audio"},
                            {"value": "8", "label": "4K60 3D, 6-channel audio"},
                            {"value": "9", "label": "4K60 3D, 8-channel audio"},
                            {"value": "10", "label": "1080p, 2-channel audio, HDR"},
                            {"value": "11", "label": "1080p, 6-channel audio, HDR"},
                            {"value": "12", "label": "1080p 3D, 2-channel audio, HDR"},
                            {"value": "13", "label": "1080p 3D, 6-channel audio, HDR"},
                            {"value": "14", "label": "4K30 3D, 2-channel audio, HDR"},
                            {"value": "15", "label": "4K30 3D, 6-channel audio, HDR"},
                            {"value": "16", "label": "4K30 3D, 8-channel audio, HDR"},
                            {"value": "17", "label": "4K60 3D, 2-channel audio, HDR"},
                            {"value": "18", "label": "4K60 3D, 6-channel audio, HDR"},
                            {"value": "19", "label": "4K60 3D, 8-channel audio, HDR"},
                            {"value": "20", "label": "1920x1200 3D, 2-channel audio, HDR"},
                        ],
                    },
                },
                "help": (
                    "The encoder presents the chosen EDID to its source, which may change "
                    "the resolution and audio the source sends. This list is not the MXNet "
                    "1G's: the same index is a different EDID there."
                ),
            },
            "copy_edid": {
                "label": "Copy Display EDID to Encoder",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Copy From (Decoder)",
                    },
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Copy To (Encoder)",
                    },
                },
                "help": "The encoder presents its source the EDID of the display on the "
                "chosen decoder.",
            },
            "set_hdmi_input": {
                "label": "Enable Encoder HDMI Input",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "Enabled",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": "Off turns the encoder's HDMI input off, so every display showing "
                "it loses picture; On turns it back on.",
            },
            "set_hdcp_request": {
                "label": "Set Encoder HDCP Request",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "mode": {
                        "type": "enum",
                        "required": True,
                        "label": "HDCP",
                        "values": [
                            {"value": "0", "label": "Auto"},
                            {"value": "1", "label": "Do not request HDCP"},
                        ],
                    },
                },
                "help": "With Do not request HDCP, the encoder tells its source it does not "
                "accept HDCP, so a source that can send unprotected content does.",
            },
            "set_source_input": {
                "label": "Select Encoder Input",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "input": {
                        "type": "enum",
                        "required": True,
                        "label": "Input",
                        "values": [
                            {"value": "auto", "label": "Auto"},
                            {"value": "hdmi", "label": "HDMI"},
                            {"value": "usb", "label": "USB-C"},
                        ],
                    },
                },
                "help": (
                    "The encoder takes its picture from HDMI or USB-C, or picks whichever "
                    "is live. Only an AC-MXNET-USP-PR accepts this; any other encoder "
                    "refuses it."
                ),
            },
            "set_open_stream": {
                "label": "Open Stream On/Off",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "Open Stream",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": (
                    "On starts the encoder's AVPro Flow open stream (RTSP or RTMP, no HDCP) "
                    "that third-party players can open; Off stops it."
                ),
            },
            "configure_open_stream": {
                "label": "Configure Open Stream",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "encoding": {
                        "type": "enum",
                        "required": True,
                        "label": "Encoding",
                        "default": "h264",
                        "values": [
                            {"value": "h264", "label": "H.264"},
                            {"value": "h265", "label": "H.265"},
                        ],
                    },
                    "profile": {
                        "type": "enum",
                        "required": True,
                        "label": "Profile",
                        "default": "main",
                        "values": [
                            {"value": "baseline", "label": "Baseline (H.264)"},
                            {"value": "main", "label": "Main (H.264 or H.265)"},
                            {"value": "high", "label": "High (H.264)"},
                            {"value": "main10", "label": "Main10 (H.265)"},
                        ],
                    },
                    "protocol": {
                        "type": "enum",
                        "required": True,
                        "label": "Protocol",
                        "default": "rtsp",
                        "values": [
                            {"value": "rtsp", "label": "RTSP"},
                            {"value": "rtmp", "label": "RTMP"},
                        ],
                    },
                    "stream_key": {
                        "type": "string",
                        "required": True,
                        "label": "Stream Key",
                        "pattern": r"^\S{1,128}$",
                        "help": "The stream's key or name, with no spaces.",
                    },
                    "bitrate": {
                        "type": "integer",
                        "required": True,
                        "label": "Bitrate",
                        "default": 25000,
                        "min": 1,
                        "help": "As the encoder takes it; AVPro's example is 25000.",
                    },
                    "rate_control": {
                        "type": "string",
                        "required": True,
                        "label": "Rate Control",
                        "default": "cbr",
                        "pattern": r"^[A-Za-z]{2,8}$",
                        "help": "As the encoder names it; AVPro's example is cbr.",
                    },
                    "gop": {
                        "type": "integer",
                        "required": True,
                        "label": "GOP",
                        "default": 30,
                        "min": 1,
                        "help": "Frames between key frames; AVPro's example is 30.",
                    },
                },
                "help": (
                    "The encoder's AVPro Flow open stream starts using these settings. "
                    "Pick a profile that belongs to the encoding."
                ),
            },
            "set_stream_bridge": {
                "label": "Open Stream Bridge On/Off",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "Bridge",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": "On lets players on the control network reach the encoder's open "
                "stream through the control box; Off stops it.",
            },
            # ── Any endpoint ──────────────────────────────────────────
            "identify": {
                "label": "Identify Endpoint",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "mode": {
                        "type": "enum",
                        "required": False,
                        "label": "LED",
                        "default": "flash",
                        "values": [
                            {"value": "flash", "label": "Flash"},
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": "The endpoint's front LED flashes (or stays on, or goes back to "
                "normal) so you can find it in the rack.",
            },
            "reboot_endpoint": {
                "label": "Reboot Endpoint",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                },
                "help": "The encoder or decoder restarts; its picture drops for the time it "
                "takes to come back.",
            },
            "set_osd": {
                "label": "On-Screen Display",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "OSD",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": "Shows or hides the endpoint's on-screen information on its display.",
            },
            "hpd_reset": {
                "label": "Reset Hot Plug",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "port": {
                        "type": "enum",
                        "required": False,
                        "label": "Port",
                        "default": "auto",
                        "values": [
                            {"value": "auto", "label": "Its own (encoder input, decoder output)"},
                            {"value": "1", "label": "HDMI input"},
                            {"value": "2", "label": "HDMI output"},
                        ],
                    },
                },
                "help": "The source or display on that port sees the cable unplugged and "
                "plugged back in, the usual fix for one that handshakes but shows nothing.",
            },
            "cec_power": {
                "label": "CEC Power",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "state": {
                        "type": "enum",
                        "required": True,
                        "label": "Power",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": (
                    "The display or source on the endpoint's HDMI port turns on or off over "
                    "CEC. The control box sends the common power sequences, so this works "
                    "without knowing the device's CEC address."
                ),
            },
            "send_cec": {
                "label": "Send CEC",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "data": {
                        "type": "string",
                        "required": True,
                        "label": "CEC Data (hex)",
                        "pattern": r"^[0-9A-Fa-f]{2,}(?:\s*,\s*[0-9A-Fa-f]{2,})*$",
                        "help": "Hex, no spaces, e.g. 0036. Separate several messages with "
                        "commas to send them in order.",
                    },
                },
                "help": "The CEC message goes out of the endpoint's HDMI port, and the "
                "attached display or source acts on it. Use CEC Power for on and off.",
            },
            "send_ir": {
                "label": "Send IR",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "code": {
                        "type": "string",
                        "required": True,
                        "label": "IR Code",
                        "trim": False,
                        "help": "Pronto hex, exactly as the code set gives it.",
                    },
                },
                "help": "The IR code goes out of the endpoint's IR emitter, and the device "
                "it points at acts on it.",
            },
            "send_serial": {
                "label": "Send Serial",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "data": {
                        "type": "string",
                        "required": True,
                        "label": "Data",
                        "trim": False,
                        "help": "ASCII text, or space-separated hex bytes when Format is Hex.",
                    },
                    "format": {
                        "type": "enum",
                        "required": False,
                        "label": "Format",
                        "default": "ascii",
                        "values": [
                            {"value": "ascii", "label": "ASCII"},
                            {"value": "hex", "label": "Hex"},
                        ],
                    },
                    "append_cr": {
                        "type": "boolean",
                        "required": False,
                        "label": "Append CR",
                        "default": False,
                        "help": "Add a carriage return; most serial devices need one.",
                    },
                },
                "help": (
                    "The data goes out of the endpoint's RS-232 port and the attached "
                    "device acts on it. Anything it sends back lands on that endpoint's "
                    "Serial Data."
                ),
            },
            "set_serial_settings": {
                "label": "Set Serial Port Settings",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "baud": {
                        "type": "enum",
                        "required": True,
                        "label": "Baud Rate",
                        "default": "9600",
                        "values": [
                            "300",
                            "600",
                            "1200",
                            "2400",
                            "4800",
                            "9600",
                            "19200",
                            "38400",
                            "57600",
                            "115200",
                        ],
                    },
                    "data_bits": {
                        "type": "enum",
                        "required": False,
                        "label": "Data Bits",
                        "default": "8",
                        "values": ["6", "7", "8"],
                    },
                    "parity": {
                        "type": "enum",
                        "required": False,
                        "label": "Parity",
                        "default": "0",
                        "values": [
                            {"value": "0", "label": "None"},
                            {"value": "1", "label": "Even"},
                            {"value": "2", "label": "Odd"},
                        ],
                    },
                    "stop_bits": {
                        "type": "enum",
                        "required": False,
                        "label": "Stop Bits",
                        "default": "1",
                        "values": ["1", "2"],
                    },
                },
                "help": "The endpoint's RS-232 port uses these settings, to match the device "
                "plugged into it.",
            },
            "set_serial_feedback": {
                "label": "Set Serial Feedback Format",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "format": {
                        "type": "enum",
                        "required": True,
                        "label": "Format",
                        "values": [
                            {"value": "ascii", "label": "ASCII"},
                            {"value": "hex", "label": "Hex"},
                            {"value": "base64", "label": "Base64"},
                        ],
                    },
                },
                "help": (
                    "The control box relays serial data coming back from this endpoint in "
                    "the chosen form. Keep it the same as this device's Serial Feedback "
                    "Format setting, or Serial Data is not readable."
                ),
            },
            "rename_encoder": {
                "label": "Rename Encoder",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "New Name",
                        "pattern": r"^(?![Aa][Ll][Ll](?:[RrTt][Xx])?$)[^\s:,]{1,32}$",
                        "help": "No spaces, colons or commas, and not ALL, ALLRX or ALLTX.",
                    },
                },
                "help": "The encoder's name changes here and in the MXNet management "
                "interface.",
            },
            "rename_decoder": {
                "label": "Rename Decoder",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "New Name",
                        "pattern": r"^(?![Aa][Ll][Ll](?:[RrTt][Xx])?$)[^\s:,]{1,32}$",
                        "help": "No spaces, colons or commas, and not ALL, ALLRX or ALLTX.",
                    },
                },
                "help": "The decoder's name changes here and in the MXNet management "
                "interface.",
            },
            "describe_encoder": {
                "label": "Describe Encoder",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
                    },
                    "description": {
                        "type": "string",
                        "required": True,
                        "label": "Description",
                        "help": "Free text: where it is, what feeds it. Spaces are fine.",
                    },
                },
                "help": "The encoder's description in the MXNet management interface "
                "changes. The control box does not report it back.",
            },
            "describe_decoder": {
                "label": "Describe Decoder",
                "params": {
                    "rx": {
                        "type": "child_id",
                        "child_type": "decoder",
                        "required": True,
                        "label": "Display (Decoder)",
                    },
                    "description": {
                        "type": "string",
                        "required": True,
                        "label": "Description",
                        "help": "Free text: where it is, what it feeds. Spaces are fine.",
                    },
                },
                "help": "The decoder's description in the MXNet management interface "
                "changes. The control box does not report it back.",
            },
            "set_device_mode": {
                "label": "Set Endpoint Mode",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "encoder": {
                        "type": "enum",
                        "required": True,
                        "label": "Encoder",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                    "decoder": {
                        "type": "enum",
                        "required": True,
                        "label": "Decoder",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": (
                    "The endpoint works as an encoder, a decoder or both. An encoder-only "
                    "endpoint's HDMI output becomes a loop-out of its input. The roster is "
                    "read again afterwards."
                ),
            },
            # ── Dante (AC-MXNET-USP-PL and -PR) ──────────────────────
            "set_dante_name": {
                "label": "Set Dante Device Name",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "name": {
                        "type": "string",
                        "required": True,
                        "label": "Dante Name",
                        "pattern": r"^\S{1,31}$",
                        "help": "No spaces.",
                    },
                },
                "help": "The endpoint's Dante module appears under this name in Dante "
                "Controller. AC-MXNET-USP-PL and -PR only.",
            },
            "set_dante_description": {
                "label": "Set Dante Description",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "description": {
                        "type": "string",
                        "required": True,
                        "label": "Description",
                        "help": "Spaces are fine.",
                    },
                },
                "help": "The endpoint's Dante module carries this description. "
                "AC-MXNET-USP-PL and -PR only.",
            },
            "set_dante_ip_dhcp": {
                "label": "Set Dante IP to DHCP",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                },
                "help": (
                    "The endpoint's Dante module takes its address from DHCP and restarts, "
                    "so its Dante audio drops for a moment. AC-MXNET-USP-PL and -PR only."
                ),
            },
            "set_dante_ip_static": {
                "label": "Set Dante IP to Static",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                    "ip": {
                        "type": "string",
                        "required": True,
                        "label": "IP Address",
                        "pattern": r"^(?:\d{1,3}\.){3}\d{1,3}$",
                    },
                    "netmask": {
                        "type": "string",
                        "required": True,
                        "label": "Subnet Mask",
                        "default": "255.255.255.0",
                        "pattern": r"^(?:\d{1,3}\.){3}\d{1,3}$",
                    },
                    "gateway": {
                        "type": "string",
                        "required": True,
                        "label": "Gateway",
                        "pattern": r"^(?:\d{1,3}\.){3}\d{1,3}$",
                    },
                },
                "help": (
                    "The endpoint's Dante module takes this fixed address and restarts, so "
                    "its Dante audio drops for a moment. AC-MXNET-USP-PL and -PR only."
                ),
            },
            "read_dante_ip": {
                "label": "Read Dante IP Settings",
                "params": {
                    "endpoint": {
                        "type": "string",
                        "required": True,
                        "label": "Endpoint",
                        "options_state": "endpoint_options",
                    },
                },
                "help": "The endpoint's Dante IP mode, address, mask and gateway are read "
                "into its Dante states. AC-MXNET-USP-PL and -PR only.",
            },
            # ── System ────────────────────────────────────────────────
            "refresh": {
                "label": "Refresh",
                "params": {},
                "help": "The endpoint roster, routes and status are read again now.",
            },
            "sync_clock": {
                "label": "Set Clock From Server",
                "params": {},
                "help": (
                    "The control box's clock is set to this server's current time. Only "
                    "needed when no NTP server is reachable."
                ),
            },
            "reboot_cbox": {
                "label": "Reboot Control Box",
                "params": {},
                # The control box drops off the network while it restarts. Saying
                # so is what stops the platform reporting a fault and alerting on
                # it: it shows a counting-down "restarting" instead, and the
                # window ends the moment the box answers again. Not measured on
                # hardware -- over-stating it only costs a longer countdown,
                # while understating it raises a fault on a healthy reboot.
                # A literal rather than a constant on purpose: the contract
                # check reads this file's source, and a name here would hide
                # the field that sets the driver's platform floor.
                "restarts_device_for": 120,
                "help": "The control box restarts. Endpoints keep passing video meanwhile; "
                "control is unavailable until it comes back.",
            },
            "raw_command": {
                "label": "Raw API Command",
                "params": {
                    "command": {
                        "type": "string",
                        "required": True,
                        "label": "Command",
                        "trim": False,
                        "help": "A raw MXNet API line, e.g. 'config get devicelist'.",
                    },
                },
                "help": "Sends any MXNet API command and returns the control box's reply. "
                "For commissioning and troubleshooting.",
            },
        },
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any) -> None:
        # Roster bookkeeping. Children are keyed by MAC (stable and unique); the
        # control box reports an endpoint by its CURRENT id, which is its MAC
        # until somebody renames it and its custom name afterwards, so we keep
        # both maps. An endpoint has an encoder name and a decoder name, and a
        # name set from here is remembered too, so a reply naming either one
        # resolves.
        self._known: dict[str, set[str]] = {"encoder": set(), "decoder": set()}
        self._mac_by_name: dict[str, str] = {}
        self._alias_mac: dict[str, str] = {}
        self._type_by_mac: dict[str, str] = {}
        self._name_by_mac: dict[str, str] = {}

        # wall name -> [layout names]
        self._walls: dict[str, list[str]] = {}
        # Multiview layout and playlist names; None until a list has been read.
        self._mv_layouts: list[str] | None = None
        self._playlists: list[str] | None = None

        # decoder MAC -> {route property: (commanded value, deadline)}
        self._route_expect: dict[str, dict[str, tuple[str, float]]] = {}
        self._request_lock = asyncio.Lock()
        self._pending: asyncio.Future | None = None
        self._pending_cmd: str | None = None
        self._poll_cycle = 0
        self._poll_misses = 0
        super().__init__(device_id, config, state, events)

    # ── Lifecycle ────────────────────────────────────────────────────

    async def _pre_connect(self) -> None:
        if not str(self.config.get("host", "")).strip():
            raise ValueError("No IP address configured")

    def _transport_kwargs(self, transport_type: str, kwargs: dict) -> dict:
        # Replies are framed by _json_frame, not by a delimiter: the API
        # document states no line terminator and not every reply is JSON.
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self) -> CallableFrameParser:
        return CallableFrameParser(_json_frame)

    async def _post_connect(self) -> None:
        # Confirm this really is a USP control box before reporting connected,
        # then learn the endpoint roster and put every endpoint on the serial
        # encapsulation we decode. A failure here aborts the attempt.
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 24))
        doc = await self._request("config get name")
        if doc is None:
            raise ConnectionError(
                f"[{self.device_id}] No answer from the MXNet API on {host}:{port}. "
                f"Check that this is the control box's control network port."
            )
        model = _txt(doc.get("info"))
        self.set_state("model", model)
        # A 1G, 10G or Dante control box answers this query too, and its
        # command set is different enough that a wrong pairing looks like a
        # broken device rather than a wrong driver. Say which one it is instead.
        refusal = _model_refusal(model)
        if refusal:
            raise ConnectionError(f"[{self.device_id}] {host}:{port} {refusal}")
        if model and "USP" not in model.upper():
            log.info(
                f"[{self.device_id}] {model} does not name USP; treating it as an MXNet "
                f"USP control box"
            )
        await self._enumerate_roster()
        await self._normalize_serial_feedback()

    async def _initial_sync(self) -> None:
        # First full read; steady-state polling starts right after this.
        await self.poll()

    async def _close_session(self) -> None:
        # Runs on every teardown path: abort any in-flight request so its
        # awaiter does not hang on a dead link, and zero the poll bookkeeping so
        # the next connection starts fresh.
        pending = self._pending
        if pending is not None and not pending.done():
            pending.cancel()
        self._pending = None
        self._pending_cmd = None
        self._route_expect.clear()
        self._poll_cycle = 0
        self._poll_misses = 0

    # ── Serialized request/response ──────────────────────────────────

    async def _request(self, line: str, timeout: float | None = None) -> dict[str, Any] | None:
        """Send one API line and await its reply.

        Requests are serialized AND matched by the reply's `cmd` echo (unwrapped
        from the escaped-JSON form where the reply uses it), which the 1G
        firmware in this family echoes byte-exactly. Matching on the echo rather
        than on arrival order is what makes the driver immune to the two frames
        that would otherwise be mistaken for a reply:

          * an unsolicited event, which the control box interleaves into the
            same connection (see on_data_received), and
          * the late reply to a request that already timed out, which would
            otherwise be handed to the NEXT request and shift every reply after
            it by one.

        The route query and the `vwid` commands have no echo to match on,
        because their replies are not JSON at all. Those are returned as
        `{"cmd": <the request>, "info": <the raw text>, "code": 0}` so the rest
        of the driver sees one shape -- and a raw frame is only ever accepted
        for a request that expects one.

        Returns the parsed object, or None on timeout.
        """
        if timeout is None:
            timeout = REQUEST_TIMEOUT_S
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")

        async with self._request_lock:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending = fut
            self._pending_cmd = line
            try:
                await self.transport.send((line + "\r\n").encode("utf-8"))
                try:
                    return await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    log.warning(f"[{self.device_id}] No reply to: {line}")
                    return None
            finally:
                if self._pending is fut:
                    self._pending = None
                    self._pending_cmd = None

    async def _write(self, line: str) -> bool:
        """Send a write command and report whether the control box accepted it."""
        doc = await self._request(line)
        if doc is None:
            raise ConnectionError(f"[{self.device_id}] No reply to: {line}")
        detail = _refusal(doc)
        if detail is not None:
            raise ValueError(f"The control box rejected '{line}': {detail}")
        return True

    @staticmethod
    def _expects_raw(line: str) -> bool:
        """Whether the reply to this request is documented as something other than JSON.

        The `vwid` family answers with a Lua table, a bare "OK", or (for the
        multiview writes) JSON with unescaped quotes inside `cmd`; the route
        query answers with a Lua table.
        """
        low = line.lower()
        return low.startswith("vwid") or low.startswith("config get device routes")

    def _echo_matches(self, echo: str, expected: str) -> bool:
        """Whether a reply's echo answers the request in flight.

        Exact, apart from whitespace -- with one allowance: IPCBox USP Commands
        V1.08 shows `hdcprequest` sent to a MAC echoing the endpoint's custom
        name (`... hdcprequest 1 TX1`). So the same command naming its target
        another way is accepted when both names resolve to the same endpoint.
        A reply to a different command, or to the same command for a different
        endpoint, is still refused.
        """
        got, want = _flat(echo), _flat(expected)
        if got == want:
            return True
        got_head, _, got_target = got.rpartition(" ")
        want_head, _, want_target = want.rpartition(" ")
        if not got_head or got_head.lower() != want_head.lower():
            return False
        mac = self._resolve(got_target)
        return mac is not None and mac == self._resolve(want_target)

    async def on_data_received(self, data: bytes) -> None:
        # The frame parser emits an empty frame when it discards inter-object
        # whitespace (see _json_frame); there is nothing to parse in one.
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return

        doc: dict[str, Any] | None = None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            doc = parsed

        if doc is None:
            # A Lua table, a bare token, or the malformed JSON the multiview
            # writes answer with. There is no echo in any of those, so it can
            # only be matched to the request in flight -- and only when that
            # request was one that answers this way.
            self._deliver_raw(text)
            return

        # An unsolicited event: empty `cmd` plus a `source` naming the channel.
        # Undocumented for USP; the 1G firmware emits AV-info events with
        # source "mxnet" and serial data with source "rs232". Anything with an
        # empty cmd is treated as an event whatever its source, because letting
        # one be consumed as a reply desynchronises every reply after it.
        if not _txt(doc.get("cmd")) and doc.get("source") is not None:
            if _txt(doc.get("source")).lower() == "rs232":
                self._apply_serial(doc)
            else:
                self._apply_event(doc)
            return

        pending = self._pending
        if pending is None or pending.done():
            return
        echo = _echo(doc)
        expected = self._pending_cmd
        if echo and expected is not None and not self._echo_matches(echo, expected):
            # A reply to something we already gave up on. Handing it to the
            # current waiter would answer this request with the previous
            # request's data and desynchronise everything after it.
            log.debug(
                f"[{self.device_id}] Ignoring stale reply to {echo!r} "
                f"while awaiting {expected!r}"
            )
            return
        pending.set_result(doc)

    def _deliver_raw(self, text: str) -> None:
        """Hand a non-JSON reply to the request that asked for one."""
        pending = self._pending
        expected = self._pending_cmd
        if pending is None or pending.done() or expected is None:
            log.debug(f"[{self.device_id}] Unsolicited non-JSON frame ignored: {text[:80]!r}")
            return
        if _flat(text.strip('"')) == _flat(expected):
            # The request line read back, which the document prints above each
            # Lua reply. It is not the answer; the table follows it.
            return
        if not self._expects_raw(expected):
            log.warning(
                f"[{self.device_id}] Unreadable reply while awaiting {expected!r}: {text[:120]!r}"
            )
            return
        # No `code` member exists in any of these shapes, so acceptance is read
        # from the text: the control box says OK, and says error when it does not.
        failed = "error" in text.lower() or '"code":-1' in text.replace(" ", "")
        pending.set_result({"cmd": expected, "info": text, "code": -1 if failed else 0})

    def _apply_event(self, doc: dict[str, Any]) -> None:
        """An unsolicited AV event, fanned out into child state.

        The USP API document does not mention this channel; the grammar below
        was read off a 1G control box, which is the same firmware family.
        Events land within about a second of the physical change, where a poll
        can be up to `poll_interval` behind.

            IN1 HPD 1
            OUT1 HPD 0
            OUT1 AV INF 1920X1080p@59Hz,RGB,8Bit,HDR OFF,HDCP OFF,PCM

        Polling stays the authority: an event only ever refreshes a field the
        poll also writes, so an unrecognised or malformed event costs nothing
        but a stale value until the next cycle. The raw line is always kept on
        `last_event`.
        """
        mac = self._resolve(_txt(doc.get("mac")) or _txt(doc.get("id")))
        if mac is None:
            return
        payload = _txt(doc.get("info"))
        if not payload:
            return
        ctype = self._type_by_mac[mac]
        updates: dict[str, Any] = {"last_event": payload}

        hpd = _RE_EVENT_HPD.match(payload)
        if hpd:
            connected = hpd.group(2) == "1"
            updates["display_connected" if ctype == "decoder" else "source_connected"] = connected

        av = _RE_EVENT_AVINF.match(payload)
        if av:
            fields = [f.strip() for f in av.group(2).split(",")]
            # <timing>,<chroma>,<depth>,HDR <x>,HDCP <x>,<audio>. A signal-less
            # port sends the same shape with the members empty ("@,,,HDR OFF,
            # HDCP ON,"), which must clear the state rather than be parsed.
            while len(fields) < 6:
                fields.append("")
            timing, chroma, depth, hdr, hdcp, audio = fields[:6]
            updates["resolution"] = _resolution(timing)
            updates["chroma"] = chroma
            updates["color_depth"] = depth
            updates["hdr"] = hdr.upper().endswith("ON")
            updates["hdcp"] = _hdcp(hdcp)
            updates["audio_format"] = audio
            if ctype == "encoder":
                updates["signal_present"] = bool(_resolution(timing))

        self.set_children_state_batch([(ctype, mac, updates)])

    def _apply_serial(self, doc: dict[str, Any]) -> None:
        """An endpoint received RS-232 data and the control box relayed it."""
        mac = self._resolve(_txt(doc.get("mac")) or _txt(doc.get("id")))
        if mac is None:
            return
        payload = _txt(doc.get("info"))
        fmt = str(self.config.get("serial_feedback_format", "ascii")).lower()
        if fmt == "base64" and payload:
            try:
                payload = base64.b64decode(payload, validate=True).decode(
                    "utf-8", errors="replace"
                )
            except (binascii.Error, ValueError):
                log.debug(f"[{self.device_id}] Serial payload is not valid base64; kept raw")
        self.set_child_state(self._type_by_mac[mac], mac, "serial_data", payload)

    async def _normalize_serial_feedback(self) -> None:
        """Put every endpoint on the serial encapsulation this driver decodes."""
        fmt = str(self.config.get("serial_feedback_format", "ascii")).lower()
        code = SERIAL_FORMATS.get(fmt)
        if code is None:
            return
        doc = await self._request(f"config set device rs232responsetype {code} ALL")
        if doc is None or _refusal(doc) is not None:
            log.warning(
                f"[{self.device_id}] Could not set the serial feedback format to {fmt}; "
                f"inbound serial data may not decode"
            )

    # ── Roster ───────────────────────────────────────────────────────

    async def _enumerate_roster(self) -> None:
        """Read the control box's device database and reconcile the children."""
        doc = await self._request("config get devicelist", timeout=ROSTER_TIMEOUT_S)
        if doc is None:
            raise ConnectionError(
                f"[{self.device_id}] The control box did not answer the endpoint roster query"
            )
        info = doc.get("info")
        if not isinstance(info, dict):
            raise ConnectionError(
                f"[{self.device_id}] The control box returned no endpoint list. Commission "
                f"the system before adding it here."
            )
        self._apply_devicelist(info)

    def _apply_devicelist(self, info: dict[str, Any]) -> None:
        found: dict[str, set[str]] = {"encoder": set(), "decoder": set()}
        updates: list[tuple[str, str, dict[str, Any]]] = []
        offline = 0
        entries = {k: v for k, v in info.items() if isinstance(v, dict)}
        present = _presence(entries)
        names: dict[str, str] = {}

        for key, entry in entries.items():
            mac = self._mac_of(key, entry)
            if mac is None:
                log.debug(f"[{self.device_id}] Skipping roster entry with no MAC: {key}")
                continue

            # An encoder is the host of its own stream channel; a decoder
            # subscribes to one. `is_host` is the control box's discriminator.
            # An endpoint set to both modes (`devicemode host client`) reports
            # is_host 1 AND the decoder's channel members; it is an encoder here,
            # as the MXNet 10G driver classifies the same entry, so its decoder
            # half is not offered as a display.
            ctype = "encoder" if str(entry.get("is_host", "")) == "1" else "decoder"
            name = _txt(entry.get("id")) or mac
            online = present.get(key, False)
            if not online:
                offline += 1

            found[ctype].add(mac)
            self._type_by_mac[mac] = ctype
            self._name_by_mac[mac] = name
            names[name.lower()] = mac

            ip = _txt(entry.get("ip"))
            common: dict[str, Any] = {
                "name": name,
                # Presence, plus WHY when it is missing. The control box keeps
                # an endpoint in its database after it stops answering (only a
                # deleted one disappears entirely, and that endpoint is
                # deregistered below), so an endpoint here with a stale
                # heartbeat is one the control box still expects and cannot
                # reach -- go and find it.
                #
                # `service_state` stays its own property and is NOT read as a
                # fault: an encoder with no source sits in `s_attaching`
                # indefinitely while being perfectly present.
                **(
                    self.child_fault()
                    if online
                    else self.child_fault(CHILD_NOT_RESPONDING)
                ),
                "mac": mac,
                "ip": ip,
                # `modelname` is the product an integrator recognises; `dtype`
                # is the chipset family (ast152x). The USP document's examples
                # only carry `dtype`.
                "model": _txt(entry.get("modelname")) or _txt(entry.get("dtype")),
                "firmware": _txt(entry.get("version")),
                "service_state": _txt(entry.get("state")),
            }
            if ctype == "encoder":
                common["channel"] = _txt(entry.get("ch"))
                if "edid" in entry:
                    common["edid"] = _txt(entry.get("edid"))
                if "exstream" in entry:
                    common["open_stream"] = _txt(entry.get("exstream")).lower() in (
                        "1", "on", "true"
                    )
                url = self._preview_url(ip)
                common["preview_url"] = url
                common["preview_format"] = "mjpeg" if url else ""

            if mac not in self._known[ctype]:
                self.register_child(ctype, mac, initial_state={"name": name, "mac": mac})
                self._known[ctype].add(mac)
            updates.append((ctype, mac, common))

        self._mac_by_name = names
        if updates:
            self.set_children_state_batch(updates)

        # Endpoints removed from the database disappear entirely. One that is
        # merely unplugged stays in the list without its heartbeat, so it keeps
        # its child and just goes online=False.
        for ctype in ("encoder", "decoder"):
            for mac in list(self._known[ctype] - found[ctype]):
                if mac in found["encoder"] or mac in found["decoder"]:
                    # Changed sides (Set Endpoint Mode): registered above under
                    # its new type; only the old child goes.
                    self.deregister_child(ctype, mac)
                    self._known[ctype].discard(mac)
                    if ctype == "decoder":
                        self._route_expect.pop(mac, None)
                    continue
                self.deregister_child(ctype, mac)
                self._known[ctype].discard(mac)
                self._type_by_mac.pop(mac, None)
                self._name_by_mac.pop(mac, None)
                self._route_expect.pop(mac, None)
                for alias in [a for a, m in self._alias_mac.items() if m == mac]:
                    self._alias_mac.pop(alias, None)

        self.set_states(
            {
                "encoder_count": len(found["encoder"]),
                "decoder_count": len(found["decoder"]),
                "offline_endpoints": offline,
            }
        )
        self._publish_endpoint_options()

    @staticmethod
    def _mac_of(key: str, entry: dict[str, Any]) -> str | None:
        """The endpoint's MAC, which is the only identifier a rename survives.

        The roster is KEYED by the endpoint's current id -- its MAC until
        somebody renames it and its custom name afterwards -- so keying children
        off that would deregister every renamed endpoint and orphan its
        bindings. The `mac` member is what stays put.
        """
        mac = _txt(entry.get("mac")) or _txt(key)
        return mac.upper() if _RE_MAC.match(mac) else None

    def _preview_url(self, ip: str) -> str:
        """The control box's proxy for an encoder's live MJPEG preview."""
        host = str(self.config.get("host", "")).strip()
        if not host or not ip:
            return ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{PREVIEW_PORT}/?ip={ip}&port={PREVIEW_ENCODER_PORT}"

    def _publish_endpoint_options(self) -> None:
        encoders = [
            {"value": mac, "label": self._name_by_mac.get(mac, mac)}
            for mac in sorted(self._known["encoder"], key=lambda m: self._name_by_mac.get(m, m))
        ]
        decoders = [
            {"value": mac, "label": self._name_by_mac.get(mac, mac)}
            for mac in sorted(self._known["decoder"], key=lambda m: self._name_by_mac.get(m, m))
        ]
        everything = [{**opt, "label": f"{opt['label']} (Encoder)"} for opt in encoders] + [
            {**opt, "label": f"{opt['label']} (Decoder)"} for opt in decoders
        ]
        self.set_states(
            {
                "encoder_options": json.dumps(encoders),
                "decoder_options": json.dumps(decoders),
                "endpoint_options": json.dumps(everything),
            }
        )

    async def refresh_children(self) -> dict[str, Any]:
        await self._enumerate_roster()
        self._poll_cycle = 0
        await self.poll()
        return {
            "encoders": len(self._known["encoder"]),
            "decoders": len(self._known["decoder"]),
        }

    # ── Response fan-out ─────────────────────────────────────────────

    def _apply_status(self, info: dict[str, Any]) -> None:
        """`config get device status ALL` -- one reply, every endpoint's AV status."""
        updates: list[tuple[str, str, dict[str, Any]]] = []
        for key, entry in info.items():
            if not isinstance(entry, dict):
                continue
            mac = self._resolve(key)
            if mac is None:
                continue
            ctype = self._type_by_mac[mac]

            common: dict[str, Any] = {
                "resolution": _resolution(entry.get("video")),
                "audio_format": _txt(entry.get("audio")),
                "hdcp": _hdcp(entry.get("hdcp")),
                "hdr": _flag(entry.get("hdr"), "HDR"),
                "color_depth": _txt(entry.get("colordepth")),
                "link_speed": _txt(entry.get("speed")),
            }
            if "chroma" in entry:
                common["chroma"] = _txt(entry.get("chroma"))
            if ctype == "encoder":
                common["signal_present"] = _has_signal(entry.get("video"))
                # HPD and a valid timing are different questions: a sleeping
                # laptop leaves the cable detected with no signal, which is
                # exactly the case someone is troubleshooting.
                common["source_connected"] = _flag(entry.get("hpd"), "HPD")
                if "edid" in entry:
                    common["edid"] = _txt(entry.get("edid"))
            else:
                common["display_connected"] = _flag(entry.get("hpd"), "HPD")
            updates.append((ctype, mac, common))

        if updates:
            self.set_children_state_batch(updates)

    def _apply_routes(self, info: Any) -> None:
        """`config get device routes vaurs ALLRX` -- every decoder's five routes.

        IPCBox USP Commands V1.08 shows the reply as a Lua table (read by
        _lua_routes); a JSON object of the same content, which is what the 1G
        firmware sends, is read as well. Decoder keys and source values are the
        endpoint's current id in either case, resolved back to the child id
        (see "Reading the routes" in the module docstring).

        Routes accepted moments ago are held at their commanded value until the
        control box catches up -- see _expect_routes for why.
        """
        if isinstance(info, dict):
            table = {
                str(k): {str(p).lower(): _txt(v) for p, v in e.items()}
                for k, e in info.items()
                if isinstance(e, dict)
            }
        else:
            table = _lua_routes(_txt(info))

        updates: list[tuple[str, str, dict[str, Any]]] = []
        for key, entry in table.items():
            mac = self._resolve(key)
            if mac is None or self._type_by_mac.get(mac) != "decoder":
                continue
            routes: dict[str, Any] = {}
            for wire, prop in WIRE_ROUTES.items():
                if wire not in entry:
                    continue
                source = _txt(entry.get(wire))
                if not source or source.lower() == "none":
                    routes[prop] = ""
                    continue
                routes[prop] = self._resolve(source) or source
            routes = self._settle_routes(mac, routes)
            if routes:
                updates.append(("decoder", mac, routes))

        if updates:
            self.set_children_state_batch(updates)

    def _expect_routes(self, rx: str, values: dict[str, str]) -> None:
        """Remember what a just-accepted route command asked for.

        The control box acknowledges a route long before it reports it -- on the
        1G box in this family, an ack in ~40ms and a readback up to sixteen
        seconds later, with the planes landing at different moments. With a
        ten-second poll a readback is likely to land mid-transition, so without
        this the panel would show the source the user picked, snap back to the
        old one for a cycle or two, then finally settle. That reads as a failed
        press and gets pressed again.

        So a commanded value wins over the device's answer until the device
        agrees or the window lapses. The window is a ceiling, not a delay:
        agreement clears it immediately.
        """
        deadline = time.monotonic() + ROUTE_SETTLE_S
        pending = self._route_expect.setdefault(rx, {})
        for prop, value in values.items():
            pending[prop] = (value, deadline)

    def _settle_routes(self, rx: str, reported: dict[str, str]) -> dict[str, str]:
        pending = self._route_expect.get(rx)
        if not pending:
            return reported
        now = time.monotonic()
        settled = dict(reported)
        for prop, (want, deadline) in list(pending.items()):
            if now >= deadline:
                # Time is the ceiling whether or not the device ever reported
                # this plane. A plane the firmware does not report back would
                # otherwise hold its expectation for the life of the session.
                pending.pop(prop, None)
                continue
            if prop not in settled:
                continue
            if settled[prop] == want:
                pending.pop(prop, None)
                continue
            settled[prop] = want
        if not pending:
            self._route_expect.pop(rx, None)
        return settled

    def _resolve(self, token: str) -> str | None:
        """Map a MAC or a custom name (either case) to a known child id."""
        token = _txt(token)
        if not token:
            return None
        upper = token.upper()
        if upper in self._type_by_mac:
            return upper
        low = token.lower()
        return self._mac_by_name.get(low) or self._alias_mac.get(low)

    # ── Polling ──────────────────────────────────────────────────────

    async def poll(self) -> None:
        """Refresh the roster, AV status and routes; system info on a slow cadence.

        Three broad queries cover the whole install regardless of endpoint
        count. Transport errors propagate so the platform's watchdog can flip
        the device offline (poll() contract).
        """
        doc = await self._request("config get devicelist", timeout=ROSTER_TIMEOUT_S)
        if doc is None:
            self._poll_misses += 1
            if self._poll_misses >= MAX_POLL_MISSES:
                raise ConnectionError(
                    f"[{self.device_id}] The control box stopped answering API queries "
                    f"({self._poll_misses} consecutive misses)"
                )
            return
        self._poll_misses = 0
        if isinstance(doc.get("info"), dict):
            self._apply_devicelist(doc["info"])

        doc = await self._request("config get device status ALL", timeout=ROSTER_TIMEOUT_S)
        if doc is not None and isinstance(doc.get("info"), dict):
            self._apply_status(doc["info"])

        if self._known["decoder"]:
            doc = await self._request(
                "config get device routes vaurs ALLRX", timeout=ROSTER_TIMEOUT_S
            )
            if doc is not None and _refusal(doc) is None:
                self._apply_routes(doc.get("info"))

        if self._poll_cycle % FULL_REFRESH_EVERY == 0:
            await self._poll_system()
        self._poll_cycle += 1

    async def _poll_system(self) -> None:
        doc = await self._request("config get version")
        if doc is not None:
            self.set_state("firmware", _txt(doc.get("info")))

        doc = await self._request("config get ipsetting")
        if doc is not None:
            self.set_state("av_ip", self._ip_of(doc.get("info")))

        doc = await self._request("config get ipsetting2")
        if doc is not None:
            self.set_state("lan_ip", self._ip_of(doc.get("info")))

        doc = await self._request("config get date")
        if doc is not None:
            self.set_state("system_date", _txt(doc.get("info")))

        doc = await self._request("config get timezone")
        if doc is not None:
            self.set_state("timezone", _txt(doc.get("info")))

        doc = await self._request("config get ntp")
        if doc is not None:
            servers = [s for s in re.split(r"[/\s]+", _txt(doc.get("info"))) if s]
            self.set_state("ntp_servers", " ".join(servers))

        doc = await self._request("config get dns")
        if doc is not None:
            servers = [s for s in re.split(r"[/\s]+", _txt(doc.get("info"))) if s]
            self.set_state("dns_servers", " ".join(servers))

        doc = await self._request("play getstorage")
        if doc is not None and _refusal(doc) is None:
            self.set_state("media_storage_used", _txt(doc.get("info")))

        await self._refresh_named_lists()

    async def _refresh_named_lists(self) -> None:
        """Populate the matrix, video wall, multiview and playlist pickers."""
        doc = await self._request("matrix list")
        if doc is not None:
            names = self._names_of(doc.get("info"))
            self.set_state(
                "matrix_options", json.dumps([{"value": n, "label": n} for n in names])
            )

        doc = await self._request("vwid list")
        if doc is not None:
            self._apply_walls(_txt(doc.get("info")))

        await self._refresh_multiview_layouts()
        await self._refresh_playlists()

    async def _refresh_multiview_layouts(self) -> None:
        doc = await self._request("mvid get layouts")
        if doc is None or _refusal(doc) is not None:
            return
        names = self._names_of(doc.get("info"))
        self._mv_layouts = names
        self.set_state(
            "multiview_layout_options", json.dumps([{"value": n, "label": n} for n in names])
        )

    async def _refresh_playlists(self) -> None:
        doc = await self._request("play get")
        if doc is None or _refusal(doc) is not None:
            return
        info = doc.get("info")
        # `play get` answers {"schedules": [...], "playlist": {name: [...]}}.
        playlists = info.get("playlist") if isinstance(info, dict) else None
        names = self._names_of(playlists)
        self._playlists = names
        self.set_state(
            "playlist_options", json.dumps([{"value": n, "label": n} for n in names])
        )

    def _apply_walls(self, text: str) -> None:
        """Read the wall and layout names out of the Lua-style `vwid list` reply."""
        tree = _lua_names(text)
        walls: dict[str, list[str]] = {}
        for wall, node in tree.items():
            layouts = node.get("layouts")
            walls[wall] = sorted(layouts) if isinstance(layouts, dict) else []
        self._walls = walls

        # Layouts belong to a wall, so a flat layout picker would be ambiguous
        # the moment two walls share a layout name. One picker offering the pair
        # keeps it to a single choice; the command splits it back apart.
        pairs = [
            {"value": f"{wall}|{layout}", "label": f"{wall} - {layout}"}
            for wall in sorted(walls)
            for layout in walls[wall]
        ]
        self.set_states(
            {
                "videowall_options": json.dumps(
                    [{"value": w, "label": w} for w in sorted(walls)]
                ),
                "videowall_layout_options": json.dumps(pairs),
            }
        )

    @staticmethod
    def _names_of(info: Any) -> list[str]:
        """List replies are either a name->detail map or a bare list of names."""
        if isinstance(info, dict):
            return sorted(str(name) for name in info)
        if isinstance(info, list):
            return sorted(str(name) for name in info)
        return []

    @staticmethod
    def _ip_of(info: Any) -> str:
        """ipsetting answers `autoip`, `dhcp`, or `<mode>/<ip>/<mask>[/<gateway>]`."""
        parts = _txt(info).split("/")
        return parts[1] if len(parts) > 1 else ""

    # ── Device settings ──────────────────────────────────────────────

    async def set_device_setting(self, setting: str, value: Any) -> None:
        if setting == "timezone":
            zone = str(value).strip().upper()
            if not re.match(r"^UTC[+-](?:[0-9]|1[0-2])$", zone):
                raise ValueError(f"Timezone must look like UTC-5 (UTC-12 to UTC+12), not {value!r}")
            await self._write(f"config set timezone {zone}")
            doc = await self._request("config get timezone")
            self.set_state("timezone", _txt(doc.get("info")) if doc is not None else zone)
            return
        if setting == "ntp_servers":
            servers = str(value).replace("/", " ").split()
            if not servers:
                raise ValueError("Enter at least one NTP server")
            if len(servers) > 5:
                raise ValueError("The control box accepts at most five NTP servers")
            await self._write("config set ntp " + " ".join(servers))
            doc = await self._request("config get ntp")
            if doc is not None:
                servers = [s for s in re.split(r"[/\s]+", _txt(doc.get("info"))) if s]
            self.set_state("ntp_servers", " ".join(servers))
            return
        if setting == "dns_servers":
            servers = str(value).replace("/", " ").split()
            if not servers:
                raise ValueError("Enter at least one DNS server")
            if len(servers) > 2:
                raise ValueError("The control box accepts at most two DNS servers")
            await self._write("config set dns " + " ".join(servers))
            doc = await self._request("config get dns")
            if doc is not None:
                servers = [s for s in re.split(r"[/\s]+", _txt(doc.get("info"))) if s]
            self.set_state("dns_servers", " ".join(servers))
            return
        raise ValueError(f"Unknown device setting: {setting}")

    # ── Commands ─────────────────────────────────────────────────────

    def _child(self, ctype: str, value: Any) -> str:
        """Resolve a child_id param to the wire identifier (the endpoint's MAC)."""
        mac = self._resolve(str(value))
        if mac is None or self._type_by_mac.get(mac) != ctype:
            raise ValueError(
                f"Unknown {ctype} {value!r}. Pick one from the dropdown "
                f"(Refresh from Device re-reads the roster)."
            )
        return mac

    def _endpoint(self, value: Any) -> str:
        mac = self._resolve(str(value))
        if mac is None:
            raise ValueError(
                f"Unknown endpoint {value!r}. Pick one from the dropdown "
                f"(Refresh from Device re-reads the roster)."
            )
        return mac

    def _more_decoders(self, value: Any) -> list[str]:
        """Extra decoders typed as names or MACs, as their MACs."""
        text = str(value or "").strip()
        if not text:
            return []
        macs = []
        for part in re.split(r"[:,\s]+", text):
            if part:
                macs.append(self._child("decoder", part))
        return macs

    def _wall_layout(self, value: Any) -> tuple[str, str]:
        """Split the wall-and-layout picker value back into its two names."""
        token = str(value).strip()
        wall, sep, layout = token.partition("|")
        wall, layout = wall.strip(), layout.strip()
        if not sep or not wall or not layout:
            raise ValueError(
                f"{token!r} is not a wall and layout. Pick one from the dropdown."
            )
        known = self._walls.get(wall)
        if known is not None and layout not in known:
            offer = ", ".join(known) if known else "none"
            raise ValueError(f"Video wall {wall!r} has no layout {layout!r} (it has: {offer})")
        return wall, layout

    async def _named(self, kind: str, value: Any) -> str:
        """A multiview layout or playlist name the control box actually has.

        `mvid layout tx` creates a layout that does not exist, so a typo would
        quietly author a new one. The list is read again once before a name is
        refused, so one made a moment ago in the management interface is found.
        """
        name = str(value).strip()
        label = "multiview layout" if kind == "multiview" else "playlist"
        if not name or any(c.isspace() for c in name):
            raise ValueError(f"{name!r} is not a {label} name. Pick one from the dropdown.")
        names = self._mv_layouts if kind == "multiview" else self._playlists
        if names is not None and name in names:
            return name
        if kind == "multiview":
            await self._refresh_multiview_layouts()
            names = self._mv_layouts
        else:
            await self._refresh_playlists()
            names = self._playlists
        if names is not None and name not in names:
            offer = ", ".join(names) if names else "none"
            raise ValueError(
                f"The control box has no {label} named {name!r} (it has: {offer})"
            )
        return name

    async def _read_dante_ip(self, mac: str, strict: bool) -> bool:
        """`config get device dante ip` into the endpoint's Dante states.

        The control box answers `STATIC/169.254.2.113/255.255.0.0/169.254.1.1`.
        After a Dante IP change (strict False) the module is restarting, so a
        refusal or silence is logged rather than raised.
        """
        line = f"config get device dante ip {mac}"
        doc = await self._request(line)
        if doc is None:
            if strict:
                raise ConnectionError(f"[{self.device_id}] No reply to: {line}")
            log.info(f"[{self.device_id}] No Dante IP read back for {mac}")
            return False
        detail = _refusal(doc)
        if detail is not None:
            if strict:
                raise ValueError(f"The control box rejected '{line}': {detail}")
            log.info(f"[{self.device_id}] Dante IP read back for {mac} refused: {detail}")
            return False
        parts = [p.strip() for p in _txt(doc.get("info")).split("/")]
        while len(parts) < 4:
            parts.append("")
        self.set_child_state_batch(
            self._type_by_mac[mac],
            mac,
            {
                "dante_ip_mode": parts[0].lower(),
                "dante_ip": parts[1],
                "dante_netmask": parts[2],
                "dante_gateway": parts[3],
            },
        )
        return True

    @staticmethod
    def _ipv4(value: Any, label: str) -> str:
        text = str(value).strip()
        if not _RE_IPV4.match(text) or any(int(o) > 255 for o in text.split(".")):
            raise ValueError(f"{label} must be an IPv4 address like 192.168.1.10, not {value!r}")
        return text

    @staticmethod
    def _endpoint_name(value: Any) -> str:
        name = str(value).strip()
        if not _RE_ENDPOINT_NAME.match(name):
            raise ValueError(
                f"{name!r} cannot be an endpoint name: use up to 32 characters with no "
                f"spaces, colons or commas, and not ALL, ALLRX or ALLTX."
            )
        return name

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        # ── Routing ──
        if command == "route":
            tx = self._child("encoder", params["tx"])
            rx = self._child("decoder", params["rx"])
            stream = str(params.get("stream") or "all").lower()
            if stream != "all" and stream not in ROUTE_COMMANDS:
                raise ValueError(f"Unknown media {stream!r}")
            targets = list(ROUTE_COMMANDS) if stream == "all" else [stream]
            for name in targets:
                await self._write(f"config set device {ROUTE_COMMANDS[name]} {tx} {rx}")
            # The routes reply lags an accepted route, so publish the accepted
            # value now rather than leaving a panel showing the old source until
            # a poll confirms it. _write has already raised if the box refused.
            commanded = {ROUTE_PROPERTIES[name]: tx for name in targets}
            self._expect_routes(rx, commanded)
            self.set_children_state_batch([("decoder", rx, commanded)])
            return True

        if command == "route_off":
            rx = self._child("decoder", params["rx"])
            stream = str(params.get("stream") or "all").lower()
            if stream != "all" and stream not in UNROUTE_COMMANDS:
                raise ValueError(f"Unknown media {stream!r}")
            targets = list(UNROUTE_COMMANDS) if stream == "all" else [stream]
            for name in targets:
                await self._write(f"config set device {UNROUTE_COMMANDS[name]} {rx}")
            commanded = {ROUTE_PROPERTIES[name]: "" for name in targets}
            self._expect_routes(rx, commanded)
            self.set_children_state_batch([("decoder", rx, commanded)])
            return True

        if command == "recall_matrix":
            name = str(params["name"]).strip()
            force = " force" if params.get("force") else ""
            return await self._write(f"matrix active {name}{force}")

        # ── Video walls ──
        if command == "recall_videowall_layout":
            wall, layout = self._wall_layout(params["layout"])
            return await self._write(f"vwid layout active {wall} {layout}")

        if command == "set_videowall_source":
            wall, layout = self._wall_layout(params["layout"])
            tx = self._child("encoder", params["tx"])
            if params.get("replace"):
                where = self._child("encoder", params["replace"])
            else:
                where = str(params.get("position") or "ALL").strip()
                if where.upper() == "ALL":
                    where = "ALL"
                elif not re.match(r"^\d{1,2}:\d{1,2}$", where):
                    raise ValueError(
                        f"Screen must be a row and column like 2:1, or ALL, not {where!r}"
                    )
            return await self._write(f"vwid layout tx {wall} {layout} {where} {tx}")

        if command == "activate_multiview":
            wall, layout = self._wall_layout(params["layout"])
            index = str(params["index"]).strip()
            if not re.match(r"^\d{1,3}:\d{1,3}$", index):
                raise ValueError(f"Position must be a row and column like 2:1, not {index!r}")
            return await self._write(
                f"vwid layout multiview active {wall} {layout} {index}"
            )

        # ── Multiview ──
        if command == "recall_multiview_layout":
            layout = await self._named("multiview", params["layout"])
            return await self._write(f"mvid layout active {layout}")

        if command == "assign_multiview_layout":
            layout = await self._named("multiview", params["layout"])
            rxs = [self._child("decoder", params["rx"])]
            rxs += [m for m in self._more_decoders(params.get("more")) if m not in rxs]
            return await self._write(f"mvid layout rx {layout} {':'.join(rxs)}")

        if command == "set_multiview_window_source":
            layout = await self._named("multiview", params["layout"])
            window = int(params["window"])
            if not 1 <= window <= 16:
                raise ValueError("Window must be 1 to 16")
            tx = self._child("encoder", params["tx"])
            return await self._write(f"mvid layout tx {layout} {window} {tx}")

        if command == "set_multiview_audio":
            layout = await self._named("multiview", params["layout"])
            mode = str(params["mode"]).strip()
            if mode == "0":
                if params.get("window") in (None, ""):
                    raise ValueError("Follow a window needs the Window to follow")
                window = int(params["window"])
                if not 1 <= window <= 16:
                    raise ValueError("Window must be 1 to 16")
                return await self._write(f"mvid layout audio {layout} 0 {window}")
            if mode == "1":
                if not params.get("tx"):
                    raise ValueError("Follow a source needs the Source to follow")
                tx = self._child("encoder", params["tx"])
                return await self._write(f"mvid layout audio {layout} 1 {tx}")
            if mode == "2":
                return await self._write(f"mvid layout audio {layout} 2")
            raise ValueError(f"Unknown multiview audio mode {mode!r}")

        # ── Media player ──
        if command == "upload_playlist":
            playlist = await self._named("playlist", params["playlist"])
            rxs = [self._child("decoder", params["rx"])]
            rxs += [m for m in self._more_decoders(params.get("more")) if m not in rxs]
            return await self._write(f"play pl upload {playlist} {','.join(rxs)}")

        if command == "apply_playlist":
            playlist = await self._named("playlist", params["playlist"])
            rx = self._child("decoder", params["rx"])
            return await self._write(f"play pl apply {playlist} {rx}")

        if command == "start_playlist":
            playlist = await self._named("playlist", params["playlist"])
            rx = self._child("decoder", params["rx"])
            return await self._write(f"play pl start {playlist} {rx}")

        if command == "stop_playlist":
            rx = self._child("decoder", params["rx"])
            return await self._write(f"play pl stop {rx}")

        # ── Decoder ──
        if command == "set_output_resolution":
            rx = self._child("decoder", params["rx"])
            timing = " ".join(str(params["timing"]).split())
            if not re.match(r"^\d+ \d+ \d+$", timing):
                raise ValueError(f"Timing must be width, height and rate, not {timing!r}")
            return await self._write(f"config set device video {timing} {rx}")

        if command == "set_hdr":
            rx = self._child("decoder", params["rx"])
            state = self._choice(params["state"], ("0", "1"), "HDR")
            return await self._write(f"config set device hdrmode {state} {rx}")

        if command == "set_hdcp":
            rx = self._child("decoder", params["rx"])
            mode = self._choice(params["mode"], ("0", "1", "2", "3"), "HDCP")
            return await self._write(f"config set device hdcp {mode} {rx}")

        if command == "set_decoder_stream":
            rx = self._child("decoder", params["rx"])
            state = self._choice(params["state"], ("on", "off"), "Stream")
            return await self._write(f"config set device stream {state} {rx}")

        if command == "set_audio_out_source":
            rx = self._child("decoder", params["rx"])
            # 3 is ARC audio, which the document marks as not supported yet.
            source = self._choice(params["source"], ("1", "2", "4"), "Audio")
            return await self._write(f"config set device exaudio type {source} {rx}")

        if command == "set_audio_out_volume":
            rx = self._child("decoder", params["rx"])
            level = int(params["level"])
            if not 0 <= level <= 100:
                raise ValueError("Volume must be 0 to 100")
            return await self._write(f"config set device exaudio volume {level} {rx}")

        if command == "set_hdmi_audio_source":
            rx = self._child("decoder", params["rx"])
            source = self._choice(params["source"], ("1", "2"), "Audio")
            return await self._write(f"config set device hdmioutaudio {source} {rx}")

        # ── Encoder ──
        if command == "set_edid":
            tx = self._child("encoder", params["tx"])
            edid = self._choice(params["edid"], tuple(str(i) for i in range(21)), "EDID")
            await self._write(f"config set device edid {edid} {tx}")
            # The roster reports the EDID back, so the next poll confirms this.
            self.set_child_state("encoder", tx, "edid", edid)
            return True

        if command == "copy_edid":
            rx = self._child("decoder", params["rx"])
            tx = self._child("encoder", params["tx"])
            return await self._write(f"config set device copyedid {rx} {tx}")

        if command == "set_hdmi_input":
            tx = self._child("encoder", params["tx"])
            state = self._choice(params["state"], ("on", "off"), "Enabled")
            return await self._write(f"config set device hdmi 0 {state} {tx}")

        if command == "set_hdcp_request":
            tx = self._child("encoder", params["tx"])
            mode = self._choice(params["mode"], ("0", "1"), "HDCP")
            return await self._write(f"config set device hdcprequest {mode} {tx}")

        if command == "set_source_input":
            tx = self._child("encoder", params["tx"])
            choice = self._choice(params["input"], ("auto", "hdmi", "usb"), "Input")
            return await self._write(f"config set device sourceinput {choice} {tx}")

        if command == "set_open_stream":
            tx = self._child("encoder", params["tx"])
            state = self._choice(params["state"], ("on", "off"), "Open Stream")
            # The command heading and the version history both spell it
            # `exstream set on|off`; the document's example omits `set`.
            return await self._write(f"config set device exstream set {state} {tx}")

        if command == "configure_open_stream":
            tx = self._child("encoder", params["tx"])
            encoding = self._choice(params["encoding"], ("h264", "h265"), "Encoding")
            profiles = ("baseline", "main", "high") if encoding == "h264" else ("main", "main10")
            profile = str(params["profile"]).strip().lower()
            if profile not in profiles:
                raise ValueError(
                    f"{profile!r} is not an {encoding.upper()} profile "
                    f"(use {', '.join(profiles)})"
                )
            protocol = self._choice(params["protocol"], ("rtsp", "rtmp"), "Protocol")
            key = str(params["stream_key"]).strip()
            if not key or any(c.isspace() for c in key):
                raise ValueError("Stream Key cannot be empty or contain spaces")
            bitrate = int(params["bitrate"])
            gop = int(params["gop"])
            if bitrate < 1 or gop < 1:
                raise ValueError("Bitrate and GOP must be positive")
            method = str(params.get("rate_control") or "cbr").strip().lower()
            if not re.match(r"^[a-z]{2,8}$", method):
                raise ValueError(f"Rate Control must be one word like cbr, not {method!r}")
            return await self._write(
                f"config set device exstream save {encoding} {profile} {protocol} "
                f"{key} {bitrate} {method} {gop} {tx}"
            )

        if command == "set_stream_bridge":
            tx = self._child("encoder", params["tx"])
            state = self._choice(params["state"], ("on", "off"), "Bridge")
            return await self._write(f"config set device exstream bridge {state} {tx}")

        # ── Any endpoint ──
        if command == "identify":
            mac = self._endpoint(params["endpoint"])
            mode = self._choice(params.get("mode") or "flash", ("flash", "on", "off"), "LED")
            return await self._write(f"config set device light {mode} {mac}")

        if command == "reboot_endpoint":
            mac = self._endpoint(params["endpoint"])
            return await self._write(f"config set device reboot {mac}")

        if command == "set_osd":
            mac = self._endpoint(params["endpoint"])
            state = self._choice(params["state"], ("on", "off"), "OSD")
            return await self._write(f"config set device osd {state} {mac}")

        if command == "hpd_reset":
            mac = self._endpoint(params["endpoint"])
            port = str(params.get("port") or "auto").strip().lower()
            if port == "auto":
                port = "1" if self._type_by_mac.get(mac) == "encoder" else "2"
            port = self._choice(port, ("1", "2"), "Port")
            return await self._write(f"config set device hpdrst {port} {mac}")

        if command == "cec_power":
            mac = self._endpoint(params["endpoint"])
            word = "poweron" if str(params["state"]).lower() == "on" else "poweroff"
            return await self._write(f"config set device cec {word} {mac}")

        if command == "send_cec":
            mac = self._endpoint(params["endpoint"])
            # Each message is one block of hex with no spaces; several are
            # separated by commas (IPCBox USP Commands V1.08).
            blocks = [b for b in re.split(r"\s*,\s*", str(params["data"]).strip()) if b]
            blocks = [b.replace(" ", "") for b in blocks]
            if not blocks or not all(re.match(r"^[0-9A-Fa-f]{2,}$", b) for b in blocks):
                raise ValueError("CEC data must be hex, e.g. 0036, with commas between messages")
            return await self._write(f"config set device cec {','.join(blocks)} {mac}")

        if command == "send_ir":
            mac = self._endpoint(params["endpoint"])
            code = " ".join(str(params["code"]).split())
            if not code:
                raise ValueError("Enter a Pronto IR code")
            return await self._write(f"config set device ir {code} {mac}")

        if command == "send_serial":
            mac = self._endpoint(params["endpoint"])
            data = str(params["data"])
            if params.get("append_cr"):
                data += "\\r"
            data_type = "2" if str(params.get("format", "ascii")).lower() == "hex" else "1"
            return await self._write(f"config set device rs232 {data_type} {data} {mac}")

        if command == "set_serial_settings":
            mac = self._endpoint(params["endpoint"])
            baud = str(params["baud"]).strip()
            if not baud.isdigit() or not 300 <= int(baud) <= 115200:
                raise ValueError("Baud rate must be 300 to 115200")
            bits = self._choice(params.get("data_bits") or "8", ("6", "7", "8"), "Data Bits")
            parity = self._choice(params.get("parity") or "0", ("0", "1", "2"), "Parity")
            stop = self._choice(params.get("stop_bits") or "1", ("1", "2"), "Stop Bits")
            return await self._write(
                f"config set device rs232setting {baud} {bits} {parity} {stop} {mac}"
            )

        if command == "set_serial_feedback":
            mac = self._endpoint(params["endpoint"])
            fmt = str(params["format"]).strip().lower()
            if fmt not in SERIAL_FORMATS:
                raise ValueError(f"Unknown serial feedback format {fmt!r}")
            return await self._write(
                f"config set device rs232responsetype {SERIAL_FORMATS[fmt]} {mac}"
            )

        if command in ("rename_encoder", "rename_decoder"):
            ctype, key, verb = (
                ("encoder", "tx", "txid") if command == "rename_encoder"
                else ("decoder", "rx", "rxid")
            )
            mac = self._child(ctype, params[key])
            name = self._endpoint_name(params["name"])
            await self._write(f"config set device {verb} {name} {mac}")
            self._alias_mac[name.lower()] = mac
            await self._enumerate_roster()
            return True

        if command in ("describe_encoder", "describe_decoder"):
            ctype, key, verb = (
                ("encoder", "tx", "txdescription") if command == "describe_encoder"
                else ("decoder", "rx", "rxdescription")
            )
            mac = self._child(ctype, params[key])
            text = " ".join(str(params["description"]).split())
            if not text:
                raise ValueError("Enter a description")
            return await self._write(f"config set device {verb} {text} {mac}")

        if command == "set_device_mode":
            mac = self._endpoint(params["endpoint"])
            host = self._choice(params["encoder"], ("on", "off"), "Encoder")
            client = self._choice(params["decoder"], ("on", "off"), "Decoder")
            if host == "off" and client == "off":
                raise ValueError("An endpoint has to be an encoder, a decoder or both")
            host_word = "host" if host == "on" else "nohost"
            client_word = "client" if client == "on" else "noclient"
            await self._write(f"config set device devicemode {host_word} {client_word} {mac}")
            await self._enumerate_roster()
            return True

        # ── Dante ──
        if command == "set_dante_name":
            mac = self._endpoint(params["endpoint"])
            name = str(params["name"]).strip()
            if not name or any(c.isspace() for c in name):
                raise ValueError("The Dante name cannot be empty or contain spaces")
            return await self._write(f"config set device danteid {name} {mac}")

        if command == "set_dante_description":
            mac = self._endpoint(params["endpoint"])
            text = " ".join(str(params["description"]).split())
            if not text:
                raise ValueError("Enter a description")
            return await self._write(f"config set device dantedes {text} {mac}")

        if command == "set_dante_ip_dhcp":
            mac = self._endpoint(params["endpoint"])
            await self._write(f"config set device dante ip dhcp {mac}")
            await self._read_dante_ip(mac, strict=False)
            return True

        if command == "set_dante_ip_static":
            mac = self._endpoint(params["endpoint"])
            ip = self._ipv4(params["ip"], "IP Address")
            mask = self._ipv4(params["netmask"], "Subnet Mask")
            gateway = self._ipv4(params["gateway"], "Gateway")
            await self._write(f"config set device dante ip static:{ip}:{mask}:{gateway} {mac}")
            await self._read_dante_ip(mac, strict=False)
            return True

        if command == "read_dante_ip":
            mac = self._endpoint(params["endpoint"])
            return await self._read_dante_ip(mac, strict=True)

        # ── System ──
        if command == "refresh":
            await self.refresh_children()
            return True

        if command == "sync_clock":
            now = time.localtime()
            await self._write(
                f"config set date {now.tm_year} {now.tm_mon} {now.tm_mday} "
                f"{now.tm_hour} {now.tm_min} {now.tm_sec}"
            )
            doc = await self._request("config get date")
            if doc is not None:
                self.set_state("system_date", _txt(doc.get("info")))
            return True

        if command == "reboot_cbox":
            return await self._write("config set reboot")

        if command == "raw_command":
            doc = await self._request(str(params["command"]).strip())
            if doc is None:
                raise ConnectionError(f"[{self.device_id}] No reply from the control box")
            return json.dumps(doc)

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

    @staticmethod
    def _choice(value: Any, allowed: tuple[str, ...], label: str) -> str:
        text = str(value).strip().lower()
        if text not in allowed:
            raise ValueError(f"{label} must be one of {', '.join(allowed)}, not {value!r}")
        return text

    # ── Setup action ─────────────────────────────────────────────────

    async def run_setup_action(
        self, action_id: str, params: dict[str, Any], progress: Any
    ) -> dict[str, Any]:
        if action_id != "test_connection":
            raise ValueError(f"Unknown setup action: {action_id}")

        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 24))
        if not host:
            raise ValueError("No IP address configured")

        await progress(f"Connecting to {host}:{port}…", 20)
        # From the adapter Settings > Network > Reach devices through chose. A
        # connection the platform makes follows that setting on its own; a
        # socket a driver opens itself does not.
        local_ip = self.local_address(host)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, local_addr=(local_ip, 0) if local_ip else None
                ),
                timeout=5.0,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach an MXNet API on {host}:{port} ({exc}). Check that this is "
                f"the control box's control network port."
            ) from exc

        try:
            await progress("Reading the control box…", 55)
            model = await self._probe(reader, writer, "config get name")
            if not model:
                raise ConnectionError(
                    f"{host}:{port} accepted the connection but did not answer the MXNet API."
                )
            refusal = _model_refusal(model)
            if refusal:
                raise ConnectionError(f"{host}:{port} {refusal}")
            firmware = await self._probe(reader, writer, "config get version")

            await progress("Counting endpoints…", 85)
            doc = await self._probe_json(reader, writer, "config get devicelist")
            info = doc.get("info")
            endpoints = len(info) if isinstance(info, dict) else 0
            encoders = (
                sum(1 for e in info.values() if isinstance(e, dict) and str(e.get("is_host", "")) == "1")
                if isinstance(info, dict)
                else 0
            )

            summary = f"Found {model}"
            if firmware:
                summary += f" (firmware {firmware})"
            summary += (
                f" with {endpoints} endpoint{'' if endpoints == 1 else 's'}: "
                f"{encoders} encoder{'' if encoders == 1 else 's'}, "
                f"{endpoints - encoders} decoder{'' if endpoints - encoders == 1 else 's'}"
            )
            if endpoints == 0:
                summary += ". Commission the system before adding endpoints here"
            return {"success": True, "message": summary}
        finally:
            writer.close()

    async def _probe_json(self, reader: Any, writer: Any, line: str) -> dict[str, Any]:
        """One request on the Test Connection socket, and its JSON reply."""
        writer.write((line + "\r\n").encode())
        await writer.drain()
        buf = b""
        while True:
            frame, buf = _json_frame(buf)
            while frame is not None:
                if frame.strip():
                    try:
                        doc = json.loads(frame.decode("utf-8", errors="replace"))
                    except (ValueError, TypeError):
                        doc = None
                    if isinstance(doc, dict) and doc.get("source") is None:
                        return doc
                frame, buf = _json_frame(buf)
            chunk = await asyncio.wait_for(reader.read(65536), timeout=REQUEST_TIMEOUT_S)
            if not chunk:
                return {}
            buf += chunk

    async def _probe(self, reader: Any, writer: Any, line: str) -> str:
        doc = await self._probe_json(reader, writer, line)
        return _txt(doc.get("info"))
