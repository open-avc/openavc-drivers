"""The ChaCha20-Poly1305 AEAD apple_tv seals every Companion Link and AirPlay
record with, checked against RFC 8439 (ChaCha20 and Poly1305 for IETF
Protocols): the worked examples of sections 2.5.2 and 2.8.2 and the Poly1305
test vectors of Appendix A.3. The driver builds the AEAD from the ChaCha20
cipher every OpenAVC build bundles, so the arithmetic of the authenticator is
its own.
"""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag

from _platform_probe import load_driver

DRV = load_driver("streaming/apple_tv.py")


def test_poly1305_matches_the_section_2_5_2_example():
    """RFC 8439 2.5.2, Poly1305 Example and Test Vector."""
    key = bytes.fromhex(
        "85d6be7857556d337f4452fe42d506a80103808afb0db2fd4abff6af4149f51b"
    )
    assert DRV.poly1305(key, b"Cryptographic Forum Research Group") == bytes.fromhex(
        "a8061dc1305136c6c22b8baf0c0127a9"
    )


@pytest.mark.parametrize(
    ("r", "s", "data", "tag"),
    [
        pytest.param(
            "02000000000000000000000000000000", "00000000000000000000000000000000",
            "ffffffffffffffffffffffffffffffff",
            "03000000000000000000000000000000", id="A.3 #5 partially reduced",
        ),
        pytest.param(
            "02000000000000000000000000000000", "ffffffffffffffffffffffffffffffff",
            "02000000000000000000000000000000",
            "03000000000000000000000000000000", id="A.3 #6 s overflows 2^128",
        ),
        pytest.param(
            "01000000000000000000000000000000", "00000000000000000000000000000000",
            "ffffffffffffffffffffffffffffffff"
            "f0ffffffffffffffffffffffffffffff"
            "11000000000000000000000000000000",
            "05000000000000000000000000000000", id="A.3 #7 carry into an all-ones limb",
        ),
        pytest.param(
            "01000000000000000000000000000000", "00000000000000000000000000000000",
            "ffffffffffffffffffffffffffffffff"
            "fbfefefefefefefefefefefefefefefe"
            "01010101010101010101010101010101",
            "00000000000000000000000000000000", id="A.3 #8 result exactly 2^130-5",
        ),
        pytest.param(
            "02000000000000000000000000000000", "00000000000000000000000000000000",
            "fdffffffffffffffffffffffffffffff",
            "faffffffffffffffffffffffffffffff", id="A.3 #9 result exactly 2^130-6",
        ),
        pytest.param(
            "01000000000000000400000000000000", "00000000000000000000000000000000",
            "e33594d7505e43b90000000000000000"
            "3394d7505e4379cd0100000000000000"
            "00000000000000000000000000000000"
            "01000000000000000000000000000000",
            "14000000000000005500000000000000", id="A.3 #10 131-bit intermediate",
        ),
        pytest.param(
            "01000000000000000400000000000000", "00000000000000000000000000000000",
            "e33594d7505e43b90000000000000000"
            "3394d7505e4379cd0100000000000000"
            "00000000000000000000000000000000",
            "13000000000000000000000000000000", id="A.3 #11 131-bit final",
        ),
    ],
)
def test_poly1305_matches_the_appendix_edge_vectors(r, s, data, tag):
    """RFC 8439 Appendix A.3, Poly1305 Message Authentication Code, test vectors #5 to #11."""
    key = bytes.fromhex(r + s)
    assert DRV.poly1305(key, bytes.fromhex(data)) == bytes.fromhex(tag)


AEAD_KEY = bytes(range(0x80, 0xA0))
AEAD_NONCE = bytes.fromhex("07000000" "4041424344454647")
AEAD_AAD = bytes.fromhex("50515253c0c1c2c3c4c5c6c7")
AEAD_PLAINTEXT = (
    b"Ladies and Gentlemen of the class of '99: If I could offer you only one "
    b"tip for the future, sunscreen would be it."
)
AEAD_SEALED = bytes.fromhex(
    "d31a8d34648e60db7b86afbc53ef7ec2a4aded51296e08fea9e2b5a736ee62d6"
    "3dbea45e8ca9671282fafb69da92728b1a71de0a9e060b2905d6a5b67ecd3b36"
    "92ddbd7f2d778b8c9803aee328091b58fab324e4fad675945585808b4831d7bc"
    "3ff4def08e4b7a9de576d26586cec64b6116"
    "1ae10b594f09e26a7e902ecbd0600691"
)


def test_aead_seals_the_section_2_8_2_example():
    """RFC 8439 2.8.2, Example and Test Vector for AEAD_CHACHA20_POLY1305: ciphertext then tag."""
    assert DRV.ChaChaPoly(AEAD_KEY).encrypt(AEAD_NONCE, AEAD_PLAINTEXT, AEAD_AAD) == AEAD_SEALED


def test_aead_opens_the_section_2_8_2_example_and_refuses_a_changed_record():
    """RFC 8439 2.8.2 read back: the same record opens; one changed byte, or other associated data, does not."""
    aead = DRV.ChaChaPoly(AEAD_KEY)
    assert aead.decrypt(AEAD_NONCE, AEAD_SEALED, AEAD_AAD) == AEAD_PLAINTEXT
    changed = bytearray(AEAD_SEALED)
    changed[5] ^= 1
    with pytest.raises(InvalidTag):
        aead.decrypt(AEAD_NONCE, bytes(changed), AEAD_AAD)
    with pytest.raises(InvalidTag):
        aead.decrypt(AEAD_NONCE, AEAD_SEALED, AEAD_AAD[:-1])
