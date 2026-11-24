"""Serialisation across the bot <-> extractor-worker boundary (T05.1).

Only data crosses it: the validated URL, the extraction request, the manifest and the result. Neither side
ever sends a credential, a signed CDN URL, a Python object or a traceback; typed errors travel as
`{code, reason, retry_after, position}` and drop their free-text `detail` (which may contain URLs).
"""

from __future__ import annotations

from typing import Any

from app.core.contracts import Platform
from app.core.url_validator import UrlKind, ValidatedUrl


def url_to_wire(v: ValidatedUrl) -> dict[str, Any]:
    return {
        "original_url": v.original_url,
        "extraction_url": v.extraction_url,
        "platform": v.platform.value,
        "kind": v.kind.value,
        "post_hint": v.post_hint,
        "post_id": v.post_id,
        "username": v.username,
    }


def url_from_wire(d: dict[str, Any]) -> ValidatedUrl:
    return ValidatedUrl(
        original_url=str(d["original_url"]),
        extraction_url=str(d["extraction_url"]),
        platform=Platform(d["platform"]),
        kind=UrlKind(d["kind"]),
        post_hint=d.get("post_hint"),
        post_id=d.get("post_id"),
        username=d.get("username"),
    )


SECRET_HEADER = "X-Worker-Secret"  # noqa: S105 - a header NAME, not a secret
MAX_BODY_BYTES = 8 * 1024 * 1024  # manifests carry candidate-format lists; results carry only metadata
