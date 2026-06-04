from __future__ import annotations

import json
import math
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
KST = ZoneInfo("Asia/Seoul")


DEFAULT_CANDIDATES = {
    "us": [
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "AVGO",
        "TSLA",
        "LLY",
        "JPM",
        "V",
        "UNH",
        "XOM",
        "COST",
        "MA",
        "NFLX",
    ],
    "kr": [
        "005930.KS",
        "000660.KS",
        "035420.KS",
        "051910.KS",
        "005380.KS",
        "068270.KS",
        "105560.KS",
        "055550.KS",
        "035720.KS",
        "012330.KS",
        "028260.KS",
        "096770.KS",
    ],
}

SYMBOL_NAMES = {
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "AMZN": "Amazon",
    "META": "Meta",
    "GOOGL": "Alphabet",
    "AVGO": "Broadcom",
    "TSLA": "Tesla",
    "LLY": "Eli Lilly",
    "JPM": "JPMorgan",
    "V": "Visa",
    "UNH": "UnitedHealth",
    "XOM": "Exxon Mobil",
    "COST": "Costco",
    "MA": "Mastercard",
    "NFLX": "Netflix",
    "005930.KS": "삼성전자",
    "000660.KS": "SK하이닉스",
    "035420.KS": "NAVER",
    "051910.KS": "LG화학",
    "005380.KS": "현대차",
    "068270.KS": "셀트리온",
    "105560.KS": "KB금융",
    "055550.KS": "신한지주",
    "035720.KS": "카카오",
    "012330.KS": "현대모비스",
    "028260.KS": "삼성물산",
    "096770.KS": "SK이노베이션",
}


@dataclass(frozen=True)
class MarketPoint:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper()


def get_symbol_name(symbol: str) -> str:
    symbol = normalize_symbol(symbol)
    return SYMBOL_NAMES.get(symbol, symbol)


def validate_symbols(symbols: list[str]) -> list[str]:
    clean = []
    seen = set()
    for symbol in symbols:
        normalized = normalize_symbol(symbol)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        clean.append(normalized)
    if not clean:
        raise ValueError("최소 1개 이상의 종목 티커가 필요합니다.")
    if len(clean) > 8:
        raise ValueError("종목은 최대 8개까지 운용할 수 있습니다.")
    return clean


def _date_to_unix(date_text: str) -> int:
    dt = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _cache_path(symbol: str, start: str, end: str) -> Path:
    safe = symbol.replace("^", "").replace(".", "_").replace("/", "_")
    return CACHE_DIR / f"{safe}_{start}_{end}.json"


def get_price_history(symbol: str, start: str, end: str, use_cache: bool = True) -> list[dict]:
    symbol = normalize_symbol(symbol)
    cache_path = _cache_path(symbol, start, end)
    if use_cache and cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    period1 = _date_to_unix(start)
    period2 = _date_to_unix(end) + 86400
    encoded = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
        f"?period1={period1}&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 StockRLSimulator/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"{symbol} 시세 조회 실패: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        if _is_certificate_error(exc):
            # Some bundled Windows/MSYS Python builds lack a usable CA bundle.
            # The endpoint is fixed to Yahoo Finance chart JSON, so retry only
            # this public data request when local certificate verification fails.
            with urllib.request.urlopen(request, timeout=15, context=ssl._create_unverified_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        else:
            raise ValueError(f"{symbol} 시세 조회 실패: 네트워크 연결을 확인하세요.") from exc

    chart = payload.get("chart", {})
    error = chart.get("error")
    if error:
        raise ValueError(f"{symbol} 시세 조회 실패: {error.get('description', '알 수 없는 오류')}")

    results = chart.get("result") or []
    if not results:
        raise ValueError(f"{symbol} 시세 데이터가 없습니다. 티커와 기간을 확인하세요.")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adjclose = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    rows: list[dict] = []

    for idx, ts in enumerate(timestamps):
        close_values = adjclose if idx < len(adjclose) and adjclose[idx] is not None else quote.get("close", [])
        close = close_values[idx] if idx < len(close_values) else None
        open_price = _value_at(quote.get("open"), idx)
        high = _value_at(quote.get("high"), idx)
        low = _value_at(quote.get("low"), idx)
        volume = _value_at(quote.get("volume"), idx, default=0)
        if close is None or open_price is None or high is None or low is None:
            continue
        if not all(math.isfinite(float(v)) for v in [open_price, high, low, close]):
            continue
        point = MarketPoint(
            date=datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            open=round(float(open_price), 4),
            high=round(float(high), 4),
            low=round(float(low), 4),
            close=round(float(close), 4),
            volume=int(volume or 0),
        )
        rows.append(point.to_dict())

    if len(rows) < 35:
        raise ValueError(f"{symbol} 시세가 부족합니다. 더 긴 기간을 선택하세요.")

    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    return rows


def _value_at(values, idx: int, default=None):
    if not values or idx >= len(values):
        return default
    return values[idx]


def _is_certificate_error(exc: urllib.error.URLError) -> bool:
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc)


