"""
测试 core/extractor.py —— 详情页干货提取。

这是 v2 相对 v1 最重要的能力增量：v1 只看标题，
导致推送卡片上"补贴多少钱/什么时候截止/什么学历能申请"永远是空的。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.models import PolicyItem, PolicyCategory
from core.extractor import DetailExtractor

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def ex():
    return DetailExtractor()


@pytest.fixture
def housing_text(ex):
    return ex.extract_main_text(load("gov_detail.html"))


@pytest.fixture
def subsidy_text(ex):
    return ex.extract_main_text(load("gov_detail_subsidy.html"))


class TestMainTextExtraction:
    def test_finds_body(self, housing_text):
        assert "人才公寓" in housing_text
        assert len(housing_text) > 200

    def test_strips_chrome(self, housing_text):
        assert "版权所有" not in housing_text or housing_text.count("首页") == 0

    def test_handles_empty(self, ex):
        assert ex.extract_main_text("<html><body></body></html>") == ""


class TestDeadline:
    def test_date_range(self, ex, housing_text):
        """申报受理时间为 2026年8月25日 至 2026年9月30日"""
        d = ex.extract_deadline(housing_text)
        assert d is not None
        assert "2026-08-25" in d and "2026-09-30" in d

    def test_explicit_deadline(self, ex, subsidy_text):
        """申报截止日期为 2026年10月15日"""
        d = ex.extract_deadline(subsidy_text)
        assert d is not None and "2026-10-15" in d

    def test_relative_deadline(self, ex):
        assert "30" in (ex.extract_deadline("请于本公告发布之日起30日内提交申请材料。") or "")

    def test_chinese_number_deadline(self, ex):
        out = ex.extract_deadline("自公告发布之日起十五个工作日内办理。")
        assert out is not None and "15" in out

    def test_no_deadline(self, ex):
        assert ex.extract_deadline("本公告自发布之日起施行。") is None

    def test_application_window_with_times(self, ex):
        text = "网上认租时间为：2026年8月19日16:00至2026年8月31日18:00。"
        out = ex.extract_deadline(text)
        assert out == "2026-08-19 至 2026-08-31"


class TestAmount:
    def test_tiered_subsidy(self, ex, subsidy_text):
        """博士10万元、硕士5万元、本科2万元、大专1万元"""
        a = ex.extract_amount(subsidy_text)
        assert a is not None
        assert "博士" in a and "10" in a

    def test_monthly_and_quota(self, ex, housing_text):
        """正文里既有分档月补贴，也有 1200 套房源"""
        a = ex.extract_amount(housing_text)
        assert a is not None
        assert "1200" in a or "3000" in a or "博士" in a

    def test_quota_extracted(self, ex):
        assert "500" in (ex.extract_amount("本批次共计推出人才公寓 500 套。") or "")

    def test_monthly_amount(self, ex):
        out = ex.extract_amount("租房补贴每月1500元，发放期限24个月。")
        assert out is not None and "1500" in out

    def test_one_time_amount(self, ex):
        out = ex.extract_amount("给予一次性生活补贴3万元。")
        assert out is not None and "3" in out

    def test_no_amount(self, ex):
        assert ex.extract_amount("请符合条件的人员按要求提交材料。") is None

    def test_sums_multiple_housing_projects(self, ex):
        text = "本批次共约208套；另一项目房源共约221套。"
        out = ex.extract_amount(text)
        assert out is not None and "429" in out and "套" in out


class TestAudience:
    def test_degrees(self, ex, housing_text):
        a = ex.extract_audience(housing_text)
        assert a is not None
        assert "本科" in a and ("硕士" in a or "博士" in a)

    def test_graduation_window(self, ex):
        out = ex.extract_audience("毕业3年内的高校毕业生优先。")
        assert out is not None and "毕业3年内" in out

    def test_fresh_graduate_identity(self, ex, subsidy_text):
        assert "应届毕业生" in (ex.extract_audience(subsidy_text) or "")

    def test_no_audience(self, ex):
        assert ex.extract_audience("本公告自发布之日起施行。") is None


class TestAgeLimit:
    def test_upper_bound(self, ex, housing_text):
        assert ex.extract_age_limit(housing_text) == "35周岁以下"

    @pytest.mark.parametrize("text,expected", [
        ("年龄45周岁以下", "45周岁以下"),
        ("年龄不超过40周岁", "40周岁以下"),
        ("年龄18-35周岁", "18-35周岁"),
    ])
    def test_variants(self, ex, text, expected):
        assert ex.extract_age_limit(text) == expected

    def test_none(self, ex):
        assert ex.extract_age_limit("无年龄要求说明。") is None

    def test_does_not_treat_child_age_as_applicant_limit(self, ex):
        text = "申请人及其配偶、未满18周岁的子女均未在本市拥有自有住房。"
        assert ex.extract_age_limit(text) is None


class TestApplyChannel:
    def test_url(self, ex, housing_text):
        c = ex.extract_apply_channel(housing_text)
        assert c is not None and "rcaj.example.gov.cn" in c

    def test_platform_name(self, ex, subsidy_text):
        c = ex.extract_apply_channel(subsidy_text)
        assert c is not None and ("公众号" in c or "人社局" in c)

    def test_ignores_image_urls(self, ex):
        assert ex.extract_apply_channel("图片地址 https://a.gov.cn/logo.png 仅供展示") is None


class TestEndToEndEnrichment:
    def test_housing_item_fully_enriched(self, ex):
        """
        ★ 核心价值测试 ★
        v1 推送出去的卡片这三个字段全是占位符；v2 必须真的填上。
        """
        item = PolicyItem(
            title="示例市2026年第三批人才公寓配租公告",
            url="https://zjj.example.gov.cn/n/101.html",
            city="示例市", source_name="示例市住房保障署",
            category=PolicyCategory.HOUSING,
        )
        assert item.has_actionable_detail() is False  # 提取前：什么干货都没有

        enriched = ex.enrich_from_html(item, load("gov_detail.html"))

        assert enriched.detail_fetched is True
        assert enriched.has_actionable_detail() is True
        assert enriched.deadline != "以官方公告为准"
        assert enriched.target_audience != "详见官方正文"
        assert enriched.amount_or_quota is not None
        assert enriched.age_limit == "35周岁以下"
        assert enriched.apply_channel is not None

    def test_subsidy_item_enriched(self, ex):
        item = PolicyItem(
            title="关于发放2026年应届高校毕业生生活补贴的公告",
            url="https://rsj.example.gov.cn/n/1.html",
            city="示例市", source_name="示例市人社局",
            category=PolicyCategory.SUBSIDY,
        )
        enriched = ex.enrich_from_html(item, load("gov_detail_subsidy.html"))
        assert "2026-10-15" in enriched.deadline
        assert enriched.amount_or_quota is not None

    def test_garbage_html_is_safe(self, ex):
        """详情页解析失败绝不能让整轮采集崩掉"""
        item = PolicyItem(
            title="测试公告标题内容", url="https://a.gov.cn/1.html",
            city="X", source_name="Y",
        )
        out = ex.enrich_from_html(item, "<html><body>短</body></html>")
        assert out.title == "测试公告标题内容"

    def test_malformed_html_is_safe(self, ex):
        item = PolicyItem(title="测试公告标题内容", url="https://a.gov.cn/1.html", city="X", source_name="Y")
        assert ex.enrich_from_html(item, "<<<>>>not html at all") is not None
