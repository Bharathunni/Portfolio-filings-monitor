#!/usr/bin/env python3
"""
Render a sample digest from SYNTHETIC filings. No network, no email.

The companies, codes and headlines below are fictional. Each raw announcement
goes through the same classify() / events_for_user() / render_html() path the
live run uses, so the output shows exactly what the rule engine keeps, drops
and how it ranks severity.

    python examples/render_sample.py      # writes docs/sample-digest.html
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import portfolio_digest as pd  # noqa: E402

USER = {"name": "Sample Investor", "_id": "sample"}

HOLDINGS = [
    {"ticker": "ALPHACEM", "name": "Alpha Cement Ltd", "bse_code": "900001", "qty": 120, "avg_price": 410.0},
    {"ticker": "BETAFIN", "name": "Beta Finance Ltd", "bse_code": "900002", "qty": 80, "avg_price": 960.0},
    {"ticker": "GAMMAPWR", "name": "Gamma Power Ltd", "bse_code": "900003", "qty": 300, "avg_price": 185.0},
    {"ticker": "DELTAIT", "name": "Delta Infotech Ltd", "bse_code": "900004", "qty": 45, "avg_price": 1520.0},
    {"ticker": "EPSILON", "name": "Epsilon Consumer Ltd", "bse_code": "900005", "qty": 60, "avg_price": 2210.0},
    {"ticker": "INDEXETF", "name": "Sample Index ETF", "bse_code": "", "qty": 500, "avg_price": 240.0,
     "note": "ETF - no company filings"},
]


def ann(subject, category, ts):
    return {"NEWSSUB": subject, "CATEGORYNAME": category, "NEWS_DT": ts, "ATTACHMENTNAME": ""}


# Raw feed as the exchange would return it: material items mixed with routine noise.
CACHE = {
    "900001": [
        ann("Resignation of Statutory Auditor", "Company Update", "2026-10-03T18:42:10"),
        ann("Closure of Trading Window", "Company Update", "2026-10-03T11:05:00"),
    ],
    "900002": [
        ann("Outcome of Board Meeting - Financial Results for Q2 FY27", "Result", "2026-10-03T16:30:00"),
        ann("Credit Rating reaffirmed by rating agency", "Company Update", "2026-10-03T12:15:00"),
        ann("Newspaper Publication of financial results", "Company Update", "2026-10-03T09:00:00"),
    ],
    "900003": [
        ann("Receipt of Order worth Rs 240 crore from state utility", "Company Update", "2026-10-03T14:20:00"),
    ],
    "900004": [
        ann("Certificate under Reg. 74 (5) of SEBI (DP) Regulations, 2018", "Compliance", "2026-10-03T10:10:00"),
        ann("Analyst / Institutional Investor Meet - Intimation", "Company Update", "2026-10-03T08:45:00"),
    ],
    "900005": [],
}


def main():
    results = pd.events_for_user(HOLDINGS, CACHE)
    body = pd.render_html(USER, results, HOLDINGS)
    out = ROOT / "docs" / "sample-digest.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(body)
    kept = sum(len(d["events"]) for d in results.values())
    raw = sum(len(v) for v in CACHE.values())
    print(f"wrote {out.relative_to(ROOT)}: {raw} raw filings -> {kept} material")


if __name__ == "__main__":
    main()
