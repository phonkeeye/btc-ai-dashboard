import json
import os
import bisect
from datetime import datetime, timezone

# =========================
# 基础配置
# =========================

DATA_DIR = "data/candles"

SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
]

INITIAL_CAPITAL = 10000.0

# 每笔交易最多承担账户权益的 1% 风险
RISK_PER_TRADE = 0.01

# 不使用杠杆，名义仓位不超过账户权益
MAX_POSITION_MULTIPLE = 1.0

# 模拟手续费：单边 0.05%
FEE_RATE = 0.0005

# 模拟滑点：单边 0.03%
SLIPPAGE_RATE = 0.0003

# ATR 止损倍数
ATR_STOP_MULTIPLE = 1.5

# 止盈风险收益比
REWARD_RISK_RATIO = 2.0

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14

PERIODS = ["15m", "1H", "4H"]

INTERVAL_MS = {
    "15m": 15 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
}


# =========================
# 读取 K 线
# =========================

def parse_candle(row):

    if isinstance(row, list):

        if len(row) < 6:
            raise ValueError("K线数组字段不足")

        candle = {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }

    elif isinstance(row, dict):

        time_value = row.get(
            "time",
            row.get("timestamp", row.get("ts"))
        )

        if time_value is None:
            raise ValueError("缺少时间字段")

        candle = {
            "time": int(time_value),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }

    else:
        raise ValueError("无法识别的数据格式")

    if candle["open"] <= 0 or candle["close"] <= 0:
        raise ValueError("价格必须大于零")

    if candle["high"] < candle["low"]:
        raise ValueError("最高价低于最低价")

    if candle["high"] < max(candle["open"], candle["close"]):
        raise ValueError("最高价异常")

    if candle["low"] > min(candle["open"], candle["close"]):
        raise ValueError("最低价异常")

    if candle["volume"] < 0:
        raise ValueError("成交量为负数")

    return candle


def load_candles(symbol, period):

    path = os.path.join(
        DATA_DIR,
        f"{symbol}_{period}.jsonl"
    )

    if not os.path.exists(path):
        raise FileNotFoundError(path)

    candles = []
    errors = 0

    with open(path, "r", encoding="utf-8") as file:

        for line_number, line in enumerate(file, 1):

            line = line.strip()

            if not line:
                continue

            try:
                row = json.loads(line)
                candle = parse_candle(row)
                candles.append(candle)

            except Exception as error:

                errors += 1

                if errors <= 3:
                    print(
                        f"{symbol} {period} "
                        f"第 {line_number} 行错误：{error}"
                    )

    candles.sort(key=lambda x: x["time"])

    # 去除重复时间记录
    unique = {}

    for candle in candles:
        unique[candle["time"]] = candle

    candles = sorted(
        unique.values(),
        key=lambda x: x["time"]
    )

    print(
        f"{symbol} {period}: "
        f"{len(candles)} 根有效K线，"
        f"{errors} 条错误记录"
    )

    return candles


# =========================
# 技术指标
# =========================

def calculate_ema(values, period):

    result = [None] * len(values)

    if len(values) < period:
        return result

    multiplier = 2 / (period + 1)

    ema = sum(values[:period]) / period

    result[period - 1] = ema

    for i in range(period, len(values)):

        ema = (
            values[i] * multiplier
            + ema * (1 - multiplier)
        )

        result[i] = ema

    return result


def calculate_atr(candles, period):

    result = [None] * len(candles)

    if len(candles) <= period:
        return result

    true_ranges = []

    for i, candle in enumerate(candles):

        if i == 0:
            tr = candle["high"] - candle["low"]

        else:

            previous_close = candles[i - 1]["close"]

            tr = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )

        true_ranges.append(tr)

    atr = sum(true_ranges[1:period + 1]) / period

    result[period] = atr

    for i in range(period + 1, len(candles)):

        atr = (
            atr * (period - 1)
            + true_ranges[i]
        ) / period

        result[i] = atr

    return result


