"""
strategy.py — Quantitative Strategy Engine (Pure math, no MT5 imports)
Shared mathematical core used by simulator.py and trade_bot.py.
Overseer may optimize parameters and formulas within safety bounds.
"""
import numpy as np
import pandas as pd

try:
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    PANDAS_TA_AVAILABLE = False


def add_indicators(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """
    Accept OHLC candle dataframe and calculate SMA, RSI, ATR, ADX indicators.
    """
    orig_attrs = dict(df.attrs)
    df = df.copy()
    df.attrs.update(orig_attrs)

    sma_period = p.get("sma_period", 50)
    rsi_period = p.get("rsi_period", 14)
    atr_period = p.get("atr_period", 14)
    adx_period = p.get("adx_period", 14)

    # SMA: Trend direction filter over N bars
    df["SMA"] = df["close"].rolling(sma_period).mean()

    # RSI (Wilder): Momentum oscillator (0 - 100)
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    a = 1.0 / max(rsi_period, 1)
    avg_gain = gain.ewm(alpha=a, adjust=False).mean()
    avg_loss = loss.ewm(alpha=a, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-9)
    df["RSI"] = 100 - (100 / (1 + rs))

    # ATR: True range volatility measure for dynamic SL/TP calculation
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1.0 / max(atr_period, 1), adjust=False).mean()

    # ADX (Average Directional Index): Trend strength filter (0 - 100)
    # Filters choppy/sideways markets (ADX < threshold returns WAIT)
    if PANDAS_TA_AVAILABLE:
        try:
            adx_res = df.ta.adx(length=adx_period)
            if adx_res is not None and not adx_res.empty:
                col_name = f"ADX_{adx_period}"
                if col_name in adx_res.columns:
                    df["ADX"] = adx_res[col_name]
                else:
                    df["ADX"] = adx_res.iloc[:, 0]
        except Exception:
            pass

    # Robust Native Wilder ADX calculation (Vectorized fallback)
    if "ADX" not in df.columns or df["ADX"].isna().all():
        up_move = df["high"] - df["high"].shift(1)
        down_move = df["low"].shift(1) - df["low"]

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        adx_alpha = 1.0 / max(adx_period, 1)
        tr_smooth = tr.ewm(alpha=adx_alpha, adjust=False).mean()
        plus_dm_s = pd.Series(plus_dm, index=df.index).ewm(alpha=adx_alpha, adjust=False).mean()
        minus_dm_s = pd.Series(minus_dm, index=df.index).ewm(alpha=adx_alpha, adjust=False).mean()

        plus_di = 100.0 * (plus_dm_s / tr_smooth.replace(0, 1e-9))
        minus_di = 100.0 * (minus_dm_s / tr_smooth.replace(0, 1e-9))

        di_sum = plus_di + minus_di
        dx = 100.0 * ((plus_di - minus_di).abs() / di_sum.replace(0, 1e-9))
        df["ADX"] = dx.ewm(alpha=adx_alpha, adjust=False).mean()

    return df


def generate_signal(df: pd.DataFrame, p: dict, sentiment: float = 0.0) -> str | None:
    """
    Analyze closed bars (last row is fully closed bar).
    - ADX Filter: If ADX < adx_threshold (choppy market), returns "WAIT"
    - BUY : Price > SMA (uptrend) and RSI crosses above rsi_buy_level (pullback bounce)
    - SELL: Price < SMA (downtrend) and RSI crosses below rsi_sell_level (rally reject)
    """
    if len(df) < 2:
        return None

    prev, last = df.iloc[-2], df.iloc[-1]

    # Guard essential columns to avoid KeyError
    for col in ("SMA", "RSI", "close"):
        if col not in last or pd.isna(last[col]):
            return None
    if "RSI" not in prev or pd.isna(prev["RSI"]):
        return None
    if "ADX" in last and pd.isna(last["ADX"]):
        return None

    # Filter sideways market via ADX threshold
    adx_threshold = p.get("adx_threshold", 20)
    if "ADX" in last and not pd.isna(last["ADX"]):
        if last["ADX"] < adx_threshold:
            return "WAIT"

    rsi_buy = p.get("rsi_buy_level", 45)
    rsi_sell = p.get("rsi_sell_level", 55)

    rsi_cross_up = (prev["RSI"] < rsi_buy) and (last["RSI"] >= rsi_buy)
    rsi_cross_down = (prev["RSI"] > rsi_sell) and (last["RSI"] <= rsi_sell)

    use_news = p.get("use_sentiment_filter", True)

    if last["close"] > last["SMA"] and rsi_cross_up:
        if use_news and sentiment < 0:
            return None  # Negative sentiment filters out BUY signal
        return "BUY"

    if last["close"] < last["SMA"] and rsi_cross_down:
        if use_news and sentiment > 0:
            return None  # Positive sentiment filters out SELL signal
        return "SELL"

    return None


def sl_tp(side: str, entry: float, atr: float, p: dict) -> tuple[float, float]:
    """
    Calculate dynamic Stop Loss (SL) and Take Profit (TP) prices using ATR multiplier.
    """
    sl_dist = atr * p["atr_sl_mult"]
    tp_dist = atr * p["atr_tp_mult"]

    if side == "BUY":
        return entry - sl_dist, entry + tp_dist
    return entry + sl_dist, entry - tp_dist

