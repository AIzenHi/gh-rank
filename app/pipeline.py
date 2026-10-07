"""刷新流水线：把 fetcher / readme / explainer 串成一条可重入的流程。

为什么做成幂等：定时任务和手动点击可能同时触发，必须保证重复跑不会
产生重复数据、不会重复烧 LLM 额度。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from . import models
from .config import get_settings
from .explainer import generate
from .fetcher import FetchError, fetch_trending, today_str
from .llm import LLMClient
from .readme import RateLimitError, ReadmeError, RepoGoneError, fetch_readme

log = logging.getLogger("gh-rank.pipeline")

PERIODS = ("daily", "weekly")

# 防止定时任务和手动触发同时跑
_refresh_lock = threading.Lock()
_running = False


def is_running() -> bool:
    return _running


def try_claim() -> bool:
    """抢刷新锁。成功返回 True，调用方**必须**在任务结束后 release()。

    为什么单独暴露：main.py 原来是 `if is_running(): return` 再
    `background.add_task(...)` —— 检查和执行之间有窗口，
    两个并发请求都能拿到 `{"ok": true}`，其中一个随后被静默 skipped，
    **API 骗了用户**。改成「先占锁、占不到就明说原因」就没有窗口了。
    """
    global _running
    if not _refresh_lock.acquire(blocking=False):
        return False
    _running = True
    return True


def release() -> None:
    """释放刷新锁。与 try_claim 配对使用。"""
    global _running
    if _refresh_lock.locked():
        _running = False
        _refresh_lock.release()


def refresh(
    periods: tuple[str, ...] = PERIODS,
    *,
    trigger: str = "manual",
    explain: bool = True,
    preclaimed: bool = False,
) -> dict[str, Any]:
    """跑一次完整刷新。返回统计摘要。

    preclaimed=True 表示调用方已经 try_claim() 抢到锁了（HTTP 入口用的
    check-then-act 模式：先占锁再排后台任务）。
    """
    if not preclaimed and not try_claim():
        return {"ok": False, "skipped": True, "reason": "已有刷新任务在跑"}

    s = get_settings()
    client = LLMClient()
    summary: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "trigger": trigger,
        "date": today_str(),
        "periods": {},
        "total_found": 0,
        "total_new": 0,
        "explained": 0,
        "fallback": 0,
        "errors": [],
    }

    try:
        models.init_db()

        for period in periods:
            log_id = models.start_refresh(trigger, period)
            period_stats: dict[str, Any] = {"found": 0, "new": 0}
            # 每个周期独立收集错误。之前共用 summary["errors"]，
            # 导致 daily 的错误被写进 weekly 的日志行，排查时严重误导。
            period_errors: list[str] = []

            try:
                entries = fetch_trending(period)
                entries = entries[: s.max_repos_per_period]
                period_stats["found"] = len(entries)

                new_names: list[str] = []
                for e in entries:
                    if models.upsert_repo(e):
                        new_names.append(e["full_name"])

                models.save_snapshot(period, summary["date"], entries)
                period_stats["new"] = len(new_names)

                # 关键：不能只处理「首次见到」的仓库。
                # 之前失败过的（fetch-only 跑过、限流失败等）必须能被补上，
                # 否则它们会永远卡在 pending 状态，再也不会生成说明。
                # 快照本身已被 max_repos_per_period 限流，所以工作量有上界。
                todo = [e["full_name"] for e in entries]

                pending_readme = []
                for name in todo:
                    repo = models.get_repo(name)
                    if repo and repo.get("readme_status") in ("pending", "error"):
                        pending_readme.append(name)
                # 每个仓库单独兜住异常（保证不连坐）
                readme_ok, readme_errors = ensure_readmes(pending_readme)
                period_errors.extend(readme_errors)
                if readme_errors:
                    summary["errors"].extend(readme_errors)
                    summary["ok"] = False

                if explain:
                    todo_explain = []
                    for name in todo:
                        repo = models.get_repo(name)
                        if repo and repo.get("readme_status") == "ok":
                            if models.needs_explanation(name, repo.get("readme_hash")):
                                todo_explain.append(name)
                    period_stats.update(_explain_many(todo_explain, client))

                summary["total_found"] += period_stats["found"]
                summary["total_new"] += period_stats["new"]
                summary["explained"] += period_stats.get("explained", 0)
                summary["fallback"] += period_stats.get("fallback", 0)

            except (FetchError, RateLimitError) as exc:
                msg = f"[{period}] {exc}"
                log.warning(msg)
                period_errors.append(msg)
                summary["errors"].append(msg)
                summary["ok"] = False
                models.finish_refresh(
                    log_id, errors=period_errors, ok=False,
                    found=period_stats["found"], new_repos=period_stats["new"],
                )
                summary["periods"][period] = period_stats
                continue
            except Exception as exc:  # noqa: BLE001 - 单个周期失败不应中断整个刷新
                msg = f"[{period}] 未预期错误：{type(exc).__name__} {exc}"
                log.exception(msg)
                period_errors.append(msg)
                summary["ok"] = False
                # 必须带上 found：快照可能已经写进去了。
                # 之前这里漏传，日志显示「抓到 0」而快照其实已更新，
                # 排查时一度以为是日期逻辑串了，白查半天。
                models.finish_refresh(
                    log_id,
                    found=period_stats["found"],
                    new_repos=period_stats["new"],
                    explained=period_stats.get("explained", 0),
                    fallback=period_stats.get("fallback", 0),
                    errors=period_errors,
                    ok=False,
                )
                summary["periods"][period] = period_stats
                continue

            summary["periods"][period] = period_stats

            # 收尾写日志也必须兜住。写日志失败（磁盘满 / DB 锁）不该让
            # 整个 refresh 抛出去 —— 那会导致 weekly 根本没机会跑，
            # daily 的统计也全丢。跟上面「周期级隔离」是同一个保证。
            try:
                models.finish_refresh(
                    log_id,
                    found=period_stats["found"],
                    new_repos=period_stats["new"],
                    explained=period_stats.get("explained", 0),
                    fallback=period_stats.get("fallback", 0),
                    errors=period_errors,
                    ok=not period_errors,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] 刷新日志写入失败：%s: %s", period, type(exc).__name__, exc)
                summary["errors"].append(
                    f"[{period}] 刷新日志写入失败：{type(exc).__name__} {exc}"
                )
                summary["ok"] = False

    finally:
        release()

    return summary


def ensure_readmes(names: list[str]) -> tuple[int, list[str]]:
    """逐个抓 README，**单个失败绝不连坐其余**。返回 (成功数, 错误列表)。

    为什么要独立成函数：2026-10-01 实测的 bug 就是这里没兜住。
    第 1 个仓库抓 README 时连接被重置，异常一路逃到周期级 handler，
    后面 14 个仓库的 README 和说明全被跳过 ——
    结果是「榜单日期是新的，内容却是空的」，还极难排查。

    抽出来也是为了能直接单测这条保证，不用跑整条 refresh。
    """
    ok = 0
    errors: list[str] = []

    for i, name in enumerate(names, start=1):
        try:
            _ensure_readme(name)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - 目的就是兜住一切
            msg = f"{name} README 异常：{type(exc).__name__} {exc}"
            log.warning(msg)
            errors.append(msg)
            # 显式落库为 error，否则会永远卡在 pending 无人重试
            models.save_readme(
                name, None, None, "error", f"{type(exc).__name__}: {exc}"[:300]
            )
        log.info("README 进度 %d/%d  %s", i, len(names), name)

    return ok, errors


def _ensure_readme(full_name: str) -> tuple[str | None, str | None]:
    """抓 README 并入库，返回 (readme, hash)。

    状态取值：ok / error（网络问题，可重试）/ gone（仓库已 404，不再重试）。
    """
    try:
        readme, digest = fetch_readme(full_name)
        models.save_readme(full_name, readme, digest, "ok")
        return readme, digest
    except RepoGoneError as exc:
        # 仓库改名或删库了，重试也没意义，直接标终态
        log.info("仓库已消失 %s：%s", full_name, exc)
        models.save_readme(full_name, None, None, "gone", str(exc))
        return None, None
    except RateLimitError as exc:
        log.warning("README 抓取中止（限流）：%s", exc)
        models.save_readme(full_name, None, None, "error", str(exc))
        return None, None
    except ReadmeError as exc:
        models.save_readme(full_name, None, None, "error", str(exc))
        return None, None


def _explain_many(names: list[str], client: LLMClient) -> dict[str, int]:
    """批量生成说明。README 没抓到的直接跳过（不浪费 LLM 调用）。"""
    stats = {"explained": 0, "fallback": 0}
    if not names:
        return stats

    if not client.ready:
        log.warning("LLM 未配置，跳过 %d 个项目的说明生成", len(names))
        return stats

    for i, name in enumerate(names, start=1):
        repo = models.get_repo(name)
        if not repo or not repo.get("readme"):
            continue

        # attempts 必须**累加**上一轮的次数。
        # 之前每次都从 1 重新数，于是 models.needs_explanation 的
        # fallback 熔断（累计 >= 3 次）永远触发不了。
        prev = models.get_explanation(name) or {}
        prev_attempts = int(prev.get("attempts") or 0)

        try:
            result = generate(
                name,
                repo.get("description"),
                repo["readme"],
                client=client,
            )
            models.save_explanation(
                name,
                result.status,
                headline=result.headline,
                body=result.body,
                tags=result.tags,
                keywords=result.keywords,
                readme_hash=repo.get("readme_hash"),
                model=result.model,
                attempts=prev_attempts + int(result.attempts or 0),
                grounded_ratio=result.grounded_ratio,
                error=result.error,
            )
            if result.status == "ok":
                stats["explained"] += 1
            else:
                stats["fallback"] += 1
        except Exception as exc:  # noqa: BLE001 - 单个项目失败不应中断整批
            log.warning("生成说明失败 %s：%s", name, exc)
            # 绝不能把已有的合格说明抹成空白。
            # save_explanation 是 ON CONFLICT DO UPDATE 全字段覆盖，
            # 这里不传 headline/body/tags 就会把昨天花几毛钱生成的 ok 说明
            # 清成空 —— 一次瞬时异常就白烧一次钱。
            # 已有 ok 说明就整条跳过，保住上一版，等下次 refresh 再试。
            if prev.get("status") == "ok" and prev.get("body"):
                log.info("保留 %s 已有的合格说明，等下次刷新再试", name)
            else:
                stats["fallback"] += 1
                # 不传 headline/body：save_explanation 里那道
                # 「空正文不许覆盖已有正文」的保护会保住上一版内容
                models.save_explanation(
                    name, "fallback",
                    readme_hash=repo.get("readme_hash"),
                    model=client.describe(),
                    attempts=prev_attempts + 1,
                    error=str(exc),
                )

        log.info("说明进度 %d/%d  %s", i, len(names), name)

    return stats


def catch_up_if_stale(max_age_hours: float = 12.0) -> dict[str, Any]:
    """启动时检查数据是否过期，过期则补跑一次。

    为什么需要：电脑关机 / 睡眠 / 断网都会让当天的定时任务失手。
    实测 9-28 那次就是断网失败，那天数据至今没补上。
    开机时顺手补一下，用户不用记得手动点刷新。

    阈值默认 12.0，必须和 scheduler.STALE_HOURS、前端 STALE_HOURS 对齐 ——
    之前这里是 20.0、别处是 12.0，README 写的又是 12 小时，三方对不上。
    """
    models.init_db()
    need, reason = models.needs_catchup(max_age_hours)
    if not need:
        log.info("启动检查：%s，无需补跑", reason)
        return {"ran": False, "reason": reason}

    log.info("启动检查：%s → 触发补跑", reason)

    def _run() -> None:
        try:
            result = refresh(trigger="catchup")
            log.info(
                "补跑完成：抓到 %s，新增 %s，AI 说明 %s，降级 %s",
                result.get("total_found"),
                result.get("total_new"),
                result.get("explained"),
                result.get("fallback"),
            )
        except Exception:  # noqa: BLE001
            log.exception("补跑异常")

    threading.Thread(target=_run, daemon=True, name="catchup-refresh").start()
    return {"ran": True, "reason": reason}


def backfill_explanations(
    limit: int = 30,
    *,
    preclaimed: bool = False,
) -> dict[str, Any]:
    """给历史上已抓到 README 但还没说明的项目补生成。

    **必须和 refresh 共用同一把锁**：两者都会调 LLM 并写同一批 explanations，
    之前 backfill 完全不设 _running、不加锁 —— 点完 backfill 再点 refresh
    就两个一起跑，LLM 重复计费、结果互相覆盖。
    """
    if not preclaimed and not try_claim():
        return {"explained": 0, "fallback": 0, "skipped": 1, "reason": "已有任务在跑"}

    try:
        models.init_db()
        client = LLMClient()
        with models.get_conn() as conn:
            rows = conn.execute(
                """
                SELECT r.full_name FROM repos r
             LEFT JOIN explanations e ON e.full_name = r.full_name
                 WHERE r.readme_status = 'ok'
                   AND (e.full_name IS NULL OR e.status != 'ok')
                   -- 和 models.needs_explanation 同一道熔断：
                   -- 否则手点一次 backfill 就把已经放弃的项目重新烧一遍钱
                   AND NOT (e.status = 'fallback' AND e.attempts >= ?)
                 LIMIT ?
                """,
                (models.FALLBACK_MAX_ATTEMPTS, limit),
            ).fetchall()
        names = [r["full_name"] for r in rows]
        log.info("补生成说明：%d 个", len(names))
        return _explain_many(names, client)
    finally:
        release()
