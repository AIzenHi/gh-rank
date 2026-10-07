"""可插拔的大模型客户端。

所有 provider 都走 OpenAI 兼容的 /chat/completions 协议，
DeepSeek、OpenAI、Ollama、one-api 网关都能用，换模型只改 .env。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .config import get_settings


class LLMError(RuntimeError):
    """模型调用失败。"""


_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_text(data: dict[str, Any]) -> str:
    """从响应里取出正文。

    content 为空但有 reasoning_content 时退回后者 —— 有些模型在 max_tokens
    用尽前把预算全花在思维链上，正式答案只在 reasoning 里（虽然不常见）。
    """
    try:
        choices = data["choices"]
    except (KeyError, TypeError) as exc:
        raise LLMError(f"响应里没有 choices：{str(data)[:200]}") from exc

    if not choices:
        raise LLMError(f"choices 为空：{str(data)[:200]}")

    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if content:
        return content

    reasoning = (message.get("reasoning_content") or "").strip()
    if reasoning:
        # 思维链里通常已经把 JSON 完整写了一遍
        start = reasoning.rfind("{")
        end = reasoning.rfind("}")
        if start != -1 and end > start:
            return reasoning[start : end + 1]
        return reasoning

    raise LLMError(
        "模型返回空内容。"
        f"（finish_reason={choices[0].get('finish_reason')!r}，"
        f"usage={data.get('usage')}）"
        "—— 若是 max_tokens 太小导致思维链吃光预算，请调大它或关闭 thinking"
    )


class LLMClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.provider = self.settings.llm_provider
        self.model = self.settings.llm_model
        # 有些 provider 不认 thinking 参数，遇到 400 就永久关掉它
        self._thinking_supported = not os.environ.get("LLM_NO_THINKING")

    @property
    def ready(self) -> bool:
        return self.settings.llm_ready

    def describe(self) -> str:
        return f"{self.provider}:{self.model}"

    # ------------------------------------------------------------------
    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 1800,
        temperature: float = 0.6,
    ) -> str:
        """发一轮对话，返回纯文本。

        注意 max_tokens 的坑：带思维链的模型（如 deepseek-flash）会把预算
        全花在 reasoning_content 上，content 直接返回空字符串。
        实测 900 会 100% 踩雷，所以这里默认给到 1800。

        超时用独立的 llm_timeout（默认 90 秒），不跟 HTTP_TIMEOUT 挂钩 ——
        后者是被 640KB 的 trending 页面撑到 90 的。
        """
        if self.provider == "mock":
            return self._mock_reply(user)

        if not self.ready:
            raise LLMError(
                f"LLM 未配置：provider={self.provider} 但缺少 LLM_API_KEY。"
                "请在 .env 里填写，或把 LLM_PROVIDER 设成 mock 先跑通流程"
            )

        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        url = f"{self.settings.llm_base_url}/chat/completions"

        try:
            with httpx.Client(timeout=self.settings.llm_timeout, headers=headers) as c:
                resp = c.post(url, json=self._payload(system, user, max_tokens, temperature))
                if resp.status_code == 400 and self._thinking_supported:
                    # provider 不认这个参数，去掉后重试
                    self._thinking_supported = False
                    resp = c.post(
                        url,
                        json=self._payload(system, user, max_tokens, temperature),
                    )
        except httpx.HTTPError as exc:
            raise LLMError(f"请求模型失败：{exc}") from exc

        if resp.status_code >= 400:
            raise LLMError(f"模型返回 {resp.status_code}：{resp.text[:300]}")

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(f"响应不是 JSON：{resp.text[:300]}") from exc

        return _extract_text(data)

    def _payload(
        self, system: str, user: str, max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if self._thinking_supported:
            # 顶层参数（不是 extra_body）。关掉思维链能省 token、快 4 倍，
            # 而且 max_tokens 有限时，不关就可能一个 content 字符都拿不到。
            payload["thinking"] = {"type": "disabled"}
        return payload

    def chat_json(self, system: str, user: str, **kw: Any) -> dict[str, Any]:
        """要求模型返回 JSON，并容错解析（去掉代码围栏、提取最外层对象）。"""
        raw = self.chat(system, user, **kw)
        return parse_json_loose(raw)

    # ------------------------------------------------------------------
    @staticmethod
    def _mock_reply(user: str) -> str:
        """不花钱的假模型，用来先验证「爬虫→定时→页面」全链路。"""
        name = "该项目"
        m = re.search(r"^#\s+(.+)$", user, re.MULTILINE)
        if m:
            name = m.group(1).strip()
        return json.dumps(
            {
                "headline": f"[MOCK] {name} —— 尚未接入真实模型",
                "body": (
                    "这是 mock 模式的占位说明，用来验证爬虫、定时刷新和前端弹窗是否正常。\n\n"
                    "把 .env 里的 LLM_PROVIDER 从 mock 改成 deepseek、填上 LLM_API_KEY，"
                    "重启后点击刷新，就能看到真正由大模型通读 README 生成的说明。"
                ),
                "tags": ["mock", "待接入模型"],
                "keywords": [],
            },
            ensure_ascii=False,
        )


def parse_json_loose(raw: str) -> dict[str, Any]:
    """从模型输出里尽最大努力抠出 JSON 对象。"""
    if not raw:
        raise LLMError("模型返回了空内容")

    text = _CODE_FENCE.sub("", raw.strip()).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 退而求其次：抓最外层的大括号
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型输出不是合法 JSON：{raw[:300]}") from exc

    raise LLMError(f"模型输出里找不到 JSON：{raw[:300]}")
