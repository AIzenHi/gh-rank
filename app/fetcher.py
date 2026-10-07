"""模块 1：抓取 GitHub Trending 榜单。

GitHub 没有官方 Trending API，只能解析 https://github.com/trending 的 HTML。
用 BeautifulSoup 而不是正则，因为 GitHub 改版时正则会静默失效，
而 CSS class（article.Box-row）比标签结构稳定得多。
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import date
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .config import get_settings

log = logging.getLogger("gh-rank.fetcher")

TRENDING_URL = "https://github.com/trending"

# 重试策略：本机网络会间歇性抽风，而定时任务一天只有一次机会
RETRIES = 3
BACKOFF_BASE = 2.0  # 秒，指数退避 2s → 4s

# GitHub 偶尔会用全角空格或零宽字符分隔数字
_NUM_RE = re.compile(r"[\d,]+")


class FetchError(RuntimeError):
    """抓取榜单失败。"""


def _parse_int(text: str | None) -> int:
    if not text:
        return 0
    m = _NUM_RE.search(text.replace("\u00a0", " "))
    return int(m.group(0).replace(",", "")) if m else 0


def _client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        timeout=s.http_timeout,
        follow_redirects=True,
        headers={
            # 不伪装成浏览器也要给个真实 UA，否则 GitHub 会 403
            "User-Agent": "gh-rank/0.1 (+local personal dashboard)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def fetch_trending(period: str = "daily", language: str | None = None) -> list[dict[str, Any]]:
    """抓取榜单。

    period: daily | weekly | monthly

    带重试：实测本机网络会间歇性抽风（实测遇到过 ConnectTimeout 和
    WinError 10054 连接重置）。定时任务一天只有一次机会，
    失败就意味着当天的数据永久丢失 —— 所以这里必须重试。
    """
    if period not in ("daily", "weekly", "monthly"):
        raise ValueError(f"period 必须是 daily/weekly/monthly，收到 {period!r}")

    params: dict[str, str] = {"since": period}
    if language:
        params["spoken_language_code"] = language

    last_error: Exception | None = None
    started = time.time()

    for attempt in range(1, RETRIES + 1):
        try:
            with _client() as client:
                resp = client.get(TRENDING_URL, params=params)
                resp.raise_for_status()
                entries = parse_trending_html(resp.text, period)
                log.info(
                    "%s 榜抓取成功：%d 条，耗时 %.1f 秒",
                    period, len(entries), time.time() - started,
                )
                return entries
        except FetchError:
            raise  # 页面结构变了，重试也没用
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (429, 403):
                raise FetchError(
                    f"GitHub 返回 {code}，被限流了。等几分钟再试，或调大 HTTP_TIMEOUT"
                ) from exc
            last_error = exc
        except (httpx.HTTPError, OSError) as exc:
            # 必须兜住 OSError：连接被重置(WinError 10054)有时以
            # ConnectionResetError 的形式直接抛出，不一定是 httpx 包装过的类型。
            # 漏掉它，异常会一路逃到 pipeline，表现为「未预期错误」，
            # 还会把该周期后续的 README/说明环节全部打断。
            last_error = exc

        if attempt < RETRIES:
            # 指数退避 + 抖动。抖动很重要：daily 和 weekly 几乎同时发起，
            # 不加随机偏移的话失败后会同步重试，等于往这条已经很挤的链路上加倍砸。
            wait = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            time.sleep(wait)

    raise FetchError(
        f"重试 {RETRIES} 次仍失败（累计耗时 {time.time() - started:.0f} 秒）："
        f"{type(last_error).__name__} {last_error}"
    )


def parse_trending_html(html: str, period: str = "daily") -> list[dict[str, Any]]:
    """从 Trending 页面 HTML 中提取条目。独立成函数方便写测试。"""
    soup = BeautifulSoup(html, "lxml")
    articles = soup.select("article.Box-row")
    if not articles:
        raise FetchError(
            "页面里没找到 article.Box-row —— GitHub 很可能改了页面结构，"
            "需要更新 fetcher.py 里的选择器"
        )

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for article in articles:
        full_name = _extract_full_name(article)
        if not full_name or full_name in seen:
            continue
        seen.add(full_name)

        results.append(
            {
                "rank": len(results) + 1,
                "full_name": full_name,
                "url": f"https://github.com/{full_name}",
                "description": _clean(_extract_description(article)),
                "language": _clean(_extract_language(article)),
                "stars": _extract_total_stars(article),
                "forks": _extract_forks(article),
                "period_stars": _extract_period_stars(article, period),
                "topics": [],
                "homepage": None,
            }
        )

    return results


# --------------------------------------------------------------------------
# 单字段提取
# --------------------------------------------------------------------------

def _extract_full_name(article) -> str | None:
    h2 = article.select_one("h2")
    if not h2:
        return None
    link = h2.find("a", href=True)
    if not link:
        return None
    full_name = link["href"].strip("/").split("?")[0]
    return full_name if full_name.count("/") == 1 else None


def _extract_description(article) -> str | None:
    p = article.select_one("p")
    return p.get_text(" ", strip=True) if p else None


def _extract_language(article) -> str | None:
    el = article.select_one('[itemprop="programmingLanguage"]')
    return el.get_text(strip=True) if el else None


def _extract_total_stars(article) -> int:
    link = article.select_one('a[href$="/stargazers"]')
    return _parse_int(link.get_text() if link else None)


def _extract_forks(article) -> int:
    link = article.select_one('a[href$="/forks"]')
    return _parse_int(link.get_text() if link else None)


def _extract_period_stars(article, period: str) -> int:
    """榜单右侧的「1,853 stars today」这类文字。"""
    text = article.get_text(" ", strip=True)
    unit = {"daily": "today", "weekly": "this week", "monthly": "this month"}[period]
    m = re.search(rf"([\d,]+)\s+stars?\s+{re.escape(unit)}", text)
    return int(m.group(1).replace(",", "")) if m else 0


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    out = re.sub(r"\s+", " ", text).strip()
    return out or None


def today_str() -> str:
    return date.today().isoformat()
