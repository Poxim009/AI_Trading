"""
simulator.py — Historical Backtest Engine (Linux & Cloud Edition)
Simulates quantitative trading strategies from strategy.py on historical candles.
Pure Python execution using cached datasets in data/.
"""
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import strategy
from settings import DATA_DIR, LOG_DIR, load_config, load_params, save_params

TIMEFRAME_MAP = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


def load_history(symbol: str = "XAUUSD", timeframe: str = "M15", bars: int = 12000, force_refresh: bool = False) -> pd.DataFrame:
    """
    Load historical candle data from local cache in data/{symbol}_{timeframe}.csv.
    """
    cache_path = DATA_DIR / f"{symbol}_{timeframe}.csv"

    if cache_path.exists():
        try:
            df = pd.read_csv(cache_path)
            if "time" in df.columns and len(df) > 0:
                df["time"] = pd.to_datetime(df["time"])
                if len(df) > bars:
                    df = df.iloc[-bars:].copy().reset_index(drop=True)
                return df
        except Exception as e:
            print(f"[Simulator] Failed reading cache {cache_path}: {e}")

    # Fallback to M15 cache if specific timeframe file is missing
    fallback_path = DATA_DIR / "XAUUSD_M15.csv"
    if fallback_path.exists():
        try:
            print(f"[Simulator] ⚠️ {cache_path.name} not found; falling back to {fallback_path.name}...")
            df = pd.read_csv(fallback_path)
            if "time" in df.columns and len(df) > 0:
                df["time"] = pd.to_datetime(df["time"])
                if len(df) > bars:
                    df = df.iloc[-bars:].copy().reset_index(drop=True)
                return df
        except Exception:
            pass

    raise RuntimeError(
        f"Missing historical candle dataset for {symbol} {timeframe} at {cache_path}"
    )


