"""
discord_bot.py — Discord Slash Commands Gateway (Linux & Cloud MetaApi Edition)
Commands supported:
  /simulate [bars: int = 12000] : Run historical backtest and report embed
  /status                      : Display system health, prices, indicators, and sentiment
  /positions                   : View active MT5 positions and floating PnL
  /buy [lot: float = None]     : Place live market BUY order with auto SL/TP
  /sell [lot: float = None]    : Place live market SELL order with auto SL/TP
  /close [ticket: str = None]  : Close open positions or cancel pending orders
  /pause [reason: str]         : Temporarily pause automated trade execution
  /resume [reason: str]        : Resume automated strategy execution
  /config                      : Inspect current symbol, timeframe, and parameters
  /set_timeframe <tf>          : Change analysis timeframe (M1, M5, M15, M30, H1, H4, D1)
  /set_symbol <symbol>         : Change trading asset symbol (e.g. XAUUSD, BTCUSD)
  /set_risk <risk: float>      : Update risk percent per trade
  /help                        : Show interactive command reference
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

import discord
from discord import app_commands
from discord.ext import commands, tasks

import pandas as pd
import strategy
import simulator
from notifier import send_alert
from settings import (
    BOT_DIR,
    LOG_DIR,
    STATE_DIR,
    DATA_DIR,
    load_config,
    save_config,
    load_params,
    save_params,
    load_trading_control,
    save_trading_control,
    is_metaapi_configured,
    get_metaapi_instructions,
    secret,
)

try:
    from metaapi_cloud_sdk import MetaApi
    METAAPI_SDK_AVAILABLE = True
except ImportError:
    METAAPI_SDK_AVAILABLE = False

from mt5_docker_adapter import DockerMT5Connection, is_docker_mt5_available


def update_heartbeat():
    """Record heartbeat timestamp for overseer monitoring."""
    hb_file = STATE_DIR / "heartbeat_discord_bot.txt"
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
        print(f"[Discord Bot] Failed to write events.csv: {e}")


def get_latest_sentiment_data() -> dict:
    """Read latest sentiment analysis from state/sentiment.json."""
    sentiment_file = STATE_DIR / "sentiment.json"
    if not sentiment_file.exists():
        return {
            "score": 0.0,
            "reasoning": "ยังไม่มีข้อมูล Sentiment ในระบบ (news_agent ยังไม่ได้วิเคราะห์)",
            "age_str": "ไม่มีข้อมูล",
            "provider": "N/A"
        }

    try:
        data = json.loads(sentiment_file.read_text(encoding="utf-8"))
        score = float(data.get("score", 0.0))
        reasoning = data.get("reasoning", "ไม่มีรายละเอียด")
        provider = data.get("provider", "Groq AI")
        ts_str = data.get("timestamp")

        age_str = "ไม่ทราบเวลา"
        if ts_str:
            ts = datetime.datetime.fromisoformat(ts_str)
            diff = datetime.datetime.now() - ts
            diff_sec = diff.total_seconds()
            if diff_sec < 0:
                age_str = "เมื่อสักครู่"
            else:
                minutes = int(diff_sec // 60)
                if minutes < 60:
                    age_str = f"{minutes} นาทีที่แล้ว"
                else:
                    hours = minutes // 60
                    if hours < 24:
                        age_str = f"{hours} ชั่วโมง {minutes % 60} นาทีที่แล้ว"
                    else:
                        days = hours // 24
                        age_str = f"{days} วันที่แล้ว"

        return {
            "score": score,
            "reasoning": reasoning,
            "age_str": age_str,
            "provider": provider
        }
    except Exception as e:
        return {
            "score": 0.0,
            "reasoning": f"เกิดข้อผิดพลาดในการอ่าน sentiment: {e}",
            "age_str": "เกิดข้อผิดพลาด",
            "provider": "N/A"
        }


def get_heartbeat_status() -> dict:
    """Read heartbeat status of all background services."""
    now = datetime.datetime.now()
    services = {
        "trade_bot": (STATE_DIR / "heartbeat_trade_bot.txt", 180),
        "news_agent": (STATE_DIR / "heartbeat_news_agent.txt", 2400),
        "overseer": (STATE_DIR / "heartbeat_overseer.txt", 1200),
        "discord_bot": (STATE_DIR / "heartbeat_discord_bot.txt", 120),
    }

    status = {}
    for name, (path, max_sec) in services.items():
        if not path.exists():
            status[name] = "⚪ ยังไม่เคยเริ่ม"
            continue
        try:
            ts_str = path.read_text(encoding="utf-8").strip()
            ts = datetime.datetime.fromisoformat(ts_str)
            age = (now - ts).total_seconds()
            if age <= max_sec:
                status[name] = "🟢 ปกติ"
            else:
                mins = int(age // 60)
                status[name] = f"⚠️ ขาดการติดต่อ ({mins} นาทีที่แล้ว)"
        except Exception:
            status[name] = "❓ ไม่ทราบสถานะ"

    return status


def run_simulation_sync(bars: int) -> dict:
    """Synchronous backtest execution helper for executor."""
    cfg = load_config()
    b_cfg = cfg.get("backtest", {})
    t_cfg = cfg.get("trading", {})
    params = load_params()

    symbol = t_cfg.get("symbol", "XAUUSD")
    tf = t_cfg.get("timeframe", "M15")

    history_df = simulator.load_history(symbol=symbol, timeframe=tf, bars=bars)
    if history_df is None or history_df.empty:
        raise RuntimeError(f"Missing historical candle data for {symbol} {tf}")

    start_str = str(history_df.iloc[0]["time"])
    end_str = str(history_df.iloc[-1]["time"])

    metrics = simulator.run_backtest(history_df, params, strategy, b_cfg, export_csv=True)
    return {
        "metrics": metrics,
        "symbol": symbol,
        "timeframe": tf,
        "version": params.get("version", 5),
        "start_str": start_str,
        "end_str": end_str,
        "bars_loaded": len(history_df),
    }


def get_market_and_indicators_offline() -> dict:
    """Get latest prices and indicators from local data cache (offline mode)."""
    cfg = load_config()
    t_cfg = cfg.get("trading", {})
    symbol = t_cfg.get("symbol", "XAUUSD")
    tf_str = t_cfg.get("timeframe", "M15")
    params = load_params()

    try:
        df = simulator.load_history(symbol=symbol, timeframe=tf_str, bars=300)
        df_ind = strategy.add_indicators(df, params)
        last_bar = df_ind.iloc[-1]
        close_price = float(last_bar["close"])
        sma_val = float(last_bar.get("SMA", 0.0))
        rsi_val = float(last_bar.get("RSI", 0.0))
        atr_val = float(last_bar.get("ATR", 0.0))
        adx_val = float(last_bar.get("ADX", 0.0))
        time_str = str(last_bar["time"])

        return {
            "ok": True,
            "offline": True,
            "symbol": symbol,
            "timeframe": tf_str,
            "bid": close_price,
            "ask": close_price + 0.30,
            "close": close_price,
            "sma": sma_val,
            "rsi": rsi_val,
            "atr": atr_val,
            "adx": adx_val,
            "time_str": time_str,
            "digits": 2,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_val(obj, key, default=None):
    """Safely extract attribute or dictionary item from MetaApi SDK response."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# -------------------------------------------------------------
