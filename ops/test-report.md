# Test report

Status vocabulary (tasks.md section 11): **pass / fail / blocked / not run**, with evidence, versions and environment.

> **Read this first.** Every "pass" below is **offline**: synthetic fixtures, a fake Telegram server, loopback media and
> real FFmpeg/yt-dlp/gallery-dl binaries driven against local servers. **No real TikTok, Instagram, X or Telegram
> endpoint was contacted, and no Android/iOS client was used.** "Live" and "device" columns are therefore *not run*
> until the owner runs the steps in README.md ("Verifying it for real"). An offline pass proves the logic and the safety
> rules; it does **not** prove that today's platforms still serve what the adapters expect (assumptions A1-A9 in
> `ops/feasibility.md`).

## Environment and versions

| Item | Value |
| --- | --- |
| Dev host | Windows 11, Python 3.12.15 (uv), FFmpeg 9.0.2 (gyan full build) |
| Linux runs | Python 3.12.16, FFmpeg 5.1.9 (Debian 12) — historical offline POSIX suite run (`ops/dev-log.md`); native Linux VPS run is still **not run** |
| Target runtimes | Native Windows 10/11 (`start-bot.bat` / PowerShell) and native Linux VPS (Ubuntu/Debian + `systemd` via `ops/setup-ubuntu.sh`); no Docker required |
| Libraries | aiogram 3.34.0, yt-dlp 2026.11.23, gallery-dl 1.32.21, pydantic 2.14.4, pydantic-settings 2.16.3, aiohttp 3.14.5, pytest 9.2.0, pytest-asyncio 1.5.0, ruff 0.17.6 |
| Date | 2026-11-25 (offline suite baseline from 2026-11-24; Docker setup removed 2026-11-25) |

## Suite results

| Run | Result |
| --- | --- |
| Full offline suite, Windows (`uv run pytest`) | **937 passed, 12 skipped** (POSIX/symlink limits; baseline recorded 2026-11-24 in `ops/dev-log.md`) |
| Full offline suite, Linux POSIX checks (`ops/dev-log.md`) | **941 passed, 3 skipped** (POSIX symlink, rlimit, and process-group tests verified) |
| `uv run ruff check app tests scripts` and `ruff format --check` | clean |
| `python scripts/scan_secrets.py --all` (pre-commit hook runs the staged variant) | clean |
| Mutation checks of security-critical tests | egress policy (5 mutants), cancellation propagation (1 mutant), cross-uid cleanup (control run) — all caught |

Skips on Windows are environmental (POSIX symlink and `resource` rlimit tests); on Linux, skips cover host-specific git/FFmpeg-version checks.

## Acceptance matrix AT01-AT26

"Offline" = automated, no network. Test names are real and checked by `tests/unit/test_report_references.py`.
Every row's **live / device status is _not run_** unless stated.

