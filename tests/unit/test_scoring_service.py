"""Unit tests for the deterministic scoring service (pure functions)."""

from __future__ import annotations

from datetime import date

import pytest

from src.services import service as svc

TODAY = date(2026, 7, 3)

CFG = {
    "weights": {"phase": 0.35, "amount": 0.25, "recency": 0.20, "bant": 0.20},
    "phase_scores": {"proposal": 0.5, "negotiation": 0.75, "won": 1.0, "lost": 0.0},
    "amount_ceiling": 100_000_000,
    "recency_horizon_days": 90,
}

ROUTING = [
    {"min_score": 0.7, "stagnant": True, "priority": "P1-urgent"},
    {"min_score": 0.7, "stagnant": None, "priority": "P2-high"},
    {"min_score": 0.4, "stagnant": True, "priority": "P2-high"},
    {"min_score": 0.4, "stagnant": None, "priority": "P3-normal"},
    {"min_score": 0.0, "stagnant": None, "priority": "P4-low"},
]


def test_normalize_bant_variants():
    assert svc.normalize_bant("yes") is True
    assert svc.normalize_bant("確認済") is True
    assert svc.normalize_bant(True) is True
    assert svc.normalize_bant("no") is False
    assert svc.normalize_bant("未") is False
    assert svc.normalize_bant(None) is None
    assert svc.normalize_bant("maybe") is None  # unknown token


def test_bant_fulfillment_excludes_missing():
    deal = {"bant_budget": "yes", "bant_authority": "no", "bant_need": None}
    ratio, fulfilled, present = svc.bant_fulfillment(deal)
    assert present == 2  # need is missing -> excluded from denominator
    assert fulfilled == 1
    assert ratio == 0.5


def test_bant_fulfillment_all_missing_returns_none():
    ratio, fulfilled, present = svc.bant_fulfillment({})
    assert ratio is None and present == 0


def test_is_stagnant():
    assert svc.is_stagnant({"last_updated": "2026-06-01"}, TODAY, 14) is True
    assert svc.is_stagnant({"last_updated": "2026-07-01"}, TODAY, 14) is False
    assert svc.is_stagnant({"last_updated": None}, TODAY, 14) is None


def test_score_deal_missing_tolerant_coverage():
    # only phase present -> coverage 1/4, score = phase_score
    deal = {"phase": "won"}
    r = svc.score_deal(deal, CFG, TODAY)
    assert r["coverage"] == 0.25
    assert r["score"] == 1.0  # weighted mean over available factors only
    assert set(r["missing_factors"]) == {"amount", "recency", "bant"}


def test_score_deal_full_factors():
    deal = {
        "phase": "negotiation", "amount": 100_000_000, "last_updated": "2026-07-03",
        "bant_budget": "yes", "bant_authority": "yes", "bant_need": "yes", "bant_timeline": "yes",
    }
    r = svc.score_deal(deal, CFG, TODAY)
    assert r["coverage"] == 1.0
    # phase .75, amount 1.0, recency 1.0, bant 1.0 -> weighted mean
    assert 0.9 <= r["score"] <= 0.92


def test_score_deal_no_factors_returns_none_score():
    r = svc.score_deal({"deal_id": "x"}, CFG, TODAY)
    assert r["score"] is None
    assert r["coverage"] == 0.0


def test_score_deal_negative_amount_excluded():
    r = svc.score_deal({"amount": -100}, CFG, TODAY)
    assert "amount" in r["missing_factors"]


@pytest.mark.parametrize("score,stag,expected", [
    (0.8, True, "P1-urgent"),
    (0.8, False, "P2-high"),
    (0.5, True, "P2-high"),
    (0.5, False, "P3-normal"),
    (0.1, False, "P4-low"),
    (None, False, "P4-low"),
])
def test_decide_priority(score, stag, expected):
    assert svc.decide_priority(score, stag, ROUTING) == expected


def test_decide_priority_unknown_stagnation_not_treated_as_fresh():
    # stagnant=None (last_updated missing) must NOT satisfy a stagnant-specific
    # rule; a high score with unknown stagnation lands in the "any" bucket.
    assert svc.decide_priority(0.8, None, ROUTING) == "P2-high"  # not P1-urgent
    assert svc.decide_priority(0.5, None, ROUTING) == "P3-normal"  # not the stagnant P2


def test_validate_row_flags_missing_and_types():
    assert "deal_id_missing" in svc.validate_row({"phase": "x"})
    issues = svc.validate_row({"deal_id": "1", "amount": "notnum"})
    assert "amount_type_invalid" in issues
    issues2 = svc.validate_row({"deal_id": "1", "last_updated": "bad-date"})
    assert "last_updated_type_invalid" in issues2


def test_apply_column_map_and_projection():
    row = {"id": "1", "stage": "won", "担当者メール": "person@example.com"}
    mapped = svc.apply_column_map(row, {"deal_id": "id", "phase": "stage"})
    assert mapped["deal_id"] == "1" and mapped["phase"] == "won"
    kept = svc.project_kept_columns(mapped)
    assert "担当者メール" not in kept  # PII contact col dropped


def test_build_report_separates_score_and_llm():
    scored = [{"deal_id": "D1", "deal_name": "A", "account_name": "AC",
               "score": 0.8, "coverage": 1.0, "priority": "P2-high", "_stagnant": False}]
    qual = {"D1": {"risks": ["r1"], "actions": ["a1"]}}
    report = svc.build_report(scored, qual, {}, non_conformant=False)
    assert "商談スコアリング" in report
    assert "r1" in report and "a1" in report


def test_build_report_non_conformant():
    report = svc.build_report([], {}, {}, non_conformant=True)
    assert "Contract 非準拠" in report


def test_build_report_empty_filter():
    report = svc.build_report([], {}, {}, non_conformant=False)
    assert "該当する案件がありません" in report
