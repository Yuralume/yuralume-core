"""Pre-flight validation for LINE image URLs.

LINE's Messaging API doesn't tell us *why* ``originalContentUrl`` was
rejected — we get an opaque 400 even for avoidable mistakes like a
``http://`` URL or a missing image extension. Running the cheap checks
ourselves lets ops see the real reason in the log line instead of
chasing through LINE Console's webhook error panel.

Rules enforced (all from LINE's public docs):

- URL parses as a URL at all
- scheme is HTTPS (http:// is rejected by LINE)
- total length ≤ 2000 chars
- path extension is a LINE-supported image format (png / jpeg / jpg)

LINE additionally requires the fetched response to be JPEG or PNG
(WebP / GIF are rejected despite the file extension), but we can't
check that without downloading the bytes — and for URLs that point
into our own ``/uploads/*`` we control the format upstream.
"""

from __future__ import annotations

from urllib.parse import urlparse

_MAX_URL_LENGTH = 2000
"""LINE's documented URL length cap for image messages."""

_ALLOWED_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
"""LINE only accepts JPEG/PNG for image messages. GIF/WebP would 400."""


class LineUrlValidationError(ValueError):
    """Raised when a URL definitely won't work as a LINE image URL.

    The ``reason`` field is suitable for log lines and user-facing
    error messages; it names the failing rule explicitly.
    """

    def __init__(self, reason: str, *, url: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.reason = reason
        self.url = url


def validate_line_image_url(url: str) -> None:
    """Raise :class:`LineUrlValidationError` if ``url`` can't be used.

    Returns ``None`` on success — caller should proceed to send.
    """
    if not url or not url.strip():
        raise LineUrlValidationError("url is empty", url=url)

    if len(url) > _MAX_URL_LENGTH:
        raise LineUrlValidationError(
            f"url length {len(url)} exceeds LINE's {_MAX_URL_LENGTH}-char limit",
            url=url,
        )

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise LineUrlValidationError(f"url is unparseable ({exc})", url=url) from exc

    if parsed.scheme != "https":
        raise LineUrlValidationError(
            f"LINE requires https:// scheme (got {parsed.scheme or 'none'})",
            url=url,
        )

    if not parsed.netloc:
        raise LineUrlValidationError("url has no host", url=url)

    path = parsed.path.lower()
    # Pick the last dot segment as extension; empty path or no dot fails.
    dot = path.rfind(".")
    ext = path[dot:] if dot != -1 else ""
    if ext not in _ALLOWED_IMAGE_EXTENSIONS:
        raise LineUrlValidationError(
            f"LINE only accepts .png / .jpg / .jpeg (url path ends with {ext or 'none'!r})",
            url=url,
        )