def compute_metrics(trades: list[dict], initial_balance: float) -> dict:
    """
    Compute quantitative performance metrics:
    - net_profit: Total net profit in USD
    - trades: Total number of trades
    - buy_trades: Number of BUY trades
    - sell_trades: Number of SELL trades
    - win_rate: Win rate percentage (%)
    - buy_win_rate: BUY win rate percentage (%)
    - sell_win_rate: SELL win rate percentage (%)
    - profit_factor: Gross profit / Gross loss
    - max_drawdown_pct: Maximum peak-to-trough drawdown (%)
    - final_balance: Ending account balance in USD
    """
    if not trades:
        return {
            "net_profit": 0.0,
            "trades": 0,
            "buy_trades": 0,
            "sell_trades": 0,
            "win_rate": 0.0,
            "buy_win_rate": 0.0,
            "sell_win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "final_balance": initial_balance,
        }

    total_trades = len(trades)
    win_trades = [t for t in trades if t["profit"] > 0]
    loss_trades = [t for t in trades if t["profit"] < 0]

    buy_trades = [t for t in trades if t["side"] == "BUY"]
    sell_trades = [t for t in trades if t["side"] == "SELL"]
    buy_wins = [t for t in buy_trades if t["profit"] > 0]
    sell_wins = [t for t in sell_trades if t["profit"] > 0]

    gross_profit = sum(t["profit"] for t in win_trades)
    gross_loss = abs(sum(t["profit"] for t in loss_trades))
    net_profit = sum(t["profit"] for t in trades)

    win_rate = (len(win_trades) / total_trades) * 100.0 if total_trades > 0 else 0.0
    buy_win_rate = (len(buy_wins) / len(buy_trades)) * 100.0 if buy_trades else 0.0
    sell_win_rate = (len(sell_wins) / len(sell_trades)) * 100.0 if sell_trades else 0.0

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = 99.0 if gross_profit > 0 else 0.0

    peak = initial_balance
    current = initial_balance
    max_dd_pct = 0.0

    for t in trades:
        current += t["profit"]
        if current > peak:
            peak = current
        if peak > 0:
            dd_pct = (peak - current) / peak * 100.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    return {
        "net_profit": round(net_profit, 2),
        "trades": total_trades,
        "buy_trades": len(buy_trades),
        "sell_trades": len(sell_trades),
        "win_rate": round(win_rate, 2),
        "buy_win_rate": round(buy_win_rate, 2),
        "sell_win_rate": round(sell_win_rate, 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "final_balance": round(initial_balance + net_profit, 2),
    }


def export_trades_csv(trades: list[dict], filepath: Path | str = None) -> Path:
    """Export complete trade ledger to CSV file."""
    if filepath is None:
        filepath = LOG_DIR / "backtest_trades.csv"
    else:
        filepath = Path(filepath)

    df_trades = pd.DataFrame(trades)
    if not df_trades.empty:
        col_order = [
            "ticket", "entry_time", "exit_time", "side", "lot",
            "entry", "exit", "sl", "tp", "profit", "balance", "exit_reason"
        ]
        present_cols = [c for c in col_order if c in df_trades.columns]
        df_trades = df_trades[present_cols]
        df_trades.to_csv(filepath, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame(columns=[
            "ticket", "entry_time", "exit_time", "side", "lot",
            "entry", "exit", "sl", "tp", "profit", "balance", "exit_reason"
        ]).to_csv(filepath, index=False, encoding="utf-8-sig")
    return filepath


def run_backtest(df: pd.DataFrame, params: dict, strategy_module=None, cfg: dict = None, export_csv: bool = False, return_trades: bool = False) -> dict | tuple[dict, list[dict]]:
    """
    Simulate historical trading bar by bar:
    - Enters at open price of the subsequent bar following a closed signal bar
    - Dynamic Stop Loss and Take Profit exits based on ATR multiplier
    - Conservative same-bar tiebreak: SL takes precedence if both high and low breach levels
    """
    if strategy_module is None:
        strategy_module = strategy

    if cfg is None:
        cfg = {
            "initial_balance": 10000.0,
            "risk_percent": 1.0,
            "spread_dollars": 0.30,
        }

    initial_balance = float(cfg.get("initial_balance", 10000.0))
    risk_pct = float(cfg.get("risk_percent", 1.0))
    spread = float(cfg.get("spread_dollars", 0.30))

    cache_key = (
        params.get("sma_period"),
        params.get("rsi_period"),
        params.get("atr_period"),
        params.get("adx_period"),
    )
    orig_df = df
    if "SMA" not in df.columns or df.attrs.get("params_cache_key") != cache_key:
        df = strategy_module.add_indicators(df, params)
        df.attrs["params_cache_key"] = cache_key
        orig_df.attrs["params_cache_key"] = cache_key

    balance = initial_balance
    trades = []
    position = None

    max_warmup = max(
        int(params.get("sma_period", 50)),
        int(params.get("rsi_period", 14)),
        int(params.get("atr_period", 14)),
        int(params.get("adx_period", 14)),
    )
    start_idx = max(max_warmup + 2, 2)
    n_bars = len(df)

    for i in range(start_idx, n_bars - 1):
        bar = df.iloc[i]

        if position is not None:
            side = position["side"]
            sl = position["sl"]
            tp = position["tp"]

            if side == "BUY":
                hit_sl = bar["low"] <= sl
                hit_tp = bar["high"] >= tp
            else:
                hit_sl = bar["high"] >= sl
                hit_tp = bar["low"] <= tp

            if hit_sl or hit_tp:
                is_sl_exit = hit_sl
                exit_price = sl if is_sl_exit else tp

                if side == "BUY":
                    pnl_dollar_per_oz = exit_price - position["entry"]
                else:
                    pnl_dollar_per_oz = position["entry"] - exit_price

                trade_profit = pnl_dollar_per_oz * position["lot"] * 100.0
                balance += trade_profit

                trades.append({
                    "ticket": len(trades) + 1,
                    "entry_time": position["entry_time"],
                    "exit_time": bar.get("time", i),
                    "side": side,
                    "lot": round(position["lot"], 2),
                    "entry": round(position["entry"], 2),
                    "exit": round(exit_price, 2),
                    "sl": round(position["sl"], 2),
                    "tp": round(position["tp"], 2),
                    "profit": round(trade_profit, 2),
                    "balance": round(balance, 2),
                    "exit_reason": "SL" if is_sl_exit else "TP",
                })
                position = None
            continue

        sub_df = df.iloc[max(0, i - 2): i + 1]
        signal = strategy_module.generate_signal(sub_df, params, sentiment=0.0)

        if signal in ("BUY", "SELL"):
            next_bar = df.iloc[i + 1]
            entry = float(next_bar["open"]) + (spread if signal == "BUY" else 0.0)
            atr_val = float(bar["ATR"]) if not pd.isna(bar["ATR"]) and bar["ATR"] > 0 else 5.0
            sl, tp = strategy_module.sl_tp(signal, entry, atr_val, params)

            sl_dist = abs(entry - sl)
            if sl_dist > 0:
                risk_amount = balance * (risk_pct / 100.0)
                lot = risk_amount / (sl_dist * 100.0)
                lot = max(0.01, round(lot, 2))
            else:
                lot = 0.01

            position = {
                "side": signal,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "lot": lot,
                "entry_time": next_bar.get("time", i + 1),
            }

    if export_csv:
        export_trades_csv(trades)

    metrics = compute_metrics(trades, initial_balance)
    if return_trades:
        return metrics, trades
    return metrics


def print_metrics_table(metrics: dict, title: str = "Backtest Results", period_info: str = ""):
    """Format and print backtest metrics in readable console table."""
    print("\n" + "=" * 60)
    print(f"  {title}")
    if period_info:
        print(f"  Period: {period_info}")
    print("=" * 60)
    print(f"  {'Total Trades:':<32} {metrics['trades']} (BUY: {metrics.get('buy_trades', 0)}, SELL: {metrics.get('sell_trades', 0)})")
    print(f"  {'Win Rate:':<32} {metrics['win_rate']}%")
    if metrics.get('buy_trades', 0) > 0:
        print(f"  {'• BUY Win Rate:':<32} {metrics.get('buy_win_rate', 0)}% ({metrics.get('buy_trades', 0)} trades)")
    if metrics.get('sell_trades', 0) > 0:
        print(f"  {'• SELL Win Rate:':<32} {metrics.get('sell_win_rate', 0)}% ({metrics.get('sell_trades', 0)} trades)")
    print(f"  {'Profit Factor:':<32} {metrics['profit_factor']}")
    print(f"  {'Maximum Drawdown:':<32} {metrics['max_drawdown_pct']}%")
    print(f"  {'Net Profit:':<32} ${metrics['net_profit']:,.2f}")
    print(f"  {'Final Balance:':<32} ${metrics['final_balance']:,.2f}")
    print("=" * 60)


if __name__ == "__main__":
    params = load_params()
    df_raw = load_history("XAUUSD", "M15", bars=12000)
    res = run_backtest(df_raw, params, export_csv=True)
    print_metrics_table(res, "Automated Strategy Validation Version 5 (Linux)")
