from __future__ import annotations

import json
import random
import threading
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.market_data import get_latest_quotes, get_symbol_name, suggest_symbols, validate_symbols


ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "data" / "runs" / "live_session.json"
STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

KST = ZoneInfo("Asia/Seoul")
NY = ZoneInfo("America/New_York")
FRESH_QUOTE_SECONDS = 2 * 60 * 60
AUTO_INTERVAL_SECONDS = 5 * 60
MIN_TRADE_INTERVAL_SECONDS = 30 * 60
MIN_POSITION_HOLD_SECONDS = 2 * 60 * 60
REENTRY_COOLDOWN_SECONDS = 2 * 60 * 60
RISK_STOP_COOLDOWN_SECONDS = 2 * 60 * 60
SYMBOL_ROTATION_SECONDS = 6 * 60 * 60
POLICY_VERSION = 4
EPSILON_DECAY = 0.997
EPSILON_FLOOR = 0.02
POLICY_DRAWDOWN_LIMIT = -0.05
PROFIT_EXIT_BUFFER = 0.003
STOP_LOSS_LIMIT = -0.025

STATE_LOCK = threading.RLock()
STEP_LOCK = threading.Lock()

DEFAULT_SESSION = {
    "symbols": ["AAPL", "MSFT", "NVDA"],
    "initialCash": 10_000_000,
    "profile": "balanced",
    "autoIntervalSeconds": AUTO_INTERVAL_SECONDS,
}

PROFILE_SETTINGS = {
    "stable": {"max_weight": 0.25, "trade_threshold": 0.04, "risk_penalty": 1.2, "epsilon": 0.06},
    "balanced": {"max_weight": 0.35, "trade_threshold": 0.025, "risk_penalty": 0.8, "epsilon": 0.08},
    "aggressive": {"max_weight": 0.55, "trade_threshold": 0.015, "risk_penalty": 0.45, "epsilon": 0.10},
}

ACTION_LABELS = {
    0: "보유",
    1: "현금 대기",
    2: "상위 1종목",
    3: "상위 3종목",
    4: "전체 동일비중",
}


def ensure_live_session() -> dict:
    with STATE_LOCK:
        if not STATE_PATH.exists():
            return reset_live_session(DEFAULT_SESSION)
        state = _migrate(_load())
        _save(state)
        return _public_state(state)


def reset_live_session(payload: dict) -> dict:
    with STATE_LOCK:
        state = _new_state(payload)
        _save(state)
        return _public_state(state)


def update_live_settings(payload: dict) -> dict:
    with STATE_LOCK:
        state = _migrate(_load()) if STATE_PATH.exists() else _new_state(DEFAULT_SESSION)
        symbols = validate_symbols(payload.get("symbols") or state["symbols"])
        profile = payload.get("profile", state["profile"])
        state["symbols"] = symbols
        state["targetUniverse"] = symbols
        state["rotationCandidates"] = []
        state["profile"] = profile if profile in PROFILE_SETTINGS else state["profile"]
        state["autoMarket"] = payload.get("market", state.get("autoMarket", "mixed"))
        state["epsilon"] = min(float(state.get("epsilon", _initial_epsilon(state))), _initial_epsilon(state))
        state["autoIntervalSeconds"] = AUTO_INTERVAL_SECONDS
        for symbol in symbols:
            state["holdings"].setdefault(symbol, {"shares": 0.0, "avgCost": 0.0})
        for symbol in list(state["holdings"].keys()):
            if symbol not in symbols and state["holdings"][symbol].get("shares", 0.0) == 0:
                del state["holdings"][symbol]
        _add_message(state, f"{_now_text()} 설정 저장: {', '.join(symbols)}")
        _save(state)
        return _public_state(state)


def get_live_state() -> dict:
    with STATE_LOCK:
        return _migrate(_load()) if STATE_PATH.exists() else _new_state(DEFAULT_SESSION)


def get_live_public_state() -> dict:
    return _public_state(get_live_state())


