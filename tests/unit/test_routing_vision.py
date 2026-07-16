"""Tests for ``parse_vision_override`` — the tri-state parser that reads
a routing entry's optional ``supports_vision`` pin.

Tri-state semantics:

* an explicit ``True`` / ``False`` bool → that value (override the
  connection flag),
* anything else (key absent, ``None``, string, number, malformed
  entry) → ``None`` (= inherit the connection-level flag).
"""

from __future__ import annotations

from kokoro_link.application.services.routing_vision import (
    parse_vision_override,
)


def test_true_bool_returns_true() -> None:
    assert parse_vision_override({"supports_vision": True}) is True


def test_false_bool_returns_false() -> None:
    assert parse_vision_override({"supports_vision": False}) is False


def test_absent_key_returns_none() -> None:
    assert parse_vision_override({"provider_id": "openrouter"}) is None


def test_explicit_none_returns_none() -> None:
    assert parse_vision_override({"supports_vision": None}) is None


def test_string_value_returns_none() -> None:
    # "repo forbids keyword/string special-casing": a stringy truthy value
    # must NOT be coerced — only an actual bool counts as an assertion.
    assert parse_vision_override({"supports_vision": "true"}) is None


def test_number_value_returns_none() -> None:
    # 1 is truthy and ``bool`` subclasses ``int``, but ``isinstance`` order
    # keeps a bare int from masquerading as a bool.
    assert parse_vision_override({"supports_vision": 1}) is None
    assert parse_vision_override({"supports_vision": 0}) is None


def test_non_dict_entry_returns_none() -> None:
    assert parse_vision_override(None) is None
    assert parse_vision_override("supports_vision") is None
    assert parse_vision_override(["supports_vision"]) is None
