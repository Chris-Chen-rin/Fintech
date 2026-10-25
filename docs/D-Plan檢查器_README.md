# D-Plan 檢查器（dplan_checker）

在把 D-Plan 上傳到參賽者後台**之前**，於本機完整檢查一次：格式、引用鏈、決策覆蓋、委託推導、
超賣、現金、持股檔數、個股權重、市場姿態一致性、主動投資比率與主題一致性。

目標只有一個：**不要因為文件問題被擋件、作廢或記警告**（累計 3 次警告取消資格）。

對應規格：`D-Plan.schema_v4.3.json`、`D-Plan_撰寫指南_v4.3.md`、`DataPack_規格書_v4.3.md`、
比賽辦法（1005 更新）、後台操作說明。

---

## 1. 安裝

專案根目錄已建立虛擬環境 `.venv`（Python 3.11）。重新建立的方式：

```powershell
cd D:\作業\碩一\Fintech
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

相依套件只有 `jsonschema`（執行）與 `pytest`（測試）。

---

## 2. 使用方式

### 2.1 命令列

```powershell
.\.venv\Scripts\python.exe -m dplan_checker D-Plan_TEAM_XXX_2026-10-28.json `
    --datapack DataPack.json `
    --prices prices.json `
    --team-id TEAM_XXX
```

| 參數 | 必要性 | 說明 |
|---|---|---|
| `dplan` | 必填 | 要檢查的 D-Plan JSON 檔 |
| `--datapack` | **強烈建議** | 當日從後台「文件繳交」頁下載的資料包。沒給的話，所有部位相關檢查（覆蓋、推導、超賣、現金、檔數、權重、姿態）都會略過 |
| `--prices` | 有新買進時必填 | 前日收盤價。資料包只有已持有標的的收盤價，新買進（BUY）的標的要從這裡補 |
| `--team-id` | 建議 | 主辦方配發的隊伍代號，用來比對 `team_id` |
| `--active-etf` | 選填 | 主動型 ETF 前 15 大持股權重，用來檢查主動投資比率（Active Share ≥ 15%） |
| `--theme` | 選填 | ETF 主題設定，用來檢查主題標的占比 |
| `--holiday` | 選填 | 額外的休市日 `YYYY-MM-DD`，可重複指定（10/26 已內建） |
| `--now` | 測試用 | 以指定時間（台北時間）當作「現在」 |
| `--universe` | 選填 | 150 檔標的清單，預設使用內建檔 |
| `--schema` | 選填 | D-Plan JSON Schema，預設使用內建 v4.3 |
| `--json` | 選填 | 以 JSON 輸出結果（給程式讀） |
| `--strict` | 選填 | 有警告也視為失敗 |

**結束碼**：`0` 表示沒有錯誤；`1` 表示有錯誤（加上 `--strict` 時，有警告也回傳 1）。

### 2.2 在 Agent 程式中呼叫

```python
from pathlib import Path
from dplan_checker import CheckOptions, check_dplan

report = check_dplan(
    Path("out/D-Plan_TEAM_XXX_2026-10-28.json"),
    CheckOptions(
        datapack_path=Path("inbox/DataPack.json"),
        prices_path=Path("cache/prices_2026-10-27.json"),
        team_id="TEAM_XXX",
    ),
)
if not report.ok:
    print(report.format_text())
    raise SystemExit(1)
```

`report.to_dict()` 會回傳可序列化的結果，方便 Agent 自動讀取錯誤並重新產生。

> ⚠️ 比賽規定 D-Plan 內容必須由 Agent 產製。檢查沒過時，**不要手動改 JSON**；
> 應修正程式或提示詞後重新執行 Agent（並更新 `code_version`）。

---

## 3. 輸入檔格式

### 3.1 資料包（`--datapack`）

直接使用後台下載的檔案，格式見 `DataPack_規格書_v4.3.md`。檢查器使用的欄位：
`team_id`、`trade_date`、`as_of_date`、`generated_at`、`missing`、`prev_day_orders`、
`account.cash`、`account.nav_tradable`、`account.aum`、`holdings[]`、`compliance`。

**持股的定義**：`holdings[].shares > 0` 才算持股。只有應收配股（`shares` 為 0、
`receivable_shares > 0`）的標的不算持股，不需要、也不應該列在 `no_trade_decisions`。

### 3.2 前日收盤價（`--prices`）

以下格式都可以：

```json
{ "2330": 1480, "2891": 42.5 }
```

```json
{ "prices": { "2330": 1480, "2891": 42.5 } }
```

