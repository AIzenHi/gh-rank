# gh-rank · 本地 GitHub 排行榜

每天自动抓 GitHub 趋势榜（日榜 + 周榜），并让大模型**通读每个项目的 README**，
自动生成一段通俗易懂、生动不装的 中文说明。点项目名即可弹出。

---

## 快速开始

```bash
# 1. 装依赖
cd gh-rank
uv venv
uv pip install "fastapi>=0.115" "uvicorn[standard]>=0.32" "httpx>=0.27" \
               "beautifulsoup4>=4.12" "lxml>=5.3" "apscheduler>=3.10" "python-dotenv>=1.0"

# 2. 配置
copy .env.example .env      # Windows
# 然后编辑 .env，填 GITHUB_TOKEN 和 LLM_API_KEY

# 3. 启动
.\.venv\Scripts\python.exe -m app.cli serve
```

打开 <http://127.0.0.1:8765> 。首次使用先点右上角「立即刷新」。

---

## 配置

### LLM_API_KEY（必填）

当前用 DeepSeek：

```ini
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-...
LLM_MODEL=deepseek-flash
```

`deepseek-flash` 便宜快；想要更好的文笔可以换 `deepseek-v4-pro`。
也支持任何 OpenAI 兼容接口（改 `LLM_BASE_URL` 即可），或本地 Ollama
（`LLM_PROVIDER=ollama`）。调试阶段想省钱，设 `LLM_PROVIDER=mock` 走假模型。

> ⚠ **思维链陷阱**：`deepseek-flash` 是带思维链的推理模型，会把 `max_tokens`
> 预算先花在 `reasoning_content` 上。`max_tokens=900` 时实测 **100% 返回
> 空 content**。本项目已在请求里带 `thinking:{"type":"disabled"}` 关掉它，
> 输出完整且快 4 倍、省掉约 2900 个白烧的 token。
> 换成不支持该参数的 provider 会自动降级（遇到 400 就摘掉重试）。

### GITHUB_TOKEN（可选）

**不配也能正常跑。** README 默认走 `raw.githubusercontent.com`，它不吃 API 配额。

`60 次/小时`那个限制只针对 `api.github.com`，跟网页域名是两套独立服务。
三路降级顺序：

| 顺序 | 路线 | 配额 | 拿到的内容 |
|---|---|---|---|
| 1 | `raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md` | **无限制** | markdown 原文，最干净 |
| 2 | `api.github.com/repos/…/readme` | 未认证 60/时 | markdown 原文 |
| 3 | `github.com/{owner}/{repo}` HTML | 无限制 | 拍平成纯文本，丑但能救 |

实测 12 个真实仓库：11 个一次命中，平均 1.0 次请求/仓。
没命中的那 1 个是仓库本身已 404（改名/删库），任何路线都救不了。

配置方法（仅当 raw 域名在你网络下不可达时才需要）：
<https://github.com/settings/tokens> → `Generate new token (classic)` → 勾 `public_repo`。
配额升到 5000 次/小时。**但主流程不依赖它。**

---

## 命令

