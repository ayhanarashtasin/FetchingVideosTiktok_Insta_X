"""Single source of truth for the extractor CLI options this project depends on.

The runners build their command lines from these names, and `tests/unit/test_cli_flags.py` plus
`scripts/feasibility_probe.py offline` verify that every one of them exists in the INSTALLED
yt-dlp / gallery-dl `--help`. Bumping an extractor therefore fails loudly if an option vanishes.
"""

from __future__ import annotations

# gallery-dl is licensed GPL-2.0-only. It is invoked as a SUBPROCESS ONLY - never imported -
# which keeps this project's own code separate from it (see ops/feasibility.md, T01.7).
YTDLP_REQUIRED_FLAGS: tuple[str, ...] = (
    "--ignore-config",
    "--no-playlist",
    "--no-cache-dir",
    "--no-warnings",
    "--use-extractors",
    "--socket-timeout",
    "--retries",
    "--extractor-retries",
    "--proxy",
    "--cookies",
    "--playlist-items",
    "--max-filesize",
    "--dump-single-json",
    "--format",
    "--merge-output-format",
    "--paths",
    "--output",
    "--restrict-filenames",
    "--no-mtime",
    "--no-part",
    "--no-progress",
    "--print-to-file",
    "--load-info-json",
    "--concurrent-fragments",
)

GALLERYDL_REQUIRED_FLAGS: tuple[str, ...] = (
    "--config-ignore",
    "--dump-json",
    "--option",
    "--proxy",
    "--cookies",
    "--http-timeout",
    "--retries",
    "--directory",
    "--filename",
    "--no-mtime",
    "--no-part",
    "--range",
)

# yt-dlp extractor names (lower-cased IE_NAMEs) the project may use. Everything else - notably the
# `generic` extractor that follows arbitrary pages - is excluded on purpose.
YTDLP_ALLOWED_EXTRACTORS: tuple[str, ...] = ("tiktok", "instagram", "instagram:story", "twitter")


def missing_flags(help_text: str, required: tuple[str, ...]) -> list[str]:
    """Flags from `required` that do not appear as whole option names in `help_text`."""
    import re

    return [flag for flag in required if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text)]
