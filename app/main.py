"""FastAPI 入口：API + 静态页面。"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import models
from .config import get_settings
from .llm import LLMClient
from .pipeline import (
    PERIODS,
    backfill_explanations,
    catch_up_if_stale,
    is_running,
    refresh,
    try_claim,
)
from .readme import probe_sources
from .scheduler import STALE_HOURS, scheduler_info, shutdown_scheduler, start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gh-rank")


@asynccontextmanager
async def lifespan(app: FastAPI):
    models.init_db()
    start_scheduler()
    s = get_settings()
    log.info("=" * 62)
    log.info("gh-rank 已启动  →  http://127.0.0.1:8765")
    log.info(
        "README 抓取：raw 域名为主（不吃配额），GitHub Token %s",
        "已配置（作次选兜底）" if s.has_github_token else "未配置（不影响运行）",
    )
    log.info("大模型：%s（%s）", LLMClient().describe(), "就绪" if s.llm_ready else "未就绪")
    # 关机/断网导致的漏天在这里补上（阈值与看门狗、前端告警统一为 12 小时）
    catch_up_if_stale(max_age_hours=STALE_HOURS)
    # 抓取源探测提前在后台开跑，避免前端第一次问的时候干等
    _start_probe()
    log.info("=" * 62)
    yield
    shutdown_scheduler()


app = FastAPI(title="gh-rank", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    client = LLMClient()
    return {
        "ok": True,
        # Token 现在是可选项：raw 路线不吃配额
        "github_token": s.has_github_token,
        "readme_source": "raw.githubusercontent.com（无配额限制）",
        "llm": {"provider": client.provider, "model": client.model, "ready": s.llm_ready},
        "scheduler": scheduler_info(),
        "refreshing": is_running(),
        # 按周期分开返回：daily 停在 5 天前而 weekly 是刚抓的，
        # 合成一个数会让前端以为数据是新鲜的（实测踩过这个坑）
        "data_age_hours": {
            period: models.days_since_last_snapshot(period=period)
            for period in PERIODS
        },
        "stats": models.stats(),
    }


@app.get("/api/leaderboard")
def leaderboard(
    period: str = Query("daily", pattern="^(daily|weekly)$"),
    date: str | None = Query(None, description="YYYY-MM-DD，默认取最新"),
) -> dict[str, Any]:
    if period not in PERIODS:
        raise HTTPException(400, f"period 必须是 {PERIODS} 之一")
    data = models.get_leaderboard(period, date)
    data["available_dates"] = models.list_available_dates(period)
    return data


@app.get("/api/repo/{owner}/{name}")
def repo_detail(owner: str, name: str) -> dict[str, Any]:
    full_name = f"{owner}/{name}"
    repo = models.get_repo(full_name)
    if not repo:
        raise HTTPException(404, f"没找到仓库 {full_name}")

    expl = models.get_explanation(full_name)
    readme = repo.pop("readme", None)

    return {
        "repo": repo,
        "explanation": expl,
        "readme_available": bool(readme),
        "readme_chars": len(readme) if readme else 0,
    }


@app.get("/api/repo/{owner}/{name}/readme")
def repo_readme(owner: str, name: str) -> dict[str, Any]:
    full_name = f"{owner}/{name}"
    repo = models.get_repo(full_name)
    if not repo:
        raise HTTPException(404, f"没找到仓库 {full_name}")
    readme = repo.get("readme")
    if not readme:
        raise HTTPException(
            404,
            repo.get("readme_error") or "README 尚未抓取成功（可能缺 Token 或该仓库没有 README）",
        )
    return {"full_name": full_name, "readme": readme}


@app.post("/api/refresh")
def do_refresh(background: BackgroundTasks) -> dict[str, Any]:
    """手动触发刷新。耗时较长，放后台跑，前端轮询 /api/health 看进度。

    **先 try_claim() 抢锁、再排后台任务**。原来是
    `if is_running(): return` + `add_task`：检查和执行之间有窗口，
    两个并发请求都能拿到 `{"ok": true}`，其中一个随后被静默 skipped ——
    API 骗了用户，而且用户会以为刷新已经跑过了。
    """
    if not try_claim():
        return {"ok": False, "reason": "已有刷新任务在跑（刷新或补生成进行中）"}
    background.add_task(refresh, PERIODS, trigger="api", preclaimed=True)
    return {"ok": True, "message": "刷新已启动"}


@app.post("/api/explain/backfill")
def backfill(background: BackgroundTasks, limit: int = 20) -> dict[str, Any]:
    """补生成历史项目的说明。和刷新共用一把锁，避免同时烧两份 LLM 额度。"""
    if not try_claim():
        return {"ok": False, "reason": "已有任务在跑（刷新或补生成进行中）"}
    background.add_task(backfill_explanations, limit, preclaimed=True)
    return {"ok": True, "message": f"开始为最多 {limit} 个历史项目补生成说明"}


@app.get("/api/quota")
def quota() -> dict[str, Any]:
    """探测各条抓取路线通不通。

    **这个接口永不阻塞。** 探测要走真实外网，最坏几分钟；
    而它只是给前端显示状态用的，堵住服务线程会连带把 localhost 调用
    全拖死（实测踩过）。所以：
      - 启动时后台线程先探一遍
      - 缓存过期时立刻返回旧值，丢后台线程刷新
      - 缓存还是冷的就返回 probing=true，让前端显示「检测中」
    """
    return _get_probe()


_probe_lock = threading.Lock()
_probe_cache: dict[str, Any] | None = None
_probe_at = 0.0
_probe_refreshing = False
PROBE_TTL = 300.0  # 秒


def _get_probe() -> dict[str, Any]:
    global _probe_refreshing

    if _probe_cache is None:
        # 冷启动：还没有任何结果可返回，但绝不能在这里干等
        _start_probe()
        return {
            "raw": None,
            "html": None,
            "api_quota": None,
            "probing": True,
            "hint": "正在检测抓取源…",
        }

    if time.monotonic() - _probe_at < PROBE_TTL:
        return {**_probe_cache, "cached": True}

    # 过期：返回旧值，后台刷新
    _start_probe()
    return {**_probe_cache, "cached": True, "stale": True}


def _start_probe() -> None:
    global _probe_refreshing
    with _probe_lock:
        if _probe_refreshing:
            return
        _probe_refreshing = True
    threading.Thread(target=_run_probe, daemon=True, name="source-probe").start()


def _run_probe() -> None:
    global _probe_refreshing
    try:
        _refresh_probe()
    finally:
        with _probe_lock:
            _probe_refreshing = False


def _refresh_probe() -> dict[str, Any]:
    global _probe_cache, _probe_at
    try:
        data = probe_sources()
    except Exception as exc:  # noqa: BLE001
        log.warning("抓取源探测失败：%s", exc)
        data = {
            "raw": None,
            "html": None,
            "api_quota": None,
            "error": str(exc)[:200],
        }
    _probe_cache = data
    _probe_at = time.monotonic()  # 失败也更新时间戳，避免疯狂重试
    log.info("抓取源探测完成：raw=%s html=%s", data.get("raw"), data.get("html"))
    return data


@app.get("/api/logs")
def logs(limit: int = 10) -> dict[str, Any]:
    """最近几次刷新的执行记录（时间、抓到几个、新增几个、失败原因）。

    保留用途：**手动排查用**。前端不调它（app.js 里搜不到），
    但刷完一次想知道"到底抓到了什么、错在哪"时，
    直接 `curl http://127.0.0.1:8765/api/logs` 比翻控制台方便。
    """
    return {"items": models.recent_refreshes(limit)}


# --------------------------------------------------------------------------
# 静态页面
# --------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(get_settings().web_dir / "index.html")


app.mount("/static", StaticFiles(directory=get_settings().web_dir), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
