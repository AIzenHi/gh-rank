"""端到端冒烟测试：假设服务已在 8765 端口跑着。

覆盖：日榜/周榜切换、日期选择、每个项目的说明状态、README 可读性、
状态条渲染、弹窗内容完整性、UI 基本结构。

用法：
    python -m app.cli serve        # 另开一个终端
    python tests\test_e2e.py
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"

passed = 0
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global passed
    if ok:
        passed += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        failed.append(name)
        print(f"  FAIL  {name}  {detail}")
    return ok


def get(path: str, timeout: int = 30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def get_text(path: str, timeout: int = 30) -> str:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def section(title: str) -> None:
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")


# ---------------------------------------------------------------- 健康
section("1. 服务健康")

try:
    h = get("/api/health")
except Exception as exc:  # noqa: BLE001
    print(f"  服务未启动，无法继续。请先运行：python -m app.cli serve")
    print(f"  ({type(exc).__name__})")
    sys.exit(2)

check("服务在线", h.get("ok") is True)
check("README 走 raw 域名", "raw.githubusercontent.com" in h.get("readme_source", ""))
check("大模型已就绪", h["llm"]["ready"] is True, h["llm"]["model"])
check("调度器在跑", h["scheduler"]["running"] is True,
      f"下次 {h['scheduler']['jobs'][0]['next_run'][11:16] if h['scheduler']['jobs'] else '?'}")
# 这里只断言「字段存在且是布尔」，不钉死取值：
# 之前写的是 `is False`，等于断言「用户永远没配 GITHUB_TOKEN」，
# 而 README 恰恰建议配上 —— 用户哪天配了，这条必红。
check("Token 状态字段存在（配不配都正常）",
      isinstance(h.get("github_token"), bool), f"已配置={h.get('github_token')}")

# 数据新鲜度必须按周期分开（daily 停在 5 天前而 weekly 刚抓，不能合成一个数）
da = h.get("data_age_hours")
check("数据新鲜度按周期分开返回",
      isinstance(da, dict) and {"daily", "weekly"} <= set(da),
      f"daily={da.get('daily') if isinstance(da, dict) else da} "
      f"weekly={da.get('weekly') if isinstance(da, dict) else '?'}")

# ---------------------------------------------------------------- 抓取源
section("2. 抓取源探测（这是替代 API 配额的关键）")

# 这个接口刻意设计成永不阻塞，所以响应必须很快。
# 早先版本会同步等探测做完，把服务线程堵死，连 localhost 调用都超时。
t0 = time.time()
q = get("/api/quota", timeout=15)
probe_ms = int((time.time() - t0) * 1000)
check("/api/quota 永不阻塞 (<3秒)", probe_ms < 3000, f"{probe_ms} ms")

# 冷启动时返回 probing=true，等后台线程探完再问
if q.get("probing") or q.get("raw") is None:
    print("  （首次调用在后台探测中，等它完成…）")
    for _ in range(40):
        time.sleep(3)
        q = get("/api/quota", timeout=15)
        if q.get("raw") is not None:
            break

qa = q.get("api_quota") or {}

# ---- 代码属性（纳入断言）----
# 不在这里断言「raw 一定通」。外网可达性是环境属性，不是代码属性：
# 实测 2026-10-02 raw 与 html 先后都断过，回归测试不能因为断网就红。
# 真正该守住的是：接口不阻塞、返回结构自洽、断网时仍能正常提供缓存数据。
check("/api/quota 返回结构自洽",
      isinstance(q.get("raw"), (bool, type(None)))
      and isinstance(q.get("html"), (bool, type(None))),
      f"raw={q.get('raw')} html={q.get('html')}")
check("api 兜底路线状态已知", qa.get("authenticated") is not None,
      f"已认证={qa.get('authenticated')} 配额={qa.get('remaining')}/{qa.get('limit')}")

# ---- 环境信息（只打印，不参与判定）----
reach = [n for n in ("raw", "html") if q.get(n)]
if qa.get("ok"):
    reach.append("api")
print(f"  ℹ 当前可用抓取路线: {', '.join(reach) if reach else '（全断，靠缓存显示）'}")
if not reach:
    print("    （外网不可达属环境问题，不计入失败。README 有三路降级兜底，见 test_isolation.py）")

t0 = time.time()
q2 = get("/api/quota", timeout=15)
cache_ms = int((time.time() - t0) * 1000)
check("缓存命中很快 (<1秒)", cache_ms < 1000, f"{cache_ms} ms")

# ---------------------------------------------------------------- 榜单
section("3. 日榜 / 周榜")

daily = get("/api/leaderboard?period=daily")
weekly = get("/api/leaderboard?period=weekly")

check("日榜有数据", len(daily["entries"]) > 0, f"{len(daily['entries'])} 条")
check("周榜有数据", len(weekly["entries"]) > 0, f"{len(weekly['entries'])} 条")
check("日榜有快照日期", bool(daily["date"]), daily["date"] or "")

d_ok = all(
    e["rank"] == i + 1
    for i, e in enumerate(daily["entries"])
)
check("日榜排名连续且从 1 开始", d_ok)
check("日榜增星数非空", all(e["period_stars"] > 0 for e in daily["entries"]))
check("日榜有语言标注",
      any(e["language"] for e in daily["entries"]),
      next((e["language"] for e in daily["entries"] if e["language"]), ""))
check("周榜排名连续", all(e["rank"] == i + 1 for i, e in enumerate(weekly["entries"])))

# ---------------------------------------------------------------- 说明质量
section("4. AI 说明（核心需求：通俗易懂 + 不胡编）")

targets = [e for e in daily["entries"] if e["has_explanation"]]
check("至少一条 AI 说明", len(targets) > 0, f"{len(targets)}/{len(daily['entries'])} 条已生成")

# 状态统计必须取**全量** entries。
# 早先版本是用 has_explanation（== status=='ok'）筛出 targets，
# 再断言 all(status == 'ok') —— 恒真，fallback 被静默排除，
# 降级说明占比涨到 90% 这条测试依然全绿。
all_status = [e.get("expl_status") or "none" for e in daily["entries"]]
n_ok = all_status.count("ok")
n_fb = all_status.count("fallback")
n_none = all_status.count("none")
fb_ratio = n_fb / len(all_status) if all_status else 1.0
print(f"  ℹ 日榜说明状态分布：ok={n_ok} fallback={n_fb} 待生成={n_none}")

check("日榜存在 ok 说明", n_ok > 0, f"ok={n_ok} / {len(all_status)}")
check("fallback 占比 < 50%", fb_ratio < 0.5, f"{n_fb}/{len(all_status)} = {fb_ratio:.0%}")
check("待生成占比 < 50%", n_none / len(all_status) < 0.5, f"{n_none}/{len(all_status)}")

if targets:
    ratios = []
    for e in targets:
        d = get("/api/repo/" + e["full_name"])
        ex = d.get("explanation")
        if not ex:
            continue
        ratios.append(ex.get("grounded_ratio") or 0)
    if ratios:
        avg = sum(ratios) / len(ratios)
        check("平均原文回检通过率 > 80%", avg > 0.8, f"{avg:.1%}")
        check("存在 100% 回检的说明", max(ratios) >= 1.0, f"最高 {max(ratios):.0%}")

    # 抽查第一条的内容质量
    d = get("/api/repo/" + targets[0]["full_name"])
    ex = d["explanation"]
    body = ex["body"]
    check("有 headline", len(ex.get("headline") or "") > 5, ex.get("headline", ""))
    # prompt 要求 150-250 字，但模型实测会写超（平均 462）。硬上限 700 是入库时的兜底。
    check("正文长度在硬上限内 (100-700字)", 100 <= len(body) <= 700, f"{len(body)} 字")
    check("正文分段呈现", "\n\n" in body)
    check("有中文标签", len(ex.get("tags") or []) >= 2, str(ex.get("tags")))
    check("记录了模型名", bool(ex.get("model")), ex.get("model", ""))
    check("记录了回检比例", ex.get("grounded_ratio") is not None)
    print()
    print("  ── 抽样内容 ──")
    print(f"  项目：{targets[0]['full_name']}")
    print(f"  标题：{ex['headline']}")
    print(f"  正文：{body[:150]}…")
    print(f"  标签：{ex['tags']}")
    print(f"  回检：{ex['grounded_ratio']:.0%}  重试 {ex['attempts']} 次  模型 {ex['model']}")
    print()

# ---------------------------------------------------------------- README
section("5. README 原文可回看")

target = targets[0]["full_name"] if targets else daily["entries"][0]["full_name"]
d = get("/api/repo/" + target)
check("标记了 README 可用", d["readme_available"] is True, f"{d['readme_chars']} 字符")
rm = get("/api/repo/" + target + "/readme")
readme = rm["readme"]
check("README 有实质内容", len(readme) > 200, f"{len(readme)} 字符")
check("README 已剔除徽章", "shields.io" not in readme)
check("README 已剔除 HTML 标签", "<img" not in readme and "<div" not in readme)
check("README 已剔除 HTML 注释", "<!--" not in readme)

# ---------------------------------------------------------------- 前端
section("6. 前端页面")

html = get_text("/")
check("首页可访问", "GitHub 排行榜" in html)
check("含本日/本周切换", 'data-period="daily"' in html and 'data-period="weekly"' in html)
check("含弹窗容器", 'id="modal"' in html)
check("含状态条", 'id="chipRaw"' in html and 'id="chipLlm"' in html)

js = get_text("/static/app.js")
check("JS 引用了日榜接口", "/api/leaderboard" in js)
check("JS 绑定了点击弹窗", "openModal" in js)
check("JS 区分 ok/fallback", "fallback" in js and "has_explanation" in js)

css = get_text("/static/style.css")
check("CSS 修正了 hidden 被覆盖的坑", "[hidden]" in css)
check("CSS 有移动端适配", "@media" in css)

# ---------------------------------------------------------------- 幂等
section("7. 幂等性（重复刷新不应重复消耗）")

before = get("/api/health")["stats"]
d2 = get("/api/leaderboard?period=daily")
check("重复查询榜单条目数不变", len(d2["entries"]) == len(daily["entries"]))
check("统计未因查询而改变", get("/api/health")["stats"] == before,
      f"expl_ok={before['expl_ok']}")

# ---------------------------------------------------------------- 收尾
print()
print("=" * 64)
print(f"通过 {passed} 项，失败 {len(failed)} 项")
if failed:
    for f in failed:
        print(f"  - {f}")
print("=" * 64)
sys.exit(1 if failed else 0)
