"""
mt5_docker_adapter.py — Local Docker MT5 (RPyC) Adapter for Linux Ubuntu
Provides a drop-in async connection & account interface compatible with trade_bot.py and discord_bot.py,
talking directly to the local `mt5-server` Docker container on port 18812 (0 Baht, no MetaApi fee).
"""
import asyncio
import datetime
import os
import socket
import time
import pandas as pd

try:
    import rpyc
    RPYC_AVAILABLE = True
except ImportError:
    RPYC_AVAILABLE = False


def is_docker_mt5_available(host: str = None, port: int = None) -> bool:
    """Check if local MT5 Docker RPyC server is listening on 127.0.0.1:18812."""
    if not RPYC_AVAILABLE:
        return False
    h = host or os.environ.get("MT5_DOCKER_HOST", "127.0.0.1")
    p = int(port or os.environ.get("MT5_DOCKER_PORT", "18812"))
    try:
        with socket.create_connection((h, p), timeout=1.5):
            return True
    except Exception:
        return False


class DockerMT5Connection:
    """Async adapter wrapping RPyC connection to MT5 running inside local Docker container."""

    TIMEFRAME_MAP = {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 16385,
        "4h": 16388,
        "1d": 16408,
    }

    def __init__(self, host: str = "127.0.0.1", port: int = 18812):
        self.host = host
        self.port = port
        self._conn = None
        self._mt5 = None
        self._closed = False
        self.state = "DEPLOYED"

    def get_rpc_connection(self):
        return self

    async def deploy(self):
        self.state = "DEPLOYED"

    def _ensure_connected_sync(self):
        if self._conn is not None:
            try:
                self._conn.ping()
                return
            except Exception:
                self._conn = None
                self._mt5 = None

        conn = rpyc.classic.connect(self.host, self.port)
        conn._config["sync_request_timeout"] = 60
        conn.execute("import sys, datetime; sys.path.append('C:\\\\mt5libs'); import MetaTrader5 as mt5")
        mt5 = conn.modules.MetaTrader5

        login_str = os.environ.get("MT5_LOGIN", "")
        login_id = int(login_str) if login_str and login_str.isdigit() else 0
        password = os.environ.get("MT5_PASSWORD", "")
        server = os.environ.get("MT5_SERVER", "MetaQuotes-Demo")

        init_kwargs = {"server": server, "portable": True}
        if login_id:
            init_kwargs["login"] = login_id
        if password:
            init_kwargs["password"] = password

        if not mt5.initialize("C:/MT5/terminal64.exe", **init_kwargs):
            err = mt5.last_error()
            raise RuntimeError(f"Docker MT5 initialize failed: {err}")

        term = mt5.terminal_info()
        if term and not term.trade_allowed:
            self._enable_algo_trading_sync(conn)

        self._conn = conn
        self._mt5 = mt5
        self._closed = False

    def _enable_algo_trading_sync(self, conn):
        """Enable Algo Trading (Ctrl+E) inside Wine via Win32 user32 API."""
        try:
            conn.execute("""
import ctypes, time
user32 = ctypes.windll.user32
hwnd = user32.FindWindowW("MetaQuotes::MetaTrader::5.00", None)
if hwnd:
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x45, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(0x45, 0, 2, 0)
    user32.keybd_event(0x11, 0, 2, 0)
    time.sleep(0.35)
""")
        except Exception as e:
            print(f"[Docker MT5] Warning toggling Algo Trading: {e}")

    async def connect(self):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_connected_sync)

    async def wait_synchronized(self):
        return True

    async def close(self):
        self._closed = True
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._mt5 = None

    def _get_account_info_sync(self) -> dict:
        self._ensure_connected_sync()
        acc = self._mt5.account_info()
        term = self._mt5.terminal_info()
        if acc is None:
            raise RuntimeError("Failed to retrieve MT5 account_info()")
        if term and not term.trade_allowed:
            self._enable_algo_trading_sync(self._conn)
            term = self._mt5.terminal_info()

        trade_mode = int(getattr(acc, "trade_mode", 0))
        acc_type = "ACCOUNT_TRADE_MODE_DEMO" if trade_mode == 0 else "ACCOUNT_TRADE_MODE_REAL"
        return {
            "login": int(acc.login),
            "server": str(acc.server),
            "balance": float(acc.balance),
            "equity": float(acc.equity),
            "type": acc_type,
            "tradeAllowed": bool((term.trade_allowed if term else True) and getattr(acc, "trade_allowed", True)),
        }

    async def get_account_information(self) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_account_info_sync)

    def _get_symbol_price_sync(self, symbol: str) -> dict:
        self._ensure_connected_sync()
        self._mt5.symbol_select(str(symbol), True)
        tick = self._mt5.symbol_info_tick(str(symbol))
        if tick is None or (float(tick.bid) <= 0 and float(tick.ask) <= 0):
            time.sleep(0.3)
            tick = self._mt5.symbol_info_tick(str(symbol))
        if tick is None:
            raise RuntimeError(f"Failed to retrieve tick for {symbol}")
        return {
            "symbol": str(symbol),
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "time": datetime.datetime.fromtimestamp(int(tick.time)),
        }

    async def get_symbol_price(self, symbol: str) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_symbol_price_sync, symbol)

    def _get_symbol_spec_sync(self, symbol: str) -> dict:
        self._ensure_connected_sync()
        self._mt5.symbol_select(str(symbol), True)
        s = self._mt5.symbol_info(str(symbol))
        if s is None:
            raise RuntimeError(f"Symbol {symbol} not found on MT5")
        t_mode = int(getattr(s, "trade_mode", 4))
        return {
            "symbol": str(symbol),
            "digits": int(getattr(s, "digits", 2)),
            "minVolume": float(getattr(s, "volume_min", 0.01) or 0.01),
            "maxVolume": float(getattr(s, "volume_max", 100.0) or 100.0),
            "volumeStep": float(getattr(s, "volume_step", 0.01) or 0.01),
            "contractSize": float(getattr(s, "trade_contract_size", 100.0) or 100.0),
            "tradeMode": "SYMBOL_TRADE_MODE_DISABLED" if t_mode == 0 else "SYMBOL_TRADE_MODE_FULL",
            "fillingMode": int(getattr(s, "filling_mode", 1)),
        }

    async def get_symbol_specification(self, symbol: str) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_symbol_spec_sync, symbol)

    def _get_historical_candles_sync(self, symbol: str, timeframe: str, limit: int = 300) -> list:
        self._ensure_connected_sync()
        self._mt5.symbol_select(str(symbol), True)
        tf_val = self.TIMEFRAME_MAP.get(timeframe, 15)
        rates = rpyc.classic.obtain(self._mt5.copy_rates_from_pos(str(symbol), int(tf_val), 0, int(limit)))
        if rates is None or len(rates) == 0:
            return []
        result = []
        for r in rates:
            result.append({
                "time": pd.to_datetime(int(r["time"]), unit="s"),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "tickVolume": float(r["tick_volume"]),
            })
        return result

    async def get_historical_candles(self, symbol: str, timeframe: str, start_time=None, limit: int = 300) -> list:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_historical_candles_sync, symbol, timeframe, limit)

    def _get_positions_sync(self) -> list:
        self._ensure_connected_sync()
        positions = self._mt5.positions_get()
        if not positions:
            return []
        result = []
        for p in positions:
            p_type = int(getattr(p, "type", 0))
            result.append({
                "id": str(p.ticket),
                "symbol": str(p.symbol),
                "type": "POSITION_TYPE_BUY" if p_type == 0 else "POSITION_TYPE_SELL",
                "volume": float(p.volume),
                "openPrice": float(p.price_open),
                "currentPrice": float(p.price_current),
                "stopLoss": float(p.sl),
                "takeProfit": float(p.tp),
                "profit": float(p.profit) + float(getattr(p, "swap", 0.0)),
                "magic": int(p.magic),
                "comment": str(getattr(p, "comment", "")),
            })
        return result

    async def get_positions(self) -> list:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_positions_sync)

    def _get_deals_sync(self, start_time: datetime.datetime, end_time: datetime.datetime) -> dict:
        self._ensure_connected_sync()
        s_ts = float(start_time.timestamp())
        e_ts = float(end_time.timestamp()) + 86400.0  # Include broker server timezone offset
        deals = self._conn.eval(
            f"mt5.history_deals_get(datetime.datetime.fromtimestamp({s_ts}), datetime.datetime.fromtimestamp({e_ts}))"
        )
        if not deals:
            return {"deals": []}
        result = []
        for d in deals:
            entry_val = int(getattr(d, "entry", 0))
            type_val = int(getattr(d, "type", 0))
            result.append({
                "id": str(d.ticket),
                "orderId": str(d.order),
                "positionId": str(getattr(d, "position_id", d.order)),
                "symbol": str(d.symbol),
                "magic": int(d.magic),
                "entryType": "DEAL_ENTRY_OUT" if entry_val == 1 else "DEAL_ENTRY_IN",
                "type": "DEAL_TYPE_BUY" if type_val == 0 else "DEAL_TYPE_SELL",
                "price": float(d.price),
                "volume": float(d.volume),
                "profit": float(d.profit),
                "commission": float(d.commission),
                "swap": float(d.swap),
            })
        return {"deals": result}

    async def get_deals_by_time_range(self, start_time: datetime.datetime, end_time: datetime.datetime) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_deals_sync, start_time, end_time)

    def _get_filling_mode_sync(self, s_info) -> int:
        modes = int(getattr(s_info, "filling_mode", 1)) if s_info else 1
        if modes & 1:
            return int(self._mt5.ORDER_FILLING_FOK)
        elif modes & 2:
            return int(self._mt5.ORDER_FILLING_IOC)
        return int(self._mt5.ORDER_FILLING_RETURN)

    def _place_market_order_sync(self, symbol: str, side: str, volume: float, stop_loss: float, take_profit: float, options: dict = None) -> dict:
        self._ensure_connected_sync()
        term = self._mt5.terminal_info()
        if term and not term.trade_allowed:
            self._enable_algo_trading_sync(self._conn)

        self._mt5.symbol_select(str(symbol), True)
        s_info = self._mt5.symbol_info(str(symbol))
        tick = self._mt5.symbol_info_tick(str(symbol))
        if s_info is None or tick is None:
            raise RuntimeError(f"Symbol or tick unavailable for {symbol}")

        digits = int(getattr(s_info, "digits", 2))
        is_buy = side.upper() == "BUY"
        price = round(float(tick.ask if is_buy else tick.bid), digits)
        order_type = int(self._mt5.ORDER_TYPE_BUY if is_buy else self._mt5.ORDER_TYPE_SELL)
        filling = self._get_filling_mode_sync(s_info)

        opts = options or {}
        magic = int(opts.get("magic", 123456))
        comment = str(opts.get("comment", "bot_v5"))

        req = {
            "action": int(self._mt5.TRADE_ACTION_DEAL),
            "symbol": str(symbol),
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": magic,
            "comment": comment,
            "type_time": int(self._mt5.ORDER_TIME_GTC),
            "type_filling": filling,
        }
        if stop_loss and float(stop_loss) > 0:
            req["sl"] = round(float(stop_loss), digits)
        if take_profit and float(take_profit) > 0:
            req["tp"] = round(float(take_profit), digits)

        res = self._conn.eval(f"mt5.order_send({repr(req)})")
        if res is None or int(res.retcode) != int(self._mt5.TRADE_RETCODE_DONE):
            code = int(res.retcode) if res else -1
            msg = str(res.comment) if res else str(self._mt5.last_error())
            raise RuntimeError(f"MT5 order_send failed ({code}): {msg}")

        order_id = str(res.order)
        return {
            "orderId": order_id,
            "positionId": order_id,
            "stringCode": "TRADE_RETCODE_DONE",
        }

    async def create_market_buy_order(self, symbol: str, volume: float, stop_loss: float = 0.0, take_profit: float = 0.0, options: dict = None) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._place_market_order_sync, symbol, "BUY", volume, stop_loss, take_profit, options)

    async def create_market_sell_order(self, symbol: str, volume: float, stop_loss: float = 0.0, take_profit: float = 0.0, options: dict = None) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._place_market_order_sync, symbol, "SELL", volume, stop_loss, take_profit, options)

    def _close_position_obj_sync(self, p) -> dict:
        sym = str(p.symbol)
        self._mt5.symbol_select(sym, True)
        s_info = self._mt5.symbol_info(sym)
        tick = self._mt5.symbol_info_tick(sym)
        if s_info is None or tick is None:
            raise RuntimeError(f"Tick unavailable to close position #{p.ticket}")

        is_buy_pos = int(p.type) == 0
        close_type = int(self._mt5.ORDER_TYPE_SELL if is_buy_pos else self._mt5.ORDER_TYPE_BUY)
        digits = int(getattr(s_info, "digits", 2))
        price = round(float(tick.bid if is_buy_pos else tick.ask), digits)
        filling = self._get_filling_mode_sync(s_info)

        req = {
            "action": int(self._mt5.TRADE_ACTION_DEAL),
            "symbol": sym,
            "volume": float(p.volume),
            "type": close_type,
            "position": int(p.ticket),
            "price": price,
            "deviation": 20,
            "magic": int(p.magic),
            "comment": "discord_close",
            "type_time": int(self._mt5.ORDER_TIME_GTC),
            "type_filling": filling,
        }
        res = self._conn.eval(f"mt5.order_send({repr(req)})")
        if res is None or int(res.retcode) != int(self._mt5.TRADE_RETCODE_DONE):
            code = int(res.retcode) if res else -1
            msg = str(res.comment) if res else str(self._mt5.last_error())
            raise RuntimeError(f"Failed to close #{p.ticket} ({code}): {msg}")
        return {"orderId": str(res.order), "stringCode": "TRADE_RETCODE_DONE"}

    def _close_position_by_id_sync(self, position_id: str) -> dict:
        self._ensure_connected_sync()
        positions = self._mt5.positions_get()
        target = None
        for p in (positions or []):
            if str(p.ticket) == str(position_id):
                target = p
                break
        if target is None:
            raise RuntimeError(f"Position #{position_id} not found on MT5")
        return self._close_position_obj_sync(target)

    def _close_positions_by_symbol_sync(self, symbol: str) -> dict:
        self._ensure_connected_sync()
        positions = self._mt5.positions_get()
        closed_count = 0
        for p in (positions or []):
            if str(p.symbol).upper() == str(symbol).upper():
                self._close_position_obj_sync(p)
                closed_count += 1
        return {"closedCount": closed_count, "stringCode": "TRADE_RETCODE_DONE"}

    async def close_position(self, position_id: str) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._close_position_by_id_sync, position_id)

    async def close_positions_by_symbol(self, symbol: str) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._close_positions_by_symbol_sync, symbol)
