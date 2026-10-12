"""Self-contained contract test for extron_sis's discovery probe.

An Extron SIS device sends its copyright banner the moment a Telnet socket
opens (DXP HD 4K PLUS guide, "Copyright Information"), so the probe sends
nothing and reads the banner. It used to send Esc 3CV, which sets verbose
mode 3: a write typed into every port-23 device a scan reached.

Runs with PyYAML only, like the rest of the community CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DRIVER_PATH = (
    Path(__file__).resolve().parent.parent / "switchers" / "extron_sis.avcdriver"
)
INFO = yaml.safe_load(DRIVER_PATH.read_text(encoding="utf-8"))
PROBE = INFO["discovery"]["tcp_probe"]

# The guide's banner, behind the Telnet option bytes a server sends first.
BANNER = (
    "\xff\xfb\x01\xff\xfb\x03(c) Copyright 2018, Extron Electronics, "
    "DXP 84 HD 4K Plus, V1.06, 60-1495-01\r\nMon, 12 Feb 2018 11:27:33\r\n"
)


def test_version_bumped():
    assert str(INFO["version"]) == "1.10.0"
    assert str(INFO["min_platform_version"]) == "0.37.0"


def test_the_probe_sends_nothing():
    assert not any(key.startswith("send") for key in PROBE), PROBE


def test_the_banner_identifies_the_device():
    assert re.search(PROBE["expect_regex"], BANNER)
    model = re.search(PROBE["extract"]["model"]["regex"], BANNER)
    assert model and model.group(1).startswith("DXP 84 HD 4K Plus")
    firmware = re.search(PROBE["extract"]["firmware"]["regex"], BANNER)
    assert firmware and firmware.group(1) == "1.06"


def _size_rule():
    rules = [r for r in INFO["responses"]
             if r.get("set", {}).get("matrix_outputs") == "$2"]
    assert len(rules) == 1
    return re.compile(rules[0]["match"])


def test_a_matrix_reports_its_size_and_the_counts_fill_in():
    """The Information request I answers "V<in>X<out> A<in>X<audio outs>"
    (DXP HD 4K PLUS guide, "General information"), tagged Info00* in
    verbose mode 3. The video half sizes the rosters and is saved into the
    count settings (learned_from)."""
    rule = _size_rule()
    for reply, size in (("V8X4 A8X2", ("8", "4")),
                        ("Info00*V8X8 A8X2", ("8", "8")),
                        ("V32X32 A32X2", ("32", "32"))):
        m = rule.match(reply)
        assert m and m.groups() == size, reply
    schema = INFO["config_schema"]
    assert schema["input_count"]["learned_from"] == "matrix_inputs"
    assert schema["output_count"]["learned_from"] == "matrix_outputs"
    types = INFO["child_entity_types"]
    assert types["output"]["instances"]["count_from_state"] == "matrix_outputs"
    assert types["input"]["instances"]["count_from_state"] == "matrix_inputs"


def test_a_single_output_device_keeps_its_flat_surface():
    rule = _size_rule()
    # A one-output size, and a DSC scaler's signal information, never match.
    assert not rule.match("V8X1 A8X1")
    assert not rule.match("Vid1 Typ6 Std0 Blk0 Hrt031.5 Vrt060.0")


# ── Bulk views: the guides' own example replies through the driver's rules ──
#
# The poll reads every output's ties a page of 16 at a time, every output's
# mutes and every input's HDCP status in one reply each, instead of a view per
# output. The platform's child_list rule places each item; this reads the
# replies the guides print with the driver's own patterns and maps, by place
# from `start`, the way that rule does (items found one after another, an item
# with no captured value writing nothing).


def _list_rule(reply: str) -> dict | None:
    for rule in INFO["responses"]:
        if "child_list" in rule and re.search(rule["match"], reply):
            return rule
    return None


def _ref(value, match) -> int:
    return int(match.group(int(value[1:]))) if isinstance(value, str) else int(value)


def _spread(reply: str, outputs: int = 32) -> dict[int, dict]:
    rule = _list_rule(reply)
    assert rule is not None, reply
    if "only_when" in rule:
        cond = rule["only_when"]
        assert cond["key"] == "matrix_outputs" and cond["operator"] == "lte"
        if outputs > cond["value"]:
            return {}
    spec = rule["child_list"]
    match = re.search(rule["match"], reply)
    group = spec.get("group", 1)
    text = match.group(int(group[1:]) if isinstance(group, str) else group)
    start = _ref(spec.get("start", 1), match)
    placed: dict[int, dict] = {}
    for place, item in enumerate(re.finditer(spec["element"], text)):
        values = {}
        for prop, expr in spec["state"].items():
            group = int(expr[1:]) if isinstance(expr, str) else expr["group"]
            raw = item.group(group)
            if raw is not None:
                values[prop] = expr["map"][raw] if isinstance(expr, dict) else int(raw)
        if values:
            placed[start + place] = values
    return placed


def test_the_poll_reads_ties_mutes_and_hdcp_in_bulk():
    queries = INFO["polling"]["queries"]
    paged = [q for q in queries if isinstance(q, dict) and q.get("page_size")]
    assert [q["send"] for q in paged] == ["\x1b0*{child_id}*1VC\r", "\x1b0*{child_id}*2VC\r"]
    assert all(q["page_size"] == 16 and q["each_child"] == "output" for q in paged)
    once = {q["send"]: q for q in queries if isinstance(q, dict) and "each_child" not in q}
    assert once["\x1bVM\r"]["when"] == "output_count"
    assert once["\x1bIHDCP\r"]["when"] == "input_count"
    # No view per output or input is left: the bulk reads replaced them.
    assert not [q for q in queries if isinstance(q, dict) and q.get("each_child")
                and not q.get("page_size")]


def test_a_tagged_tie_page_starts_at_the_output_it_names():
    """The CrossPoint 450 Plus/Ultra guide's MAV Plus 3232 example (preset 4,
    from output 17), tagged the way the DXP guide shows verbose mode 2/3."""
    reply = ("Vgp04*Out17 08 08 24 08 08 29 29 00 08 01 01 01 08 08 08 08 Vid")
    placed = _spread(reply)
    assert sorted(placed) == list(range(17, 33))
    assert [placed[o]["input"] for o in range(17, 33)] == [
        8, 8, 24, 8, 8, 29, 29, 0, 8, 1, 1, 1, 8, 8, 8, 8,
    ]


def test_an_untagged_page_is_read_only_on_16_outputs_or_fewer():
    """The XTP CrossPoint guide's example reply (XTP CrossPoint 1600 with two
    output boards): eight outputs, then "--" for the eight it does not have."""
    reply = "0808120808110000----------------Vid"
    placed = _spread(reply, outputs=8)
    assert {o: v["input"] for o, v in placed.items()} == {
        1: 8, 2: 8, 3: 12, 4: 8, 5: 8, 6: 11, 7: 0, 8: 0,
    }
    # Untagged, the reply does not say which page it is: on a frame of more
    # than 16 outputs it is not read at all.
    assert _spread(reply, outputs=32) == {}
    # The CrossPoint guide's audio example writes the items apart.
    audio = _spread("01 01 01 01 02 22 22 00 03 03 03 03 -- -- -- -- Aud", outputs=12)
    assert [audio[o]["audio_input"] for o in range(1, 13)] == [1, 1, 1, 1, 2, 22, 22, 0, 3, 3, 3, 3]


def test_every_outputs_mutes_come_in_one_reply():
    """The guides' Esc VM examples. XTP II CrossPoint 3200: digital audio
    muted on outputs 2 and 3, video on 5, video and audio on 26. CrossPoint 450
    Plus 3232 HVA: video on 5, audio on 12, video and audio on 13."""
    xtp = _spread("Mut02201000000000000000000003000000")
    muted = {o: (v["video_mute"], v["audio_mute"]) for o, v in xtp.items()
             if v["video_mute"] == "true" or v["audio_mute"] == "true"}
    assert muted == {2: ("false", "true"), 3: ("false", "true"),
                     5: ("true", "false"), 26: ("true", "true")}
    cp = _spread("Mut00001000000230000000000000000000")
    muted = {o: (v["video_mute"], v["audio_mute"]) for o, v in cp.items()
             if v["video_mute"] == "true" or v["audio_mute"] == "true"}
    assert muted == {5: ("true", "false"), 12: ("false", "true"), 13: ("true", "true")}
    # The XTP's analog audio mute (4) reads as muted too.
    assert _spread("Mut4")[1] == {"video_mute": "false", "audio_mute": "true"}


def test_every_inputs_hdcp_comes_in_one_reply():
    placed = _spread("HdcpI00*0120")
    assert placed == {
        1: {"signal": "false", "hdcp": "none"},
        2: {"signal": "true", "hdcp": "hdcp"},
        3: {"signal": "true", "hdcp": "non_hdcp"},
        4: {"signal": "false", "hdcp": "none"},
    }
    # Ahead of the per-input rules, which would read a one-input list as input 0.
    rules = [r for r in INFO["responses"] if r["match"].startswith("^HdcpI")]
    assert "child_list" in rules[0]
