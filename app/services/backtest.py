from __future__ import annotations

import math
import random
from dataclasses import dataclass


PROFILE_SETTINGS = {
    "stable": {"max_weight": 0.25, "trade_threshold": 0.035, "risk_penalty": 1.25},
    "balanced": {"max_weight": 0.35, "trade_threshold": 0.025, "risk_penalty": 0.9},
    "aggressive": {"max_weight": 0.55, "trade_threshold": 0.015, "risk_penalty": 0.45},
}


@dataclass
class Portfolio:
    cash: float
    shares: dict[str, float]


def run_backtest(
    histories: dict[str, list[dict]],
    initial_cash: float = 10_000_000,
    strategy: str = "momentum",
    profile: str = "balanced",
    fee_rate: float = 0.0005,
    slippage_rate: float = 0.0005,
    learned_policy: dict[str, int] | None = None,
) -> dict:
    symbols, dates, price_map = _align_histories(histories)
    if len(dates) < 35:
        raise ValueError("공통 거래일 데이터가 부족합니다. 기간을 더 길게 선택하세요.")

    settings = PROFILE_SETTINGS.get(profile, PROFILE_SETTINGS["balanced"])
    portfolio = Portfolio(cash=float(initial_cash), shares={symbol: 0.0 for symbol in symbols})
    trades = []
    equity_curve = []
    rewards = []
    previous_value = float(initial_cash)
    daily_returns = []

    for idx, date in enumerate(dates):
        prices = {symbol: price_map[symbol][date]["close"] for symbol in symbols}
        current_value = _portfolio_value(portfolio, prices)
        reward = 0.0 if idx == 0 else (current_value - previous_value) / max(previous_value, 1e-9)
        rewards.append(round(reward, 6))
        if idx > 0:
            daily_returns.append(reward)

        weights = _current_weights(portfolio, prices, current_value)
        equity_curve.append(
            {
                "date": date,
                "value": round(current_value, 2),
                "cash": round(portfolio.cash, 2),
                "return": round((current_value / initial_cash) - 1, 6),
                "weights": {symbol: round(weights.get(symbol, 0.0), 4) for symbol in symbols},
            }
        )

        if idx < 25 or idx == len(dates) - 1:
            previous_value = current_value
            continue

        if strategy == "buy_hold" and idx > 25:
            previous_value = current_value
            continue

        target_weights = _target_weights(
            strategy=strategy,
            symbols=symbols,
            dates=dates,
            date=date,
            idx=idx,
            price_map=price_map,
            current_weights=weights,
            max_weight=settings["max_weight"],
            learned_policy=learned_policy,
        )

        if strategy == "buy_hold" and idx == 25:
            target_weights = {symbol: min(1.0 / len(symbols), settings["max_weight"]) for symbol in symbols}

        executed = _rebalance(
            portfolio=portfolio,
            prices=prices,
            target_weights=target_weights,
            total_value=current_value,
            date=date,
            fee_rate=fee_rate,
            slippage_rate=slippage_rate,
            trade_threshold=settings["trade_threshold"],
        )
        trades.extend(executed)
        previous_value = _portfolio_value(portfolio, prices)

    final_prices = {symbol: price_map[symbol][dates[-1]]["close"] for symbol in symbols}
    final_value = _portfolio_value(portfolio, final_prices)
    metrics = _metrics(initial_cash, final_value, equity_curve, trades, daily_returns, rewards, profile)
    return {
        "symbols": symbols,
        "dates": dates,
        "strategy": strategy,
        "profile": profile,
        "initialCash": round(initial_cash, 2),
        "finalValue": round(final_value, 2),
        "metrics": metrics,
        "equityCurve": equity_curve,
        "trades": trades,
        "priceSeries": {
            symbol: [
                {"date": date, "close": price_map[symbol][date]["close"], "volume": price_map[symbol][date]["volume"]}
                for date in dates
            ]
            for symbol in symbols
        },
        "holdings": {
            symbol: {
                "shares": round(portfolio.shares.get(symbol, 0.0), 6),
                "price": round(final_prices[symbol], 4),
                "value": round(portfolio.shares.get(symbol, 0.0) * final_prices[symbol], 2),
            }
            for symbol in symbols
        },
    }


