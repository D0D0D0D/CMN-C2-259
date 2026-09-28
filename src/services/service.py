"""AgentCore Platform v1.0"""

# Service layer: pure deterministic domain logic for deal scoring.
# No I/O, no credentials, no LLM calls here — nodes wire those.
# Every function is pure so unit tests need no framework fixtures.

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# Score factors that contribute to coverage (deal_id/notes are excluded).
SCORE_FACTORS = ("phase", "amount", "recency", "bant")

# Truthy / falsy tokens for BANT normalization (case-insensitive).
_BANT_TRUE = {"true", "yes", "y", "1", "確認済", "済", "ok", "done"}
_BANT_FALSE = {"false", "no", "n", "0", "未", "未確認", "ng", ""}


def normalize_bant(value: Any) -> bool | None:
    """Normalize a raw BANT cell to bool. Returns None when unknown/missing."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in _BANT_TRUE:
        return True
    if token in _BANT_FALSE:
        return False
    return None


def parse_date(value: Any) -> date | None:
    """Best-effort ISO date parse. Returns None when unparseable/missing."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def days_since(value: Any, today: date) -> int | None:
    """Whole days between last_updated and `today`. None when unparseable."""
    d = parse_date(value)
    if d is None:
        return None
    return (today - d).days


def is_stagnant(deal: dict, today: date, stagnation_days: int) -> bool | None:
    """True when last_updated is older than the stagnation threshold."""
    n = days_since(deal.get("last_updated"), today)
    if n is None:
        return None
    return n > stagnation_days


def _phase_score(deal: dict, cfg: dict) -> float | None:
    phase = deal.get("phase")
    if phase is None or str(phase).strip() == "":
        return None
    table = {k.lower(): v for k, v in cfg.get("phase_scores", {}).items()}
    return table.get(str(phase).strip().lower())  # None when not in table


def _amount_score(deal: dict, cfg: dict) -> float | None:
    raw = deal.get("amount")
    if raw is None or raw == "":
        return None
    try:
        amount = float(raw)
    except (ValueError, TypeError):
        return None
    if amount < 0:
        return None
    ceiling = float(cfg.get("amount_ceiling", 100_000_000)) or 1.0
    return min(amount / ceiling, 1.0)


def _recency_score(deal: dict, cfg: dict, today: date) -> float | None:
    n = days_since(deal.get("last_updated"), today)
    if n is None:
        return None
    horizon = float(cfg.get("recency_horizon_days", 90)) or 1.0
    # Fresh (n<=0) => 1.0, older than horizon => 0.0, linear in between.
    return max(0.0, min(1.0, 1.0 - (n / horizon)))


def bant_fulfillment(deal: dict) -> tuple[float | None, int, int]:
    """Return (ratio, fulfilled, present). ratio None when no BANT present.

    Denominator = BANT items actually present (missing items excluded).
    """
    present = 0
    fulfilled = 0
    for key in ("bant_budget", "bant_authority", "bant_need", "bant_timeline"):
        v = normalize_bant(deal.get(key))
        if v is None:
            continue
        present += 1
        if v:
            fulfilled += 1
    if present == 0:
        return None, 0, 0
    return fulfilled / present, fulfilled, present


def score_deal(deal: dict, cfg: dict, today: date) -> dict:
    """Compute a deterministic win-probability score with explicit coverage.

    Only factors that are present contribute; the score is the weighted mean
    over *available* factors (missing factors are excluded from the weight
    denominator = missing-tolerant). Returns a dict with score, coverage,
    per-factor values and a list of missing factors.
    """
    weights = cfg.get("weights", {})
    bant_ratio, _, _ = bant_fulfillment(deal)
    factor_values: dict[str, float | None] = {
        "phase": _phase_score(deal, cfg),
        "amount": _amount_score(deal, cfg),
        "recency": _recency_score(deal, cfg, today),
        "bant": bant_ratio,
    }

    num = 0.0
    denom = 0.0
    present_factors = []
    missing_factors = []
    for factor in SCORE_FACTORS:
        val = factor_values[factor]
        w = float(weights.get(factor, 0.0))
        if val is None:
            missing_factors.append(factor)
            continue
        present_factors.append(factor)
        num += val * w
        denom += w

    score = (num / denom) if denom > 0 else None
    coverage = len(present_factors) / len(SCORE_FACTORS)
    return {
        "score": round(score, 4) if score is not None else None,
        "coverage": round(coverage, 4),
        "present_factors": present_factors,
        "missing_factors": missing_factors,
        "factor_values": {k: v for k, v in factor_values.items() if v is not None},
    }


# ── Contract normalization / validation (pre_process helpers) ────────────

# Columns the agent consumes (Data Contract 1.0.0). PII contact columns are
# NOT in this set and are dropped before rows enter State.
KEPT_COLUMNS = (
    "deal_id", "deal_name", "account_name", "owner", "phase", "amount",
    "last_updated", "bant_budget", "bant_authority", "bant_need",
    "bant_timeline", "notes",
)

# Required (NULL 不可) columns in contract 1.0.0. Missing ones on route (b)
# data are logged as "Contract 逸脱" rather than silently tolerated.
REQUIRED_COLUMNS = ("deal_id", "deal_name", "account_name", "phase", "last_updated")


def apply_column_map(row: dict, column_map: dict) -> dict:
    """Rename actual DB columns to normalized names. Empty map = passthrough."""
    if not column_map:
        return dict(row)
    # column_map: {normalized_name: actual_column_name}
    inverse = {actual: norm for norm, actual in column_map.items()}
    out = {}
    for k, v in row.items():
        out[inverse.get(k, k)] = v
    return out


