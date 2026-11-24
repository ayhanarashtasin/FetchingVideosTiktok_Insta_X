# Implementation Task Plan

# Telegram Social Media Downloader Bot

Version 1.0 | 2026-11-24 | Related: [PRD](PRD.md), [Architecture](architecture.md)

## 1. Execution instructions

This checklist is intended for a developer or coding agent (Claude Code, Codex, Cursor, etc.) working in the project repository. Read the PRD and architecture in full before changing code. The three documents define the requested scope; if a platform integration is blocked, document the evidence and limitation instead of silently removing a requirement or claiming success.

- Execute phases in order, except independent tests/documentation may be parallelized. The high-risk extraction feasibility phase must precede the full implementation.
- Keep tasks unchecked until their acceptance criteria pass. Do not mark mocked-only or theoretical support as live-tested.
- After each phase, run relevant tests, record commands and outcomes in the development log, and commit a coherent change. Never commit actual secrets or downloaded third-party media.
- Use the simplest maintainable implementation consistent with the architecture; no unnecessary frontend, microservice infrastructure, paid API or AI component.
- Use only authorized public test content and ordinary permitted access. Stop on access denial, private content, CAPTCHA or platform restriction; do not attempt bypasses.
- Do not silently downscale, strip watermarks, send a thumbnail in place of a source file, or return incomplete carousels as complete.
- Keep the user informed of real blockers; a failed live integration cannot be fixed by adding a fake success fixture.

### Priority definitions

- **P0:** required for the full release.
- **P1:** important operational improvement; may follow launch if the P0 release gate is met.
- **P2:** optional future enhancement; not required for v1.

## 2. Milestones and effort estimate

Estimates assume one developer familiar with Python, basic async programming and Docker, with access to a Telegram bot token and permission-cleared test posts. They are not commitments. A coding agent may accelerate scaffolding, but platform access, integration tests and review still take real effort.

| Phase | Deliverable | Depends on | Estimated effort |
| --- | --- | --- | --- |
| 0 | Repo, credentials and test environment | None | 0.5 day |
| 1 | Extraction feasibility matrix | 0 | 1-2 days |
| 2 | Telegram bot and job infrastructure | 1 | 1 day |
| 3 | Platform adapters and unified extraction | 1, 2 | 2-3 days |
| 4 | Media quality, album delivery and original files | 2, 3 | 1-2 days |
| 5 | Reliability, privacy and security hardening | 2-4 | 1-2 days |
| 6 | Tests and VPS deployment | 0-5 | 1-2 days |
| 7 | Client acceptance and handover | 6 | 0.5 day |

Estimated total: approximately **8-13 focused development days**, assuming the initial feasibility tests pass. Upstream breakages, public Story access and quality-preserving delivery can increase this materially. A single-video prototype is substantially smaller than the full scope.

## 3. Phase 0 - Setup and project foundation (P0)

**Goal:** A reproducible local Python project, a private test bot and a safe source corpus.

- [x] **T00.1** Create Git repository and copy `PRD.md`, `architecture.md`, `tasks.md` to its root. Create `README.md` and the directory structure from the architecture.
- [x] **T00.2** Install a supported Python version, Git, FFmpeg/FFprobe, Docker and Compose. On Windows, use Docker Desktop/WSL2 or another suitable Linux environment. Verify tool availability. — _Done: Python 3.12.15, Git 2.55, FFmpeg/FFprobe 9.0.2, Docker 29.6.1 + Compose v5.2.0; daemon started and an image built (ops/dev-log.md)._
- [x] **T00.3** Set up `pyproject.toml`, dependency lock, development tools and `.gitignore`. Include `.env`, `secrets/`, local databases, temp files and test downloads in ignore rules. Initially install aiogram, yt-dlp, gallery-dl, Pydantic, HTTP client, pytest and pytest-asyncio; pin after verification.
- [ ] **T00.4** Owner creates a Telegram bot through BotFather, sets a suitable display name, and securely configures its token and numeric Telegram user ID. No secret values are required in the repository or in the documentation. — _Status: **owner-only** (BotFather + your numeric ID); put them in `.env` (see `.env.example`). Not done._
- [x] **T00.5** Create `.env.example` with the documented configuration defaults and placeholders. Add config validation, private-chat-only default and startup allowlist checks.
- [ ] **T00.6** Build a permission-cleared live test corpus for all P01-P09 content types. Use owner-created posts, licensed material, or explicit permission. Collect active public Instagram photo/video Story URLs shortly before testing. Keep the private live-test URL list out of Git. — _Status: **owner-only**. Template + instructions in `tests/live/` (corpus file is git-ignored). Not done._
- [x] **T00.7** Set up `tests/fixtures/` with synthetic media and sanitized extractor outputs, a test runner and a development log. Use Git hooks or CI secret scanning where practical. — _Done: synthetic media is generated by FFmpeg at test time; `tests/fixtures/extractor/` holds 27 SYNTHETIC extractor outputs (no live captures exist yet, so none could be 'sanitized'; each is marked `_synthetic`, regenerated and drift-checked by a test); pytest runner, dev log and pre-commit secret scan are in place._

