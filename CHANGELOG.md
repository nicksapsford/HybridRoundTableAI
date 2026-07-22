## [1.0.22] - 2026-07-19
### Changed -- atomic macro flag write (supports live polling)
- `write_macro()` now writes `logs/macro_sentiment.json` atomically (temp file + os.replace,
  fsync) so the four Guinevere systems, which poll the file live (Macro Live Reload), can
  never read a half-written flag. No behavioural change to the macro control.

## [1.0.21] - 2026-07-18
### Fixed -- Snag 18: Lancelot column stale fail counts after session end
- When a running system's market has closed naturally (OUT OF SESSION / WEEKEND /
  DAILY BREAK, computed server-side in UTC), the Lancelot column now shows **BLOCKED**
  instead of a stale cached fail count (e.g. "2 FAILS"). Fail counts are shown only
  while the market is actually in session. New `_session_is_active()` + `session_active`
  row flag; the browser forces BLOCKED when `session_active === false`.

## [1.0.20] - 2026-07-18
### Added -- Macro Sentiment Overlay (Guinevere Part 4)
- **Macro sentiment control** below the system table: four buttons RISK_ON / NEUTRAL /
  RISK_OFF / CRISIS (NEUTRAL default; RISK_ON green, RISK_OFF red, CRISIS flashing red).
- **`/api/macro` route** (GET + POST) persists the flag to `logs/macro_sentiment.json`
  (`{flag,set_at,set_by}`); each Guinevere system re-reads it every 5 min.
- **Header indicator** "Macro: NEUTRAL \u26ab" + last-change timestamp.

# The Round Table -- Changelog

