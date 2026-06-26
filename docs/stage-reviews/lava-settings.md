# LAVA Settings Stage Review

Scope: LAVA/LLM provider visibility, task registry completeness, safe task binding updates, sanitized connection verification, deterministic no-key smoke tests, and a usable settings UI.

## Round 1

1. Existing task registry only had labels/descriptions, not stage/category metadata.
2. Existing task registry did not document fallback behavior per task.
3. Dispatcher accepted only static binding constants.
4. Users had no safe place to inspect all LAVA tasks.
5. Users had no safe way to change task to connection bindings.
6. Provider status did not expose configured vs missing env keys clearly.
7. Model overrides were not represented in binding metadata.
8. Runtime config needed to avoid storing secrets.
9. Runtime config needed to be outside committed source paths.
10. Unknown task/connection updates needed explicit validation.

Implemented: task metadata, non-secret `DATA_DIR/lava_settings.json`, binding validation, optional model override, and `.gitignore` coverage for runtime LAVA settings.

## Round 2

1. LAVA settings page did not exist.
2. Project LLM panel had no direct settings link.
3. Home page only linked API docs, not LAVA controls.
4. Status was only visible as a compact table on project pages.
5. Provider verification had no UI action.
6. Task binding update had no UI action.
7. Required vs optional tasks were not visually distinct.
8. Fallback notes were not visible to the operator.
9. Status JSON endpoint was missing for automation checks.
10. Binding forms needed to stay usable on narrow screens.

Implemented: `/settings/lava`, `/settings/lava/status`, binding POST, verify POST, settings links, provider table, task binding table, and responsive binding form layout.

## Round 3

1. Dispatcher did not report fallback order.
2. Dispatcher skipped missing keys silently.
3. Inactive connections needed to be skipped explicitly.
4. Unknown task failures needed a deterministic error payload.
5. Provider errors needed API-key redaction.
6. Verify needed to be capability-aware.
7. Verify needed to work without paid keys.
8. Verify should use runtime env secrets only.
9. Success payloads needed connection/model metadata.
10. Failure payloads needed enough detail for UI/debug without leaking credentials.

Implemented: fallback order reporting, missing-key/inactive attempt tracking, sanitized verifier, and chat-only verification guard.

## Round 4

1. No LAVA-specific smoke existed.
2. Smoke needed to run without real API keys.
3. Smoke needed isolated data dir to avoid mutating real settings.
4. Smoke needed to prove registry/status consistency.
5. Smoke needed to prove required tasks have binding or fallback.
6. Smoke needed to prove binding persistence.
7. Smoke needed to reject unknown task ids.
8. Smoke needed to reject unknown connection ids.
9. Smoke needed to prove provider error redaction.
10. Smoke needed to prove no-key dispatch reports missing-key fallback order.

Implemented: `scripts/smoke_lava_settings.py` with isolated `data/temp/lava_settings_smoke` and no-key assertions.

## Round 5

1. Docker daemon may not always be running on the Windows host.
2. Docker verification must be rerun after daemon startup.
3. Browser-level UI click verification is still pending for this stage.
4. Real provider verification with user keys should remain optional.
5. Binding updates currently store only task bindings, not connection CRUD.
6. Connection list remains env-backed defaults rather than user-created providers.
7. Rate-limit values are displayed as model fields but not enforced locally.
8. Live task status/progress is not part of this stage.
9. Navigation is still minimal and will be handled in the next UI stage.
10. LAVA settings do not yet expose import/export controls for config snapshots.

Implemented now: Docker startup was attempted when daemon was unavailable; final Docker/browser verification remains required before closing this stage.
