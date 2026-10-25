# 外部資源清單（依初賽多 Agent 計畫）

> 更新日期：2026-10-25。「已確認」表示 10/25 在本機實際連線成功並看過回傳欄位；「待確認」表示尚未實測。
> 相關文件：[AI_Agent_開發計畫.md](AI_Agent_開發計畫.md)、[多Agent流程圖.md](多Agent流程圖.md)。

## 1. 需要準備的清單（依急迫程度）

| 項目 | 用途 | 期限 | 負責 |
|---|---|---|---|
| 參賽者後台帳號（玉山 AI 競技場） | 每天下載資料包、上傳並送出 D-Plan | 已有 | |
| 隊伍代號 | 寫入 `config/settings.yaml` | 已有，待填入 | |
| ETF 投資主題、標的清單、理念摘要 | 所有 Agent 的提示詞與選股依據 | **10/26 晚上前** | |
| GitHub 私人 repo | 放程式碼；`code_version` 用 commit 識別 | 10/26 前 | |
| 共用 Google Drive 資料夾 | 放資料包、D-Plan、每日存檔 | 10/26 前 | |
| LLM 供應商與 API 金鑰 | LLM 版 Agent | 越早越好；首日可先用規則版 | |
| 比賽 Slack | 接收主辦方公告（休市、規則異動） | 已可加入 | |

## 2. 帳號與服務

| 服務 | 用途 | 費用 | 備註 |
|---|---|---|---|
| 玉山 AI 競技場後台 | 資料包下載、D-Plan 上傳與送出、合規狀態、排行榜 | 免費 | 上傳與送出目前只能手動 |
| GitHub | 程式碼版本控制、多人協作（branch、PR） | 免費（私人 repo） | Colab 下載私人 repo 需要個人存取權杖，存放在 Colab Secrets |
| Google Colab | 雲端執行每日流程 | 免費版即可 | 主機在海外、時區 UTC（程式已處理時區） |
| Google Drive | 共用資料夾 | 免費 15 GB | 每日存檔量預估每天數 MB |
| LLM API | 各 Agent 的 LLM 版 | 依供應商計費 | 供應商待定；主辦方提供完成 Google Cloud 課程的前 750 隊 20 美元額度（僅限 Google Cloud 上的 LLM） |
| Internet Archive（web.archive.org） | 新聞來源存檔，填入 `archive_url` | 免費 | 存檔有頻率限制；大量使用可申請帳號金鑰 |

## 3. 資料來源

### 3.1 主辦方提供（已在 repo 內或每日下載）

| 資料 | 位置 | 用途 |
|---|---|---|
| 資料包（DataPack） | 後台「文件繳交」頁，每天下載 | 前日結算持股、現金、可交易淨值、違規紀錄 |
| 150 檔標的清單 | `dplan_checker/data/universe_150.json` | 可投資範圍 |
| 30 檔主動型 ETF 清單 | `dplan_checker/data/active_etfs.json` | 主動投資比率比較對象 |
| D-Plan schema v4.3 | `dplan_checker/data/D-Plan.schema_v4.3.json` | 格式檢查 |

### 3.2 公開資料 API

D-Plan 的 `authority` 欄位要依來源填寫，下表一併列出。

| 資料 | 網址 | authority | 狀態 | 備註 |
|---|---|---|---|---|
| 上市個股每日收盤（全部） | `https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` | twse | 已確認 | 只有最近一個交易日；欄位 `Code`、`ClosingPrice`、`TradeVolume`、`TradeValue`；日期為民國格式（`1151022`） |
| 上市指數 | `https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX` | twse | 已確認 | 含加權指數與各類指數收盤、漲跌幅 |
| 上市本益比、殖利率、股價淨值比 | `https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL` | twse | 已確認 | 價值、高股息主題需要 |
| 上市月營收 | `https://openapi.twse.com.tw/v1/opendata/t187ap05_L` | mops | 已確認 | 含當月營收、月增率、年增率、`產業別` |
| 上市重大訊息 | `https://openapi.twse.com.tw/v1/opendata/t187ap04_L` | mops | 已確認 | 情報 Agent 的原文來源 |
| 上市三大法人買賣超 | `https://www.twse.com.tw/rwd/zh/fund/T86?date=YYYYMMDD&selectType=ALLBUT0999&response=json` | twse | 已確認 | 非交易日回傳「沒有符合條件的資料」 |
| 上市個股月成交資訊（歷史） | `https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date=YYYYMMDD&stockNo=代號&response=json` | twse | 已確認 | 一次一檔一個月；計算動能需要歷史價格。網站有頻率限制，請求間隔建議 3 秒以上並快取 |
| 上櫃個股每日收盤（全部） | `https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes` | tpex | 已確認 | 欄位 `SecuritiesCompanyCode`、`Close`、`Average`（均價）、`TradingShares` |
| 上櫃月營收 | `https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O` | mops | 已確認 | 欄位同上市月營收 |
| 上櫃三大法人 | `https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading` | tpex | 待確認 | API 文件中有此端點，尚未看過資料 |
| 上櫃指數 | `https://www.tpex.org.tw/openapi/v1/tpex_index` | tpex | 待確認 | 同上 |
| 上櫃本益比、殖利率 | 櫃買中心 OpenAPI | tpex | 待確認 | 尚未找到對應端點 |
| 期貨每日行情（含台指期） | `https://openapi.taifex.com.tw/v1/DailyMarketReportFut` | taifex | 待確認 | API 文件已確認存在；是否含夜盤需實測 |
| 期貨法人未平倉 | `https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate` | taifex | 待確認 | |
| 匯率 | `https://openapi.taifex.com.tw/v1/DailyForeignExchangeRates` | taifex | 待確認 | |
| 美股指數、台股 ADR | `https://query1.finance.yahoo.com/v8/finance/chart/代號?range=5d&interval=1d` | vendor | 已確認 | 非官方介面，可能變動；代號如 `^SOX`、`^NDX`、`^GSPC`、`TSM`、`UMC` |
| 台股新聞 | `https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=30` | media | 已確認 | 非官方介面；欄位 `newsId`、`title`、`publishAt`（Unix 時間）；**必須附 `archive_url`** |
| 網頁存檔查詢 | `https://archive.org/wayback/available?url=網址` | — | 已確認 | 查詢既有存檔 |
| 網頁存檔建立 | `https://web.archive.org/save/網址` | — | 待確認 | 有頻率限制 |
| 第三季財報 | 公開資訊觀測站 | mops | 待確認 | 11/14 前公布，成長主題需要 |
| 主動型 ETF 每日持股 | 各投信官網的持股揭露 | fininst | 待確認 | 沒有統一 API，需逐家調查；主動投資比率監控需要 |

