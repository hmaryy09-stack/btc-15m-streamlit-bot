import os
import time
import math
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_SERIES = "KXBTC15M"
PAIR = "XBTUSD"
REQUEST_TIMEOUT = 15

_cache = {"data": None, "updated": 0}
_cache_lock = threading.Lock()
_signal_state = {"bucket": None, "signal": None}
_signal_lock = threading.Lock()


def safe_float(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def get_candles(interval=15):
    try:
        response = requests.get(
            KRAKEN_URL,
            params={"pair": PAIR, "interval": interval},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))

        result = payload.get("result", {})
        pair_key = next((key for key in result if key != "last"), None)

        if not pair_key:
            raise RuntimeError("Kraken no devolvió velas BTC.")

        candles = []

        for row in result[pair_key]:
            candles.append({
                "time": int(row[0]),
                "open": safe_float(row[1]),
                "high": safe_float(row[2]),
                "low": safe_float(row[3]),
                "close": safe_float(row[4]),
                "volume": safe_float(row[6]),
            })

        return candles

    except Exception as exc:
        print("ERROR KRAKEN:", repr(exc))
        return []


def ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period

    output = [None] * (period - 1) + [current]

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )
        output.append(current)

    return output


def rsi(values, period=14):
    if len(values) <= period:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0

    return round(
        100 - 100 / (1 + avg_gain / avg_loss),
        2
    )


def macd(values):
    if len(values) < 35:
        return None

    fast = ema(values, 12)
    slow = ema(values, 26)

    series = [
        a - b
        for a, b in zip(fast, slow)
        if a is not None and b is not None
    ]

    if len(series) < 9:
        return None

    signal = ema(series, 9)

    return round(series[-1] - signal[-1], 4)


def candle_shape(candle):
    body = abs(candle["close"] - candle["open"])
    full_range = candle["high"] - candle["low"]

    upper = (
        candle["high"]
        - max(candle["open"], candle["close"])
    )

    lower = (
        min(candle["open"], candle["close"])
        - candle["low"]
    )

    ratio = body / full_range if full_range > 0 else 1

    return body, full_range, upper, lower, ratio


# ==========================================================
# DOJI INTELLIGENCE
# ==========================================================

def analyze_doji(candles):

    if len(candles) < 3:
        return {
            "detected": False,
            "direction": "NEUTRAL",
            "status": "ESPERANDO DATOS",
            "description": "Datos insuficientes"
        }

    # Analizar las dos últimas velas cerradas.
    doji = candles[-3]
    confirmation = candles[-2]

    _, candle_range, upper, lower, body_ratio = (
        candle_shape(doji)
    )

    if candle_range <= 0 or body_ratio > 0.10:
        return {
            "detected": False,
            "direction": "NEUTRAL",
            "status": "SIN DOJI",
            "description": "No se detecta DOJI en la última vela cerrada"
        }

    # CONFIRMACION ALCISTA
    if confirmation["close"] > doji["high"]:

        direction = "ALCISTA"
        status = "DOJI ALCISTA CONFIRMADO"

        description = (
            "La vela siguiente rompió el máximo del DOJI."
        )

    # CONFIRMACION BAJISTA
    elif confirmation["close"] < doji["low"]:

        direction = "BAJISTA"
        status = "DOJI BAJISTA CONFIRMADO"

        description = (
            "La vela siguiente rompió el mínimo del DOJI."
        )

    else:

        direction = "NEUTRAL"
        status = "DOJI: ESPERAR"

        description = (
            "Todavía no hay ruptura del máximo ni del mínimo del DOJI."
        )

    return {
        "detected": True,
        "direction": direction,
        "status": status,
        "description": description,
        "high": round(doji["high"], 2),
        "low": round(doji["low"], 2),
        "upper_wick": round(upper, 2),
        "lower_wick": round(lower, 2),
    }


# ==========================================================
# PATRONES DE VELAS
# ==========================================================

