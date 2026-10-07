"""SQLite 存储层。

设计要点：
- repos 存仓库元信息 + README 原文（缓存，避免重复请求烧配额）
- snapshots 存「某天某周期的排名」，同一仓库在不同周期可重复出现
- explanations 存 AI 说明，keyed by full_name，README 变了就重新生成
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings

# fallback 说明的重试熔断线：累计到这个次数就不再自动重烧 LLM。
# 实测不设熔断时，一个回检稳定不过关的项目只要还在当天快照里，
# 每次 refresh（cron + 看门狗，一天 2~6 次）都会重跑 1~2 次 LLM 调用，
# 永远不收敛 —— 白烧钱还把配额占满。
FALLBACK_MAX_ATTEMPTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    full_name        TEXT PRIMARY KEY,
    owner            TEXT NOT NULL,
    name             TEXT NOT NULL,
    url              TEXT NOT NULL,
    homepage         TEXT,
    description      TEXT,
    language         TEXT,
    topics           TEXT DEFAULT '[]',
    stars            INTEGER DEFAULT 0,
    forks            INTEGER DEFAULT 0,
    readme           TEXT,
    readme_hash      TEXT,
    readme_status    TEXT DEFAULT 'pending',
    readme_error     TEXT,
    readme_fetched_at TEXT,
    first_seen_at    TEXT,
    updated_at       TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    period        TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    full_name     TEXT NOT NULL,
    period_stars  INTEGER DEFAULT 0,
    total_stars   INTEGER DEFAULT 0,
    language      TEXT,
    fetched_at    TEXT,
    UNIQUE (period, snapshot_date, full_name)
);

CREATE TABLE IF NOT EXISTS explanations (
    full_name     TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending',
    headline      TEXT,
    body          TEXT,
    tags          TEXT DEFAULT '[]',
    keywords      TEXT DEFAULT '[]',
    readme_hash   TEXT,
    model         TEXT,
    attempts      INTEGER DEFAULT 0,
    grounded_ratio REAL DEFAULT 0,
    error         TEXT,
    generated_at  TEXT
);

CREATE TABLE IF NOT EXISTS refresh_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    ended_at   TEXT,
    trigger    TEXT,
    period     TEXT,
    found      INTEGER DEFAULT 0,
    new_repos  INTEGER DEFAULT 0,
    explained   INTEGER DEFAULT 0,
    fallback   INTEGER DEFAULT 0,
    errors     TEXT DEFAULT '[]',
    ok         INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
    ON snapshots (period, snapshot_date, rank);
CREATE INDEX IF NOT EXISTS idx_expl_status
    ON explanations (status);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# 每线程一个连接。实测「每次查询都新建连接」要 13.5ms/次，而一次 refresh
# 要调约 177 次，纯连接开销就是 ~2.4 秒 —— 比抓 README 还贵。
_local = threading.local()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """取当前线程的连接，并负责事务的提交/回滚。

    连接**不再每次新建、也不再关闭**，改为 thread-local 缓存：
      - 每个线程拿到自己的连接，所以天然线程安全，
        check_same_thread 也就自然不再是约束
      - 事务边界没变：正常提交、异常回滚，照旧
      - 配置里的 db_path 变了（切换库）会自动重建连接

    代价：连接会一直留着直到进程退出。这是刻意的 —— 单机单进程的个人面板，
    几十个常驻连接级别的开销可以忽略，换来的是 177 次调用省下 2.4 秒。
    """
    db_path = get_settings().db_path
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)

    if conn is None or getattr(_local, "db_path", None) != db_path:
        if conn is not None:
            conn.close()
        conn = _connect(db_path)
        _local.conn = conn
        _local.db_path = db_path

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# repos
# --------------------------------------------------------------------------

def upsert_repo(repo: dict[str, Any]) -> bool:
    """写入/更新仓库。返回 True 表示这是首次见到该仓库。"""
    full_name = repo["full_name"]
    now = utcnow()
    with get_conn() as conn:
        cur = conn.execute("SELECT 1 FROM repos WHERE full_name = ?", (full_name,))
        is_new = cur.fetchone() is None

        conn.execute(
            """
            INSERT INTO repos (
                full_name, owner, name, url, homepage, description,
                language, topics, stars, forks, first_seen_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(full_name) DO UPDATE SET
                homepage    = excluded.homepage,
                description = excluded.description,
                language    = excluded.language,
                topics      = excluded.topics,
                stars       = excluded.stars,
                forks       = excluded.forks,
                updated_at  = excluded.updated_at
            """,
            (
                full_name,
                full_name.split("/")[0],
                full_name.split("/")[-1],
                repo.get("url") or f"https://github.com/{full_name}",
                repo.get("homepage") or None,
                repo.get("description") or None,
                repo.get("language") or None,
                json.dumps(repo.get("topics") or [], ensure_ascii=False),
                int(repo.get("stars") or 0),
                int(repo.get("forks") or 0),
                now,
                now,
            ),
        )
    return is_new


def get_repo(full_name: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM repos WHERE full_name = ?", (full_name,)
        ).fetchone()
    return _repo_row_to_dict(row) if row else None


def _repo_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["topics"] = _safe_json(d.get("topics"), [])
    return d


def save_readme(
    full_name: str,
    readme: str | None,
    readme_hash: str | None,
    status: str,
    error: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE repos
               SET readme = ?, readme_hash = ?, readme_status = ?,
                   readme_error = ?, readme_fetched_at = ?, updated_at = ?
             WHERE full_name = ?
            """,
            (readme, readme_hash, status, error, utcnow(), utcnow(), full_name),
        )


