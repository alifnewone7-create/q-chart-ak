/**
 * 📡 signal.js — "Get Signal" button: transparent, rule-based technical bias
 * =============================================================================
 * WHAT THIS IS (please read before wiring it to real trades):
 *   • A simple EMA-alignment heuristic (fast=9 / mid=21 / slow=50), the same
 *     category as the built-in "SignalArrows" indicator already in this repo
 *     (indicators.js → TPL_ARROWS). Nothing here is a black box.
 *   • Every number in the panel is computed live from the candles you
 *     already have loaded:
 *       - "Indicator Agreement" = how many of 5 independently-listed checks
 *         currently point the same way (not a probability, just a count).
 *       - "Backtested Accuracy" = replaying this exact rule over your
 *         loaded candle history and scoring it against what candles
 *         actually did next. Real, computed, usually far from 100%.
 *   • It deliberately does NOT show a made-up "confidence %" or a
 *     "100% win rate" badge. Numbers like that aren't measuring anything —
 *     they're just there to look convincing, and on short-duration OTC
 *     binary options that can cost real money. See the on-screen disclaimer.
 *
 * Dependencies: indicators.js (IndicatorBase, Indicators), chart.js (CM),
 *               datafeed.js (AppState), app.js (toast)
 */

const SIGNAL_PERIODS = { fast: 9, mid: 21, slow: 50 };
const SIGNAL_COLORS  = { fast: '#22d3ee', mid: '#c084fc', slow: '#d4af37' };

// =============================================================================
// 🧮 EMA helper — same formula/seeding as indicators.js TPL_EMA, kept consistent
// =============================================================================
function emaSeries(candles, period) {
    if (!candles || candles.length < 2) return [];
    const k = 2 / (period + 1);
    let val = candles[0].close;
    const out = [val];
    for (let i = 1; i < candles.length; i++) {
        val = (candles[i].close - val) * k + val;
        out.push(val);
    }
    return out;
}

// =============================================================================
// 📈 Always-on Triple-EMA overlay — registers into AppState.indicators just
// like any indicator started from the Editor, so it gets live updates for free
// =============================================================================
Indicators.TripleMA = class extends IndicatorBase {
    constructor(period, color) {
        super({ type: 'overlay' });
        this.settings = { period, color, lineWidth: 2 };
        this._series = null;
    }
    init(cm) {
        super.init(cm);
        this._series = this.createOverlayLine({
            color: this.settings.color,
            lineWidth: this.settings.lineWidth,
            lastValueVisible: true,
            priceLineVisible: false,
            title: 'EMA(' + this.settings.period + ')'
        });
    }
    update(candles) {
        try {
            if (!candles || !candles.length || !this._series) return;
            const values = emaSeries(candles, this.settings.period);
            const result = candles.map((c, i) => ({ time: c.time, value: values[i] }));
            this._series.setData(result);
            this._lastCalculatedValue = values[values.length - 1];
            this._initialized = true;
        } catch (e) { console.warn('TripleMA update error:', e); }
    }
    // Matches the MA/EMA templates' convention: extend the last closed value on
    // live (still-open) ticks instead of recalculating, so the line doesn't jitter.
    updateLast(candle) {
        if (this._initialized && candle && this._series && this._lastCalculatedValue !== null) {
            this._series.update({ time: candle.time, value: this._lastCalculatedValue });
        }
    }
    destroy() { this._series = null; super.destroy(); }
};

function loadTripleMAOverlay() {
    if (!window.CM || typeof Indicators === 'undefined') { setTimeout(loadTripleMAOverlay, 300); return; }
    ['fast', 'mid', 'slow'].forEach(key => {
        const name = 'TripleMA_' + key;
        if (AppState.indicators[name]) return; // already loaded, don't duplicate
        try {
            const inst = new Indicators.TripleMA(SIGNAL_PERIODS[key], SIGNAL_COLORS[key]);
            inst.init(window.CM);
            if (AppState.currentCandles?.length) inst.update(AppState.currentCandles);
            AppState.indicators[name] = inst;
        } catch (e) { console.warn('⚠️ Failed to load TripleMA overlay:', key, e); }
    });
}

