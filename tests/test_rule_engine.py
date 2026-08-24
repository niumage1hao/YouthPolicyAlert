"""
测试 core/rule_engine.py 与 core/auto_extractor.py —— 采集层的解析正确性。
全部使用本地 fixture，不依赖外网。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.rule_engine import DeclarativeRuleCollector, parse_date_text, validate_rule, collect_all
from core.auto_extractor import AutoContentExtractor

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class FakeRequester:
    """离线替身：直接返回 fixture 内容，可模拟异常"""

    def __init__(self, html="", error=None):
        self.html = html
        self.error = error
        self.calls = []

    def get_text(self, url, **kw):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.html

    def get_json(self, url, **kw):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.html


BASE_RULE = {
    "city": "示例市",
    "district": "全市",
    "source_name": "示例市住房保障署",
    "category": "housing",
    "url": "https://zjj.example.gov.cn/xxgk/tzgg/index.html",
    "parser_type": "html",
    "selectors": {"list_item": "ul.ftdt-list li", "title": "a", "link": "a", "date": "span.date"},
}


class TestDateParsing:
    @pytest.mark.parametrize("text,expected", [
        ("2026-08-20", "2026-08-20"),
        ("2026/8/5", "2026-08-05"),
        ("发布时间：2026年8月20日", "2026-08-20"),
        ("20260820", "2026-08-20"),
        ("[2026-08-20]", "2026-08-20"),
    ])
    def test_formats(self, text, expected):
        assert parse_date_text(text) == expected

    @pytest.mark.parametrize("text", ["近期", "", "第三批", "9999-99-99"])
    def test_invalid(self, text):
        assert parse_date_text(text) is None


class TestRuleValidation:
    def test_valid_rule(self):
        assert validate_rule(BASE_RULE) == []

    def test_missing_url(self):
        bad = {**BASE_RULE}
        del bad["url"]
        assert any("url" in p for p in validate_rule(bad))

    def test_bad_url_scheme(self):
        assert any("http" in p for p in validate_rule({**BASE_RULE, "url": "zjj.example.gov.cn"}))

    def test_bad_parser_type(self):
        assert any("parser_type" in p for p in validate_rule({**BASE_RULE, "parser_type": "xml"}))


class TestDeclarativeCollector:
    def test_extracts_with_selectors(self):
        req = FakeRequester(load_fixture("gov_list.html"))
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        titles = [i.title for i in items]
        assert "示例市2026年第三批人才公寓配租公告" in titles
        assert len(items) >= 4

    def test_relative_urls_resolved(self):
        req = FakeRequester(load_fixture("gov_list.html"))
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        assert all(i.url.startswith("https://zjj.example.gov.cn/") for i in items)

    def test_dates_extracted(self):
        req = FakeRequester(load_fixture("gov_list.html"))
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        first = next(i for i in items if "第三批人才公寓" in i.title)
        assert first.publish_date == "2026-08-20"

    def test_javascript_links_skipped(self):
        req = FakeRequester(load_fixture("gov_list.html"))
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        assert not any("javascript" in i.url for i in items)
        assert not any("无效的脚本链接" in i.title for i in items)

    def test_attachment_links_skipped(self):
        req = FakeRequester(load_fixture("gov_list.html"))
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        assert not any(i.url.endswith(".pdf") for i in items)

    def test_pagination_link_skipped(self):
        req = FakeRequester(load_fixture("gov_list.html"))
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        assert not any(i.title.strip() == "下一页" for i in items)

    def test_title_attribute_preferred(self):
        """政务站长标题常被 CSS 截断，title 属性里才是完整标题"""
        req = FakeRequester(load_fixture("gov_list.html"))
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        assert any(i.title == "示例市2026年第三批人才公寓配租公告" for i in items)

    def test_city_and_source_propagated(self):
        req = FakeRequester(load_fixture("gov_list.html"))
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        assert all(i.city == "示例市" for i in items)
        assert all(i.source_name == "示例市住房保障署" for i in items)

    def test_bad_rule_raises_clear_error(self):
        req = FakeRequester(load_fixture("gov_list.html"))
        with pytest.raises(ValueError, match="规则配置有误"):
            DeclarativeRuleCollector({**BASE_RULE, "url": ""}, requester=req).fetch()

    def test_internal_dedup(self):
        """同一页面里重复出现的链接只保留一条"""
        html = load_fixture("gov_list.html")
        doubled = html.replace("</ul>", "") + html[html.index("<ul"):]
        req = FakeRequester(doubled)
        items = DeclarativeRuleCollector(BASE_RULE, requester=req).fetch()
        urls = [i.url for i in items]
        assert len(urls) == len(set(urls))


class TestHeuristicFallback:
    def test_falls_back_when_selector_misses(self):
        """
        选择器失效时（政务网站改版的常态），启发式提取必须能兜住，
        否则用户会在毫无察觉的情况下彻底断更。
        """
        rule = {**BASE_RULE, "selectors": {"list_item": "ul.this-class-does-not-exist li"}}
        req = FakeRequester(load_fixture("gov_list_no_class.html"))
        items = DeclarativeRuleCollector(rule, requester=req).fetch()
        assert len(items) >= 3, "选择器未命中时应由启发式提取兜底"
        assert any("青年人才驿站" in i.title for i in items)

    def test_heuristic_prefers_content_over_nav(self):
        extractor = AutoContentExtractor(
            base_url="https://x.example.gov.cn/list.html",
            default_city="示例市", source_name="测试源",
        )
        items = extractor.extract_from_html(load_fixture("gov_list_no_class.html"))
        titles = " ".join(i.title for i in items)
        assert "友情链接" not in titles
        assert "机构概况" not in titles

    def test_heuristic_extracts_dates(self):
        extractor = AutoContentExtractor(
            base_url="https://x.example.gov.cn/list.html",
            default_city="示例市", source_name="测试源",
        )
        items = extractor.extract_from_html(load_fixture("gov_list_no_class.html"))
        assert any(i.publish_date for i in items)

    def test_heuristic_empty_page(self):
        extractor = AutoContentExtractor(base_url="https://x.gov.cn/", default_city="X", source_name="Y")
        assert extractor.extract_from_html("<html><body><p>没有任何列表</p></body></html>") == []


class TestSandboxIsolation:
    def test_one_failure_does_not_kill_others(self):
        """
        ★ 关键可靠性测试 ★
        一个政务网站宕机，绝不能影响其他城市的采集。
        """
        good_rule = {**BASE_RULE, "city": "好城市"}
        bad_rule = {**BASE_RULE, "city": "坏城市", "url": "https://broken.example.gov.cn/x.html"}

        class SelectiveRequester(FakeRequester):
            def get_text(self, url, **kw):
                if "broken" in url:
                    raise ConnectionError("模拟站点宕机")
                return load_fixture("gov_list.html")

        results = collect_all([good_rule, bad_rule], requester=SelectiveRequester(), max_workers=2)

        by_city = {r.city: r for r in results}
        assert by_city["好城市"].ok is True
        assert len(by_city["好城市"].items) > 0
        assert by_city["坏城市"].ok is False
        assert "宕机" in by_city["坏城市"].error

    def test_all_rules_reported(self):
        rules = [{**BASE_RULE, "city": f"城市{i}"} for i in range(5)]
        results = collect_all(rules, requester=FakeRequester(load_fixture("gov_list.html")), max_workers=3)
        assert len(results) == 5

    def test_empty_rules(self):
        assert collect_all([], requester=FakeRequester()) == []

    def test_json_html_parser_extracts_embedded_html(self):
        """政府站点常把列表 HTML 放在 JSON 的 data.html 字段中。"""
        payload = {
            "data": {
                "html": """
                <ul class="zfxxgk_item">
                  <li><a href="/art/2026/8/20/art_1.html" title="示例市青年人才补贴公示">示例市青年人才补贴公示</a><b>2026-08-20</b></li>
                </ul>
                """
            }
        }
        rule = {
            **BASE_RULE,
            "parser_type": "json_html",
            "url": "https://hrss.example.gov.cn/api/list",
            "json_path": {"html": "data.html"},
            "selectors": {"list_item": "li", "title": "a", "link": "a", "date": "b"},
        }
        req = FakeRequester(payload)
        items = DeclarativeRuleCollector(rule, requester=req).fetch()
        assert len(items) == 1
        assert items[0].title == "示例市青年人才补贴公示"
        assert items[0].url == "https://hrss.example.gov.cn/art/2026/8/20/art_1.html"
        assert items[0].publish_date == "2026-08-20"

    def test_json_html_parser_passes_query_params(self):
        payload = {"data": {"html": "<ul><li><a href='/a.html'>青年人才住房补贴公告</a></li></ul>"}}
        rule = {
            **BASE_RULE,
            "parser_type": "json_html",
            "url": "https://hrss.example.gov.cn/api/list",
            "params": {"pageId": "123", "paramJson": '{"pageNo":1}'},
            "json_path": {"html": "data.html"},
            "selectors": {"list_item": "li"},
        }
        req = FakeRequester(payload)
        DeclarativeRuleCollector(rule, requester=req).fetch()
        assert req.calls == ["https://hrss.example.gov.cn/api/list"]
