from __future__ import annotations

import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from app.services.backtest import run_backtest, train_q_learning
from app.services.live_trading import (
    ensure_live_session,
    get_live_public_state,
    reset_live_session,
    run_live_step,
    update_live_settings,
)
from app.services.market_data import fetch_many, get_price_history, suggest_symbols, validate_symbols


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "app" / "static"
RUNS_DIR = ROOT / "data" / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format, *args):
        print("[server]", format % args)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json({"ok": True, "name": "Stock RL Trader"})
            return
        if parsed.path == "/api/live/state":
            self._json(get_live_public_state())
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/market-data":
                symbols = validate_symbols(payload.get("symbols", []))
                start = payload.get("start")
                end = payload.get("end")
                data = {symbol: get_price_history(symbol, start, end) for symbol in symbols}
                self._json({"symbols": symbols, "data": data})
                return
            if parsed.path == "/api/live/reset":
                self._json(reset_live_session(payload))
                return
            if parsed.path == "/api/live/settings":
                self._json(update_live_settings(payload))
                return
            if parsed.path == "/api/live/step":
                self._json(run_live_step())
                return
            if parsed.path == "/api/live/state":
                self._json(get_live_public_state())
                return
            if parsed.path == "/api/symbols/suggest":
                items = suggest_symbols(
                    market=payload.get("market", "mixed"),
                    start=payload.get("start"),
                    end=payload.get("end"),
                    limit=payload.get("limit", 8),
                )
                self._json({"items": items})
                return
            if parsed.path == "/api/backtest":
                result = _run_simulation(payload)
                self._json(result)
                return
            if parsed.path == "/api/train":
                symbols = validate_symbols(payload.get("symbols", []))
                histories = fetch_many(symbols, payload.get("start"), payload.get("end"))
                result = train_q_learning(
                    histories=histories,
                    initial_cash=float(payload.get("initialCash", 10_000_000)),
                    profile=payload.get("profile", "balanced"),
                    episodes=int(payload.get("episodes", 40)),
                )
                self._json(result)
                return
            if parsed.path == "/api/advice":
                result = _build_advice(payload)
                self._json(result)
                return
            self._json({"error": "알 수 없는 API입니다."}, status=404)
        except Exception as exc:
            self._json({"error": str(exc)}, status=400)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def _json(self, data, status=200):
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _run_simulation(payload):
    symbols = validate_symbols(payload.get("symbols", []))
    histories = fetch_many(symbols, payload.get("start"), payload.get("end"))
    return run_backtest(
        histories=histories,
        initial_cash=float(payload.get("initialCash", 10_000_000)),
        strategy=payload.get("strategy", "momentum"),
        profile=payload.get("profile", "balanced"),
        fee_rate=float(payload.get("feeRate", 0.0005)),
        slippage_rate=float(payload.get("slippageRate", 0.0005)),
    )


def _build_advice(payload):
    symbols = validate_symbols(payload.get("symbols", []))
    histories = fetch_many(symbols, payload.get("start"), payload.get("end"))
    profile = payload.get("profile", "balanced")
    initial_cash = float(payload.get("initialCash", 10_000_000))
    strategies = ["momentum", "buy_hold", "random"]
    results = [
        run_backtest(histories, initial_cash=initial_cash, strategy=strategy, profile=profile)
        for strategy in strategies
    ]
    results.sort(key=lambda item: item["metrics"]["adviceScore"], reverse=True)
    best = results[0]
    warnings = []
    if best["metrics"]["maxDrawdown"] < -0.2:
        warnings.append("과거 시뮬레이션에서 최대 낙폭이 큽니다.")
    if best["metrics"]["tradeCount"] > 250:
        warnings.append("거래 빈도가 높아 수수료와 슬리피지에 민감합니다.")
    if not warnings:
        warnings.append("시뮬레이션 결과는 미래 수익을 보장하지 않습니다.")
    return {
        "profile": profile,
        "bestStrategy": best["strategy"],
        "summary": {
            "totalReturn": best["metrics"]["totalReturn"],
            "maxDrawdown": best["metrics"]["maxDrawdown"],
            "winRate": best["metrics"]["winRate"],
            "tradeCount": best["metrics"]["tradeCount"],
            "adviceScore": best["metrics"]["adviceScore"],
        },
        "warnings": warnings,
        "rankedResults": [
            {"strategy": item["strategy"], "metrics": item["metrics"]} for item in results
        ],
    }


def run(host="127.0.0.1", port=8000):
    ensure_live_session()
    _start_live_worker()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Stock RL Trader running at http://{host}:{port}")
    print("Background paper trader is running while this server stays on.")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


_worker_started = False


def _start_live_worker():
    global _worker_started
    if _worker_started:
        return
    _worker_started = True
    thread = threading.Thread(target=_live_worker_loop, name="live-paper-trader", daemon=True)
    thread.start()


def _live_worker_loop():
    while True:
        interval = 300
        try:
            result = run_live_step(auto=True)
            interval = int(result.get("autoIntervalSeconds") or 300)
        except Exception as exc:
            print(f"[live-worker] {exc}")
        time.sleep(max(15, min(3600, interval)))
