/**
 * 📤 telegram.js — "Test" button: send chart screenshot to Telegram
 * =============================================================================
 * Flow:
 *   1. LightweightCharts' own chart.takeScreenshot() grabs a <canvas> of the
 *      current chart (this is the "lightweight" screenshot — no external
 *      screenshot library needed).
 *   2. The canvas is converted to a base64 PNG in the browser.
 *   3. The base64 string is handed to the Python backend via eel.
 *      engine.py does the actual Telegram Bot API call — the bot token
 *      never lives in this file or in the browser, only server-side.
 *
 * Dependencies: chart.js (window.chart), app.js (toast, toolbarMap)
 */

async function sendChartToTelegram() {
    const btn = document.getElementById('telegramTestBtn');

    if (!window.chart) {
        toast('⚠️ Chart not ready yet', 'error');
        return;
    }
    if (typeof eel === 'undefined' || typeof eel.send_chart_to_telegram !== 'function') {
        toast('⚠️ Telegram bridge not available (restart the app)', 'error');
        return;
    }

    if (btn) btn.classList.add('sending');

    try {
        // 1) Screenshot the chart canvas
        const canvas = window.chart.takeScreenshot();
        const dataUrl = canvas.toDataURL('image/png');

        toast('📤 Sending chart to Telegram…', 'info');

        // 2) Hand it to Python (engine.py) — token stays server-side
        const result = await eel.send_chart_to_telegram(dataUrl)();

        if (result && result.success) {
            toast('✅ Chart sent to Telegram', 'success');
        } else {
            toast(`❌ Telegram send failed: ${(result && result.error) || 'unknown error'}`, 'error');
        }
    } catch (e) {
        console.error('❌ sendChartToTelegram failed:', e);
        toast(`❌ Screenshot failed: ${e.message}`, 'error');
    } finally {
        if (btn) btn.classList.remove('sending');
    }
}

window.sendChartToTelegram = sendChartToTelegram;
console.log('✅ telegram.js loaded — Test button wired to Telegram bot');
