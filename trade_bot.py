"""
trade_bot.py — Linux Trading Engine (MetaApi Cloud MT5 Edition)
- Headless MetaTrader 5 connectivity on Linux via MetaApi Cloud SDK
- Pure Python & WebSockets execution (no local MT5 desktop app required)
- Signal generation & technical indicators loaded from strategy.py
- Processes closed bars only to avoid repainting
- Risk controls: Daily Loss Limit, ATR Position Sizing, Max Open Trades
- Complete audit logging to logs/trades.csv and logs/events.csv
- Graceful offline fallback when METAAPI_TOKEN is pending configuration
"""
import asyncio
import csv
import datetime
import json
import math
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

import pandas as pd

try:
    from metaapi_cloud_sdk import MetaApi
    METAAPI_SDK_AVAILABLE = True
except ImportError:
    METAAPI_SDK_AVAILABLE = False

from mt5_docker_adapter import DockerMT5Connection, is_docker_mt5_available
import strategy
from notifier import send_alert, send_ai_error_alert
from settings import (
    BOT_DIR,
    LOG_DIR,
    STATE_DIR,
    get_metaapi_instructions,
    is_metaapi_configured,
    load_config,
    load_params,
    load_trading_control,
    secret,
)

# Map MT5 timeframe strings to MetaApi candle format
TIMEFRAME_MAP = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}


def get_val(obj, key, default=None):
    """Safely extract key from either dict or MetaApi SDK object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def update_heartbeat():
    """Record heartbeat timestamp for overseer monitoring."""
    hb_file = STATE_DIR / "heartbeat_trade_bot.txt"
    try:
        hb_file.write_text(datetime.datetime.now().isoformat(), encoding="utf-8")
    except Exception:
        pass


def log_event(source: str, level: str, message: str):
    """Log system event to logs/events.csv."""
    events_file = LOG_DIR / "events.csv"
    file_exists = events_file.exists()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with open(events_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "source", "level", "message"])
            writer.writerow([now_str, source, level, message])
    except Exception as e:
        print(f"[Trade Bot] Failed to write events.csv: {e}")


def log_trade(event: str, ticket: str | int, side: str, price: float, lot: float,
              sl: float, tp: float, profit: float, balance: float,
              version: int, note: str):
    """Log trade execution or exit to logs/trades.csv."""
    trades_file = LOG_DIR / "trades.csv"
    file_exists = trades_file.exists()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with open(trades_file, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "timestamp", "event", "ticket", "side", "price", "lot",
                    "sl", "tp", "profit", "balance", "strategy_version", "note"
                ])
            writer.writerow([
                now_str, event, str(ticket), side,
                round(price, 3) if price else "",
                round(lot, 2) if lot else "",
                round(sl, 3) if sl else "",
                round(tp, 3) if tp else "",
                round(profit, 2) if profit != "" and profit is not None else "",
                round(balance, 2) if balance else "",
                version, note
            ])
    except Exception as e:
        print(f"[Trade Bot] Failed to write trades.csv: {e}")


def get_sentiment(max_age_hours: float = 2.0) -> float:
    """Read latest sentiment score from state/sentiment.json."""
    sentiment_file = STATE_DIR / "sentiment.json"
    if not sentiment_file.exists():
        return 0.0

    try:
        data = json.loads(sentiment_file.read_text(encoding="utf-8"))
        ts_str = data.get("timestamp")
        if not ts_str:
            return 0.0
        ts = datetime.datetime.fromisoformat(ts_str)
        age = (datetime.datetime.now() - ts).total_seconds() / 3600.0
        if age > max_age_hours:
            return 0.0
        return float(data.get("score", 0.0))
    except Exception:
        return 0.0


def is_market_open(symbol: str, price_dict: dict | None) -> bool:
    """Check if market is open and streaming valid quote prices."""
    if not price_dict:
        return False
    bid = float(get_val(price_dict, "bid", 0.0))
    ask = float(get_val(price_dict, "ask", 0.0))
    if bid <= 0 and ask <= 0:
        return False

    # Check weekend market close for gold and FX
    is_crypto = any(c in symbol.upper() for c in ("BTC", "ETH", "SOL", "XRP", "USDT", "CRYPTO"))
    if not is_crypto:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if now_utc.weekday() == 5 or (now_utc.weekday() == 6 and now_utc.hour < 21):
            return False

    return True


def calculate_lot_size(balance: float, risk_percent: float, entry_price: float, sl_price: float, spec) -> float:
    """Calculate position lot size based on Stop Loss distance and account risk percentage."""
    min_vol = float(get_val(spec, "minVolume", 0.01) or 0.01)
    max_vol = float(get_val(spec, "maxVolume", 100.0) or 100.0)
    step = float(get_val(spec, "volumeStep", 0.01) or 0.01)
    contract_size = float(get_val(spec, "contractSize", 100.0) or 100.0)

    sl_dist = abs(entry_price - sl_price)
    if sl_dist <= 0:
        return min_vol

    risk_usd = balance * (risk_percent / 100.0)
    raw_lot = risk_usd / (sl_dist * contract_size)

    lot = math.floor(raw_lot / step) * step
    lot = max(min_vol, min(lot, max_vol))

    step_str = f"{step:.8f}".rstrip("0")
    decimals = len(step_str.split(".")[1]) if "." in step_str else 0
    return round(lot, decimals)


async def get_daily_loss(connection, magic: int) -> float:
    """Calculate net profit/loss for today since 00:00:00 UTC, respecting reset marker."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        start_of_day = datetime.datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=datetime.timezone.utc)

        # ตรวจสอบว่ามีการบันทึกเวลารีเซ็ตขาดทุนรายวัน (เช่น กด /resume หรือรีเซ็ตด้วยมือ)
        reset_file = STATE_DIR / "daily_loss_reset.json"
        if reset_file.exists():
            try:
                data = json.loads(reset_file.read_text(encoding="utf-8"))
                reset_ts = datetime.datetime.fromisoformat(data.get("reset_at")).astimezone(datetime.timezone.utc)
                if reset_ts.date() == now.date() and reset_ts > start_of_day:
                    start_of_day = reset_ts
            except Exception:
                pass

        deals_resp = await connection.get_deals_by_time_range(start_of_day, now)
        deals = get_val(deals_resp, "deals", [])

        total_pnl = 0.0
        for d in deals:
            d_magic = get_val(d, "magic")
            d_entry = get_val(d, "entryType")
            if str(d_magic) == str(magic) and d_entry == "DEAL_ENTRY_OUT":
                p = float(get_val(d, "profit", 0.0))
                c = float(get_val(d, "commission", 0.0))
                s = float(get_val(d, "swap", 0.0))
                total_pnl += (p + c + s)
        return total_pnl
    except Exception as e:
        print(f"[Trade Bot] Daily loss calculation error: {e}")
        return 0.0


