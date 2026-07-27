import sys
sys.path.append("/Users/dhanydelio/Swing_Trade")
from dotenv import load_dotenv
load_dotenv()
from core.futures_trade_executor import load_futures_log

trades = load_futures_log()
for trade in trades:
    if trade.get("symbol") == "LTCUSDT":
        print(f"LTCUSDT exit_orders_placed: {trade.get('exit_orders_placed')}")
        print(f"LTCUSDT tp_order_id: {trade.get('tp_order_id')}")
        print(f"LTCUSDT sl_order_id: {trade.get('sl_order_id')}")
        print(f"LTCUSDT tp_algo_id: {trade.get('tp_algo_id')}")
        print(f"LTCUSDT sl_algo_id: {trade.get('sl_algo_id')}")
