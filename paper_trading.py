import json
import glob
import os
from datetime import datetime, timezone

DATA_DIR = "data/candles"

SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]
PERIODS = ["15m", "1H", "4H"]


def parse_candle(row):
    """兼容 OKX 数组格式和 JSON 对象格式。"""

    if isinstance(row, list):
        if len(row) < 6:
            raise ValueError("K线数组字段不足")

        return {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }

    if isinstance(row, dict):
        time_value = row.get("time", row.get("timestamp", row.get("ts")))

        if time_value is None:
            raise ValueError("缺少时间字段")

        return {
            "time": int(time_value),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }

    raise ValueError("无法识别的数据格式")


def check_file(path):
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

                if candle["high"] < candle["low"]:
                    raise ValueError("最高价低于最低价")

                if candle["volume"] < 0:
                    raise ValueError("成交量为负数")

                candles.append(candle)

            except Exception as error:
                errors += 1

                if errors <= 3:
                    print(f"第 {line_number} 行错误：{error}")

    timestamps = [c["time"] for c in candles]
    unique_timestamps = set(timestamps)

    if candles:
        first_time = min(timestamps)
        last_time = max(timestamps)

        first_date = datetime.fromtimestamp(
            first_time / 1000, timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

        last_date = datetime.fromtimestamp(
            last_time / 1000, timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

        print("有效K线数量：", len(candles))
        print("重复时间记录：", len(timestamps) - len(unique_timestamps))
        print("第一根K线：", first_date)
        print("最后一根K线：", last_date)
        print("最新收盘价：", candles[-1]["close"])
    else:
        print("没有读取到有效K线")

    print("错误记录：", errors)

    return candles


def main():
    total = 0
    success = 0

    print("=" * 60)
    print("模拟交易引擎 - 历史K线数据检查")
    print("=" * 60)

    for symbol in SYMBOLS:
        for period in PERIODS:
            path = os.path.join(
                DATA_DIR, f"{symbol}_{period}.jsonl"
            )

            print("\n检查文件：", path)

            if not os.path.exists(path):
                print("文件不存在")
                continue

            candles = check_file(path)
            total += 1

            if candles:
                success += 1

    print("\n" + "=" * 60)
    print(f"检查完成：{success}/{total} 个文件读取成功")
    print("=" * 60)


if __name__ == "__main__":
    main()
