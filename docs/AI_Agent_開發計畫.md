# AI Agent 開發計畫（初賽版，多 Agent 架構）

> 更新日期：2026-10-25（日）。所有時間為台北時間。
> 範圍：只處理初賽，目標是 **多個 Agent 每個交易日自行產生投資決策，並輸出合法的 D-Plan**。
> 不含：複賽、ETF 投資策略說明文件（另行撰寫）。
> 流程圖：[多Agent流程圖.md](多Agent流程圖.md)；外部資源：[外部資源_README.md](外部資源_README.md)；主題影響分析：[投資主題影響分析.md](投資主題影響分析.md)。

---

## 1. 目標與時程

| 時間 | 事項 |
|---|---|
| 10/23（五）、10/26（一） | 休市 |
| **10/26（一）19:30 ～ 10/27（二）08:55** | 第 1 個交易日（10/27）的 D-Plan 繳交時段；前一交易日是 10/22 |
| 10/27 ～ 11/27 | 每個交易日的前一日 19:30 ～ 當日 08:55 繳交（共 24 個交易日，**至少 22 天**成功繳交才列入排名） |

**完成的定義**：給定當天的資料包，執行一條指令（本機或 Colab 都可以），各 Agent 依序產生事實、市場判斷、推論與投資決策，
最後得到一份通過 `dplan_checker`（0 錯誤）的 D-Plan，人只需要上傳並按送出。

### 決策狀態

| 事項 | 狀態 | 怎麼處理 |
|---|---|---|
| ETF 投資主題與理念 | 待定（策略說明文件稍晚寫好）；不一定有主題代號清單 | `theme.mode` 五種：`tickers` 代號清單、`industries` 產業別、`rule` 量化條件（已持有用較寬鬆的退出門檻）、`llm` 賽前分類檔、`none` 不考慮主題（`agent/theme.py`）。影響見主題影響分析 |
| LLM 供應商 | 待定；可能五個 Agent 用不同模型 | `llm.default`／`llm.agents.<名稱>`／`llm.fallback.<名稱>` 分別設定；每個步驟依序試 [主模型, 備援模型, 規則版]。多家供應商時 D-Plan 的 `model_provider` 填 `other`，`model_version` 列出各 Agent 模型（例 `pm:claude-x;mk:gpt-x;rs:rule`），完整對照存 `archive/.../models.json`。**此填法建議先在 Slack 向主辦方確認** |
| ETF 名稱與核心選股池 | 暫定「主動台灣優質動能精選」 | `theme.mode: rule`：近四季歸屬母公司淨利 > 0、市值前 100 名（已持有放寬到 120 名）；核心池占淨值下限 60%，投資組合經理會限制非成員檔數 |
| LLM API 金鑰 | OpenRouter 已取得（免費額度每日 50 次） | 五個 Agent 的模型已寫進設定；LLM 版 Agent 實作前仍跑規則版 |
| 每日操作 | 晚上與早上都可以 | 晚間送基準版，早上視情況更新 |
| 隊伍代號 | 已有 | 寫進設定檔 |

---

## 2. 設計原則

1. **Agent 做投資判斷，程式負責數字與把關**：選股、加減碼、目標權重由投資組合經理 Agent 決定；程式只抓資料、計算數字、檢查規則、依官方公式換算委託股數。違規時退回 Agent 修正，不由程式代為決定。
2. **資訊傳遞依 D-Plan 分層規定**：每個 Agent 只讀上一層、輸出必須引用上一層的編號；Agent 不得自己產生數字型事實。
3. **每個 Agent 都有規則版**：介面與 LLM 版相同，用於開發初期、LLM 失敗時的降級。
4. **不合規就不輸出**：D-Plan 一律先通過風控閘門與 `dplan_checker` 才會寫到輸出資料夾。
5. **人不介入決策**：人只負責下載資料包、執行、上傳、送出。檢查沒過時修程式或設定後重跑，**不手改 JSON**。
6. **每次變更都是新版本**：參數與提示詞都算程式碼，`code_version` 自動取 git commit。
7. **每天存證**：原始資料快照、每個 Agent 的輸入輸出、D-Plan、檢查報告都存檔。
8. **環境無關**：同一份程式在 Windows 本機與 Colab（Linux、UTC 時區）都能跑。