def train_q_learning(
    histories: dict[str, list[dict]],
    initial_cash: float = 10_000_000,
    profile: str = "balanced",
    episodes: int = 40,
) -> dict:
    symbols, dates, price_map = _align_histories(histories)
    q_table: dict[str, list[float]] = {}
    actions = [0, 1, 2, 3, 4]
    alpha = 0.18
    gamma = 0.86
    epsilon = 0.28
    episode_summaries = []

    for episode in range(max(1, min(300, int(episodes or 40)))):
        portfolio = Portfolio(cash=float(initial_cash), shares={symbol: 0.0 for symbol in symbols})
        previous_value = float(initial_cash)
        total_reward = 0.0
        for idx, date in enumerate(dates):
            if idx < 25:
                continue
            prices = {symbol: price_map[symbol][date]["close"] for symbol in symbols}
            state = _state_key(symbols, dates, idx, price_map, portfolio, prices)
            q_table.setdefault(state, [0.0 for _ in actions])
            if random.random() < epsilon:
                action = random.choice(actions)
            else:
                action = _best_action(q_table[state])
            target_weights = _weights_for_action(action, symbols, dates, idx, price_map, PROFILE_SETTINGS[profile]["max_weight"])
            value_before = _portfolio_value(portfolio, prices)
            _rebalance(
                portfolio=portfolio,
                prices=prices,
                target_weights=target_weights,
                total_value=value_before,
                date=date,
                fee_rate=0.0005,
                slippage_rate=0.0005,
                trade_threshold=PROFILE_SETTINGS[profile]["trade_threshold"],
            )
            value_after = _portfolio_value(portfolio, prices)
            reward = (value_after - previous_value) / max(previous_value, 1e-9)
            reward -= _drawdown_like_penalty(value_after, initial_cash, profile)
            next_state = _state_key(symbols, dates, min(idx + 1, len(dates) - 1), price_map, portfolio, prices)
            q_table.setdefault(next_state, [0.0 for _ in actions])
            old = q_table[state][action]
            q_table[state][action] = old + alpha * (reward + gamma * max(q_table[next_state]) - old)
            previous_value = value_after
            total_reward += reward
        epsilon = max(0.04, epsilon * 0.96)
        final_prices = {symbol: price_map[symbol][dates[-1]]["close"] for symbol in symbols}
        episode_summaries.append(
            {
                "episode": episode + 1,
                "reward": round(total_reward, 5),
                "value": round(_portfolio_value(portfolio, final_prices), 2),
            }
        )

    policy = {state: _best_action(values) for state, values in q_table.items()}
    learned = run_backtest(
        histories=histories,
        initial_cash=initial_cash,
        strategy="q_learning",
        profile=profile,
        learned_policy=policy,
    )
    return {
        "episodes": episode_summaries,
        "policySize": len(policy),
        "policy": policy,
        "result": learned,
    }


def _align_histories(histories: dict[str, list[dict]]) -> tuple[list[str], list[str], dict[str, dict[str, dict]]]:
    symbols = list(histories.keys())
    date_sets = [set(row["date"] for row in rows) for rows in histories.values()]
    dates = sorted(set.intersection(*date_sets))
    price_map = {symbol: {row["date"]: row for row in rows if row["date"] in dates} for symbol, rows in histories.items()}
    return symbols, dates, price_map


def _portfolio_value(portfolio: Portfolio, prices: dict[str, float]) -> float:
    return portfolio.cash + sum(portfolio.shares.get(symbol, 0.0) * prices[symbol] for symbol in prices)


def _current_weights(portfolio: Portfolio, prices: dict[str, float], total_value: float) -> dict[str, float]:
    if total_value <= 0:
        return {symbol: 0.0 for symbol in prices}
    return {symbol: portfolio.shares.get(symbol, 0.0) * price / total_value for symbol, price in prices.items()}


def _target_weights(strategy, symbols, dates, date, idx, price_map, current_weights, max_weight, learned_policy=None):
    if strategy == "random":
        picks = random.sample(symbols, k=random.randint(1, len(symbols)))
        raw = {symbol: random.random() for symbol in picks}
        return _normalize_capped(raw, max_weight)
    if strategy == "q_learning":
        invested = sum(current_weights.values())
        exposure = "high" if invested > 0.75 else "mid" if invested > 0.35 else "low"
        state = f"{_market_state_key(symbols, dates, idx, price_map)}:{exposure}"
        action = (learned_policy or {}).get(state, 2)
        return _weights_for_action(action, symbols, dates, idx, price_map, max_weight)
    return _momentum_weights(symbols, dates, idx, price_map, max_weight)


def _momentum_weights(symbols, dates, idx, price_map, max_weight):
    raw = {}
    for symbol in symbols:
        now = price_map[symbol][dates[idx]]["close"]
        short = price_map[symbol][dates[idx - 20]]["close"]
        long = price_map[symbol][dates[max(0, idx - 60)]]["close"]
        signal = ((now / short) - 1) * 1.8 + ((now / long) - 1)
        if signal > 0:
            raw[symbol] = signal
    return _normalize_capped(raw, max_weight)


def _weights_for_action(action, symbols, dates, idx, price_map, max_weight):
    if action == 0:
        return {symbol: 0.0 for symbol in symbols}
    ranked = []
    for symbol in symbols:
        now = price_map[symbol][dates[idx]]["close"]
        prev = price_map[symbol][dates[max(0, idx - 20)]]["close"]
        ranked.append((symbol, (now / prev) - 1))
    ranked.sort(key=lambda item: item[1], reverse=True)
    if action == 1:
        raw = {symbol: max(score, 0.01) for symbol, score in ranked[:2] if score > -0.02}
    elif action == 2:
        raw = {symbol: max(score, 0.01) for symbol, score in ranked[:4] if score > -0.04}
    elif action == 3:
        raw = {symbol: max(score, 0.01) for symbol, score in ranked if score > -0.08}
    else:
        raw = {symbol: 1.0 for symbol in symbols}
    return _normalize_capped(raw, max_weight)


