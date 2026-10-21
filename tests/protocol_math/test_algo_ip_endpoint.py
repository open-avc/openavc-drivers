"""The Standard authentication signature algo_ip_endpoint puts on every
request (HMAC-SHA256 over the request line, body MD5, timestamp and nonce),
checked against the worked examples in Algo's RESTful API Guide, section
Authentication Requirements.
"""

from __future__ import annotations

from _platform_probe import load_driver

DRV = load_driver("audio/algo_ip_endpoint.py")


def test_hmac_matches_the_guides_payload_example():
    """Algo RESTful API Guide, Standard Authentication Request with a JSON Payload: Example HMAC Input and Example HMAC Digest."""
    text = DRV.signature_input(
        "POST", "/api/controls/tone/start", 1601312252, "49936",
        "6e43c05d82f71e77c586e29edb93b129",
    )
    assert text == (
        "POST:/api/controls/tone/start:6e43c05d82f71e77c586e29edb93b129:"
        "application/json:1601312252:49936"
    )
    assert DRV.signature("algo", text) == (
        "2e109d7aeed54a1cb04c6b72b1d854f442cf1ca15eb0af32f2512dd77ab6b330"
    )


def test_hmac_matches_both_sample_requests():
    """Algo RESTful API Guide, the Example Payload Request of both Standard Authentication sections (with and without a JSON payload)."""
    # The sample POST: Date 28 Sep 2020 17:07:18 GMT, nonce 1028014788.
    post = DRV.signature_input(
        "POST", "/api/controls/tone/start", 1601312838, "1028014788",
        "6e43c05d82f71e77c586e29edb93b129",
    )
    assert DRV.signature("algo", post) == (
        "c450024af4493f9cdf582499456bf58d7e134b161566e764e61bd52d24f759dc"
    )
    # The sample GET (no payload): nonce 881767496, signed three seconds
    # before its Date header. The guide's "Example HMAC Input" for this case
    # (nonce 49936) does not produce the digest it prints; this request does.
    get = DRV.signature_input(
        "GET", "/api/settings/audio.page.vol", 1601312835, "881767496", None,
    )
    assert get == "GET:/api/settings/audio.page.vol:1601312835:881767496"
    assert DRV.signature("algo", get) == (
        "c5b349415bce0b9e1b8122829d32fbe0a078791b311c4cf40369c7ab4eb165a8"
    )
