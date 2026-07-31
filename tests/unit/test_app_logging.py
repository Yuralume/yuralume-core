from __future__ import annotations

import logging

from kokoro_link.api.app import _configure_logging


def test_configure_logging_suppresses_dependency_request_urls_when_root_is_preconfigured() -> None:
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    handler = logging.NullHandler()
    previous_httpx_level = httpx_logger.level
    previous_httpcore_level = httpcore_logger.level

    root.addHandler(handler)
    httpx_logger.setLevel(logging.INFO)
    httpcore_logger.setLevel(logging.DEBUG)
    try:
        _configure_logging()

        assert httpx_logger.level >= logging.WARNING
        assert httpcore_logger.level >= logging.WARNING
    finally:
        httpx_logger.setLevel(previous_httpx_level)
        httpcore_logger.setLevel(previous_httpcore_level)
        root.removeHandler(handler)