def run_live_step(auto: bool = False) -> dict:
    del auto
    with STEP_LOCK:
        with STATE_LOCK:
            state = _migrate(_load()) if STATE_PATH.exists() else _new_state(DEFAULT_SESSION)

            checked_at = _now_text()
            _maybe_rotate_symbols(state, checked_at)
            quotes = get_latest_quotes(state["symbols"])
            quote_timestamp = max(quote["timestamp"] for quote in quotes.values())
            quote_time = _latest_time(quotes)
            state["lastCheckedAt"] = checked_at
            state["lastQuoteTime"] = quote_time
            state["lastQuotes"] = {symbol: _quote_public(quote) for symbol, quote in quotes.items()}
            tradable_symbols = _tradable_symbols(quotes)
            tradable_timestamp = max((quotes[symbol]["timestamp"] for symbol in tradable_symbols), default=0)

            value_before = _portfolio_value(state, quotes)

            if not tradable_symbols:
                status = "거래 가능한 장중 종목이 없어 자동매매 판단을 보류했습니다."
                state["lastStatus"] = status
                state["lastDecision"] = _decision_summary(checked_at, quote_time, "보류", 0, 0.0, status)
                _record_status_once(state, quote_timestamp, f"{checked_at} 확인: 장외 종목만 감지, 신규 체결 없음")
                _save(state)
                return _public_state(state)

            if state.get("lastMarketTimestamp") == tradable_timestamp:
                status = "이전 판단 이후 새 시세가 없어 대기 중입니다."
                state["lastStatus"] = status
                state["lastDecision"] = _decision_summary(checked_at, quote_time, "대기", 0, 0.0, status)
                _record_status_once(state, tradable_timestamp, f"{checked_at} 확인: 장중 종목 새 시세 없음")
                _save(state)
                return _public_state(state)

            _release_expired_risk_stop(state, value_before)

            market_reward = (value_before - state["lastValue"]) / max(state["lastValue"], 1e-9)
            cost_penalty = float(state.get("lastTradeCostRate", 0.0))
            drawdown = _policy_drawdown(state, value_before)
            drawdown_penalty = _drawdown_penalty(state, drawdown)
            reward = market_reward - cost_penalty - drawdown_penalty
            current_state = _state_key(state, quotes, value_before)

            if state["lastState"] is not None and state["lastAction"] is not None:
                _update_q_table(state, reward, current_state)

            action = _choose_action(state, current_state)
            trade_remaining = _trade_cooldown_remaining(state)
            risk_remaining = _risk_stop_remaining(state)
            forced_reason = ""

            if risk_remaining > 0:
                action = 1
                forced_reason = f"손실 제한 휴식 {max(1, (risk_remaining + 59) // 60)}분 남음"
            elif drawdown <= POLICY_DRAWDOWN_LIMIT:
                action = 1
                state["riskStopUntil"] = _future_text(RISK_STOP_COOLDOWN_SECONDS)
                state["lastRiskStopAt"] = checked_at
                forced_reason = f"정책 손실 제한 작동: {RISK_STOP_COOLDOWN_SECONDS // 3600}시간 휴식"
            elif trade_remaining > 0:
                action = 0
                forced_reason = f"거래 쿨다운 {max(1, (trade_remaining + 59) // 60)}분 남음"

            target_weights = _target_weights_for_action(state, quotes, action, value_before, tradable_symbols)
            trades = _rebalance(state, quotes, target_weights, value_before, checked_at, quote_time, tradable_symbols)
            value_after = _portfolio_value(state, quotes)
            trade_cost_rate = max(0.0, (value_before - value_after) / max(value_before, 1e-9)) if trades else 0.0
            if trades:
                state["lastTradeAt"] = checked_at

            action_label = _action_label(action, trades)
            status_bits = [f"새 시세 반영 완료: {action_label}", f"체결 {len(trades)}건"]
            status_bits.append(f"장중 거래 가능 {len(tradable_symbols)}/{len(state['symbols'])}개")
            if state.get("lastTradeGuards"):
                status_bits.append(f"보호 규칙으로 {len(state['lastTradeGuards'])}건 보류")
            if forced_reason:
                status_bits.append(forced_reason)
            status = ", ".join(status_bits)

            state["step"] += 1
            state["lastState"] = current_state
            state["lastAction"] = action
            state["lastValue"] = value_after
            state["lastTradeCostRate"] = trade_cost_rate
            state["lastMarketTimestamp"] = tradable_timestamp
            state["lastStatus"] = status
            state["peakValue"] = max(float(state.get("peakValue", value_after)), value_after)
            state["epsilon"] = max(EPSILON_FLOOR, float(state.get("epsilon", _initial_epsilon(state))) * EPSILON_DECAY)
            state["lastDecision"] = _decision_summary(checked_at, quote_time, action_label, len(trades), reward, status)
            state["equityCurve"].append(
                {
                    "time": checked_at,
                    "quoteTime": quote_time,
                    "value": round(value_after, 2),
                    "cash": round(state["cash"], 2),
                    "return": round(value_after / state["initialCash"] - 1, 6),
                    "policyReturn": round(_policy_return(state, value_after), 6),
                    "reward": round(reward, 6),
                    "marketReward": round(market_reward, 6),
                    "costPenalty": round(cost_penalty, 6),
                    "tradeCostRate": round(trade_cost_rate, 6),
                    "action": action,
                    "actionLabel": action_label,
                    "state": current_state,
                    "weights": _current_weights(state, quotes, value_after),
                }
            )
            _add_message(
                state,
                (
                    f"{checked_at} 판단: 시세 기준 {quote_time}, {action_label}, "
                    f"reward {reward:.5f}, 체결 {len(trades)}건"
                    + (f", {forced_reason}" if forced_reason else "")
                ),
            )
            _save(state)
            return _public_state(state)


