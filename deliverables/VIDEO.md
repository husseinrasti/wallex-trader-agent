# Demo video

**Summary**
1. **The video is not recorded yet.** The link below is a placeholder until the operator records and uploads it.
2. The video is about 5 minutes long and is **recorded by the operator**, not by the agent. It follows the shot list [`docs/VIDEO_SHOTLIST.md`](../docs/VIDEO_SHOTLIST.md), with [`scripts/demo.sh`](../scripts/demo.sh) running each scene.
3. Scenes: problem & approach, architecture, live dashboard, risk in action (a veto and a kill-switch drill), evidence, reproducibility, wrap-up (PRD-06 §5).
4. What it shows is offline. The synthetic feed (labelled on every screen) runs with the real OpenRouter LLM. The replay of the 4-hour shadow run gives 40/40 decisions with 0 network calls.
5. At the time of writing no live trade has happened, so the video shows no live result. If it is recorded after the live run, the shot list says what to change.

| | |
|---|---|
| Video link | _pending: paste the YouTube (unlisted) or Google Drive ("anyone with the link") URL here_ |
| Length | _pending_ (target about 5 min) |
| Recorded on (UTC) | _pending_ |
| Recorded at commit | _pending_ (`git describe --tags` at recording time) |
| Recorded by | the operator |
| Shot list and narration | [`docs/VIDEO_SHOTLIST.md`](../docs/VIDEO_SHOTLIST.md) |
| Demo helper | [`scripts/demo.sh`](../scripts/demo.sh) (`bash scripts/demo.sh --list` for the scenes) |

After uploading, the operator fills in the table, puts the same link in the README section "Demo video", and commits
both. The video file is not committed: `video/` is gitignored, and a 5-minute 1080p file is often over GitHub's
50 MB warning size.
