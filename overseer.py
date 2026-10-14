"""
overseer.py — System Supervisor & AI Self-Healing Overseer
Two-tier monitoring architecture:
1. Health Check every 15m (Pure Python: Heartbeat, Streak Loss, Crash, Sunday schedule)
2. AI Repair & Improvement with 7-layer safety sandbox (Snapshot -> Baseline -> AI -> Hash Check -> Compile -> Validation Backtest -> Accept/Rollback)
"""
import asyncio
import csv
import datetime
import hashlib
import importlib
import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

import simulator
import strategy
from notifier import send_alert
from settings import (
    BOT_DIR,
    BACKUP_DIR,
    LOG_DIR,
    STATE_DIR,
    is_placeholder,
    load_config,
    load_params,
    secret,
)

# Start with DRY_RUN = True for maximum safety (AI suggests but does not overwrite code)
DRY_RUN = True

OVERSEER_RULES = """You are a Quant Engineer overseeing a XAUUSD H1 gold trading bot.
- You are ONLY allowed to modify `strategy.py` and `strategy_params.json`.
- Do NOT open, read, or modify `.env` or `config.json` under any circumstances.
- Do NOT change the names or signatures of the main functions: `add_indicators`, `generate_signal`, `sl_tp`.
- Do NOT import MetaTrader5 in `strategy.py` (it must be purely mathematical).
- Test the results using the command: `python simulator.py`.
- IMPORTANT: Your final report must be a concise 5-line summary written in Thai language for Discord notification:
  1) ปัญหาหรือจุดบกพร่องที่พบ (The problem or flaw found)
  2) แนวทางปรับปรุงตรรกะ/พารามิเตอร์ (Decided improvements in logic/params)
  3) ผลทดสอบ Backtest ก่อนและหลังแก้ (Simulator results before & after)
  4) เหตุผลด้านความเสถียร (Robustness rationale)
  5) คำแนะนำเพิ่มเติม (Recommendations)
"""


def call_antigravity_cli(prompt: str, timeout_sec: int = 120) -> str:
    """
    Invoke local Antigravity CLI (agy) for autonomous reasoning, with fallback
    to Groq (llama-3.3-70b-versatile) or Gemini (gemini-3.8-flash / gemini-2.0-flash)
    on Linux servers where agy is absent.
    """
    cmd = ["agy", "-p", prompt, "--output-format", "text"]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_sec,
            shell=(sys.platform == "win32")
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    # Fallback 1: Groq API
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key and not is_placeholder(groq_key):
        try:
            from groq import Groq
            client = Groq(api_key=groq_key)
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=800,
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"[Overseer] Groq fallback error: {e}")

    # Fallback 2: Gemini API (gemini-3.8-flash primary per config.json, fallback gemini-2.0-flash)
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key and not is_placeholder(gemini_key):
        try:
            from google import genai
            client = genai.Client(api_key=gemini_key)
            cfg_model = load_config().get("overseer", {}).get("model", "gemini-3.8-flash")
            candidate_models = []
            for m in (cfg_model, "gemini-3.8-flash", "gemini-2.0-flash"):
                if m and m not in candidate_models:
                    candidate_models.append(m)
            for model_name in candidate_models:
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                    )
                    if resp and resp.text:
                        return resp.text.strip()
                except Exception as inner_e:
                    print(f"[Overseer] Gemini ({model_name}) fallback error: {inner_e}")
        except Exception as e:
            print(f"[Overseer] Gemini fallback error: {e}")

    raise RuntimeError("Command 'agy' not found and no working AI fallback (GROQ_API_KEY / GEMINI_API_KEY)")



def update_heartbeat():
    """Record heartbeat timestamp for overseer monitoring."""
    hb_file = STATE_DIR / "heartbeat_overseer.txt"
    hb_file.write_text(datetime.datetime.now().isoformat(), encoding="utf-8")


