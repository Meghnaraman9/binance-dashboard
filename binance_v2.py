#!/usr/bin/env python3
"""
Binance Futures Mobile Dashboard V2
------------------------------------
Signal-only. NO Binance API key. NO order execution.

Run in Termux:
    pkg update
    pkg install python
    pip install flask requests
    python binance_mobile_dashboard_v2.py

Then open on the phone:
    http://127.0.0.1:5000

Optional Android notifications:
    pkg install termux-api
    Install the Termux:API Android app.
    The dashboard will try to call termux-notification when a new LONG signal appears.

Strategy:
1D setup:
- Previous completed daily volume < previous 50-SMA volume.
- Latest completed daily candle is green.
- Latest volume >= previous volume * 1.06.
- Latest volume > latest 50-SMA volume.
- Save setup-day high.

1H entry:
- After setup day, at least one completed 1H candle has volume below
  its 50-SMA volume.
- Then live price reaches/breaks setup-day high.
- LONG alert.

The dashboard is a local web app. It does not place trades.
"""

from flask import Flask, jsonify, render_template_string
import requests
import time
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from threading import Lock

app = Flask(__name__)

BASE_URL = "https://fapi.binance.com"
STATE_FILE = "binance_dashboard_state.json"
VOLUME_SMA = 50
VOLUME_JUMP = 0.06
REQUEST_TIMEOUT = 12

session = requests.Session()
session.headers.update({"User-Agent": "Binance-Mobile-Dashboard-V2/1.0"})
state_lock = Lock()
state = {}
last_scan = 0
last_prices = {}
last_error = ""
alert_history = []


def now_ms():
    return int(time.time() * 1000)


def utc_text(ms=None):
    if ms is None:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def api_get(path, params=None):
    r = session.get(BASE_URL + path, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_symbols():
    data = api_get("/fapi/v1/exchangeInfo")
    return sorted(
        s["symbol"]
        for s in data["symbols"]
        if s.get("status") == "TRADING"
        and s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
    )


def get_klines(symbol, interval, limit=100):
    rows = api_get(
        "/fapi/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    return [r for r in rows if int(r[6]) <= now_ms()]


def get_prices():
    data = api_get("/fapi/v1/ticker/price")
    return {x["symbol"]: float(x["price"]) for x in data}


def sma(values, period=50):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def daily_setup(symbol):
    try:
        rows = get_klines(symbol, "1d", 100)
        if len(rows) < VOLUME_SMA + 2:
            return None

        prev = rows[-2]
        setup = rows[-1]

        vols = [float(r[5]) for r in rows]
        prev_sma = sum(vols[-VOLUME_SMA-1:-1]) / VOLUME_SMA
        setup_sma = sum(vols[-VOLUME_SMA:]) / VOLUME_SMA

        pv = float(prev[5])
        sv = float(setup[5])
        op = float(setup[1])
        close = float(setup[4])
        high = float(setup[2])

        if not (
            pv < prev_sma
            and close > op
            and sv >= pv * (1 + VOLUME_JUMP)
            and sv > setup_sma
        ):
            return None

        return {
            "symbol": symbol,
            "setup_time": int(setup[0]),
            "setup_close_time": int(setup[6]),
            "setup_high": high,
            "volume_jump_pct": (sv / pv - 1) * 100,
            "dry_up": False,
            "alerted": False,
            "alerted_at": None,
        }
    except Exception:
        return None


def check_1h_dryup(item):
    try:
        rows = get_klines(item["symbol"], "1h", 100)
        vols = [float(r[5]) for r in rows]

        for i, row in enumerate(rows):
            if int(row[0]) <= item["setup_close_time"]:
                continue
            if i < VOLUME_SMA:
                continue

            avg = sum(vols[i - VOLUME_SMA:i]) / VOLUME_SMA
            if float(row[5]) < avg:
                return True
        return False
    except Exception:
        return item.get("dry_up", False)


def save_state():
    with state_lock:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)


def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}


def android_notify(symbol, price, high):
    msg = (
        f"{symbol} LONG signal | Price {price:.8g} "
        f">= setup high {high:.8g}. Check Binance manually."
    )
    if shutil.which("termux-notification"):
        try:
            subprocess.run(
                [
                    "termux-notification",
                    "--title",
                    "🚨 Binance LONG Signal",
                    "--content",
                    msg,
                    "--priority",
                    "high",
                    "--sound",
                ],
                timeout=5,
                check=False,
            )
        except Exception:
            pass


