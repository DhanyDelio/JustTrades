import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock
import requests

# Agar bisa mengimpor file dari architecture_test
sys.path.append(str(Path(__file__).resolve().parent))

from order_executor_base import SpotOrderExecutor

class TestSpotOrderExecutor(unittest.TestCase):
    def test_place_entry_order(self):
        print("\n=== Menjalankan Unit Test: SpotOrderExecutor.place_entry_order ===")
        
        # 1. Setup Mock Client (Pengganti API Binance)
        mock_client = MagicMock()
        
        # Simulasi balasan dari client.get_symbol_info("BTCUSDT")
        mock_client.get_symbol_info.return_value = {
            'filters': [
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},   # Harga dibulatkan ke 2 desimal
                {'filterType': 'LOT_SIZE', 'stepSize': '0.001'}       # Qty dibulatkan ke 3 desimal
            ]
        }
        
        # Simulasi balasan dari client.create_order
        expected_api_response = {"orderId": 99999, "status": "NEW"}
        mock_client.create_order.return_value = expected_api_response
        
        # 2. Inisialisasi Executor
        executor = SpotOrderExecutor(mock_client)
        
        # 3. Siapkan data kandidat tiruan (mentah / belum dibulatkan)
        candidate = {
            "symbol": "BTCUSDT",
            "entry_price": 60000.12888, # Seharusnya dibulatkan menjadi 60000.13 (ROUND_HALF_UP)
            "sizing": {
                "qty": 0.012567         # Seharusnya dibulatkan menjadi 0.012 (ROUND_DOWN)
            }
        }
        
        # 4. Eksekusi fungsi
        result = executor.place_entry_order(candidate)
        
        # 5. Verifikasi hasil dan behavior
        
        # A. Pastikan nilai return-nya adalah balasan dari create_order
        self.assertEqual(result, expected_api_response)
        print("✓ Return value cocok dengan respons API.")
        
        # B. Pastikan get_symbol_info dipanggil untuk mengambil cache
        mock_client.get_symbol_info.assert_called_once_with("BTCUSDT")
        print("✓ get_symbol_constraints() berhasil memanggil get_symbol_info secara on-demand.")
        
        # C. Pastikan create_order dipanggil dengan parameter yang sudah ter-format dengan benar
        # (SIDE_BUY, ORDER_TYPE_LIMIT, TIME_IN_FORCE_GTC di-hardcode di dalam fungsi)
        mock_client.create_order.assert_called_once_with(
            symbol="BTCUSDT",
            side="BUY",
            type="LIMIT",
            timeInForce="GTC",
            quantity="0.012",      # Hasil pembulatan step_size (string tanpa trailing zero)
            price="60000.13"       # Hasil pembulatan tick_size (string tanpa trailing zero)
        )
        print("✓ Format parameter (SIDE, TYPE, QTY_STR, PRICE_STR) 100% identik dengan place_limit_order() asli.")

    def test_get_symbol_constraints_network_error_propagation(self):
        print("\n=== Menjalankan Unit Test: Edge Case Network Error ===")
        from binance.exceptions import BinanceAPIException
        mock_client = MagicMock()
        
        # Simulasi network error saat menarik filter dari Binance
        error = BinanceAPIException(MagicMock(), 400, "Network Error")
        mock_client.get_symbol_info.side_effect = error
        
        executor = SpotOrderExecutor(mock_client)
        candidate = {"symbol": "BTCUSDT", "entry_price": 60000, "sizing": {"qty": 1}}
        
        # Exception API harus bocor keluar (tidak di-swallow diam-diam)
        with self.assertRaises(BinanceAPIException):
            executor.place_entry_order(candidate)
        print("✓ BinanceAPIException sukses diteruskan (propagate) jika get_symbol_info gagal.")

    def test_place_entry_order_missing_fields(self):
        print("\n=== Menjalankan Unit Test: Edge Case Missing Fields ===")
        mock_client = MagicMock()
        executor = SpotOrderExecutor(mock_client)
        
        # Candidate dict cacat (tidak punya entry_price)
        bad_candidate = {
            "symbol": "BTCUSDT",
            "sizing": {"qty": 1}
        }
        
        # Harus menghasilkan KeyError (Sesuai dengan cara kerja dictionary di Python)
        with self.assertRaises(KeyError) as context:
            executor.place_entry_order(bad_candidate)
        
        self.assertIn("entry_price", str(context.exception))
        print("✓ KeyError dilemparkan dengan benar saat field entry_price hilang.")

    def test_caching_and_ttl(self):
        print("\n=== Menjalankan Unit Test: Caching & TTL Validation ===")
        import time
        mock_client = MagicMock()
        mock_client.get_symbol_info.return_value = {
            'filters': [
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                {'filterType': 'LOT_SIZE', 'stepSize': '0.001'}
            ]
        }
        
        executor = SpotOrderExecutor(mock_client)
        candidate = {"symbol": "BTCUSDT", "entry_price": 60000.1, "sizing": {"qty": 0.01}}
        
        # 1. Panggilan Pertama
        executor.place_entry_order(candidate)
        self.assertEqual(mock_client.get_symbol_info.call_count, 1)
        
        # 2. Panggilan Kedua (Seketika)
        executor.place_entry_order(candidate)
        # Harus tetap 1 karena mengambil dari cache
        self.assertEqual(mock_client.get_symbol_info.call_count, 1)
        print("✓ Cache sukses! Pemanggilan kedua pada simbol yang sama TIDAK melakukan API call lagi.")
        
        # 3. Simulasi Waktu Berlalu (25 Jam Kemudian)
        # Kita manipulasi isi dictionary cache-nya agar terlihat sudah expired
        executor._constraints_cache["BTCUSDT"]["timestamp"] = time.time() - (25 * 3600)
        
        # 4. Panggilan Ketiga
        executor.place_entry_order(candidate)
        # Sekarang API harus dipanggil lagi (karena data kadaluarsa)
        self.assertEqual(mock_client.get_symbol_info.call_count, 2)
        print("✓ TTL Expired sukses! Data cache ditarik ulang secara paksa setelah lebih dari 24 jam.")


# =============================================================================
# TEST: place_exit_orders() — OCO SELL
# =============================================================================

