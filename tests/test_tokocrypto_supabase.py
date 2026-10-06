import unittest
from unittest.mock import patch, MagicMock


class TestTokoSupabaseHelpers(unittest.TestCase):

    def test_toko_supabase_helpers_exist(self):
        """fetch_all_tokocrypto, upsert_tokocrypto, update_tokocrypto_by_order_id must be importable."""
        from services.supabase_client import (
            fetch_all_tokocrypto,
            upsert_tokocrypto,
            update_tokocrypto_by_order_id,
        )
        self.assertTrue(callable(fetch_all_tokocrypto))
        self.assertTrue(callable(upsert_tokocrypto))
        self.assertTrue(callable(update_tokocrypto_by_order_id))

    def test_fetch_all_tokocrypto_returns_empty_on_missing_table(self):
        """fetch_all_tokocrypto must return [] gracefully when Supabase raises."""
        with patch("services.supabase_client.get_client") as mock_get_client:
            mock_client = MagicMock()
            mock_get_client.return_value = mock_client
            mock_client.table.return_value.select.return_value.order.return_value.execute.side_effect = Exception(
                "relation \"trades_tokocrypto\" does not exist"
            )
            from services.supabase_client import fetch_all_tokocrypto
            result = fetch_all_tokocrypto()
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