def _new_state(payload: dict) -> dict:
    symbols = validate_symbols(payload.get("symbols") or DEFAULT_SESSION["symbols"])
    initial_cash = float(payload.get("initialCash", DEFAULT_SESSION["initialCash"]))
    profile = payload.get("profile", DEFAULT_SESSION["profile"])
    if profile not in PROFILE_SETTINGS:
        profile = DEFAULT_SESSION["profile"]
    status = "자동 모의매매 세션이 준비되었습니다."
    return {
        "symbols": symbols,
        "profile": profile,
        "initialCash": initial_cash,
        "cash": initial_cash,
        "holdings": {symbol: {"shares": 0.0, "avgCost": 0.0} for symbol in symbols},
        "trades": [],
        "equityCurve": [],
        "qTable": {},
        "lastState": None,
        "lastAction": None,
        "lastValue": initial_cash,
        "step": 0,
        "lastQuotes": {},
        "lastMarketTimestamp": None,
        "lastQuoteTime": None,
        "lastCheckedAt": None,
        "lastStatus": status,
        "autoIntervalSeconds": AUTO_INTERVAL_SECONDS,
        "lastDecision": _decision_summary(None, None, "대기", 0, 0.0, status),
        "messages": [],
        "policyVersion": POLICY_VERSION,
        "policyArchives": [],
        "policyBaselineValue": initial_cash,
        "peakValue": initial_cash,
        "lastTradeAt": None,
        "lastSymbolBuyAt": {},
        "lastSymbolSellAt": {},
        "lastTradeGuards": [],
        "riskStopUntil": None,
        "lastRiskStopAt": None,
        "lastTradeCostRate": 0.0,
        "epsilon": PROFILE_SETTINGS[profile]["epsilon"],
        "autoRotateSymbols": True,
        "autoMarket": "mixed",
        "rotationLastAt": None,
        "rotationCandidates": [],
        "targetUniverse": symbols,
    }


def _migrate(state: dict) -> dict:
    state.setdefault("symbols", DEFAULT_SESSION["symbols"])
    state.setdefault("profile", DEFAULT_SESSION["profile"])
    if state["profile"] not in PROFILE_SETTINGS:
        state["profile"] = DEFAULT_SESSION["profile"]
    state.setdefault("initialCash", DEFAULT_SESSION["initialCash"])
    state.setdefault("cash", state["initialCash"])
    state.setdefault("holdings", {})
    for symbol in state["symbols"]:
        state["holdings"].setdefault(symbol, {"shares": 0.0, "avgCost": 0.0})
    state.setdefault("trades", [])
    state.setdefault("equityCurve", [])
    state.setdefault("qTable", {})
    state.setdefault("lastState", None)
    state.setdefault("lastAction", None)
    state.setdefault("lastValue", state["initialCash"])
    state.setdefault("step", 0)
    state.setdefault("lastQuotes", {})
    state.setdefault("lastMarketTimestamp", None)
    state.setdefault("lastQuoteTime", None)
    state.setdefault("lastCheckedAt", None)
    state.setdefault("lastStatus", "자동 모의매매가 실행 중입니다.")
    state.setdefault("messages", [])
    state.setdefault("policyArchives", [])

    current_value = _portfolio_value(state, state.get("lastQuotes", {}))
    if state.get("policyVersion") != POLICY_VERSION:
        old_q_table = state.get("qTable") or {}
        if old_q_table:
            state["policyArchives"].append(
                {
                    "version": state.get("policyVersion", 1),
                    "archivedAt": _now_text(),
                    "reason": "손실 제한 영구 잠금 방지 정책으로 교체",
                    "qTable": old_q_table,
                }
            )
            state["policyArchives"] = state["policyArchives"][-3:]
        state["policyVersion"] = POLICY_VERSION
        state["qTable"] = {}
        state["lastState"] = None
        state["lastAction"] = None
        state["lastValue"] = current_value
        state["policyBaselineValue"] = current_value
        state["peakValue"] = current_value
        state["riskStopUntil"] = None
        state["lastRiskStopAt"] = None
        state["lastTradeCostRate"] = 0.0
        state["epsilon"] = PROFILE_SETTINGS[state["profile"]]["epsilon"]
        state["lastStatus"] = "안전 정책 v3을 적용했습니다. 손실 제한은 휴식 후 재시작됩니다."
        _add_message(state, f"{_now_text()} 안전 정책 v3 적용: 손실 제한 영구 잠금 해제")

    state.setdefault("policyVersion", POLICY_VERSION)
    state.setdefault("policyBaselineValue", current_value)
    state.setdefault("peakValue", current_value)
    state.setdefault("lastTradeAt", None)
    state.setdefault("lastSymbolBuyAt", {})
    state.setdefault("lastSymbolSellAt", {})
    _rebuild_missing_symbol_timestamps(state)
    state.setdefault("lastTradeGuards", [])
    state.setdefault("riskStopUntil", None)
    state.setdefault("lastRiskStopAt", None)
    state.setdefault("lastTradeCostRate", 0.0)
    state.setdefault("epsilon", PROFILE_SETTINGS[state["profile"]]["epsilon"])
    state.setdefault("autoRotateSymbols", True)
    state.setdefault("autoMarket", "mixed")
    state.setdefault("rotationLastAt", None)
    state.setdefault("rotationCandidates", [])
    state.setdefault("targetUniverse", state["symbols"])
    state["autoIntervalSeconds"] = AUTO_INTERVAL_SECONDS
    state["messages"] = state["messages"][:50]
    state.setdefault(
        "lastDecision",
        _decision_summary(
            state.get("lastCheckedAt"),
            state.get("lastQuoteTime"),
            "대기",
            0,
            0.0,
            state.get("lastStatus", "자동 모의매매가 실행 중입니다."),
        ),
    )
    return state


