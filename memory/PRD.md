# q-chart-fin — TaNix Alpha 2.0 Telegram Signal Bot

## Original problem statement
1. **Session 1 (import)**: Verbatim full-repo import of `https://github.com/alifnewone7-create/q-chart-fin.git`
   (branch `main`, commit `e1ec500`) into `/app/q-chart-fin/` with full 41-commit `.git` history,
   `origin` remote intact. No build, no run, no dependency install, no template file touched.
2. **Session 2 (features)**: Owner username in the signal message must come from `.env` and render in
   normal bold so it stays clickable in Telegram. Result chart image must not show LOSS (only WIN, under
   PERFORMANCE). Add a premium `ENGINE` section reading `TaNix Ultra Volt` under `Signal Details` on the
   signal image and under `PERFORMANCE` on the result image.
3. **Session 3 (bug)**: Bot runs on a VPS under systemd but goes silently "off" after roughly 24 hours;
   only a manual `systemctl restart` brings it back. Find and fix the root cause.

## Architecture
- Standalone Python Telegram bot package (NOT the /app FastAPI+React template — that stays dormant).
- `backend/bot.py` — python-telegram-bot admin UI, `run_polling`, watchdog task.
- `backend/qx.py` — `QuotexManager`, single `asyncio.Lock` gating every Quotex call.
- `backend/pyquotex/` — vendored broker client (websocket in a daemon thread).
- `backend/ticks.py` — `TickCollector`, builds live 1m candles for ~400 markets.
- `backend/sessions.py` — `SessionManager`, signal -> result -> partial report loop.
- `backend/analysis.py` / `strategies.py` — strategy selection and confidence scoring.
- `backend/charting.py` — matplotlib HUD chart PNG (signal + result frames).
- `backend/messages.py` / `premium_emojis.py` / `notifier.py` / `user_sender.py` — caption templates,
  premium custom-emoji pipeline, aiogram bot sender and Telethon MTProto premium-user sender.
- `backend_encrypted/` and `backend_vps_encrypted/` — pyarmor build artifacts, NOT editable.
  All feature work must happen in `backend/` and be rebuilt.

## User personas
- **Owner/admin** (single Telegram admin id): starts/stops sessions, picks markets or auto-select by
  payout threshold, chooses target channels, reads partial reports.
- **Channel subscribers**: read-only consumers of signal and result posts.

## Core requirements (static)
- Signals and results are posted as chart images with premium-emoji captions.
- Owner tag comes from `.env` (`OWNER_TAG`) and must be a clickable @mention.
- Times are always UTC+6 (Asia/Dhaka), forced in `config.py`.
- MTG (martingale) is 1 step; P&L uses the recovery model in `sessions.compute_delta`.
- The bot must survive multi-day unattended operation on a VPS under systemd.

## Implemented

### 2026-06 — Repo import
- Cloned into `/app/q-chart-fin/` with full history. Verified: 41 commits, HEAD `e1ec500`,
  branch `main`, `origin` present, 313 tracked files = 313 on disk, clean tree.
- 8 top-level folders + 4 root files present. Nested git repo (root git treats it as opaque).

### 2026-06 — Owner tag + chart image features
- `config.py`: `OWNER_TAG` default changed to `@BfsTraderQX`, still read from `.env`.
- `messages.py`: owner rendered as `<b>{owner_tag}</b>` instead of `mono(owner_tag)` so the
  @mention stays clickable.
- `premium_emojis.py`: added `_strip_bold()` and `bold_entities()`; `_replace_all()` and
  `plain_html()` now preserve `<b>`/`</b>` through HTML escaping; `to_entities()` strips the
  bold tags from the MTProto plain text (signature kept for the existing test suite).
- `user_sender.py`: `_entities()` now emits `MessageEntityBold` alongside `MessageEntityCustomEmoji`.
- `charting.py`: removed the `LOSSES` stat box (single full-width `WINS` card); added
  `engine_section()` premium gold card (`TaNix Ultra Volt` + `PRECISION ENGINE`) under
  `SIGNAL DETAILS` on the signal image and under `PERFORMANCE` on the result image.

### 2026-06 — ~24h silent freeze fix (root cause found)
RCA: every Quotex call funnels through `QuotexManager.ensure_connected()` under one
`asyncio.Lock`. Inside it, the vendored pyquotex had **unbounded waits** —
`QuotexAPI.start_websocket()` used `while True: await asyncio.sleep(0.1)` with no timeout, and
`QuotexAPI.close()` called `websocket_thread.join()` with no timeout (a blocking join inside async
code). One stalled socket therefore held the lock forever: the tick collector, the signal loop and
every admin button blocked, while the **process stayed alive** so systemd's `Restart=always` never
fired. Secondary cause: `Quotex.connect()` reassigned `self.api` *before* calling `close()`, so the
old websocket thread was never closed and kept feeding buffers nobody drained — an FD/memory leak
that grew for ~24h.

- `pyquotex/api.py`: added `WS_HANDSHAKE_TIMEOUT=45` deadline to `start_websocket()` (all four
  original state-flag return paths kept intact) and `WS_JOIN_TIMEOUT=10` bounded join in `close()`,
  which now also clears `websocket_client`.
- `pyquotex/stable_api.py`: `Quotex.connect()` closes the PREVIOUS `self.api` before reassigning;
  `Quotex.close()` guards `self.api is None`.
- `qx.py`: `CHECK_TIMEOUT`/`CONNECT_TIMEOUT`/`CLOSE_TIMEOUT` wrap every network step in
  `asyncio.wait_for`; new `_discard()` closes the client being replaced; new `last_ok` timestamp.
