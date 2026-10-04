#!/usr/bin/env python3
"""
Multi-User Portfolio Intelligence Digest Mailer
================================================
Fetches BSE corporate announcements for N users' portfolios, filters for
MATERIAL events only, and emails each user their own formatted digest via
Gmail SMTP (App Password). Runs unattended via GitHub Actions at 7 AM IST.

MULTI-USER MODEL
----------------
- Each user = one JSON file in ./portfolios/ (e.g. portfolios/sample.json)
- Adding a user = drop a new <name>.json file in that folder. Nothing else.
- Files starting with "_" are ignored (use _TEMPLATE.json as the template).
- Each user file has: name, email (comma-separated for multiple recipients),
  optional sheet_csv_url, and holdings[].

DYNAMIC PORTFOLIO (two layers)
------------------------------
1. Git layer: the script globs portfolios/*.json at runtime. Any commit that
   edits a user file is automatically live on the next scheduled run.
2. Sheet layer (no git needed): if a user sets "sheet_csv_url" to a published
   Google Sheet CSV link, holdings are pulled LIVE from the sheet each run.
   Edit the sheet from your phone -> next 7 AM run reflects it. The inline
   "holdings" array then acts as a fallback if the sheet fetch fails.
   Sheet columns (header row required): ticker,name,bse_code,qty,avg_price

EFFICIENCY
----------
BSE is hit ONCE per unique scrip code across ALL users (shared cache), so
5 users holding RELIANCE = 1 API call, not 5.

ENV VARS
--------
GMAIL_SENDER        sending Gmail address (required)
GMAIL_APP_PASSWORD  16-char app password (required unless --dry-run/--validate)
DIGEST_LOOKBACK_HOURS  default 26

CLI
---
python portfolio_digest.py                 # full run, all users
python portfolio_digest.py --validate      # schema check only, no network/email
python portfolio_digest.py --dry-run       # fetch + render, write HTML to disk, no email
python portfolio_digest.py --user sample   # run for one user only
python portfolio_digest.py --limit 5       # smoke test: cap scrips fetched
"""

import argparse
import csv
import html
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

from keywords import matches

# ---------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
PORTFOLIO_DIR = BASE_DIR / "portfolios"
LOOKBACK_HOURS = int(os.environ.get("DIGEST_LOOKBACK_HOURS", "26"))

# GitHub runners are on UTC. BSE timestamps (NEWS_DT) are IST wall-clock, so
# every date window and label in this script is computed in IST.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Current IST time as a naive datetime (comparable with BSE NEWS_DT)."""
    return datetime.now(IST).replace(tzinfo=None)

# ---------------------------------------------------------------
# MATERIALITY FILTER
# ---------------------------------------------------------------
NOISE_PATTERNS = [
    "trading window",
    "newspaper publication",
    "certificate under reg",
    "compliance certificate",
    "reg. 74 (5)",
    "regulation 74",
    "reg. 39",
    "loss of share certificate",
    "duplicate share certificate",
    "investor meet - intimation",
    "analyst / institutional investor meet - intimation",
    "esop",
    "espp",
    "allotment of equity shares under",
    "book closure",
    "share transfer agent",
    "registrar",
    "change in company secretary",
    "postal ballot - intimation",
    "reg 40(9)",
    "reg 7(3)",
    "pcs certificate",
    "spurt in volume",
    "clarification not sought",
]

