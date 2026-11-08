import json
import os
import bisect
from collections import Counter
from datetime import datetime, timezone

DATA_DIR = "data/candles"
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]

INITIAL_CAPITAL = 10000.0
RISK_PER_TRADE = 0.01
MAX_POSITION_MULTIPLE = 1.0
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0003
ATR_STOP_MULTIPLE = 1.5
REWARD_RISK_RATIO = 2.0
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14

INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
}


def parse_candle(row):
    if isinstance(row, list):
        if len(row) < 6:
            raise ValueError("K线数组字段不足")
        c = {
            "time": int(row[0]), "open": float(row[1]),
            "high": float(row[2]), "low": float(row[3]),
            "close": float(row[4]), "volume": float(row[5]),
        }
    elif isinstance(row, dict):
        ts = row.get("time", row.get("timestamp", row.get("ts")))
        if ts is None:
            raise ValueError("缺少时间字段")
        c = {
            "time": int(ts), "open": float(row["open"]),
            "high": float(row["high"]), "low": float(row["low"]),
            "close": float(row["close"]), "volume": float(row["volume"]),
        }
    else:
        raise ValueError("无法识别的数据格式")

    if min(c["open"], c["high"], c["low"], c["close"]) <= 0:
        raise ValueError("价格必须大于零")
    if c["high"] < max(c["open"], c["close"], c["low"]):
        raise ValueError("最高价异常")
    if c["low"] > min(c["open"], c["close"], c["high"]):
        raise ValueError("最低价异常")
    if c["volume"] < 0:
        raise ValueError("成交量为负数")
    return c


def load_candles(symbol, period):
    path = os.path.join(DATA_DIR, f"{symbol}_{period}.jsonl")
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    rows, errors = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(parse_candle(json.loads(line)))
            except Exception as exc:
                errors += 1
                if errors <= 3:
                    print(f"{symbol} {period} 第{line_no}行错误：{exc}")

    # 同一时间戳只保留最后一条；排序后确保时间序列严格递增
    by_time = {x["time"]: x for x in rows}
    candles = [by_time[t] for t in sorted(by_time)]
    print(f"{symbol} {period}: {len(candles)}根有效K线，{errors}条错误")
    return candles


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2 / (period + 1)
    value = sum(values[:period]) / period
    out[period - 1] = value
    for i in range(period, len(values)):
        value = values[i] * alpha + value * (1 - alpha)
        out[i] = value
    return out


def atr_values(candles, period):
    out = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev = candles[i - 1]["close"]
            tr = max(c["high"] - c["low"],
                     abs(c["high"] - prev), abs(c["low"] - prev))
        trs.append(tr)
    value = sum(trs[1:period + 1]) / period
    out[period] = value
    for i in range(period + 1, len(candles)):
        value = (value * (period - 1) + trs[i]) / period
        out[i] = value
    return out


def add_indicators(candles):
    closes = [c["close"] for c in candles]
    fast, slow, atr = ema(closes, EMA_FAST), ema(closes, EMA_SLOW), atr_values(candles, ATR_PERIOD)
    for i, c in enumerate(candles):
        c["ema_fast"], c["ema_slow"], c["atr"] = fast[i], slow[i], atr[i]
    return candles


def closed_time_index(candles, period):
    return [c["time"] + INTERVAL_MS[period] for c in candles]


def last_closed(candles, close_times, timestamp):
    # 允许恰好在信号K线收盘时使用刚刚收盘的高周期K线
    i = bisect.bisect_right(close_times, timestamp) - 1
    return candles[i] if i >= 0 else None


def trend(candle):
    if candle is None or candle["ema_fast"] is None or candle["ema_slow"] is None:
        return 0
    if candle["ema_fast"] > candle["ema_slow"]:
        return 1
    if candle["ema_fast"] < candle["ema_slow"]:
        return -1
    return 0


def signal_for(c15, c1h, t1h, c4h, t4h):
    t15 = trend(c15)
    signal_time = c15["time"] + INTERVAL_MS["15m"]
    t1 = trend(last_closed(c1h, t1h, signal_time))
    t4 = trend(last_closed(c4h, t4h, signal_time))
    return t15 if t15 != 0 and t15 == t1 == t4 else 0


def slipped(price, side):
    return price * (1 + SLIPPAGE_RATE) if side == "buy" else price * (1 - SLIPPAGE_RATE)