def _normalize_capped(raw: dict[str, float], max_weight: float) -> dict[str, float]:
    if not raw:
        return {}
    total = sum(max(0.0, value) for value in raw.values())
    if total <= 0:
        return {}
    weights = {symbol: min(max_weight, max(0.0, value) / total) for symbol, value in raw.items()}
    scale = min(1.0, 1.0 / max(sum(weights.values()), 1e-9))
    return {symbol: value * scale for symbol, value in weights.items()}


def _rebalance(portfolio, prices, target_weights, total_value, date, fee_rate, slippage_rate, trade_threshold):
    trades = []
    current_weights = _current_weights(portfolio, prices, total_value)
    all_symbols = set(prices) | set(target_weights)

    for symbol in sorted(all_symbols):
        target = target_weights.get(symbol, 0.0)
        current = current_weights.get(symbol, 0.0)
        diff_value = (target - current) * total_value
        if abs(diff_value) / max(total_value, 1e-9) < trade_threshold:
            continue
        price = prices[symbol]
        if diff_value > 0:
            execution_price = price * (1 + slippage_rate)
            affordable = portfolio.cash / (1 + fee_rate)
            trade_value = min(diff_value, affordable)
            if trade_value <= 0:
                continue
            shares = trade_value / execution_price
            fee = trade_value * fee_rate
            portfolio.cash -= trade_value + fee
            portfolio.shares[symbol] = portfolio.shares.get(symbol, 0.0) + shares
            side = "BUY"
        else:
            execution_price = price * (1 - slippage_rate)
            shares = min(portfolio.shares.get(symbol, 0.0), abs(diff_value) / execution_price)
            if shares <= 0:
                continue
            trade_value = shares * execution_price
            fee = trade_value * fee_rate
            portfolio.cash += trade_value - fee
            portfolio.shares[symbol] = max(0.0, portfolio.shares.get(symbol, 0.0) - shares)
            side = "SELL"
        trades.append(
            {
                "date": date,
                "symbol": symbol,
                "side": side,
                "shares": round(shares, 6),
                "price": round(execution_price, 4),
                "value": round(trade_value, 2),
                "fee": round(fee, 2),
            }
        )
    return trades


def _metrics(initial_cash, final_value, equity_curve, trades, daily_returns, rewards, profile):
    values = [point["value"] for point in equity_curve]
    peak = values[0] if values else initial_cash
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    wins = sum(1 for r in daily_returns if r > 0)
    annualized = 0.0
    if len(values) > 1 and final_value > 0:
        annualized = (final_value / initial_cash) ** (252 / len(values)) - 1
    mean = sum(daily_returns) / max(1, len(daily_returns))
    variance = sum((r - mean) ** 2 for r in daily_returns) / max(1, len(daily_returns))
    volatility = math.sqrt(variance) * math.sqrt(252)
    sharpe = 0.0 if volatility == 0 else annualized / volatility
    advice_score = (final_value / initial_cash - 1) - abs(max_drawdown) * PROFILE_SETTINGS[profile]["risk_penalty"]
    return {
        "totalReturn": round(final_value / initial_cash - 1, 6),
        "annualizedReturn": round(annualized, 6),
        "maxDrawdown": round(max_drawdown, 6),
        "winRate": round(wins / max(1, len(daily_returns)), 4),
        "volatility": round(volatility, 6),
        "sharpeLike": round(sharpe, 4),
        "tradeCount": len(trades),
        "avgReward": round(sum(rewards) / max(1, len(rewards)), 6),
        "adviceScore": round(advice_score, 6),
    }


def _market_state_key(symbols, dates, idx, price_map):
    returns = []
    for symbol in symbols:
        now = price_map[symbol][dates[idx]]["close"]
        prev = price_map[symbol][dates[max(0, idx - 20)]]["close"]
        returns.append((now / prev) - 1)
    avg_return = sum(returns) / len(returns)
    trend = "up" if avg_return > 0.04 else "down" if avg_return < -0.04 else "flat"
    dispersion = max(returns) - min(returns)
    spread = "wide" if dispersion > 0.12 else "normal"
    return f"{trend}:{spread}"


def _state_key(symbols, dates, idx, price_map, portfolio, prices):
    total = _portfolio_value(portfolio, prices)
    invested = 1 - (portfolio.cash / max(total, 1e-9))
    exposure = "high" if invested > 0.75 else "mid" if invested > 0.35 else "low"
    return f"{_market_state_key(symbols, dates, idx, price_map)}:{exposure}"


def _best_action(values):
    return max(range(len(values)), key=lambda idx: values[idx])


def _drawdown_like_penalty(value, initial_cash, profile):
    loss = max(0.0, 1 - value / max(initial_cash, 1e-9))
    return loss * 0.002 * PROFILE_SETTINGS[profile]["risk_penalty"]