### 3.3 使用注意

- **日期格式**：證交所與櫃買中心多用民國年（`1151022` = 2026-10-22）；數字常為字串且可能含逗號、空白。
- **時點**：每日收盤資料約在下午公布，法人資料約在傍晚公布；`content_as_of` 要填資料描述的時點，不是抓取時間。
- **10/23（五）休市**：首日（10/27）使用的前一交易日收盤是 10/22。
- **頻率限制**：`www.twse.com.tw` 的非 OpenAPI 端點請求過快會被暫時封鎖；所有原始回應存入 `cache/`，同一天不重抓。
- **非官方介面**（Yahoo、鉅亨網）：隨時可能失效，失效時對應的事實直接略過，不影響主流程。

## 4. 軟體套件

| 套件 | 用途 | 狀態 |
|---|---|---|
| Python 3.10 以上 | 執行環境（本機 `.venv` 為 3.11） | 已有 |
| `jsonschema` | D-Plan schema 檢查 | 已安裝 |
| `PyYAML` | 讀設定檔 | 已安裝 |
| `pytest` | 測試 | 已安裝 |
| `requests` 或 `httpx` | 資料抓取 | 待加入 |
| `pandas` | 量化工具計算 | 待加入 |
| LLM SDK（例如 `google-genai`、`openai`、`anthropic`） | LLM 介面 | 依供應商決定後加入 |

## 5. LLM 用量估計

依多 Agent 設計，每次完整執行大約：

| Agent | 呼叫次數 | 說明 |
|---|---|---|
| 情報 | 1–3 | 新聞與重大訊息分批 |
| 市場策略 | 1 | |
| 研究 | 4–6 | 依產業分組並行 |
| 反方審查 | 1–2 | 含退回後重審 |
| 投資組合經理 | 1–4 | 含風控閘門退回最多 3 次 |
| 合計 | 約 8–16 次 | 粗估輸入 10–25 萬 tokens、輸出 2–4 萬 tokens |

初賽 24 個交易日，晚間版與早盤版各一次，最多約 48 次完整執行，**總量粗估輸入 500–1,200 萬 tokens、輸出 100–200 萬 tokens**。
實際費用請用供應商公布的單價計算：`輸入量 × 輸入單價 ＋ 輸出量 × 輸出單價`。
降低成本的方法：研究與情報這類大量摘要使用便宜模型、早盤版只在隔夜資料有明顯變化時才跑完整流程、資料重複使用快取。

## 6. 人力與時間

| 工作 | 時間 | 頻率 |
|---|---|---|
| 下載資料包、執行、上傳、送出、確認當日委託 | 約 15–20 分鐘 | 每個交易日的前一晚 19:30 後 |
| 早盤版更新 | 約 10–15 分鐘 | 選做，交易日 06:00–08:55 |
| 看資料包回饋（委託是否作廢、合規紀錄） | 約 5 分鐘 | 每天 |

## 7. 網路環境

- 本機（台灣）：10/25 實測證交所、櫃買中心、期交所、Yahoo、鉅亨網、Internet Archive 都能連線。
- Colab（海外主機）：**尚未實測**。若台灣官方資料源連不上，備案是在本機抓取後放到共用 Drive，Colab 只負責後續的 Agent 流程。
