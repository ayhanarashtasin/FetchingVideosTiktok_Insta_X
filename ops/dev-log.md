# Development log

Chronological record of commands that were actually run and what actually happened (tasks.md
section 1: "record commands and outcomes in the development log"). Entries are facts, not
intentions. `PASS` / `FAIL` / `NOT RUN (reason)`.

Test command used throughout: `uv run pytest -q` (default run excludes `live` and `docker` markers).

---

## Phase 0 - setup and foundation (2026-11-24)

### T00.2 environment verification

| Check | Command | Result |
| --- | --- | --- |
| Python | `python --version` / `py -0p` | system default 3.11.17, also 3.14.3. Project is pinned to **3.12** (architecture: 3.12+) |
| Python 3.12 | `uv python install 3.12` | PASS - CPython 3.12.15 installed |
| Git | `git --version` | PASS - 2.55.0.windows.1 |
| FFmpeg / FFprobe | `ffmpeg -version` | PASS - 9.0.2 (gyan.dev full build; has libx264, libx265, libvpx-vp9, libopus) |
| uv | `uv --version` | PASS - 0.13.1 |
| Docker client / Compose | `docker --version`, `docker compose version` | PASS - 29.6.1 / v5.2.0 |
| **Docker daemon** | `docker info` | initially **FAIL - not running** (`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`). Docker Desktop was then started (`Start-Process "Docker Desktop.exe"`); `docker info` -> server 29.6.1, linux, 12 CPUs, 8 GB. **PASS** from then on. |
| Network / PyPI | `uv pip install --dry-run ...` | PASS - dependencies resolved |

Resolved dependency set (see `uv.lock`): aiogram 3.34.0, yt-dlp 2026.11.23, gallery-dl 1.32.21,
pydantic 2.14.4, pydantic-settings 2.16.3, aiohttp 3.14.5, pytest 9.2.0, pytest-asyncio 1.5.0, ruff 0.17.6.

### T00.1 / T00.3 repository and dependencies

* `git init -b main`; `.gitignore` and `.gitattributes` written **before** the first `git add`.
* `uv lock`, `uv sync` -> PASS. `uv export --format requirements-txt --no-dev --no-emit-project --hashes -o requirements.lock` -> PASS (hashed).
* Local git config: `core.hooksPath=.githooks` (repo-local only). Global git identity was left untouched.

### T00.5 configuration

* `app/config.py`, `.env.example`. `tests/unit/test_config.py`: PASS (37 tests). A completeness test
  initially FAILED because `WORKER_HOST` / `WORKER_PORT` were missing from `.env.example`; fixed.

### T00.7 test infrastructure and secret scanning

* `scripts/scan_secrets.py` + `.githooks/pre-commit`. The real-repo scan initially FAILED on my own
  test file (a literal credentials-in-URL sample) - the scanner was right; the sample is now built at runtime.
* `tests/conftest.py` generates synthetic media with FFmpeg lavfi (H.264/AAC mp4+mkv, VP9/Opus webm,
  HEVC, split streams, JPEG/PNG, a deliberately broken JPEG). Nothing binary is committed.

### Validation of the tests themselves

* Mutation check: replaced the exact host lookup in `url_validator.py` with `host.endswith(...)`;
  3 spoofing tests failed as intended; original restored (`74 passed`).

---

## Phase 1 - extraction feasibility, OFFLINE slice only (2026-11-24)

> **No platform was contacted and no live test was run.** The live part (T01.1-T01.6, T01.8, T01.9)
> needs permission-cleared URLs (T00.6), a bot token (T00.4) and/or API credentials, which only the owner has.

* **Installed-source reading** (gallery_dl 1.32.21, yt_dlp 2026.11.23): findings recorded in `ops/feasibility.md` section 3.
* **Real-binary flag checks against a loopback server** (`gallery-dl --config-ignore -j`, `-D/-f`,
  `yt-dlp --use-extractors ...`): `-j` output shape confirmed; `directlink` has no `num` (`{num}` -> `None`);
  unsupported URL -> exit 64; `--use-extractors "tiktok,instagram,instagram:story,twitter"` selects the
  right extractor for platform URLs and rejects `https://example.com/video.mp4` (generic excluded). PASS.
* **Offline routing** (`scripts/feasibility_probe.py offline`): every PRD URL shape is claimed by an
  extractor; **TikTok `/photo/<id>` is claimed only by gallery-dl** (yt-dlp falls through to generic).
  IG Highlights, IG Stories without an id, and profile URLs also match extractors, so the app's own
  validator must (and does) reject them. Pattern match only.
* **Bug found by tests:** `ffprobe` rejects `-nostdin` (ffmpeg-only option) -> every probe returned
  "unprobeable". Fixed (`stdin=DEVNULL`); rule for later: `-nostdin` for ffmpeg only.
* `tests/unit/test_cli_flags.py`: every CLI option the runners will use exists in the installed versions. PASS.
* `tests/unit/test_feasibility_probe.py`: parsers validated against real tool output. PASS.
* Full unit run at end of this slice: `218 passed` (+6 dependency-pin tests = 224).

