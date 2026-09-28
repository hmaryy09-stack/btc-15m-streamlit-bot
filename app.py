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
SUGGESTED_SIZE = 25
MIN_EXPECTED_RETURN = 0.10
PREVIEW_SECONDS = 180


# ============================================================
# ESTILO
# ============================================================

st.markdown(
    """
    <style>
    .block-container {
        max-width: 680px;
        padding-top: 0.8rem;
        padding-left: 0.8rem;
        padding-right: 0.8rem;
    }

    h1 {
        font-size: 1.45rem !important;
    }

    h2 {
        font-size: 1.10rem !important;
    }

    h3 {
        font-size: 0.98rem !important;
    }

    .signal {
        text-align: center;
        font-size: 1.55rem;
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


def money(value):
    if value is None:
        return "—"

    return f"${value:,.2f}"


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


def countdown(seconds):
    seconds = max(0, int(seconds))

    minutes = seconds // 60
    secs = seconds % 60

    return f"{minutes:02d}:{secs:02d}"


# ============================================================
# BTC — KRAKEN
# ============================================================

@st.cache_data(ttl=5, show_spinner=False)
def get_btc_data():

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
            errors = ", ".join(str(x) for x in errors)

        raise RuntimeError(
            f"Kraken: {errors}"
        )

    result = payload.get("result", {})

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
            "Kraken no devolvió datos de BTC."
        )

    candles = result[pair_key]

    closes = []

    for candle in candles:

        if len(candle) >= 5:

            try:
                closes.append(
                    float(candle[4])
                )

            except (TypeError, ValueError):
                pass

    if len(closes) < 16:
        raise RuntimeError(
            "No hay suficientes datos de BTC."
        )

    price = closes[-1]

    def percentage_change(minutes):

        if len(closes) <= minutes:
            return 0.0

        previous = closes[-1 - minutes]

        return (
            (price - previous)
            / previous
        ) * 100.0

    return {
        "price": price,
        "c1": percentage_change(1),
        "c3": percentage_change(3),
        "c5": percentage_change(5),
        "c10": percentage_change(10),
        "c15": percentage_change(15),
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
        raw_score -= volatility * 0.05
    else:
        raw_score += volatility * 0.05

    yes_probability = (
        0.50 + raw_score * 18.0
    )

    yes_probability = max(
        0.16,
        min(0.84, yes_probability),
    )

    if yes_probability >= 0.50:

        return (
            "SUBE",
            yes_probability,
        )

    return (
        "BAJA",
        1.0 - yes_probability,
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
# KALSHI
# ============================================================

@st.cache_data(ttl=5, show_spinner=False)
def get_kalshi_markets():

    errors = []

    for host in KALSHI_HOSTS:

        try:

            response = requests.get(
                f"{host}/markets",
                params={
                    "series_ticker": SERIES,
                    "limit": 100,
                },
                timeout=10,
            )

            if response.ok:

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

            errors.append(
                f"{host}: HTTP "
                f"{response.status_code}"
            )

        except requests.RequestException as exc:

            errors.append(
                f"{host}: {exc}"
            )

    raise RuntimeError(
        "No se pudo consultar Kalshi. "
        + " | ".join(errors)
    )


def market_close(market):

    return parse_dt(
        market.get("close_time")
        or market.get("expiration_time")
    )


def find_current_market(markets):

    now = now_utc()

    candidates = []

    for market in markets:

        close = market_close(market)

        if close is None:
            continue

        if close <= now:
            continue

        status = str(
            market.get("status", "")
        ).lower()

        if status in {
            "closed",
            "settled",
            "finalized",
        }:
            continue

        candidates.append(
            (
                close,
                market,
            )
        )

    candidates.sort(
        key=lambda item: item[0]
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

        close = market_close(market)

        if (
            close is not None
            and close > current_close
        ):
            candidates.append(
                (
                    close,
                    market,
                )
            )

    candidates.sort(
        key=lambda item: item[0]
    )

    if not candidates:
        return None

    return candidates[0][1]


def kalshi_yes_probability(
    market,
):

    try:

        yes_bid = market.get(
            "yes_bid"
        )

        yes_ask = market.get(
            "yes_ask"
        )

        if (
            yes_bid is not None
            and yes_ask is not None
        ):

            probability = (
                float(yes_bid)
                + float(yes_ask)
            ) / 200.0

            return max(
                0.0,
                min(1.0, probability),
            )

        last_price = market.get(
            "last_price"
        )

        if last_price is not None:

            probability = (
                float(last_price)
                / 100.0
            )

            return max(
                0.0,
                min(1.0, probability),
            )

    except (
        TypeError,
        ValueError,
    ):
        pass

    return None


def get_target_price(market):

    keys = [
        "floor_strike",
        "cap_strike",
        "custom_strike",
        "strike",
    ]

    for key in keys:

        value = market.get(key)

        if value is None:
            continue

        try:

            return float(value)

        except (
            TypeError,
            ValueError,
        ):
            continue

    return None


# ============================================================
# PRECIO MÁXIMO DE ENTRADA
# ============================================================

def max_entry_price(
    probability,
):

    return (
        probability
        / (1.0 + MIN_EXPECTED_RETURN)
    )


# ============================================================
# DATOS PRINCIPALES
# ============================================================

try:

    btc = get_btc_data()

    markets = get_kalshi_markets()

    current_market = (
        find_current_market(
            markets
        )
    )

    if current_market is None:

        st.error(
            "No hay un mercado "
            "BTC 15M activo."
        )

        st.stop()

except Exception as exc:

    st.error(
        f"Error de conexión: {exc}"
    )

    st.stop()


# ============================================================
# MERCADO ACTUAL
# ============================================================

ticker = str(
    current_market.get(
        "ticker",
        "",
    )
)

close_time = market_close(
    current_market
)

if close_time is not None:

    remaining = max(
        0.0,
        (
            close_time
            - now_utc()
        ).total_seconds(),
    )

else:

    remaining = 0.0


# ============================================================
# SEÑAL BLOQUEADA
# ============================================================

previous_ticker = (
    st.session_state.get(
        "locked_ticker"
    )
)

if previous_ticker != ticker:

    new_direction, new_probability = (
        model_signal(btc)
    )

    st.session_state.locked_ticker = (
        ticker
    )

    st.session_state.locked_direction = (
        new_direction
    )

    st.session_state.locked_probability = (
        new_probability
    )


direction = (
    st.session_state.locked_direction
)

probability = float(
    st.session_state.locked_probability
)


# ============================================================
# KALSHI EN VIVO
# ============================================================

live_yes = kalshi_yes_probability(
    current_market
)

if live_yes is None:
    live_yes = 0.50

live_sube = live_yes

live_baja = (
    1.0 - live_yes
)

live_direction = (
    "SUBE"
    if live_sube >= live_baja
    else "BAJA"
)


# ============================================================
# ALERTA DE GIRO
# ============================================================

previous_live_direction = (
    st.session_state.get(
        "previous_live_direction"
    )
)

alert = None

if (
    previous_live_direction is not None
    and previous_live_direction
    != live_direction
):

    alert = (
        "🚨 ALERTA DE GIRO: "
        f"{previous_live_direction} "
        f"→ {live_direction}"
    )

st.session_state.previous_live_direction = (
    live_direction
)


# ============================================================
# TARGET Y PROYECCIÓN
# ============================================================

target = get_target_price(
    current_market
)

current_btc_price = btc["price"]

projected_close = None
required_velocity = None

if (
    target is not None
    and remaining > 0
):

    required_velocity = (
        target - current_btc_price
    ) / remaining

    projected_close = (
        current_btc_price
        + (
            btc["c1"]
            / 100.0
            * current_btc_price
        )
    )


# ============================================================
# MEJOR ENTRADA
# ============================================================

entry_limit = max_entry_price(
    probability
)

if direction == "SUBE":

    current_entry = live_sube

else:

    current_entry = live_baja


if current_entry < (
    entry_limit - 0.005
):

    entry_status = (
        "🟢 FAVORABLE"
    )

elif current_entry <= (
    entry_limit + 0.005
):

    entry_status = (
        "🟡 EN LÍMITE"
    )

else:

    entry_status = (
        "🔴 ESPERAR"
    )


# ============================================================
# INTERFAZ
# ============================================================

signal_icon = (
    "🟢"
    if direction == "SUBE"
    else "🔴"
)

st.title(
    "₿ BTC 15 MIN — SIGNAL BOT"
)

st.caption(
    "Señales informativas • "
    "NO realiza compras automáticas"
)


st.markdown(
    f"""
    <div class="signal">
        {signal_icon} {direction}
    </div>
    """,
    unsafe_allow_html=True,
)


col1, col2 = st.columns(2)

with col1:

    st.metric(
        "📊 Probabilidad",
        f"{probability * 100:.1f}%",
    )

with col2:

    st.metric(
        "💪 Fuerza",
        strength(probability),
    )


# ============================================================
# MERCADO ACTUAL
# ============================================================

ny_time = "—"

if close_time is not None:

    ny_time = (
        close_time
        .astimezone(NY_TZ)
        .strftime("%I:%M:%S %p")
    )


st.markdown(
    f"""
    <div class="box">
        <b>⏱️ MERCADO ACTUAL</b><br>
        Cierra en:
        <b>{countdown(remaining)}</b><br>
        Hora NY:
        <b>{ny_time}</b>
    </div>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# KALSHI EN VIVO
# ============================================================

st.subheader(
    "📊 KALSHI EN VIVO"
)

col1, col2 = st.columns(2)

with col1:

    st.metric(
        "🟢 SUBE",
        f"{live_sube * 100:.1f}%",
    )

with col2:

    st.metric(
        "🔴 BAJA",
        f"{live_baja * 100:.1f}%",
    )


st.write(
    "Dirección Kalshi en vivo: "
    f"**{live_direction}**"
)


# ============================================================
# TARGET
# ============================================================

if target is not None:

    st.subheader(
        "🎯 TARGET KALSHI"
    )

    st.write(
        "Precio objetivo: "
        f"**{money(target)}**"
    )

    st.write(
        "BTC actual: "
        f"**{money(current_btc_price)}**"
    )

    if projected_close is not None:

        st.write(
            "Proyección de cierre: "
            f"**{money(projected_close)}**"
        )

    if required_velocity is not None:

        sign = (
            "+"
            if required_velocity >= 0
            else ""
        )

        st.write(
            "Movimiento requerido: "
            f"**{sign}"
            f"{money(required_velocity)}/s**"
        )


# ============================================================
# MEJOR ENTRADA
# ============================================================

st.subheader(
    "💰 MEJOR ENTRADA"
)

st.write(
    "Probabilidad del modelo: "
    f"**{probability * 100:.1f}%**"
)

st.write(
    "Precio máximo sugerido: "
    f"**{entry_limit * 100:.1f}¢**"
)

st.write(
    "Precio actual: "
    f"**{current_entry * 100:.1f}¢**"
)

st.write(
    f"### {entry_status}"
)


# ============================================================
# TAMAÑO SUGERIDO
# ============================================================

st.subheader(
    "💵 TAMAÑO SUGERIDO"
)

st.write(
    f"**{SUGGESTED_SIZE}%** del capital"
)

st.caption(
    "Solo es una sugerencia informativa. "
    "El bot NO ejecuta órdenes."
)


# ============================================================
# RADAR
# ============================================================

if alert:

    st.error(alert)

else:

    st.success(
        "🟢 Radar: sin giro significativo"
    )


# ============================================================
# PRÓXIMA VELA
# ============================================================

if remaining <= PREVIEW_SECONDS:

    next_market = find_next_market(
        markets,
        current_market,
    )

    st.subheader(
        "🔮 PRÓXIMA VELA"
    )

    preview_direction, preview_probability = (
        model_signal(btc)
    )

    preview_icon = (
        "🟢"
        if preview_direction == "SUBE"
        else "🔴"
    )

    st.info(
        f"{preview_icon} "
        f"{preview_direction} — "
        f"{preview_probability * 100:.1f}%"
    )

    st.caption(
        "Vista preliminar. "
        "No cambia la señal actual."
    )


# ============================================================
# DATOS BTC
# ============================================================

with st.expander(
    "📈 Datos BTC"
):

    st.write(
        f"1 min: {btc['c1']:+.4f}%"
    )

    st.write(
        f"3 min: {btc['c3']:+.4f}%"
    )

    st.write(
        f"5 min: {btc['c5']:+.4f}%"
    )

    st.write(
        f"10 min: {btc['c10']:+.4f}%"
    )

    st.write(
        f"15 min: {btc['c15']:+.4f}%"
    )


st.caption(
    f"Actualización automática "
    f"cada {REFRESH_SECONDS} segundos."
)


# ============================================================
# ACTUALIZACIÓN
# ============================================================

time.sleep(
    REFRESH_SECONDS
)

st.rerun()
