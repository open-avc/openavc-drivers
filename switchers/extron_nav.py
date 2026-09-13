"""
OpenAVC Extron NAV Pro AV-over-IP driver (NAVigator System Manager).

Controls an Extron NAV system through its **NAVigator System Manager**, which is
the only supported third-party control point for the line: OpenAVC talks to the
NAVigator and the NAVigator talks to the NAV encoders and decoders. Every
endpoint the NAVigator manages (16 as shipped, up to 240 with LinkLicense) is
modelled as an OpenAVC *child entity*, so routes, presence and names are
addressable as ``device.<id>.encoder.<n>.<prop>`` / ``device.<id>.decoder.<n>.<prop>``.

**The NAVigator needs the free "LinkLicense for Third-Party Control" activated
before any of this exists.** Without it the SSH/SIS interface is not offered and
the driver cannot connect at all -- see ``compatible_models[].setup``.

Transport / protocol:

* **SSH on port 22023** (not 22), carrying Extron's SIS command grammar -- the
  same grammar ``extron_sis`` speaks to a crosspoint, with a NAVigator-specific
  command set on top. ``transport: ssh`` uses the platform SSH transport, which
  shells out to the OS OpenSSH client. ``transports`` also lists ``tcp`` so the
  same driver code runs against the bundled simulator over a raw socket; the
  framing is identical because both transports are raw byte pipes.
* Commands are ESC-prefixed and CR-terminated; responses are CRLF-terminated
  lines. The connect ceremony turns **echo off** (it is on by default, which
  would otherwise interleave a copy of every command with its reply) and sets
  **verbose mode 3**, which makes the NAVigator tag its replies with a constant
  string. That tagging is what lets the driver tell an unsolicited endpoint
  notice from the answer to a question it just asked.
* The NAVigator pushes ``DevpA`` / ``DevpC`` / ``DevpP`` (endpoint assigned /
  connected / online changed) and ``Hkdm`` (KVM hot key pressed) frames
  **on the established control connection**, interleaved with request replies.
  They are diverted structurally -- by the tag every one of them carries, not by
  enumerating the ones we happen to know -- so a frame arriving mid-request can
  never be handed to the waiting request as its reply.

Why Python rather than YAML:

``transport: ssh`` is Python-only (``PYTHON_ONLY_TRANSPORTS`` in
``drivers/spec.py``), which settles it on its own. Three things would force it
anyway: the endpoint roster is enumerated from the device out of a 4096-character
inventory string rather than declared; the tie report is a multi-line
tab-delimited table that fans out into a route property on every decoder child;
and endpoint names are read through SIS *encapsulation*, a per-endpoint
request/response nested inside the NAVigator's own grammar.

Source: Extron NAVigator System Manager User Guide, 68-2740-01 Rev. F
(2025-12-16), "SIS Operation" -- archived under
``driver-roadmap/reference-docs/``.

License: MIT.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from openavc.core.connection_fault import (
    CHILD_NOT_RESPONDING,
    CHILD_SERVICE_FAULT,
)
from openavc.drivers.base import BaseDriver
from openavc.utils.logger import get_logger

log = get_logger(__name__)

ESC = "\x1b"
CR = "\r"

# Endpoint numbers run 1..4096 (the inventory report is one status digit per
# number, and the guide's own field editor caps input/output numbers there).
ENDPOINT_MAX = 4096

# Inventory digit -> what the NAVigator is saying about that endpoint number.
# 0 is "no device or unassigned", which is the absence of a child rather than a
# child in trouble, so it never reaches child_fault().
_INV_ONLINE = "1"
_INV_OFFLINE = "2"
_INV_NOT_CONNECTED = "3"

# Every error the NAVigator can answer with, with the sentence an integrator
# should read. Guide p.151.
_ERROR_TEXT = {
    "E10": "Invalid command.",
    "E12": "Invalid port number.",
    "E13": "Invalid parameter.",
    "E14": "Invalid for this port configuration.",
    "E17": "Invalid command for this signal type.",
    "E22": "The NAVigator is busy. Try again.",
    "E24": "Privilege violation — this account may not run that command.",
    "E25": "Device not present.",
    "E28": "Bad file name, or file not found.",
    "E36": "Maximum number of ties exceeded.",
}
_ERROR_RE = re.compile(r"^(E\d{2})$")

# ── async (device-initiated) frames, recognised by the tag every one carries ──
# In verbose mode 3 an endpoint-state change arrives as DevpA/DevpC/DevpP and a
# KVM hot key as HkdmP/HkdmK. Matching the FAMILY rather than a list of known
# events is deliberate: an undocumented sibling frame must fall into the event
# path, never into a waiting request's reply (rules.md, "An async frame consumed
# as a reply desynchronises everything after it").
_ASYNC_RE = re.compile(r"^(Devp[ACP]|Hkdm[PK])\*")
_DEVP_RE = re.compile(r"^Devp([ACP])\*(\d{1,4})([ioIO])\*([01])$")
_HOTKEY_RE = re.compile(r"^Hkdm([PK])\*(\d{1,4})([ioIO])$")

# ── reply shapes ──
_TIE_RE = re.compile(
    r"^Out(\S+?)[ *]In(\S+?)[ *](All|Vid|Aud|Usb)$", re.IGNORECASE)
_INVENTORY_RE = re.compile(r"^Rprt\*Inventory\*([IO])\*([0-3]*)$")
_SYSTEM_SIZE_RE = re.compile(r"^V(\d+)X(\d+)\s+A(\d+)X(\d+)$")
_TEMP_RE = re.compile(r"^(\d+)F\s+(\d+)C$")
_ALARM_RE = re.compile(
    r"^I/O:(\S+?),Event:(\S+?),Severity:(\w+),Time:\s*(\S+)$")
_ENCAP_RE = re.compile(r"^\{([^}]*)\}(.*)$")
_CISG_RE = re.compile(r"^(?:Cisg\S*\s+)?(\d+\.\d+\.\d+\.\d+)/(\d+)\*(\d+\.\d+\.\d+\.\d+)$")

# Alarm severities worst-first, so a summary can name the worst one present.
_SEVERITY_ORDER = ["emergency", "critical", "warning", "info"]


def _endpoint_ref(number: int, kind: str) -> str:
    """The NAVigator's own way of naming one endpoint: '306i' or '1217o'."""
    return f"{number}{kind}"


def _parse_inventory(digits: str) -> dict[int, str]:
    """Inventory digit string -> {endpoint number: digit} for assigned ones.

    Position is the endpoint number (1-based). A '0' means no device is
    assigned at that number, so it is dropped rather than reported as a child
    that is down -- an unassigned slot is not a fault.
    """
    return {
        i: d
        for i, d in enumerate(digits, start=1)
        if d != "0" and i <= ENDPOINT_MAX
    }


def _tie_value(cell: str) -> int:
    """A tie-report cell to an input number. Dashes mean no tie -> 0."""
    cell = cell.strip()
    if not cell or set(cell) == {"-"}:
        return 0
    try:
        return int(cell)
    except ValueError:
        return 0


