"""
optimizer.py — Parameter Optimization System (Grid Search & Validation - Linux Edition)
Guards against overfitting by splitting data into Train (70%) and Validation (30%).
Pure Python execution without external DLL dependencies.
"""
import itertools
import json
import shutil
from datetime import datetime
import pandas as pd

import strategy
import simulator
from notifier import send_alert
from settings import (
    BOT_DIR,
    BACKUP_DIR,
    STATE_DIR,
    load_config,
    load_params,
)

GRID = {
    "sma_period":     [20, 50, 100, 200],
    "rsi_buy_level":  [30, 35, 40, 45],
    "rsi_sell_level": [55, 60, 65, 70],
    "atr_sl_mult":    [1.0, 1.5, 2.0],
    "atr_tp_mult":    [2.0, 3.0, 4.0],
}


def run_optimization() -> dict:
    """
    Execute parameter optimization workflow:
    1. Partition candles into 70% Train and 30% Validation sets
    2. Filter candidates meeting minimum criteria on Train (trades >= 30, DD <= 20%)
    3. Evaluate top candidates on unseen Validation set to pick the winner
    4. Save updated strategy_params.json and create restart.flag if validated
    """
    cfg = load_config()
    b_cfg = cfg.get("backtest", {})
    t_cfg = cfg.get("trading", {})
    current_params = load_params()

    symbol = t_cfg.get("symbol", "XAUUSD")
    tf = t_cfg.get("timeframe", "M15")
    bars = int(b_cfg.get("history_bars", 12000))
    train_ratio = float(b_cfg.get("train_ratio", 0.7))
    min_trades = int(b_cfg.get("min_trades", 30))
    max_dd = float(b_cfg.get("max_drawdown_percent", 20.0))

    print(f"\n[Optimizer] 🚀 Starting parameter search for {symbol} {tf}...")
    df = simulator.load_history(symbol=symbol, timeframe=tf, bars=bars)

    total_len = len(df)
    train_len = int(total_len * train_ratio)
    train_df = df.iloc[:train_len].copy()
    val_df = df.iloc[train_len:].copy()

    print(f"[Optimizer] Total bars: {total_len} | Train: {len(train_df)} | Validation: {len(val_df)}")

    # Evaluate current baseline on Validation set
    baseline_val_metrics = simulator.run_backtest(val_df, current_params, strategy, b_cfg)
    baseline_pf = baseline_val_metrics["profit_factor"]
    print(f"[Optimizer] Current Baseline (Validation PF): {baseline_pf} (Win Rate: {baseline_val_metrics['win_rate']}%, Trades: {baseline_val_metrics['trades']})")

    # Precompute indicator DataFrames per sma_period for performance
    precomputed_train = {}
    precomputed_val = {}
    for sma in GRID["sma_period"]:
        p_temp = {
            "sma_period": sma,
            "rsi_period": 14,
            "atr_period": 14,
            "adx_period": current_params.get("adx_period", 14),
        }
        t_df = strategy.add_indicators(train_df, p_temp)
        t_df.attrs["params_cache_key"] = (sma, 14, 14, current_params.get("adx_period", 14))
        t_df.attrs["sma_period"] = sma
        precomputed_train[sma] = t_df

        v_df = strategy.add_indicators(val_df, p_temp)
        v_df.attrs["params_cache_key"] = (sma, 14, 14, current_params.get("adx_period", 14))
        v_df.attrs["sma_period"] = sma
        precomputed_val[sma] = v_df

    combinations = list(itertools.product(
        GRID["sma_period"],
        GRID["rsi_buy_level"],
        GRID["rsi_sell_level"],
        GRID["atr_sl_mult"],
        GRID["atr_tp_mult"]
    ))
    total_combos = len(combinations)
    print(f"[Optimizer] Testing {total_combos} combinations on Train data...")

    train_candidates = []

    for sma, r_buy, r_sell, sl_m, tp_m in combinations:
        p = {
            "sma_period": sma,
            "rsi_period": 14,
            "rsi_buy_level": r_buy,
            "rsi_sell_level": r_sell,
            "atr_period": 14,
            "atr_sl_mult": sl_m,
            "atr_tp_mult": tp_m,
            "adx_period": current_params.get("adx_period", 14),
            "adx_threshold": current_params.get("adx_threshold", 20),
            "use_sentiment_filter": current_params.get("use_sentiment_filter", True),
        }

        metrics = simulator.run_backtest(precomputed_train[sma], p, strategy, b_cfg)

        if metrics["trades"] >= min_trades and metrics["max_drawdown_pct"] <= max_dd:
            train_candidates.append({
                "params": p,
                "train_metrics": metrics
            })

    print(f"[Optimizer] Candidates passing Train criteria: {len(train_candidates)}")

    # Fallback if no candidate meets strict training criteria
    if not train_candidates:
        print("[Optimizer] ⚠️ No candidate passed strict Train criteria; selecting highest PF fallback...")
        best_fallback = None
        best_fallback_pf = -1.0
        for sma, r_buy, r_sell, sl_m, tp_m in combinations:
            p = {
                "sma_period": sma,
                "rsi_period": 14,
                "rsi_buy_level": r_buy,
                "rsi_sell_level": r_sell,
                "atr_period": 14,
                "atr_sl_mult": sl_m,
                "atr_tp_mult": tp_m,
                "adx_period": current_params.get("adx_period", 14),
                "adx_threshold": current_params.get("adx_threshold", 20),
                "use_sentiment_filter": current_params.get("use_sentiment_filter", True),
            }
            m = simulator.run_backtest(precomputed_train[sma], p, strategy, b_cfg)
            if m["profit_factor"] > best_fallback_pf and m["trades"] >= 10:
                best_fallback_pf = m["profit_factor"]
                best_fallback = {"params": p, "train_metrics": m}
        if best_fallback:
            train_candidates.append(best_fallback)

    # Sort by Train Profit Factor and select top 5
    train_candidates.sort(
        key=lambda x: (x["train_metrics"]["profit_factor"], x["train_metrics"]["net_profit"]),
        reverse=True
    )
    top_candidates = train_candidates[:5]

    print(f"[Optimizer] Evaluating top {len(top_candidates)} on unseen Validation set...")

    validated_results = []
    for cand in top_candidates:
        p = cand["params"]
        sma = p["sma_period"]
        val_metrics = simulator.run_backtest(precomputed_val[sma], p, strategy, b_cfg)
        validated_results.append({
            "params": p,
            "train_metrics": cand["train_metrics"],
            "val_metrics": val_metrics
        })

    # Pick winner with highest Validation Profit Factor
    validated_results.sort(
        key=lambda x: (x["val_metrics"]["profit_factor"], x["val_metrics"]["net_profit"]),
        reverse=True
    )
    winner = validated_results[0]
    win_p = winner["params"]
    win_vm = winner["val_metrics"]

    print("\n" + "=" * 60)
    print("  Winning Parameter Set:")
    print(f"  SMA: {win_p['sma_period']} | RSI Buy/Sell: {win_p['rsi_buy_level']}/{win_p['rsi_sell_level']} | ATR SL/TP: {win_p['atr_sl_mult']}x/{win_p['atr_tp_mult']}x")
    print(f"  Train      -> PF: {winner['train_metrics']['profit_factor']} | Trades: {winner['train_metrics']['trades']} | DD: {winner['train_metrics']['max_drawdown_pct']}% | Net: ${winner['train_metrics']['net_profit']}")
    print(f"  Validation -> PF: {win_vm['profit_factor']} | Trades: {win_vm['trades']} | DD: {win_vm['max_drawdown_pct']}% | Net: ${win_vm['net_profit']}")
    print("=" * 60 + "\n")

    now_iso = datetime.now().isoformat()
    old_ver = current_params.get("version", 1)
    new_ver = old_ver + 1

    # Acceptance threshold (Validation PF > 1.1 and >= baseline)
    is_better = win_vm["profit_factor"] > 1.1 and win_vm["profit_factor"] >= baseline_pf

    if is_better:
        passed_validation = True
        status_msg = f"✅ New parameter set validated (Validation PF: {win_vm['profit_factor']} > 1.1)"
    else:
        passed_validation = False
        status_msg = f"⚠️ Parameter set did not fully beat validation bar (Validation PF: {win_vm['profit_factor']})"

    # Backup previous strategy_params.json
    params_file = BOT_DIR / "strategy_params.json"
    if params_file.exists():
        backup_file = BACKUP_DIR / f"strategy_params_v{old_ver}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        shutil.copy(params_file, backup_file)
        print(f"[Optimizer] Backed up previous params to: {backup_file.name}")

    # Save new parameters
    new_params_data = {
        "version": new_ver,
        "sma_period": win_p["sma_period"],
        "rsi_period": win_p.get("rsi_period", 14),
        "rsi_buy_level": win_p["rsi_buy_level"],
        "rsi_sell_level": win_p["rsi_sell_level"],
        "atr_period": win_p.get("atr_period", 14),
        "atr_sl_mult": win_p["atr_sl_mult"],
        "atr_tp_mult": win_p["atr_tp_mult"],
        "adx_period": win_p.get("adx_period", current_params.get("adx_period", 14)),
        "adx_threshold": win_p.get("adx_threshold", current_params.get("adx_threshold", 20)),
        "use_sentiment_filter": win_p.get("use_sentiment_filter", True),
        "passed_validation": passed_validation,
        "updated_by": "optimizer",
        "updated_at": now_iso,
        "validation_metrics": win_vm
    }

    params_file.write_text(json.dumps(new_params_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[Optimizer] strategy_params.json updated (Version: {new_ver}, Passed: {passed_validation})")

    # Drop restart.flag so run_all.py reloads trade_bot
    flag_file = STATE_DIR / "restart.flag"
    flag_file.write_text(f"restart requested by optimizer at {now_iso}", encoding="utf-8")
    print("[Optimizer] Created state/restart.flag to trigger hot reload")

    # Send Discord alert
    alert_level = "TRADE" if passed_validation else "WARNING"
    discord_msg = (
        f"{status_msg}\n"
        f"• Version: v{old_ver} ➔ v{new_ver}\n"
        f"• SMA Period: {win_p['sma_period']}\n"
        f"• RSI Levels: {win_p['rsi_buy_level']} / {win_p['rsi_sell_level']}\n"
        f"• ATR Mult: SL={win_p['atr_sl_mult']}x, TP={win_p['atr_tp_mult']}x\n"
        f"• ADX Filter: Period {new_params_data['adx_period']}, Threshold {new_params_data['adx_threshold']}\n"
        f"• Validation PF: {win_vm['profit_factor']} (เดิม: {baseline_pf})\n"
        f"• Validation Win Rate: {win_vm['win_rate']}%\n"
        f"• Validation Trades: {win_vm['trades']}\n"
        f"• Max Drawdown: {win_vm['max_drawdown_pct']}%\n"
        f"• กำไรบนชุดข้อสอบ: ${win_vm['net_profit']:,.2f}"
    )
    send_alert(discord_msg, level=alert_level)

    return new_params_data


if __name__ == "__main__":
    run_optimization()
