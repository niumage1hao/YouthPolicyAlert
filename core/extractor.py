"""
core/extractor.py
详情页"干货"提取器 —— 从公告正文中抽出毕业生真正关心的关键信息。

为什么需要它：
  v1 的 enrich_item() 只从【标题】里做正则匹配，而政务公告标题几乎从不包含
  "补贴多少钱""什么时候截止""什么学历能申请"这些信息。
  结果就是推送卡片上"门槛/额度/截止日期"永远是"详见官方正文"，
  用户还是得自己点进去一条条读 —— 这等于没有解决信息差问题。

  v2 对【命中的】政策额外抓取一次详情页，从正文里提取：
    - 申报截止日期 / 受理时间段
    - 补贴金额（每月X元 / 一次性X万元）
    - 房源套数
    - 学历与身份门槛
    - 年龄限制
    - 申报入口（线上系统网址 / 办理窗口）
    - 一句话摘要

  为保持对政务服务器的礼貌，只对通过相关性筛选的少量条目抓详情，且有数量上限。
"""
import re
import logging
from typing import Optional, List, Dict, Tuple

from bs4 import BeautifulSoup

from core.models import PolicyItem

logger = logging.getLogger("YouthPolicyAlert.Extractor")

# 中文数字转换（政务公文里"三十日""十五个工作日"很常见）
_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# --- 正文容器候选选择器（覆盖国内主流政务 CMS：用友、创宇、中通、TRS 等）---
CONTENT_SELECTORS = [
    "div.article-content", "div.articleCont", "div.art_content", "div.content_box",
    "div.TRS_Editor", "div.trs_editor", "div#zoom", "div#Zoom", "div.zoom",
    "div.article", "div.content", "div.detail-content", "div.detail_content",
    "div.view-content", "div.wznr", "div.xxgk_content", "div.news_content",
    "div.pages_content", "div.conTxt", "div.text", "article",
]

DATE_RE = r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
MD_RE = r"(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"


def _normalize_date(y: str, m: str, d: str) -> Optional[str]:
    try:
        yi, mi, di = int(y), int(m), int(d)
        if not (1 <= mi <= 12 and 1 <= di <= 31):
            return None
        return f"{yi:04d}-{mi:02d}-{di:02d}"
    except (ValueError, TypeError):
        return None


