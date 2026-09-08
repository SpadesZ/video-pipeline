# E5a Correctness Hardening Stage Review

Scope: V1 前的正確性強化。修正七項會讓資料失真或讓殘缺結果看起來正常的
缺陷。這些缺陷不影響 happy path，但會在真實使用中悄悄污染統計。

## Round 1

1. `_resolve_job` 在 `request_hash` 找不到時退回「同 shot 同 provider 的
   最近一筆工作」。使用者貼錯 hash 時，候選會被靜默接到別的工作上，
   之後所有以工作為單位的統計都是錯的。
2. `select_variant` 與 `record_variant_qc` 直接以 `variant_id` 查詢，
   未驗證歸屬。帶著別的專案的 id 就能跨專案改動資料。
3. 匯入以 `ShotPlan.target_duration_ms` 計算片長落差，但派工前該值會經
   `ProductionProfile.shot_duration.clamp_ms()` 夾住。實際送出 6000ms
   卻以 5000ms 為基準，算出的偏差是錯的。
4. `summarize_project_qc` 只從「已有 AssetVariant 的鏡頭」建分母。
   完全生不出東西的鏡頭直接從統計中消失，usable-shot rate 被高估。
5. 鏡頭宣告了 `character_refs` 但找不到對應的 `CharacterIdentityPack`
   時，仍會照常派工。平台收到沒有參考圖的 job package，產出無法歸因。
6. `preflight` 不存在。缺少已選定候選的鏡頭會被 `build_edit_decisions`
   直接跳過，成片少了幾顆鏡頭卻照樣輸出，長度也對不上時間線。
7. `retime_mode` 與 `hold_ms` 只影響時間線長度計算，組裝階段未套用，
   成片與時間線靜默分歧。

## Round 2

1. `request_hash` 改為嚴格比對：找到工作後必須同時符合 `project_id`、
   `shot_id` 與 `provider`，任一不符即拋出 `JobLinkError` 拒絕匯入。
   刻意移除所有 fallback 路徑。完全未提供 hash 時回傳 `linked=False`，
   明確標示為未關聯匯入，而非假裝找到了來源。
2. 新增 `load_owned_variant()` 作為所有以 `variant_id` 為入口的操作的
   統一閘門，驗證失敗即拒絕。`select_variant`、`record_variant_qc` 與
   `record_continuity_qc` 引用的候選全數經此檢查。
3. 新增 migration 0007，於 `capability_jobs` 記錄
   `requested_duration_ms` 與 `requested_aspect_ratio`，即派工當下真正
   送出的規格。落差比對只以此為基準；未關聯工作時 `requested_duration_ms`
   留空，不回頭採用 ShotPlan 的意圖值充當。
4. 定義互斥的 `ShotOutcome`：planned、dispatched、failed、completed、
   usable，判定優先序為 usable > completed > failed > dispatched > planned。
   分母改由 `artifact.shot_plans` 建立，並提供兩種 usable-shot rate：
   `of_planned` 回答「整支片能否完成」，`of_dispatched` 回答「模型好不好」。
5. 新增 `ShotReadinessState`（ready / incomplete / blocked）與
   `check_shot_readiness()`。派工預設 fail-closed，blocked 一律不派，
   incomplete 需明確傳入 `allow_incomplete` 才派。未通過者狀態標為
   `not_dispatched` 並附原因，不會以已派工的外觀混入報告。
6. 新增 `preflight_assembly()`。分鏡表上每一顆鏡頭都必須有已選定且檔案
   存在的候選，否則拋出 `AssemblyBlocked` 並列出缺少的鏡頭。
7. `TimelineClip` 帶出 `retime_mode`、`retime_factor` 與 `hold_ms`，
   組裝時分別轉為 `setpts` 與 `tpad` 濾鏡實際 render。

## Round 3

1. 修正 ffmpeg 參數位置。`-t` 原本置於 `-i` 之後屬於輸出時長限制，
   會把 `tpad` 停格與 `setpts` 變速產生的額外長度截掉。改為與 `-ss`
   一同置於 `-i` 之前，成為輸入端限制，濾鏡才能真正延長輸出。
   此問題在加入成片長度驗證前完全靜默。
2. 冒煙測試以「加入 2 秒停格後成片必須變長」驗證濾鏡確實生效，
   而非只檢查濾鏡字串存在。
3. 統計口徑的各狀態計數總和必須等於分鏡數，測試以此斷言狀態互斥。
4. 測試明確區分「候選可用率」與「鏡頭可用率」，並斷言兩者在有多個候選
   時不相等，避免日後誤用。
5. `seed_job` 的 `requested_duration_ms` 刻意設為 6000 而非 ShotPlan 的
   5000，使「基準取自派工記錄」這件事在測試中真的被驗證，而非巧合成立。
6. `JobLinkError` 於 Web 層單獨捕捉，回應明確說明 hash 不符，
   並在 `decision_log` 留下 `variants_import_rejected` 稽核記錄。
7. UI 的鏡頭派工面板新增 Ready 欄位，三態各有樣式與 tooltip 說明原因，
   並在表格下方說明 Incomplete 與 Blocked 的差異。
8. 候選面板改為顯示兩種 usable-shot rate 與每可用鏡頭的人工分鐘數。
9. `resolve_reference_availability()` 同時檢查素材已登錄且實體檔案存在，
   只登錄未上傳的素材一樣視為不完備。
10. `assess_project_readiness()` 供 UI 一次取得全專案完備度，
    避免逐鏡頭查詢資料庫。

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
| request_hash 不存在時拒絕 | PASS |
| request_hash 跨 project 時拒絕 | PASS |
| request_hash 跨 shot 時拒絕 | PASS |
| request_hash 跨 provider 時拒絕 | PASS |
| 未提供 hash 時標示 unlinked 且基準留空 | PASS |
| 外部鏡頭匯入被拒絕 | PASS |
| 跨專案 select / QC / 連戲引用被拒絕 | PASS |
| 落差以派工規格而非 ShotPlan 為基準 | PASS |
| 容差內不誤報落差 | PASS |
| 統計分母涵蓋無候選的鏡頭 | PASS |
| ShotOutcome 各狀態互斥且總和相符 | PASS |
| 候選可用率與鏡頭可用率不混用 | PASS |
| readiness 三態判定 | PASS |
| 預設 fail-closed 不派工 | PASS |
| allow_incomplete 可派 incomplete 但仍擋 blocked | PASS |
| preflight 擋下缺候選的組裝 | PASS |
| AssemblyBlocked 列出缺少鏡頭 | PASS |
| 停格實際 render（成片變長） | PASS |
| 變速轉為 setpts 濾鏡 | PASS |
| migration 0007 於 PostgreSQL upgrade/downgrade | PASS |
| migration 0..7 於 SQLite 雙向 | PASS |
| create_all 與 migration schema parity | PASS |
| PostgreSQL 既有資料（最早 2026-06-18） | 未受影響 |

## Deferred

1. 轉場仍僅在模型中定義，組裝一律硬切。淡入淡出只作用於整支片。
2. 音訊仍為單一 voiceover，尚未支援逐鏡頭對白軌。
3. `GenerationCost` 仍未於匯入時填寫，待 V1 確認可收集的粒度。
4. ContinuityQC 仍無 UI，只能由程式呼叫。
5. `parent_variant_id` 的血緣仍需人工指定，匯入時不會自動串接。
6. 剪輯 UI 未提供，取用區間只能由程式調整。
