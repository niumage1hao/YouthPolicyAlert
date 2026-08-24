"""
core/models.py
统一领域数据模型与枚举定义

v2 变更要点：
1. unique_id 改为基于【归一化 URL】计算，杜绝因 ?t=时间戳 / #锚点 / 大小写域名
   导致同一条政策被反复识别为"新政策"而重复推送。
2. 新增 dedup_key（城市+标题指纹），用于捕捉"同一政策换了个链接重发"的情况。
3. 新增 relevance_score / detail_fetched / age_days 等字段，支撑打分过滤与干货提取。
"""
import re
import hashlib
from datetime import datetime, date
from enum import Enum
from typing import Optional, List, Dict, Any
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from pydantic import BaseModel, Field, field_validator


# ----------------------------------------------------------------------------
# URL 归一化：去重正确性的基石
# ----------------------------------------------------------------------------

# 政务网站常见的与内容无关的查询参数（时间戳、埋点、会话）
_VOLATILE_QUERY_KEYS = {
    "t", "_t", "timestamp", "ts", "_", "rnd", "random", "r",
    "from", "source", "spm", "share", "shareid", "sharesource",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "jsessionid", "sessionid", "sid", "phpsessid",
}

# 常见首页文件名，/index.html 与 / 视为同一页
_INDEX_FILENAMES = {"index.html", "index.htm", "index.shtml", "index.jsp", "index.php", "default.html"}


def normalize_url(raw_url: str) -> str:
    """
    把同一篇公告的各种 URL 写法收敛成唯一形式，用于稳定去重。

    处理内容：
      - 去掉 #fragment
      - 去掉时间戳 / 埋点 / 会话类查询参数，其余参数按 key 排序
      - 域名转小写、去掉默认端口
      - 去掉末尾的 index.html 之类的默认文件名
      - http/https 视为同一资源（很多政务站两种协议都能访问同一页）
    """
    if not raw_url:
        return ""

    url = raw_url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    scheme = (parts.scheme or "http").lower()
    # 统一协议，避免 http/https 两份记录
    canonical_scheme = "https" if scheme in ("http", "https") else scheme

    netloc = parts.netloc.lower()
    # 去掉默认端口
    if netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = parts.path or "/"
    # 折叠重复斜杠
    path = re.sub(r"/{2,}", "/", path)
    # 去掉默认首页文件名
    segments = path.split("/")
    if segments and segments[-1].lower() in _INDEX_FILENAMES:
        segments[-1] = ""
        path = "/".join(segments)
    if not path:
        path = "/"

    # 过滤易变查询参数
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _VOLATILE_QUERY_KEYS
    ]
    query = urlencode(sorted(kept))

    return urlunsplit((canonical_scheme, netloc, path, query, ""))


def normalize_title(title: str) -> str:
    """标题归一化：去掉空白与常见修饰符号，用于同政策换链接的兜底识别"""
    if not title:
        return ""
    cleaned = re.sub(r"\s+", "", title)
    cleaned = re.sub(r"[【】\[\]（）()《》<>“”\"'’‘·、,，。.:：!！?？\-—_]+", "", cleaned)
    return cleaned


# ----------------------------------------------------------------------------
# 枚举
# ----------------------------------------------------------------------------

class PolicyCategory(str, Enum):
    """政策分类"""
    HOUSING = "housing"               # 保障房/人才房/保租房/公租房/安居房
    SUBSIDY = "subsidy"               # 综合补贴/津贴
    RENT_SUBSIDY = "rent_subsidy"     # 租房补贴
    LIVING_SUBSIDY = "living_subsidy" # 一次性生活补贴/落户补贴
    EMPLOYMENT = "employment"         # 就业创业/见习实习补贴
    OTHER = "other"                   # 综合政策/其他

    @classmethod
    def coerce(cls, value: Any) -> "PolicyCategory":
        """把任意输入安全地转成合法分类，绝不抛异常（规则文件里写错也不该让整轮采集崩掉）"""
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except (ValueError, AttributeError):
            return cls.OTHER


class NotifyState(int, Enum):
    """
    推送状态机 —— v2 新增，用于修复"推送失败即永久丢失"的致命缺陷。

    PENDING   : 已入库，等待推送（推送失败会保持此状态，下轮自动重试）
    SENT      : 已成功推送，不再重复打扰
    BASELINE  : 冷启动基线数据，只记录不推送（避免首次运行刷屏几百条历史公告）
    ABANDONED : 连续重试多次仍失败，放弃推送并计入健康报告，避免无限重试堆积
    """
    PENDING = 0
    SENT = 1
    BASELINE = 2
    ABANDONED = 3


class TargetAudience(str, Enum):
    """适用人群层级"""
    ALL = "all"
    COLLEGE = "college"
    BACHELOR = "bachelor"
    MASTER = "master"
    DOCTOR = "doctor"
    OVERSEAS = "overseas"