**Exit criteria:** Tests can run from a fresh environment; config validates; no credentials or third-party downloads are tracked; developer can start the test bot with a valid token and knows which live URLs to use.

## 4. Phase 1 - Extraction feasibility spike (P0, critical path)

**Goal:** Prove the candidate dependencies can retrieve the required content before investing in the rest of the bot. Run tests only on permitted individual posts. Record the exact version, observed capabilities, item counts, formats, access requirements and known blockers. Do not implement fallback by evading restrictions.

- [ ] **T01.1** Test yt-dlp on TikTok long-form item URLs and short share links. Confirm best available video, audio, dimensions and file size. Confirm URL identifies a single target post. — _Status: live pending (needs T00.6). Offline: `TikTok` extractor claims the URL shapes (pattern match only)._
- [ ] **T01.2** Test gallery-dl on TikTok photo-mode links, including a short link. Confirm every actual photo is returned in original order, not just audio or a video thumbnail. This is a high-risk task; investigate documented permitted alternatives if the candidate is unsupported. — _Status: live pending (needs T00.6). Offline: only gallery-dl claims `/photo/<id>`; its source yields photos then an audio item (must be disabled). See ops/feasibility.md §3._
- [ ] **T01.3** Test Instagram single photos, Reels and video posts using candidate extractors. Record whether a normal authorized session is needed. — _Status: live pending (needs T00.6)._
- [ ] **T01.4** Test Instagram photo and mixed photo/video carousels. Validate count and order against the visible source post; verify videos are not replaced with preview images. — _Status: live pending (needs T00.6). Offline: gallery-dl silently skips carousel items lacking media (only a warning), so `count` cannot prove completeness._
- [ ] **T01.5** Test an individual active public Instagram photo Story and video Story. Start with ordinary public access; if needed and permitted, use an owner-authorized normal session via a restricted secret file. Confirm the extractor selects only the requested Story ID, and can distinguish public vs restricted content. Reject private/Close Friends/unknown access. Document expiry behavior. — _Status: live pending (needs T00.6 + a normal session). Offline: gallery-dl filters to the single requested Story `pk` or raises NotFound; whether "public" can be proven from its metadata is unverified._
- [ ] **T01.6** Test X/Twitter single photo, multi-photo, video and a mixed-media post where available. Confirm actual media attachments only; no unrelated quoted posts or avatars. Validate both x.com and twitter.com URL paths. — _Status: live pending (needs T00.6). Offline: both hosts route to `twitter:tweet`; quoted/retweets/cards default off._
- [x] **T01.7** Evaluate candidate extractor compatibility, license, configuration behavior and dependency versions. Create the tested lock file. Avoid introducing unreviewed forks or unknown downloader endpoints. — _Done offline: licences read from package metadata (gallery-dl is GPL-2.0-only -> subprocess only), CLI flags verified against the installed `--help`, config isolation (`--config-ignore`, `--ignore-config`) verified, exact pins in `uv.lock` + hashed `requirements.lock`, guarded by `test_dependency_pins`. Re-verify live with the probe before each bump._
- [ ] **T01.8** Verify Telegram hosted API with a small synthetic photo, video, document, and mixed album. Check source caption placement and native photo compression using a test client. — _Update: `scripts/telegram_smoke.py` is written and tested against the fake server; it has **not** been run against real Telegram._
- [ ] **T01.9** If quality-preserving larger uploads are needed, test the official local Bot API profile with separate securely provided API credentials and a >50 MB synthetic video/document. Verify operation, network exposure, disk usage and migration procedure. Do not run hosted and local polling concurrently for the same token. — _Status: **owner-dependent** (needs Telegram API ID/hash). Not run._
- [ ] **T01.10** Produce `ops/feasibility.md` with the following matrix completed. If a requirement is blocked, stop claiming full release coverage and identify a permitted alternative, mitigation or explicit scope decision needed from the owner. Proceed with independent validated components only. — _Status: `ops/feasibility.md` exists with offline evidence and a probe that fills the matrix, but **every matrix row is still "Not tested"**, so this is not complete._