HIGH_SIGNAL_PATTERNS = {
    "RED_FLAG": [
        "resignation of auditor", "auditor resignation", "qualified opinion",
        "resignation of statutory auditor", "statutory auditor resign*",
        "forensic*", "fraud*", "default*", "insolvency", "ibc", "nclt",
        "suspension of trading", "demat suspension", "gst raid", "income tax raid",
        "search and seizure", "sebi order", "surveillance measure", "asm", "gsm",
        "pledge invo*", "invocation of pledge",
    ],
    "MANAGEMENT": [
        "resignation of managing director", "resignation of md",
        "resignation of chief financial officer", "resignation of cfo",
        "resignation of ceo", "appointment of managing director",
        "appointment of chief financial officer", "appointment of ceo",
    ],
    "EARNINGS": [
        "financial results", "outcome of board meeting",
    ],
    "DIVIDEND": ["dividend", "record date"],
    "CAPITAL": [
        "buyback", "buy back", "rights issue", "qip", "preferential issue",
        "preferential allotment", "bonus issue", "stock split", "sub-division",
        "fund raising", "raising of funds",
    ],
    "M&A": [
        "scheme of arrangement", "amalgamation", "merger", "demerger",
        "acquisition", "divestment", "slump sale", "joint venture",
    ],
    "RATING": ["credit rating", "rating action", "rating upgrade", "rating downgrade"],
    "PLEDGE": ["pledge", "encumbrance", "sast", "insider trading"],
    "ORDERS": ["bagging", "award of order", "receipt of order", "letter of intent", "contract"],
}

SEVERITY = {
    "RED_FLAG": "HIGH", "MANAGEMENT": "HIGH", "PLEDGE": "HIGH",
    "EARNINGS": "MEDIUM", "DIVIDEND": "MEDIUM", "CAPITAL": "MEDIUM",
    "M&A": "MEDIUM", "RATING": "MEDIUM", "ORDERS": "LOW",
}

SEV_COLOR = {"HIGH": "#dc2626", "MEDIUM": "#b45309", "LOW": "#0f6e56"}
SEV_BG = {"HIGH": "#fef2f2", "MEDIUM": "#fffbeb", "LOW": "#f0fdf4"}


# ---------------------------------------------------------------
# USER DISCOVERY + HOLDINGS LOADING
# ---------------------------------------------------------------
def discover_users() -> list:
    """Every non-underscore *.json in ./portfolios/ is a user. Fails loud."""
    if not PORTFOLIO_DIR.is_dir():
        raise RuntimeError(
            f"Missing directory {PORTFOLIO_DIR}. Create it and add <name>.json per user.")
    files = sorted(p for p in PORTFOLIO_DIR.glob("*.json")
                   if not p.name.startswith("_"))
    if not files:
        raise RuntimeError(
            f"No user files in {PORTFOLIO_DIR}. Copy _TEMPLATE.json to <name>.json.")
    users = []
    seen_emails = set()
    for f in files:
        try:
            with open(f) as fh:
                u = json.load(fh)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"{f.name}: invalid JSON ({e})")
        if not isinstance(u, dict):
            raise RuntimeError(
                f"{f.name}: must be an object with name/email/holdings, "
                f"not a bare array. See _TEMPLATE.json.")
        email = (u.get("email") or "").strip()
        if not email or "@" not in email:
            raise RuntimeError(f"{f.name}: missing or invalid 'email'")
        u["_file"] = f.name
        u["_id"] = f.stem
        u.setdefault("name", f.stem.title())
        u["_recipients"] = [e.strip() for e in email.split(",") if e.strip()]
        dup = seen_emails & set(u["_recipients"])
        if dup:
            print(f"  [warn] {f.name}: recipient(s) {dup} already used by "
                  f"another user file; they will receive multiple digests",
                  file=sys.stderr)
        seen_emails |= set(u["_recipients"])
        users.append(u)
    return users


def load_holdings(user: dict) -> list:
    """Live Google Sheet CSV if configured, else inline holdings. Fails loud
    for THIS user only; caller isolates the failure."""
    url = (user.get("sheet_csv_url") or "").strip()
    if url:
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(r.text)))
            holdings = []
            for raw in rows:
                row = {(k or "").strip().lower(): (v or "").strip()
                       for k, v in raw.items()}
                if not row.get("ticker"):
                    continue
                holdings.append({
                    "ticker": row["ticker"].upper(),
                    "name": row.get("name") or row["ticker"],
                    "bse_code": row.get("bse_code", ""),
                    "qty": float(row.get("qty") or 0),
                    "avg_price": float(row.get("avg_price") or 0),
                    "note": row.get("note", ""),
                })
            if not holdings:
                raise RuntimeError("sheet parsed but returned 0 holdings")
            print(f"  [{user['_id']}] {len(holdings)} holdings loaded LIVE from sheet")
            return holdings
        except Exception as e:
            print(f"  [warn] {user['_id']}: sheet fetch failed ({e}); "
                  f"falling back to inline holdings in {user['_file']}",
                  file=sys.stderr)
    holdings = user.get("holdings") or []
    if not holdings:
        raise RuntimeError(
            f"{user['_file']}: no holdings available (inline array empty "
            f"and sheet missing/unreachable)")
    print(f"  [{user['_id']}] {len(holdings)} holdings from {user['_file']}")
    return holdings


