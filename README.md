# HybridRoundTable A.I.

Command centre for the **Hybrid Desk** — the desk's third parallel operation
(alongside the Original desk and Benchmark desk). Port **5050**.

The Hybrid desk uses a hybrid architecture: **Lancelot enters, Arthur manages exits.**
Hybrid system ports mirror the Original desk **+40** (5041–5048).

## The unique metric
This command centre exists for one measurement the other two RoundTables cannot give:

    HYBRID P&L  −  BENCHMARK P&L  =  Arthur's pure exit value

(both desks share the Lancelot entry, so the difference is purely Arthur's exit
management — Gaius Commission 005, 22 Jul 2026). Shown per matched system and as a
desk total ("ARTHUR EXIT VALUE").

## What it shows
- **Portfolio**: Hybrid total + Original / Benchmark cumulative P&L + deltas + today + N/7.
- **Three-way comparison table**: Original | Benchmark | Hybrid | Arthur exit value, per system.
- **Systems table**: price, position, floating, locked, today, balance, Lancelot, **Arthur (HOLD/EXIT)**, Morgan, session.
- **Archie Brief** button (three-way portfolio + per-system Arthur decision + comparison + open positions).
- Per-system open/shutdown controls; START ALL / MAINTENANCE manage the **Hybrid** systems only
  (Chronicle + Gaius are shared services owned by the Original RoundTable and are left running).

Reads Original desk (5001–5008), Benchmark desk (5021–5026) and Hybrid systems (5041–5048)
via same-machine `/api/state`. Unbuilt hybrid systems show "awaiting build / launch" and
appear automatically once running. Read-only aggregator — no logs, no .env, no trading.

## Running
```
python dashboard_hybridroundtable.py     # port 5050
```
Or the **Start HybridRoundTable** desktop shortcut. All times UTC.