### Feasibility record (initially untested)

| Content | Candidate | Required verification | Result |
| --- | --- | --- | --- |
| TikTok video and short links | yt-dlp | Video + audio, correct target, quality | Not tested |
| TikTok photo-mode carousel | gallery-dl | Full ordered images, not thumbnails | Not tested |
| Instagram photo | gallery-dl | Highest available image | Not tested |
| Instagram video / Reel | yt-dlp | Playable source video + audio | Not tested |
| Instagram mixed carousel | gallery-dl, targeted video fallback | All media in source order | Not tested |
| Instagram photo Story | gallery-dl | Active, public, exact Story ID | Not tested |
| Instagram video Story | gallery-dl / yt-dlp | Active, public, exact Story ID | Not tested |
| X photos | gallery-dl | Single/multiple, order | Not tested |
| X video / mixed media | yt-dlp / gallery-dl | Correct attachments, audio, order | Not tested |
| Hosted Telegram delivery | Bot API | Native media, documents and source caption | Not tested |
| Local Telegram delivery | Local Bot API | Quality-preserving upload >50 MB | Not tested |

**Exit criteria:** Every core content type has a real authorized successful example or a documented blocker. No unproven assumption about TikTok photo mode or Stories enters the implementation as a completed feature. Establish which extractor and version to use for each type.

## 5. Phase 2 - Telegram interface and job infrastructure (P0)

**Goal:** A secure bot that accepts URLs and dispatches background jobs without blocking incoming updates.

- [ ] **T02.1** Implement aiogram startup, shutdown, long polling, structured logging, and error middleware in `app/main.py`. — _Status: implemented (`app/main.py`) with tested pieces (error boundary, structured redacted logging, recovery, tracking session, dispatcher wiring), but **not yet startable** (needs the Phase 3 extractor backend and Phase 4 sender) and **never run against real Telegram polling** (needs T00.4)._
- [x] **T02.2** Implement authorization middleware before any job or network request. Validate numeric sender ID, private chat and callback ownership. Deny unapproved users/groups. — _Done: outer middleware; tested on the real dispatcher (AT12): unauthorized/group/bot/foreign-chat messages and callbacks cause zero replies, zero jobs, zero extractor calls._
- [x] **T02.3** Implement `/start`, `/help`, `/settings`, URL-message handler, `/cancel` and readable error messages. Default to Media mode. Reject multiple URLs and unsupported message types gracefully. — _Done (fake-Telegram tests): commands, default Media mode, multiple URLs and non-text messages rejected politely (AT25)._
- [x] **T02.4** Implement `url_validator.py`: supported hosts and path patterns, HTTPS normalization, max length, unsafe host rejection, controlled redirect resolution, post-type detection and exact original URL preservation. Separate extraction URL from submitted URL. — _Done: syntactic validator + worker-side `ShortLinkResolver` (per-hop re-validation, <=5 hops, loop detection, pinned DNS, hostile targets never contacted). Tested on loopback fixtures; real platform redirects not yet exercised._
- [x] **T02.5** Implement Pydantic models and typed exceptions for `ExtractionRequest`, `PostManifest`, `MediaAsset`, `ExtractionResult` and `DeliveryOutcome`. — _Done._
- [x] **T02.6** Implement SQLite migrations for jobs, preferences, request idempotency and per-batch send outcomes. Add repository functions, parameterized queries, retention timestamps and transaction tests. — _Done: migration, WAL, parameterised SQL, TTL purge, rollback tests._
- [x] **T02.7** Build bounded `asyncio.Queue` with one active job per user, two global by default and queue size ten. Make reservation atomic; reject excess load with a clear response. Deduplicate repeated Telegram update events without suppressing an intentional repeat request. — _Done: atomic reservation, per-user/global/queue caps, duplicate events idempotent (AT22)._
- [x] **T02.8** Implement worker state machine, cancellation signaling and coarse status updates (`Fetching media...`, `Preparing...`, `Sending...`). Acknowledge valid requests promptly without waiting for downloads. — _Done with fakes: state machine, cancel at every stage, shutdown->INTERRUPTED, throttled status updates._
- [x] **T02.9** Implement per-job temporary directories with secure names/permissions. Validate workspace containment. Add cleanup interfaces and mock worker tests. — _Done: containment/symlink/traversal tests pass on Linux (5 of them skip on Windows)._