class TestSpotExitOrders(unittest.TestCase):
    """Test suite for SpotOrderExecutor.place_exit_orders()"""

    def _make_executor(self):
        """Helper: buat executor dengan mock client yang sudah ter-setup constraint cache."""
        mock_client = MagicMock()
        mock_client.get_symbol_info.return_value = {
            'filters': [
                {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                {'filterType': 'LOT_SIZE', 'stepSize': '0.001'}
            ]
        }
        return SpotOrderExecutor(mock_client), mock_client

    def _make_trade(self, sl=59000.0, tp1=63000.0):
        """Helper: buat trade dict standar."""
        return {
            "symbol": "BTCUSDT",
            "entry_qty": 0.012567,
            "sl": sl,
            "tp1": tp1,
        }

    # ── 1. Happy Path: OCO ditempatkan secara normal ───────────────────
    def test_happy_path_oco_placed(self):
        print("\n=== Test: OCO Happy Path ===")
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=59000.0, tp1=63000.0)

        # Harga saat ini berada di antara SL dan TP (kondisi ideal)
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        exchange_resp = {"orderListId": 12345, "listStatusType": "EXEC_STARTED"}
        mock_client.create_oco_order.return_value = exchange_resp

        result = executor.place_exit_orders(trade)

        # Result is now a structured dict — check protection_state and nested oco_resp
        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(result["oco_resp"]["orderListId"], 12345)
        mock_client.create_oco_order.assert_called_once()

        # Verifikasi parameter OCO
        call_kwargs = mock_client.create_oco_order.call_args.kwargs
        self.assertEqual(call_kwargs["symbol"], "BTCUSDT")
        self.assertEqual(call_kwargs["side"], "SELL")
        self.assertEqual(call_kwargs["aboveType"], "LIMIT_MAKER")
        self.assertEqual(call_kwargs["belowType"], "STOP_LOSS_LIMIT")
        self.assertEqual(call_kwargs["belowTimeInForce"], "GTC")

        # Verifikasi SL limit = sl * 0.9985
        sl_limit_expected = executor.round_tick(59000.0 * 0.9985, 0.01)
        self.assertEqual(call_kwargs["belowPrice"], f"{sl_limit_expected:.8f}".rstrip("0").rstrip("."))
        print("✓ OCO ditempatkan dengan benar: above=LIMIT_MAKER(TP), below=STOP_LOSS_LIMIT(SL)")
        print(f"✓ SL limit price = {sl_limit_expected} (sl * 0.9985, dibulatkan ke tick)")

    # ── 2. Race Condition: Harga sudah <= SL → Emergency Market Sell ───
    def test_price_below_sl_emergency_market_sell(self):
        print("\n=== Test: Emergency Market Sell (price <= SL) ===")
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=59000.0, tp1=63000.0)

        # Harga saat ini SUDAH JATUH di bawah SL!
        mock_client.get_symbol_ticker.return_value = {"price": "58500.00"}
        mock_client.create_order.return_value = {"orderId": 77777, "status": "FILLED"}

        result = executor.place_exit_orders(trade)

        # Harus memanggil create_order (MARKET SELL), BUKAN create_oco_order
        mock_client.create_order.assert_called_once()
        mock_client.create_oco_order.assert_not_called()

        # Verifikasi parameter market sell
        call_kwargs = mock_client.create_order.call_args.kwargs
        self.assertEqual(call_kwargs["side"], "SELL")
        self.assertEqual(call_kwargs["type"], "MARKET")

        # Trade dict harus ditandai _market_sold
        self.assertTrue(trade.get("_market_sold"))
        print("✓ Emergency MARKET SELL dieksekusi karena harga sudah di bawah SL.")
        print("✓ create_oco_order TIDAK dipanggil (sudah terlambat untuk OCO).")
        print("✓ trade['_market_sold'] = True (flag untuk update log).")

    # ── 3. Retry: Error di percobaan 1, sukses di percobaan 2 ─────────
    @unittest.mock.patch("time.sleep", return_value=None)  # Skip sleep
    def test_retry_on_price_constraint_error(self, mock_sleep):
        print("\n=== Test: Retry Logic (price constraint error) ===")
        from binance.exceptions import BinanceAPIException

        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=59000.0, tp1=63000.0)

        # Harga normal (di antara SL dan TP)
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}

        # Buat mock Response yang benar agar BinanceAPIException menghasilkan str() yang sesuai
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 400
        mock_resp.text = '{"code":-1013,"msg":"Filter failure: PRICE_FILTER"}'
        mock_resp.json.return_value = {'code': -1013, 'msg': 'Filter failure: PRICE_FILTER'}

        # Percobaan 1: error -1013 (price constraint)
        # Percobaan 2: sukses
        price_err = BinanceAPIException(mock_resp, 400, mock_resp.text)
        expected_resp = {"orderListId": 99999}
        mock_client.create_oco_order.side_effect = [price_err, expected_resp]

        result = executor.place_exit_orders(trade)

        # Result is a structured dict — check protection_state + nested oco_resp
        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(result["oco_resp"]["orderListId"], 99999)
        self.assertEqual(mock_client.create_oco_order.call_count, 2)
        print("✓ Percobaan 1 gagal (error -1013), retry otomatis.")
        print("✓ Percobaan 2 berhasil — OCO ditempatkan.")

    # ── 4. Semua Retry Gagal (3x) → RuntimeError ─────────────────────
    @unittest.mock.patch("time.sleep", return_value=None)  # Skip sleep
    def test_all_retries_exhausted_raises_error(self, mock_sleep):
        print("\n=== Test: All Retries Exhausted ===")
        from binance.exceptions import BinanceAPIException

        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=59000.0, tp1=63000.0)

        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}

        # Buat mock Response yang benar
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 400
        mock_resp.text = '{"code":-1013,"msg":"Filter failure: PRICE_FILTER"}'
        mock_resp.json.return_value = {'code': -1013, 'msg': 'Filter failure: PRICE_FILTER'}

        # Semua 3 percobaan gagal dengan price constraint error
        price_err = BinanceAPIException(mock_resp, 400, mock_resp.text)
        mock_client.create_oco_order.side_effect = [price_err, price_err, price_err]

        with self.assertRaises(RuntimeError) as context:
            executor.place_exit_orders(trade)

        self.assertIn("attempt", str(context.exception).lower())
        self.assertEqual(mock_client.create_oco_order.call_count, 3)
        print("✓ Setelah 3x retry gagal, RuntimeError dilempar (TIDAK silent fail).")
        print(f"✓ Error message: {context.exception}")



# =============================================================================
# TEST: OCO API -1102 / -1013 REGRESSION (new format + error handling)
# =============================================================================

