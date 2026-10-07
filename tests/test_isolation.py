"""回归测试：单个仓库失败不能拖垮整批，且周期之间不能互相串扰。

复现的 bug（2026-10-01 实测）：
  1. daily 榜单抓取成功，快照写入（fetched_at 15:43:28）
  2. 接着抓第 1 个仓库的 README 时连接被重置，抛出 httpx.ReadError
  3. 该异常不在 fetcher/readme 的捕获列表里，一路逃到周期级 handler
  4. 结果：日榜日期是新的，但 15 个新项目全卡在 readme=pending、expl=NONE
  5. 刷新日志还显示「抓到 0」，与实际写入的快照矛盾，极难排查

场景 3 守的是另一个 bug：daily 的错误被写进 weekly 的日志行
（之前两个周期共用一份 summary["errors"]），排查时严重误导。

**重要：本测试全程只写临时库**（tempfile.mkdtemp）——
早先版本直接把 6 个真实仓库的 README 全文清空，在一台会间歇性断网的
机器上一失败就永久丢失，生产数据就这么被测试毁掉的。

注意：场景 3 用**行为断言**（真的跑一遍 refresh，看 refresh_log 里两行
各自的 found / errors），不靠 inspect.getsource 去匹配源码文本 ——
把变量改名就红、用别的方式引入同样的 bug 照样绿，那种断言没有价值。
"""

import dataclasses
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import models, pipeline  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.fetcher import FetchError  # noqa: E402
from app.readme import ReadmeError  # noqa: E402

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


# --------------------------------------------------------------------------
# 临时库：绝不碰 data/rank.db
# --------------------------------------------------------------------------

_TMPDIR = Path(tempfile.mkdtemp(prefix="ghrank-isolation-"))
_TMP_DB = _TMPDIR / "rank.db"
_TMP_SETTINGS = dataclasses.replace(get_settings(), db_path=_TMP_DB)
# Settings 是 frozen dataclass + lru_cache，直接改不动；
# 换掉这两个模块里持有的引用即可（get_conn 是按 db_path 重建连接的）
models.get_settings = lambda: _TMP_SETTINGS
pipeline.get_settings = lambda: _TMP_SETTINGS
models.init_db()

# 素材仓库：写死几个几乎不可能消失的真实仓库，不依赖生产库里有没有。
# 早先版本是从生产库里查出来的，查不到就退化成"in ()"这种空 IN 子句 ——
# sqlite3.OperationalError，或者更糟：静默变成空跑、什么都没验证。
# 现在素材是常量 + 全参数化 SQL（根本不拼 IN 子句），空列表直接报错。
NAMES = [
    "torvalds/linux",
    "microsoft/vscode",
    "facebook/react",
    "rust-lang/rust",
    "python/cpython",
    "golang/go",
]
assert len(NAMES) >= 3, "素材仓库被清空了，后面的场景会变成空跑"

print("=" * 64)
print("回归测试：单仓库失败不拖垮整批（临时库，不碰生产数据）")
print("=" * 64)
print(f"  临时库 {len(NAMES)} 个素材: {', '.join(NAMES[:3])} …")
print(f"  {models.stats()}")

with models.get_conn() as c:
    for name in NAMES:
        owner, _, repo = name.partition("/")
        c.execute(
            "INSERT OR REPLACE INTO repos "
            "(full_name, owner, name, url, stars, forks, first_seen_at, updated_at) "
            "VALUES (?,?,?,?,1,1,?,?)",
            (name, owner, repo, f"https://github.com/{name}",
             models.utcnow(), models.utcnow()),
        )
    c.execute(
        "UPDATE repos SET readme=NULL, readme_hash=NULL, readme_status='pending'"
    )

# ---------------------------------------------------------------- 场景 1
print("\n[场景 1] 生产函数 ensure_readmes：第 2 个仓库抛未捕获的 httpx.ReadError")
_original = pipeline._ensure_readme
calls = {"n": 0}


def flaky(full_name: str):
    """第 2 个调用抛 httpx.ReadError —— 这正是实测漏掉的那个类型。"""
    calls["n"] += 1
    if calls["n"] == 2:
        raise httpx.ReadError("[WinError 10054] 远程主机强迫关闭了一个现有的连接")
    return _original(full_name)


pipeline._ensure_readme = flaky
try:
    # 直接调生产函数，测的是真实的隔离保证，不是测试自己写的循环
    ok_count, err_msgs = pipeline.ensure_readmes(NAMES)
finally:
    pipeline._ensure_readme = _original