def position_size(equity, entry, stop_distance):
    if stop_distance <= 0 or equity <= 0:
        return 0.0
    qty_by_risk = equity * RISK_PER_TRADE / stop_distance
    qty_by_notional = equity * MAX_POSITION_MULTIPLE / entry
    return min(qty_by_risk, qty_by_notional)


def close_position(pos, raw_exit, exit_time, reason, symbol):
    if pos["direction"] == 1:
        exit_price = slipped(raw_exit, "sell")
        gross = (exit_price - pos["entry"]) * pos["qty"]
    else:
        exit_price = slipped(raw_exit, "buy")
        gross = (pos["entry"] - exit_price) * pos["qty"]

    exit_fee = exit_price * pos["qty"] * FEE_RATE
    net = gross - exit_fee - pos["entry_fee"]
    trade = {
        "symbol": symbol,
        "direction": "做多" if pos["direction"] == 1 else "做空",
        "entry_time": pos["entry_time"],
        "exit_time": exit_time,
        "entry_price": round(pos["entry"], 8),
        "exit_price": round(exit_price, 8),
        "quantity": round(pos["qty"], 10),
        "gross_pnl": round(gross, 6),
        "entry_fee": round(pos["entry_fee"], 6),
        "exit_fee": round(exit_fee, 6),
        "total_fees": round(pos["entry_fee"] + exit_fee, 6),
        "net_pnl": round(net, 6),
        "reason": reason,
        "holding_minutes": round((exit_time - pos["entry_time"]) / 60000, 2),
    }
    # 开仓费已在开仓时从 equity 扣除，这里只扣平仓盈亏和手续费
    equity_change = gross - exit_fee
    return trade, equity_change


