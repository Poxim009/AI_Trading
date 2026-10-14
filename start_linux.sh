#!/usr/bin/env bash
# ==============================================================================
# start_linux.sh — Setup & Launch Script for AI Trading Bot 2 (Ubuntu 24.04 LTS)
# ==============================================================================

set -e

# ไปยังโฟลเดอร์ของสคริปต์
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "================================================================="
echo "  🚀 AI Trading Bot 2 (Linux & Cloud MetaApi Edition)"
echo "  Target OS: Ubuntu 24.04 LTS | Python 3.12+"
echo "================================================================="

# ตรวจสอบ Python 3
if ! command -v python3 &> /dev/null; then
    echo "❌ ไม่พบ python3 ในระบบ กรุณาติดตั้งด้วย: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi

echo "📌 Python Version: $(python3 --version)"

# ตรวจสอบและสร้าง Virtual Environment (.venv)
if [ ! -d ".venv" ]; then
    echo "📦 กำลังสร้าง Virtual Environment (.venv)..."
    python3 -m venv .venv
fi

# เปิดใช้งาน Virtual Environment
echo "🔌 เปิดใช้งาน Virtual Environment..."
source .venv/bin/activate

# ตรวจสอบและติดตั้ง Dependencies
echo "📥 ตรวจสอบและติดตั้ง Packages จาก requirements.txt..."
pip install --upgrade pip
pip install -r requirements.txt

# ตรวจสอบไฟล์ .env
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        echo "⚠️ ไม่พบ .env ทำการคัดลอกตัวอย่างจาก .env.example..."
        cp .env.example .env
        echo "💡 กรุณาระบุ METAAPI_TOKEN, DISCORD_BOT_TOKEN ในไฟล์ .env"
    fi
fi

echo "================================================================="
echo "  ✅ สภาพแวดล้อมพร้อมแล้ว กำลังเริ่มรัน Supervisor (run_all.py)..."
echo "================================================================="

# รันระบบทั้งหมดผ่าน Supervisor
exec python3 -u run_all.py
