import sys
import os
sys.path.append("/Users/dhanydelio/Swing_Trade")
from dotenv import load_dotenv
load_dotenv()
from core.futures_trade_executor import get_futures_client

try:
    client = get_futures_client()
    orders = client.futures_get_open_orders(symbol="LTCUSDT")
    print("Open orders for LTCUSDT:")
    for o in orders:
        print(f"- {o['type']} {o['side']} {o['positionSide']} | Price: {o['price']} | StopPrice: {o.get('stopPrice')} | Status: {o['status']}")
    if not orders:
        print("TIDAK ADA")
except Exception as e:
    print(f"Error fetching orders: {e}")