def _load() -> dict:
    with STATE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save(state: dict) -> None:
    temp_path = STATE_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    temp_path.replace(STATE_PATH)


def _rebuild_missing_symbol_timestamps(state: dict) -> None:
    buy_times = state.setdefault("lastSymbolBuyAt", {})
    sell_times = state.setdefault("lastSymbolSellAt", {})
    if buy_times and sell_times:
        return
    for trade in state.get("trades", []):
        symbol = trade.get("symbol")
        trade_time = trade.get("time")
        if not symbol or not trade_time:
            continue
        if trade.get("side") == "BUY":
            buy_times[symbol] = trade_time
        elif trade.get("side") == "SELL":
            sell_times[symbol] = trade_time
    for symbol, holding in state.get("holdings", {}).items():
        if float(holding.get("shares", 0.0)) > 0 and not buy_times.get(symbol):
            buy_times[symbol] = state.get("lastTradeAt") or _now_text()


def _maybe_rotate_symbols(state: dict, checked_at: str) -> None:
    if not state.get("autoRotateSymbols", True):
        return
    last_at = _parse_kst_text(state.get("rotationLastAt"))
    if last_at and (datetime.now(KST) - last_at).total_seconds() < SYMBOL_ROTATION_SECONDS:
        return
    try:
        candidates = suggest_symbols(state.get("autoMarket", "mixed"), start=None, end=None, limit=8)
    except Exception as exc:
        _add_message(state, f"{checked_at} 자동 종목 교체 보류: 후보 조회 실패 ({exc})")
        state["rotationLastAt"] = checked_at
        return

    ranked = [item["symbol"] for item in candidates]
    if not ranked:
        _add_message(state, f"{checked_at} 자동 종목 교체 보류: 유효 후보 없음")
        state["rotationLastAt"] = checked_at
        return

    held = [
        symbol
        for symbol, holding in state["holdings"].items()
        if float(holding.get("shares", 0.0)) > 0
    ]
    selected = []
    for symbol in held + ranked:
        if symbol not in selected:
            selected.append(symbol)
        if len(selected) >= 8:
            break

    state["symbols"] = validate_symbols(selected)
    state["targetUniverse"] = ranked[:8]
    state["rotationCandidates"] = candidates
    state["rotationLastAt"] = checked_at
    for symbol in state["symbols"]:
        state["holdings"].setdefault(symbol, {"shares": 0.0, "avgCost": 0.0})
    for symbol in list(state["holdings"].keys()):
        if symbol not in state["symbols"] and float(state["holdings"][symbol].get("shares", 0.0)) == 0:
            del state["holdings"][symbol]

    labels = ", ".join(f"{get_symbol_name(item['symbol'])}({item['symbol']})" for item in candidates[:5])
    _add_message(state, f"{checked_at} 자동 종목 후보 갱신: {labels}")