**Exit criteria:** Approved owner can send a URL and receive an acknowledgement from a stubbed job; unauthorized requests cause zero network extraction; queued, cancelled and failed jobs transition correctly; duplicate events do not produce duplicate jobs.

## 6. Phase 3 - Platform extraction adapters (P0)

**Goal:** A consistent, verified media manifest and ordered local source files for all supported target items.

- [x] **T03.1** Implement `PlatformAdapter` protocol, platform router and isolated extractor runner. Use safe argument arrays, no shell execution, explicit config, bounded process output, cancellation and process-group timeout handling. — _Done: `PlatformAdapter` protocol, router, `SubprocessRunner` (arg arrays, sanitized env, bounded output, process-TREE kill on timeout/cancel, byte/disk watcher, POSIX rlimits); tested with real processes on Windows and Linux._
- [x] **T03.2** Implement yt-dlp runner: extract format metadata, choose best available source tracks, retrieve only the target item(s), and report typed failures. Do not use generic extraction for arbitrary websites. — _Done and run against the REAL pinned yt-dlp (loopback): format ranking, targeted download, generic extractor refused, hostile config/URL inert. No platform contacted._
- [x] **T03.3** Implement gallery-dl runner: structured item records, target identity validation, ordered media metadata and local file mapping. Exclude unrelated profile images, audio-only tracks and thumbnails. — _Done and run against the REAL pinned gallery-dl (loopback): structured dump parsing, `NNN.ext` identity mapping, foreign files ignored. No platform contacted._
- [ ] **T03.4** Implement TikTok video adapter and safe short-share-link resolution (P01). — _Status: implemented; synthetic-fixture contract tests pass; **live pending (needs T00.6)**._
- [ ] **T03.5** Implement TikTok photo-mode adapter using the verified Phase 1 approach (P02). Return source photos, not a generated slideshow; preserve ordering and report missing items. — _Status: implemented (audio/covers excluded, source photos in order, missing items reported); synthetic tests pass; **live pending (needs T00.6, assumption A6)**._
- [ ] **T03.6** Implement Instagram photo, Reel and video support (P03-P04). — _Status: implemented; synthetic tests pass; **live pending (needs T00.6, A2/A4/A5)**._
- [ ] **T03.7** Implement Instagram mixed/photo carousel support (P05). Preserve source item IDs, count, order and type; reconcile fallback items only against the same post. — _Status: implemented (order, gaps/skip-warning => partial, >10 items); synthetic tests pass; **live pending (needs T00.6, A5/A7)**._
- [ ] **T03.8** Implement Instagram individual public photo and video Story extraction (P06-P07). Validate public/authorized access and exact Story ID; do not crawl other Stories, private content or Highlights. Mount any permitted session as a secret, fail closed for unknown visibility and distinguish auth-required from expired/restricted errors. — _Status: implemented fail-closed (public-account proof, exact id, audience check, session for Stories only); synthetic tests pass; **live pending (needs T00.6 + an owner session, A1/A3)**._
- [ ] **T03.9** Implement X single/multiple photos and video/mixed posts (P08-P09). Accept X/Twitter variants, return direct attachments only, and preserve source order. — _Status: implemented (own-attachments only, foreign/quoted media rejected, single-video yt-dlp retry); synthetic tests pass; **live pending (needs T00.6)**._
- [x] **T03.10** Implement typed, limited fallback orchestration for unsupported format or missing media, not for access restrictions, rate limits or challenges. Preserve the intended target. If any item cannot be verified, record an omission or unknown completeness. — _Done: fallback only for no_video/unsupported and only for a sole video; never after access/auth/rate/challenge errors; omissions and `unverified` recorded._
- [ ] **T03.11** Enforce maximum item count, byte/disk budget and extraction deadline during retrieval. Validate output type and paths; reject symlinks or workspace escape. Isolate source access from Telegram secrets and SQLite. — _Status: item/byte/disk/deadline enforcement and path/type validation are done and tested; **isolation from Telegram secrets and SQLite is only process-level (sanitized env) until the worker service (T05.1) exists**, so this stays open._
- [ ] **T03.12** Add contract tests per adapter against sanitized metadata and opt-in live tests for permission-cleared URLs. Update the feasibility matrix with actual test evidence. — _Status: 61 contract tests + real-binary tests done; opt-in live tests exist (`tests/live/test_live_corpus.py`) but **have never been run** (needs T00.6), so the feasibility matrix has no live evidence yet._

