"""全量测试套件汇总。

用法：
    python -m app.cli serve        # 先把服务起起来
    python tests\run_all.py

注意：网络类断言依赖真实外网，而本机网络会间歇性抽风。
所以这里对每个测试跑两遍，只要有一遍通过就算过（避免被抖动误判为回归）。
"""

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"

SUITES = [
    ("README 清洗", "test_clean_readme.py", 1),
    ("防编造机制", "test_explainer_antifab.py", 1),
    # 纯静态检查（读 css/js/html），不联网也不碰数据库
    ("主题与字号", "test_theme_font.py", 1),
    # 断网韧性：所有场景都注入了假网络，不依赖外网可达性（1 次即可）
    ("断网韧性", "test_offline_resilience.py", 1),
    ("故障隔离", "test_isolation.py", 2),
    # 这套纯本地（只读生产库 + 临时库），不联网，加进来不会因外网抽风误红
    ("日/周榜周期隔离", "test_bug_daily_weekly_date.py", 1),
    ("快捷方式与响应时间", "test_launcher.py", 2),
    ("端到端", "test_e2e.py", 2),
]

RESULT_RE = re.compile(r"通过\s*(\d+)\s*项.*?失败\s*(\d+)\s*项")
ALT_RE = re.compile(r"失败项：(\d+)")


def run_once(script: str) -> tuple[bool, str]:
    proc = subprocess.run(
        [str(PY), "-X", "utf8", str(ROOT / "tests" / script)],
        capture_output=True, text=True, timeout=1800, cwd=str(ROOT),
        encoding="utf-8", errors="replace",
    )
    out = proc.stdout + proc.stderr
    passed = failed = None

    m = RESULT_RE.search(out)
    if m:
        passed, failed = int(m.group(1)), int(m.group(2))
    else:
        # test_clean_readme.py 用的是另一种汇总格式（"失败项：N"）
        m2 = ALT_RE.search(out)
        if m2:
            passed, failed = out.count("  PASS  "), int(m2.group(1))

    if passed is None:  # 没解析出来，只能看退出码
        if proc.returncode == 0:
            passed = out.count("  PASS  ")
            failed = 0
        else:
            passed = out.count("  PASS  ")
            failed = max(1, out.count("  FAIL  "))

    return failed == 0, f"通过 {passed} / 失败 {failed}"


print("=" * 70)
print("gh-rank 全量测试")
print("=" * 70)

all_ok = True
summary_rows = []

for label, script, attempts in SUITES:
    ok = False
    detail = ""
    for i in range(attempts):
        ok, detail = run_once(script)
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {label:<20} 第 {i + 1}/{attempts} 次   {detail}")
        if ok:
            break
        if i < attempts - 1:
            print(f"         网络可能抖动，重试一次…")
            time.sleep(5)
    summary_rows.append((label, ok, detail))
    if not ok:
        all_ok = False

print()
print("=" * 70)
for label, ok, detail in summary_rows:
    print(f"  {'✓' if ok else '✗'}  {label:<20} {detail}")
print()
print("全部通过 ✓" if all_ok else "存在失败项 ✗ — 跑具体脚本看详细输出")
print("=" * 70)

sys.exit(0 if all_ok else 1)