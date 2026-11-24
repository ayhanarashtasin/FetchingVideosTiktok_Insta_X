# Product Requirements Document (PRD)

# Telegram Social Media Downloader Bot

| Field | Value |
| --- | --- |
| Version | 1.0 |
| Date | 2026-11-24 |
| Status | Proposed implementation specification |
| Product type | Private, self-hosted Telegram bot |
| Audience | One owner initially; optional explicitly approved users |
| Related documents | [Architecture](architecture.md), [Implementation tasks](tasks.md) |

## 1. Product overview

Build a private Telegram bot that receives a supported social media post URL, retrieves the highest-quality media available through permitted access, and returns actual photos or playable videos directly in Telegram with the original submitted URL below the media. The owner should not need a separate downloader website, a browser workflow, or manual file transfers.

The bot is for personal use with content the owner is authorized to download. It is not a public scraping service, an account monitoring tool, or a tool to obtain private or restricted content.

## 2. Problem and goals

Saving a video, photo, or multi-item post normally requires switching apps, locating a downloader, downloading files separately, and retaining the source link manually. This bot removes those steps.

### Goals

- One-step experience: paste a link in a Telegram private chat and receive the media.
- Support TikTok, Instagram, and Twitter/X, including Instagram Stories.
- Preserve the best obtainable source quality, original aspect ratio, audio where present in a video, and carousel order.
- Keep the submitted original URL attached to the delivered media for future reference.
- Run continuously on a small VPS with no required paid downloader API if supported extraction works.
- Provide clear, actionable errors rather than silent failures, wrong media, or unannounced quality loss.

### Non-goals

- Public registration, payments, web dashboard, mobile app, or public groups.
- Private/protected posts, Close Friends stories, deleted or expired stories, paywalled media, or bypassing access controls, challenges, or download restrictions.
- Profile-wide downloads, bulk archiving, timelines, automatic story monitoring, livestream recording, or highlights (unless added later).
- Watermark removal, AI upscaling, video editing, audio-only downloads, or converting photo slideshows into videos. Existing watermarks are acceptable and should not be altered.
- Permanent storage or redistribution of downloaded media by the bot service.

## 3. Supported content

All rows below are launch requirements, conditional on successful permitted access. A platform listing in an extractor's documentation does not by itself establish reliable support. Validate each row with a live, authorized test URL before claiming support.

| ID | Platform | Content | Expected response |
| --- | --- | --- | --- |
| P01 | TikTok | Single video, including short share links | Playable video and original link |
| P02 | TikTok | Photo-mode post / slideshow | Every obtainable source photo in order and original link; no synthetic slideshow |
| P03 | Instagram | Single photo post | Photo and original link |
| P04 | Instagram | Reels and video posts | Playable video and original link |
| P05 | Instagram | Photo or mixed photo/video carousel | All target post items, ordered, with original link |
| P06 | Instagram | Active, publicly shared photo Story | The specified Story photo and original link |
| P07 | Instagram | Active, publicly shared video Story | The specified Story video and original link |
| P08 | X / Twitter | Single photo or multi-photo post | All attached photos in order and original link |
| P09 | X / Twitter | Video or mixed-media post (where available) | All directly attached supported items and original link |

### Example accepted URL shapes

```text
https://vt.tiktok.com/<share-code>/
https://vm.tiktok.com/<share-code>/
https://www.tiktok.com/@<user>/video/<id>
https://www.tiktok.com/@<user>/photo/<id>
https://www.instagram.com/p/<shortcode>/
https://www.instagram.com/reel/<shortcode>/
https://www.instagram.com/stories/<user>/<story-id>/
https://x.com/<user>/status/<id>
https://twitter.com/<user>/status/<id>
```

Also accept valid mobile host variants explicitly allowed in configuration. Normalize tracking query parameters for extraction but retain the exact submitted URL for attribution. Story URLs must identify an individual Story item. If a shared Instagram URL cannot resolve to an exact supported post or Story, request a direct item link. X quote posts should return only the media directly attached to the linked post, not media from unrelated or quoted posts.

