"""快捷方式与接口响应时间的验证脚本。

背景：/api/quota 早先版本会同步等抓取源探测做完（最坏几分钟），
把服务线程堵死，连 localhost 调用都超时。这里锁定这个回归。
"""

import json
import subprocess
from pathlib import Path
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
ROOT = str(Path(__file__).resolve().parent.parent)

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        failed.append(name)
        print(f"  FAIL  {name}  {detail}")


def timed_get(path: str, timeout: int = 20) -> tuple[int, dict]:
    t0 = time.time()
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8", "ignore"))
    return int((time.time() - t0) * 1000), body


print("=" * 64)
print("快捷方式 + 接口响应时间验证")
print("=" * 64)

# ---------------------------------------------------------------- 1. 接口不阻塞
print("\n[1] /api/quota 必须立即返回（探测在后台跑）")
times = []
for i in range(5):
    ms, q = timed_get("/api/quota")
    times.append(ms)
    flag = "后台探测中" if q.get("probing") else f"raw={q.get('raw')} html={q.get('html')}"
    print(f"      #{i + 1}  {ms:>5} ms   {flag}")
    time.sleep(0.5)

worst = max(times)
check("最慢响应 < 3 秒", worst < 3000, f"最慢 {worst} ms")
check("平均响应 < 1 秒", sum(times) / len(times) < 1000, f"平均 {sum(times) // len(times)} ms")

# ---------------------------------------------------------------- 2. 其他接口不被拖死
print("\n[2] 探测进行中，其他接口仍要正常响应")
ms, h = timed_get("/api/health")
check("/api/health 响应正常", ms < 3000 and h.get("ok") is True, f"{ms} ms")

ms, lb = timed_get("/api/leaderboard?period=daily")
check("/api/leaderboard 响应正常", ms < 3000 and len(lb["entries"]) > 0,
      f"{ms} ms / {len(lb['entries'])} 条")

# ---------------------------------------------------------------- 3. 等探测完成
print("\n[3] 等后台探测跑完")
final = None
for _ in range(50):
    time.sleep(3)
    _, q = timed_get("/api/quota")
    if q.get("raw") is not None:
        final = q
        break

if final:
    # 不要求 raw 一定通：它会被墙/抽风（实测 10-02 raw 与 html 先后都断过）。
    # 探测接口的职责是"如实报告 + 永不阻塞"，这两点上面已断言。
    reach = [n for n in ("raw", "html") if final.get(n)]
    print(f"      可用路线: {', '.join(reach) if reach else '（全断，靠缓存显示）'}"
          f"   投票 {final.get('rounds')} 轮")
    check("探测如实报告了状态", isinstance(final.get("raw"), bool), str(final.get("raw")))
else:
    check("后台探测在 150 秒内完成", False, "还没出结果")

# ---------------------------------------------------------------- 4. 重复点快捷方式
print("\n[4] 重复双击快捷方式：应只开浏览器，不重复起进程")
before = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "(Get-NetTCPConnection -LocalPort 8765 -State Listen).OwningProcess"],
    capture_output=True, text=True, timeout=60,
).stdout.strip()

t0 = time.time()
r = subprocess.run(
    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
     ROOT + r"\launcher.ps1"],
    capture_output=True, text=True, timeout=120,
)
dup_seconds = time.time() - t0

after = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "(Get-NetTCPConnection -LocalPort 8765 -State Listen).OwningProcess"],
    capture_output=True, text=True, timeout=60,
).stdout.strip()

check("重复点击返回成功", r.returncode == 0, f"返回码 {r.returncode}")
check("重复点击很快（<10秒，说明没在重启）", dup_seconds < 10, f"{dup_seconds:.1f} 秒")
check("服务 PID 未变（没重复启动）", before == after, f"{before} → {after}")

# ---------------------------------------------------------------- 收尾
print()
print("=" * 64)
print(f"通过 {passed} 项，失败 {len(failed)} 项")
for f in failed:
    print(f"  - {f}")
print("=" * 64)
sys.exit(1 if failed else 0)