async def sync_closed_deals(connection, symbol: str, magic: int, current_version: int, acc_tag: str):
    """
    Inspect closed deals in MetaApi and record to logs/trades.csv,
    deduplicating with state/processed_deals.json.
    """
    processed_file = STATE_DIR / "processed_deals.json"
    processed_tickets = set()
    if processed_file.exists():
        try:
            processed_tickets = set(json.loads(processed_file.read_text(encoding="utf-8")))
        except Exception:
            pass

    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        from_date = now - datetime.timedelta(days=7)
        deals_resp = await connection.get_deals_by_time_range(from_date, now)
        deals = get_val(deals_resp, "deals", [])

        if not deals:
            return

        new_processed = False
        acc_info = await connection.get_account_information()
        balance = float(get_val(acc_info, "balance", 0.0))

        for d in deals:
            ticket = str(get_val(d, "id", ""))
            d_magic = get_val(d, "magic")
            d_entry = get_val(d, "entryType")

            if str(d_magic) != str(magic) or d_entry != "DEAL_ENTRY_OUT":
                continue
            if ticket in processed_tickets:
                continue

            d_type = get_val(d, "type", "")
            side = "BUY" if "SELL" in d_type else "SELL"
            price = float(get_val(d, "price", 0.0))
            vol = float(get_val(d, "volume", 0.0))
            profit = float(get_val(d, "profit", 0.0)) + float(get_val(d, "commission", 0.0)) + float(get_val(d, "swap", 0.0))
            order_id = str(get_val(d, "orderId", ticket))

            log_trade(
                event="CLOSE",
                ticket=ticket,
                side=side,
                price=price,
                lot=vol,
                sl=0.0,
                tp=0.0,
                profit=profit,
                balance=balance,
                version=current_version,
                note=f"MetaApi Deal closed (Order #{order_id})"
            )

            icon = "🟢 กำไร" if profit >= 0 else "🔴 ขาดทุน"
            msg = (
                f"**{acc_tag}ปิดออเดอร์ ({icon}) [MT5 Live]**\n"
                f"• Ticket: `{ticket}`\n"
                f"• ราคาปิด: `{price}`\n"
                f"• กำไร/ขาดทุน: `${profit:,.2f}`\n"
                f"• บาลานซ์คงเหลือ: `${balance:,.2f}`"
            )
            send_alert(msg, level="TRADE")
            processed_tickets.add(ticket)
            new_processed = True

        if new_processed:
            processed_file.write_text(json.dumps(list(processed_tickets)), encoding="utf-8")
    except Exception as e:
        print(f"[Trade Bot] sync_closed_deals error: {e}")


