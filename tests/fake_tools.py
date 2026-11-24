"""A `ProcessRunner` double that plays back synthetic gallery-dl / yt-dlp behaviour.

It lets the adapter logic (identity checks, ordering, fallbacks, completeness, policy) be tested
deterministically. It also records every command line and environment so tests can assert the
SAFETY properties of how the tools are invoked (URL after `--`, cookies only where allowed, ...).
The real binaries are exercised separately in tests/integration/test_tool_runners_real.py.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.services.subprocess_runner import ProcessResult

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "extractor"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeToolRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[Mapping[str, str]] = []
        self._gd_dump: dict[str, tuple[Any, str, int]] = {}
        self._gd_files: dict[str, dict[int, Path]] = {}
        self._gd_download_fail: dict[str, tuple[int, str]] = {}
        self._yt_info: dict[str, tuple[Any, str, int]] = {}
        self._yt_file: dict[str, Path] = {}
        self._yt_fail: dict[str, tuple[int, str]] = {}

    # ------------------------------------------------------------------ programming the fake
    def gd_dump(self, url: str, payload: Any, *, stderr: str = "", rc: int = 0) -> None:
        self._gd_dump[url] = (fixture(payload) if isinstance(payload, str) else payload, stderr, rc)

    def gd_files(self, url: str, files: Mapping[int, Path]) -> None:
        self._gd_files[url] = dict(files)

    def gd_download_fails(self, url: str, stderr: str, rc: int = 4) -> None:
        self._gd_download_fail[url] = (rc, stderr)

    def yt_info(self, url: str, payload: Any, *, stderr: str = "", rc: int = 0) -> None:
        self._yt_info[url] = (fixture(payload) if isinstance(payload, str) else payload, stderr, rc)

    def yt_fails(self, url: str, stderr: str, rc: int = 1) -> None:
        self._yt_fail[url] = (rc, stderr)

    def yt_file(self, url: str, path: Path) -> None:
        self._yt_file[url] = path

    # ------------------------------------------------------------------ inspection helpers
    def calls_to(self, tool: str) -> list[list[str]]:
        return [c for c in self.calls if tool in c]

    def urls_requested(self, tool: str | None = None) -> list[str]:
        return [c[-1] for c in self.calls if tool is None or tool in c]

    # ------------------------------------------------------------------ ProcessRunner
    async def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float,
        max_stdout: int = 4_000_000,
        max_stderr: int = 200_000,
        watch_dir: Path | None = None,
        max_dir_bytes: int | None = None,
        min_free_bytes: int = 0,
    ) -> ProcessResult:
        argv = [str(a) for a in args]
        self.calls.append(argv)
        self.envs.append(dict(env))
        url = argv[-1]
        if "gallery_dl" in argv:
            return self._gallery_dl(argv, url)
        if "yt_dlp" in argv:
            return self._yt_dlp(argv, url)
        if argv and "ffprobe" in argv[0].lower():  # real probing of the real synthetic media
            from app.services.subprocess_runner import SubprocessRunner

            return await SubprocessRunner().run(args, cwd=cwd, env=env, timeout=timeout, max_stdout=max_stdout)
        return ProcessResult(returncode=1, stdout=b"", stderr=b"fake runner: unexpected command")

    def _gallery_dl(self, argv: list[str], url: str) -> ProcessResult:
        if "--dump-json" in argv:
            payload, stderr, rc = self._gd_dump.get(url, ([], "no such fixture", 1))
            return ProcessResult(rc, json.dumps(payload).encode(), stderr.encode())
        if url in self._gd_download_fail:
            rc, stderr = self._gd_download_fail[url]
            return ProcessResult(rc, b"", stderr.encode())
        out = Path(argv[argv.index("--directory") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for num, src in self._gd_files.get(url, {}).items():
            shutil.copyfile(src, out / f"{num:03d}{src.suffix}")
        return ProcessResult(0, b"", b"")

    def _yt_dlp(self, argv: list[str], url: str) -> ProcessResult:
        if "-J" in argv:
            if url in self._yt_fail:
                rc, stderr = self._yt_fail[url]
                return ProcessResult(rc, b"null", stderr.encode())
            payload, stderr, rc = self._yt_info.get(url, (None, "ERROR: Unsupported URL", 1))
            return ProcessResult(rc, json.dumps(payload).encode(), stderr.encode())
        if url in self._yt_fail:
            rc, stderr = self._yt_fail[url]
            return ProcessResult(rc, b"", stderr.encode())
        home = next(a[len("home:") :] for a in argv if a.startswith("home:"))
        template = argv[argv.index("-o") + 1]
        prefix = template.split("_", 1)[0]
        src = self._yt_file[url]
        dest = Path(home) / f"{prefix}_1_clip{src.suffix}"
        shutil.copyfile(src, dest)
        (Path(home) / ".paths.txt").write_text(str(dest) + "\n", encoding="utf-8")
        return ProcessResult(0, b"", b"")