def scan():
    global state, last_scan, last_error, last_prices, alert_history

    try:
        symbols = get_symbols()
        fresh = {}

        for symbol in symbols:
            item = daily_setup(symbol)
            if item:
                old = state.get(symbol)
                if old and old.get("setup_time") == item["setup_time"]:
                    item["dry_up"] = old.get("dry_up", False)
                    item["alerted"] = old.get("alerted", False)
                    item["alerted_at"] = old.get("alerted_at")
                fresh[symbol] = item

        state = fresh
        last_prices = get_prices()
        last_scan = now_ms()
        last_error = ""
        save_state()
    except Exception as e:
        last_error = str(e)


def update_signals():
    global last_prices, last_error, alert_history

    try:
        last_prices = get_prices()

        changed = False
        for symbol, item in state.items():
            if not item.get("dry_up"):
                if check_1h_dryup(item):
                    item["dry_up"] = True
                    changed = True

            price = last_prices.get(symbol)
            if price is None:
                continue

            if (
                item.get("dry_up")
                and price >= item["setup_high"]
                and not item.get("alerted")
            ):
                item["alerted"] = True
                item["alerted_at"] = now_ms()
                alert_history.insert(
                    0,
                    {
                        "symbol": symbol,
                        "price": price,
                        "high": item["setup_high"],
                        "time": utc_text(),
                    },
                )
                alert_history = alert_history[:20]
                android_notify(symbol, price, item["setup_high"])
                changed = True

        if changed:
            save_state()
        last_error = ""
    except Exception as e:
        last_error = str(e)


def background_refresh():
    # Flask's development server is enough for a personal phone dashboard.
    global last_scan

    while True:
        try:
            # Refresh daily watchlist roughly once every 30 minutes.
            if last_scan == 0 or time.time() * 1000 - last_scan > 30 * 60 * 1000:
                scan()
            else:
                update_signals()
        except Exception:
            pass
        time.sleep(60)