def add_indicators(candles):

    closes = [c["close"] for c in candles]

    ema_fast = calculate_ema(closes, EMA_FAST)
    ema_slow = calculate_ema(closes, EMA_SLOW)
    atr = calculate_atr(candles, ATR_PERIOD)

    for i in range(len(candles)):

        candles[i]["ema_fast"] = ema_fast[i]
        candles[i]["ema_slow"] = ema_slow[i]
        candles[i]["atr"] = atr[i]

    return candles


# =========================
# 多周期趋势判断
# =========================

def build_closed_time_index(candles, period):

    interval = INTERVAL_MS[period]

    close_times = [
        candle["time"] + interval
        for candle in candles
    ]

    return close_times


def get_closed_candle(candles, close_times, signal_time):

    # 只取已经收盘的高周期K线
    index = bisect.bisect_right(
        close_times,
        signal_time
    ) - 1

    if index < 0:
        return None

    return candles[index]


def get_trend(candle):

    if candle is None:
        return None

    fast = candle.get("ema_fast")
    slow = candle.get("ema_slow")

    if fast is None or slow is None:
        return None

    if fast > slow:
        return 1

    if fast < slow:
        return -1

    return 0


def get_signal(
    candle_15m,
    candles_1h,
    close_times_1h,
    candles_4h,
    close_times_4h,
):

    trend_15m = get_trend(candle_15m)

    if trend_15m is None or trend_15m == 0:
        return 0

    signal_time = (
        candle_15m["time"] + INTERVAL_MS["15m"]
    )

    candle_1h = get_closed_candle(
        candles_1h,
        close_times_1h,
        signal_time
    )

    candle_4h = get_closed_candle(
        candles_4h,
        close_times_4h,
        signal_time
    )

    trend_1h = get_trend(candle_1h)
    trend_4h = get_trend(candle_4h)

    if trend_1h is None or trend_4h is None:
        return 0

    # 三个周期方向一致才交易
    if trend_15m == trend_1h == trend_4h:
        return trend_15m

    return 0


# =========================
# 模拟交易
# =========================

def apply_slippage(price, side):

    if side == "buy":
        return price * (1 + SLIPPAGE_RATE)

    return price * (1 - SLIPPAGE_RATE)


def calculate_position_size(
    equity,
    entry_price,
    stop_distance
):

    if stop_distance <= 0:
        return 0

    risk_money = equity * RISK_PER_TRADE

    quantity = risk_money / stop_distance

    max_quantity = (
        equity * MAX_POSITION_MULTIPLE
    ) / entry_price

    return min(quantity, max_quantity)


def simulate_symbol(symbol):

    print("\n" + "=" * 60)
    print("开始回测：", symbol)
    print("=" * 60)

    candles_15m = load_candles(symbol, "15m")
    candles_1h = load_candles(symbol, "1H")
    candles_4h = load_candles(symbol, "4H")

    candles_15m = add_indicators(candles_15m)
    candles_1h = add_indicators(candles_1h)
    candles_4h = add_indicators(candles_4h)

    close_times_1h = build_closed_time_index(
        candles_1h,
        "1H"
    )

    close_times_4h = build_closed_time_index(
        candles_4h,
        "4H"
    )

    equity = INITIAL_CAPITAL
    peak_equity = equity
    max_drawdown = 0.0

    position = None
    trades = []

    for i in range(1, len(candles_15m)):

        candle = candles_15m[i]

        # ---------------------
        # 先管理已有仓位
        # ---------------------

        if position is not None:

            exit_price = None
            exit_reason = None

            stop = position["stop"]
            target = position["target"]

            if position["direction"] == 1:

                hit_stop = candle["low"] <= stop
                hit_target = candle["high"] >= target

                # 同一根K线同时触发，保守地按止损处理
                if hit_stop:
                    exit_price = stop
                    exit_reason = "止损"

                elif hit_target:
                    exit_price = target
                    exit_reason = "止盈"

            else:

                hit_stop = candle["high"] >= stop
                hit_target = candle["low"] <= target

                if hit_stop:
                    exit_price = stop
                    exit_reason = "止损"

                elif hit_target:
                    exit_price = target
                    exit_reason = "止盈"

            if exit_price is not None:

                if position["direction"] == 1:

                    actual_exit = apply_slippage(
                        exit_price,
                        "sell"
                    )

                    gross_pnl = (
                        actual_exit - position["entry"]
                    ) * position["quantity"]

                else:

                    actual_exit = apply_slippage(
                        exit_price,
                        "buy"
                    )

                    gross_pnl = (
                        position["entry"] - actual_exit
                    ) * position["quantity"]

                exit_fee = (
                    actual_exit
                    * position["quantity"]
                    * FEE_RATE
                )

                net_pnl = (
    gross_pnl
    - exit_fee
    - position["entry_fee"]
)