def candle_pattern(candles):

    if len(candles) < 2:
        return "INDEFINIDO", "NEUTRAL"

    candle = candles[-2]

    body, full_range, upper, lower, ratio = (
        candle_shape(candle)
    )

    if full_range <= 0:
        return "INDEFINIDO", "NEUTRAL"

    if ratio <= 0.10:
        return "DOJI", "NEUTRAL"

    if (
        lower >= body * 2
        and upper <= body * 0.7
        and candle["close"] > candle["open"]
    ):
        return "MARTILLO", "ALCISTA"

    if (
        upper >= body * 2
        and lower <= body * 0.7
        and candle["close"] < candle["open"]
    ):
        return "ESTRELLA FUGAZ", "BAJISTA"

    if candle["close"] > candle["open"]:
        return "VELA ALCISTA", "ALCISTA"

    if candle["close"] < candle["open"]:
        return "VELA BAJISTA", "BAJISTA"

    return "INDEFINIDO", "NEUTRAL"


# ==========================================================
# TENDENCIA MULTI-TIEMPO
# ==========================================================

def trend_for(interval):

    candles = get_candles(interval)

    closes = (
        [c["close"] for c in candles[:-1]]
        if len(candles) > 1 else []
    )

    e5 = ema(closes, 5)
    e13 = ema(closes, 13)
    e21 = ema(closes, 21)

    if not e5 or not e13 or not e21:
        return "SIN DATOS"

    if e5[-1] > e13[-1] > e21[-1]:
        return "SUBE"

    if e5[-1] < e13[-1] < e21[-1]:
        return "BAJA"

    return "NEUTRAL"


# ==========================================================
# MOTOR DE SEÑALES
# ==========================================================

def make_signal(candles, indicators, doji_info):

    if len(candles) < 35:
        return {
            "direction": "ESPERAR",
            "probability": 50,
            "reason": "Esperando suficientes velas."
        }

    score = 0

    e5 = indicators["ema5"]
    e13 = indicators["ema13"]
    e21 = indicators["ema21"]

    if e5 and e13:
        score += 1 if e5 > e13 else -1 if e5 < e13 else 0

    if e13 and e21:
        score += 1 if e13 > e21 else -1 if e13 < e21 else 0

    value_rsi = indicators["rsi"]

    if value_rsi is not None:
        score += (
            1 if value_rsi >= 55
            else -1 if value_rsi <= 45
            else 0
        )

    value_macd = indicators["macd"]

    if value_macd is not None:
        score += 1 if value_macd > 0 else -1 if value_macd < 0 else 0

    closed = [c["close"] for c in candles[:-1]]

    if len(closed) > 5:
        change = closed[-1] - closed[-6]
        score += 1 if change > 0 else -1 if change < 0 else 0

    if (
        doji_info["detected"]
        and doji_info["direction"] == "NEUTRAL"
    ):
        return {
            "direction": "ESPERAR",
            "probability": 50,
            "reason": "DOJI detectado: esperar ruptura de su máximo o mínimo."
        }

    if (
        doji_info["detected"]
        and doji_info["direction"] == "ALCISTA"
    ):
        score += 1

    elif (
        doji_info["detected"]
        and doji_info["direction"] == "BAJISTA"
    ):
        score -= 1

    if score >= 3:
        return {
            "direction": "SUBE",
            "probability": min(80, 55 + score * 4),
            "reason": "Indicadores con inclinación alcista."
        }

    if score <= -3:
        return {
            "direction": "BAJA",
            "probability": min(80, 55 + abs(score) * 4),
            "reason": "Indicadores con inclinación bajista."
        }

    return {
        "direction": "ESPERAR",
        "probability": 50,
        "reason": "Señales mixtas; esperar confirmación."
    }


# ==========================================================
# BLOQUEO DE SEÑAL POR VELA DE 15 MINUTOS
# ==========================================================

def fixed_signal(candles, indicators, doji_info):

    bucket = int(time.time()) // 900

    with _signal_lock:

        if _signal_state["bucket"] != bucket:

            _signal_state["bucket"] = bucket

            _signal_state["signal"] = make_signal(
                candles,
                indicators,
                doji_info
            )

        return _signal_state["signal"]


# ==========================================================
# CONSTRUIR DASHBOARD
# ==========================================================

