"""
run_all.py — Process Supervisor & Watchdog (Linux & Cloud Edition)
- Concurrently launches news_agent.py, trade_bot.py, overseer.py, discord_bot.py
- Designed for Ubuntu Linux with native Systemd service compatibility
- Handles SIGTERM / SIGINT signals for clean graceful shutdowns
- Automatically restarts failed child processes
- Monitors state/restart.flag to reload trade_bot upon parameter updates
"""
import os
import signal
import subprocess
import sys
import time
from settings import BOT_DIR, STATE_DIR

SERVICES = ["news_agent.py", "trade_bot.py", "overseer.py", "discord_bot.py"]
procs = {}
is_shutting_down = False


def start_service(name: str):
    """Launch child service with unbuffered Python execution."""
    script_path = BOT_DIR / name
    if not script_path.exists():
        print(f"[run_all] ❌ File not found: {script_path}")
        return

    # Use unbuffered mode (-u) for instantaneous journalctl and console logging
    p = subprocess.Popen(
        [sys.executable, "-u", str(script_path)],
        cwd=BOT_DIR
    )
    procs[name] = p
    print(f"[run_all] ▶ Started {name} (PID: {p.pid})")


def shutdown(signum=None, frame=None):
    """Graceful signal handler to prevent zombie processes."""
    global is_shutting_down
    if is_shutting_down:
        return
    is_shutting_down = True

    sig_name = "SIGTERM" if signum == signal.SIGTERM else ("SIGINT" if signum == signal.SIGINT else str(signum))
    print(f"\n[run_all] 🛑 Received {sig_name}; shutting down all services gracefully...")

    for name, p in list(procs.items()):
        if p.poll() is None:
            print(f"[run_all] ⏳ Terminating {name} (PID: {p.pid})...")
            p.terminate()

    # Wait for processes to exit
    for name, p in list(procs.items()):
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"[run_all] ⚠️ {name} did not exit in time; killing process")
            p.kill()

    print("[run_all] ✅ All services stopped cleanly")
    sys.exit(0)


def main():
    print("=" * 65)
    print("  🚀 Starting AI Trading Bot 2 (Linux Cloud Supervisor)")
    print("=" * 65)

    # Register POSIX / Systemd termination signal handlers
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for s in SERVICES:
        start_service(s)

    try:
        from notifier import send_alert
        send_alert(
            "🚀 **[AI Trading System 2 เริ่มต้นการทำงานบน Linux]**\n"
            "• โฮสต์: Ubuntu 24.04 LTS (Cloud Environment)\n"
            "• บริการ: `news_agent`, `trade_bot`, `overseer`, `discord_bot`\n"
            "• ระบบเชื่อมต่อ: MetaApi Cloud SDK & WebSocket\n"
            "• พร้อมรับคำสั่ง Discord และเฝ้าดูกราฟอัตโนมัติ...",
            level="INFO"
        )
    except Exception as e:
        print(f"[run_all] Failed to send startup alert: {e}")

    while not is_shutting_down:
        try:
            # 1. Check for stopped processes and auto-recover
            for name, p in list(procs.items()):
                if p.poll() is not None:
                    ret_code = p.returncode
                    print(f"[run_all] ⚠️ Service {name} stopped (Exit Code: {ret_code}); restarting in 10s...")
                    time.sleep(10)
                    start_service(name)

            # 2. Check for strategy restart flag
            flag_file = STATE_DIR / "restart.flag"
            if flag_file.exists():
                try:
                    flag_file.unlink()
                except Exception:
                    pass

                print("[run_all] 🔄 ตรวจพบ restart.flag กำลังรีสตาร์ท trade_bot.py...")
                if "trade_bot.py" in procs:
                    p = procs["trade_bot.py"]
                    p.terminate()
                    try:
                        p.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        p.kill()
                    start_service("trade_bot.py")

            time.sleep(5)
        except KeyboardInterrupt:
            shutdown()
        except Exception as e:
            print(f"[run_all] ข้อผิดพลาดในลูป Supervisor: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