check("ensure_readmes 全部处理完，没有被中断", calls["n"] == len(NAMES),
      f"实际处理 {calls['n']}/{len(NAMES)}")
check("批量循环：失败的仓库被单独兜住",
      ok_count + len(err_msgs) == len(NAMES),
      f"成功 {ok_count} 失败 {len(err_msgs)}")

with models.get_conn() as c:
    rows = {
        r["full_name"]: r["readme_status"]
        for r in c.execute("SELECT full_name, readme_status FROM repos")
    }
ok_rows = [k for k, v in rows.items() if v == "ok"]
err_rows = [k for k, v in rows.items() if v == "error"]
pending_rows = [k for k, v in rows.items() if v == "pending"]

check("其余仓库仍走到了各自流程（没有连坐中断）", len(ok_rows) >= 1,
      f"ok={len(ok_rows)} err={len(err_rows)}（err 含真实网络失败）")
check("注入失败的仓库被显式记为 error（可重试）",
      len(err_rows) >= 1, str(err_rows))
check("没有仓库卡在未处理的 pending", not pending_rows, str(pending_rows))

# ---------------------------------------------------------------- 场景 2
print("\n[场景 2] ReadError 能被 fetch_readme 转成业务异常")
import app.readme as readme_mod  # noqa: E402

original_get = httpx.Client.get


def raise_read_error(self, url, *a, **k):
    if "raw.githubusercontent.com" in str(url):
        raise httpx.ReadError("[WinError 10054] connection reset")
    return original_get(self, url, *a, **k)


httpx.Client.get = raise_read_error
try:
    with_readme = readme_mod.fetch_readme(NAMES[0])
    check("fetch_readme 在 ReadError 下仍返回内容（走了降级路线）",
          isinstance(with_readme, tuple) and len(with_readme[0]) > 0,
          f"{len(with_readme[0])} 字符")
except (readme_mod.ReadmeError, readme_mod.RateLimitError) as exc:
    check("fetch_readme 把 ReadError 转成了业务异常（不逃逸）", True, type(exc).__name__)
except Exception as exc:  # noqa: BLE001
    check("fetch_readme 把 ReadError 转成了业务异常（不逃逸）", False,
          f"逃出了 {type(exc).__name__}: {exc}")
finally:
    httpx.Client.get = original_get

# ---------------------------------------------------------------- 场景 3
print("\n[场景 3] 周期级错误不互相串扰（行为断言：看 refresh_log 里的两行）")

# 造一份只属于本测试的快照素材
FAKE_ENTRIES = [
    {
        "rank": 1,
        "full_name": "acme/alpha",
        "url": "https://github.com/acme/alpha",
        "description": "隔离测试用的假仓库",
        "language": "Python",
        "stars": 100,
        "forks": 10,
        "period_stars": 5,
        "topics": [],
        "homepage": None,
    },
    {
        "rank": 2,
        "full_name": "acme/beta",
        "url": "https://github.com/acme/beta",
        "description": "隔离测试用的假仓库",
        "language": "Go",
        "stars": 200,
        "forks": 20,
        "period_stars": 7,
        "topics": [],
        "homepage": None,
    },
]


def fake_daily_fails(period: str = "daily", language=None):
    if period == "daily":
        raise FetchError("注入：daily 抓取失败")
    return [dict(e) for e in FAKE_ENTRIES]


# --- 第 1 轮：daily 抓取失败，weekly 正常 ---
pipeline.fetch_trending = fake_daily_fails
res_a = pipeline.refresh(("daily", "weekly"), explain=False, trigger="iso-a")
print(f"  第 1 轮 -> ok={res_a['ok']} periods={res_a['periods']}")


def _rows(trigger: str) -> dict:
    with models.get_conn() as c:
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM refresh_log WHERE trigger = ? ORDER BY id", (trigger,)
            )
        ]
    return {r["period"]: r for r in rows}


def _errors(row) -> list[str]:
    return models._safe_json(row["errors"], []) if row else []


rows_a = _rows("iso-a")
daily_a, weekly_a = rows_a.get("daily"), rows_a.get("weekly")
err_daily_a, err_weekly_a = _errors(daily_a), _errors(weekly_a)

check("两个周期各留下一条刷新日志", len(rows_a) == 2, f"{len(rows_a)} 行")
check("daily 抓取失败 → 自己的日志行标记为失败", bool(daily_a and not daily_a["ok"]),
      f"ok={daily_a['ok'] if daily_a else '缺行'}")
check("weekly 成功 → 日志行 ok=1", bool(weekly_a and weekly_a["ok"]),
      f"ok={weekly_a['ok'] if weekly_a else '缺行'}")