async def place_order(connection, symbol: str, signal: str, lot: float, sl: float, tp: float,
                      magic: int, version: int, passed_validation: bool, acc_tag: str, spec) -> bool:
    """Send market entry order to MetaTrader via MT5 Live."""
    try:
        digits = int(get_val(spec, "digits", 2))
        sl = round(sl, digits)
        tp = round(tp, digits)
        comment = f"strat_v{version}"

        trade_options = {
            "comment": comment,
            "magic": str(magic),
        }

        print(f"[Trade Bot] 🚀 Sending {signal} order for {symbol} {lot} Lot (SL: {sl}, TP: {tp})...")
        if signal == "BUY":
            result = await connection.create_market_buy_order(
                symbol=symbol,
                volume=lot,
                stop_loss=sl,
                take_profit=tp,
                options=trade_options
            )
        else:
            result = await connection.create_market_sell_order(
                symbol=symbol,
                volume=lot,
                stop_loss=sl,
                take_profit=tp,
                options=trade_options
            )

        order_id = get_val(result, "orderId") or get_val(result, "positionId") or "N/A"
        price_info = await connection.get_symbol_price(symbol)
        price = get_val(price_info, "ask" if signal == "BUY" else "bid", 0.0)

        acc = await connection.get_account_information()
        balance = float(get_val(acc, "balance", 0.0))

        log_trade("OPEN", order_id, signal, price, lot, sl, tp, 0.0, balance, version, f"MT5 #{order_id}")
        prefix = "" if passed_validation else "⚠️ [ใช้สูตรที่ยังไม่ผ่านเกณฑ์]\n"
        msg = (
            f"{prefix}✅ **{acc_tag}เปิดออเดอร์ใหม่: {signal} {symbol} [MT5 Live]**\n"
            f"• Order ID: `{order_id}`\n"
            f"• Lot Size: `{lot}`\n"
            f"• ราคาเปิด: `{price}`\n"
            f"• Stop Loss: `{sl}`\n"
            f"• Take Profit: `{tp}`\n"
            f"• Version: v{version}"
        )
        send_alert(msg, level="TRADE")
        return True

    except Exception as e:
        err_msg = f"Order failed: {e}"
        print(f"[Trade Bot] ❌ {err_msg}")
        log_event("trade_bot", "ERROR", err_msg)
        send_alert(f"🚨 ส่งออเดอร์ไม่สำเร็จ: {err_msg}", level="ERROR")
        send_ai_error_alert(str(e), context="MetaApi Place Order")
        return False