- `bot.py`: new `_watchdog()` task (started in `post_init`, cancelled in `post_shutdown`) that
  revives a dead signal loop and `os._exit(1)`s after `TICK_STALL_LIMIT=900`s with no ticks so
  systemd restarts the bot. Deliberately does nothing while `TICKS.started_at is None` to avoid a
  boot-time restart loop.
- `start.py`: replaced `logging.disable(logging.CRITICAL)` + every-logger-disabled with a
  `RotatingFileHandler` writing `backend/data/bot.log` (console stays silent) so a future incident
  leaves evidence.
- `tanix-bot.service`: `StartLimitIntervalSec=0` / `StartLimitBurst=0` so systemd never gives up.
- Verified: 39/39 targeted tests in `backend/tests/test_freeze_fix.py`; full repo suite shows the
  same 29 failures as the untouched `e1ec500` baseline (stale pre-existing expectations), so no
  regressions were introduced.

### 2026-06 — ~30-48h freeze, ROUND 2 (real root cause: blocked event loop)
User feedback: still going off, just later (30-48h), and critically **`/start` gets no reply**.
`bot.cmd_start` never touches Quotex, so no reply proves the whole **asyncio event loop** was
blocked — not just the Quotex lock. Round-1 fixes were therefore incomplete, and worse, gave false
confidence: `asyncio.wait_for` CANNOT cancel a synchronous call, and the round-1 `_watchdog()` was
an asyncio task so it froze along with the loop and could never rescue anything.

RCA: the vendored login path is synchronous. `pyquotex/http/navigator.py Browser(requests.Session)
.send_request()` did blocking `requests` I/O with **no timeout**, called straight from the event loop
by `pyquotex/http/login.py Login.__call__` -> `get_token()` / `_post()` / `get_profile()`. Quotex
sessions expire every ~30-48h, triggering a fresh credential login; one half-open TCP connection then
froze Telegram polling, the tick collector and the signal loop simultaneously, with the process still
alive so systemd never restarted it.

- `pyquotex/http/navigator.py`: `DEFAULT_HTTP_TIMEOUT = (15, 45)` applied via
  `kwargs.setdefault("timeout", ...)` in `send_request()` (explicit caller values still win).
- `pyquotex/http/login.py`: `get_token`, `get_profile` and the `send_request` calls in `_post()` and
  `awaiting_pin()` are now offloaded with `loop.run_in_executor(None, ...)` so the loop keeps
  breathing during login.
- `pyquotex/http/login.py`: `awaiting_pin()` raises a clear RuntimeError when `sys.stdin` is absent or
  not a tty instead of calling `input()` (which blocked the loop on a tty and raised an uncaught
  EOFError on /dev/null).
- `bot.py`: new `_heartbeat()` asyncio task writing `_HEARTBEAT["loop"]` every second, plus
  `_hard_watchdog()` running in a **plain daemon thread** (started in `post_init`) that calls
  `os._exit(1)` after `LOOP_STALL_LIMIT=300`s without a heartbeat. A thread is the only supervisor
  that still runs while the loop is blocked. `post_shutdown` cancels both tasks.
- Verified: 23/23 new tests (`backend/tests/test_freeze_fix_r2.py`) + 39/39 round-1 tests
  (`test_freeze_fix.py`); whole suite still exactly 29 pre-existing failures, no new regressions.
  The "loop kept breathing during a slow login" test asserts a concurrent heartbeat coroutine kept
  ticking (max gap < 0.35s) through a 0.8s blocking login.

### Open verification item (blocking confirmation)
The user reported **no log output at all**, yet round 1 enabled `backend/data/bot.log`. Either the new
code was never deployed, or they are running one of the encrypted builds. Asked them to check
`ls -la /root/tanix-bot/data/bot.log`, `grep RotatingFileHandler /root/tanix-bot/start.py` and
`systemctl status tanix-bot`. Until that is confirmed, we cannot tell whether the round-1 fixes were
ever actually live.

## Backlog

### P0
- **Confirm the fixed code is actually deployed** (bot.log exists, start.py has the
  RotatingFileHandler). The user saw no logs at all, which suggests it may not be live.
- Deploy the fixed `backend/` to the VPS and watch `backend/data/bot.log` for a full 48h to confirm
  the freeze is gone in production (only unit-verified so far, never run against the live broker).
- Rebuild `backend_encrypted/` and `backend_vps_encrypted/` from the fixed `backend/` — the shipped
  obfuscated builds still contain the freeze bug.

### P1
- Chart footer still hardcodes `Developed by @iamhear1`; should read `OWNER_TAG` from `.env`.
- `pyquotex` caches `api.instruments` forever, so payouts never refresh within a connection.
- `start_websocket()` returns `True` on "Websocket Token Rejected." (upstream pyquotex bug) — works
  only because `check_connect()` then fails; worth making explicit.
- 29 pre-existing test failures in `test_messages.py`, `test_martingale_pnl.py`, `test_bot_units.py`,
  `test_channel_selection.py` — stale expectations from before this repo's own earlier changes.
- `tests/test_charting_pricetag.py` imports `backend.charting`, which only resolves from the repo
  root, so it fails collection when running pytest from `backend/`.

### P2
- Make the engine name configurable from Telegram instead of hardcoded in `charting.py`.
- Hide losses in the partial report / result caption too, if the owner wants that everywhere.
- Persist session state so a watchdog restart can resume an in-flight signal session.
