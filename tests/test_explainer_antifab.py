"""防编造机制的行为测试：重试时铁律还在 + 回检不会被单个关键词骗过。

复现的两个 bug：
  1. 重试时把 SYSTEM_PROMPT 整个换掉了 ——
     旧代码 `SYSTEM_PROMPT if attempt == 1 else _build_retry(user, readme, vocab)`，
     于是第二次尝试（恰恰是最容易编造的那次）只剩一句"请重新写一遍"，
     4 条铁律全没了；而且返回的 `user + RETRY_PROMPT`（含 README 全文）
     被当成 **system 角色**传进去，等于把不可信的 README 提到最高优先级指令位。
  2. 回检是整篇 README 上的子串搜索（`clean.lower() in haystack`）——
     模型只吐 ["Go"] 而原文出现过 go，通过率就是 1.0；
     AI / JS / DB 这类词天然命中，回检等于没有。

这两条都是**行为**：断言真的发出去的 system/user 消息，以及回检函数的判定，
不靠 inspect.getsource 去匹配源码文本（改个变量名就红、换个写法引入同样的
bug 照样绿，那种断言没有价值）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.explainer import (  # noqa: E402
    GROUNDED_THRESHOLD,
    SYSTEM_PROMPT,
    generate,
    verify_grounding,
)

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


class ScriptedClient:
    """按脚本返回一串 JSON，并记录每次发出去的 system / user。"""

    ready = True

    def __init__(self, replies: list[dict]):
        self._replies = replies
        self.calls: list[tuple[str, str]] = []

    def describe(self) -> str:
        return "test:model"

    def chat_json(self, system: str, user: str, **kw) -> dict:
        self.calls.append((system, user))
        return self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]


README = """# Distributed Queue

This project implements a **Kafka** compatible log with a **Go** client library.
It stores every event in a **SQLite** backed segment file, and replays them
in order when the process restarts.

## Install

    pip install distqueue
