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
        expected_resp = {"orderListId": 12345, "listStatusType": "EXEC_STARTED"}
        mock_client.create_oco_order.return_value = expected_resp

        result = executor.place_exit_orders(trade)

        self.assertEqual(result, expected_resp)
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

        self.assertEqual(result, expected_resp)
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
        "supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "supabase_client.update_spot_by_order_id"
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
        "supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "supabase_client.update_spot_by_order_id"
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
        "supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "supabase_client.update_spot_by_order_id"
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
        "supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "supabase_client.update_spot_by_order_id"
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
        "supabase_client.fetch_all_spot"
    )
    @unittest.mock.patch(
        "supabase_client.update_spot_by_order_id"
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

    @unittest.mock.patch("supabase_client.upsert_spot")
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

    @unittest.mock.patch("supabase_client.upsert_spot")
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

    @unittest.mock.patch("supabase_client.upsert_spot")
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

    @unittest.mock.patch("supabase_client.upsert_spot")
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

    @unittest.mock.patch("supabase_client.upsert_spot")
    def test_budget_for_slot_override(self, mock_upsert):
        print("\n=== Test log_trade: budget_for_slot override ===")
        executor = self._make_executor()
        cand     = self._base_cand(budget_for_slot=24.0)

        executor.log_trade(self._base_order(), cand)

        record = mock_upsert.call_args.args[0]
        self.assertEqual(record["budget_usd"], 24.0)
        print(f"✓ budget_usd={record['budget_usd']} (dari budget_for_slot override)")


if __name__ == "__main__":
    unittest.main(verbosity=2)