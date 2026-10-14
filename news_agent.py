"""
news_agent.py — Economic News Sentiment Analyzer (AI Agent 1)
- Aggregates financial RSS feeds (filters items > 24h old and duplicates)
- Evaluates macroeconomic sentiment impact on Gold (XAUUSD) via Groq / Gemini
- Persists sentiment scores in state/sentiment.json
- Emits Discord alerts when score delta exceeds threshold
"""
import calendar
import datetime
import json
import os
import sys
import time
import feedparser

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
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from notifier import send_alert
from settings import BOT_DIR, STATE_DIR, load_config, secret


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def update_heartbeat():
    """Record heartbeat timestamp for overseer monitoring."""
    hb_file = STATE_DIR / "heartbeat_news_agent.txt"
    hb_file.write_text(datetime.datetime.now().isoformat(), encoding="utf-8")


def load_seen_news() -> list:
    """Load previously analyzed article URLs to prevent redundant AI API calls."""
    seen_file = STATE_DIR / "seen_news.json"
    if seen_file.exists():
        try:
            return json.loads(seen_file.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_seen_news(seen_list: list):
    """Save processed article URLs (retains last 500 entries)."""
    seen_file = STATE_DIR / "seen_news.json"
    seen_list = seen_list[-500:]
    seen_file.write_text(json.dumps(seen_list, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_news(rss_urls: list, max_age_hours: float = 24.0, max_articles: int = 10) -> list:
    """
    Fetch articles from RSS feeds with filtering:
    1. Filter out articles older than max_age_hours (default 24h)
    2. Filter out already processed URLs in seen_news.json
    """
    seen_links = set(load_seen_news())
    valid_articles = []
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    for url in rss_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                link = entry.get("link", "").strip()
                if not link or link in seen_links:
                    continue

                # กรองอายุข่าวจาก published_parsed
                if entry.get("published_parsed"):
                    pub_ts = calendar.timegm(entry.published_parsed)
                    pub_dt = datetime.datetime.fromtimestamp(pub_ts, tz=datetime.timezone.utc)
                    age_hours = (now_utc - pub_dt).total_seconds() / 3600.0
                    if age_hours > max_age_hours:
                        continue
                else:
                    # ถ้าไม่มี timestamp ระบุ อนุโลมให้ผ่าน
                    pass

                title = entry.get("title", "").strip()
                summary = entry.get("summary", "").strip()[:500]

                valid_articles.append({
                    "title": title,
                    "summary": summary,
                    "link": link,
                    "source": feed.feed.get("title", url),
                    "published": entry.get("published", ""),
                    "pub_dt": pub_dt if entry.get("published_parsed") else now_utc
                })

        except Exception as e:
            print(f"[News Agent] เกิดข้อผิดพลาดในการดึง {url}: {e}")

    # Sort by publish date descending
    valid_articles.sort(key=lambda x: x["pub_dt"], reverse=True)
    
    # Remove the temporary pub_dt key
    for a in valid_articles:
        a.pop("pub_dt", None)

    return valid_articles[:max_articles]


def analyze_with_groq(prompt: str, groq_key: str, model_name: str = "qwen/qwen3.8-27b") -> dict:
    """
    วิเคราะห์ Sentiment ผ่าน Groq API
    ความเร็วสูงพิเศษ (Latency < 1s) ฟรี และไม่ติดปัญหาคิว 503
    มีระบบ Auto-Fallback ลองสลับโมเดลอัตโนมัติหากโมเดลใดโมเดลหนึ่งติด 404 หรือ Decommissioned
    """
    client = None
    if GROQ_AVAILABLE:
        client = Groq(api_key=groq_key)
    elif OPENAI_AVAILABLE:
        client = OpenAI(api_key=groq_key, base_url="https://api.groq.com/openai/v1")
    else:
        return {"ok": False, "error": "ไม่มีไลบรารี groq หรือ openai ติดตั้งอยู่ในระบบ"}

    candidate_models = [model_name, "qwen/qwen3.8-27b", "llama-3.3-70b-versatile", "llama-3.1-8b-instant"]
    seen_models = set()
    models_to_try = []
    for m in candidate_models:
        if m and m not in seen_models:
            seen_models.add(m)
            models_to_try.append(m)

    last_err = ""
    for current_model in models_to_try:
        try:
            completion = client.chat.completions.create(
                model=current_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a world-class economic and financial analyst specializing in Gold (XAUUSD). You must respond strictly in JSON format with keys 'score' and 'reasoning'."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=300,
            )

            raw_text = completion.choices[0].message.content.strip()
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                raw_text = "\n".join(lines).strip()

            data = json.loads(raw_text)
            score = float(data.get("score", 0.0))
            score = max(-1.0, min(1.0, score))

            return {
                "ok": True,
                "score": round(score, 2),
                "reasoning": data.get("reasoning", "วิเคราะห์ด้วย Groq สำเร็จ"),
                "provider": f"Groq ({current_model})"
            }
        except Exception as e:
            last_err = str(e)
            continue

    return {"ok": False, "error": last_err}


def analyze_with_gemini(prompt: str, gemini_key: str, model_name: str) -> dict:
    """
    วิเคราะห์ Sentiment สำรองผ่าน Gemini AI (google.genai) พร้อม Retry
    """
    if not GEMINI_AVAILABLE:
        return {"ok": False, "reasoning": "ไม่ได้ติดตั้งไลบรารี google-genai"}

    try:
        client = genai.Client(api_key=gemini_key)
    except Exception as e:
        return {"ok": False, "reasoning": f"ไม่สามารถสร้าง Gemini Client: {e}"}

    retries = [10, 30, 60]
    transient_markers = ("429", "quota", "503", "unavailable", "overloaded", "deadline")
    for attempt in range(len(retries) + 1):
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={"response_mime_type": "application/json"}
            )
            raw_text = resp.text.strip()
            if raw_text.startswith("```"):
                lines = raw_text.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                raw_text = "\n".join(lines).strip()

            data = json.loads(raw_text)

            score = float(data.get("score", 0.0))
            score = max(-1.0, min(1.0, score))

            return {
                "ok": True,
                "score": round(score, 2),
                "reasoning": data.get("reasoning", "วิเคราะห์ด้วย Gemini สำเร็จ"),
                "provider": f"Gemini ({model_name})"
            }
        except Exception as e:
            err_str = str(e)
            print(f"[News Agent] Gemini Error (ครั้งที่ {attempt + 1}): {err_str}")
            if any(m in err_str.lower() for m in transient_markers):
                if attempt < len(retries):
                    wait_sec = retries[attempt]
                    print(f"[News Agent] ⏳ Gemini ติดโควต้า/คิวเต็ม รอ {wait_sec} วินาทีก่อนลองใหม่...")
                    time.sleep(wait_sec)
                    continue
            return {"ok": False, "score": 0.0, "reasoning": f"เกิดข้อผิดพลาดในการเรียก Gemini: {err_str[:100]}"}

    return {"ok": False, "score": 0.0, "reasoning": "Gemini โควต้าเต็ม (ไม่สามารถวิเคราะห์ได้)"}


def analyze_sentiment(articles: list, model_name: str) -> dict:
    """
    ส่งข่าวใหม่ทั้งหมด (Batch) ให้ AI วิเคราะห์:
    1. ถ้ามี GROQ_API_KEY จะใช้ Groq (llama-3.3-70b-versatile) เป็นอันดับแรก (ฟรี เร็ว 0.3s ไม่ติด 503)
    2. หากไม่มีคีย์ Groq หรือ Groq มีปัญหา จะ Fallback สลับไปใช้ Gemini ทันที
    3. หากทั้งคู่ใช้ไม่ได้ จะคืนค่าสถานะ ok=False เพื่อความปลอดภัย
    """
    news_text = ""
    for i, a in enumerate(articles, 1):
        news_text += f"\n--- Article {i} ---\nTitle: {a['title']}\nSummary: {a['summary']}\n"

    prompt = f"""You are a professional financial analyst specializing in Gold (XAUUSD).
Evaluate the overall impact of the following economic news on Gold price:

{news_text}

Respond strictly in valid JSON format:
{{
    "score": <float between -1.0 and 1.0>,
    "reasoning": "<concise 1-2 sentence explanation in Thai language>"
}}

Scoring guidelines:
-1.0 = Highly bearish for Gold (e.g. rate hikes, strong USD surge)
 0.0 = Neutral / negligible impact / mixed signals
+1.0 = Highly bullish for Gold (e.g. rate cuts, rising inflation, geopolitical conflict)

IMPORTANT: The reasoning field MUST be written in concise Thai language for Discord display.
"""

    # 1) ตรวจสอบ GROQ_API_KEY
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        print("[News Agent] ⚡ ตรวจพบ GROQ_API_KEY! ส่งวิเคราะห์ผ่าน Groq (qwen/qwen3.8-27b)...")
        res = analyze_with_groq(prompt, groq_key, model_name="qwen/qwen3.8-27b")
        if res.get("ok"):
            print(f"[News Agent] ⚡ Groq ประมวลผลสำเร็จ ({res.get('provider')}): Score={res.get('score')}")
            return res
        print(f"[News Agent] ⚠️ Groq เกิดข้อผิดพลาด ({res.get('error')}) -> กำลังสลับไปใช้ Gemini สำรอง...")
    else:
        print("[News Agent] ℹ️ ไม่พบ GROQ_API_KEY ในระบบ สลับไปใช้งาน Gemini สำรอง...")

    # 2) Fallback ไปยัง Gemini
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        print(f"[News Agent] 🔄 กำลังส่งวิเคราะห์ผ่าน Gemini ({model_name})...")
        res = analyze_with_gemini(prompt, gemini_key, model_name=model_name)
        if res.get("ok"):
            print(f"[News Agent] ✅ Gemini ประมวลผลสำเร็จ: Score={res.get('score')}")
            return res
        print(f"[News Agent] ❌ Gemini สำรองล้มเหลว: {res.get('reasoning')}")
        return res

    return {
        "ok": False,
        "score": 0.0,
        "reasoning": "ไม่พบคีย์ AI ที่พร้อมใช้งานใน .env (กรุณาระบุ GROQ_API_KEY หรือ GEMINI_API_KEY)"
    }



def save_sentiment(result: dict):
    """บันทึกข้อมูล sentiment ลง state/sentiment.json"""
    sentiment_file = STATE_DIR / "sentiment.json"
    result["timestamp"] = datetime.datetime.now().isoformat()
    sentiment_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def get_previous_sentiment() -> float | None:
    """อ่านคะแนน sentiment ล่าสุดก่อนหน้านี้"""
    sentiment_file = STATE_DIR / "sentiment.json"
    if sentiment_file.exists():
        try:
            data = json.loads(sentiment_file.read_text(encoding="utf-8"))
            return float(data.get("score", 0.0))
        except Exception:
            return None
    return None


def run_once():
    """วงจรสแกนข่าว 1 รอบ"""
    update_heartbeat()
    cfg = load_config()
    n_cfg = cfg.get("news", {})

    feeds = n_cfg.get("rss_feeds", [])
    max_age = float(n_cfg.get("max_article_age_hours", 24))
    model_name = n_cfg.get("model", "gemini-3.8-flash")

    print(f"\n[News Agent] 🔎 กำลังสแกนข่าวเศรษฐกิจ ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')})...")
    articles = fetch_news(feeds, max_age_hours=max_age, max_articles=8)

    if not articles:
        print("[News Agent] ✅ ไม่มีข่าวใหม่ที่ยังไม่เคยวิเคราะห์ (ข้ามการเรียก AI เพื่อประหยัดโควต้า)")
        return

    print(f"[News Agent] 📰 พบข่าวใหม่ {len(articles)} ข่าว กำลังส่งให้ AI วิเคราะห์...")
    result = analyze_sentiment(articles, model_name=model_name)

    # ถ้า AI ล้มเหลว: ห้ามจดว่าข่าวอ่านแล้ว (รอบหน้าจะได้ลองใหม่) และห้ามเขียนทับ sentiment เดิมด้วย 0.00 ปลอม
    # sentiment เดิมจะหมดอายุเองตาม sentiment_max_age_hours แล้วบอทเทรดจะใช้ 0.0 เอง
    if not result.get("ok", False):
        print(f"[News Agent] ⚠️ วิเคราะห์ไม่สำเร็จ ({result.get('reasoning')}) "
              f"→ ไม่บันทึกข่าว/คะแนน จะลองข่าวชุดเดิมใหม่รอบหน้า")
        return
    result.pop("ok", None)

    prev_score = get_previous_sentiment()
    new_score = result.get("score", 0.0)

    # บันทึกข่าวที่อ่านแล้ว (เฉพาะเมื่อ AI วิเคราะห์สำเร็จ)
    seen_links = load_seen_news()
    for a in articles:
        seen_links.append(a["link"])
    save_seen_news(seen_links)

    # บันทึกคะแนนใหม่
    save_sentiment(result)
    print(f"[News Agent] บันทึกผล Sentiment สำเร็จ: Score={new_score:+.2f} ({result.get('reasoning')})")

    # ตรวจสอบว่าต้องแจ้ง Discord หรือไม่ (เมื่อคะแนนเปลี่ยน > 0.2 หรือรันครั้งแรก)
    should_alert = False
    if prev_score is None:
        should_alert = True
    elif abs(new_score - prev_score) >= 0.2:
        should_alert = True

    if should_alert:
        mood = "บวกต่อทองคำ 🟢" if new_score > 0.15 else ("ลบต่อทองคำ 🔴" if new_score < -0.15 else "เป็นกลาง ⚪")
        links_md = ""
        for i, a in enumerate(articles[:5], 1):
            links_md += f"{i}. [{a['title']}]({a['link']})\n"

        prev_line = f"• **คะแนนก่อนหน้า:** `{prev_score:+.2f}`\n" if prev_score is not None else ""
        msg = (
            f"**รายงานอารมณ์ตลาด (Market Sentiment)**\n\n"
            f"• **คะแนนอารมณ์:** `{new_score:+.2f}` ({mood})\n"
            f"{prev_line}"
            f"• **บทวิเคราะห์ AI:** {result.get('reasoning')}\n\n"
            f"🔗 **ข่าวเด่นรอบนี้ ({len(articles)} ข่าว):**\n{links_md}"
        )
        send_alert(msg, level="INFO")


if __name__ == "__main__":
    cfg = load_config()
    interval_min = cfg.get("news", {}).get("check_interval_minutes", 30)

    print(f"[News Agent] เริ่มต้นระบบอ่านข่าว (ตรวจทุก {interval_min} นาที)")
    send_alert("📰 News Agent เริ่มทำงานแล้ว!", level="INFO")

    while True:
        try:
            run_once()
        except Exception as e:
            err_msg = f"News Agent เกิดข้อผิดพลาด: {e}"
            print(f"[ERROR] {err_msg}")
            send_alert(err_msg, level="ERROR")

        update_heartbeat()
        time.sleep(interval_min * 60)
