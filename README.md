# AI Agent 基金經理人（2026 玉山人工智慧公開挑戰賽・初賽）

多個 AI Agent 分工合作，每個交易日讀取主辦方的資料包（DataPack），產生一份合格的 **D-Plan**（當日投資決策 JSON），由隊員上傳到後台並送出。
初賽期間 10/26 ～ 11/27，主題「主動台灣優質動能精選」。

**原則**

- 決策內容一律由程式與 Agent 產生，**不得手改 D-Plan JSON**；要調整就改程式或設定後重跑。
- 金鑰不進 repo（`.env` 已列入 `.gitignore`）。`config/settings.yaml` 是全隊共用的正式設定，只寫金鑰的環境變數名稱、不寫金鑰本身。
- 策略參數在初賽開始前依回測選定，初賽期間不為了個別交易日的持股或績效調整；要改就改程式或設定並 commit 寫明原因。
- 上傳與送出由隊員手動操作。

---

## 1. 安裝

需要 **Python 3.10 以上**（開發環境 3.11）與 Git。

### Windows（PowerShell）

```powershell
git clone https://github.com/Chris-Chen-rin/Fintech.git
cd Fintech
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### macOS／Linux

```bash
git clone https://github.com/Chris-Chen-rin/Fintech.git
cd Fintech
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

以下指令以 Windows 寫法為例；macOS／Linux 把 `.venv\Scripts\python.exe` 換成 `.venv/bin/python`。

### 設定檔

`config/settings.yaml` 已在 repo 裡，是全隊共用的正式設定（隊伍代號、主題、風控、策略參數、Gemini 模型配置），clone 下來就能用，**不要再從範本複製覆蓋**。

- 要修改請 commit，D-Plan 記錄的程式版本才會跟著更新。
- `config/settings.example.yaml` 是說明各欄位的範本，預設全部用規則版 Agent，測試也使用這份。
- 想用個人設定試驗時，另存一份檔案並用 `--settings 路徑` 或環境變數 `AGENT_SETTINGS` 指定，不要改動共用的那份。

### 設定金鑰

在專案根目錄建立 `.env`（不會進 repo）：

```
GEMINI_API_KEY=你的金鑰
```

也可以改用環境變數，或在 Colab 的 Secrets 設定同名金鑰。程式依序從環境變數、Colab Secrets、`.env` 讀取。

### 確認安裝

```powershell
.venv\Scripts\python.exe -m agent doctor     # 設定、金鑰、各 Agent 模型與今日 LLM 用量、目前的繳交時段
.venv\Scripts\python.exe -m pytest -q        # 全部測試應通過
.venv\Scripts\python.exe -m agent run --mock-first-day --no-out --rules-only   # 用規格書的首日資料包試跑
```

第一次執行會連到證交所、櫃買中心、公開資訊觀測站抓行情與財報（有請求間隔，約數分鐘），之後都從 `cache/` 讀取。

---

## 2. 每天怎麼交件

| 時間（台北） | 動作 |
|---|---|
| T-1 19:30 後 | 到後台「文件繳交」下載資料包，放進 `inbox/` |
| 接著 | 執行 `.venv\Scripts\python.exe -m agent run` |
| 通過檢查後 | 上傳 `out/D-Plan_<隊伍代號>_<交易日>.json`，確認收件層檢核通過後**按送出**，再到「當日委託」核對 |
| T 08:55 | 繳交截止（以最後一次送出的版本為準） |

- 程式會在 `inbox/` 找 `trade_date` 相符的資料包，跑完五個 Agent、風控與檢查器；**檢查器沒有錯誤才會複製到 `out/`**。
- 每次執行的完整紀錄（資料包、各 Agent 輸入輸出、LLM 原始回應、D-Plan、檢查報告）存在 `archive/<交易日>/<執行時間>_<evening|morning>/`。
- LLM 失敗或額度用完時會自動依序改用備援模型、最後改用規則版，D-Plan 照樣產生；整段 LLM 連不上而拖太久時，改跑 `run --rules-only`。

---

## 3. 系統架構

```
資料包 + 行情／財報／月營收／重大訊息（官方來源）
   │  工具計算所有數字（事實層，每個數字都可回對來源快照）
   ▼
① 情報 Agent ── 重大訊息 → 事實
② 市場策略 Agent ── 多空判斷、現金區間、加減碼方向
③ 研究 Agent ── 逐檔看法與分數（依產業分批並行）
④ 反方審查 Agent ── 挑出論據薄弱或沒解釋矛盾的推論，退回研究 Agent 重寫
⑤ 投資組合經理 Agent ── 買賣名單；權重與股數由程式依現金目標計算
   │
   ▼
風控閘門 → 組裝 D-Plan → 檢查器（比照主辦方規則）→ out/
```

- **工具算數字、LLM 做判斷寫理由、程式驗證契約**：LLM 寫的數字必須出自已登錄的事實，否則退回重做。
- 每個 Agent 都有 LLM 版與規則版：主模型 → 備援模型 → 規則版，任一層失敗就往下降級。
- LLM 額度控管：每次執行與每日的呼叫上限、稀缺模型（3.8 Flash）每日上限，計數存在 `cache/llm_calls.json`。

### 投資策略

依 2026/4/1～10/22 規則版回測選定（粗搜尋 27 組、細搜尋 27 組，挑前後段都穩定、相鄰參數也表現好的組合），初賽期間不調整。

