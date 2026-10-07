"""模块 2：抓取并清洗 README。

关键结论（实测，2026-09-26）：
  GitHub 的 60 次/小时配额只约束 api.github.com，跟网页域名完全是两套服务。
  所以 README 优先走 raw.githubusercontent.com —— 它不吃配额，还直接返回
  干净的 markdown 原文，不需要解析 HTML。

三路降级：
  1. raw.githubusercontent.com   首选。无配额限制，markdown 原文，按候选文件名依次试
  2. api.github.com             配了 Token 时的次选（读公开仓库的 README 够用）
  3. github.com 仓库页 HTML     兜底，抽 .markdown-body 的文本。丑但能救

实测命中率 11/12，平均每仓 1.0 次请求。
唯一抓不到的那一个，是仓库本身已经 404（改名/删库），任何路线都救不了。
"""

from __future__ import annotations

import hashlib
import re
import time
from contextlib import ExitStack
from typing import Any
from urllib.parse import quote

import httpx

from .config import get_settings

RAW_BASE = "https://raw.githubusercontent.com"
HTML_BASE = "https://github.com"

# README 候选文件名的单请求超时（秒）。
# 不能继承 HTTP_TIMEOUT=90 —— 那个数字是为 640KB 的 trending 页面定的
# （实测要 83 秒），拿来扫 200 字节的 README 纯属灾难：
# raw 域名被黑洞时，17 个候选 × 2 次重试 × 90 秒 ≈ 51 分钟只处理一个仓库。
# 15 秒足够「连不上就立刻放弃」，后续还有 api / html 两路兜底。
README_SCAN_TIMEOUT = 15.0

# 按命中概率排序。实测 README.md 覆盖约 90% 的仓库
README_CANDIDATES = (
    "README.md",
    "readme.md",
    "Readme.md",
    "README.MD",
    "README.rst",
    "readme.rst",
    "README.txt",
    "README",
    "README.markdown",
    "docs/README.md",
    "doc/README.md",
    "README.en.md",
    "README-EN.md",
    "README_zh.md",
    "README_CN.md",
    "README.en.rst",
)


class RateLimitError(RuntimeError):
    """GitHub API 配额耗尽（只可能发生在 api 那一路）。"""


class RepoGoneError(RuntimeError):
    """仓库 404 —— 改名或删库了，不是我们的问题。"""


class ReadmeError(RuntimeError):
    """三条路线都没拿到 README。"""


