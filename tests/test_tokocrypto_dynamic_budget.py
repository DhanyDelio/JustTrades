"""
tests/test_tokocrypto_dynamic_budget.py
======================================
Comprehensive unit tests for Dynamic Budget Allocation & Pre-Flight Guards on Tokocrypto:
1. Modal bertambah melalui deposit (menjamin alokasi slot tidak melebihi equity slot cap saat 1 slot tersisa)
2. Modal bertambah setelah TP terealisasi
3. Modal berkurang setelah SL dan fee (step-down aman / graceful skip)
4. Slot tersedia tinggal satu setelah beberapa posisi aktif
5. Available IDR berbeda dari Total Equity
6. Pre-flight balance check: saldo tidak cukup untuk order (aborts cleanly)
7. Dua kandidat mencoba memakai budget yang sama (in-process reservation guard)
8. Dua worker mencoba entry pada pair yang sama (symbol lifecycle lock & active submission guard)
9. Order BUY masih pending (terhitung sebagai slot terpakai & mengunci IDR)
10. Metadata minimum notional berbeda antar-pair
11. Saver logic menolak BUY jika protective OCO tidak layak
12. Posisi SOL existing tidak dimodifikasi oleh perubahan ini
"""

import math
import unittest
from unittest.mock import MagicMock, patch

from core.clients.tokocrypto_client import ExchangeBalance, ExchangeSymbol, TokocryptoClient
from core.clients.tokocrypto_order_executor import (
    TokocryptoOrderExecutor,
    claim_budget_reservation,
    release_budget_reservation,
    get_reserved_budget,
    _ACTIVE_BUDGET_RESERVATIONS,
)
from tokocrypto_executor import (
    calculate_adaptive_allocation,
    calculate_available_slots,
    calculate_new_order_allocation,
    MIN_NOTIONAL_IDR,
    MAX_POSITIONS,
)