equity += gross_pnl - exit_fee

                trades.append({
                    "symbol": symbol,
                    "direction": (
                        "做多"
                        if position["direction"] == 1
                        else "做空"
                    ),
                    "entry_time": position["entry_time"],
                    "exit_time": candle["time"],
                    "entry_price": round(
                        position["entry"], 6
                    ),
                    "exit_price": round(
                        actual_exit, 6
                    ),
                    "quantity": round(
                        position["quantity"], 8
                    ),
                    "pnl": round(net_pnl, 4),
                    "reason": exit_reason,
                })

                position = None

                peak_equity = max(
                    peak_equity,
                    equity
                )

                drawdown = (
                    (peak_equity - equity)
                    / peak_equity
                    if peak_equity > 0
                    else 0
                )

                max_drawdown = max(
                    max_drawdown,
                    drawdown
                )

        # ---------------------
        # 已有仓位时不再开新仓
        # ---------------------

        if position is not None:
            continue

        if equity <= 0:
            break

        # ---------------------
        # 使用上一根已收盘K线判断
        # ---------------------

        signal_candle = candles_15m[i - 1]

        atr = signal_candle.get("atr")

        if atr is None or atr <= 0:
            continue

        signal = get_signal(
            signal_candle,
            candles_1h,
            close_times_1h,
            candles_4h,
            close_times_4h,
        )

        if signal == 0:
            continue

        # 下一根15分钟K线开盘成交
        raw_entry = candle["open"]

        if signal == 1:

            entry = apply_slippage(
                raw_entry,
                "buy"
            )

        else:

            entry = apply_slippage(
                raw_entry,
                "sell"
            )

        stop_distance = (
            atr * ATR_STOP_MULTIPLE
        )

        quantity = calculate_position_size(
            equity,
            entry,
            stop_distance
        )

        if quantity <= 0:
            continue

        entry_fee = (
            entry * quantity * FEE_RATE
        )

        # 开仓手续费从账户权益扣除
        equity -= entry_fee

        if signal == 1:

            stop = entry - stop_distance

            target = (
                entry
                + stop_distance * REWARD_RISK_RATIO
            )

        else:

            stop = entry + stop_distance

            target = (
                entry
                - stop_distance * REWARD_RISK_RATIO
            )

        position = {
            "direction": signal,
            "entry": entry,
            "entry_time": candle["time"],
            "quantity": quantity,
            "stop": stop,
            "target": target,
            "entry_fee": entry_fee,
        }

    # ---------------------
    # 数据结束时强制平仓
    # ---------------------

    if position is not None:

        last_candle = candles_15m[-1]

        raw_exit = last_candle["close"]

        if position["direction"] == 1:

            actual_exit = apply_slippage(
                raw_exit,
                "sell"
            )

            gross_pnl = (
                actual_exit - position["entry"]
            ) * position["quantity"]

        else:

            actual_exit = apply_slippage(
                raw_exit,
                "buy"
            )

            gross_pnl = (
                position["entry"] - actual_exit
            ) * position["quantity"]

        exit_fee = (
            actual_exit
            * position["quantity"]
            * FEE_RATE
        )

        net_pnl = (
            gross_pnl
            - exit_fee
            - position["entry_fee"]
        )

        equity += net_pnl

        trades.append({
            "symbol": symbol,
            "direction": (
                "做多"
                if position["direction"] == 1
                else "做空"
            ),
            "entry_time": position["entry_time"],
            "exit_time": last_candle["time"],
            "entry_price": round(
                position["entry"], 6
            ),
            "exit_price": round(
                actual_exit, 6
            ),
            "quantity": round(
                position["quantity"], 8
            ),
            "pnl": round(net_pnl, 4),
            "reason": "数据结束平仓",
        })

    # ---------------------
    # 统计结果
    # ---------------------

    wins = [
        trade for trade in trades
        if trade["pnl"] > 0
    ]

    losses = [
        trade for trade in trades
        if trade["pnl"] <= 0
    ]

    total_pnl = equity - INITIAL_CAPITAL

    win_rate = (
        len(wins) / len(trades) * 100
        if trades
        else 0
    )

    gross_profit = sum(
        trade["pnl"] for trade in wins
    )

    gross_loss = abs(sum(
        trade["pnl"] for trade in losses
    ))

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else 0
    )

    print("\n回测结果：", symbol)
    print("初始资金：", round(INITIAL_CAPITAL, 2))
    print("最终权益：", round(equity, 2))
    print("总盈亏：", round(total_pnl, 2))
    print("收益率：", round(
        total_pnl / INITIAL_CAPITAL * 100, 2
    ), "%")
    print("交易次数：", len(trades))
    print("盈利次数：", len(wins))
    print("亏损次数：", len(losses))
    print("胜率：", round(win_rate, 2), "%")
    print("盈利因子：", round(profit_factor, 2))
    print("最大回撤：", round(
        max_drawdown * 100, 2
    ), "%")

    return trades, equity, max_drawdown


