"""从追踪/校验报告中提取覆盖率双指标（设计覆盖率 / 推演通过率）。"""

from __future__ import annotations

import re
from pathlib import Path

DESIGN_RE = re.compile(r"设计覆盖率\D{0,12}?([\d.]+)\s*%")
PASS_RE = re.compile(r"推演通过率\D{0,12}?([\d.]+)\s*%")
MAX_REPORTS_SCANNED = 15


def coverage(root: Path) -> dict:
    reports_dir = root / "reports"
    if not reports_dir.is_dir():
        return {"design_coverage": None, "pass_rate": None, "source": None}
    reports = sorted(
        reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:MAX_REPORTS_SCANNED]
    for report in reports:
        try:
            text = report.read_text(encoding="utf-8")
        except OSError:
            continue
        design = DESIGN_RE.search(text)
        passed = PASS_RE.search(text)
        if design or passed:
            return {
                "design_coverage": float(design.group(1)) if design else None,
                "pass_rate": float(passed.group(1)) if passed else None,
                "source": f"reports/{report.name}",
            }
    return {"design_coverage": None, "pass_rate": None, "source": None}