def _portfolio_value(state: dict, quotes: dict[str, dict]) -> float:
    total = float(state["cash"])
    for symbol, holding in state["holdings"].items():
        quote = quotes.get(symbol) or state.get("lastQuotes", {}).get(symbol)
        if quote:
            total += float(holding.get("shares", 0.0)) * float(quote["price"])
    return total


def _current_weights(state: dict, quotes: dict[str, dict], total_value: float) -> dict[str, float]:
    weights = {}
    if total_value <= 0:
        return {symbol: 0.0 for symbol in state["symbols"]}
    for symbol in state["symbols"]:
        shares = float(state["holdings"][symbol].get("shares", 0.0))
        price = float(quotes[symbol]["price"])
        weights[symbol] = round((shares * price) / total_value, 4)
    return weights


def _state_key(state: dict, quotes: dict[str, dict], total_value: float) -> str:
    changes = [quote["change"] for quote in quotes.values()]
    avg = sum(changes) / max(1, len(changes))
    trend = "up" if avg > 0.003 else "down" if avg < -0.003 else "flat"
    spread = "wide" if max(changes) - min(changes) > 0.015 else "normal"
    invested = 1 - state["cash"] / max(total_value, 1e-9)
    exposure = "high" if invested > 0.75 else "mid" if invested > 0.35 else "low"
    return f"{trend}:{spread}:{exposure}"


def _choose_action(state: dict, key: str) -> int:
    q_table = state["qTable"]
    q_table.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0.0])
    if random.random() < float(state.get("epsilon", _initial_epsilon(state))):
        return random.randint(0, 4)
    values = q_table[key]
    best = max(values)
    candidates = [idx for idx, value in enumerate(values) if value == best]
    if key.endswith(":low") and len(candidates) == len(values):
        candidates = [0, 2, 3, 4]
    return random.choice(candidates)


def _update_q_table(state: dict, reward: float, next_state: str) -> None:
    q_table = state["qTable"]
    old_state = state["lastState"]
    old_action = int(state["lastAction"])
    q_table.setdefault(old_state, [0.0, 0.0, 0.0, 0.0, 0.0])
    q_table.setdefault(next_state, [0.0, 0.0, 0.0, 0.0, 0.0])
    alpha = 0.2
    gamma = 0.85
    old = q_table[old_state][old_action]
    q_table[old_state][old_action] = round(old + alpha * (reward + gamma * max(q_table[next_state]) - old), 8)


def _target_weights_for_action(
    state: dict,
    quotes: dict[str, dict],
    action: int,
    total_value: float,
    tradable_symbols: set[str] | None = None,
) -> dict[str, float]:
    max_weight = PROFILE_SETTINGS[state["profile"]]["max_weight"]
    tradable_symbols = set(quotes.keys()) if tradable_symbols is None else set(tradable_symbols)
    investable = set(state.get("targetUniverse") or state["symbols"]) & tradable_symbols
    ranked = sorted(
        [quote for quote in quotes.values() if quote["symbol"] in investable],
        key=lambda item: item["change"],
        reverse=True,
    )
    if action in (0, 1):
        return _current_weights(state, quotes, total_value)
    if action == 2:
        raw = {item["symbol"]: 1.0 for item in ranked[:1] if item["change"] > -0.003}
    elif action == 3:
        raw = {item["symbol"]: max(0.001, item["change"] + 0.01) for item in ranked[:3] if item["change"] > -0.006}
    else:
        raw = {symbol: 1.0 for symbol in state["symbols"] if symbol in investable}
    return _normalize(raw, max_weight)


def _normalize(raw: dict[str, float], max_weight: float) -> dict[str, float]:
    if not raw:
        return {}
    total = sum(max(0.0, value) for value in raw.values())
    if total <= 0:
        return {}
    weights = {symbol: min(max_weight, max(0.0, value) / total) for symbol, value in raw.items()}
    scale = min(1.0, 1.0 / max(sum(weights.values()), 1e-9))
    return {symbol: value * scale for symbol, value in weights.items()}