async def connect_metaapi():
    """Connect to local Docker MT5 container (Option 2: 0 Baht) or MetaApi Cloud RPC."""
    if is_docker_mt5_available():
        print("[Trade Bot] 🐳 ตรวจพบ MT5 Docker Container บนเครื่อง! กำลังเชื่อมต่อผ่าน RPyC (ฟรี 0 บาท)...")
        conn = DockerMT5Connection()
        await conn.connect()
        await conn.wait_synchronized()

        acc_info = await conn.get_account_information()
        balance = float(get_val(acc_info, "balance", 0.0))
        login_num = get_val(acc_info, "login", "113645327")
        server_name = get_val(acc_info, "server", "MetaQuotes-Demo")
        acc_type = get_val(acc_info, "type", "DEMO")
        account_tag = "[🔴 REAL พอร์ตจริง] " if "REAL" in str(acc_type).upper() else "[🎮 DEMO พอร์ตจำลอง] "

        print(f"[Trade Bot] ✅ Docker MT5 connected! Account: {login_num} ({server_name}), Balance: ${balance:,.2f}")
        log_event("trade_bot", "INFO", f"Connected to Docker MT5 account {login_num} ({server_name})")
        send_alert(
            f"✅ **{account_tag}เชื่อมต่อ MT5 (Docker Ubuntu) สำเร็จ!**\n"
            f"• หมายเลขบัญชี: `{login_num}` ({server_name})\n"
            f"• ยอดเงินในพอร์ต: `${balance:,.2f}`\n"
            f"• สภาพแวดล้อม: Linux Ubuntu + Docker MT5 (ฟรี 0 บาท)\n"
            f"• สถานะระบบ: บอทพร้อมเทรดอัตโนมัติ 🟢",
            level="INFO"
        )
        return conn, conn, conn, account_tag

    if not is_metaapi_configured():
        return None, None, None, ""

    token = secret("METAAPI_TOKEN")
    acc_id = secret("METAAPI_ACCOUNT_ID")
    print(f"[Trade Bot] 🔑 Found MetaApi token! Connecting to cloud account `{acc_id}`...")
    api = MetaApi(token=token)
    account = await api.metatrader_account_api.get_account(acc_id)

    if account.state != "DEPLOYED":
        print("[Trade Bot] 🔄 Deploying account on cloud server...")
        await account.deploy()

    print("[Trade Bot] ⏳ Establishing RPC connection and synchronizing...")
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    acc_info = await connection.get_account_information()
    balance = float(get_val(acc_info, "balance", 0.0))
    server_name = get_val(acc_info, "server", "Unknown")
    acc_type = get_val(acc_info, "type", "DEMO")
    account_tag = "[🔴 REAL พอร์ตจริง] " if "REAL" in str(acc_type).upper() else "[🎮 DEMO พอร์ตจำลอง] "

    print(f"[Trade Bot] ✅ MetaApi connected! Balance: ${balance:,.2f} on {server_name}")
    log_event("trade_bot", "INFO", f"Connected to MetaApi account {acc_id} ({server_name})")
    send_alert(
        f"✅ **{account_tag}เชื่อมต่อ MetaApi MT5 สำเร็จ!**\n"
        f"• หมายเลขบัญชี: `{acc_id}` ({server_name})\n"
        f"• ยอดเงินในพอร์ต: `${balance:,.2f}`\n"
        f"• สภาพแวดล้อม: Linux Ubuntu (Headless Cloud)\n"
        f"• สถานะระบบ: บอทพร้อมเทรดอัตโนมัติ 🟢",
        level="INFO"
    )
    return api, account, connection, account_tag


