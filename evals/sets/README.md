# Eval sets

Each folder here is one eval set. `zvc eval evals/sets/<name>` runs it, and CI runs
all of them on every push.

| set | source | what it measures |
|---|---|---|
| `synthetic_smoke` | invented transcript (`synthetic: true`) | one case per guardrail, so a broken validator fails CI |
| `pilot_v1` | first real Cronovember recording (to add) | hand-picked good and bad clips from real footage |

## Adding the pilot set

1. `zvc transcribe path/to/video.mp4 --id <video_id>` writes `data/<video_id>/transcript.whisper.json`.
2. Copy that transcript into `evals/sets/pilot_v1/transcript.json` and write a `manifest.yaml`
   (copy the smoke one, set `synthetic: false`, list any spoilers for that video).
3. Add one YAML file per case under `cases/`: `label: good` for clips you would post as is,
   `label: bad` plus `expected_codes` for clips that should be stopped and why.
   Bad cases are most useful when they are mistakes a model actually makes, so after
   milestone 2 the best source of new bad cases is rejected LLM proposals.

Results land in `evals/results/<set>/` as a timestamped JSON per run plus `latest.md`.