| 項目 | 目前設定 | 設定位置 |
|---|---|---|
| 核心選股池 | 近四季獲利、市值前 100 名（約 98 檔），占淨值至少 60% | `theme` |
| 選股分數 | 品質 50%（ROE、EPS 年增、累計營收年增、現金流量）＋動能 50%（60 日與 5 日報酬各半） | `strategy.momentum`、`strategy.quality_share` |
| 市值錨定 | 市值前 3 大依市值比例配置（受個股上限限制），超過上限的部分給分數高的股票 | `strategy.anchor_top_n` |
| 持股與權重 | 22～28 檔；台積電上限 23%、其他 9%；現金 3%～15% | `risk` |
| 控制周轉成本 | 權重偏離 < 1.5% 不調整；每天最多出清 3 檔 | `risk.no_trade_band`、`strategy.max_swaps` |
| 個股分析原則 | 營收看年增與較上月、股價與基本面矛盾要找原因等 | `config/research_playbook.md`（改文字即可） |

---

## 4. 其他指令

```powershell
# 多日演練：用過去的真實行情連續跑（第 1 天建倉，之後用前一天的成交結果當持股）
# 預設規則版、不耗 LLM 額度；加 --llm 才用設定檔的模型（每天約 9～12 次呼叫）
.venv\Scripts\python.exe -m agent rehearse --start 2026-10-01 --end 2026-10-22

# 策略參數回測：config/optimize_grid.yaml 的所有組合（規則版、平行），含手續費、證交稅、出清上限
# --split 之前的期間挑參數、之後驗證；--review 用 1 次 LLM 呼叫挑選並寫理由
.venv\Scripts\python.exe -m agent optimize --start 2026-04-01 --end 2026-10-22 --split 2026-07-31 --workers 6

# 不呼叫 LLM，全部用規則版（LLM 故障或額度用完時）
.venv\Scripts\python.exe -m agent run --rules-only

# 單獨檢查一份 D-Plan（說明見 docs/D-Plan檢查器_README.md）
.venv\Scripts\python.exe -m dplan_checker out\D-Plan_TEAM_11770_2026-10-27.json --datapack inbox\DataPack.json --prices prev_close.json --team-id TEAM_11770
```

`rehearse` 與 `optimize` 是開發與研究用工具，**不用於產生 D-Plan**；每日交件只用 `run`。演練與回測結果在 `rehearsals/`（不進 repo）。回測的 `results.md` 附有欄位說明、參數名稱的讀法與自動統整（贏大盤組數、前後段排名相關係數、前後段都穩定的組合、各參數的平均影響）。回測第一次需要連網抓歷史行情與財報（4～10 月約 1 小時），之後可加 `--offline`。

---

## 5. 專案結構

```
Fintech/
├─ agent/
│  ├─ __main__.py            指令入口（doctor / run / rehearse / optimize）
│  ├─ run_daily.py           每日流程：資料 → 五個 Agent → 風控 → 組裝 → 檢查 → 輸出
│  ├─ settings.py            讀取與驗證設定檔
│  ├─ datapack.py            讀取資料包
│  ├─ trading_calendar.py    交易日與繳交時段
│  ├─ theme.py               核心選股池（主題成員）
│  ├─ risk_gate.py           風控閘門
│  ├─ orders.py              目標權重 → 委託股數
│  ├─ dplan.py               組裝 D-Plan
│  ├─ sources.py             來源登錄（每筆事實都能回對快照）
│  ├─ rehearse.py            多日演練（開發用，不用於產生 D-Plan）
│  ├─ optimize.py            策略參數回測搜尋（開發用，不用於產生 D-Plan）
│  ├─ agents/                五個 Agent（規則版、LLM 版）
│  ├─ framework/             Agent 契約、執行與重試、紀錄
│  ├─ llm/                   LLM 客戶端、JSON 修復、呼叫額度
│  └─ tools/                 行情、財報、重大訊息、量化指標、矛盾訊號
├─ dplan_checker/            D-Plan 檢查器（比照主辦方規則，可單獨使用）
├─ config/
│  ├─ settings.yaml          全隊共用的正式設定
│  ├─ settings.example.yaml  各欄位說明的範本（測試使用）
│  ├─ optimize_grid.yaml     回測的參數組合
│  └─ research_playbook.md   個股分析手冊（研究與反方審查 Agent 的提示詞會載入）
├─ datapack/                 範例資料包
├─ tests/                    pytest 測試
├─ docs/                     比賽規格、開發計畫、分析文件
└─ inbox/ out/ archive/ cache/ rehearsals/   執行時產生的資料（不進 repo）
```

資料根目錄預設是專案資料夾；要放到共用雲端硬碟（例如 Colab 掛載的 Google Drive）時，設定環境變數 `AGENT_DATA_ROOT` 或 `paths.data_root`。

---

## 6. 文件

| 文件 | 內容 |
|---|---|
| `docs/AI_Agent_開發計畫.md` | 目標、設計原則、模組、時程、每日作業流程 |
| `docs/D-Plan_撰寫指南_v4.3.md`、`docs/D-Plan.schema_v4.3.json` | D-Plan 格式規格 |
| `docs/DataPack_規格書_v4.3.md` | 資料包規格 |
| `docs/D-Plan檢查器_README.md` | 檢查器的檢查項目與用法 |
| `docs/多Agent流程圖.md` | Agent 協作流程 |
| `docs/投資主題影響分析.md` | 不同投資主題對專案的影響 |
| `docs/外部資源_README.md` | 外部資料來源 |