## [1.0.14] - 2026-07-15
### Fixed
- CRITICAL (regression from v1.0.12, commit 3b90f6f): after restart RoundTable
  showed NO data -- version "v--", every system row "---", Gaius "awaiting data",
  frozen clock -- even though GET /api/systems returned correct data (backend was
  fine). Root cause: a JavaScript syntax error disabled the ENTIRE page <script>,
  so poll()/render()/the clock never ran. The v1.0.12 showStartupProgress() spinner
  was written `["|","/","-","\\"]` inside the Python triple-quoted _PAGE string;
  Python collapses `\\` to a single backslash, emitting the unterminated JS string
  `"\"` -> SyntaxError. Fixed to `\\\\` (emits a valid `"\\"`). The same
  backslash/newline-escaping class of bug in v1.0.13's gaiusReport() confirm
  (`\n\n` was becoming real newlines; `\reports\` mangling) is fixed too.
- Added a static validation of the emitted <script> to the build check (scans every
  JS line for unterminated string literals + bracket balance) -- this class of bug
  is invisible to py_compile.

## [1.0.13] - 2026-07-15
### Added
- "GAIUS REPORT" button in the header (next to MAINTENANCE) + POST /gaius-report
  endpoint. Launches ../GaiusAI/gaius_report.py in its own console (cmd /k so the
  output/paths -- or any error -- stay visible); the script compiles the data
  package, calls the Claude API, writes HTML+TXT to GaiusAI/reports/, and opens the
  HTML in the browser on completion (~1-2 min). Same relative-path/derived-launcher
  pattern as START ALL; RoundTable does not block on it.

## [1.0.12] - 2026-07-15
### Fixed
- Snag 5: the maintenance-banner START ALL now relaunches the WHOLE desk --
  Chronicle (5011) and the two Gaius background jobs are launched alongside the 6
  traders, matching START_ALBION.bat. RoundTable is still not relaunched (it is the
  process serving the button). /start-all was previously the 6 traders only.
- Snag 6: after START ALL the maintenance page shows a live startup progress list
  (each system: waiting / starting / online) polled from the new
  /api/startup-status endpoint every 2.5s, and auto-redirects back to the Command
  Centre once all systems are online (or after a 3-minute cap) -- no manual reload.
- Snag 7: GasTrader (and every trader) now launches exactly 2 windows via the
  maintenance route. Cause: START ALL used to run each system's start_*.bat through
  the shell; that wrapper bat's own `cmd /c` console -- titled "<System> - Port 500x"
  by its `title` line -- was the spurious 3rd window. Each process is now launched
  directly (`start /min "<title>" cmd /c <python> <script>`), exactly like
  START_ALBION.bat, with no wrapper console.
### Changed
- /start-all rewritten to launch every process directly from launch-group data with
  START_ALBION-matching stagger; paths derived from this file's location (../<System>)
  rather than hardcoded absolutes, per the relative-paths standing rule. All UTC.

## [1.0.11] - 2026-07-14
### Added
- Gaius (Strategic Intelligence) status row in the footer: reads the sibling
  ../GaiusAI/logs output files and shows the daily collector and weekly
  market-data service last-run timestamps with an OK/ERROR indicator (collector
  ERROR if >25h stale; market-data ERROR if >8 days stale). All times UTC.
- Synced the tracked START_ALBION.bat copy to the current desktop launcher
  (now includes the two Gaius background jobs after the 6 traders).

## [1.0.10] - 2026-07-14
### Added
- Snag 1: START ALL button on the maintenance banner page, so all systems can be
  restarted straight from the browser (no need to double-click START_ALBION.bat).
  Includes an "Are you sure you want to start all systems?" confirmation and reuses
  the existing /start-all endpoint.

## [1.0.9] - 2026-07-13
### Fixed
- Bug B: RoundTable now shows floating P&L for Oil and Gas open positions (was ("ftse","gold","us") only) — prefers each system's spread-inclusive `unrealised_gbp`, with a points×stake fallback.
- Bug C: "Locked" column inherits the new break-even gating from each system (shows "---" until the trailing stop trails to break-even, then the secured-profit figure in green).

## [1.0.8] - 2026-07-12
### Fixed
- START_ALBION.bat rebuilt self-contained using the full pythonw path (removed bare-pythonw / app.py assumptions). Launches each system's dashboard + watchdog silently, staggered, and opens http://localhost:5050. Canonical copy now tracked in this repo.

## [1.0.7] - 2026-07-11
### Added
- [START ALL] button + /start-all endpoint -- staggered launch of all 6 trading systems
- [MAINTENANCE] button + /maintenance endpoint -- graceful shutdown of all systems
- Silent launcher (pythonw) + logs/console.log daily rotation

## [1.0.6] - 2026-07-11
### Changed
- Renamed to "HybridRoundTable A.I. — Command Centre" (header + browser tab title)
### Added
- Split the single ARTHUR column into LANCELOT (CLEAR/BLOCKED/N FAILS) + ARTHUR (decision + confidence, e.g. "STAY OUT (35)")
- LOCKED P&L column (🔒 guaranteed profit/loss at the current trailing stop) between FLOATING and TODAY
- Reads new flat /api/state fields from each system: lancelot_status, lancelot_fails, arthur_decision, arthur_confidence, arthur_consulted, locked_pnl (falls back gracefully if absent)
### Fixed
- GasTrader session status now shows correctly (added a "gas" case to the server-side session clock — 22:00-21:00 UTC, DAILY BREAK 21:00-22:00)
- Systems-running placeholder corrected to -/6 (count itself was already dynamic X/total)

## [1.0.5] - 2026-07-10
### Added
- [📊 REPORTS] button in the header linking to Merlin's Chronicle (http://localhost:5011, opens in new tab)

## [1.0.4] - 2026-07-10
### Added
- NaturalGasTrader (GasTrader) added to the polling list — Natural Gas, port 5006, Forest Green #228B22. Sixth row below OilTrader.

## [1.0.1] - 2026-07-08
### Fixed
- STAY OUT QUALITY panel now ignores PENDING rows in the quality score (matches Morgan's get_summary)
### Changed
- README rewritten with Albion Trading Desk branding

## v1.0.0 -- 7 Jul 2026
### Added
- Master dashboard for all four systems
- Real-time polling every 15 seconds
- Session status per system in UTC
- Portfolio total and today P&L
- Albion Trading Desk branding
- Port 5050
