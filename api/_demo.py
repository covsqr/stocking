from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
AUTO_INTERVAL_SECONDS = 300
MIN_TRADE_INTERVAL_SECONDS = 1800
RISK_STOP_COOLDOWN_SECONDS = 7200
SYMBOL_ROTATION_SECONDS = 21600

SYMBOL_NAMES = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "AVGO": "Broadcom",
    "LLY": "Eli Lilly",
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "035420.KS": "NAVER",
    "005380.KS": "현대차",
    "105560.KS": "KB금융",
    "055550.KS": "신한지주",
}

DEFAULT_SYMBOLS = ["005930.KS", "000660.KS", "035420.KS"]

FALLBACK_PRICES = {
    "005930.KS": 356750,
    "000660.KS": 2298000,
    "035420.KS": 270750,
    "005380.KS": 285000,
    "105560.KS": 107000,
    "055550.KS": 65500,
    "AAPL": 196.45,
    "MSFT": 486.12,
    "NVDA": 141.72,
    "AVGO": 265.2,
    "LLY": 825.3,
}

STATIC_CANDIDATES = [
    {"symbol": "000660.KS", "score": 4.429, "recentReturn": 1.042, "mediumReturn": 1.14, "volatility": 0.031},
    {"symbol": "005380.KS", "score": 1.2732, "recentReturn": 0.318, "mediumReturn": 0.41, "volatility": 0.022},
    {"symbol": "AVGO", "score": 1.1272, "recentReturn": 0.1916, "mediumReturn": 0.35, "volatility": 0.026},
    {"symbol": "AAPL", "score": 0.8151, "recentReturn": 0.1667, "mediumReturn": 0.21, "volatility": 0.018},
    {"symbol": "035420.KS", "score": 0.732, "recentReturn": 0.116, "mediumReturn": 0.18, "volatility": 0.02},
    {"symbol": "105560.KS", "score": 0.612, "recentReturn": 0.091, "mediumReturn": 0.14, "volatility": 0.015},
    {"symbol": "055550.KS", "score": 0.522, "recentReturn": 0.073, "mediumReturn": 0.11, "volatility": 0.014},
    {"symbol": "LLY", "score": 0.481, "recentReturn": 0.058, "mediumReturn": 0.09, "volatility": 0.017},
]


def symbol_name(symbol: str) -> str:
    return SYMBOL_NAMES.get(symbol, symbol)


def read_json(handler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length).decode("utf-8") if length else "{}"
    return json.loads(raw or "{}")


