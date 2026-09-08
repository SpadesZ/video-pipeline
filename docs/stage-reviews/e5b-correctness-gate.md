# E5b Correctness Gate Stage Review

Scope: V1 前最後一道正確性關卡。五項缺陷都不影響 happy path，
但會讓血緣、成片內容與統計在真實使用中悄悄失真。不新增功能。

基線：`63391e9`

## Round 1

1. 匯入候選時以當前 `ShotPlan` 填寫 `prompt_snapshot` 與
   `reference_asset_ids`。分鏡在派工後被修改，血緣就會指向從未送出去的
   內容。job package 目錄為 `<shot>/<provider>`，同一鏡頭重新派工會覆寫
   前一份 manifest，連對照的機會都沒有。
2. 改選候選後，既有 `EditDecision` 仍指向舊候選。直接 render 會用到已被
   換掉的素材，而畫面上完全看不出異常。
3. 音訊使用 `-shortest`。voiceover 比時間線短時，成片會被截到音訊長度，
   默默少掉數秒。render 後沒有任何長度驗證。
4. `variant_id` 由 `shot_id + provider + file_hash` 組成，不同專案匯入
   同一支檔案會撞主鍵。去重只比對 `project/shot/file_hash`，相同位元組
   若來自不同平台會被合併，平台歸屬直接消失。
5. `summarize_project_qc()` 以 `setdefault` 從 job 與 variant 回填統計列。
   分鏡被刪除後，殘留資料仍會建立新的統計列並稀釋可用率。
   `ContinuityQC` 未驗證 `shot_id`/`ref_shot_id`/`scene_id` 的歸屬。

## Round 2

1. migration 0008 於 `capability_jobs` 新增 `request_snapshot`、
   `provider_parameters`、`reference_asset_ids`、`manifest_path`，
   在派工當下凍結完整請求。新增 `build_provenance()`，血緣只讀這份快照，
   函式簽名刻意不接受 `ShotPlan`。job package 目錄改為
   `<shot>/<provider>/<request_hash 前綴>`，內容不同的請求落在不同目錄。
2. 新增 `check_selection_consistency()`，驗證 ShotPlan → selected variant
   → EditDecision → TimelineClip 四層一致，並整合進 `preflight_assembly()`。
   不一致時列入 `stale_shots` 並 fail-closed 拋出 `AssemblyBlocked`。
   傳入的時間線也會與當前剪輯決策比對 variant 序列與總長。
3. 移除 `-shortest`。改以 `apad` 補靜音、輸出端 `-t` 取時間線長度，
   影片長度一律由時間線決定。新增 `verify_output_duration()` 於 render 後
   以 ffprobe 比對，超出容差即刪除成片並改寫組裝清單，不留下長度錯誤的檔案。
4. `variant_id` 改為 `var_{uuid4[:20]}`，全域唯一。去重條件加入
   `provider` 與 `job_id`，相同位元組但來源不同即視為兩筆獨立生成。
5. 統計分母改為只由 `artifact.shot_plans` 建立。不在分鏡表中的 job 與
   variant 歸入 `orphan_shot_ids` / `orphan_jobs` / `orphan_variants`，
   不建立統計列也不影響任何比率。`record_continuity_qc()` 驗證鏡頭必須
   在分鏡表中、場景必須在 NarrativeIR 中。

## Round 3

1. `JOB_SCHEMA_VERSION` 升為 1.1，反映目錄結構變更。
2. 相同內容重複產生 job package 仍落在同一目錄，維持冪等；只有內容不同
   才產生新目錄。
3. 未關聯派工的匯入，血緣全部留空並附「無生成血緣可記錄」警告，
   而非以空字串假裝有值。
4. 音訊測試同時涵蓋較短與較長兩種情形，兩者的成片長度都必須等於時間線。
5. 改選測試在還原狀態後重建時間線，避免污染後續驗證。
6. 冒煙測試以「ShotPlan 改寫後 variant 血緣不得變動」直接斷言，
   而非只檢查欄位有值。

## Verification

| 項目 | 結果 |
|---|---|
| `smoke_test.py` (api) | PASS |
| `smoke_navigation_ui.py` (api) | PASS |
| `smoke_migrations.py` (api) | PASS |
| `smoke_narrative_models.py` (api) | PASS |
| `smoke_capability_router.py` (api) | PASS |
| `smoke_manual_provider.py` (api) | PASS |
| `smoke_variant_qc.py` (api) | PASS |
| `smoke_shot_assembly.py` (api) | PASS |
| `smoke_project_store.py` (worker) | PASS |
| `smoke_lava_settings.py` (worker) | PASS |
| `smoke_asr.py` (asr-worker) | PASS |
| `pytest tests/` | 6 passed |

E5b 新增的針對性驗證：

| 情境 | 結果 |
|---|---|
| ShotPlan 派工後修改，job 快照不變 | PASS |
| ShotPlan 修改後，既有 variant 血緣不變 | PASS |
| 血緣取自派工快照而非當前分鏡 | PASS |
| 同 shot/provider 第二個 request 不覆寫 manifest | PASS |
| 相同內容重複產生維持冪等 | PASS |
| 改選候選後舊時間線 render 被擋 | PASS |
| 重建後 render 通過並指向新候選 | PASS |
| 音訊短於時間線不截斷影片 | PASS |
| 音訊長於時間線成片仍等於時間線 | PASS |
| 跨專案相同檔案不撞主鍵 | PASS |
| 不同平台相同位元組保留 attribution | PASS |
| 同專案同平台相同檔案判定重複 | PASS |
| orphan 不改變分母與可用率 | PASS |
| orphan 另列且不出現在統計列 | PASS |
| 外部 shot_id / ref_shot_id / scene_id 被拒絕 | PASS |

Schema 與資料：

| 項目 | 結果 |
|---|---|
| migration 0008 於 PostgreSQL upgrade/downgrade | PASS |
| migration 0–8 於 SQLite 雙向 | PASS |
| create_all 與 migration schema parity | PASS |
| PostgreSQL 專案資料（migration 前後） | 153 筆，未變動 |
| 最早資料時間 | 2026-06-18，仍在 |
| 既有 capability_jobs / asset_variants | 0 筆，無回填需求 |

## Deferred

沿用 E5a 的未竟項目，皆不阻擋 V1：轉場僅硬切、音訊為單一 voiceover、
`GenerationCost` 匯入時無填寫欄位、ContinuityQC 無 UI、剪輯取用區間
只能由程式調整、`parent_variant_id` 需人工指定。
