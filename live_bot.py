import time
import subprocess
import sys
from datetime import datetime

# Interval set to 1 hour (3600 seconds) to match the previous GitHub Actions cron
POLL_INTERVAL = 3600 

def run_jobs():
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n=======================================================", flush=True)
    print(f"[{timestamp}] Executing Live Trading & Shadow Scoring Jobs", flush=True)
    print(f"=======================================================\n", flush=True)
    
    print("[1/4] Checking Spot Positions...", flush=True)
    subprocess.run([sys.executable, "paper_trade_executor.py", "--check-positions"])
    
    print("\n[2/4] Checking Futures Positions...", flush=True)
    subprocess.run([sys.executable, "futures_trade_executor.py", "--check-positions"])
    
    print("\n[3/4] Proposing New Spot Trades (Shadow Scoring)...", flush=True)
    subprocess.run([sys.executable, "paper_trade_executor.py", "--propose-all", "--yes"])
    
    print("\n[4/4] Proposing New Futures Trades...", flush=True)
    subprocess.run([sys.executable, "futures_trade_executor.py", "--propose"])
    
    print("\n--- All Jobs Completed. Sleeping for 1 hour. ---", flush=True)

def main():
    print("Starting Live Trading Bot in Production Mode (24/7 Polling Loop)", flush=True)
    
    while True:
        try:
            run_jobs()
        except Exception as e:
            print(f"Error executing jobs: {e}", flush=True)
            
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