# ----------------------------------------------------------------------------
# 核心实体
# ----------------------------------------------------------------------------

class PolicyItem(BaseModel):
    """
    统一政策/福利房标准数据实体。
    所有采集器（声明式规则、启发式兜底、JSON 接口）最终都输出这个对象。
    """

    # 1. 基础识别信息
    title: str = Field(..., description="公告完整标题")
    url: str = Field(..., description="原文链接/申报入口")
    city: str = Field(..., description="所属城市")
    district: str = Field(default="全市", description="所属区县")
    source_name: str = Field(..., description="发布来源单位")
    category: PolicyCategory = Field(default=PolicyCategory.OTHER, description="政策分类")

    # 2. 提炼的核心要素（面向毕业生的"干货"，由 core/extractor.py 从正文提取）
    target_audience: str = Field(default="详见官方正文", description="适用人群/申报门槛")
    deadline: str = Field(default="以官方公告为准", description="申报截止日期/开放时间段")
    amount_or_quota: Optional[str] = Field(default=None, description="补贴金额/房源套数/租金价格")
    age_limit: Optional[str] = Field(default=None, description="年龄限制")
    apply_channel: Optional[str] = Field(default=None, description="申报入口/办理方式")
    notes: Optional[str] = Field(default=None, description="特别注意事项/批次信息")

    # 3. 系统元数据
    publish_date: Optional[str] = Field(default=None, description="官方发布日期 YYYY-MM-DD")
    raw_content: Optional[str] = Field(default="", description="正文摘要")
    relevance_score: float = Field(default=0.0, description="青年政策相关性得分")
    detail_fetched: bool = Field(default=False, description="是否已抓取详情页做深度提取")
    extra_meta: Dict[str, Any] = Field(default_factory=dict, description="扩展元数据")
    created_at: datetime = Field(default_factory=datetime.now, description="系统抓取入库时间")

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        cleaned = re.sub(r"\s+", " ", (v or "")).strip()
        if not cleaned:
            raise ValueError("标题不能为空")
        return cleaned

    @field_validator("city", "source_name")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        return (v or "").strip()

    @property
    def canonical_url(self) -> str:
        """归一化后的稳定 URL"""
        return normalize_url(self.url)

    @property
    def unique_id(self) -> str:
        """
        主键指纹：基于【归一化 URL】。
        修复了 v1 直接哈希原始 URL 导致同一政策带不同时间戳参数被反复推送的问题。
        """
        return hashlib.md5(self.canonical_url.encode("utf-8")).hexdigest()

    @property
    def dedup_key(self) -> str:
        """
        二级去重键：城市 + 归一化标题。
        用于识别"同一条政策换了个 URL 重新发布"（政务网站改版时很常见）。
        """
        base = f"{self.city}|{normalize_title(self.title)}"
        return hashlib.md5(base.encode("utf-8")).hexdigest()

    @property
    def content_fingerprint(self) -> str:
        """内容版本指纹：用于检测同链接下官方是否更新了标题或截止时间"""
        combined = f"{normalize_title(self.title)}_{self.publish_date or ''}_{self.deadline}"
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()

    @property
    def publish_date_obj(self) -> Optional[date]:
        """把发布日期字符串解析成 date 对象，解析不了返回 None"""
        if not self.publish_date:
            return None
        text = self.publish_date.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if match:
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                return None
        return None

    def age_days(self, today: Optional[date] = None) -> Optional[int]:
        """公告距今天数；无法解析发布日期时返回 None"""
        pub = self.publish_date_obj
        if pub is None:
            return None
        return ((today or date.today()) - pub).days

    def has_actionable_detail(self) -> bool:
        """是否已经提取到了对用户真正有用的关键信息"""
        return bool(
            self.amount_or_quota
            or (self.deadline and self.deadline != "以官方公告为准")
            or (self.target_audience and self.target_audience != "详见官方正文")
        )

    def to_brief_dict(self) -> Dict[str, Any]:
        """用于推送与前端展示的精简字典"""
        return {
            "title": self.title,
            "url": self.url,
            "city": self.city,
            "district": self.district,
            "source_name": self.source_name,
            "category": self.category.value,
            "target_audience": self.target_audience,
            "deadline": self.deadline,
            "amount_or_quota": self.amount_or_quota,
            "age_limit": self.age_limit,
            "apply_channel": self.apply_channel,
            "publish_date": self.publish_date or "近期",
            "notes": self.notes,
            "relevance_score": round(self.relevance_score, 2),
        }


class CollectorHealth(BaseModel):
    """单个数据源的健康状态（用于失效告警与 doctor 体检）"""
    source_name: str
    city: str
    consecutive_failures: int = 0
    last_status: str = "UNKNOWN"
    last_error: Optional[str] = None
    last_success_at: Optional[str] = None
    last_items_found: int = 0

    @property
    def is_broken(self) -> bool:
        return self.consecutive_failures >= 3
