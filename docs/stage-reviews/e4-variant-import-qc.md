# E4 Variant Import and QC Stage Review

Scope: 將人工於平台生成的影片匯回系統，建立候選管理、品質評分與選片流程，
使 Benchmark 有可統計的資料來源。

## Round 1

1. 人工生成的影片必須回到系統才能比較模型，否則第一階段的測試無從累積。
2. 平台常無法精準命中要求的片長，若沿用 ShotPlan 的目標值，這個落差就會
   消失，而它正是評估模型的指標之一。
3. 同一支檔案可能被重複上傳，若無冪等會灌水候選數量與 retry rate。
4. 匯入後 `CapabilityJob` 若仍停在 `pending_manual`，工作狀態將永遠不準。
5. 單鏡頭品質與跨鏡頭連戲是不同語義，評分表單必須分開。
6. 表單留空的欄位若被當成 0 分，會與「該維度極差」混淆，並拉低加權。
7. 同一顆鏡頭若同時有多個 selected 候選，剪輯階段將無法決定用哪一個。
8. 沒有 ffprobe 的環境仍應能匯入，不能因為探測失敗而中斷。

## Round 2

1. 匯入流程沿用 `transcript_importer` 的型態：解析輸入、重建下游狀態、
   寫入 `decision_log`、落盤並入庫。
2. 實際規格一律以檔案為準，由 `media_probe` 以 ffprobe 取得，
   並與 ShotPlan 的目標值比對後記錄落差為警告，但不阻擋匯入。
3. 以 `file_hash`（檔案內容 SHA-256）為冪等鍵。相同內容重複匯入時
   回報 duplicate，不建立第二筆候選。
4. 匯入以 `request_hash` 關聯既有 `CapabilityJob`，找不到時退回以
   project/shot/provider 尋找最近一筆。有新候選進來即將工作推進為
   `completed` 並記錄完成時間。
5. `VariantQC` 與 `ContinuityQC` 分開記錄，後者要求 `scope=pair` 時
   必須指定 `ref_shot_id`。
6. 評分欄位留空即為 `None`。Web 表單的空字串明確轉為 `None` 而非 0。
7. 選片為互斥操作：選定新候選時，同一鏡頭其餘 selected 候選一併退回
   `imported` 並清除選片理由。
8. `media_probe` 的任何失敗都回傳 `probed=False` 與原因，不拋出例外。

## Round 3

1. 單一候選的評分為覆寫而非累加，`variant_qc.variant_id` 具唯一索引，
   重複評分只會更新既有列。
2. 評分範圍在寫入前檢查，超出 0-100 直接拒絕，避免髒資料進入統計。
3. 非影片副檔名與空檔案於匯入時跳過並記錄原因，不中斷整批作業。
4. `prompt_snapshot` 於匯入當下由 ShotPlan 複製，日後 ShotPlan 改版
   也不影響已匯入候選的紀錄。
5. `usable_shot_rate` 定義為「至少有一個可用候選的鏡頭比例」，
   與「可用候選佔全部候選的比例」不同，前者才反映一支片能不能完成。
6. 專案彙整累計 `human_correction_minutes`。能產好片但每支要人工整理
   數小時，就還不是 production pipeline，這個數字必須看得見。
7. 加權一律取自 `ProductionProfile.qc_weights`，不在 QC 模組內寫死。
8. 冒煙測試在 ffmpeg 可用時產生真實影片並驗證片長落差警告，
   不可用時退回位元組佔位檔，測試仍需通過。
9. UI 將留空欄位標示為 N/A，並在表頭說明 0-100 的範圍。
10. 候選列表顯示實際片長與解析度，與 ShotPlan 目標值並列以便人工判斷。

## Verification

| 項目 | 結果 |
|---|---|
| 既有 9 支 smoke | 全數 PASS |
| `smoke_variant_qc.py` (api) | PASS |
| 匯入建立 AssetVariant 並落盤 | PASS |
| 實際規格以 ffprobe 為準 | PASS |
| 片長偏離目標時產生警告 | PASS |
| file_hash 冪等（重複匯入不新增） | PASS |
| 非影片與空檔案被跳過 | PASS |
| request_hash 正確關聯 CapabilityJob | PASS |
| 匯入後工作推進為 completed | PASS |
| QC 留空欄位保持 N/A | PASS |
| N/A 未被當作 0 分計入加權 | PASS |
| 評分範圍檢查拒絕越界值 | PASS |
| 同一候選重複評分為覆寫 | PASS |
| scope=pair 缺 ref_shot_id 被拒絕 | PASS |
| 選片互斥且可改選 | PASS |
| 加權依 ProductionProfile 而異 | PASS |
| usable_shot_rate 與修正時間彙整 | PASS |
| 架構檢查：QC 未把 N/A 當 0 | PASS |
| 架構檢查：無 provider 名稱分支 | PASS |
| 新增檔案中文檔頭 | 4/4 |
| 新增檔案無 TODO 佔位 | PASS |

## Deferred

1. `GenerationCost` 尚未於匯入時填寫。credits、牆鐘時間與人工分鐘數需要
   人工輸入，待 V1 實測時確認要收集到什麼粒度再補表單。
2. 候選預覽目前只顯示規格，未內嵌播放器。人工評分仍需自行開啟檔案。
3. ContinuityQC 尚無 UI，目前僅能由程式呼叫。待 V1 確認實際比對方式
   （逐對比較或以場景為單位）後再設計介面。
4. `parent_variant_id` 的重生成血緣於匯入時未自動串接，需要人工指定
   來源候選，待候選管理介面完善後補上。
5. 匯入僅支援單一平台單次上傳。跨平台批次比較的操作流程待 V1 回饋。
