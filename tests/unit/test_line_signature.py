from kokoro_link.infrastructure.messaging.line.signature import (
    compute_signature,
    verify_signature,
)


def test_compute_signature_is_stable() -> None:
    sig = compute_signature(channel_secret="secret", body=b"{\"a\":1}")
    assert sig == compute_signature(channel_secret="secret", body=b"{\"a\":1}")
    assert isinstance(sig, str)
    assert sig


def test_verify_accepts_matching_signature() -> None:
    body = b'{"events":[]}'
    sig = compute_signature(channel_secret="secret", body=body)

    assert verify_signature(channel_secret="secret", body=body, signature=sig)


def test_verify_rejects_tampered_body() -> None:
    body = b'{"events":[]}'
    sig = compute_signature(channel_secret="secret", body=body)

    assert not verify_signature(
        channel_secret="secret", body=body + b" ", signature=sig,
    )


def test_verify_rejects_wrong_secret() -> None:
    body = b'{"events":[]}'
    sig = compute_signature(channel_secret="secret", body=body)

    assert not verify_signature(
        channel_secret="other", body=body, signature=sig,
    )


def test_verify_rejects_empty_inputs() -> None:
    assert not verify_signature(channel_secret="", body=b"x", signature="abc")
    assert not verify_signature(channel_secret="s", body=b"x", signature="")