```json
[ { "code": "2330", "close": 1480 }, { "code": "2891", "close": 42.5 } ]
```

```csv
code,close
2330,1480
2891,42.5
```

同一檔若資料包與價格檔都有收盤價，**以資料包為準**；兩者差異超過 0.1% 時會發出警告。

### 3.3 主動型 ETF 前 15 大持股（`--active-etf`）

權重以比率表示（`0.09` = 9%）：

```json
{
  "00980A": { "2330": 0.092, "2454": 0.051, "2317": 0.048 },
  "00981A": { "2330": 0.088, "3017": 0.061 }
}
```

也接受 `{"etfs": {"00980A": {"top15": {...}}}}`。
內建的主動型 ETF 清單（30 檔，`dplan_checker/data/active_etfs.json`）若有 ETF 沒有提供持股，會發出警告。

### 3.4 ETF 主題設定（`--theme`）

```json
{ "name": "AI 半導體", "tickers": ["2330", "2454", "3711", "3443"], "min_weight": 0.6 }
```

`min_weight` 是主題標的占淨值的下限，低於下限會發出警告。

---

## 4. 結果判讀

每個問題分三個等級，並附上「送出後可能的後果」：

| 等級 | 意義 | 該怎麼做 |
|---|---|---|
| 錯誤（ERROR） | 會被後台擋下、當日委託作廢並記警告，或違反比賽規則 | **必須修正**，否則不要上傳 |
| 警告（WARN） | 不一定會被擋，但有被抽檢質疑、或實際成交後越界的風險 | 逐條確認；能修就修 |
| 提示（INFO） | 參考資訊 | 看過即可 |

後果標示的類型：

| 標示 | 意思 |
|---|---|
| 上傳即被擋下 | 後台「文件檢查未通過」，無法按送出，不扣當日次數 |
| 嚴重錯誤 | 後台顯示紅框，仍可送出，但當日委託可能整批作廢並記警告 |
| 當日交易作廢並記警告 | 結算時違反交易規則，持股沿用前一日，記一次警告 |
| 可能取消資格 | 例如主動投資比率連續 2 日低於 15% |

報告最後的「未執行的檢查」會列出因為缺少輸入檔而略過的項目。

---

## 5. 檢查項目一覽

括號內為報告中的代碼。

### 5.1 檔案與 JSON
- 檔名必須是 `D-Plan_<team_id>_<trade_date>.json`，且與 JSON 內容一致（`FILENAME_FORMAT`、`FILENAME_TEAM_MISMATCH`、`FILENAME_DATE_MISMATCH`）
- 合法 JSON、UTF-8、沒有 `NaN`／`Infinity`（`JSON_INVALID`）；沒有重複的鍵（`JSON_DUPLICATE_KEY`）；BOM（`JSON_BOM`）
- 依官方 schema v4.3 逐項檢查，錯誤訊息為中文（`SCHEMA`）：必填欄位、多餘欄位、型別、格式、列舉值、筆數與長度、整張（1000 股倍數）
- 範例殘留：`_NOTE`（`NOTE_FIELD`）、範例隊伍代號（`TEAM_ID_EXAMPLE`）、範例 `code_version`（`META_CODE_VERSION_EXAMPLE`）、範例網址（`SOURCE_URL_PLACEHOLDER`）、`TODO`／`<think>`／程式碼區塊等殘留文字（`TEXT_LEFTOVER`）

### 5.2 時間
- `trade_date` 是真實日期、不是週末或休市日、在初賽期間內（`TRADE_DATE_*`）
- 時間戳是真實存在的時間（`TIME_INVALID`）
- `run_started_at ≤ run_completed_at`（`META_TIME_ORDER`）
- `run_completed_at` 不晚於現在（`META_COMPLETED_IN_FUTURE`）、不晚於交易日 08:55（`META_AFTER_DEADLINE`）、不早於前一日 19:30（`META_BEFORE_WINDOW`）
- 來源的 `content_as_of` 不晚於 Agent 完成時間與截止時間，即不可用未來資料（`SOURCE_FUTURE_CONTENT`、`SOURCE_LOOKAHEAD`）
- `fetched_at`、`published_at` 的先後合理（`SOURCE_FETCHED_*`、`SOURCE_CONTENT_AFTER_FETCH`、`SOURCE_PUBLISHED_AFTER_FETCH`）；資料過舊（`SOURCE_STALE`）
- 現在是否在繳交時段內（`NOW_AFTER_DEADLINE`、`NOW_BEFORE_WINDOW`）
- Agent 完成時間早於資料包產生時間，代表可能沒有用這份資料包（`RUN_BEFORE_DATAPACK`）