def build_dashboard():

    candles = get_candles(15)

    if len(candles) < 35:
        return {
            "error": "Kraken no devolvió suficientes velas. Intenta actualizar en unos segundos."
        }

    closed = candles[:-1]

    closes = [c["close"] for c in closed]

    e5 = ema(closes, 5)
    e13 = ema(closes, 13)
    e21 = ema(closes, 21)

    value_rsi = rsi(closes)
    value_macd = macd(closes)

    doji_info = analyze_doji(candles)

    pattern_name, pattern_direction = candle_pattern(candles)

    indicator_data = {

        "ema5": round(e5[-1], 2) if e5 else None,

        "ema13": round(e13[-1], 2) if e13 else None,

        "ema21": round(e21[-1], 2) if e21 else None,

        "rsi": value_rsi,

        "macd": value_macd,

        "momentum": round(
            (closes[-1] - closes[-6]) / closes[-6] * 100,
            5
        ) if len(closes) > 6 and closes[-6] else 0,

        "volume": "ALTO" if (
            len(closed) > 21
            and closed[-1]["volume"] >
            sum(c["volume"] for c in closed[-21:-1]) / 20 * 1.8
        ) else "BAJO" if (
            len(closed) > 21
            and closed[-1]["volume"] <
            sum(c["volume"] for c in closed[-21:-1]) / 20 * 0.6
        ) else "NORMAL",
    }

    signal = fixed_signal(
        candles,
        indicator_data,
        doji_info
    )

    strength = 0.0

    if e5 and e21 and e21[-1]:
        strength = round(
            min(abs(e5[-1] - e21[-1]) / e21[-1] * 2000, 100),
            1
        )

    recent = closed[-60:]

    lows = sorted(c["low"] for c in recent)
    highs = sorted(c["high"] for c in recent)

    ranges = [
        c["high"] - c["low"]
        for c in closed[-15:]
    ]

    avg_range = (
        sum(ranges) / len(ranges)
        if ranges else 0
    )

    direction = (
        1 if indicator_data["momentum"] >= 0 else -1
    )

    current_price = candles[-1]["close"]

    chart_candles = candles[-100:]

    def line(series):

        return (
            [None] * max(
                0,
                len(chart_candles) - len(series[-len(chart_candles):])
            )
            + [
                round(v, 2) if v is not None else None
                for v in series[-len(chart_candles):]
            ]
        )

    now = int(time.time())
    remaining = 900 - (now % 900)

    return {

        "price": round(current_price, 2),

        "signal": signal,

        "strength": strength,

        "indicators": indicator_data,

        "doji": doji_info,

        "pattern": {
            "name": pattern_name,
            "direction": pattern_direction
        },

        "levels": {

            "support1": round(lows[0], 2),

            "support2": round(
                lows[min(3, len(lows)-1)], 2
            ),

            "resistance1": round(highs[-1], 2),

            "resistance2": round(
                highs[max(0, len(highs)-4)], 2
            ),
        },

        "projections": {

            "conservative": round(
                current_price + direction * avg_range * 0.5, 2
            ),

            "average": round(
                current_price + direction * avg_range, 2
            ),

            "optimistic": round(
                current_price + direction * avg_range * 1.5, 2
            ),
        },

        "timeframes": {
            "1M": trend_for(1),
            "5M": trend_for(5),
            "15M": trend_for(15)
        },

        "ohlc": {
            key: round(candles[-1][key], 2)
            for key in ("open", "high", "low", "close")
        },

        "candles": chart_candles,

        "ema5_series": line(
            ema([c["close"] for c in candles], 5)
        ),

        "ema13_series": line(
            ema([c["close"] for c in candles], 13)
        ),

        "ema21_series": line(
            ema([c["close"] for c in candles], 21)
        ),

        "remaining": (
            f"{remaining // 60:02d}:{remaining % 60:02d}"
        ),

        "updated": datetime.now(
            timezone.utc
        ).strftime("%H:%M:%S UTC"),
    }


# ==========================================================
# KALSHI
# ==========================================================