class DetailExtractor:
    """从公告详情页 HTML 中提取结构化关键信息"""

    # --------------------------------------------------------------
    # 正文定位
    # --------------------------------------------------------------
    def extract_main_text(self, html: str) -> str:
        """从整页 HTML 中定位正文区域并返回纯文本"""
        soup = BeautifulSoup(html, "lxml")

        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
            tag.decompose()

        # 1. 优先使用已知的正文容器选择器
        for selector in CONTENT_SELECTORS:
            node = soup.select_one(selector)
            if node:
                text = node.get_text("\n", strip=True)
                if len(text) >= 120:
                    return self._clean_text(text)

        # 2. 兜底：挑选文字量最大的 div/td（正文通常是全页最"重"的块）
        best_text = ""
        for node in soup.find_all(["div", "td", "section"]):
            # 跳过嵌套容器，只看叶子密度较高的块
            text = node.get_text("\n", strip=True)
            if len(text) > len(best_text):
                link_text_len = sum(len(a.get_text(strip=True)) for a in node.find_all("a"))
                # 链接文字占比过高的多半是导航/列表，不是正文
                if len(text) > 0 and link_text_len / len(text) < 0.35:
                    best_text = text

        if len(best_text) >= 120:
            return self._clean_text(best_text)

        return self._clean_text(soup.get_text("\n", strip=True))

    @staticmethod
    def _clean_text(text: str) -> str:
        text = re.sub(r"[ \t　]+", " ", text)
        text = re.sub(r"\n{2,}", "\n", text)
        return text.strip()

    # --------------------------------------------------------------
    # 各字段提取
    # --------------------------------------------------------------
    def extract_deadline(self, text: str) -> Optional[str]:
        """
        提取申报截止时间。
        优先匹配显式"截止"表述，其次匹配"受理时间 X 至 Y"这类区间。
        """
        # 1. 显式截止表述
        explicit_patterns = [
            rf"(?:申报|申请|受理|提交|报名|登记|认租|选房)?\s*(?:截止|结束)\s*(?:日期|时间|时限)?\s*[为是：:至到]*\s*{DATE_RE}",
            rf"(?:截止|不得晚于|不迟于)\s*(?:到|至)?\s*{DATE_RE}",
            rf"{DATE_RE}\s*(?:前|之前|以前)\s*(?:提交|申报|申请|报送|完成)",
        ]
        for pat in explicit_patterns:
            m = re.search(pat, text)
            if m:
                normalized = _normalize_date(*m.groups()[:3])
                if normalized:
                    return f"{normalized} 截止"

        # 2. 时间区间："2026年8月1日至2026年8月31日"
        # 政务公告常把具体时刻紧跟在日期后面，例如
        # "2026年8月19日16:00至2026年8月31日18:00"。
        # 时刻不属于结构化日期字段，因此只允许它出现但不捕获。
        time_suffix = r"\s*(?:[0-2]?\d\s*[:：]\s*[0-5]\d)?"
        range_pat = rf"{DATE_RE}{time_suffix}\s*(?:至|到|—|-|~|起至)\s*{DATE_RE}{time_suffix}"
        m = re.search(range_pat, text)
        if m:
            g = m.groups()
            start = _normalize_date(g[0], g[1], g[2])
            end = _normalize_date(g[3], g[4], g[5])
            if start and end:
                return f"{start} 至 {end}"

        # 3. 同年简写区间："8月1日至8月31日"
        m = re.search(rf"{MD_RE}\s*(?:至|到|—|-|~)\s*{MD_RE}", text)
        if m:
            year_m = re.search(r"(20\d{2})\s*年", text)
            if year_m:
                y = year_m.group(1)
                g = m.groups()
                start = _normalize_date(y, g[0], g[1])
                end = _normalize_date(y, g[2], g[3])
                if start and end:
                    return f"{start} 至 {end}"

        # 4. 相对期限："自公告发布之日起30日内"
        m = re.search(r"(?:之日起|发布之日起|起)\s*([0-9]{1,3}|[一二三四五六七八九十]+)\s*(?:个)?\s*(工作日|日|天)内", text)
        if m:
            num = m.group(1)
            if not num.isdigit():
                num = str(self._cn_to_int(num) or num)
            return f"公告发布起 {num} {m.group(2)}内"

        return None

    @staticmethod
    def _cn_to_int(cn: str) -> Optional[int]:
        """把"三十""十五"这类中文数字转成整数"""
        if not cn:
            return None
        if cn in _CN_NUM:
            return _CN_NUM[cn]
        if "十" in cn:
            parts = cn.split("十")
            tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
            ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
            return tens * 10 + ones
        return None

    def extract_amount(self, text: str) -> Optional[str]:
        """提取补贴金额或房源套数（金额优先，这是用户最关心的数字）"""
        results: List[str] = []

        # 1. 分学历列出的补贴标准："博士10万元、硕士5万元、本科2万元"
        tiered = re.findall(
            r"(博士|硕士|研究生|本科|大专|专科|技师|高级工)[^，。；、\n]{0,12}?"
            r"([0-9]+(?:\.[0-9]+)?)\s*(万元|万|元)",
            text,
        )
        if len(tiered) >= 2:
            parts = []
            seen = set()
            for degree, num, unit in tiered[:4]:
                key = (degree, num)
                if key in seen:
                    continue
                seen.add(key)
                unit_txt = "万元" if unit in ("万元", "万") else "元"
                parts.append(f"{degree}{num}{unit_txt}")
            if parts:
                results.append(" / ".join(parts))

        # 2. 每月/每年定额补贴
        if not results:
            m = re.search(
                r"(?:每月|按月|月)\s*(?:补贴|发放|标准|租金)?\s*(?:为|是|：|:)?\s*([0-9]+(?:\.[0-9]+)?)\s*(元|万元)",
                text,
            )
            if m:
                results.append(f"每月 {m.group(1)} {m.group(2)}")

        # 3. 一次性补贴
        # 关键词和金额之间常隔着"生活补贴""给予"等修饰语（如"给予一次性生活补贴3万元"），
        # 因此允许中间出现少量非数字字符，但不跨行以免匹配到无关句子。
        if not results:
            m = re.search(
                r"(?:一次性|共计|合计|总额)[^0-9\n]{0,12}?([0-9]+(?:\.[0-9]+)?)\s*(万元|万|元)",
                text,
            )
            if m:
                unit = "万元" if m.group(2) in ("万元", "万") else "元"
                results.append(f"一次性 {m.group(1)} {unit}")

        # 4. 通用补贴标准表述
        if not results:
            m = re.search(
                r"(?:补贴|补助|资助|奖励)\s*(?:标准|金额|额度)?\s*(?:为|是|：|:)\s*([0-9]+(?:\.[0-9]+)?)\s*(万元|万|元)",
                text,
            )
            if m:
                unit = "万元" if m.group(2) in ("万元", "万") else "元"
                results.append(f"{m.group(1)} {unit}")

        # 5. 房源套数
        # 只累计带“共/合计/提供”等聚合语义的数字，避免把“其中170套、38套”
        # 这类分项与项目总数重复相加。不同单位分别累计。
        quota_pattern = re.compile(
            r"(?:共计|合计|共|房源|提供|推出|配租)"
            r"[^0-9\n]{0,12}?([0-9]{1,6})\s*(套|间|户)"
        )
        quota_totals: Dict[str, int] = {}
        for m in quota_pattern.finditer(text):
            unit = m.group(2)
            quota_totals[unit] = quota_totals.get(unit, 0) + int(m.group(1))
        for unit, total in quota_totals.items():
            results.append(f"房源 {total} {unit}")

        return " ｜ ".join(results[:2]) if results else None

    def extract_audience(self, text: str, title: str = "") -> Optional[str]:
        """提取学历与身份门槛"""
        combined = f"{title}\n{text[:2500]}"
        tags: List[str] = []

        degree_map: List[Tuple[str, str]] = [
            (r"博士(?:研究生|后)?", "博士"),
            (r"硕士(?:研究生)?|研究生", "硕士"),
            (r"本科|学士", "本科"),
            (r"大专|专科|高职", "专科"),
        ]
        for pattern, label in degree_map:
            if re.search(pattern, combined):
                tags.append(label)

        identity_map: List[Tuple[str, str]] = [
            (r"应届(?:高校)?毕业生|应届生", "应届毕业生"),
            (r"往届毕业生", "往届毕业生"),
            (r"留学(?:回国)?(?:人员|生)|海外", "留学归国"),
            (r"高层次人才|领军人才", "高层次人才"),
            (r"新就业(?:大学生|职工|无房职工)", "新就业职工"),
            (r"技能人才|技师|高级工", "技能人才"),
            (r"创业(?:者|人员|团队)", "创业人员"),
        ]
        for pattern, label in identity_map:
            if re.search(pattern, combined):
                tags.append(label)

        # 毕业年限要求："毕业2年内""近三年毕业"
        m = re.search(r"毕业\s*([0-9]|[一二三四五])\s*年(?:以)?内", combined)
        if m:
            num = m.group(1)
            if not num.isdigit():
                num = str(self._cn_to_int(num) or num)
            tags.append(f"毕业{num}年内")

        if not tags:
            return None

        # 去重并保序
        seen, ordered = set(), []
        for t in tags:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        return " / ".join(ordered[:5])

    def extract_age_limit(self, text: str) -> Optional[str]:
        """提取年龄限制"""
        patterns = [
            r"年龄\s*(?:在)?\s*([0-9]{2})\s*(?:周)?岁\s*(?:及)?\s*(?:以下|以内|below)",
            r"(?:不超过|未满|不满)\s*([0-9]{2})\s*(?:周)?岁(?!\s*的?\s*(?:子女|儿童|未成年人))",
            r"年龄\s*([0-9]{2})\s*[-—~至]\s*([0-9]{2})\s*(?:周)?岁",
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                groups = [g for g in m.groups() if g]
                if len(groups) == 2:
                    return f"{groups[0]}-{groups[1]}周岁"
                return f"{groups[0]}周岁以下"
        return None

    def extract_apply_channel(self, text: str) -> Optional[str]:
        """提取申报入口：优先线上系统网址，其次办理平台名称"""
        m = re.search(r"(https?://[^\s，。；、）)】\"'<>]{8,120})", text)
        if m:
            url = m.group(1).rstrip(".,;")
            if not re.search(r"\.(jpg|jpeg|png|gif|css|js|ico|pdf)$", url, re.I):
                return url

        m = re.search(
            r"(?:登录|登陆|通过|进入|访问)\s*[“\"']?([^\s，。；、“”\"'\n]{4,30}?"
            r"(?:网|平台|系统|APP|app|小程序|公众号|窗口))[”\"']?",
            text,
        )
        if m:
            return m.group(1).strip()
        return None

    def extract_summary(self, text: str, max_len: int = 130) -> Optional[str]:
        """取正文开头有实质内容的一段作为摘要"""
        for line in text.split("\n"):
            line = line.strip()
            if len(line) < 25:
                continue
            # 跳过纯发文字号、机构落款之类
            if re.match(r"^[〔\[（(]?\d{4}[〕\]）)]?\s*第?\s*\d+\s*号", line):
                continue
            if re.match(r"^(各|附件|来源|发布时间|字体|打印|分享|索引号)", line):
                continue
            return line[:max_len] + ("…" if len(line) > max_len else "")
        return None

    # --------------------------------------------------------------
    # 对外主入口
    # --------------------------------------------------------------
    def enrich_from_html(self, item: PolicyItem, html: str) -> PolicyItem:
        """用详情页 HTML 补全 PolicyItem 的关键字段"""
        try:
            text = self.extract_main_text(html)
        except Exception as e:
            logger.debug(f"正文解析失败 {item.url}: {e}")
            return item

        if not text or len(text) < 40:
            return item

        deadline = self.extract_deadline(text)
        if deadline:
            item.deadline = deadline

        amount = self.extract_amount(text)
        if amount:
            item.amount_or_quota = amount

        audience = self.extract_audience(text, item.title)
        if audience:
            item.target_audience = audience

        age = self.extract_age_limit(text)
        if age:
            item.age_limit = age

        channel = self.extract_apply_channel(text)
        if channel:
            item.apply_channel = channel

        summary = self.extract_summary(text)
        if summary:
            item.raw_content = summary

        item.detail_fetched = True
        return item


# 全局单例
detail_extractor = DetailExtractor()
