# E3 Manual Transport and Job Manifest Stage Review

Scope: 建立人工派工路徑。系統依 ShotPlan 產出標準 job package，人工至
Kling / Runway / Veo / Seedance 生成後再匯回。第一階段不購買任何 API。

## Round 1

1. 第一階段不買 API，但仍需要一條與未來 API 完全相同的派工路徑，
   否則屆時替換等同重寫。
2. 各平台的欄位名稱不同（首幀在 Kling 叫 start_frame、Runway 叫
   input_image），若寫成程式分支，每接一個平台都要動 transport。
3. 人工操作需要看得懂的說明，但機器匯入需要結構化資料，兩者需求不同。
4. 同一顆鏡頭可能被重複派工，若無冪等鍵會產生重複的工作記錄，
   使 retry rate 失真。
5. ShotPlan 只引用 character_id，實際參考素材在 CharacterIdentityPack，
   派工前若不展開，job package 的 refs/ 會是空的。
6. 產出的 job package 若沒有版本資訊，日後 schema 改版將無法判斷舊工作
   屬於哪一版。
7. 人工生成需要一段時間，adapter 不能阻塞等待。
8. 專案的 shot 數量多時，逐一下載 job package 不切實際。

## Round 2

1. `job.json` 是唯一事實來源，其 `request` 欄位即 CapabilityRequest 的
   完整序列化。未來 API transport 直接讀取同一欄位，人工與 API 兩條路徑
   共用契約。
2. 平台欄位差異全數由 `ProviderSpec.parameter_mapping` 表達。
   `job_package.py` 內不出現任何平台名稱。
3. job package 同時產出 `prompt.txt`（供人工複製）與 `README.md`
   （由 `human_instructions` 生成），與 machine-readable 的 `job.json` 並存。
4. 以 `request_hash` 為冪等鍵。相同內容的請求得到相同雜湊，重複派工時
   沿用既有 `CapabilityJob`，不新增記錄。雜湊刻意排除 `request_id` 與
   `created_at`。
5. `shot_dispatcher` 負責把 CharacterIdentityPack 的 face、fullbody、
   wardrobe、voice 素材展開併入請求。
6. `job.json` 帶 `schema_version`、`shot_plan_version`、`profile_version`
   與 `created_at`。
7. `execute()` 產出 job package 後立即回傳 `pending_manual`，不阻塞。
   由匯入流程接續，與未來 API 的非同步等待形態一致。
8. 提供 `job-packages.zip` 一次下載整個專案的派工內容。
9. 人工 transport 依 `ProviderSpec.transport` 欄位資料驅動註冊，
   新增平台只需新增一份 YAML，不需改動任何程式碼。

## Round 3

1. 實地檢視產出後發現片長單位錯誤：內部以毫秒表達，直接寫入 job package
   會讓人工照著填成 6000 秒。更明顯的是 Veo 的欄位名宣告為
   `duration_seconds`，值卻是毫秒。新增 `ProviderSpec.duration_unit`，
   輸出時依平台換算，並在 README 標示單位。冒煙測試補上此項驗證。
2. 工作記錄失敗時回傳明確的 `job_record_failed` 與原因，而非讓例外冒泡
   成 router 的 `exception`。job package 已產出但無記錄則無法在匯回時
   對應，因此仍視為失敗。
3. E2 的冒煙測試原本假設影片能力沒有轉接器，E3 註冊人工 transport 後
   此假設失效。改為明確清空登錄表驗證該行為，結束後復原內建轉接器，
   並新增 `install_default_adapters()` 作為公開復原入口。
4. 相容性檢查會依參考素材上限排除平台。測試中一個帶臉部與服裝兩張參考
   圖的角色，在上限為 1 的平台上會被正確排除。這是真實的製作限制，
   V1 需要決定如何在上限內取捨。
5. 缺少參考素材檔案時記錄於 `missing_references` 並產生警告，
   而非靜默產出空的 refs 目錄。
6. 平台必填欄位缺漏時同樣寫入警告並顯示於 README 的注意事項。
7. `model_id` 屬於路由決策，不寫入平台參數，避免與平台自身的模型選單混淆。
8. job package 目錄以 `<shot_id>/<provider>/` 分層，同一顆鏡頭送往多個
   平台比較時結構清晰。
9. 派工於 `decision_log` 留下 `shots_dispatched` 事件與統計摘要。
10. UI 面板將 `pending_manual` 呈現為正常狀態而非錯誤，因為第一階段
    所有影片能力本就以人工完成。

## Verification

| 項目 | 結果 |
|---|---|
| `smoke_test.py` (api) | PASS |
| `smoke_navigation_ui.py` (api) | PASS |
| `smoke_migrations.py` (api) | PASS |
| `smoke_narrative_models.py` (api) | PASS |
| `smoke_capability_router.py` (api) | PASS |
| `smoke_manual_provider.py` (api) | PASS |
| `smoke_project_store.py` (worker) | PASS |
| `smoke_lava_settings.py` (worker) | PASS |
| `smoke_asr.py` (asr-worker) | PASS |
| job package 結構完整（job.json / prompt.txt / refs / README） | PASS |
| job.json 必要欄位齊全 | 13/13 |
| job.json 的 request 可還原且雜湊一致 | PASS |
| 平台欄位依 parameter_mapping 轉換 | PASS |
| 片長依平台單位換算 | PASS |
| request_hash 冪等（內容相同則雜湊相同） | PASS |
| 重複派工不新增工作記錄 | PASS |
| CharacterIdentityPack 參考素材展開 | PASS |
| 鏡頭長度套用 profile 政策上限 | PASS |
| 參考素材超限的平台被排除 | PASS |
| 缺漏素材產生警告並記錄 | PASS |
| `pending_manual` 終止 fallback | PASS |
| 架構檢查：transport 無平台名稱分支 | PASS |
| 架構檢查：job_package 無硬編碼平台欄位名 | PASS |
| 新增檔案中文檔頭 | 5/5 |
| 新增檔案無 TODO 佔位 | PASS |

## Deferred

1. 匯入流程於 E4 實作。目前可產出 job package 並記錄工作，但尚無法將
   人工生成的影片匯回為 AssetVariant。
2. 平台規格（片長範圍、參考素材上限、支援比例）仍為未實測的保守預設。
   V1 必須逐項校正，校正前不得視為事實。
3. 參考素材上限的取捨策略未定。角色若有多張參考圖，在上限為 1 的平台上
   應優先送臉部還是全身，需依 V1 的實測結果決定。
4. UI 目前僅支援整批派工。逐鏡頭重新派工需待候選管理介面於 E4 一併處理。
5. `CapabilityJob` 的狀態目前只會停在 `pending_manual`，其餘狀態待 E4
   匯入與 E8 API transport 才會使用。
