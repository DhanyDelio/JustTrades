from __future__ import annotations

import argparse
import json
import os
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests
import subprocess
from dotenv import load_dotenv

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paper_trade_executor import get_testnet_client, BUDGET_USD, LAB_STARTING_CAPITAL, PER_TRADE_BUDGET
from core.managers.portfolio_manager import PortfolioManager
from services import chart_analyzer as ca
from services.notifier import notifier

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TRADE_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "json" / "trade_log.json"
LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "logs" / "position_listener.log"
PID_PATH = Path(__file__).resolve().parent.parent / "data" / "pid" / "position_listener.pid"
COOLDOWN_MINUTES = 60
ENTRY_PLACED_GRACE_SECONDS = 0.25
_PENDING_ENTRY_NOTIFICATIONS: dict[tuple[str, int], dict] = {}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_trade_log() -> list[dict]:
    if TRADE_LOG_PATH.exists():
        try:
            with open(TRADE_LOG_PATH) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_trade_log(trades: list[dict]) -> None:
    tmp_path = TRADE_LOG_PATH.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(trades, f, indent=2)
    tmp_path.replace(TRADE_LOG_PATH)


def append_log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"[{_now_utc().isoformat()}] {message}\n")


def is_telegram_configured() -> bool:
    return notifier.is_configured


def send_telegram_notification(message: str) -> None:
    if not notifier.is_configured:
        append_log(f"telegram not configured; fallback: {message}")
        return
    try:
        notifier.send(message)
        append_log(f"telegram sent: {message}")
    except Exception as e:
        append_log(f"telegram failed: {e}")


def _get_trade_for_event(trades: list[dict], event: dict) -> dict | None:
    order_list_id = event.get("orderListId")
    if order_list_id is not None:
        for trade in trades:
            if str(trade.get("oco_list_id")) == str(order_list_id):
                return trade

    order_id = event.get("i")
    if order_id is not None:
        for trade in trades:
            if str(trade.get("entry_order_id")) == str(order_id):
                return trade
    return None


def _infer_exit_status(event: dict) -> str | None:
    side = str(event.get("S", "")).upper()
    exec_type = str(event.get("x", "") or event.get("X", "")).upper()
    if exec_type != "TRADE":
        return None
    if side == "SELL":
        return "TP_HIT"
    if side == "BUY":
        return "SL_HIT"
    return None


def _detect_notification_event(trade: dict, event: dict) -> tuple[str | None, dict]:
    status = str(event.get("X", "") or event.get("x", "") or "").upper()
    order_list_id = event.get("orderListId")
    symbol = str(trade.get("symbol") or "").upper()
    direction = str(trade.get("direction") or "").lower()
    fill_price = float(event.get("L", 0) or 0)
    sl = trade.get("sl")
    tp = trade.get("tp1") or trade.get("tp")

    if status == "NEW" and str(trade.get("entry_status", "")).upper() != "FILLED":
        entry_price = trade.get("entry_price")
        zone_price = trade.get("entry_zone_price") or trade.get("entry_price")
        zone_touches = trade.get("zone_touches") or 0
        sl = trade.get("sl")
        tp1 = trade.get("tp1") or trade.get("tp")
        planned_rr = trade.get("planned_rr") or 0
        entry_qty = trade.get("entry_qty") or 0
        position_value = trade.get("entry_notional") or (float(entry_qty) * float(entry_price) if entry_price is not None else 0)
        sl_pct = ((sl - entry_price) / entry_price * 100) if entry_price and sl is not None else None
        tp_pct = ((tp1 - entry_price) / entry_price * 100) if entry_price and tp1 is not None else None
        return "ENTRY_PLACED", {
            "symbol": symbol,
            "direction": direction,
            "message": (
                f"📥 Order placed: {symbol} {direction}\n"
                f"Entry: {ca._fmt_price(entry_price).strip()} (zone: {ca._fmt_price(zone_price).strip()}, {zone_touches}x tested)\n"
                f"SL: {ca._fmt_price(sl).strip()} ({sl_pct:+.2f}% risk)\n"
                f"TP: {ca._fmt_price(tp1).strip()} ({tp_pct:+.2f}% target)\n"
                f"R:R: {planned_rr:.2f}:1\n"
                f"Qty: {entry_qty} (${position_value:,.2f})"
            ),
        }

    if status == "FILLED" and str(trade.get("entry_status", "")).upper() != "FILLED":
        return "ENTRY_FILLED", {
            "symbol": symbol,
            "direction": direction,
            "fill_price": fill_price or trade.get("entry_price"),
            "sl": sl,
            "tp": tp,
            "message": f"✅ Filled: {symbol} {direction} @ {fill_price or trade.get('entry_price')} | SL: {sl} | TP: {tp}",
        }

    if status == "TRADE" and order_list_id is not None:
        exit_status = _infer_exit_status(event)
        if exit_status:
            return "RESOLVED", {
                "symbol": symbol,
                "exit_status": exit_status,
                "pnl_usd": trade.get("realized_pnl_usd", 0),
                "pnl_pct": trade.get("realized_pnl_pct", 0),
                "message": (
                    f"🟢 TP HIT: {symbol} +{trade.get('realized_pnl_usd', 0):.2f} ({trade.get('realized_pnl_pct', 0):.2f}%)"
                    if exit_status == "TP_HIT"
                    else f"🔴 SL HIT: {symbol} {trade.get('realized_pnl_usd', 0):.2f} ({trade.get('realized_pnl_pct', 0):.2f}%)"
                ),
            }

    return None, {}


