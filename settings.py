import json
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent
LOG_DIR = BOT_DIR / "logs"
STATE_DIR = BOT_DIR / "state"
DATA_DIR = BOT_DIR / "data"
BACKUP_DIR = BOT_DIR / "backups"

# Create required directories if not present
for d in (LOG_DIR, STATE_DIR, DATA_DIR, BACKUP_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Load environment variables from .env
load_dotenv(BOT_DIR / ".env")

# Ensure UTF-8 console encoding on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_config() -> dict:
    config_file = BOT_DIR / "config.json"
    if not config_file.exists():
        return {}
    return json.loads(config_file.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    config_file = BOT_DIR / "config.json"
    temp_file = BOT_DIR / "config.json.tmp"
    temp_file.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_file.replace(config_file)


def load_params() -> dict:
    params_file = BOT_DIR / "strategy_params.json"
    if not params_file.exists():
        return {}
    return json.loads(params_file.read_text(encoding="utf-8"))


def save_params(params: dict) -> None:
    params_file = BOT_DIR / "strategy_params.json"
    temp_file = BOT_DIR / "strategy_params.json.tmp"
    temp_file.write_text(json.dumps(params, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_file.replace(params_file)


def load_trading_control() -> dict:
    import datetime
    ctrl_file = STATE_DIR / "trading_control.json"
    if not ctrl_file.exists():
        return {
            "trading_enabled": True,
            "reason": "default_active",
            "updated_at": datetime.datetime.now().isoformat()
        }
    try:
        return json.loads(ctrl_file.read_text(encoding="utf-8"))
    except Exception:
        return {
            "trading_enabled": True,
            "reason": "corrupted_fallback",
            "updated_at": datetime.datetime.now().isoformat()
        }


def save_trading_control(data: dict) -> None:
    ctrl_file = STATE_DIR / "trading_control.json"
    temp_file = STATE_DIR / "trading_control.json.tmp"
    temp_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_file.replace(ctrl_file)


PLACEHOLDER_MARKERS = [
    "your_metaapi_token_here",
    "your_metaapi_account_id_here",
    "your_discord_bot_token_here",
    "your_discord_webhook_url_here",
    "your_groq_api_key_here",
    "placeholder",
    "changeme",
    "todo",
]


def is_placeholder(val: str | None) -> bool:
    if not val:
        return True
    s = val.strip().lower()
    return any(p in s for p in PLACEHOLDER_MARKERS) or s.startswith("your_")


def secret(name: str, default: str | None = None) -> str:
    val = os.environ.get(name)
    if not val or val.strip() == "":
        if default is not None:
            return default
        raise RuntimeError(f"ไม่พบค่า {name} ในไฟล์ .env (กรุณาระบุค่าใน .env)")
    return val.strip()


def is_metaapi_configured() -> bool:
    """Verify whether valid MetaApi credentials are provided (non-empty, non-placeholder)."""
    token = os.environ.get("METAAPI_TOKEN", "").strip()
    acc_id = os.environ.get("METAAPI_ACCOUNT_ID", "").strip()
    if not token or not acc_id:
        return False
    if is_placeholder(token) or is_placeholder(acc_id):
        return False
    return True


def get_metaapi_instructions() -> str:
    """Return user setup guide for MetaApi on Linux."""
    return (
        "💡 **คำแนะนำการตั้งค่า MetaApi (สำหรับ MT5 บน Linux):**\n"
        "1. สมัครบัญชีและสร้าง API Token ที่ https://app.metaapi.cloud\n"
        "2. เพิ่มบัญชี MT5 ของคุณลงใน MetaApi แล้วคัดลอก `Account ID`\n"
        "3. ใส่ค่าลงในไฟล์ `.env`:\n"
        "   ```ini\n"
        "   METAAPI_TOKEN=\"<ใส่ MetaApi Token ที่นี่>\"\n"
        "   METAAPI_ACCOUNT_ID=\"<ใส่ MetaApi Account ID ที่นี่>\"\n"
        "   ```\n"
        "4. เมื่อบันทึกไฟล์เรียบร้อยแล้ว บอทจะเริ่มเชื่อมต่อ MT5 ผ่าน Cloud อัตโนมัติ"
    )