def monitorable_code(stock: dict) -> str:
    """Return numeric BSE code or '' (skips blanks, VERIFY_*, ETFs)."""
    code = str(stock.get("bse_code", "")).strip()
    return code if code.isdigit() else ""


# ---------------------------------------------------------------
# BSE ANNOUNCEMENTS API (shared cache across all users)
# ---------------------------------------------------------------
BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_bse_announcements(scrip_code: str, from_date: str, to_date: str) -> list:
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    params = {
        "pageno": 1, "strCat": "-1", "strPrevDate": from_date,
        "strScrip": scrip_code, "strSearch": "P", "strToDate": to_date,
        "strType": "C", "subcategory": "-1",
    }
    try:
        r = requests.get(url, params=params, headers=BSE_HEADERS, timeout=20)
        r.raise_for_status()
        return r.json().get("Table", []) or []
    except Exception as e:
        print(f"  [warn] BSE fetch failed for {scrip_code}: {e}", file=sys.stderr)
        return []


def parse_bse_time(raw: str):
    """BSE NEWS_DT looks like '2026-10-02T19:05:23.817' (IST). None if unparseable."""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt)
        except ValueError:
            continue
    return None


def within_lookback(ann: dict, cutoff: datetime) -> bool:
    """BSE's API filters by calendar DATE only, so a 07:00 run would otherwise
    re-report everything filed between midnight and 07:00 the previous day.
    Unparseable timestamps are kept (better a duplicate than a miss)."""
    ts = parse_bse_time(ann.get("NEWS_DT") or ann.get("DT_TM") or "")
    return ts is None or ts >= cutoff


def build_announcement_cache(user_holdings: list, limit: int = 0) -> dict:
    """One BSE call per UNIQUE scrip across all users. Returns {code: [anns]}."""
    now = now_ist()
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    frm = cutoff.strftime("%Y%m%d")
    to = now.strftime("%Y%m%d")
    codes = []
    for _, holdings in user_holdings:
        for s in holdings:
            code = monitorable_code(s)
            if code and code not in codes:
                codes.append(code)
    if limit:
        codes = codes[:limit]
        print(f"  [smoke test] fetching only first {limit} unique scrips")
    print(f"  {len(codes)} unique scrips across all users "
          f"(window {frm} -> {to})")
    cache = {}
    for code in codes:
        anns = fetch_bse_announcements(code, frm, to)
        cache[code] = [a for a in anns if within_lookback(a, cutoff)]
    return cache


def classify(subject: str, category: str) -> tuple:
    text = f"{subject} {category}"
    for etype in ["RED_FLAG", "MANAGEMENT", "PLEDGE"]:
        if matches(text, HIGH_SIGNAL_PATTERNS[etype]):
            return etype, SEVERITY[etype]
    if matches(text, NOISE_PATTERNS):
        return None, None
    for etype, patterns in HIGH_SIGNAL_PATTERNS.items():
        if etype in ("RED_FLAG", "MANAGEMENT", "PLEDGE"):
            continue
        if matches(text, patterns):
            return etype, SEVERITY[etype]
    return None, None


def events_for_user(holdings: list, cache: dict) -> dict:
    """Classify cached announcements against ONE user's holdings."""
    results = {}
    for stock in holdings:
        code = monitorable_code(stock)
        if not code or code not in cache:
            continue
        events = []
        for a in cache[code]:
            subject = a.get("NEWSSUB", "") or a.get("HEADLINE", "")
            category = a.get("CATEGORYNAME", "")
            etype, sev = classify(subject, category)
            if not etype:
                continue
            attach = a.get("ATTACHMENTNAME", "")
            pdf_url = (f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attach}"
                       if attach else "")
            events.append({
                "type": etype, "severity": sev,
                "headline": subject.strip()[:300],
                "category": category, "time": a.get("NEWS_DT", ""), "pdf": pdf_url,
            })
        if events:
            results[stock["ticker"]] = {"name": stock["name"], "events": events}
    return results


