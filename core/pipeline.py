"""
core/pipeline.py
数据清洗、相关性打分与关键要素提取管道。

v2 相比 v1 的改进：
1. 【打分替代二元匹配】v1 只要标题命中任一白名单词就放行，导致
   "关于做好高校毕业生档案转递工作的通知"这类跟安居补贴无关的公告也会推送。
   v2 用加权打分 + 阈值，把"人才公寓配租"这类强信号和"高校毕业生"这类弱信号区分开。
2. 【时效过滤】v1 不看发布日期，网站列表里躺着的 2019 年公告一样会被当新政策推送。
   v2 默认只保留 max_age_days 天内的公告。
3. 【黑名单增强】补充了"招聘/考试/拟聘用/成绩/面试"等噪音 —— 用户数据库里
   混进来的"招聘退役大学生士兵拟聘用人员公示"正是被 v1 漏放行的典型。
4. 【标题预清洗】去掉政务列表常见的前后缀噪音。
"""
import re
import logging
from datetime import date
from typing import List, Optional, Dict, Tuple

from core.models import PolicyItem, PolicyCategory

logger = logging.getLogger("YouthPolicyAlert.Pipeline")


# ---------------------------------------------------------------------------
# 1. 黑名单：命中即直接排除（政务网噪音）
# ---------------------------------------------------------------------------
DEFAULT_BLACKLIST = [
    # 买卖房产类（不是青年租赁需求）
    "商品房", "配售", "认购", "产权买卖", "购房资格", "销售公示", "二手房",
    "预售许可", "网签", "限购", "房产交易", "不动产登记",
    # 招标采购与工程
    "中标", "废标", "招标", "采购", "比选", "询价", "施工许可", "规划许可",
    "监理", "造价", "竣工验收", "工程结算", "设备更新", "物业服务招标",
    "老旧小区", "加装电梯", "节能改造", "危房改造", "建筑机器人",
    # 人事招考类（v1 漏网重灾区：这类公告大量包含"高校毕业生"字样）
    "招聘", "拟聘用", "聘用人员公示", "笔试", "面试成绩", "资格复审",
    "考试成绩", "岗位表", "报考", "事业单位公开", "遴选", "选调",
    "体检结果", "拟录用", "录用公示", "退役大学生士兵",
    # 行政与党务
    "行政处罚", "行政许可", "注销", "作废", "预算执行", "决算公开",
    "三公经费", "党建", "主题党日", "党课", "消防演习", "食堂", "车辆处置",
    "双随机", "抽查结果", "信用评价", "统计公报", "人事任免",
    # 其他
    "问卷调查", "征求意见反馈", "网站年报", "值班安排", "放假通知",
]

# ---------------------------------------------------------------------------
# 2. 加权白名单：强信号 3 分，中信号 2 分，弱信号 1 分
# ---------------------------------------------------------------------------
STRONG_KEYWORDS = {
    # 直接就是青年安居/补贴动作的词 —— 命中基本可确定是目标公告
    "人才公寓": 3.0, "青年公寓": 3.0, "青年驿站": 3.0, "青年人才驿站": 3.0,
    "保障性租赁住房": 3.0, "保租房": 3.0, "人才住房": 3.0, "人才安居": 3.0,
    "公租房": 2.5, "安居房": 2.5, "租赁补贴": 3.0, "租房补贴": 3.0,
    "住房补贴": 2.5, "生活补贴": 3.0, "安家补贴": 3.0, "落户补贴": 3.0,
    "购房补贴": 1.5, "见习补贴": 2.5, "求职创业补贴": 3.0, "就业补贴": 2.5,
    "青年发展型城市": 2.0, "人才补贴": 3.0, "青年人才补贴": 3.0,
}

MEDIUM_KEYWORDS = {
    "配租": 2.0, "认租": 2.0, "轮候": 2.0, "摇号": 1.8, "选房": 1.8,
    "分配方案": 1.5, "房源": 1.5, "申领": 1.5, "申报指南": 1.5,
    "人才引进": 1.8, "新引进人才": 2.0, "青年人才": 2.0,
    "高校毕业生": 1.5, "应届毕业生": 1.8, "应届生": 1.8, "大学生": 1.2,
    "毕业生": 1.2, "留学回国": 1.5, "补贴发放": 1.8, "补贴申报": 2.0,
}

