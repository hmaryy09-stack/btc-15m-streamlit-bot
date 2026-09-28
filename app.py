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

NY_TZ = ZoneInfo("America/New_York")

KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"

KALSHI_HOSTS = [
    "https://external-api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v2",
]

SERIES = "KXBTC15M"

REFRESH_SECONDS = 10
PREVIEW_SECONDS = 180

SUGGESTED_SIZE = 25
MIN_EXPECTED_RETURN = 0.10


# ============================================================
# ESTILO
# ============================================================

st.markdown(
    """
    <style>
    .block-container {
        max-width: 680px;
        padding-top: 0.7rem;
        padding-left: 0.7rem;
        padding-right: 0.7rem;
    }

    h1 {
        font-size: 1.35rem !important;
    }

    h2 {
        font-size: 1.05rem !important;
    }

    h3 {
        font-size: 0.95rem !important;
    }

    .signal {
        text-align: center;
        font-size: 1.45rem;
        font-weight: 800;
        padding: 10px;
        border: 1px solid rgba(128,128,128,.45);
        border-radius: 12px;
        margin-bottom: 10px;
    }

    .box {
        border: 1px solid rgba(128,128,128,.45);
        border-radius: 12px;
        padding: 10px;
        margin: 8px 0;
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
        dt = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt

    except (TypeError, ValueError):
        return None


def money(value):
    if value is None:
        return "—"

    return f"${value:,.2f}"


def countdown(seconds):
    seconds = max(0, int(seconds))

    minutes = seconds // 60
    secs = seconds % 60

    return f"{minutes:02d}:{secs:02d}"


# ============================================================
# KRAKEN — HISTORIAL BTC
# ============================================================

@st.cache_data(ttl=5, show_spinner=False)
def get_btc_history():

    response = requests.get(
        KRAKEN_URL,
        params={
            "pair": "XBTUSD",
            "interval": 1,
        },
        timeout=10,
    )

    response.raise_for_status()

    payload = response.json()

    if payload.get("error"):
        errors = payload["error"]

        if isinstance(errors, list):
            errors = ", ".join(
                str(x) for x in errors
            )

        raise RuntimeError(
            f"Kraken: {errors}"
        )

    result = payload.get(
        "result",
        {},
    )

    pair_key = next(
        (
            key
            for key in result
            if key != "last"
        ),
        None,
    )

    if not pair_key:
        raise RuntimeError(
            "Kraken no devolvió BTC."
        )

    candles = result[pair_key]

    data = []

    for candle in candles:

        if len(candle) < 5:
            continue

        try:

            timestamp = float(
                candle[0]
            )

            close = float(
                candle[4]
            )

            data.append(
                {
                    "timestamp": timestamp,
                    "close": close,
                }
            )

        except (
            TypeError,
            ValueError,
        ):
            continue

    if len(data) < 20:

        raise RuntimeError(
            "No hay suficientes datos de BTC."
        )

    data.sort(
        key=lambda x: x["timestamp"]
    )

    return data


# ============================================================
# BTC ACTUAL
# ============================================================

def get_current_btc(history):

    if not history:
        return None

    return float(
        history[-1]["close"]
    )


# ============================================================
# BTC EN UN MOMENTO ESPECÍFICO
# ============================================================

def btc_snapshot_at(
    history,
    target_datetime,
):

    target_timestamp = (
        target_datetime.timestamp()
    )

    candles = [
        item
        for item in history
        if item["timestamp"]
        < target_timestamp
    ]

    if len(candles) < 16:

        return None

    candles = candles[-16:]

    closes = [
        float(item["close"])
        for item in candles
    ]

    price = closes[-1]

    def change(minutes):

        if len(closes) <= minutes:
            return 0.0

        previous = closes[
            -1 - minutes
        ]

        if previous == 0:
            return 0.0

        return (
            (price - previous)
            / previous
        ) * 100.0

    return {
        "price": price,
        "c1": change(1),
        "c3": change(3),
        "c5": change(5),
        "c10": change(10),
        "c15": change(15),
    }


# ============================================================
# MODELO
# ============================================================

def model_signal(btc):

    raw_score = (
        btc["c1"] * 0.10
        + btc["c3"] * 0.25
        + btc["c5"] * 0.25
        + btc["c10"] * 0.25
        + btc["c15"] * 0.15
    )

    volatility = (
        abs(btc["c1"])
        + abs(btc["c3"])
        + abs(btc["c5"])
    )

    if raw_score > 0:

        raw_score -= (
            volatility * 0.05
        )

    else:

        raw_score += (
            volatility * 0.05
        )

    probability = (
        0.50
        + raw_score * 18.0
    )

    probability = max(
        0.16,
        min(0.84, probability),
    )

    if probability >= 0.50:

        return (
            "SUBE",
            probability,
        )

    return (
        "BAJA",
        1.0 - probability,
    )


def strength(probability):

    if probability >= 0.75:
        return "MUY FUERTE"

    if probability >= 0.65:
        return "FUERTE"

    if probability >= 0.58:
        return "MEDIA"

    return "BAJA"


# ============================================================
# SEÑAL FIJA DE LA VELA
# ============================================================

@st.cache_data(
    show_spinner=False
)
def locked_signal(
    ticker,
    open_time_iso,
):

    open_time = parse_dt(
        open_time_iso
    )

    if open_time is None:

        raise RuntimeError(
            "No se pudo determinar "
            "la apertura del mercado."
        )

    history = get_btc_history()

    btc_at_open = btc_snapshot_at(
        history,
        open_time,
    )

    if btc_at_open is None:

        raise RuntimeError(
            "No hay suficiente historial "
            "de BTC para calcular la señal "
            "al inicio de esta vela."
        )

    direction, probability = (
        model_signal(btc_at_open)
    )

    return {
        "direction": direction,
        "probability": probability,
        "btc_at_open": btc_at_open,
    }


# ============================================================
# KALSHI — MERCADOS
# ============================================================

@st.cache_data(
    ttl=5,
    show_spinner=False,
)
def get_kalshi_markets():

    errors = []

    for host in KALSHI_HOSTS:

        try:

            response = requests.get(
                f"{host}/markets",
                params={
                    "series_ticker": SERIES,
                    "status": "open",
                    "limit": 100,
                },
                timeout=10,
            )

            response.raise_for_status()

            payload = response.json()

            markets = payload.get(
                "markets",
                [],
            )

            if isinstance(
                markets,
                list,
            ):

                return markets

        except Exception as exc:

            errors.append(
                f"{host}: {exc}"
            )

    raise RuntimeError(
        "No se pudo consultar Kalshi. "
        + " | ".join(errors)
    )


def market_open(market):

    return parse_dt(
        market.get(
            "open_time"
        )
    )


def market_close(market):

    return parse_dt(
        market.get("close_time")
        or market.get(
            "expiration_time"
        )
    )


def find_current_market(
    markets,
):

    now = now_utc()

    candidates = []

    for market in markets:

        open_time = market_open(
            market
        )

        close_time = market_close(
            market
        )

        if close_time is None:
            continue

        if close_time <= now:
            continue

        if (
            open_time is not None
            and open_time > now
        ):
            continue

        status = str(
            market.get(
                "status",
                "",
            )
        ).lower()

        if status not in {
            "",
            "open",
            "active",
        }:

            continue

        candidates.append(
            (
                close_time,
                market,
            )
        )

    candidates.sort(
        key=lambda x: x[0]
    )

    if not candidates:
        return None

    return candidates[0][1]


def find_next_market(
    markets,
    current_market,
):

    current_close = market_close(
        current_market
    )

    if current_close is None:
        return None

    candidates = []

    for market in markets:

        close_time = market_close(
            market
        )

        if (
            close_time is not None
            and close_time > current_close
        ):

            candidates.append(
                (
                    close_time,
                    market,
                )
            )

    candidates.sort(
        key=lambda x: x[0]
    )

    if not candidates:
        return None

    return candidates[0][1]


# ============================================================
# KALSHI — PROBABILIDAD EN VIVO
# ============================================================

def dollar_probability(value):

    if value is None:
        return None

    try:

        value = float(value)

        return max(
            0.0,
            min(1.0, value),
        )

    except (
        TypeError,
        ValueError,
    ):

        return None


def kalshi_yes_probability(
    market,
):

    yes_bid = dollar_probability(
        market.get(
            "yes_bid_dollars"
        )
    )

    yes_ask = dollar_probability(
        market.get(
            "yes_ask_dollars"
        )
    )

    if (
        yes_bid is not None
        and yes_ask is not None
    ):

        return (
            yes_bid
            + yes_ask
        ) / 2.0

    last_price = dollar_probability(
        market.get(
            "last_price_dollars"
        )
    )

    if last_price is not None:

        return last_price

    # Compatibilidad con campos antiguos

    old_bid = market.get(
        "yes_bid"
    )

    old_ask = market.get(
        "yes_ask"
    )

    try:

        if (
            old_bid is not None
            and old_ask is not None
        ):

            return max(
                0.0,
                min(
                    1.0,
                    (
                        float(old_bid)
                        + float(old_ask)
                    ) / 200.0,
                ),
            )

        old_last = market.get(
            "last_price"
        )

        if old_last is not None:

            return max(
                0.0,
                min(
                    1.0,
                    float(old_last)
                    / 100.0,
                ),
            )

    except (
        TypeError,
        ValueError,
    ):

        pass

    return None


# ============================================================
# TARGET
# ============================================================

def get_target_info(
    market,
):

    floor_value = market.get(
        "floor_strike"
    )

    cap_value = market.get(
        "cap_strike"
    )

    strike