## 4. User experience

### Primary flow

1. Owner opens the bot and pastes a supported URL (no command required).
2. Bot validates the link, confirms authorization, and sends a brief status message: `Fetching media...`
3. Bot detects the platform and content type, retrieves all in-scope items, and selects the best available source representations.
4. Bot sends the actual media in the same private chat, replying to the originating message where supported.
5. The media caption includes `Original link: <exact submitted URL>`. Albums also have a final source-link message immediately below the last batch.
6. The owner can optionally request original files using an inline action without having to paste the URL again.
7. Bot cleans temporary files and finalizes the status message (or removes it on success).

### Example replies

Single video:

```text
[Playable video attachment]
Original link: https://vt.tiktok.com/<share-code>/
[Original files]
```

Carousel:

```text
[Photo 1] [Photo 2] [Video 3] ...
Original link: https://www.instagram.com/p/<shortcode>/
[Original files]
```

The final album summary is a separate message because Telegram's media-group API does not attach inline keyboards directly. Include a caption on the first item of each batch; the final summary ensures the link is below the delivered album. Single-media captions appear beneath the media by default.

### Commands and controls

| Control | Behavior | Priority |
| --- | --- | --- |
| `/start` | Show purpose, supported platforms, example links, and usage notice | Must |
| `/help` | Explain URLs, Stories, quality modes, and common errors | Must |
| Paste a URL | Start a download using the current delivery preference | Must |
| `Original files` | Send source files as documents, avoiding Telegram photo compression | Must |
| `/settings` | Choose `Media` (default) or `Original files` for future jobs | Must |
| `/cancel` | Cancel an active or queued download where possible; report if already sent | Must |
| `Retry` | Retry a retryable failed job without repasting the URL | Should |

The `Original files` button is shown in Media mode for successful results within a configurable 24-hour job-metadata window. It may need to fetch the source again, because files are not retained after delivery. If the Story has expired or the source changed, report this clearly; do not promise exact re-delivery. The owner can select Original files mode before downloading a time-sensitive Story. No need to send duplicate attachments by default.

## 5. Functional requirements

| ID | Priority | Requirement / acceptance condition |
| --- | --- | --- |
| FR-01 | Must | Restrict access to configured Telegram numeric user IDs in private chats. Reject others before any extraction or network fetch. |
| FR-02 | Must | Accept one supported HTTP(S) item URL per message, including URLs embedded in text. Ask the user to send separately if multiple are present. |
| FR-03 | Must | Resolve approved short-link redirects safely, detect the platform, and reject unsupported hosts, paths and unsafe destinations. |
| FR-04 | Must | Implement every content type P01-P09 and return all available attached items in original order. Do not silently include thumbnails, profile photos, or unrelated posts. |
| FR-05 | Must | Retrieve highest-quality source representations reasonably available to the permitted session. Keep video audio and original aspect ratio. Do not upscale, strip watermarks, or silently downscale. |
| FR-06 | Must | Send single photos/videos natively when compatible. Send mixed photo/video groups in ordered batches of 2-10; send a lone remaining item individually. |
| FR-07 | Must | Support original-file delivery using Telegram documents. For this mode, preserve downloaded photo bytes and video source quality; merging separate streams or remuxing may change container bytes without re-encoding. |
| FR-08 | Must | Attach the exact submitted original URL, not a temporary CDN URL or normalized extraction URL, to each single result and album batch. Provide a final link message below albums. |
| FR-09 | Must | Support currently accessible individual public Instagram Stories; an ordinary authorized session may be required. Reject restricted, unavailable or expired Stories without bypassing controls. |
| FR-10 | Must | Send a prompt acknowledgement, provide useful coarse progress, and honor cancel requests. |
| FR-11 | Must | Handle file-size and media-compatibility limits with original-file fallback when feasible. Report any delivery that cannot be completed without losing quality. |
| FR-12 | Must | Detect unavailable content, access restrictions, extractor failures, rate limits, timeouts, disk limits, and partial results; communicate what actually happened. |
| FR-13 | Must | Remove temporary files on completion, failure, cancellation, and startup recovery. |
| FR-14 | Must | Support multiple sequential requests without cross-chat delivery or filename collisions; one active job per user by default. |
| FR-15 | Must | Preserve source order and report the count of delivered vs expected items when known; mark unverified completeness or partial delivery explicitly. |
| FR-16 | Should | Retry only safe transient failures with bounded exponential backoff and respect platform retry instructions. |
| FR-17 | Must | Provide a configurable delivery preference and bounded Original files callback, scoped to the owner and chat. |