# MetaApi Connection Cache for Discord Bot
# -------------------------------------------------------------
_metaapi_client = None
_metaapi_conn = None


async def get_metaapi_connection(force_reconnect: bool = False):
    """Reusable Docker MT5 / MetaApi RPC connection manager with reconnect logic."""
    global _metaapi_client, _metaapi_conn

    if is_docker_mt5_available():
        if _metaapi_conn is not None and not force_reconnect:
            if not getattr(_metaapi_conn, "_closed", False):
                return True, _metaapi_client, _metaapi_conn
        try:
            conn = DockerMT5Connection()
            await conn.connect()
            await conn.wait_synchronized()
            _metaapi_client = conn
            _metaapi_conn = conn
            return True, _metaapi_client, conn
        except Exception as e:
            print(f"[Discord Bot] การเชื่อมต่อ Docker MT5 ผิดพลาด: {e}")
            _metaapi_conn = None
            return False, None, None

    if not METAAPI_SDK_AVAILABLE or not is_metaapi_configured():
        return False, None, None

    if _metaapi_conn is not None and not force_reconnect:
        if not getattr(_metaapi_conn, "_closed", False):
            return True, _metaapi_client, _metaapi_conn
        else:
            _metaapi_conn = None

    try:
        token = secret("METAAPI_TOKEN")
        acc_id = secret("METAAPI_ACCOUNT_ID")
        _metaapi_client = MetaApi(token=token)
        acc = await _metaapi_client.metatrader_account_api.get_account(acc_id)
        if acc.state != "DEPLOYED":
            await acc.deploy()
        conn = acc.get_rpc_connection()
        await conn.connect()
        await conn.wait_synchronized()
        _metaapi_conn = conn
        return True, _metaapi_client, conn
    except Exception as e:
        print(f"[Discord Bot] การเชื่อมต่อ MetaApi ผิดพลาด: {e}")
        _metaapi_conn = None
        return False, None, None


# -------------------------------------------------------------
# Discord Bot Setup & Slash Commands
# -------------------------------------------------------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
_ready_announced = False


@tasks.loop(seconds=30)
async def heartbeat_loop():
    update_heartbeat()


