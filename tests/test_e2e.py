"""
端到端验收测试。

起一个真实的本地 HTTP 服务器充当"政务网站"，跑通完整链路：
  真实 HTTP 请求 → 采集 → 清洗打分 → 详情页干货提取 → 去重入库 → 推送

与其他测试不同，这里走的是真正的 socket、真正的 httpx 客户端、
真正的编码探测与 SQLite 事务，唯一被替换的是"发邮件"这一步（用假通道记录调用）。

覆盖的关键场景：
  1. 冷启动不刷屏
  2. 增量只推新政策
  3. 推送失败 → 下轮自动重试（v1 会丢消息）
  4. 单站宕机不影响其他城市
  5. GBK 编码政务站不乱码
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shutil
import tempfile
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import date, timedelta

import pytest

from core.models import PolicyItem, PolicyCategory
from core.requester import BaseRequester
from core.rule_engine import collect_all, enrich_with_details
from core.pipeline import PolicyPipeline
from core.storage import PolicyStorage
from core.notify_center import NotifyCenter
from core.config_schema import AppConfig


# ---------------------------------------------------------------------------
# 假政务网站
# ---------------------------------------------------------------------------
TODAY = date.today()
D0 = TODAY.strftime("%Y-%m-%d")
D1 = (TODAY - timedelta(days=2)).strftime("%Y-%m-%d")
D_OLD = (TODAY - timedelta(days=400)).strftime("%Y-%m-%d")

LIST_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>通知公告</title></head><body>
<ul class="news-list">
{rows}
  <li><a href="/n/noise1.html">2026年办公家具采购项目中标结果公告</a><span class="date">{d1}</span></li>
  <li><a href="/n/old.html">2019年人才公寓配租公告</a><span class="date">{dold}</span></li>
  <li><a href="javascript:;">无效链接的公告标题内容</a><span class="date">{d1}</span></li>
</ul></body></html>"""

ROW = '  <li><a href="/n/{slug}.html">{title}</a><span class="date">{date}</span></li>'

DETAIL_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body>
<div class="TRS_Editor">
<p>为做好青年人才安居保障，现将有关事项公告如下：</p>
<p>一、本批次共计推出人才公寓 800 套。</p>
<p>二、申请人须具有全日制本科及以上学历，年龄不超过 35 周岁。</p>
<p>三、租房补贴标准：博士 3000 元/月，硕士 2000 元/月，本科 1200 元/月。</p>
<p>四、申报受理时间为 2026年9月1日 至 2026年9月30日。</p>
<p>五、请登录“示例市人才安居服务平台”（https://rcaj.example.gov.cn/apply）提交申请。</p>
</div></body></html>"""

GBK_LIST = """<!DOCTYPE html>
<html><head><meta charset="gb2312"><title>֪ͨ����</title></head><body>
<ul class="news-list">
  <li><a href="/n/gbk1.html">2026年第二批人才公寓配租公告</a><span class="date">{d}</span></li>
  <li><a href="/n/gbk2.html">高校毕业生租房补贴申报通知</a><span class="date">{d}</span></li>
