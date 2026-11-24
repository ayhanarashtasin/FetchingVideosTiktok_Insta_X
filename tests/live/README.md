# Live tests (opt-in)

Nothing in this folder runs by default (`addopts` excludes the `live` marker). These are the only
tests that touch real platforms or real Telegram, and they exist so that **you** can turn every
"Not tested" row of `ops/feasibility.md` into evidence with one command each.

## 1. The private corpus (T00.6)

1. `cp tests/live/corpus.example.txt tests/live/corpus.txt` (the real file is git-ignored).
2. Add permission-cleared URLs only: your own posts, licensed material, or explicit permission.
   Aim for at least **3 per content type** (PRD section 10). Never private/restricted content.
3. For Stories (P06/P07) collect the URLs **shortly before** testing; they expire after 24 h.
4. Optionally add `count=N` / `kinds=photo,video,...` from what you can see in the app so the probe
   can verify completeness and order.

## 2. Feasibility probe (T01.1 - T01.7, T01.10)

```bash
uv run python scripts/feasibility_probe.py live --download
# Instagram Stories may need a normal logged-in session. Export YOUR OWN cookies in Netscape
# format to secrets/instagram_cookies.txt (git-ignored) and add:  --cookies secrets/instagram_cookies.txt
# Re-run only some content types:  --only P02,P05
```

It stops at (and records) any CAPTCHA/challenge/auth/rate-limit - it never retries around them.
Downloaded files are ffprobe'd and then **deleted**. Raw results (with URLs) go to
`ops/feasibility-raw/` (git-ignored); a sanitized summary goes to `ops/feasibility-results.json`
and the matrix in `ops/feasibility.md` is regenerated.

## 3. Telegram delivery smoke test (T01.8, T01.9)

Create a bot with @BotFather, put the token and your numeric user id in `.env`
(`BOT_TOKEN`, `ALLOWED_USER_IDS`), send `/start` to the bot once, then:

```bash
uv run python scripts/telegram_smoke.py            # hosted Bot API: photo, video, document, mixed album
uv run python scripts/telegram_smoke.py --large    # local Bot API profile: >50 MB synthetic file
```

Everything sent is synthetic media generated on the spot.

## 4. Opt-in pytest live tests

```bash
uv run pytest -m live
```

These re-run the corpus through the real adapters (`app/extractors`) and assert count, order and
item kinds against the `count=`/`kinds=` hints.