def simulate_symbol(symbol):
    print("\n" + "=" * 58 + f"\n开始诊断回测：{symbol}\n" + "=" * 58)
    c15 = add_indicators(load_candles(symbol, "15m"))
    c1 = add_indicators(load_candles(symbol, "1H"))
    c4 = add_indicators(load_candles(symbol, "4H"))
    if len(c15) < 100 or len(c1) < 60 or len(c4) < 60:
        raise ValueError("历史数据不足，无法可靠计算指标")

    t1, t4 = closed_time_index(c1, "1H"), closed_time_index(c4, "4H")
    equity = INITIAL_CAPITAL
    peak = equity
    max_dd = 0.0
    pos = None
    trades = []
    exit_reasons = Counter()
    daily_counts = Counter()
    equity_curve = []

    for i in range(1, len(c15)):
        bar = c15[i]
        bar_time = bar["time"]

        if pos is not None:
            exit_price, reason = None, None
            stop, target = pos["stop"], pos["target"]

            # 保守处理：开盘跳空优先按开盘价；盘中同时触及则止损优先
            if pos["direction"] == 1:
                if bar["open"] <= stop:
                    exit_price, reason = bar["open"], "止损跳空"
                elif bar["open"] >= target:
                    exit_price, reason = bar["open"], "止盈跳空"
                elif bar["low"] <= stop:
                    exit_price, reason = stop, "止损"
                elif bar["high"] >= target:
                    exit_price, reason = target, "止盈"
            else:
                if bar["open"] >= stop:
                    exit_price, reason = bar["open"], "止损跳空"
                elif bar["open"] <= target:
                    exit_price, reason = bar["open"], "止盈跳空"
                elif bar["high"] >= stop:
                    exit_price, reason = stop, "止损"
                elif bar["low"] <= target:
                    exit_price, reason = target, "止盈"

            if exit_price is not None:
                trade, change = close_position(pos, exit_price, bar_time, reason, symbol)
                trades.append(trade)
                exit_reasons[reason] += 1
                equity += change
                pos = None

        # 每根K线收盘后记录权益；若持仓，按收盘价估算未实现盈亏
        mark_equity = equity
        if pos is not None:
            if pos["direction"] == 1:
                mark_equity += (bar["close"] - pos["entry"]) * pos["qty"]
            else:
                mark_equity += (pos["entry"] - bar["close"]) * pos["qty"]
        peak = max(peak, mark_equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - mark_equity) / peak)
        equity_curve.append({"time": bar_time, "equity": round(mark_equity, 6)})

        if pos is not None or equity <= 0:
            continue

        # 用刚刚收盘的上一根15m K线计算信号，在当前K线开盘成交
        previous = c15[i - 1]
        atr = previous["atr"]
        if atr is None or atr <= 0:
            continue

        sig = signal_for(previous, c1, t1, c4, t4)
        if sig == 0:
            continue

        raw_entry = bar["open"]
        entry = slipped(raw_entry, "buy" if sig == 1 else "sell")
        stop_distance = atr * ATR_STOP_MULTIPLE
        qty = position_size(equity, entry, stop_distance)
        if qty <= 0:
            continue

        entry_fee = entry * qty * FEE_RATE
        equity -= entry_fee

        if sig == 1:
            stop, target = entry - stop_distance, entry + stop_distance * REWARD_RISK_RATIO
        else:
            stop, target = entry + stop_distance, entry - stop_distance * REWARD_RISK_RATIO

        pos = {
            "direction": sig, "entry": entry, "entry_time": bar_time,
            "qty": qty, "stop": stop, "target": target, "entry_fee": entry_fee,
        }
        daily_counts[datetime.fromtimestamp(bar_time / 1000, timezone.utc).strftime("%Y-%m-%d")] += 1

    # 数据末尾按最后收盘价平仓
    if pos is not None:
        last = c15[-1]
        trade, change = close_position(pos, last["close"], last["time"], "数据结束平仓", symbol)
        trades.append(trade)
        exit_reasons["数据结束平仓"] += 1
        equity += change

    wins = [x for x in trades if x["net_pnl"] > 0]
    losses = [x for x in trades if x["net_pnl"] <= 0]
    gross_profit = sum(x["net_pnl"] for x in wins)
    gross_loss = abs(sum(x["net_pnl"] for x in losses))
    pf = gross_profit / gross_loss if gross_loss else None
    avg_win = sum(x["net_pnl"] for x in wins) / len(wins) if wins else 0
    avg_loss = sum(x["net_pnl"] for x in losses) / len(losses) if losses else 0
    total_fees = sum(x["total_fees"] for x in trades)
    avg_hold = sum(x["holding_minutes"] for x in trades) / len(trades) if trades else 0
    max_daily = max(daily_counts.values()) if daily_counts else 0
    max_consecutive_losses, streak = 0, 0
    for x in trades:
        if x["net_pnl"] <= 0:
            streak += 1
            max_consecutive_losses = max(max_consecutive_losses, streak)
        else:
            streak = 0

    summary = {
        "symbol": symbol,
        "initial_capital": INITIAL_CAPITAL,
        "final_equity": round(equity, 2),
        "net_pnl": round(equity - INITIAL_CAPITAL, 2),
        "return_pct": round((equity / INITIAL_CAPITAL - 1) * 100, 2),
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "profit_factor": round(pf, 3) if pf is not None else None,
        "average_win": round(avg_win, 4),
        "average_loss": round(avg_loss, 4),
        "total_fees": round(total_fees, 4),
        "average_holding_minutes": round(avg_hold, 2),
        "max_consecutive_losses": max_consecutive_losses,
        "max_trades_per_utc_day": max_daily,
        "exit_reasons": dict(exit_reasons),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }

    print("\n诊断结果：")
    for key, value in summary.items():
        print(f"{key}: {value}")

    return trades, summary, equity_curve


def main():
    print("=" * 58)
    print("BTC AI Quant - 回测诊断报告")
    print("每个币独立使用相同初始资金；非组合账户")
    print("=" * 58)

    all_trades, summaries, all_curves = [], [], []
    for symbol in SYMBOLS:
        try:
            trades, summary, curve = simulate_symbol(symbol)
            all_trades.extend(trades)
            summaries.append(summary)
            all_curves.append({"symbol": symbol, "equity_curve": curve})
        except Exception as exc:
            print(f"\n回测失败：{symbol}: {exc}")

    os.makedirs("data", exist_ok=True)
    with open("data/paper_trades.json", "w", encoding="utf-8") as f:
        json.dump(all_trades, f, ensure_ascii=False, indent=2)
    with open("data/backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2)
    with open("data/equity_curves.json", "w", encoding="utf-8") as f:
        json.dump(all_curves, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 58)
    print(f"完成：{len(summaries)}/{len(SYMBOLS)} 个币种回测成功")
    print("交易明细：data/paper_trades.json")
    print("诊断汇总：data/backtest_summary.json")
    print("权益曲线：data/equity_curves.json")
    print("=" * 58)


if __name__ == "__main__":
    main()
