"""
测试 core/storage.py —— 覆盖 v1 的两个致命缺陷。

test_notification_failure_is_retried_not_lost  → 推送失败即永久丢失
test_cold_start_does_not_notify_history        → 首次运行推送几百条历史公告刷屏
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
import shutil

import pytest

from core.models import PolicyItem, PolicyCategory, NotifyState
from core.storage import PolicyStorage, MAX_NOTIFY_ATTEMPTS


@pytest.fixture
def storage():
    tmp = tempfile.mkdtemp()
    yield PolicyStorage(db_path=os.path.join(tmp, "test.db"))
    shutil.rmtree(tmp, ignore_errors=True)


def item(n=1, city="深圳", title=None, url=None, **kw):
    return PolicyItem(
        title=title or f"2026年第{n}批人才公寓配租公告",
        url=url or f"https://zjj.sz.gov.cn/notice/{n}.html",
        city=city,
        source_name="深圳市住房保障署",
        category=PolicyCategory.HOUSING,
        **kw,
    )


class TestDeduplication:
    def test_new_items_recorded(self, storage):
        new = storage.record_discovered([item(1), item(2)])
        assert len(new) == 2
        assert storage.total_policies() == 2

    def test_same_item_not_recorded_twice(self, storage):
        storage.record_discovered([item(1)])
        again = storage.record_discovered([item(1)])
        assert again == []
        assert storage.total_policies() == 1

    def test_url_variants_deduped(self, storage):
        """带不同时间戳参数的同一公告，只应入库一次"""
        storage.record_discovered([item(1, url="https://a.gov.cn/n/1.html?t=111")])
        again = storage.record_discovered([item(1, url="https://a.gov.cn/n/1.html?t=222")])
        assert again == []
        assert storage.total_policies() == 1

    def test_same_title_new_url_deduped(self, storage):
        """网站改版换了链接结构，同一条政策不该再推一次"""
        storage.record_discovered([item(1, url="https://old.gov.cn/1.html")])
        again = storage.record_discovered([item(1, url="https://new.gov.cn/article/1.shtml")])
        assert again == []

    def test_dedup_within_single_batch(self, storage):
        """同一轮里多个数据源抓到同一条公告，只算一条"""
        dup = [item(1), item(1), item(1)]
        new = storage.record_discovered(dup)
        assert len(new) == 1

    def test_different_cities_not_deduped(self, storage):
        a = item(1, city="深圳", url="https://sz.gov.cn/1.html")
        b = item(1, city="杭州", url="https://hz.gov.cn/1.html")
        new = storage.record_discovered([a, b])
        assert len(new) == 2


class TestNotificationStateMachine:
    def test_new_items_are_pending(self, storage):
        storage.record_discovered([item(1), item(2)])
        pending = storage.get_pending_notifications()
        assert len(pending) == 2

    def test_notified_items_not_pending_again(self, storage):
        storage.record_discovered([item(1)])
        pending = storage.get_pending_notifications()
        storage.mark_notified([p.unique_id for p in pending])
        assert storage.get_pending_notifications() == []

    def test_notification_failure_is_retried_not_lost(self, storage):
        """
        ★ 核心回归测试 ★
        复现 v1 的致命缺陷：政策入库后推送失败，v1 会永远不再返回它们，用户永久收不到。
        v2 必须让失败的条目留在待推送队列里，下一轮继续重试。
        """
        storage.record_discovered([item(1), item(2)])

        # 第 1 轮：取出待推送 → 模拟 SMTP 挂掉
        round1 = storage.get_pending_notifications()
        assert len(round1) == 2
        storage.mark_notify_failed([p.unique_id for p in round1], "SMTP 连接超时")

        # 第 2 轮：这两条必须还在队列里（v1 在这里会返回空列表，消息就此丢失）
        round2 = storage.get_pending_notifications()
        assert len(round2) == 2, "推送失败的政策必须留在队列中等待重试，绝不能丢失"

        # 第 2 轮推送成功
        storage.mark_notified([p.unique_id for p in round2])

        # 第 3 轮：已送达，不再重复打扰
        assert storage.get_pending_notifications() == []

    def test_rediscovery_does_not_resurrect_sent_items(self, storage):
        """已推送过的政策，下轮再次抓到时不能又变成待推送"""
        storage.record_discovered([item(1)])
        pending = storage.get_pending_notifications()
        storage.mark_notified([p.unique_id for p in pending])

        storage.record_discovered([item(1)])  # 网站上这条公告还在，又被抓到
        assert storage.get_pending_notifications() == []

    def test_abandoned_after_max_attempts(self, storage):
        """无限重试会拖垮系统，超过上限应放弃并计入报告"""
        storage.record_discovered([item(1)])
        for _ in range(MAX_NOTIFY_ATTEMPTS):
            pending = storage.get_pending_notifications()
            if not pending:
                break
            storage.mark_notify_failed([p.unique_id for p in pending], "持续失败")

        assert storage.get_pending_notifications() == []
        assert storage.count_by_state()["abandoned"] == 1

    def test_failure_error_recorded(self, storage):
        storage.record_discovered([item(1)])
        p = storage.get_pending_notifications()
        storage.mark_notify_failed([p[0].unique_id], "邮箱授权码错误")
        assert storage.count_by_state()["pending"] == 1


class TestColdStart:
    def test_fresh_db_not_initialized(self, storage):
        assert storage.is_initialized() is False

    def test_cold_start_does_not_notify_history(self, storage):
        """
        ★ 核心回归测试 ★
        首次运行抓到 200 条历史公告，v1 会把它们全当"新政策"塞进第一封邮件。
        v2 应记录为基线（只存不推），推送队列必须为空。
        """
        history = [item(n) for n in range(200)]
        recorded = storage.record_discovered(history, as_baseline=True)
        storage.mark_initialized(len(recorded))

        assert len(recorded) == 200
        assert storage.total_policies() == 200
        assert storage.get_pending_notifications() == [], "冷启动不得推送历史公告"
        assert storage.count_by_state()["baseline"] == 200

    def test_after_baseline_new_items_do_notify(self, storage):
        """建立基线之后，真正的新增政策必须正常推送"""
        storage.record_discovered([item(n) for n in range(50)], as_baseline=True)
        storage.mark_initialized(50)
        assert storage.is_initialized() is True

        new = storage.record_discovered([item(999, title="2026年新增第七批保租房配租公告")])
        assert len(new) == 1

        pending = storage.get_pending_notifications()
        assert len(pending) == 1
        assert "第七批" in pending[0].title

    def test_baseline_flag_persists_across_instances(self, storage):
        """重启进程后不能忘记已建过基线，否则会再次静默一整轮"""
        storage.record_discovered([item(1)], as_baseline=True)
        storage.mark_initialized(1)

        reopened = PolicyStorage(db_path=storage.db_path)
        assert reopened.is_initialized() is True


class TestHealthReport:
    def test_consecutive_failures_counted(self, storage):
        for _ in range(4):
            storage.log_collector_run("杭州市人社局", "杭州", "FAILED", error_message="403")
        report = storage.get_health_report()
        h = next(x for x in report if x.source_name == "杭州市人社局")
        assert h.consecutive_failures == 4
        assert h.is_broken is True

    def test_success_resets_streak(self, storage):
        for _ in range(3):
            storage.log_collector_run("深圳住保署", "深圳", "FAILED", error_message="timeout")
        storage.log_collector_run("深圳住保署", "深圳", "SUCCESS", items_found=20)
        h = next(x for x in storage.get_health_report() if x.source_name == "深圳住保署")
        assert h.consecutive_failures == 0
        assert h.is_broken is False

    def test_success_with_zero_items_counts_as_failure(self, storage):
        """
        能打开网页但一条都没解析出来 = 选择器失效，
        这是最隐蔽的失效方式，v1 会当成"成功"从而永远不告警。
        """
        for _ in range(3):
            storage.log_collector_run("广州住建局", "广州", "SUCCESS", items_found=0)
        h = next(x for x in storage.get_health_report() if x.source_name == "广州住建局")
        assert h.consecutive_failures == 3
        assert h.is_broken is True

    def test_alert_cooldown(self, storage):
        assert storage.should_send_health_alert() is True
        storage.mark_health_alert_sent()
        assert storage.should_send_health_alert(cooldown_hours=24) is False


class TestMaintenance:
    def test_stats_shape(self, storage):
        storage.record_discovered([item(1), item(2)])
        s = storage.stats()
        assert s["total_policies"] == 2
        assert s["pending"] == 2
        assert "sent" in s and "baseline" in s

    def test_prune_logs_runs(self, storage):
        storage.log_collector_run("x", "y", "SUCCESS", items_found=1)
        assert storage.prune_logs(keep_days=30) == 0  # 刚写的日志不该被清理


class TestMigrationFromV1:
    def test_v1_database_upgrades_cleanly(self):
        """用户已有的 v1 数据库必须能平滑升级，不丢数据"""
        import sqlite3
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "v1.db")
        try:
            conn = sqlite3.connect(db)
            conn.execute("""
                CREATE TABLE policies (
                    unique_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
                    city TEXT NOT NULL, district TEXT, category TEXT, source_name TEXT,
                    publish_date TEXT, deadline TEXT, target_audience TEXT,
                    amount_or_quota TEXT, content_fingerprint TEXT,
                    notified INTEGER DEFAULT 0,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notified_at TIMESTAMP
                )""")
            conn.execute("""
                CREATE TABLE collector_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_name TEXT NOT NULL,
                    city TEXT NOT NULL, status TEXT NOT NULL, items_found INTEGER DEFAULT 0,
                    error_message TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""")
            conn.execute(
                "INSERT INTO policies (unique_id,title,url,city,district,category,source_name,notified) "
                "VALUES ('abc','旧政策公告标题','https://a.gov.cn/1.html','深圳','全市','housing','住保署',0)"
            )
            conn.commit()
            conn.close()

            upgraded = PolicyStorage(db_path=db)
            assert upgraded.total_policies() == 1

            # v1 里 notified=0 的历史数据，升级后应能被正常取出推送
            pending = upgraded.get_pending_notifications()
            assert len(pending) == 1
            assert pending[0].title == "旧政策公告标题"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