```bash
python -m app.cli serve         # 启动网页 + 每日定时任务
python -m app.cli refresh       # 手动刷新一次后退出（配 Windows 任务计划用）
python -m app.cli fetch-only    # 只爬榜单，不调模型（省钱调试）
python -m app.cli backfill      # 给历史项目补生成说明
python -m app.cli status        # 看状态、统计、剩余配额
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务状态、模型配置、统计、数据新鲜度（**按周期分开**）、调度器信息 |
| GET | `/api/leaderboard?period=daily\|weekly&date=YYYY-MM-DD` | 榜单 |
| GET | `/api/repo/{owner}/{name}` | 仓库详情 + AI 说明 |
| GET | `/api/repo/{owner}/{name}/readme` | README 原文（已清洗） |
| GET | `/api/quota` | 探测各抓取路线通不通（**永不阻塞**，探测在后台跑） |
| POST | `/api/refresh` | 手动触发刷新（后台执行） |
| POST | `/api/explain/backfill?limit=20` | 补生成历史项目的说明 |
| GET | `/api/logs?limit=10` | 最近几次刷新的执行记录。**前端不调它**，手动排查用（`curl` 即可） |

`/api/refresh` 与 `/api/explain/backfill` **共用一把锁**：谁先抢到谁跑，
另一个立刻返回「已有任务在跑」。两者都会调 LLM 并写同一批表，
同时跑就是重复计费 + 结果互相覆盖。

---

## 界面偏好

### 字号（三档）

用户屏幕小，默认给**「大」**档（根字号 19.2px，正文约 18px）。顶栏可切：

| 档位 | `--font-scale` | 根字号 | 正文 |
|---|---|---|---|
| 标准 | 1.0 | 16px | 15px |
| **大（默认）** | 1.2 | 19.2px | 18px |
| 特大 | 1.4 | 22.4px | 21px |

快捷键 `+` / `-` 增减档位。选择持久化到 `localStorage`。

实现要点：**全站字号用 rem，根字号 = `calc(16px * var(--font-scale))`**，
所以改一个变量整个页面（含等宽字体、徽章、弹窗）同比缩放。
`tests/test_theme_font.py` 断言规则体里不许出现裸 `px` 字号，防止以后有人新加一处。

### 主题（夜间 / 浅亮）

顶栏 🌙 / ☀️ 按钮切换，快捷键 `T`。持久化到 `localStorage`。
浅色主题不是纯白（`#f4f6f8`），长时间看不刺眼；金色、银色、铜色名次都按主题单独调过
（白底上必须压暗才看得清）。

`index.html` 的 `<head>` 里有段内联脚本，在首屏渲染前就把主题和字号套上，
否则会先闪一下默认深色再跳变。

> **⚠ 颜色一律瞬时切换，不要加颜色 transition。**
> 踩过的坑：原本 `.rank-item` 等都有 `transition: background .15s`，
> 以为切主题时淡入淡出更顺滑。实测证明颜色过渡在 tab 不在前台时
> **会永久停在起始值** —— 强制把 `--bg-soft` 改成 `#ff00ff`，
> 绘制出来的背景仍是旧色。表现是「点了切主题，页面变浅了但卡片还是深色」，
> 半截状态比不过渡糟糕得多。现在只保留 `transform` / `opacity` 动画。
> `tests/test_theme_font.py` 会守住这条。

## 一键启动

桌面已放好 **「GitHub 排行榜」** 快捷方式，双击即可。它会：

1. 服务已在跑 → 直接开浏览器（重复点不会起一堆进程，实测 PID 不变）
2. 服务没跑 → 后台拉起，等端口就绪（约 10 秒）后自动开浏览器
3. 缺 `.env` / 缺依赖 / 缺 API Key → 弹窗告诉你具体缺什么、怎么补

**要求 PowerShell 脚本带 UTF-8 BOM**。Windows PowerShell 5.1 会把无 BOM 的 `.ps1`
按 GBK 读，中文乱码会破坏字符串引号导致解析失败（这个坑踩过一次）。

---

## 抗网络抖动

**这台机器的网络会间歇性抽风** —— 实测遇到过 `ConnectTimeout`、`WinError 10054`
连接重置，2026-09-28 那天的定时任务就是这么失败的，那天数据至今没补上。
所以有几处专门为此设计：