| ID | Scenario | Offline | Evidence (tests) | Live / device |
| --- | --- | --- | --- | --- |
| AT01 | TikTok short video URL | pass | `test_p01_tiktok_video_picks_the_highest_resolution_source`; `test_a_single_item_carries_the_caption_and_the_button_itself`; `test_original_files_button_refetches_and_sends_the_same_items_as_documents` | not run |
| AT02 | TikTok photo post, several slides | pass | `test_p02_tiktok_photo_post_returns_the_source_photos_in_order_without_the_audio_track`; `test_p02_a_single_tiktok_photo_is_a_photo_not_a_carousel`; `test_p02_post_declaring_more_images_than_listed_is_partial_with_the_missing_position` | not run |
| AT03 | Instagram single photo | pass | `test_p03_instagram_single_photo`; `test_a_rejected_native_photo_is_resent_once_as_a_document_losslessly` | not run |
| AT04 | Instagram Reel | pass | `test_p04_instagram_reel_is_merged_from_the_best_video_and_audio_tracks`; `test_p04_instagram_video_post_via_gallerydl`; `test_reel_falls_back_to_gallerydl_when_ytdlp_cannot_handle_it`; `test_p04_a_reel_that_resolves_to_a_photo_is_a_preview_stand_in_and_is_refused`; `test_incompatible_video_goes_as_a_document_not_transcoded` | not run |
| AT05 | Instagram mixed carousel | pass | `test_p05_mixed_carousel_keeps_every_item_in_source_order_and_type`; `test_p05_a_carousel_beyond_ten_items_is_preserved_whole`; `test_a_carousel_travels_from_a_telegram_message_to_byte_exact_uploads`; `test_a_full_carousel_round_trip_through_the_worker_equals_the_in_process_result` | not run |
| AT06 | Active public photo Story | pass | `test_p06_public_photo_story_exact_id`; `test_story_visibility_is_checked_before_the_story_itself_is_requested`; `test_story_selection_is_exact_when_the_whole_reel_comes_back`; `test_session_cookies_are_used_for_stories_only` | not run (needs an owner-authorised session; assumption A-Stories) |
| AT07 | Active public video Story | pass | `test_p07_public_video_story`; `test_ytdlp_works_with_a_read_only_session_file_because_it_only_sees_a_private_copy`; `test_gallerydl_with_a_session_file_never_modifies_the_original` | not run (same) |
| AT08 | Expired / restricted / unknown-visibility Story | pass | `test_expired_story_is_reported_as_unavailable`; `test_story_of_a_private_or_unverifiable_account_is_refused_and_never_requested`; `test_close_friends_story_is_never_delivered`; `test_story_with_a_different_id_is_treated_as_expired_or_unavailable`; `test_story_without_a_configured_session_asks_the_owner_to_configure_one`; `test_only_proven_public_content_is_fetched_fail_closed` | not run |
| AT09 | X single and multi-photo | pass | `test_p08_x_multi_photo_in_source_order`; `test_x_uses_no_session_cookies`; `test_x_text_only_post_has_no_media` | not run |
| AT10 | X video / mixed media | pass | `test_p09_x_mixed_media_keeps_positions`; `test_p09_x_video_and_gif_are_videos`; `test_x_media_belonging_to_a_quoted_post_is_rejected`; `test_x_retweet_media_is_rejected`; `test_x_single_video_gets_one_identity_checked_ytdlp_retry` | not run |
| AT11 | Synthetic album with 11+ items | pass | `test_eleven_photos_are_a_group_of_ten_then_a_single`; `test_batch_sizes_are_two_to_ten_with_a_lone_remainder_sent_alone`; `test_every_batch_of_a_long_album_has_a_caption_and_one_link_message_closes_it`; `test_albums_get_a_separate_final_link_message_carrying_the_button`; `test_a_flood_wait_is_waited_out_and_the_album_is_not_duplicated` | not run (real Telegram album rendering) |
| AT12 | Unauthorized sender / group / callback | pass | `test_strangers_and_groups_get_nothing_and_trigger_no_extraction`; `test_unauthorized_messages_cause_zero_work`; `test_unauthorized_callback_is_dropped_before_any_lookup`; `test_a_different_user_cannot_use_someone_elses_button`; `test_button_pressed_from_another_chat_is_refused`; `test_callback_parser_accepts_only_prefix_plus_32_lowercase_hex` | not run |
| AT13 | Unsupported domain, spoofed host, unsafe redirect | pass | `test_host_confusion_attempts_are_rejected`; `test_validator_fuzz_mutated_valid_urls_keep_the_invariants`; `test_host_matching_is_not_substring`; `test_hostile_redirect_targets_are_rejected_without_being_requested`; `test_dns_rebinding_between_resolutions_cannot_redirect_the_connection`; `test_a_short_link_host_resolving_to_a_private_address_is_never_connected_to`; `test_redirect_hop_limit_is_enforced`; egress proxy policy in `tests/integration/test_egress_proxy.py` (403 for unlisted / IP-literal / loopback / metadata / non-443) | not run (live short-link behaviour) |
| AT14 | Photo delivered as original file | pass | `test_original_mode_sends_everything_as_documents_unmodified`; `test_original_mode_albums_are_document_groups`; `test_hosted_run_proves_original_bytes_are_preserved_and_records_t01_8` (script logic) | not run (real Telegram byte round-trip = T01.8) |
| AT15 | Source video needs audio merge | pass | `test_separate_video_and_audio_tracks_are_merged_losslessly_and_audio_is_kept` (real FFmpeg + yt-dlp, decoded-hash proof); `test_the_merged_result_is_then_remuxed_to_a_native_mp4_without_re_encoding`; `test_original_mode_keeps_the_merged_file_exactly_as_downloaded` | not run (needs a real DASH-style source) |
| AT16 | Video incompatible with native player | pass | `test_mkv_h264_aac_is_remuxed_to_mp4_without_touching_the_streams` (packet-hash proof); `test_remux_that_changes_the_streams_is_discarded`; `test_a_failed_or_unverifiable_remux_keeps_the_original`; `test_hevc_is_native_only_when_enabled_and_never_re_encoded`; `test_prepare_remuxes_only_what_it_can_do_losslessly` (Media auto-transcode fallback); `test_remux_is_abandoned_and_the_original_kept_when_the_disk_headroom_is_gone` | not run (playback on devices) |
| AT17 | Oversized hosted upload / local profile | pass (planner, sender, fake server 413) | `test_oversized_files_are_rejected_not_reduced_on_the_hosted_profile`; `test_the_local_profile_accepts_what_hosted_rejects`; `test_the_local_profile_lifts_only_the_file_cap_not_the_photo_rules`; `test_local_profile_accepting_the_large_file_is_the_expected_outcome` | **not run** - real hosted refusal (T01.8) and real local Bot API server (T01.9) need the owner |
| AT18 | Item missing / send fails mid-album | pass | `test_missing_item_is_partial_never_complete`; `test_partial_delivery_failure_reports_which_items`; `test_a_missing_downloaded_file_is_an_explicit_omission_never_silent`; `test_ambiguous_send_is_delivery_unknown_and_offers_retry_but_never_auto_replays`; `test_partial_album_message_matches_prd_example` | not run |
| AT19 | User cancels active / queued job | pass | `test_cancel_stops_the_job_cleans_up_and_sends_no_media`; `test_cancel_during_extraction_stops_it_and_cleans_up`; `test_cancel_during_upload_reports_what_was_already_confirmed`; `test_cancel_a_queued_job_marks_it_cancelled_and_frees_the_slot`; `test_when_the_bot_hangs_up_the_workers_job_is_cancelled_so_the_tool_tree_gets_killed` | not run |
| AT20 | Platform / Telegram rate limit | pass | `test_a_long_platform_rate_limit_is_reported_not_slept_through`; `test_a_flood_wait_is_waited_out_and_the_album_is_not_duplicated`; `test_flood_wait_on_send_is_retried_once_after_the_requested_delay`; `test_no_fallback_extractor_is_tried_after_access_rate_or_challenge_errors` | not run |
| AT21 | Restart during download / upload | pass (in-process, real startup/shutdown path) | `test_shutdown_interrupts_a_running_job_cleans_up_and_closes_cleanly`; `test_startup_recovery_marks_dead_jobs_removes_orphans_and_notifies`; `test_recovery_is_idempotent`; `test_an_ambiguous_server_error_is_reported_as_unknown_and_never_replayed` | **not run** - `systemd` service restart of a real bot mid-job on a VPS (T06.13) |
| AT22 | Duplicate update / callback | pass | `test_duplicate_telegram_update_makes_one_job_and_one_ack`; `test_duplicate_event_does_not_create_a_second_job`; `test_concurrent_submissions_for_one_user_accept_exactly_one`; `test_duplicate_callback_event_does_not_start_a_second_job` | not run |
| AT23 | Original-files button after expiry | pass | `test_expired_metadata_blocks_the_button_without_promising_retrieval`; `test_unknown_job_id_is_reported_as_expired` | not run |
| AT24 | Expired metadata / failed downloads cleaned per policy | pass | `test_ttl_purge_removes_expired_urls_and_deliveries`; `test_purge_never_deletes_an_active_job`; `test_recovery_interrupts_dead_jobs_removes_orphans_and_purges_expired`; `test_startup_sweep_removes_every_job_shaped_directory_and_nothing_else`; `test_release_removes_files_and_is_idempotent` | not run on the VPS |
| AT25 | Multiple links in one message | pass | `test_multiple_links_are_refused_politely`; `test_multiple_links_ask_for_one_at_a_time`; `test_supported_plus_unrelated_url_is_still_multiple` | not run |
| AT26 | Android and iOS native view | **not run** | Only the inputs to it are tested offline: plain-text captions with no `parse_mode` (`test_no_request_to_telegram_ever_carries_a_parse_mode`) and the exact `Original link: <url>` line | **not run - needs a mobile client** (playback, rendering, clickable links) |

