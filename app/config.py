"""集中读取配置。所有可调参数都在这里，别散落到各模块。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = Path(__file__).resolve().parent / "web"
DB_PATH = DATA_DIR / "rank.db"


@dataclass(frozen=True)
class Settings:
    github_token: str
    github_api_base: str

    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str

    refresh_hour: int
    max_repos_per_period: int
    readme_max_chars: int
    http_timeout: int
    llm_timeout: int

    db_path: Path
    web_dir: Path

    @property
    def has_github_token(self) -> bool:
        return bool(self.github_token)

    @property
    def llm_ready(self) -> bool:
        """mock 模式永远就绪；其他模式必须有 key。"""
        if self.llm_provider == "mock":
            return True
        if self.llm_provider == "ollama":
            return True
        return bool(self.llm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # 用官方的 python-dotenv（pyproject 里本来就声明了，只是全项目从没 import 过）。
    # 手写解析不支持行尾注释：用户写
    #     HTTP_TIMEOUT=30  # 30 秒够抓 trending
    # 的话 int("30  # ...") 抛 ValueError，然后 env_int 静默回落到默认 90 ——
    # 用户以为自己设了 30，而且永远不会有任何告警。
    # override=False 等价于原来的 os.environ.setdefault 语义（不覆盖已有环境变量）。
    load_dotenv(BASE_DIR / ".env", override=False)

    def env(key: str, default: str = "") -> str:
        return os.environ.get(key, default).strip()

    def env_int(key: str, default: int) -> int:
        try:
            return int(env(key) or default)
        except ValueError:
            return default

    provider = (env("LLM_PROVIDER", "mock") or "mock").lower()

    return Settings(
        github_token=env("GITHUB_TOKEN"),
        github_api_base=env("GITHUB_API_BASE", "https://api.github.com").rstrip("/"),
        llm_provider=provider,
        llm_base_url=env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        llm_api_key=env("LLM_API_KEY"),
        llm_model=env("LLM_MODEL", "deepseek-flash"),
        refresh_hour=max(0, min(23, env_int("REFRESH_HOUR", 9))),
        max_repos_per_period=max(1, env_int("MAX_REPOS_PER_PERIOD", 25)),
        readme_max_chars=max(2000, env_int("README_MAX_CHARS", 12000)),
        # 90 秒不是随便写的：实测本机抓一次 github.com/trending（640KB）
        # 成功也要 83 秒。设 30 秒的话 httpx 会判定 read timeout 而失败。
        http_timeout=max(10, env_int("HTTP_TIMEOUT", 90)),
        # LLM 超时独立配置。旧代码是 http_timeout * 3 —— 那是搭了
        # trending 调大后的便车，结果 LLM 单次要等 270 秒，
        # 25 个仓最坏 112 分钟。模型接口只要返回几百字，90 秒绰绰有余。
        llm_timeout=max(10, env_int("LLM_TIMEOUT", 90)),
        db_path=DB_PATH,
        web_dir=WEB_DIR,
    )
