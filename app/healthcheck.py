"""Container health check: `python -m app.healthcheck` (exit 0 = healthy, 1 = unhealthy).

Reads only the heartbeat file; it never contacts Telegram, so the check itself cannot leak or
rate-limit anything.
"""

from __future__ import annotations

import sys

from app.config import Settings
from app.services.health import evaluate_health, read_heartbeat


def main() -> int:
    settings = Settings()
    problems = evaluate_health(
        read_heartbeat(settings.heartbeat_file),
        max_age=settings.heartbeat_max_age_seconds,
        min_free_bytes=settings.min_free_disk_bytes // 4,  # unhealthy only when really critical
    )
    for p in problems:
        print(f"UNHEALTHY: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
