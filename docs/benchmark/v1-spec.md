# V1 Benchmark Spec

Pack 版本：`v1.0`　專案 ID：`benchmark_v1`

## 目的

在不購買任何 API 的前提下，以人工操作四個平台，取得**可重複、可比較、
可追溯**的資料，回答一個問題：**哪一顆鏡頭該交給哪個平台拍。**

刻意不產生單一「全球最佳模型」排名。一個在對話戲表現最好的模型，
在高動態場景可能崩壞得最嚴重，把兩者平均成一個分數會抹掉這個資訊。

## 比較對象（BenchmarkTarget）

比較的單位是 **BenchmarkTarget**，不是 provider。同一平台會同時提供多個
版本，介面上的選項名稱也未必等同 catalog 的 model_id。若只記錄「這是
Kling 生的」，日後無從得知當時選的是哪一版，跨輪次比較會失效。

定義於 `pipeline/benchmark/catalog/v1_targets.yaml`：

| 欄位 | 說明 |
|---|---|
| `target_id` | 唯一識別。job package、attempt ledger、評分表與統計皆以此為關聯鍵 |
| `provider` | 平台 |
| `model_id` | catalog 中的模型識別 |
| `model_version` | 平台介面顯示的實際版本 |
| `ui_label` | 平台介面上該選項的名稱 |
| `transport` | V1 一律 `manual` |
| `provisional` | 尚未經真人於平台確認者為 `true` |

目前四個 target（`kling_default`、`seedance_default`、`veo_default`、
`runway_default`）的 `model_id` 皆為通稱，`model_version` 未填，
**全部標記 provisional**。

> **provisional 的 target 不得視為最終模型真相。** 真人登入平台確認實際
> 版本後，需更新 YAML 並將 `provisional` 改為 `false`。
> 若平台提供多個版本，應為每個版本各建一個 target。
>
> catalog 中的片長範圍、參考素材上限與支援比例同樣是未實測的保守預設。

## 公平性約束

所有平台共用同一份 `CharacterIdentityPack`、`Scene`、`ShotPlan`、
參考素材與核心提示詞。**只允許 `ProviderSpec.parameter_mapping` 的欄位
名稱與單位轉換**，不得為個別平台改寫內容。

冒煙測試 `smoke_benchmark_v1.py` 以 `verify_prompt_parity()` 強制檢查：
同一顆鏡頭在四個平台的 `prompt`、`negative_prompt`、`first_frame_ref`、
`reference_asset_ids`、`duration_ms`、`aspect_ratio` 必須完全相同。

`request_hash` 跨平台必然不同（它涵蓋路由選定的 model_id），這是必要的，
否則四次派工會被冪等機制視為同一筆。

### 為何每顆鏡頭只用單張首幀

Runway 與 Veo 的參考素材上限為 1（provisional）。若同時附上角色設定圖與
首幀，這兩個平台會被相容性檢查排除，四平台就無法在同一條件下比較。
因此 V1 統一以**單張首幀**傳遞身份，角色特徵改寫入 `identity_description`
並帶進提示詞。這也是 I2V 實際的操作方式。

角色設定圖仍列入必備素材，但**不會上傳至平台**，僅供人工製作首幀時對照。

## 三類固定情境

### 1. 單角色 cinematic（`single_character_cinematic`）

測人物身份穩定、走動自然度、表情與慢速運鏡的跟隨能力。

| 鏡頭 | 內容 | 運鏡 |
|---|---|---|
| `bm_a1_walk_slow_push` | 角色 A 於雨夜天橋緩步走向鏡頭 | 慢速推軌，等速無晃動 |
| `bm_a2_closeup_expression` | 角色 A 特寫，表情由平靜轉為警覺 | 固定鏡位 |

兩顆刻意成對，用於評估**跨鏡頭身份一致性**。

### 2. 雙角色對話（`two_character_dialogue`）

測兩名角色是否被混淆、視線是否對上、表情演技，以及正反打之間的連戲。

| 鏡頭 | 內容 | 運鏡 |
|---|---|---|
| `bm_b1_two_shot_dialogue` | A 與 B 於便利商店對站，B 開口 | 固定雙人中景 |
| `bm_b2_ots_on_a` | 越過 B 的肩膀看向 A | 過肩，固定 |
| `bm_b3_ots_on_b` | 越過 A 的肩膀看向 B，與 B2 互為反打 | 過肩，固定 |

B2 與 B3 是完整的 shot-reverse-shot 配對。**角色互換服裝或臉部混淆是
本情境最重要的失敗模式。**

### 3. 高動態（`high_dynamic_action`）

人物、環境與運鏡同時運動，測時間穩定性與結構崩壞程度。

| 鏡頭 | 內容 | 運鏡 |
|---|---|---|
| `bm_c1_run_tracking` | 角色 A 於雨中巷弄奔跑，蒸氣、水花、招牌晃動 | 側向跟拍，速度匹配 |

## 生成規模

```
6 鏡頭 x 4 平台 x 3 候選 = 72 次人工生成
```