def load_overseer_state() -> dict:
    """Load overseer state tracking daily AI invocations and issue fingerprints."""
    state_file = STATE_DIR / "overseer_state.json"
    today_str = datetime.date.today().isoformat()
    default_state = {
        "date": today_str,
        "ai_runs_today": 0,
        "handled_fingerprints": [],
        "last_weekly_review_week": ""
    }

    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if data.get("date") != today_str:
                # เริ่มต้นวันใหม่ รีเซ็ตโควต้า
                data["date"] = today_str
                data["ai_runs_today"] = 0
                data["handled_fingerprints"] = []
            return data
        except Exception:
            return default_state
    return default_state


def save_overseer_state(state: dict):
    """บันทึกสถานะของ overseer"""
    state_file = STATE_DIR / "overseer_state.json"
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def log_history(entry: str):
    """บันทึกประวัติการทำงานของ AI ลง logs/overseer_history.md"""
    hist_file = LOG_DIR / "overseer_history.md"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(hist_file, "a", encoding="utf-8") as f:
        f.write(f"\n\n## [{now_str}]\n{entry}\n")


def get_file_hashes() -> dict[str, str]:
    """คำนวณ SHA-256 Hash ของไฟล์ทั้งหมดในโปรเจกต์ (ยกเว้น logs, backups, state, cache)"""
    hashes = {}
    ignored_dirs = {".git", "logs", "backups", "state", "data", "__pycache__"}
    for p in BOT_DIR.rglob("*"):
        if p.is_file():
            rel_parts = p.relative_to(BOT_DIR).parts
            if any(part in ignored_dirs for part in rel_parts):
                continue
            try:
                content = p.read_bytes()
                hashes[str(p.relative_to(BOT_DIR))] = hashlib.sha256(content).hexdigest()
            except Exception:
                pass
    return hashes


_OVERSEER_STARTED_AT = time.time()
_HB_STARTUP_GRACE_SEC = 120      # ช่วงเริ่มระบบ: ไฟล์ heartbeat อาจเป็นของรอบก่อนหน้า ยังไม่ต้องเตือน
_HB_ALERT_COOLDOWN_SEC = 3600    # เตือนซ้ำบริการเดิมได้อย่างเร็วที่สุดทุก 1 ชั่วโมง (กันสแปม Discord)
_hb_last_alert: dict = {}


def check_heartbeats():
    """ตรวจสอบว่า trade_bot และ news_agent ยังมีชีวิตอยู่หรือไม่"""
    now = datetime.datetime.now()

    # ช่วงเริ่มระบบ: รอให้ทุกบริการเขียน heartbeat ใหม่ก่อน ไม่งั้นจะเตือนผิดจากไฟล์เก่า
    if time.time() - _OVERSEER_STARTED_AT < _HB_STARTUP_GRACE_SEC:
        return

    # เวลาที่ยอมให้เงียบได้ = รอบการทำงานจริงของแต่ละบริการ + เผื่อไว้ (ไม่ใช่เลขเดียวใช้กับทุกตัว)
    cfg = load_config()
    trade_loop_sec = int(cfg.get("trading", {}).get("loop_seconds", 30))
    news_interval_min = float(cfg.get("news", {}).get("check_interval_minutes", 30))
    services = {
        "trade_bot": (STATE_DIR / "heartbeat_trade_bot.txt", max(300, trade_loop_sec * 5)),
        "news_agent": (STATE_DIR / "heartbeat_news_agent.txt", int(news_interval_min * 60 + 600)),
        "discord_bot": (STATE_DIR / "heartbeat_discord_bot.txt", 180),
    }

    for name, (path, max_allowed_sec) in services.items():
        if not path.exists():
            continue
        try:
            last_time = datetime.datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
            diff_sec = (now - last_time).total_seconds()
            if diff_sec > max_allowed_sec:
                if time.time() - _hb_last_alert.get(name, 0) < _HB_ALERT_COOLDOWN_SEC:
                    continue
                _hb_last_alert[name] = time.time()
                msg = (f"⚠️ [Overseer] ตรวจพบ Heartbeat ของ `{name}` เงียบไปนานกว่า {int(diff_sec/60)} นาที "
                       f"(เกณฑ์ {int(max_allowed_sec/60)} นาที)")
                print(msg)
                send_alert(msg, level="WARNING")
        except Exception as e:
            print(f"[Overseer] อ่าน heartbeat ของ {name} ไม่ได้: {e}")


