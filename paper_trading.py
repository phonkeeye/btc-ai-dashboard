import bisect
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone

DATA_DIR = "data/candles"
FUNDING_DIR = "data/funding"
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
MIN_15M_BARS = 200
MIN_HTF_BARS = 60

INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
}


def normalize_ts_ms(ts):
    if ts is None or isinstance(ts, bool):
        raise ValueError("无效时间戳")
    if isinstance(ts, float):
        raise ValueError(f"拒绝浮点时间戳: {ts}")
    if isinstance(ts, int):
        val = ts
    elif isinstance(ts, str):
        text = ts.strip()
        if not text or not text.isdigit():
            raise ValueError(f"无效时间戳字符串: {ts!r}")
        val = int(text)
    else:
        raise ValueError(f"不支持的时间戳类型: {type(ts).__name__}")

    if val <= 0:
        raise ValueError(f"时间戳必须大于零: {val}")

    if val < 10**11:
        return val * 1000
    return val


def finite_float(val, name):
    if isinstance(val, bool) or val is None:
        raise ValueError(f"{name} 不能为空或布尔值")
    f = float(val)
    if not math.isfinite(f):
        raise ValueError(f"{name} 非有限数值（NaN/Inf）")
    return f


def assert_finite_structure(obj, path="root"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert_finite_structure(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for idx, item in enumerate(obj):
            assert_finite_structure(item, f"{path}[{idx}]")
    elif isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"输出结构存在非有限浮点数: {path}={obj}")


def parse_candle(row):
    if isinstance(row, list):
        if len(row) < 6:
            raise ValueError("K线数组字段不足")
        ts_raw, o_raw, h_raw, l_raw, c_raw, v_raw = row[:6]
    elif isinstance(row, dict):
        ts_raw = row.get("time", row.get("timestamp", row.get("ts")))
        o_raw = row.get("open")
        h_raw = row.get("high")
        l_raw = row.get("low")
        c_raw = row.get("close")
        v_raw = row.get("volume", 0.0)
    else:
        raise ValueError("无法识别的K线数据格式")

    c = {
        "time": normalize_ts_ms(ts_raw),
        "open": finite_float(o_raw, "open"),
        "high": finite_float(h_raw, "high"),
        "low": finite_float(l_raw, "low"),
        "close": finite_float(c_raw, "close"),
        "volume": finite_float(v_raw, "volume"),
    }

    if min(c["open"], c["high"], c["low"], c["close"]) <= 0:
        raise ValueError("价格必须大于零")
    if c["high"] < max(c["open"], c["close"], c["low"]):
        raise ValueError("最高价异常")
    if c["low"] > min(c["open"], c["close"], c["high"]):
        raise ValueError("最低价异常")
    if c["volume"] < 0:
        raise ValueError("成交量为负数")
    return c


def check_continuity(candles, period, symbol):
    step = INTERVAL_MS[period]
    for i in range(1, len(candles)):
        diff = candles[i]["time"] - candles[i - 1]["time"]
        if diff <= 0:
            raise ValueError(f"[{symbol} {period}] 时间戳非严格递增")
        if diff != step:
            raise ValueError(
                f"[{symbol} {period}] 发现时间断层: {candles[i - 1]['time']} -> {candles[i]['time']} "
                f"(期望 {step}ms，实际 {diff}ms)"
            )


def load_candles(symbol, period):
    path = os.path.join(DATA_DIR, f"{symbol}_{period}.jsonl")
    if not os.path.exists(path):
        raise FileNotFoundError(f"缺少K线文件: {path}")

    rows, errors = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(parse_candle(json.loads(line)))
            except Exception as exc:
                errors += 1
                if errors <= 5:
                    print(f"[{symbol} {period}] 第{line_no}行错误：{exc}")

    if errors > 0:
        raise ValueError(f"[{symbol} {period}] 存在 {errors} 条无效K线记录，终止回测")

    by_time = {}
    duplicate_count = 0
    conflict_count = 0
    for x in rows:
        ts = x["time"]
        if ts in by_time:
            duplicate_count += 1
            if by_time[ts] != x:
                conflict_count += 1
                if conflict_count <= 5:
                    print(f"[告警][{symbol} {period}] 时间戳 {ts} 存在重复记录且内容冲突，保留最新一条")
        by_time[ts] = x

    if duplicate_count > 0:
        print(
            f"[{symbol} {period}] 发现重复时间戳 {duplicate_count} 条（内容冲突 {conflict_count} 条）"
        )

    candles = [by_time[t] for t in sorted(by_time)]
    check_continuity(candles, period, symbol)
    print(f"[{symbol} {period}] 有效K线: {len(candles)} | 时间连续性: 通过")
    return candles


def load_funding_rates(symbol):
    """读取可选资金费率文件，返回 (事件列表, 质量统计字典)"""
    path = os.path.join(FUNDING_DIR, f"{symbol}_funding.jsonl")
    quality = {
        "funding_file_missing": not os.path.exists(path),
        "loaded_event_count": 0,
        "applied_event_count": 0,
        "real_mark_price_count": 0,
        "estimated_mark_price_count": 0,
        "duplicate_count": 0,
        "conflict_count": 0,
    }
    if not os.path.exists(path):
        print(f"[提示][{symbol}] 未找到资金费率文件 {path}，标记为缺失")
        return [], quality

    items = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise ValueError(f"[{symbol} funding] 第{line_no}行格式错误")
            ts = normalize_ts_ms(raw.get("time", raw.get("fundingTime", raw.get("ts"))))
            rate = finite_float(raw.get("rate", raw.get("fundingRate")), "fundingRate")
            mark_raw = raw.get("markPrice", raw.get("mark_price"))
            mark_price = finite_float(mark_raw, "markPrice") if mark_raw is not None else None
            if mark_price is not None and mark_price <= 0:
                raise ValueError(f"[{symbol} funding] 第{line_no}行 markPrice 必须大于零")

            rec = {"time": ts, "rate": rate, "mark_price": mark_price}
            if ts in items:
                quality["duplicate_count"] += 1
                if items[ts] != rec:
                    quality["conflict_count"] += 1
                    if quality["conflict_count"] <= 5:
                        print(f"[告警][{symbol} funding] 结算时间 {ts} 存在重复记录且内容冲突，保留最新一条")
            items[ts] = rec

    if quality["duplicate_count"] > 0:
        print(
            f"[{symbol} funding] 发现重复结算时间 {quality['duplicate_count']} 条"
            f"（内容冲突 {quality['conflict_count']} 条）"
        )

    events = [items[ts] for ts in sorted(items)]
    quality["loaded_event_count"] = len(events)
    return events, quality


def resolve_mark_price_on_bar(bar, f_ts, event_mark_price, quality):
    """结算时优先使用事件自带标记价，否则线性估算并统计估算次数"""
    quality["applied_event_count"] += 1
    if event_mark_price is not None:
        quality["real_mark_price_count"] += 1
        return event_mark_price
    quality["estimated_mark_price_count"] += 1
    span = INTERVAL_MS["15m"]
    ratio = min(max((f_ts - bar["time"]) / span, 0.0), 1.0)
    return bar["open"] + (bar["close"] - bar["open"]) * ratio


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    acc = sum(values[:period]) / period
    out[period - 1] = acc
    for i in range(period, len(values)):
        acc = values[i] * alpha + acc * (1.0 - alpha)
        out[i] = acc
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
            prev_close = candles[i - 1]["close"]
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - prev_close),
                abs(c["low"] - prev_close),
            )
        trs.append(tr)
    acc = sum(trs[1 : period + 1]) / period
    out[period] = acc
    for i in range(period + 1, len(candles)):
        acc = (acc * (period - 1) + trs[i]) / period
        out[i] = acc
    return out