---

## 3. 模組拆分

### 3.1 程式模組（不做投資判斷）

| 代號 | 模組 | 輸入 → 輸出 | 狀態 |
|---|---|---|---|
| M0 | 專案骨架與設定 | 設定檔、環境變數 → `Settings` | ✅ 完成 |
| M1 | 時間與交易日曆 | 現在時間、休市日 → 目標交易日、繳交時段、前一交易日 | ✅ 完成 |
| M2 | 資料包讀取 | DataPack → `Book`（現金、可交易淨值、持股、合規狀態） | ✅ `agent/datapack.py` |
| M3 | 資料工具 | 各資料源 → 結構化資料＋原始快照（行情、法人、月營收、重大訊息、美股、夜盤、新聞） | 🟡 行情（61 個交易日）與財報完成（`agent/tools/fundamentals.py`：公開資訊觀測站損益表／現金流量表彙總、月營收，本益比／股價淨值比、市值排名）；法人、重大訊息、美股、新聞在里程碑 D |
| M4 | 來源登錄 | 每次抓取 → `sources`（S、authority、url、時點、存檔連結） | ✅ `agent/sources.py` |
| M5 | 量化工具 | 行情與財務資料 → 數字型事實（漲跌、動能、營收年增、籌碼、估值、主題內排名） | 🟡 漲跌、5／20／60 日動能、漲跌家數；每檔一筆財報事實（EPS、近四季 EPS、淨利、營業現金流、營收年增、本益比、股價淨值比）。研究分數 = 0.5×品質（ROE、EPS 年增、累計營收年增、現金流量）＋0.5×動能，近四季虧損扣分 |
| M6 | Agent 協作框架 | 各 Agent 的輸入輸出契約（JSON 結構）、暫時代號、退回與重試、紀錄存檔 | ✅ `agent/framework/` |
| M7 | 風控閘門 | 投資組合經理的決策 → 通過，或違規清單（退回用） | ✅ `agent/risk_gate.py` |
| M8 | 委託推導 | 決策、淨值、收盤價、現持股 → `orders`（沿用 `dplan_checker.portfolio.target_shares`） | ✅ `agent/orders.py` |
| M9 | D-Plan 組裝 | 各層結果 → 統一編號、引用鏈、`agent_metadata`、檔名 | ✅ `agent/dplan.py` |
| M10 | 檢查與退回 | 檢查器報告 → 依錯誤所在的層退回負責的 Agent | ✅ `agent/run_daily.py`（目前退回投資組合經理） |
| M11 | LLM 介面 | 提示詞＋JSON 結構 → 結構化結果；可切換供應商；重試；記錄 token | 🟡 骨架完成（`agent/llm/`：OpenAI 相容〔OpenAI／xAI／DeepSeek／自架〕、Anthropic、Gemini、Mock；`agent/agents/llm_base.py`；metadata 計算）。尚未實際呼叫過 API |
| M12 | 每日執行入口 | 一條指令或 Colab notebook → D-Plan＋存檔 | 🟡 本機指令完成（`python -m agent run`）；Colab 在里程碑 B |
| M13 | 主動投資比率資料 | 30 檔主動型 ETF 前 15 大持股 → 風控閘門與檢查器 | |
| — | `dplan_checker` 檢查器 | D-Plan → 檢查報告 | ✅ 完成 |

### 3.2 Agent（每個都有 LLM 版與規則版）

| 代號 | Agent | 產出 | 規則版的做法 |
|---|---|---|---|
| A1 | 情報 Agent | 文字型 `observations`（新聞、重大訊息中的事實） | 不產生文字型事實，只用量化工具的事實 |
| A2 | 市場策略 Agent | `market_view` | 依大盤、美股、夜盤漲跌的門檻判定走向與現金區間 |
| A3 | 研究 Agent（依產業分組並行） | `inferences` | 依量化分數與門檻產生看法與模板文字 |
| A4 | 反方審查 Agent | 補強 `counter_evidence`、退回論據不足的推論 | 依反向指標（例如動能轉弱、法人賣超）自動填入反方訊號 |
| A5 | 投資組合經理 Agent | `decisions`、`no_trade_decisions`（動作、目標權重、資金調度、續抱理由） | 依分數排序選股、等權或分數加權、無交易帶、被動超限減碼 |

