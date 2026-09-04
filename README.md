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

### Smoke Suite

Each smoke is deterministic and needs no API key. Note the container column:
`smoke_project_store.py` imports the worker module and fails in the `api`
container.

| Smoke | Container |
|---|---|
| `smoke_test.py` | `api` |
| `smoke_navigation_ui.py` | `api` |
| `smoke_migrations.py` | `api` |
| `smoke_narrative_models.py` | `api` |
| `smoke_capability_router.py` | `api` |
| `smoke_project_store.py` | `worker` |
| `smoke_lava_settings.py` | `worker` |
| `smoke_asr.py` | `asr-worker` (needs `--profile asr`) |

## Database Migrations

Schema changes use explicit versioned migrations. `SQLModel.metadata.create_all()`
creates missing tables but never alters existing ones, so any new column must
ship with a migration.

```powershell
docker compose run --rm api python scripts/migrate.py status
docker compose run --rm api python scripts/migrate.py upgrade
docker compose run --rm api python scripts/migrate.py check
docker compose run --rm api python scripts/migrate.py downgrade --target 1
```

`check` exits non-zero when the schema is behind, which is what CI uses.
Migrations live in `pipeline/migrations/versions/` as `vNNNN_<name>.py`, each
defining `VERSION`, `NAME`, `upgrade(conn, dialect)`, and `downgrade(conn, dialect)`.
They are dialect-aware and verified against both SQLite and PostgreSQL.

The application never modifies schema on startup. It only logs a warning when
the schema is behind.

Outputs land under:

```text
data/projects/<project_id>/
```

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

## Local CPU ASR

The default Docker stack keeps ASR import lightweight. Local transcription runs
through the optional CPU ASR worker profile, which installs `faster-whisper` at
image build time so restarts do not require manual package installation.

```powershell
docker compose --profile asr up --build -d
```

In the project detail page, use `Run CPU ASR` after the project has a
`voiceover.wav`, `voiceover.mp3`, `audio.wav`, or `audio.mp3` in its project
directory. The ASR job writes:

```text
transcript_import.json
cue_ledger.json
subtitles.srt
production_artifact.json
```

Run the deterministic ASR smoke without downloading a Whisper model:

```powershell
docker compose --profile asr run --rm asr-worker python scripts/smoke_asr.py
```

The smoke uses a deterministic mocked transcript by default so it is stable on
CPU-only Windows hosts and does not depend on model downloads. Real local ASR is
exercised by running the ASR worker on a project that already has a voiceover or
uploaded audio file.

## Navigation and Status Flow

The web console has a persistent top navigation bar for Projects, LAVA Settings,
and API Docs. The project detail page includes a Project Status Flow panel that
summarizes transcript/ASR, LAVA output, asset rights, preview, compliance, and
upload readiness, plus the next recommended action and missing prerequisite.

Run the deterministic navigation/UI smoke:

```powershell
docker compose run --rm api python scripts/smoke_navigation_ui.py
```

## Capability Router

LAVA dispatch runs through a general capability router rather than an
LLM-only path. Layers:

```text
CapabilityRequest -> ModelRegistry -> RoutingPolicy -> ProviderAdapter
```

Models and providers are decoupled: a provider hosts many models, and a model
may be hosted by several providers. Adapters are looked up by
`(capability, provider)` instead of a hardcoded if/else chain.

Catalog lives in `pipeline/capability/catalog/`:

```text
providers/*.yaml   per-platform spec: capabilities, limits, required fields,
                   parameter mapping, human instructions
models.yaml        model capabilities, duration and aspect limits, hosted_by
routing.yaml       preference rules matched on ProductionProfile policy fields
```

Routing rules match on policy fields (`quality_tier`, `motion_policy`,
`aspect_ratio`). They must never match on `preset_id` — a preset is only a
bundle of policy defaults, and branching on its name would grow a separate
pipeline per content type.

`CapabilityResult.status` covers the full job lifecycle, not a boolean:

```text
pending_manual -> submitted -> queued -> running -> completed
                                      -> failed / cancelled / expired
```

`pending_manual` ends the fallback loop. A manual job is already dispatched to
a human, so retrying other providers would duplicate the work.

Video platform limits in the catalog are conservative placeholders and are
marked as pending real measurement. Treat them as unverified until calibrated.

## LAVA Settings

Open LAVA provider and task binding settings:

```text
http://localhost:8010/settings/lava
```

The settings page shows configured provider env keys, registered video LLM
tasks, required/optional status, current task bindings, and deterministic
fallback behavior. Binding updates are stored in `DATA_DIR/lava_settings.json`
and do not store API keys.

Private provider keys stay in `secrets/.env.local`:

```text
OPENROUTER_API_KEY=...
GOOGLE_API_KEY=...
OPENROUTER_MODEL_ID=openai/gpt-4o-mini
GOOGLE_MODEL_ID=gemini-2.0-flash
```

Run the no-key LAVA settings smoke:

```powershell
docker compose run --rm worker python scripts/smoke_lava_settings.py
```

## Next Build Steps

1. Improve navigation, project state visibility, and user-facing status flow.
2. Add external import stability checks and restart-stable Docker operations.
3. Add quality scoring gates for transcript, script, visual, audio, and video.
4. Add image/asset generation with safe fallbacks.
5. Add optional GPU/WhisperX profile only after the CPU workflow is stable.
