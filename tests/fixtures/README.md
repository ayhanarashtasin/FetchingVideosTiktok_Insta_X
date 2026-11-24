# Test fixtures

* `extractor/*.json` - **synthetic** extractor outputs (gallery-dl `-j` dumps and yt-dlp `-J` documents),
  produced by `make_fixtures.py`. They are modelled on the field names read from the pinned extractor
  sources; they are **not captures of real platform responses** (every record carries `_synthetic: true`,
  all ids and users are invented). They prove that this project's mapping handles the shapes it expects, not
  that TikTok/Instagram/X currently return them - only `scripts/feasibility_probe.py live` can show that.
* Binary media is never committed: tests generate synthetic photos/videos with FFmpeg's `lavfi` sources
  (`tests/conftest.py`).
* `tests/contract/test_fixtures_in_sync.py` regenerates the fixtures in memory and fails if a committed
  file was edited by hand, so a fixture cannot quietly turn into a pseudo-capture.