---

## 4. 實作順序

### 里程碑 A：規則版 Agent 跑通整條流程（目標：10/26 晚上首日交件）

| 順序 | 工作 | 說明 |
|---|---|---|
| A1 | M0、M1 | ✅ 完成 |
| A2 | M2 資料包讀取、M3 資料工具（行情）、M4 來源登錄 | ✅ |
| A3 | M6 Agent 協作框架 | ✅ |
| A4 | M5 量化工具（最小版） | ✅ |
| A5 | 五個 Agent 的規則版 | ✅ 首日演練：建倉 25 檔、姿態 `increase` |
| A6 | M7 風控閘門、M8 委託推導 | ✅ 含「成交價差 3% 現金不得變負」與「做不到姿態時請市場策略重新宣告」 |
| A7 | M9 D-Plan 組裝、M10 檢查與退回、M12 執行入口（本機） | ✅ 模擬首日資料包演練通過（檢查器 0 錯誤）；**待 10/26 晚上用真的資料包產生** |

**驗收**：用真的首日資料包產生 D-Plan，檢查器 0 錯誤，後台「收件層檢核全數通過」，「當日委託」與 D-Plan 相同。

### 里程碑 B：每天穩定交件（10/27 ～ 10/28）

| 順序 | 工作 |
|---|---|
| B1 | 投資組合經理規則版的日常調整：無交易帶、被動超限減碼、現金與檔數保護、資金調度標記 |
| B2 | 降級流程：任何 Agent 失敗都自動換成規則版 |
| B3 | M12 的 Colab 版（見第 5 節） |
| B4 | 用真的資料包回饋校正檢查器與風控閘門 |

### 里程碑 C：Agent 換成 LLM 版（LLM 決定後，目標 10/30 前）

依「對決策影響由大到小」的順序接上，每接一個都能單獨回退成規則版：

| 順序 | 工作 | 說明 |
|---|---|---|
| C1 | M11 LLM 介面 | 骨架已完成；剩下用真金鑰各打一次、確認回應格式與 token 欄位 |
| C2 | A5 投資組合經理 LLM 版 | 讓 Agent 真正做投資決策；風控閘門退回機制在此驗證 |
| C3 | A2 市場策略 LLM 版 | |
| C4 | A3 研究 LLM 版 | 依產業分組並行 |
| C5 | A4 反方審查 LLM 版 | |
| C6 | A1 情報 LLM 版、M3 新聞與重大訊息 | 程式驗證數字出現在原文 |

### 里程碑 D：決策品質（11/2 ～ 11/13）

| 順序 | 工作 |
|---|---|
| D1 | M3 擴充：法人買賣超、美股與 ADR、台指期夜盤、月營收（11/10 前公布 10 月營收）、第三季財報（11/14 前公布） |
| D2 | M5 量化工具強化：依主題調整因子 |
| D3 | M13 主動投資比率資料：接上風控閘門與檢查器 `--active-etf` |
| D4 | 早盤版流程：用隔夜資料重跑，有變化才重新送出 |

---

## 5. Colab 與共同編輯

### 5.1 建議做法：GitHub 放程式、Colab 只負責執行、Google Drive 放資料

| 放什麼 | 放哪裡 | 理由 |
|---|---|---|
| 程式碼（`.py` 模組、提示詞、設定範本、測試） | **GitHub 私人 repo** | 有版本紀錄，`code_version` 直接用 commit；多人修改靠 branch 與 PR |
| 每日執行 | **Colab notebook**（`notebooks/run_daily.ipynb`） | notebook 只做四件事：clone repo、安裝套件、掛載 Drive、執行 `run_daily` |
| 資料包、輸出、存檔 | **共用的 Google Drive 資料夾** | 隊友都看得到每天的資料包、D-Plan、各 Agent 紀錄與檢查報告 |
| API 金鑰 | **Colab Secrets**／本機 `.env` | 金鑰不進 repo、不寫在 notebook 裡 |

