"""模块 4：每日定时刷新。

两种用法：
  - 内置调度（默认）：进程常驻，APScheduler 每天 REFRESH_HOUR 点跑一次
  - 外部调度：python -m app.cli refresh --once 跑一次就退出，
    配 Windows 任务计划 / GitHub Actions 更省内存
"""

from __future__ import annotations

import logging
import time
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import models
from .config import get_settings
from .pipeline import is_running, refresh

log = logging.getLogger("gh-rank.scheduler")

# 数据超过这么多小时就算过期，触发补跑。
# 为什么要存在：2026-10-01 当天 daily 抓取连续失败，榜单停在 09-27，
# 而用户看到的只是"2026-09-27"，完全不知道数据已经 4 天没更新。
STALE_HOURS = 12.0

# 失败退避。外网持续不通时，看门狗每 30 分钟会烧
# 2 周期 × 3 次重试 × 90 秒 ≈ 9 分钟纯连接超时，永远如此、从不自愈。
# 失败后退避一小时，给链路喘息机会；成功就清零。
BACKOFF_MINUTES = 60.0
_backoff_until = 0.0  # monotonic 时钟，不受系统时间跳变影响

_scheduler: BackgroundScheduler | None = None


def in_backoff() -> bool:
    return time.monotonic() < _backoff_until


def _after_attempt(*, ok: bool) -> None:
    """按本次刷新的结果维护退避窗口。"""
    global _backoff_until
    if ok:
        if in_backoff():
            log.info("刷新成功，清除失败退避")
        _backoff_until = 0.0
    else:
        _backoff_until = time.monotonic() + BACKOFF_MINUTES * 60
        log.warning("刷新失败，退避 %.0f 分钟（期间定时任务与看门狗都会跳过）", BACKOFF_MINUTES)


def _can_run(label: str) -> bool:
    """两个 job 共用的前置闸门：正在跑 / 退避中就跳过。"""
    if is_running():
        log.info("%s跳过：已有刷新任务在跑", label)
        return False
    if in_backoff():
        log.info(
            "%s跳过：处于失败退避期，还剩 %.0f 分钟",
            label, (_backoff_until - time.monotonic()) / 60,
        )
        return False
    return True


def _job() -> None:
    if not _can_run("定时刷新"):
        return
    log.info("定时刷新开始")
    try:
        result = refresh(trigger="scheduled")
        # skipped 必须单独说。之前不管成功失败都按成功格式打印，
        # 结果被跳过的执行也打出「新项目 0，AI 说明 0」这种误导性日志。
        if result.get("skipped"):
            log.info("定时刷新跳过：%s", result.get("reason") or "已有任务在跑")
            return
        log.info(
            "定时刷新完成：新项目 %s，AI 说明 %s，降级 %s",
            result.get("total_new"),
            result.get("explained"),
            result.get("fallback"),
        )
        _after_attempt(ok=bool(result.get("ok")))
    except Exception:  # noqa: BLE001 - 定时任务绝不能因异常静默死掉
        log.exception("定时刷新异常")
        _after_attempt(ok=False)


def _watchdog_job() -> None:
    """每 30 分钟查一次数据新鲜度，过期就补跑。

    为什么需要这个：原本只在「启动时」检查漏天。但定时任务一天只有一次机会，
    而实测本机网络很抽风（10-01 当天 daily 连续失败，日榜停在 09-27）。
    光等第二天就等于那天没数据。
    """
    if not _can_run("看门狗"):
        return
    try:
        need, reason = models.needs_catchup(max_age_hours=STALE_HOURS)
        if not need:
            return
        log.info("看门狗：%s → 补跑刷新", reason)
        result = refresh(trigger="watchdog")
        if result.get("skipped"):
            log.info("看门狗补跑跳过：%s", result.get("reason") or "已有任务在跑")
            return
        log.info(
            "看门狗补跑完成：抓到 %s，AI 说明 %s，降级 %s",
            result.get("total_found"), result.get("explained"), result.get("fallback"),
        )
        _after_attempt(ok=bool(result.get("ok")))
    except Exception:  # noqa: BLE001
        log.exception("看门狗异常")
        _after_attempt(ok=False)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    s = get_settings()
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        _job,
        # 分钟数不只是"错开整点"：看门狗是 interval(minutes=30)，
        # 从进程启动时刻起算，可能正好抢在 cron 前面跑一轮长达几分钟的刷新，
        # 然后把 cron 自己挤掉（skipped）。挪到 :47 拉开窗口。
        CronTrigger(hour=s.refresh_hour, minute=47),
        id="daily-refresh",
        name="每日 GitHub 榜单刷新",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    # 看门狗：漏天自愈。每 30 分钟看一眼，别等到第二天
    scheduler.add_job(
        _watchdog_job,
        "interval",
        minutes=30,
        id="stale-watchdog",
        name="数据新鲜度看门狗",
        replace_existing=True,
        misfire_grace_time=1800,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    _scheduler = scheduler

    jobs = [
        {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
        for j in scheduler.get_jobs()
    ]
    log.info("调度器已启动（每天 %d:47，失败退避 %.0f 分钟），任务：%s",
             s.refresh_hour, BACKOFF_MINUTES, jobs)
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def scheduler_info() -> dict[str, Any]:
    if _scheduler is None:
        return {"running": False, "jobs": []}
    return {
        "running": True,
        "jobs": [
            {"id": j.id, "name": j.name, "next_run": str(j.next_run_time)}
            for j in _scheduler.get_jobs()
        ],
    }
