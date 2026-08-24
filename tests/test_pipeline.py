"""
测试 core/pipeline.py —— 过滤质量决定了用户收到的是"有用提醒"还是"垃圾骚扰"。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta

import pytest

from core.models import PolicyItem, PolicyCategory
from core.pipeline import PolicyPipeline

TODAY = date(2026, 8, 24)


def make(title, category=PolicyCategory.HOUSING, publish_date="2026-08-20", source="深圳市住房保障署", city="深圳"):
    return PolicyItem(
        title=title, url=f"https://a.gov.cn/{abs(hash(title))}.html",
        city=city, source_name=source, category=category, publish_date=publish_date,
    )


@pytest.fixture
def pipe():
    return PolicyPipeline(today=TODAY, max_age_days=45)


class TestRelevantPolicies:
    """真正应该推送给毕业生的公告"""

    @pytest.mark.parametrize("title", [
        "2026年第三批人才公寓配租公告",
        "关于开展2026年高校毕业生租房补贴申报工作的通知",
        "深圳市保障性租赁住房认租公告",
        "2026年应届毕业生生活补贴发放公示",
        "青年驿站免费住宿申请指南",
        "关于新引进人才安家补贴申报的通知",
        "第五批公租房配租方案",
        "青年人才公寓房源分配方案公告",
        "关于发放高校毕业生求职创业补贴的公告",
    ])
    def test_kept(self, pipe, title):
        assert pipe.is_relevant(make(title)) is True, f"应当保留: {title}"


class TestNoisePolicies:
    """必须被过滤掉的政务噪音"""

    @pytest.mark.parametrize("title", [
        "关于XX小区物业服务招标结果的公示",
        "2026年办公设备采购中标公告",
        "关于XX公司行政处罚决定书",
        "老旧小区加装电梯施工许可公示",
        "2026年部门预算执行情况公开",
        "商品房销售价格备案公示",
        "关于开展消防演习的通知",
    ])
    def test_filtered(self, pipe, title):
        assert pipe.is_relevant(make(title)) is False, f"应当过滤: {title}"

    def test_recruitment_noise_filtered(self):
        """
        ★ 回归测试 ★
        用户现有数据库里混进了这条：
        "2026年北京市人力资源和社会保障局所属事业单位招聘退役大学生士兵拟聘用人员公示"
        它含"大学生"因而被 v1 的白名单放行，但跟安居补贴毫无关系。
        """
        pipe = PolicyPipeline(today=TODAY)
        noise = make(
            "2026年北京市人力资源和社会保障局所属事业单位招聘退役大学生士兵拟聘用人员公示",
            category=PolicyCategory.SUBSIDY, city="北京",
        )
        assert pipe.is_relevant(noise) is False

    def test_招聘公告_filtered(self, pipe):
        assert pipe.is_relevant(make("关于公开招聘高校毕业生见习岗位工作人员的公告")) is False

    def test_档案转递_filtered(self, pipe):
        """含"高校毕业生"但与安居补贴无关，弱信号不该单独越过阈值"""
        item = make("关于做好高校毕业生档案转递工作的通知", category=PolicyCategory.OTHER, source="市档案局")
        assert pipe.is_relevant(item) is False


class TestScoring:
    def test_strong_beats_weak(self, pipe):
        strong = pipe.score(make("2026年人才公寓配租公告"))
        weak = pipe.score(make("关于公示有关事项的通知", category=PolicyCategory.OTHER))
        assert strong > weak

    def test_score_recorded_on_output(self, pipe):
        out = pipe.process([make("2026年第三批人才公寓配租公告")])
        assert len(out) == 1
        assert out[0].relevance_score > 0

    def test_results_sorted_by_relevance(self, pipe):
        items = [
            make("关于住房保障有关事项的公示"),
            make("2026年青年人才公寓租房补贴配租公告"),
        ]
        out = pipe.process(items)
        assert out[0].relevance_score >= out[-1].relevance_score

    def test_threshold_configurable(self):
        item = make("住房保障公示")
        assert PolicyPipeline(today=TODAY, threshold=99).is_relevant(item) is False
        assert PolicyPipeline(today=TODAY, threshold=0.1).is_relevant(item) is True


class TestRecencyFilter:
    def test_old_policy_filtered(self, pipe):
        """网站列表里躺着的 2019 年公告不该被当新政策推送"""
        old = make("2019年人才公寓配租公告", publish_date="2019-05-01")
        assert pipe.is_relevant(old) is False

    def test_recent_policy_kept(self, pipe):
        recent = make("人才公寓配租公告", publish_date=(TODAY - timedelta(days=5)).isoformat())
        assert pipe.is_relevant(recent) is True

    def test_boundary(self, pipe):
        inside = make("人才公寓配租公告", publish_date=(TODAY - timedelta(days=45)).isoformat())
        outside = make("人才公寓配租公告", publish_date=(TODAY - timedelta(days=46)).isoformat())
        assert pipe.is_relevant(inside) is True
        assert pipe.is_relevant(outside) is False

    def test_unknown_date_allowed(self, pipe):
        """很多政务列表页不带日期，一律拦掉会漏掉真实新政策"""
        assert pipe.is_relevant(make("人才公寓配租公告", publish_date=None)) is True

    def test_disabled_when_none(self):
        p = PolicyPipeline(today=TODAY, max_age_days=None)
        assert p.is_relevant(make("人才公寓配租公告", publish_date="2015-01-01")) is True


class TestTitleCleaning:
    def test_date_prefix_removed(self, pipe):
        assert pipe.clean_title("[2026-08-20] 人才公寓配租公告") == "人才公寓配租公告"

    def test_date_suffix_removed(self, pipe):
        assert pipe.clean_title("人才公寓配租公告 2026-08-20") == "人才公寓配租公告"

    def test_whitespace_collapsed(self, pipe):
        assert pipe.clean_title("人才公寓   配租\n公告") == "人才公寓 配租 公告"


class TestEnrichment:
    def test_quota_from_title(self, pipe):
        out = pipe.process([make("2026年第三批人才公寓配租公告(共1200套)")])
        assert out and "1200" in (out[0].amount_or_quota or "")

    def test_audience_from_title(self, pipe):
        out = pipe.process([make("面向博士硕士人才的公寓配租公告")])
        assert out and "博士" in out[0].target_audience

    def test_batch_note(self, pipe):
        out = pipe.process([make("2026年第三批人才公寓配租公告")])
        assert out and "第三批" in (out[0].notes or "")

    def test_category_refined_to_rent_subsidy(self, pipe):
        out = pipe.process([make("关于发放高校毕业生租房补贴的通知", category=PolicyCategory.SUBSIDY)])
        assert out and out[0].category == PolicyCategory.RENT_SUBSIDY

    def test_category_refined_to_living_subsidy(self, pipe):
        out = pipe.process([make("应届毕业生生活补贴申报通知", category=PolicyCategory.SUBSIDY)])
        assert out and out[0].category == PolicyCategory.LIVING_SUBSIDY


class TestBatchProcessing:
    def test_mixed_batch(self, pipe):
        items = [
            make("2026年第三批人才公寓配租公告"),
            make("办公用品采购中标公告"),
            make("2019年人才公寓配租公告", publish_date="2019-01-01"),
            make("高校毕业生租房补贴申报通知"),
        ]
        out = pipe.process(items)
        titles = [i.title for i in out]
        assert len(out) == 2
        assert all("采购" not in t and "2019" not in t for t in titles)

    def test_empty_input(self, pipe):
        assert pipe.process([]) == []
