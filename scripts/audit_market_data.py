import json
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "candles"
REPORT = ROOT / "audit-report.md"

MARKETS = ["BTC", "ETH", "SOL"]
PERIODS = {
    "15m": 15 * 60 * 1000,
    "1H": 60 * 60 * 1000,
    "4H": 4 * 60 * 60 * 1000,
}

REQUIRED_FIELDS = [
    "time", "open", "high", "low", "close", "volume"
]

BUY_SELL_KEYWORDS = [
    "taker_buy", "taker_sell",
    "buy_volume", "sell_volume",
    "buy_vol", "sell_vol",
    "active_buy", "active_sell",
]


def format_time(timestamp):
    try:
        return datetime.fromtimestamp(
            timestamp / 1000,
            tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return "无法转换"


def audit_file(path, interval):
    result = {
        "file": path.name,
        "exists": path.exists(),
    }

    if not path.exists():
        result["error"] = "文件不存在"
        return result

    records = []
    malformed = []
    timestamps = []
    field_missing = set()
    buy_sell_fields = set()

    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue

            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("记录不是JSON对象")

                records.append(row)

                for field in REQUIRED_FIELDS:
                    if field not in row:
                        field_missing.add(field)

                for key in row:
                    key_lower = key.lower()
                    if any(word in key_lower for word in BUY_SELL_KEYWORDS):
                        buy_sell_fields.add(key)

                timestamp = row.get("time")

                if isinstance(timestamp, (int, float)):
                    timestamps.append((line_number, int(timestamp)))
                else:
                    malformed.append(
                        f"第{line_number}行：time字段无效"
                    )

            except Exception as e:
                malformed.append(
                    f"第{line_number}行：{str(e)}"
                )

    valid_times = [item[1] for item in timestamps]

    duplicates = []
    out_of_order = []
    gaps = []
    unusual_intervals = []

    for i in range(1, len(timestamps)):
        previous_line, previous_time = timestamps[i - 1]
        current_line, current_time = timestamps[i]

        difference = current_time - previous_time

        if difference == 0:
            duplicates.append(current_line)
        elif difference < 0:
            out_of_order.append(current_line)
        elif difference > interval:
            missing = difference // interval - 1
            if difference % interval == 0:
                gaps.append({
                    "line": current_line,
                    "missing": missing,
                    "difference": difference,
                })
            else:
                unusual_intervals.append({
                    "line": current_line,
                    "difference": difference,
                })

    result.update({
        "records": len(records),
        "valid_timestamps": len(valid_times),
        "malformed": malformed,
        "missing_fields": sorted(field_missing),
        "duplicates": duplicates,
        "out_of_order": out_of_order,
        "gaps": gaps,
        "unusual_intervals": unusual_intervals,
        "buy_sell_fields": sorted(buy_sell_fields),
    })

    if valid_times:
        result["first_time"] = format_time(valid_times[0])
        result["last_time"] = format_time(valid_times[-1])
        result["first_timestamp"] = valid_times[0]
        result["last_timestamp"] = valid_times[-1]

    return result


def main():
    results = []

    for market in MARKETS:
        for period, interval in PERIODS.items():
            filename = f"{market}-USDT-SWAP_{period}.jsonl"
            path = DATA_DIR / filename
            results.append(audit_file(path, interval))

    lines = [
        "# DQ-01 市场K线数据只读审计报告",
        "",
        f"审计时间：{datetime.now(timezone.utc).isoformat()}",
        "",
        "说明：本报告只读取数据，不修改原始K线文件。",
        "",
    ]

    for item in results:
        lines.extend([
            f"## {item['file']}",
            "",
            f"- 文件存在：{'是' if item['exists'] else '否'}",
        ])

        if not item["exists"]:
            lines.extend([f"- 结果：{item['error']}", ""])
            continue

        lines.extend([
            f"- 记录数量：{item['records']}",
            f"- 有效时间戳数量：{item['valid_timestamps']}",
            f"- 第一条时间：{item.get('first_time', '无')}",
            f"- 最后一条时间：{item.get('last_time', '无')}",
            f"- 缺少字段：{item['missing_fields'] or '未发现'}",
            f"- 重复记录位置：{item['duplicates'] or '未发现'}",
            f"- 乱序记录位置：{item['out_of_order'] or '未发现'}",
            f"- 时间缺口：{item['gaps'] or '未发现'}",
            f"- 非标准时间间隔：{item['unusual_intervals'] or '未发现'}",
            f"- 主动买卖量相关字段：{item['buy_sell_fields'] or '未发现'}",
            f"- 无法解析记录：{item['malformed'] or '未发现'}",
            "",
        ])

    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n报告已生成：{REPORT}")


if __name__ == "__main__":
    main()
