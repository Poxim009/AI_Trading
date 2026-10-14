"""
notifier.py — Discord Webhook Notification Dispatcher (Linux & Cloud Edition)
Dispatches trading signals, fills, backtest summaries, and system alerts to Discord.
"""
import datetime
import os
import requests
from settings import secret, is_placeholder, LOG_DIR

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    from google import genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False



def send_alert(message: str, level: str = "INFO"):
    """
    Send formatted alert embed message to Discord via webhook.
    """
    try:
        webhook_url = secret("DISCORD_WEBHOOK_URL")
    except Exception as e:
        print(f"[Discord] ไม่สามารถดึง Webhook URL: {e}")
        return

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ตัดข้อความไม่ให้เกิน 4000 ตัวอักษร
    if len(message) > 4000:
        message = message[:3990] + "... (truncated)"

    styles = {
        "INFO":     {"color": 3447003,  "icon": "ℹ️"},   # สีน้ำเงิน
        "TRADE":    {"color": 5763719,  "icon": "💰"},   # สีเขียว
        "ERROR":    {"color": 15548997, "icon": "🚨"},   # สีแดง
        "SUMMARY":  {"color": 16776960, "icon": "📊"},   # สีเหลือง
        "OVERSEER": {"color": 10181046, "icon": "🔮"},   # สีม่วง
        "WARNING":  {"color": 15105570, "icon": "⚠️"},   # สีส้ม
    }
    style = styles.get(level, {"color": 9807270, "icon": "📌"})

    payload = {
        "username": "AI Trading Bot [Linux]",
        "embeds": [{
            "title": f"{style['icon']} [{level}]",
            "description": message,
            "color": style["color"],
            "footer": {"text": f"🕐 {now} | Linux Cloud Edition"}
        }]
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        if response.status_code == 204:
            print(f"[Discord] ส่งสำเร็จ: {message[:50]}...")
        else:
            print(f"[Discord] ส่งไม่สำเร็จ: {response.status_code} {response.text}")
    except Exception as e:
        print(f"[Discord] Error: {e}")


def _log_silent_error(error_msg: str):
    """Silently write raw error to log file."""
    log_file = LOG_DIR / "silent_errors.log"
    try:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{now_str}] {error_msg}\n")
    except Exception:
        pass


def send_ai_error_alert(raw_error: str, context: str = ""):
    """
    Supreme Error Catcher: Summarize root cause via AI diagnostics before notifying Discord.
    Uses Groq (llama-3.3-70b-versatile) first, and falls back to Gemini (gemini-2.0-flash).
    """
    prompt = (
        "You are an Error Diagnostics agent for a Linux trading bot.\n"
        "Summarize the root cause in 1 concise, direct sentence in Thai language for user notification.\n"
        f"Context: {context}\n"
        f"Raw Error: {raw_error}"
    )

    try:
        groq_key = secret("GROQ_API_KEY")
    except Exception:
        groq_key = os.environ.get("GROQ_API_KEY", "").strip() or None

    if groq_key and not is_placeholder(groq_key) and (GROQ_AVAILABLE or OPENAI_AVAILABLE):
        try:
            client = Groq(api_key=groq_key) if GROQ_AVAILABLE else OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1")
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=200,
            )
            translated_error = completion.choices[0].message.content.strip()
            msg_context = f"**Context**: {context}\n" if context else ""
            discord_msg = f"**Supreme Error Catcher** 🚨\n{msg_context}**ปัญหา**: {translated_error}"
            send_alert(discord_msg, level="ERROR")
            return
        except Exception as e:
            _log_silent_error(f"Groq Translator Error: {e} | Falling back to Gemini")

    try:
        api_key = secret("GEMINI_API_KEY")
    except Exception:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip() or None

    if not api_key or is_placeholder(api_key) or not GENAI_AVAILABLE:
        _log_silent_error(f"Raw Error ({context}): {raw_error}")
        return

    client = genai.Client(api_key=api_key)
    last_err = None
    for gemini_model in ("gemini-2.0-flash", "gemini-3.8-flash"):
        try:
            resp = client.models.generate_content(
                model=gemini_model,
                contents=prompt,
            )
            translated_error = resp.text.strip()
            msg_context = f"**Context**: {context}\n" if context else ""
            discord_msg = f"**Supreme Error Catcher** 🚨\n{msg_context}**ปัญหา**: {translated_error}"
            send_alert(discord_msg, level="ERROR")
            return
        except Exception as e:
            last_err = e

    _log_silent_error(f"AI Translator Error: {last_err} | Raw Error: {raw_error}")



if __name__ == "__main__":
    send_alert("🔔 ทดสอบระบบแจ้งเตือน - AI Trading Bot 2 (Linux Edition) เริ่มทำงาน!", level="INFO")
    print("ทดสอบส่งข้อความแจ้งเตือนเรียบร้อย")