def add_indicators(candles):
    closes = [c["close"] for c in candles]
    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    atr = atr_values(candles, ATR_PERIOD)
    for i, c in enumerate(candles):
        c["ema_fast"] = fast[i]
        c["ema_slow"] = slow[i]
        c["atr"] = atr[i]
    return candles


def closed_time_index(candles, period):
    step = INTERVAL_MS[period]
    return [c["time"] + step for c in candles]


def last_closed(candles, close_times, timestamp):
    idx = bisect.bisect_right(close_times, timestamp) - 1
    return candles[idx] if idx >= 0 else None


def trend(candle):
    if candle is None or candle["ema_fast"] is None or candle["ema_slow"] is None:
        return 0
    if candle["ema_fast"] > candle["ema_slow"]:
        return 1
    if candle["ema_fast"] < candle["ema_slow"]:
        return -1
    return 0


def signal_for(prev_15m, c1h, t1h, c4h, t4h):
    t15 = trend(prev_15m)
    signal_time = prev_15m["time"] + INTERVAL_MS["15m"]
    latest_1h = last_closed(c1h, t1h, signal_time)
    latest_4h = last_closed(c4h, t4h, signal_time)
    t1 = trend(latest_1h)
    t4 = trend(latest_4h)
    return t15 if t15 != 0 and t15 == t1 == t4 else 0


def slipped(price, side):
    return price * (1.0 + SLIPPAGE_RATE) if side == "buy" else price * (1.0 - SLIPPAGE_RATE)