WEAK_KEYWORDS = {
    "住房保障": 1.0, "租金": 0.8, "补助": 0.8, "扶持": 0.6, "资助": 0.8,
    "就业创业": 0.8, "实习见习": 0.8, "落户": 0.8, "安居": 1.0,
    "申请": 0.3, "公示": 0.3, "通告": 0.3, "政策": 0.3,
}

# 命中这些组合可额外加分（"动作词 + 对象词"同时出现，几乎必是目标公告）
ACTION_WORDS = ["配租", "认租", "申报", "申领", "发放", "受理", "开放", "启动", "公示", "分配"]
OBJECT_WORDS = ["公寓", "保租房", "补贴", "住房", "房源", "驿站", "安居"]

DEFAULT_THRESHOLD = 2.5


class PolicyPipeline:
    """政策数据过滤、打分与增强管道"""

    def __init__(
        self,
        blacklist: Optional[List[str]] = None,
        threshold: float = DEFAULT_THRESHOLD,
        max_age_days: Optional[int] = 45,
        extra_keywords: Optional[Dict[str, float]] = None,
        today: Optional[date] = None,
    ):
        """
        :param threshold: 相关性得分阈值，低于此值的公告不推送
        :param max_age_days: 只保留 N 天内发布的公告；None 表示不做时效过滤
        :param extra_keywords: 用户自定义关键词及权重
        :param today: 注入"今天"，便于测试
        """
        self.blacklist = blacklist if blacklist is not None else DEFAULT_BLACKLIST
        self.threshold = threshold
        self.max_age_days = max_age_days
        self.today = today or date.today()

        self.keywords: Dict[str, float] = {}
        self.keywords.update(WEAK_KEYWORDS)
        self.keywords.update(MEDIUM_KEYWORDS)
        self.keywords.update(STRONG_KEYWORDS)
        if extra_keywords:
            self.keywords.update(extra_keywords)

    # ------------------------------------------------------------------
    # 标题清洗
    # ------------------------------------------------------------------
    @staticmethod
    def clean_title(title: str) -> str:
        """去掉政务列表页常见的噪音前后缀"""
        t = re.sub(r"\s+", " ", title or "").strip()
        # 去掉开头的日期前缀 "[2026-08-20] xxx"
        t = re.sub(r"^[\[\(【]\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*[\]\)】]\s*", "", t)
        # 去掉结尾的日期后缀
        t = re.sub(r"\s*[\[\(【]?\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\s*[\]\)】]?\s*$", "", t)
        # 去掉"更多>>"之类
        t = re.sub(r"(更多|详情|查看)\s*[>》]+\s*$", "", t)
        return t.strip()

    # ------------------------------------------------------------------
    # 打分
    # ------------------------------------------------------------------
    def score(self, item: PolicyItem) -> float:
        """
        计算政策与"青年安居/毕业生补贴"主题的相关性得分。
        标题权重最高，来源单位名与分类作为辅助信号。
        """
        title = self.clean_title(item.title)
        if not title:
            return 0.0

        total = 0.0
        matched: List[str] = []

        # 按关键词长度降序匹配，并跳过已命中长词的子串。
        # 否则 "高校毕业生" 会同时命中 "高校毕业生"(1.5) 和 "毕业生"(1.2)，
        # 让 "关于做好高校毕业生档案转递工作的通知" 这类无关公告靠重复计分越过阈值。
        for kw in sorted(self.keywords, key=len, reverse=True):
            if kw not in title:
                continue
            if any(kw in longer for longer in matched):
                continue
            total += self.keywords[kw]
            matched.append(kw)

        # 动作词 + 对象词共现加分
        if any(a in title for a in ACTION_WORDS) and any(o in title for o in OBJECT_WORDS):
            total += 1.2

        # 分类先验：规则已声明这是住房/补贴专栏，给一点基础分
        if item.category in (PolicyCategory.HOUSING, PolicyCategory.SUBSIDY,
                             PolicyCategory.RENT_SUBSIDY, PolicyCategory.LIVING_SUBSIDY):
            total += 0.5

        # 来源单位辅助信号（住房保障署/人才安居集团发的东西相关性天然更高）
        source = item.source_name or ""
        if any(k in source for k in ("住房保障", "人才安居", "住保", "人才服务")):
            total += 0.5

        # 过短标题多半是导航项，降权
        if len(title) < 10:
            total *= 0.5

        if matched:
            logger.debug(f"打分 {total:.1f} [{','.join(matched[:4])}] {title[:40]}")
        return total

    # ------------------------------------------------------------------
    # 过滤判定
    # ------------------------------------------------------------------
    def is_blacklisted(self, item: PolicyItem) -> Optional[str]:
        title = self.clean_title(item.title)
        for bad in self.blacklist:
            if bad in title:
                return bad
        return None

    def is_fresh(self, item: PolicyItem) -> bool:
        """
        时效判定。无法解析发布日期时按"放行"处理 —— 政务站很多列表页不带日期，
        一律拦掉会漏掉真实新政策；这类条目靠数据库去重保证只推一次。
        """
        if self.max_age_days is None:
            return True
        age = item.age_days(self.today)
        if age is None:
            return True
        if age < 0:
            # 发布日期在未来：多半是页面日期解析错位，放行但记录
            logger.debug(f"发布日期晚于今天，按新公告处理: {item.title[:30]}")
            return True
        return age <= self.max_age_days

    def is_relevant(self, item: PolicyItem) -> bool:
        """综合判定一条公告是否值得推送给用户"""
        bad = self.is_blacklisted(item)
        if bad:
            logger.debug(f"命中黑名单[{bad}]，排除: {item.title[:40]}")
            return False

        if not self.is_fresh(item):
            logger.debug(f"超出时效({self.max_age_days}天)，排除: {item.title[:40]}")
            return False

        return self.score(item) >= self.threshold

    # ------------------------------------------------------------------
    # 标题层面的轻量增强（详情页提取由 core/extractor.py 负责）
    # ------------------------------------------------------------------
    def enrich_from_title(self, item: PolicyItem) -> PolicyItem:
        """先从标题榨取能拿到的信息，作为详情页提取失败时的兜底"""
        title = self.clean_title(item.title)
        item.title = title

        audiences: List[str] = []
        for pattern, label in [
            (r"博士", "博士"), (r"硕士|研究生", "硕士"),
            (r"本科", "本科"), (r"大专|专科", "专科"), (r"应届", "应届毕业生"),
        ]:
            if re.search(pattern, title):
                audiences.append(label)
        if audiences and item.target_audience == "详见官方正文":
            item.target_audience = " / ".join(dict.fromkeys(audiences))

        if not item.amount_or_quota:
            m = re.search(r"([0-9]{1,6})\s*(套|间|户)", title)
            if m:
                item.amount_or_quota = f"房源 {m.group(1)} {m.group(2)}"

        batch = re.search(r"(第[一二三四五六七八九十百\d]+批(?:次)?)", title)
        if batch and not item.notes:
            item.notes = f"批次: {batch.group(1)}"

        return item

    def refine_category(self, item: PolicyItem) -> PolicyItem:
        """根据标题把粗粒度的 housing/subsidy 细化到更准确的分类"""
        title = item.title
        if re.search(r"租房补贴|租赁补贴|住房租赁补贴", title):
            item.category = PolicyCategory.RENT_SUBSIDY
        elif re.search(r"生活补贴|安家补贴|落户补贴|一次性.*补贴", title):
            item.category = PolicyCategory.LIVING_SUBSIDY
        elif re.search(r"见习|实习|就业创业|求职", title):
            item.category = PolicyCategory.EMPLOYMENT
        elif re.search(r"公寓|保租房|保障性租赁住房|公租房|配租|认租|房源|安居", title):
            item.category = PolicyCategory.HOUSING
        return item

    # ------------------------------------------------------------------
    # 批量处理
    # ------------------------------------------------------------------
    def process(self, items: List[PolicyItem]) -> List[PolicyItem]:
        """过滤 + 打分 + 增强，返回按相关性降序排列的结果"""
        results: List[PolicyItem] = []
        stats = {"total": len(items), "blacklisted": 0, "stale": 0, "low_score": 0, "kept": 0}

        for it in items:
            bad = self.is_blacklisted(it)
            if bad:
                stats["blacklisted"] += 1
                continue
            if not self.is_fresh(it):
                stats["stale"] += 1
                continue

            s = self.score(it)
            if s < self.threshold:
                stats["low_score"] += 1
                continue

            it.relevance_score = s
            it = self.enrich_from_title(it)
            it = self.refine_category(it)
            results.append(it)
            stats["kept"] += 1

        results.sort(key=lambda x: x.relevance_score, reverse=True)
        logger.info(
            f"🎯 清洗完成: 共 {stats['total']} 条 → 保留 {stats['kept']} 条 "
            f"(黑名单 {stats['blacklisted']} / 过期 {stats['stale']} / 相关性不足 {stats['low_score']})"
        )
        return results