def _schedule_entry_placed_notification(trade: dict, event: dict, payload: dict) -> None:
    order_id = event.get("i")
    if order_id is None:
        return
    key = (str(trade.get("symbol") or "").upper(), int(order_id))
    _PENDING_ENTRY_NOTIFICATIONS[key] = payload

    def _deliver() -> None:
        if key in _PENDING_ENTRY_NOTIFICATIONS and str(trade.get("entry_status", "")).upper() != "FILLED":
            send_telegram_notification(payload["message"])
            _PENDING_ENTRY_NOTIFICATIONS.pop(key, None)

    threading.Timer(ENTRY_PLACED_GRACE_SECONDS, _deliver).start()


def handle_execution_report(
    event: dict,
    batch_runner: Callable | None = None,
    notifier: Callable | None = None,
    cooldown_minutes: int = COOLDOWN_MINUTES,
    now_dt: datetime | None = None,
) -> dict:
    order_list_id = event.get("orderListId")
    trades = load_trade_log()
    trade = _get_trade_for_event(trades, event)
    if trade is None:
        return {"updated": False, "triggered": False}

    notification_event, notification_payload = _detect_notification_event(trade, event)
    if notification_event == "ENTRY_PLACED":
        trade["entry_status"] = "NEW"
        trade["entry_price"] = float(event.get("p", 0) or trade.get("entry_price", 0) or 0)
        trade["entry_qty"] = float(event.get("q", trade.get("entry_qty", 0)) or trade.get("entry_qty", 0))
        save_trade_log(trades)
        _schedule_entry_placed_notification(trade, event, notification_payload)
        return {"updated": True, "triggered": False, "event": notification_event, "message": notification_payload["message"]}

    if notification_event == "ENTRY_FILLED":
        trade["entry_status"] = "FILLED"
        trade["entry_fill_price"] = float(event.get("L", 0) or trade.get("entry_price", 0) or 0)
        trade["entry_fill_time"] = int(time.time() * 1000)
        key = (str(trade.get("symbol") or "").upper(), int(event.get("i", 0) or 0))
        _PENDING_ENTRY_NOTIFICATIONS.pop(key, None)
        save_trade_log(trades)
        send_telegram_notification(notification_payload["message"])
        return {"updated": True, "triggered": False, "event": notification_event, "message": notification_payload["message"]}

    # ── Resolve detection (TP/SL) is intentionally disabled. ──────────────
    # Binance testnet does not reliably send executionReport for OCO completion
    # via WebSocket. Resolve detection is handled by --check-positions (manual).
    # Listener is kept running only for real-time ENTRY_FILLED notifications.
    return {"updated": False, "triggered": False}