def list_repos_without_explanation(limit: int) -> list[str]:
    """有 README 但还没有 ok 说明的仓库。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.full_name FROM repos r
         LEFT JOIN explanations e ON e.full_name = r.full_name
             WHERE r.readme_status = 'ok'
               AND (e.full_name IS NULL OR e.status != 'ok')
             ORDER BY r.first_seen_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [r["full_name"] for r in rows]


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------

def save_snapshot(period: str, snapshot_date: str, entries: list[dict[str, Any]]) -> int:
    """整批写入某周期某天的榜单。重复执行会覆盖同日记录。"""
    now = utcnow()
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM snapshots WHERE period = ? AND snapshot_date = ?",
            (period, snapshot_date),
        )
        conn.executemany(
            """
            INSERT INTO snapshots
                (period, snapshot_date, rank, full_name,
                 period_stars, total_stars, language, fetched_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            [
                (
                    period,
                    snapshot_date,
                    int(e["rank"]),
                    e["full_name"],
                    int(e.get("period_stars") or 0),
                    int(e.get("stars") or 0),
                    e.get("language"),
                    now,
                )
                for e in entries
            ],
        )
    return len(entries)


def get_leaderboard(period: str, snapshot_date: str | None = None) -> dict[str, Any]:
    """取某周期最新（或指定日期）的榜单，含说明状态。"""
    with get_conn() as conn:
        if snapshot_date is None:
            row = conn.execute(
                "SELECT MAX(snapshot_date) AS d FROM snapshots WHERE period = ?",
                (period,),
            ).fetchone()
            snapshot_date = row["d"] if row else None
        if not snapshot_date:
            return {"period": period, "date": None, "entries": []}

        rows = conn.execute(
            """
            SELECT s.rank, s.full_name, s.period_stars, s.total_stars,
                   s.language, r.description, r.homepage, r.url, r.topics,
                   e.status AS expl_status, e.headline
              FROM snapshots s
              JOIN repos r ON r.full_name = s.full_name
         LEFT JOIN explanations e ON e.full_name = s.full_name
             WHERE s.period = ? AND s.snapshot_date = ?
          ORDER BY s.rank ASC
            """,
            (period, snapshot_date),
        ).fetchall()

    entries = []
    for r in rows:
        d = dict(r)
        d["topics"] = _safe_json(d.get("topics"), [])
        d["period_stars"] = int(d.get("period_stars") or 0)
        d["total_stars"] = int(d.get("total_stars") or 0)
        d["has_explanation"] = d.get("expl_status") == "ok"
        entries.append(d)

    return {"period": period, "date": snapshot_date, "entries": entries}