| 机制 | 作用 |
|---|---|
| `HTTP_TIMEOUT=90` | 匹配实测 83 秒的传输耗时（设 30 秒会误判 read timeout） |
| `README_SCAN_TIMEOUT=15` | 扫 README 候选文件名单独用短超时。raw 域名被黑洞时，17 个候选 × 2 次重试 × 90 秒 ≈ **51 分钟只处理一个仓库** |
| `LLM_TIMEOUT=90` | 模型接口独立超时，不跟 `HTTP_TIMEOUT` 挂钩（旧代码是 ×3，最坏 112 分钟） |
| `fetch_trending` 重试 3 次（指数退避 + 抖动） | 抖动防止 daily/weekly 同步重试加倍砸链路 |
| `ensure_readmes()` 逐个兜异常 | 单仓库失败不连坐整批 |
| 启动时 `catch_up_if_stale(12)` | 开机发现数据超过 12 小时就自动补跑 |
| **看门狗每 30 分钟** | 定时任务一天只有一次机会，失败当天得有第二次 |
| **失败退避 1 小时** | 外网持续不通时不无限重试，失败后退避、成功清零 |
| 前端过期告警横幅（按周期判断） | 数据超 12 小时显式告警，daily 陈旧不会被 weekly 的新鲜度掩盖 |
| `/api/quota` 永不阻塞 | 探测走真实外网最坏几分钟，同步等会**堵死服务线程** |
| 探测 8 秒短超时 + 3 轮多数决 | 快速失败；单次成败在这条网络下不可信 |
| `/api/health` 返回 `data_age_hours` | **按 daily/weekly 分开**返回，一眼看出哪个周期不新鲜 |

**别把网络探测放在同步请求路径上** —— 这是本次踩的最大的坑。
**兜底要兜父类，不要枚举子类** —— `_RETRYABLE = (httpx.HTTPError, OSError)`；
枚举具体子类必然漏，漏掉 `httpx.ReadError` 就会把整批 README 打断。
**分不清「这个名字不存在」和「连不上」** —— 404 才换下一个候选文件名，
网络异常必须直接跳出，换 16 个文件名也是白打。

### 三条抓取路线的实际取舍

| 场景 | README 质量 |
|---|---|
| raw 通（主路线） | **最好** —— 干净 markdown，实测 28k 字符 |
| raw 不通 → HTML 兜底 | 明显更薄 —— 拍平成纯文本，实测约 2k 字符 |
| 配了 Token → api 兜底 | 好 —— 干净 markdown，但受 60 次/小时限制 |

所以 **raw 不通时说明质量会下降**。想稳就配一个 GitHub Token（免费，5 分钟申请），
`api` 路线会顶上来。

---

## 防编造机制

大模型天生爱脑补 README 里没有的功能。这个项目用三层防线压制它：

1. **Prompt 铁律** —— 明确要求只用原文信息、禁止发明功能和性能数字、
   英文技术名词必须原文出现过。**重试时铁律仍在**：
   第二次尝试是把重试指令**追加**在 `SYSTEM_PROMPT` 之后，不是替换它；
   README 原文也始终留在 user 角色，绝不进 system（不可信内容不能占最高优先级指令位）。
2. **关键词回检** —— 要求模型吐出正文中出现的 5-8 个英文技术名词，
   程序回到 README 里**按词边界**逐个查证（`Golang` 里的 `Go` 不算命中原文的 `Go`；
   `JS` 也不算 `JavaScript`）。关键词少于 2 个直接判不通过（样本不足没有判定力）。
   通过率 < 60% 判定为疑似编造，自动带着「可用名词清单」重试一次。
3. **降级兜底** —— 两次都不过关，就用 README 首段拼一段标注清楚的摘要，
   绝不显示空白。前端会明确标出「⚠ 摘要兜底」而非伪装成 AI 内容。

另外两道省钱/保数据的护栏：

- **瞬时异常不许抹掉已有说明** —— 异常分支不传 `body`，且
  `save_explanation` 里 `body` 为空不允许覆盖已有正文。
  否则一次网络抖动就能把昨天花几毛钱生成的 `ok` 说明清成空白。
- **fallback 熔断** —— 累计尝试 ≥ 3 次且 README 没变就不再自动重烧 LLM
  （旧实现对 fallback 永远返回「需要重试」，
  一个回检稳定不过关的项目会在一天 2~6 次 refresh 里反复烧钱，永远不收敛）。

每条说明都记录 `status`（`ok` / `fallback`）、`grounded_ratio`（回检通过率）、
`model`、`attempts`，前端可见，不存在「黑箱生成」。

## 成本

日榜 + 周榜去重后约 20~40 个新项目/天，README 清洗后平均 6k~10k token。

| 模型 | 单价（输入/输出 $/M） | 每天 | 每月 |
|---|---|---|---|
| `deepseek-flash` | ~0.1 / ~0.4 | < $0.05 | < $1.5 |
| `deepseek-v4-pro` | 较高 | ~$0.2 | ~$6 |