# =========================
# 主程序
# =========================

def main():

    print("=" * 60)
    print("BTC AI Quant - 第一版历史回测")
    print("=" * 60)

    print("初始资金：", INITIAL_CAPITAL)
    print("单笔风险：", RISK_PER_TRADE * 100, "%")
    print("手续费：", FEE_RATE * 100, "%")
    print("模拟滑点：", SLIPPAGE_RATE * 100, "%")
    print("止损：", ATR_STOP_MULTIPLE, "倍 ATR")
    print("止盈风险收益比：", REWARD_RISK_RATIO, ": 1")

    all_trades = []
    results = []

    for symbol in SYMBOLS:

        try:

            trades, equity, drawdown = simulate_symbol(
                symbol
            )

            all_trades.extend(trades)

            results.append({
                "symbol": symbol,
                "final_equity": round(equity, 2),
                "pnl": round(
                    equity - INITIAL_CAPITAL, 2
                ),
                "max_drawdown": round(
                    drawdown * 100, 2
                ),
                "trades": len(trades),
            })

        except Exception as error:

            print(
                "\n回测失败：",
                symbol,
                error
            )

    os.makedirs("data", exist_ok=True)

    with open(
        "data/paper_trades.json",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            all_trades,
            file,
            ensure_ascii=False,
            indent=2
        )

    with open(
        "data/backtest_summary.json",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            results,
            file,
            ensure_ascii=False,
            indent=2
        )

    print("\n" + "=" * 60)
    print("全部币种回测汇总")
    print("=" * 60)

    for result in results:

        print(
            result["symbol"],
            "| 最终权益：",
            result["final_equity"],
            "| 盈亏：",
            result["pnl"],
            "| 最大回撤：",
            result["max_drawdown"],
            "%",
            "| 交易次数：",
            result["trades"],
        )

    print("\n交易记录已保存：data/paper_trades.json")
    print("汇总结果已保存：data/backtest_summary.json")
    print("回测完成。")


if __name__ == "__main__":
    main()