多 Agent 很適合分工：每位隊友負責一兩個 Agent 的提示詞與規則版，靠固定的 JSON 契約整合，互不干擾。

不建議把主要程式寫在 notebook 裡：多人同時編輯同一份 notebook 容易互相覆蓋，也無法精準對應 `code_version`。

Drive 資料夾規劃：

```
共用雲端硬碟/Fintech-Agent/
├─ inbox/                     每天從後台下載的資料包
├─ out/                       產出的 D-Plan（上傳用）
└─ archive/2026-10-27/        原始資料、各 Agent 輸入輸出、D-Plan、檢查報告
```

### 5.2 為了能在 Colab 跑，程式要遵守的事

1. **路徑**：資料根目錄由環境變數 `AGENT_DATA_ROOT` 決定（已實作）。
2. **時區**：所有時間明確使用台北時區（已實作）。
3. **金鑰**：依序從環境變數、Colab Secrets、`.env` 讀取（已實作）。
4. **套件**：`requirements.txt` 固定版本；相容 Python 3.10 以上。
5. **網路**：Colab 主機在海外。10/25 已在本機確認證交所、櫃買中心、期交所的 API 都能連上，**Colab 上仍需實測**；連不上時改在本機抓資料再放到 Drive。

### 5.3 其他選項

| 選項 | 適合情境 | 缺點 |
|---|---|---|
| GitHub Codespaces | 雲端完整 VS Code 環境 | 免費時數有限 |
| VS Code／Cursor Live Share | 兩人同時改同一份程式 | 主持人的電腦要開著 |
| Deepnote、Hex | 即時共同編輯 notebook | 免費方案有限制 |

---

## 6. 預定的專案結構

```
Fintech/
├─ agent/
│  ├─ settings.py          M0 ✅
│  ├─ trading_calendar.py  M1 ✅
│  ├─ datapack.py          M2
│  ├─ tools/               M3 資料工具、M5 量化工具
│  ├─ sources.py           M4
│  ├─ framework/           M6 契約、暫時代號、退回與重試、紀錄
│  ├─ agents/
│  │  ├─ intel.py          A1 情報
│  │  ├─ market.py         A2 市場策略
│  │  ├─ research.py       A3 研究
│  │  ├─ reviewer.py       A4 反方審查
│  │  └─ portfolio_manager.py  A5 投資組合經理
│  ├─ prompts/             各 Agent 的提示詞模板（改動即換版本）
│  ├─ risk_gate.py         M7
│  ├─ orders.py            M8
│  ├─ dplan.py             M9
│  ├─ llm/                 M11
│  └─ run_daily.py         M10、M12
├─ config/                 settings.example.yaml（進 repo）、settings.yaml（不進 repo）
├─ dplan_checker/          ✅
├─ notebooks/run_daily.ipynb
├─ tests/
└─ docs/
```

---

## 7. 每日作業流程

```powershell
# 演練（不需要資料包、不輸出到 out/）
.venv\Scripts\python.exe -m agent run --mock-first-day --no-out
# 正式：先把後台下載的資料包放進 inbox/
.venv\Scripts\python.exe -m agent run
```

正式執行會自動在 `inbox/` 找 `trade_date` 相符的資料包，通過檢查器才把 D-Plan 複製到 `out/`；
每次執行的資料包、各 Agent 的輸入輸出、風控結果、D-Plan、檢查報告都存在 `archive/<交易日>/<執行時間>_<evening|morning>/`。

| 時間 | 動作 |
|---|---|
| T-1 19:30 後 | 下載資料包放到 `inbox/` → 執行 `run_daily` → 檢查通過 → 上傳 `out/` 的 D-Plan 並**按送出** → 到「當日委託」確認 |
| T 06:00 ～ 07:30（選做） | 用隔夜資料再跑一次；有變化才重新上傳並送出（以最後一次送出的版本為準） |
| T 08:55 | 繳交截止 |

其他指令：

