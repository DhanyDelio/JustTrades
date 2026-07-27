import sys
sys.path.append("/Users/dhanydelio/Swing_Trade")
from dotenv import load_dotenv
load_dotenv()
from core.futures_trade_executor import get_futures_client

try:
    client = get_futures_client()
    algo = client.futures_get_algo_order(symbol="LTCUSDT", algoId="1000000136377503")
    print(f"TP Algo status: {algo.get('algoStatus') or algo.get('orderStatus')}")
except Exception as e:
    print(f"TP Algo error: {e}")

try:
    algo_sl = client.futures_get_algo_order(symbol="LTCUSDT", algoId="1000000136377507")
    print(f"SL Algo status: {algo_sl.get('algoStatus') or algo_sl.get('orderStatus')}")
except Exception as e:
    print(f"SL Algo error: {e}")
