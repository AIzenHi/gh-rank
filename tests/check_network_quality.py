"""网络质量体检：区分「偶尔抽风」与「真的不通」。

背景：这台机器的网络会间歇性抽风（实测 ConnectTimeout、WinError 10054）。
所以探测逻辑不能只看一次成败，否则状态条会疯狂误报。

本脚本对每个目标连打 N 次，统计成功率与延迟分布，
用来判断重试次数该怎么设、探测该怎么判。
"""

import socket
import ssl
import statistics
import sys
import time
import urllib.request
from collections import defaultdict

socket.setdefaulttimeout(15)

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

TARGETS = {
    "raw README": "https://raw.githubusercontent.com/torvalds/linux/HEAD/README",
    "github.com 首页": "https://github.com/torvalds/linux",
    "trending 日榜": "https://github.com/trending?since=daily",
}

ROUNDS = 8

results = defaultdict(list)
errors = defaultdict(list)

print(f"每个目标连打 {ROUNDS} 次\n" + "=" * 58)

for name, url in TARGETS.items():
    for i in range(ROUNDS):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read(2048)
            ms = (time.time() - t0) * 1000
            results[name].append(ms)
        except Exception as exc:  # noqa: BLE001
            ms = (time.time() - t0) * 1000
            results[name].append(None)
            errors[name].append(type(exc).__name__)
            print(f"  {name} 第{i + 1}次失败 {ms:.0f}ms  {type(exc).__name__}")
        time.sleep(0.6)

print("\n" + "=" * 58)
print(f"{'目标':<18}{'成功率':>10}{'中位延迟':>12}{'最慢':>10}")
print("-" * 58)

summary = {}
for name in TARGETS:
    vals = [v for v in results[name] if v is not None]
    ok = len(vals)
    total = len(results[name])
    rate = ok / total if total else 0
    med = statistics.median(vals) if vals else float("nan")
    worst = max(vals) if vals else float("nan")
    summary[name] = (rate, med, worst)
    print(f"{name:<18}{rate:>9.0%}{med:>10.0f}ms{worst:>8.0f}ms")
    if errors[name]:
        print(f"{'':<18}失败类型: {set(errors[name])}")

print()
worst_rate = min(r for r, _, _ in summary.values())
print(f"最差路线成功率 = {worst_rate:.0%}")

if worst_rate >= 0.9:
    print("→ 网络基本稳定。重试 3 次足够，探测 2 次即可判定可用。")
elif worst_rate >= 0.5:
    print("→ 网络会抽风但多数时候通。重试 3 次 + 探测多数决（3 轮）是合理配置。")
else:
    print("→ 网络很差。建议把重试提到 5 次，并把探测超时缩短（快速失败）。")

sys.exit(0)