"""模块 3：让 AI 读 README，产出一段通俗生动的说明。

这个模块的核心不是「怎么让模型写得好」，而是「怎么让它不胡编」。

三层防线：
  1. Prompt 约束：明确只准用原文信息、禁止发明功能与性能数字。
  2. 关键词回检：要求模型吐出正文中出现的 5-8 个英文技术名词，
     程序再回到 README 里逐个查证。查证通过率低于阈值就判定为「疑似编造」。
  3. 降级兜底：两次都不过关，就用 README 首段拼一段标注清楚的兜底文案，
     宁可朴素也不放假货。

状态字段 status：pending（待生成）/ ok（AI 生成且通过回检）/ fallback（降级兜底）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .llm import LLMClient, LLMError
from .readme import extract_front_summary

# 回检通过率阈值：低于此值认为模型可能在编造
GROUNDED_THRESHOLD = 0.6

# 关键词少于这个数就不算有效样本。
# 只回检 1 个词时通过率必然是 0 或 1，没有任何判定力，
# 等于把「防编造」这道防线交给运气。
MIN_GROUNDING_KEYWORDS = 2

SYSTEM_PROMPT = """你是一位能把技术项目讲成故事的科技博主。你的读者有编程基础，但不一定了解这个项目所在的领域。

我会给你一个 GitHub 项目的 README 原文，你要写一段中文介绍。

铁律（违反就算失败）：
1. 只准使用原文中出现的信息。原文没写的功能、性能、兼容性、作者观点，一律不准编。
2. 不准写具体的性能数字、benchmark 结果、star 数，除非原文里明确写了。
3. 任何英文技术名词，必须在原文里原样出现过。
4. 语气生动、有画面感，可以打比方，但不要用「震撼」「颠覆」「最强」这类营销词。

输出要求：只输出一个 JSON 对象，不要任何解释文字或代码围栏。
{
  "headline": "一句话概括这个项目是干什么的，25 字以内，要有信息量，不要空话",
  "body": "正文，150-250 字，分 2-3 段。第一段说它解决什么问题，第二段说它怎么做到、有何特点。",
  "tags": ["3-5 个中文标签，每个 2-6 字"],
  "keywords": ["正文中出现过的 5-8 个英文技术名词，必须与 README 原文完全一致"]
}"""

RETRY_PROMPT = """你上一次的输出里有原文中不存在的技术名词，这属于编造，是不可接受的。

请重新写一遍，并且只使用下面这份「可用名词清单」里的词。
如果你想表达的概念不在清单里，就换一种说法，不要生造英文名。

可用名词清单：
{allowed}

