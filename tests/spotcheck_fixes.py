"""独立验证：抽查实施 Agent 的关键修复是否真的生效。

不信任报告，实测。这是抽查脚本，不是回归测试。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import readme as R  # noqa: E402

ok = 0
bad = []


def check(name, cond, detail=""):
    global ok
    if cond:
        ok += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        bad.append(name)
        print(f"  FAIL  {name}  {detail}")


print("=" * 64)
print("抽查实施修复")
print("=" * 64)

# ---------------------------------------------------------------- 阻塞 #1
print("\n[1] raw 域名被黑洞时，候选循环是否立刻跳出")

calls = {"n": 0}
_orig = httpx.Client.get


def blackhole(self, url, *a, **k):
    if "raw.githubusercontent.com" in str(url):
        calls["n"] += 1
        raise httpx.ConnectError("[WinError 10054] 黑洞")
    return _orig(self, url, *a, **k)


httpx.Client.get = blackhole
t0 = time.time()
try:
    R.fetch_readme("some/repo")
except Exception:
    pass
elapsed = time.time() - t0
httpx.Client.get = _orig

# 旧代码：17 候选 × 2 次 = 34 次请求 × 90s ≈ 51 分钟
check("网络故障时请求数被压到很小", calls["n"] <= 4, f"{calls['n']} 次（修复前会打 34 次）")
check("耗时被压到分钟级以下", elapsed < 30, f"{elapsed:.1f} 秒（旧代码约 51 分钟）")
print(f"      README 候选数 = {len(R.README_CANDIDATES)}，扫描超时 = {R.README_SCAN_TIMEOUT}s")

# ---------------------------------------------------------------- 阻塞 #2
print("\n[2] 新鲜度是否按周期区分")
from app import models  # noqa: E402

models.init_db()
h_all = models.days_since_last_snapshot()
h_d = models.days_since_last_snapshot(period="daily")
h_w = models.days_since_last_snapshot(period="weekly")
check("支持按周期查询", h_d is not None and h_w is not None,
      f"daily={h_d}h weekly={h_w}h all={h_all}h")

import inspect  # noqa: E402
src = inspect.getsource(models.needs_catchup)
check("needs_catchup 逐周期判断", "for p in periods" in src or "periods" in src)

# ---------------------------------------------------------------- 重要 #3
print("\n[3] 已有 ok 说明是否不会被空 body 覆盖")
import tempfile  # noqa: E402
import dataclasses  # noqa: E402

tmp = Path(tempfile.mkdtemp()) / "t.db"
orig_settings = models.get_settings


def patched():
    return dataclasses.replace(orig_settings(), db_path=tmp)


models.get_settings = patched
models._local = getattr(models, "_local", None)
try:
    models.init_db()
    models.upsert_repo({"full_name": "a/b", "url": "", "description": ""})
    models.save_explanation("a/b", "ok", headline="好标题", body="一段有用的正文",
                            tags=["t"], readme_hash="h1", model="m")
    # 模拟异常分支：只传 status 和 error，body/headline 全空
    models.save_explanation("a/b", "fallback", readme_hash="h1", model="m", error="boom")

    got = models.get_explanation("a/b")
    check("空 body 没有覆盖已有正文", got["body"] == "一段有用的正文", repr(got["body"]))
    check("空 headline 没有覆盖已有标题", got["headline"] == "好标题", repr(got["headline"]))
    check("状态仍更新为 fallback", got["status"] == "fallback", got["status"])
finally:
    models.get_settings = orig_settings
    try:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(tmp) + suffix)
            if p.exists():
                p.unlink()
    except Exception:  # noqa: BLE001
        pass

# ---------------------------------------------------------------- 重要 #4
print("\n[4] 重试时 system 是否保留铁律")
from app import explainer as E  # noqa: E402

seen = []


class Spy:
    ready = True

    def describe(self):
        return "spy"

    def chat_json(self, system, user, **kw):
        seen.append((system, user))
        # 第一次返回一个必然回检失败的关键词，触发重试
        return {"headline": "测试标题内容", "body": "正文" * 30,
                "tags": ["x"], "keywords": ["ZZZNotInReadme"]}


readme_text = "This is a real README with Go and JavaScript and Python code inside."
res = E.generate("a/b", "d", readme_text, client=Spy())

check("确实触发了第 2 次尝试", len(seen) == 2, f"{len(seen)} 次调用")
if len(seen) >= 2:
    s2 = seen[1][0]
    check("第 2 次 system 仍含铁律关键词",
          "铁律" in s2 or "只准使用原文" in s2)
    check("第 2 次 system 以完整 SYSTEM_PROMPT 开头",
          s2.startswith(E.SYSTEM_PROMPT))
    check("重试指令是追加而非替换", len(s2) > len(E.SYSTEM_PROMPT))
    check("README 正文不在 system 里", "real README with Go" not in s2)
    check("README 正文在 user 里", "real README with Go" in seen[1][1])

# ---------------------------------------------------------------- 重要 #8
print("\n[5] fallback 熔断是否生效")
import inspect as _i  # noqa: E402
src8 = _i.getsource(models.needs_explanation)
check("fallback 达阈值后不再重试", "FALLBACK_MAX_ATTEMPTS" in src8 or "attempts" in src8)
print(f"      阈值 = {getattr(models, 'FALLBACK_MAX_ATTEMPTS', '未定义')}")

# ---------------------------------------------------------------- 建议 #15
print("\n[6] dotenv 行尾注释")
import inspect as _i2  # noqa: E402
from app import config as C  # noqa: E402

src15 = _i2.getsource(C)
check("已改用 python-dotenv", "load_dotenv" in src15 and "def _load_dotenv" not in src15)

print()
print("=" * 64)
print(f"抽查通过 {ok} 项，失败 {len(bad)} 项")
for b in bad:
    print(f"  - {b}")
print("=" * 64)
sys.exit(1 if bad else 0)