def get_kalshi_markets():

    try:

        response = requests.get(
            f"{KALSHI_BASE_URL}/markets",

            params={
                "series_ticker": KALSHI_SERIES,
                "status": "open",
                "limit": 100
            },

            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

        markets = response.json().get("markets", [])

        results = []

        for market in markets:

            def cents(field):

                dollars = market.get(field)

                if dollars in (None, ""):
                    return None

                return round(
                    safe_float(dollars) * 100,
                    2
                )

            results.append({

                "ticker": market.get("ticker"),

                "title": market.get("title"),

                "status": market.get("status"),

                "yes_bid": cents("yes_bid_dollars"),

                "yes_ask": cents("yes_ask_dollars"),

                "no_bid": cents("no_bid_dollars"),

                "no_ask": cents("no_ask_dollars"),
            })

        return {
            "connected": True,
            "count": len(results),
            "markets": results,
            "error": None
        }

    except Exception as exc:

        print("ERROR KALSHI:", repr(exc))

        return {
            "connected": False,
            "count": 0,
            "markets": [],
            "error": str(exc)
        }


# ==========================================================
# HTML DASHBOARD
# ==========================================================

HTML = r"""
<!doctype html>
<html lang="es">
<head>

<meta charset="utf-8">

<meta name="viewport" content="width=device-width,initial-scale=1">

<title>BTC 15M Candle Calculator PRO AI</title>

<script src="https://unpkg.com/lightweight-charts@4.2.2/dist/lightweight-charts.standalone.production.js"></script>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    padding: 10px;
    background: #050a12;
    color: #f4f7fb;
    font-family: Arial, sans-serif;
}

.container {
    max-width: 1450px;
    margin: auto;
}

.panel {
    background: #101b2c;
    border: 1px solid #29415d;
    border-radius: 13px;
    padding: 15px;
    margin-bottom: 11px;
}

.header {
    font-size: 22px;
    font-weight: bold;
    color: #45b5ff;
}

.sub {
    color: #00e6a0;
    font-size: 13px;
    margin-top: 8px;
}

.grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 10px;
}

.metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 9px;
}

.metric {
    background: #17253a;
    border: 1px solid #263b55;
    border-radius: 10px;
    padding: 13px;
    min-width: 0;
}

.label {
    color: #91a9c7;
    font-size: 13px;
}

.metric strong {
    display: block;
    font-size: 18px;
    margin-top: 8px;
    overflow-wrap: anywhere;
}

.title {
    font-size: 17px;
    font-weight: bold;
    margin-bottom: 12px;
}

.signal {
    text-align: center;
    font-size: 34px;
    font-weight: bold;
    margin: 14px;
}

.up, .bull {
    color: #00efa0;
}

.down, .bear {
    color: #ff526b;
}

.wait, .neutral {
    color: #ffd34e;
}

.row {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 0;
    border-bottom: 1px solid #26364c;
    overflow-wrap: anywhere;
}

#chart {
    height: 330px;
    width: 100%;
}

.statusbox {
    border: 1px solid currentColor;
    border-radius: 9px;
    padding: 12px;
    margin-top: 10px;
    font-weight: bold;
}

.conclusion {
    color: #00efa0;
    border-color: #00efa0;
    box-shadow: 0 0 10px #00a96b;
}

.footer {
    text-align: center;
    color: #8499b6;
    font-size: 12px;
    padding: 15px;
}

@media(max-width: 800px) {

    .grid {
        grid-template-columns: 1fr;
    }

    .metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    #chart {
        height: 290px;
    }
}

</style>
</head>

<body>

<main class="container">

<section class="panel">

<div class="header">
₿ BTC 15M - CANDLE CALCULATOR PRO
<span style="color:#d59aff">AI</span>
</div>

<div class="sub" id="connection">
● CONECTANDO A KRAKEN...
</div>

</section>


<section class="grid">

<div class="panel">

<div class="title">SEÑAL ACTUAL (15M)</div>

<div class="label">Dirección del mercado</div>

<div id="direction" class="signal wait">
ESPERAR
</div>

<div id="probability" class="signal">
50%
</div>

<div class="row">
<span>Precio actual</span>
<b id="price">--</b>
</div>

<div class="row">
<span>Fuerza</span>
<b id="strength">--</b>
</div>

<div class="row">
<span>Tiempo restante</span>
<b id="remaining">--</b>
</div>

<p id="reason">Analizando...</p>

</div>


<div class="panel">

<div class="title">GRÁFICA BTC/USD - 15M</div>

<div id="chart"></div>

</div>


<div class="panel">

<div class="title">ANÁLISIS DE DOJI</div>

<div id="doji-status" class="wait">
ANALIZANDO
</div>

<div id="doji-box" class="statusbox neutral">
Esperando patrón...
</div>

<div class="title" style="margin-top:18px">
PATRÓN ACTUAL
</div>

<b id="pattern">--</b>

</div>

</section>


<section class="panel">

<div class="title">INDICADORES</div>

<div class="metrics">

<div class="metric">
<span class="label">EMA 5</span>
<strong id="ema5">--</strong>
</div>

<div class="metric">
<span class="label">EMA 13</span>
<strong id="ema13">--</strong>
</div>

<div class="metric">
<span class="label">EMA 21</span>
<strong id="ema21">--</strong>
</div>

<div class="metric">
<span class="label">RSI (14)</span>
<strong id="rsi">--</strong>
</div>

<div class="metric">
<span class="label">MACD</span>
<strong id="macd">--</strong>
</div>

<div class="metric">
<span class="label">MOMENTUM</span>
<strong id="momentum">--</strong>
</div>

<div class="metric">
<span class="label">VOLUMEN</span>
<strong id="volume">--</strong>
</div>

<div class="metric">
<span class="label">FUERZA</span>
<strong id="strength2">--</strong>
</div>

</div>
</section>


<section class="panel">

<div class="title">PROYECCIÓN DE CIERRE (15M)</div>

<div class="metrics">

<div class="metric">
<span class="label">CONSERVADOR</span>
<strong id="conservative">--</strong>
</div>

<div class="metric">
<span class="label">PROMEDIO</span>
<strong id="average">--</strong>
</div>

<div class="metric">
<span class="label">OPTIMISTA</span>
<strong id="optimistic">--</strong>
</div>

<div class="metric">
<span class="label">CIERRE ESTIMADO</span>
<strong id="estimated">--</strong>
</div>

</div>
</section>


<section class="panel">

<div class="title">VELAS OHLC - 15M</div>

<div class="metrics">

<div class="metric">
<span class="label">OPEN</span>
<strong id="open">--</strong>
</div>

<div class="metric">
<span class="label">HIGH</span>
<strong id="high">--</strong>
</div>

<div class="metric">
<span class="label">LOW</span>
<strong id="low">--</strong>
</div>

<div class="metric">
<span class="label">CLOSE</span>
<strong id="close">--</strong>
</div>

</div>
</section>


<section class="panel">

<div class="title">ANÁLISIS MULTI-TIEMPO</div>

<div class="metrics">

<div class="metric">
<span class="label">1 MINUTO</span>
<strong id="tf1">--</strong>
</div>

<div class="metric">
<span class="label">5 MINUTOS</span>
<strong id="tf5">--</strong>
</div>

<div class="metric">
<span class="label">15 MINUTOS</span>
<strong id="tf15">--</strong>
</div>

</div>
</section>


<section class="panel">

<div class="title">NIVELES IMPORTANTES</div>

<div class="row">
<span>Resistencia 2</span>
<b id="resistance2">--</b>
</div>

<div class="row">
<span>Resistencia 1</span>
<b id="resistance1">--</b>
</div>

<div class="row">
<span>Soporte 1</span>
<b id="support1">--</b>
</div>

<div class="row">
<span>Soporte 2</span>
<b id="support2">--</b>
</div>

</section>


<section class="panel conclusion">

<div class="title">CONCLUSIÓN DEL MERCADO</div>

<b id="conclusion">Analizando...</b>

</section>


<section class="panel">

<div class="title">KALSHI BTC 15M — EN VIVO</div>

<div id="kalshi-connection" class="sub">
● CONECTANDO A KALSHI...
</div>

<div class="metrics" style="margin-top:12px">

<div class="metric">
<span class="label">YES ASK</span>
<strong id="yesask">--</strong>
</div>

<div class="metric">
<span class="label">NO ASK</span>
<strong id="noask">--</strong>
</div>

<div class="metric">
<span class="label">YES BID</span>
<strong id="yesbid">--</strong>
</div>

<div class="metric">
<span class="label">NO BID</span>
<strong id="nobid">--</strong>
</div>

</div>

<div class="row">
<span>Mercado</span>
<b id="ticker">--</b>
</div>

<div class="row">
<span>Estado</span>
<b id="marketstatus">--</b>
</div>

<div class="row">
<span>Actualización</span>
<b id="kalshiupdated">--</b>
</div>

</section>


<section class="panel" style="text-align:center;font-size:20px;font-weight:bold">

SEÑAL FINAL (15M):

<span id="finaldirection" class="wait">ESPERAR</span>

—

<span id="finalprob">50%</span>

</section>


<div class="footer">

DATOS DE KRAKEN Y CONSULTA DE KALSHI

<br>

SOLO SEÑALES • SIN COMPRAS AUTOMÁTICAS

<br>

Las estimaciones no garantizan resultados futuros.

<br>

<span id="updated"></span>

</div>

</main>


<script>

const el = id => document.getElementById(id);

const money = value => {

    if (value === null || value === undefined) {
        return "--";
    }

    return "$" + Number(value).toLocaleString(
        "en-US",
        {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2
        }
    );
};


const set = (id, value) => {

    if (el(id)) {
        el(id).textContent =
            value === null || value === undefined
            ? "--"
            : value;
    }

};


const cls = direction => {

    if (
        direction === "SUBE"
        || direction === "ALCISTA"
    ) return "up";

    if (
        direction === "BAJA"
        || direction === "BAJISTA"
    ) return "down";

    return "wait";
};


// ==========================================================
// GRAFICA
// ==========================================================

const chart = LightweightCharts.createChart(
    el("chart"),
    {
        width: el("chart").clientWidth,
        height: 330,

        layout: {
            background: {
                color: "#101b2c"
            },
            textColor: "#a9bfd9"
        },

        grid: {
            vertLines: {
                color: "#26384f"
            },
            horzLines: {
                color: "#26384f"
            }
        },

        timeScale: {
            timeVisible: true
        }
    }
);


const candlesSeries = chart.addCandlestickSeries({

    upColor: "#00e69a",
    downColor: "#ff526b",

    borderUpColor: "#00e69a",
    borderDownColor: "#ff526b",

    wickUpColor: "#00e69a",
    wickDownColor: "#ff526b"

});


const l5 = chart.addLineSeries({
    color: "#00e69a",
    lineWidth: 1
});

const l13 = chart.addLineSeries({
    color: "#ffc857",
    lineWidth: 1
});

const l21 = chart.addLineSeries({
    color: "#ff526b",
    lineWidth: 1
});


window.addEventListener("resize", () => {

    chart.applyOptions({
        width: el("chart").clientWidth
    });

});


function lineData(candles, values) {

    return candles.map((candle, index) => {

        const value = values[index];

        if (value === null || value === undefined) {
            return null;
        }

        return {
            time: candle.time,
            value: value
        };

    }).filter(Boolean);

}


// ==========================================================
// ACTUALIZAR DASHBOARD
// ==========================================================

async function refresh() {

    try {

        const response = await fetch(
            "/api/data",
            {
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw Error("HTTP " + response.status);
        }

        const data = await response.json();

        if (data.error) {
            throw Error(data.error);
        }

        const signal = data.signal;
        const indicators = data.indicators;


        set("connection", "● CONECTADO A KRAKEN");

        el("connection").style.color = "#00e6a0";


        // SEÑAL

        set("direction", signal.direction);

        el("direction").className =
            "signal " + cls(signal.direction);

        set(
            "probability",
            signal.probability + "%"
        );


        set("price", money(data.price));

        set("strength", data.strength + "%");

        set("strength2", data.strength + "%");

        set("remaining", data.remaining);

        set("reason", signal.reason);


        // INDICADORES

        ["ema5", "ema13", "ema21"].forEach(key => {

            set(key, money(indicators[key]));

        });

        set("rsi", indicators.rsi);

        set("macd", indicators.macd);

        set("momentum", indicators.momentum + "%");

        set("volume", indicators.volume);


        // DOJI

        set("doji-status", data.doji.status);

        el("doji-status").className =
            cls(data.doji.direction);

        set(
            "doji-box",
            data.doji.description
        );

        el("doji-box").className =
            "statusbox " +
            (
                data.doji.direction === "ALCISTA"
                ? "bull"
                : data.doji.direction === "BAJISTA"
                ? "bear"
                : "neutral"
            );


        // PATRÓN

        set("pattern", data.pattern.name);


        // PROYECCIONES

        ["conservative", "average", "optimistic"].forEach(key => {

            set(
                key,
                money(data.projections[key])
            );

        });

        set(
            "estimated",
            money(data.projections.average)
        );


        // OHLC

        ["open", "high", "low", "close"].forEach(key => {

            set(key, money(data.ohlc[key]));

        });


        // TEMPORALIDADES

        set("tf1", data.timeframes["1M"]);

        set("tf5", data.timeframes["5M"]);

        set("tf15", data.timeframes["15M"]);


        // SOPORTES Y RESISTENCIAS

        [
            "support1",
            "support2",
            "resistance1",
            "resistance2"
        ].forEach(key => {

            set(
                key,
                money(data.levels[key])
            );

        });


        // CONCLUSIÓN

        set("conclusion", signal.reason);


        // SEÑAL FINAL

        set("finaldirection", signal.direction);

        el("finaldirection").className =
            cls(signal.direction);

        set(
            "finalprob",
            signal.probability + "%"
        );


        // GRÁFICA DE VELAS

        candlesSeries.setData(
            data.candles.map(candle => ({

                time: candle.time,
                open: candle.open,
                high: candle.high,
                low: candle.low,
                close: candle.close

            }))
        );


        l5.setData(
            lineData(data.candles, data.ema5_series)
        );

        l13.setData(
            lineData(data.candles, data.ema13_series)
        );

        l21.setData(
            lineData(data.candles, data.ema21_series)
        );


        chart.timeScale().fitContent();


        set(
            "updated",
            "Última actualización: " + data.updated
        );


    } catch (error) {

        console.error(error);

        set(
            "connection",
            "● ERROR KRAKEN: " + error.message
        );

        el("connection").style.color = "#ff526b";

    }

}


// ==========================================================
// ACTUALIZAR KALSHI
// ==========================================================

async function refreshKalshi() {

    try {

        const response = await fetch(
            "/api/kalshi",
            {
                cache: "no-store"
            }
        );

        if (!response.ok) {
            throw Error("HTTP " + response.status);
        }

        const data = await response.json();

        if (
            !data.connected
            || !data.markets.length
        ) {

            set(
                "kalshi-connection",
                "● KALSHI SIN MERCADOS ABIERTOS"
            );

            return;
        }


        const market = data.markets[0];

        set(
            "kalshi-connection",
            "● CONECTADO A KALSHI"
        );


        set(
            "yesask",
            market.yes_ask === null
            ? "--"
            : market.yes_ask + "%"
        );

        set(
            "noask",
            market.no_ask === null
            ? "--"
            : market.no_ask + "%"
        );

        set(
            "yesbid",
            market.yes_bid === null
            ? "--"
            : market.yes_bid + "%"
        );

        set(
            "nobid",
            market.no_bid === null
            ? "--"
            : market.no_bid + "%"
        );


        set("ticker", market.ticker);

        set("marketstatus", market.status);

        set(
            "kalshiupdated",
            new Date().toLocaleTimeString()
        );


    } catch (error) {

        set(
            "kalshi-connection",
            "● ERROR KALSHI: " + error.message
        );

    }

}


refresh();

refreshKalshi();

setInterval(refresh, 15000);

setInterval(refreshKalshi, 15000);

</script>

</body>
</html>
"""


# ==========================================================
# RUTAS FLASK
# ==========================================================

@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/api/data")
def api_data():

    now = time.time()

    with _cache_lock:

        if (
            _cache["data"] is not None
            and now - _cache["updated"] < 8
        ):
            return jsonify(_cache["data"])

    data = build_dashboard()

    if "error" not in data:

        with _cache_lock:

            _cache["data"] = data

            _cache["updated"] = now

    return jsonify(data)


@app.route("/api/kalshi")
def api_kalshi():
    return jsonify(get_kalshi_markets())


@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "app": "BTC 15M CANDLE CALCULATOR PRO AI"
    })


# ==========================================================
# INICIO
# ==========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