"""

# 铁律关键词：SYSTEM_PROMPT 里那条"铁律（违反就算失败）"和 4 条约束
IRON_LAW = "铁律"

print("=" * 64)
print("防编造机制测试：重试保铁律 + 回检按词边界")
print("=" * 64)

# ---------------------------------------------------------------- 场景 1
print("\n[场景 1] 第一次编造 → 重试：铁律必须还在，README 不能进 system")

fabricated = {
    "headline": "一个飞快的分布式队列",
    # Zephyr 与 Quartz 是原文里没有的词 → 第一次回检必然不通过
    "body": "它用 Zephyr 和 Quartz 协调节点，吞吐惊人，是每个后端工程师都该立刻上手的神器。",
    "tags": ["队列", "分布式"],
    "keywords": ["Zephyr", "Quartz"],
}
fixed = {
    "headline": "一个能重放事件日志的分布式队列",
    "body": "它实现了一个兼容 Kafka 的日志，每条事件都存在 SQLite 支撑的分段文件里，"
            "进程重启后按顺序重放，Go 客户端库也一并提供。",
    "tags": ["队列", "日志"],
    "keywords": ["Kafka", "SQLite", "Go"],
}

client = ScriptedClient([fabricated, fixed])
res = generate("acme/distqueue", "A distributed queue", README, client=client)

check("确实发起了两次请求（第一次被回检拦下）", len(client.calls) == 2,
      f"实际 {len(client.calls)} 次")

if len(client.calls) == 2:
    sys1, user1 = client.calls[0]
    sys2, user2 = client.calls[1]

    check("第 1 次 system 里有铁律", IRON_LAW in sys1)
    check("第 2 次（重试）system 里仍有铁律", IRON_LAW in sys2,
          sys2[:60] if IRON_LAW not in sys2 else "")
    for i, (s, u) in enumerate(client.calls, start=1):
        # README 的**正文**不许进 system。判断标准用整篇原文 + 一句特征散文，
        # 不能用 README 里出现过的词 —— 重试指令里的「可用名词清单」本来就
        # 是从 README 抽的词表（那是刻意设计，用来约束模型别生造英文名）。
        check(f"第 {i} 次 system 里没有 README 正文",
              README.strip() not in s and "in order when the process restarts" not in s)
        check(f"第 {i} 次 user 里带着 README 原文",
              "Distributed Queue" in u)

    # 重试指令要同时出现在 system 与 user（system 里必须接在铁律之后）
    check("重试 system 保留了完整 SYSTEM_PROMPT 前缀",
          sys2.startswith(SYSTEM_PROMPT), "system 被整体替换了")
    check("重试 system 追加了「可用名词清单」", "可用名词清单" in sys2)

check("重试后拿到的是 ok 说明", res.status == "ok", f"status={res.status}")
check("回检通过率 >= 阈值", res.grounded_ratio >= GROUNDED_THRESHOLD,
      f"{res.grounded_ratio:.0%}")
check("记录了 2 次尝试", res.attempts == 2, str(res.attempts))

# ---------------------------------------------------------------- 场景 2
print("\n[场景 2] 回检按词边界匹配（不能再被子串糊弄）")

# 「Go」不该被「Golang」里的 "go" 命中
ratio, failed_words = verify_grounding("用 Go 写的", ["Golang", "Kafka"], "We use Go here.")
check("Golang 不算命中原文里的 Go", ratio == 0.0,
      f"通过率={ratio:.0%} 未命中={failed_words}")

# 「JS」不该被「JavaScript」里的 "js" 命中
ratio, _ = verify_grounding("用 JS 写的", ["JS", "Rust"], "Written in JavaScript and Go.")
check("JS 不算命中原文里的 JavaScript", ratio == 0.0, f"通过率={ratio:.0%}")

# 正常词边界命中仍然要算过
ratio, failed_words = verify_grounding("用 Go 写的", ["Go", "Kafka"], "We use Go and Kafka.")
check("正常词边界命中算通过", ratio == 1.0, f"通过率={ratio:.0%} 未命中={failed_words}")

# 缩写形态：Kafka 不该被 "Kafkaesque" 之外的词误伤
ratio, _ = verify_grounding("基于 Kafka", ["Kafka", "SQLite"], "Built on Kafka with SQLite storage.")
check("Kafka / SQLite 正常命中", ratio == 1.0, f"通过率={ratio:.0%}")

# ---------------------------------------------------------------- 场景 3
print("\n[场景 3] 关键词少于 2 个 → 样本不足，直接判不通过")

ratio, words = verify_grounding("用 Go 写的", ["Go"], "We use Go here.")
check("只给 1 个关键词时不判通过", ratio == 0.0, f"通过率={ratio:.0%}")

ratio, words = verify_grounding("用 Go 写的", [], "We use Go here.")
check("一个关键词都没给时不判通过", ratio == 0.0, f"通过率={ratio:.0%}")

client1 = ScriptedClient([{
    "headline": "一个队列",
    "body": "只给一个关键词 Go，正文写得再漂亮也判不通过，"
            "因为一个词的通过率不是零就是一，样本不足根本没有判定力可言。",
    "tags": ["队列"],
    "keywords": ["Go"],
}])
res1 = generate("acme/distqueue", "d", README, client=client1)
check("单关键词输出被降级为 fallback", res1.status == "fallback", f"status={res1.status}")
check("降级原因写明回检未通过", "回检" in (res1.error or ""), str(res1.error)[:60])

# ---------------------------------------------------------------- 场景 4
print("\n[场景 4] 模型输出不合法时，system 也必须带铁律")
client2 = ScriptedClient([{"headline": "只有标题没有正文"}])
res2 = generate("acme/distqueue", "d", README, client=client2)
check("结构不合法 → 降级兜底", res2.status == "fallback", f"status={res2.status}")
check("降级正文不是空白", bool(res2.body and res2.body.strip()), res2.body[:50])
for i, (s, _u) in enumerate(client2.calls, start=1):
    check(f"第 {i} 次 system 里有铁律", IRON_LAW in s)

# ---------------------------------------------------------------- 收尾
print()
print("=" * 64)
print(f"通过 {passed} 项，失败 {len(failed)} 项")
for f in failed:
    print(f"  - {f}")
print("=" * 64)
sys.exit(1 if failed else 0)