## Load and limit checks (T06.7)

| Sub-item | Offline evidence |
| --- | --- |
| Two concurrent jobs | `test_global_concurrency_cap`; `test_concurrency_is_bounded` (worker) |
| One active job per user | `test_accepts_then_rejects_second_job_for_same_user`; `test_reserve_accepts_then_dedupes_then_limits_per_user`; `test_second_request_while_busy_is_refused` |
| Queue cap (10) | `test_queue_size_is_bounded`; `test_queue_full_is_reported` |
| Disk / byte budget | `test_directory_byte_budget_kills_the_process`; `test_low_disk_headroom_kills_the_process`; `test_temp_byte_budget_is_enforced`; `test_gallerydl_budget_kills_runaway_downloads`; `test_ytdlp_enforces_the_per_file_size_limit` |
| Cancel during download | `test_cancel_during_extraction_stops_it_and_cleans_up`; `test_when_the_bot_hangs_up_the_workers_job_is_cancelled_so_the_tool_tree_gets_killed` |
| Large file | planner/sender/fake-server rules above; real uploads are T01.8 / T01.9 (owner) |
| Process restart | `test_startup_recovery_marks_dead_jobs_removes_orphans_and_notifies`; `test_shutdown_interrupts_a_running_job_cleans_up_and_closes_cleanly` |