def send_json(handler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def candidates(limit: int = 8) -> list[dict]:
    return [
        {"name": symbol_name(item["symbol"]), "avgVolume": 0, **item}
        for item in STATIC_CANDIDATES[: max(1, min(8, int(limit or 8)))]
    ]


def state_payload(symbols: list[str] | None = None, profile: str = "balanced") -> dict:
    now = datetime.now(KST)
    symbols = symbols or DEFAULT_SYMBOLS
    symbols = [symbol.strip().upper() for symbol in symbols if symbol.strip()][:8] or DEFAULT_SYMBOLS
    quotes = {}
    positions = []
    holdings = {}
    equity = []
    initial_cash = 10_000_000
    cash = initial_cash

    for idx, symbol in enumerate(symbols):
        price = float(FALLBACK_PRICES.get(symbol, 100.0 + idx * 10))
        change = 0.012 - idx * 0.006
        points = []
        for minute in range(40):
            t = now - timedelta(minutes=39 - minute)
            p = price * (1 + (minute - 20) * 0.0008 + idx * 0.0003)
            points.append(
                {
                    "time": t.strftime("%Y-%m-%d %H:%M:%S KST"),
                    "timestamp": int(t.timestamp()),
                    "price": round(p, 4),
                    "volume": 0,
                }
            )
        quotes[symbol] = {
            "symbol": symbol,
            "name": symbol_name(symbol),
            "time": now.strftime("%Y-%m-%d %H:%M:%S KST"),
            "timestamp": int(now.timestamp()),
            "checkedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
            "price": round(price, 4),
            "change": round(change, 6),
            "volume": 0,
            "points": points,
        }
        shares = 0.0
        avg_cost = 0.0
        if idx < 3:
            value = initial_cash * (0.22 - idx * 0.04)
            shares = value / price
            avg_cost = price * (0.985 + idx * 0.01)
            cash -= value
        market_value = shares * price
        cost = shares * avg_cost
        pnl = market_value - cost
        holdings[symbol] = {"shares": shares, "avgCost": avg_cost}
        positions.append(
            {
                "symbol": symbol,
                "name": symbol_name(symbol),
                "shares": round(shares, 6),
                "avgCost": round(avg_cost, 4),
                "price": round(price, 4),
                "value": round(market_value, 2),
                "cost": round(cost, 2),
                "weight": round(market_value / initial_cash, 6),
                "unrealizedPnl": round(pnl, 2),
                "unrealizedPnlRate": round(pnl / cost, 6) if cost else 0,
                "points": points,
                "targeted": symbol in {item["symbol"] for item in STATIC_CANDIDATES[:8]},
            }
        )

    final_value = cash + sum(item["value"] for item in positions)
    for idx in range(40):
        t = now - timedelta(minutes=39 - idx)
        equity.append(
            {
                "time": t.strftime("%Y-%m-%d %H:%M:%S KST"),
                "quoteTime": t.strftime("%Y-%m-%d %H:%M:%S KST"),
                "value": round(final_value * (0.992 + idx * 0.0002), 2),
                "cash": round(cash, 2),
                "return": round(final_value / initial_cash - 1, 6),
                "policyReturn": round(final_value / initial_cash - 1, 6),
                "reward": 0,
                "action": 0,
                "actionLabel": "보유",
                "state": "vercel:preview",
                "weights": {},
            }
        )

    return {
        "symbols": symbols,
        "symbolNames": {symbol: symbol_name(symbol) for symbol in symbols},
        "profile": profile,
        "initialCash": initial_cash,
        "cash": round(cash, 2),
        "finalValue": round(final_value, 2),
        "lastCheckedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "lastQuoteTime": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "lastStatus": "Vercel 미리보기 모드입니다. 자동매매 루프와 장부 저장은 로컬 서버에서 실행하세요.",
        "lastDecision": {
            "checkedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
            "quoteTime": now.strftime("%Y-%m-%d %H:%M:%S KST"),
            "action": "미리보기",
            "tradeCount": 0,
            "reward": 0,
            "status": "Vercel 미리보기 모드: 장기 실행 자동매매는 비활성화",
            "nextCheckAt": (now + timedelta(seconds=AUTO_INTERVAL_SECONDS)).strftime("%Y-%m-%d %H:%M:%S KST"),
        },
        "autoIntervalSeconds": AUTO_INTERVAL_SECONDS,
        "minTradeIntervalSeconds": MIN_TRADE_INTERVAL_SECONDS,
        "riskStopCooldownSeconds": RISK_STOP_COOLDOWN_SECONDS,
        "symbolRotationSeconds": SYMBOL_ROTATION_SECONDS,
        "policyVersion": 3,
        "rotation": {
            "enabled": True,
            "market": "mixed",
            "lastAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
            "nextAt": (now + timedelta(seconds=SYMBOL_ROTATION_SECONDS)).strftime("%Y-%m-%d %H:%M:%S KST"),
            "candidates": candidates(8),
            "targetUniverse": [item["symbol"] for item in STATIC_CANDIDATES[:8]],
        },
        "metrics": {
            "totalReturn": round(final_value / initial_cash - 1, 6),
            "policyReturn": round(final_value / initial_cash - 1, 6),
            "policyDrawdown": 0,
            "realizedPnl": 0,
            "totalFees": 0,
            "winRate": 0,
            "tradeCount": 0,
            "step": 0,
            "policySize": 0,
            "epsilon": 0,
            "riskStopRemainingSeconds": 0,
        },
        "holdings": holdings,
        "positions": positions,
        "quotes": quotes,
        "equityCurve": equity,
        "trades": [],
        "messages": [
            "Vercel 미리보기 모드입니다.",
            "실제 5분 자동 판단과 JSON 장부 저장은 로컬 서버에서 실행됩니다.",
        ],
    }
