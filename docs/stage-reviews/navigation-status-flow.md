# Navigation and Status Flow Stage Review

## Round 1

1. No persistent navigation existed across dashboard, project detail, and settings pages.
2. Project detail relied on a one-off back link instead of a consistent app frame.
3. LAVA settings used a separate back link pattern from the rest of the app.
4. Home page hid project readiness behind status chips that did not show artifact progress.
5. Project detail showed review state but not transcript, LAVA, assets, preview, compliance, and upload as one flow.
6. Next action was inferred from disabled buttons instead of stated directly.
7. Missing prerequisites were not summarized in one visible place.
8. API docs were only visible on the dashboard header, not everywhere.
9. Project list did not expose whether transcript or assets were present.
10. Navigation changes had no deterministic smoke coverage.

## Round 2

1. A global nav was needed, but duplicating old back links would create two navigation systems.
2. The dashboard title needed to identify the view as an operational dashboard, not a landing page.
3. Project detail should keep ASR, LAVA, and review actions in their existing panels to avoid duplicate buttons.
4. The status flow needed to read existing `ProductionArtifact` fields instead of checking files ad hoc.
5. Upload readiness needed to remain separate from review status because compliance can still block upload.
6. LAVA state needed a visible placeholder even when no provider key is configured.
7. Asset readiness needed to display approved count versus total count.
8. Preview readiness needed to handle both `preview_mp4` and review status.
9. Compliance state needed to be visible even before external publishing exists.
10. The home list needed compact markers without making the table unreadable.

## Round 3

1. The nav should be sticky so long project pages keep orientation.
2. Nav labels should be stable: Projects, LAVA Settings, and API Docs.
3. The active nav state should mark both dashboard and project detail as Projects.
4. The project status flow should appear immediately after review stepper and overview cards.
5. Status node labels need both stage name and short detail, not only color.
6. Short status markers need tooltips for scan speed and accessibility.
7. Mobile layout needs single-column fallback for status nodes.
8. CSS should avoid viewport-scaled font sizes.
9. Status UI should use existing palette variables rather than adding a new theme.
10. The flow should use text and border states, not color alone.

## Round 4

1. Test data should not be written into persistent repo `data/projects`.
2. Smoke DB should be isolated from the shared local `test.db`.
3. No-key LAVA settings must keep working without live OpenRouter or Google calls.
4. `/docs` should be verified because it is now a first-class nav target.
5. The smoke should create a project through the same web form a user uses.
6. The project detail smoke should verify ASR, LAVA, and Review Gate remain discoverable.
7. The smoke should verify status flow text, not just HTTP 200.
8. The smoke should run under Docker `worker` with the existing requirements.
9. The status helper should centralize logic used by both home and detail pages.
10. The change should avoid introducing new service dependencies.

## Round 5

1. README needed a short operational note for the new navigation/status flow.
2. README needed a command to run the navigation smoke.
3. Existing LAVA settings instructions should remain intact.
4. Existing CPU ASR instructions should remain intact.
5. Existing AI niche research UI on the dashboard must not be removed by the Jules patch.
6. Trend research result page should use the same global nav.
7. Error pages should keep the global nav through the base `page()` template.
8. No raw API keys or secret paths should be rendered by the new UI.
9. No Windows boot autostart should be added.
10. The stage should stop at navigation/status visibility and leave external import stability for the next stage.
