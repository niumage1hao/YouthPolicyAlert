"""
core/auto_extractor.py
自适应启发式列表提取器 —— 当规则里的 CSS 选择器失效时的兜底方案。

v2 改进：
1. v1 的候选容器判定过于宽松（任何含 3~80 个链接的 div 都算），
   经常把导航栏、友情链接、页脚整块当成公告列表抓进来。
   v2 引入"列表评分"：综合日期覆盖率、标题长度分布、链接文字占比来挑最像公告列表的容器。
2. v1 找到 5 条就 break，可能停在质量最差的容器上。v2 评估全部候选后选最优。
3. 补充过滤：跳过导航词、纯数字、过短标题与附件下载链接。
"""
import re
import logging
from typing import List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from core.models import PolicyItem, PolicyCategory

logger = logging.getLogger("YouthPolicyAlert.AutoExtractor")

DATE_PATTERN = re.compile(
    r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})|(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?"
)

NAV_WORDS = {
    "首页", "更多", "上一页", "下一页", "末页", "尾页", "返回顶部", "返回",
    "登录", "注册", "搜索", "网站地图", "联系我们", "关于我们", "版权声明",
    "无障碍", "长辈版", "English", "繁體", "打印", "关闭", "下载", "分享",
}

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".css", ".js", ".ico", ".exe",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z",
)


class AutoContentExtractor:
    """自适应启发式列表提取器"""

    def __init__(
        self,
        base_url: str,
        default_city: str = "全国",
        source_name: str = "政务官网",
        district: str = "全市",
    ):
        self.base_url = base_url
        self.default_city = default_city
        self.source_name = source_name
        self.district = district

    # ------------------------------------------------------------------
    def extract_from_html(self, html_text: str, category: str = "housing") -> List[PolicyItem]:
        """从任意 HTML 页面中自适应抽取公告列表"""
        soup = BeautifulSoup(html_text, "lxml")

        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "iframe"]):
            tag.decompose()

        candidates = self._find_candidate_containers(soup)
        if not candidates:
            return []

        # 对每个候选容器打分，选最像"公告列表"的那个
        scored: List[Tuple[float, List[PolicyItem]]] = []
        for container in candidates:
            rows = self._extract_rows(container)
            items = self._rows_to_items(rows, category)
            if len(items) >= 2:
                scored.append((self._score_list(items), items))

        if not scored:
            return []

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best_items = scored[0]
        logger.debug(f"启发式提取选中最佳容器，评分 {best_score:.2f}，条目 {len(best_items)}")
        return best_items[:40]

    # ------------------------------------------------------------------
    def _find_candidate_containers(self, soup: BeautifulSoup) -> List[Tag]:
        """找出所有可能是公告列表的容器"""
        candidates: List[Tag] = []
        for tag_name in ("ul", "tbody", "table", "div", "ol"):
            for el in soup.find_all(tag_name):
                anchors = el.find_all("a", href=True)
                if 3 <= len(anchors) <= 100:
                    candidates.append(el)
        return candidates

    def _extract_rows(self, container: Tag) -> List[Tag]:
        """从容器中取出"行"元素"""
        rows = container.find_all(["li", "tr"], recursive=False)
        if len(rows) < 2:
            rows = container.find_all(["li", "tr"])
        if len(rows) < 2:
            rows = container.find_all(["div", "p"], recursive=False)
        return rows

    def _rows_to_items(self, rows: List[Tag], category: str) -> List[PolicyItem]:
        items: List[PolicyItem] = []
        seen_urls = set()

        cat_enum = PolicyCategory.coerce(category)

        for row in rows:
            anchor = row.find("a", href=True)
            if anchor is None:
                continue

            title = (anchor.get("title") or "").strip()
            if not title or len(title) < 4:
                title = anchor.get_text(strip=True)

            href = (anchor.get("href") or "").strip()

            if not self._is_valid(title, href):
                continue

            full_url = urljoin(self.base_url, href)
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            publish_date = self._parse_date(row.get_text(" ", strip=True))

            try:
                items.append(PolicyItem(
                    title=title,
                    url=full_url,
                    city=self.default_city,
                    district=self.district,
                    source_name=self.source_name,
                    category=cat_enum,
                    publish_date=publish_date,
                ))
            except Exception:
                continue

            if len(items) >= 40:
                break

        return items

    @staticmethod
    def _is_valid(title: str, href: str) -> bool:
        if not title or not href:
            return False
        if len(title) < 8:
            return False
        if title.strip() in NAV_WORDS:
            return False
        if any(w in title for w in ("上一页", "下一页", "更多>>", "返回顶部")):
            return False
        if title.strip().isdigit():
            return False
        low = href.lower()
        if low.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            return False
        if low.split("?")[0].endswith(SKIP_EXTENSIONS):
            return False
        return True

    @staticmethod
    def _parse_date(text: str) -> Optional[str]:
        m = DATE_PATTERN.search(text or "")
        if not m:
            return None
        if m.group(1):
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _score_list(items: List[PolicyItem]) -> float:
        """
        给一组提取结果打分，用来判断这个容器是不是真的公告列表。
        真实公告列表的特征：条目多、带日期比例高、标题长度适中且分布集中。
        """
        if not items:
            return 0.0

        count = len(items)
        score = min(count / 10.0, 2.0)  # 条目数量分（封顶 2 分）

        # 带日期的比例：公告列表通常每行都有发布日期
        dated = sum(1 for it in items if it.publish_date)
        score += (dated / count) * 3.0

        # 标题平均长度：公告标题通常 15~60 字，导航项则很短
        avg_len = sum(len(it.title) for it in items) / count
        if 12 <= avg_len <= 70:
            score += 2.0
        elif avg_len < 8:
            score -= 1.5

        # 标题长度方差小说明是同质列表
        variance = sum((len(it.title) - avg_len) ** 2 for it in items) / count
        if variance < 400:
            score += 1.0

        return score