async def run_bot_loop():
    """Main asynchronous trading loop on Linux."""
    print("=" * 65)
    print("  🚀 AI Trading Bot 2 (Linux Ubuntu + Docker MT5 Edition) เริ่มทำงาน")
    print("=" * 65)

    api = None
    account = None
    connection = None
    account_tag = "[Docker MT5] "
    notified_token_missing = False

    # โหลดการตั้งค่าเริ่มต้น
    cfg = load_config()
    t_cfg = cfg.get("trading", {})
    symbol = t_cfg.get("symbol", "XAUUSD")
    tf_str = t_cfg.get("timeframe", "M15")
    magic = int(t_cfg.get("magic_number", 123456))
    max_open = int(t_cfg.get("max_open_trades", 1))
    daily_loss_limit = float(t_cfg.get("daily_loss_limit_percent", 3.0))
    loop_sec = int(t_cfg.get("loop_seconds", 30))

    last_bar_file = STATE_DIR / "last_bar.txt"
    last_logged_closed = False
    daily_halted_day = -1
    was_paused = False

    try:
        while True:
            try:
                update_heartbeat()
                load_dotenv(BOT_DIR / ".env", override=True)

                if not is_docker_mt5_available() and not is_metaapi_configured():
                    if not notified_token_missing:
                        print("\n" + "=" * 65)
                        print("[Trade Bot] ⚠️ ไม่พบ Docker MT5 และยังไม่ได้ตั้งค่า METAAPI_TOKEN ใน .env")
                        print(get_metaapi_instructions())
                        print("=" * 65)
                        log_event("trade_bot", "WAIT_TOKEN", "Waiting for Docker MT5 or MetaApi credentials")
                        send_alert(
                            "⚠️ **[Trade Bot แจ้งเตือน]** ระบบพร้อมทำงาน แต่กำลังรอการเชื่อมต่อ Docker MT5 หรือ `METAAPI_TOKEN`\n"
                            + get_metaapi_instructions(),
                            level="WARNING"
                        )
                        notified_token_missing = True

                    await asyncio.sleep(15)
                    continue

                # เชื่อมต่อหรือ Reconnect เมื่อ connection หลุด
                if connection is None or getattr(connection, "_closed", False):
                    try:
                        api, account, connection, account_tag = await connect_metaapi()
                        notified_token_missing = False
                    except Exception as e:
                        err = f"ไม่สามารถเชื่อมต่อ MetaApi: {e}"
                        print(f"[Trade Bot] ❌ {err}")
                        log_event("trade_bot", "WARNING", err)
                        send_alert(f"🚨 **[MetaApi Error]** {err}\nระบบจะพยายามเชื่อมต่อใหม่ใน 15 วินาที...", level="WARNING")
                        connection = None
                        await asyncio.sleep(15)
                        continue

                    if connection is None:
                        await asyncio.sleep(15)
                        continue

                # โหลดการตั้งค่าล่าสุด
                cfg = load_config()
                t_cfg = cfg.get("trading", {})
                current_symbol = t_cfg.get("symbol", "XAUUSD")
                current_tf_str = t_cfg.get("timeframe", "M15")
                magic = int(t_cfg.get("magic_number", 123456))
                max_open = int(t_cfg.get("max_open_trades", 1))
                daily_loss_limit = float(t_cfg.get("daily_loss_limit_percent", 3.0))
                loop_sec = int(t_cfg.get("loop_seconds", 30))

                if current_symbol != symbol or current_tf_str != tf_str:
                    print(f"[Trade Bot] 🔄 อัปเดตการตั้งค่า: {symbol} {tf_str} -> {current_symbol} {current_tf_str}")
                    log_event("trade_bot", "CONFIG_CHANGE", f"Switched to {current_symbol} {current_tf_str}")
                    send_alert(f"🔄 **[Trade Bot]** สลับสินทรัพย์/ไทม์เฟรมเป็น `{current_symbol}` `{current_tf_str}` สำเร็จ", level="INFO")
                    symbol = current_symbol
                    tf_str = current_tf_str
                    last_logged_closed = False
                    if last_bar_file.exists():
                        try:
                            last_bar_file.unlink()
                        except Exception:
                            pass

                params = load_params()
                version = params.get("version", 5)
                passed_validation = params.get("passed_validation", True)

                # ตรวจสอบการพักเทรด (Discord Pause/Resume)
                trading_ctrl = load_trading_control()
                trading_enabled = trading_ctrl.get("trading_enabled", True)
                if not trading_enabled:
                    if not was_paused:
                        pause_reason = trading_ctrl.get("reason", "พักการเทรดโดยผู้ใช้")
                        print(f"[Trade Bot] ⏸️ บอทหยุดพักการเปิดออเดอร์ชั่วคราว: {pause_reason}")
                        log_event("trade_bot", "PAUSED", f"Trading paused: {pause_reason}")
                        send_alert(f"⏸️ **[Trade Bot]** หยุดพักการเปิดออเดอร์ชั่วคราว: {pause_reason}", level="WARNING")
                        was_paused = True

                    await sync_closed_deals(connection, symbol, magic, version, account_tag)
                    await asyncio.sleep(loop_sec)
                    continue
                else:
                    if was_paused:
                        resume_reason = trading_ctrl.get("reason", "กลับมาเปิดเทรดอัตโนมัติ")
                        print(f"[Trade Bot] ▶️ บอทกลับมาทำการวิเคราะห์ตามปกติ: {resume_reason}")
                        log_event("trade_bot", "RESUMED", f"Trading resumed: {resume_reason}")
                        send_alert(f"▶️ **[Trade Bot]** กลับมาทำการวิเคราะห์ตามปกติ: {resume_reason}", level="INFO")
                        was_paused = False

                # 1. ตรวจสอบราคาตลาดสดและสภาวะตลาด
                price_info = None
                try:
                    price_info = await connection.get_symbol_price(symbol)
                except Exception as e:
                    print(f"[Trade Bot] ดึงราคา {symbol} ไม่สำเร็จ: {e}")

                if not is_market_open(symbol, price_info):
                    if not last_logged_closed:
                        log_event("trade_bot", "MARKET_CLOSED", f"ตลาด {symbol} ปิดทำการ หรืออยู่นอกเวลาเทรด")
                        print(f"[Trade Bot] ⏸ ตลาด {symbol} ปิดทำการ หรืออยู่นอกเวลาเทรด")
                        last_logged_closed = True
                    await asyncio.sleep(loop_sec)
                    continue
                else:
                    last_logged_closed = False

                # 2. ซิงก์ประวัติออเดอร์ปิด
                await sync_closed_deals(connection, symbol, magic, version, account_tag)

                # 3. ตรวจสอบ Daily Loss Limit
                acc = await connection.get_account_information()
                balance = float(get_val(acc, "balance", 10000.0))
                loss_today = await get_daily_loss(connection, magic)
                max_allowed_loss = balance * (daily_loss_limit / 100.0)
                today_int = datetime.datetime.now().day

                if loss_today <= -max_allowed_loss:
                    if daily_halted_day != today_int:
                        halt_msg = f"🛑 ขาดทุนวันนี้ถึงขีดจำกัด (${abs(loss_today):,.2f} >= {daily_loss_limit}%) หยุดเปิดออเดอร์ใหม่ตลอดวัน"
                        print(f"[Trade Bot] {halt_msg}")
                        log_event("trade_bot", "HALT", halt_msg)
                        send_alert(halt_msg, level="WARNING")
                        daily_halted_day = today_int
                    await asyncio.sleep(loop_sec)
                    continue
                else:
                    daily_halted_day = None

                # 4. ดึงแท่งเทียนย้อนหลังผ่าน MetaApi
                meta_tf = TIMEFRAME_MAP.get(tf_str, "15m")
                candles = await account.get_historical_candles(symbol, meta_tf, limit=300)

                if not candles or len(candles) < 50:
                    await asyncio.sleep(loop_sec)
                    continue

                candle_dicts = []
                for c in candles:
                    t_val = get_val(c, "time")
                    if isinstance(t_val, str):
                        t_val = pd.to_datetime(t_val)
                    candle_dicts.append({
                        "time": t_val,
                        "open": float(get_val(c, "open", 0.0)),
                        "high": float(get_val(c, "high", 0.0)),
                        "low": float(get_val(c, "low", 0.0)),
                        "close": float(get_val(c, "close", 0.0)),
                        "tick_volume": float(get_val(c, "tickVolume", 0.0)),
                    })

                df = pd.DataFrame(candle_dicts)
                df.sort_values("time", inplace=True)
                df.reset_index(drop=True, inplace=True)

                # ตัดแท่งล่าสุดที่ยังไม่ปิดทิ้ง (ใช้เฉพาะแท่งที่ปิดสมบูรณ์แล้ว)
                closed_df = df.iloc[:-1].copy()
                last_closed_bar = closed_df.iloc[-1]
                last_closed_time = str(last_closed_bar["time"])

                prev_closed_time = ""
                if last_bar_file.exists():
                    try:
                        prev_closed_time = last_bar_file.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass

                if last_closed_time == prev_closed_time:
                    # ยังไม่มีแท่งใหม่ปิด รอแท่งถัดไป
                    await asyncio.sleep(loop_sec)
                    continue

                # บันทึกว่าแท่งนี้ได้รับการวิเคราะห์แล้ว
                last_bar_file.write_text(last_closed_time, encoding="utf-8")
                print(f"\n[Trade Bot] 📊 ตรวจพบแท่งใหม่ปิดสมบูรณ์: {last_closed_time}")

                # เติมอินดิเคเตอร์
                closed_df = strategy.add_indicators(closed_df, params)
                sentiment_score = get_sentiment(max_age_hours=float(cfg.get("news", {}).get("sentiment_max_age_hours", 2)))

                # ส่งเฉพาะแถวท้ายเพื่อ generate signal
                signal = strategy.generate_signal(closed_df.iloc[-5:], params, sentiment=sentiment_score)

                if signal in ("BUY", "SELL"):
                    print(f"[Trade Bot] 🎯 พบสัญญาณเทรด: {signal} (Sentiment: {sentiment_score})")

                    # ตรวจสอบจำนวนไม้ที่เปิดอยู่
                    positions = await connection.get_positions()
                    open_trades = [p for p in positions if str(get_val(p, "magic")) == str(magic)] if positions else []

                    if len(open_trades) >= max_open:
                        print(f"[Trade Bot] ⚠️ มีออเดอร์เปิดอยู่แล้ว {len(open_trades)}/{max_open} ไม้ ข้ามการเปิดใหม่")
                        log_event("trade_bot", "WAIT", f"Signal {signal} skipped due to max open trades reached")
                    else:
                        spec = await connection.get_symbol_specification(symbol)
                        p_info = await connection.get_symbol_price(symbol)
                        current_price = float(get_val(p_info, "ask" if signal == "BUY" else "bid", 0.0))
                        atr_val = float(closed_df.iloc[-1]["ATR"]) if not pd.isna(closed_df.iloc[-1]["ATR"]) else 5.0

                        sl, tp = strategy.sl_tp(signal, current_price, atr_val, params)
                        lot = calculate_lot_size(
                            balance=balance,
                            risk_percent=float(t_cfg.get("risk_percent", 1.0)),
                            entry_price=current_price,
                            sl_price=sl,
                            spec=spec
                        )

                        await place_order(
                            connection=connection,
                            symbol=symbol,
                            signal=signal,
                            lot=lot,
                            sl=sl,
                            tp=tp,
                            magic=magic,
                            version=version,
                            passed_validation=passed_validation,
                            acc_tag=account_tag,
                            spec=spec
                        )
                else:
                    print(f"[Trade Bot] ⏳ แท่ง {last_closed_time} ไม่มีสัญญาณเข้าเงื่อนไข (WAIT)")
                    log_event("trade_bot", "WAIT", f"Bar {last_closed_time} closed without signal")

                    # Market Pulse Notification
                    digits = 2
                    try:
                        spec = await connection.get_symbol_specification(symbol)
                        digits = int(get_val(spec, "digits", 2))
                    except Exception:
                        pass

                    p_info = await connection.get_symbol_price(symbol)
                    cur_price = float(get_val(p_info, "bid", closed_df.iloc[-1]["close"]))
                    sma_val = closed_df.iloc[-1].get("SMA", 0.0)
                    rsi_val = closed_df.iloc[-1].get("RSI", 0.0)
                    adx_val = closed_df.iloc[-1].get("ADX", 0.0)

                    wait_reason = "ตลาดไซด์เวย์ ADX < 25" if signal == "WAIT" else "ยังไม่เข้าเงื่อนไขกลยุทธ์"
                    time_str = last_closed_bar["time"].strftime("%H:%M") if hasattr(last_closed_bar["time"], "strftime") else str(last_closed_time)

                    pulse_msg = (
                        f"📊 **{account_tag}[{symbol} {tf_str} Market Pulse]** แท่ง {time_str} | "
                        f"ราคา: `{cur_price:,.{digits}f}` | "
                        f"SMA: `{sma_val:,.{digits}f}` | "
                        f"RSI: `{rsi_val:.1f}` | "
                        f"ADX: `{adx_val:.1f}` | "
                        f"ข่าว: `{sentiment_score:+.2f}` | "
                        f"สถานะ: ⏳ รอจังหวะ ({wait_reason})"
                    )
                    send_alert(pulse_msg, level="INFO")

            except Exception as e:
                err = f"เกิดข้อผิดพลาดในลูป trade_bot: {e}"
                print(f"[Trade Bot ERROR] {err}")
                err_lower = str(e).lower()
                is_conn_error = any(kw in err_lower for kw in ("connect", "socket", "timeout", "network", "synchroniz", "closed", "rpc"))
                if is_conn_error:
                    print("[Trade Bot] ⚠️ ตรวจพบปัญหาการเชื่อมต่อ MetaApi กำลังเตรียม Reconnect ในรอบถัดไป...")
                    log_event("trade_bot", "DISCONNECT", str(e))
                    if connection is not None:
                        try:
                            await connection.close()
                        except Exception:
                            pass
                    connection = None
                else:
                    log_event("trade_bot", "CRASH", err)
                    send_ai_error_alert(str(e), context="Trade Bot Linux Main Loop")

            await asyncio.sleep(loop_sec)
    finally:
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass


def main():
    try:
        asyncio.run(run_bot_loop())
    except KeyboardInterrupt:
        print("\n[Trade Bot] 🛑 หยุดการทำงานโดยผู้ใช้")


if __name__ == "__main__":
    main()
