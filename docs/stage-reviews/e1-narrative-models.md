# E1 Narrative/Shot Core Models Stage Review

Scope: 建立 Narrative/Shot 驅動架構的核心資料模型，將 operational data 由
project JSON 提升為獨立資料表，並以顯式 migration 落實 schema 變更。

## Round 1

1. Cue 以固定秒數切分，時間是輸入而非產出，無法表達「這顆鏡頭拍什麼」。
2. `VisualQualityContract.ShotSpec` 以 `cue_id` 為鍵，使視覺層依賴 cue，
   依賴方向與 AI 生成流程相反。
3. AssetVariant 若存於 ProductionArtifact 的 JSON 欄位，一支片 20 顆鏡頭
   乘以每顆 5-10 個候選，再加 QC、成本與血緣，查詢與統計都無法進行。
4. 失敗、取消與逾期的生成工作若不留存，retry rate 與嘗試次數無從計算。
5. `seed` 不可作為角色身份的核心欄位，多數商用平台的 seed 不可控。
6. 單一 clip 品質與跨鏡頭連戲若混在同一結構，「Shot 8 的一致性」究竟
   對照 Shot 7 還是 Shot 1 將無法回答。
7. 時間線長度不等於生成素材長度相加，缺少 in/out point 與 retime 一層。
8. Profile 若以 `comic_drama | short_drama | film` 這類內容種類分類，
   會被程式拿來分支，最終長出多套 pipeline。
9. SQLModel 會將 StrEnum 自動建成資料庫原生 enum，日後新增狀態需要
   `ALTER TYPE ADD VALUE`，該語句無法在交易內執行也無法回退。
10. `PydanticJSON` 定義於 `production_artifact.py`，若新模型需引用它，
    而該檔又要引用新模型，將形成循環相依。

## Round 2

1. 確立五層真相邊界：NarrativeIR（故事）、ShotPlan（導演意圖）、
   VisualContract（生成/QC 約束）、EditDecision（剪輯）、CueLedger（時間線）。
   `ShotSpec` 降為 ShotPlan 的 view，不得成為第二來源。
2. 依資料性質分流持久化。NarrativeIR、ProductionProfile、ShotPlan 是一次
   讀寫的 project document，續存 JSON 欄位；AssetVariant、CapabilityJob、
   VariantQC、ContinuityQC、ReferenceAsset 是大量累積且需統計的
   operational data，各自獨立成表。
3. 新表的狀態欄位一律 `Column(String)` 搭配 Python 列舉驗證。StrEnum 是
   str 子類，比較語義不變，但資料庫端維持跨方言一致的 VARCHAR。
4. `CharacterIdentityPack` 保持 provider-neutral，所有素材以
   `ReferenceAsset.asset_id` 引用；seed、LoRA、平台 reference ID 一律
   收進 `provider_bindings`。
5. VariantQC 與 ContinuityQC 分為兩張表。後者帶 `scope` 與 `ref_shot_id`，
   明確標示比較對象。
6. 三段時長分離：`ShotPlan.target_duration_ms`（意圖）、
   `AssetVariant.actual_duration_ms`（生成結果）、
   `TimelineClip.used_duration_ms`（成片佔用）。
7. ProductionProfile 為純政策集合，`preset_id` 僅用於載入預設值，
   禁止參與執行期判斷。既有投影片路徑成為 `slideshow_legacy` 一組 preset。
8. QC 評分欄位可為 NULL 表示 N/A。無對白鏡頭的嘴型分數不應記為 0 分，
   否則會與「嘴型極差」混淆。
9. QC 各維度權重由 ProductionProfile 決定：漫劇的角色一致性權重遠高於
   嘴型，電影的運鏡與場景連戲權重較高。
10. `PydanticJSON` 抽離至 `pipeline/models/json_column.py`，
    `production_artifact.py` re-export 以維持既有 import 路徑。

## Round 3

1. `conn.dialect.has_table()` 是內部介面，缺少 `info_cache` 參數時會回報
   錯誤結果，導致 migration 的存在性保護失效。改用公開的 `inspect()`，
   同時使 `column_exists` 不再需要方言分支。
2. Migration 必須對「基礎資料表尚未初始化」的情況安全。基礎表由
   `create_all()` 建立，migration 只負責增量，因此 v0002 在表不存在時
   直接返回。
3. 冒煙測試加入 schema parity 檢查：比對 `create_all()` 與 migration 各自
   建出的資料表欄位集合，藉此攔截模型與 migration 不同步。
4. `metadata` 是 SQLAlchemy declarative 的保留名稱，ReferenceAsset 的
   對應欄位改名為 `asset_metadata`。
5. `artifact_severity` 的方向與其他評分相反，加權時以 `100 - severity`
   計入，並於模組註解標明評分方向。
6. `EditDecision` 以 model_validator 拒絕 `out_point_ms <= in_point_ms`，
   避免產生負長度的時間線片段。
7. `GenerationCost` 記錄 credits、嘗試次數、牆鐘時間與人工分鐘數等可觀測
   量，而非估算金額。第一階段為人工於平台會員方案操作，金額不可靠。
8. `AssetVariant.parent_variant_id` 記錄重生成血緣，使「這顆鏡頭重試幾次
   才可用」可被追溯。
9. `prompt_snapshot` 保存生成當下的實際提示詞，不可事後由 ShotPlan 回推，
   因為 ShotPlan 會隨版本演進而改變。
10. 冒煙測試明確斷言 operational data 不得洩漏進 `production_artifact.json`，
    以免持久化分層在日後被無意破壞。

## Verification

| 項目 | 結果 |
|---|---|
| `smoke_test.py` (api) | PASS |
| `smoke_navigation_ui.py` (api) | PASS |
| `smoke_migrations.py` (api) | PASS |
| `smoke_narrative_models.py` (api) | PASS |
| `smoke_project_store.py` (worker) | PASS |
| `smoke_lava_settings.py` (worker) | PASS |
| `smoke_asr.py` (asr-worker) | PASS |
| Migration 0002-0005 於 PostgreSQL upgrade | PASS |
| Migration 0002-0005 於 PostgreSQL downgrade | PASS |
| 既有 121 筆專案資料於 upgrade/downgrade 後 | 未受影響 |
| Migration 於 SQLite 雙情境（無基礎表 / create_all 後） | PASS |
| create_all 與 migration 的 schema parity | PASS |
| PostgreSQL 原生 enum 型別數量 | 1（僅既有 reviewstatus） |
| 模型層使用 Enum 型別的欄位 | 1（僅既有 review_status） |
| 架構檢查：Capability 未進入 llm_control | PASS |
| 架構檢查：無依 preset_id 分支 | PASS |
| 架構檢查：模型層無依 provider 硬編碼分支 | PASS |
| 新增檔案中文檔頭 | 14/14 |
| 新增檔案無 TODO 佔位 | PASS |
| production_artifact.py 無未使用 import | PASS |

## Deferred

1. `ShotSpec` 尚未實際降為 ShotPlan 的 view，目前兩者並存。待 E3 產生
   job manifest 時再以 ShotPlan 為唯一來源，並移除 ShotSpec 的獨立產生路徑。
2. `shot_plans` 目前存於 JSON 欄位。若單一專案鏡頭數成長至需要查詢與
   分頁，再獨立成表。
3. ReferenceAsset 與既有 `asset_manifest.AssetItem` 概念不同但名稱相近，
   後續 UI 需明確區分用詞，避免操作者混淆。