def project_kept_columns(row: dict) -> dict:
    """Keep only contract columns the agent consumes (drops PII contact cols)."""
    return {k: row[k] for k in KEPT_COLUMNS if k in row}


def validate_row(row: dict) -> list[str]:
    """Return a list of contract-deviation descriptors for one row.

    - deal_id missing        -> "deal_id_missing" (row will be skipped)
    - required col missing   -> "<col>_missing (Contract逸脱)"
    - type-invalid factor    -> "<col>_type_invalid"
    """
    issues: list[str] = []
    if row.get("deal_id") in (None, ""):
        issues.append("deal_id_missing")
    for col in REQUIRED_COLUMNS:
        if col == "deal_id":
            continue
        if row.get(col) in (None, ""):
            issues.append(f"{col}_missing (Contract逸脱)")
    # type checks on score-contributing optional/required columns
    if row.get("amount") not in (None, ""):
        try:
            if float(row["amount"]) < 0:
                issues.append("amount_negative")
        except (ValueError, TypeError):
            issues.append("amount_type_invalid")
    if row.get("last_updated") not in (None, "") and parse_date(row.get("last_updated")) is None:
        issues.append("last_updated_type_invalid")
    return issues


def decide_priority(score: float | None, stagnant: bool | None, routing_table: list[dict]) -> str:
    """Rule-based priority via a top-down routing table (first match wins).

    stagnant may be None (unknown — e.g. last_updated missing). An unknown
    stagnation state matches ONLY rules whose `stagnant` is null (any); it does
    NOT satisfy a rule that explicitly requires stagnant true/false, so an
    incomplete record cannot jump into a stagnation-specific bucket.
    """
    s = score if score is not None else 0.0
    for rule in routing_table:
        min_score = float(rule.get("min_score", 0.0))
        want_stag = rule.get("stagnant", None)  # None = any
        if s < min_score:
            continue
        if want_stag is not None:
            if stagnant is None or bool(want_stag) != bool(stagnant):
                continue
        return str(rule.get("priority", "P4-low"))
    return "P4-low"


# ── Report rendering (post_process helper) ───────────────────────────────

# priority sort order for the report (lower index = higher priority)
_PRIORITY_ORDER = ("P1-urgent", "P2-high", "P3-normal", "P4-low")


def _priority_rank(priority: str) -> int:
    try:
        return _PRIORITY_ORDER.index(priority)
    except ValueError:
        return len(_PRIORITY_ORDER)


def build_report(
    scored_deals: list[dict],
    qualitative: dict,
    missing_map: dict,
    non_conformant: bool,
    low_coverage_ratio: float = 0.4,
) -> str:
    """Render the Markdown report. Deterministic score and LLM qualitative
    findings are presented in SEPARATE columns (never merged into one number).
    Handles the empty-deals branches (A non-conformant / B no rows)."""
    lines: list[str] = ["# 商談スコアリング・優先度レポート", ""]

    if non_conformant:
        lines += [
            "> ⚠️ **Contract 非準拠の可能性・要スキーマ確認**",
            ">",
            "> 取得した全行に `deal_id` がありませんでした。`deals` テーブルが Data Contract "
            "（`deal_id` 必須）に準拠しているか、`column_map` の設定を確認してください。",
        ]
        return "\n".join(lines)

    if not scored_deals:
        lines += ["> 該当する案件がありませんでした（フィルタ条件に一致する案件なし）。"]
        return "\n".join(lines)

    ranked = sorted(
        scored_deals,
        key=lambda d: (_priority_rank(d.get("priority", "P4-low")), -(d.get("score") or 0.0)),
    )

    lines += [
        f"対象案件数: {len(scored_deals)} 件",
        "",
        "| 優先度 | 案件 | 顧客 | スコア | カバレッジ | 停滞 | 決定論所見(欠損) | 定性リスク(LLM) | 推奨アクション(LLM) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    n_factors = len(SCORE_FACTORS)
    for deal in ranked:
        did = str(deal.get("deal_id"))
        name = deal.get("deal_name") or "(名称未設定)"
        account = deal.get("account_name") or "(企業名未設定)"
        score = deal.get("score")
        score_str = f"{score:.2f}" if score is not None else "—"
        coverage = deal.get("coverage", 0.0)
        cov_str = f"{int(round(coverage * n_factors))}/{n_factors}"
        stag = deal.get("_stagnant")
        stag_str = "⚠️停滞" if stag else ("—" if stag is not None else "不明")

        det_notes = []
        if coverage < low_coverage_ratio:
            det_notes.append("**要手動確認**(低カバレッジ)")
        det_notes += missing_map.get(did, [])
        det_str = "; ".join(det_notes) if det_notes else "—"

        q = qualitative.get(did, {})
        risks = "; ".join(q.get("risks", [])) or "—"
        actions = "; ".join(q.get("actions", [])) or "—"

        lines.append(
            f"| {deal.get('priority', 'P4-low')} | {name} | {account} | {score_str} | "
            f"{cov_str} | {stag_str} | {det_str} | {risks} | {actions} |"
        )

    lines += [
        "",
        "> 決定論スコア（説明可能）と LLM 定性所見（不確実・要確認）は分離して提示しています。",
        "> LLM 所見は人間レビューを前提としてください。",
    ]
    return "\n".join(lines)