### 5.3 編號與引用鏈
- 流水號從 1 開始、連續不跳號、不加前導零、不重複（`ID_NOT_SEQUENTIAL`、`ID_LEADING_ZERO`、`ID_DUPLICATE`、`ID_ORDER`）
- 每一層引用都必須存在：order → decision → inference → observation → source，以及 `market_view.basis_refs`、`reason_refs`、`funding_for`（`REF_BROKEN`）
- 沒被引用的來源、事實、推論（`SOURCE_UNUSED`、`OBS_UNUSED`、`INF_UNUSED`）
- 文字中提到的編號不存在，或提到的事實沒列進引用欄位（`TEXT_MENTIONS_*`）

### 5.4 內容品質
- 事實層混入推論用語，例如「因此」「預期」「看好」（`OBS_HAS_INFERENCE`）
- `values` 的數字沒有出現在 `statement` 中（`OBS_VALUES_NOT_IN_TEXT`）
- media／vendor／other 來源沒有 `archive_url`（`SOURCE_NO_ARCHIVE`）
- `authority` 與網址網域不符，例如櫃買中心網址填 twse（`SOURCE_AUTHORITY_MISMATCH`、`SOURCE_AUTHORITY_DOMAIN`）
- `counter_evidence` 寫空字串而不是 `null`（`EMPTY_COUNTER_EVIDENCE`）
- `stance` 與淨曝險意圖矛盾（`STANCE_INTENT_CONFLICT`）
- 決策或續抱引用的推論沒有提到該股票（`DECISION_INF_NO_TICKER`、`NO_TRADE_INF_NO_TICKER`）

### 5.5 決策覆蓋（需要資料包）
- 昨日每一檔持股（`shares > 0`）都要出現在 `decisions` 或 `no_trade_decisions` 其中之一（`COVERAGE_MISSING`）
- 同一檔不能兩邊都寫、不能重複（`DECISION_AND_NO_TRADE`、`DECISION_DUP_TICKER`、`NO_TRADE_DUP_TICKER`）
- 沒有持股的標的（包含只有應收配股的）不能列在 `no_trade_decisions`（`NO_TRADE_NOT_HELD`）
- 動作與持股相符：已持有不能用 BUY、未持有不能用 ADD／TRIM／SELL_ALL（`ACTION_BUY_BUT_HELD`、`ACTION_NOT_HELD`）

### 5.6 委託推導（需要資料包與收盤價）
- 依官方公式重算每筆委託：`floor(target_weight × nav_tradable ÷ 前日收盤價 ÷ 1000) × 1000 − 現持股數`，比對方向與股數（`ORDER_DERIVATION_MISMATCH`、`ORDER_MISSING`、`ORDER_UNEXPECTED`）
- 動作與推導結果一致：ADD 必須買、TRIM 必須賣且不歸零、SELL_ALL 必須歸零（`ACTION_DERIVATION_MISMATCH`）；BUY 至少一張（`BUY_ZERO_SHARES`）
- 推導不出委託的決策應改列續抱（`DECISION_NO_ORDER`）
- **浮點邊界**：例如 `target_weight = 0.0157` 時，精確計算得 157 張、浮點計算得 156 張，伺服器可能算出不同結果（`DERIVATION_FLOAT_EDGE`）
- 賣出不超過庫存（`OVERSELL`）；一檔一筆、一筆決策一筆委託（`ORDER_DUP_*`）；委託與決策的股票、方向一致（`ORDER_TICKER_MISMATCH`、`ORDER_SIDE_MISMATCH`）
- 缺收盤價時無法驗證（`PRICE_MISSING`）

