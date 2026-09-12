"""AVPro Edge MXNet 10G — AV-over-IP ecosystem controlled through the 10G Control Box.

The CBOX is the head end: encoders (TX) and decoders (RX) never take control
connections of their own, so the CBOX is the device you add and every endpoint
appears beneath it as a child entity.

This is a SEPARATE ecosystem from MXNet 1G, not a variant of it — AVPro
publishes its own API document ("IPCBox-10G Commands") and the command surface
genuinely diverges. What is the same: the `config get/set device` grammar, TCP
24 with no login, and the endpoint roster. What is different, and why a 1G
project file will not simply work here:

  * Six routing planes, not five — the 10G adds `analogaudiopath`.
  * The EDID list is 0-20 with its own meanings (the 1G's is 1-15), and the
    decoder HDCP argument is 0/1/2 where the 1G's is 2/3/4. Both are silent
    when wrong: the CBOX accepts the number and applies a different mode.
  * The encoder owns the stream gate (`config set device stream` takes a TX),
    where on the 1G the decoder does.
  * The 10G has no blackout, rotation, scaling, test pattern, OSD, audio-mute
    window, decoder volume or audio-input selection, and no scene, KVM, mosaic
    or matrix-preset lists. It adds dual-HDMI input gating, the AVDM downmix
    presets, CEC power keywords and multiview.
  * There is no `config get device routes` and no `rs232responsetype`.

Python rather than YAML for three reasons, none of which ConfigurableDriver can
express today:

  1. The endpoint roster is enumerated FROM the device (`config get
     devicelist`), not declared by the integrator. YAML's `instances:` roster
     covers fixed and config-driven rosters only.
  2. Replies are JSON objects whose `info` member fans one response out into N
     children x M properties, and the routes have to be *derived* by joining
     each decoder's per-plane channel subscriptions against the encoder that
     owns that channel. YAML response rules are regex-per-line with no JSON-path
     routing into children and no cross-entry join.
  3. Encoders and decoders are different shapes, and the reply format is not
     even consistent across the API (see Framing), so the driver has to decide
     per reply how to read it.

Protocol
    Telnet-style ASCII on TCP 24 with no login. The API document does not state
    a line terminator for either direction; sends are CRLF-terminated, which is
    what a PuTTY session does with Enter.

Framing
    Three reply shapes on one connection, and the driver has to frame all of
    them:

      * a JSON object, which is what every `config`, `matrix` and multiview
        command answers with;
      * a Lua-style table, which is what the `vwid list` / `vwid get` /
        `vwid layout list` / `vwid layout get` video-wall queries answer with
        (`videowall1 = { cols = 2, ... }` — brace-balanced, but not JSON);
      * a bare token, which is what the remaining `vwid` commands answer with
        ("OK").

    So framing is brace-balancing when the frame opens with `{` (correct for
    both object shapes whatever the terminator turns out to be) and a line
    otherwise. A frame that fails `json.loads` is handed back as raw text, and
    only satisfies a request that was expecting one — see _request.

    The document also shows the multiview write commands answering with JSON
    whose `cmd` member contains unescaped quotes (`{"info":"OK","cmd":"{"cmd":
    "vwid layout multiview window ..."}"}`), which no parser can read. Those
    land on the raw-text path too.

Push vs poll
    Poll. The API document has no subscription, notification, feedback or event
    section: the roster, status and matrix state are all request/response.

    The CBOX does interleave unsolicited frames on the control connection all
    the same — a frame with an EMPTY `cmd` and a `source` member, which is never
    a reply. This is undocumented for the 10G and was found on a 1G CBOX, whose
    firmware family this shares; the driver diverts any empty-`cmd` frame
    whatever its source, because mistaking one for a reply hands it to the
    waiting request and shifts every reply after it by one. Their CONTENT is
    parsed defensively (an unrecognised shape only lands on `last_event`), and
    polling stays the authority.

    Two broad queries cover the whole system, so poll cost is flat in the size
    of the install: `config get devicelist` (roster, config AND routes) and
    `config get device status ALL` (AV status of every endpoint).

Reading the routes
    The one mechanic here that the document does not state in words. There is no
    `config get device routes` on the 10G, but `config get devicelist` publishes
    what it is made of: every encoder carries the stream channel it hosts (`ch`),
    and every decoder carries the channel it subscribes to on each plane
    (`ch_v`, `ch_a`, `ch_u`, `ch_r`, `ch_s`, and `ch_l` for analog audio — the
    same letters the `matrix add` type argument uses). Joining the two gives the
    live route per plane. A channel no encoder owns reads as unrouted, which
    needs no assumption about what an idle channel number looks like.

    The document's own example output is the evidence: its two decoders carry
    `ch_v`/`ch_a` of 0002 and 0009, which are exactly the `ch` values of its two
    encoders. Unverified against hardware.

Source: IPCBox-10G Commands v1.05 (AVPro Global Holdings), published at
https://support.avproglobal.com/portal/en/kb/articles/mxnet-api
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

# How long a commanded route outranks the CBOX's own answer. Carried over from
# the 1G driver, where convergence was measured at ~16s on a CBOX-B; not
# measured on a 10G box. It is a ceiling, and agreement clears it sooner.
ROUTE_SETTLE_S = 30

# Routing is done with the per-plane `*path` commands, never with `matrix aset`.
# `aset` looks like the natural "route everything" call and the API document
# presents it that way, but on the 1G CBOX it is acknowledged and then IGNORED
# whenever the destination's path is currently disabled -- reproduced
# deterministically on an AC-MXNET-CBOX-B, where an accepted `aset` left the
# route unset twenty seconds later while the per-plane `videopath` applied in
# about six. That combination is exactly what a matrix panel produces (press Off
# on a destination, then press a source), and the two boxes share the command
# grammar, so the same discipline applies here until a 10G box says otherwise.
ROUTE_COMMANDS = {
    "video": "videopath",
    "audio": "audiopath",
    "analogaudio": "analogaudiopath",
    "usb": "usbpath",
    "infrared": "irpath",
    "serial": "rs232path",
}

UNROUTE_COMMANDS = {name: cmd + "disable" for name, cmd in ROUTE_COMMANDS.items()}

# The decoder child property each plane's current source lands on.
ROUTE_PROPERTIES = {
    "video": "source_video",
    "audio": "source_audio",
    "analogaudio": "source_analog_audio",
    "usb": "source_usb",
    "infrared": "source_infrared",
    "serial": "source_serial",
}

# Where a decoder reports the channel it is subscribed to, per plane. The
# letters are the ones `matrix add` uses for the same planes (video=v, audio=a,
# analogaudio=l, usb=u, infrared=r, serial=s). A member the firmware does not
# report simply leaves that plane's route alone rather than blanking it.
CHANNEL_MEMBERS = {
    "ch_v": "video",
    "ch_a": "audio",
    "ch_l": "analogaudio",
    "ch_u": "usb",
    "ch_r": "infrared",
    "ch_s": "serial",
}

# Encoder EDID presets, indices 0-20 per the 10G API document. NOTE these are
# NOT the 1G's list -- the same number means a different EDID on the two boxes.
EDID_PRESETS = [
    ("0", "1080p, 2-channel audio"),
    ("1", "1080p, 6-channel audio"),
    ("2", "1080p 3D, 2-channel audio"),
    ("3", "1080p 3D, 6-channel audio"),
    ("4", "4K30 3D, 2-channel audio"),
    ("5", "4K30 3D, 6-channel audio"),
    ("6", "4K30 3D, 8-channel audio"),
    ("7", "4K60 3D, 2-channel audio"),
    ("8", "4K60 3D, 6-channel audio"),
    ("9", "4K60 3D, 8-channel audio"),
    ("10", "1080p, 2-channel audio, HDR"),
    ("11", "1080p, 6-channel audio, HDR"),
    ("12", "1080p 3D, 2-channel audio, HDR"),
    ("13", "1080p 3D, 6-channel audio, HDR"),
    ("14", "4K30 3D, 2-channel audio, HDR"),
    ("15", "4K30 3D, 6-channel audio, HDR"),
    ("16", "4K30 3D, 8-channel audio, HDR"),
    ("17", "4K60 3D, 2-channel audio, HDR"),
    ("18", "4K60 3D, 6-channel audio, HDR"),
    ("19", "4K60 3D, 8-channel audio, HDR"),
    ("20", "1920x1200 3D, 2-channel audio, HDR"),
]

# Decoder output timings every 10G decoder supports, as `width height fps`.
OUTPUT_TIMINGS = [
    ("0 0 0", "Pass through (follow the source)"),
    ("1280 720 50", "1280x720 50Hz"),
    ("1280 720 60", "1280x720 60Hz"),
    ("1920 1080 24", "1920x1080 24Hz"),
    ("1920 1080 50", "1920x1080 50Hz"),
    ("1920 1080 60", "1920x1080 60Hz"),
    ("3840 2160 30", "3840x2160 30Hz"),
    ("3840 2160 60", "3840x2160 60Hz"),
]

# `config set device exmxmode` — the AVDM daughter card's downmix presets.
DOWNMIX_MODES = [
    ("1", "STD FX (default)"),
    ("2", "Low Center +"),
    ("3", "Mid Center +"),
    ("4", "High Center +"),
    ("5", "Middle FX (recommended)"),
    ("6", "Full FX"),
    ("7", "Voice FX"),
]

_RE_MAC = re.compile(r"^[0-9A-Fa-f]{12}$")

# Unsolicited event lines. The port label is IN<n> on an encoder and OUT<n> on a
# decoder; the number is captured rather than assumed.
_RE_EVENT_HPD = re.compile(r"^(?:IN|OUT)(\d+)\s+HPD\s+([01])\s*$", re.IGNORECASE)
_RE_EVENT_AVINF = re.compile(r"^(?:IN|OUT)(\d+)\s+AV\s+INF\s+(.*)$", re.IGNORECASE)

# The `vwid` queries answer with a Lua-style table. `<name> = {` is the only
# piece of that grammar this driver needs.
_RE_LUA_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*\{")

# Commands whose reply is documented as something other than a readable JSON
# object -- a Lua table, a bare "OK", or (for the multiview writes) JSON with
# unescaped quotes inside `cmd`. A raw frame is only accepted as the answer to
# one of these, so a banner or a stray line can never satisfy a real request.
_RAW_REPLY_PREFIX = "vwid"


def _json_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Frame the CBOX's reply stream, which is not all one shape.

    A frame that opens with `{` is brace-balanced (string/escape aware), which
    is correct for a JSON object AND for the Lua-style table the video-wall
    queries answer with, whichever line terminator the firmware uses. Anything
    else is framed on a line ending, which is the only thing that completes a
    bare `OK`.

    Garbage before the first token is consumed by returning an EMPTY frame with
    the trimmed remainder; CallableFrameParser keeps whatever buffer a parse
    function returns on either branch.
    """
    lead = 0
    while lead < len(buf) and buf[lead] in (0x20, 0x09, 0x0D, 0x0A):
        lead += 1
    if lead:
        return b"", buf[lead:]
    if not buf:
        return None, buf

    if buf[0] != 0x7B:  # not "{" — a bare token, framed on its line ending
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

    The video-wall queries answer with the CBOX's own config syntax rather than
    JSON, and the only thing this driver wants from it is which walls exist and
    which layouts each one holds. Values are ignored entirely, so the reader
    needs no grammar beyond `<name> = {` and a brace depth.
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