def days_since_last_snapshot(
    max_age_hours: float = 20.0,
    period: str | None = None,
) -> float | None:
    """最新快照距今多少小时。返回 None 表示从来没抓到过。

    **必须能按周期过滤**（实测踩过的坑）：
    原本这里是 `SELECT MAX(fetched_at) FROM snapshots`，不分周期 ——
    于是 daily 抓取连续失败、weekly 成功时，MAX 取到的是 weekly 的时间戳，
    看门狗认为「数据新鲜」永不补 daily，前端也不告警，
    用户就停在 5 天前的日榜上，界面一切正常。

    period=None 时取所有周期里最新的那个（仅用于整体展示）；
    要判断某个周期新不新鲜，必须显式传 period。

    max_age_hours 不参与计算（阈值判断在 needs_catchup 里做），
    保留只为兼容既有调用方。
    """
    if period:
        sql = "SELECT MAX(fetched_at) AS t FROM snapshots WHERE period = ?"
        args: tuple[Any, ...] = (period,)
    else:
        sql = "SELECT MAX(fetched_at) AS t FROM snapshots"
        args = ()

    with get_conn() as conn:
        row = conn.execute(sql, args).fetchone()
    if not row or not row["t"]:
        return None

    try:
        last = datetime.fromisoformat(row["t"])
    except ValueError:
        return None

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return round(hours, 1)


def needs_catchup(
    max_age_hours: float = 20.0,
    periods: tuple[str, ...] = ("daily", "weekly"),
) -> tuple[bool, str]:
    """判断是否需要补跑一次刷新。**逐周期判断**。返回 (是否需要, 原因描述)。

    任一周期陈旧就返回 True —— daily 停在 5 天前而 weekly 是刚抓的，
    对用户来说日榜就是没更新，笼统的「数据新鲜」会把问题掩盖掉。
    原因里写明是哪个周期、多久没更新，方便直接对着日志排查。
    """
    ages = {p: days_since_last_snapshot(period=p) for p in periods}

    stale: list[str] = []
    for period, hours in ages.items():
        if hours is None:
            stale.append(f"{period} 还没有任何数据")
        elif hours > max_age_hours:
            stale.append(f"{period} 已是 {hours} 小时前的了")

    if stale:
        return True, "；".join(stale)

    freshest = min(h for h in ages.values() if h is not None)
    return False, f"数据新鲜（{freshest} 小时前抓的）"


def list_available_dates(period: str, limit: int = 30) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT snapshot_date FROM snapshots
             WHERE period = ? ORDER BY snapshot_date DESC LIMIT ?
            """,
            (period, limit),
        ).fetchall()
    return [r["snapshot_date"] for r in rows]


# --------------------------------------------------------------------------
# explanations
# --------------------------------------------------------------------------

def get_explanation(full_name: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM explanations WHERE full_name = ?", (full_name,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["tags"] = _safe_json(d.get("tags"), [])
    d["keywords"] = _safe_json(d.get("keywords"), [])
    return d


def save_explanation(
    full_name: str,
    status: str,
    *,
    headline: str | None = None,
    body: str | None = None,
    tags: list[str] | None = None,
    keywords: list[str] | None = None,
    readme_hash: str | None = None,
    model: str | None = None,
    attempts: int = 0,
    grounded_ratio: float = 0.0,
    error: str | None = None,
) -> None:
    # 入库前统一截断。弹窗要能一口气读完，模型偶尔会写超长。
    body = _truncate(body, 700)
    headline = _truncate(headline, 80) if headline else headline
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO explanations
                (full_name, status, headline, body, tags, keywords,
                 readme_hash, model, attempts, grounded_ratio, error, generated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(full_name) DO UPDATE SET
                status         = excluded.status,
                -- 空正文绝不允许覆盖已有正文。ON CONFLICT 是全字段覆盖，
                -- 调用方（比如 pipeline 的异常分支）没传 body 时，
                -- 昨天花几毛钱生成的说明会被这一行抹成空白。
                headline       = COALESCE(NULLIF(excluded.headline, ''), explanations.headline),
                body           = COALESCE(NULLIF(excluded.body, ''), explanations.body),
                tags           = excluded.tags,
                keywords       = excluded.keywords,
                readme_hash    = excluded.readme_hash,
                model          = excluded.model,
                attempts       = excluded.attempts,
                grounded_ratio = excluded.grounded_ratio,
                error          = excluded.error,
                generated_at   = excluded.generated_at
            """,
            (
                full_name,
                status,
                headline,
                body,
                json.dumps(tags or [], ensure_ascii=False),
                json.dumps(keywords or [], ensure_ascii=False),
                readme_hash,
                model,
                attempts,
                grounded_ratio,
                error,
                utcnow(),
            ),
        )


