"""
test_suite.py — Comprehensive Unit & Integration Tests for AI Trading Bot 2 (Linux & Cloud Edition)
"""
import asyncio
import datetime
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd

import strategy
import simulator
import settings
import trade_bot
import discord_bot


class TestStrategyVersion5(unittest.TestCase):
    def setUp(self):
        self.params = settings.load_params()
        # Synthetic DataFrame 100 bars
        np.random.seed(42)
        base = 2500.0
        prices = [base]
        for _ in range(99):
            prices.append(prices[-1] + np.random.normal(0, 2.0))
        prices = np.array(prices)
        highs = prices + np.random.uniform(0.5, 3.0, 100)
        lows = prices - np.random.uniform(0.5, 3.0, 100)
        opens = prices + np.random.uniform(-1.0, 1.0, 100)
        closes = prices

        self.df = pd.DataFrame({
            "time": pd.date_range("2026-01-01", periods=100, freq="15min"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.random.uniform(100, 1000, 100)
        })

    def test_add_indicators(self):
        df_ind = strategy.add_indicators(self.df, self.params)
        for col in ("SMA", "RSI", "ATR", "ADX"):
            self.assertIn(col, df_ind.columns, f"Indicator column {col} missing")
            self.assertFalse(df_ind[col].iloc[-10:].isna().any(), f"Indicator {col} contains NaN in recent bars")

    def test_adx_filter_returns_wait(self):
        df_ind = strategy.add_indicators(self.df, self.params)
        strict_params = dict(self.params)
        strict_params["adx_threshold"] = 99.0
        signal = strategy.generate_signal(df_ind.iloc[-5:], strict_params)
        self.assertEqual(signal, "WAIT", "Should return WAIT when ADX < adx_threshold")

    def test_buy_and_sell_signals(self):
        df_buy = pd.DataFrame([
            {"close": 2505.0, "SMA": 2500.0, "RSI": 40.0, "ADX": 30.0},
            {"close": 2510.0, "SMA": 2500.0, "RSI": 48.0, "ADX": 30.0},
        ])
        sig_buy = strategy.generate_signal(df_buy, self.params, sentiment=0.0)
        self.assertEqual(sig_buy, "BUY", "Expected BUY when close > SMA and RSI crosses above buy level")

        df_sell = pd.DataFrame([
            {"close": 2495.0, "SMA": 2500.0, "RSI": 60.0, "ADX": 30.0},
            {"close": 2490.0, "SMA": 2500.0, "RSI": 52.0, "ADX": 30.0},
        ])
        sig_sell = strategy.generate_signal(df_sell, self.params, sentiment=0.0)
        self.assertEqual(sig_sell, "SELL", "Expected SELL when close < SMA and RSI crosses below sell level")

    def test_sentiment_filter(self):
        df_buy = pd.DataFrame([
            {"close": 2505.0, "SMA": 2500.0, "RSI": 40.0, "ADX": 30.0},
            {"close": 2510.0, "SMA": 2500.0, "RSI": 48.0, "ADX": 30.0},
        ])
        # Negative sentiment cancels BUY
        sig_blocked = strategy.generate_signal(df_buy, self.params, sentiment=-0.5)
        self.assertIsNone(sig_blocked, "Negative sentiment should filter out BUY signal")

    def test_sl_tp_calculation(self):
        entry = 2500.0
        atr = 10.0
        sl_buy, tp_buy = strategy.sl_tp("BUY", entry, atr, self.params)
        self.assertAlmostEqual(sl_buy, 2500.0 - 10.0 * self.params["atr_sl_mult"])
        self.assertAlmostEqual(tp_buy, 2500.0 + 10.0 * self.params["atr_tp_mult"])

        sl_sell, tp_sell = strategy.sl_tp("SELL", entry, atr, self.params)
        self.assertAlmostEqual(sl_sell, 2500.0 + 10.0 * self.params["atr_sl_mult"])
        self.assertAlmostEqual(tp_sell, 2500.0 - 10.0 * self.params["atr_tp_mult"])


class TestSettingsAndMetaApiConfig(unittest.TestCase):
    def test_paths_exist(self):
        self.assertTrue(settings.BOT_DIR.exists())
        self.assertTrue(settings.LOG_DIR.exists())
        self.assertTrue(settings.STATE_DIR.exists())
        self.assertTrue(settings.DATA_DIR.exists())

    def test_load_and_save_config(self):
        cfg = settings.load_config()
        self.assertIn("trading", cfg)
        self.assertIn("symbol", cfg["trading"])

    def test_load_and_save_params(self):
        params = settings.load_params()
        self.assertEqual(params.get("version"), 5)
        self.assertEqual(params.get("sma_period"), 50)
        self.assertEqual(params.get("adx_threshold"), 25)

    def test_placeholder_detection(self):
        self.assertTrue(settings.is_placeholder("your_metaapi_token_here"))
        self.assertTrue(settings.is_placeholder("your_metaapi_account_id_here"))
        self.assertTrue(settings.is_placeholder(""))
        self.assertTrue(settings.is_placeholder(None))
        self.assertFalse(settings.is_placeholder("real_token_1234567890abcdef"))

    def test_is_metaapi_configured(self):
        with patch.dict(os.environ, {"METAAPI_TOKEN": "your_metaapi_token_here", "METAAPI_ACCOUNT_ID": "your_id"}):
            self.assertFalse(settings.is_metaapi_configured())

        with patch.dict(os.environ, {"METAAPI_TOKEN": "valid_token_xyz", "METAAPI_ACCOUNT_ID": "acc_12345"}):
            self.assertTrue(settings.is_metaapi_configured())

    def test_instructions_in_thai(self):
        instructions = settings.get_metaapi_instructions()
        self.assertIn("MetaApi", instructions)
        self.assertIn(".env", instructions)
        self.assertIn("METAAPI_TOKEN", instructions)


class TestSimulator(unittest.TestCase):
    def test_load_history(self):
        df = simulator.load_history("XAUUSD", "M15", bars=500)
        self.assertFalse(df.empty)
        self.assertEqual(len(df), 500)
        for col in ("time", "open", "high", "low", "close"):
            self.assertIn(col, df.columns)

    def test_compute_metrics_empty(self):
        res = simulator.compute_metrics([], 10000.0)
        self.assertEqual(res["trades"], 0)
        self.assertEqual(res["net_profit"], 0.0)
        self.assertEqual(res["final_balance"], 10000.0)

    def test_compute_metrics_known(self):
        trades = [
            {"side": "BUY", "profit": 200.0},
            {"side": "SELL", "profit": -100.0},
            {"side": "BUY", "profit": 150.0},
        ]
        res = simulator.compute_metrics(trades, 10000.0)
        self.assertEqual(res["trades"], 3)
        self.assertEqual(res["buy_trades"], 2)
        self.assertEqual(res["sell_trades"], 1)
        self.assertAlmostEqual(res["net_profit"], 250.0)
        self.assertAlmostEqual(res["win_rate"], 66.67, places=2)
        self.assertAlmostEqual(res["profit_factor"], 3.50, places=2)

    def test_run_backtest_execution(self):
        df = simulator.load_history("XAUUSD", "M15", bars=1000)
        params = settings.load_params()
        res = simulator.run_backtest(df, params)
        self.assertIn("trades", res)
        self.assertIn("net_profit", res)
        self.assertIn("win_rate", res)


class TestTradeBotLogic(unittest.TestCase):
    def test_calculate_lot_size(self):
        spec = {
            "minVolume": 0.01,
            "maxVolume": 100.0,
            "volumeStep": 0.01,
            "contractSize": 100.0
        }
        # Balance = $10,000, 1% risk = $100
        # Entry = 2500, SL = 2490 (sl_dist = 10)
        # 100 / (10 * 100) = 0.10 Lot
        lot = trade_bot.calculate_lot_size(
            balance=10000.0,
            risk_percent=1.0,
            entry_price=2500.0,
            sl_price=2490.0,
            spec=spec
        )
        self.assertAlmostEqual(lot, 0.10)

    def test_get_val_helper(self):
        d = {"name": "test", "val": 42}
        self.assertEqual(trade_bot.get_val(d, "name"), "test")
        self.assertEqual(trade_bot.get_val(d, "val"), 42)
        self.assertEqual(trade_bot.get_val(d, "missing", "default"), "default")

        class DummyObj:
            def __init__(self):
                self.foo = "bar"
        obj = DummyObj()
        self.assertEqual(trade_bot.get_val(obj, "foo"), "bar")
        self.assertIsNone(trade_bot.get_val(obj, "baz"))

    def test_is_market_open(self):
        # Invalid / zero price
        self.assertFalse(trade_bot.is_market_open("XAUUSD", None))
        self.assertFalse(trade_bot.is_market_open("XAUUSD", {"bid": 0.0, "ask": 0.0}))


class TestDiscordBotLogic(unittest.TestCase):
    @patch("simulator.export_trades_csv")
    def test_run_simulation_sync(self, mock_export_csv):
        res = discord_bot.run_simulation_sync(500)
        self.assertIn("metrics", res)
        self.assertIn("symbol", res)
        self.assertIn("timeframe", res)
        self.assertIn("bars_loaded", res)
        self.assertEqual(res["bars_loaded"], 500)
        mock_export_csv.assert_called_once()

        m = res["metrics"]
        required_keys = [
            "net_profit", "trades", "buy_trades", "sell_trades",
            "win_rate", "buy_win_rate", "sell_win_rate",
            "profit_factor", "max_drawdown_pct", "final_balance"
        ]
        for k in required_keys:
            self.assertIn(k, m, f"Metric key {k} missing in simulation result")

    def test_get_latest_sentiment_data(self):
        res = discord_bot.get_latest_sentiment_data()
        self.assertIn("score", res)
        self.assertIn("reasoning", res)
        self.assertIn("age_str", res)
        self.assertIn("provider", res)
        self.assertIsInstance(res["score"], float)

    def test_get_heartbeat_status(self):
        hb = discord_bot.get_heartbeat_status()
        for svc in ("trade_bot", "news_agent", "overseer", "discord_bot"):
            self.assertIn(svc, hb)


    def test_discord_get_val(self):
        d = {"hello": "world"}
        self.assertEqual(discord_bot.get_val(d, "hello"), "world")
        self.assertEqual(discord_bot.get_val(d, "unknown", 123), 123)

        class Sample:
            x = 99
        self.assertEqual(discord_bot.get_val(Sample(), "x"), 99)
        self.assertIsNone(discord_bot.get_val(Sample(), "y"))

    def test_get_metaapi_connection_unconfigured(self):
        with patch("discord_bot.is_metaapi_configured", return_value=False), \
             patch("discord_bot.is_docker_mt5_available", return_value=False):
            ok, client, conn = asyncio.run(discord_bot.get_metaapi_connection())
            self.assertFalse(ok)
            self.assertIsNone(client)
            self.assertIsNone(conn)


class TestTradeBotOrderAndDeals(unittest.TestCase):
    @patch("trade_bot.send_ai_error_alert")
    @patch("trade_bot.send_alert")
    @patch("trade_bot.log_event")
    @patch("trade_bot.log_trade")
    def test_place_order_buy_success(self, mock_log_trade, mock_log_event, mock_send_alert, mock_ai_alert):
        mock_conn = AsyncMock()
        mock_conn.create_market_buy_order.return_value = {"orderId": "1001", "numericCode": 10009}
        mock_conn.get_symbol_price.return_value = {"ask": 2500.50, "bid": 2500.20}
        mock_conn.get_account_information.return_value = {"balance": 10000.0}

        spec = {"digits": 2, "minVolume": 0.01, "maxVolume": 100.0, "volumeStep": 0.01, "contractSize": 100.0}

        ok = asyncio.run(trade_bot.place_order(
            connection=mock_conn,
            symbol="XAUUSD",
            signal="BUY",
            lot=0.10,
            sl=2485.0,
            tp=2520.0,
            magic=123456,
            version=5,
            passed_validation=True,
            acc_tag="[TEST] ",
            spec=spec
        ))
        self.assertTrue(ok)
        mock_conn.create_market_buy_order.assert_called_once()
        mock_log_trade.assert_called_once()
        mock_send_alert.assert_called_once()
        self.assertIn("[MT5 Live]", mock_send_alert.call_args[0][0])

    @patch("trade_bot.send_ai_error_alert")
    @patch("trade_bot.send_alert")
    @patch("trade_bot.log_event")
    @patch("trade_bot.log_trade")
    def test_place_order_sell_success(self, mock_log_trade, mock_log_event, mock_send_alert, mock_ai_alert):
        mock_conn = AsyncMock()
        mock_conn.create_market_sell_order.return_value = {"orderId": "1002", "numericCode": 10009}
        mock_conn.get_symbol_price.return_value = {"ask": 2500.50, "bid": 2500.20}
        mock_conn.get_account_information.return_value = {"balance": 10000.0}

        spec = {"digits": 2, "minVolume": 0.01, "maxVolume": 100.0, "volumeStep": 0.01, "contractSize": 100.0}

        ok = asyncio.run(trade_bot.place_order(
            connection=mock_conn,
            symbol="XAUUSD",
            signal="SELL",
            lot=0.10,
            sl=2515.0,
            tp=2480.0,
            magic=123456,
            version=5,
            passed_validation=True,
            acc_tag="[TEST] ",
            spec=spec
        ))
        self.assertTrue(ok)
        mock_conn.create_market_sell_order.assert_called_once()
        mock_log_trade.assert_called_once()
        mock_send_alert.assert_called_once()
        self.assertIn("[MT5 Live]", mock_send_alert.call_args[0][0])

    @patch("trade_bot.send_ai_error_alert")
    @patch("trade_bot.send_alert")
    @patch("trade_bot.log_event")
    @patch("trade_bot.log_trade")
    def test_place_order_failure_handles_gracefully(self, mock_log_trade, mock_log_event, mock_send_alert, mock_ai_alert):
        mock_conn = AsyncMock()
        mock_conn.create_market_buy_order.side_effect = RuntimeError("Broker connection timeout")
        spec = {"digits": 2}

        ok = asyncio.run(trade_bot.place_order(
            connection=mock_conn,
            symbol="XAUUSD",
            signal="BUY",
            lot=0.10,
            sl=2485.0,
            tp=2520.0,
            magic=123456,
            version=5,
            passed_validation=True,
            acc_tag="[TEST] ",
            spec=spec
        ))
        self.assertFalse(ok)
        mock_log_event.assert_called_once()
        mock_send_alert.assert_called_once()
        mock_ai_alert.assert_called_once()

    def test_get_daily_loss_calculation(self):
        mock_conn = AsyncMock()
        mock_conn.get_deals_by_time_range.return_value = {
            "deals": [
                {"magic": 123456, "entryType": "DEAL_ENTRY_OUT", "profit": -50.0, "commission": -2.0, "swap": 0.0},
                {"magic": 123456, "entryType": "DEAL_ENTRY_OUT", "profit": 30.0, "commission": -2.0, "swap": -1.0},
                {"magic": 999999, "entryType": "DEAL_ENTRY_OUT", "profit": -100.0, "commission": 0.0, "swap": 0.0},  # other magic
                {"magic": 123456, "entryType": "DEAL_ENTRY_IN", "profit": 0.0, "commission": 0.0, "swap": 0.0},   # entry in
            ]
        }
        loss = asyncio.run(trade_bot.get_daily_loss(mock_conn, 123456))
        # Deal 1: -50 + -2 = -52
        # Deal 2: 30 + -2 + -1 = 27
        # Total = -52 + 27 = -25
        self.assertAlmostEqual(loss, -25.0)

    def test_connect_metaapi_unconfigured(self):
        with patch("trade_bot.is_metaapi_configured", return_value=False), \
             patch("trade_bot.is_docker_mt5_available", return_value=False):
            api, acc, conn, tag = asyncio.run(trade_bot.connect_metaapi())
            self.assertIsNone(api)
            self.assertIsNone(acc)
            self.assertIsNone(conn)
            self.assertEqual(tag, "")

    @patch("trade_bot.send_alert")
    @patch("trade_bot.log_trade")
    def test_sync_closed_deals_uses_mt5_live_label(self, mock_log_trade, mock_send_alert):
        mock_conn = AsyncMock()
        mock_conn.get_deals_by_time_range.return_value = {
            "deals": [
                {
                    "id": "999001",
                    "orderId": "888001",
                    "magic": 123456,
                    "entryType": "DEAL_ENTRY_OUT",
                    "type": "DEAL_TYPE_SELL",
                    "price": 2510.0,
                    "volume": 0.10,
                    "profit": 45.0,
                    "commission": -1.0,
                    "swap": 0.0,
                }
            ]
        }
        mock_conn.get_account_information.return_value = {"balance": 10044.0}
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(trade_bot, "STATE_DIR", Path(tmpdir)):
                asyncio.run(trade_bot.sync_closed_deals(mock_conn, "XAUUSD", 123456, 5, "[TEST] "))
        mock_log_trade.assert_called_once()
        mock_send_alert.assert_called_once()
        self.assertIn("[MT5 Live]", mock_send_alert.call_args[0][0])


class TestOverseerAndOptimizer(unittest.TestCase):
    def test_optimizer_grid_structure(self):
        import optimizer
        self.assertIn("sma_period", optimizer.GRID)
        self.assertIn("rsi_buy_level", optimizer.GRID)
        self.assertIn("rsi_sell_level", optimizer.GRID)
        self.assertIn("atr_sl_mult", optimizer.GRID)
        self.assertIn("atr_tp_mult", optimizer.GRID)

    def test_overseer_file_hashes(self):
        import overseer
        hashes = overseer.get_file_hashes()
        self.assertIsInstance(hashes, dict)
        self.assertIn("strategy.py", hashes)
        self.assertIn("settings.py", hashes)
        # Should not include state or logs
        for k in hashes:
            self.assertFalse(k.startswith("logs/"))
            self.assertFalse(k.startswith("state/"))

    def test_overseer_state_persistence(self):
        import overseer
        st = overseer.load_overseer_state()
        self.assertIn("date", st)
        self.assertIn("ai_runs_today", st)
        self.assertIn("handled_fingerprints", st)

    @patch("overseer.subprocess.run", side_effect=FileNotFoundError("agy not found"))
    def test_overseer_call_antigravity_cli_fallback_to_gemini_3_8(self, mock_subproc):
        import overseer
        mock_genai_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "สรุปผลการวิเคราะห์จาก Gemini 3.8"
        mock_genai_client.models.generate_content.return_value = mock_resp

        with patch.dict(os.environ, {"GROQ_API_KEY": "your_groq_api_key_here", "GEMINI_API_KEY": "real_gemini_key_123"}):
            with patch("google.genai.Client", return_value=mock_genai_client):
                out = overseer.call_antigravity_cli("Test prompt")
                self.assertEqual(out, "สรุปผลการวิเคราะห์จาก Gemini 3.8")
                call_kwargs = mock_genai_client.models.generate_content.call_args[1]
                self.assertEqual(call_kwargs["model"], "gemini-3.8-flash")


class TestNotifierDiagnostics(unittest.TestCase):
    @patch("notifier.send_alert")
    @patch("notifier.secret", return_value="fake_groq_key")
    def test_send_ai_error_alert_uses_groq_first(self, mock_secret, mock_send_alert):
        import notifier
        mock_client = MagicMock()
        mock_msg = MagicMock()
        mock_msg.content = "เชื่อมต่อโบรกเกอร์หมดเวลา"
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        mock_client.chat.completions.create.return_value = MagicMock(choices=[mock_choice])

        with patch.object(notifier, "GROQ_AVAILABLE", True), \
             patch.object(notifier, "Groq", return_value=mock_client, create=True):
            notifier.send_ai_error_alert("TimeoutError", context="UnitTest")
            mock_client.chat.completions.create.assert_called_once()
            call_kwargs = mock_client.chat.completions.create.call_args[1]
            self.assertEqual(call_kwargs["model"], "llama-3.3-70b-versatile")
            mock_send_alert.assert_called_once()
            self.assertIn("เชื่อมต่อโบรกเกอร์หมดเวลา", mock_send_alert.call_args[0][0])

    @patch("notifier.send_alert")
    def test_send_ai_error_alert_falls_back_to_gemini_2_0_flash(self, mock_send_alert):
        import notifier
        mock_genai_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "การเชื่อมต่อขัดข้องชั่วคราว"
        mock_genai_client.models.generate_content.return_value = mock_resp

        def fake_secret(name, default=None):
            if name == "GROQ_API_KEY":
                return "your_groq_api_key_here"
            if name == "GEMINI_API_KEY":
                return "real_gemini_key_123"
            return default

        with patch.object(notifier, "secret", side_effect=fake_secret), \
             patch.object(notifier, "GENAI_AVAILABLE", True), \
             patch("notifier.genai.Client", return_value=mock_genai_client):
            notifier.send_ai_error_alert("ConnectionError", context="FallbackTest")
            mock_genai_client.models.generate_content.assert_called_once()
            self.assertEqual(mock_genai_client.models.generate_content.call_args[1]["model"], "gemini-2.0-flash")
            mock_send_alert.assert_called_once()


if __name__ == "__main__":
    unittest.main()