# ---------------------------------------------------------------
# EMAIL RENDERING + SMTP SEND
# ---------------------------------------------------------------
def render_html(user: dict, results: dict, holdings: list) -> str:
    esc = html.escape
    today = now_ist().strftime("%A, %d %B %Y")
    n_total = len(holdings)
    n_alerts = sum(1 for d in results.values()
                   if any(e["severity"] == "HIGH" for e in d["events"]))
    monitored = [s for s in holdings if monitorable_code(s)]
    unverified = [s["ticker"] for s in holdings
                  if not monitorable_code(s) and not s.get("note")]
    quiet = [s["ticker"] for s in monitored if s["ticker"] not in results]

    blocks = []
    ordered = sorted(results.items(),
                     key=lambda kv: 0 if any(e["severity"] == "HIGH"
                                             for e in kv[1]["events"]) else 1)
    for tkr, data in ordered:
        has_high = any(e["severity"] == "HIGH" for e in data["events"])
        evs = []
        for e in data["events"]:
            link = (f'&nbsp;<a href="{esc(e["pdf"])}" style="color:#185FA5;font-size:11px">'
                    f'[filing PDF]</a>' if e["pdf"] else "")
            evs.append(
                f'<div style="margin:8px 0;padding:10px 12px;background:{SEV_BG[e["severity"]]};'
                f'border-left:3px solid {SEV_COLOR[e["severity"]]};border-radius:4px">'
                f'<div style="font-size:10px;font-weight:700;color:{SEV_COLOR[e["severity"]]}">'
                f'{e["type"]} · {e["severity"]} · {esc(e["time"][:16])}</div>'
                f'<div style="font-size:13px;color:#111827;margin-top:3px">{esc(e["headline"])}{link}</div>'
                f'</div>')
        blocks.append(
            f'<div style="background:#fff;border-left:4px solid '
            f'{"#dc2626" if has_high else "#3b82f6"};padding:14px 20px;margin-top:2px">'
            f'<div style="font-size:15px;font-weight:700;color:#185FA5">{esc(tkr)}'
            f'<span style="font-weight:400;font-size:12px;color:#64748b;margin-left:8px">'
            f'{esc(data["name"])}</span></div>{"".join(evs)}</div>')

    body = "".join(blocks) if blocks else (
        f'<div style="background:#fff;padding:30px;text-align:center;color:#6b7280">'
        f'No material filings across {len(monitored)} monitored holdings '
        f'in the lookback window.</div>')

    unver_line = (f'<br><b style="color:#94a3b8">Unverified BSE codes (not monitored):</b><br>'
                  f'{esc(" · ".join(unverified))}' if unverified else "")

    return f"""<!DOCTYPE html><html><body style="margin:0;background:#f1f5f9;font-family:Arial,sans-serif">
<div style="max-width:660px;margin:0 auto;padding:16px 0">
<div style="background:#0f172a;border-radius:10px 10px 0 0;padding:20px 26px">
<div style="color:#f8fafc;font-size:19px;font-weight:700">Portfolio Intelligence Digest</div>
<div style="color:#94a3b8;font-size:12px;margin-top:4px">{esc(user['name'])} · {today} ·
{n_total} holdings · {len(monitored)} monitored · {len(results)} with material filings ·
{n_alerts} high-severity</div></div>
{body}
<div style="background:#0f172a;border-radius:0 0 10px 10px;padding:14px 26px;margin-top:2px">
<div style="color:#64748b;font-size:11px;line-height:1.7">
<b style="color:#94a3b8">Quiet today (routine filings filtered):</b><br>{esc(" · ".join(quiet))}
{unver_line}</div>
<div style="border-top:1px solid #1e293b;margin-top:10px;padding-top:10px;color:#475569;font-size:10px">
Auto-generated {now_ist().strftime("%d %b %Y %H:%M IST")} · BSE filings API · material-only filter
</div></div></div></body></html>"""


