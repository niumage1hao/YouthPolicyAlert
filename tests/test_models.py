"""
测试 core/models.py —— 去重指纹的正确性是"绝不重复推送"承诺的地基。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date

import pytest

from core.models import PolicyItem, PolicyCategory, normalize_url, normalize_title


def make(title="2026年第三批人才公寓配租公告", url="https://zjj.sz.gov.cn/a/b.html", city="深圳", **kw):
    return PolicyItem(title=title, url=url, city=city, source_name="深圳市住房保障署", **kw)


class TestNormalizeUrl:
    def test_strips_fragment(self):
        assert normalize_url("https://a.gov.cn/p.html#top") == normalize_url("https://a.gov.cn/p.html")

    def test_strips_timestamp_params(self):
        """政务站常在链接尾部挂时间戳，v1 会因此把同一公告反复当新政策推送"""
        a = normalize_url("https://a.gov.cn/p.html?t=1699999999")
        b = normalize_url("https://a.gov.cn/p.html?t=1700000000")
        assert a == b

    def test_strips_tracking_params(self):
        a = normalize_url("https://a.gov.cn/p.html?utm_source=wx&spm=abc")
        assert a == normalize_url("https://a.gov.cn/p.html")

    def test_keeps_meaningful_params(self):
        """内容相关的参数必须保留，否则不同公告会被误判为同一条"""
        a = normalize_url("https://a.gov.cn/detail?id=100")
        b = normalize_url("https://a.gov.cn/detail?id=200")
        assert a != b

    def test_param_order_irrelevant(self):
        a = normalize_url("https://a.gov.cn/d?id=1&cat=2")
        b = normalize_url("https://a.gov.cn/d?cat=2&id=1")
        assert a == b

    def test_scheme_and_case_unified(self):
        a = normalize_url("HTTP://ZJJ.SZ.GOV.CN/Path/")
        b = normalize_url("https://zjj.sz.gov.cn/Path/")
        assert a == b

    def test_index_filename_collapsed(self):
        a = normalize_url("https://a.gov.cn/tzgg/index.html")
        b = normalize_url("https://a.gov.cn/tzgg/")
        assert a == b

    def test_default_ports_stripped(self):
        assert normalize_url("https://a.gov.cn:443/p") == normalize_url("https://a.gov.cn/p")
        assert normalize_url("http://a.gov.cn:80/p") == normalize_url("http://a.gov.cn/p")

    def test_empty_input(self):
        assert normalize_url("") == ""


class TestUniqueId:
    def test_same_policy_different_timestamps_same_id(self):
        """核心回归测试：这正是 v1 会重复推送的场景"""
        a = make(url="https://a.gov.cn/notice/123.html?t=111")
        b = make(url="https://a.gov.cn/notice/123.html?t=999")
        assert a.unique_id == b.unique_id

    def test_different_policies_different_id(self):
        a = make(url="https://a.gov.cn/notice/123.html")
        b = make(url="https://a.gov.cn/notice/456.html")
        assert a.unique_id != b.unique_id

    def test_dedup_key_catches_url_change(self):
        """同城市同标题换了个链接（政务站改版常见），应被二级去重识别"""
        a = make(url="https://old.gov.cn/n/1.html")
        b = make(url="https://new.gov.cn/article/999.shtml")
        assert a.unique_id != b.unique_id
        assert a.dedup_key == b.dedup_key

    def test_dedup_key_city_sensitive(self):
        """不同城市的同名公告是两条真实政策，不能被合并"""
        a = make(city="深圳")
        b = make(city="杭州")
        assert a.dedup_key != b.dedup_key

    def test_title_punctuation_ignored(self):
        a = make(title="关于印发《人才公寓管理办法》的通知")
        b = make(title="关于印发人才公寓管理办法的通知")
        assert a.dedup_key == b.dedup_key


class TestDateHandling:
    @pytest.mark.parametrize("raw,expected", [
        ("2026-08-20", date(2026, 8, 20)),
        ("2026/08/20", date(2026, 8, 20)),
        ("2026.8.20", date(2026, 8, 20)),
        ("2026年8月20日", date(2026, 8, 20)),
    ])
    def test_parses_formats(self, raw, expected):
        assert make(publish_date=raw).publish_date_obj == expected

    def test_invalid_date_returns_none(self):
        assert make(publish_date="近期").publish_date_obj is None
        assert make(publish_date=None).publish_date_obj is None

    def test_age_days(self):
        item = make(publish_date="2026-08-01")
        assert item.age_days(today=date(2026, 8, 21)) == 20

    def test_age_days_unknown(self):
        assert make(publish_date=None).age_days(today=date(2026, 8, 21)) is None


class TestPolicyItem:
    def test_title_whitespace_normalized(self):
        assert make(title="  人才公寓   配租  公告 ").title == "人才公寓 配租 公告"

    def test_empty_title_rejected(self):
        with pytest.raises(Exception):
            make(title="   ")

    def test_category_coerce_invalid(self):
        """规则文件里 category 写错不该让整轮采集崩掉"""
        assert PolicyCategory.coerce("不存在的分类") == PolicyCategory.OTHER
        assert PolicyCategory.coerce("housing") == PolicyCategory.HOUSING
        assert PolicyCategory.coerce(None) == PolicyCategory.OTHER

    def test_has_actionable_detail(self):
        assert not make().has_actionable_detail()
        assert make(amount_or_quota="每月1500元").has_actionable_detail()
        assert make(deadline="2026-09-30 截止").has_actionable_detail()
