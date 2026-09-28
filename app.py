import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import streamlit as st


# ============================================================
# CONFIGURACIÓN
# ============================================================

st.set_page_config(
    page_title="BTC 15M Signal Bot",
    page_icon="₿",
    layout="centered",
)

NY = ZoneInfo("America/New_York")

SERIES = "KXBTC15M"

KALSHI_HOSTS = [
    "https://external-api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v2",
]

REFRESH_SECONDS = 10
PREVIEW_SECONDS = 180
SUGGESTED_POSITION = 25
MIN_EXPECTED_RETURN = 0.10


# ============================================================
# ESTILO
# ============================================================

st.markdown(
    """
    <style>
    .block-container {
        max-width: 700px;
        padding-top: 1rem;
        padding-left: 1rem;
        padding-right: 1rem;
    }

    h1 {
        font-size: 1.7rem !important;
    }

    h2 {
        font-size: 1.25rem !important;
    }

    h3 {
        font-size: 1.05rem !important;
    }

    .big-signal {
        font-size: 2rem;
        font-weight: 800;
        text-align: center;
        padding: 12px;
        border-radius: 14px;
        margin: 8px 0;
    }

    .green {
        background: rgba(0, 180, 80, 0.12);
        border: 1px solid rgba(0, 180, 80, 0.4);
    }

    .red {
        background: rgba(220, 50, 50, 0.12);
        border: 1px solid rgba(220, 50, 50, 0.4);
    }

    .card {
        padding: 12px;
        border-radius: 12px;
        border: 1px solid rgba(128,128,128,.25);
        margin-bottom: 10px;
    }

    .small {
        font-size: 0.85rem;
        opacity: .8;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# FUNCIONES GENERALES
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def parse_dt(value):
    if not value:
        return None

    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)

        value = str(value)

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except Exception:
        return None


def fmt_money(value):
    if value is None:
        return "—"

    return f"${value:,.2f}"


def fmt_pct(value):
    if value is None:
        return "—"

    return f"{value * 100:.1f}%"


def countdown(seconds):
    if seconds is None:
        return "—"

    seconds = max(0, int(seconds))

    minutes = seconds // 60
    secs = seconds % 60

    return f"{minutes:02d}:{secs:02d}"


# ============================================================
# KRAKEN
# ============================================================

@st.cache_data(ttl=8, show_spinner=False)
def get_btc_ohlc():

    url = "https://api.kraken.com/0/public/OHLC"

    params = {
        "pair": "XBTUSD",
        "interval": 1,
    }

    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()

        data = r.json()

        if data.get("error"):
            return []

        result = data.get("result", {})

        pair_key = None

        for key in result:
            if key != "last":
                pair_key = key
                break

        if not pair_key:
            return []

        candles = result[pair_key]

        output = []

        for candle in candles:

            try:
                output.append(
                    {
                        "time": datetime.fromtimestamp(
                            float(candle[0]),
                            tz=timezone.utc,
                        ),
                        "open": float(candle[1]),
                        "high": float(candle[2]),
                        "low": float(candle[3]),
                        "close": float(candle[4]),
                    }
                )
            except Exception:
                continue

        return output

    except Exception:
        return []


def price_at_or_before(candles, target):

    valid = [
        c for c in candles
        if c["time"] <= target
    ]

    if not valid:
        return None

    return valid[-1]["close"]


def percent_change(old, new):

    if old is None or new is None or old == 0:
        return 0.0

    return (new - old) / old


# ============================================================
# MODELO
# ============================================================

def calculate_model(candles, market_open):

    if not candles:
        return None

    # MUY IMPORTANTE:
    # Solo usamos información ANTERIOR a la apertura
    # de la vela actual.

    previous = [
        c for c in candles
        if c["time"] < market_open
    ]

    if len(previous) < 20:
        return None

    latest = previous[-1]

    p1 = price_at_or_before(
        previous,
        latest["time"].replace(second=0, microsecond=0)
    )

    p3 = price_at_or_before(
        previous,
        latest["time"] - __import__("datetime").timedelta(minutes=3)
    )

    p5 = price_at_or_before(
        previous,
        latest["time"] - __import__("datetime").timedelta(minutes=5)
    )

    p10 = price_at_or_before(
        previous,
        latest["time"] - __import__("datetime").timedelta(minutes=10)
    )

    p15 = price_at_or_before(
        previous,
        latest["time"] - __import__("datetime").timedelta(minutes=15)
    )

    c1 = percent_change(p1, latest["close"])
    c3 = percent_change(p3, latest["close"])
    c5 = percent_change(p5, latest["close"])
    c10 = percent_change(p10, latest["close"])
    c15 = percent_change(p15, latest["close"])

    score = (
        c1 * 0.10
        + c3 * 0.20
        + c5 * 0.25
        + c10 * 0.25
        + c15 * 0.20
    )

    momentum = (
        c1 * 0.30
        + c3 * 0.30
        + c5 * 0.40
    )

    score += momentum * 0.25

    probability = 0.50 + abs(score) * 12

    probability = max(0.55, min(0.84, probability))

    if score >= 0:
        direction = "SUBE"
    else:
        direction = "BAJA"

    return {
        "direction": direction,
        "probability": probability,
        "score": score,
        "price": latest["close"],
        "c1": c1,
        "c3": c3,
        "c5": c5,
        "c10": c10,
        "c15": c15,
    }


def strength(probability):

    if probability >= 0.70:
        return "FUERTE"

    if probability >= 0.62:
        return "MEDIA"

    return "BAJA"


# ============================================================
# KALSHI
# ============================================================

def get_markets():

    for host in KALSHI_HOSTS:

        url = f"{host}/markets"

        params = {
            "series_ticker": SERIES,
            "status": "open",
            "limit": 100,
        }

        try:
            r = requests.get(
                url,
                params=params,
                timeout=10,
            )

            if r.status_code != 200:
                continue

            data = r.json()

            markets = data.get("markets", [])

            if markets:
                return markets

        except Exception:
            continue

    return []


def get_current_market(markets):

    current = now_utc()

    candidates = []

    for market in markets:

        ticker = market.get("ticker")

        if not ticker:
            continue

        open_time = parse_dt(
            market.get("open_time")
        )

        close_time = parse_dt(
            market.get("close_time")
        )

        if not open_time or not close_time:
            continue

        if open_time <= current < close_time:
            candidates.append(
                (
                    open_time,
                    close_time,
                    market,
                )
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0],
        reverse=True,
    )

    return candidates[0][2]


def get_next_market(markets, current_ticker=None):

    current = now_utc()

    candidates = []

    for market in markets:

        ticker = market.get("ticker")

        if not ticker:
            continue

        if ticker == current_ticker:
            continue

        open_time = parse_dt(
            market.get("open_time")
        )

        if not open_time:
            continue

        if open_time > current:
            candidates.append(
                (open_time, market)
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: x[0]
    )

    return candidates[0][1]


def dollar_to_probability(value):

    if value is None:
        return None

    try:
        return float(value)
    except Exception:
        return None


def get_kalshi_probability(market):

    yes_bid = dollar_to_probability(
        market.get("yes_bid_dollars")
    )

    yes_ask = dollar_to_probability(
        market.get("yes_ask_dollars")
    )

    last = dollar_to_probability(
        market.get("last_price_dollars")
    )

    if yes_bid is not None and yes_ask is not None:
        probability = (
            yes_bid + yes_ask
        ) / 2

    elif last is not None:
        probability = last

    elif yes_bid is not None:
        probability = yes_bid

    elif yes_ask is not None:
        probability = yes_ask

    else:
        return None

    return max(0.0, min(1.0, probability))


# ============================================================
# OBJETIVO
# ============================================================

def get_target(market):

    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    custom = market.get("custom_strike")
    strike = market.get("strike")

    for value in [custom, strike, cap, floor]:

        if value is not None:

            try:
                return float(value)
            except Exception:
                pass

    return None


# ============================================================
# SEÑAL FIJA
# ============================================================

@st.cache_data(
    show_spinner=False,
    ttl=900,
)
def fixed_signal(ticker, open_time_iso):

    market_open = parse_dt(open_time_iso)

    candles = get_btc_ohlc()

    result = calculate_model(
        candles,
        market_open,
    )

    if result is None:

        return {
            "direction": "SUBE",
            "probability": 0.55,
            "score": 0,
            "price": None,
        }

    return result


# ============================================================
# ENTRADA
# ============================================================

def calculate_max_entry(model_probability):

    if model_probability is None:
        return None

    return model_probability / (
        1 + MIN_EXPECTED_RETURN
    )


# ============================================================
# ALERTA
# ============================================================

def live_kalshi_direction(probability):

    if probability is None:
        return None

    if probability >= 0.50:
        return "SUBE"

    return "BAJA"


# ============================================================
# UI PRINCIPAL
# ============================================================

st.title("₿ BTC 15M Signal Bot")

st.caption(
    "Señales solamente • Sin compras automáticas"
)


@st.fragment(run_every=REFRESH_SECONDS)
def live_dashboard():

    markets = get_markets()

    if not markets:

        st.error(
            "No se pudieron obtener los mercados de Kalshi."
        )

        st.stop()

    market = get_current_market(markets)

    if not market:

        st.warning(
            "No hay una vela BTC 15M activa en este momento."
        )

        next_market = get_next_market(markets)

        if next_market:

            next_open = parse_dt(
                next_market.get("open_time")
            )

            if next_open:

                st.info(
                    "Próxima vela: "
                    + next_open.astimezone(NY).strftime(
                        "%I:%M:%S %p"
                    )
                )

        st.stop()

    ticker = market.get("ticker")

    open_time = parse_dt(
        market.get("open_time")
    )

    close_time = parse_dt(
        market.get("close_time")
    )

    if not open_time or not close_time:

        st.error(
            "El mercado no tiene horarios válidos."
        )

        st.stop()

    # --------------------------------------------------------
    # SEÑAL FIJA
    # --------------------------------------------------------

    signal = fixed_signal(
        ticker,
        open_time.isoformat(),
    )

    direction = signal["direction"]
    probability = signal["probability"]

    # --------------------------------------------------------
    # TIEMPO
    # --------------------------------------------------------

    current = now_utc()

    remaining = (
        close_time - current
    ).total_seconds()

    elapsed = (
        current - open_time
    ).total_seconds()

    # --------------------------------------------------------
    # BTC ACTUAL
    # --------------------------------------------------------

    candles = get_btc_ohlc()

    btc_current = None

    if candles:

        btc_current = candles[-1]["close"]

    # --------------------------------------------------------
    # KALSHI LIVE
    # --------------------------------------------------------

    kalshi_prob = get_kalshi_probability(
        market
    )

    kalshi_direction = live_kalshi_direction(
        kalshi_prob
    )

    # --------------------------------------------------------
    # TARGET
    # --------------------------------------------------------

    target = get_target(market)

    # --------------------------------------------------------
    # ENTRADA
    # --------------------------------------------------------

    max_entry = calculate_max_entry(
        probability
    )

    live_direction_price = None

    if kalshi_prob is not None:

        if direction == "SUBE":
            live_direction_price = kalshi_prob
        else:
            live_direction_price = 1 - kalshi_prob

    # --------------------------------------------------------
    # ALERTA
    # --------------------------------------------------------

    if (
        kalshi_direction
        and kalshi_direction != direction
    ):

        st.warning(
            f"🚨 ALERTA DE GIRO\n\n"
            f"{direction} → {kalshi_direction}"
        )

    # --------------------------------------------------------
    # SEÑAL PRINCIPAL
    # --------------------------------------------------------

    if direction == "SUBE":

        st.markdown(
            """
            <div class="big-signal green">
            🟢 SUBE
            </div>
            """,
            unsafe_allow_html=True,
        )

    else:

        st.markdown(
            """
            <div class="big-signal red">
            🔴 BAJA
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown(
        f"""
        <div class="card">
        <b>📈 Probabilidad del modelo:</b>
        {probability * 100:.1f}%<br>

        <b>💪 Fuerza:</b>
        {strength(probability)}<br>

        <b>⏱️ Cierra en:</b>
        {countdown(remaining)}<br>

        <b>🕐 Cierre NY:</b>
        {close_time.astimezone(NY).strftime("%I:%M:%S %p")}<br>

        <span class="small">
        Mercado: {ticker}
        </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # --------------------------------------------------------
    # KALSHI
    # --------------------------------------------------------

    st.subheader("📊 Kalshi en vivo")

    if kalshi_prob is not None:

        st.write(
            f"🟢 SUBE: **{kalshi_prob * 100:.1f}%**"
        )

        st.write(
            f"🔴 BAJA: **{(1 - kalshi_prob) * 100:.1f}%**"
        )

        st.write(
            f"Dirección actual de Kalshi: "
            f"**{kalshi_direction}**"
        )

    else:

        st.write(
            "No hay precio disponible."
        )

    # --------------------------------------------------------
    # BTC
    # --------------------------------------------------------

    st.subheader("₿ BTC")

    if btc_current is not None:

        st.write(
            f"BTC actual: **{fmt_money(btc_current)}**"
        )

    if target is not None:

        st.write(
            f"🎯 Precio objetivo: "
            f"**{fmt_money(target)}**"
        )

        if btc_current is not None:

            difference = btc_current - target

            if difference >= 0:

                st.write(
                    f"BTC sobre objetivo: "
                    f"**+${abs(difference):,.2f}**"
                )

            else:

                st.write(
                    f"BTC hasta objetivo: "
                    f"**-${abs(difference):,.2f}**"
                )

    # --------------------------------------------------------
    # MEJOR ENTRADA
    # --------------------------------------------------------

    st.subheader("💰 MEJOR ENTRADA")

    if max_entry is not None:

        st.write(
            f"Probabilidad modelo: "
            f"**{probability * 100:.1f}%**"
        )

        st.write(
            f"Entrada máxima sugerida: "
            f"**{max_entry * 100:.1f}¢**"
        )

        if live_direction_price is not None:

            st.write(
                f"Precio actual de dirección: "
                f"**{live_direction_price * 100:.1f}¢**"
            )

            if live_direction_price <= max_entry:

                st.success(
                    "✅ ENTRADA FAVORABLE"
                )

            else:

                st.info(
                    "⏳ ESPERAR MEJOR PRECIO"
                )

    # --------------------------------------------------------
    # POSICIÓN
    # --------------------------------------------------------

    st.subheader("📊 Tamaño sugerido")

    st.write(
        f"**{SUGGESTED_POSITION}%** "
        "del capital destinado a esta operación"
    )

    st.caption(
        "Esto es una sugerencia de gestión de riesgo. "
        "No realiza ninguna compra."
    )

    # --------------------------------------------------------
    # RADAR
    # --------------------------------------------------------

    st.subheader("🚨 Radar")

    if kalshi_direction == direction:

        st.success(
            "Sin giro significativo."
        )

    elif kalshi_direction:

        st.warning(
            f"Kalshi está mostrando {kalshi_direction} "
            f"mientras la señal fija es {direction}."
        )

    # --------------------------------------------------------
    # PRÓXIMA VELA
    # --------------------------------------------------------

    if remaining <= PREVIEW_SECONDS:

        st.subheader("🔮 PRÓXIMA VELA")

        next_market = get_next_market(
            markets,
            ticker,
        )

        if next_market:

            next_ticker = next_market.get(
                "ticker"
            )

            next_open = parse_dt(
                next_market.get("open_time")
            )

            if next_open:

                next_signal = fixed_signal(
                    next_ticker,
                    next_open.isoformat(),
                )

                if next_signal:

                    next_direction = (
                        next_signal["direction"]
                    )

                    next_probability = (
                        next_signal["probability"]
                    )

                    st.info(
                        f"Preliminar: "
                        f"**{next_direction}** — "
                        f"{next_probability * 100:.1f}%"
                    )

                    st.caption(
                        "Esta señal es preliminar. "
                        "No reemplaza la señal actual "
                        "hasta que comience la nueva vela."
                    )

    # --------------------------------------------------------
    # ACTUALIZACIÓN
    # --------------------------------------------------------

    st.caption(
        f"Actualización automática cada "
        f"{REFRESH_SECONDS} segundos."
    )


live_dashboard()
