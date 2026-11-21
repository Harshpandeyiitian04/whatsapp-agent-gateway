from app.whatsapp.signature import sign, verify

SECRET = "test-secret"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def test_valid_signature_accepted() -> None:
    assert verify(SECRET, BODY, sign(SECRET, BODY))


def test_tampered_body_rejected() -> None:
    assert not verify(SECRET, BODY + b" ", sign(SECRET, BODY))


def test_wrong_secret_rejected() -> None:
    assert not verify(SECRET, BODY, sign("other-secret", BODY))


def test_missing_header_rejected() -> None:
    assert not verify(SECRET, BODY, None)
    assert not verify(SECRET, BODY, "")


def test_malformed_prefix_rejected() -> None:
    digest = sign(SECRET, BODY).removeprefix("sha256=")
    assert not verify(SECRET, BODY, digest)
    assert not verify(SECRET, BODY, "sha1=" + digest)


def test_known_vector() -> None:
    # HMAC-SHA256("key", "The quick brown fox jumps over the lazy dog")
    expected = "sha256=f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8"
    assert sign("key", b"The quick brown fox jumps over the lazy dog") == expected