V1 Gate 只要求至少兩個平台，可先做 `6 x 2 x 3 = 36` 次再擴充。

## 記錄指標

### 單鏡頭（`variant_qc`）

| 欄位 | 含義 |
|---|---|
| `identity_consistency` | 單一鏡頭內角色身份是否穩定（臉部漂移屬此） |
| `temporal_stability` | 畫面在時間上是否穩定，有無閃爍變形 |
| `prompt_adherence` | 是否照著提示詞做 |
| `motion_quality` | 動作是否自然合理 |
| `camera_control` | 運鏡是否符合要求 |
| `facial_acting` | 表情演技（非對話鏡頭留空） |
| `artifact_severity` | 瑕疵嚴重度，**越高越糟** |
| `usable_without_repair` | 不需修補即可用 |
| `retries_to_usable` | 重試幾次才得到可用結果 |
| `human_correction_minutes` | 人工修正耗時 |
| `generation_seconds` | 平台生成耗時 |

### 跨鏡頭（`continuity_qc`）

| 欄位 | 含義 |
|---|---|
| `cross_shot_identity` | 兩顆鏡頭是否像同一個人 |
| `wardrobe_continuity` | 服裝是否連戲 |
| `location_continuity` | 場景是否連戲 |
| `lip_sync_quality` | 嘴型（非對話鏡頭留空） |

配對：`a2↔a1`、`b2↔b1`、`b3↔b2`、`c1↔a1`

### 評分規則

- 全部 0–100，**留空代表不適用，不可填 0**。0 分是「極差」，
  與「不適用」是兩件事，混用會把加權拉低。
- `artifact_severity` 方向相反，加權時以 `100 - severity` 計入。
- 成本（credits）目前無法由系統取得，於 attempt ledger 人工記錄。
  V1 不建置成本 UI。

### 適用性（依鏡頭而非情境）

`facial_acting` 依**鏡頭**判定，不依情境。`bm_a2_closeup_expression`
雖屬單角色情境，但它是特寫且明確要求表情變化，是表情演技的主要觀察
對象，必須評分。

| 鏡頭 | facial_acting |
|---|---|
| `bm_a2_closeup_expression` | **可評** |
| `bm_b1` / `bm_b2` / `bm_b3` | **可評** |
| `bm_a1_walk_slow_push` | n/a（遠景看不清臉） |
| `bm_c1_run_tracking` | n/a（高速運動） |

`lip_sync_quality` 於 V1 **一律 n/a**，權重為 0，不參與任何排名。

### 素材驗證

`check_assets` 會實際解碼檔案，而非只看大小：

- 必須能以 Pillow 解碼且為有效影像
- 首幀比例須為 9:16（容差 ±0.05）
- 短邊至少 512px
- 角色設定圖不上傳平台，不限比例

壞圖、假 PNG 與錯比例一律 fail-closed，不會進入派工。

### 素材內容血緣

派工時對**實際複製進 job package 的檔案**計算 SHA256，寫入：

- `job.json` 的 `reference_hashes`
- 請求本身的 `visual.reference_hashes`（因此納入 `request_hash`）
- `ReferenceAsset.file_hash`（每次登錄以檔案內容重算）

同一個 `asset_id` 換了圖片內容時，`request_hash` 必然改變，
產生新的 job package 目錄，**舊 manifest 保留**。
兩次生成用的不是同一張參考圖，這件事必須看得出來。

## 嘗試紀錄（Attempt Ledger）

`data/benchmark/v1/sheets/v1_attempts.csv`

**每按一次 Generate 就記一列，即使沒有產出影片。** 只統計成功匯入的候選
會嚴重高估平台表現：一個試十次才成功兩次的平台，若只看那兩支成品，
會看起來和一次就中的平台一樣好。

| 欄位 | 說明 |
|---|---|
| `shot_id` / `target_id` / `provider` / `model_id` | 身份 |
| `attempt_no` | 第幾次嘗試 |
| `status` | `success` / `failed` / `cancelled` |
| `generation_seconds` | 平台耗時 |
| `credits_used` | 消耗點數 |
| `output_file` | 產出檔名，失敗留空 |
| `variant_id` | 匯入後由 `sync` 回填，失敗列維持空白 |
| `failure_reason` | 失敗原因 |

**重試次數、耗時與成本一律以此為統計基礎**，不以匯入的候選數推算。
`sync` 只會補齊 `variant_id`，**不會刪除任何列**。

## 統計公式（預先註冊）

定義於 `pipeline/benchmark/aggregation.py`，版本 `v1.0`。
**這些公式在真人生成開始前就已寫死**，不得於看到結果後修改。
若要調整，必須重跑全部資料。

### 聚合方式

一律採**中位數**。平均會被單次崩壞或單次神來一筆拉走；
最佳值等於獎勵運氣，與「穩定產出可用鏡頭」的目標相反。

