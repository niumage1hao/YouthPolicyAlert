"""
core/storage.py
SQLite 持久化、增量去重与推送状态机。

v2 修复的两个致命缺陷：

【缺陷 1：推送失败即永久丢失】
  v1 的 filter_and_save_new() 一边入库一边返回"新政策"，调用方拿去发邮件。
  一旦邮件发送失败（SMTP 抽风、授权码过期、网络波动），这批政策已经在库里了，
  下一轮 filter_and_save_new() 不会再返回它们 —— 用户永远收不到，且毫无察觉。
  （用户现有数据库里 7 条政策全部 notified=0，正是这个 bug 的现场证据。）

  v2 把"发现"和"推送"彻底拆开：
    record_discovered()        -> 只负责入库，标记为 PENDING
    get_pending_notifications()-> 取出所有待推送的（含上轮失败的）
    mark_notified()/mark_failed()-> 推送成功才置 SENT，失败保持 PENDING 下轮重试

【缺陷 2：冷启动刷屏】
  v1 首次运行时数据库为空，抓到的几百条历史公告全被判定为"新政策"，
  用户第一封邮件就会收到几百条几年前的公告，直接劝退。

  v2 首次运行进入"基线模式"：全部记录为 BASELINE 只存不推，
  并发送一封"监控已启动"的确认邮件，之后只推真正的增量。
"""
import os
import json
import sqlite3
import logging
from contextlib import contextmanager
from typing import List, Optional, Dict, Any, Iterable
from datetime import datetime, date, timedelta

from core.models import PolicyItem, NotifyState, PolicyCategory, CollectorHealth

logger = logging.getLogger("YouthPolicyAlert.Storage")

SCHEMA_VERSION = 2
MAX_NOTIFY_ATTEMPTS = 5