def get_intraday_points(symbol: str, range_text: str = "5d", interval: str = "1m") -> list[dict]:
    symbol = normalize_symbol(symbol)
    encoded = urllib.parse.quote(symbol, safe="")
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
        f"?range={urllib.parse.quote(range_text)}&interval={urllib.parse.quote(interval)}"
        "&includePrePost=false"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 StockRLSimulator/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"{symbol} 시세 조회 실패: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        if _is_certificate_error(exc):
            with urllib.request.urlopen(request, timeout=15, context=ssl._create_unverified_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
        else:
            raise ValueError(f"{symbol} 시세 조회 실패: 네트워크 연결을 확인하세요.") from exc

    chart = payload.get("chart", {})
    error = chart.get("error")
    if error:
        raise ValueError(f"{symbol} 시세 조회 실패: {error.get('description', '알 수 없는 오류')}")
    results = chart.get("result") or []
    if not results:
        raise ValueError(f"{symbol} 현재가 데이터가 없습니다.")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    rows = []
    for idx, ts in enumerate(timestamps):
        close = _value_at(quote.get("close"), idx)
        if close is None:
            continue
        rows.append(
            {
                "time": datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
                "timestamp": int(ts),
                "price": round(float(close), 4),
                "volume": int(_value_at(quote.get("volume"), idx, default=0) or 0),
            }
        )
    if not rows:
        raise ValueError(f"{symbol} 현재가 데이터가 비어 있습니다.")
    return rows


def get_latest_quotes(symbols: list[str]) -> dict[str, dict]:
    clean = validate_symbols(symbols)
    quotes = {}
    errors = {}
    for symbol in clean:
        try:
            points = get_intraday_points(symbol)
            latest = points[-1]
            prior_index = max(0, len(points) - 21)
            prior = points[prior_index]
            change = 0.0 if prior["price"] == 0 else latest["price"] / prior["price"] - 1
            quotes[symbol] = {
                **latest,
                "symbol": symbol,
                "name": get_symbol_name(symbol),
                "change": round(change, 6),
                "checkedAt": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
                "points": points[-80:],
            }
            time.sleep(0.05)
        except ValueError as exc:
            errors[symbol] = str(exc)
    if not quotes:
        message = "; ".join(errors.values()) if errors else "현재가를 가져오지 못했습니다."
        raise ValueError(message)
    if errors:
        raise ValueError("; ".join(errors.values()))
    return quotes


def fetch_many(symbols: list[str], start: str, end: str) -> dict[str, list[dict]]:
    clean = validate_symbols(symbols)
    histories = {}
    errors = {}
    for symbol in clean:
        try:
            histories[symbol] = get_price_history(symbol, start, end)
            time.sleep(0.1)
        except ValueError as exc:
            errors[symbol] = str(exc)
    if not histories:
        message = "; ".join(errors.values()) if errors else "시세를 가져오지 못했습니다."
        raise ValueError(message)
    if errors:
        raise ValueError("; ".join(errors.values()))
    return histories


def suggest_symbols(market: str, start: str, end: str, limit: int = 8) -> list[dict]:
    if not end:
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not start:
        start = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    market = (market or "mixed").lower()
    if market == "us":
        candidates = DEFAULT_CANDIDATES["us"]
    elif market == "kr":
        candidates = DEFAULT_CANDIDATES["kr"]
    else:
        candidates = DEFAULT_CANDIDATES["us"][:10] + DEFAULT_CANDIDATES["kr"][:8]

    scored = []
    for symbol in candidates:
        try:
            rows = get_price_history(symbol, start, end)
            score = _score_history(rows)
            scored.append({"symbol": symbol, "name": get_symbol_name(symbol), **score})
        except ValueError:
            continue

    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[: max(1, min(8, int(limit or 8)))]


def _score_history(rows: list[dict]) -> dict:
    closes = [row["close"] for row in rows]
    volumes = [row["volume"] for row in rows[-30:]]
    recent_return = closes[-1] / closes[max(0, len(closes) - 31)] - 1
    medium_return = closes[-1] / closes[max(0, len(closes) - 91)] - 1
    returns = [(closes[i] / closes[i - 1]) - 1 for i in range(1, len(closes))]
    recent_returns = returns[-30:] or returns
    avg = sum(recent_returns) / len(recent_returns)
    variance = sum((r - avg) ** 2 for r in recent_returns) / len(recent_returns)
    volatility = math.sqrt(variance)
    liquidity = math.log10(max(1, sum(volumes) / max(1, len(volumes))))
    score = recent_return * 1.8 + medium_return * 1.2 + liquidity * 0.03 - volatility * 1.5
    return {
        "score": round(score, 4),
        "recentReturn": round(recent_return, 4),
        "mediumReturn": round(medium_return, 4),
        "volatility": round(volatility, 4),
        "avgVolume": int(sum(volumes) / max(1, len(volumes))),
    }
