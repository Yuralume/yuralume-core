"""CLI guard tests for the one-time user timezone repair tool."""

from __future__ import annotations

import pytest

from kokoro_link.cli import repair_user_timezone


def test_parse_requires_exactly_one_user_selector() -> None:
    with pytest.raises(SystemExit):
        repair_user_timezone._parse_args(["--timezone", "Asia/Taipei"])

    with pytest.raises(SystemExit):
        repair_user_timezone._parse_args([
            "--user-id", "u1",
            "--email", "a@example.com",
            "--timezone", "Asia/Taipei",
        ])


def test_parse_validates_timezone_id() -> None:
    with pytest.raises(SystemExit):
        repair_user_timezone._parse_args([
            "--email", "a@example.com",
            "--timezone", "server-local",
        ])


def test_parse_defaults_to_dry_run_until_apply_is_passed() -> None:
    args = repair_user_timezone._parse_args([
        "--email", "Alice@Example.COM",
        "--timezone", "Asia/Taipei",
    ])

    assert args.email == "alice@example.com"
    assert args.timezone == "Asia/Taipei"
    assert args.apply is False


def test_parse_accepts_apply_for_real_repair() -> None:
    args = repair_user_timezone._parse_args([
        "--user-id", "default",
        "--timezone", "UTC",
        "--apply",
    ])

    assert args.user_id == "default"
    assert args.apply is True