同样只输出 JSON 对象，字段为 headline / body / tags / keywords。"""


@dataclass
class Explanation:
    status: str  # ok | fallback
    headline: str
    body: str
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    grounded_ratio: float = 0.0
    attempts: int = 0
    error: str | None = None
    model: str = ""


def _readme_vocab(readme: str) -> set[str]:
    """抽出 README 里出现过的「像技术名词」的大写/驼峰/带符号词。"""
    pattern = re.compile(r"[A-Za-z][A-Za-z0-9_+\-.]{1,24}")
    words = set()
    for w in pattern.findall(readme):
        if len(w) >= 2:
            words.add(w)
    return words


def _contains_word(word: str, readme: str) -> bool:
    """按**词边界**匹配，而不是整篇 README 上的子串搜索。

    之前是 `word.lower() in readme.lower()`，后果是回检几乎形同虚设：
      - 模型只吐 ["Go"]，而原文出现过 go → 通过率直接 1.0
      - AI / JS / DB / R 这类词天然命中，怎么写都过
      - 甚至 "Go" 会被 "Golang" 里的 "Go" 误判为命中
    """
    return bool(re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", readme, re.I))


def verify_grounding(body: str, keywords: list[str], readme: str) -> tuple[float, list[str]]:
    """回检：正文提到的技术名词是否真的存在于 README。

    返回 (通过率, 未通过的名词列表)。
    """
    if not keywords:
        # 模型没给关键词，退而求其次：检查正文里的驼峰/大写词
        candidates = [
            w
            for w in re.findall(r"\b[A-Z][A-Za-z0-9_+\-.]{2,24}\b", body)
            if w.lower() not in ("github", "readme", "api", "cli", "sdk", "json", "http")
        ]
        keywords = list(dict.fromkeys(candidates))[:8]

    cleaned: list[str] = []
    for kw in keywords:
        c = kw.strip().strip("`.,;:()[]")
        if c:
            cleaned.append(c)
    keywords = cleaned

    if len(keywords) < MIN_GROUNDING_KEYWORDS:
        # 样本不足：直接判不通过，别拿 0% 或 100% 去当"回检通过率"
        return 0.0, keywords

    passed, failed = 0, []
    for kw in keywords:
        if _contains_word(kw, readme):
            passed += 1
        else:
            failed.append(kw)

    total = passed + len(failed)
    return (passed / total if total else 0.0), failed


def generate(
    full_name: str,
    description: str | None,
    readme: str,
    *,
    client: LLMClient | None = None,
) -> Explanation:
    """主入口：读 README → 生成说明 → 回检 → 必要时降级。"""
    client = client or LLMClient()
    vocab = _readme_vocab(readme)

    user = _build_user_prompt(full_name, description, readme)
    last_err: str | None = None

    for attempt in (1, 2):
        # 铁律必须**每一次**都在 system 里。
        # 之前第二次尝试时用的是 `SYSTEM_PROMPT if attempt == 1 else _build_retry(...)`，
        # 等于把含 4 条铁律的 SYSTEM_PROMPT 整个换掉，只剩一句"请重新写一遍" ——
        # 恰恰在最容易被编造的那一次把防线撤了。
        # 而且旧实现返回的 `user + RETRY_PROMPT`（含 README 全文）被当成
        # **system 角色**传进去，等于把不可信的 README 提到最高优先级指令位。
        system = SYSTEM_PROMPT
        user_msg = user
        if attempt == 2:
            retry_text = _build_retry(vocab)
            system = f"{SYSTEM_PROMPT}\n\n{retry_text}"
            # README 正文留在 user 角色，不能进 system
            user_msg = f"{user}\n\n{retry_text}"

        try:
            raw = client.chat_json(
                system,
                user_msg,
                max_tokens=1800,
                temperature=0.6 if attempt == 1 else 0.3,
            )
        except LLMError as exc:
            if attempt == 2:
                fb = _fallback(full_name, description, readme, str(exc), attempt)
                fb.model = client.describe()
                return fb
            continue

        parsed = _coerce(raw)
        if parsed is None:
            if attempt == 2:
                fb = _fallback(full_name, description, readme, "模型输出结构不合法", attempt)
                fb.model = client.describe()
                return fb
            continue

        headline, body, tags, keywords = parsed
        ratio, failed = verify_grounding(body, keywords, readme)

        if ratio >= GROUNDED_THRESHOLD:
            return Explanation(
                status="ok",
                headline=headline,
                body=body,
                tags=tags,
                keywords=[k for k in keywords if _contains_word(k, readme)],
                grounded_ratio=ratio,
                attempts=attempt,
                model=client.describe(),
            )

        last_err = f"回检未通过（{ratio:.0%}），原文里找不到：{', '.join(failed[:5])}"

    fb = _fallback(full_name, description, readme, last_err, 2)
    fb.model = client.describe()
    return fb


# --------------------------------------------------------------------------

def _build_user_prompt(full_name: str, description: str | None, readme: str) -> str:
    desc_line = f"仓库简介（GitHub 上的一句话）：{description}\n" if description else ""
    return (
        f"项目：{full_name}\n"
        f"{desc_line}\n"
        f"以下是 README 原文：\n\n"
        f"---\n{readme}\n---\n\n"
        f"请按系统提示的 JSON 格式回答。"
    )


def _build_retry(vocab: set[str]) -> str:
    """重试指令（纯指令文本，不含 README）。

    调用方负责把它同时追加到 system（接在 SYSTEM_PROMPT 之后，铁律仍在）
    和 user 末尾（真正的指令所在），README 正文则始终留在 user 角色。
    """
    # 只给「像技术名词」的词，避免清单爆炸
    allowed = sorted(w for w in vocab if len(w) >= 3)[:120]
    return RETRY_PROMPT.format(allowed="、".join(allowed))


def _coerce(raw: dict) -> tuple[str, str, list[str], list[str]] | None:
    """把模型输出规整成 (headline, body, tags, keywords)，不合格返回 None。"""
    if not isinstance(raw, dict):
        return None

    headline = str(raw.get("headline") or "").strip()
    body = str(raw.get("body") or "").strip()
    if not headline or not body:
        return None
    if len(body) < 40:  # 太短说明模型敷衍了
        return None

    tags = raw.get("tags") or []
    keywords = raw.get("keywords") or []
    if isinstance(tags, str):
        tags = [tags]
    if isinstance(keywords, str):
        keywords = [keywords]

    return (
        headline[:80],
        body,
        [str(t).strip() for t in tags if str(t).strip()][:6],
        [str(k).strip() for k in keywords if str(k).strip()][:10],
    )


def _fallback(
    full_name: str,
    description: str | None,
    readme: str,
    error: str | None,
    attempts: int,
) -> Explanation:
    """降级兜底：用 README 首段，绝不展示空白。"""
    summary = extract_front_summary(readme) or (description or "")
    name = full_name.split("/")[-1]

    if summary:
        body = (
            f"{summary}\n\n"
            f"（以上内容直接摘自该项目的 README 原文。AI 说明暂未生成，"
            f"通常是因为大模型没配置好，或生成内容没通过防编造校验：{error or '未知原因'}）"
        )
        headline = f"{name}：{description}" if description else f"{name} 项目 README 摘要"
    else:
        body = (
            f"该项目没有可用的 README 内容，无法生成说明。"
            f"（原因：{error or '未知原因'}）"
        )
        headline = f"{name}：暂无法生成说明"

    return Explanation(
        status="fallback",
        headline=headline[:80],
        body=body,
        tags=["README 摘要"],
        keywords=[],
        grounded_ratio=0.0,
        attempts=attempts,
        error=error,
    )