@bot.event
async def on_ready():
    global _ready_announced
    update_heartbeat()
    if not heartbeat_loop.is_running():
        heartbeat_loop.start()

    print(f"[Discord Bot] ✅ เชื่อมต่อ Discord สำเร็จ! ล็อกอินในนาม: {bot.user} (ID: {bot.user.id})")

    if not _ready_announced:
        _ready_announced = True
        try:
            for guild in bot.guilds:
                try:
                    bot.tree.clear_commands(guild=guild)
                    await bot.tree.sync(guild=guild)
                except Exception:
                    pass
            synced = await bot.tree.sync()
            print(f"[Discord Bot] 🔄 ซิงก์ Global Slash Commands สำเร็จ {len(synced)} คำสั่ง!")
        except Exception as e:
            print(f"[Discord Bot] ⚠️ ข้อผิดพลาดในการซิงก์คำสั่ง: {e}")

        try:
            send_alert(
                "🤖 **[Discord Command Center ออนไลน์ - Linux Ubuntu]**\n"
                f"• ล็อกอินสำเร็จในนาม: `{bot.user}`\n"
                "• สภาพแวดล้อม: Linux Ubuntu + Docker MT5 (ฟรี 0 บาท)\n"
                "• ระบบรับคำสั่งผ่าน Slash Command พร้อมใช้งานแล้ว 🟢\n"
                "• พิมพ์ `/help` ในห้องแชทเพื่อดูคำสั่งทั้งหมดได้เลย!",
                "INFO"
            )
        except Exception:
            pass


# 1. /simulate
@bot.tree.command(name="simulate", description="Run historical backtest on current symbol and timeframe")
@app_commands.describe(bars="Number of historical candles to backtest (default: 12000)")
async def simulate_command(interaction: discord.Interaction, bars: int = 12000):
    await interaction.response.defer(thinking=True)
    loop = asyncio.get_running_loop()

    if bars < 100 or bars > 50000:
        await interaction.followup.send("⚠️ กรุณาระบุจำนวนแท่งเทียนระหว่าง 100 ถึง 50,000 แท่ง")
        return

    try:
        res = await loop.run_in_executor(None, run_simulation_sync, bars)
        m = res["metrics"]
        symbol = res["symbol"]
        tf = res["timeframe"]
        ver = res["version"]
        start_d = res["start_str"][:10]
        end_d = res["end_str"][:10]
        loaded_bars = res["bars_loaded"]

        net_profit = m.get("net_profit", 0.0)
        win_rate = m.get("win_rate", 0.0)
        trades = m.get("trades", 0)
        buy_trades = m.get("buy_trades", 0)
        sell_trades = m.get("sell_trades", 0)
        profit_factor = m.get("profit_factor", 0.0)
        max_dd = m.get("max_drawdown_pct", 0.0)
        final_bal = m.get("final_balance", 10000.0)

        color = 0x2ECC71 if net_profit >= 0 else 0xE74C3C
        icon = "🟢" if net_profit >= 0 else "🔴"

        embed = discord.Embed(
            title=f"📊 ผลการทดสอบย้อนหลัง (Backtest Report) v{ver}",
            description=f"ทดสอบสินทรัพย์ `{symbol}` บนไทม์เฟรม `{tf}` ({loaded_bars:,} แท่ง)",
            color=color,
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="ช่วงเวลาข้อมูล", value=f"📅 `{start_d}` ถึง `{end_d}`", inline=False)
        embed.add_field(name="กำไรสุทธิ (Net Profit)", value=f"**{icon} ${net_profit:+,.2f}**", inline=True)
        embed.add_field(name="ยอดเงินคงเหลือ", value=f"`${final_bal:,.2f}`", inline=True)
        embed.add_field(name="Win Rate รวม", value=f"`{win_rate:.2f}%`", inline=True)
        embed.add_field(name="จำนวนออเดอร์", value=f"`{trades}` ไม้ (BUY: {buy_trades}, SELL: {sell_trades})", inline=True)
        embed.add_field(name="Profit Factor", value=f"`{profit_factor:.2f}`", inline=True)
        embed.add_field(name="Max Drawdown", value=f"`{max_dd:.2f}%`", inline=True)
        embed.set_footer(text="AI Trading Bot 2 — Linux & Cloud Edition")

        await interaction.followup.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการรัน Backtest: {e}")


