from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

ERROR = "ERROR"
WARN = "WARN"
INFO = "INFO"

LEVEL_LABEL = {ERROR: "錯誤", WARN: "警告", INFO: "提示"}
LEVEL_ORDER = {ERROR: 0, WARN: 1, INFO: 2}

REJECT = "上傳即被擋下（文件檢查未通過），無法送出"
SEVERE = "後台會顯示嚴重錯誤；若照樣送出，當日委託可能整批作廢並記警告"
VIOLATION = "當日交易作廢，並記一次警告（累計 3 次取消資格）"
DISQUALIFY = "可能直接取消參賽資格"
MISSED = "視同當日未繳交（記警告）"


@dataclass
class Issue:
    level: str
    code: str
    message: str
    path: str = ""
    consequence: str = ""


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def add(self, level: str, code: str, message: str, path: str = "", consequence: str = "") -> None:
        self.issues.append(Issue(level, code, message, path, consequence))

    def error(self, code: str, message: str, path: str = "", consequence: str = "") -> None:
        self.add(ERROR, code, message, path, consequence)

    def warn(self, code: str, message: str, path: str = "", consequence: str = "") -> None:
        self.add(WARN, code, message, path, consequence)

    def info(self, code: str, message: str, path: str = "") -> None:
        self.add(INFO, code, message, path)

    def skip(self, reason: str) -> None:
        self.skipped.append(reason)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == WARN]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self, level: str | None = None) -> set[str]:
        return {i.code for i in self.issues if level is None or i.level == level}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "counts": {lvl: sum(1 for i in self.issues if i.level == lvl) for lvl in (ERROR, WARN, INFO)},
            "summary": self.summary,
            "skipped": self.skipped,
            "issues": [asdict(i) for i in sorted(self.issues, key=lambda i: LEVEL_ORDER[i.level])],
        }

    def format_text(self) -> str:
        lines: list[str] = []
        n_err, n_warn = len(self.errors), len(self.warnings)
        n_info = len(self.issues) - n_err - n_warn
        verdict = "通過（沒有錯誤）" if self.ok else "未通過（有錯誤，請修正後再上傳）"
        lines.append(f"D-Plan 檢查結果：{verdict}")
        lines.append(f"錯誤 {n_err}　警告 {n_warn}　提示 {n_info}")

        if self.summary:
            lines.append("")
            lines.append("── 摘要 ──")
            for key, value in self.summary.items():
                lines.append(f"  {key}：{value}")

        for level in (ERROR, WARN, INFO):
            group = [i for i in self.issues if i.level == level]
            if not group:
                continue
            lines.append("")
            lines.append(f"── {LEVEL_LABEL[level]}（{len(group)}）──")
            for issue in group:
                where = f" @ {issue.path}" if issue.path else ""
                lines.append(f"  [{issue.code}]{where}")
                lines.append(f"    {issue.message}")
                if issue.consequence:
                    lines.append(f"    → 後果：{issue.consequence}")

        if self.skipped:
            lines.append("")
            lines.append("── 未執行的檢查 ──")
            for reason in self.skipped:
                lines.append(f"  - {reason}")
        return "\n".join(lines)