class TestOcoApiRegressions(unittest.TestCase):
    """
    Regression tests for the Binance Spot OCO API format fix.

    Background:
      - Binance Spot OCO API now requires aboveType/belowType (new format).
      - Old format used price= / stopPrice= / stopLimitPrice= parameters.
      - Error -1102 fires if mandatory aboveType/belowType are missing.
      - Error -1013 PERCENT_PRICE_BY_SIDE fires when SL is structurally too far
        from current price (outside the exchange price-band filter, e.g. < 80%
        of avgPrice). Retrying this is futile — must fail-fast.
      - Error -1013 PRICE_FILTER (different sub-type) is transient — retry allowed.

    Tests:
      1.  aboveType=LIMIT_MAKER and belowType=STOP_LOSS_LIMIT are always sent
      2.  abovePrice maps to TP, belowStopPrice maps to SL trigger
      3.  -1102 raises RuntimeError immediately (no retry)
      4.  -1013 PERCENT_PRICE_BY_SIDE raises RuntimeError immediately (no retry)
      5.  -1013 PRICE_FILTER retries (transient constraint — different sub-type)
      6.  Normal SELL OCO succeeds and returns exchange response
      7.  Partial fill quantity: qty from trade dict used, not hardcoded
      8.  Duplicate/retry: if OCO already placed, no duplicate is created
      9.  Exchange rejection on non-price error raises RuntimeError (no retry)
    """

    def _make_executor(self):
        mock_client = MagicMock()
        mock_client.get_symbol_info.return_value = {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE",     "stepSize": "0.001"},
            ]
        }
        return SpotOrderExecutor(mock_client), mock_client

    def _make_trade(self, sl=59000.0, tp1=63000.0, qty=0.012567):
        return {
            "symbol":    "BTCUSDT",
            "entry_qty": qty,
            "sl":        sl,
            "tp1":       tp1,
        }

    def _make_api_error(self, code: int, msg: str) -> "BinanceAPIException":
        from binance.exceptions import BinanceAPIException
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 400
        body = f'{{"code":{code},"msg":"{msg}"}}'
        resp.text = body
        resp.json.return_value = {"code": code, "msg": msg}
        return BinanceAPIException(resp, 400, body)

    # ── Test 1: New format — aboveType and belowType always sent ──────────────
    def test_new_format_above_below_type_always_sent(self):
        """aboveType=LIMIT_MAKER and belowType=STOP_LOSS_LIMIT must always be in payload."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=59000.0, tp1=63000.0)
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        mock_client.create_oco_order.return_value = {"orderListId": 1}

        executor.place_exit_orders(trade)

        call_kwargs = mock_client.create_oco_order.call_args.kwargs
        self.assertEqual(call_kwargs["aboveType"], "LIMIT_MAKER",
                         "aboveType must be LIMIT_MAKER (TP leg)")
        self.assertEqual(call_kwargs["belowType"], "STOP_LOSS_LIMIT",
                         "belowType must be STOP_LOSS_LIMIT (SL leg)")
        # Old format params must NOT be present
        self.assertNotIn("price",          call_kwargs, "old 'price' param must not be sent")
        self.assertNotIn("stopPrice",      call_kwargs, "old 'stopPrice' param must not be sent")
        self.assertNotIn("stopLimitPrice", call_kwargs, "old 'stopLimitPrice' param must not be sent")
        print("✓ Test 1: aboveType=LIMIT_MAKER, belowType=STOP_LOSS_LIMIT always sent; old params absent")

    # ── Test 2: Price mapping — TP → abovePrice, SL trigger → belowStopPrice ──
    def test_tp_maps_to_above_price_sl_maps_to_below_stop_price(self):
        """TP price must go to abovePrice; SL trigger must go to belowStopPrice."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=59000.0, tp1=63000.0)
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        mock_client.create_oco_order.return_value = {"orderListId": 2}

        executor.place_exit_orders(trade)

        kw = mock_client.create_oco_order.call_args.kwargs
        # abovePrice = TP1 (rounded)
        self.assertAlmostEqual(float(kw["abovePrice"]), 63000.0, places=1,
                               msg="abovePrice must match TP1")
        # belowStopPrice = SL trigger
        self.assertAlmostEqual(float(kw["belowStopPrice"]), 59000.0, places=1,
                               msg="belowStopPrice must match SL")
        # belowPrice = SL limit (below trigger)
        self.assertLess(float(kw["belowPrice"]), float(kw["belowStopPrice"]),
                        "belowPrice (limit) must be below belowStopPrice (trigger)")
        print(f"✓ Test 2: abovePrice={kw['abovePrice']} (TP), "
              f"belowStopPrice={kw['belowStopPrice']} (SL trigger), "
              f"belowPrice={kw['belowPrice']} (SL limit)")

    # ── Test 3: -1102 raises immediately, no retry ────────────────────────────
    @unittest.mock.patch("time.sleep", return_value=None)
    def test_1102_raises_immediately_no_retry(self, _sleep):
        """-1102 (missing mandatory param) must raise RuntimeError on first attempt, no retry."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade()
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        err_1102 = self._make_api_error(-1102,
                       "Mandatory parameter 'aboveType' was not sent")
        mock_client.create_oco_order.side_effect = err_1102

        with self.assertRaises(RuntimeError) as ctx:
            executor.place_exit_orders(trade)

        # Must have raised after exactly 1 call — no retry on -1102
        self.assertEqual(mock_client.create_oco_order.call_count, 1,
                         "-1102 must not be retried")
        self.assertIn("-1102", str(ctx.exception))
        _sleep.assert_not_called()
        print(f"✓ Test 3: -1102 raises immediately after 1 attempt, no sleep/retry")

    # ── Test 4: -1013 PERCENT_PRICE_BY_SIDE raises immediately, no retry ──────
    @unittest.mock.patch("time.sleep", return_value=None)
    def test_1013_percent_price_by_side_raises_immediately(self, _sleep):
        """-1013 PERCENT_PRICE_BY_SIDE must raise immediately, not retry 3x."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=7580.0, tp1=12000.0)
        mock_client.get_symbol_ticker.return_value = {"price": "11000.00"}
        err_percent = self._make_api_error(
            -1013, "Filter failure: PERCENT_PRICE_BY_SIDE"
        )
        mock_client.create_oco_order.side_effect = err_percent

        with self.assertRaises(RuntimeError) as ctx:
            executor.place_exit_orders(trade)

        # Must NOT have retried — PERCENT_PRICE_BY_SIDE is structural, not transient
        self.assertEqual(mock_client.create_oco_order.call_count, 1,
                         "PERCENT_PRICE_BY_SIDE must not be retried")
        _sleep.assert_not_called()
        exc_msg = str(ctx.exception)
        self.assertIn("PERCENT_PRICE_BY_SIDE", exc_msg)
        print(f"✓ Test 4: -1013 PERCENT_PRICE_BY_SIDE → immediate RuntimeError, no retry")

    # ── Test 5: -1013 PRICE_FILTER (transient) → retry allowed ───────────────
    @unittest.mock.patch("time.sleep", return_value=None)
    def test_1013_price_filter_transient_retries(self, _sleep):
        """-1013 PRICE_FILTER (not PERCENT_PRICE_BY_SIDE) is transient — retry allowed."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=59000.0, tp1=63000.0)
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        err_price = self._make_api_error(-1013, "Filter failure: PRICE_FILTER")
        success   = {"orderListId": 5}
        # First attempt fails with transient price filter, second succeeds
        mock_client.create_oco_order.side_effect = [err_price, success]

        result = executor.place_exit_orders(trade)

        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(result["oco_resp"]["orderListId"], 5)
        self.assertEqual(mock_client.create_oco_order.call_count, 2,
                         "Transient -1013 PRICE_FILTER should be retried")
        print("✓ Test 5: -1013 PRICE_FILTER retried; succeeded on attempt 2")

    # ── Test 6: Normal SELL OCO success ───────────────────────────────────────
    def test_normal_sell_oco_success(self):
        """Happy path: normal OCO SELL with price between SL and TP."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=59000.0, tp1=63000.0)
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        expected = {"orderListId": 99, "listStatusType": "EXEC_STARTED",
                    "orderReports": [{"orderId": 1001}, {"orderId": 1002}]}
        mock_client.create_oco_order.return_value = expected

        result = executor.place_exit_orders(trade)

        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(result["oco_resp"]["orderListId"], 99)
        self.assertEqual(result["oco_resp"]["listStatusType"], "EXEC_STARTED")
        kw = mock_client.create_oco_order.call_args.kwargs
        self.assertEqual(kw["symbol"], "BTCUSDT")
        self.assertEqual(kw["side"],   "SELL")
        self.assertIn("belowTimeInForce", kw)
        self.assertEqual(kw["belowTimeInForce"], "GTC")
        print("✓ Test 6: Normal SELL OCO success; response parsed correctly")

    # ── Test 7: Partial fill quantity — qty from trade dict ───────────────────
    def test_partial_fill_quantity_used_from_trade(self):
        """Quantity passed to exchange must come from trade['entry_qty']."""
        executor, mock_client = self._make_executor()
        # Non-standard partial quantity
        trade = self._make_trade(sl=59000.0, tp1=63000.0, qty=0.007)
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        mock_client.create_oco_order.return_value = {"orderListId": 7}

        executor.place_exit_orders(trade)

        kw = mock_client.create_oco_order.call_args.kwargs
        # qty_str is rounded to stepSize (0.001) so 0.007 stays 0.007
        self.assertAlmostEqual(float(kw["quantity"]), 0.007, places=3,
                               msg="Quantity must match trade['entry_qty']")
        print(f"✓ Test 7: Partial fill qty {kw['quantity']} correctly taken from trade dict")

    # ── Test 8: Duplicate/retry — no double OCO if first call succeeds ────────
    def test_no_duplicate_oco_on_retry(self):
        """
        If place_exit_orders is called twice (e.g. after monitor restart),
        it should not create a second OCO if the caller checks oco_placed first.
        The executor itself does not guard against double-call — that guard lives
        in the monitor. Verify that calling it twice makes two API calls (no
        internal dedup at executor level), so the monitor's guard is relied upon.
        """
        executor, mock_client = self._make_executor()
        trade = self._make_trade()
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        mock_client.create_oco_order.return_value = {"orderListId": 8}

        executor.place_exit_orders(trade)
        executor.place_exit_orders(trade)

        # Two calls → two OCO requests (dedup is monitor's responsibility)
        self.assertEqual(mock_client.create_oco_order.call_count, 2)
        print("✓ Test 8: Executor makes API call each time; dedup is monitor's responsibility")

    # ── Test 9: Non-price exchange rejection → immediate RuntimeError ─────────
    def test_non_price_rejection_raises_immediately(self):
        """Non-price API errors (e.g. -2010 insufficient balance) raise immediately."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade()
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        err_balance = self._make_api_error(-2010, "Account has insufficient balance")
        mock_client.create_oco_order.side_effect = err_balance

        with self.assertRaises(RuntimeError) as ctx:
            executor.place_exit_orders(trade)

        # Must raise on first attempt — no retry for non-price errors
        self.assertEqual(mock_client.create_oco_order.call_count, 1)
        self.assertIn("OCO placement failed", str(ctx.exception))
        print("✓ Test 9: -2010 insufficient balance → immediate RuntimeError, no retry")


# =============================================================================
# TEST: check_positions() — State Machine
# =============================================================================

class TestCheckPositions(unittest.TestCase):
    """
    Unit tests for SpotOrderExecutor.check_positions().

    Skenario yang diuji:
      1. Entry masih NEW  → tidak ada perubahan state, tidak place OCO
      2. Entry baru FILLED → catat fill price, trigger place_exit_orders()
      3. TP_HIT terdeteksi via OCO ALL_DONE → resolve TP_HIT + hitung PnL
      4. SL_HIT terdeteksi via OCO ALL_DONE → resolve SL_HIT + hitung PnL
      5. OCO belum placed saat check (oco_placed=False, entry FILLED) → retry place_exit_orders()
    """

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _make_executor(self):
        """Buat SpotOrderExecutor dengan mock client kosong."""
        mock_client = MagicMock()
        # Default: get_all_tickers mengembalikan list kosong (tidak ada ticker)
        mock_client.get_all_tickers.return_value = []
        return SpotOrderExecutor(mock_client), mock_client

    def _base_trade(self, **overrides) -> dict:
        """Trade dict standar yang sudah OPEN tapi belum filled."""
        t = {
            "entry_order_id":   111111,
            "symbol":           "BTCUSDT",
            "direction":        "long",
            "entry_price":      60000.0,
            "entry_fill_price": None,
            "entry_fill_time":  None,
            "entry_qty":        None,
            "entry_status":     "NEW",
            "entry_notional":   720.0,
            "sl":               58000.0,
            "tp1":              64000.0,
            "oco_placed":       False,
            "oco_list_id":      None,
            "exit_status":      "OPEN",
            "exit_price":       None,
            "realized_pnl_usd": None,
        }
        t.update(overrides)
        return t

    # ── Skenario 1: Entry masih NEW ───────────────────────────────────────────

    @unittest.mock.patch(
        "services.supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "services.supabase_client.update_spot_by_order_id"
    )
    def test_entry_still_new_no_state_change(self, mock_update, mock_fetch):
        """Entry masih NEW → tidak ada OCO, tidak ada update ke Supabase."""
        print("\n=== Test check_positions: Entry still NEW ===")

        executor, mock_client = self._make_executor()
        trade = self._base_trade()   # entry_status = NEW
        mock_fetch.return_value = [trade]

        # Entry order dari exchange masih NEW
        mock_client.get_order.return_value = {
            "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0",
            "price": "60000.00", "updateTime": 1700000000000,
        }
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}

        executor.check_positions()

        # Tidak boleh place OCO karena belum filled
        mock_client.create_oco_order.assert_not_called()
        # Tidak ada perubahan status, update hanya kalau entry_status berubah
        # (di sini tidak berubah, tetap NEW) → update_spot tidak dipanggil
        mock_update.assert_not_called()
        print("✓ Entry masih NEW: OCO tidak dipasang, Supabase tidak di-update.")

    # ── Skenario 2: Entry baru FILLED → trigger place_exit_orders() ──────────

    @unittest.mock.patch(
        "services.supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "services.supabase_client.update_spot_by_order_id"
    )
    def test_entry_newly_filled_places_oco(self, mock_update, mock_fetch):
        """Entry baru FILLED → fill price dicatat + OCO langsung dipasang."""
        print("\n=== Test check_positions: Entry newly FILLED → place OCO ===")

        executor, mock_client = self._make_executor()
        trade = self._base_trade()   # entry_fill_price=None → baru filled
        mock_fetch.return_value = [trade]

        # Exchange: order sekarang FILLED dengan fill data
        mock_client.get_order.return_value = {
            "status":                  "FILLED",
            "executedQty":             "0.012",
            "cummulativeQuoteQty":     "720.00",   # 0.012 × 60000
            "price":                   "60000.00",
            "updateTime":              1700001000000,
        }
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        mock_client.get_symbol_info.return_value = {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE",     "stepSize": "0.001"},
            ]
        }
        # OCO placement sukses
        mock_client.create_oco_order.return_value = {
            "orderListId":  99001,
            "orderReports": [
                {"orderId": 200}, {"orderId": 201},
            ],
        }

        executor.check_positions()

        # OCO harus dipasang
        mock_client.create_oco_order.assert_called_once()

        # Supabase harus di-update (minimal dua kali: fill + oco)
        self.assertTrue(mock_update.called)

        # Verifikasi field yang di-update mengandung fill price
        all_calls = mock_update.call_args_list
        updated_fields = {}
        for call in all_calls:
            updated_fields.update(call.args[1])

        self.assertAlmostEqual(updated_fields.get("entry_fill_price"), 60000.0, places=2)
        self.assertEqual(updated_fields.get("entry_qty"), 0.012)
        self.assertEqual(updated_fields.get("oco_placed"), True)
        self.assertEqual(updated_fields.get("oco_list_id"), 99001)
        print(f"✓ Fill price dicatat: {updated_fields['entry_fill_price']}")
        print(f"✓ OCO dipasang: list_id={updated_fields['oco_list_id']}")
        print(f"✓ update_spot_by_order_id dipanggil {len(all_calls)} kali.")

    # ── Skenario 3: TP_HIT terdeteksi ────────────────────────────────────────

    @unittest.mock.patch(
        "services.supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "services.supabase_client.update_spot_by_order_id"
    )
    def test_tp_hit_detected_and_resolved(self, mock_update, mock_fetch):
        """OCO ALL_DONE, LIMIT_MAKER (TP) leg FILLED → exit_status=TP_HIT."""
        print("\n=== Test check_positions: TP_HIT detected ===")

        executor, mock_client = self._make_executor()
        trade = self._base_trade(
            entry_status     = "FILLED",
            entry_fill_price = 60000.0,
            entry_fill_time  = 1700000000000,
            entry_qty        = 0.012,
            oco_placed       = True,
            oco_list_id      = 88001,
        )
        mock_fetch.return_value = [trade]

        # Entry order masih FILLED (sudah fill sebelumnya)
        mock_client.get_order.side_effect = [
            # First call: entry order
            {
                "status": "FILLED", "executedQty": "0.012",
                "cummulativeQuoteQty": "720.00", "price": "60000.00",
                "updateTime": 1700000000000,
            },
            # Second call: TP leg (LIMIT_MAKER filled at 64000)
            {
                "status": "FILLED", "type": "LIMIT_MAKER",
                "executedQty": "0.012", "cummulativeQuoteQty": "768.00",
                "price": "64000.00", "updateTime": 1700005000000,
            },
        ]

        # OCO list: ALL_DONE
        mock_client.v3_get_order_list.return_value = {
            "listOrderStatus": "ALL_DONE",
            "orders": [{"orderId": 300}, {"orderId": 301}],
        }
        mock_client.get_symbol_ticker.return_value = {"price": "64100.00"}

        executor.check_positions()

        # Verifikasi field yang di-update
        self.assertTrue(mock_update.called)
        all_fields = {}
        for call in mock_update.call_args_list:
            all_fields.update(call.args[1])

        self.assertEqual(all_fields.get("exit_status"), "TP_HIT")
        self.assertAlmostEqual(all_fields.get("exit_price"), 64000.0, places=2)
        # PnL = 0.012 × (64000 − 60000) = +$48
        self.assertAlmostEqual(all_fields.get("realized_pnl_usd"), 48.0, places=2)
        self.assertIsNotNone(all_fields.get("time_to_resolution_sec"))
        print(f"✓ exit_status=TP_HIT")
        print(f"✓ exit_price={all_fields['exit_price']}")
        print(f"✓ realized_pnl_usd={all_fields['realized_pnl_usd']} (+$48.00 expected)")
        print(f"✓ time_to_resolution_sec={all_fields['time_to_resolution_sec']}")

    # ── Skenario 4: SL_HIT terdeteksi ────────────────────────────────────────

    @unittest.mock.patch(
        "services.supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "services.supabase_client.update_spot_by_order_id"
    )
    def test_sl_hit_detected_and_resolved(self, mock_update, mock_fetch):
        """OCO ALL_DONE, STOP_LOSS_LIMIT leg FILLED → exit_status=SL_HIT."""
        print("\n=== Test check_positions: SL_HIT detected ===")

        executor, mock_client = self._make_executor()
        trade = self._base_trade(
            entry_status     = "FILLED",
            entry_fill_price = 60000.0,
            entry_fill_time  = 1700000000000,
            entry_qty        = 0.012,
            oco_placed       = True,
            oco_list_id      = 88002,
        )
        mock_fetch.return_value = [trade]

        mock_client.get_order.side_effect = [
            # Entry order
            {
                "status": "FILLED", "executedQty": "0.012",
                "cummulativeQuoteQty": "720.00", "price": "60000.00",
                "updateTime": 1700000000000,
            },
            # SL leg (STOP_LOSS_LIMIT filled at 58000)
            {
                "status": "FILLED", "type": "STOP_LOSS_LIMIT",
                "executedQty": "0.012", "cummulativeQuoteQty": "696.00",
                "price": "58000.00", "updateTime": 1700003000000,
            },
        ]

        mock_client.v3_get_order_list.return_value = {
            "listOrderStatus": "ALL_DONE",
            "orders": [{"orderId": 400}, {"orderId": 401}],
        }
        mock_client.get_symbol_ticker.return_value = {"price": "57900.00"}

        executor.check_positions()

        self.assertTrue(mock_update.called)
        all_fields = {}
        for call in mock_update.call_args_list:
            all_fields.update(call.args[1])

        self.assertEqual(all_fields.get("exit_status"), "SL_HIT")
        self.assertAlmostEqual(all_fields.get("exit_price"), 58000.0, places=2)
        # PnL = 0.012 × (58000 − 60000) = −$24
        self.assertAlmostEqual(all_fields.get("realized_pnl_usd"), -24.0, places=2)
        print(f"✓ exit_status=SL_HIT")
        print(f"✓ exit_price={all_fields['exit_price']}")
        print(f"✓ realized_pnl_usd={all_fields['realized_pnl_usd']} (-$24.00 expected)")

    # ── Skenario 5: OCO belum placed (oco_placed=False), entry FILLED → retry ─

    @unittest.mock.patch(
        "services.supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "services.supabase_client.update_spot_by_order_id"
    )
    def test_oco_not_placed_triggers_placement(self, mock_update, mock_fetch):
        """
        Trade FILLED tapi oco_placed=False (OCO belum pernah dipasang atau
        gagal di run sebelumnya) → check_positions harus coba pasang OCO.
        """
        print("\n=== Test check_positions: OCO not placed, retry placement ===")

        executor, mock_client = self._make_executor()
        trade = self._base_trade(
            entry_status     = "FILLED",
            entry_fill_price = 60000.0,   # sudah fill sebelumnya
            entry_fill_time  = 1700000000000,
            entry_qty        = 0.012,
            oco_placed       = False,     # OCO belum ada!
            oco_list_id      = None,
        )
        mock_fetch.return_value = [trade]

        mock_client.get_order.return_value = {
            "status": "FILLED", "executedQty": "0.012",
            "cummulativeQuoteQty": "720.00", "price": "60000.00",
            "updateTime": 1700000000000,
        }
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        mock_client.get_symbol_info.return_value = {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE",     "stepSize": "0.001"},
            ]
        }
        mock_client.create_oco_order.return_value = {
            "orderListId":  99002,
            "orderReports": [{"orderId": 500}, {"orderId": 501}],
        }

        executor.check_positions()

        # OCO harus dicoba pasang
        mock_client.create_oco_order.assert_called_once()
        self.assertTrue(mock_update.called)

        all_fields = {}
        for call in mock_update.call_args_list:
            all_fields.update(call.args[1])

        self.assertEqual(all_fields.get("oco_placed"), True)
        self.assertEqual(all_fields.get("oco_list_id"), 99002)
        print(f"✓ OCO berhasil dipasang ulang: list_id={all_fields['oco_list_id']}")
        print(f"✓ oco_placed=True tercatat di Supabase.")


# =============================================================================
# TEST: log_trade() — Insert New Trade to Supabase
# =============================================================================

class TestLogTrade(unittest.TestCase):
    """
    Unit tests for SpotOrderExecutor.log_trade().

    Skenario yang diuji:
      1. Happy path — semua field wajib ada → upsert_spot dipanggil dengan
         record yang field-nya identik dengan log_trade() di paper_trade_executor.py
      2. Field opsional (ml_score, symbol_rank, correlation_cluster_id) None
         → tetap masuk record sebagai None (tidak di-skip)
      3. winning_zone None → zone_type default "T1", zone_label None
      4. Field yang diambil dari sizing dict benar (notional, qty, max_loss_usd)
      5. fee_usd_roundtrip dihitung benar: notional × 0.001 × 2
    """

    def _make_executor(self):
        mock_client = MagicMock()
        return SpotOrderExecutor(mock_client)

    def _base_order(self) -> dict:
        return {
            "orderId":       12345,
            "clientOrderId": "x-abc123",
            "status":        "NEW",
        }

    def _base_cand(self, **overrides) -> dict:
        cand = {
            "symbol":       "ETHUSDT",
            "direction":    "long",
            "entry_price":  3000.0,
            "sl":           2850.0,
            "tp1":          3300.0,
            "tp2":          3500.0,
            "rr":           2.0,
            "risk_pct":     1.5,
            "atr_pct":      1.8,
            "sizing": {
                "notional_usd": 36.0,
                "qty":          0.012,
                "max_loss_usd": 0.54,
            },
            "entry_zone":   {"center": 2990.0, "touches": 3},
            "winning_zone": {"tier": "T1", "label": "Zone 3×"},
            "ml_score":     None,
            "ml_model_version": None,
            "symbol_rank":  None,
        }
        cand.update(overrides)
        return cand

    # ── 1. Happy path ─────────────────────────────────────────────────────────

    @unittest.mock.patch("services.supabase_client.upsert_spot")
    def test_happy_path_all_fields(self, mock_upsert):
        print("\n=== Test log_trade: Happy path ===")
        executor = self._make_executor()
        order    = self._base_order()
        cand     = self._base_cand(ml_score=0.62, ml_model_version="v1", symbol_rank=7)

        executor.log_trade(order, cand, correlation_cluster_id="20260720_120000")

        mock_upsert.assert_called_once()
        record = mock_upsert.call_args.args[0]

        # Identity
        self.assertEqual(record["symbol"],        "ETHUSDT")
        self.assertEqual(record["direction"],     "long")
        self.assertEqual(record["rule_version"],  "v1.0.0")
        self.assertEqual(record["correlation_cluster_id"], "20260720_120000")

        # Entry order
        self.assertEqual(record["entry_order_id"],   12345)
        self.assertEqual(record["entry_client_id"],  "x-abc123")
        self.assertEqual(record["entry_status"],     "NEW")
        self.assertEqual(record["entry_price"],      3000.0)
        self.assertIsNone(record["entry_fill_price"])
        self.assertEqual(record["entry_qty"],        0.012)
        self.assertEqual(record["entry_notional"],   36.0)

        # OCO initial state
        self.assertFalse(record["oco_placed"])
        self.assertIsNone(record["oco_list_id"])

        # Levels
        self.assertEqual(record["sl"],   2850.0)
        self.assertEqual(record["tp1"],  3300.0)
        self.assertEqual(record["tp2"],  3500.0)
        self.assertEqual(record["entry_zone_center"],  2990.0)
        self.assertEqual(record["entry_zone_touches"], 3)

        # Setup metadata
        self.assertEqual(record["planned_rr"],       2.0)
        self.assertEqual(record["risk_pct"],         1.5)
        self.assertEqual(record["max_loss_usd"],     0.54)
        self.assertEqual(record["zone_type"],        "T1")
        self.assertEqual(record["zone_label"],       "Zone 3×")
        self.assertEqual(record["zone_touches"],     3)
        self.assertEqual(record["atr_pct_at_entry"], 1.8)

        # Fee: 36.0 × 0.001 × 2 = 0.072
        self.assertAlmostEqual(record["fee_usd_roundtrip"], 0.072, places=4)

        # Exit initial state
        self.assertEqual(record["exit_status"],     "OPEN")
        self.assertIsNone(record["exit_price"])
        self.assertIsNone(record["realized_pnl_usd"])

        # ML + scan metadata
        self.assertAlmostEqual(record["ml_score"], 0.62, places=3)
        self.assertEqual(record["ml_model_version"], "v1")
        self.assertEqual(record["symbol_rank"], 7)

        # Raw order preserved
        self.assertEqual(record["raw_entry_order"], order)

        print(f"✓ upsert_spot dipanggil sekali dengan record yang benar.")
        print(f"✓ fee_usd_roundtrip = {record['fee_usd_roundtrip']} (36 × 0.001 × 2 = 0.072)")
        print(f"✓ entry_order_id = {record['entry_order_id']}, status = {record['entry_status']}")

    # ── 2. Opsional fields None → masuk sebagai None ──────────────────────────

    @unittest.mock.patch("services.supabase_client.upsert_spot")
    def test_optional_fields_passed_as_none(self, mock_upsert):
        print("\n=== Test log_trade: Optional fields None ===")
        executor = self._make_executor()
        order    = self._base_order()
        cand     = self._base_cand()   # ml_score=None, symbol_rank=None

        executor.log_trade(order, cand)   # no cluster_id

        record = mock_upsert.call_args.args[0]
        self.assertIsNone(record["ml_score"])
        self.assertIsNone(record["ml_model_version"])
        self.assertIsNone(record["symbol_rank"])
        self.assertIsNone(record["correlation_cluster_id"])
        print("✓ ml_score=None, symbol_rank=None, correlation_cluster_id=None → semua masuk record.")

    # ── 3. winning_zone None → default zone_type "T1" ────────────────────────

    @unittest.mock.patch("services.supabase_client.upsert_spot")
    def test_winning_zone_none_defaults_to_T1(self, mock_upsert):
        print("\n=== Test log_trade: winning_zone=None → zone_type=T1 ===")
        executor = self._make_executor()
        cand     = self._base_cand(winning_zone=None)

        executor.log_trade(self._base_order(), cand)

        record = mock_upsert.call_args.args[0]
        self.assertEqual(record["zone_type"],  "T1")
        self.assertIsNone(record["zone_label"])
        print("✓ zone_type='T1', zone_label=None saat winning_zone tidak ada.")

    # ── 4. Sizing fields diambil dengan benar ────────────────────────────────

    @unittest.mock.patch("services.supabase_client.upsert_spot")
    def test_sizing_fields_extracted_correctly(self, mock_upsert):
        print("\n=== Test log_trade: sizing fields ===")
        executor = self._make_executor()
        cand     = self._base_cand()
        cand["sizing"] = {"notional_usd": 50.0, "qty": 0.02, "max_loss_usd": 0.75}

        executor.log_trade(self._base_order(), cand)

        record = mock_upsert.call_args.args[0]
        self.assertEqual(record["entry_notional"], 50.0)
        self.assertEqual(record["entry_qty"],       0.02)
        self.assertEqual(record["max_loss_usd"],    0.75)
        # fee: 50 × 0.001 × 2 = 0.1
        self.assertAlmostEqual(record["fee_usd_roundtrip"], 0.1, places=4)
        print(f"✓ notional={record['entry_notional']}, qty={record['entry_qty']}")
        print(f"✓ fee_usd_roundtrip={record['fee_usd_roundtrip']} (50 × 0.001 × 2 = 0.1)")

    # ── 5. budget_for_slot override ───────────────────────────────────────────

    @unittest.mock.patch("services.supabase_client.upsert_spot")
    def test_budget_for_slot_override(self, mock_upsert):
        print("\n=== Test log_trade: budget_for_slot override ===")
        executor = self._make_executor()
        cand     = self._base_cand(budget_for_slot=24.0)

        executor.log_trade(self._base_order(), cand)

        record = mock_upsert.call_args.args[0]
        self.assertEqual(record["budget_usd"], 24.0)
        print(f"✓ budget_usd={record['budget_usd']} (dari budget_for_slot override)")





class TestPartialProtectionLifecycle(unittest.TestCase):
    """
    15 regression tests for the partial-protection lifecycle.

    Scenarios:
      1.  AVAXUSDT: SL filter-invalid, TP valid → TP_ONLY returned
      2.  TP_ONLY: standalone LIMIT_MAKER order params verified
      3.  SL_ONLY: TP filter-invalid, SL valid → SL_ONLY returned
      4.  SL_ONLY: standalone STOP_LOSS_LIMIT order params verified
      5.  UNPROTECTED: both legs filter-invalid → no exchange call
      6.  FULLY_PROTECTED: both legs valid → standard OCO path unchanged
      7.  Preflight no filter found → fallback to OCO path (FULLY_PROTECTED)
      8.  get_avg_price failure → falls back to get_symbol_ticker for avgPrice
      9.  Emergency market sell still takes priority over preflight (price ≤ SL)
      10. TP already exceeded → adjust TP then re-check preflight (FULLY_PROTECTED)
      11. TP_ONLY standalone order API failure → raises RuntimeError
      12. SL_ONLY standalone order API failure → raises RuntimeError
      13. UNPROTECTED state: trade["sl"] / trade["tp1"] not mutated
      14. _check_percent_price_filter returns correct validity flags
      15. PERCENT_PRICE_BY_SIDE post-preflight exchange rejection still fail-fast
    """

    @classmethod
    def setUpClass(cls):
        # Import the real SpotOrderExecutor (not the base-class stub)
        import importlib, types
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        module = importlib.import_module("core.executors.spot_order_executor")
        cls.RealSpotOrderExecutor = module.SpotOrderExecutor

    def _make_executor(self):
        mock_client = MagicMock()
        mock_client.get_symbol_info.return_value = {
            "filters": [
                {"filterType": "PRICE_FILTER",         "tickSize": "0.01"},
                {"filterType": "LOT_SIZE",             "stepSize": "0.001"},
                {"filterType": "PERCENT_PRICE_BY_SIDE",
                 "askMultiplierDown": "0.8",
                 "askMultiplierUp":   "2.0",
                 "avgPriceMins":      "5"},
            ]
        }
        return self.RealSpotOrderExecutor(mock_client), mock_client

    def _make_trade(self, sl=7.58, tp1=11.799, qty=1.53, symbol="AVAXUSDT"):
        return {"symbol": symbol, "entry_qty": qty, "sl": sl, "tp1": tp1}

    def _avax_state(self, mock_client, current=11.13, avg_price=None):
        """Configure mock to reflect AVAXUSDT scenario (SL filter-invalid)."""
        mock_client.get_symbol_ticker.return_value = {"price": str(current)}
        mock_client.get_avg_price.return_value = {"price": str(avg_price or current)}

    # ── Test 1: AVAXUSDT scenario → TP_ONLY ────────────────────────────────
    def test_avax_sl_filter_invalid_returns_tp_only(self):
        """
        AVAXUSDT: entry 7.836, current 11.13, SL 7.58, TP1 11.799.
        SL/avgPrice = 7.58/11.13 = 0.681 < askMultiplierDown=0.80 → SL invalid.
        TP/avgPrice = 11.799/11.13 = 1.060 < askMultiplierUp=2.0  → TP valid.
        Expected: TP_ONLY state, standalone LIMIT_MAKER order placed.
        """
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=7.58, tp1=11.799, qty=1.53)
        self._avax_state(mock_client, current=11.13)
        mock_client.create_order.return_value = {"orderId": 55001, "status": "NEW"}

        result = executor.place_oco_order(trade)

        self.assertEqual(result["protection_state"], "TP_ONLY")
        self.assertEqual(result["tp_order_id"], 55001)
        self.assertIsNone(result["sl_order_id"])
        self.assertIsNone(result["oco_resp"])
        self.assertEqual(result["filter_reason"], "SL_FILTER_INVALID")
        mock_client.create_oco_order.assert_not_called()
        print("✓ Test 1: AVAXUSDT SL filter-invalid → TP_ONLY, TP order placed")

    # ── Test 2: TP_ONLY standalone order params ─────────────────────────────
    def test_tp_only_order_uses_limit_maker(self):
        """Standalone TP order must be LIMIT_MAKER SELL with correct price."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=7.58, tp1=11.799)
        self._avax_state(mock_client, current=11.13)
        mock_client.create_order.return_value = {"orderId": 55002}

        executor.place_oco_order(trade)

        kw = mock_client.create_order.call_args.kwargs
        self.assertEqual(kw["side"],   "SELL")
        self.assertEqual(kw["type"],   "LIMIT_MAKER")
        self.assertAlmostEqual(float(kw["price"]), 11.8, places=0)
        self.assertNotIn("stopPrice", kw, "LIMIT_MAKER must not have stopPrice")
        print(f"✓ Test 2: TP_ONLY order type=LIMIT_MAKER price={kw['price']}")

    # ── Test 3: SL_ONLY state when TP fails filter ──────────────────────────
    def test_sl_only_returned_when_tp_fails_filter(self):
        """TP far above current (askMultiplierUp exceeded) → SL_ONLY."""
        executor, mock_client = self._make_executor()
        # TP at 30.0 / avgPrice 11.13 = 2.69 > askMultiplierUp=2.0 → TP invalid
        # SL at 10.5 / avgPrice 11.13 = 0.94 > askMultiplierDown=0.8 → SL valid
        trade = self._make_trade(sl=10.5, tp1=30.0, qty=1.0)
        self._avax_state(mock_client, current=11.13)
        mock_client.create_order.return_value = {"orderId": 55003}

        result = executor.place_oco_order(trade)

        self.assertEqual(result["protection_state"], "SL_ONLY")
        self.assertEqual(result["sl_order_id"], 55003)
        self.assertIsNone(result["tp_order_id"])
        self.assertEqual(result["filter_reason"], "TP_FILTER_INVALID")
        mock_client.create_oco_order.assert_not_called()
        print("✓ Test 3: TP filter-invalid → SL_ONLY, SL order placed")

    # ── Test 4: SL_ONLY order params ────────────────────────────────────────
    def test_sl_only_order_uses_stop_loss_limit(self):
        """Standalone SL order must be STOP_LOSS_LIMIT with stopPrice and price."""
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=10.5, tp1=30.0, qty=1.0)
        self._avax_state(mock_client, current=11.13)
        mock_client.create_order.return_value = {"orderId": 55004}

        executor.place_oco_order(trade)

        kw = mock_client.create_order.call_args.kwargs
        self.assertEqual(kw["side"],        "SELL")
        self.assertEqual(kw["type"],        "STOP_LOSS_LIMIT")
        self.assertEqual(kw["timeInForce"], "GTC")
        self.assertIn("stopPrice", kw, "STOP_LOSS_LIMIT must have stopPrice")
        self.assertIn("price",     kw, "STOP_LOSS_LIMIT must have limit price")
        self.assertLess(float(kw["price"]), float(kw["stopPrice"]),
                        "limit price must be below stop trigger")
        print(f"✓ Test 4: SL_ONLY order type=STOP_LOSS_LIMIT "
              f"stopPrice={kw['stopPrice']} price={kw['price']}")

    # ── Test 5: UNPROTECTED when both legs fail filter ───────────────────────
    def test_unprotected_when_both_legs_fail_filter(self):
        """Both TP and SL outside filter → UNPROTECTED, no exchange order placed."""
        executor, mock_client = self._make_executor()
        # SL too low (0.681), TP too high (3.0) — both outside band
        trade = self._make_trade(sl=7.58, tp1=35.0, qty=1.53)
        self._avax_state(mock_client, current=11.13)

        result = executor.place_oco_order(trade)

        self.assertEqual(result["protection_state"], "UNPROTECTED")
        self.assertIsNone(result["oco_resp"])
        self.assertIsNone(result["tp_order_id"])
        self.assertIsNone(result["sl_order_id"])
        self.assertEqual(result["filter_reason"], "BOTH_LEGS_FILTER_INVALID")
        mock_client.create_order.assert_not_called()
        mock_client.create_oco_order.assert_not_called()
        print("✓ Test 5: Both legs filter-invalid → UNPROTECTED, no exchange call")

    # ── Test 6: FULLY_PROTECTED when both legs valid ─────────────────────────
    def test_fully_protected_when_both_legs_valid(self):
        """Both legs within filter → FULLY_PROTECTED, standard OCO placed."""
        executor, mock_client = self._make_executor()
        # current=10.0, avgPrice=10.0, SL=9.0 (0.9>0.8 ✓), TP=11.5 (1.15<2.0 ✓)
        trade = self._make_trade(sl=9.0, tp1=11.5, qty=1.0)
        mock_client.get_symbol_ticker.return_value = {"price": "10.0"}
        mock_client.get_avg_price.return_value = {"price": "10.0"}
        mock_client.create_oco_order.return_value = {
            "orderListId": 7001,
            "orderReports": [{"orderId": 700}, {"orderId": 701}],
        }

        result = executor.place_oco_order(trade)

        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertEqual(result["oco_resp"]["orderListId"], 7001)
        self.assertIsNone(result["tp_order_id"])
        self.assertIsNone(result["sl_order_id"])
        mock_client.create_oco_order.assert_called_once()
        mock_client.create_order.assert_not_called()
        print("✓ Test 6: Both legs valid → FULLY_PROTECTED, standard OCO placed")

    # ── Test 7: No PERCENT_PRICE_BY_SIDE filter → fallback to OCO ────────────
    def test_no_percent_price_filter_falls_back_to_oco(self):
        """Symbol without PERCENT_PRICE_BY_SIDE filter → treat both as valid."""
        mock_client = MagicMock()
        # No PERCENT_PRICE_BY_SIDE in filters
        mock_client.get_symbol_info.return_value = {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE",     "stepSize": "0.001"},
            ]
        }
        mock_client.get_symbol_ticker.return_value = {"price": "61000.00"}
        mock_client.get_avg_price.return_value = {"price": "61000.00"}
        mock_client.create_oco_order.return_value = {
            "orderListId": 8001,
            "orderReports": [{"orderId": 800}],
        }
        executor = self.RealSpotOrderExecutor(mock_client)
        trade = {"symbol": "BTCUSDT", "entry_qty": 0.01, "sl": 59000.0, "tp1": 63000.0}

        result = executor.place_oco_order(trade)

        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        mock_client.create_oco_order.assert_called_once()
        print("✓ Test 7: No PERCENT_PRICE_BY_SIDE filter → OCO placed (FULLY_PROTECTED)")

    # ── Test 8: get_avg_price failure → fallback to get_symbol_ticker ─────────
    def test_avg_price_failure_falls_back_to_ticker(self):
        """If get_avg_price() raises, _check_percent_price_filter uses ticker price."""
        executor, mock_client = self._make_executor()
        mock_client.get_avg_price.side_effect = Exception("network error")
        # With ticker price = 11.13:  SL 7.58/11.13=0.681 < 0.8 → invalid
        mock_client.get_symbol_ticker.return_value = {"price": "11.13"}
        trade = self._make_trade(sl=7.58, tp1=11.5)
        mock_client.create_order.return_value = {"orderId": 55008}

        result = executor.place_oco_order(trade)

        self.assertEqual(result["protection_state"], "TP_ONLY",
                         "Fallback to ticker should still detect SL as filter-invalid")
        print("✓ Test 8: get_avg_price failure → ticker fallback, SL still detected as invalid")

    # ── Test 9: Emergency market sell takes priority over preflight ───────────
    def test_price_below_sl_emergency_sell_before_preflight(self):
        """Price ≤ SL → emergency market sell regardless of filter state."""
        executor, mock_client = self._make_executor()
        # Price already below SL
        mock_client.get_symbol_ticker.return_value = {"price": "7.0"}
        mock_client.get_avg_price.return_value = {"price": "7.0"}
        mock_client.create_order.return_value = {
            "orderId": 9001, "status": "FILLED",
            "executedQty": "1.53", "cummulativeQuoteQty": "10.71",
        }
        trade = self._make_trade(sl=7.58, tp1=11.799, qty=1.53)

        result = executor.place_oco_order(trade)

        self.assertTrue(result.get("_market_sold") or trade.get("_market_sold"))
        # create_order called for MARKET SELL, create_oco_order NOT called
        mock_client.create_oco_order.assert_not_called()
        kw = mock_client.create_order.call_args.kwargs
        self.assertEqual(kw["type"], "MARKET")
        print("✓ Test 9: Price ≤ SL → emergency market sell (preflight not reached)")

    # ── Test 10: TP exceeded → adjust TP then check preflight ─────────────────
    @unittest.mock.patch("time.sleep", return_value=None)
    def test_tp_exceeded_adjust_then_preflight_passes(self, _sleep):
        """Price ≥ TP → TP adjusted upward, preflight re-checks new TP value."""
        executor, mock_client = self._make_executor()
        # current=12.0, TP=11.5 → TP exceeded; adjusted TP = 12.0 * 1.003 = 12.036
        # With adjusted TP: 12.036/12.0 = 1.003 < 2.0 ✓; SL 10.8/12.0 = 0.9 > 0.8 ✓
        mock_client.get_symbol_ticker.return_value = {"price": "12.0"}
        mock_client.get_avg_price.return_value = {"price": "12.0"}
        trade = self._make_trade(sl=10.8, tp1=11.5, qty=1.0)
        mock_client.create_oco_order.return_value = {
            "orderListId": 9010, "orderReports": [{"orderId": 901}],
        }

        result = executor.place_oco_order(trade)

        self.assertEqual(result["protection_state"], "FULLY_PROTECTED")
        self.assertGreater(trade["tp1"], 12.0, "TP must have been adjusted above current")
        print(f"✓ Test 10: TP exceeded → adjusted to {trade['tp1']:.4f}, FULLY_PROTECTED")

    # ── Test 11: TP_ONLY standalone order API failure → RuntimeError ──────────
    def test_tp_only_standalone_order_failure_raises(self):
        """If standalone TP order placement fails → RuntimeError propagated."""
        from binance.exceptions import BinanceAPIException
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=7.58, tp1=11.799)
        self._avax_state(mock_client, current=11.13)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 400
        mock_resp.text = '{"code":-2010,"msg":"Account has insufficient balance"}'
        mock_resp.json.return_value = {"code": -2010, "msg": "Account has insufficient balance"}
        mock_client.create_order.side_effect = BinanceAPIException(
            mock_resp, 400, mock_resp.text
        )

        with self.assertRaises(RuntimeError) as ctx:
            executor.place_oco_order(trade)

        self.assertIn("Standalone TP order failed", str(ctx.exception))
        print("✓ Test 11: TP_ONLY standalone order API failure → RuntimeError")

    # ── Test 12: SL_ONLY standalone order API failure → RuntimeError ──────────
    def test_sl_only_standalone_order_failure_raises(self):
        """If standalone SL order placement fails → RuntimeError propagated."""
        from binance.exceptions import BinanceAPIException
        executor, mock_client = self._make_executor()
        trade = self._make_trade(sl=10.5, tp1=30.0, qty=1.0)  # TP invalid, SL valid
        self._avax_state(mock_client, current=11.13)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 400
        mock_resp.text = '{"code":-2010,"msg":"Account has insufficient balance"}'
        mock_resp.json.return_value = {"code": -2010, "msg": "Account has insufficient balance"}
        mock_client.create_order.side_effect = BinanceAPIException(
            mock_resp, 400, mock_resp.text
        )

        with self.assertRaises(RuntimeError) as ctx:
            executor.place_oco_order(trade)

        self.assertIn("Standalone SL order failed", str(ctx.exception))
        print("✓ Test 12: SL_ONLY standalone order API failure → RuntimeError")

    # ── Test 13: UNPROTECTED never mutates trade["sl"] or trade["tp1"] ────────
    def test_unprotected_does_not_mutate_sl_or_tp(self):
        """No protection path should ever shift SL or TP values on the trade."""
        executor, mock_client = self._make_executor()
        original_sl  = 7.58
        original_tp1 = 35.0
        trade = self._make_trade(sl=original_sl, tp1=original_tp1)
        self._avax_state(mock_client, current=11.13)

        result = executor.place_oco_order(trade)

        self.assertEqual(result["protection_state"], "UNPROTECTED")
        self.assertEqual(trade["sl"],  original_sl,  "SL must never be mutated")
        self.assertEqual(trade["tp1"], original_tp1, "TP must never be mutated")
        print("✓ Test 13: UNPROTECTED — sl and tp1 unchanged on trade dict")

    # ── Test 14: _check_percent_price_filter correctness ─────────────────────
    def test_check_percent_price_filter_returns_correct_validity(self):
        """_check_percent_price_filter returns valid=True/False based on ratio."""
        executor, mock_client = self._make_executor()
        mock_client.get_avg_price.return_value = {"price": "11.13"}
        mock_client.get_symbol_ticker.return_value = {"price": "11.13"}

        # SL 7.58: ratio=0.681, askMultiplierDown=0.8 → INVALID
        sl_result = executor._check_percent_price_filter("AVAXUSDT", 7.58)
        self.assertFalse(sl_result["valid"])
        self.assertTrue(sl_result["filter_found"])
        self.assertAlmostEqual(sl_result["ratio"], 7.58 / 11.13, places=3)

        # TP 11.799: ratio=1.060, within [0.8, 2.0] → VALID
        tp_result = executor._check_percent_price_filter("AVAXUSDT", 11.799)
        self.assertTrue(tp_result["valid"])
        self.assertTrue(tp_result["filter_found"])
        self.assertAlmostEqual(tp_result["ratio"], 11.799 / 11.13, places=3)
        print(f"✓ Test 14: SL ratio={sl_result['ratio']:.3f} → invalid; "
              f"TP ratio={tp_result['ratio']:.3f} → valid")

    # ── Test 15: Post-preflight PERCENT_PRICE_BY_SIDE rejection still fail-fast
    @unittest.mock.patch("time.sleep", return_value=None)
    def test_post_preflight_percent_price_rejection_still_fail_fast(self, _sleep):
        """
        If avgPrice shifts between preflight and OCO call so that the exchange
        rejects with PERCENT_PRICE_BY_SIDE, the error must still be fail-fast
        (no retry).  FULLY_PROTECTED path, but exchange fires -1013 anyway.
        """
        from binance.exceptions import BinanceAPIException
        executor, mock_client = self._make_executor()
        # Preflight passes: both legs valid with avg=10.0
        mock_client.get_symbol_ticker.return_value = {"price": "10.0"}
        mock_client.get_avg_price.return_value = {"price": "10.0"}
        trade = self._make_trade(sl=9.0, tp1=11.5, qty=1.0)
        mock_resp = MagicMock(spec=requests.Response)
        mock_resp.status_code = 400
        mock_resp.text = '{"code":-1013,"msg":"Filter failure: PERCENT_PRICE_BY_SIDE"}'
        mock_resp.json.return_value = {"code": -1013,
                                       "msg": "Filter failure: PERCENT_PRICE_BY_SIDE"}
        mock_client.create_oco_order.side_effect = BinanceAPIException(
            mock_resp, 400, mock_resp.text
        )

        with self.assertRaises(RuntimeError) as ctx:
            executor.place_oco_order(trade)

        self.assertEqual(mock_client.create_oco_order.call_count, 1,
                         "Post-preflight PERCENT_PRICE_BY_SIDE must not be retried")
        self.assertIn("PERCENT_PRICE_BY_SIDE", str(ctx.exception))
        _sleep.assert_not_called()
        print("✓ Test 15: Post-preflight PERCENT_PRICE_BY_SIDE → fail-fast, no retry")


if __name__ == "__main__":
    unittest.main(verbosity=2)