class ExtronNavDriver(BaseDriver):
    """Extron NAV Pro AV-over-IP, via the NAVigator System Manager."""

    DRIVER_INFO = {
        "id": "extron_nav",
        "name": "Extron NAV Pro AV-over-IP (NAVigator)",
        "manufacturer": "Extron",
        "category": "switcher",
        "version": "1.0.0",
        "author": "OpenAVC",
        # Computed by `python -m openavc.drivers.check` from restarts_device_for
        # (0.34.0). BaseDriver.child_fault() -- which this driver calls on every
        # endpoint poll -- needs 0.29.0 and the check cannot see a method call,
        # so 0.34.0 covers both. On an older box child_fault() would be an
        # AttributeError in the middle of a poll and take the roster down.
        "min_platform_version": "0.34.0",
        "description": (
            "Route and monitor an Extron NAV Pro AV-over-IP system through its "
            "NAVigator System Manager: video, audio and USB ties, WindoWall "
            "video-wall presets and windows, KVM workstation presets, system "
            "alarms, and every managed encoder and decoder as a child entity. "
            "Requires the free LinkLicense for Third-Party Control on the "
            "NAVigator."
        ),
        "source_url": (
            "https://media.extron.com/public/download/files/userman/"
            "68-2740-01_F_NAVigator-UG__.pdf"
        ),
        "tags": ["av-over-ip", "matrix", "encoder", "decoder", "video-wall",
                 "kvm", "sis"],
        "verified": False,
        "simulated": True,
        "protocols": ["extron_sis"],
        "ports": [22023],
        "transport": "ssh",
        # The bundled simulator is reached over raw TCP. The driver's framing is
        # byte-identical either way (both are raw pipes), so exercising it over
        # TCP validates the real connect -> poll -> command path.
        "transports": ["ssh", "tcp"],
        # USB ties are deliberately NOT a routing plane. The AV planes run
        # encoder -> decoder, but a USB tie is any-to-any: the guide's own
        # examples tie an encoder as host to a decoder as device AND a decoder
        # as host to an encoder as device. That does not fit a fixed
        # source/destination child type, so USB is offered as a command with
        # pickers over every endpoint instead of being forced into the matrix.
        "routing": {
            "destination_child_type": "decoder",
            "source_child_type": "encoder",
            "destination_param": "output",
            "source_param": "input",
            "planes": [
                {"label": "Video", "route_property": "source_video",
                 "command": "tie_video"},
                {"label": "Audio", "route_property": "source_audio",
                 "command": "tie_audio"},
            ],
        },
        "discovery": {
            # The SIS interface is an SSH server on 22023, which is the whole
            # fingerprint: the banner itself is a stock OpenSSH ident and says
            # nothing about Extron, so the PORT is what is distinctive. Declared
            # cross_vendor because of that -- the matcher leans on the OUI and
            # hostname enrichment below to settle the vendor rather than
            # trusting the banner. A banner-read probe sends nothing; the
            # server speaks first.
            #
            # Not verified against hardware. Two things are unknown without a
            # NAVigator on a bench: whether the listener is present before the
            # third-party LinkLicense is activated, and whether Extron's
            # factory SSL certificate on 443 would be a stronger fingerprint.
            "tcp_probe": {
                "port": 22023,
                "expect_regex": r"^SSH-2\.0-",
                "cross_vendor": True,
                "timeout_ms": 4000,
            },
            # Extron's MAC block, from the guide's own key (X35 = 00-05-A6-xx-xx-xx).
            "oui": ["00:05:a6"],
            # Factory device name is "NAVigator-" plus the last 3 MAC pairs.
            "hostname": [r"^NAVigator-"],
            "manufacturer_alias": ["extron"],
        },
        "compatible_models": [
            {
                "manufacturer": "Extron",
                "models": ["NAVigator"],
                "confidence": "untested",
                "notes": (
                    "Needs the free LinkLicense for Third-Party Control "
                    "activated on the NAVigator: until it is, the SSH/SIS "
                    "interface does not exist and this driver cannot connect. "
                    "Built from the NAVigator System Manager User Guide "
                    "68-2740-01 Rev. F. Not exercised against hardware: every "
                    "behaviour here is doc-derived and simulator-verified. "
                    "Endpoint RS-232 and IR ports are NOT reachable this way — "
                    "Extron requires a Pro Series control processor for the "
                    "Secure Platform Device ports on NAV endpoints."
                ),
            },
        ],
        "default_config": {
            "host": "",
            "port": 22023,
            "username": "admin",
            "ssh_auth_method": "password",
            "transport": "ssh",
            "poll_interval": 10,
            "detail_poll_interval": 120,
            "read_endpoint_names": True,
            "command_timeout": 8,
        },
        "config_schema": {
            "host": {
                "type": "string", "label": "IP Address", "required": True,
                "description": "The NAVigator's IP address (OOB or NAV LAN).",
            },
            "port": {
                "type": "integer", "default": 22023, "min": 1, "max": 65535,
                "label": "SSH/SIS Port",
                "description": "22023 unless it was changed under "
                               "Settings > Ports on the NAVigator.",
            },
            "username": {
                "type": "string", "default": "admin", "label": "Username",
                "description": "A NAVigator user account. Routing and "
                               "configuration need Administrator level; a User "
                               "level account is refused with E24.",
            },
            "password": {
                "type": "string", "secret": True, "label": "Password",
                "description": "The account's password.",
            },
            "ssh_auth_method": {
                "type": "enum", "values": ["password", "key"],
                "default": "password", "label": "SSH Auth Method",
                "description": "The NAVigator's built-in accounts are "
                               "password-based.",
            },
            "transport": {
                "type": "enum", "values": ["ssh", "tcp"], "default": "ssh",
                "label": "Transport",
                "description": "ssh for a real NAVigator. tcp is for the "
                               "bundled simulator, which serves the same "
                               "grammar on a plain socket.",
            },
            "poll_interval": {
                "type": "integer", "default": 10, "min": 2, "max": 3600,
                "label": "Poll Interval (s)",
                "description": "How often ties, endpoint presence and alarms "
                               "are re-read.",
            },
            "detail_poll_interval": {
                "type": "integer", "default": 120, "min": 10, "max": 86400,
                "label": "Detail Poll Interval (s)",
                "description": "Slower cadence for temperature, connected "
                               "users, licensing and network settings.",
            },
            "read_endpoint_names": {
                "type": "boolean", "default": True,
                "label": "Read Endpoint Names",
                "description": "Ask each endpoint for its name when it first "
                               "appears. One extra request per endpoint, so on "
                               "a 240-endpoint system the first connect takes "
                               "noticeably longer. Turn off to identify "
                               "endpoints by number alone.",
            },
            "command_timeout": {
                "type": "integer", "default": 8, "min": 2, "max": 60,
                "label": "Command Timeout (s)",
                "description": "How long to wait for one SIS reply.",
            },
        },
        "state_variables": {
            "model": {"type": "string", "label": "Model"},
            "model_description": {"type": "string", "label": "Model Description"},
            "part_number": {"type": "string", "label": "Part Number"},
            "serial_number": {"type": "string", "label": "Serial Number"},
            "device_name": {"type": "string", "label": "Device Name"},
            "firmware_version": {"type": "string", "label": "Firmware"},
            "firmware_full": {"type": "string", "label": "Firmware (Full)",
                              "cloud_priority": "low"},
            "firmware_advanced": {"type": "string", "label": "Firmware (Build)",
                                  "cloud_priority": "low"},
            "mac_address": {"type": "string", "label": "MAC Address",
                            "cloud_priority": "low"},
            "temperature_c": {
                "type": "integer", "label": "Internal Temperature",
                "unit": "C", "min": 0, "max": 100, "cloud_priority": "low",
            },
            "temperature_f": {
                "type": "integer", "label": "Internal Temperature (F)",
                "unit": "F", "min": 32, "max": 212, "cloud_priority": "low",
            },
            "input_count": {"type": "integer", "label": "Inputs"},
            "output_count": {"type": "integer", "label": "Outputs"},
            "encoders_online": {"type": "integer", "label": "Encoders Online"},
            "decoders_online": {"type": "integer", "label": "Decoders Online"},
            "connected_users": {
                "type": "integer", "label": "Connected Users",
                "min": 0, "max": 15, "cloud_priority": "low",
            },
            "igmp_querier": {"type": "string", "label": "IGMP Querier",
                             "cloud_priority": "low"},
            "alarm_count": {"type": "integer", "label": "Active Alarms",
                            "cloud_priority": "high"},
            "alarm_active": {"type": "boolean", "label": "Alarm Active",
                             "cloud_priority": "high"},
            "alarm_worst_severity": {
                "type": "enum",
                "values": ["none", "info", "warning", "critical", "emergency"],
                "label": "Worst Alarm Severity", "cloud_priority": "high",
            },
            "alarm_summary": {"type": "string", "label": "Alarm Summary",
                              "cloud_priority": "high"},
            "license_endpoints": {
                "type": "integer", "label": "Licensed Endpoints",
                "help": "How many NAV endpoints this NAVigator is licensed for "
                        "(16 as shipped, up to 240 with LinkLicense).",
                "cloud_priority": "low",
            },
            "license_summary": {"type": "string", "label": "LinkLicense",
                                "cloud_priority": "low"},
            "oob_ip_address": {"type": "string", "label": "OOB IP Address",
                               "cloud_priority": "low"},
            "oob_subnet_mask": {"type": "string", "label": "OOB Subnet Mask",
                                "cloud_priority": "low"},
            "oob_gateway": {"type": "string", "label": "OOB Gateway",
                            "cloud_priority": "low"},
            "nav_ip_address": {"type": "string", "label": "NAV LAN IP Address",
                               "cloud_priority": "low"},
            "nav_subnet_mask": {"type": "string", "label": "NAV LAN Subnet Mask",
                                "cloud_priority": "low"},
            "nav_gateway": {"type": "string", "label": "NAV LAN Gateway",
                            "cloud_priority": "low"},
            "dns_servers": {"type": "string", "label": "DNS Servers",
                            "cloud_priority": "low"},
            "last_hotkey": {
                "type": "string", "label": "Last KVM Hot Key",
                "help": "The endpoint whose KVM hot key the NAVigator last "
                        "detected, and which combination it was.",
                "cloud_priority": "low",
            },
            # Picker sources (JSON lists), read by options_state params below.
            "endpoint_options": {
                "type": "string", "label": "Endpoint Options",
                "help": "Every assigned endpoint in the NAVigator's own "
                        "notation, for the USB tie pickers.",
                "cloud_priority": "low",
            },
        },
        # Both child blocks are written out in full, with literal bounds rather
        # than the ENDPOINT_MAX constant. The contract check reads this file's
        # source: a call or a name here reads as "computed" and it silently
        # stops cross-checking the routing planes against the properties they
        # name, which is the one thing here most worth checking.
        "child_entity_types": {
            "encoder": {
                "label": "Encoder",
                "label_plural": "Encoders",
                "id_format": {"type": "integer", "min": 1, "max": 4096,
                              "pad_width": 4},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "number": {"type": "integer", "label": "I/O Number"},
                    "assigned": {
                        "type": "boolean", "label": "Assigned",
                        "help": "The NAVigator has this endpoint in its system.",
                        "cloud_priority": "low",
                    },
                    "connected": {
                        "type": "boolean", "label": "Connected",
                        "help": "The endpoint has a live control connection to "
                                "the NAVigator.",
                        "cloud_priority": "high",
                    },
                },
                "summary_fields": ["name", "number", "connected"],
                "label_field": "name",
            },
            "decoder": {
                "label": "Decoder",
                "label_plural": "Decoders",
                "id_format": {"type": "integer", "min": 1, "max": 4096,
                              "pad_width": 4},
                "state_variables": {
                    "name": {"type": "string", "label": "Name"},
                    "number": {"type": "integer", "label": "I/O Number"},
                    "assigned": {
                        "type": "boolean", "label": "Assigned",
                        "help": "The NAVigator has this endpoint in its system.",
                        "cloud_priority": "low",
                    },
                    "connected": {
                        "type": "boolean", "label": "Connected",
                        "help": "The endpoint has a live control connection to "
                                "the NAVigator.",
                        "cloud_priority": "high",
                    },
                    "source_video": {
                        "type": "integer", "label": "Video Source",
                        "help": "Input number tied to this decoder's video. "
                                "0 = untied.",
                        "cloud_priority": "high",
                    },
                    "source_audio": {
                        "type": "integer", "label": "Audio Source",
                        "help": "Input number tied to this decoder's audio. "
                                "0 = untied.",
                        "cloud_priority": "high",
                    },
                    "usb_host": {
                        "type": "string", "label": "USB Host",
                        "help": "The endpoint acting as USB host for this one, "
                                "in the NAVigator's own notation (e.g. 2026i).",
                        "cloud_priority": "low",
                    },
                },
                "summary_fields": ["name", "number", "source_video",
                                   "connected"],
                "label_field": "name",
            },
        },
        "device_settings": {
            "device_name": {
                "type": "string",
                "label": "NAVigator Name",
                "state_key": "device_name",
                "default": "",
                "setup": False,
                "regex": r"^[A-Za-z0-9\-]{1,63}$",
                "help": "The NAVigator's own name, up to 63 characters. "
                        "Letters, digits and hyphens.",
            },
        },
        "commands": {
            # ── ties ──
            "tie_av": {
                "label": "Tie Video + Audio",
                "help": "Tie an input's video and audio to one output.",
                "params": {
                    "input": {"type": "child_id", "child_type": "encoder",
                              "label": "Input", "required": True},
                    "output": {"type": "child_id", "child_type": "decoder",
                               "label": "Output", "required": True},
                },
            },
            "tie_video": {
                "label": "Tie Video",
                "help": "Tie an input's video to one output, leaving its audio "
                        "tie alone.",
                "params": {
                    "input": {"type": "child_id", "child_type": "encoder",
                              "label": "Input", "required": True},
                    "output": {"type": "child_id", "child_type": "decoder",
                               "label": "Output", "required": True},
                },
            },
            "tie_audio": {
                "label": "Tie Audio",
                "help": "Tie an input's audio to one output, leaving its video "
                        "tie alone.",
                "params": {
                    "input": {"type": "child_id", "child_type": "encoder",
                              "label": "Input", "required": True},
                    "output": {"type": "child_id", "child_type": "decoder",
                               "label": "Output", "required": True},
                },
            },
            "tie_usb": {
                "label": "Tie USB",
                "help": "Tie a USB host to a USB device. Either end may be an "
                        "encoder or a decoder — pick both from the endpoint "
                        "list.",
                "params": {
                    "host": {
                        "type": "string", "label": "Host", "required": True,
                        "options_state": "endpoint_options",
                        "help": "The endpoint acting as USB host.",
                    },
                    "device": {
                        "type": "string", "label": "USB Device", "required": True,
                        "options_state": "endpoint_options",
                        "help": "The endpoint the USB peripheral is plugged into.",
                    },
                },
            },
            "tie_av_all": {
                "label": "Tie Video + Audio To All Outputs",
                "params": {
                    "input": {"type": "child_id", "child_type": "encoder",
                              "label": "Input", "required": True},
                },
            },
            "tie_video_all": {
                "label": "Tie Video To All Outputs",
                "params": {
                    "input": {"type": "child_id", "child_type": "encoder",
                              "label": "Input", "required": True},
                },
            },
            "tie_audio_all": {
                "label": "Tie Audio To All Outputs",
                "params": {
                    "input": {"type": "child_id", "child_type": "encoder",
                              "label": "Input", "required": True},
                },
            },
            "untie_output": {
                "label": "Untie Output",
                "help": "Clear the video and audio tie on one output.",
                "params": {
                    "output": {"type": "child_id", "child_type": "decoder",
                               "label": "Output", "required": True},
                },
            },
            "untie_input": {
                "label": "Untie Input",
                "help": "Clear this input from every output it feeds.",
                "params": {
                    "input": {"type": "child_id", "child_type": "encoder",
                              "label": "Input", "required": True},
                },
            },
            "clear_av_ties": {
                "label": "Clear All AV Ties",
                "help": "Untie every output on the system.",
                "params": {},
            },
            "clear_usb_ties": {
                "label": "Clear All USB Ties",
                "params": {},
            },
            # ── WindoWall (video wall) ──
            "recall_windowall_preset": {
                "label": "Recall WindoWall Preset",
                "params": {
                    "canvas": {"type": "integer", "label": "Canvas",
                               "min": 1, "max": 8, "required": True},
                    "preset": {"type": "integer", "label": "Preset",
                               "min": 1, "max": 8, "required": True},
                },
            },
            "select_window_input": {
                "label": "Select WindoWall Window Input",
                "help": "Show an input in one window of a WindoWall canvas.",
                "params": {
                    "canvas": {"type": "integer", "label": "Canvas",
                               "min": 1, "max": 8, "required": True},
                    "window": {"type": "integer", "label": "Window",
                               "min": 1, "max": 64, "required": True},
                    "input": {"type": "child_id", "child_type": "encoder",
                              "label": "Input", "required": True},
                },
            },
            "mute_window": {
                "label": "Mute WindoWall Window",
                "params": {
                    "canvas": {"type": "integer", "label": "Canvas",
                               "min": 1, "max": 8, "required": True},
                    "window": {"type": "integer", "label": "Window",
                               "min": 1, "max": 64, "required": True},
                },
            },
            "unmute_window": {
                "label": "Unmute WindoWall Window",
                "params": {
                    "canvas": {"type": "integer", "label": "Canvas",
                               "min": 1, "max": 8, "required": True},
                    "window": {"type": "integer", "label": "Window",
                               "min": 1, "max": 64, "required": True},
                },
            },
            # ── KVM ──
            "recall_workstation_preset": {
                "label": "Recall KVM Workstation Preset",
                "params": {
                    "workstation": {"type": "integer", "label": "Workstation",
                                    "min": 1, "max": 30, "required": True},
                    "preset": {"type": "integer", "label": "Preset",
                               "min": 1, "max": 30, "required": True},
                },
            },
            # ── system ──
            "clear_alarms": {
                "label": "Clear Active Alarms",
                "help": "Clear the NAVigator's active alarm list. An alarm "
                        "whose cause is still present comes straight back.",
                "params": {},
            },
            "refresh_inventory": {
                "label": "Refresh Endpoints",
                "help": "Re-read the endpoint roster, names and ties from the "
                        "NAVigator.",
                "params": {},
            },
            "send_endpoint_command": {
                "label": "Send Command To Endpoint",
                "help": "Send a raw SIS command to one endpoint through the "
                        "NAVigator (encapsulation). The command must be valid "
                        "for that endpoint — see its own user guide. Use "
                        "<ESC> for the escape character, e.g. <ESC>CN to read "
                        "the endpoint's name.",
                "params": {
                    "endpoint": {
                        "type": "string", "label": "Endpoint", "required": True,
                        "options_state": "endpoint_options",
                    },
                    "command": {
                        "type": "string", "label": "SIS Command",
                        "required": True, "trim": False,
                        "help": "The endpoint's own SIS command, e.g. 1B to "
                                "mute its video.",
                    },
                },
            },
            "factory_reset": {
                "label": "Full Factory Reset",
                "help": "Absolute system reset. This ERASES the NAVigator: "
                        "every setting, every endpoint assignment and every "
                        "file, and the passwords revert to the factory "
                        "defaults. The system stops passing video until it is "
                        "configured again.",
                "params": {},
                # The guide does not time the reset and no NAVigator was
                # available to measure it, so this is deliberately generous:
                # over-stating it only makes the countdown longer, while
                # under-stating it raises a fault on a healthy reset. A literal
                # rather than a constant on purpose -- the contract check reads
                # this file's source, and a name here would hide the field that
                # sets the driver's platform floor.
                "restarts_device_for": 180,
            },
        },
        "quick_actions": ["tie_av", "untie_output", "refresh_inventory"],
        "actions": [
            {"id": "clear_alarms", "kind": "command", "icon": "bell-off"},
            {
                "id": "clear_av_ties", "kind": "command", "icon": "eraser",
                "confirm": "Untie every output on this NAV system? Every "
                           "display goes blank until something is routed to "
                           "it again.",
            },
            {
                "id": "factory_reset", "kind": "command", "icon": "trash-2",
                "confirm": "Factory-reset this NAVigator? It erases every "
                           "setting, every endpoint assignment and every file, "
                           "and resets the passwords to the factory defaults. "
                           "The NAV system stops passing video until somebody "
                           "configures it again. This cannot be undone.",
            },
            {
                "id": "open_web_ui", "kind": "link", "icon": "external-link",
                "label": "Open NAVigator Web UI",
                "url": "https://{host}/",
            },
        ],
        "help": {
            "overview": (
                "Controls an Extron NAV Pro AV-over-IP system through its "
                "NAVigator System Manager. OpenAVC talks to the NAVigator, "
                "which talks to the encoders and decoders — the endpoints are "
                "never addressed directly. Every endpoint the NAVigator "
                "manages appears as a child entity with its own presence and, "
                "for decoders, the input currently routed to it."
            ),
            "setup": (
                "1. Activate the free LinkLicense for Third-Party Control on "
                "the NAVigator, through Extron S3 Sales and Technical Support. "
                "Until it is activated the NAVigator does not offer the "
                "SSH/SIS interface this driver uses, and nothing here can "
                "connect.\n"
                "2. Check the SSH/SIS port on the NAVigator under "
                "Settings > Ports. 22023 is the default.\n"
                "3. Add the device with the NAVigator's IP address, that port, "
                "and a NAVigator user account. The built-in accounts are "
                "superadmin, admin and user; a User-level account is refused "
                "(E24) on anything that changes configuration, so use an "
                "Administrator account if you want to route.\n"
                "4. Either the OOB port or the NAV LAN port reaches the SIS "
                "interface — use whichever this server can see.\n\n"
                "Endpoint RS-232 and IR ports are not reachable from a "
                "third-party controller: Extron requires one of its own Pro "
                "Series control processors for those. Routing, presence, "
                "WindoWall, KVM presets and alarms are all available here."
            ),
            "connection": (
                "Check that the LinkLicense for Third-Party Control is "
                "activated on the NAVigator — without it there is no SSH/SIS "
                "interface to connect to — and that the port matches "
                "Settings > Ports (22023 by default)."
            ),
        },
    }

    def __init__(self, device_id: str, config: dict[str, Any], state, events) -> None:
        self._rx = ""
        self._lines: asyncio.Queue[str] = asyncio.Queue()
        self._cmd_lock = asyncio.Lock()
        self._detail_countdown = 0
        # {(number, kind): name} — names are expensive (one encapsulated
        # request each), so they are read once per endpoint and cached.
        self._names: dict[tuple[int, str], str] = {}
        self._roster: dict[str, dict[int, str]] = {"encoder": {}, "decoder": {}}
        super().__init__(device_id, config, state, events)

    # A raw byte pipe over both SSH and TCP: the driver frames lines itself.
    def _resolve_delimiter(self) -> bytes | None:
        return None

    # ── inbound framing ──

    async def on_data_received(self, data: bytes) -> None:
        """Split the byte stream into CRLF-terminated lines.

        Every complete line is routed for device-initiated content FIRST and
        then queued, so an unsolicited endpoint notice updates state whether or
        not a request happens to be waiting, and can never be mistaken for
        somebody's reply.
        """
        self._rx += data.decode("latin-1", errors="replace")
        while True:
            match = re.search(r"\r\n|\r|\n", self._rx)
            if not match:
                break
            line = self._rx[:match.start()]
            self._rx = self._rx[match.end():]
            self._route_line(line.strip())

    def _route_line(self, line: str) -> None:
        if not line:
            # A blank line terminates a multi-line report; the collector needs
            # to see it, so it is queued rather than dropped.
            self._lines.put_nowait("")
            return
        if _ASYNC_RE.match(line):
            try:
                self._apply_async(line)
            except Exception:
                log.debug(f"[{self.device_id}] async frame {line!r} not applied",
                          exc_info=True)
        self._lines.put_nowait(line)

    def _apply_async(self, line: str) -> None:
        """Apply a device-initiated frame to state.

        These arrive unsolicited AND as the tagged answer to an explicit query,
        which is why they are applied here in both cases rather than only in a
        reply handler: the state write is idempotent and the waiting request
        still gets its line.
        """
        m = _DEVP_RE.match(line)
        if m:
            what, number, kind, value = m.group(1), int(m.group(2)), \
                m.group(3).lower(), m.group(4) == "1"
            ctype = "encoder" if kind == "i" else "decoder"
            if not self.is_child_registered(ctype, number):
                # An endpoint we have not enumerated yet (it was just
                # assigned). The next roster poll registers it; nothing to
                # write in the meantime.
                return
            if what == "A":
                self.set_child_state(ctype, number, "assigned", value)
            elif what == "C":
                self.set_child_state(ctype, number, "connected", value)
            elif what == "P":
                self._apply_presence(ctype, number,
                                     _INV_ONLINE if value else _INV_OFFLINE)
            return

        m = _HOTKEY_RE.match(line)
        if m:
            combo = "Ctrl+Ctrl" if m.group(1) == "P" else "Ctrl+Shift"
            ref = _endpoint_ref(int(m.group(2)), m.group(3).lower())
            self.set_state("last_hotkey", f"{ref} ({combo})")
            self._bg(self.events.emit(
                f"device.hotkey.{self.device_id}",
                {"endpoint": ref, "combination": combo},
            ))

    def _bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ── request / reply ──

    def _clear(self) -> None:
        while not self._lines.empty():
            self._lines.get_nowait()

    async def _send_raw(self, wire: str) -> None:
        if not self.transport or not self.transport.connected:
            raise ConnectionError(f"[{self.device_id}] Not connected")
        await self.transport.send(wire.encode("latin-1", errors="replace"))

    async def _request(
        self,
        wire: str,
        expect: re.Pattern[str],
        timeout: float | None = None,
        *,
        allow_error: bool = False,
    ) -> re.Match[str]:
        """Send one command and return the match for its reply.

        Lines that are not the reply are discarded here — they have already
        been routed for device-initiated content by ``_route_line``, so
        dropping them costs nothing and keeps a stray frame from being read as
        an answer.
        """
        timeout = timeout or float(self.config.get("command_timeout", 8) or 8)
        async with self._cmd_lock:
            self._clear()
            await self._send_raw(wire)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"[{self.device_id}] No reply to {self._printable(wire)}")
                try:
                    line = await asyncio.wait_for(self._lines.get(), remaining)
                except asyncio.TimeoutError as e:
                    raise TimeoutError(
                        f"[{self.device_id}] No reply to "
                        f"{self._printable(wire)}") from e
                if not line:
                    continue
                m = expect.match(line)
                if m:
                    return m
                err = _ERROR_RE.match(line)
                if err and not allow_error:
                    code = err.group(1)
                    raise ValueError(
                        f"{code}: {_ERROR_TEXT.get(code, 'Command refused.')}")

    async def _request_report(
        self, wire: str, header: re.Pattern[str], timeout: float | None = None,
        *, header_is_row: bool = False,
    ) -> list[str]:
        """Send a report command and collect its multi-line table.

        A report is a header line, then one line per row, terminated by a blank
        line. The row count is bounded so a device that never sends the blank
        line cannot hold the lock until the timeout on every poll.

        ``header_is_row`` is for the one reply that has no header of its own:
        the alarm list opens straight onto its first alarm, so the line that
        proves the reply started is also data and must be kept.
        """
        timeout = timeout or float(self.config.get("command_timeout", 8) or 8)
        async with self._cmd_lock:
            self._clear()
            await self._send_raw(wire)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            rows: list[str] = []
            seen_header = False
            while len(rows) <= ENDPOINT_MAX + 2:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    if seen_header:
                        return rows
                    raise TimeoutError(
                        f"[{self.device_id}] No reply to "
                        f"{self._printable(wire)}")
                try:
                    line = await asyncio.wait_for(self._lines.get(), remaining)
                except asyncio.TimeoutError as e:
                    if seen_header:
                        return rows
                    raise TimeoutError(
                        f"[{self.device_id}] No reply to "
                        f"{self._printable(wire)}") from e
                if not seen_header:
                    if header.match(line):
                        seen_header = True
                        if header_is_row:
                            rows.append(line)
                    elif _ERROR_RE.match(line):
                        code = line
                        raise ValueError(
                            f"{code}: {_ERROR_TEXT.get(code, 'Command refused.')}")
                    continue
                if not line:
                    return rows
                rows.append(line)
            return rows

    @staticmethod
    def _printable(wire: str) -> str:
        return repr(wire.replace(ESC, "<ESC>"))

    # ── connection setup ──

    async def _post_connect(self) -> None:
        """Turn echo off and verbose tagging on, before the device is reported
        connected.

        Order matters. Echo is ON by default, so until it is off the NAVigator
        sends back a copy of every command alongside its reply; the echo of
        this very command is tolerated because the reply is matched by its own
        tag rather than by arrival order. Verbose 3 then makes every query
        answer carry a constant string, which is what tells an unsolicited
        endpoint notice apart from a reply.
        """
        self._rx = ""
        self._clear()
        self._names.clear()
        self._roster = {"encoder": {}, "decoder": {}}

        await self._request(f"{ESC}0ECHO{CR}", re.compile(r"^Echo0$"),
                            timeout=12.0)
        await self._request(f"{ESC}3CV{CR}", re.compile(r"^Vrb3$"))

    async def _initial_sync(self) -> None:
        """Identity, roster and ties, before the first poll runs against them."""
        await self._read_identity()
        await self._read_detail()
        await self._reconcile_roster()
        await self._read_ties()
        await self._read_alarms()

    # ── polling ──

    async def poll(self) -> None:
        await self._reconcile_roster()
        await self._read_ties()
        await self._read_alarms()

        self._detail_countdown -= max(1, int(self.config.get("poll_interval", 10) or 10))
        if self._detail_countdown <= 0:
            await self._read_detail()

    async def _liveness_probe(self) -> None:
        """Awaited probe so a silent link raises instead of looking healthy.

        ``send`` alone succeeds against a socket nobody is reading, so the
        probe has to wait for an answer. BaseDriver turns repeated failures
        into a typed no_response fault and reconnects.
        """
        await self._request(f"1I{CR}", re.compile(r"^NAVigator$"),
                            timeout=float(self.HEALTH_TIMEOUT_S))

    # ── reads ──

    async def _read_identity(self) -> None:
        for wire, pattern, key in (
            (f"1I{CR}", r"^(NAVigator.*)$", "model"),
            (f"2I{CR}", r"^(NAV\s+System\s+Manager.*)$", "model_description"),
            (f"N{CR}", r"^(60-\S+)$", "part_number"),
            (f"98I{CR}", r"^(\S+)$", "serial_number"),
            (f"Q{CR}", r"^(\d+\.\d+)$", "firmware_version"),
            (f"*Q{CR}", r"^(\d+\.\d+\.\d+)$", "firmware_full"),
            (f"20Q{CR}", r"^(\d+\.\d+\.\S+)$", "firmware_advanced"),
        ):
            try:
                m = await self._request(wire, re.compile(pattern))
                self.set_state(key, m.group(1).strip())
            except (TimeoutError, ValueError) as e:
                log.debug(f"[{self.device_id}] {key} unavailable: {e}")

        try:
            m = await self._request(f"{ESC}CN{CR}",
                                    re.compile(r"^(?:Ipn\s+)?(\S.*)$"))
            self.set_state("device_name", m.group(1).strip())
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] device name unavailable: {e}")

        try:
            m = await self._request(f"{ESC}CH{CR}",
                                    re.compile(r"^(?:Iph\s+)?([0-9A-Fa-f:\-]{17})$"))
            self.set_state("mac_address", m.group(1).strip())
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] MAC unavailable: {e}")

        try:
            m = await self._request(f"I{CR}", _SYSTEM_SIZE_RE)
            self.set_state("input_count", int(m.group(1)))
            self.set_state("output_count", int(m.group(2)))
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] system size unavailable: {e}")

    async def _read_detail(self) -> None:
        """The slower cadence: temperature, users, licensing, network."""
        self._detail_countdown = int(
            self.config.get("detail_poll_interval", 120) or 120)

        try:
            m = await self._request(f"{ESC}20STAT{CR}", _TEMP_RE)
            self.set_state("temperature_f", int(m.group(1)))
            self.set_state("temperature_c", int(m.group(2)))
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] temperature unavailable: {e}")

        try:
            m = await self._request(f"10I{CR}", re.compile(r"^(\d{1,2})$"))
            self.set_state("connected_users", int(m.group(1)))
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] user count unavailable: {e}")

        try:
            m = await self._request(f"50I{CR}",
                                    re.compile(r"^(\d+\.\d+\.\d+\.\d+)$"))
            self.set_state("igmp_querier", m.group(1))
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] IGMP querier unavailable: {e}")

        await self._read_license()
        await self._read_network()

    async def _read_license(self) -> None:
        try:
            m = await self._request(f"{ESC}LELIC{CR}", re.compile(r"^(\{.*)$"),
                                    allow_error=True)
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] LinkLicense unavailable: {e}")
            return
        raw = m.group(1)
        # The guide prints the reply with typographic quotes; a real unit sends
        # JSON. Normalise before parsing so either survives.
        normalised = raw.replace("\u201c", '"').replace("\u201d", '"')
        try:
            doc = json.loads(normalised)
        except ValueError:
            log.debug(f"[{self.device_id}] LinkLicense reply not JSON: {raw!r}")
            return
        features = doc.get("licensedFeature") or []
        if isinstance(features, dict):
            features = [features]
        names, endpoints = [], 0
        for entry in features:
            if not isinstance(entry, dict):
                continue
            if entry.get("status"):
                names.append(str(entry.get("name") or entry.get("description") or ""))
            count = re.search(r"(\d+)\s*Endpoints",
                              str(entry.get("description") or ""))
            if count and entry.get("status"):
                endpoints = max(endpoints, int(count.group(1)))
        if endpoints:
            self.set_state("license_endpoints", endpoints)
        self.set_state("license_summary",
                       ", ".join(n for n in names if n) or "None active")

    async def _read_network(self) -> None:
        for iface, prefix in ((1, "oob"), (2, "nav")):
            try:
                m = await self._request(f"{ESC}{iface}*CISG{CR}", _CISG_RE,
                                        allow_error=True)
            except (TimeoutError, ValueError) as e:
                log.debug(f"[{self.device_id}] {prefix} network unavailable: {e}")
                continue
            self.set_state(f"{prefix}_ip_address", m.group(1))
            self.set_state(f"{prefix}_subnet_mask", _prefix_to_mask(int(m.group(2))))
            self.set_state(f"{prefix}_gateway", m.group(3))

        try:
            m = await self._request(
                f"{ESC}1DNSS{CR}",
                re.compile(r"^(?:Dnss\d*\*)?((?:\d+\.\d+\.\d+\.\d+)(?:\*\d+\.\d+\.\d+\.\d+)*)$"),
                allow_error=True)
            self.set_state("dns_servers", m.group(1).replace("*", ", "))
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] DNS unavailable: {e}")

    async def _read_alarms(self) -> None:
        try:
            m = await self._request(f"55I{CR}", re.compile(r"^(\d{1,2})$"))
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] alarm count unavailable: {e}")
            return
        count = int(m.group(1))
        self.set_state("alarm_count", count)
        self.set_state("alarm_active", count > 0)
        if count == 0:
            self.set_state("alarm_worst_severity", "none")
            self.set_state("alarm_summary", "")
            return

        # 0 asks for every current alarm rather than the newest N.
        try:
            rows = await self._request_report(f"{ESC}V0ALRM{CR}", _ALARM_RE,
                                              header_is_row=True)
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] alarm list unavailable: {e}")
            return
        parsed = [m for m in (_ALARM_RE.match(r) for r in rows) if m]
        if not parsed:
            return
        severities = {m.group(3).lower() for m in parsed}
        worst = next((s for s in _SEVERITY_ORDER if s in severities), "info")
        self.set_state("alarm_worst_severity", worst)
        self.set_state(
            "alarm_summary",
            "; ".join(f"{m.group(1)} {m.group(2)} ({m.group(3)})"
                      for m in parsed[:6]),
        )

    # ── roster ──

    async def _reconcile_roster(self) -> None:
        """Register endpoints the NAVigator reports, drop ones it no longer does.

        The inventory report is one status digit per endpoint number, so the
        roster and each endpoint's presence arrive in the same two requests
        however many endpoints the system has.
        """
        for kind, ctype in (("I", "encoder"), ("O", "decoder")):
            try:
                m = await self._request(
                    f"{ESC}Inventory*{kind}*RPRT{CR}", _INVENTORY_RE)
            except (TimeoutError, ValueError) as e:
                log.warning(f"[{self.device_id}] {ctype} inventory failed: {e}")
                continue
            present = _parse_inventory(m.group(2))
            self._roster[ctype] = present

            for number, digit in present.items():
                if not self.is_child_registered(ctype, number):
                    self.register_child(
                        ctype, number,
                        initial_state={"number": number,
                                       "name": f"{ctype.title()} {number}"},
                    )
                self._apply_presence(ctype, number, digit)

            for stale in set(self.list_children(ctype)) - set(present):
                self.deregister_child(ctype, stale)
                self._names.pop((stale, kind.lower()), None)

        self.set_state("encoders_online", sum(
            1 for d in self._roster["encoder"].values() if d == _INV_ONLINE))
        self.set_state("decoders_online", sum(
            1 for d in self._roster["decoder"].values() if d == _INV_ONLINE))
        self._publish_endpoint_options()

        if self.config.get("read_endpoint_names", True):
            await self._read_missing_names()

    def _apply_presence(self, ctype: str, number: int, digit: str) -> None:
        """Turn one inventory digit into the child's presence and fault keys.

        The NAVigator distinguishes "offline" from "present but not connected",
        which are different jobs for whoever has to fix it, so they get
        different fault codes rather than one boolean.
        """
        if digit == _INV_ONLINE:
            fault = self.child_fault()
            extra = {"assigned": True, "connected": True}
        elif digit == _INV_NOT_CONNECTED:
            fault = self.child_fault(
                CHILD_SERVICE_FAULT,
                "On the network, but not connected to the NAVigator. Check "
                "that unicast routing between them is possible.")
            extra = {"assigned": True, "connected": False}
        else:
            fault = self.child_fault(
                CHILD_NOT_RESPONDING,
                "Offline. Check the endpoint for a power failure or a lost "
                "network connection.")
            extra = {"assigned": True, "connected": False}
        self.set_child_state_batch(ctype, number, {**extra, **fault})

    async def _read_missing_names(self) -> None:
        """Ask each newly-seen endpoint for its name, through encapsulation.

        One request per endpoint, so it runs only for endpoints whose name is
        not cached — on a steady system that is nothing at all after the first
        connect.
        """
        for ctype, kind in (("encoder", "i"), ("decoder", "o")):
            for number in sorted(self._roster[ctype]):
                if (number, kind) in self._names:
                    continue
                name = await self._read_endpoint_name(number, kind)
                # Cache the miss too, so an endpoint that will not answer is
                # not re-asked on every single poll.
                self._names[(number, kind)] = name or ""
                if name and self.is_child_registered(ctype, number):
                    self.set_child_state(ctype, number, "name", name)

    async def _read_endpoint_name(self, number: int, kind: str) -> str:
        """`{<n>I:<ESC>CN<CR>}<CR>` -> `{<n>i}<name>`."""
        ref = f"{number}{kind.upper()}"
        wire = "{" + ref + ":" + ESC + "CN" + CR + "}" + CR
        try:
            m = await self._request(wire, _ENCAP_RE, timeout=4.0,
                                    allow_error=True)
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] name for {ref} unavailable: {e}")
            return ""
        body = m.group(2).strip()
        # Verbose tagging reaches the endpoint too, so the name may come back
        # as the set-command form ("Ipn <name>").
        body = re.sub(r"^Ipn\s+", "", body)
        if _ERROR_RE.match(body) or not body:
            return ""
        return body

    def _publish_endpoint_options(self) -> None:
        """The USB-tie and encapsulation pickers, in the NAVigator's notation."""
        options = []
        for ctype, kind, label in (("encoder", "i", "Encoder"),
                                   ("decoder", "o", "Decoder")):
            for number in sorted(self._roster[ctype]):
                name = self._names.get((number, kind)) or ""
                ref = _endpoint_ref(number, kind)
                options.append({
                    "value": ref,
                    "label": f"{label} {number}" + (f" — {name}" if name else ""),
                })
        self.set_state("endpoint_options", json.dumps(options))

    # ── ties ──

    async def _read_ties(self) -> None:
        """One report gives the video and audio source of every output."""
        try:
            rows = await self._request_report(
                f"{ESC}Ties*A*RPRT{CR}", re.compile(r"^Rprt\s+ties$"))
        except (TimeoutError, ValueError) as e:
            log.warning(f"[{self.device_id}] tie report failed: {e}")
            return
        for row in rows:
            cells = [c.strip() for c in row.split("\t")]
            if len(cells) < 3:
                cells = row.split()
            if len(cells) < 3 or not cells[0].isdigit():
                continue  # the "Output InVid InAud" header, or a divider
            output = int(cells[0])
            if not self.is_child_registered("decoder", output):
                continue
            self.set_child_state_batch("decoder", output, {
                "source_video": _tie_value(cells[1]),
                "source_audio": _tie_value(cells[2]),
            })

        try:
            rows = await self._request_report(
                f"{ESC}Ties*U*RPRT{CR}", re.compile(r"^Rprt\*ties\*U$"))
        except (TimeoutError, ValueError) as e:
            log.debug(f"[{self.device_id}] USB tie report unavailable: {e}")
            return
        for row in rows:
            cells = [c.strip() for c in row.split("\t")]
            if len(cells) < 2:
                cells = row.split()
            if len(cells) < 2:
                continue
            m = re.match(r"^(\d{1,4})([ioIO])$", cells[0])
            if not m:
                continue  # the "Device Host" header, or a divider
            number, kind = int(m.group(1)), m.group(2).lower()
            ctype = "encoder" if kind == "i" else "decoder"
            if ctype != "decoder" or not self.is_child_registered(ctype, number):
                continue
            host = "" if set(cells[1]) <= {"-"} else cells[1].lower()
            self.set_child_state("decoder", number, "usb_host", host)

    # ── refresh_children (IDE "Refresh from Device") ──

    async def refresh_children(self) -> dict[str, Any]:
        self._names.clear()
        await self._reconcile_roster()
        await self._read_ties()
        return {
            "encoders": len(self.list_children("encoder")),
            "decoders": len(self.list_children("decoder")),
        }

    # ── device settings ──

    async def set_device_setting(self, setting: str, value: Any) -> bool:
        if setting != "device_name":
            return await super().set_device_setting(setting, value)
        name = str(value).strip()
        m = await self._request(f"{ESC}{name}CN{CR}",
                                re.compile(r"^Ipn\s+(\S.*)$"))
        self.set_state("device_name", m.group(1).strip())
        return True

    # ── commands ──

    async def send_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        p = params or {}

        if command == "tie_av":
            return await self._tie(p, "!", "All")
        if command == "tie_video":
            return await self._tie(p, "%", "Vid")
        if command == "tie_audio":
            return await self._tie(p, "$", "Aud")
        if command == "tie_usb":
            host, device = self._usb_ref(p, "host"), self._usb_ref(p, "device")
            await self._request(f"{ESC}{host}*{device}^{CR}", _TIE_RE)
            return True

        if command in ("tie_av_all", "tie_video_all", "tie_audio_all"):
            suffix = {"tie_av_all": "!", "tie_video_all": "%",
                      "tie_audio_all": "$"}[command]
            inp = int(p["input"])
            # The reply to a tie-to-all is a bare "<n> All" / "<n> Vid", not the
            # Out/In form a single tie answers with.
            await self._request(
                f"{ESC}{inp}*{suffix}{CR}",
                re.compile(r"^\S+\s+(All|Vid|Aud)$", re.IGNORECASE))
            await self._read_ties()
            return True

        if command == "untie_output":
            out = int(p["output"])
            await self._request(f"{ESC}00*{out}!{CR}", _TIE_RE)
            if self.is_child_registered("decoder", out):
                self.set_child_state_batch("decoder", out, {
                    "source_video": 0, "source_audio": 0})
            return True

        if command == "untie_input":
            inp = int(p["input"])
            await self._request(f"{ESC}{inp}*00!{CR}", _TIE_RE)
            await self._read_ties()
            return True

        if command == "clear_av_ties":
            await self._request(f"{ESC}0*!{CR}",
                                re.compile(r"^\S+\s+All$", re.IGNORECASE))
            await self._read_ties()
            return True

        if command == "clear_usb_ties":
            await self._request(f"{ESC}0i*^{CR}",
                                re.compile(r"^\S+\s+Usb$", re.IGNORECASE))
            await self._read_ties()
            return True

        if command == "recall_windowall_preset":
            canvas, preset = int(p["canvas"]), int(p["preset"])
            await self._request(f"{ESC}R1*{canvas}*{preset}PRST{CR}",
                                re.compile(rf"^PrstR1\*{canvas}\*{preset}$"))
            return True

        if command == "select_window_input":
            canvas, window, inp = int(p["canvas"]), int(p["window"]), int(p["input"])
            await self._request(f"{ESC}{canvas}*{window}*{inp}!X{CR}",
                                re.compile(rf"^Grp{canvas}\*{window}\*\S+$"))
            return True

        if command in ("mute_window", "unmute_window"):
            canvas, window = int(p["canvas"]), int(p["window"])
            value = "1" if command == "mute_window" else "0"
            # No ESC and no terminator: the trailing B ends this one, which is
            # how the guide writes it.
            await self._request(f"{canvas}*{window}*{value}B{CR}",
                                re.compile(rf"^Vmt{canvas}\*{window}\*{value}$"))
            return True

        if command == "recall_workstation_preset":
            ws, preset = int(p["workstation"]), int(p["preset"])
            await self._request(f"{ESC}R3*{ws}*{preset}PRST{CR}",
                                re.compile(rf"^PrstR3\*{ws}\*{preset}$"))
            return True

        if command == "clear_alarms":
            await self._request(f"{ESC}C0ALRM{CR}", re.compile(r"^AlrmC\d+$"))
            await self._read_alarms()
            return True

        if command == "refresh_inventory":
            return await self.refresh_children()

        if command == "send_endpoint_command":
            return await self._send_endpoint_command(
                str(p["endpoint"]).strip(), str(p["command"]))

        if command == "factory_reset":
            await self._request(f"{ESC}ZQQQ{CR}", re.compile(r"^Zpq$"),
                                timeout=15.0)
            return True

        log.warning(f"[{self.device_id}] Unknown command: {command}")
        return False

    async def _tie(self, params: dict[str, Any], suffix: str, tag: str) -> bool:
        inp, out = int(params["input"]), int(params["output"])
        await self._request(f"{ESC}{inp}*{out}{suffix}{CR}", _TIE_RE)
        # The NAVigator confirmed the tie, so writing it through is a readback
        # rather than a guess — and it means the crosspoint lights now instead
        # of on the next poll.
        if self.is_child_registered("decoder", out):
            updates = {}
            if tag in ("All", "Vid"):
                updates["source_video"] = inp
            if tag in ("All", "Aud"):
                updates["source_audio"] = inp
            self.set_child_state_batch("decoder", out, updates)
        return True

    @staticmethod
    def _usb_ref(params: dict[str, Any], key: str) -> str:
        """A USB tie endpoint reference, as the NAVigator writes them.

        The picker hands back '306i'. A hand-typed number with no i/o suffix is
        ambiguous (an input and an output may share a number), so it is refused
        here rather than sent and mis-tied.
        """
        raw = str(params[key]).strip()
        if re.match(r"^\d{1,4}[ioIO]$", raw):
            return raw.lower()
        if re.match(r"^\d+$", raw):
            raise ValueError(
                f"{key!r} needs an i or o suffix saying whether {raw} is an "
                f"input or an output (e.g. {raw}i)")
        return raw  # a device name or IP address, both valid here

    async def _send_endpoint_command(self, endpoint: str, command: str) -> str:
        """Encapsulation passthrough: run one endpoint's own SIS command."""
        if not endpoint:
            raise ValueError("An endpoint is required.")
        body = command.replace("<ESC>", ESC).replace("<CR>", CR)
        if not body.endswith(CR):
            body += CR
        wire = "{" + endpoint + ":" + body + "}" + CR
        m = await self._request(wire, _ENCAP_RE, allow_error=True)
        reply = m.group(2).strip()
        err = _ERROR_RE.match(reply)
        if err:
            raise ValueError(
                f"{err.group(1)}: "
                f"{_ERROR_TEXT.get(err.group(1), 'The endpoint refused it.')}")
        return reply


def _prefix_to_mask(bits: int) -> str:
    """CIDR prefix length to dotted-quad, for the subnet the NAVigator reports
    as a prefix.
    """
    bits = max(0, min(32, bits))
    value = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF if bits else 0
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))