# 2. /status
@bot.tree.command(name="status", description="Display system health, live market prices, and sentiment")
async def status_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    hb = get_heartbeat_status()
    s_data = get_latest_sentiment_data()
    ctrl = load_trading_control()
    cfg = load_config()
    t_cfg = cfg.get("trading", {})
    symbol = t_cfg.get("symbol", "XAUUSD")
    tf = t_cfg.get("timeframe", "M15")

    # ตรวจสอบ Docker MT5 หรือ MetaApi
    is_meta = is_docker_mt5_available() or is_metaapi_configured()
    meta_status_text = ""
    balance_text = "N/A"
    equity_text = "N/A"
    market_text = ""

    if is_meta:
        meta_ok, client, conn = await get_metaapi_connection()
        if meta_ok and conn:
            try:
                acc_info = await conn.get_account_information()
                bal = float(get_val(acc_info, "balance", 0.0) or 0.0)
                eq = float(get_val(acc_info, "equity", 0.0) or 0.0)
                server = get_val(acc_info, "server", "Cloud")
                balance_text = f"${bal:,.2f}"
                equity_text = f"${eq:,.2f}"
                meta_status_text = f"🟢 เชื่อมต่อ MT5 สำเร็จ (`{server}`)"

                p_info = await conn.get_symbol_price(symbol)
                bid = float(get_val(p_info, "bid", 0.0) or 0.0)
                ask = float(get_val(p_info, "ask", 0.0) or 0.0)
                market_text = f"Bid: `{bid:,.2f}` | Ask: `{ask:,.2f}`"
            except Exception as e:
                meta_status_text = f"⚠️ กำลังเชื่อมต่อ: {e}"
                global _metaapi_conn
                _metaapi_conn = None
        else:
            meta_status_text = "⚠️ เชื่อมต่อ MT5 ไม่สำเร็จ (ตรวจสอบรหัส/เซิร์ฟเวอร์)"
    else:
        meta_status_text = "⚪ ยังไม่ได้เชื่อมต่อ MT5 (รันโหมด Offline Data)"

    # ถ้าไม่มีราคาจาก live ให้ดึงจาก offline cache
    if not market_text:
        off = get_market_and_indicators_offline()
        if off.get("ok"):
            market_text = f"ราคาปิดล่าสุด: `{off.get('close'):,.2f}` | SMA: `{off.get('sma'):,.2f}` | RSI: `{off.get('rsi'):.1f}` | ADX: `{off.get('adx'):.1f}`"

    embed = discord.Embed(
        title="🖥️ สถานะระบบ AI Trading Bot 2 (Linux)",
        color=0x3498DB,
        timestamp=datetime.datetime.now()
    )
    embed.add_field(name="การเชื่อมต่อ MT5", value=meta_status_text, inline=False)
    if is_meta:
        embed.add_field(name="Balance / Equity", value=f"Balance: `{balance_text}`\nEquity: `{equity_text}`", inline=True)

    embed.add_field(name="สินทรัพย์ & ไทม์เฟรม", value=f"`{symbol}` ({tf})", inline=True)
    embed.add_field(name="สถานะเทรดอัตโนมัติ", value="🟢 เปิดทำงาน" if ctrl.get("trading_enabled", True) else f"⏸️ หยุดพัก ({ctrl.get('reason')})", inline=True)

    if market_text:
        embed.add_field(name="ข้อมูลตลาด & สัญญาณ", value=market_text, inline=False)

    hb_text = (
        f"• Trade Bot: {hb.get('trade_bot')}\n"
        f"• News Agent: {hb.get('news_agent')}\n"
        f"• Overseer: {hb.get('overseer')}\n"
        f"• Discord Bot: {hb.get('discord_bot')}"
    )
    embed.add_field(name="สถานะบริการ (Heartbeat)", value=hb_text, inline=True)

    score = s_data.get("score", 0.0)
    sentiment_icon = "🟢 บวก" if score > 0.1 else ("🔴 ลบ" if score < -0.1 else "⚪ กลาง")
    news_text = f"คะแนน: `{score:+.2f}` ({sentiment_icon})\nผู้ให้บริการ: `{s_data.get('provider')}`\nอัปเดต: `{s_data.get('age_str')}`"
    embed.add_field(name="สภาวะข่าวเศรษฐกิจ", value=news_text, inline=True)

    if not is_meta:
        embed.add_field(
            name="💡 วิธีเชื่อมต่อ MT5 Live",
            value="เปิด Docker MT5 (`mt5-server`) หรือใส่ `METAAPI_TOKEN` ในไฟล์ `.env` บนเซิร์ฟเวอร์เพื่อเริ่มเทรดจริง",
            inline=False
        )

    await interaction.followup.send(embed=embed)