These are automated limit checks with a stubbed extractor and fake Telegram, not a throughput benchmark on real
platforms; sizing for the VPS must still be measured (`ops/deploy.md`).

## What the offline checks prove, and what they do not

Proven offline in native Python:

* Single-process `app.main` (`WORKER_MODE=inprocess`) startup, config guards (`APP_ENV=production`), SQLite WAL migrations,
  heartbeat health check (`python -m app.healthcheck`), graceful shutdown, orphan workspace sweeps, and 24-hour TTL purge.
* Optional pure-Python remote worker (`app.extractors.service`) and allowlisting egress proxy (`app.services.egress_proxy`)
  on loopback: shared-secret auth (constant-time comparison, 401 on missing/bad secret), workspace containment, cancellation
  propagation to subprocess trees, and 403 refusal for unlisted hosts, IP literals, loopback, cloud-metadata, and non-443 ports.
* Platform adapters, lossless FFmpeg merge/remux + Media-mode H.264/AAC auto-transcode fallback, delivery planning, redaction, and pre-commit secret scanning.

Note: The earlier Docker/Compose stack was removed on 2026-11-25 in favor of native Windows and Linux VPS (`systemd`)
operation (see `ops/dev-log.md`).

Not proven: behaviour on a live Linux VPS (`systemd` unit hardening, firewall, host file permissions), the CDN allowlist
against live traffic, real Telegram delivery, the local Bot API server, the Instagram session flow, mobile clients.

## Blockers and open items

* **Owner-only live acceptance items:** All live rows of the feasibility matrix (T01.1-T01.6, T01.10), real Telegram token setup (T00.4),
  permission-cleared live corpus (T00.6), Telegram smoke tests (T01.8, T01.9), mobile checks on physical iOS/Android (T06.6), live corpus test suite
  (T06.5), VPS provisioning, production secrets, `ops/setup-ubuntu.sh` + `systemd` startup, smoke and restart recovery (T06.9-T06.13), and client acceptance
  demonstration (Phase 7 / T07.1-T07.8) are **owner-only live acceptance items** and have not been run. All code, configuration,
  and offline verification for them are complete.
* Assumptions A1-A9 (`ops/feasibility.md`) are unverified against live endpoints; in particular the egress allowlist (A8) is a best guess of the
  platforms' CDN domains.
* Cleanup of the optional local `telegram-bot-api` server's own internal cache (T05.6 / T01.9) requires live inspection after real large uploads on the VPS.
