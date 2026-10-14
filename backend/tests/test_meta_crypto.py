"""Token encryption at rest.

These are the properties the whole credential story rests on: the token is not readable
in the database, a modified ciphertext fails loudly instead of decrypting to garbage, and
a missing or wrong key never degrades into storing plaintext.
"""

import base64

import pytest

from app.core.crypto import DecryptionError, decrypt, encrypt, generate_key

KEY = generate_key()
OTHER_KEY = generate_key()
TOKEN = "EAABwzLixnjYBO7ZBsecretpageaccessTokenValue1234567890"


def test_a_token_survives_a_round_trip():
    assert decrypt(encrypt(TOKEN, key=KEY), key=KEY) == TOKEN


def test_the_ciphertext_does_not_contain_the_plaintext():
    sealed = encrypt(TOKEN, key=KEY)
    assert TOKEN not in sealed
    # Base64url of nonce + GCM output, so the token's own bytes cannot appear even
    # base64-encoded.
    encoded = base64.urlsafe_b64encode(TOKEN.encode()).decode()
    assert encoded[:16] not in sealed


def test_encrypting_the_same_token_twice_differs():
    """Required by GCM: a reused nonce would break confidentiality outright."""
    assert encrypt(TOKEN, key=KEY) != encrypt(TOKEN, key=KEY)


def test_a_tampered_ciphertext_is_rejected():
    version, nonce, sealed = encrypt(TOKEN, key=KEY).split(".")
    flipped = ("A" if sealed[0] != "A" else "B") + sealed[1:]
    with pytest.raises(DecryptionError, match="authentication"):
        decrypt(f"{version}.{nonce}.{flipped}", key=KEY)


def test_a_different_key_cannot_decrypt():
    with pytest.raises(DecryptionError):
        decrypt(encrypt(TOKEN, key=KEY), key=OTHER_KEY)


def test_a_missing_key_fails_instead_of_returning_anything():
    with pytest.raises(DecryptionError, match="no encryption key"):
        decrypt(encrypt(TOKEN, key=KEY), key=None)


def test_a_wrong_length_key_is_a_configuration_error_not_a_silent_no_op():
    with pytest.raises(DecryptionError, match="32 bytes"):
        encrypt(TOKEN, key=base64.urlsafe_b64encode(b"too-short").decode().rstrip("="))


def test_garbage_input_does_not_raise_anything_unexpected():
    for bad in ("", "not-a-ciphertext", "v1.onlyonepart", "v2.a.b", "a.b.c.d"):
        with pytest.raises(DecryptionError):
            decrypt(bad, key=KEY)


def test_the_ciphertext_is_tagged_with_its_version():
    assert encrypt(TOKEN, key=KEY).startswith("v1.")


def test_generated_keys_are_32_bytes_and_distinct():
    key = generate_key()
    assert len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) == 32
    assert generate_key() != key


def test_an_error_message_never_contains_key_or_token_material():
    try:
        decrypt("v1.aaaa.bbbb", key=KEY)
    except DecryptionError as exc:
        assert TOKEN[:8] not in str(exc)
        assert KEY[:8] not in str(exc)
    else:  # pragma: no cover - the call above must raise
        pytest.fail("expected DecryptionError")
