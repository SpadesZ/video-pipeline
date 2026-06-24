# Video Pipeline MVP

CPU-first Docker scaffold for a semi-automated video production pipeline.

The MVP target is intentionally narrow:

```text
approved script + optional voice/transcript
  -> cue_ledger.json
  -> asset_manifest.json
  -> subtitles.srt
  -> preview manifest / preview.mp4 when ffmpeg inputs exist
  -> upload_package.md
```

## Why CPU-first

This project does not assume a local GPU. The default `ASR_PROVIDER=import`
accepts imported transcript segments, or falls back to rough script-derived cues.
WhisperX/CUDA can be added later behind the same adapter interface.

## Services

- `api`: FastAPI control plane.
- `worker`: Celery worker for long-running pipeline jobs.
- `redis`: Celery broker and result backend.
- `postgres`: durable project/review state, planned for Phase 2.

## Quick Start

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open the console:

```text
http://localhost:8010/
```

Manual start/stop scripts:

```powershell
scripts/start.ps1
scripts/health.ps1
scripts/stop.ps1
```

Windows startup automation is intentionally not enabled.

Secrets:

```powershell
Copy-Item secrets/.env.local.example secrets/.env.local
```

Paste provider keys into `secrets/.env.local` when LLM tasks need live calls.

Health check:

```text
http://localhost:8010/health
```

Run a smoke job:

```powershell
docker compose run --rm api python scripts/smoke_test.py
```

Outputs land under:

```text
data/projects/<project_id>/
```

## Next Build Steps

## Review Flow

Projects move through:

```text
cues_ready -> assets_review -> preview_ready -> approved
```

At any point, use `Request Changes` to move the project to `changes_requested`.
`Upload Ready` is only true when the final preview is approved and every asset
has `approved` rights status.

## External ASR Import

Use the project detail page's `Import Transcript` panel to paste external ASR
output. Supported formats:

```text
SRT / VTT / JSON / Text
```

Importing a transcript rebuilds `cue_ledger.json`, `subtitles.srt`,
`preview.mp4`, `asset_manifest.json`, and the visual quality contract, then
returns review status to `cues_ready`.

## Next Build Steps

1. Add transcript/SRT import so cue timing can come from real ASR output.
2. Replace rough cue timing with CPU-friendly faster-whisper output.
3. Persist project state in Postgres instead of JSON files only.
4. Add YouTube analytics CSV import for 24h, 7d, and 28d decisions.
5. Add optional GPU/WhisperX profile only after the CPU workflow is stable.
