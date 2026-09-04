# E0 Revival Baseline Stage Review

Scope: 復甦 2026-06 停擺的專案基線、建立顯式版本化 schema migration 機制、
建立分層 CI，作為後續 Narrative/Shot 架構改造的地基。

## Round 1

1. 專案自 2026-06-26 停擺約 10 週，基線是否仍可建置與執行完全未知。
2. 六月留下三個 stacked PR（#2 草稿 / #3 / #4），`main` 落後 5 個 commits。
3. `db/migrations/` 僅有 `.gitkeep`，專案沒有任何 migration 工具。
4. `SQLModel.metadata.create_all()` 只建立缺少的表，不會對既有表新增欄位。
5. 既有 `test.db` 的 `production_artifacts` 只有 23 欄，新增模型欄位會導致
   SELECT 失敗於 "no such column"，且是在功能看似正常之後才爆發。
6. 沒有任何 CI，PR 不受任何自動檢查保護。
7. `requirements.txt` 不含 `pytest`，測試無法在乾淨環境執行。
8. 專案沒有 `pytest.ini` / `pyproject.toml` / `conftest.py` 等測試設定。
9. README 未載明 `smoke_project_store.py` 必須在 `worker` 容器執行；在 `api`
   容器會因缺少 worker 模組而失敗。
10. `db/` 目錄未被任何 Dockerfile COPY，migration 若置於該處在容器內不可見。

## Round 2

1. 不沿用六月的 stacked PR 拓樸。最上層分支已線性包含全部 5 個 commits，
   改以單一 revival baseline 整合，避免舊 PR 結構成為現在的架構限制。
2. 拒絕「比對 model 欄位後自動 ALTER TABLE」的做法。該做法在 rename、
   型別變更、索引、約束與回退情境下會失控，且會偷改 production schema。
3. 改採顯式版本化 migration：每個版本是一個明確檔案，含 `upgrade` 與
   `downgrade`，版本號連續且不重複。
4. 不引入 Alembic。MVP 階段過重，但保留未來遷移路徑。
5. Migration 置於 `pipeline/migrations/` 而非 `db/migrations/`。`pipeline` 已
   被三個映像 COPY，如此不需修改任何 Dockerfile，在復甦階段不引入額外風險。
6. 保留 `create_all()` 負責初始建表，migration 只負責增量變更。兩者職責分離。
7. `v0001_baseline` 刻意為 no-op：此時 schema 由 `create_all()` 建立，全新
   與既有資料庫套用後結果一致，僅作為後續增量的版本基準。
8. 應用啟動只做只讀檢查。schema 落後時記錄警告但不阻斷啟動，且絕不自動套用。
   強制把關交給 CI 的 `migrate.py check`。
9. `status()` 必須是純只讀操作，不得順手建立 `schema_version` 表，否則
   「檢查」本身就變成了「改寫」。
10. CI 分層：PR 走 native Python 求快，Docker 整合測試不在每個 PR 執行，
    避免小改動也要重建 ASR 與 FFmpeg 映像。

## Round 3

1. Migration runner 提供方言感知輔助函式（`table_exists`、`column_exists`、
   `json_type`、`timestamp_type`），供各 migration 明確呼叫，不做自動推導。
2. 每個 migration 在獨立交易中套用，失敗時附上版本標籤再拋出。
3. `downgrade` 由高版本往低版本執行，順序與 `upgrade` 相反。
4. 探索階段即驗證版本號重複，避免兩個檔案宣告同一版本而靜默覆蓋。
5. ASR 整合測試獨立為單一 job，其映像含 `faster-whisper`，建置時間遠高於
   其他服務，不應阻塞核心整合測試。
6. `smoke_lava_settings.py` 自行覆寫 `DATA_DIR`，workflow 不再重複設定該
   環境變數，避免留下無效且誤導的設定。
7. Postgres 的 `review_status` 是 native enum 型別，SQLite 端則為 varchar。
   E1 新增的表一律使用 `VARCHAR` 搭配 Python enum 驗證，不建立 native enum，
   否則未來新增狀態需 `ALTER TYPE ADD VALUE`，該語句無法在交易內執行也無法回退。
8. README 補上 smoke 對應容器的表格與 migration 使用說明。
9. 新增 `requirements-dev.txt` 分離開發依賴，執行期映像不安裝。
10. PR CI 的每個步驟先在乾淨 `python:3.12-slim` 容器實地執行驗證，
    避免推送後才發現 workflow 失敗。

## Verification

| 項目 | 結果 |
|---|---|
| `smoke_test.py` (api) | PASS |
| `smoke_navigation_ui.py` (api) | PASS |
| `smoke_migrations.py` (api) | PASS |
| `smoke_project_store.py` (worker) | PASS |
| `smoke_lava_settings.py` (worker) | PASS |
| `smoke_asr.py` (asr-worker) | PASS |
| Migration upgrade/downgrade (PostgreSQL) | PASS |
| Migration upgrade/downgrade (SQLite) | PASS |
| `migrate.py check` 落後時 exit code | 1 |
| `migrate.py check` 最新時 exit code | 0 |
| `status()` 未建立 schema_version 表 | PASS |
| 既有 Postgres 專案資料 | 未受影響 |
| PR CI 步驟於乾淨 Python 容器 | PASS（pytest 6 passed） |
| 架構檢查：無自動 ALTER TABLE | PASS |
| 架構檢查：未引入 Alembic | PASS |
| 架構檢查：無 schema 自動推導 | PASS |
| 新增檔案中文檔頭 | 8/8 |
| 新增檔案無 TODO 佔位 | PASS |

## Known Issues (不阻塞 E0，另行處理)

1. LAVA 無法呼叫真實 LLM：OpenRouter 回 402（餘額用盡）、Google 回 404
   （`gemini-2.0-flash` 於 v1beta 已不存在）。確定性 fallback 正常運作，
   因此冒煙測試仍通過。E7 進行 Narrative 自動規劃前必須先解決。
2. `tests/integration/` 目錄為空，單元測試僅涵蓋 3 個模組。
3. 專案仍無 pytest 設定檔，測試依賴 `python -m pytest` 將 cwd 加入 `sys.path`
   的行為。
