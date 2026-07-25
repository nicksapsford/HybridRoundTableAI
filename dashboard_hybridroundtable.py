"""
The Round Table -- dashboard_hybridroundtable.py
Master monitoring dashboard for the Albion Trading Desk.

Watches all four trading systems at once (CryptoTrader, FTSETrader,
GoldTrader, USTrader) and renders them as full-width rows on one page.

Design notes:
  * The four systems each run their own dashboard on a different port. A browser
    fetch() from :5050 to :5001-:5004 is cross-origin (different port = different
    origin) and would be blocked by CORS. So THIS backend polls each system's
    /api/state server-side, normalises the very different per-system schemas into
    one uniform shape, and exposes a single same-origin GET /api/systems that the
    page polls every 15s. Shutdown is likewise proxied server-side.
  * Response() is used directly -- no render_template_string.
  * The client JS uses string concatenation only -- no backtick template literals.
  * A per-system 5s fetch timeout marks a system OFFLINE without ever crashing the
    aggregate view; the last known good snapshot is retained and greyed out.

Port: 5050.  No logs folder, no .env -- this dashboard only reads other systems.
"""

import csv
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, request

# Semantic version -- read from the VERSION file so the header always shows the
# current version without a code change.
_BASE = Path(__file__).resolve().parent
_VER = _BASE / "VERSION"
APP_VERSION = _VER.read_text().strip() if _VER.exists() else "1.0.0"


def get_git_hash():
    try:
        result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
        return result.stdout.strip() or 'unknown'
    except Exception:
        return 'unknown'


VERSION_STRING = "v" + str(APP_VERSION) + " (" + get_git_hash() + ")"


def get_stay_out_quality():
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'phantom_trades.csv')
    if not os.path.exists(csv_path):
        return {'status': 'No data yet', 'decisions': [], 'quality_score': None,
                'net_saved': None, 'correct': 0, 'wrong': 0, 'neutral': 0}
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        last_10 = rows[-10:]
        correct = sum(1 for r in last_10 if r.get('verdict') == 'CORRECT')
        wrong   = sum(1 for r in last_10 if r.get('verdict') == 'WRONG')
        neutral = sum(1 for r in last_10 if r.get('verdict') == 'NEUTRAL')
        total   = (correct + wrong + neutral)
        quality_score = round((correct / total) * 100) if total else 0
        net_saved  = sum(float(r.get('pnl_1hr', 0) or 0) for r in last_10 if r.get('verdict') == 'CORRECT')
        net_missed = sum(float(r.get('pnl_1hr', 0) or 0) for r in last_10 if r.get('verdict') == 'WRONG')
        return {'status': 'ok', 'decisions': last_10, 'quality_score': quality_score,
                'net_saved': net_saved, 'net_missed': net_missed, 'correct': correct, 'wrong': wrong, 'neutral': neutral}
    except Exception as e:
        return {'status': 'Error: ' + str(e), 'decisions': []}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-22s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("RoundTable")

app = Flask(__name__)

PORT         = 5050
FETCH_TIMEOUT = 5           # seconds per system before it is marked OFFLINE
BG_COLOUR    = "#0d0d0d"
TEXT_COLOUR  = "#e0e0e0"
BORDER       = "#333333"

# ── System registry ───────────────────────────────────────────────────────────

# Hybrid Desk systems (port = original + 40). orig_port / bench_port let the
# HybridRoundTable read the matched Original- and Benchmark-desk P&L for the
# three-way comparison. start = each system's starting balance (same across desks).
SYSTEMS = [
    {"key": "crypto", "name": "CryptoHybrid", "market": "BTC & ETH",   "colour": "#00d4ff", "port": 5041, "kind": "crypto", "orig_port": 5001, "bench_port": 5021, "start": 2000.0},
    {"key": "albion", "name": "FTSEHybrid",   "market": "FTSE 100",    "colour": "#4169E1", "port": 5042, "kind": "ftse",   "orig_port": 5002, "bench_port": 5022, "start": 1000.0},
    {"key": "gold",   "name": "GoldHybrid",   "market": "GOLD (XAU)",  "colour": "#FFD700", "port": 5043, "kind": "gold",   "orig_port": 5003, "bench_port": 5023, "start": 1000.0},
    {"key": "us",     "name": "USHybrid",     "market": "S&P 500",     "colour": "#FFFFFF", "port": 5044, "kind": "us",     "orig_port": 5004, "bench_port": 5024, "start": 1000.0},
    {"key": "oil",    "name": "OilHybrid",    "market": "Brent Crude", "colour": "#FF6600", "port": 5045, "kind": "oil",    "orig_port": 5005, "bench_port": 5025, "start": 1000.0},
    {"key": "gas",    "name": "GasHybrid",    "market": "Natural Gas", "colour": "#228B22", "port": 5046, "kind": "gas",    "orig_port": 5006, "bench_port": 5026, "start": 1000.0},
    {"key": "nikkei", "name": "NikkeiHybrid", "market": "Japan 225",   "colour": "#C8102E", "port": 5048, "kind": "nikkei", "orig_port": 5008, "bench_port": None, "start": 1000.0},
]
SYS_BY_KEY = {s["key"]: s for s in SYSTEMS}

# Last known good normalised snapshot per system (for graceful offline display).
_last_good: dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fnum(v, default=None):
    try:
        f = float(v)
        return None if f != f else f  # NaN guard
    except (TypeError, ValueError):
        return default


def _norm_decision(dec):
    """Collapse a system's decision (dict or string or None) to a badge label."""
    if isinstance(dec, dict):
        dec = dec.get("decision")
    if not dec:
        return "STAY OUT"
    s = str(dec).upper()
    if "LONG" in s:  return "LONG"
    if "SHORT" in s: return "SHORT"
    if "EXIT" in s:  return "EXIT"
    if "HOLD" in s:  return "HOLD"
    return "STAY OUT"


def _confidence(state, kind):
    perf = state.get("performance" if kind == "crypto" else "perf") or {}
    c = _fnum(perf.get("confidence_score"))
    if c is None:
        return None
    return max(0, min(100, int(c)))


def _price_num(v):
    """Price sentinel: treat 0 / None / missing / NaN as 'no price' so callers
    can render '---' instead of a misleading £0."""
    f = _fnum(v)
    if f is None or f == 0:
        return None
    return f


def _price_html(state, kind):
    if kind == "crypto":
        # BTC is top-level `price`; ETH lives under state['eth']['price']
        # (confirmed against the live CryptoTrader payload on :5001).
        btc = _price_num(state.get("price"))
        eth = _price_num((state.get("eth") or {}).get("price"))
        parts = []
        parts.append("BTC " + ("£{:,.0f}".format(btc) if btc is not None else "---"))
        parts.append("ETH " + ("£{:,.0f}".format(eth) if eth is not None else "---"))
        return " | ".join(parts)
    if kind == "ftse":
        v = _price_num(state.get("ftse_level"))
        return "FTSE " + ("{:,.0f}".format(v) if v is not None else "---")
    if kind == "nikkei":
        v = _price_num(state.get("nikkei_level"))
        return "NIKKEI " + ("{:,.0f}".format(v) if v is not None else "---")
    if kind == "gold":
        v = _price_num(state.get("gold_price_usd"))
        return "GOLD " + ("${:,.0f}".format(v) if v is not None else "---")
    if kind == "us":
        v = _price_num(state.get("us_level"))
        return "US500 " + ("{:,.0f}".format(v) if v is not None else "---")
    if kind == "oil":
        v = _price_num(state.get("oil_price_usd"))
        return "BRENT " + ("${:,.2f}".format(v) if v is not None else "---")
    if kind == "gas":
        v = _price_num(state.get("gas_price_usd") or state.get("oil_price_usd"))
        return "NATGAS " + ("${:,.2f}".format(v) if v is not None else "---")
    return "---"


def _current_price(state, kind):
    if kind == "crypto":
        return _fnum(state.get("price"))
    if kind == "ftse":
        return _fnum(state.get("ftse_level"))
    if kind == "nikkei":
        return _fnum(state.get("nikkei_level"))
    if kind == "gold":
        return _fnum(state.get("gold_price_usd"))
    if kind == "us":
        return _fnum(state.get("us_level"))
    if kind == "oil":
        return _fnum(state.get("oil_price_usd"))
    if kind == "gas":
        return _fnum(state.get("gas_price_usd") or state.get("oil_price_usd"))
    return None


def _position(state, kind):
    """
    Return (state_label, entry, float_pts, float_gbp, glow, active_note).
    state_label: NO_TRADE | LONG | SHORT.  glow: '' | 'long' | 'short'.
    """
    trade = None
    note = ""
    if kind == "crypto":
        # Show whichever pair has an active trade; BTC takes precedence.
        if state.get("position"):
            trade, note = state.get("position"), "BTC"
        elif (state.get("eth") or {}).get("position"):
            trade, note = state["eth"]["position"], "ETH"
    else:
        trade = state.get("current_trade")

    if not isinstance(trade, dict):
        return ("NO_TRADE", None, None, None, "", note)

    direction = str(trade.get("direction", "")).upper()
    if direction not in ("LONG", "SHORT"):
        return ("NO_TRADE", None, None, None, "", note)

    entry = _fnum(trade.get("entry_price")) or _fnum(trade.get("entry"))
    stake = _fnum(trade.get("stake")) or _fnum(trade.get("stake_per_point"))
    price = _current_price(state, kind)

    float_pts = None
    float_gbp = None
    # Prefer a live P&L field if the system provides one.
    for k in ("pnl_gbp", "unrealised_gbp", "floating_gbp", "pnl"):
        if trade.get(k) not in (None, "", "None"):
            float_gbp = _fnum(trade.get(k))
            break
    # Bug B: Oil/Gas push a top-level unrealised_gbp (spread-inclusive) -- prefer it.
    if float_gbp is None and kind in ("oil", "gas"):
        float_gbp = _fnum(state.get("unrealised_gbp"))
    # Otherwise compute points (point-based systems) from entry vs current price.
    # Bug B: oil and gas added -- their open-position floating P&L was not shown.
    if entry is not None and price is not None and kind in ("ftse", "nikkei", "gold", "us", "oil", "gas"):
        float_pts = (price - entry) if direction == "LONG" else (entry - price)
        if float_gbp is None and stake:
            float_gbp = float_pts * stake

    glow = "long" if direction == "LONG" else "short"
    return (direction, entry, float_pts, float_gbp, glow, note)