def smtp_connect():
    import smtplib
    sender = os.environ.get("GMAIL_SENDER", "").strip()
    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not sender:
        raise RuntimeError("GMAIL_SENDER env var not set (the sending Gmail address)")
    if not app_pw:
        raise RuntimeError("GMAIL_APP_PASSWORD env var not set. "
                           "Create one at myaccount.google.com/apppasswords")
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
    s.login(sender, app_pw)
    return s, sender


def send_digest(smtp, sender: str, user: dict, html: str, subject: str):
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = ", ".join(user["_recipients"])
    msg["Subject"] = subject
    msg.attach(MIMEText(html, "html"))
    smtp.sendmail(sender, user["_recipients"], msg.as_string())
    print(f"  [{user['_id']}] sent to {msg['To']}")


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true",
                    help="validate user files only; no network, no email")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + render, write HTML files, skip email")
    ap.add_argument("--user", default="",
                    help="run for one user id (filename stem) only")
    ap.add_argument("--limit", type=int, default=0,
                    help="smoke test: cap number of unique scrips fetched")
    args = ap.parse_args()

    print(f"=== Multi-User Portfolio Digest run: {now_ist():%Y-%m-%d %H:%M} IST ===")
    users = discover_users()
    if args.user:
        users = [u for u in users if u["_id"] == args.user]
        if not users:
            raise RuntimeError(f"No user file named {args.user}.json in {PORTFOLIO_DIR}")
    print(f"  {len(users)} user(s): {', '.join(u['_id'] for u in users)}")

    # Load holdings per user; isolate per-user failures
    user_holdings, failures = [], []
    for u in users:
        try:
            user_holdings.append((u, load_holdings(u)))
        except Exception as e:
            failures.append(f"{u['_id']}: {e}")
            print(f"  [ERROR] skipping {u['_id']}: {e}", file=sys.stderr)

    if args.validate:
        for u, h in user_holdings:
            n_mon = sum(1 for s in h if monitorable_code(s))
            n_unv = sum(1 for s in h if not monitorable_code(s) and not s.get("note"))
            print(f"  OK {u['_id']}: {len(h)} holdings, {n_mon} monitorable, "
                  f"{n_unv} unverified, recipients={u['_recipients']}")
        if failures:
            raise RuntimeError("Validation failures: " + "; ".join(failures))
        print("=== validate: all user files OK ===")
        return

    if not user_holdings:
        raise RuntimeError("No users loaded successfully: " + "; ".join(failures))

    # ONE fetch pass across all users
    cache = build_announcement_cache(user_holdings, limit=args.limit)

    smtp, sender = (None, "")
    if not args.dry_run:
        smtp, sender = smtp_connect()

    try:
        for user, holdings in user_holdings:
            try:
                results = events_for_user(holdings, cache)
                n_high = sum(1 for d in results.values()
                             if any(e["severity"] == "HIGH" for e in d["events"]))
                subject = (f"Portfolio Digest {now_ist().strftime('%d %b')} | "
                           f"{user['name']} | {len(results)} material"
                           + (f" | {n_high} ALERTS" if n_high else ""))
                body = render_html(user, results, holdings)
                if args.dry_run:
                    out = BASE_DIR / f"digest_{user['_id']}.html"
                    out.write_text(body)
                    print(f"  [{user['_id']}] dry-run: wrote {out.name} "
                          f"({len(results)} material, {n_high} high)")
                else:
                    send_digest(smtp, sender, user, body, subject)
            except Exception as e:
                failures.append(f"{user['_id']}: {e}")
                print(f"  [ERROR] digest failed for {user['_id']}: {e}", file=sys.stderr)
    finally:
        if smtp:
            smtp.quit()

    if failures:
        # fail loud so GitHub Actions marks the run red and emails you
        raise RuntimeError("Run completed with failures: " + "; ".join(failures))
    print("=== all digests sent ===")


if __name__ == "__main__":
    main()