class PositionListener:
    def __init__(self, client, batch_runner=None, notifier=None):
        self.client = client
        self.batch_runner = batch_runner
        self.notifier = notifier
        self._stop = threading.Event()
        self._thread = None
        self._ws = None
        self.api_key = os.getenv("BINANCE_TESTNET_API_KEY")
        self.api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._listen_loop()
            except Exception as exc:
                append_log(f"listener loop error: {exc}")
                time.sleep(5)

    def _listen_loop(self) -> None:
        import websocket
        from urllib.parse import urlencode
        import hmac
        import hashlib

        ws_url = "wss://ws-api.testnet.binance.vision/ws-api/v3"

        def on_open(ws):
            append_log("WebSocket connected, sending subscription request")
            timestamp = int(time.time() * 1000)
            params = {"apiKey": self.api_key, "timestamp": timestamp}
            query_string = urlencode(params)
            signature = hmac.new(self.api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()

            payload = {
                "id": "position_listener_sub",
                "method": "userDataStream.subscribe.signature",
                "params": {
                    "apiKey": self.api_key,
                    "signature": signature,
                    "timestamp": timestamp
                }
            }
            ws.send(json.dumps(payload))

        ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=self._on_message,
            on_error=lambda ws, err: append_log(f"ws error: {err}"),
            on_close=lambda ws, code, msg: append_log(f"ws closed: {code} {msg}"),
        )
        self._ws = ws
        ws.run_forever(ping_interval=20, ping_timeout=10)
        self._ws = None
        if not self._stop.is_set():
            append_log("websocket disconnected; reconnecting")
            time.sleep(3)

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
        except Exception:
            return

        # Support subscription response message
        if isinstance(data, dict) and data.get("id") == "position_listener_sub":
            status = data.get("status")
            if status == 200:
                append_log("User Data Stream subscription active.")
            else:
                error_msg = data.get("error", {}).get("msg", "Unknown error")
                append_log(f"Subscription failed (status {status}): {error_msg}")
            return

        # Support both wrapped and direct formats
        payload = data.get("event") if isinstance(data, dict) and "event" in data else data
        if not isinstance(payload, dict):
            return

        if payload.get("e") == "executionReport":
            handle_execution_report(
                payload,
                batch_runner=self.batch_runner,
                notifier=self.notifier
            )


def build_startup_banner(client) -> str:
    trades = load_trade_log()
    open_trades = [t for t in trades if t.get("exit_status") == "OPEN" and t.get("oco_list_id")]
    telegram_status = "active" if is_telegram_configured() else "log-only fallback"
    return (
        "\n=== Position Listener ===\n"
        f"Connected: {'yes' if client else 'no'}\n"
        f"Tracked OCO positions: {len(open_trades)}\n"
        f"Telegram notifications: {telegram_status}\n"
        "Listening for execution reports..."
    )


def _check_singleton() -> None:
    """Prevent starting a second instance if one is already running.

    Reads the PID file, checks whether that process is still alive, and
    aborts with a clear error message if so.  Stale PID files (where the
    recorded process no longer exists) are silently cleaned up.
    """
    if not PID_PATH.exists():
        return
    try:
        old_pid = int(PID_PATH.read_text().strip())
    except (ValueError, OSError):
        PID_PATH.unlink(missing_ok=True)
        return
    try:
        os.kill(old_pid, 0)  # signal 0 = existence check, doesn't kill
    except ProcessNotFoundError:
        # Stale PID file from a crashed/killed process
        PID_PATH.unlink(missing_ok=True)
        return
    except PermissionError:
        pass  # process exists but owned by another user – still a conflict
    # If we get here, the process is alive
    print(
        f"❌ Another position_listener is already running (PID {old_pid}).\n"
        f"   Kill it first:  kill {old_pid}\n"
        f"   Or remove stale PID file:  rm {PID_PATH}"
    )
    raise SystemExit(1)