---

## 目录结构

```
gh-rank/
├── pyproject.toml
├── .env                    # 密钥（已 gitignore）
├── app/
│   ├── config.py           # 集中配置（用 python-dotenv 读，支持行尾注释）
│   ├── models.py           # SQLite：repos / snapshots / explanations / refresh_log
│   ├── fetcher.py          # ① 爬 Trending（BeautifulSoup，解析失败会明确报错）
│   ├── readme.py           # ② 取 README + 清洗（实测省 55% token）
│   ├── llm.py              # 可插拔 LLM 客户端（OpenAI 兼容协议）
│   ├── explainer.py        # ③ AI 生成 + 关键词回检 + 降级兜底
│   ├── pipeline.py         # 串起来，幂等 + 抢锁防并发
│   ├── scheduler.py        # ④ APScheduler 每日 9:47 + 每 30 分钟看门狗
│   ├── main.py             # FastAPI
│   ├── cli.py              # 命令行
│   └── web/                # 原生 HTML/CSS/JS，无构建步骤
├── tests/
│   ├── run_all.py                # 全量测试汇总（回归就跑这个）
│   ├── check_network_quality.py  # 外网可达性诊断（断网红不算回归）
│   ├── test_clean_readme.py      # README 清洗
│   ├── test_explainer_antifab.py # 防编造：重试保铁律 + 回检按词边界
│   ├── test_offline_resilience.py# 断网韧性
│   ├── test_isolation.py         # 故障隔离（全程临时库，不碰生产数据）
│   ├── test_bug_daily_weekly_date.py # 日/周榜日期与新鲜度按周期隔离
│   ├── test_launcher.py          # 快捷方式 + 接口响应时间
│   ├── test_e2e.py               # 端到端（需要服务在 8765 跑着）
│   └── test_probe_cache.py       # 抓取源探测缓存
└── data/rank.db            # 自动创建
```

## 设计要点

- **README 必须缓存**。同一个仓库只抓一次，命中后永久复用。
- **抓取源三路降级**，主路线 raw 域名不吃配额，Token 只是兜底（见上）。
- **候选 README 文件名表**：`README.md` 覆盖约 90%，但会遇到 `README`（无扩展名，
  如 torvalds/linux）、`README.rst`、`docs/README.md` 等，所以要依次试。
  但只有 404 才换下一个 —— 网络异常直接跳出这一路。
- **定时任务错开整点**（9:47 而非 9:00），避开全球任务高峰，
  也和看门狗的 30 分钟窗口拉开距离，且设了 `coalesce` + `max_instances=1`，
  补跑不会叠加。
- **刷新幂等**：`snapshots` 用 `(period, date, full_name)` 唯一约束，
  同日重跑先删后插，不会产生重复。
- **一切写操作都按周期独立收集错误**：daily 失败不能写进 weekly 的日志行
  （早期共用一份 `errors`，排查时严重误导）。
- **不只处理新仓库**：按「当前快照里所有待办」处理，
  所以之前失败过的项目也能被补上，不会永远卡在 pending。
- **README 变了就重新生成说明**：`explanations.readme_hash` 与
  `repos.readme_hash` 不一致时视为过期。
- **仓库消失要能认出来**：404 标记为 `gone` 终态，不再重试刷请求。
- **SQLite 连接按线程复用**：实测「每条查询新建连接」要 13.5ms/次，
  一次 refresh 约 177 次调用 ≈ 2.4 秒纯开销。改成 thread-local 缓存后，
  事务边界（正常提交 / 异常回滚）不变，配置里的 `db_path` 一变就自动重建。

## 已知限制

- Trending 页面无官方 API，GitHub 改版时 `fetcher.py` 的 CSS 选择器可能失效
  （已做检测：找不到 `article.Box-row` 会抛出明确错误而非静默返回空）。
- 榜单一存一天，无法回溯 GitHub 官方历史（官方本身也不提供）。
- 单机单进程，没有账号体系，定位是「个人自用面板」。
