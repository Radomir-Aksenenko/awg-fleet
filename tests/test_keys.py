import base64

from awgfleet.keys import generate_keypair, generate_obfuscation, public_from_private


def test_keypair_is_valid_curve25519():
    priv, pub = generate_keypair()
    assert len(base64.b64decode(priv)) == 32
    assert len(base64.b64decode(pub)) == 32


def test_public_is_derived_from_private():
    priv, pub = generate_keypair()
    assert public_from_private(priv) == pub


def test_obfuscation_constraints():
    o = generate_obfuscation()
    assert o["S1"] != o["S2"]
    assert len({o["H1"], o["H2"], o["H3"], o["H4"]}) == 4
    assert 3 <= o["Jc"] <= 10


def test_obfuscation_is_v2():
    o = generate_obfuscation()
    assert 1 <= o["S3"] <= 63 and 1 <= o["S4"] <= 31
    assert o["I1"]  # non-empty CPS packet flips the tunnel into 2.0 mode