| 層級 | 公式 |
|---|---|
| candidate quality | 該候選的加權分數（權重見下） |
| 每 (shot, target) | 該組合所有候選 quality 的**中位數** |
| 每 target | 各鏡頭中位數的**中位數** |
| scenario quality | 該情境內各鏡頭中位數的**中位數** |
| scenario stability | 該情境內各鏡頭中位數的 **IQR**（樣本少於 4 時退回全距），**越小越穩定** |
| continuity | 該 target 所有配對加權分數的**中位數** |
| generation time | 成功嘗試的 `generation_seconds` **中位數** |
| credits | 全部嘗試的 `credits_used` **總和**，含失敗 |

### 核心指標

| 指標 | 定義 |
|---|---|
| **usable-shot rate** | `有可用候選的鏡頭數 / 有嘗試過的鏡頭數` |
| **retries per usable shot** | `總嘗試次數（含失敗） / 有可用候選的鏡頭數` |
| **human minutes per usable shot** | `人工修正總分鐘 / 有可用候選的鏡頭數` |
| **failure rate** | `失敗嘗試數 / 總嘗試數` |

無可用鏡頭時，`retries per usable` 與 `human minutes per usable` 回傳
空值而非 0，避免「完全失敗」看起來像「零成本」。

### benchmark 權重

與 ProductionProfile 的權重分開，因為 benchmark 衡量的是模型能力，
不是某支片的製作偏好：

```
identity_consistency 3.0    cross_shot_identity  3.0
temporal_stability   2.0    wardrobe_continuity  2.0
prompt_adherence     1.5    location_continuity  1.5
motion_quality       1.5    lip_sync_quality     0.0
camera_control       1.5
artifact_severity    1.5   （加權時取 100 - severity）
facial_acting        1.0
```

**`lip_sync_quality` 權重為 0**：V1 沒有音訊，也沒有嘴型 ground truth，
一律不評、不排名。

一個平均分高但每三次才成功一次的平台，在生產上不如平均分中等但穩定
命中的平台。

## Scenario-specific winner 判定

各情境獨立評選，供 `RoutingPolicy` 依 scenario 決定派工對象。
**不產生跨情境的總排名。**

| 標籤 | 適用情境 | 評選依據（依序） |
|---|---|---|
| **best for character dialogue** | 雙角色對話 | `continuity_identity` → `identity_consistency` → `facial_acting` → usable-shot rate |
| **best for cinematic camera** | 單角色 cinematic | `camera_control` → `temporal_stability` → usable-shot rate |
| **best for anime-comic motion** | 單角色 + 對話 | `motion_quality` → `artifact_cleanliness` → `identity_consistency` |
| **best for high-dynamic action** | 高動態 | `temporal_stability` → `artifact_cleanliness` → `motion_quality` |
| **best for low-retry production** | 全部情境 | usable-shot rate → retries per usable → human minutes per usable |

同一 target 可同時是多個標籤的贏家，也可能一個都不是。

### Tie-break

上述依據全部相同時，依序比較（皆為越小越好）：

```
retries_per_usable → human_minutes_per_usable
→ median_generation_seconds → target_id（字典序，確保結果可重現）
```

缺資料的維度一律排在最後，不因缺漏而意外勝出。

**報表不含跨情境的總冠軍欄位。** 這不是疏漏，是刻意的：一個在對話戲
最強的模型可能在高動態場景崩壞得最嚴重，平均成一個分數會抹掉這個資訊。

## V1 Gate 判定門檻

全部滿足才算 V1 通過：

| # | 門檻 | 驗證方式 |
|---|---|---|
| 1 | 至少 **2 個平台**完成全部 6 顆鏡頭的真實生成 | `benchmark_v1.py status` 的候選統計 |
| 2 | 每個核心 scenario 至少有 **2 個候選**可比較 | 同上，依 scenario 分組 |
| 3 | 資料完整匯回 `AssetVariant` 與 QC，無 orphan | `status` 的可用鏡頭統計；orphan 數為 0 |
| 4 | 能以選定素材完成第一支 **60–90 秒** AI 漫劇 | 組裝 preflight 通過並產出 MP4 |
| 5 | 人工測試未破壞 immutable lineage | 每個候選都有 `job_id` 與非空 `prompt_snapshot` |
| 6 | 四個平台的 provisional 規格已校正或明確標記為仍未驗證 | catalog YAML 的 notes 更新 |

第 5 項特別重要：未提供 `request_hash` 的匯入會被標記為 unlinked、
血緣留空，這類候選**不得計入 benchmark 結論**。

## 產出

| 檔案 | 用途 |
|---|---|
| `pipeline/benchmark/v1_pack.py` | 固定測試案例，唯一事實來源 |
| `data/benchmark/v1/assets/` | 人工準備的參考素材 |
| `data/projects/benchmark_v1/job_packages/` | 各平台 job packages |
| `data/benchmark/v1/sheets/v1_variant_scores.csv` | 單鏡頭評分表 |
| `data/benchmark/v1/sheets/v1_continuity_scores.csv` | 跨鏡頭連戲評分表 |

操作步驟見 [v1-sop.md](v1-sop.md)。