def _write_pid() -> None:
    """Write the current PID to the PID file."""
    PID_PATH.write_text(str(os.getpid()))


def _remove_pid() -> None:
    """Remove the PID file if it belongs to this process."""
    try:
        if PID_PATH.exists() and int(PID_PATH.read_text().strip()) == os.getpid():
            PID_PATH.unlink(missing_ok=True)
    except (ValueError, OSError):
        PID_PATH.unlink(missing_ok=True)


def run_auto_propose(trade) -> None:
    """Trigger a new scan and auto-propose batch when a trade is resolved.

    Runs in a background thread to prevent blocking the WebSocket thread.
    """
    def _run():
        append_log(f"Auto-triggering new batch proposals since {trade.get('symbol')} resolved...")
        try:
            # Run paper_trade_executor.py --propose-all
            # This is non-interactive because should_auto_confirm_batch(is_lab_batch=True) returns True.
            script_path = Path(__file__).resolve().parent / "paper_trade_executor.py"
            res = subprocess.run(
                ["python3", str(script_path), "--propose-all"],
                capture_output=True,
                text=True,
                timeout=300
            )
            # Log the output summary
            lines = res.stdout.splitlines()
            summary = "\n".join(lines[-15:]) if len(lines) > 15 else res.stdout
            append_log(f"Auto-propose output summary:\n{summary}")
            if res.stderr:
                append_log(f"Auto-propose Stderr: {res.stderr[:500]}")
        except Exception as e:
            append_log(f"Auto-propose failed to execute: {e}")

    threading.Thread(target=_run, daemon=True).start()


# Python < 3.3 compat alias – os module doesn't always expose ProcessLookupError
try:
    ProcessNotFoundError = ProcessLookupError
except NameError:
    ProcessNotFoundError = OSError


def cmd_status() -> None:
    """Print whether the automated system is currently ON or OFF."""
    if not PID_PATH.exists():
        print("🔴  Automated system is OFF")
        return

    try:
        pid = int(PID_PATH.read_text().strip())
    except (ValueError, OSError):
        print("🔴  Automated system is OFF")
        return

    try:
        os.kill(pid, 0)  # existence check only
    except (ProcessLookupError, ProcessNotFoundError):
        print("🔴  Automated system is OFF  (stale PID file)")
        return
    except PermissionError:
        pass  # process exists, owned by another user — still counts as running

    # Process is alive — also show last log line as health hint
    last_log = ""
    try:
        lines = LOG_PATH.read_text().splitlines()
        last_log = next((l for l in reversed(lines) if l.strip()), "")
    except Exception:
        pass

    print(f"🟢  Automated system is ON  (PID {pid})")
    if last_log:
        print(f"    Last activity: {last_log}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Listen for Binance testnet execution events")
    parser.add_argument("--test-notify", action="store_true", help="Send a test Telegram notification and exit")
    parser.add_argument("--status", action="store_true", help="Check if the automated system is running")
    args = parser.parse_args()

    if args.status:
        cmd_status()
        return

    if args.test_notify:
        send_telegram_notification("Position listener test notification: started successfully")
        print("Test notification attempted.")
        return

    # ── Singleton guard ─────────────────────────────────────────────
    _check_singleton()
    _write_pid()
    import atexit
    atexit.register(_remove_pid)

    try:
        client = get_testnet_client()
        client.ping()
    except Exception as exc:
        _remove_pid()
        print(f"❌ Testnet connection failed: {exc}")
        raise SystemExit(1)

    print("✅ Testnet connected")
    print(build_startup_banner(client))

    listener = PositionListener(client, batch_runner=None)

    def _handle_shutdown(signum, frame):
        print("\nStopping listener gracefully...")
        listener.stop()
        _remove_pid()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    listener.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        listener.stop()
        _remove_pid()
        print("Listener stopped cleanly.")


if __name__ == "__main__":
    main()