**Exit criteria:** Every validated P01-P09 adapter produces the same internal contract; all reference posts match expected count/order; public Story selection is exact; no fallback occurs on restricted access; blocked cases are explicitly recorded.

## 7. Phase 4 - Quality preservation and Telegram delivery (P0)

**Goal:** Media arrives in Telegram in the best available quality, with the original submitted URL.

- [x] **T04.1** Implement FFprobe-based validation for file type, dimensions, audio/video streams, codecs, duration and file size. Reject corrupted or unrelated assets. — _Done (ahead of Phase 4): ffprobe validation incl. rotation, audio-only/HTML/empty rejection, hash for photos, and a test proving a hostile playlist cannot make ffprobe touch the network._
- [x] **T04.2** Implement source quality selection, compatible audio/video stream selection and lossless FFmpeg merging/remuxing. Preserve aspect ratio and watermarks. Never silently upscale, downscale or re-encode. Ensure local-only processing. — _Done: format ranking/selection; the separate-track merge by yt-dlp's stream copy is verified on a real HLS master with a separate audio rendition (decoded frames/samples identical = no re-encode, audio kept); the only other transformation, MKV->MP4 remux, is proven bit-identical by packet hashes and is discarded if unverifiable; FFmpeg runs with `-protocol_whitelist file -nostdin` on generated local paths only._
- [x] **T04.3** Implement delivery planner for hosted/local Bot API size caps, native photo constraints, native video codec compatibility and document fallback. Return typed size errors when even original documents cannot fit. — _Done: pure planner covering hosted/local caps, native-photo limits (size, w+h<=10000, ratio<=20, type), native video codec/container/pixel-format policy, document fallback and typed FILE_TOO_LARGE rejections (49 tests)._
- [ ] **T04.4** Implement `sendVideo` and `sendPhoto` for single native media and `sendDocument` for original/incompatible files. Make videos playable where possible without intentional loss. — _Status: implemented and verified against a fake Bot API HTTP server (real aiogram serialisation, byte-exact multipart uploads). **Not yet run against real Telegram**, so 'videos are playable' is unverified (needs T00.4/T01.8)._
- [x] **T04.5** Implement ordered mixed photo/video albums, chunking 2-10 items; handle singletons separately. Split contiguous document/native runs without moving item positions. Support all-original document groups. — _Done at the planning + wire level: ordered mixed albums, 2-10 batches, lone remainder sent singly, contiguous document/native runs, document-only groups (planner tests + HTTP-level e2e). Real-client rendering is T06.6._
- [x] **T04.6** Implement captions with the exact submitted original URL. For albums, first item of each batch carries a caption and a final separate source-link message follows the last batch. Escape untrusted text or use plain text. For long URLs use an adjacent source message rather than truncation. — _Done: caption `Original link: <exact submitted URL>` on the first item of every batch, a closing source-link message for albums, no truncation (a too-long URL gets its own message); plain text only (no markup injection)._
- [x] **T04.7** Implement `Original files` inline callback and `/settings` persistent preference. Verify owner/chat/TTL, use opaque callback IDs and prevent duplicate callback events. Re-fetch public source on demand, report expiry or change, and never reuse a compressed native photo file_id as an original document. — _Done: `Original files` callback + `/settings` preference; owner/chat/TTL verified against the stored job, opaque 32-hex ids, duplicate callbacks idempotent, source re-fetched (never a Telegram file_id), expiry reported. Tested through the real dispatcher and end-to-end over HTTP._
- [x] **T04.8** Track confirmed Telegram message IDs and delivered item positions. Report partial or unverified source completeness; report unknown outcomes after ambiguous send timeouts. Do not blindly resend a potentially delivered batch. — _Done: every batch outcome (sent/failed/unknown + message ids) is persisted as it happens; an ambiguous timeout/5xx is `unknown`, never replayed, and halts further batches; a cancel mid-send is recorded as `unknown`._
- [x] **T04.9** Implement progress completion and failure/cancel responses, including partial delivery if some batches have already been sent. Clean files in finally blocks after delivery. — _Done: coarse progress, final/failed/cancelled/partial/unknown reports incl. already-delivered counts; workspace removed in `finally` on every path (tested for success, failure, cancel, shutdown)._
- [ ] **T04.10** Integrate optional local Bot API mode with private endpoint, securely supplied service credentials, migration guidance and configured limits. Test a larger file where needed; maintain hosted mode for small files. — _Status: the local profile is wired (`BOT_API_MODE=local`, `LOCAL_BOT_API_URL`, 1.9 GB cap, photo cap kept) and exercised against the fake server, but the **real telegram-bot-api container, API credentials and a >50 MB upload have not been tested** (needs T01.9); the compose profile is Phase 6._

