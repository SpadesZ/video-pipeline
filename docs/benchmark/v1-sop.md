# V1 Benchmark 操作 SOP

本文是給人操作的流程。系統只做到產出 job package 與接收結果，
**真實影片生成必須由你在各平台網頁手動完成**。

規格定義見 [v1-spec.md](v1-spec.md)。

---

## 步驟 1：準備參考素材

```powershell
docker compose run --rm api python scripts/benchmark_v1.py check
```

指令會列出需要的檔案與存放目錄（`data/benchmark/v1/assets/`）。
共 8 個檔案：

| 檔名 | 內容 | 是否上傳至平台 |
|---|---|---|
| `char_a_sheet.png` | 角色 A 設定圖：短黑鮑伯頭、深藏青風衣、二十多歲女性 | 否，僅供你製作首幀時對照 |
| `char_b_sheet.png` | 角色 B 設定圖：中長棕髮、淺灰針織外套、三十多歲男性 | 否 |
| `frame_a1.png` | A 站在雨夜天橋遠端，全身入鏡 | **是** |
| `frame_a2.png` | A 的臉部特寫，雨滴與霓虹反光 | **是** |
| `frame_b1.png` | A 與 B 在便利商店內對站，雙人中景 | **是** |
| `frame_b2.png` | 越過 B 的肩膀看向 A，A 的臉清晰 | **是** |
| `frame_b3.png` | 越過 A 的肩膀看向 B，與 b2 互為反打 | **是** |
| `frame_c1.png` | A 在雨中巷弄起跑，環境有動態元素 | **是** |

全部 **9:16 直式**。

首幀怎麼來不限：AI 生圖、手繪、實拍皆可。重點是**六張首幀裡的角色必須
看起來是同一個人**——benchmark 要測的是各平台維持身份的能力，
若起點就不一致，測到的會是你首幀的問題。

`frame_b2` 與 `frame_b3` 要真的是同一空間的正反打（軸線一致），
否則測不出平台的連戲能力。

素材備齊後再跑一次 `check`，顯示 `OK 全部就緒` 才往下。

---

## 步驟 2：建立專案並產出 job packages

```powershell
docker compose run --rm api python scripts/migrate.py upgrade
docker compose run --rm api python scripts/benchmark_v1.py build
```

`build` 會：

1. 登錄 8 個參考素材
2. 建立 `benchmark_v1` 專案（6 鏡頭、2 角色、3 場景）
3. 對四個平台**各自**產出 6 份 job package
4. 產生兩張空白評分表

只想先做兩個平台：

```powershell
docker compose run --rm api python scripts/benchmark_v1.py build --providers kling seedance
```

產出位置：

```
data/projects/benchmark_v1/job_packages/<shot_id>/<provider>/<hash>/
    job.json      機器可讀的完整請求
    prompt.txt    直接複製貼上用
    refs/         首幀圖
    README.md     該平台的操作說明與參數
```

也可從專案頁 `Download job packages` 打包下載。

---

## 步驟 2.5：確認各平台的實際版本

`pipeline/benchmark/catalog/v1_targets.yaml` 目前四個 target 的
`model_version` 都是空的，`provisional` 皆為 `true`。

登入各平台後，把介面上實際選到的版本填進去：

```yaml
  - target_id: kling_default
    provider: kling
    model_id: kling-video
    model_version: "2.1"        # 平台顯示的版本
    ui_label: "Kling 2.1 標準模式"  # 介面上的選項名稱
    provisional: false           # 確認後改為 false
```

**若平台同時提供多個版本且你打算都測，為每個版本各建一個 target**
（例如 `kling_v1_6` 與 `kling_v2_1`）。只記「這是 Kling 生的」，
日後無從得知當時用的是哪一版。

## 步驟 3：到各平台生成

**每顆鏡頭每個 target 生成 3 次**，全部保留，包含失敗的。
重試次數本身就是要測的指標，不要只留好的那一支。

### 每按一次 Generate 就記一列

打開 `data/benchmark/v1/sheets/v1_attempts.csv`，**每次按下生成就填一列**，
即使沒有產出任何影片：

| status | 什麼時候用 |
|---|---|
| `success` | 有產出影片 |
| `failed` | 平台報錯、內容政策拒絕、生成崩壞到不可用 |
| `cancelled` | 你自己中斷 |

`generation_seconds`、`credits_used` 一律要填，失敗的也要——失敗同樣
花時間和點數。`failure_reason` 寫清楚原因。

這張表是重試次數、耗時與成本的**唯一統計來源**。只看成功的候選會嚴重
高估平台表現：試十次成功兩次，和一次就中，成品看起來一樣好。

實際重試超過預留的三列時直接增列，不要覆蓋既有列。

對每個 `<shot_id>/<provider>/<hash>/` 目錄：

1. 開啟 `README.md`，照裡面的步驟操作
2. 開啟平台網頁，選圖生視頻（image-to-video）
3. 上傳 `refs/` 裡的首幀
4. 貼上 `prompt.txt` 全文
5. 依 `README.md` 的「平台參數」設定片長與比例
   > 片長已依各平台單位換算，直接照填。Veo 的欄位是秒，其餘也是秒。
6. 生成，下載影片
7. **記下三件事**：實際片長、生成花了多久、用掉多少 credits