### Not done in Phase 1 (owner-dependent) - see ops/feasibility.md section 6

T01.1-T01.6 (live extraction), T01.8 (hosted Telegram delivery), T01.9 (local Bot API), and therefore
T01.10 (matrix still entirely "Not tested").

---

## Phase 2 - Telegram interface and job infrastructure (2026-11-24)

Built: `app/storage` (SQLite + migration), `app/core/job_queue.py`, `worker.py` (state machine), `retry.py`,
`deadline.py`, `ports.py`, `app/services/cleanup.py` (workspaces), `app/bot/*` (auth, handlers, callbacks,
notifier, messages, middleware, keyboards), `app/logging_setup.py`, `app/services/health.py`, `app/main.py`.

| Check | Command | Result |
| --- | --- | --- |
| Unit + integration suite, Windows | `uv run pytest -q` | PASS - 445 passed, 5 skipped (symlinks need privileges on Windows) |
| **Same suite on Linux** | `docker build --target test -t tmd-test . && docker run --rm tmd-test` | **PASS - 462 passed, 2 skipped** (the 2 need a `.git` directory; covered on the host). The 5 Windows-skipped symlink-escape tests **ran and passed**; permission bits (0700/0770) asserted. |
| Lint | `uv run ruff check app scripts tests` | PASS |
| Secret scan of the whole repo | `python scripts/scan_secrets.py --all` | PASS |

Docker base image is pinned by digest (`sha256:82836454...`), read from the registry with
`docker buildx imagetools inspect python:3.12-slim-bookworm` (the local `docker image inspect` could not
resolve the tag under the containerd image store, so the registry was used as the authority).

### Bugs the tests caught (all fixed)
* A module-level aiogram `Router` can only attach to one `Dispatcher`; a second app instance raised
  `RuntimeError`. Handlers now use router factories (`create_router()`).
* `TelegramNotifier.finish()` could raise when the initial status send had also failed.
* Test mistakes (not product bugs): a TTL test created its "fresh" job before advancing the fake clock; a
  "hostile URL" DB test was blocked by the validator before reaching the DB (kept both layers tested).

### Mutation checks (tests must fail when the invariant is broken)
Optimistic UNVERIFIED->COMPLETED, skipped workspace cleanup, allowing non-public access, ignoring unknown
send outcomes: each made 2-8 tests fail; originals restored (`41 passed`).

### Phase 2 exit criteria (as tested with in-memory fakes - NOT against real Telegram)
* owner sends a URL -> acknowledgement from a job whose extractor is a stub; the handler never calls the extractor;
* unauthorized users/groups/bots/foreign callbacks -> zero replies, zero DB rows, zero extractor calls;
* queued / running / cancelled / failed jobs transition per the lifecycle diagram; illegal transitions raise;
* duplicate Telegram updates and duplicate callbacks create exactly one job.

Not done: running the real bot against Telegram (needs a token, T00.4); `app.main` cannot start yet because
the real extractor backend (Phase 3) and Telegram sender (Phase 4) are not written.

---

## Phase 3 - platform extraction adapters (2026-11-24)

Built: `app/services/subprocess_runner.py`, `app/extractors/{base,classify,ytdlp_runner,gallerydl_runner,resolver,
tiktok,instagram,twitter,router,finalize,local,factory}.py`, `app/media/{probe,quality}.py`, synthetic fixtures
(`tests/fixtures/make_fixtures.py`, 27 files) and `tests/fake_tools.py`.