**Exit criteria:** Single media and reference albums arrive in order with source URL; original photo delivery preserves bytes; video preparation does not re-encode; invalid/oversize cases have useful errors; partial and uncertain deliveries are not labeled fully successful. Check Android and iOS rendering.

## 8. Phase 5 - Security, reliability and operational hardening (P0/P1)

- [ ] **T05.1 (P0)** Isolate extractor worker under restricted identity/container, separate from bot token and database. Restrict shared workspace and secrets to necessary scope; no Docker socket or unnecessary host mounts. — _Status: the worker service (`app/extractors/service.py`) and client are implemented and tested over real HTTP (shared-secret auth, workspace/URL re-validation, no token or database in its code path). **Not yet closed:** the container isolation (internal network, non-root, read-only FS, no host mounts) must still be built and verified in Docker (Phase 6)._
- [ ] **T05.2 (P0)** Enforce outbound network policy for extractor and redirect clients: allowed platform/CDN destinations, no internal/private/link-local/metadata targets, DNS rebinding defense and safe redirect checks. Validate IPv4/IPv6 rules using controlled local fixtures. — _Status: the allowlisting egress proxy is implemented and tested with local fixtures (IPv4/IPv6 deny rules, all-records-public, pinned dial, rebinding, mutation-checked). **Not yet closed:** enforcement on the deployed worker network, and the CDN allowlist is unverified against live traffic (assumption A8)._
- [x] **T05.3 (P0)** Apply input length, URL path, callback, file path and output-type validation. Test shell flag injection, unexpected extractor config, unsafe filenames/symlinks and caption injection. — _Done: length/URL/callback/path/type validation; shell-flag and config injection, hostile file names, symlinks and caption injection are tested (hand-written cases + seeded fuzzing of the URL validator, callback parser, redactor and filename gate)._
- [ ] **T05.4 (P0)** Apply resource limits and timeouts to worker/subprocesses; cap active jobs, queue, number of items, temporary bytes, output/log size and retries. Check disk headroom during download/merge. — _Status: per-process limits (rlimits, timeouts, output caps, byte/disk watcher incl. the remux, item/queue/job caps) are done and tested; **container-level CPU/memory/pids limits arrive with compose (Phase 6)**._
- [x] **T05.5 (P0)** Implement bounded retry/backoff with jitter for safe transient failures; respect rate limits and `Retry-After`. No bypass or blind upload replay. Add typed error mapping and tests. — _Done: bounded exponential backoff with jitter (<=2 retries), `Retry-After` never undercut and not slept through when very long, no retry for permanent/unsafe errors, no blind upload replay, typed error mapping (tests)._
- [ ] **T05.6 (P0)** Implement graceful shutdown, startup recovery, interruption status, orphan workspace cleanup and periodic cleanup. Verify active jobs cannot be deleted accidentally. Clean local Bot API temporary/cache files according to its documented behavior. — _Status: graceful shutdown (running jobs -> INTERRUPTED + cleanup), startup recovery, orphan/periodic sweeps that never touch active jobs are done and verified end-to-end. **Open:** cleanup of the local Bot API server's own cache/temp files (needs the real server, T01.9)._
- [ ] **T05.7 (P0)** Implement 24-hour job metadata TTL, purge expired callback mappings and sensitive URLs, restrict SQLite volume, redact logs/subprocess output, exclude secrets from backups. Document Telegram's separate retention. — _Status: 24 h TTL purge, 0700/0600 data permissions, log/subprocess redaction are done and tested. **Open:** the backup-exclusion and Telegram-retention documentation (ops/deploy.md, Phase 6)._
- [ ] **T05.8 (P0)** Add process health checks, log rotation, restart policy, dependency pinning and basic monitoring for disk, polling and worker connectivity. — _Status: heartbeat + `python -m app.healthcheck` (polling freshness, DB writability, disk, worker reachability) and a polling watchdog are done and tested. **Open:** compose restart policy and log rotation (Phase 6)._
- [x] **T05.9 (P1)** Add owner-only `Retry` action for retryable failures with idempotency and a fresh request ID. Confirm original authorization and current source access again. — _Done (P1): owner-only Retry button for retryable failures / interrupted / unknown sends; fresh request id per press (duplicates idempotent), authorization and source access re-checked by running the whole pipeline again._
- [x] **T05.10 (P1)** Add optional operational alerts for repeated extractor failures or low disk space, without sending source URLs or media. — _Done (P1, opt-in `ALERTS_ENABLED`): once-per-episode alerts for repeated extractor failures and low disk; never contain URLs or media; undeliverable alerts never raise._