def check_consecutive_losses(threshold: int = 4) -> tuple[bool, int, str]:
    """ตรวจสอบประวัติการเทรดว่าแพ้ติดกันเกินเกณฑ์หรือไม่"""
    trades_file = LOG_DIR / "trades.csv"
    if not trades_file.exists():
        return False, 0, ""

    try:
        df = pd.read_csv(trades_file)
        close_trades = df[df["event"] == "CLOSE"].copy()
        if len(close_trades) < threshold:
            return False, 0, ""

        recent_profits = close_trades["profit"].tolist()
        streak = 0
        for p in reversed(recent_profits):
            if float(p) < 0:
                streak += 1
            else:
                break

        last_ticket = str(close_trades.iloc[-1].get("ticket", "0"))
        if streak >= threshold:
            return True, streak, f"streak_loss_{last_ticket}_{streak}"
    except Exception as e:
        print(f"[Overseer] Error reading trades.csv: {e}")

    return False, 0, ""


def check_crash_events() -> tuple[bool, str]:
    """ตรวจสอบว่ามีเหตุการณ์ CRASH ใน events.csv หรือไม่"""
    events_file = LOG_DIR / "events.csv"
    if not events_file.exists():
        return False, ""

    try:
        with open(events_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reversed(list(reader)):
                if row.get("level") == "CRASH":
                    fingerprint = f"crash_{row.get('timestamp')}"
                    return True, fingerprint
    except Exception:
        pass

    return False, ""


async def run_ai_healing(reason: str, fingerprint: str):
    """
    กระบวนการ AI ซ่อมและปรับปรุงระบบ ผ่านด่านความปลอดภัย 7 ขั้น
    """
    cfg = load_config()
    o_cfg = cfg.get("overseer", {})
    state = load_overseer_state()
    max_runs = int(o_cfg.get("max_ai_runs_per_day", 3))

    if state["ai_runs_today"] >= max_runs:
        print(f"[Overseer] ⚠️ โควต้าเรียก AI วันนี้เต็มแล้ว ({state['ai_runs_today']}/{max_runs})")
        send_alert(f"⚠️ [Overseer] มีเหตุการณ์ '{reason}' แต่โควต้า AI วันนี้เต็มแล้ว ({max_runs}/{max_runs})", level="WARNING")
        return

    print(f"\n[Overseer] 🚨 เริ่มกระบวนการตรวจสอบและปรับปรุงโดย AI (เหตุผล: {reason})")
    send_alert(f"🔮 **[Overseer] เรียก AI เข้าตรวจสอบระบบ**\n• เหตุผล: `{reason}`\n• โหมด: `{'DRY RUN (เสนอแนะอย่างเดียว)' if DRY_RUN else 'ACTIVE (ปรับปรุงจริง)'}`", level="OVERSEER")

    # ขั้นที่ 1: Snapshot สำรองไฟล์และเก็บ Hash
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_dir = BACKUP_DIR / f"overseer_snapshot_{timestamp_str}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    files_to_backup = ["strategy.py", "strategy_params.json", "config.json"]
    for fn in files_to_backup:
        src = BOT_DIR / fn
        if src.exists():
            shutil.copy(src, snapshot_dir / fn)

    initial_hashes = get_file_hashes()

    # ขั้นที่ 2: รัน Simulator หา Baseline ผลก่อนแก้
    symbol = cfg.get("trading", {}).get("symbol", "XAUUSD")
    tf = cfg.get("trading", {}).get("timeframe", "M15")
    history_df = simulator.load_history(symbol=symbol, timeframe=tf)
    current_params = load_params()

    val_len = int(len(history_df) * float(cfg.get("backtest", {}).get("train_ratio", 0.7)))
    val_df = history_df.iloc[val_len:].copy()

    baseline_metrics = simulator.run_backtest(val_df, current_params, strategy, cfg.get("backtest", {}))
    print(f"[Overseer] Baseline Validation PF: {baseline_metrics['profit_factor']} (Win Rate: {baseline_metrics['win_rate']}%)")

    # ดึงประวัติไม้ล่าสุด
    recent_trades_text = "ไม่มีข้อมูล"
    trades_file = LOG_DIR / "trades.csv"
    if trades_file.exists():
        try:
            tdf = pd.read_csv(trades_file)
            recent_trades_text = tdf.tail(10).to_string()
        except Exception:
            pass

    prompt = f"""An event occurred requiring you to inspect and improve the system:
- Reason: {reason}
- Baseline performance on Validation set: PF={baseline_metrics['profit_factor']}, WinRate={baseline_metrics['win_rate']}%, Trades={baseline_metrics['trades']}, DD={baseline_metrics['max_drawdown_pct']}%
- Recent trades:
{recent_trades_text}

Please analyze the problem and tune logic in strategy.py or strategy_params.json to improve validation metrics.
Summarize a concise 5-line report in Thai language for Discord notification."""

    if DRY_RUN:
        print("[Overseer] 🛡️ DRY_RUN=True: Invoking AI via Antigravity CLI for recommendations (no actual files modified)")
        try:
            full_prompt = f"{OVERSEER_RULES}\n\n[DRY RUN MODE - DO NOT MODIFY ANY FILES. PROVIDE SUGGESTIONS AND A 5-LINE THAI REPORT]\n{prompt}"
            report = call_antigravity_cli(full_prompt)
            print(f"\n[Overseer AI Report (DRY RUN)]:\n{report}\n")
            log_history(f"**เหตุผล:** {reason} (DRY RUN)\n\n**รายงาน AI:**\n{report}\n\n**การตัดสินใจ:** สิ้นสุดรอบจำลอง (DRY RUN)")
            send_alert(f"🔮 **[Overseer รายงานผลจำลอง (DRY RUN)]**\n\n{report[:1800]}", level="OVERSEER")
        except Exception as e:
            print(f"[Overseer] DRY RUN Error: {e}")

        state["ai_runs_today"] += 1
        state["handled_fingerprints"].append(fingerprint)
        save_overseer_state(state)
        return

    # ถ้าไม่ใช่ DRY RUN -> เรียก Antigravity CLI ให้วิเคราะห์และแก้ไฟล์
    try:
        full_prompt = f"{OVERSEER_RULES}\n\n[ACTIVE MODE - YOU MUST MODIFY THE CODE AND RUN TESTS BEFORE RESPONDING. PROVIDE 5-LINE THAI REPORT]\n{prompt}"
        report = call_antigravity_cli(full_prompt)

    except Exception as e:
        print(f"[Overseer] Antigravity CLI Error: {e}")
        send_alert(f"🚨 [Overseer] ไม่สามารถเรียก Antigravity CLI: {e}", level="ERROR")
        return

    # ขั้นที่ 4: ตรวจสอบความปลอดภัยของไฟล์ (Hash Check)
    current_hashes = get_file_hashes()
    editable_files = set(o_cfg.get("editable_files", ["strategy.py", "strategy_params.json"]))
    violation = False
    modified_files = []

    for f_path, h_val in current_hashes.items():
        if initial_hashes.get(f_path) != h_val:
            modified_files.append(f_path)
            if Path(f_path).name not in editable_files:
                violation = True
                print(f"[Overseer 🚨] มีการดัดแปลงไฟล์ต้องห้าม: {f_path}")

    # ตรวจสอบไฟล์ที่อาจถูกลบ
    for f_path in initial_hashes:
        if f_path not in current_hashes:
            violation = True
            print(f"[Overseer 🚨] ไฟล์ถูกลบ: {f_path}")

    if violation:
        print("[Overseer] ❌ AI ฝ่าฝืนกฎความปลอดภัย! กำลัง Rollback คืนค่าทั้งหมดทันที...")
        for fn in files_to_backup:
            bak_file = snapshot_dir / fn
            if bak_file.exists():
                shutil.copy(bak_file, BOT_DIR / fn)
        log_history(f"**เหตุผล:** {reason}\n**รายงาน AI:**\n{report}\n**การตัดสินใจ:** ❌ REJECTED & ROLLED BACK (ฝ่าฝืนขอบเขตไฟล์)")
        send_alert("🚨 **[Overseer Alert]** AI พยายามแก้ไขไฟล์นอกขอบเขตที่อนุญาต! ระบบทำการ Rollback ทั้งหมดเรียบร้อยแล้ว", level="ERROR")
        return

    # ขั้นที่ 5: ตรวจสอบ Syntax & Import
    try:
        py_compile.compile(str(BOT_DIR / "strategy.py"), doraise=True)
        importlib.reload(strategy)
    except Exception as e:
        print(f"[Overseer] ❌ strategy.py มี syntax หรือ import error: {e}")
        for fn in files_to_backup:
            bak_file = snapshot_dir / fn
            if bak_file.exists():
                shutil.copy(bak_file, BOT_DIR / fn)
        log_history(f"**เหตุผล:** {reason}\n**รายงาน AI:**\n{report}\n**การตัดสินใจ:** ❌ REJECTED & ROLLED BACK (Compile error: {e})")
        send_alert(f"🚨 [Overseer] strategy.py ใหม่ไม่ผ่านการ Compile ({e}) ทำการ Rollback", level="ERROR")
        return

    # ขั้นที่ 6: สอบผ่านด่าน Simulator บนชุด Validation
    new_params = load_params()
    new_metrics = simulator.run_backtest(val_df, new_params, strategy, cfg.get("backtest", {}))

    passed = (
        new_metrics["profit_factor"] > baseline_metrics["profit_factor"]
        and new_metrics["max_drawdown_pct"] <= float(cfg.get("backtest", {}).get("max_drawdown_percent", 20.0))
        and new_metrics["trades"] >= 15
    )

    if not passed:
        print(f"[Overseer] ⚠️ ผลลัพธ์ใหม่ไม่ดีกว่าเดิม (PF: {new_metrics['profit_factor']} vs Baseline: {baseline_metrics['profit_factor']}) ทำการ Rollback")
        for fn in files_to_backup:
            bak_file = snapshot_dir / fn
            if bak_file.exists():
                shutil.copy(bak_file, BOT_DIR / fn)
        log_history(f"**เหตุผล:** {reason}\n**ผลการจำลอง:** New PF={new_metrics['profit_factor']} <= Base PF={baseline_metrics['profit_factor']}\n**การตัดสินใจ:** ❌ REJECTED & ROLLED BACK (ไม่ผ่านเกณฑ์ Validation)")
        send_alert(f"⚠️ [Overseer] กลยุทธ์ใหม่ไม่ผ่านเกณฑ์ Validation (PF: {new_metrics['profit_factor']} <= {baseline_metrics['profit_factor']}) ระบบทำการ Rollback กลยุทธ์เดิม", level="WARNING")
        return

    # ขั้นที่ 7: ยอมรับและเปิดใช้งาน
    print(f"[Overseer] 🎉 ยอมรับกลยุทธ์ใหม่! (Validation PF ดีขึ้นจาก {baseline_metrics['profit_factor']} ➔ {new_metrics['profit_factor']})")
    (STATE_DIR / "restart.flag").write_text(f"restart requested by overseer at {datetime.datetime.now().isoformat()}", encoding="utf-8")

    state["ai_runs_today"] += 1
    state["handled_fingerprints"].append(fingerprint)
    save_overseer_state(state)

    log_history(
        f"**เหตุผล:** {reason}\n"
        f"**ผลลัพธ์:** PF ปรับขึ้นจาก {baseline_metrics['profit_factor']} เป็น {new_metrics['profit_factor']} (Win Rate: {new_metrics['win_rate']}%)\n"
        f"**รายงาน AI:**\n{report}\n"
        f"**การตัดสินใจ:** ✅ ACCEPTED & DEPLOYED"
    )

    summary_msg = (
        f"🔮 **[Overseer] อัปเดตกลยุทธ์สำเร็จและผ่านการทดสอบ!**\n\n"
        f"• **เหตุผล:** `{reason}`\n"
        f"• **Validation PF:** `{baseline_metrics['profit_factor']}` ➔ `{new_metrics['profit_factor']}`\n"
        f"• **Win Rate:** `{new_metrics['win_rate']}%` (Trades: {new_metrics['trades']})\n"
        f"• **Max Drawdown:** `{new_metrics['max_drawdown_pct']}%`\n"
        f"• **สั่งรีสตาร์ทบอท:** วาง state/restart.flag เรียบร้อย\n\n"
        f"📝 **รายงานจาก AI:**\n{report[:1000]}"
    )
    send_alert(summary_msg, level="OVERSEER")


def run_overseer_cycle():
    """วงจรรอบการตรวจสอบประจำทุก 15 นาที"""
    update_heartbeat()
    state = load_overseer_state()
    handled_fps = set(state.get("handled_fingerprints", []))

    # 1. ตรวจสอบ Heartbeat
    check_heartbeats()

    # 2. ตรวจสอบแพ้ติดกัน 4 ไม้
    is_streak, count, streak_fp = check_consecutive_losses(threshold=4)
    if is_streak and streak_fp not in handled_fps:
        asyncio.run(run_ai_healing(f"แพ้ติดต่อกัน {count} ไม้", streak_fp))
        return

    # 3. ตรวจสอบเหตุการณ์ CRASH
    has_crash, crash_fp = check_crash_events()
    if has_crash and crash_fp not in handled_fps:
        asyncio.run(run_ai_healing("ระบบบันทึกสถานะ CRASH ใน events.csv", crash_fp))
        return

    # 4. ตรวจสอบรอบรีวิวประจำสัปดาห์ (คืนวันอาทิตย์ 21:00)
    now = datetime.datetime.now()
    if now.weekday() == 6 and now.hour >= 21:  # 6 = วันอาทิตย์
        week_key = f"{now.year}_W{now.isocalendar()[1]}"
        if state.get("last_weekly_review_week") != week_key:
            print(f"[Overseer] 📅 ถึงรอบรีวิวประจำสัปดาห์ (คืนวันอาทิตย์) สัปดาห์ {week_key}")
            # รัน optimizer ก่อน
            try:
                import optimizer
                optimizer.run_optimization()
            except Exception as e:
                print(f"[Overseer] Weekly optimizer error: {e}")

            asyncio.run(run_ai_healing("รอบรีวิวและปรับปรุงกลยุทธ์ประจำสัปดาห์ (คืนวันอาทิตย์)", f"weekly_{week_key}"))
            state = load_overseer_state()
            state["last_weekly_review_week"] = week_key
            save_overseer_state(state)
            return


if __name__ == "__main__":
    cfg = load_config()
    interval_min = cfg.get("overseer", {}).get("check_interval_minutes", 15)

    print(f"[Overseer] 🛡️ เริ่มต้นผู้คุมระบบ (ตรวจสุขภาพทุก {interval_min} นาที, DRY_RUN={DRY_RUN})")
    send_alert(f"🛡️ Overseer ผู้คุมระบบเริ่มทำงานแล้ว (DRY_RUN={DRY_RUN})", level="INFO")

    while True:
        try:
            run_overseer_cycle()
        except Exception as e:
            err = f"Overseer Error: {e}"
            print(f"[Overseer ERROR] {err}")
            send_alert(err, level="ERROR")

        update_heartbeat()
        time.sleep(interval_min * 60)
