# ASR Stage Review

Scope: CPU-first ASR completion with Docker restart stability, external transcript import preserved, project state persisted through transcript, cues, subtitles, and artifact files.

## Round 1

1. CPU ASR dependencies must not be installed manually after Docker restart.
2. Default `worker` image should stay lightweight and not pull ASR dependencies.
3. ASR should run on a dedicated queue to avoid blocking preview or LAVA jobs.
4. CPU ASR worker concurrency should default to 1 on local Windows hosts.
5. ASR smoke should not require paid keys or a real Whisper model download.
6. README commands should target the actual ASR image, not the default worker by accident.
7. ASR adapter should import `faster-whisper` lazily so default runtime stays usable.
8. Missing audio should produce a clear worker error.
9. Generated transcript, cue ledger, subtitles, and asset manifest should all be written.
10. External transcript import must remain first-class and untouched by the local ASR path.

Implemented: optional `asr-worker` profile, dedicated `asr` Celery queue, build-time ASR dependencies, deterministic ASR smoke, README correction, and single-task ASR worker concurrency.

## Round 2

1. Existing projects can contain JSON dicts where Pydantic models are expected.
2. `asset_manifest` dicts can crash compliance checks because `.assets` is missing.
3. `visual_qc_report` dicts can crash visual compliance checks.
4. `decision_log` entries can reload as dicts and lose typed behavior.
5. `review_status` can reload as a string and trigger serializer warnings.
6. `created_at` and `updated_at` can reload as strings and trigger serializer warnings.
7. Project list and project detail must normalize data consistently.
8. JSON fallback project load must normalize before compliance.
9. Normalization should repair data on save without requiring destructive migration.
10. The fix should not modify secrets or require manual database cleanup.

Implemented: `normalize_project_artifact` coercion in project load/list paths for nested models, decision logs, review status, and datetimes.

## Round 3

1. ASR enqueue events should include a Celery task id for traceability.
2. UI should explain that the optional ASR worker must be running.
3. ASR worker logs should show it consumes only the `asr` queue.
4. The project page should show the ASR action without opening raw JSON.
5. The ASR route should redirect back to the transcript panel.
6. Enqueued tasks should not silently fall into the default worker queue.
7. Smoke should verify persisted project state after reload.
8. ASR should preserve existing visual prompt metadata when cue indexes align.
9. Subtitle generation should use existing SRT formatting logic.
10. ASR should mark the project back to `cues_ready`.

Implemented: Celery task id is recorded in the decision log, UI ASR action is present on project detail, and smoke reloads persisted state.

## Round 4

1. Browser automation tooling is not yet available in this thread.
2. HTTP checks should at least prove the existing project view returns 200.
3. HTML checks should prove the ASR button and POST action exist.
4. Docker health should be checked after rebuilding images.
5. `asr-worker` queue and registered tasks should be verified from logs.
6. ASR profile startup should not enable Windows boot autostart.
7. ASR smoke should run through the ASR image after rebuild.
8. Existing project-store smoke should still pass after normalization changes.
9. Compose config should validate before build.
10. Python files touched by the stage should compile.

Implemented: HTTP health, existing project page HTML checks, ASR worker log inspection, compose config, Docker build, ASR smoke, project-store smoke, and Python compile checks.

## Round 5

1. Celery still warns that containers run as root; keep as a future hardening task.
2. Real Whisper model cache is not yet mounted separately; first real run may download.
3. UI does not yet show live Celery task state.
4. UI does not yet preflight whether a project has an audio file before enqueue.
5. There is no user-upload audio form for ASR yet.
6. ASR language/model controls are environment-based, not UI-based.
7. Real ASR quality scoring is still absent.
8. Multi-speaker diarization is not implemented.
9. Large audio chunking/retry logic is not implemented.
10. End-to-end video output is not part of this stage and remains for later closure stages.

Implemented after Antigravity verification: missing audio is now caught before enqueue, and zero-segment ASR results fail without overwriting the existing timeline. Carry the remaining hardening items into later stages instead of widening this patch further.