HTML = r"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Binance Volume Watchlist</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif;
  background:#0b0f14; color:#eef2f6;
}
.header {
  position:sticky; top:0; z-index:10; padding:14px;
  background:#111821; border-bottom:1px solid #27313d;
}
h1 { font-size:20px; margin:0 0 5px; }
.sub { color:#9ca8b5; font-size:12px; }
.wrap { padding:12px; max-width:900px; margin:auto; }
.buttons { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:12px; }
button {
  border:0; border-radius:10px; padding:12px; font-weight:700;
  background:#263240; color:white;
}
button.primary { background:#1769aa; }
button.danger { background:#8e2930; }
.card {
  background:#131a22; border:1px solid #27313d; border-radius:14px;
  padding:13px; margin-bottom:10px;
}
.row { display:flex; justify-content:space-between; gap:10px; }
.symbol { font-size:18px; font-weight:800; }
.price { font-size:18px; font-weight:800; }
.meta { color:#a9b4c0; font-size:12px; margin-top:7px; }
.badge {
  display:inline-block; margin-top:9px; padding:6px 9px; border-radius:999px;
  font-size:12px; font-weight:800;
}
.green { background:#164d35; color:#6ff0ae; }
.red { background:#5b2026; color:#ff8f99; }
.yellow { background:#55470e; color:#ffe36e; }
.blue { background:#173f5e; color:#76c7ff; }
.progress {
  height:6px; background:#252f39; border-radius:99px; margin-top:10px; overflow:hidden;
}
.bar { height:100%; background:#4da3ff; }
.alert {
  border:1px solid #8b3038; background:#35171b;
}
.small { font-size:11px; color:#8e9aa7; }
.empty { text-align:center; padding:35px 10px; color:#9ca8b5; }
</style>
</head>
<body>
<div class="header">
  <h1>📊 Binance Futures Watchlist</h1>
  <div class="sub">Manual execution • Signal-only • No orders placed</div>
</div>

<div class="wrap">
  <div class="buttons">
    <button class="primary" onclick="scan()">🔎 Scan 1D Setups</button>
    <button onclick="refreshNow()">🔄 Refresh Prices</button>
  </div>

  <div id="info" class="card">Loading...</div>
  <div id="alerts"></div>
  <div id="list"></div>
</div>

<script>
function esc(s) {
  return String(s).replace(/[&<>"']/g, x => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'
  }[x]));
}

function money(x) {
  if (x === null || x === undefined) return '-';
  return Number(x).toLocaleString(undefined,{maximumSignificantDigits:10});
}

async function getData() {
  const r = await fetch('/api/data');
  return await r.json();
}

function render(d) {
  document.getElementById('info').innerHTML =
    `<div class="row"><b>${d.count} watchlist coins</b><span>${esc(d.updated)}</span></div>` +
    `<div class="meta">1D: dry volume → green candle +6% volume → above 50-SMA</div>` +
    `<div class="meta">1H: dry-up → setup high breakout</div>` +
    (d.error ? `<div class="badge red">API ERROR: ${esc(d.error)}</div>` : '');

  const alerts = d.alerts || [];
  document.getElementById('alerts').innerHTML = alerts.length ?
    `<div class="card alert"><b>🚨 Recent LONG Alerts</b><div class="meta">${
      alerts.slice(0,5).map(a =>
        `<div style="margin-top:8px"><b>${esc(a.symbol)}</b> @ ${money(a.price)}
        <br><span class="small">${esc(a.time)}</span></div>`
      ).join('')
    }</div></div>` : '';

  if (!d.items.length) {
    document.getElementById('list').innerHTML =
      `<div class="card empty">No valid 1D setups right now.<br>Tap “Scan 1D Setups”.</div>`;
    return;
  }

  document.getElementById('list').innerHTML = d.items.map(x => {
    let status, cls;
    if (x.alerted) { status='🚨 LONG ALERT — CHECK BINANCE'; cls='red'; }
    else if (x.dry_up && x.price >= x.high) { status='🚨 BREAKOUT'; cls='red'; }
    else if (x.dry_up) { status='🟢 1H DRY-UP — WAIT FOR HIGH'; cls='green'; }
    else if (x.price >= x.high) { status='🟡 HIGH REACHED — WAIT FOR DRY-UP'; cls='yellow'; }
    else { status='🔵 WAITING FOR 1H DRY-UP'; cls='blue'; }

    let distance = x.high > 0 ? Math.min(100, Math.max(0, x.price/x.high*100)) : 0;

    return `<div class="card ${x.alerted ? 'alert':''}">
      <div class="row">
        <div class="symbol">${esc(x.symbol)}</div>
        <div class="price">${money(x.price)}</div>
      </div>
      <div class="meta">Setup high: <b>${money(x.high)}</b> &nbsp; | &nbsp;
        Volume jump: <b>${Number(x.vol_jump).toFixed(1)}%</b></div>
      <div class="progress"><div class="bar" style="width:${distance}%"></div></div>
      <div class="badge ${cls}">${status}</div>
      <div class="meta">Setup: ${esc(x.setup_time)}</div>
    </div>`;
  }).join('');
}

async function refreshNow() {
  try { render(await getData()); } catch(e) {}
}

async function scan() {
  const b = document.querySelector('.primary');
  b.textContent = '⏳ Scanning...';
  b.disabled = true;
  try {
    await fetch('/api/scan', {method:'POST'});
    await refreshNow();
  } finally {
    b.textContent = '🔎 Scan 1D Setups';
    b.disabled = false;
  }
}

refreshNow();
setInterval(refreshNow, 15000);
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/api/data")
def api_data():
    items = []
    for symbol, item in sorted(state.items()):
        price = last_prices.get(symbol)
        items.append(
            {
                "symbol": symbol,
                "price": price,
                "high": item["setup_high"],
                "vol_jump": item["volume_jump_pct"],
                "dry_up": item.get("dry_up", False),
                "alerted": item.get("alerted", False),
                "setup_time": utc_text(item["setup_time"]),
            }
        )

    return jsonify(
        {
            "items": items,
            "count": len(items),
            "updated": utc_text(),
            "error": last_error,
            "alerts": alert_history,
        }
    )


@app.route("/api/scan", methods=["POST"])
def api_scan():
    scan()
    return jsonify({"ok": True, "count": len(state), "error": last_error})


import threading

# Runs on import — so this fires whether you launch with
# `python binance_v2.py` (dev) OR `gunicorn binance_v2:app` (prod).
# IMPORTANT: only ever run this with ONE gunicorn worker. Each worker
# is a separate process with its OWN copy of `state`/`last_prices` and
# its OWN background thread — with 2+ workers you get duplicate API
# calls, duplicate Android-notify attempts, and a state file being
# written by two processes at once (data corruption / lost writes).
load_state()
threading.Thread(target=background_refresh, daemon=True).start()

if __name__ == "__main__":
    print("=" * 65)
    print(" Binance Futures Mobile Dashboard V2")
    print("=" * 65)
    print(" Open on this phone: http://127.0.0.1:5000")
    print(" Signal-only. No API key. No order execution.")
    print("=" * 65)

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
        threaded=True,
    )