def position_size(equity, raw_entry, stop_distance, direction):
    if not all(math.isfinite(x) for x in (equity, raw_entry, stop_distance)):
        raise ValueError("仓位计算输入存在非有限数值")
    if isinstance(direction, bool) or not isinstance(direction, int) or direction not in (1, -1):
        raise ValueError(f"无效交易方向: {direction!r}")

    if stop_distance <= 0 or equity <= 0 or raw_entry <= 0:
        return 0.0

    entry = slipped(raw_entry, "buy" if direction == 1 else "sell")
    if direction == 1:
        raw_stop = entry - stop_distance
        if raw_stop <= 0:
            return 0.0
        worst_exit = slipped(raw_stop, "sell")
        price_loss_per_unit = max(entry - worst_exit, 0.0)
    else:
        raw_stop = entry + stop_distance
        if raw_stop <= 0:
            return 0.0
        worst_exit = slipped(raw_stop, "buy")
        price_loss_per_unit = max(worst_exit - entry, 0.0)

    fee_per_unit = (entry + worst_exit) * FEE_RATE
    total_loss_per_unit = price_loss_per_unit + fee_per_unit
    if total_loss_per_unit <= 0:
        return 0.0

    qty_by_risk = (equity * RISK_PER_TRADE) / total_loss_per_unit
    qty_by_notional = (equity * MAX_POSITION_MULTIPLE) / entry
    qty = max(0.0, min(qty_by_risk, qty_by_notional))
    if not math.isfinite(qty):
        raise ValueError("仓位计算结果存在非有限数值")
    return qty


def adverse_equity_on_bar(equity, pos, bar):
    """估算按当根K线最不利极值平仓（含平仓滑点与平仓手续费）后的最坏权益"""
    if pos is None:
        return equity
    if pos["direction"] == 1:
        worst_exit_price = slipped(bar["low"], "sell")
        gross_pnl = (worst_exit_price - pos["entry"]) * pos["qty"]
    else:
        worst_exit_price = slipped(bar["high"], "buy")
        gross_pnl = (pos["entry"] - worst_exit_price) * pos["qty"]
    est_exit_fee = worst_exit_price * pos["qty"] * FEE_RATE
    return equity + gross_pnl - est_exit_fee


def settle_funding_for_window(
    pos, bar, window_start, window_end, funding_events, funding_idx, quality
):
    """结算落在 (max(window_start, pos['entry_time']), window_end] 的资金费率事件"""
    cash_delta = 0.0
    effective_start = max(window_start, pos["entry_time"])
    while funding_idx < len(funding_events):
        ev = funding_events[funding_idx]
        f_ts = ev["time"]
        if f_ts <= effective_start:
            funding_idx += 1
            continue
        if f_ts > window_end:
            break
        mark_price = resolve_mark_price_on_bar(bar, f_ts, ev["mark_price"], quality)
        notional = pos["qty"] * mark_price
        payment = -pos["direction"] * notional * ev["rate"]
        cash_delta += payment
        pos["funding_pnl"] += payment
        funding_idx += 1
    return cash_delta, funding_idx


def evaluate_exit_on_bar(pos, bar):
    stop, target = pos["stop"], pos["target"]
    if pos["direction"] == 1:
        if bar["open"] <= stop:
            return bar["open"], "止损跳空"
        if bar["open"] >= target:
            return bar["open"], "止盈跳空"
        if bar["low"] <= stop:
            return stop, "止损"
        if bar["high"] >= target:
            return target, "止盈"
    else:
        if bar["open"] >= stop:
            return bar["open"], "止损跳空"
        if bar["open"] <= target:
            return bar["open"], "止盈跳空"
        if bar["high"] >= stop:
            return stop, "止损"
        if bar["low"] <= target:
            return target, "止盈"
    return None, None


def close_position(pos, raw_exit, exit_time, reason, symbol):
    if pos["direction"] == 1:
        exit_price = slipped(raw_exit, "sell")
        gross = (exit_price - pos["entry"]) * pos["qty"]
    else:
        exit_price = slipped(raw_exit, "buy")
        gross = (pos["entry"] - exit_price) * pos["qty"]

    exit_fee = exit_price * pos["qty"] * FEE_RATE
    funding_pnl = pos.get("funding_pnl", 0.0)
    net = gross - exit_fee - pos["entry_fee"] + funding_pnl

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
        "funding_pnl": round(funding_pnl, 6),
        "net_pnl": round(net, 6),
        "reason": reason,
        "holding_minutes": round((exit_time - pos["entry_time"]) / 60000, 2),
    }
    equity_change = gross - exit_fee
    return trade, equity_change