def _rebalance(
    state: dict,
    quotes: dict[str, dict],
    target_weights: dict[str, float],
    total_value: float,
    checked_at: str,
    quote_time: str,
    tradable_symbols: set[str] | None = None,
) -> list[dict]:
    settings = PROFILE_SETTINGS[state["profile"]]
    current = _current_weights(state, quotes, total_value)
    trades = []
    state["lastTradeGuards"] = []
    fee_rate = 0.0005
    slippage_rate = 0.0005
    tradable_symbols = set(quotes.keys()) if tradable_symbols is None else set(tradable_symbols)
    for symbol in state["symbols"]:
        if symbol not in tradable_symbols:
            continue
        target = target_weights.get(symbol, 0.0)
        diff_value = (target - current.get(symbol, 0.0)) * total_value
        if abs(diff_value) / max(total_value, 1e-9) < settings["trade_threshold"]:
            continue
        price = float(quotes[symbol]["price"])
        holding = state["holdings"][symbol]
        if diff_value > 0:
            if not _can_buy_symbol(state, symbol):
                state["lastTradeGuards"].append({"symbol": symbol, "side": "BUY", "reason": "reentry_cooldown"})
                continue
            execution_price = price * (1 + slippage_rate)
            trade_value = min(diff_value, state["cash"] / (1 + fee_rate))
            if trade_value <= 0:
                continue
            shares = trade_value / execution_price
            fee = trade_value * fee_rate
            previous_value = holding["shares"] * holding["avgCost"]
            holding["shares"] += shares
            holding["avgCost"] = (previous_value + trade_value) / max(holding["shares"], 1e-9)
            state["cash"] -= trade_value + fee
            side = "BUY"
            realized = 0.0
            state.setdefault("lastSymbolBuyAt", {})[symbol] = checked_at
        else:
            execution_price = price * (1 - slippage_rate)
            shares = min(holding["shares"], abs(diff_value) / execution_price)
            if shares <= 0:
                continue
            trade_value = shares * execution_price
            fee = trade_value * fee_rate
            realized = (execution_price - holding["avgCost"]) * shares - fee
            if not _can_sell_symbol(state, symbol, checked_at, realized, shares):
                state["lastTradeGuards"].append({"symbol": symbol, "side": "SELL", "reason": "hold_or_loss_guard"})
                continue
            holding["shares"] = max(0.0, holding["shares"] - shares)
            if holding["shares"] == 0:
                holding["avgCost"] = 0.0
            state["cash"] += trade_value - fee
            side = "SELL"
            state.setdefault("lastSymbolSellAt", {})[symbol] = checked_at
        trade = {
            "time": checked_at,
            "quoteTime": quote_time,
            "symbol": symbol,
            "side": side,
            "shares": round(shares, 6),
            "price": round(execution_price, 4),
            "marketPrice": round(price, 4),
            "value": round(trade_value, 2),
            "fee": round(fee, 2),
            "realizedPnl": round(realized, 2),
        }
        state["trades"].append(trade)
        trades.append(trade)
    return trades


def _can_buy_symbol(state: dict, symbol: str) -> bool:
    last_sell = _parse_kst_text(state.get("lastSymbolSellAt", {}).get(symbol))
    if not last_sell:
        return True
    elapsed = (datetime.now(KST) - last_sell).total_seconds()
    return elapsed >= REENTRY_COOLDOWN_SECONDS


def _can_sell_symbol(state: dict, symbol: str, checked_at: str, realized: float, shares: float) -> bool:
    holding = state["holdings"].get(symbol, {})
    avg_cost = float(holding.get("avgCost", 0.0))
    cost = avg_cost * max(float(shares), 0.0)
    if cost <= 0:
        return True

    realized_rate = realized / cost
    if realized_rate <= STOP_LOSS_LIMIT:
        return True

    buy_time = _parse_kst_text(state.get("lastSymbolBuyAt", {}).get(symbol))
    checked_time = _parse_kst_text(checked_at) or datetime.now(KST)
    if buy_time and (checked_time - buy_time).total_seconds() < MIN_POSITION_HOLD_SECONDS:
        return False

    return realized_rate >= PROFIT_EXIT_BUFFER


def _initial_epsilon(state: dict) -> float:
    return PROFILE_SETTINGS[state["profile"]]["epsilon"]


def _policy_return(state: dict, value: float) -> float:
    return value / max(float(state.get("policyBaselineValue", value)), 1e-9) - 1


def _policy_drawdown(state: dict, value: float) -> float:
    return value / max(float(state.get("peakValue", value)), 1e-9) - 1


def _drawdown_penalty(state: dict, drawdown: float) -> float:
    penalty = max(0.0, -drawdown - 0.02)
    return penalty * PROFILE_SETTINGS[state["profile"]]["risk_penalty"] * 0.05


def _trade_cooldown_remaining(state: dict) -> int:
    value = _parse_kst_text(state.get("lastTradeAt"))
    if not value:
        return 0
    elapsed = (datetime.now(KST) - value).total_seconds()
    return max(0, int(MIN_TRADE_INTERVAL_SECONDS - elapsed))