**Exit criteria:** Security tests pass; no unauthorized extraction or cross-chat delivery; no secrets in logs; failure, cancellation, timeouts and restarts remove temporary files; rate limits are honored; production worker isolation is actually enforced.

## 9. Phase 6 - Testing and deployment (P0)

### Automated tests

- [ ] **T06.1** Unit tests for authorization, URL parsing/host validation, platform routing, format ranking, album chunking, limits, captions, callbacks and typed errors.
- [ ] **T06.2** Contract tests for all P01-P09 content types using sanitized/synthetic fixtures and an ordered normalized manifest.
- [ ] **T06.3** Integration tests for the job queue, database states, mocked Telegram sends, partial/unknown outcomes, temporary cleanup, interrupted jobs, worker boundaries and retry limits.
- [ ] **T06.4** Security tests using controlled targets: unsupported domains, misleading domain suffix, private IP redirects, DNS rebinding, symlinks, malicious filenames, secret isolation and unauthorized callbacks.
- [ ] **T06.5** Run opted-in live tests: aim for at least 3 authorized successful examples per content type where available. Record exact extractor versions and failures. Test active Instagram Stories while available and explicit expired/restricted behavior without attempting bypass.
- [ ] **T06.6** Test original and media modes on Telegram Android/iOS. Compare source/delivered metadata and original photo hashes where feasible. Confirm no unexpected resolution or audio loss before Telegram's native processing.
- [ ] **T06.7** Run load/limit checks: two concurrent approved jobs if multiple test users configured, one active per user, ten-job queue cap, disk budget, cancel during download, large file and process restart.

### Deployment

- [ ] **T06.8** Create reproducible Dockerfile(s), Compose configuration, health checks, persistent metadata and bounded temporary volumes, worker isolation and optional local Bot API profile.
- [ ] **T06.9** Provision Linux VPS. Starting sizing estimate: 2 vCPU / 4 GB RAM / 40 GB SSD, adjust based on tested workload, file size and bandwidth. Set up SSH key access, updates, firewall and restricted service user. No public bot HTTP endpoint required for long polling.
- [ ] **T06.10** Supply bot token and allowlist securely. Supply normal authorized session and local API credentials only if required/permitted, to their intended services. Verify file permissions and secret isolation.
- [ ] **T06.11** Build and start services using Docker Compose. Confirm one polling instance, healthy worker, metadata volume and private local API networking where enabled.
- [ ] **T06.12** Run production smoke tests for each platform, including carousel, Story and original-file delivery. Verify source URL, quality, logging and cleanup. Record actual latency, size and success outcomes.
- [ ] **T06.13** Restart services and verify recovery, no duplicate sends, interruption records and temporary cleanup. Document rollback procedure to previous tested image/version and local API migration.
- [ ] **T06.14** Complete `README.md`, `ops/deploy.md`, `ops/troubleshooting.md` and deployment configuration examples. Include how to update extractors safely and diagnose auth/rate-limit/Story issues without leaking secrets.

**Exit criteria:** Automated suite passes; every supported type has live validation or an explicitly disclosed blocker; bot operates on VPS and recovers safely; production security and privacy checks pass; delivery is verified on both mobile clients.

## 10. Phase 7 - Client acceptance and handover (P0)

- [ ] **T07.1** Demonstrate TikTok single video with original link and original-file option.
- [ ] **T07.2** Demonstrate TikTok photo-mode carousel and Instagram mixed carousel, preserving all items and order.
- [ ] **T07.3** Demonstrate Instagram single photo, Reel, and individual active public photo/video Stories.
- [ ] **T07.4** Demonstrate X single/multi-photo and video/mixed posts.
- [ ] **T07.5** Demonstrate original-file delivery, oversize handling, cancellation, expired Story, restricted post rejection and partial-result reporting.
- [ ] **T07.6** Owner checks source links below delivered media and confirms default delivery preference.
- [ ] **T07.7** Deliver repository, tested dependency lock, deployment instructions, service configuration templates, support/limitations matrix and test report. Transfer secrets through a separate secure method only if needed; no secrets in deliverables.
- [ ] **T07.8** Obtain acceptance or document outstanding issues. Do not label the full scope complete while an agreed mandatory content type remains blocked.

