"""競賽規則常數（出處：比賽辦法、D-Plan 撰寫指南 v4.3、DataPack 規格書 v4.3、後台操作說明）。"""

import datetime as dt
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_SCHEMA = DATA_DIR / "D-Plan.schema_v4.3.json"
DEFAULT_UNIVERSE = DATA_DIR / "universe_150.json"
DEFAULT_ACTIVE_ETFS = DATA_DIR / "active_etfs.json"

TPE = dt.timezone(dt.timedelta(hours=8))

SCHEMA_VERSION = "4.3"
COMPETITION_START = dt.date(2026, 10, 26)
COMPETITION_END = dt.date(2026, 11, 27)
KNOWN_HOLIDAYS = frozenset({dt.date(2026, 10, 23), dt.date(2026, 10, 26)})
SUBMIT_OPEN_TIME = dt.time(19, 30)   # 前一日
SUBMIT_DEADLINE_TIME = dt.time(8, 55)  # 交易日當天

LOT_SIZE = 1000
TSMC = "2330"
TSMC_WEIGHT_CAP = 0.25
STOCK_WEIGHT_CAP = 0.10
WEIGHT_CAP_BUFFER = 0.005
MIN_HOLDINGS = 20
MAX_HOLDINGS = 30
CASH_MAX = 0.25
CASH_EDGE_BUFFER = 0.01
HOLD_NET_FLOW_TOLERANCE = 0.02
HOLD_NET_FLOW_WARN = 0.018
PASSIVE_OVERWEIGHT_GRACE_DAYS = 5

FEE_RATE = 0.001425
TAX_RATE = 0.003
STRESS_PRICE_MOVE = 0.03

ACTIVE_SHARE_MIN = 0.15
ACTIVE_SHARE_WARN = 0.20
ACTIVE_SHARE_TOP_N = 15

MAX_VIOLATIONS = 3
SOURCE_STALE_DAYS = 7

EXAMPLE_TEAM_IDS = {"TEAM_042", "TEAM_043"}
EXAMPLE_CODE_VERSIONS = {"git:a1b2c3d"}

ID_PREFIX = {
    "sources": ("source_id", "S"),
    "observations": ("obs_id", "O"),
    "inferences": ("inf_id", "I"),
    "decisions": ("decision_id", "D"),
}

AUTHORITY_DOMAINS = {
    "mops": ("mops.twse.com.tw", "mopsov.twse.com.tw"),
    "twse": ("twse.com.tw",),
    "tpex": ("tpex.org.tw",),
    "taifex": ("taifex.com.tw",),
}
ARCHIVE_DOMAINS = ("web.archive.org", "archive.today", "archive.ph", "archive.is", "archive.org")
ARCHIVE_RECOMMENDED_AUTHORITIES = {"media", "vendor", "other"}

INFERENCE_WORDS = (
    "因此", "所以", "預期", "可望", "看好", "看壞", "看淡", "推測", "推估", "應該",
    "將會", "研判", "判定", "判斷", "建議", "顯示出", "意味", "代表著", "→",
)
FUNDING_LOGIC_WORDS = ("讓位", "調度", "資金", "騰", "支應", "釋出", "換股", "最弱", "最小")
LEFTOVER_MARKERS = ("TODO", "TBD", "FIXME", "XXX", "<think>", "```", "（宣稱）", "(宣稱)", "lorem")
PLACEHOLDER_URL_HOSTS = ("example.com", "example.org", "example.net", "localhost", "127.0.0.1")

REGIME_STANCE_CONFLICT = {("risk_on", "defensive"), ("risk_off", "aggressive")}
STANCE_INTENT_CONFLICT = {("aggressive", "reduce"), ("defensive", "increase")}

SELL_ACTIONS = {"TRIM", "SELL_ALL"}
BUY_ACTIONS = {"BUY", "ADD"}


def weight_cap(ticker: str) -> float:
    return TSMC_WEIGHT_CAP if ticker == TSMC else STOCK_WEIGHT_CAP