def _txt(value: Any) -> str:
    """Coerce a JSON member to a trimmed string ('' for absent/null)."""
    if value is None:
        return ""
    return str(value).strip()


def _flag(value: Any, token: str) -> bool:
    """MXNet reports several booleans as a token plus a digit: HDR1 / HPD0."""
    return _txt(value).upper() == f"{token}1"


def _hdcp(value: Any) -> str:
    """Normalise the CBOX's two spellings of the same HDCP state.

    An encoder reports `HDCP0` / `HDCP1` while a decoder reports `HDCP OFF` /
    `HDCP ON` for the same condition on the same firmware (measured on the 1G
    CBOX; the 10G document shows the encoder spelling). Left raw, one endpoint's
    card reads "HDCP0" and its neighbour's "HDCP OFF" and no trigger can compare
    them. Anything unrecognised passes through, so a version string is never
    mangled into a yes/no.
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
    """Normalise a timing to one spelling, whichever shape the CBOX used.

    Three forms reach this, all documented or observed: the status reply's
    string (`3840X2160p/30Hz`), the AV-info event's string (`3840X2160p@30Hz`
    for the identical timing), and the decoder `device info` reply's object
    (`{"width": "3840", "height": "2160", "frames_per_second": "30"}`). A panel
    would otherwise show the separator flipping as different reads take turns.
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

    Absent is the CBOX's way of saying "this is a database record for something
    that is not here" — it is not zero and not false.
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
    heartbeat in the same reply rather than against this host's clock. That is
    what makes it correct without either clock agreeing with the other, which
    matters more here than on the 1G: the 10G document's examples carry values
    around 14000-17500, which is a CBOX whose clock has never been set and is
    counting from the epoch, while the 1G document also shows true Unix times
    from a box with NTP. Both are the same field on the same kind of clock.

    It is a heartbeat rather than a per-endpoint uptime, which would break the
    comparison: the document's roster example reports the identical value for
    all four endpoints at once, which four independently-booted devices do not
    do and one clock sampled once does.

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