### 5.7 交易後合規（以前日收盤價估算）
- 現金不為負（`CASH_NEGATIVE`）、不超過 25%（`CASH_OVER_LIMIT`）、離邊界太近（`CASH_NEAR_LIMIT`）
- 壓力測試：買進均價高 3%、賣出均價低 3% 時現金是否轉負（`CASH_STRESS_NEGATIVE`），以及反向情境下現金是否超過 25%（`CASH_STRESS_OVER`）
- 持股 20–30 檔（`HOLDINGS_COUNT`）
- 個股權重：2330 上限 25%、其他 10%。加碼造成的超限當日即記警告（`DECISION_OVER_CAP`、`WEIGHT_OVER_CAP_BY_BUY`）；價格波動造成的被動超限依資料包紀錄計算連續天數，第 6 天仍超限即為錯誤（`PASSIVE_OVERWEIGHT`、`PASSIVE_OVERWEIGHT_DEADLINE`）
- 只能持有 150 檔清單內的股票（`TICKER_NOT_IN_UNIVERSE`、`HOLDING_NOT_IN_UNIVERSE`）
- 市場姿態承諾：`increase` 要淨買超、`reduce` 要淨賣超、`hold` 淨買賣差 ≤ 2% 淨值（`POSTURE_NET_FLOW`）；交易後現金比落在宣告區間內（`POSTURE_CASH_RANGE`）
- 資金調度：`funding_for` 只能用在 TRIM／SELL_ALL、必須指向同日的 BUY／ADD（`FUNDING_*`）；拿掉調度賣出後現金必須真的跌破宣告下限（`FUNDING_NO_GAP`）；有賣有買、確實需要騰錢卻沒標記（`FUNDING_UNTAGGED`）
- 主動投資比率：與每一檔主動型 ETF 的前 15 大持股比較，低於 15% 為錯誤、低於 20% 為警告（`ACTIVE_SHARE_*`）
- 主題標的占比低於設定下限（`THEME_WEIGHT_LOW`）
- 周轉率與交易成本（`HIGH_TURNOVER`）

### 5.8 資料包狀態
- 前一交易日結算尚未產生（`DATAPACK_NOT_SETTLED`），此時請等結算完成後重新下載
- 資料包的交易日或隊伍與 D-Plan 不符（`DATAPACK_DATE_MISMATCH`、`DATAPACK_TEAM_MISMATCH`）
- 累計違規警告：已 2 次時提醒再一次即取消資格（`VIOLATIONS_LAST_CHANCE`、`VIOLATIONS_EXHAUSTED`）
- 前一日委託被作廢時列出原因（`PREV_DAY_VOIDED`）
- 收盤價缺漏、淨值不一致、持股非整張（`DATAPACK_NO_CLOSE`、`DATAPACK_NAV_INCONSISTENT`、`HOLDING_ODD_LOT`）

---

## 6. 已知限制與假設

1. **後果標示是推斷**：文件沒有完整說明後台對每一種錯誤的處理，標示的後果依規格描述推斷，實際以後台檢核結果為準。
2. **被動超限天數**：從資料包 `compliance.incidents` 中比對 `status=warn`、訊息含股票代號與「權重」的紀錄來計算。資料包規格沒有寫這類紀錄的實際格式，拿到真實資料後需校正。
3. **主動投資比率**：比賽辦法沒有說明權重是否要重新加總。判定使用「雙方前 15 大各自重新加總為 100%」的版本，另列出直接用占淨值權重的版本供參考。主動型 ETF 的持股資料需要自行抓取。
4. **休市日**：已內建 2026-10-26（初賽共 24 個交易日）。若有其他臨時休市（例如颱風），用 `--holiday` 追加。
5. **繳交時段**：以「交易日前一個日曆日 19:30 至交易日 08:55」判斷，與比賽辦法的範例一致。
6. **現金與權重估算**：使用前日收盤價；實際成交為當日均價、權重以當日收盤價計，所以檢查器對邊界值保留緩衝並做 ±3% 壓力測試。
7. **手續費**：手續費 0.1425%、證交稅 0.3%（賣出），未考慮最低手續費。

---

## 7. 測試

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

測試涵蓋每一類錯誤，並以官方反例 `D-Plan_TEAM_043_2026-10-28.json` 驗證範例檔 `_NOTE` 所列的三個問題
（孤兒決策、2330 委託股數錯誤、姿態與委託方向相反）都能被抓到。

---

## 8. 檔案結構

```
dplan_checker/
├─ __main__.py      命令列入口
├─ checker.py       所有檢查邏輯
├─ portfolio.py     官方委託公式、成交模擬、主動投資比率
├─ loaders.py       讀檔（重複鍵、BOM、NaN 偵測；價格、ETF 持股、主題設定）
├─ report.py        結果等級、後果標示、文字與 JSON 輸出
├─ rules.py         比賽規則常數（上限、費率、時間）
└─ data/
   ├─ D-Plan.schema_v4.3.json   官方 schema
   ├─ universe_150.json         150 檔標的清單（由官方 PDF 轉出）
   └─ active_etfs.json          30 檔主動型 ETF 清單（由官方 PDF 轉出）
tests/test_checker.py
```