_BADGE_HOST = re.compile(
    r"(shields\.io|badge\.fury\.io|badgen\.net|travis-ci|circleci\.com|coveralls\.io|"
    r"codecov\.io|snyk\.io|opencollective\.com|appveyor|gitlab\.com/.+/badges)",
    re.I,
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# YAML front matter（GitHub 上很常见：mkdocs / hugo / 各类文档站）。
# 必须在「纯符号碎屑」清理**之前**剥掉：否则首行的 --- 被当成碎屑删掉，
# 键值行却留下来当正文，extract_front_summary 再把它当第一段摘要 ——
# 用户看到的降级兜底说明就变成一串 YAML（实测复现过）。
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", re.S)
_HTML_TAG = re.compile(r"<[^>]+>")
# markdown 图片 ![alt](url) 与 html <img ...>：对模型没有信息量，直接删
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# 删掉图片后，[![alt](img)](link) 会剩下 [](link) 这种空标签链接
_EMPTY_LINK = re.compile(r"\[\s*\]\([^)]*\)")
_HTML_IMG = re.compile(r"<img\b[^>]*>", re.I)
_TOC_LINK = re.compile(r"^\s*[-*]?\s*\[(?:contents|目录|table of contents)\]\(.*\)\s*:?\s*$", re.I)
# 删完图片后残留的 [](url)、===、--- 之类纯符号碎屑
_NO_WORD = re.compile(r"^[\W_]+$")
_MULTI_BLANK = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 抓取源探测：多数决所需的轮数与单请求超时
# 本机网络会间歇性抽风，所以要多轮投票；但探测要快速失败，不能慢慢等
PROBE_ROUNDS = 3
PROBE_TIMEOUT = 8.0

# 网络抖动重试。raw 域名实测偶发超时/连接重置，重试一次基本就好。
#
# 注意：这里必须用 httpx.HTTPError / OSError 这种「父类」，不能只列
# ConnectError、ReadTimeout 那几个子类。漏掉的 httpx.ReadError（连接被重置，
# WinError 10054）会一路逃出去，把整个刷新批次的 README 环节打断 ——
# 实测就是这么导致 15 个新项目全卡在 pending、说明全为 NONE 的。
_RETRYABLE = (httpx.HTTPError, OSError)


def _http(timeout: float | None = None) -> httpx.Client:
    """新建一个抓取用 client。

    timeout 不传时用配置里的 HTTP_TIMEOUT（trending 需要 90 秒）；
    扫 README 候选文件名时请显式传 README_SCAN_TIMEOUT。
    """
    return httpx.Client(
        timeout=timeout or get_settings().http_timeout,
        follow_redirects=True,
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


def fetch_readme(full_name: str) -> tuple[str, str]:
    """返回 (清洗后的 README 文本, 内容哈希)。三条路线全败则抛异常。"""
    problems: list[str] = []

    # 三条路线共用 client（原来每次迭代新建一个、迭代完还不关）：
    #   - 25 仓 × 2 周期 × 3 = 150 次 SSLContext 构造，白白重复读 CA bundle
    #   - 现在只建 2 个，且都用 with 显式关闭
    # raw 单独一个，因为只有它需要短超时（见 README_SCAN_TIMEOUT）。
    with ExitStack() as stack:
        raw_client = stack.enter_context(_http(timeout=README_SCAN_TIMEOUT))
        web_client = stack.enter_context(_http())

        for client, getter, label in (
            (raw_client, _via_raw, "raw"),
            (web_client, _via_api, "api"),
            (web_client, _via_html, "html"),
        ):
            try:
                text = getter(client, full_name)
            except RepoGoneError:
                # 仓库本身没了，换路线也没意义，直接失败
                raise
            except RateLimitError as exc:
                problems.append(f"{label}: {exc}")
                continue
            except ReadmeError as exc:
                problems.append(f"{label}: {exc}")
                continue
            except _RETRYABLE as exc:
                problems.append(f"{label}: 网络抖动 {type(exc).__name__}")
                continue

            cleaned = clean_readme(text, get_settings().readme_max_chars)
            if not cleaned.strip():
                problems.append(f"{label}: 内容清洗后为空（可能全是图片）")
                continue

            digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
            return cleaned, digest

    raise ReadmeError("三条路线都没拿到 README → " + "；".join(problems[:3]))


# --------------------------------------------------------------------------
# 路线 1：raw.githubusercontent.com（首选，无配额）
# --------------------------------------------------------------------------

def _via_raw(client: httpx.Client, full_name: str) -> str:
    owner, _, repo = full_name.partition("/")
    last_error = "候选文件名都没命中"
    # 候选全 404 时才值得再探一次仓库页，确认是不是改名/删库了。
    # 网络故障不是「文件名不存在」，探了也是白等一个超时。
    worth_probing = True

    for path in README_CANDIDATES:
        url = f"{RAW_BASE}/{owner}/{quote(repo)}/HEAD/{quote(path)}"
        try:
            resp = client.get(url)
        except _RETRYABLE:
            # 网络问题重试一次
            time.sleep(0.8)
            try:
                resp = client.get(url)
            except _RETRYABLE as exc:
                # **break 而不是 continue**：这是域名级故障，不是「这个文件名
                # 不存在」。换剩下 16 个文件名也是白打。
                # 之前是 continue，实测 raw 域名被黑洞时
                # 17 个候选 × 2 次重试 × 90 秒 ≈ 51 分钟只处理一个仓库。
                last_error = f"网络失败 {type(exc).__name__}"
                worth_probing = False
                break

        if resp.status_code == 404:
            continue  # 这个文件名不存在，换下一个
        if resp.status_code == 429:
            raise RateLimitError("raw 域名被限流（429），稍后再试")
        if resp.status_code >= 400:
            last_error = f"HTTP {resp.status_code}"
            continue

        text = resp.text
        if text.strip():
            return text
        last_error = "内容为空"

    # 候选全 404，仓库本身可能没了
    if worth_probing:
        try:
            probe = client.get(f"{HTML_BASE}/{owner}/{quote(repo)}")
            if probe.status_code == 404:
                raise RepoGoneError(f"{full_name} 已 404，可能改名或被删库了")
        except _RETRYABLE:
            pass

    raise ReadmeError(f"raw 未命中（{last_error}）")


# --------------------------------------------------------------------------
# 路线 2：api.github.com（有 Token 时的次选）
# --------------------------------------------------------------------------

def _via_api(client: httpx.Client, full_name: str) -> str:
    s = get_settings()
    if not s.has_github_token:
        raise ReadmeError("未配置 GITHUB_TOKEN，跳过")

    headers = {
        "Accept": "application/vnd.github.raw",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if s.has_github_token:
        headers["Authorization"] = f"Bearer {s.github_token}"

    try:
        resp = client.get(f"{s.github_api_base}/repos/{full_name}/readme", headers=headers)
    except _RETRYABLE as exc:
        raise ReadmeError(f"网络失败 {type(exc).__name__}") from exc

    if resp.status_code == 404:
        raise RepoGoneError(f"{full_name} 404，可能改名或被删库了")
    if resp.status_code == 403 and _is_rate_limited(resp):
        raise RateLimitError(
            "api.github.com 配额耗尽（未认证只有 60/小时）。"
            "这不影响主流程 —— raw 路线不消耗配额"
        )
    if resp.status_code >= 400:
        raise ReadmeError(f"GitHub 返回 {resp.status_code}")

    return resp.text


def _is_rate_limited(resp: httpx.Response) -> bool:
    if resp.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "rate limit" in resp.text.lower()


# --------------------------------------------------------------------------
# 路线 3：仓库页 HTML 兜底
# --------------------------------------------------------------------------

def _via_html(client: httpx.Client, full_name: str) -> str:
    owner, _, repo = full_name.partition("/")
    try:
        resp = client.get(f"{HTML_BASE}/{owner}/{quote(repo)}")
    except _RETRYABLE as exc:
        raise ReadmeError(f"网络失败 {type(exc).__name__}") from exc

    if resp.status_code == 404:
        raise RepoGoneError(f"{full_name} 已 404，可能改名或被删库了")
    if resp.status_code >= 400:
        raise ReadmeError(f"GitHub 返回 {resp.status_code}")

    text = _extract_markdown_body(resp.text)
    if not text.strip():
        raise ReadmeError("页面里没有 .markdown-body（该仓库可能确实没有 README）")
    return text


def _extract_markdown_body(html: str) -> str:
    """从仓库页 HTML 里抠出 README 正文。

    这里不追求还原 markdown 格式 —— 反正只喂给模型看散文，
    把 DOM 拍平成纯文本就够了。
    """
    from bs4 import BeautifulSoup  # 延迟导入，只在兜底路线用得上

    soup = BeautifulSoup(html, "lxml")
    box = soup.select_one(".markdown-body") or soup.select_one("article.markdown-body")
    if not box:
        return ""
    for junk in box.select("script, style, .anchor, .sr-only"):
        junk.decompose()
    text = box.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)


def clean_readme(text: str, max_chars: int = 12000) -> str:
    """把 README 削成适合喂给模型的干净文本。

    做的事：去 front matter、去 HTML 注释、去徽章行、去 HTML 标签、去目录、
    压缩空白、截断。截断时优先在段落边界切，避免把一句话劈成两半。
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n")
    text = _HTML_COMMENT.sub("", text)
    # front matter 要在碎屑清理之前剥（注释见 _FRONT_MATTER 定义）
    text = _FRONT_MATTER.sub("", text.lstrip("\n"))
    # 图片对纯文本模型没有信息量，且徽章最占地方 —— 先全部删掉
    text = _MD_IMAGE.sub("", text)
    text = _HTML_IMG.sub("", text)
    text = _EMPTY_LINK.sub("", text)

    kept: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if _TOC_LINK.match(stripped):
            continue
        if _BADGE_HOST.search(stripped):
            continue
        kept.append(line)

    text = "\n".join(kept)
    text = _HTML_TAG.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text)
    text = _MULTI_BLANK.sub("\n\n", text).strip()

    # 最后一遍：丢掉删完图片后只剩括号横线的碎屑行
    lines = [ln for ln in text.split("\n") if not _NO_WORD.match(ln.strip())]
    text = _MULTI_BLANK.sub("\n\n", "\n".join(lines)).strip()

    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    # 往回退到最近的段落边界，找不到就退到换行，再找不到就硬切
    for sep in ("\n\n", "\n", " "):
        idx = truncated.rfind(sep)
        if idx > max_chars * 0.6:
            return truncated[:idx].rstrip()
    return truncated.rstrip()


def extract_front_summary(cleaned: str, max_chars: int = 220) -> str:
    """从清洗后的 README 里抠一段人类可读的摘要，供降级兜底用。

    要点：段落常以标题开头（"## Features\\n真正重要的正文…"），
    所以先剥掉开头的标题行，只看正文，否则会误判成"标题段"而跳过。
    """
    for block in cleaned.split("\n\n"):
        lines = [
            ln for ln in block.strip().split("\n")
            if not ln.strip().startswith("#")
        ]
        body = "\n".join(lines).strip()
        if len(body) < 40:
            continue
        # 跳过命令块、表格、纯链接行
        if body.startswith(("```", "|", ">", "- ", "* ", "1. ")):
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]{4,}", body):
            continue
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "…"
        return re.sub(r"\s+", " ", body)
    return ""


def probe_sources() -> dict[str, Any]:
    """探测各条抓取路线通不通，供前端状态条显示。

    三个坑（都实测踩过）：
    - 用 HEAD 方法探 github.com 会超时 15s+，但 GET 只要 1s。所以必须用 GET。
    - 单次探测不足以判断可达性，网络抖动很常见。所以每个探 2 次，
      任一次成功就算通。
    - 再叠加「多数决」：本机网络会间歇性抽风，连续失败 2 轮才算真不通，
      否则会在断网边缘疯狂误报，把前端状态条搞得一闪一闪。
    """
    rounds: list[tuple[bool, bool]] = []

    # 探测专用的 client：超时要短。探测只是想知道"通不通"，
    # 等 30 秒才失败毫无意义，快速失败才是对的。
    with httpx.Client(
        timeout=PROBE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
    ) as client:
        for _ in range(PROBE_ROUNDS):
            rounds.append(
                (
                    _probe_twice(client, f"{RAW_BASE}/torvalds/linux/HEAD/README"),
                    _probe_twice(client, f"{HTML_BASE}/torvalds/linux"),
                )
            )
            if rounds[-1] != (True, True):
                time.sleep(0.8)  # 有失败就歇一下再试，别连续硬打

    # 多数通过就算通
    raw_ok = sum(1 for r, _ in rounds if r) > len(rounds) / 2
    html_ok = sum(1 for _, h in rounds if h) > len(rounds) / 2

    out: dict[str, Any] = {
        "raw": raw_ok,
        "html": html_ok,
        "api_quota": check_rate_limit(),
        "rounds": len(rounds),
    }
    return out


def _probe_twice(client: httpx.Client, url: str) -> bool:
    """探两次，任一次成功即视为可达。"""
    for _ in range(2):
        try:
            # 必须 GET：HEAD 探 github.com 实测会卡 15 秒以上
            if client.get(url).status_code < 400:
                return True
        except _RETRYABLE:
            # 兜父类而不是只兜 httpx.HTTPError —— 见上面 _RETRYABLE 的注释。
            # 之前这里只写 httpx.HTTPError，裸 OSError（WinError 10054 的
            # ConnectionResetError 就常这样抛）会一路逃出 probe_sources()，
            # 结果是前端永久显示「检测中…」。
            pass
    return False


def check_rate_limit() -> dict[str, Any]:
    """查询 api.github.com 配额。Token 非必需 —— 只影响 api 这条次选路线。"""
    s = get_settings()
    headers = {"User-Agent": _BROWSER_UA, "Accept": "application/vnd.github+json"}
    if s.has_github_token:
        headers["Authorization"] = f"Bearer {s.github_token}"
    try:
        with httpx.Client(timeout=10, headers=headers) as client:
            resp = client.get(f"{s.github_api_base}/rate_limit")
            resp.raise_for_status()
            core = resp.json()["resources"]["core"]
        return {
            "ok": True,
            "limit": core["limit"],
            "remaining": core["remaining"],
            "authenticated": s.has_github_token,
        }
    except Exception as exc:  # noqa: BLE001 - 配额查询失败不该影响主流程
        return {"ok": False, "error": str(exc), "authenticated": s.has_github_token}
