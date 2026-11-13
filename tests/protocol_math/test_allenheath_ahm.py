"""AHM TLS login examples supplied by Allen & Heath for TCP/IP Protocol V1.5."""
from _platform_probe import load_driver

ahm = load_driver("audio/allenheath_ahm.py")


def test_profile_and_password_examples():
    assert ahm.login_bytes({"profile": 0, "password": ""}) == bytes.fromhex("00 00")
    assert ahm.login_bytes({"profile": 1, "password": "abc"}) == bytes.fromhex("01 61 62 63")
