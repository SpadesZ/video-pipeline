# E2 Capability Router Stage Review

Scope: 將 LAVA 由 LLM 專用派送升級為通用 Capability Router，使 Image、
I2V、T2V、Reference Video、Lip-sync、TTS、Music、SFX、Video QC 能以同一
契約調度，且不需修改既有 LLM 任務執行器。

## Round 1

1. `dispatch_llm_task(task_id, messages, ...)` 的簽名綁死 chat messages，
   影音生成無法沿用。
2. 平台選擇是 `if connection.provider == "openrouter" / elif "google"` 的
   硬編碼分支，每接一種能力就要再長一段 elif。
3. `LLMCapability` 只有 CHAT 與 VISION，描述的是 LLM 世界，硬塞
   IMAGE、I2V、TTS 會使命名失去意義。
4. `LLMConnection` 把 provider 與 model_id 綁在同一個物件，但一個平台會
   託管多個模型，同一模型也可能被不同平台託管。
5. 回傳只有 `ok` 布林。商業影音 API 多半不是 request-response 即時完成，
   manual transport 更是必然要表達「已派工、等待人工」。
6. Kling、Runway、Veo、Seedance 的欄位各不相同，若寫成程式分支，
   每接一個平台都要動 transport 程式碼。
7. 沒有任何結構可以回答「這顆鏡頭應該給哪個模型拍」。
8. `pipeline/stages/llm_executors.py` 的六個任務執行器已穩定運作，
   重構不應波及。
9. `lava_verifier.py` 直接依賴 `openai_compatible_chat` 與 `google_chat`
   兩個低階函式。
10. 以逐字元快照驗證重構過於脆弱，JSON 鍵序或文案調整都會誤報失敗。

## Round 2

1. 拆為三層：`ModelRegistry` 描述模型能力與限制，`RoutingPolicy` 依政策
   選型，`ProviderAdapter` 只負責 transport。
2. 模型與平台以 `hosted_by` 解耦，兩個方向都可查詢：由平台列模型，
   由模型列平台。
3. `Capability` 另建於 `pipeline/models/capability.py`，不擴充
   `LLMCapability`。後者僅作為既有 LLM 任務註冊表的相容層保留。
4. `ProviderSpec` 為宣告式 YAML，包含能力、片長限制、參考素材上限、
   必填欄位、參數對應與人工操作說明。平台差異以資料表達，不寫成分支。
   轉為 API transport 時同一份 spec 繼續沿用。
5. `CapabilityResult.status` 一開始就給滿八個狀態，並帶 `provider_job_id`、
   `submitted_at`、`completed_at`、`error_code`。
6. `pending_manual` 與成功同為終止條件。manual transport 產出 job package
   後即回傳等待人工，此時若繼續嘗試其他平台，同一顆鏡頭會被重複派工。
7. HTTP 實作由 `lava_dispatcher.py` 逐字搬移至
   `pipeline/capability/adapters/chat.py`，使依賴方向由 capability 層
   向外，而非新架構反向依賴舊模組。`lava_dispatcher` 保留 re-export，
   `lava_verifier` 不需改動。
8. `dispatch_llm_task` 降為薄相容層：解析 binding、轉為 CapabilityRequest、
   再將結果轉回既有鍵值。六個執行器一行未改。
9. 路由規則只比對 ProductionProfile 的政策欄位，不比對 `preset_id`。
   preset 只是政策的預設值組合，一旦用於分支，漫劇、短劇與電影就會
   各自長出一條路由路徑。
10. 以語義契約驗證重構：binding 解析穩定、fallback 順序一致、回傳鍵值
    完整、無金鑰行為一致。不使用逐字元快照。

## Round 3

1. `CapabilityRequest.content_hash()` 刻意排除 `request_id` 與
   `created_at`，使同樣的生成需求得到相同雜湊，供 E3 的 job manifest
   作為冪等鍵。
2. 冒煙測試檢查 catalog 三者的一致性：模型宣告的託管平台必須存在、
   兩者必須有共同能力、路由規則引用的模型必須存在且支援該能力。
3. 影音平台的片長與參考素材上限為保守預設，尚未實測，YAML 中明確標注
   必須於 V1 校正。在校正前不得視為事實。
4. `PyYAML` 明確加入 `requirements.txt`。原本僅由 `uvicorn[standard]`
   間接帶入，不應依賴 transitive dependency。
5. 路由以 `setdefault` 傳遞選定的模型，使呼叫端明確指定的 `model_id`
   優先於路由決策，保留 LAVA 設定頁的每任務模型覆寫行為。
6. 單一轉接器拋出例外時記錄為一次 attempt 並繼續下一候選，不中斷整體派送。
7. 找不到轉接器時回報 `no_adapter` 並繼續，而非拋出例外。E3 之前影音
   能力都會走到這條路徑，屬預期行為。
8. `benchmark_stats` 在 catalog 中留空，由 E6 依 scenario 回填。在累積
   真實資料前，routing 的偏好順序只是初始猜測，應於 V3 依 usable-shot
   rate 與人工修正時間重新排序。
9. `clear_adapters()` 明確標注僅供測試使用。
10. catalog 載入以 `lru_cache` 快取，避免每次派送都讀檔。

## Verification

| 項目 | 結果 |
|---|---|
| `smoke_test.py` (api) | PASS |
| `smoke_navigation_ui.py` (api) | PASS |
| `smoke_migrations.py` (api) | PASS |
| `smoke_narrative_models.py` (api) | PASS |
| `smoke_capability_router.py` (api) | PASS |
| `smoke_project_store.py` (worker) | PASS |
| `smoke_lava_settings.py` (worker) | PASS |
| `smoke_asr.py` (asr-worker) | PASS |
| catalog 一致性（provider / model / routing） | PASS |
| model 與 provider 雙向查詢解耦 | PASS |
| 相容性過濾（片長、比例、參考素材、跨平台錯配） | PASS |
| 路由依政策欄位而非 preset 名稱 | PASS |
| fallback 軌跡與次選接手 | PASS |
| pending_manual 終止 fallback | PASS |
| request content_hash 冪等性 | PASS |
| `llm_executors.py` 變更行數 | 0 |
| `lava_verifier.py` 變更行數 | 0 |
| `lava_settings.py` 變更行數 | 0 |
| `task_registry.py` 變更行數 | 0 |
| 架構檢查：capability 層無 provider 硬編碼分支 | PASS |
| 架構檢查：capability 層無 preset_id 分支 | PASS |
| 架構檢查：舊 dispatcher 的 if-else 已消除 | PASS |
| 新增檔案中文檔頭 | 10/10 |
| 新增檔案無 TODO 佔位 | PASS |

## Deferred

1. Manual transport 與 job manifest 在 E3 實作。目前影音能力可完成路由，
   但因尚無轉接器而回報 `no_adapter`。
2. TTS、Music、SFX、Lip-sync 已於 `Capability` 宣告，使 registry 能表達，
   但第一階段不實作。Audio scope 依計畫限縮。
3. `LLMCapability` 相容層仍存在。待 LAVA 設定頁改用 Capability 後再移除。
4. 影音平台規格與路由偏好皆為未經實測的初始值，V1 與 V3 必須回頭校正。
5. `lava_settings.py` 的 connection 概念與新架構的 provider/model 分離
   尚未整併。目前以 provider 名稱對應 connection_id，待設定頁改版時處理。