</ul></body></html>"""


class FakeGovHandler(BaseHTTPRequestHandler):
    """模拟政务网站，支持动态切换公告列表"""

    listings = {}   # path -> html
    hits = []

    def do_GET(self):
        path = self.path.split("?")[0]
        FakeGovHandler.hits.append(path)

        if path == "/down":
            self.send_error(503, "Service Unavailable")
            return

        if path == "/gbk":
            body = GBK_LIST.format(d=D0).encode("gb18030")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=gb2312")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/n/"):
            body = DETAIL_HTML.encode("utf-8")
        else:
            html = FakeGovHandler.listings.get(path, FakeGovHandler.listings.get("/list", ""))
            body = html.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def build_listing(entries):
    rows = "\n".join(ROW.format(slug=s, title=t, date=d) for s, t, d in entries)
    return LIST_TEMPLATE.format(rows=rows, d1=D1, dold=D_OLD)


BATCH_1 = [
    ("p1", "示例市2026年第三批人才公寓配租公告", D0),
    ("p2", "关于开展2026年高校毕业生租房补贴申报工作的通知", D1),
]
BATCH_2 = BATCH_1 + [
    ("p3", "示例市第七批保障性租赁住房认租公告", D0),
]


@pytest.fixture(scope="module")
def gov_server():
    FakeGovHandler.listings["/list"] = build_listing(BATCH_1)
    server = HTTPServer(("127.0.0.1", 0), FakeGovHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def workspace():
    tmp = tempfile.mkdtemp()
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 假推送通道
# ---------------------------------------------------------------------------
class RecordingNotifier:
    """记录收到了哪些政策，可切换成"发送失败"以验证重试"""

    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.batches = []
        self.plain = []

    def send(self, items):
        if self.should_fail:
            return False
        self.batches.append(list(items))
        return True

    def send_plain(self, title, lines):
        if self.should_fail:
            return False
        self.plain.append(title)
        return True

    @property
    def all_titles(self):
        return [i.title for batch in self.batches for i in batch]


def make_center(storage, notifier):
    center = NotifyCenter(AppConfig(), storage)
    center.channels = [("测试通道", notifier)]
    return center


def run_collection(base_url, workspace, path="/list", fetch_detail=True):
    """跑一遍采集+清洗+提取，返回处理好的政策列表"""
    rule = {
        "id": "e2e", "city": "示例市", "district": "全市",
        "source_name": "示例市住房保障署", "category": "housing",
        "url": f"{base_url}{path}", "parser_type": "html",
        "selectors": {"list_item": "ul.news-list li", "title": "a", "link": "a", "date": "span.date"},
    }
    req = BaseRequester(min_delay=0, max_delay=0, enable_warmup=False, max_retries=1, timeout=10)
    try:
        results = collect_all([rule], requester=req, max_workers=1)
        raw = [i for r in results for i in r.items]
        pipeline = PolicyPipeline(max_age_days=45)
        clean = pipeline.process(raw)
        if fetch_detail and clean:
            clean = enrich_with_details(clean, requester=req, max_details=10, max_workers=2)
        return results, clean
    finally:
        req.close()


# ---------------------------------------------------------------------------
class TestFullPipeline:
    def test_real_http_collection_and_filtering(self, gov_server, workspace):
        """真实 HTTP 请求 → 抓到目标公告、滤掉噪音与过期公告"""
        FakeGovHandler.listings["/list"] = build_listing(BATCH_1)
        results, clean = run_collection(gov_server, workspace)

        assert results[0].ok is True
        titles = [i.title for i in clean]

        assert any("人才公寓配租" in t for t in titles)
        assert any("租房补贴" in t for t in titles)
        assert not any("采购" in t for t in titles), "采购公告应被黑名单过滤"
        assert not any("2019" in t for t in titles), "过期公告应被时效过滤"
        assert not any("无效链接" in t for t in titles), "javascript 链接应被跳过"

    def test_detail_extraction_fills_real_values(self, gov_server, workspace):
        """★ 端到端验证干货提取：推送出去的卡片必须带真实的金额/期限/门槛 ★"""
        FakeGovHandler.listings["/list"] = build_listing(BATCH_1)
        _, clean = run_collection(gov_server, workspace)

        assert clean
        enriched = [i for i in clean if i.detail_fetched]
        assert enriched, "应至少有一条成功抓取到详情页"

        item = enriched[0]
        assert item.age_limit == "35周岁以下"
        assert item.amount_or_quota is not None
        assert "2026-09" in item.deadline
        assert "本科" in item.target_audience
        assert item.has_actionable_detail() is True


class TestColdStartAndIncrement:
    def test_cold_start_then_increment(self, gov_server, workspace):
        """
        ★ 完整生命周期验收 ★
        第 1 轮（冷启动）：只建基线，一条都不推
        第 2 轮（无变化）：没有新政策，不打扰用户
        第 3 轮（官网新增）：只推新增的那一条
        """
        db = os.path.join(workspace, "e2e.db")
        storage = PolicyStorage(db_path=db)
        notifier = RecordingNotifier()
        center = make_center(storage, notifier)

        # --- 第 1 轮：冷启动 ---
        FakeGovHandler.listings["/list"] = build_listing(BATCH_1)
        _, clean = run_collection(gov_server, workspace)
        baseline = storage.record_discovered(clean, as_baseline=True)
        storage.mark_initialized(len(baseline))
        center.send_baseline_welcome(len(baseline), 1, ["示例市"])

        assert len(baseline) >= 2
        assert notifier.batches == [], "冷启动绝不能推送历史公告"
        assert notifier.plain, "应发送一封'监控已启动'确认信"

        # --- 第 2 轮：官网没有更新 ---
        _, clean2 = run_collection(gov_server, workspace)
        new2 = storage.record_discovered(clean2)
        center.dispatch_pending()

        assert new2 == [], "同样的公告不应被再次识别为新政策"
        assert notifier.batches == [], "没有新政策时不该打扰用户"

        # --- 第 3 轮：官网新增一条 ---
        FakeGovHandler.listings["/list"] = build_listing(BATCH_2)
        _, clean3 = run_collection(gov_server, workspace)
        new3 = storage.record_discovered(clean3)
        center.dispatch_pending()

        assert len(new3) == 1, "应当只识别出新增的那一条"
        assert len(notifier.all_titles) == 1
        assert "第七批" in notifier.all_titles[0]

    def test_push_failure_recovers_next_round(self, gov_server, workspace):
        """
        ★ v1 致命缺陷的端到端验收 ★
        推送失败后，政策必须在下一轮补发出去，而不是永久消失。
        """
        db = os.path.join(workspace, "retry.db")
        storage = PolicyStorage(db_path=db)

        FakeGovHandler.listings["/list"] = build_listing(BATCH_1)
        _, clean = run_collection(gov_server, workspace, fetch_detail=False)

        storage.record_discovered(clean, as_baseline=True)
        storage.mark_initialized(len(clean))

        # 官网新增一条
        FakeGovHandler.listings["/list"] = build_listing(BATCH_2)
        _, clean2 = run_collection(gov_server, workspace, fetch_detail=False)
        new = storage.record_discovered(clean2)
        assert len(new) == 1

        # 第 1 次推送：邮件服务器挂了
        failing = RecordingNotifier(should_fail=True)
        make_center(storage, failing).dispatch_pending()
        assert failing.batches == []

        # 第 2 次推送：邮件恢复 —— 那条政策必须被补发
        working = RecordingNotifier()
        summary = make_center(storage, working).dispatch_pending()

        assert summary["succeeded"] == 1
        assert len(working.all_titles) == 1
        assert "第七批" in working.all_titles[0], "上轮推送失败的政策必须在本轮补发"

        # 第 3 次：已送达，不再重复
        again = RecordingNotifier()
        make_center(storage, again).dispatch_pending()
        assert again.batches == []

    def test_no_channel_keeps_items_pending(self, gov_server, workspace):
        """没配推送通道时，政策要留在队列里等配好后补发，而不是被当成已推送丢掉"""
        db = os.path.join(workspace, "nochan.db")
        storage = PolicyStorage(db_path=db)

        FakeGovHandler.listings["/list"] = build_listing(BATCH_1)
        _, clean = run_collection(gov_server, workspace, fetch_detail=False)
        storage.record_discovered(clean)

        center = NotifyCenter(AppConfig(), storage)  # 默认配置没有任何可用通道
        summary = center.dispatch_pending()
        assert summary["skipped_reason"] == "no_channel"

        # 配好通道后应当补发
        notifier = RecordingNotifier()
        make_center(storage, notifier).dispatch_pending()
        assert len(notifier.all_titles) >= 1


class TestResilience:
    def test_one_site_down_others_still_work(self, gov_server, workspace):
        """★ 沙盒隔离端到端验收：一个政务站 503，其他城市照常出结果 ★"""
        FakeGovHandler.listings["/list"] = build_listing(BATCH_1)

        good = {
            "id": "ok", "city": "正常市", "source_name": "正常住建局", "category": "housing",
            "url": f"{gov_server}/list", "parser_type": "html",
            "selectors": {"list_item": "ul.news-list li", "title": "a", "link": "a", "date": "span.date"},
        }
        bad = {
            "id": "down", "city": "故障市", "source_name": "故障住建局", "category": "housing",
            "url": f"{gov_server}/down", "parser_type": "html",
            "selectors": {"list_item": "ul.news-list li", "title": "a", "link": "a", "date": "span.date"},
        }

        req = BaseRequester(min_delay=0, max_delay=0, enable_warmup=False, max_retries=1, timeout=10)
        try:
            results = collect_all([good, bad], requester=req, max_workers=2)
        finally:
            req.close()

        by_city = {r.city: r for r in results}
        assert by_city["正常市"].ok is True and by_city["正常市"].items
        assert by_city["故障市"].ok is False

    def test_gbk_site_not_garbled(self, gov_server, workspace):
        """★ 中文编码验收：GB2312 政务站不能出现乱码 ★"""
        rule = {
            "id": "gbk", "city": "编码测试市", "source_name": "测试局", "category": "housing",
            "url": f"{gov_server}/gbk", "parser_type": "html",
            "selectors": {"list_item": "ul.news-list li", "title": "a", "link": "a", "date": "span.date"},
        }
        req = BaseRequester(min_delay=0, max_delay=0, enable_warmup=False, max_retries=1, timeout=10)
        try:
            results = collect_all([rule], requester=req, max_workers=1)
        finally:
            req.close()

        assert results[0].ok is True
        titles = [i.title for i in results[0].items]
        assert any("人才公寓配租公告" in t for t in titles), f"GBK 页面解码失败: {titles}"
        assert not any("锟斤拷" in t or "�" in t for t in titles)

    def test_selector_failure_falls_back(self, gov_server, workspace):
        """选择器写错时启发式兜底仍能出结果，用户不会毫无察觉地断更"""
        FakeGovHandler.listings["/list"] = build_listing(BATCH_1)
        rule = {
            "id": "badsel", "city": "示例市", "source_name": "住建局", "category": "housing",
            "url": f"{gov_server}/list", "parser_type": "html",
            "selectors": {"list_item": "ul.totally-wrong-class li"},
        }
        req = BaseRequester(min_delay=0, max_delay=0, enable_warmup=False, max_retries=1, timeout=10)
        try:
            results = collect_all([rule], requester=req, max_workers=1)
        finally:
            req.close()

        assert results[0].ok is True
        assert len(results[0].items) >= 2, "选择器失效时应由启发式兜底"
