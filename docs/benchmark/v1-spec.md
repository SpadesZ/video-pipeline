# V1 Benchmark Spec

Pack 版本：`v1.0`　專案 ID：`benchmark_v1`

## 目的

在不購買任何 API 的前提下，以人工操作四個平台，取得**可重複、可比較、
可追溯**的資料，回答一個問題：**哪一顆鏡頭該交給哪個平台拍。**

刻意不產生單一「全球最佳模型」排名。一個在對話戲表現最好的模型，
在高動態場景可能崩壞得最嚴重，把兩者平均成一個分數會抹掉這個資訊。

## 目標平台

`kling`、`seedance`、`veo`、`runway`

> **平台規格為 provisional。** catalog 中的片長範圍、參考素材上限與支援
> 比例皆為保守預設，**尚未經過實測**。V1 的任務之一就是校正這些值。
> 在校正前，不得將它們當作事實引用。

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
- 成本（credits）目前無法由系統取得，於評分表以 `credits_used` 人工記錄。
  V1 不建置成本 UI。

## 核心指標

判定依據**不是**「最好看的那一支」，而是：

| 指標 | 定義 |
|---|---|
| **usable-shot rate** | 至少有一個可用候選的鏡頭比例。分母為分鏡表全部鏡頭，含完全生不出東西的 |
| **retries per usable shot** | 每產出一顆可用鏡頭平均重試次數 |
| **human minutes per usable shot** | 每產出一顆可用鏡頭平均人工修正時間 |
| **cross-shot continuity** | 配對鏡頭的身份與場景連戲平均分 |
| **scenario 穩定性** | 同一平台在同情境下三個候選的分數離散程度 |

一個平均分高但每三次才成功一次的平台，在生產上不如平均分中等但穩定
命中的平台。

## Scenario-specific winner 判定

各情境獨立評選，供 `RoutingPolicy` 依 scenario 決定派工對象。
**不產生跨情境的總排名。**

| 標籤 | 評選依據（依序） |
|---|---|
| **best for character dialogue** | `cross_shot_identity` → `identity_consistency` → `facial_acting` → usable-shot rate |
| **best for cinematic camera** | `camera_control` → `temporal_stability` → usable-shot rate |
| **best for anime-comic motion** | `motion_quality` → `artifact_severity`（反向） → `identity_consistency` |
| **best for high-dynamic action** | `temporal_stability` → `artifact_severity`（反向） → `motion_quality` |
| **best for low-retry production** | usable-shot rate → retries per usable → human minutes per usable |

同一平台可同時是多個標籤的贏家，也可能一個都不是。

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
