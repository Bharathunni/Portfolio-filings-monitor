"""Regression tests for the keyword filters. No network, no email."""
from datetime import datetime

import portfolio_digest as pd
from keywords import matches


# --- keywords.matches ---------------------------------------------------------

def test_whole_word_only():
    assert not matches("Simplification of compliance process", ["pli", "cess", "rpo"])
    assert not matches("examine the determined window", ["mine", "wind"])
    assert matches("Auction of coal mines", ["mine"])


def test_plurals_and_prefix():
    assert matches("Revised royalties on iron ore", ["royalty"])
    assert matches("RE tender for renewables", ["renewable"])
    assert matches("Pledge invoked by lender", ["pledge invo*"])
    assert matches("Company defaulted on NCD interest", ["default*"])


def test_punctuated_terms():
    assert matches("Compliance under Reg. 74 (5) of SEBI DP Regs", ["reg. 74 (5)"])
    assert matches("New F&O framework", ["f&o"])


# --- portfolio_digest -----------------------------------------------------------

def test_digest_classify():
    assert pd.classify("Resignation of auditor", "")[1] == "HIGH"
    # BSE's usual wording puts "Statutory" in the middle of the phrase
    assert pd.classify("Resignation of Statutory Auditor", "")[0] == "RED_FLAG"
    assert pd.classify("Closure of Trading Window", "")[0] is None
    assert pd.classify("Board Meeting Outcome - Financial Results Q2", "")[0] == "EARNINGS"
    # 'asm' used to fire RED_FLAG on any word containing it
    assert pd.classify("Enthusiasm at plasma unit inauguration", "")[0] is None


def test_lookback_filter():
    cutoff = datetime(2026, 10, 2, 5, 0)
    assert pd.within_lookback({"NEWS_DT": "2026-10-02T19:05:23.817"}, cutoff)
    assert not pd.within_lookback({"NEWS_DT": "2026-10-01T23:59:00"}, cutoff)
    assert pd.within_lookback({"NEWS_DT": ""}, cutoff)  # unknown -> keep


def test_render_escapes_html():
    user = {"name": "Test", "_id": "t"}
    holdings = [{"ticker": "ABC", "name": "A&B <Co>", "bse_code": "500001"}]
    results = {"ABC": {"name": "A&B <Co>", "events": [{
        "type": "EARNINGS", "severity": "MEDIUM", "headline": "Q2 <results> & more",
        "category": "", "time": "2026-10-02T10:00", "pdf": ""}]}}
    out = pd.render_html(user, results, holdings)
    assert "Q2 &lt;results&gt; &amp; more" in out
    assert "<results>" not in out