// =============================================================================
// 🧮 Signal computation — every field below is independently reproducible
// from AppState.currentCandles. No fabricated numbers.
// =============================================================================
function computeSignal() {
    const candles = AppState.currentCandles;
    const { fast: FAST, mid: MID, slow: SLOW } = SIGNAL_PERIODS;
    const MIN_NEEDED = SLOW + 10;

    if (!candles || candles.length < MIN_NEEDED) {
        return { ok: false, reason: `Need at least ${MIN_NEEDED} loaded candles to compute this (have ${candles?.length || 0}). Give the chart a moment to load more history.` };
    }

    const fastArr = emaSeries(candles, FAST);
    const midArr  = emaSeries(candles, MID);
    const slowArr = emaSeries(candles, SLOW);
    const n = candles.length;

    const price = candles[n - 1].close;
    const f = fastArr[n - 1], m = midArr[n - 1], s = slowArr[n - 1];
    const fPrev = fastArr[n - 2], mPrev = midArr[n - 2];

    // 5 plain, independently-checkable conditions — this IS the whole method.
    const checks = [
        { label: `Fast EMA(${FAST}) above Mid EMA(${MID})`, up: f > m },
        { label: `Mid EMA(${MID}) above Slow EMA(${SLOW})`, up: m > s },
        { label: 'Price above Fast EMA',                    up: price > f },
        { label: 'Fast EMA rising',                         up: f > fPrev },
        { label: 'Mid EMA rising',                           up: m > mPrev }
    ];
    const upCount = checks.filter(c => c.up).length;
    const downCount = checks.length - upCount;
    const direction = upCount === downCount ? 'NEUTRAL' : (upCount > downCount ? 'CALL' : 'PUT');
    const agree = Math.max(upCount, downCount);

    // Genuine backtest: replay full 3-EMA alignment across loaded history,
    // score each occurrence against what the NEXT candle actually did.
    let wins = 0, total = 0;
    for (let i = SLOW + 1; i < n - 1; i++) {
        const bull = fastArr[i] > midArr[i] && midArr[i] > slowArr[i];
        const bear = fastArr[i] < midArr[i] && midArr[i] < slowArr[i];
        if (!bull && !bear) continue;
        total++;
        const nextUp = candles[i + 1].close > candles[i].close;
        if ((bull && nextUp) || (bear && !nextUp)) wins++;
    }

    return {
        ok: true,
        direction, checks, agree, agreeTotal: checks.length,
        wins, total, accuracy: total > 0 ? (wins / total * 100) : null,
        asset: AppState.currentAsset, timeframe: AppState.currentTimeframe,
        price, time: new Date().toLocaleTimeString()
    };
}

// =============================================================================
// 🖥️ Panel rendering
// =============================================================================
function renderSignalPanel(r) {
    const body = document.getElementById('signalPanelBody');
    if (!body) return;

    if (!r.ok) {
        body.innerHTML = `<div class="sig-empty">⏳ ${r.reason}</div>`;
        return;
    }

    const dirClass = r.direction === 'CALL' ? 'up' : (r.direction === 'PUT' ? 'down' : 'neutral');
    const arrow = r.direction === 'CALL' ? '▲' : (r.direction === 'PUT' ? '▼' : '■');
    const accText = r.accuracy === null
        ? 'Not enough past 3-EMA alignments yet in this chart\'s history to backtest.'
        : `${r.accuracy.toFixed(1)}% correct — ${r.wins} of ${r.total} past signals in your currently loaded history.`;

    const checksHtml = r.checks.map(c =>
        `<div class="sp-row"><label>${c.label}</label><span class="sig-check ${c.up ? 'up' : 'down'}">${c.up ? '▲' : '▼'}</span></div>`
    ).join('');

    body.innerHTML = `
        <div class="sig-direction ${dirClass}">${arrow} ${r.direction}</div>
        <div class="sp-section">
            <span class="sp-label">Indicator Agreement — ${r.agree}/${r.agreeTotal}</span>
            ${checksHtml}
        </div>
        <div class="divider"></div>
        <div class="sp-section">
            <span class="sp-label">Backtested Accuracy (real, computed)</span>
            <div class="sig-acc">${accText}</div>
        </div>
        <div class="divider"></div>
        <div class="sp-row"><label>Asset</label><span>${r.asset}</span></div>
        <div class="sp-row"><label>Timeframe</label><span>${r.timeframe}</span></div>
        <div class="sp-row"><label>Price</label><span>${r.price}</span></div>
        <div class="sp-row"><label>Computed at</label><span>${r.time}</span></div>
        <div class="divider"></div>
        <div class="sig-disclaimer">⚠️ Technical bias only, from EMA(${SIGNAL_PERIODS.fast}/${SIGNAL_PERIODS.mid}/${SIGNAL_PERIODS.slow}) alignment. Not financial advice and not a guarantee for the next trade. Short-duration OTC binary options are highly speculative — the accuracy above describes the past, not the future.</div>
    `;
}

function getSignal() {
    loadTripleMAOverlay();
    const panel = document.getElementById('signalPanel');
    if (panel) panel.style.display = 'block';

    const result = computeSignal();
    renderSignalPanel(result);

    if (!result.ok) {
        toast(result.reason, 'info');
    } else {
        const toastType = result.direction === 'PUT' ? 'error' : (result.direction === 'CALL' ? 'success' : 'info');
        toast(`Signal: ${result.direction} — ${result.agree}/${result.agreeTotal} indicators agree`, toastType);
    }
}

function closeSignalPanel() {
    const panel = document.getElementById('signalPanel');
    if (panel) panel.style.display = 'none';
}

window.getSignal = getSignal;
window.closeSignalPanel = closeSignalPanel;
window.loadTripleMAOverlay = loadTripleMAOverlay;

// Auto-load the visual EMA overlay once the chart/manager is ready.
// (Only draws the 3 lines on the chart — does NOT open the signal panel;
// that only happens when the user clicks "Get Signal".)
(function initSignalOverlay() {
    const tryInit = () => {
        if (window.CM && AppState) { loadTripleMAOverlay(); }
        else setTimeout(tryInit, 300);
    };
    setTimeout(tryInit, 500);
})();

console.log('✅ signal.js loaded — Get Signal button + Triple-EMA overlay ready');
