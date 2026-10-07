"""命令行入口。

用法：
  python -m app.cli serve            启动网页服务（默认）
  python -m app.cli refresh          手动刷新一次后退出（适合任务计划）
  python -m app.cli backfill         给历史项目补生成说明
  python -m app.cli status           查看当前状态与配额
  python -m app.cli fetch-only       只抓榜单，不调大模型（省钱调试用）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import get_settings


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gh-rank", description="本地 GitHub 排行榜")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("serve", help="启动网页服务")
    sub.add_parser("refresh", help="手动刷新一次后退出")
    sub.add_parser("backfill", help="给历史项目补生成说明")
    sub.add_parser("status", help="查看状态与配额")
    sub.add_parser("fetch-only", help="只抓榜单，不调大模型")

    p_backfill = sub.choices["backfill"]
    p_backfill.add_argument("--limit", type=int, default=30)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cmd = args.cmd or "serve"

    s = get_settings()

    if cmd == "serve":
        import uvicorn

        print(f"\n  gh-rank  →  http://127.0.0.1:8765\n")
        uvicorn.run("app.main:app", host="127.0.0.1", port=8765, log_level="info")
        return 0

    if cmd == "status":
        from . import models
        from .llm import LLMClient
        from .readme import probe_sources

        models.init_db()
        info = {
            "readme_source": "raw.githubusercontent.com（无配额限制）",
            "github_token": "已配置（api 路线兜底）" if s.has_github_token else "未配置（不影响运行）",
            "llm": f"{LLMClient().describe()} ({'就绪' if s.llm_ready else '未就绪 ⚠'})",
            "refresh_hour": s.refresh_hour,
            "db": str(s.db_path),
            "stats": models.stats(),
            "sources": probe_sources(),
        }
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0

    if cmd == "fetch-only":
        from . import models
        from .fetcher import fetch_trending, today_str

        models.init_db()
        total = 0
        for period in ("daily", "weekly"):
            entries = fetch_trending(period)[: s.max_repos_per_period]
            new = sum(1 for e in entries if models.upsert_repo(e))
            models.save_snapshot(period, today_str(), entries)
            total += len(entries)
            print(f"{period:8} 抓到 {len(entries):3} 个（新增 {new}）")
        print(f"合计 {total} 条，已写入 {s.db_path}")
        return 0

    if cmd == "refresh":
        from . import models
        from .pipeline import refresh

        models.init_db()
        if not s.has_github_token:
            print(
                "ℹ 未配 GITHUB_TOKEN。不影响运行 —— README 走 raw.githubusercontent.com，"
                "不吃 API 配额。Token 只是 api 路线的兜底。",
                file=sys.stderr,
            )
        result = refresh(trigger="cli")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    if cmd == "backfill":
        from . import models
        from .pipeline import backfill_explanations

        models.init_db()
        stats = backfill_explanations(args.limit)
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