```powershell
# 用過去的真實行情連續演練多天（第 1 天建倉，之後每天用前一天的成交結果當持股；結果在 rehearsals/<起>_<迄>/summary.json）
# 預設用規則版、不耗 LLM 額度；加 --llm 才用設定檔的模型（每天約 9～12 次呼叫）
.venv\Scripts\python.exe -m agent rehearse --start 2026-10-01 --end 2026-10-22
# 回測搜尋策略參數（config/optimize_grid.yaml 的所有組合，規則版、離線平行；--review 用 1 次 LLM 挑選並寫理由）
.venv\Scripts\python.exe -m agent optimize --start 2026-04-01 --end 2026-10-22 --split 2026-07-31 --workers 6 --review
# 降級：LLM 故障或額度用完時，全部改用規則版
.venv\Scripts\python.exe -m agent run --rules-only
```

策略參數在 `settings.yaml` 的 `strategy`：動能天數與權重（`momentum`）、品質比重（`quality_share`）、市值前 N 大依市值比例配置（`anchor_top_n`，超過個股上限的部分給分數高的其他股票）、每日出清上限（`max_swaps`）。回測含手續費 0.1425%、證交稅 0.3%、無交易帶與每日出清上限，結果在 `rehearsals/optimize_<起>_<迄>/results.md`。

LLM 額度：Gemini Flash-Lite 約 500 次／天、3.8 Flash 約 20 次／天（太平洋時間午夜重置，約台北 15:00～16:00）。一次完整執行約 9～12 次，其中 3.8 Flash 2～3 次。
`llm.max_calls_per_run`（30）與 `llm.daily_call_limit`（150）、個別模型的 `daily_limit` 超過時改用下一個備援模型，最後改用規則版；計數存在 `cache/llm_calls.json`，`doctor` 會顯示今日已用次數。
**正式交件當天不要先試跑**，把額度留給正式執行。模型回傳的 JSON 有小錯（未跳脫的引號、多餘逗號、輸出被截斷）時會先自動修復再解析。

### 7.1 10/26 晚上首日驗收步驟

1. 19:30 前：`git pull`、`python -m agent doctor`（確認交易日 10/27、繳交時段、各 Agent 模型與金鑰）、`python -m pytest -q` 全過。
2. 19:30 後：後台下載資料包放到 `inbox/`，確認檔名的 `trade_date` 是 2026-10-27、`team_id` 是自己隊伍。
3. 執行 `python -m agent run`。看輸出中的財報覆蓋率（正常約 149/150）、主題成員數（約 100）、持股 20～30 檔、現金比落在市場策略區間內、檢查器 0 錯誤。
4. 若 LLM 階段失敗會自動改用備援模型、再改用規則版，D-Plan 照樣產生；`models.json` 記錄每一步實際用了哪個模型。若整段 LLM 都連不上而拖太久，改跑 `--rules-only`。
5. 若檢查器有錯誤：看 `archive/2026-10-27/<時間>_evening/check_report.txt`，修程式或設定後重跑，**不要手改 JSON**。
6. 上傳 `out/` 的 D-Plan → 收件層檢核通過 → **按送出** → 到「當日委託」核對檔數與股數。
7. 第二天起（10/27 晚上以後）：資料包的持股就是前一天的成交結果；市場策略會讀前一個交易日已通過的 D-Plan，判斷是否沿用前一天的多空看法（避免單日小波動就來回加減碼）。

## 8. 主要風險

| 風險 | 對策 |
|---|---|
| 首日來不及 | 里程碑 A 只用規則版 Agent，不依賴 LLM |
| 規則版是否算「AI Agent 自主決策」不明確 | 里程碑 C 優先接上投資組合經理 LLM 版 |
| LLM 決策違規 | 風控閘門退回修正；3 次仍違規改用規則版 |
| Agent 之間引用對不上 | 固定 JSON 契約、暫時代號由程式統一重新編號、檢查器最後把關 |
| LLM 成本與時間 | 便宜模型處理大量摘要；記錄 token；早盤版只在有變化時跑完整流程 |
| 主題遲遲未定 | 首日執行前至少要有主題名稱、標的清單與理念摘要 |
| Colab 連不上台灣資料源 | 盡早實測；備案是本機抓取後放到 Drive |