check("weekly 的日志行 found>0", bool(weekly_a and weekly_a["found"] > 0),
      f"found={weekly_a['found'] if weekly_a else '缺行'}")
check("daily 的错误写进 daily 自己的日志行",
      any("daily" in e for e in err_daily_a), str(err_daily_a)[:90])
# 核心断言：daily 的错误绝不能出现在 weekly 的日志行里
check("weekly 的 errors 里不含 daily 的内容",
      not any("daily" in e for e in err_weekly_a), str(err_weekly_a)[:90])
check("weekly 成功时 errors 为空", err_weekly_a == [], str(err_weekly_a)[:90])

# --- 第 2 轮：快照已经写进去了，之后才炸（守「异常分支也必须传 found」）---
def boom_ensure_readmes(names):
    raise RuntimeError("注入：README 环节整段炸了")


pipeline.fetch_trending = lambda period="daily", language=None: [dict(e) for e in FAKE_ENTRIES]
_orig_ensure = pipeline.ensure_readmes
pipeline.ensure_readmes = boom_ensure_readmes
try:
    res_b = pipeline.refresh(("daily",), explain=False, trigger="iso-b")
finally:
    pipeline.ensure_readmes = _orig_ensure
print(f"  第 2 轮 -> ok={res_b['ok']} periods={res_b['periods']}")

rows_b = _rows("iso-b")
daily_b = rows_b.get("daily")
err_daily_b = _errors(daily_b)

check("快照写完后才失败的周期，日志行 found 仍 > 0",
      bool(daily_b and daily_b["found"] == len(FAKE_ENTRIES)),
      f"found={daily_b['found'] if daily_b else '缺行'}（实测漏传时会显示 0，"
      f"与已写入的快照矛盾）")
check("该周期日志行标记为失败", bool(daily_b and not daily_b["ok"]))
check("该周期 errors 记的是它自己的异常",
      any("ensure_readmes" in e or "未预期错误" in e for e in err_daily_b),
      str(err_daily_b)[:90])

# weekly 的快照确实写进去了（说明失败没有回滚掉已抓到的数据）
with models.get_conn() as c:
    n_weekly = c.execute(
        "SELECT COUNT(*) FROM snapshots WHERE period='weekly' AND full_name LIKE 'acme/%'"
    ).fetchone()[0]
check("weekly 快照已落库", n_weekly == len(FAKE_ENTRIES), f"{n_weekly} 行")

# ---------------------------------------------------------------- 场景 4
print("\n[场景 4] 新鲜度必须按周期分开判断")
with models.get_conn() as c:
    # 先清掉上一场景写的快照，否则 MAX(fetched_at) 会被它们盖住
    c.execute("DELETE FROM snapshots WHERE full_name LIKE 'acme/%'")
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
    new = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # daily 停在 5 天前，weekly 是刚抓的
    c.execute(
        "INSERT INTO snapshots "
        "(period, snapshot_date, rank, full_name, fetched_at) VALUES ('daily',?,?,?,?)",
        ("2026-09-27", 1, "acme/alpha", old),
    )
    c.execute(
        "INSERT INTO snapshots "
        "(period, snapshot_date, rank, full_name, fetched_at) VALUES ('weekly',?,?,?,?)",
        ("2026-10-02", 1, "acme/alpha", new),
    )

age_daily = models.days_since_last_snapshot(period="daily")
age_weekly = models.days_since_last_snapshot(period="weekly")
age_all = models.days_since_last_snapshot()
print(f"  daily={age_daily} 小时  weekly={age_weekly} 小时  全部={age_all} 小时")

check("daily 的新鲜度单独算（不是被 weekly 掩盖）",
      age_daily is not None and age_daily > 100, str(age_daily))
check("weekly 的新鲜度单独算", age_weekly is not None and age_weekly < 1, str(age_weekly))
check("不传 period 时取所有周期里最新的", age_all == age_weekly, f"{age_all} vs {age_weekly}")

need, why = models.needs_catchup(20.0)
check("daily 陈旧时仍判定需要补跑", need is True, why)
check("补跑原因里写明是哪个周期", "daily" in why, why)

need_ok, why_ok = models.needs_catchup(9999.0)
check("阈值放宽时不误触发", need_ok is False, why_ok)

# ---------------------------------------------------------------- 收尾
shutil.rmtree(_TMPDIR, ignore_errors=True)

print()
print("=" * 64)
print(f"通过 {passed} 项，失败 {len(failed)} 项")
for f in failed:
    print(f"  - {f}")
print("=" * 64)
sys.exit(1 if failed else 0)