## 11. Acceptance test matrix

| Test ID | Scenario | Expected result | PRD coverage |
| --- | --- | --- | --- |
| AT01 | Approved user sends TikTok short video URL | Correct video, audio if present, original submitted URL | P01, FR01-05, FR08 |
| AT02 | TikTok photo post with multiple slides | Actual source photos in correct order | P02, FR04-06, FR15 |
| AT03 | Instagram single photo | Native photo, source caption, original-file action | P03, FR05-08 |
| AT04 | Instagram Reel | Correct playable video or lossless document fallback | P04, FR05-08 |
| AT05 | Instagram mixed carousel | All attached photos/videos in order | P05, FR04-08, FR15 |
| AT06 | Active public photo Story | Exact specified Story photo, source URL | P06, FR09 |
| AT07 | Active public video Story | Exact specified Story video, source URL | P07, FR09 |
| AT08 | Expired Story, restricted Story, or unknown visibility | Clear error; no restricted extraction | FR09, FR12 |
| AT09 | X single and multi-photo | All direct attachments, source order and link | P08, FR04-08 |
| AT10 | X video/mixed-media post | Direct attachments only, ordered, source link | P09, FR04-08 |
| AT11 | Synthetic album with 11+ items | Groups of up to 10, singleton if needed, final source link | FR06, FR08 |
| AT12 | Unauthorized sender / group / callback | Rejected before extraction, zero media disclosure | FR01, FR17 |
| AT13 | Unsupported domain, spoofed host or unsafe redirect | Rejected; no internal network access | FR02-03 |
| AT14 | Photo delivered as original file | Downloaded source bytes preserved | FR07 |
| AT15 | Source video requires audio merge | No lossy re-encoding; original audio retained | FR05, FR07 |
| AT16 | Video incompatible with native Telegram player | Lossless remux or document; no silent transcoding | FR07, FR11 |
| AT17 | Oversized hosted upload / local profile | Local API if configured and within cap; otherwise explicit error | FR11 |
| AT18 | Item missing or mid-album send fails | Confirmed count and omission/uncertainty reported | FR12, FR15 |
| AT19 | User cancels active/queued job | Safe cancellation and cleanup, confirmed sends reported | FR10, FR13 |
| AT20 | Platform or Telegram rate limit | Respect limits, bounded retry or clear response | FR12, FR16 |
| AT21 | Service restarts during download/upload | Interrupted/unknown status; cleanup; no blind replay | FR13-14 |
| AT22 | Duplicate update / callback | Idempotent processing without cross-chat delivery | FR14, FR17 |
| AT23 | Original-file button used after source expiry | Clear expiry error; no promise of original retrieval | FR07, FR09, FR17 |
| AT24 | Expired metadata / failed downloads | URLs and temporary files removed per retention policy | FR13, privacy |
| AT25 | Multiple links in one message | Ask owner to send one link at a time | FR02 |
| AT26 | Android and iOS native view | Playable compatible media, readable clickable source URL | FR06, FR08 |

Record status (pass/fail/blocked/not run), evidence, dependency version, environment and notes for each. Do not claim these tests have been run simply because they are specified here.

## 12. Definition of done

The full release is done only when:

- All P0 tasks are checked and acceptance tests pass, with explicit owner-approved exceptions if the scope changes.
- TikTok, Instagram (including active public Stories), and X are demonstrated across the agreed photo/video/carousel types, using permitted access.
- Media quality, order and source link behavior meet the PRD; original files are supported; delivery limits are handled without silent degradation.
- Authorization, network isolation, secret handling, temporary cleanup and restart recovery are verified in production configuration.
- Source code, documentation, lock files, test evidence, support limitations, and reproducible deployment instructions are delivered.
- No unresolved mandatory feature is represented as shipped; no production secret or unauthorized media is committed.

## 13. Optional post-launch backlog (P2)

- [ ] Configurable authorized-user management with explicit owner approval.
- [ ] Opt-in quality-reduced previews when original files exceed upload or compatibility limits (requires explicit user consent).
- [ ] Reuse confirmed Telegram document file IDs within the retention policy to avoid repeated retrieval.
- [ ] Local operational dashboard only if a real multi-user need emerges.
- [ ] Additional platform or content-type support after a separate PRD/feasibility review.

Do not add these items to the launch critical path. They are not part of the agreed initial client request.