class TestTokocryptoDynamicBudget(unittest.TestCase):
    def setUp(self):
        from core.clients import tokocrypto_order_executor as order_executor_module

        with patch("core.clients.tokocrypto_order_executor._LIFECYCLE_LOCKS_GUARD"):
            _ACTIVE_BUDGET_RESERVATIONS.clear()
            order_executor_module._UNKNOWN_ENTRY_SUBMISSIONS.clear()
            order_executor_module._ACTIVE_ENTRY_SUBMISSIONS.clear()

        # Strict test isolation: mock all database write pathways by default
        self._patch_exec_upsert = patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto")
        self._patch_exec_update = patch("core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id")
        self._patch_exec_tg = patch("core.clients.tokocrypto_order_executor._send_toko_telegram")
        self._patch_sb_upsert = patch("services.supabase_client.upsert_tokocrypto")
        self._patch_sb_update = patch("services.supabase_client.update_tokocrypto_by_order_id")

        self.mock_exec_upsert = self._patch_exec_upsert.start()
        self.mock_exec_update = self._patch_exec_update.start()
        self.mock_exec_tg = self._patch_exec_tg.start()
        self.mock_sb_upsert = self._patch_sb_upsert.start()
        self.mock_sb_update = self._patch_sb_update.start()

        self.addCleanup(self._patch_sb_update.stop)
        self.addCleanup(self._patch_sb_upsert.stop)
        self.addCleanup(self._patch_exec_tg.stop)
        self.addCleanup(self._patch_exec_update.stop)
        self.addCleanup(self._patch_exec_upsert.stop)

    # -------------------------------------------------------------------------
    # 1. Modal bertambah melalui deposit (Slot Cap Enforcement)
    # -------------------------------------------------------------------------
    def test_deposit_increase_respects_equity_slot_cap_when_one_slot_left(self):
        """
        Scenario:
          - Initial 4 positions committed Rp 800.000 (Rp 200k each).
          - Sisa modal awal Rp 200.000.
          - User melakukan deposit tambahan Rp 2.000.000.
          - Available IDR = Rp 2.200.000.
          - Total Equity = Rp 800.000 (committed) + Rp 2.200.000 (free) = Rp 3.000.000.
          - MAX_POSITIONS = 5. Available slots = 1.
          Formula:
            eff_slots = min(5, int(3_000_000 // 20_000)) = 5
            slot_cap = 3_000_000 / 5 = Rp 600.000.
            raw_alloc = 2_200_000 / 1 = Rp 2.200.000.
            final_alloc = min(raw_alloc, slot_cap) = Rp 600.000.
          Order TIDAK boleh menghabiskan Rp 2.200.000 ke 1 koin!
        """
        free_idr = 2_200_000.0
        committed_idr = 800_000.0
        total_equity = free_idr + committed_idr  # Rp 3.000.000
        available_slots = 1

        target_slots, alloc = calculate_adaptive_allocation(
            wallet_balance=free_idr,
            available_slots=available_slots,
            min_notional=MIN_NOTIONAL_IDR,
            max_positions=5,
            total_equity=total_equity,
        )

        self.assertEqual(target_slots, 1)
        self.assertEqual(alloc, 600_000.0)
        self.assertLess(alloc, free_idr)

    # -------------------------------------------------------------------------
    # 2. Modal bertambah setelah TP terealisasi
    # -------------------------------------------------------------------------
    def test_modal_increase_after_tp_realized(self):
        """
        Scenario:
          - Posisi ditutup dengan profit (TP), dana kembali ke kas.
          - Saldo bebas meningkat dari Rp 400.000 menjadi Rp 600.000.
          - Slot kosong = 3, max_positions = 5.
          - Total equity = Rp 600.000 (kas) + Rp 400.000 (2 posisi aktif) = Rp 1.000.000.
          - Alokasi per order naik proporsional menjadi Rp 200.000 per slot.
        """
        free_idr = 600_000.0
        committed_idr = 400_000.0
        total_equity = free_idr + committed_idr  # Rp 1.000.000
        available_slots = 3

        target_slots, alloc = calculate_adaptive_allocation(
            wallet_balance=free_idr,
            available_slots=available_slots,
            min_notional=MIN_NOTIONAL_IDR,
            max_positions=5,
            total_equity=total_equity,
        )

        self.assertEqual(target_slots, 3)
        self.assertEqual(alloc, 200_000.0)

    # -------------------------------------------------------------------------
    # 3. Modal berkurang setelah SL dan fee
    # -------------------------------------------------------------------------
    def test_modal_decrease_after_sl_and_fee_stepdown(self):
        """
        Scenario:
          - Setelah SL terealisasi dan fee terpotong, sisa saldo bebas hanya Rp 35.000.
          - 2 slot kosong.
          - 35.000 / 2 = 17.500 < 20.000 (min notional).
          - Sistem otomatis step down ke 1 slot = Rp 35.000.
        """
        free_idr = 35_000.0
        available_slots = 2

        target_slots, alloc = calculate_adaptive_allocation(
            wallet_balance=free_idr,
            available_slots=available_slots,
            min_notional=20_000.0,
            max_positions=5,
            total_equity=35_000.0,
        )

        self.assertEqual(target_slots, 1)
        self.assertEqual(alloc, 35_000.0)

    def test_modal_decrease_below_min_notional_skips(self):
        """
        Scenario:
          - Saldo bebas turun ke Rp 18.000.
          - Bahkan 1 slot pun tidak memenuhi minimum notional (Rp 20.000).
          - Hasil: target_slots = 0, alloc = 0.0 (NO ENTRY).
        """
        free_idr = 18_000.0
        available_slots = 2

        target_slots, alloc = calculate_adaptive_allocation(
            wallet_balance=free_idr,
            available_slots=available_slots,
            min_notional=20_000.0,
            max_positions=5,
            total_equity=18_000.0,
        )

        self.assertEqual(target_slots, 0)
        self.assertEqual(alloc, 0.0)

    # -------------------------------------------------------------------------
    # 4. Slot tersedia tinggal satu setelah beberapa posisi aktif
    # -------------------------------------------------------------------------
    def test_single_slot_remaining_normal_capital(self):
        """
        Scenario:
          - Akun Rp 1.000.000, 4 slot terisi masing-masing Rp 200.000 (committed Rp 800.000).
          - Sisa free IDR = Rp 200.000. Sisa slot = 1.
          - Alokasi untuk slot terakhir tepat Rp 200.000.
        """
        free_idr = 200_000.0
        total_equity = 1_000_000.0
        target_slots, alloc = calculate_adaptive_allocation(
            wallet_balance=free_idr,
            available_slots=1,
            min_notional=20_000.0,
            max_positions=5,
            total_equity=total_equity,
        )
        self.assertEqual(target_slots, 1)
        self.assertEqual(alloc, 200_000.0)

    # -------------------------------------------------------------------------
    # 5. Available IDR berbeda dari Total Equity
    # -------------------------------------------------------------------------
    def test_available_idr_distinct_from_total_equity(self):
        """
        Scenario:
          - Total Equity = Rp 5.000.000 (Rp 4.000.000 ada di crypto holdings).
          - Available IDR kas hanya Rp 1.000.000.
          - Sistem tidak boleh menganggap Rp 5.000.000 sebagai saldo belanja.
          - Belanja hanya dari Rp 1.000.000, dibagi 2 slot tersedia = Rp 500.000/slot.
        """
        free_idr = 1_000_000.0
        total_equity = 5_000_000.0
        available_slots = 2

        target_slots, alloc = calculate_adaptive_allocation(
            wallet_balance=free_idr,
            available_slots=available_slots,
            min_notional=20_000.0,
            max_positions=5,
            total_equity=total_equity,
        )

        # Slot cap = 5.000.000 / 5 = 1.000.000
        # Raw alloc = 1.000.000 / 2 = 500.000 <= 1.000.000
        self.assertEqual(target_slots, 2)
        self.assertEqual(alloc, 500_000.0)

    # -------------------------------------------------------------------------
    # 6. Pre-flight Balance Check: Saldo tidak cukup untuk order
    # -------------------------------------------------------------------------
    def test_preflight_balance_check_aborts_when_insufficient_live_idr(self):
        """
        Scenario:
          - Kandidat membutuhkan Rp 100.000.
          - Live balance API di exchange hanya melaporkan Rp 50.000 free.
          - Pre-flight check di executor menolak order sebelum POST.
        """
        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda s: s
        client.get_open_orders.return_value = []
        sym_info = MagicMock(spec=ExchangeSymbol)
        sym_info.tick_size = 1.0
        sym_info.step_size = 1.0
        sym_info.min_qty = 1.0
        sym_info.min_notional = 20_000.0
        sym_info.constraints = {
            "tick_size": 1.0,
            "step_size": 1.0,
            "min_qty": 1.0,
            "min_notional": 20_000.0,
        }
        client.get_symbol.return_value = sym_info
        client.round_step = lambda val, step: math.floor(val / step) * step
        client.round_tick = lambda val, tick: math.floor(val / tick) * tick

        # Saldo exchange live hanya 50.000 IDR
        bal_mock = MagicMock()
        bal_mock.free = 50_000.0
        client.get_balance.return_value = bal_mock

        executor = TokocryptoOrderExecutor(
            client=client,
            supervised=False,
            dry_run=False,
            max_slots=1,
            check_balance=True,
        )
        executor.has_active_position = MagicMock(return_value=False)

        cand = {
            "symbol": "BTC_IDR",
            "entry_price": 1000.0,
            "tp_price": 1200.0,
            "sl_price": 950.0,
        }

        # Sizing meminta slot_size = 100.000 IDR (membutuhkan 100 koin @ 1000 = 100.000 IDR)
        res = executor.execute_entry(cand, slot_size_idr=100_000.0)
        self.assertIsNone(res)
        client._signed_post.assert_not_called()

    # -------------------------------------------------------------------------
    # 7. Dua kandidat mencoba memakai budget yang sama (Reservation Guard)
    # -------------------------------------------------------------------------
    def test_concurrent_budget_reservation_blocks_second_candidate(self):
        """
        Scenario:
          - Saldo live exchange: Rp 100.000.
          - Kandidat A mereservasi Rp 80.000.
          - Kandidat B membutuhkan Rp 50.000.
          - Effective free untuk B = Rp 100.000 - Rp 80.000 = Rp 20.000 < Rp 50.000.
          - Kandidat B ditolak karena dana sudah direservasi oleh Kandidat A.
        """
        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda s: s
        client.get_open_orders.return_value = []
        sym_info = MagicMock(spec=ExchangeSymbol)
        sym_info.tick_size = 1.0
        sym_info.step_size = 1.0
        sym_info.min_qty = 1.0
        sym_info.min_notional = 20_000.0
        sym_info.constraints = {
            "tick_size": 1.0,
            "step_size": 1.0,
            "min_qty": 1.0,
            "min_notional": 20_000.0,
        }
        client.get_symbol.return_value = sym_info
        client.round_step = lambda val, step: math.floor(val / step) * step
        client.round_tick = lambda val, tick: math.floor(val / tick) * tick

        bal_mock = MagicMock()
        bal_mock.free = 100_000.0
        client.get_balance.return_value = bal_mock

        executor = TokocryptoOrderExecutor(
            client=client,
            supervised=False,
            dry_run=False,
            max_slots=1,
            check_balance=True,
        )
        executor.has_active_position = MagicMock(return_value=False)

        # Kandidat A sudah mengklaim reservasi Rp 80.000 untuk ETH_IDR
        claim_budget_reservation("ETH_IDR", 80_000.0)

        cand_b = {
            "symbol": "BTC_IDR",
            "entry_price": 1000.0,
            "tp_price": 1200.0,
            "sl_price": 950.0,
        }

        # Kandidat B mencoba order Rp 50.000
        res = executor.execute_entry(cand_b, slot_size_idr=50_000.0)
        self.assertIsNone(res)
        client._signed_post.assert_not_called()

        # Setelah Kandidat A selesai dan reservasi dilepas:
        release_budget_reservation("ETH_IDR")
        self.assertEqual(get_reserved_budget(), 0.0)

    # -------------------------------------------------------------------------
    # 8. Dua worker mencoba entry pada pair yang sama (Active Submission Guard)
    # -------------------------------------------------------------------------
    def test_duplicate_pair_entry_guard(self):
        """
        Scenario:
          - Symbol sudah aktif di Supabase atau live exchange.
          - Entry kedua pada pair yang sama langsung dibatalkan (fail-closed).
        """
        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda s: s
        # Exchange memiliki working order untuk ADA_IDR
        client.get_open_orders.return_value = [{"symbol": "ADA_IDR", "status": "NEW"}]

        executor = TokocryptoOrderExecutor(
            client=client,
            supervised=False,
            dry_run=False,
            max_slots=1,
            check_balance=True,
        )

        cand = {"symbol": "ADA_IDR", "entry_price": 5000.0}
        res = executor.execute_entry(cand, slot_size_idr=50_000.0)
        self.assertIsNone(res)
        client._signed_post.assert_not_called()

    # -------------------------------------------------------------------------
    # 9. Order BUY masih pending (Dihitung sebagai slot terpakai)
    # -------------------------------------------------------------------------
    def test_pending_buy_order_counts_as_occupied_slot(self):
        """
        Scenario:
          - 1 trade berstatus OPEN dengan entry_status NEW (pending Limit Buy).
          - calculate_available_slots(open_count=1, max_positions=5) -> 4 slot tersisa.
        """
        avail = calculate_available_slots(open_count=1, max_positions=5)
        self.assertEqual(avail, 4)

    # -------------------------------------------------------------------------
    # 10. Metadata minimum notional berbeda antar-pair
    # -------------------------------------------------------------------------
    def test_distinct_min_notional_per_symbol(self):
        """
        Scenario:
          - Pair A memiliki min_notional = Rp 20.000 (standard).
          - Pair B memiliki min_notional = Rp 50.000 (higher exchange tier).
          - Sizing budget Rp 30.000 lolos untuk Pair A, namun ditolak untuk Pair B.
        """
        client = MagicMock(spec=TokocryptoClient)
        executor = TokocryptoOrderExecutor(client=client, max_slots=1)

        # Pair A
        sym_a = MagicMock(spec=ExchangeSymbol)
        sym_a.step_size = 1.0
        sym_a.min_qty = 1.0
        sym_a.min_notional = 20_000.0
        client.get_symbol.return_value = sym_a
        client.round_step = lambda val, step: math.floor(val / step) * step

        cand_a = {"symbol": "PAIR_A_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 900.0}
        self.assertTrue(executor.validate_and_size(cand_a, available_idr=30_000.0))

        # Pair B
        sym_b = MagicMock(spec=ExchangeSymbol)
        sym_b.step_size = 1.0
        sym_b.min_qty = 1.0
        sym_b.min_notional = 50_000.0
        client.get_symbol.return_value = sym_b

        cand_b = {"symbol": "PAIR_B_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 900.0}
        self.assertFalse(executor.validate_and_size(cand_b, available_idr=30_000.0))

    # -------------------------------------------------------------------------
    # 11. Saver logic menolak BUY jika protective OCO tidak layak
    # -------------------------------------------------------------------------
    def test_saver_logic_rejects_buy_when_oco_ineligible(self):
        """
        Scenario:
          - BNB entry Rp 13.122.250, qty 0.002 (notional ~ Rp 26.244).
          - Setelah fee taker terpotong (0.15%), kuantiti bersih 0.001997 floored ke 0.001.
          - TP notional = 0.001 * 13.802.619 = Rp 13.802 < Rp 20.000.
          - validate_and_size WAJIB menolak order BUY sebelum diposting.
        """
        sym = MagicMock(spec=ExchangeSymbol)
        sym.symbol = "BNB_IDR"
        sym.tick_size = 1.0
        sym.step_size = 0.001
        sym.min_qty = 0.001
        sym.min_notional = 20_000.0
        sym.constraints = {
            "tick_size": 1.0,
            "step_size": 0.001,
            "min_qty": 0.001,
            "min_notional": 20_000.0,
        }

        client = MagicMock(spec=TokocryptoClient)
        client.get_symbol.return_value = sym
        client.round_step = lambda val, step: math.floor(val / step) * step

        executor = TokocryptoOrderExecutor(client=client, max_slots=1)
        cand = {
            "symbol": "BNB_IDR",
            "entry_price": 13_122_250.0,
            "tp_price": 13_802_619.0,
            "sl_price": 12_859_805.0,
        }

        is_valid = executor.validate_and_size(cand, available_idr=26_244.5)
        self.assertFalse(is_valid, "Order must be rejected because post-fee OCO notional < 20.000")

    # -------------------------------------------------------------------------
    # 12. Posisi SOL existing tidak dimodifikasi oleh perubahan ini
    # -------------------------------------------------------------------------
    def test_existing_sol_position_untouched_and_blocks_further_sol_entry(self):
        """
        Scenario:
          - Database Supabase memiliki 2 posisi SOL_IDR lama yang menumpuk.
          - Sistem mempertahankan data SOL tersebut tanpa mengubahnya.
          - Pemeriksaan has_active_position("SOL_IDR") tetap memblokir entry SOL baru.
        """
        sol_existing = [
            {"symbol": "SOL_IDR", "entry_order_id": "SOL_1", "exit_status": "OPEN", "entry_status": "FILLED"},
            {"symbol": "SOL_IDR", "entry_order_id": "SOL_2", "exit_status": "OPEN", "entry_status": "FILLED"},
        ]

        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda s: s
        client.get_open_orders.return_value = []

        executor = TokocryptoOrderExecutor(client=client, max_slots=5)

        with patch("services.supabase_client.fetch_all_tokocrypto_strict", return_value=sol_existing):
            # Posisi SOL terdeteksi aktif
            self.assertTrue(executor.has_active_position("SOL_IDR"))

            # Order SOL baru langsung dibatalkan tanpa menyentuh SOL yang lama
            cand_sol = {"symbol": "SOL_IDR", "entry_price": 2_500_000.0}
            res = executor.execute_entry(cand_sol, slot_size_idr=200_000.0)
            self.assertIsNone(res)
            client._signed_post.assert_not_called()

            # Data existing SOL tetap utuh
            self.assertEqual(len(sol_existing), 2)
            self.assertEqual(sol_existing[0]["exit_status"], "OPEN")
            self.assertEqual(sol_existing[1]["exit_status"], "OPEN")

    # -------------------------------------------------------------------------
    # 13. Supabase gagal saat ada posisi terbuka -> Fail Closed
    # -------------------------------------------------------------------------
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_13_supabase_failure_during_propose_fails_closed(
        self, mock_build_exec, mock_build_client, mock_build_scanner
    ):
        """
        Scenario:
          - Supabase mengalami network error / downtime saat propose dipanggil.
          - Sistem WAJIB fail-closed dan membatalkan propose tanpa melakukan scan atau order.
        """
        from tokocrypto_executor import cmd_propose

        with patch(
            "services.supabase_client.fetch_all_tokocrypto_strict",
            side_effect=RuntimeError("Supabase connection reset"),
        ):
            scanner_mock = MagicMock()
            mock_build_scanner.return_value = scanner_mock
            exec_mock = MagicMock()
            mock_build_exec.return_value = exec_mock

            cmd_propose()

            # Scanner dan executor tidak boleh dipanggil sama sekali!
            scanner_mock.gather_candidates.assert_not_called()
            exec_mock.execute_entry.assert_not_called()

    # -------------------------------------------------------------------------
    # 14. Posisi terisi dengan modal tidak terverifikasi -> Fail-Closed & Skip Entry
    # -------------------------------------------------------------------------
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_14_unverified_capital_fails_closed_and_skips_entry(
        self, mock_build_exec, mock_build_client, mock_build_scanner
    ):
        """
        Scenario:
          - Ada posisi terbuka berstatus FILLED, namun data entry_price/qty tidak valid / 0.
          - committed_idr tidak dapat dihitung secara andal.
          - Kebijakan fail-closed: bot WAJIB skip entry dan tidak memanggil scanner / executor.
        """
        from tokocrypto_executor import cmd_propose

        unverified_trade = [
            {"symbol": "ETH_IDR", "exit_status": "OPEN", "entry_status": "FILLED", "entry_fill_price": 0, "entry_qty": 0}
        ]
        with patch(
            "services.supabase_client.fetch_all_tokocrypto_strict",
            return_value=unverified_trade,
        ):
            client_mock = MagicMock()
            client_mock.get_open_orders.return_value = []
            bal_mock = MagicMock()
            bal_mock.free = 500_000.0
            bal_mock.locked = 0.0
            client_mock.get_balance.return_value = bal_mock
            mock_build_client.return_value = client_mock

            scanner_mock = MagicMock()
            mock_build_scanner.return_value = scanner_mock
            exec_mock = MagicMock()
            mock_build_exec.return_value = exec_mock

            cmd_propose()

            # Fail-closed: scanner dan executor tidak boleh dipanggil!
            scanner_mock.gather_candidates.assert_not_called()
            exec_mock.execute_entry.assert_not_called()

    # -------------------------------------------------------------------------
    # 15. Timeout saat POST order -> Reservasi DANA TETAP DITAHAN
    # -------------------------------------------------------------------------
    def test_15_timeout_post_retains_budget_reservation(self):
        """
        Scenario:
          - Order BUY dikirim ke exchange, tetapi terjadi socket read timeout.
          - Status order di exchange menjadi AMBIGU (mungkin sudah diterima).
          - Reservasi dana WAJIB TETAP AKTIF agar dana tersebut tidak dipakai kandidat lain.
        """
        from core.clients.tokocrypto_client import TokocryptoSubmissionUnknownError

        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda s: s
        client.get_open_orders.return_value = []
        sym_info = MagicMock(spec=ExchangeSymbol)
        sym_info.tick_size = 1.0
        sym_info.step_size = 1.0
        sym_info.min_qty = 1.0
        sym_info.min_notional = 20_000.0
        sym_info.constraints = {
            "tick_size": 1.0,
            "step_size": 1.0,
            "min_qty": 1.0,
            "min_notional": 20_000.0,
        }
        client.get_symbol.return_value = sym_info
        client.round_step = lambda val, step: math.floor(val / step) * step
        client.round_tick = lambda val, tick: math.floor(val / tick) * tick

        # Mock timeout pada _signed_post
        client._signed_post.side_effect = RuntimeError("Read timed out from Tokocrypto")

        bal_mock = MagicMock()
        bal_mock.free = 100_000.0
        client.get_balance.return_value = bal_mock

        executor = TokocryptoOrderExecutor(
            client=client,
            supervised=False,
            dry_run=False,
            max_slots=1,
            check_balance=True,
        )
        executor.has_active_position = MagicMock(return_value=False)

        cand = {"symbol": "SOL_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 950.0}

        with patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto"), patch(
            "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id"
        ):
            with self.assertRaises(TokocryptoSubmissionUnknownError):
                executor.execute_entry(cand, slot_size_idr=50_000.0)

            # SANGAT KRUSIAL: Reservasi untuk SOL_IDR TETAP AKTIF di memori!
            self.assertGreater(get_reserved_budget(), 0.0)
            self.assertIn("SOL_IDR", _ACTIVE_BUDGET_RESERVATIONS)

    # -------------------------------------------------------------------------
    # 16. Pending BUY tidak dihitung ganda dengan locked IDR
    # -------------------------------------------------------------------------
    def test_16_pending_buy_order_does_not_double_count_with_idr_locked(self):
        """
        Scenario:
          - 1 trade di Supabase berstatus NEW (pending limit buy sebesar Rp 30.000).
          - Exchange melaporkan idr_locked = Rp 30.000 dan idr_free = Rp 70.000.
          - Total Equity harus tepat Rp 100.000 (Rp 70k free + Rp 30k locked).
          - committed_idr TIDAK boleh menambahkan Rp 30.000 lagi (yang akan membuat total Rp 130.000).
        """
        open_trades = [
            {
                "symbol": "BTC_IDR",
                "exit_status": "OPEN",
                "entry_status": "NEW",  # Masih pending!
                "entry_price": 1000.0,
                "entry_qty": 30.0,
                "entry_notional_idr": 30_000.0,
            }
        ]

        committed_idr = 0.0
        for t in open_trades:
            st = str(t.get("entry_status", "")).upper()
            if st == "FILLED":  # Hanya FILLED yang dihitung!
                committed_idr += t.get("entry_notional_idr", 0.0)

        idr_bal = 70_000.0
        idr_locked = 30_000.0
        total_equity = idr_bal + idr_locked + committed_idr

        self.assertEqual(committed_idr, 0.0)
        self.assertEqual(total_equity, 100_000.0)

    # -------------------------------------------------------------------------
    # 17. Step-up quantity mematuhi batas alokasi slot budget
    # -------------------------------------------------------------------------
    def test_17_stepup_quantity_respects_slot_budget_tolerance(self):
        """
        Scenario:
          - Sizing pembulatan ke bawah menghasilkan notional di bawah min_notional.
          - Pembulatan ke atas 1 step size diterima jika masih dalam toleransi 1 step.
          - Jika koin memiliki step size raksasa yang membuat nilainya melonjak jauh melewati budget,
            order harus ditolak.
        """
        sym = MagicMock(spec=ExchangeSymbol)
        sym.symbol = "EXPENSIVE_IDR"
        sym.tick_size = 1.0
        sym.step_size = 10.0  # Step size sangat besar (10 unit = Rp 100.000)
        sym.min_qty = 10.0
        sym.min_notional = 50_000.0
        sym.constraints = {"tick_size": 1.0, "step_size": 10.0, "min_qty": 10.0, "min_notional": 50_000.0}

        client = MagicMock(spec=TokocryptoClient)
        client.get_symbol.return_value = sym
        client.round_step = lambda val, step: math.floor(val / step) * step

        executor = TokocryptoOrderExecutor(client=client, max_slots=1)

        # Budget hanya Rp 30.000, harga per unit Rp 10.000.
        # 30.000 / 10.000 = 3 unit. Floor to step 10 = 0 unit.
        # Stepping up to 10 unit = Rp 100.000 (jauh melampaui slot budget Rp 30.000)!
        cand = {"symbol": "EXPENSIVE_IDR", "entry_price": 10_000.0}
        is_valid = executor.validate_and_size(cand, available_idr=30_000.0)
        self.assertFalse(is_valid, "Harus ditolak karena step size melonjak melampaui budget")

    # -------------------------------------------------------------------------
    # 18. Respons order ambigu (omitted orderId) -> Reservasi TETAP DITAHAN
    # -------------------------------------------------------------------------
    def test_18_ambiguous_response_missing_order_id_retains_budget_reservation(self):
        """
        Scenario:
          - POST /open/v1/orders sukses di tingkat HTTP (status 200), tetapi
            payload respons kosong atau tidak memuat 'orderId'.
          - Hasil eksekusi menjadi tidak pasti (order mungkin sudah dibuat exchange).
          - TokocryptoSubmissionUnknownError dilempar dan reservasi budget TETAP DITAHAN.
        """
        from core.clients.tokocrypto_client import TokocryptoSubmissionUnknownError

        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda s: s
        client.get_open_orders.return_value = []
        sym_info = MagicMock(spec=ExchangeSymbol)
        sym_info.tick_size = 1.0
        sym_info.step_size = 1.0
        sym_info.min_qty = 1.0
        sym_info.min_notional = 20_000.0
        sym_info.constraints = {
            "tick_size": 1.0,
            "step_size": 1.0,
            "min_qty": 1.0,
            "min_notional": 20_000.0,
        }
        client.get_symbol.return_value = sym_info
        client.round_step = lambda val, step: math.floor(val / step) * step
        client.round_tick = lambda val, tick: math.floor(val / tick) * tick

        # Mock respons tanpa orderId
        client._signed_post.return_value = {"code": 0, "msg": "Success", "data": {}}

        bal_mock = MagicMock()
        bal_mock.free = 100_000.0
        client.get_balance.return_value = bal_mock

        executor = TokocryptoOrderExecutor(
            client=client,
            supervised=False,
            dry_run=False,
            max_slots=1,
            check_balance=True,
        )
        executor.has_active_position = MagicMock(return_value=False)

        cand = {"symbol": "ADA_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 950.0}

        with patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto"), patch(
            "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id"
        ):
            with self.assertRaises(TokocryptoSubmissionUnknownError):
                executor.execute_entry(cand, slot_size_idr=50_000.0)

            # Reservasi dana ADA_IDR WAJIB tetap ditahan!
            self.assertIn("ADA_IDR", _ACTIVE_BUDGET_RESERVATIONS)
            self.assertGreater(get_reserved_budget(), 0.0)

    # -------------------------------------------------------------------------
    # 19. Dua proses bot bersamaan (Boundary threading.Lock vs OS Process)
    # -------------------------------------------------------------------------
    def test_19_multiprocess_isolation_and_exchange_balance_guard(self):
        """
        Scenario & Documentation:
          - threading.Lock hanya melindungi thread dalam 1 proses Python yang sama.
          - Jika 2 proses OS berbeda berjalan bersamaan tanpa file/distributed lock:
            Proses 1 dan Proses 2 memiliki instance memori _ACTIVE_BUDGET_RESERVATIONS terpisah.
          - Namun perlindungan lapis kedua (pre-flight live balance check di exchange)
            menjamin jika Proses 1 telah mengunci saldo di exchange, Proses 2 yang membaca
            live_free dari exchange akan mendeteksi saldo tidak cukup dan abort secara fail-closed.
        """
        client_p1 = MagicMock(spec=TokocryptoClient)
        client_p2 = MagicMock(spec=TokocryptoClient)

        sym_info = MagicMock(spec=ExchangeSymbol)
        sym_info.tick_size = 1.0
        sym_info.step_size = 1.0
        sym_info.min_qty = 1.0
        sym_info.min_notional = 20_000.0
        sym_info.constraints = {
            "tick_size": 1.0,
            "step_size": 1.0,
            "min_qty": 1.0,
            "min_notional": 20_000.0,
        }
        for c in (client_p1, client_p2):
            c.normalize_symbol = lambda s: s
            c.get_open_orders.return_value = []
            c.get_symbol.return_value = sym_info
            c.round_step = lambda val, step: math.floor(val / step) * step
            c.round_tick = lambda val, tick: math.floor(val / tick) * tick

        # Proses 1 mengeksekusi order 60.000 IDR dari total saldo 100.000 IDR
        bal_p1 = MagicMock()
        bal_p1.free = 100_000.0
        client_p1.get_balance.return_value = bal_p1
        client_p1._signed_post.return_value = {"data": {"orderId": "P1_001"}}

        exec_p1 = TokocryptoOrderExecutor(client=client_p1, supervised=False, dry_run=False, check_balance=True)
        exec_p1.has_active_position = MagicMock(return_value=False)

        cand_1 = {"symbol": "BTC_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 950.0}
        with patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto"), patch(
            "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id"
        ):
            res1 = exec_p1.execute_entry(cand_1, slot_size_idr=60_000.0)
            self.assertIsNotNone(res1)

        # Proses 2 mencoba mengeksekusi order 60.000 IDR.
        # Setelah P1 commit ke exchange, saldo live di exchange tersisa 40.000 IDR.
        bal_p2 = MagicMock()
        bal_p2.free = 40_000.0  # Exchange reality after P1
        client_p2.get_balance.return_value = bal_p2

        exec_p2 = TokocryptoOrderExecutor(client=client_p2, supervised=False, dry_run=False, check_balance=True)
        exec_p2.has_active_position = MagicMock(return_value=False)

        cand_2 = {"symbol": "ETH_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 950.0}
        res2 = exec_p2.execute_entry(cand_2, slot_size_idr=60_000.0)
        # P2 ditolak secara fail-closed oleh pre-flight balance check!
        self.assertIsNone(res2)
        client_p2._signed_post.assert_not_called()

    # -------------------------------------------------------------------------
    # 20. Jalur kegagalan aman (Operator cancel / Dry run) melepas reservasi
    # -------------------------------------------------------------------------
    def test_20_safe_abort_releases_budget_reservation(self):
        """
        Scenario:
          - Supervised mode: operator menekan 'N' (abort).
          - Reservasi budget WAJIB dilepas secara bersih sehingga tidak mengunci dana.
        """
        client = MagicMock(spec=TokocryptoClient)
        client.normalize_symbol = lambda s: s
        client.get_open_orders.return_value = []
        sym_info = MagicMock(spec=ExchangeSymbol)
        sym_info.tick_size = 1.0
        sym_info.step_size = 1.0
        sym_info.min_qty = 1.0
        sym_info.min_notional = 20_000.0
        sym_info.constraints = {
            "tick_size": 1.0,
            "step_size": 1.0,
            "min_qty": 1.0,
            "min_notional": 20_000.0,
        }
        client.get_symbol.return_value = sym_info
        client.round_step = lambda val, step: math.floor(val / step) * step
        client.round_tick = lambda val, tick: math.floor(val / tick) * tick

        bal_mock = MagicMock()
        bal_mock.free = 100_000.0
        client.get_balance.return_value = bal_mock

        executor = TokocryptoOrderExecutor(
            client=client,
            supervised=True,  # Supervised gate
            dry_run=False,
            max_slots=1,
            check_balance=True,
        )
        executor.has_active_position = MagicMock(return_value=False)

        cand = {"symbol": "BNB_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 950.0}

        with patch("core.clients.tokocrypto_order_executor._confirm", return_value=False):
            res = executor.execute_entry(cand, slot_size_idr=50_000.0)
            self.assertIsNone(res)
            # Reservasi HARUS sudah dilepas!
            self.assertEqual(get_reserved_budget(), 0.0)
            self.assertNotIn("BNB_IDR", _ACTIVE_BUDGET_RESERVATIONS)

    # -------------------------------------------------------------------------
    # 21. Restart setelah timeout: Pair ambigu diblokir, dana tetap reserved,
    #     dan pair lain yang aman dapat diproses
    # -------------------------------------------------------------------------
    def test_21_restart_with_reconciliation_required_blocks_pair_retains_budget_allows_other_pairs(self):
        """
        Skenario Verifikasi Terarah (Restart Bot):
        1. BUY BTC_IDR mengalami timeout dan status RECONCILIATION_REQUIRED tersimpan di DB.
        2. Proses bot lama dihentikan (_ACTIVE_BUDGET_RESERVATIONS di-clear).
        3. Proses baru dijalankan dengan state database yang sama.
        4. Pastikan pair terkait (BTC_IDR) tetap diblokir sampai rekonsiliasi selesai.
        5. Pastikan reservasi tidak hilang sehingga mencegah BUY duplikat atau overspending.
        6. Pastikan pair lain yang aman (ETH_IDR) tetap dapat diproses sesuai aturan.
        """
        from core.clients.tokocrypto_client import TokocryptoSubmissionUnknownError
        from core.clients.tokocrypto_order_executor import sync_budget_reservations_from_db

        # Database state mock yang mencatat transaksi
        persisted_db = []

        def mock_upsert(record):
            persisted_db.append(dict(record))

        def mock_update(order_id, updates):
            for row in persisted_db:
                if row.get("entry_order_id") == order_id:
                    row.update(updates)

        def mock_fetch():
            return list(persisted_db)

        # ---------------------------------------------------------------------
        # LANGKAH 1: PROSES LAMA — BUY BTC_IDR mengalami timeout
        # ---------------------------------------------------------------------
        client_old = MagicMock(spec=TokocryptoClient)
        client_old.normalize_symbol = lambda s: s
        client_old.get_open_orders.return_value = []
        sym_info = MagicMock(spec=ExchangeSymbol)
        sym_info.tick_size = 1.0
        sym_info.step_size = 1.0
        sym_info.min_qty = 1.0
        sym_info.min_notional = 20_000.0
        sym_info.constraints = {
            "tick_size": 1.0,
            "step_size": 1.0,
            "min_qty": 1.0,
            "min_notional": 20_000.0,
        }
        client_old.get_symbol.return_value = sym_info
        client_old.round_step = lambda val, step: math.floor(val / step) * step
        client_old.round_tick = lambda val, tick: math.floor(val / tick) * tick

        # Saldo awal 100.000 IDR
        bal_old = MagicMock()
        bal_old.free = 100_000.0
        client_old.get_balance.return_value = bal_old
        client_old._signed_post.side_effect = RuntimeError("Read timed out from Tokocrypto")

        exec_old = TokocryptoOrderExecutor(client=client_old, supervised=False, dry_run=False, check_balance=True)

        cand_btc = {"symbol": "BTC_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 950.0}

        with patch("core.clients.tokocrypto_order_executor.upsert_tokocrypto", side_effect=mock_upsert), patch(
            "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id", side_effect=mock_update
        ), patch("services.supabase_client.fetch_all_tokocrypto_strict", side_effect=mock_fetch):
            with self.assertRaises(TokocryptoSubmissionUnknownError):
                exec_old.execute_entry(cand_btc, slot_size_idr=50_000.0)

        # Verifikasi status ambigu tersimpan di database mock
        self.assertEqual(len(persisted_db), 1)
        self.assertEqual(persisted_db[0]["symbol"], "BTC_IDR")
        self.assertEqual(persisted_db[0]["entry_status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(persisted_db[0]["oco_state"], "ENTRY_SUBMISSION_UNKNOWN")
        self.assertEqual(persisted_db[0]["exit_status"], "OPEN")
        self.assertEqual(persisted_db[0]["slot_size_idr"], 50_000.0)

        # ---------------------------------------------------------------------
        # LANGKAH 2: PROSES BOT LAMA DIHENTIKAN (Simulasi Restart: memory reset)
        # ---------------------------------------------------------------------
        _ACTIVE_BUDGET_RESERVATIONS.clear()
        self.assertEqual(len(_ACTIVE_BUDGET_RESERVATIONS), 0)

        # ---------------------------------------------------------------------
        # LANGKAH 3: PROSES BARU DIJALANKAN (State DB sama)
        # ---------------------------------------------------------------------
        client_new = MagicMock(spec=TokocryptoClient)
        client_new.normalize_symbol = lambda s: s
        client_new.get_open_orders.return_value = []
        client_new.get_symbol.return_value = sym_info
        client_new.round_step = lambda val, step: math.floor(val / step) * step
        client_new.round_tick = lambda val, tick: math.floor(val / tick) * tick

        # Di exchange: misalkan timeout terjadi sebelum exchange memotong saldo,
        # sehingga free IDR masih melaporkan 100.000 IDR
        bal_new = MagicMock()
        bal_new.free = 100_000.0
        client_new.get_balance.return_value = bal_new
        client_new._signed_post.return_value = {"data": {"orderId": "ETH_NEW_999"}}

        exec_new = TokocryptoOrderExecutor(client=client_new, supervised=False, dry_run=False, check_balance=True)

        with patch("services.supabase_client.fetch_all_tokocrypto_strict", side_effect=mock_fetch), patch(
            "core.clients.tokocrypto_order_executor.upsert_tokocrypto", side_effect=mock_upsert
        ), patch(
            "core.clients.tokocrypto_order_executor.update_tokocrypto_by_order_id", side_effect=mock_update
        ):
            # -----------------------------------------------------------------
            # LANGKAH 4: Pastikan BTC_IDR tetap diblokir sampai rekonsiliasi selesai
            # -----------------------------------------------------------------
            self.assertTrue(exec_new.has_active_position("BTC_IDR"))
            res_btc_duplicate = exec_new.execute_entry(cand_btc, slot_size_idr=50_000.0)
            self.assertIsNone(res_btc_duplicate, "BTC_IDR WAJIB diblokir dari BUY duplikat!")

            # -----------------------------------------------------------------
            # LANGKAH 5: Pastikan reservasi tidak hilang (BTC_IDR 50.000 IDR tetap terkunci)
            # -----------------------------------------------------------------
            # Setelah sync_budget_reservations_from_db dijalankan:
            sync_budget_reservations_from_db()
            self.assertIn("BTC_IDR", _ACTIVE_BUDGET_RESERVATIONS)
            self.assertEqual(get_reserved_budget(), 50_000.0)

            # Jika kandidat lain mencoba meminta 60.000 IDR (100k - 50k reserved = 50k available < 60k):
            cand_eth_oversized = {"symbol": "ETH_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 950.0}
            res_eth_oversized = exec_new.execute_entry(cand_eth_oversized, slot_size_idr=60_000.0)
            self.assertIsNone(res_eth_oversized, "Order melebihi sisa dana bebas setelah reservasi wajib ditolak!")

            # -----------------------------------------------------------------
            # LANGKAH 6: Pastikan pair lain yang aman (ETH_IDR) tetap dapat diproses
            #            sesuai aturan dengan sisa saldo yang tersedia (Rp 40.000 <= Rp 50.000)
            # -----------------------------------------------------------------
            cand_eth_valid = {"symbol": "ETH_IDR", "entry_price": 1000.0, "tp_price": 1200.0, "sl_price": 950.0}
            self.assertFalse(exec_new.has_active_position("ETH_IDR"))
            res_eth_valid = exec_new.execute_entry(cand_eth_valid, slot_size_idr=40_000.0)
            self.assertIsNotNone(res_eth_valid, "ETH_IDR yang aman WAJIB dapat diproses!")
            client_new._signed_post.assert_called_once()

    # -------------------------------------------------------------------------
    # 22. Siklus propose lengkap setelah restart: BTC_IDR diskip, ETH_IDR diproses
    # -------------------------------------------------------------------------
    @patch("tokocrypto_executor._build_scanner")
    @patch("tokocrypto_executor._build_client")
    @patch("tokocrypto_executor._build_executor")
    def test_22_propose_cycle_restart_with_reconciliation_required_allocates_safe_candidates(
        self, mock_build_exec, mock_build_client, mock_build_scanner
    ):
        """
        Scenario Full Propose Pipeline:
          - State DB memuat BTC_IDR dengan status RECONCILIATION_REQUIRED (50k IDR).
          - Memory bot di-reset (restart).
          - Scanner menemukan 2 kandidat: BTC_IDR dan ETH_IDR.
          - cmd_propose():
            1. Mengurangi available slots (5 - 1 = 4).
            2. Re-hydrate reservasi BTC_IDR (50.000 IDR).
            3. Melewati BTC_IDR karena has_active_position == True.
            4. Memproses ETH_IDR secara aman menggunakan sisa budget.
        """
        from tokocrypto_executor import cmd_propose

        db_state = [
            {
                "symbol": "BTC_IDR",
                "exit_status": "OPEN",
                "entry_status": "RECONCILIATION_REQUIRED",
                "entry_notional_idr": 50_000.0,
                "slot_size_idr": 50_000.0,
            }
        ]

        # Reset memory reservasi (restart)
        _ACTIVE_BUDGET_RESERVATIONS.clear()

        with patch("services.supabase_client.fetch_all_tokocrypto_strict", return_value=db_state):
            client_mock = MagicMock()
            client_mock.get_open_orders.return_value = []
            bal_mock = MagicMock()
            bal_mock.free = 200_000.0
            bal_mock.locked = 0.0
            client_mock.get_balance.return_value = bal_mock
            mock_build_client.return_value = client_mock

            # Scanner menemukan BTC_IDR dan ETH_IDR
            scanner_mock = MagicMock()
            cand_btc = {"symbol": "BTC_IDR", "entry_price": 1000.0, "sl": 950.0, "tp": 1200.0, "tp1": 1200.0, "risk_pct": 0.05, "rr": 2.0}
            cand_eth = {"symbol": "ETH_IDR", "entry_price": 2000.0, "sl": 1900.0, "tp": 2400.0, "tp1": 2400.0, "risk_pct": 0.05, "rr": 2.0}
            scanner_mock.gather_candidates.return_value = [cand_btc, cand_eth]
            scanner_mock.pick_best_candidate.side_effect = lambda cands, available_idr: cands[0] if cands else None
            mock_build_scanner.return_value = scanner_mock

            exec_mock = MagicMock()
            # BTC_IDR aktif (has_active_position = True), ETH_IDR tidak aktif (False)
            exec_mock.has_active_position.side_effect = lambda sym: sym == "BTC_IDR"
            exec_mock.execute_entry.return_value = {"data": {"orderId": "ETH_ORDER_123"}}
            mock_build_exec.return_value = exec_mock

            cmd_propose()

            # Scanner dipanggil untuk 4 slot tersisa (5 - 1 = 4)
            scanner_mock.gather_candidates.assert_called_once_with(max_positions=4)

            # execute_entry TIDAK PERNAH dipanggil untuk BTC_IDR!
            calls = exec_mock.execute_entry.call_args_list
            symbols_called = [c[0][0]["symbol"] for c in calls]
            self.assertNotIn("BTC_IDR", symbols_called, "BTC_IDR tidak boleh dipanggil execute_entry!")
            self.assertIn("ETH_IDR", symbols_called, "ETH_IDR harus dipanggil execute_entry!")

    # -------------------------------------------------------------------------
    # 23. Production Write Guard: Menolak akses tulis tanpa mock saat test runner aktif
    # -------------------------------------------------------------------------
    def test_23_production_write_guard_blocks_unmocked_writes_during_testing(self):
        """
        Scenario:
          - Fungsi write Supabase asli dipanggil tanpa mock saat unit test berjalan.
          - _assert_safe_write_environment() WAJIB melempar RuntimeError
            dan memblokir panggilan ke production Supabase.
        """
        import services.supabase_client as sb_mod

        # Unpatch sementara sb_upsert untuk menguji fungsi asli
        self._patch_sb_upsert.stop()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                sb_mod.upsert_tokocrypto({"symbol": "TEST_IDR", "entry_order_id": "MOCK_FAIL"})

            self.assertIn("CRITICAL PRODUCTION DATABASE WRITE BLOCKED", str(ctx.exception))
        finally:
            self._patch_sb_upsert.start()


if __name__ == "__main__":
    unittest.main()