def _risk_stop_remaining(state: dict) -> int:
    value = _parse_kst_text(state.get("riskStopUntil"))
    if not value:
        return 0
    return max(0, int((value - datetime.now(KST)).total_seconds()))


def _release_expired_risk_stop(state: dict, value: float) -> None:
    if state.get("riskStopUntil") and _risk_stop_remaining(state) == 0:
        state["riskStopUntil"] = None
        state["peakValue"] = value
        state["policyBaselineValue"] = value
        state["lastState"] = None
        state["lastAction"] = None
        state["lastValue"] = value
        state["lastTradeCostRate"] = 0.0
        _add_message(state, f"{_now_text()} 손실 제한 휴식 종료: 현재 자산을 새 정책 기준으로 재시작")


def _parse_kst_text(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S KST").replace(tzinfo=KST)
    except ValueError:
        return None


def _latest_time(quotes: dict[str, dict]) -> str:
    latest = max(quotes.values(), key=lambda quote: quote["timestamp"])
    return latest["time"]


def _quote_public(quote: dict) -> dict:
    market_open = _is_symbol_tradable(quote["symbol"], quote)
    return {
        "symbol": quote["symbol"],
        "name": quote.get("name") or get_symbol_name(quote["symbol"]),
        "market": _symbol_market(quote["symbol"]),
        "marketOpen": market_open,
        "time": quote["time"],
        "timestamp": quote["timestamp"],
        "checkedAt": quote.get("checkedAt"),
        "price": quote["price"],
        "change": quote["change"],
        "volume": quote["volume"],
        "points": quote.get("points", []),
    }


def _tradable_symbols(quotes: dict[str, dict]) -> set[str]:
    return {symbol for symbol, quote in quotes.items() if _is_symbol_tradable(symbol, quote)}


def _is_symbol_tradable(symbol: str, quote: dict) -> bool:
    timestamp = int(quote.get("timestamp") or 0)
    if not _quotes_are_fresh(timestamp):
        return False
    quote_dt = datetime.fromtimestamp(timestamp, tz=KST)
    market = _symbol_market(symbol)
    if market == "KR":
        return quote_dt.weekday() < 5 and time(9, 0) <= quote_dt.time() <= time(15, 30)
    if market == "US":
        ny_dt = quote_dt.astimezone(NY)
        return ny_dt.weekday() < 5 and time(9, 30) <= ny_dt.time() <= time(16, 0)
    return False


def _symbol_market(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith(".KS") or upper.endswith(".KQ"):
        return "KR"
    return "US"


def _positions_public(state: dict, quotes: dict[str, dict], total_value: float) -> list[dict]:
    positions = []
    targets = set(state.get("targetUniverse") or state["symbols"])
    for symbol in state["symbols"]:
        holding = state["holdings"].get(symbol, {"shares": 0.0, "avgCost": 0.0})
        quote = quotes.get(symbol) or state.get("lastQuotes", {}).get(symbol) or {}
        shares = float(holding.get("shares", 0.0))
        avg_cost = float(holding.get("avgCost", 0.0))
        price = float(quote.get("price", 0.0) or 0.0)
        value = shares * price
        cost = shares * avg_cost
        pnl = value - cost if shares and avg_cost else 0.0
        pnl_rate = pnl / cost if cost else 0.0
        positions.append(
            {
                "symbol": symbol,
                "name": quote.get("name") or get_symbol_name(symbol),
                "shares": round(shares, 6),
                "avgCost": round(avg_cost, 4),
                "price": round(price, 4),
                "value": round(value, 2),
                "cost": round(cost, 2),
                "weight": round(value / max(total_value, 1e-9), 6),
                "unrealizedPnl": round(pnl, 2),
                "unrealizedPnlRate": round(pnl_rate, 6),
                "points": quote.get("points", [])[-40:],
                "targeted": symbol in targets,
                "market": quote.get("market") or _symbol_market(symbol),
                "marketOpen": bool(quote.get("marketOpen", False)),
            }
        )
    positions.sort(key=lambda item: item["value"], reverse=True)
    return positions


def _quotes_are_fresh(quote_timestamp: int) -> bool:
    now_ts = int(datetime.now(KST).timestamp())
    return 0 <= now_ts - int(quote_timestamp) <= FRESH_QUOTE_SECONDS


def _now_text() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def _next_check_text() -> str:
    return _future_text(AUTO_INTERVAL_SECONDS)


def _rotation_next_text(state: dict) -> str | None:
    last_at = _parse_kst_text(state.get("rotationLastAt"))
    if not last_at:
        return None
    return (last_at + timedelta(seconds=SYMBOL_ROTATION_SECONDS)).strftime("%Y-%m-%d %H:%M:%S KST")


def _future_text(seconds: int) -> str:
    return (datetime.now(KST) + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S KST")


def _decision_summary(checked_at, quote_time, action, trade_count, reward, status) -> dict:
    return {
        "checkedAt": checked_at,
        "quoteTime": quote_time,
        "action": action,
        "tradeCount": int(trade_count or 0),
        "reward": round(float(reward or 0.0), 6),
        "status": status,
        "nextCheckAt": _next_check_text(),
    }


def _action_label(action: int, trades: list[dict]) -> str:
    if not trades:
        return ACTION_LABELS.get(action, "보유")
    buys = sum(1 for trade in trades if trade["side"] == "BUY")
    sells = sum(1 for trade in trades if trade["side"] == "SELL")
    if buys and sells:
        return "비중 조정"
    if buys:
        return "매수"
    if sells:
        return "매도"
    return ACTION_LABELS.get(action, "보유")


def _record_status_once(state: dict, quote_timestamp: int, message: str) -> None:
    key = f"status:{quote_timestamp}:{state['lastStatus']}"
    if state.get("lastStatusKey") == key:
        return
    state["lastStatusKey"] = key
    _add_message(state, message)


def _add_message(state: dict, message: str) -> None:
    state.setdefault("messages", [])
    state["messages"].insert(0, message)
    state["messages"] = state["messages"][:50]


def _public_state(state: dict) -> dict:
    quotes = state.get("lastQuotes", {})
    value = _portfolio_value(state, quotes) if quotes else state["cash"]
    realized = sum(trade.get("realizedPnl", 0.0) for trade in state["trades"])
    fees = sum(trade.get("fee", 0.0) for trade in state["trades"])
    wins = sum(1 for trade in state["trades"] if trade.get("side") == "SELL" and trade.get("realizedPnl", 0.0) > 0)
    sells = sum(1 for trade in state["trades"] if trade.get("side") == "SELL")
    return {
        "symbols": state["symbols"],
        "symbolNames": {symbol: get_symbol_name(symbol) for symbol in state["symbols"]},
        "profile": state["profile"],
        "initialCash": round(state["initialCash"], 2),
        "cash": round(state["cash"], 2),
        "finalValue": round(value, 2),
        "lastCheckedAt": state.get("lastCheckedAt"),
        "lastQuoteTime": state.get("lastQuoteTime"),
        "lastStatus": state.get("lastStatus"),
        "lastDecision": state.get("lastDecision"),
        "autoIntervalSeconds": AUTO_INTERVAL_SECONDS,
        "minTradeIntervalSeconds": MIN_TRADE_INTERVAL_SECONDS,
        "minPositionHoldSeconds": MIN_POSITION_HOLD_SECONDS,
        "reentryCooldownSeconds": REENTRY_COOLDOWN_SECONDS,
        "riskStopCooldownSeconds": RISK_STOP_COOLDOWN_SECONDS,
        "symbolRotationSeconds": SYMBOL_ROTATION_SECONDS,
        "policyVersion": state.get("policyVersion", POLICY_VERSION),
        "rotation": {
            "enabled": bool(state.get("autoRotateSymbols", True)),
            "market": state.get("autoMarket", "mixed"),
            "lastAt": state.get("rotationLastAt"),
            "nextAt": _rotation_next_text(state),
            "candidates": state.get("rotationCandidates", []),
            "targetUniverse": state.get("targetUniverse", state["symbols"]),
        },
        "metrics": {
            "totalReturn": round(value / state["initialCash"] - 1, 6),
            "policyReturn": round(_policy_return(state, value), 6),
            "policyDrawdown": round(_policy_drawdown(state, value), 6),
            "realizedPnl": round(realized, 2),
            "totalFees": round(fees, 2),
            "winRate": round(wins / max(1, sells), 4),
            "tradeCount": len(state["trades"]),
            "step": state["step"],
            "policySize": len(state["qTable"]),
            "epsilon": round(float(state.get("epsilon", _initial_epsilon(state))), 4),
            "riskStopRemainingSeconds": _risk_stop_remaining(state),
        },
        "holdings": state["holdings"],
        "positions": _positions_public(state, quotes, value),
        "quotes": quotes,
        "equityCurve": state["equityCurve"],
        "trades": state["trades"],
        "tradeGuards": state.get("lastTradeGuards", []),
        "messages": state["messages"],
    }