class PolicyStorage:
    """基于 SQLite 的政策存储、去重与推送状态引擎"""

    def __init__(self, db_path: str = "data/policy.db"):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(parent, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # 连接与建表
    # ------------------------------------------------------------------
    # WAL 需要 shm 共享内存文件，在网络驱动器、OneDrive/坚果云同步目录、
    # FUSE 挂载点上会直接报 "disk I/O error"。首次探测失败后就记住并降级，
    # 避免用户把项目放在同步盘里时整个程序跑不起来。
    _wal_supported: Optional[bool] = None

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if PolicyStorage._wal_supported is not False:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    PolicyStorage._wal_supported = True
                except sqlite3.OperationalError:
                    PolicyStorage._wal_supported = False
                    logger.info("当前文件系统不支持 WAL 模式（网络盘/同步盘常见），已自动降级为默认日志模式。")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # 向后兼容：老代码/web.py 里用的是 _get_connection()
    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS policies (
                    unique_id           TEXT PRIMARY KEY,
                    dedup_key           TEXT,
                    title               TEXT NOT NULL,
                    url                 TEXT NOT NULL,
                    canonical_url       TEXT,
                    city                TEXT NOT NULL,
                    district            TEXT,
                    category            TEXT,
                    source_name         TEXT,
                    publish_date        TEXT,
                    deadline            TEXT,
                    target_audience     TEXT,
                    amount_or_quota     TEXT,
                    age_limit           TEXT,
                    apply_channel       TEXT,
                    notes               TEXT,
                    relevance_score     REAL DEFAULT 0,
                    content_fingerprint TEXT,
                    notified            INTEGER DEFAULT 0,
                    notify_attempts     INTEGER DEFAULT 0,
                    notify_error        TEXT,
                    first_seen_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified_at         TIMESTAMP
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS collector_logs (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_name   TEXT NOT NULL,
                    city          TEXT NOT NULL,
                    status        TEXT NOT NULL,
                    items_found   INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 系统元数据表：记录 schema 版本、是否已完成冷启动基线等
            cur.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key        TEXT PRIMARY KEY,
                    value      TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 必须先补齐 v1 老表缺失的字段，再建索引 —— 否则在老库上会因
            # "no such column: dedup_key" 直接崩溃，用户的历史数据将无法升级。
            self._migrate(cur)

            cur.execute("CREATE INDEX IF NOT EXISTS idx_policies_notified ON policies(notified)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_policies_dedup ON policies(dedup_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_policies_city ON policies(city)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_policies_seen ON policies(first_seen_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_source ON collector_logs(city, source_name, created_at)")

    def _migrate(self, cur: sqlite3.Cursor):
        """把 v1 建的老表平滑升级到 v2，不丢用户已有数据"""
        cur.execute("PRAGMA table_info(policies)")
        existing = {row[1] for row in cur.fetchall()}

        new_columns = {
            "dedup_key": "TEXT",
            "canonical_url": "TEXT",
            "age_limit": "TEXT",
            "apply_channel": "TEXT",
            "notes": "TEXT",
            "relevance_score": "REAL DEFAULT 0",
            "notify_attempts": "INTEGER DEFAULT 0",
            "notify_error": "TEXT",
        }
        for col, decl in new_columns.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE policies ADD COLUMN {col} {decl}")
                logger.info(f"数据库升级：为 policies 表新增字段 {col}")

        # 回填老数据的归一化 URL 与去重键
        cur.execute("SELECT unique_id, url, city, title FROM policies WHERE canonical_url IS NULL OR dedup_key IS NULL")
        rows = cur.fetchall()
        if rows:
            from core.models import normalize_url, normalize_title
            import hashlib
            payload = []
            for r in rows:
                canonical = normalize_url(r["url"])
                dkey = hashlib.md5(f"{r['city']}|{normalize_title(r['title'])}".encode("utf-8")).hexdigest()
                payload.append((canonical, dkey, r["unique_id"]))
            cur.executemany(
                "UPDATE policies SET canonical_url = ?, dedup_key = ? WHERE unique_id = ?", payload
            )
            logger.info(f"数据库升级：已为 {len(payload)} 条历史政策回填归一化 URL 与去重键")

        cur.execute(
            "INSERT INTO meta(key, value, updated_at) VALUES('schema_version', ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (str(SCHEMA_VERSION),),
        )

    # ------------------------------------------------------------------
    # meta 读写
    # ------------------------------------------------------------------
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO meta(key, value, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
                (key, value),
            )

    def is_initialized(self) -> bool:
        """是否已经完成过冷启动基线建立"""
        return self.get_meta("baseline_established") == "1"

    def mark_initialized(self, item_count: int):
        self.set_meta("baseline_established", "1")
        self.set_meta("baseline_at", datetime.now().isoformat(timespec="seconds"))
        self.set_meta("baseline_count", str(item_count))

    def total_policies(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM policies").fetchone()["c"]

    # ------------------------------------------------------------------
    # 核心：发现与入库（不负责推送）
    # ------------------------------------------------------------------
    def record_discovered(
        self,
        items: List[PolicyItem],
        as_baseline: bool = False,
    ) -> List[PolicyItem]:
        """
        将采集到的政策入库，返回其中【本次首次发现】的条目。

        :param as_baseline: 冷启动基线模式。True 时新条目直接标记为 BASELINE（只记录不推送）。
        :return: 首次发现的条目列表（基线模式下同样返回，供调用方统计，但状态已是 BASELINE）
        """
        if not items:
            return []

        state = NotifyState.BASELINE.value if as_baseline else NotifyState.PENDING.value
        new_items: List[PolicyItem] = []

        with self._conn() as conn:
            cur = conn.cursor()

            # 一次性把已有的主键与二级去重键读进内存，避免逐条查询
            existing_ids = {r["unique_id"] for r in cur.execute("SELECT unique_id FROM policies")}
            existing_dedup = {
                r["dedup_key"] for r in cur.execute("SELECT dedup_key FROM policies WHERE dedup_key IS NOT NULL")
            }

            # 同一轮抓取内部也可能出现重复（多个数据源抓到同一条），本轮内先自去重
            seen_this_run_ids = set()
            seen_this_run_dedup = set()

            payload = []
            for item in items:
                uid = item.unique_id
                dkey = item.dedup_key

                if uid in existing_ids or uid in seen_this_run_ids:
                    continue
                # 二级去重：同城市同标题，即使 URL 变了也不重复推送
                if dkey in existing_dedup or dkey in seen_this_run_dedup:
                    logger.debug(f"二级去重命中（同城同标题换链接）：{item.title}")
                    continue

                seen_this_run_ids.add(uid)
                seen_this_run_dedup.add(dkey)

                payload.append((
                    uid, dkey, item.title, item.url, item.canonical_url,
                    item.city, item.district, item.category.value, item.source_name,
                    item.publish_date, item.deadline, item.target_audience,
                    item.amount_or_quota, item.age_limit, item.apply_channel, item.notes,
                    float(item.relevance_score), item.content_fingerprint,
                    state, datetime.now(),
                    datetime.now() if as_baseline else None,
                ))
                new_items.append(item)

            if payload:
                cur.executemany(
                    """
                    INSERT OR IGNORE INTO policies (
                        unique_id, dedup_key, title, url, canonical_url,
                        city, district, category, source_name,
                        publish_date, deadline, target_audience,
                        amount_or_quota, age_limit, apply_channel, notes,
                        relevance_score, content_fingerprint,
                        notified, first_seen_at, notified_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    payload,
                )

        if new_items:
            label = "基线记录" if as_baseline else "待推送"
            logger.info(f"✨ 本轮新发现 {len(new_items)} 条政策（已存为{label}）")
        else:
            logger.info("ℹ️ 本轮未发现新政策。")
        return new_items

    # 兼容旧接口名（老脚本/测试可能还在用）
    def filter_and_save_new(self, items: List[PolicyItem]) -> List[PolicyItem]:
        return self.record_discovered(items, as_baseline=False)

    def is_exists(self, unique_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute(
                "SELECT 1 FROM policies WHERE unique_id = ?", (unique_id,)
            ).fetchone() is not None

    # ------------------------------------------------------------------
    # 核心：推送队列（含失败重试）
    # ------------------------------------------------------------------
    def get_pending_notifications(self, limit: int = 50) -> List[PolicyItem]:
        """
        取出所有待推送的政策 —— 包含【上一轮推送失败的】。
        这是修复"推送失败即丢失"的关键：失败的条目状态仍是 PENDING，会被再次取出重试。
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM policies
                WHERE notified = ? AND notify_attempts < ?
                ORDER BY
                    CASE WHEN publish_date IS NULL THEN 1 ELSE 0 END,
                    publish_date DESC,
                    relevance_score DESC,
                    first_seen_at DESC
                LIMIT ?
                """,
                (NotifyState.PENDING.value, MAX_NOTIFY_ATTEMPTS, limit),
            ).fetchall()

        return [self._row_to_item(r) for r in rows]

    def _row_to_item(self, row: sqlite3.Row) -> PolicyItem:
        return PolicyItem(
            title=row["title"],
            url=row["url"],
            city=row["city"],
            district=row["district"] or "全市",
            source_name=row["source_name"] or "",
            category=PolicyCategory.coerce(row["category"]),
            target_audience=row["target_audience"] or "详见官方正文",
            deadline=row["deadline"] or "以官方公告为准",
            amount_or_quota=row["amount_or_quota"],
            age_limit=self._safe_get(row, "age_limit"),
            apply_channel=self._safe_get(row, "apply_channel"),
            notes=self._safe_get(row, "notes"),
            publish_date=row["publish_date"],
            relevance_score=self._safe_get(row, "relevance_score") or 0.0,
        )

    @staticmethod
    def _safe_get(row: sqlite3.Row, key: str):
        try:
            return row[key]
        except (IndexError, KeyError):
            return None

    def mark_notified(self, unique_ids: Iterable[str]):
        """推送成功：置为 SENT，不再打扰用户"""
        ids = list(unique_ids)
        if not ids:
            return
        with self._conn() as conn:
            conn.executemany(
                "UPDATE policies SET notified = ?, notified_at = ?, notify_error = NULL WHERE unique_id = ?",
                [(NotifyState.SENT.value, datetime.now(), uid) for uid in ids],
            )
        logger.info(f"✅ 已将 {len(ids)} 条政策标记为推送成功")

    def mark_notify_failed(self, unique_ids: Iterable[str], error: str):
        """
        推送失败：累加重试次数并记录原因，状态仍保持 PENDING，下一轮自动重试。
        超过 MAX_NOTIFY_ATTEMPTS 次后置为 ABANDONED，避免无限重试拖垮系统。
        """
        ids = list(unique_ids)
        if not ids:
            return
        short_err = (error or "")[:500]
        with self._conn() as conn:
            conn.executemany(
                "UPDATE policies SET notify_attempts = notify_attempts + 1, notify_error = ? WHERE unique_id = ?",
                [(short_err, uid) for uid in ids],
            )
            conn.execute(
                "UPDATE policies SET notified = ? WHERE notify_attempts >= ? AND notified = ?",
                (NotifyState.ABANDONED.value, MAX_NOTIFY_ATTEMPTS, NotifyState.PENDING.value),
            )
        logger.warning(f"⚠️ {len(ids)} 条政策推送失败，已记录并将在下轮重试。原因: {short_err[:120]}")

    def count_by_state(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT notified, COUNT(*) AS c FROM policies GROUP BY notified").fetchall()
        mapping = {
            NotifyState.PENDING.value: "pending",
            NotifyState.SENT.value: "sent",
            NotifyState.BASELINE.value: "baseline",
            NotifyState.ABANDONED.value: "abandoned",
        }
        out = {"pending": 0, "sent": 0, "baseline": 0, "abandoned": 0}
        for r in rows:
            key = mapping.get(r["notified"])
            if key:
                out[key] = r["c"]
        return out

    # ------------------------------------------------------------------
    # 采集健康
    # ------------------------------------------------------------------
    def log_collector_run(
        self,
        source_name: str,
        city: str,
        status: str,
        items_found: int = 0,
        error_message: Optional[str] = None,
    ):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO collector_logs (source_name, city, status, items_found, error_message) "
                "VALUES (?,?,?,?,?)",
                (source_name, city, status, items_found, (error_message or "")[:500] or None),
            )

    def get_health_report(self) -> List[CollectorHealth]:
        """
        统计每个数据源最近的连续失败次数 —— 用于"网站改版了但没人发现"的主动告警。
        """
        with self._conn() as conn:
            sources = conn.execute(
                "SELECT DISTINCT city, source_name FROM collector_logs"
            ).fetchall()

            report: List[CollectorHealth] = []
            for s in sources:
                rows = conn.execute(
                    """
                    SELECT status, items_found, error_message, created_at
                    FROM collector_logs
                    WHERE city = ? AND source_name = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 20
                    """,
                    (s["city"], s["source_name"]),
                ).fetchall()

                if not rows:
                    continue

                consecutive = 0
                for r in rows:
                    # 抓取成功但一条都没解析出来，同样视为"实质失败"（多半是选择器失效）
                    failed = r["status"] != "SUCCESS" or (r["items_found"] or 0) == 0
                    if failed:
                        consecutive += 1
                    else:
                        break

                last_success = next(
                    (r["created_at"] for r in rows if r["status"] == "SUCCESS" and (r["items_found"] or 0) > 0),
                    None,
                )

                report.append(CollectorHealth(
                    source_name=s["source_name"],
                    city=s["city"],
                    consecutive_failures=consecutive,
                    last_status=rows[0]["status"],
                    last_error=rows[0]["error_message"],
                    last_success_at=last_success,
                    last_items_found=rows[0]["items_found"] or 0,
                ))

        report.sort(key=lambda h: h.consecutive_failures, reverse=True)
        return report

    def should_send_health_alert(self, cooldown_hours: int = 24) -> bool:
        """健康告警冷却：避免每小时都给维护者发同样的告警邮件"""
        last = self.get_meta("last_health_alert_at")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return datetime.now() - last_dt > timedelta(hours=cooldown_hours)

    def mark_health_alert_sent(self):
        self.set_meta("last_health_alert_at", datetime.now().isoformat(timespec="seconds"))

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def prune_logs(self, keep_days: int = 30) -> int:
        """清理过期采集日志，防止长期运行后数据库无限膨胀"""
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM collector_logs WHERE created_at < ?", (cutoff,))
            deleted = cur.rowcount or 0

        # checkpoint 必须在事务提交之后单独执行，否则会报 "database table is locked"；
        # 且只在确实启用了 WAL 时才有意义。
        if deleted and PolicyStorage._wal_supported:
            try:
                conn2 = sqlite3.connect(self.db_path, timeout=30)
                conn2.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn2.close()
            except Exception as e:
                logger.debug(f"WAL checkpoint 跳过: {e}")

        if deleted:
            logger.info(f"🧹 已清理 {deleted} 条 {keep_days} 天前的采集日志")
        return deleted

    def stats(self) -> Dict[str, Any]:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM policies").fetchone()["c"]
            cities = conn.execute("SELECT COUNT(DISTINCT city) c FROM policies").fetchone()["c"]
            today = conn.execute(
                "SELECT COUNT(*) c FROM policies WHERE date(first_seen_at) = date('now','localtime')"
            ).fetchone()["c"]
        states = self.count_by_state()
        return {
            "total_policies": total,
            "active_cities": cities,
            "today_count": today,
            **states,
        }