### 各平台重點

| 平台 | 注意事項 |
|---|---|
| **Kling** | 參考素材上限 4（provisional），本次只用 1 張首幀 |
| **Seedance** | 支援 reference video，本次不使用，只走 I2V |
| **Veo** | 片長上限 8 秒（provisional），本次 6 秒應在範圍內 |
| **Runway** | 參考素材上限 1（provisional），只能上傳首幀 |

**若某平台實際不支援 job.json 指定的參數**（例如片長選項只有 5 或 10 秒，
沒有 6 秒），**不要私自改內容**。改用最接近的選項，並在評分表 `notes`
記下實際使用的值。之後我們據此校正 catalog 的 provisional 規格。

檔名建議 `<shot_id>_<provider>_<候選編號>.mp4`，方便對應。

---

## 步驟 4：匯回系統

在專案頁 `benchmark_v1` 的 **Variants** 面板：

1. 選擇 Shot 與 Provider
2. 上傳該組合的 3 支影片（可一次多選）
3. **`request_hash` 欄位填入該 `job.json` 的 `request_hash`**

第 3 點很重要。填了才會綁定到正確的派工，血緣與片長落差比對才成立。
填錯或填成別的鏡頭的 hash 會被直接拒絕，不會猜測。

不填也能匯入，但會標記為 unlinked、血緣留空，**這類候選不計入
benchmark 結論**。

---

## 步驟 5：評分

```powershell
docker compose run --rm api python scripts/benchmark_v1.py sync
```

`sync` 會處理三張表：

- **評分表**：回填 `variant_id` 與檔名
- **連戲表**：依已選定（Select）的候選建立配對，填入 `variant_id` 與
  `ref_variant_id`。兩顆鏡頭都要先選定候選才會產生配對
- **嘗試紀錄**：為成功的列補上 `variant_id`。**不會刪除任何列**，
  失敗的嘗試維持原樣

連戲配對必須是同一個 target 的兩支影片。跨平台或跨版本的配對會在匯入時
被拒絕——那種分數無法歸因給任何一個比較對象。

用 Excel 或任何試算表打開，逐列填分：

- 全部 0–100
- **不適用就留空，不要填 0**。非對話鏡頭的 `facial_acting` 已預填 `n/a`
- `artifact_severity` 是越高越糟
- `usable_without_repair` 填 `yes` / `no`
- `generation_seconds`、`credits_used`、`retries_to_usable`、
  `human_correction_minutes` 依步驟 3 記下的填

跨鏡頭連戲填 `v1_continuity_scores.csv`，四組配對：

| 主鏡頭 | 對照 | 看什麼 |
|---|---|---|
| `bm_a2_closeup_expression` | `bm_a1_walk_slow_push` | 特寫與遠景是不是同一個人 |
| `bm_b2_ots_on_a` | `bm_b1_two_shot_dialogue` | 過肩鏡頭的 A 與雙人鏡頭的 A 一致嗎 |
| `bm_b3_ots_on_b` | `bm_b2_ots_on_a` | 正反打之間有沒有角色互換或場景跳動 |
| `bm_c1_run_tracking` | `bm_a1_walk_slow_push` | 高動態下身份還撐得住嗎 |

填完匯入：

```powershell
docker compose run --rm api python scripts/benchmark_v1.py import-scores
```

---

## 步驟 6：查看進度

```powershell
docker compose run --rm api python scripts/benchmark_v1.py status
```

顯示素材、鏡頭、派工、候選、已評分與可用鏡頭數。

---

## 步驟 7：校正 provisional 規格

實際操作後，把發現的真實限制寫回
`pipeline/capability/catalog/providers/<provider>.yaml`：

- 實際可選的片長選項
- 參考素材數量上限
- 支援的畫面比例
- 移除 `notes` 裡的 provisional 字樣，或改寫為實測結果

這是 V1 Gate 第 6 項。

---

## 步驟 8：組一支片

從各鏡頭選出最佳候選（Variants 面板的 `Select`），然後組裝。
目前分鏡是 6 顆 × 6 秒 = 36 秒，未達 60–90 秒。

要滿足 V1 Gate 第 4 項，有兩個做法：

- 沿用同一組角色與首幀風格，自行擴充分鏡到 60–90 秒
- 或以 benchmark 素材為基礎，另建一個實拍用專案

**不要為了湊長度而改動 `benchmark_v1` 的分鏡。** 它是固定測試案例，
改了之後跨輪次的資料就不能比較了。

---

## 常見狀況

**某鏡頭在某平台顯示 Blocked 或 Incomplete**
→ 素材缺失或角色定義找不到。跑 `status` 看原因。系統刻意不送出殘缺的
job package。

**匯入時 request_hash 被拒**
→ hash 對應到的派工其專案、鏡頭或平台與你選的不符。確認是不是複製到
別的目錄的 `job.json`。

**同一支檔案匯入兩次**
→ 同專案同平台會判定重複，不會產生第二筆。不同平台的相同檔案會各自
保留，因為那是兩次獨立生成。

**想重跑某顆鏡頭的派工**
→ 直接重跑 `build`。內容沒變的話 job package 目錄不變（冪等）；
內容變了會產生新目錄，舊的 manifest 保留。