def _balance(state, kind):
    if kind == "crypto":
        b = _fnum(state.get("combined_capital"))
        if b is not None:
            return b
        a = _fnum((state.get("account") or {}).get("capital")) or 0.0
        e = _fnum((state.get("account_eth") or {}).get("capital")) or 0.0
        return a + e
    b = _fnum(state.get("capital"))
    if b is None:
        b = _fnum((state.get("account") or {}).get("capital"))
    return b


def _daily_pnl(state, kind):
    if kind == "crypto":
        a = _fnum((state.get("account") or {}).get("daily_pnl")) or 0.0
        e = _fnum((state.get("account_eth") or {}).get("daily_pnl")) or 0.0
        return a + e
    # account.daily_pnl carries the running session P&L (matches each dashboard).
    v = _fnum((state.get("account") or {}).get("daily_pnl"))
    if v is None:
        v = _fnum(state.get("daily_pnl"))
    return v if v is not None else 0.0


def _decision(state, kind):
    return _norm_decision(state.get("decision"))


def _session_status(kind, now_utc=None):
    """
    Market session line (hours + status), computed STRICTLY in UTC server-side
    so there is never any BST/ET timezone confusion in the browser.
    Mon=0 .. Sun=6.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    wd = now_utc.weekday()
    t  = now_utc.hour * 60 + now_utc.minute

    if kind == "crypto":
        return "24/7 -- ALWAYS ACTIVE \U0001F7E2"           # crypto never closes

    if kind == "ftse":
        hrs = "07:45-16:30 UTC"
        if wd >= 5:
            return hrs + " ⚫ WEEKEND"
        if (7 * 60 + 45) <= t < (16 * 60 + 30):
            return hrs + " \U0001F7E2 IN SESSION"
        return hrs + " ⚫ OUT OF SESSION"

    if kind == "nikkei":
        hrs = "00:00-06:30 UTC"
        if wd >= 5:
            return hrs + " ⚫ WEEKEND"
        if 0 <= t < (6 * 60 + 30):
            return hrs + " \U0001F7E2 IN SESSION"
        return hrs + " ⚫ OUT OF SESSION"

    if kind == "us":
        hrs = "14:30-21:00 UTC"
        if wd >= 5:
            return hrs + " ⚫ WEEKEND"
        if (14 * 60 + 30) <= t < (21 * 60):
            return hrs + " \U0001F7E2 IN SESSION"
        return hrs + " ⚫ OUT OF SESSION"

    if kind == "gold":
        hrs = "22:00-21:00 UTC"
        # Weekend closed: Fri >= 21:00 UTC, all Saturday, Sunday before 22:00 UTC.
        if (wd == 4 and t >= 21 * 60) or wd == 5 or (wd == 6 and t < 22 * 60):
            return hrs + " ⚫ WEEKEND"
        # Daily maintenance break 21:00-22:00 UTC on trading days.
        if (21 * 60) <= t < (22 * 60):
            return hrs + " ⚫ DAILY BREAK"
        return hrs + " \U0001F7E2 ACTIVE"

    if kind == "oil":
        # Brent Crude trades a ~23h day like Gold (daily break 21:00-22:00 UTC).
        hrs = "22:00-21:00 UTC"
        if (wd == 4 and t >= 21 * 60) or wd == 5 or (wd == 6 and t < 22 * 60):
            return hrs + " ⚫ WEEKEND"
        if (21 * 60) <= t < (22 * 60):
            return hrs + " ⚫ DAILY BREAK"
        return hrs + " \U0001F7E2 ACTIVE"

    if kind == "gas":
        # Natural Gas trades a ~23h day like Oil/Gold (daily break 21:00-22:00 UTC).
        hrs = "22:00-21:00 UTC"
        if (wd == 4 and t >= 21 * 60) or wd == 5 or (wd == 6 and t < 22 * 60):
            return hrs + " ⚫ WEEKEND"
        if (21 * 60) <= t < (22 * 60):
            return hrs + " ⚫ DAILY BREAK"
        return hrs + " \U0001F7E2 ACTIVE"

    return ""


def _session_is_active(session_line):
    """Snag 18: True only when the market session line says the market is open.
    OUT OF SESSION / WEEKEND / DAILY BREAK -> False, so RoundTable can show Lancelot
    as BLOCKED rather than a stale cached fail count once a session ends naturally."""
    s = (session_line or "").upper()
    if "OUT OF SESSION" in s or "WEEKEND" in s or "DAILY BREAK" in s:
        return False
    return True


def _locked_with_ladder(state):
    """Locked profit for the Locked column: the profit-ladder floor (Variant 2)
    counts as locked even before the trailing stop crosses break-even."""
    ct = state.get("current_trade") or {}
    try:
        floor = float(ct.get("ladder_floor_gbp") or 0)
        step = int(float(ct.get("ladder_step") or 0))
    except (TypeError, ValueError):
        floor, step = 0.0, 0
    base = state.get("locked_pnl")
    if step > 0:
        return round(max(floor, base or 0.0), 2)
    return base


def _phantom_recent(state, n=5):
    """Last n phantom stay-out decisions (with 30m/1hr/2hr moves) from a system's
    /api/state, trimmed to the fields the RoundTable Archie brief needs."""
    soq = state.get("stay_out_quality") or {}
    decs = soq.get("decisions") or []
    keep = ("timestamp", "market", "direction_blocked", "confidence",
            "pnl_30min", "pnl_1hr", "pnl_2hr", "verdict")
    return [{k: d.get(k) for k in keep} for d in decs[-n:] if isinstance(d, dict)]


def _normalise(cfg, state):
    """Turn a raw /api/state payload into the uniform row shape."""
    kind = cfg["kind"]
    pos_label, entry, fpts, fgbp, glow, note = _position(state, kind)
    return {
        "key":        cfg["key"],
        "name":       cfg["name"],
        "market":     cfg["market"],
        "colour":     cfg["colour"],
        "port":       cfg["port"],
        "status":     "RUNNING",
        "price_html": _price_html(state, kind),
        "position":   pos_label,          # NO_TRADE | LONG | SHORT
        "entry":      entry,
        "pair_note":  note,               # crypto: which pair is in trade
        "float_pts":  fpts,
        "float_gbp":  fgbp,
        "glow":       glow,               # '' | 'long' | 'short'
        "daily_pnl":  _daily_pnl(state, kind),
        "balance":    _balance(state, kind),
        "decision":   _decision(state, kind),
        "confidence": _confidence(state, kind),
        "lancelot_status":  state.get("lancelot_status") or "--",
        "arthur_decision":  state.get("arthur_decision") or _decision(state, kind),
        "arthur_confidence": state.get("arthur_confidence", None),
        "locked_pnl":       _locked_with_ladder(state),
        "phantom_recent":   _phantom_recent(state),
    }


def _read_cum_pnl(port, kind, start, desk):
    """Cumulative P&L (balance - start) for a matched system on the Original or
    Benchmark desk, or None if offline. Same-machine read. NOTE: the two desks push
    different state shapes -- the Original desk exposes balance via 'capital'/account
    (read by _balance), while the Benchmark desk exposes it as portfolio.balance."""
    if not port:
        return None
    try:
        with urllib.request.urlopen("http://localhost:%d/api/state" % port, timeout=FETCH_TIMEOUT) as r:
            st = json.loads(r.read().decode("utf-8", "replace"))
        if desk == "bench":
            bal = _fnum((st.get("portfolio") or {}).get("balance"))
        else:
            bal = _balance(st, kind)
        return round(bal - start, 2) if bal is not None else None
    except Exception:
        return None


def _add_comparison(row, cfg):
    """Attach the three-way comparison: this hybrid system's cum P&L plus the matched
    Original- and Benchmark-desk cum P&L, and Arthur's exit value (Hybrid - Benchmark,
    since both share the Lancelot entry -- the difference is purely Arthur's exits)."""
    start = cfg.get("start", 1000.0)
    bal = row.get("balance")
    row["start"]      = start
    row["hybrid_cum"] = round(bal - start, 2) if bal is not None else None
    row["orig_cum"]   = _read_cum_pnl(cfg.get("orig_port"), cfg["kind"], start, "orig")
    row["bench_cum"]  = _read_cum_pnl(cfg.get("bench_port"), cfg["kind"], start, "bench")
    if row["hybrid_cum"] is not None and row["bench_cum"] is not None:
        row["exit_value"] = round(row["hybrid_cum"] - row["bench_cum"], 2)
    else:
        row["exit_value"] = None


def _fetch_one(cfg):
    """Fetch + normalise a single system. Never raises -- returns an OFFLINE row on error."""
    url = "http://localhost:{}/api/state".format(cfg["port"])
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as r:
            raw = json.loads(r.read().decode("utf-8", "replace"))
        row = _normalise(cfg, raw)
        row["session"] = _session_status(cfg["kind"])
        row["session_active"] = _session_is_active(row["session"])   # Snag 18
        _add_comparison(row, cfg)
        _last_good[cfg["key"]] = row
        return row
    except Exception as exc:                       # noqa: BLE001 -- offline is expected
        log.debug("System %s offline: %s", cfg["key"], exc)
        row = dict(_last_good.get(cfg["key"], {}))
        row.update({
            "key": cfg["key"], "name": cfg["name"], "market": cfg["market"],
            "colour": cfg["colour"], "port": cfg["port"],
            "status": "OFFLINE", "glow": "",
            "session": _session_status(cfg["kind"]),   # market clock is independent of the app
            "session_active": _session_is_active(_session_status(cfg["kind"])),   # Snag 18
        })
        row.setdefault("price_html", "---")
        row.setdefault("position", "---")
        row.setdefault("decision", "---")
        row.setdefault("confidence", None)
        row.setdefault("phantom_recent", [])
        row.setdefault("float_pts", None)
        row.setdefault("float_gbp", None)
        row.setdefault("lancelot_status", "--")
        row.setdefault("arthur_decision", "---")
        row.setdefault("arthur_confidence", None)
        row.setdefault("locked_pnl", None)
        _add_comparison(row, cfg)
        return row


