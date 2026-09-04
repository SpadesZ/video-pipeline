# E5 Edit Decisions and Shot Assembly Stage Review

Scope: 以選定候選建立剪輯決策、由決策衍生時間線與 CueLedger，
並將實際生成的影片片段組裝為成片。

## Round 1

1. 剪輯不等於把生成檔案接起來。若時間線直接採用素材長度，
   取用區間、retime 與停格都無處表達，對白戲的節奏會失控。
2. 剪輯決策是人工調整的成果，若不持久化，每次重載都會被自動重建覆寫。
3. `CueLedger` 原本由固定秒數切分產生，與實際成片無關，字幕會對不上。
4. 各平台產出的解析度與影格率不一致，直接 concat 會失敗或畫面跳動。
5. 既有投影片路徑仍在使用，不能被鏡頭組裝直接取代。
6. ffmpeg 不可用的環境仍應能完成流程，不能直接崩潰。

## Round 2

1. 新增 `edit_decisions` 欄位（migration 0006）持久化人工調整。
2. `rebuild_timeline(preserve_existing=True)` 只補上尚未有決策的鏡頭，
   保留既有的 in/out point 與停格。要重設必須明確傳入 False。
3. `CueLedger` 降為衍生產物，起訖時間取自 `TimelineClip`，
   文字取自鏡頭所屬節拍，使字幕與畫面對齊。
4. 裁切階段一律重新編碼為統一解析度與影格率，再進行 concat。
   輸出尺寸由 `ProductionProfile.aspect_ratio` 決定。
5. `render_by_profile` 依 `render_mode` 分流，投影片與鏡頭組裝並存。
   分流依政策欄位，不依 preset 名稱。
6. ffmpeg 或素材缺失時輸出 `.assembly.txt` 組裝清單，供人工檢視，
   而非拋出例外。

## Round 3

1. 組裝模組只讀 `TimelineClip`，完全不碰 `AssetVariant.actual_duration_ms`。
   素材長度與成片長度在此徹底分離，冒煙測試以 grep 驗證這一點。
2. 只有已選定候選的鏡頭會產生剪輯決策，未選定者不會留下空片段。
3. `update_edit_decision` 以重新驗證取代 `model_copy`，
   確保 `out_point_ms > in_point_ms` 的約束在人工調整時仍生效。
4. 裁切時關閉音軌，音訊於最終 concat 階段以 voiceover 統一併入，
   避免各片段殘留的環境音互相干擾。
5. 臨時裁切檔於 `finally` 清除，失敗路徑也不會留下垃圾。
6. 淡出起點由時間線總長回推，不使用素材長度。
7. 冒煙測試刻意讓素材實際長度（8 秒、6 秒）不同於鏡頭目標長度（5 秒），
   以確保測試真的在驗證分離而非巧合成立。
8. 成片以 ffprobe 回頭驗證長度與解析度，而非只檢查檔案存在。
9. 既有投影片路徑於同一支冒煙測試中一併驗證，確保雙軌未被破壞。
10. `hold_ms` 延長成片但不改變素材紀錄，測試明確斷言此行為。

## Verification

| 項目 | 結果 |
|---|---|
| 既有 10 支 smoke | 全數 PASS |
| `smoke_shot_assembly.py` (api) | PASS |
| 預設取用區間為素材實際長度 | PASS |
| 未選定候選的鏡頭不產生決策 | PASS |
| 調整取用區間後時間線改變、素材不變 | PASS |
| 時間線長度不等於素材長度總和 | PASS |
| 停格計入成片但不改變素材 | PASS |
| 非法取用區間被拒絕 | PASS |
| 重建保留人工調整 | PASS |
| 明確重設才覆寫 | PASS |
| CueLedger 由時間線衍生且連續銜接 | PASS |
| cue 文字取自敘事節拍 | PASS |
| 實際產出可播放 MP4 | PASS |
| 成片長度符合時間線（誤差內） | PASS |
| 成片解析度統一 | PASS |
| 投影片路徑仍可運作 | PASS |
| migration 0006 於 PostgreSQL upgrade/downgrade | PASS |
| 既有 140 筆專案資料 | 未受影響 |
| 架構檢查：組裝不讀素材長度 | PASS |
| 架構檢查：分流依 render_mode 而非 preset | PASS |
| 新增檔案中文檔頭 | 4/4 |
| 新增檔案無 TODO 佔位 | PASS |

## Deferred

1. 剪輯 UI 尚未提供。目前可由程式調整取用區間，但專案頁還沒有
   時間線編輯介面，V2 之前需補上。
2. 字幕目前仍以外掛 SRT 呈現，未燒錄進成片。待 V2 確認平台需求再決定。
3. 轉場僅在模型中定義，組裝階段一律以硬切處理。淡入淡出只作用於整支片。
4. 音訊仍以單一 voiceover 併入，尚未支援逐鏡頭對白軌。
   對白戲的音訊先決節奏待短劇 profile 驗證時處理。
5. `retime_mode=SPEED` 已於模型與時間線計算中支援，但組裝階段尚未
   套用變速濾鏡，目前僅影響時間線長度。