def needs_explanation(full_name: str, readme_hash: str | None) -> bool:
    """README 没变且已有 ok 说明 → 不需要重新生成。

    fallback 说明有**熔断**：累计尝试达到 FALLBACK_MAX_ATTEMPTS 次就停下，
    等 readme_hash 变了再给机会。之前对 fallback 永远返回 True，
    一个回检稳定不过关的项目会在每次 refresh 时重烧 1~2 次 LLM 调用，
    永远不收敛。
    """
    row = get_explanation(full_name)
    if not row:
        return True
    if row["status"] == "ok" and row.get("readme_hash") == readme_hash:
        return False
    if row["status"] == "ok" and readme_hash is None:
        return False
    if row["status"] == "fallback" and int(row.get("attempts") or 0) >= FALLBACK_MAX_ATTEMPTS:
        # 熔断中；README 变了说明内容确实更新了，才值得再试一次
        return row.get("readme_hash") != readme_hash
    return True


# --------------------------------------------------------------------------
# refresh_log
# --------------------------------------------------------------------------

def start_refresh(trigger: str, period: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO refresh_log (started_at, trigger, period) VALUES (?,?,?)",
            (utcnow(), trigger, period),
        )
        return int(cur.lastrowid)


def finish_refresh(
    log_id: int,
    *,
    found: int = 0,
    new_repos: int = 0,
    explained: int = 0,
    fallback: int = 0,
    errors: list[str] | None = None,
    ok: bool = True,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE refresh_log
               SET ended_at = ?, found = ?, new_repos = ?, explained = ?,
                   fallback = ?, errors = ?, ok = ?
             WHERE id = ?
            """,
            (
                utcnow(),
                found,
                new_repos,
                explained,
                fallback,
                json.dumps(errors or [], ensure_ascii=False),
                1 if ok else 0,
                log_id,
            ),
        )


def recent_refreshes(limit: int = 10) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM refresh_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["errors"] = _safe_json(d.get("errors"), [])
        out.append(d)
    return out


def stats() -> dict[str, Any]:
    with get_conn() as conn:
        def one(sql: str) -> int:
            return int(conn.execute(sql).fetchone()[0] or 0)

        return {
            "repos": one("SELECT COUNT(*) FROM repos"),
            "readme_ok": one("SELECT COUNT(*) FROM repos WHERE readme_status='ok'"),
            "readme_error": one("SELECT COUNT(*) FROM repos WHERE readme_status='error'"),
            "expl_ok": one("SELECT COUNT(*) FROM explanations WHERE status='ok'"),
            "expl_fallback": one("SELECT COUNT(*) FROM explanations WHERE status='fallback'"),
            "expl_pending": one("SELECT COUNT(*) FROM explanations WHERE status='pending'"),
            "snapshots": one("SELECT COUNT(*) FROM snapshots"),
        }


def _truncate(text: str | None, limit: int) -> str | None:
    """按段落边界截断，避免把句子劈成两半。"""
    if not text or len(text) <= limit:
        return text
    clipped = text[:limit]
    idx = max(clipped.rfind("\n\n"), clipped.rfind("。"), clipped.rfind("\n"))
    if idx > limit * 0.5:
        return clipped[: idx + 1].rstrip()
    return clipped.rstrip() + "…"


def _safe_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
