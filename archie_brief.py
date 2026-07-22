"""Prototype Archie Brief builders -- tested against live /api/state before being
inserted into the dashboards. Plain text only, all UTC, reads already-assembled
state (no new external fetch)."""
import csv
import os
from datetime import datetime, timezone

BAR = "=" * 64


def _pmove(v):
    """Phantom directional move -> '+GBP x.xx' / '-GBP x.xx' / '--'."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "--"
    return ("+GBP %.2f" % n) if n >= 0 else ("-GBP %.2f" % abs(n))


def _num(v, nd=2):
    try:
        return ("{:." + str(nd) + "f}").format(float(v))
    except (TypeError, ValueError):
        return "--"


def _ssl(ind):
    if not isinstance(ind, dict) or ind.get("ssl_bull") is None:
        return "--"
    return "BULL" if ind.get("ssl_bull") else "BEAR"


def _morgan_journey(logs_dir):
    """(start, current) confidence from morgan_confidence.csv, else (None, None)."""
    try:
        path = os.path.join(logs_dir, "morgan_confidence.csv")
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        if rows:
            return rows[0].get("confidence"), rows[-1].get("confidence")
    except Exception:
        pass
    return None, None


def build_system_brief(state, system_name, asset_label, logs_dir=None, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    ver = state.get("version_string") or ("v" + str(state.get("version", "--")))
    price = None
    for pk in ("gas_price_usd", "oil_price_usd", "gold_price_usd",
               "ftse_level", "us_level", "price", "current_price"):
        if state.get(pk) is not None:
            price = state[pk]
            break
    session = state.get("liquidity_period") or state.get("phase") or "--"
    i1d = state.get("indicators_1d") or {}
    i1h = state.get("indicators_1h") or {}
    i5m = state.get("indicators_5m") or {}
    perf = state.get("perf") or state.get("performance") or {}
    soq = state.get("stay_out_quality") or {}
    dec = state.get("decision") if isinstance(state.get("decision"), dict) else {}
    cal = state.get("calendar")

    L = []
    a = L.append
    a(BAR)
    a("ARCHIE BRIEF -- %s %s" % (system_name, ver))
    a("Generated: %s UTC" % now_utc.strftime("%Y-%m-%d %H:%M:%S"))
    a(BAR)
    a("")
    a("MARKET")
    pnd = 2 if (isinstance(price, (int, float)) and price < 100) else 1
    a("%s %s | Session: %s" % (asset_label, _num(price, pnd), session))
    if cal:
        a(str(cal))
    a("")
    a("TREND")
    a("Daily:  SSL %s  RSI %s" % (_ssl(i1d), _num(i1d.get("rsi"), 1)))
    a("1-Hour: SSL %s  RSI %s  MACD %s  TMO %s  Chande MO %s  Money Flow %s" % (
        _ssl(i1h), _num(i1h.get("rsi"), 1), _num(i1h.get("macd"), 3),
        _num(i1h.get("tmo_main"), 2), _num(i1h.get("chande_mo"), 1), _num(i1h.get("money_flow"), 1)))
    a("5-Min:  SSL %s  RSI %s  MACD %s  TMO %s  Chande MO %s  Money Flow %s" % (
        _ssl(i5m), _num(i5m.get("rsi"), 1), _num(i5m.get("macd"), 3),
        _num(i5m.get("tmo_main"), 2), _num(i5m.get("chande_mo"), 1), _num(i5m.get("money_flow"), 1)))
    a("")
    a("ARTHUR")
    a("Decision: %s" % (state.get("arthur_decision") or "--"))
    ac = state.get("arthur_confidence")
    a("Confidence: %s | Liquidity Bias: %s" % (
        ac if ac is not None else "--", dec.get("liquidity_bias") or "--"))
    if dec.get("reasoning"):
        a("Reasoning: %s" % dec.get("reasoning"))
    a("")
    a("OPEN POSITION")
    ct = state.get("current_trade")
    if isinstance(ct, dict) and ct:
        a("Direction: %s | Entry: %s" % (ct.get("direction", "--"), _num(ct.get("entry_price"), pnd)))
        a("Stop: %s | Target: %s" % (_num(ct.get("stop_loss"), pnd), _num(ct.get("take_profit"), pnd)))
        pnl = state.get("unrealised_gbp")
        pnl = ct.get("pnl_gbp") if pnl is None else pnl
        a("P&L GBP: %s" % _num(pnl, 2))
    else:
        a("No open position")
    a("")
    a("MORGAN")
    start, cur = _morgan_journey(logs_dir) if logs_dir else (None, None)
    jr = ""
    if start is not None and cur is not None:
        jr = " | Journey: %s->%s" % (_num(start, 0), _num(cur, 0))
    a("Score: %s/100 (%s)%s" % (perf.get("confidence_score", "--"), perf.get("confidence_level", "--"), jr))
    a("")
    a("STAY OUT QUALITY")
    dcount = soq.get("decisions")
    if isinstance(dcount, list):
        dcount = len(dcount)
    if dcount is None:
        dcount = soq.get("correct", 0) + soq.get("wrong", 0) + soq.get("neutral", 0)
    a("Last %s decisions | Quality: %s%%" % (dcount,
      soq.get("quality_score") if soq.get("quality_score") is not None else "--"))
    a("Correct: %s | Wrong: %s | Neutral: %s" % (
        soq.get("correct", 0), soq.get("wrong", 0), soq.get("neutral", 0)))
    a("Net Saved: GBP %s | Net Missed: GBP %s" % (_num(soq.get("net_saved", 0), 2), _num(soq.get("net_missed", 0), 2)))
    a("")
    a("GUINEVERE")
    a(str(cal) if cal else "No Guinevere data")
    a("")
    a("LANCELOT PRE-CHECKS")
    pc = state.get("pre_checks")
    if isinstance(pc, dict) and pc:
        for k, v in pc.items():
            a("  [%s] %s" % ("PASS" if v else "FAIL", k))
    else:
        a("  %s" % (state.get("lancelot_status") or "n/a"))
    a("")
    a("SYSTEM")
    cs = state.get("connector_status")
    feed = "OK" if cs in ("capitalcom", "kraken", "ig", "yahoo") else (cs or "--")
    a("Mode: %s | Version: %s | Feed: %s" % (state.get("mode", "--"), ver, feed))
    a(BAR)
    a("End of %s Archie Brief" % system_name)
    a(BAR)
    return "\n".join(L)


def build_roundtable_brief(agg, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    L = []
    a = L.append
    a(BAR)
    a("ARCHIE BRIEF -- HYBRID ROUNDTABLE COMMAND CENTRE")
    a("Generated: %s UTC" % now_utc.strftime("%Y-%m-%d %H:%M:%S"))
    a(BAR)
    a("")
    a("PORTFOLIO (Hybrid Desk vs Original vs Benchmark)")
    a("Hybrid total: GBP %s | Today: GBP %s | Systems online: %s/%s" % (
        _num(agg.get("portfolio", 0), 2), _num(agg.get("today_pnl", 0), 2),
        agg.get("running", 0), agg.get("total", 7)))
    a("Cum P&L -- Hybrid: GBP %s | Original: GBP %s | Benchmark: GBP %s" % (
        _num(agg.get("hybrid_cum", 0), 2), _num(agg.get("orig_cum", 0), 2), _num(agg.get("bench_cum", 0), 2)))
    a("Delta vs Original: GBP %s | ARTHUR EXIT VALUE (Hybrid - Benchmark): GBP %s" % (
        _num(agg.get("delta_orig", 0), 2), _num(agg.get("delta_bench", 0), 2)))
    a("")
    a("SYSTEMS")
    opens = []
    for s in agg.get("systems", []):
        pxt = (s.get("price_html", "") or "")
        pxt = pxt.replace("£", "GBP ").replace("&pound;", "GBP ").replace("&nbsp;", " ").replace("&middot;", "-")
        a("%-13s %-8s %-24s %s" % (s.get("name", "?"), s.get("status", "?"), pxt, s.get("position", "")))
        conf = s.get("confidence")
        a("   locked=%s  today=GBP %s  bal=GBP %s  lancelot=%s  arthur=%s  morgan=%s" % (
            s.get("locked_pnl") if s.get("locked_pnl") is not None else "--",
            _num(s.get("daily_pnl", 0), 2), _num(s.get("balance", 0), 2),
            s.get("lancelot_status", "--"),
            s.get("arthur_decision") or s.get("decision") or "--",
            conf if conf is not None else "--"))
        if (s.get("position") or "").upper() not in ("", "NO_TRADE", "FLAT", "NONE"):
            opens.append("%s %s (float GBP %s)" % (s.get("name"), s.get("position"),
                         _num(s.get("float_gbp", 0), 2)))
    a("")
    a("OPEN POSITIONS")
    a("\n".join(opens) if opens else "No open positions")
    a("")
    a("THREE-WAY COMPARISON (cumulative P&L per matched system)")
    a("  Hybrid minus Benchmark = Arthur's pure exit value (both share the Lancelot entry)")
    for s in agg.get("systems", []):
        def _c(v):
            return ("GBP %s" % _num(v, 2)) if v is not None else "--"
        a("  %-13s  Original %-11s Benchmark %-11s Hybrid %-11s Exit value %s" % (
            s.get("name", "?"), _c(s.get("orig_cum")), _c(s.get("bench_cum")),
            _c(s.get("hybrid_cum")), _c(s.get("exit_value"))))
    a("")
    a("GAIUS")
    g = agg.get("gaius") or {}
    a("Collector:   %s  %s" % ("OK" if g.get("collector_ok") else "ERROR", g.get("collector_last") or "never"))
    a("Market data: %s  %s" % ("OK" if g.get("market_ok") else "ERROR", g.get("market_last") or "never"))
    a("")
    a("ALERTS")
    al = []
    for s in agg.get("systems", []):
        if s.get("status") not in ("RUNNING", "OK"):
            al.append("%s is %s" % (s.get("name"), s.get("status")))
        ls = s.get("lancelot_status") or ""
        if "FAIL" in ls:
            al.append("%s Lancelot: %s" % (s.get("name"), ls))
    a("\n".join(al) if al else "None")
    a(BAR)
    a("End of RoundTable Archie Brief")
    a(BAR)
    return "\n".join(L)
