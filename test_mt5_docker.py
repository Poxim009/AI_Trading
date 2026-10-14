"""
test_mt5_docker.py — Test all MT5 methods over RPyC on Ubuntu Docker
"""
import datetime
import rpyc
import pandas as pd

def main():
    conn = rpyc.classic.connect("127.0.0.1", 18812)
    conn._config["sync_request_timeout"] = 60
    conn.execute("import sys, datetime; sys.path.append('C:\\\\mt5libs'); import MetaTrader5 as mt5")
    mt5 = conn.modules.MetaTrader5
    
    import os
    from dotenv import load_dotenv
    load_dotenv()

    login_str = os.environ.get("MT5_LOGIN", "")
    login_id = int(login_str) if login_str and login_str.isdigit() else 0
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "MetaQuotes-Demo")

    init_kwargs = {"server": server, "portable": True}
    if login_id:
        init_kwargs["login"] = login_id
    if password:
        init_kwargs["password"] = password

    ok = mt5.initialize("C:/MT5/terminal64.exe", **init_kwargs)
    print("initialize:", ok)
    
    now = datetime.datetime.now()
    from_date = now - datetime.timedelta(days=7)
    deals = conn.eval(f"mt5.history_deals_get(datetime.datetime.fromtimestamp({from_date.timestamp()}), datetime.datetime.fromtimestamp({now.timestamp()}))")
    print("deals count (last 7 days):", len(deals) if deals else 0)
    if deals:
        for d in deals[-3:]:
            print(f"  Deal #{d.ticket}: entry={d.entry}, type={d.type}, vol={d.volume}, price={d.price}, profit={d.profit}")

    rates = rpyc.classic.obtain(mt5.copy_rates_from_pos("XAUUSD", int(mt5.TIMEFRAME_M15), 0, 10))
    df = pd.DataFrame(rates)
    print("rates df shape:", df.shape, "columns:", list(df.columns))

if __name__ == "__main__":
    main()