def _gaius_status():
    """Read Gaius's output files (sibling ../GaiusAI/logs) for a minimal status row.
    Collector is stale/ERROR if its last daily snapshot is >25h old; the weekly
    market-data service is ERROR if its last fetch is >8 days old. All times UTC."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "GaiusAI", "logs")
    now = datetime.now(timezone.utc)

    def _last(fname, key):
        try:
            with open(os.path.join(base, fname), "r", encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, list) and d:
                return d[-1].get(key)
        except Exception:
            return None
        return None

    def _age_h(ts):
        if not ts:
            return None
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            return (now - t).total_seconds() / 3600.0
        except Exception:
            return None

    coll = _last("gaius_daily_snapshots.json", "generated_utc")
    mkt  = _last("market_context.json", "fetched_utc")
    ch, mh = _age_h(coll), _age_h(mkt)
    coll_ok = ch is not None and ch <= 25
    mkt_ok  = mh is not None and mh <= 24 * 8
    return {"collector_last": coll, "market_last": mkt,
            "collector_ok": coll_ok, "market_ok": mkt_ok, "ok": coll_ok and mkt_ok}


def _active_interventions():
    """System names with an OPEN Gaius intervention (Phase 2b) -- drives the
    RoundTable telescope indicator. Reads the sibling GaiusAI intervention log."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "GaiusAI", "logs", "gaius_interventions.json")
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
        return {r.get("system") for r in rows
                if isinstance(r, dict) and not r.get("resolved_utc")}
    except Exception:
        return set()


VALID_MACRO = ("RISK_ON", "NEUTRAL", "RISK_OFF", "CRISIS")
MACRO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "macro_sentiment.json")


def read_macro():
    """Current desk-wide macro sentiment flag (Part 4). Reads logs/macro_sentiment.json;
    each Guinevere system reads the same file every 5 min. Defaults to NEUTRAL."""
    try:
        with open(MACRO_FILE, encoding="utf-8") as f:
            d = json.load(f)
        flag = str(d.get("flag", "NEUTRAL")).upper()
        if flag not in VALID_MACRO:
            flag = "NEUTRAL"
        return {"flag": flag, "set_at": d.get("set_at", ""), "set_by": d.get("set_by", "")}
    except Exception:
        return {"flag": "NEUTRAL", "set_at": "", "set_by": ""}


def write_macro(flag, set_by="Nick"):
    """Persist the macro sentiment flag to logs/macro_sentiment.json (Part 4). Written
    ATOMICALLY (temp file + os.replace) so a Guinevere system polling the file live
    (Macro Live Reload, 19 Jul 2026) can never read a half-written flag."""
    flag = str(flag).upper()
    if flag not in VALID_MACRO:
        raise ValueError("invalid macro flag: %s" % flag)
    os.makedirs(os.path.dirname(MACRO_FILE), exist_ok=True)
    data = {"flag": flag,
            "set_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "set_by": set_by or "Nick"}
    tmp = MACRO_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, MACRO_FILE)   # atomic swap
    return data