def update_drawdown(equity, peak, max_dd):
    new_peak = max(peak, equity)
    new_dd = max(max_dd, (new_peak - equity) / new_peak) if new_peak > 0 else max_dd
    return new_peak, new_dd


def simulate_symbol(symbol):
    print("\n" + "=" * 58 + f"\n开始回测：{symbol}\n" + "=" * 58)
    c15 = load_candles(symbol, "15m")
    c1h = load_candles(symbol, "1H")
    c4h = load_candles(symbol, "4H")
    funding_events, funding_quality = load_funding_rates(symbol)

    if len(c15) < MIN_15M_BARS or len(c1h) < MIN_HTF_BARS or len(c4h) < MIN_HTF_BARS:
        raise ValueError(
            f"[{symbol}] 历史K线不足（15m={len(c15)}, 1H={len(c1h)}, 4H={len(c4h)}）"
        )

    add_indicators(c15)
    add_indicators(c1h)
    add_indicators(c4h)

    t1h = closed_time_index(c1h, "1H")
    t4h = closed_time_index(c4h, "4H")

    equity = INITIAL_CAPITAL
    peak = equity
    max_dd = 0.0
    pos = None
    funding_idx = 0

    trades = []
    exit_reasons = Counter()
    daily_entry_counts = Counter()
    equity_curve = []

    step_15m = INTERVAL_MS["15m"]

    for i in range(1, len(c15)):
        bar = c15[i]
        bar_time = bar["time"]
        bar_end = bar_time + step_15m
        closed_on_bar = False

        while funding_idx > 0 and funding_events[funding_idx - 1]["time"] > bar_time:
            funding_idx -= 1

        if pos is not None:
            f_delta, funding_idx = settle_funding_for_window(
                pos, bar, bar_time, bar_end, funding_events, funding_idx, funding_quality
            )
            equity += f_delta

            worst_eq = adverse_equity_on_bar(equity, pos, bar)
            peak, max_dd = update_drawdown(worst_eq, peak, max_dd)

            exit_price, reason = evaluate_exit_on_bar(pos, bar)
            if exit_price is not None:
                trade, change = close_position(pos, exit_price, bar_end, reason, symbol)
                trades.append(trade)
                exit_reasons[reason] += 1
                equity += change
                pos = None
                closed_on_bar = True

        mark_equity = equity
        if pos is not None:
            if pos["direction"] == 1:
                mark_equity += (bar["close"] - pos["entry"]) * pos["qty"]
            else:
                mark_equity += (pos["entry"] - bar["close"]) * pos["qty"]
        peak, max_dd = update_drawdown(mark_equity, peak, max_dd)
        equity_curve.append({"time": bar_end, "equity": round(mark_equity, 6)})

        if closed_on_bar or pos is not None or equity <= 0:
            continue

        prev_15m = c15[i - 1]
        atr = prev_15m["atr"]
        if atr is None or atr <= 0:
            continue

        sig = signal_for(prev_15m, c1h, t1h, c4h, t4h)
        if sig == 0:
            continue

        raw_entry = bar["open"]
        stop_distance = atr * ATR_STOP_MULTIPLE
        qty = position_size(equity, raw_entry, stop_distance, sig)
        if qty <= 0:
            continue

        entry = slipped(raw_entry, "buy" if sig == 1 else "sell")
        entry_fee = entry * qty * FEE_RATE
        equity -= entry_fee

        if sig == 1:
            stop = entry - stop_distance
            target = entry + stop_distance * REWARD_RISK_RATIO
        else:
            stop = entry + stop_distance
            target = entry - stop_distance * REWARD_RISK_RATIO

        pos = {
            "direction": sig,
            "entry": entry,
            "entry_time": bar_time,
            "qty": qty,
            "stop": stop,
            "target": target,
            "entry_fee": entry_fee,
            "funding_pnl": 0.0,
        }
        day_str = datetime.fromtimestamp(bar_time / 1000, timezone.utc).strftime("%Y-%m-%d")
        daily_entry_counts[day_str] += 1

        # 新仓仅结算 entry_time 之后且落在本根K线内的资金费率事件
        f_delta, funding_idx = settle_funding_for_window(
            pos, bar, bar_time, bar_end, funding_events, funding_idx, funding_quality
        )
        equity += f_delta

        # 新开仓后立即检查当前入场K线最坏风险及止损/止盈
        worst_eq = adverse_equity_on_bar(equity, pos, bar)
        peak, max_dd = update_drawdown(worst_eq, peak, max_dd)

        exit_price, reason = evaluate_exit_on_bar(pos, bar)
        if exit_price is not None:
            trade, change = close_position(pos, exit_price, bar_end, reason, symbol)
            trades.append(trade)
            exit_reasons[reason] += 1
            equity += change
            pos = None

        close_mark = equity
        if pos is not None:
            if pos["direction"] == 1:
                close_mark += (bar["close"] - pos["entry"]) * pos["qty"]
            else:
                close_mark += (pos["entry"] - bar["close"]) * pos["qty"]
        peak, max_dd = update_drawdown(close_mark, peak, max_dd)
        equity_curve[-1]["equity"] = round(close_mark, 6)

    # 数据末尾按最后一根K线收盘时间强平
    if pos is not None:
        last = c15[-1]
        final_exit_time = last["time"] + step_15m
        trade, change = close_position(pos, last["close"], final_exit_time, "数据结束平仓", symbol)
        trades.append(trade)
        exit_reasons["数据结束平仓"] += 1
        equity += change
        peak, max_dd = update_drawdown(equity, peak, max_dd)
        if equity_curve:
            equity_curve[-1]["time"] = final_exit_time
            equity_curve[-1]["equity"] = round(equity, 6)

    wins = [x for x in trades if x["net_pnl"] > 0]
    losses = [x for x in trades if x["net_pnl"] <= 0]
    gross_profit = sum(x["net_pnl"] for x in wins)
    gross_loss = abs(sum(x["net_pnl"] for x in losses))
    pf = gross_profit / gross_loss if gross_loss else None
    avg_win = sum(x["net_pnl"] for x in wins) / len(wins) if wins else 0.0
    avg_loss = sum(x["net_pnl"] for x in losses) / len(losses) if losses else 0.0
    total_fees = sum(x["total_fees"] for x in trades)
    total_funding = sum(x["funding_pnl"] for x in trades)
    avg_hold = sum(x["holding_minutes"] for x in trades) / len(trades) if trades else 0.0
    max_daily_entries = max(daily_entry_counts.values()) if daily_entry_counts else 0

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
        "return_pct": round((equity / INITIAL_CAPITAL - 1.0) * 100, 2),
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(pf, 3) if pf is not None else None,
        "average_win": round(avg_win, 4),
        "average_loss": round(avg_loss, 4),
        "total_fees": round(total_fees, 4),
        "total_funding_pnl": round(total_funding, 4),
        "funding_quality": funding_quality,
        "average_holding_minutes": round(avg_hold, 2),
        "max_consecutive_losses": max_consecutive_losses,
        "max_entries_per_utc_day": max_daily_entries,
        "exit_reasons": dict(exit_reasons),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "max_drawdown_basis": "基于15分钟OHLC极值与资金费率结算顺序的估算最大回撤（非实时逐笔路径）",
        "approximation_notes": {
            "funding_vs_intraday_stop": "15分钟K线内先结算资金费率再检查止损/止盈，属于OHLC时序近似",
            "max_drawdown": "按当根最不利极值含平仓滑点与手续费估算，属于15分钟OHLC近似",
            "atomic_output": "三个JSON文件各自独立原子写入，尚未实现跨三文件整体事务提交",
        },
    }
    assert_finite_structure(summary, f"summary[{symbol}]")
    assert_finite_structure(trades, f"trades[{symbol}]")
    assert_finite_structure(equity_curve, f"equity_curve[{symbol}]")

    print("\n诊断结果：")
    for key, value in summary.items():
        print(f"  {key}: {value}")

    return trades, summary, equity_curve


def atomic_write_json(path, payload):
    """P09: 先写临时文件再原子替换，严格禁用 NaN/Infinity"""
    assert_finite_structure(payload, path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def main():
    print("=" * 58)
    print("BTC AI Quant - 回测诊断报告（单币独立资金）")
    print("=" * 58)

    all_trades, summaries, all_curves = [], [], []
    for symbol in SYMBOLS:
        trades, summary, curve = simulate_symbol(symbol)
        all_trades.extend(trades)
        summaries.append(summary)
        all_curves.append({"symbol": symbol, "equity_curve": curve})

    atomic_write_json("data/paper_trades.json", all_trades)
    atomic_write_json("data/backtest_summary.json", summaries)
    atomic_write_json("data/equity_curves.json", all_curves)

    print("\n" + "=" * 58)
    print(f"完成：{len(summaries)}/{len(SYMBOLS)} 个币种回测成功")
    print("交易明细：data/paper_trades.json")
    print("诊断汇总：data/backtest_summary.json")
    print("权益曲线：data/equity_curves.json")
    print("=" * 58)


if __name__ == "__main__":
    main()
