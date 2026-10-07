"""复现 bug：日榜和周榜被同一个 MAX(snapshot_date) 卡住。

现象（2026-10-01 实测）：
  daily  最新快照 = 2026-09-27
  weekly 最新快照 = 2026-10-01
前端显示 2026-09-27，但实际数据里有 10-01 的周榜。

怀疑对象：get_leaderboard 里用 period 过滤的 MAX(snapshot_date)。

同一个根因还造成了第二个更隐蔽的 bug：**新鲜度也不分周期**。
SELECT MAX(fetched_at) FROM snapshots 不带 period 时，
daily 停 5 天 + weekly 刚抓 = 取到 weekly 的时间戳 → 看门狗以为数据新鲜，
永不补 daily。现在两处都必须按周期过滤，这里一并守住。

**全程不联网**（可达性是环境属性，交给 tests/check_network_quality.py），
也不写生产库 —— 断言在临时库里跑。
"""

import dataclasses
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402
from app.config import get_settings  # noqa: E402

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


print("=" * 64)
print("回归：日/周榜的日期与新鲜度必须按周期独立")
print("=" * 64)

# ---------------------------------------------------------------- 生产库现状
print("\n[1] 生产库现状（只读，不改）")
models.init_db()
with models.get_conn() as c:
    rows = c.execute(
        "select period, snapshot_date, count(*) n from snapshots "
        "group by period, snapshot_date order by snapshot_date desc"
    ).fetchall()
for r in rows:
    print(f"      {r['period']:<7} {r['snapshot_date']}  {r['n']} 条")

lb_daily_real = models.get_leaderboard("daily")
lb_weekly_real = models.get_leaderboard("weekly")
print(f"      get_leaderboard('daily')  -> {lb_daily_real['date']}")
print(f"      get_leaderboard('weekly') -> {lb_weekly_real['date']}")

with models.get_conn() as c:
    true_daily_real = c.execute(
        "select max(snapshot_date) d from snapshots where period='daily'"
    ).fetchone()["d"]

check("生产库：daily 取到的就是 daily 表里的最大日期",
      lb_daily_real["date"] == true_daily_real,
      f"接口={lb_daily_real['date']} vs 真实={true_daily_real}")

# ---------------------------------------------------------------- 临时库
print("\n[2] 在临时库里造出「两个周期日期不同」的场景做行为断言")

_TMPDIR = Path(tempfile.mkdtemp(prefix="ghrank-daily-weekly-"))
_real_settings = get_settings()
models.get_settings = lambda: dataclasses.replace(_real_settings, db_path=_TMPDIR / "rank.db")
models.init_db()

OLD_DAILY = "2026-09-27"
NEW_WEEKLY = "2026-10-02"
old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
new_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

with models.get_conn() as c:
    c.execute(
        "INSERT INTO repos (full_name, owner, name, url, stars, forks, "
        "first_seen_at, updated_at) VALUES ('acme/x','acme','x','u',1,1,?,?)",
        (new_ts, new_ts),
    )
    c.execute(
        "INSERT INTO snapshots (period, snapshot_date, rank, full_name, fetched_at) "
        "VALUES ('daily',?,1,'acme/x',?)",
        (OLD_DAILY, old_ts),
    )
    c.execute(
        "INSERT INTO snapshots (period, snapshot_date, rank, full_name, fetched_at) "
        "VALUES ('weekly',?,1,'acme/x',?)",
        (NEW_WEEKLY, new_ts),
    )

lb_daily = models.get_leaderboard("daily")
lb_weekly = models.get_leaderboard("weekly")
print(f"      get_leaderboard('daily')  -> {lb_daily['date']}")
print(f"      get_leaderboard('weekly') -> {lb_weekly['date']}")

check("日榜取到 daily 自己的最新日期", lb_daily["date"] == OLD_DAILY, str(lb_daily["date"]))
check("周榜取到 weekly 自己的最新日期", lb_weekly["date"] == NEW_WEEKLY, str(lb_weekly["date"]))
check("两个周期的最新日期确实不同（场景成立）",
      lb_daily["date"] != lb_weekly["date"],
      f"daily={lb_daily['date']} weekly={lb_weekly['date']}")
hist = models.get_leaderboard("daily", OLD_DAILY)
absent = models.get_leaderboard("daily", "2026-01-01")
check("指定日期能取到历史那一份",
      hist["date"] == OLD_DAILY and len(hist["entries"]) == 1,
      f"date={hist['date']} {len(hist['entries'])} 条")
check("不存在的日期返回空条目而不是报错", absent["entries"] == [])

# ---------------------------------------------------------------- 新鲜度
print("\n[3] 新鲜度也必须按周期分开")

age_daily = models.days_since_last_snapshot(period="daily")
age_weekly = models.days_since_last_snapshot(period="weekly")
age_all = models.days_since_last_snapshot()
print(f"      daily={age_daily} 小时  weekly={age_weekly} 小时  全部={age_all} 小时")

check("daily 的新鲜度没有被 weekly 掩盖",
      age_daily is not None and age_daily > 100, str(age_daily))
check("weekly 的新鲜度是新的",
      age_weekly is not None and age_weekly < 1, str(age_weekly))
check("不传 period 时取所有周期里最新的",
      age_all == age_weekly, f"{age_all} vs {age_weekly}")

need, why = models.needs_catchup(20.0)
check("daily 陈旧时仍判定需要补跑", need is True, why)
check("补跑原因里点明是哪个周期", "daily" in why, why)

# 单周期查询也可用
need_d, why_d = models.needs_catchup(20.0, periods=("daily",))
check("只查 daily 时判定为需要补跑", need_d is True, why_d)
need_w, why_w = models.needs_catchup(20.0, periods=("weekly",))
check("只查 weekly 时判定为新鲜", need_w is False, why_w)

shutil.rmtree(_TMPDIR, ignore_errors=True)

print()
print("=" * 64)
print(f"通过 {passed} 项，失败 {len(failed)} 项")
for f in failed:
    print(f"  - {f}")
print("=" * 64)
print("结论：" + ("未复现（已修）" if not failed else "确认是 BUG，需要修"))
sys.exit(1 if failed else 0)