| Check | Command | Result |
| --- | --- | --- |
| Whole suite, Windows | `uv run pytest -q` | PASS - 684 passed, 6 skipped |
| **Whole suite, Linux** | `docker build --target test -t tmd-test . && docker run --rm tmd-test` | **PASS - 687 passed, 3 skipped** (1 needs ffmpeg >= 6 `-display_rotation`, covered on Windows' ffmpeg 9; 2 need a `.git` dir). POSIX-only tests (killpg tree kill, rlimits, symlinks) ran. |
| Real yt-dlp / gallery-dl binaries (loopback only) | `tests/integration/test_tool_runners_real.py` | PASS - flags accepted by the pinned versions, hostile config files inert, option-looking URLs inert, generic extractor refused, size limit enforced |
| Process runner with real processes | `tests/unit/test_subprocess_runner.py` | PASS - grandchild processes die on timeout AND on cancellation; output bounded; env isolated; byte/disk budget kills |
| Lint | `uv run ruff check app scripts tests` | PASS |

### Bugs / design flaws found by the tests and fixed
* Error classification: `HTTP Error 503: Service Unavailable` matched the "unavailable => not found" rule; "redirect to
  login page" was not a login wall; bare `404`/`410`/`429` matched inside ports and error numbers.
* `yt-dlp --print` implies `--quiet`, which hid the "File is larger than max-filesize" message -> switched to
  `--print-to-file` (path file written under yt-dlp's `home` path).
* When gallery-dl produced NO files for a single-video post the yt-dlp fallback was skipped (it only handled partial
  results). Fixed by one shared `fetch_via_gallerydl` used by Instagram and X.
* `discover()` needed the job workspace (tool HOME/cwd); the port now passes it.
* **Tooling lesson:** regex word boundaries written through a shell heredoc were silently turned into backspace
  characters in `classify.py`; caught by failing tests, repaired, and every source file was scanned for stray control
  characters (none). Rule: code containing backslashes goes through the editor tools, never a heredoc.
* **Test-quality lesson:** a few early contract tests contained tautological placeholder assertions
  (`... if False else True`). They were found in review, replaced with real assertions, and a grep for the pattern now
  returns nothing.

### What Phase 3 does and does NOT establish
* ESTABLISHED (offline): given the extractor output shapes read from the pinned sources, the adapters return ordered,
  identity-checked, in-scope items; audio/covers/foreign media are excluded; a skipped item is an explicit omission;
  Stories are fetched only after account visibility, exact id and audience checks; no fallback happens after
  access/auth/rate/challenge errors; short links are resolved with per-hop validation and pinned DNS.
* NOT ESTABLISHED: that any platform currently serves those shapes. See `ops/feasibility.md` section 5 for the nine
  assumptions (A1-A9) a live run must confirm. `tests/live/test_live_corpus.py` and the probe are ready for that.

---

## Phase 4 - quality preservation and Telegram delivery (2026-11-24)

Built: `app/media/{prepare,delivery_plan}.py`, `app/services/telegram_sender.py` (+ `FdInputFile`),
`scripts/telegram_smoke.py`, `app.main.run()` (testable startup), and the test doubles
`tests/fake_telegram.py` (a fake Bot API **HTTP server**), `tests/e2e.py`.

| Check | Command | Result |
| --- | --- | --- |
| Whole suite, Windows | `uv run pytest -q` | PASS (see final numbers in the Phase 6 section) |
| **Whole suite, Linux** | `docker build --target test -t tmd-test . && docker run --rm tmd-test` | **PASS - 812 passed, 3 skipped** |
| **AT15 merge, real tools** | `tests/integration/test_merge_separate_tracks.py` | PASS - local HLS master with a SEPARATE audio rendition; real yt-dlp lists video-only + audio-only, our selector picks the merge, yt-dlp stream-copies; **decoded frames and samples are identical** to the source renditions (no re-encode), audio retained |
| **AT16 remux, real ffmpeg** | `tests/unit/test_prepare_remux.py`, same file above | PASS - MKV(H.264/AAC) -> MP4 with bit-identical packets (`-f streamhash`); VP9/Opus and HEVC(off) are never transcoded; unverifiable output is discarded |
| End-to-end vs fake Telegram HTTP server | `tests/integration/test_e2e_fake_telegram.py` | PASS (14) - the real `app.main.run` + real aiogram client: a user message becomes a media group whose multipart bytes are **identical to the source files**, in source order, with the exact submitted URL, then the real "Original files" button re-delivers documents |

### Bugs / flaws found by the tests and fixed
* The remux test first compared DECODED audio and failed: AAC-in-MP4 stores encoder-delay "priming" samples in an edit
  list, so decoders legitimately drop 1024 samples. The right proof for a remux is packet equality; for a merge across a
  framing change (TS -> MKV) it is decoded equality. Each claim now uses the matching proof.
* `prepare_assets(ffmpeg="")` fell through `or` to the PATH lookup, so "FFmpeg unavailable" could not be expressed.
* `getMe` in aiogram 3.34 returns `BotFullInfo` with 15 required fields; the fake server derives them from the installed
  model so it cannot drift.
* aiogram only raises its flood-wait exception for a POSITIVE `retry_after`; an unrecognised 429 is classified
  conservatively as an ambiguous outcome (never auto-replayed).
* Test-quality: a smoke-script test contained `... or True` hedging; replaced by a fake server that models the hosted size
  ceiling (413) so hosted-refuses / local-accepts / unexpected-outcome verdicts are deterministic. An e2e helper had two
  constant-returning stubs (`_busy`, `_polled`); replaced by real readiness/idleness checks.

### Verified by hand
* `scripts/telegram_smoke.py --large` generator: 71 MB synthetic file in 31 s (> the 50 MB hosted limit).

### What Phase 4 does and does NOT establish
* ESTABLISHED: planning rules (native vs document, caps, 2-10 batching, runs, captions with the exact URL, link message,
  button placement), byte-exact streaming from a verified file descriptor, definite-vs-ambiguous failure classification,
  no replay after an uncertain send, lossless-only video preparation, honest PARTIAL/UNVERIFIED/DELIVERY_UNKNOWN states.
* NOT ESTABLISHED: behaviour of the REAL Telegram service and clients (T01.8/T01.9/T06.6): whether it accepts every
  album shape, that videos play on Android/iOS, the real hosted size ceiling, the real local Bot API server.