## 6. Quality and delivery policy

`Highest quality` means the best representation the platform exposes through permitted access, not necessarily the creator's original camera file. A platform may publish only compressed or watermarked versions. Do not represent a 720p source as 1080p or claim a watermark-free result.

| Situation | Required behavior |
| --- | --- |
| Video has separate audio/video streams | Select best compatible source streams and losslessly merge/remux where possible. |
| Video is already suitable for Telegram playback | Upload natively as a video, with no intentional quality reduction. |
| Video is not natively playable without re-encoding | Try a compatible lossless remux; otherwise send as a document and explain why. |
| Photo is sent as a native Telegram photo | Preview may be compressed/resized by Telegram; do not label it bit-identical. Offer Original files. |
| Original files mode | Send as documents; do not intentionally resize or re-encode. Photos preserve downloaded bytes; merged/remuxed video preserves encoded stream quality. |
| File exceeds upload limit | Use configured local Bot API if within its limits; otherwise return a clear size error. Never silently reduce resolution. |
| Carousel contains an unsupported item | Keep the order of deliverable items, identify omissions, and label the result partial. Never report full success if incomplete. |

The official hosted Telegram Bot API documents 10 MB for multipart photo uploads and 50 MB for general files/videos. A self-hosted local Bot API server supports uploads up to 2000 MB. Native photo constraints and codec compatibility still apply. Set safe application thresholds below service ceilings and verify during deployment. Media mode should optimize viewing convenience; Original files mode should optimize fidelity. A quality-reduced preview option can be considered later only with explicit consent.

## 7. Security, rights and privacy

- Permit only explicitly authorized Telegram users, private chats, and supported post URL patterns.
- Use public content for which the owner has permission or a lawful basis to download. Respect platform terms, access restrictions, and reasonable request limits. Do not assume public visibility automatically permits copying.
- If a normal authenticated session is necessary for a publicly shared Story, use only an owner-authorized session with permitted access. Never request passwords, cookies, or tokens through Telegram; do not implement credential harvesting or access-control bypass.
- Keep bot tokens and session files out of Git, messages, logs and unencrypted backups. Restrict access to the service account.
- Do not fetch arbitrary user-provided hosts or follow redirects to internal addresses.
- Delete temporary media promptly. Default metadata retention: 24 hours for retry and original-file controls; store only required operational data.
- Do not log full URLs, query strings, Story content, credentials, or download payloads. The original URL is stored only in restricted job metadata where required.
- Telegram receives the uploaded media and URL; deleting local files does not delete messages already delivered to Telegram.

## 8. Non-functional requirements

These are proposed engineering targets, not guaranteed performance figures.