# 3. /positions
@bot.tree.command(name="positions", description="View open positions and floating PnL on MT5")
async def positions_command(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    if not (is_docker_mt5_available() or is_metaapi_configured()):
        embed = discord.Embed(
            title="⚠️ ยังไม่ได้เชื่อมต่อ MT5",
            description=(
                "ขณะนี้ระบบยังไม่ได้เชื่อมต่อ Docker MT5 หรือระบุโทเคน MetaApi ในไฟล์ `.env`\n\n"
                + get_metaapi_instructions()
            ),
            color=0xF1C40F
        )
        await interaction.followup.send(embed=embed)
        return

    meta_ok, client, conn = await get_metaapi_connection()
    if not meta_ok or not conn:
        await interaction.followup.send("❌ ไม่สามารถเชื่อมต่อกับ MT5 ได้ในขณะนี้ กรุณารอสักครู่แล้วลองใหม่")
        return

    try:
        positions = await conn.get_positions()
        if not positions:
            await interaction.followup.send("ℹ️ ขณะนี้ไม่มีออเดอร์ที่เปิดค้างอยู่บน MT5 (0 ไม้)")
            return

        embed = discord.Embed(
            title=f"📋 รายการออเดอร์ที่เปิดอยู่ ({len(positions)} ไม้)",
            color=0x2ECC71,
            timestamp=datetime.datetime.now()
        )
        total_pnl = 0.0
        for p in positions:
            ticket = get_val(p, "id")
            sym = get_val(p, "symbol")
            p_type = get_val(p, "type", "")
            side = "BUY" if "BUY" in str(p_type).upper() else "SELL"
            vol = float(get_val(p, "volume", 0.0) or 0.0)
            open_p = float(get_val(p, "openPrice", 0.0) or 0.0)
            cur_p = float(get_val(p, "currentPrice", 0.0) or 0.0)
            sl = float(get_val(p, "stopLoss", 0.0) or 0.0)
            tp = float(get_val(p, "takeProfit", 0.0) or 0.0)
            profit = float(get_val(p, "profit", 0.0) or 0.0)
            total_pnl += profit

            side_icon = "🟢" if side == "BUY" else "🔴"
            embed.add_field(
                name=f"{side_icon} #{ticket} {side} {sym} ({vol} Lot)",
                value=f"เปิดที่: `{open_p}` | ปัจจุบัน: `{cur_p}`\nSL: `{sl}` | TP: `{tp}`\nPnL: **${profit:+,.2f}**",
                inline=False
            )

        embed.set_footer(text=f"กำไร/ขาดทุนรวมที่ถืออยู่: ${total_pnl:+,.2f}")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        global _metaapi_conn
        _metaapi_conn = None
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการดึงรายการ Positions: {e}")


# 4. /buy
@bot.tree.command(name="buy", description="Place live market BUY order with automated SL/TP")
@app_commands.describe(lot="Lot volume (leave empty for auto ATR risk sizing)")
async def buy_command(interaction: discord.Interaction, lot: float = None):
    await interaction.response.defer(thinking=True)

    if not (is_docker_mt5_available() or is_metaapi_configured()):
        await interaction.followup.send("⚠️ ยังไม่ได้เชื่อมต่อ MT5 จึงไม่สามารถส่งคำสั่งสดเข้า MT5 ได้")
        return

    meta_ok, client, conn = await get_metaapi_connection()
    if not meta_ok or not conn:
        await interaction.followup.send("❌ ไม่สามารถเชื่อมต่อกับ MT5 ได้ในขณะนี้")
        return

    cfg = load_config()
    t_cfg = cfg.get("trading", {})
    symbol = t_cfg.get("symbol", "XAUUSD")
    params = load_params()
    ver = params.get("version", 5)
    magic = int(t_cfg.get("magic_number", 123456))

    try:
        spec = await conn.get_symbol_specification(symbol)
        p_info = await conn.get_symbol_price(symbol)
        ask = float(get_val(p_info, "ask", 0.0) or 0.0)
        digits = int(get_val(spec, "digits", 2) or 2)
        min_vol = float(get_val(spec, "minVolume", 0.01) or 0.01)
        max_vol = float(get_val(spec, "maxVolume", 100.0) or 100.0)
        step = float(get_val(spec, "volumeStep", 0.01) or 0.01)

        # คำนวณ SL/TP
        atr_val = 5.0
        off = get_market_and_indicators_offline()
        if off.get("ok"):
            atr_val = off.get("atr", 5.0)

        sl, tp = strategy.sl_tp("BUY", ask, atr_val, params)
        sl = round(sl, digits)
        tp = round(tp, digits)

        if lot is not None:
            if lot < min_vol or lot > max_vol:
                await interaction.followup.send(f"⚠️ ขนาด Lot ({lot}) ไม่อยู่ในช่วงที่โบรเกอร์กำหนด ({min_vol} - {max_vol} Lot)")
                return
            lot = math.floor(lot / step) * step
            step_str = f"{step:.8f}".rstrip("0")
            decimals = len(step_str.split(".")[1]) if "." in step_str else 0
            lot = round(lot, decimals)
        else:
            acc = await conn.get_account_information()
            bal = float(get_val(acc, "balance", 10000.0) or 10000.0)
            from trade_bot import calculate_lot_size
            lot = calculate_lot_size(bal, float(t_cfg.get("risk_percent", 1.0)), ask, sl, spec)

        res = await conn.create_market_buy_order(
            symbol=symbol,
            volume=lot,
            stop_loss=sl,
            take_profit=tp,
            options={"comment": f"manual_v{ver}", "magic": "999999"}
        )
        order_id = get_val(res, "orderId") or get_val(res, "positionId") or "N/A"

        embed = discord.Embed(
            title="🚀 เปิดออเดอร์ BUY สำเร็จ [MT5 Live]",
            color=0x2ECC71,
            description=f"ส่งคำสั่ง BUY `{symbol}` `{lot}` Lot เข้า MT5 เรียบร้อยแล้ว",
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="Order ID", value=f"`#{order_id}`", inline=True)
        embed.add_field(name="ราคาเปิด", value=f"`{ask:,.{digits}f}`", inline=True)
        embed.add_field(name="Stop Loss", value=f"`{sl:,.{digits}f}`", inline=True)
        embed.add_field(name="Take Profit", value=f"`{tp:,.{digits}f}`", inline=True)
        await interaction.followup.send(embed=embed)

    except Exception as e:
        global _metaapi_conn
        _metaapi_conn = None
        await interaction.followup.send(f"❌ ส่งคำสั่ง BUY ไม่สำเร็จ: {e}")


# 5. /sell
@bot.tree.command(name="sell", description="Place live market SELL order with automated SL/TP")
@app_commands.describe(lot="Lot volume (leave empty for auto ATR risk sizing)")
async def sell_command(interaction: discord.Interaction, lot: float = None):
    await interaction.response.defer(thinking=True)

    if not (is_docker_mt5_available() or is_metaapi_configured()):
        await interaction.followup.send("⚠️ ยังไม่ได้เชื่อมต่อ MT5 จึงไม่สามารถส่งคำสั่งสดเข้า MT5 ได้")
        return

    meta_ok, client, conn = await get_metaapi_connection()
    if not meta_ok or not conn:
        await interaction.followup.send("❌ ไม่สามารถเชื่อมต่อกับ MT5 ได้ในขณะนี้")
        return

    cfg = load_config()
    t_cfg = cfg.get("trading", {})
    symbol = t_cfg.get("symbol", "XAUUSD")
    params = load_params()
    ver = params.get("version", 5)
    magic = int(t_cfg.get("magic_number", 123456))

    try:
        spec = await conn.get_symbol_specification(symbol)
        p_info = await conn.get_symbol_price(symbol)
        bid = float(get_val(p_info, "bid", 0.0) or 0.0)
        digits = int(get_val(spec, "digits", 2) or 2)
        min_vol = float(get_val(spec, "minVolume", 0.01) or 0.01)
        max_vol = float(get_val(spec, "maxVolume", 100.0) or 100.0)
        step = float(get_val(spec, "volumeStep", 0.01) or 0.01)

        atr_val = 5.0
        off = get_market_and_indicators_offline()
        if off.get("ok"):
            atr_val = off.get("atr", 5.0)

        sl, tp = strategy.sl_tp("SELL", bid, atr_val, params)
        sl = round(sl, digits)
        tp = round(tp, digits)

        if lot is not None:
            if lot < min_vol or lot > max_vol:
                await interaction.followup.send(f"⚠️ ขนาด Lot ({lot}) ไม่อยู่ในช่วงที่โบรเกอร์กำหนด ({min_vol} - {max_vol} Lot)")
                return
            lot = math.floor(lot / step) * step
            step_str = f"{step:.8f}".rstrip("0")
            decimals = len(step_str.split(".")[1]) if "." in step_str else 0
            lot = round(lot, decimals)
        else:
            acc = await conn.get_account_information()
            bal = float(get_val(acc, "balance", 10000.0) or 10000.0)
            from trade_bot import calculate_lot_size
            lot = calculate_lot_size(bal, float(t_cfg.get("risk_percent", 1.0)), bid, sl, spec)

        res = await conn.create_market_sell_order(
            symbol=symbol,
            volume=lot,
            stop_loss=sl,
            take_profit=tp,
            options={"comment": f"manual_v{ver}", "magic": "999999"}
        )
        order_id = get_val(res, "orderId") or get_val(res, "positionId") or "N/A"

        embed = discord.Embed(
            title="🚀 เปิดออเดอร์ SELL สำเร็จ [MT5 Live]",
            color=0xE74C3C,
            description=f"ส่งคำสั่ง SELL `{symbol}` `{lot}` Lot เข้า MT5 เรียบร้อยแล้ว",
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="Order ID", value=f"`#{order_id}`", inline=True)
        embed.add_field(name="ราคาเปิด", value=f"`{bid:,.{digits}f}`", inline=True)
        embed.add_field(name="Stop Loss", value=f"`{sl:,.{digits}f}`", inline=True)
        embed.add_field(name="Take Profit", value=f"`{tp:,.{digits}f}`", inline=True)
        await interaction.followup.send(embed=embed)

    except Exception as e:
        global _metaapi_conn
        _metaapi_conn = None
        await interaction.followup.send(f"❌ ส่งคำสั่ง SELL ไม่สำเร็จ: {e}")


# 6. /close
@bot.tree.command(name="close", description="Close open position or cancel pending orders on MT5")
@app_commands.describe(ticket="Order ticket number to close (closes all for current symbol if omitted)")
async def close_command(interaction: discord.Interaction, ticket: str = None):
    await interaction.response.defer(thinking=True)

    if not (is_docker_mt5_available() or is_metaapi_configured()):
        await interaction.followup.send("⚠️ ยังไม่ได้เชื่อมต่อ MT5")
        return

    meta_ok, client, conn = await get_metaapi_connection()
    if not meta_ok or not conn:
        await interaction.followup.send("❌ ไม่สามารถเชื่อมต่อกับ MT5 ได้ในขณะนี้")
        return

    try:
        positions = await conn.get_positions()
        if not positions:
            await interaction.followup.send("ℹ️ ขณะนี้ไม่มีออเดอร์ที่เปิดค้างอยู่บน MT5 (0 ไม้)")
            return

        if ticket:
            res = await conn.close_position(str(ticket))
            await interaction.followup.send(f"✅ ปิดออเดอร์ `#{ticket}` สำเร็จเรียบร้อยแล้ว!")
        else:
            cfg = load_config()
            sym = cfg.get("trading", {}).get("symbol", "XAUUSD")
            res = await conn.close_positions_by_symbol(sym)
            await interaction.followup.send(f"✅ สั่งปิดออเดอร์ของ `{sym}` ทั้งหมดเรียบร้อยแล้ว!")
    except Exception as e:
        global _metaapi_conn
        _metaapi_conn = None
        await interaction.followup.send(f"❌ ปิดออเดอร์ไม่สำเร็จ: {e}")


# 7. /pause
@bot.tree.command(name="pause", description="Temporarily pause automated new order execution")
@app_commands.describe(reason="Reason for pausing execution")
async def pause_command(interaction: discord.Interaction, reason: str = "สั่งพักผ่าน Discord"):
    ctrl = {
        "trading_enabled": False,
        "reason": reason,
        "updated_at": datetime.datetime.now().isoformat()
    }
    save_trading_control(ctrl)
    log_event("discord_bot", "PAUSED", f"Trading paused via Discord: {reason}")
    await interaction.response.send_message(f"⏸️ **[บอทหยุดพักการเปิดออเดอร์]** {reason}")


# 8. /resume
@bot.tree.command(name="resume", description="Resume automated strategy execution")
@app_commands.describe(reason="Reason for resuming execution")
async def resume_command(interaction: discord.Interaction, reason: str = "เปิดเทรดต่อผ่าน Discord"):
    ctrl = {
        "trading_enabled": True,
        "reason": reason,
        "updated_at": datetime.datetime.now().isoformat()
    }
    save_trading_control(ctrl)
    try:
        reset_file = STATE_DIR / "daily_loss_reset.json"
        reset_file.write_text(json.dumps({"reset_at": datetime.datetime.now().isoformat()}), encoding="utf-8")
    except Exception as e:
        print(f"[Discord Bot] Failed to write daily_loss_reset.json: {e}")
    log_event("discord_bot", "RESUMED", f"Trading resumed via Discord: {reason}")
    await interaction.response.send_message(f"▶️ **[บอทกลับมาทำการเทรดตามปกติ]** {reason} (รีเซ็ตเกณฑ์ความเสี่ยงรายวันเรียบร้อย)")


# 9. /config
@bot.tree.command(name="config", description="Inspect current symbol, timeframe, and strategy parameters")
async def config_command(interaction: discord.Interaction):
    cfg = load_config()
    params = load_params()
    t_cfg = cfg.get("trading", {})

    embed = discord.Embed(
        title="⚙️ การตั้งค่าระบบ AI Trading Bot (Version 5)",
        color=0x9B59B6,
        timestamp=datetime.datetime.now()
    )
    embed.add_field(
        name="การเทรด (Trading)",
        value=(
            f"• สินทรัพย์: `{t_cfg.get('symbol', 'XAUUSD')}`\n"
            f"• ไทม์เฟรม: `{t_cfg.get('timeframe', 'M15')}`\n"
            f"• ความเสี่ยงต่อไม้: `{t_cfg.get('risk_percent', 1.0)}%`\n"
            f"• จำนวนไม้สูงสุด: `{t_cfg.get('max_open_trades', 1)}` ไม้\n"
            f"• ขีดจำกัดขาดทุนต่อวัน: `{t_cfg.get('daily_loss_limit_percent', 3.0)}%`"
        ),
        inline=False
    )
    embed.add_field(
        name="พารามิเตอร์กลยุทธ์ (Strategy v5)",
        value=(
            f"• SMA Period: `{params.get('sma_period', 50)}`\n"
            f"• RSI Period: `{params.get('rsi_period', 14)}` (Buy: `{params.get('rsi_buy_level', 45)}`, Sell: `{params.get('rsi_sell_level', 55)}`)\n"
            f"• ADX Threshold: `{params.get('adx_threshold', 25)}`\n"
            f"• ATR SL/TP: SL `{params.get('atr_sl_mult', 1.5)}x` / TP `{params.get('atr_tp_mult', 2.0)}x`\n"
            f"• Sentiment Filter: `{'เปิด' if params.get('use_sentiment_filter', True) else 'ปิด'}`"
        ),
        inline=False
    )
    await interaction.response.send_message(embed=embed)


# 10. /set_timeframe
@bot.tree.command(name="set_timeframe", description="Change analysis timeframe (M1, M5, M15, M30, H1, H4, D1)")
@app_commands.describe(timeframe="Timeframe string (e.g. M1, M5, M15, M30, H1, H4, D1)")
async def set_timeframe_command(interaction: discord.Interaction, timeframe: str):
    valid_tfs = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]
    tf_upper = timeframe.upper().strip()
    if tf_upper not in valid_tfs:
        await interaction.response.send_message(f"⚠️ ไทม์เฟรมไม่ถูกต้อง กรุณาเลือกจาก: {', '.join(valid_tfs)}")
        return

    cfg = load_config()
    cfg.setdefault("trading", {})["timeframe"] = tf_upper
    save_config(cfg)
    log_event("discord_bot", "CONFIG", f"Timeframe changed to {tf_upper}")
    await interaction.response.send_message(f"✅ สลับ Timeframe เป็น `{tf_upper}` เรียบร้อยแล้ว (บอทจะอัปเดตอัตโนมัติในลูปถัดไป)")