def _gather():
    """Fetch all systems concurrently so one slow/hung system can't stall the rest."""
    with ThreadPoolExecutor(max_workers=len(SYSTEMS)) as ex:
        rows = list(ex.map(_fetch_one, SYSTEMS))
    active_iv = _active_interventions()
    for r in rows:
        r["intervention_active"] = r.get("name") in active_iv
    running = sum(1 for r in rows if r["status"] == "RUNNING")
    portfolio = sum((r.get("balance") or 0.0) for r in rows if r.get("balance") is not None)
    today = sum((r.get("daily_pnl") or 0.0) for r in rows)

    # ── Three-way comparison (Hybrid vs Original vs Benchmark) ────────────────
    online = [r for r in rows if r["status"] == "RUNNING"]

    def _sum(rs, key):
        vals = [r.get(key) for r in rs if r.get(key) is not None]
        return round(sum(vals), 2) if vals else 0.0

    hybrid_cum = _sum(online, "hybrid_cum")          # hybrid total P&L (online systems)
    orig_cum   = _sum(rows, "orig_cum")              # original desk cum (matched, available)
    bench_cum  = _sum(rows, "bench_cum")             # benchmark desk cum (matched, available)
    # Deltas only over systems where BOTH sides are available (apples-to-apples).
    m_o = [r for r in rows if r.get("hybrid_cum") is not None and r.get("orig_cum") is not None]
    m_b = [r for r in rows if r.get("hybrid_cum") is not None and r.get("bench_cum") is not None]
    delta_orig  = round(_sum(m_o, "hybrid_cum") - _sum(m_o, "orig_cum"), 2)
    delta_bench = round(_sum(m_b, "hybrid_cum") - _sum(m_b, "bench_cum"), 2)  # total Arthur exit value

    return {
        "systems":     rows,
        "portfolio":   round(portfolio, 2),
        "today_pnl":   round(today, 2),
        "running":     running,
        "total":       len(SYSTEMS),
        "hybrid_cum":  hybrid_cum,
        "orig_cum":    orig_cum,
        "bench_cum":   bench_cum,
        "delta_orig":  delta_orig,
        "delta_bench": delta_bench,
        "exit_value":  delta_bench,
        "version":     APP_VERSION,
        "version_string": VERSION_STRING,
        "stay_out_quality": get_stay_out_quality(),
        "gaius":       _gaius_status(),
        "macro":       read_macro(),
        "updated_utc": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/archie-brief")
def api_archie_brief():
    """Plain-text RoundTable snapshot for pasting to Archie."""
    import archie_brief
    txt = archie_brief.build_roundtable_brief(_gather())
    return txt, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/api/systems")
def api_systems():
    return Response(json.dumps(_gather(), default=str), mimetype="application/json")


@app.route("/api/macro", methods=["GET", "POST"])
def api_macro():
    """Desk-wide macro sentiment flag (Part 4). GET returns the current flag; POST
    {flag:"RISK_ON|NEUTRAL|RISK_OFF|CRISIS", by:"Nick"} sets it. Written to
    logs/macro_sentiment.json; each Guinevere system re-reads it every 5 min."""
    if request.method == "GET":
        return Response(json.dumps(read_macro()), mimetype="application/json")
    try:
        body = request.get_json(force=True, silent=True) or {}
        flag = str(body.get("flag", "")).upper()
        by = str(body.get("by") or "Nick").strip() or "Nick"
        data = write_macro(flag, by)
        log.info("Macro sentiment set to %s by %s", data["flag"], by)
        return Response(json.dumps(data), mimetype="application/json")
    except Exception as exc:                       # noqa: BLE001
        return Response(json.dumps({"error": str(exc)}), status=400,
                        mimetype="application/json")


@app.route("/api/shutdown/<key>", methods=["POST"])
def api_shutdown(key):
    cfg = SYS_BY_KEY.get(key)
    if not cfg:
        return Response(json.dumps({"ok": False, "error": "unknown system"}),
                        status=404, mimetype="application/json")
    url = "http://localhost:{}/api/shutdown".format(cfg["port"])
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace")
        log.info("Shutdown proxied to %s (:%d)", key, cfg["port"])
        return Response(json.dumps({"ok": True, "system": key, "response": body[:200]}),
                        mimetype="application/json")
    except Exception as exc:                       # noqa: BLE001
        log.warning("Shutdown of %s failed: %s", key, exc)
        return Response(json.dumps({"ok": False, "system": key, "error": str(exc)}),
                        status=502, mimetype="application/json")


# ── Master control: START ALL + MAINTENANCE MODE ──────────────────────────────

# START ALL replicates START_ALBION.bat *exactly*: every process is opened in its
# own minimised console via `start /min "<title>" cmd /c <python> <script>`, with
# the same staggering. Two design points that fix the maintenance-route snags:
#
#   * Chronicle (5011) and the two Gaius background jobs ARE relaunched here, so a
#     browser START ALL brings the WHOLE desk back, not just the 6 traders (Snag 5).
#     RoundTable is NOT relaunched -- it is the process serving this button.
#   * Each process is launched DIRECTLY (no intermediate start_*.bat wrapper). The
#     old route ran each trader's start_*.bat via the shell; that wrapper's own
#     `cmd /c` console -- titled "<System> - Port 500x" by the bat's `title` line --
#     was the spurious extra window (Snag 7, seen on GasTrader). Launching the way
#     START_ALBION.bat does means exactly 2 windows per trader, no wrapper console.
#
# Paths are derived from this file's location (../<System>), never hardcoded
# absolutes, per the relative-paths standing rule. All times/logs are UTC.

_DESK = _BASE.parent            # ...\Desktop at runtime, derived not hardcoded
# Gaius has no port; its two background jobs poll this flag file and exit cleanly
# when maintenance mode writes it. Path derived from _DESK -- never hardcoded.
_GAIUS_SHUTDOWN_FLAG = _DESK / "GaiusAI" / "logs" / "shutdown.flag"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Progress rows shown on the maintenance startup display, in launch order.
# Port-serving systems are health-checked by a TCP connect; Gaius has no port
# (two background jobs) so it is marked online once its launch command is issued.
# HYBRID DESK ONLY. START ALL / MAINTENANCE here manage the Hybrid systems
# (ports 5041-5048), NOT the Original desk. Chronicle + Gaius are shared services
# owned by the Original RoundTable (5010) and are deliberately NOT relaunched here.
# Only FTSEHybrid is built so far; the rest launch gracefully once their repos exist.
_PROGRESS = [
    ("crypto", "CryptoHybrid", 5041),
    ("albion", "FTSEHybrid",   5042),
    ("gold",   "GoldHybrid",   5043),
    ("us",     "USHybrid",     5044),
    ("oil",    "OilHybrid",    5045),
    ("gas",    "GasHybrid",    5046),
    ("nikkei", "NikkeiHybrid", 5048),
]

# Launch groups: (gap_before_seconds, progress_key, subdir, [(window_title, script), ...]).
# 15s stagger between broker-hitting traders so no two hit the broker API at once.
_LAUNCH_GROUPS = [
    (0,  "albion", "FTSEHybridAI",   [("FTSEHybrid Dashboard",   "dashboard_ftse.py"),
                                      ("FTSEHybrid Engine",      "watchdog_ftse.py")]),
    (15, "crypto", "CryptoHybridAI", [("CryptoHybrid Dashboard", "dashboard_btc.py"),
                                      ("CryptoHybrid Engine",    "watchdog_btc.py")]),
    (15, "gold",   "GoldHybridAI",   [("GoldHybrid Dashboard",   "dashboard_gold.py"),
                                      ("GoldHybrid Engine",      "watchdog_gold.py")]),
    (15, "us",     "USHybridAI",     [("USHybrid Dashboard",     "dashboard_us.py"),
                                      ("USHybrid Engine",        "watchdog_us.py")]),
    (15, "oil",    "OilHybridAI",    [("OilHybrid Dashboard",    "dashboard_oil.py"),
                                      ("OilHybrid Engine",       "watchdog_oil.py")]),
    (15, "gas",    "GasHybridAI",    [("GasHybrid Dashboard",    "dashboard_gas.py"),
                                      ("GasHybrid Engine",       "watchdog_gas.py")]),
    (15, "nikkei", "NikkeiHybridAI", [("NikkeiHybrid Dashboard", "dashboard_nikkei.py"),
                                      ("NikkeiHybrid Engine",    "watchdog_nikkei.py")]),
]

# Shared launch-progress state, updated by the background launch thread.
_startup_lock = threading.Lock()
_startup = {"active": False, "done": False, "launched": set(), "t0": 0.0}


def _py_exe():
    """The console python (never pythonw) so each system opens a visible minimised
    window. Derived from the running interpreter -- no hardcoded absolute path."""
    exe = sys.executable or "python.exe"
    if exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    return exe


def _spawn(title, workdir, script):
    """Open one minimised console running `python <script>` in <workdir>, exactly
    like a START_ALBION.bat line. CREATE_NO_WINDOW hides the transient launcher
    cmd so only the minimised system window remains (no wrapper console)."""
    script_path = workdir / script
    if not script_path.exists():
        log.warning("START ALL: missing %s (%s)", title, script_path)
        return
    inner = 'cd /d {wd} && {py} {script}'.format(wd=str(workdir), py=_py_exe(), script=script)
    cmd = 'start /min "{title}" cmd /c "{inner}"'.format(title=title, inner=inner)
    try:
        subprocess.Popen(cmd, shell=True, creationflags=_NO_WINDOW)
        log.info("START ALL: launched %s (%s)", title, script)
    except Exception as exc:                           # noqa: BLE001
        log.error("START ALL: failed to start %s: %s", title, exc)


def _port_up(port):
    """True if something is accepting connections on localhost:<port>."""
    if not port:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except OSError:
        return False


@app.route("/start-all", methods=["POST"])
def start_all():
    """Relaunch the whole desk (Chronicle + 6 traders + Gaius) staggered like
    START_ALBION.bat, in a background thread. RoundTable relaunches itself is not
    possible/needed -- it serves this request."""
    def _launch_sequence():
        with _startup_lock:
            _startup.update(active=True, done=False, launched=set(), t0=time.monotonic())
        for gap_before, key, subdir, procs in _LAUNCH_GROUPS:
            if gap_before:
                time.sleep(gap_before)
            workdir = _DESK / subdir
            for title, script in procs:
                _spawn(title, workdir, script)
            with _startup_lock:
                _startup["launched"].add(key)
        with _startup_lock:
            _startup["done"] = True
        log.info("START ALL: launch sequence complete (Chronicle + 6 traders + Gaius)")

    threading.Thread(target=_launch_sequence, daemon=True).start()
    return Response(json.dumps({"status": "launching",
                                "message": "Whole desk launching with stagger"}),
                    mimetype="application/json")


@app.route("/api/startup-status")
def api_startup_status():
    """Live progress for the maintenance startup display. Each system is one of:
    waiting (not launched yet), starting (launched, port not yet answering), or
    online (port answering; Gaius has no port so online == launched)."""
    with _startup_lock:
        launched = set(_startup["launched"])
        active   = _startup["active"]
        done     = _startup["done"]
        t0       = _startup["t0"]
    rows, all_online = [], True
    for key, name, port in _PROGRESS:
        if key not in launched:
            state = "waiting"
        elif port is None:
            state = "online"            # Gaius: no port to probe -> online once launched
        else:
            state = "online" if _port_up(port) else "starting"
        if state != "online":
            all_online = False
        rows.append({"key": key, "name": name, "state": state})
    elapsed = int(time.monotonic() - t0) if t0 else 0
    return Response(json.dumps({"systems": rows, "all_online": all_online,
                                "sequence_done": done, "active": active,
                                "elapsed_s": elapsed}),
                    mimetype="application/json")


@app.route("/maintenance", methods=["POST"])
def maintenance_mode():
    """Enter maintenance mode: log it and send a graceful shutdown to all 6 traders."""
    maint_log = os.path.join(os.path.dirname(__file__), "logs", "maintenance.log")
    try:
        os.makedirs(os.path.dirname(maint_log), exist_ok=True)
        with open(maint_log, "a") as f:
            f.write("%s -- MAINTENANCE MODE STARTED\n"
                    % datetime.now(timezone.utc).isoformat())
    except Exception as exc:                           # noqa: BLE001
        log.warning("Could not write maintenance log: %s", exc)

    results = {}
    for cfg in SYSTEMS:
        url = "http://localhost:{}/api/shutdown".format(cfg["port"])
        try:
            req = urllib.request.Request(url, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT):
                pass
            results[cfg["name"]] = "shutdown sent"
        except Exception as exc:                        # noqa: BLE001
            results[cfg["name"]] = "error: %s" % exc

    # NOTE: the HYBRID desk does NOT shut down Chronicle (5011) or Gaius -- those are
    # shared desk services owned by the Original RoundTable (5010). Maintenance here
    # only stops the Hybrid trading systems (ports 5041-5048).

    log.warning("MAINTENANCE MODE activated -- shutdown sent to the Hybrid systems "
                "(Chronicle + Gaius left running -- owned by the Original RoundTable)")
    return Response(json.dumps({"status": "maintenance",
                                "message": "MAINTENANCE MODE ACTIVE",
                                "systems": results}),
                    mimetype="application/json")


@app.route("/gaius-report", methods=["POST"])
def gaius_report():
    """Launch the Gaius strategic report generator (../GaiusAI/gaius_report.py) in
    its own console. The script compiles the data package, calls the Claude API,
    writes HTML+TXT to GaiusAI/reports/, and opens the HTML in the browser on
    completion (~1-2 min). The window is left open (cmd /k) so Nick can read the
    output/paths -- or any error (e.g. a missing ANTHROPIC_API_KEY in .env)."""
    gdir = _DESK / "GaiusAI"
    script = gdir / "gaius_report.py"
    if not script.exists():
        return Response(json.dumps({"ok": False, "error": "gaius_report.py not found (%s)" % script}),
                        status=404, mimetype="application/json")
    inner = 'cd /d {wd} && {py} gaius_report.py'.format(wd=str(gdir), py=_py_exe())
    cmd = 'start "Gaius Strategic Report" cmd /k "{inner}"'.format(inner=inner)
    try:
        subprocess.Popen(cmd, shell=True, creationflags=_NO_WINDOW)
        log.info("Gaius report generation launched")
        return Response(json.dumps({"ok": True, "message": "Gaius report generating"}),
                        mimetype="application/json")
    except Exception as exc:                               # noqa: BLE001
        log.error("Gaius report launch failed: %s", exc)
        return Response(json.dumps({"ok": False, "error": str(exc)}),
                        status=500, mimetype="application/json")


@app.route("/api/lift-confidence/<key>", methods=["POST"])
def api_lift_confidence(key):
    """Lift a system's Morgan confidence to 50 via Gaius (intervention Step 4).
    Runs ../GaiusAI/gaius_intervention.py --lift <System>, which POSTs the system's
    live /api/lift-confidence endpoint (engine applies in-process, no restart) and
    fires the Percival notification. Surfaced on a row only while a Gaius
    intervention is active (the telescope indicator)."""
    cfg = SYS_BY_KEY.get(key)
    if not cfg:
        return Response(json.dumps({"ok": False, "error": "unknown system"}),
                        status=404, mimetype="application/json")
    gdir = _DESK / "GaiusAI"
    script = gdir / "gaius_intervention.py"
    if not script.exists():
        return Response(json.dumps({"ok": False, "error": "gaius_intervention.py not found"}),
                        status=500, mimetype="application/json")
    try:
        proc = subprocess.run(
            [_py_exe(), "gaius_intervention.py", "--lift", cfg["name"], "--to", "50"],
            cwd=str(gdir), capture_output=True, text=True, timeout=45,
            creationflags=_NO_WINDOW)
        ok = proc.returncode == 0
        log.info("Confidence lift for %s via Gaius: rc=%d", key, proc.returncode)
        return Response(json.dumps({"ok": ok, "system": cfg["name"],
                                    "output": (proc.stdout or proc.stderr or "").strip()[-400:]}),
                        mimetype="application/json", status=(200 if ok else 502))
    except Exception as exc:                               # noqa: BLE001
        log.warning("Confidence lift for %s failed: %s", key, exc)
        return Response(json.dumps({"ok": False, "system": cfg["name"], "error": str(exc)}),
                        status=502, mimetype="application/json")


@app.route("/api/health")
def api_health():
    return Response(json.dumps({"status": "ok"}), mimetype="application/json")


@app.route("/")
def index():
    return Response(_PAGE, mimetype="text/html")


# ── HTML page (server-rendered skeleton; JS fills the live values) ─────────────

def _build_rows_html():
    out = []
    for s in SYSTEMS:
        k = s["key"]
        out.append(
            '<div class="row" id="row-' + k + '">'
            '  <div class="cbar" style="background:' + s["colour"] + ';"></div>'
            '  <div class="col name">'
            '    <div class="sysname" style="color:' + s["colour"] + ';">' + s["name"] + '<span class="iv-flag" id="iv-' + k + '" title="Gaius intervention active -- check reports/"></span></div>'
            '    <div class="market">' + s["market"] + '</div>'
            '    <div class="session" id="session-' + k + '">--</div>'
            '  </div>'
            '  <div class="col status"><span class="badge" id="badge-' + k + '">--</span></div>'
            '  <div class="col price" id="price-' + k + '">---</div>'
            '  <div class="col pos" id="pos-' + k + '">---</div>'
            '  <div class="col flt" id="flt-' + k + '">---</div>'
            '  <div class="col locked" id="locked-' + k + '">---</div>'
            '  <div class="col daily" id="daily-' + k + '">---</div>'
            '  <div class="col bal" id="bal-' + k + '">---</div>'
            '  <div class="col lanc"><span class="lbadge" id="lanc-' + k + '">--</span></div>'
            '  <div class="col dec"><span class="dbadge" id="dec-' + k + '">--</span></div>'
            '  <div class="col conf">'
            '    <div class="confwrap"><div class="conffill" id="conf-' + k + '"></div></div>'
            '    <div class="conftxt" id="conftxt-' + k + '">--</div>'
            '  </div>'
            '  <div class="col acts">'
            '    <button class="obtn" id="open-' + k + '" title="Open dashboard" '
            'onclick="openSys(' + str(s["port"]) + ')">&rarr;</button>'
            '    <button class="kbtn" id="kill-' + k + '" title="Shut down" '
            "onclick=\"askKill('" + k + "','" + s["name"] + "')\">&#9211;</button>"
            '    <button id="lift-' + k + '" title="Lift Morgan confidence to 50 (Gaius intervention)" '
            "onclick=\"askLift('" + k + "','" + s["name"] + "')\" "
            'style="display:none;margin-left:4px;font-size:10px;font-weight:700;color:#3498db;'
            'background:rgba(52,152,219,0.15);border:1px solid rgba(52,152,219,0.6);'
            'padding:2px 6px;border-radius:4px;cursor:pointer;vertical-align:middle;">&#128301; LIFT</button>'
            '  </div>'
            '</div>'
        )
    return "".join(out)


_HEAD = [
    "COLOUR BAR", "SYSTEM", "STATUS", "PRICE", "POSITION",
    "FLOATING P&L", "TODAY", "BALANCE", "ARTHUR", "CONFIDENCE", "ACTIONS",
]


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HybridRoundTable A.I. — Command Centre</title>
<style>
  :root{ --bg:""" + BG_COLOUR + """; --tx:""" + TEXT_COLOUR + """; --bd:""" + BORDER + """; }
  *{ box-sizing:border-box; }
  body{ margin:0; background:var(--bg); color:var(--tx);
        font-family:'Consolas','SFMono-Regular',Menlo,monospace; font-size:13px; }
  a{ color:inherit; }

  header{ display:flex; align-items:center; justify-content:space-between;
          background:#000; border-bottom:1px solid var(--bd); padding:14px 22px; }
  header .brand{ font-size:20px; font-weight:700; letter-spacing:1px; }
  header .brand .cap{ color:#c8a24a; }
  header .centre{ text-align:center; line-height:1.5; }
  header .centre .big{ font-size:18px; font-weight:700; }
  header .muted{ color:#888; }
  header .clock{ text-align:right; }
  header .clock .t{ font-size:22px; font-weight:700; letter-spacing:1px; }
  header .clock .z{ color:#888; font-size:11px; }

  .green{ color:#2ecc71; } .red{ color:#e74c3c; } .amber{ color:#f1c40f; }
  .dim{ color:#666; }

  .tablewrap{ padding:16px 22px; }
  .thead{ display:flex; align-items:center; color:#777; font-size:10px;
          letter-spacing:0.5px; padding:0 10px 8px 10px; border-bottom:1px solid var(--bd); }
  .row{ display:flex; align-items:center; background:#151515; border:1px solid var(--bd);
        border-radius:6px; margin-top:10px; min-height:60px; overflow:hidden;
        transition:background 0.3s, box-shadow 0.3s; position:relative; }
  .row.glow-long{ background:rgba(46,204,113,0.06); box-shadow:inset 3px 0 0 rgba(46,204,113,0.0), 0 0 14px rgba(46,204,113,0.10); }
  .row.glow-short{ background:rgba(231,76,60,0.06); box-shadow:0 0 14px rgba(231,76,60,0.10); }
  .row.offline{ opacity:0.42; filter:grayscale(0.7); }

  .cbar{ width:8px; align-self:stretch; }
  .col{ padding:10px 12px; }
  .col.name{ width:220px; } .sysname{ font-weight:700; letter-spacing:0.5px; }
  .market{ color:#888; font-size:11px; margin-top:2px; }
  .session{ font-size:10px; margin-top:3px; color:#9a9a9a; letter-spacing:0.2px; white-space:nowrap; }
  .col.status{ width:110px; }
  .col.price{ flex:1; min-width:180px; font-weight:600; }
  .col.pos{ width:150px; } .col.flt{ width:150px; } .col.locked{ width:120px; } .col.daily{ width:110px; }
  .col.bal{ width:120px; } .col.lanc{ width:120px; } .col.dec{ width:120px; }
  .col.conf{ width:130px; } .col.acts{ width:96px; display:flex; gap:8px; }

  .badge{ padding:3px 9px; border-radius:4px; font-size:11px; font-weight:700; letter-spacing:0.5px; }
  .b-run{ background:rgba(46,204,113,0.15); color:#2ecc71; border:1px solid #2ecc71; }
  .b-off{ background:rgba(120,120,120,0.15); color:#999; border:1px solid #666; }
  .b-err{ background:rgba(231,76,60,0.15); color:#e74c3c; border:1px solid #e74c3c; }

  .dbadge{ padding:3px 9px; border-radius:4px; font-size:11px; font-weight:700; }
  .d-out{ background:rgba(120,120,120,0.15); color:#aaa; border:1px solid #666; }
  .d-hold{ background:rgba(241,196,15,0.15); color:#f1c40f; border:1px solid #f1c40f; }
  .d-long{ background:rgba(46,204,113,0.15); color:#2ecc71; border:1px solid #2ecc71; }
  .d-short{ background:rgba(231,76,60,0.15); color:#e74c3c; border:1px solid #e74c3c; }
  .lbadge{ padding:3px 9px; border-radius:4px; font-size:11px; font-weight:700; }
  .l-clear{ background:rgba(46,204,113,0.15); color:#2ecc71; border:1px solid #2ecc71; }
  .l-fails{ background:rgba(241,196,15,0.15); color:#f1c40f; border:1px solid #f1c40f; }
  .l-block{ background:rgba(231,76,60,0.15); color:#e74c3c; border:1px solid #e74c3c; }
  .l-na{ background:rgba(120,120,120,0.15); color:#aaa; border:1px solid #666; }

  .confwrap{ height:8px; background:#2a2a2a; border-radius:4px; overflow:hidden; }
  .conffill{ height:100%; width:0%; background:#666; transition:width 0.4s, background 0.4s; }
  .conftxt{ font-size:10px; color:#888; margin-top:3px; }

  .obtn,.kbtn{ cursor:pointer; border-radius:5px; width:34px; height:32px;
               font-size:15px; font-weight:700; }
  .obtn{ background:#1f1f1f; border:1px solid #555; color:var(--tx); }
  .obtn:hover{ background:#2b2b2b; }
  .kbtn{ background:rgba(231,76,60,0.12); border:1px solid #e74c3c; color:#e74c3c; }
  .kbtn:hover{ background:rgba(231,76,60,0.30); }
  button:disabled{ opacity:0.3; cursor:not-allowed; }

  header .countdown{ color:#c8a24a; font-size:12px; margin-top:3px; letter-spacing:0.3px; }

  .soq{ margin:16px 22px 0 22px; background:#151515; border:1px solid var(--bd);
        border-radius:6px; padding:14px 18px; }
  .soq h4{ margin:0 0 8px 0; font-size:13px; letter-spacing:0.6px; color:#c8a24a; }
  .soq .soq-sub{ color:#9a9a9a; font-size:11px; margin-bottom:10px; }
  .soq .soq-stats{ font-size:12px; margin-bottom:10px; }
  .soq .soq-stats .sep{ color:#555; margin:0 8px; }
  .soq table{ width:100%; border-collapse:collapse; font-size:11px; }
  .soq th{ text-align:left; color:#777; font-weight:600; padding:4px 8px;
           border-bottom:1px solid var(--bd); letter-spacing:0.3px; }
  .soq td{ padding:4px 8px; border-bottom:1px solid #222; color:#ccc; }
  .soq .v-correct{ color:#2ecc71; } .soq .v-wrong{ color:#e74c3c; } .soq .v-neutral{ color:#aaa; }
  .soq .empty{ color:#888; font-size:12px; }

  .threeway{ font-size:12px; color:#9a9a9a; margin-top:3px; }
  .threeway .exitval{ font-weight:800; }
  .twpanel{ margin:14px 22px 0 22px; background:#141414; border:1px solid var(--bd); border-radius:8px; padding:12px 16px; }
  .twpanel-title{ font-size:12px; letter-spacing:0.5px; color:#8ab4e0; font-weight:700; margin-bottom:8px; }
  .twtable{ width:100%; border-collapse:collapse; font-size:12px; }
  .twtable th{ text-align:right; color:#777; font-weight:600; padding:4px 10px; border-bottom:1px solid var(--bd); }
  .twtable th:first-child{ text-align:left; }
  .twtable td{ padding:5px 10px; border-bottom:1px solid #222; text-align:right; color:#ccc; }
  .twtable td:first-child{ text-align:left; }
  .twtable .exitval{ font-weight:800; font-size:13px; }
  .twtable th.evcol{ color:#8ab4e0; font-weight:800; }
  .twtable td.evcol{ background:rgba(138,180,224,0.05); }
  .twtable tr.tw-total td{ border-top:2px solid #444; border-bottom:none;
        font-weight:800; color:#e8e8e8; padding-top:8px; letter-spacing:0.3px; }
  .twtable .mut{ color:#666; font-size:10px; }
  .twtable .dim{ color:#666; }

  footer{ padding:16px 22px; color:#777; border-top:1px solid var(--bd); font-size:11px; line-height:1.7; }

  #modal{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6);
          align-items:center; justify-content:center; z-index:50; }
  #modalbox{ background:#181818; border:1px solid #e74c3c; border-radius:8px; padding:24px 28px; text-align:center; }
  #modalbox h3{ margin:0 0 18px 0; }
  #modalbox button{ margin:0 8px; padding:8px 22px; border-radius:5px; font-weight:700; cursor:pointer; font-size:13px; }
  .yes{ background:#e74c3c; color:#fff; border:none; }
  .no{ background:#333; color:#ddd; border:1px solid #555; }

  /* Macro sentiment overlay (Part 4) */
  .macro{ margin:16px 22px 0 22px; background:#151515; border:1px solid var(--bd);
          border-radius:8px; padding:12px 16px; display:flex; align-items:center;
          flex-wrap:wrap; gap:14px; }
  .macro .macro-lbl{ font-size:13px; letter-spacing:0.6px; color:#c8a24a; font-weight:700; }
  .macro .macro-btns{ display:flex; gap:8px; }
  .macro .macro-btn{ font-size:12px; font-weight:700; letter-spacing:0.5px; padding:5px 14px;
          border-radius:5px; cursor:pointer; background:#1e1e1e; color:#aaa; border:1px solid #444; }
  .macro .macro-btn:hover{ background:#262626; }
  .macro .macro-btn.active-RISK_ON{ background:rgba(46,204,113,0.22); color:#2ecc71; border-color:#2ecc71; }
  .macro .macro-btn.active-NEUTRAL{ background:rgba(150,150,150,0.20); color:#ddd; border-color:#888; }
  .macro .macro-btn.active-RISK_OFF{ background:rgba(231,76,60,0.22); color:#e74c3c; border-color:#e74c3c; }
  .macro .macro-btn.active-CRISIS{ background:rgba(231,76,60,0.30); color:#fff; border-color:#e74c3c;
          animation:macroflash 1s steps(1) infinite; }
  @keyframes macroflash{ 50%{ background:rgba(231,76,60,0.75); border-color:#ff6b5b; } }
  .macro .macro-meta{ color:#9a9a9a; font-size:12px; margin-left:auto; }
  .macro .macro-meta .dim{ color:#666; }
  #macro-hdr{ color:#c8a24a; font-size:12px; margin-top:3px; letter-spacing:0.3px; }
</style>
</head>
<body>
<header>
  <div class="brand">&#127984; <span class="cap">HybridRoundTable A.I. &mdash; Command Centre</span>
    <a href="http://localhost:5011" target="_blank" rel="noopener" title="Merlin's Chronicle -- reports" style="margin-left:14px;font-size:12px;font-weight:700;letter-spacing:0.5px;color:#b58ae0;background:rgba(75,0,130,0.25);border:1px solid rgba(75,0,130,0.7);padding:3px 10px;border-radius:4px;text-decoration:none;vertical-align:middle;">&#128202; REPORTS</a>
    <button onclick="startAll()" title="Launch all 6 trading systems (staggered)" style="margin-left:10px;font-size:12px;font-weight:700;color:#2ecc71;background:rgba(46,204,113,0.15);border:1px solid rgba(46,204,113,0.6);padding:3px 10px;border-radius:4px;cursor:pointer;vertical-align:middle;">&#9654; START ALL</button>
    <button onclick="maintenanceMode()" title="Gracefully shut down all systems" style="margin-left:6px;font-size:12px;font-weight:700;color:#f1c40f;background:rgba(241,196,15,0.15);border:1px solid rgba(241,196,15,0.6);padding:3px 10px;border-radius:4px;cursor:pointer;vertical-align:middle;">&#128295; MAINTENANCE</button>
    <button onclick="gaiusReport()" title="Generate a new Gaius strategic intelligence report (calls Claude, ~1-2 min)" style="margin-left:6px;font-size:12px;font-weight:700;color:#c0415f;background:rgba(139,30,63,0.18);border:1px solid rgba(139,30,63,0.7);padding:3px 10px;border-radius:4px;cursor:pointer;vertical-align:middle;">&#128300; GAIUS REPORT</button>
  </div>
  <div class="centre">
    <div class="big">Hybrid Portfolio: <span id="portfolio">&pound;--</span></div>
    <div>Today's P&amp;L: <span id="today">&pound;--</span>
         &nbsp;&middot;&nbsp; Systems online: <span id="running">-/7</span></div>
    <div class="threeway">Hybrid <span id="hybCum">--</span>
         &nbsp;&middot;&nbsp; Original <span id="origCum">--</span>
         &nbsp;&middot;&nbsp; Benchmark <span id="benchCum">--</span>
         &nbsp;&middot;&nbsp; vs Orig <span id="dOrig">--</span>
         &nbsp;&middot;&nbsp; ARTHUR EXIT VALUE (vs Bench) <span id="dBench" class="exitval">--</span></div>
    <div class="countdown" id="countdown">--</div>
    <div id="macro-hdr">Macro: --</div>
  </div>
  <div class="clock"><div class="t" id="clock">--:--:--</div><div class="z">UTC</div></div>
</header>

<div class="twpanel">
  <div class="twpanel-title">&#9878; THREE-WAY COMPARISON &mdash; cumulative P&amp;L per matched system. HYBRID &minus; BENCHMARK = Arthur's pure exit value (both share the Lancelot entry).</div>
  <table class="twtable"><thead><tr>
    <th>System</th><th>Original P&amp;L</th><th>Benchmark P&amp;L</th><th>Hybrid P&amp;L</th><th class="evcol">Arthur Exit Value</th>
  </tr></thead><tbody id="twbody"></tbody></table>
</div>

<div class="tablewrap">
  <div class="thead">
    <div class="cbar"></div>
    <div class="col name">SYSTEM</div>
    <div class="col status">STATUS</div>
    <div class="col price">PRICE</div>
    <div class="col pos">POSITION</div>
    <div class="col flt">FLOATING P&amp;L</div>
    <div class="col locked">LOCKED</div>
    <div class="col daily">TODAY</div>
    <div class="col bal">BALANCE</div>
    <div class="col lanc">LANCELOT</div>
    <div class="col dec">ARTHUR</div>
    <div class="col conf">CONFIDENCE</div>
    <div class="col acts">ACT</div>
  </div>
  """ + _build_rows_html() + """
</div>

<div class="macro" id="macro">
  <div class="macro-lbl">MACRO SENTIMENT</div>
  <div class="macro-btns">
    <button class="macro-btn" id="macro-RISK_ON"  onclick="setMacro('RISK_ON')">RISK_ON</button>
    <button class="macro-btn" id="macro-NEUTRAL"  onclick="setMacro('NEUTRAL')">NEUTRAL</button>
    <button class="macro-btn" id="macro-RISK_OFF" onclick="setMacro('RISK_OFF')">RISK_OFF</button>
    <button class="macro-btn" id="macro-CRISIS"   onclick="setMacro('CRISIS')">CRISIS</button>
  </div>
  <div class="macro-meta" id="macro-meta">Macro: NEUTRAL &#9899;</div>
</div>

<div class="soq" id="soq">
  <h4>STAY OUT QUALITY</h4>
  <div id="soq-body"><div class="empty">Awaiting first decisions</div></div>
</div>

<footer>
  <div id="gaius-status">&#128300; Gaius Strategic Intelligence &nbsp;|&nbsp; <span class="dim">awaiting data</span></div>
  <div>Albion Trading Desk -- Paper Trading Mode &nbsp;&middot;&nbsp; <span id="version">v--</span></div>
  <div>All systems monitored -- refresh every 15s &nbsp;&middot;&nbsp;
       Last updated: <span id="lastupd">--:--:--</span> UTC</div>
</footer>

<div id="modal">
  <div id="modalbox">
    <h3 id="modaltxt">Shut down?</h3>
    <button class="yes" id="modalyes">YES</button>
    <button class="no" onclick="closeModal()">NO</button>
  </div>
</div>

<script>
var KEYS = ["crypto","albion","gold","us"];
var pendingKill = null;

function money(v){
  if(v === null || v === undefined) return "\\u00a3--";
  var n = Number(v);
  var s = Math.abs(n).toLocaleString("en-GB",{minimumFractionDigits:2,maximumFractionDigits:2});
  return (n < 0 ? "-\\u00a3" : "\\u00a3") + s;
}
function signMoney(v){
  if(v === null || v === undefined) return "---";
  var n = Number(v);
  return (n >= 0 ? "+" : "") + money(n);
}
function cls(v){ if(v===null||v===undefined) return ""; return Number(v) >= 0 ? "green" : "red"; }

function decClass(d){
  if(d === "LONG")  return "d-long";
  if(d === "SHORT") return "d-short";
  if(d === "HOLD" || d === "EXIT") return "d-hold";
  return "d-out";
}

function updateClock(){
  var d = new Date();
  var p = function(x){ return (x<10?"0":"") + x; };
  document.getElementById("clock").textContent =
    p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + ":" + p(d.getUTCSeconds());
}

function renderRow(s){
  var k = s.key;
  var row = document.getElementById("row-" + k);
  var badge = document.getElementById("badge-" + k);
  var open = document.getElementById("open-" + k);
  var kill = document.getElementById("kill-" + k);
  var offline = (s.status !== "RUNNING");

  var sess = document.getElementById("session-" + k);
  if(sess){ sess.textContent = s.session || "--"; }

  // Gaius intervention telescope indicator + Lift Confidence button (Phase 2b)
  var ivf = document.getElementById("iv-" + k);
  if(ivf){ ivf.textContent = s.intervention_active ? " \\ud83d\\udd2d" : ""; }
  var liftBtn = document.getElementById("lift-" + k);
  if(liftBtn){ liftBtn.style.display = (s.intervention_active && !offline) ? "inline-block" : "none"; }

  row.className = "row" + (offline ? " offline" :
    (s.glow === "long" ? " glow-long" : (s.glow === "short" ? " glow-short" : "")));

  badge.textContent = offline ? "OFFLINE" : "RUNNING";
  badge.className = "badge " + (offline ? "b-off" : "b-run");

  document.getElementById("price-" + k).textContent = offline ? "---" : (s.price_html || "---");

  // Position
  var pos = document.getElementById("pos-" + k);
  if(offline){ pos.textContent = "---"; pos.className = "col pos dim"; }
  else if(s.position === "LONG" || s.position === "SHORT"){
    var arrow = (s.position === "LONG") ? "\\u25b2 LONG" : "\\u25bc SHORT";
    var pair = s.pair_note ? (s.pair_note + " ") : "";
    var entry = (s.entry !== null && s.entry !== undefined) ? (" @ " + Number(s.entry).toLocaleString("en-GB")) : "";
    pos.innerHTML = '<span class="' + (s.position==="LONG"?"green":"red") + '">' + pair + arrow + entry + "</span>";
    pos.className = "col pos";
  } else { pos.innerHTML = '<span class="dim">NO TRADE</span>'; pos.className = "col pos"; }

  // Floating P&L
  var flt = document.getElementById("flt-" + k);
  if(offline || s.position === "NO_TRADE" || s.position === "---" || s.float_gbp === null && s.float_pts === null){
    flt.textContent = "---"; flt.className = "col flt dim";
  } else {
    var bits = [];
    if(s.float_pts !== null && s.float_pts !== undefined){
      bits.push((s.float_pts >= 0 ? "+" : "") + Number(s.float_pts).toFixed(1) + " pts");
    }
    if(s.float_gbp !== null && s.float_gbp !== undefined){ bits.push(signMoney(s.float_gbp)); }
    var basis = (s.float_gbp !== null && s.float_gbp !== undefined) ? s.float_gbp : s.float_pts;
    flt.textContent = bits.join(" / ") || "---";
    flt.className = "col flt " + cls(basis);
  }

  // Today's P&L
  var daily = document.getElementById("daily-" + k);
  daily.textContent = offline ? "---" : signMoney(s.daily_pnl);
  daily.className = "col daily " + (offline ? "dim" : cls(s.daily_pnl));

  // Balance
  var bal = document.getElementById("bal-" + k);
  bal.textContent = offline ? "---" : money(s.balance);
  bal.className = "col bal" + (offline ? " dim" : "");

  // Locked P&L (guaranteed at current trailing stop)
  var locked = document.getElementById("locked-" + k);
  if(locked){
    var lp = s.locked_pnl;
    if(offline || lp === null || lp === undefined){
      locked.textContent = "---"; locked.className = "col locked dim";
    } else {
      var ln = Number(lp);
      locked.textContent = "🔒 " + (ln >= 0 ? "+" : "-") + "£" + Math.abs(ln).toFixed(2);
      locked.className = "col locked " + (ln > 0 ? "green" : (ln < 0 ? "red" : "dim"));
    }
  }

  // Lancelot gate status
  var lanc = document.getElementById("lanc-" + k);
  if(lanc){
    /* Snag 18: once a session ends naturally (OUT OF SESSION / WEEKEND / DAILY BREAK),
       show BLOCKED regardless of any stale cached fail count. Fail counts only show
       while the market is actually in session. */
    var ls;
    if(offline){ ls = "--"; }
    else if(s.session_active === false){ ls = "BLOCKED"; }
    else { ls = s.lancelot_status || "--"; }
    lanc.textContent = ls;
    var lc = "l-na";
    if(!offline){
      if(ls === "CLEAR") lc = "l-clear";
      else if(ls === "BLOCKED") lc = "l-block";
      else if(/FAILS/.test(ls)) lc = "l-fails";
    }
    lanc.className = "lbadge " + lc;
  }

  // Arthur decision (+ confidence)
  var dec = document.getElementById("dec-" + k);
  var adec = offline ? "---" : (s.arthur_decision || s.decision || "STAY OUT");
  var aconf = s.arthur_confidence;
  var dlabel = adec;
  if(!offline && adec !== "---" && aconf !== null && aconf !== undefined){
    dlabel = adec + " (" + Number(aconf) + ")";
  }
  dec.textContent = dlabel;
  dec.className = "dbadge " + (offline ? "d-out" : decClass(adec));

  // Morgan confidence bar
  var fill = document.getElementById("conf-" + k);
  var ctxt = document.getElementById("conftxt-" + k);
  if(offline || s.confidence === null || s.confidence === undefined){
    fill.style.width = "0%"; fill.style.background = "#666"; ctxt.textContent = offline ? "--" : "n/a";
  } else {
    var c = Number(s.confidence);
    fill.style.width = c + "%";
    fill.style.background = (c <= 30) ? "#e74c3c" : (c <= 60 ? "#f1c40f" : "#2ecc71");
    ctxt.textContent = c + "/100";
  }

  open.disabled = false;             // opening a dashboard is harmless even if offline
  kill.disabled = offline;
}

function startAll(){
  fetch("/start-all", {method:"POST"})
    .then(function(r){ return r.json(); })
    .then(function(){ alert("Whole desk launching (staggered): Chronicle + 6 traders + Gaius. Rows come online over ~90s."); })
    .catch(function(e){ alert("START ALL failed: " + e); });
}

function gaiusReport(){
  if(!confirm("Generate a new Gaius strategic report?\\n\\nThis calls the Claude API (~1-2 min). A console window shows progress and the report opens in your browser when ready. HTML + plain-text copies are saved to the GaiusAI reports folder.")) return;
  fetch("/gaius-report", {method:"POST"})
    .then(function(r){ return r.json(); })
    .then(function(d){
      if(d && d.ok){ alert("Gaius report generating (~1-2 min). Watch the 'Gaius Strategic Report' console window; the report opens in your browser when ready."); }
      else { alert("Gaius report failed to start: " + ((d && d.error) || "unknown")); }
    })
    .catch(function(e){ alert("Gaius report failed: " + e); });
}

// Live startup progress + auto-return to the Command Centre. Used by the
// maintenance banner after START ALL so Nick sees each system come online and
// the page returns on its own (no manual reload). Polls /api/startup-status.
function showStartupProgress(){
  document.body.innerHTML =
    '<div style="text-align:center;margin-top:12vh;font-family:sans-serif;">' +
      '<h1 style="color:#2ecc71;">&#9654; ALL SYSTEMS LAUNCHING</h1>' +
      '<p id="startup-sub" style="font-size:13px;color:#888;max-width:560px;margin:6px auto 18px;">' +
        'Systems are staggering up (~90s). This page returns to the Command Centre ' +
        'automatically once every system is online.</p>' +
      '<div id="startup-list" style="display:inline-block;text-align:left;font-family:monospace;' +
        'font-size:15px;line-height:1.95;min-width:290px;"></div>' +
    '</div>';
  var frames = ["|","/","-","\\\\"], fi = 0, deadline = Date.now() + 180000, redirecting = false, timer;
  function paint(rows){
    var sp = frames[fi % frames.length]; fi++;
    var html = "";
    for(var i=0;i<rows.length;i++){
      var r = rows[i], mark, col, label;
      if(r.state === "online"){ mark = "&#10003;"; col = "#2ecc71"; label = "online"; }
      else if(r.state === "starting"){ mark = sp; col = "#f1c40f"; label = "starting..."; }
      else { mark = "&#9675;"; col = "#555"; label = "waiting"; }
      html += '<div><span style="display:inline-block;width:1.5em;color:' + col + ';">' + mark + '</span>' +
              '<span style="color:#ccc;">' + r.name + '</span> ' +
              '<span style="color:' + col + ';">&mdash; ' + label + '</span></div>';
    }
    document.getElementById("startup-list").innerHTML = html;
  }
  function finish(msg){
    if(redirecting) return; redirecting = true;
    if(timer){ clearInterval(timer); }
    var sub = document.getElementById("startup-sub");
    if(sub){ sub.innerHTML = msg; sub.style.color = "#2ecc71"; }
    setTimeout(function(){ window.location.href = "/"; }, 1600);
  }
  function tick(){
    fetch("/api/startup-status").then(function(r){ return r.json(); }).then(function(d){
      paint(d.systems || []);
      if(d.all_online && d.sequence_done){
        finish("All systems online &mdash; returning to Command Centre...");
      } else if(Date.now() > deadline){
        finish("Startup timed out (3 min) &mdash; returning to Command Centre...");
      }
    }).catch(function(){ /* transient errors while systems restart -- keep polling */ });
  }
  timer = setInterval(tick, 2500);
  tick();
}

function maintenanceMode(){
  if(!confirm("Enter maintenance mode? This will shut down all 6 trading systems.")) return;
  fetch("/maintenance", {method:"POST"})
    .then(function(r){ return r.json(); })
    .then(function(){ showMaintenanceBanner(); })
    .catch(function(e){ alert("MAINTENANCE failed: " + e); });
}

function showMaintenanceBanner(){
  // Maintenance banner WITH a START ALL button so all systems can be restarted
  // straight from the browser (no need to double-click START_ALBION.bat).
  document.body.innerHTML =
    '<div style="text-align:center;margin-top:16vh;font-family:sans-serif;">' +
      '<h1 style="color:orange;">&#128295; MAINTENANCE MODE ACTIVE</h1>' +
      '<p style="font-size:14px;color:#888;max-width:620px;margin:8px auto 28px;">' +
        'All trading systems have been sent a shutdown signal. When maintenance is ' +
        'complete, start every system again below &mdash; or reload the page.</p>' +
      '<button id="maintStartAllBtn" onclick="maintenanceStartAll(this)" ' +
        'style="font-size:16px;font-weight:700;color:#2ecc71;background:rgba(46,204,113,0.15);' +
        'border:1px solid rgba(46,204,113,0.6);padding:11px 24px;border-radius:6px;cursor:pointer;">' +
        '&#9654; START ALL SYSTEMS</button>' +
    '</div>';
}

function maintenanceStartAll(btn){
  if(!confirm("Are you sure you want to start all systems?")) return;
  if(btn){ btn.disabled = true; btn.textContent = "Starting..."; }
  fetch("/start-all", {method:"POST"})
    .then(function(r){ return r.json(); })
    .then(function(){ showStartupProgress(); })
    .catch(function(e){
      alert("START ALL failed: " + e);
      if(btn){ btn.disabled = false; btn.innerHTML = "&#9654; START ALL SYSTEMS"; }
    });
}

function renderThreeWay(data){
  var body = document.getElementById("twbody");
  if(!body){ return; }
  var systems = data.systems || [];
  var cell = function(v){ return (v===null||v===undefined) ? '<span class="dim">--</span>' : '<span class="'+cls(v)+'">'+signMoney(v)+'</span>'; };
  var evcell = function(v){ return (v===null||v===undefined) ? '<span class="dim">--</span>' : '<span class="exitval '+cls(v)+'">'+signMoney(v)+'</span>'; };
  var h = "";
  for(var i=0;i<systems.length;i++){
    var s = systems[i];
    h += '<tr><td style="color:'+(s.colour||'#ccc')+'">'+s.name+' <span class="mut">:'+s.port+'</span></td>'
       + '<td>'+cell(s.orig_cum)+'</td><td>'+cell(s.bench_cum)+'</td><td>'+cell(s.hybrid_cum)+'</td>'
       + '<td class="evcol">'+evcell(s.exit_value)+'</td></tr>';
  }
  // DESK TOTAL: total Hybrid P&L, total Benchmark P&L, and total Arthur exit value
  // (Hybrid total minus Benchmark total, matched systems) -- same green/red treatment.
  h += '<tr class="tw-total"><td>DESK TOTAL</td>'
     + '<td>'+cell(data.orig_cum)+'</td><td>'+cell(data.bench_cum)+'</td><td>'+cell(data.hybrid_cum)+'</td>'
     + '<td class="evcol">'+evcell(data.delta_bench)+'</td></tr>';
  body.innerHTML = h;
}

function render(data){
  document.getElementById("portfolio").textContent = money(data.portfolio);
  var t = document.getElementById("today");
  t.textContent = signMoney(data.today_pnl);
  t.className = cls(data.today_pnl);
  document.getElementById("running").textContent = data.running + "/" + data.total;
  // Three-way desk comparison (Hybrid vs Original vs Benchmark)
  var setTw = function(id, v, signed){
    var el = document.getElementById(id);
    if(!el){ return; }
    if(v === null || v === undefined){ el.textContent = "--"; el.className = ""; return; }
    el.textContent = signed ? signMoney(v) : money(v);
    el.className = cls(v) + (id === "dBench" ? " exitval" : "");
  };
  setTw("hybCum", data.hybrid_cum, true);
  setTw("origCum", data.orig_cum, true);
  setTw("benchCum", data.bench_cum, true);
  setTw("dOrig", data.delta_orig, true);
  setTw("dBench", data.delta_bench, true);
  renderThreeWay(data);
  document.getElementById("lastupd").textContent = data.updated_utc;
  var ver = document.getElementById("version");
  if(ver){ ver.textContent = data.version_string || ("v" + (data.version || "--")); }
  renderMacro(data.macro);
  renderStayOut(data.stay_out_quality);
  renderGaius(data.gaius);
  for(var i=0;i<data.systems.length;i++){ renderRow(data.systems[i]); }
}

var MACRO_FLAGS = ["RISK_ON","NEUTRAL","RISK_OFF","CRISIS"];
var MACRO_DOTS = {RISK_ON:"&#128994;", NEUTRAL:"&#9899;", RISK_OFF:"&#128308;", CRISIS:"&#128308;"};
function renderMacro(m){
  m = m || {};
  var flag = m.flag || "NEUTRAL";
  if(MACRO_FLAGS.indexOf(flag) < 0){ flag = "NEUTRAL"; }
  for(var i=0;i<MACRO_FLAGS.length;i++){
    var b = document.getElementById("macro-" + MACRO_FLAGS[i]);
    if(b){ b.className = "macro-btn" + (MACRO_FLAGS[i] === flag ? " active-" + flag : ""); }
  }
  var dot = MACRO_DOTS[flag] || "&#9899;";
  var when = m.set_at ? m.set_at : "";
  var meta = document.getElementById("macro-meta");
  if(meta){ meta.innerHTML = "Macro: " + flag + " " + dot +
      (when ? ' <span class="dim">(set ' + when + (m.set_by ? " by " + m.set_by : "") + ")</span>" : ""); }
  var hdr = document.getElementById("macro-hdr");
  if(hdr){ hdr.innerHTML = "Macro: " + flag + " " + dot + (when ? " &middot; set " + when : ""); }
}
function setMacro(flag){
  fetch("/api/macro", {method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({flag:flag, by:"Nick"})})
    .then(function(r){ return r.json(); })
    .then(function(m){ if(m && m.flag){ renderMacro(m); } })
    .catch(function(e){ console.error("macro set error", e); });
}

function renderGaius(g){
  var el = document.getElementById("gaius-status");
  if(!el) return;
  g = g || {};
  function tag(ok, ts){
    var col = ok ? "#2ecc71" : "#e74c3c";
    var lbl = ok ? "OK" : "ERROR";
    return '<span style="color:' + col + ';font-weight:700">&#9679; ' + lbl + '</span> '
         + '<span class="dim">' + (ts || "never") + '</span>';
  }
  el.innerHTML = '&#128300; Gaius Strategic Intelligence &nbsp;|&nbsp; '
    + 'Collector: ' + tag(g.collector_ok, g.collector_last) + ' &nbsp;&middot;&nbsp; '
    + 'Market data: ' + tag(g.market_ok, g.market_last);
}

function fmtSaved(v){
  if(v === null || v === undefined) return "£0";
  var n = Number(v);
  var s = Math.abs(n).toLocaleString("en-GB",{minimumFractionDigits:0,maximumFractionDigits:2});
  return (n < 0 ? "-£" : "+£") + s;
}

function renderStayOut(q){
  var body = document.getElementById("soq-body");
  if(!body){ return; }
  if(!q || q.status === "No data yet" || !q.decisions || q.decisions.length === 0){
    body.innerHTML = '<div class="empty">Awaiting first decisions</div>';
    return;
  }
  if(q.status && q.status.indexOf("Error") === 0){
    body.innerHTML = '<div class="empty">' + q.status + '</div>';
    return;
  }
  var html = "";
  html += '<div class="soq-sub">Last 10 decisions &nbsp;&middot;&nbsp; Quality: '
        + (q.quality_score === null || q.quality_score === undefined ? "--" : q.quality_score) + '%</div>';
  html += '<div class="soq-stats">'
        + '\\u2705 Correct: ' + (q.correct || 0)
        + '<span class="sep">&middot;</span>'
        + '\\u274c Wrong: ' + (q.wrong || 0)
        + '<span class="sep">&middot;</span>'
        + '\\u2796 Neutral: ' + (q.neutral || 0)
        + '</div>';
  html += '<div class="soq-stats">Net Saved: <span class="v-correct">' + fmtSaved(q.net_saved)
        + '</span><span class="sep">&middot;</span>Net Missed: <span class="v-wrong">'
        + fmtSaved(q.net_missed === undefined ? 0 : q.net_missed) + '</span></div>';
  var rows = q.decisions.slice(-10);
  html += '<table><thead><tr><th>TIME</th><th>SYSTEM</th><th>DECISION</th><th>VERDICT</th><th>P&amp;L 1HR</th></tr></thead><tbody>';
  for(var i=0;i<rows.length;i++){
    var r = rows[i];
    var verdict = r.verdict || "--";
    var vc = verdict === "CORRECT" ? "v-correct" : (verdict === "WRONG" ? "v-wrong" : "v-neutral");
    html += '<tr>'
          + '<td>' + (r.timestamp || r.time || "--") + '</td>'
          + '<td>' + (r.system || r.symbol || "--") + '</td>'
          + '<td>' + (r.decision || "--") + '</td>'
          + '<td class="' + vc + '">' + verdict + '</td>'
          + '<td>' + (r.pnl_1hr === undefined ? "--" : r.pnl_1hr) + '</td>'
          + '</tr>';
  }
  html += '</tbody></table>';
  body.innerHTML = html;
}

function updateCountdown(){
  var el = document.getElementById("countdown");
  if(!el){ return; }
  var d = new Date();
  var nowSec = d.getUTCHours() * 3600 + d.getUTCMinutes() * 60 + d.getUTCSeconds();
  var sessions = [
    {sec: 0 * 3600,          label: "Asia session opens"},
    {sec: 8 * 3600,          label: "London session opens"},
    {sec: 13 * 3600 + 1800,  label: "US session opens"},
    {sec: 20 * 3600,         label: "US session closes"}
  ];
  var target = null;
  var label = "";
  for(var i=0;i<sessions.length;i++){
    if(sessions[i].sec > nowSec){ target = sessions[i].sec; label = sessions[i].label; break; }
  }
  var remain;
  if(target === null){
    // all four passed -> Asia open 00:00 UTC tomorrow
    remain = (24 * 3600 - nowSec) + 0;
    label = "Asia session opens";
  } else {
    remain = target - nowSec;
  }
  var h = Math.floor(remain / 3600);
  var m = Math.floor((remain % 3600) / 60);
  var sc = remain % 60;
  var pad = function(x){ return (x < 10 ? "0" : "") + x; };
  el.textContent = label + " in " + h + ":" + pad(m) + ":" + pad(sc);
}

function poll(){
  var ctrl = new AbortController();
  var to = setTimeout(function(){ ctrl.abort(); }, 10000);
  fetch("/api/systems", {signal: ctrl.signal})
    .then(function(r){ return r.json(); })
    .then(function(d){ clearTimeout(to); render(d); })
    .catch(function(e){ clearTimeout(to); /* keep last view, never crash */ });
}

function openSys(port){ window.open("http://localhost:" + port, "_blank"); }

function askKill(key, name){
  pendingKill = key;
  document.getElementById("modaltxt").textContent = "Shut down " + name + "?";
  document.getElementById("modal").style.display = "flex";
}
function closeModal(){ document.getElementById("modal").style.display = "none"; pendingKill = null; }
document.getElementById("modalyes").onclick = function(){
  if(!pendingKill){ closeModal(); return; }
  var key = pendingKill;
  fetch("/api/shutdown/" + key, {method:"POST"})
    .then(function(r){ return r.json(); })
    .then(function(){ closeModal(); setTimeout(poll, 1500); })
    .catch(function(){ closeModal(); });
};

function askLift(key, name){
  if(!confirm("Lift Morgan confidence for " + name + " to 50?\\n\\nGaius intervention override: resets the confidence baseline (does NOT wipe trade history), takes effect live on the engine, and Percival notifies Nick.")) return;
  var btn = document.getElementById("lift-" + key);
  if(btn){ btn.disabled = true; btn.textContent = "..."; }
  fetch("/api/lift-confidence/" + key, {method:"POST"})
    .then(function(r){ return r.json(); })
    .then(function(res){
      alert(res.ok ? ("Confidence lift sent to " + name + " -> 50. Takes effect on the engine's next cycle; Percival has notified Nick.")
                    : ("Lift failed: " + (res.error || res.output || "unknown")));
    })
    .catch(function(e){ alert("Lift request failed: " + e); })
    .then(function(){ if(btn){ btn.disabled = false; btn.innerHTML = "&#128301; LIFT"; } setTimeout(poll, 1500); });
}

updateClock();
setInterval(updateClock, 1000);
updateCountdown();
setInterval(updateCountdown, 1000);
poll();
setInterval(poll, 15000);
</script>
<!-- ARCHIE BRIEF (Job 5) -->
<script>
(function(){
  var ARCHIE_LABEL = '&#9993; ARCHIE BRIEF';
  var BASE_CSS = 'margin-left:6px;font-size:12px;font-weight:700;color:#3498db;background:rgba(52,152,219,0.15);border:1px solid rgba(52,152,219,0.6);padding:3px 10px;border-radius:4px;cursor:pointer;vertical-align:middle;';
  function fallback(txt, done){
    var ta=document.createElement('textarea');
    ta.value=txt; ta.style.position='fixed'; ta.style.top='-2000px'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    try{ document.execCommand('copy'); }catch(e){}
    document.body.removeChild(ta); done();
  }
  function copyText(txt, btn){
    function done(){
      btn.style.color='#2ecc71'; btn.style.borderColor='rgba(46,204,113,0.7)';
      btn.textContent='COPIED!';
      setTimeout(function(){ btn.style.cssText=BASE_CSS; btn.innerHTML=ARCHIE_LABEL; },2000);
    }
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(done, function(){ fallback(txt, done); });
    } else { fallback(txt, done); }
  }
  window.archieBrief=function(btn){
    btn.textContent='...';
    fetch('/api/archie-brief').then(function(r){return r.text();}).then(function(txt){
      copyText(txt, btn);
    }).catch(function(){ btn.textContent='ERROR'; setTimeout(function(){ btn.innerHTML=ARCHIE_LABEL; },2000); });
  };
  function inject(){
    if(document.getElementById('archieBtn')) return;
    var btn=document.createElement('button');
    btn.id='archieBtn'; btn.type='button'; btn.innerHTML=ARCHIE_LABEL;
    btn.setAttribute('onclick','archieBrief(this)');
    btn.style.cssText=BASE_CSS;
    var brand=document.querySelector('.brand');
    if(brand){ brand.appendChild(btn); }
    else { var h=document.querySelector('header'); if(h){ h.appendChild(btn); } }
  }
  if(document.readyState==='loading'){ document.addEventListener('DOMContentLoaded', inject); }
  else { inject(); }
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  The Round Table -- Albion Trading Desk command centre")
    log.info("  Monitoring: %s", ", ".join(s["name"] for s in SYSTEMS))
    log.info("  http://localhost:%d", PORT)
    log.info("=" * 60)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
