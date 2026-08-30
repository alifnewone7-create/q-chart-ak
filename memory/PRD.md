# SignalMaster Pro — Telegram/Quotex Signal Bot (repo mirror of q-new-jour)

## Original problem statement
Mirror of the user's GitHub repo `q-new-jour.git` into `/app` (default Emergent React/FastAPI
scaffold fully removed). This is a standalone Python Telegram bot — no web servers, no supervisor
services. Runtime credentials (.env: BOT_TOKEN, QUOTEX_EMAIL/PASSWORD) are intentionally absent;
the bot must NOT be started here. All verification is done with offline python scripts / pytest.

## User language
Bengali (Latin script). Always reply in Bengali.

## Architecture
- `backend/bot.py` — python-telegram-bot admin UI + watchdogs
- `backend/qx.py`, `backend/pyquotex/` — Quotex broker client
- `backend/ticks.py` — live 1m candle builder; `backend/sessions.py` — signal session engine
- `backend/analysis.py` — routes to the active strategy
- `backend/strategies/` — package: `classic.py`, `otc_sniper.py`, `zone_sniper.py`, `common.py`
- `backend/charting.py` — matplotlib HUD chart PNG (SignalMaster Pro design)
- `backend/indicators_py.py` — pure-python cached indicator context (Ctx)
- `backend/SignalMaster.service` — systemd unit

## Implemented
### 2026-06 (earlier sessions)
- Repo mirrored, scaffold wiped; rebrand TaNix -> SignalMaster Pro
- Chart redesign (target logo, conditional S/R levels, entry-candle box + WIN/LOSS pill,
  no ENTRY/BUY/SELL on result image)
- Zone Reversal Sniper strategy added; strategies refactored into a package
- systemd unit renamed to SignalMaster.service

### 2026-06 — Strategy mega-upgrade (this session)
- **indicators_py.Ctx**: new `adx(p)` -> (ADX, DI+, DI-) series.
- **strategies/common.py**: `_choppiness` (Choppiness Index), `_squeeze_on` (TTM squeeze),
  `no_trade(x, i)` universal gate (news-spike candle, dead chop ADX<13+CHOP>62, 4-candle
  active squeeze, flat EMAs w/ no efficiency), plus shared modules `_m_adx_dir`, `_m_cci`,
  `_m_willr`, `_m_heikin`, `_m_accel`, `_m_squeeze_break`.
- **Classic Momentum**: rewritten 4 -> 12 weighted filters (originals kept as votes), min_candles
  15 -> 30, min_confidence 55 -> 58, no-trade gate, agreement-based confidence.
- **OTC Sniper Pro**: 15 -> 21 modules (adds ADX dir, Heikin-Ashi, acceleration,
  squeeze-release, CCI, Williams %R), no-trade gate. Regime + self-weighting intact.
- **Zone Reversal Sniper**: 15 -> 20 filters (adds fresh-vs-ground-down zone, wick cluster,
  parallel channel position, level confluence stack, squeeze-release), no-trade gate.
- **All engines may now return None on untradeable markets** (no-trade gate) — the session
  scanner simply skips those markets.
- **Deep-analysis mode (sessions.py)**: after a LOSS the next signal must pass
  (a) confidence gate +12 (cap 90), (b) filter agreement >= 0.70, (c) ALL other engines run on
  the same candles and must agree on the direction (`_deep_pass`). Signal caption gets a
  "Deep analysis" note. Mode resets to normal after WIN / WIN_MTG.
- **Chart trendlines**: `zone_trendlines()` public helper (tight least-squares fit through last
  3-4 swing pivots, residual < 0.9 ATR); charting.py draws dashed diagonal S/R trendlines with
  "TL" tag + legend entry.
- Tests: `tests/verify_upgrades.py` (36 checks, all pass), verify_zone_sniper.py updated for the
  new contracts (all pass), test_strategies.py updated (31/31 pass serially with `-n 0`).

## Testing notes
- Test deps (aiogram, python-telegram-bot, telethon, orjson, ...) are pip-installed in the pod
  for OFFLINE testing only; the bot itself is never started (no .env by design).
- Full suite baseline: 29 pre-existing failures in test_messages/martingale/bot_units/
  channel_selection (stale expectations, documented before this session) — unchanged.
- TestSessionWiring pick-best tests are wall-clock sensitive (fail if run during the last 8s of
  a real minute) — pre-existing design, pass in isolation.
- Runtime checks: `python tests/verify_upgrades.py`, `python tests/verify_zone_sniper.py`,
  `python tests/verify_chart_design.py`, `python -m pytest tests/test_strategies.py -n 0`.

## Backlog
### P1
- Bot logic walkthrough (plain-English map bot.py -> qx.py -> strategies)
- Secret sweep (scan all files for leaked keys/tokens with file+line numbers)
### P2
- VPS deploy prep (fresh-server steps for SignalMaster.service)
- Controlled startup (install deps + start bot only on user go-ahead)
### P3 / Backlog
- Strategy scoreboard (win rate per strategy, persisted)
- Threshold control (adjust confidence gate from the bot menu)
- Deep-mode stats in partial report (how many signals were deep-verified)