# 11. /set_symbol
@bot.tree.command(name="set_symbol", description="Change trading asset symbol (e.g. XAUUSD, BTCUSD, EURUSD)")
@app_commands.describe(symbol="Asset symbol name (e.g. XAUUSD)")
async def set_symbol_command(interaction: discord.Interaction, symbol: str):
    sym_clean = symbol.upper().strip()
    cfg = load_config()
    cfg.setdefault("trading", {})["symbol"] = sym_clean
    save_config(cfg)
    log_event("discord_bot", "CONFIG", f"Symbol changed to {sym_clean}")
    await interaction.response.send_message(f"✅ สลับสินทรัพย์เป็น `{sym_clean}` เรียบร้อยแล้ว")


# 12. /set_risk
@bot.tree.command(name="set_risk", description="Update risk percentage per trade")
@app_commands.describe(risk_percent="Risk percent (e.g. 1.0 or 2.0)")
async def set_risk_command(interaction: discord.Interaction, risk_percent: float):
    if risk_percent <= 0 or risk_percent > 10.0:
        await interaction.response.send_message("⚠️ กรุณาระบุความเสี่ยงระหว่าง 0.1% ถึง 10.0%")
        return

    cfg = load_config()
    cfg.setdefault("trading", {})["risk_percent"] = round(risk_percent, 2)
    save_config(cfg)
    log_event("discord_bot", "CONFIG", f"Risk percent changed to {risk_percent}%")
    await interaction.response.send_message(f"✅ ปรับสัดส่วนความเสี่ยงเป็น `{risk_percent:.2f}%` เรียบร้อยแล้ว")