| Category | Launch target |
| --- | --- |
| Availability | Runs continuously on a VPS, auto-restarts on failure; target 99% bot process availability excluding upstream outages and planned maintenance. |
| Responsiveness | Acknowledge a valid new request within 2 seconds in normal conditions. |
| Download speed | Target completion within 30 seconds for typical files up to 20 MB and accessible posts on the chosen VPS; measure rather than promise. |
| Concurrency | One active job per owner, two globally, bounded queue (default 10). |
| Limits | 120-second extraction timeout, configurable upload timeout (default 600 seconds), max 30 attached items and 4 GB temporary budget per job; explicitly reject over-budget jobs. |
| Reliability | No false full-success status, no cross-chat delivery, and no unbounded retries. |
| Portability | Containerized deployment, Python service, no mandatory paid API key for extractors. |
| Maintainability | Separate platform adapters, tested extraction versions, structured errors and dependency updates. |

## 9. Error messages

| Case | User-facing response example |
| --- | --- |
| Invalid link | `Please send a TikTok, Instagram, or X post link.` |
| Unsupported item | `This link is not a supported individual post or Story.` |
| Unavailable/expired | `This post or Story is unavailable or may have expired.` |
| Restricted access | `This content is private or restricted. I can only retrieve permitted public media.` |
| Public Story requires session | `This public Story requires an authorized Instagram session. The bot owner needs to configure it.` |
| Rate limit | `The platform is limiting requests. Please try again later.` |
| Over upload limit | `The source file exceeds the configured Telegram upload limit. No quality was reduced.` |
| Partial album | `Delivered 4 of 5 items. Item 3 could not be retrieved. Original link: ...` |
| Unknown count | `Delivered 4 items. I could not verify whether the source contains additional media.` |
| Extractor failure | `The platform could not be processed right now. Please try again later.` |

## 10. Release acceptance criteria

- Owner can paste valid links covering P01-P09 and receive the expected media and source URL.
- Use at least 3 successful authorized live examples per content type where available, plus recorded sanitized fixtures for repeatable tests. Test Story photo/video while active and public, and record when authentication is required.
- On sample carousels, item count and sequence match the visible source post, including mixed formats and a carousel exceeding 10 items (real or fixture).
- On sample files, verify resolution, aspect ratio and audio; compare downloaded and delivered original-photo hashes where feasible and confirm no video re-encoding in original mode.
- Test media mode and original-file mode on Android and iOS Telegram clients; original URL is clickable and shown below media or the final album batch.
- Unauthorized users, private/expired content, invalid links, unsafe redirects, oversized files, partial failures, cancellation and restart recovery pass expected behavior tests.
- VPS deployment survives a process restart and removes orphaned temporary files; logs contain no credentials or complete URLs.
- Every content type passes its feasibility gate, or is explicitly disclosed as blocked and kept out of any claim of full release support. No silent scope reductions.

## 11. Success metrics and trade-offs

- 100% of delivered test results contain the correct original link.
- 100% ordered delivery on successful reference carousels.
- At least 95% success across a baseline of accessible, permission-cleared, supported live test cases in normal conditions; track upstream restrictions and network failures separately instead of hiding them.
- Track completion time, result type, size, platform failure categories and temporary storage usage without logging content.
- No required paid extractor API in the first implementation; hosting, bandwidth and maintenance remain operational costs.

## 12. Implementation assumptions and risks

- Initial owner is the only approved user; additional users must be explicitly configured.
- Media mode is the default; original-document delivery is available as an option. Watermarks are acceptable.
- Platform extractors are volatile. TikTok photo mode and Instagram Stories are early feasibility risks, not features to postpone until the end.
- A local Bot API service is recommended for the full-quality release if files exceed the hosted limits; document hosted mode as the simpler development profile.
- Public Story support is conditional on valid access and availability. An expired Story cannot reliably be retrieved again.
- No deadline or budget was supplied; estimates in the task plan are planning assumptions only.

## 13. References

- Telegram Bot API: https://core.telegram.org/bots/api (file limits, send methods, media groups and local API mode)
- yt-dlp supported sites: https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md
- gallery-dl supported sites: https://github.com/mikf/gallery-dl/blob/master/docs/supportedsites.md

Technical references checked on 2026-11-24. Actual extractor coverage must be validated with live authorized examples.