class AVProEdgeMXNet10GDriver(BaseDriver):
    """AVPro Edge MXNet 10G ecosystem, via the AC-MXNET-10G-CBOX control box."""

    DRIVER_INFO = {
        "id": "avproedge_mxnet_10g",
        "name": "AVPro Edge MXNet 10G",
        "manufacturer": "AVPro Edge",
        "category": "switcher",
        "version": "1.0.0",
        # Computed, not chosen: `routing:` needs 0.27.0, sharing a route
        # property between the All-streams and Video planes needs 0.28.0,
        # BaseDriver.child_fault() needs 0.29.0, and `restarts_device_for`
        # needs 0.34.0. The child_fault() one is a method call the contract
        # check cannot see, so it is carried by hand here the way the 1G
        # driver carries it.
        "min_platform_version": "0.34.0",
        "author": "OpenAVC",
        "description": (
            "Controls an AVPro Edge MXNet 10G AV-over-IP system through its 10G "
            "Control Box. Encoders and decoders appear as child entities with "
            "per-plane routing (video, audio, analog audio, USB, IR, RS-232), EDID, "
            "CEC/IR/RS-232 passthrough, and video wall, multiview and matrix recall."
        ),
        "source_url": "https://support.avproglobal.com/portal/en/kb/articles/mxnet-api",
        "tags": [
            "av-over-ip",
            "mxnet",
            "matrix-switcher",
            "video-wall",
            "multiview",
            "encoder",
            "decoder",
        ],
        "verified": False,
        "simulated": True,
        "protocols": ["mxnet_api"],
        "ports": [24],
        "transport": "tcp",
        # Six independently routable planes per decoder, all switched by the one
        # route command and told apart by its stream parameter -- plus the
        # combined mode that moves all six, which is what a room wants and so is
        # offered first. Without it the ordinary matrix somebody builds here
        # routes video and leaves audio, USB, IR and serial on whatever was on
        # that display before, with nothing on the panel to say so. It watches
        # source_video because that is what a tile should read; the Video plane
        # below watches the same property and sends a different stream, which is
        # a different control rather than the same one twice.
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
                {"label": "Analog audio", "route_property": "source_analog_audio",
                 "params": {"stream": "analogaudio"}},
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
            # The CBOX answers `config get name` with its model, e.g.
            # {"gid":200000,"cmd":"config get name","info":"AC-MXNET-10G-CBOX","code":0}
            # The regex names the 10G box specifically: every MXNet control box
            # answers this query, and a probe matching "AC-MXNET" alone would
            # claim all four ecosystems for whichever driver was asked first.
            "tcp_probe": {
                "port": 24,
                "send_ascii": "config get name\r\n",
                "expect_regex": r'"info"\s*:\s*"AC-MXNET-10G[^"]*"',
                "extract_manufacturer": "AVPro Edge",
                "extract": {
                    "model": {"regex": r'"info"\s*:\s*"(AC-MXNET-10G[^"]*)"'},
                },
                "timeout_ms": 2000,
            },
        },
        "compatible_models": [
            {
                "manufacturer": "AVPro Edge",
                "models": ["AC-MXNET-10G-CBOX"],
                "confidence": "untested",
                "notes": (
                    "Add the CONTROL BOX as the device — the encoders and decoders it "
                    "manages appear as children. Covers the AC-MXNET-10G-E / -EV2 / "
                    "-AVDM-E encoders and AC-MXNET-10G-D / -DV2 / -AVDM-D decoders the "
                    "10G control box manages. The 1G, USP and Dante control boxes speak "
                    "different command sets and have their own drivers."
                ),
            },
        ],
        "help": {
            "overview": (
                "The 10G Control Box is the head end of an MXNet 10G system. Add the "
                "control box (not the individual encoders and decoders) and every "
                "endpoint it knows about shows up as a child you can route and pass "
                "CEC, IR and RS-232 through.\n\n"
                "Routing works per plane: video, audio, analog audio, USB, IR and RS-232 "
                "can each follow a different source, or move together with the Route "
                "command's Media setting of 'All'. Video walls, multiview layouts and "
                "matrices built in the MXNet management interface are recalled by name.\n\n"
                "This is not the same product as MXNet 1G. The EDID numbers, the HDCP "
                "numbers and the stream gate all differ between the two, so use the "
                "driver that matches the control box you have."
            ),
            "setup": (
                "1. Connect to the control box's PC Control (LAN2) port — the one used "
                "for the web interface and third-party control, not the AV network port.\n"
                "2. Enter that port's IP address. The API listens on TCP 24 and needs no "
                "login.\n"
                "3. Commission the system first (endpoints adopted, names assigned). The "
                "driver reads the roster from the control box; it does not adopt "
                "endpoints.\n"
                "4. Endpoints are listed by their custom name. Renaming one changes its "
                "label here on the next poll; its identity is its MAC address, so "
                "renaming never breaks a button that points at it.\n"
                "5. RS-232 passthrough needs the endpoint's serial port configured for "
                "the attached device — use Set Serial Port Settings."
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
                "description": "IP address of the control box's PC Control (LAN2) port.",
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
                    "How to read RS-232 data the control box relays back from an "
                    "endpoint. The 10G API has no command to set the endpoint's "
                    "encapsulation, so this has to match whatever the endpoint is "
                    "configured for."
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
                "help": "Address of the PC Control port this driver talks to (LAN2).",
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
            "encoder_options": {
                "type": "string",
                "label": "Encoder Options",
                "help": "JSON list of encoders — feeds the source dropdowns.",
                "cloud_priority": "low",
            },
            "decoder_options": {
                "type": "string",
                "label": "Decoder Options",
                "help": "JSON list of decoders — feeds the display dropdowns.",
                "cloud_priority": "low",
            },
            "endpoint_options": {
                "type": "string",
                "label": "Endpoint Options",
                "help": "JSON list of every endpoint — feeds the endpoint dropdowns.",
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
                "help": "JSON list of every wall-and-layout pair — feeds the recall dropdowns.",
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
                    "audio_volume": {
                        "type": "integer",
                        "label": "Analog Audio Volume",
                        "help": "Extracted analog audio output level on the encoder.",
                        "min": 0,
                        "max": 100,
                        "step": 1,
                        "unit": "%",
                        "control": True,
                        "cloud_priority": "high",
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
                            "reports it (s_srv_on, s_attaching...). This is not presence "
                            "— an endpoint with no source sits in s_attaching while being "
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
                    "source_analog_audio": {
                        "type": "string",
                        "label": "Analog Audio Source",
                        "control": True,
                        "cloud_priority": "low",
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
                            "reports it (s_srv_on, s_attaching...). This is not presence "
                            "— an endpoint with no source sits in s_attaching while being "
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
            {"id": "activate_multiview", "kind": "command", "icon": "layout-dashboard"},
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
                            {"value": "analogaudio", "label": "Analog Audio"},
                            {"value": "usb", "label": "USB"},
                            {"value": "infrared", "label": "Infrared"},
                            {"value": "serial", "label": "RS-232"},
                        ],
                        "help": "Route one plane on its own, or all of them together.",
                    },
                },
                "help": "Subscribe a decoder to an encoder's stream. Takes effect immediately.",
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
                            {"value": "analogaudio", "label": "Analog Audio"},
                            {"value": "usb", "label": "USB"},
                            {"value": "infrared", "label": "Infrared"},
                            {"value": "serial", "label": "RS-232"},
                        ],
                    },
                },
                "help": "Drop a decoder's incoming stream subscription.",
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
                "help": "Apply a saved matrix — its whole set of source-to-display routes.",
            },
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
                "help": "Activate a layout on a video wall.",
            },
            "activate_multiview": {
                "label": "Activate Multiview",
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
                "help": "Switch one display in a layout to its multiview arrangement.",
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
                            {"value": value, "label": label}
                            for value, label in OUTPUT_TIMINGS
                        ],
                    },
                },
                "help": (
                    "Force the resolution the decoder sends to its display. Pass through "
                    "follows the source and is the right answer unless a display refuses it."
                ),
            },
            "set_hdcp": {
                "label": "Set HDCP Mode",
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
                            {"value": "0", "label": "Off"},
                            {"value": "1", "label": "HDCP 1.4"},
                            {"value": "2", "label": "HDCP 2.2"},
                        ],
                    },
                },
                "help": (
                    "Force the HDCP version the decoder presents to the display. These "
                    "numbers are not the MXNet 1G's — the same value means a different mode "
                    "on that box."
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
                "help": "Let the decoder pass HDR through, or force it off for a display "
                "that mishandles it.",
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
                            {"value": value, "label": label}
                            for value, label in EDID_PRESETS
                        ],
                    },
                },
                "help": (
                    "Choose the EDID the encoder presents to its source. This list is not "
                    "the MXNet 1G's — the same index is a different EDID on that box."
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
                "help": "Read the EDID of the display on a decoder and present it on an encoder.",
            },
            "set_encoder_volume": {
                "label": "Set Encoder Analog Volume",
                "params": {
                    "tx": {
                        "type": "child_id",
                        "child_type": "encoder",
                        "required": True,
                        "label": "Source (Encoder)",
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
                "help": "Extracted analog audio output level on an encoder.",
            },
            "set_encoder_stream": {
                "label": "Encoder Stream",
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
                        "label": "Stream",
                        "values": [
                            {"value": "on", "label": "On"},
                            {"value": "off", "label": "Off"},
                        ],
                    },
                },
                "help": (
                    "Stop or start an encoder putting its stream on the network. Every "
                    "decoder subscribed to it loses picture while it is off. On MXNet 1G "
                    "this gate is on the decoder instead."
                ),
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
                    "input": {
                        "type": "enum",
                        "required": True,
                        "label": "Input",
                        "values": [
                            {"value": "0", "label": "HDMI 1"},
                            {"value": "1", "label": "HDMI 2"},
                        ],
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
                "help": "Turn one of a dual-input encoder's HDMI ports on or off.",
            },
            "set_downmix": {
                "label": "Set AVDM Downmix",
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
                        "label": "Downmix",
                        "values": [
                            {"value": value, "label": label}
                            for value, label in DOWNMIX_MODES
                        ],
                    },
                },
                "help": (
                    "Downmix preset for an encoder with the AVDM daughter card, which "
                    "shapes its balanced audio output. No effect on an encoder without one."
                ),
            },
            "rename_avdm": {
                "label": "Rename AVDM Card",
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
                        "pattern": "^[A-Za-z0-9_-]{1,32}$",
                    },
                },
                "help": "Name the AVDM audio daughter card on an encoder. Only encoders have one.",
            },
            "describe_avdm": {
                "label": "Describe AVDM Card",
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
                    },
                },
                "help": "Set the description shown against an encoder's AVDM daughter card.",
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
                "help": "Flash an endpoint's front LED so you can find it in the rack.",
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
                "help": "Reboot a single encoder or decoder.",
            },
            "rename_endpoint": {
                "label": "Rename Endpoint",
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
                        "label": "New Name",
                        # The API document bars a colon and a comma outright and
                        # reserves ALL / ALLRX / ALLTX; this is the safe subset.
                        "pattern": "^[A-Za-z0-9_-]{1,32}$",
                    },
                },
                "help": "Change an endpoint's custom name. It is also the name shown in the "
                "MXNet management interface.",
            },
            "describe_endpoint": {
                "label": "Describe Endpoint",
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
                        "help": "Free text — where it is, what it feeds. Spaces are fine.",
                    },
                },
                "help": "Set an endpoint's description. The control box stores it but does "
                "not report it back, so it is not shown here.",
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
                },
                "help": "Re-assert hot-plug detect — the usual fix for a source or display "
                "that handshakes but shows nothing.",
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
                    "Power the display or source on an endpoint's HDMI port over CEC. The "
                    "control box sends the whole power sequence, so this works without "
                    "knowing the device's CEC address."
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
                        "label": "CEC Bytes (hex)",
                        "pattern": "^[0-9A-Fa-f]{2,}(:[0-9A-Fa-f]{2,})*$",
                        "help": "Hex bytes, e.g. 0036. Colon-separate several messages to "
                        "send them in order.",
                    },
                },
                "help": "Send a raw CEC message out of an endpoint's HDMI port. Use CEC Power "
                "for on and off.",
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
                "help": "Send an IR code out of an endpoint's IR emitter port.",
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
                        "help": "Add a carriage return — most serial devices need one.",
                    },
                },
                "help": (
                    "Send RS-232 data out of an endpoint's serial port. Anything the "
                    "attached device sends back lands on that endpoint's Serial Data state."
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
                "help": "Configure an endpoint's RS-232 port to match the device plugged "
                "into it.",
            },
            # ── System ────────────────────────────────────────────────
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
                "help": "Reboot the control box. The MXNet system keeps passing video while "
                "it restarts; control is unavailable until it comes back.",
            },
            "sync_clock": {
                "label": "Set Clock From Server",
                "params": {},
                "help": (
                    "Set the control box's clock to this server's current time. Only needed "
                    "when no NTP server is reachable — the control box stamps its endpoint "
                    "heartbeats with this clock."
                ),
            },
            "refresh": {
                "label": "Refresh",
                "params": {},
                "help": "Re-read the endpoint roster, routes and status now.",
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
                "help": "Send any MXNet API command and return the control box's reply. For "
                "commissioning and troubleshooting.",
            },
        },
    }

    def __init__(self, device_id: str, config: dict, state: Any, events: Any) -> None:
        # Roster bookkeeping. Children are keyed by MAC (stable and unique); the
        # CBOX reports an endpoint by its CURRENT id, which is its MAC until
        # somebody renames it and its custom name afterwards, so we keep both
        # maps. Channel ownership is what turns a decoder's subscriptions back
        # into a source.
        self._known: dict[str, set[str]] = {"encoder": set(), "decoder": set()}
        self._mac_by_name: dict[str, str] = {}
        self._type_by_mac: dict[str, str] = {}
        self._name_by_mac: dict[str, str] = {}
        self._mac_by_channel: dict[str, str] = {}

        # wall name -> [layout names]
        self._walls: dict[str, list[str]] = {}

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
        # Replies are framed by _json_frame, not by a delimiter — the API
        # document states no line terminator and not every reply is JSON.
        kwargs["delimiter"] = None
        return kwargs

    def _create_frame_parser(self) -> CallableFrameParser:
        return CallableFrameParser(_json_frame)

    async def _post_connect(self) -> None:
        # Confirm this really is the MXNet 10G API before reporting connected,
        # then learn the endpoint roster. A failure here aborts the attempt.
        host = str(self.config.get("host", "")).strip()
        port = int(self.config.get("port", 24))
        doc = await self._request("config get name")
        if doc is None:
            raise ConnectionError(
                f"[{self.device_id}] No answer from the MXNet API on {host}:{port} — "
                f"check that this is the control box's PC Control port"
            )
        model = _txt(doc.get("info"))
        self.set_state("model", model)
        # A 1G, USP or Dante control box answers this query too, and its command
        # set is different enough that a wrong pairing looks like a broken
        # device rather than a wrong driver. Say which one it is instead.
        if model and "10G" not in model.upper():
            raise ConnectionError(
                f"[{self.device_id}] {host}:{port} is a {model}, not a 10G control box — "
                f"use the driver for that model"
            )
        await self._enumerate_roster()

    async def _initial_sync(self) -> None:
        # First full read; steady-state polling starts right after this.
        await self.poll()

    async def _close_session(self) -> None:
        # Runs on every teardown path: abort any in-flight request so its
        # awaiter does not hang on a dead link, and zero the poll bookkeeping so
        # the next session starts fresh.
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

        Requests are serialized AND matched by the reply's `cmd` echo, which the
        1G firmware in this family echoes byte-exactly. Matching on the echo
        rather than on arrival order is what makes the driver immune to the two
        frames that would otherwise be mistaken for a reply:

          * an unsolicited event, which the control box interleaves into the
            same connection (see on_data_received), and
          * the late reply to a request that already timed out, which would
            otherwise be handed to the NEXT request and shift every reply after
            it by one.

        The `vwid` commands have no echo to match on, because their replies are
        not JSON at all (a Lua table, or a bare "OK"). Those are returned as
        `{"cmd": <the request>, "info": <the raw text>, "code": 0}` so the rest
        of the driver sees one shape — and a raw frame is only ever accepted for
        a request that expects one, so a banner or a stray line cannot satisfy a
        real query.

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
        if int(doc.get("code", 0)) != 0:
            detail = _txt(doc.get("error")) or _txt(doc.get("info")) or "rejected"
            raise ValueError(f"The control box rejected '{line}': {detail}")
        return True

    async def on_data_received(self, data: bytes) -> None:
        # The frame parser emits an empty frame when it discards inter-object
        # noise (see _json_frame); there is nothing to parse in one.
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
            # only be matched to the request in flight — and only when that
            # request was one that answers this way.
            self._deliver_raw(text)
            return

        # An unsolicited event: empty `cmd` plus a `source` naming the channel.
        # Undocumented for the 10G; the 1G firmware emits AV-info events with
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
        echo = _txt(doc.get("cmd"))
        expected = self._pending_cmd
        if echo and expected is not None and echo != expected:
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
        if not expected.lower().startswith(_RAW_REPLY_PREFIX):
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

        The 10G API document does not mention this channel at all; the grammar
        below was read off a 1G control box, which is the same firmware family.
        Events land within about a second of the physical change, where a poll
        can be up to `poll_interval` behind, so parsing them is what makes a
        display's hot-plug and a source's timing feel immediate.

        Three shapes seen, keyed by the endpoint's port label (`IN<n>` on an
        encoder, `OUT<n>` on a decoder):

            IN1 HPD 1
            OUT1 HPD 0
            OUT1 AV INF 1920X1080p@59Hz,RGB,8Bit,HDR OFF,HDCP OFF,PCM

        Polling stays the authority — an event only ever refreshes a field the
        poll also writes, so an unrecognised or malformed event costs nothing
        but a stale value until the next cycle. The raw line is always kept on
        `last_event`, which is what makes an unknown shape visible rather than
        silently dropped.
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
                f"[{self.device_id}] The control box returned no endpoint list — commission "
                f"the system before adding it here"
            )
        self._apply_devicelist(info)

    def _apply_devicelist(self, info: dict[str, Any]) -> None:
        found: dict[str, set[str]] = {"encoder": set(), "decoder": set()}
        updates: list[tuple[str, str, dict[str, Any]]] = []
        offline = 0
        entries = {k: v for k, v in info.items() if isinstance(v, dict)}
        present = _presence(entries)

        # Channel ownership first: a decoder's routes are read by looking up the
        # channel it subscribes to, so every encoder has to be known before any
        # decoder's subscriptions can be resolved (see _routes_from_entry).
        channels: dict[str, str] = {}
        for key, entry in entries.items():
            mac = self._mac_of(key, entry)
            if mac and str(entry.get("is_host", "")) == "1":
                channel = _txt(entry.get("ch"))
                if channel:
                    channels[channel] = mac
        self._mac_by_channel = channels

        for key, entry in entries.items():
            mac = self._mac_of(key, entry)
            if mac is None:
                log.debug(f"[{self.device_id}] Skipping roster entry with no MAC: {key}")
                continue

            # An encoder is the host of its own stream channel; a decoder
            # subscribes to one. `is_host` is the control box's discriminator.
            ctype = "encoder" if str(entry.get("is_host", "")) == "1" else "decoder"
            name = _txt(entry.get("id")) or mac
            online = present.get(key, False)
            if not online:
                offline += 1

            found[ctype].add(mac)
            self._type_by_mac[mac] = ctype
            self._name_by_mac[mac] = name
            self._mac_by_name[name.lower()] = mac

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
                # indefinitely while being perfectly present, so mapping stream
                # states onto a fault code would report a fault on a frame with
                # nothing wrong with it. Which states are genuinely faults is
                # not in the API document, and guessing is what put a present
                # encoder in the offline list on the 1G driver.
                **(
                    self.child_fault()
                    if online
                    else self.child_fault(CHILD_NOT_RESPONDING)
                ),
                "mac": mac,
                "ip": _txt(entry.get("ip")),
                # `modelname` is the product an integrator recognises; `dtype`
                # is the chipset family (ast152x) and is the same on every
                # endpoint. The 10G document's examples only carry `dtype`.
                "model": _txt(entry.get("modelname")) or _txt(entry.get("dtype")),
                "firmware": _txt(entry.get("version")),
                "service_state": _txt(entry.get("state")),
            }
            if ctype == "encoder":
                common["channel"] = _txt(entry.get("ch"))
                if "edid" in entry:
                    common["edid"] = _txt(entry.get("edid"))
                if "exaudiovolume" in entry:
                    common["audio_volume"] = self._as_int(entry.get("exaudiovolume"), 0, 100)
            else:
                common.update(self._settle_routes(mac, self._routes_from_entry(entry)))

            if mac not in self._known[ctype]:
                self.register_child(ctype, mac, initial_state={"name": name, "mac": mac})
                self._known[ctype].add(mac)
            updates.append((ctype, mac, common))

        if updates:
            self.set_children_state_batch(updates)

        # Endpoints removed from the database disappear entirely. One that is
        # merely unplugged stays in the list without its heartbeat, so it keeps
        # its child and just goes online=False.
        for ctype in ("encoder", "decoder"):
            for mac in list(self._known[ctype] - found[ctype]):
                self.deregister_child(ctype, mac)
                self._known[ctype].discard(mac)
                self._type_by_mac.pop(mac, None)
                name = self._name_by_mac.pop(mac, "")
                self._mac_by_name.pop(name.lower(), None)
                self._route_expect.pop(mac, None)

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

        The roster is KEYED by the endpoint's current id — its MAC until
        somebody renames it and its custom name afterwards — so keying children
        off that would deregister every renamed endpoint and orphan its
        bindings. The `mac` member is what stays put.
        """
        mac = _txt(entry.get("mac")) or _txt(key)
        return mac.upper() if _RE_MAC.match(mac) else None

    def _routes_from_entry(self, entry: dict[str, Any]) -> dict[str, str]:
        """Derive one decoder's live routes from its channel subscriptions.

        There is no route query on the 10G. Every encoder hosts a stream channel
        (`ch`) and every decoder reports the channel it subscribes to on each
        plane, so the join is the route. A channel no encoder owns reads as
        unrouted, which is what an idle plane looks like without having to
        assume what an idle channel number is.

        A plane whose member the firmware does not report is left alone rather
        than blanked — `analogaudiopath` arrived after the document's example
        output was captured, so `ch_l` may simply not be there.
        """
        routes: dict[str, str] = {}
        for member, plane in CHANNEL_MEMBERS.items():
            if member not in entry:
                continue
            channel = _txt(entry.get(member))
            source = self._mac_by_channel.get(channel, "") if channel else ""
            routes[ROUTE_PROPERTIES[plane]] = source
        return routes

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
        """`config get device status ALL` — one reply, every endpoint's AV status."""
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

    def _expect_routes(self, rx: str, values: dict[str, str]) -> None:
        """Remember what a just-accepted route command asked for.

        The control box acknowledges a route long before it reports it — on the
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
        return self._mac_by_name.get(token.lower())

    @staticmethod
    def _as_int(value: Any, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(str(value).strip())))
        except (TypeError, ValueError):
            return low

    # ── Polling ──────────────────────────────────────────────────────

    async def poll(self) -> None:
        """Refresh the roster, routes and AV status; system info on a slow cadence.

        Two broad queries cover the whole install regardless of endpoint count —
        the roster reply carries the routes too. Transport errors propagate so
        the platform's watchdog can flip the device offline (poll() contract).
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

        await self._refresh_named_lists()

    async def _refresh_named_lists(self) -> None:
        """Populate the matrix and video wall pickers."""
        doc = await self._request("matrix list")
        if doc is not None:
            names = self._names_of(doc.get("info"))
            self.set_state(
                "matrix_options", json.dumps([{"value": n, "label": n} for n in names])
            )

        doc = await self._request("vwid list")
        if doc is not None:
            self._apply_walls(_txt(doc.get("info")))

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
            {"value": f"{wall}|{layout}", "label": f"{wall} — {layout}"}
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
            self.set_state("timezone", zone)
            return
        if setting == "ntp_servers":
            servers = str(value).replace("/", " ").split()
            if not servers:
                raise ValueError("Enter at least one NTP server")
            if len(servers) > 5:
                raise ValueError("The control box accepts at most five NTP servers")
            await self._write("config set ntp " + " ".join(servers))
            self.set_state("ntp_servers", " ".join(servers))
            return
        if setting == "dns_servers":
            servers = str(value).replace("/", " ").split()
            if not servers:
                raise ValueError("Enter at least one DNS server")
            if len(servers) > 2:
                raise ValueError("The control box accepts at most two DNS servers")
            await self._write("config set dns " + " ".join(servers))
            self.set_state("dns_servers", " ".join(servers))
            return
        raise ValueError(f"Unknown device setting: {setting}")

    # ── Commands ─────────────────────────────────────────────────────

    def _child(self, ctype: str, value: Any) -> str:
        """Resolve a child_id param to the wire identifier (the endpoint's MAC)."""
        mac = self._resolve(str(value))
        if mac is None or self._type_by_mac.get(mac) != ctype:
            raise ValueError(
                f"Unknown {ctype} {value!r} — pick one from the dropdown "
                f"(Refresh from Device re-reads the roster)"
            )
        return mac

    def _endpoint(self, value: Any) -> str:
        mac = self._resolve(str(value))
        if mac is None:
            raise ValueError(
                f"Unknown endpoint {value!r} — pick one from the dropdown "
                f"(Refresh from Device re-reads the roster)"
            )
        return mac

    def _wall_layout(self, value: Any) -> tuple[str, str]:
        """Split the wall-and-layout picker value back into its two names."""
        token = str(value).strip()
        wall, sep, layout = token.partition("|")
        wall, layout = wall.strip(), layout.strip()
        if not sep or not wall or not layout:
            raise ValueError(
                f"{token!r} is not a wall and layout — pick one from the dropdown"
            )
        known = self._walls.get(wall)
        if known is not None and layout not in known:
            offer = ", ".join(known) if known else "none"
            raise ValueError(f"Video wall {wall!r} has no layout {layout!r} (it has: {offer})")
        return wall, layout

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}

        # ── Routing ──
        if command == "route":
            tx = self._child("encoder", params["tx"])
            rx = self._child("decoder", params["rx"])
            stream = str(params.get("stream", "all")).lower()
            targets = list(ROUTE_COMMANDS) if stream == "all" else [stream]
            for name in targets:
                await self._write(f"config set device {ROUTE_COMMANDS[name]} {tx} {rx}")
            # The roster lags an accepted route, so publish the accepted value
            # now rather than leaving a panel showing the old source until a
            # poll confirms it. _write has already raised if the box refused.
            commanded = {ROUTE_PROPERTIES[name]: tx for name in targets}
            self._expect_routes(rx, commanded)
            self.set_children_state_batch([("decoder", rx, commanded)])
            return True

        if command == "route_off":
            rx = self._child("decoder", params["rx"])
            stream = str(params.get("stream", "all")).lower()
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

        if command == "recall_videowall_layout":
            wall, layout = self._wall_layout(params["layout"])
            return await self._write(f"vwid layout active {wall} {layout}")

        if command == "activate_multiview":
            wall, layout = self._wall_layout(params["layout"])
            index = str(params["index"]).strip()
            return await self._write(
                f"vwid layout multiview active {wall} {layout} {index}"
            )

        # ── Decoder ──
        if command == "set_output_resolution":
            rx = self._child("decoder", params["rx"])
            timing = str(params["timing"]).strip()
            return await self._write(f"config set device video {timing} {rx}")

        if command == "set_hdcp":
            rx = self._child("decoder", params["rx"])
            return await self._write(f"config set device hdcp {str(params['mode'])} {rx}")

        if command == "set_hdr":
            rx = self._child("decoder", params["rx"])
            return await self._write(f"config set device hdrmode {str(params['state'])} {rx}")

        # ── Encoder ──
        if command == "set_edid":
            tx = self._child("encoder", params["tx"])
            edid = str(params["edid"])
            await self._write(f"config set device edid {edid} {tx}")
            self.set_child_state("encoder", tx, "edid", edid)
            return True

        if command == "copy_edid":
            rx = self._child("decoder", params["rx"])
            tx = self._child("encoder", params["tx"])
            return await self._write(f"config set device copyedid {rx} {tx}")

        if command == "set_encoder_volume":
            tx = self._child("encoder", params["tx"])
            level = int(params["level"])
            await self._write(f"config set device exaudio volume {level} {tx}")
            self.set_child_state("encoder", tx, "audio_volume", level)
            return True

        if command == "set_encoder_stream":
            tx = self._child("encoder", params["tx"])
            state = str(params["state"]).lower()
            return await self._write(f"config set device stream {state} {tx}")

        if command == "set_hdmi_input":
            tx = self._child("encoder", params["tx"])
            port = str(params["input"])
            state = str(params["state"]).lower()
            return await self._write(f"config set device hdmi {port} {state} {tx}")

        if command == "set_downmix":
            tx = self._child("encoder", params["tx"])
            return await self._write(
                f"config set device exmxmode {str(params['mode'])} {tx}"
            )

        if command == "rename_avdm":
            tx = self._child("encoder", params["tx"])
            return await self._write(
                f"config set device avdmid {str(params['name']).strip()} {tx}"
            )

        if command == "describe_avdm":
            tx = self._child("encoder", params["tx"])
            return await self._write(
                f"config set device avdmdes {str(params['description']).strip()} {tx}"
            )

        # ── Any endpoint ──
        if command == "identify":
            mac = self._endpoint(params["endpoint"])
            return await self._write(
                f"config set device light {str(params.get('mode', 'flash')).lower()} {mac}"
            )

        if command == "reboot_endpoint":
            mac = self._endpoint(params["endpoint"])
            return await self._write(f"config set device reboot {mac}")

        if command == "rename_endpoint":
            mac = self._endpoint(params["endpoint"])
            name = str(params["name"]).strip()
            await self._write(f"config set device id {name} {mac}")
            await self._enumerate_roster()
            return True

        if command == "describe_endpoint":
            mac = self._endpoint(params["endpoint"])
            return await self._write(
                f"config set device description {str(params['description']).strip()} {mac}"
            )

        if command == "hpd_reset":
            mac = self._endpoint(params["endpoint"])
            return await self._write(f"config set device hpdrst {mac}")

        if command == "cec_power":
            mac = self._endpoint(params["endpoint"])
            word = "poweron" if str(params["state"]).lower() == "on" else "poweroff"
            return await self._write(f"config set device cec {word} {mac}")

        if command == "send_cec":
            mac = self._endpoint(params["endpoint"])
            data = str(params["data"]).replace(" ", "")
            return await self._write(f"config set device cec {data} {mac}")

        if command == "send_ir":
            mac = self._endpoint(params["endpoint"])
            return await self._write(f"config set device ir {str(params['code']).strip()} {mac}")

        if command == "send_serial":
            mac = self._endpoint(params["endpoint"])
            data = str(params["data"])
            if params.get("append_cr"):
                data += "\\r"
            data_type = "2" if str(params.get("format", "ascii")).lower() == "hex" else "1"
            return await self._write(f"config set device rs232 {data_type} {data} {mac}")

        if command == "set_serial_settings":
            mac = self._endpoint(params["endpoint"])
            baud = str(params["baud"])
            bits = str(params.get("data_bits", "8"))
            parity = str(params.get("parity", "0"))
            stop = str(params.get("stop_bits", "1"))
            return await self._write(
                f"config set device rs232setting {baud} {bits} {parity} {stop} {mac}"
            )

        # ── System ──
        if command == "reboot_cbox":
            return await self._write("config set reboot")

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

        if command == "refresh":
            await self.refresh_children()
            return True

        if command == "raw_command":
            doc = await self._request(str(params["command"]).strip())
            if doc is None:
                raise ConnectionError(f"[{self.device_id}] No reply from the control box")
            return json.dumps(doc)

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return None

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
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Could not reach an MXNet API on {host}:{port} ({exc}). Check that this is "
                f"the control box's PC Control port."
            ) from exc

        try:
            await progress("Reading the control box…", 55)
            model = await self._probe(reader, writer, "config get name")
            if not model:
                raise ConnectionError(
                    f"{host}:{port} accepted the connection but did not answer the MXNet API."
                )
            if "10G" not in model.upper():
                raise ConnectionError(
                    f"{host}:{port} is a {model}, not a 10G control box. Use the driver for "
                    f"that model — the two command sets differ."
                )
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
                f" with {endpoints} endpoint{'' if endpoints == 1 else 's'} "
                f"— {encoders} encoder{'' if encoders == 1 else 's'}, "
                f"{endpoints - encoders} decoder{'' if endpoints - encoders == 1 else 's'}"
            )
            if endpoints == 0:
                summary += ". Commission the system before adding endpoints here"
            return {"success": True, "message": summary}
        finally:
            writer.close()

    async def _probe_json(self, reader: Any, writer: Any, line: str) -> dict[str, Any]:
        writer.write((line + "\r\n").encode())
        await writer.drain()
        buf = b""
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=REQUEST_TIMEOUT_S)
            if not chunk:
                return {}
            buf += chunk
            frame, buf = _json_frame(buf)
            if frame:
                try:
                    doc = json.loads(frame.decode("utf-8", errors="replace"))
                except (ValueError, TypeError):
                    return {}
                return doc if isinstance(doc, dict) else {}

    async def _probe(self, reader: Any, writer: Any, line: str) -> str:
        doc = await self._probe_json(reader, writer, line)
        return _txt(doc.get("info"))