# 13. /help
@bot.tree.command(name="help", description="Display interactive command reference and documentation")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 คู่มือการใช้งาน Discord Command Center (Linux Edition)",
        description="คุณสามารถสั่งการและควบคุมระบบ AI Trading Bot ได้ตลอด 24 ชั่วโมงผ่าน Slash Commands:",
        color=0x3498DB
    )
    embed.add_field(name="/status", value="ตรวจสอบสถานะ MT5, ราคาตลาด, สัญญาณเทคนิค และ Sentiment ข่าว", inline=False)
    embed.add_field(name="/simulate [bars]", value="รันทดสอบย้อนหลัง (Backtest) พร้อมสรุปตัวชี้วัดกำไร/ขาดทุนทันที", inline=False)
    embed.add_field(name="/positions", value="ดูรายการออเดอร์ที่เปิดค้างอยู่บน MT5 พร้อมกำไร/ขาดทุนลอยตัว", inline=False)
    embed.add_field(name="/buy [lot] / /sell [lot]", value="ส่งคำสั่งเปิดออเดอร์สดเข้า MT5 พร้อม SL/TP อัตโนมัติ", inline=False)
    embed.add_field(name="/close [ticket]", value="สั่งปิดออเดอร์ที่เปิดอยู่บน MT5 ทันที", inline=False)
    embed.add_field(name="/pause / /resume", value="หยุดพักหรือเปิดให้บอทกลับมาทำการเทรดอัตโนมัติตามต้องการ", inline=False)
    embed.add_field(name="/config", value="ดูการตั้งค่าพอร์ตและพารามิเตอร์กลยุทธ์ Version 5 ปัจจุบัน", inline=False)
    embed.add_field(name="/set_timeframe / /set_symbol / /set_risk", value="ปรับแต่งสินทรัพย์ ไทม์เฟรม และความเสี่ยงแบบไดนามิก", inline=False)
    embed.set_footer(text="AI Trading Bot 2 — Headless Ubuntu 24.04 LTS")
    await interaction.response.send_message(embed=embed)


def main():
    try:
        token = secret("DISCORD_BOT_TOKEN")
        # ตัด quote หากมี
        token = token.strip("\"'")
        print("[Discord Bot] 🤖 กำลังเข้าสู่ระบบ Discord Command Center...")
        bot.run(token)
    except Exception as e:
        print(f"[Discord Bot] ❌ เกิดข้อผิดพลาดในการเริ่มต้น Discord Bot: {e}")


if __name__ == "__main__":
    main()
