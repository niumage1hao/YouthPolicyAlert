"""
core/rule_engine.py
声明式规则采集引擎。

v2 相比 v1 的改进：
1. 【并发采集】v1 串行抓取，每站带 1.5~3s 延时，20 个城市要跑好几分钟。
   v2 用线程池并发（不同域名并行、同域名仍串行且保持礼貌间隔），整体快 5~8 倍。
2. 【日期提取增强】v1 只在 date 选择器指定时才取日期，且只认一种格式。
   v2 在选择器缺失/未命中时自动从整行文本中兜底提取，支持多种中文日期写法。
3. 【链接质量校验】过滤掉 javascript:、mailto:、锚点、附件下载等非公告链接。
4. 【分页支持】可选抓取列表页的前 N 页。
5. 【规则校验】跑之前先检查规则配置是否合法，配置写错时给出清晰提示而不是静默返回 0 条。
"""
import re
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from core.models import PolicyItem, PolicyCategory
from core.requester import BaseRequester, default_requester, RequestNotFound, RequestBlocked

logger = logging.getLogger("YouthPolicyAlert.RuleEngine")

# 多种中文日期写法
DATE_PATTERNS = [
    re.compile(r"(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})"),
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"(\d{4})(\d{2})(\d{2})(?!\d)"),
]

# 明显不是公告正文的链接
SKIP_HREF_PREFIXES = ("javascript:", "mailto:", "tel:", "#", "data:")
SKIP_HREF_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".css", ".js", ".ico", ".exe",
    # 附件类：正文提取器解析不了，且标题通常与所属公告重复
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z",
)

# 明显是导航/分页而非公告的标题
NAV_TITLE_PATTERNS = re.compile(
    r"^(首页|上一页|下一页|末页|尾页|更多|返回|下载|打印|关闭|登录|注册|English|无障碍|长辈版|"
    r"\d+|>+|<+|\.{2,}|全部|详情)$"
)


def parse_date_text(text: str) -> Optional[str]:
    """从任意文本中提取第一个合法日期，统一成 YYYY-MM-DD"""
    if not text:
        return None
    for pattern in DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
            except (ValueError, IndexError):
                continue
    return None


def validate_rule(rule: Dict[str, Any]) -> List[str]:
    """校验规则配置，返回问题列表（空列表 = 配置合法）"""
    problems = []
    if not rule.get("url"):
        problems.append("缺少 url 字段")
    elif not str(rule["url"]).startswith(("http://", "https://")):
        problems.append(f"url 必须以 http:// 或 https:// 开头，当前为: {rule['url']}")

    if not rule.get("city"):
        problems.append("缺少 city 字段")
    if not rule.get("source_name"):
        problems.append("缺少 source_name 字段")

    parser_type = str(rule.get("parser_type", "html")).lower()
    if parser_type not in ("html", "json", "json_html"):
        problems.append(f"parser_type 只支持 html、json 或 json_html，当前为: {parser_type}")

    if parser_type == "html" and not rule.get("selectors", {}).get("list_item"):
        # 不算致命错误：引擎会走启发式兜底
        pass

    return problems


class DeclarativeRuleCollector:
    """单个数据源的声明式采集器"""

    def __init__(self, rule: Dict[str, Any], requester: Optional[BaseRequester] = None):
        self.rule = rule
        self.requester = requester or default_requester
        self.city = rule.get("city", "未知城市")
        self.district = rule.get("district", "全市")
        self.source_name = rule.get("source_name", "政务公开")
        self.category = PolicyCategory.coerce(rule.get("category", PolicyCategory.OTHER.value))
        self.url = rule.get("url", "")
        self.parser_type = str(rule.get("parser_type", "html")).lower()
        self.encoding = rule.get("encoding")
        self.max_pages = int(rule.get("max_pages", 1) or 1)
        self.page_pattern = rule.get("page_pattern")  # 如 ".../index_{page}.html"

    # ------------------------------------------------------------------
    def fetch(self) -> List[PolicyItem]:
        """执行采集"""
        problems = validate_rule(self.rule)
        if problems:
            raise ValueError(f"规则配置有误 [{self.city}-{self.source_name}]: {'; '.join(problems)}")

        logger.info(f"🔍 抓取 [{self.city}] {self.source_name} -> {self.url}")

        if self.parser_type == "json":
            return self._parse_json()
        if self.parser_type == "json_html":
            items = self._parse_json_html()
            return self._deduplicate(items)

        items = self._parse_html_page(self.url)

        # 可选：继续抓分页
        if self.max_pages > 1 and self.page_pattern:
            for page in range(2, self.max_pages + 1):
                page_url = urljoin(self.url, self.page_pattern.replace("{page}", str(page)))
                try:
                    items.extend(self._parse_html_page(page_url))
                except Exception as e:
                    logger.debug(f"分页 {page} 抓取失败（忽略）: {e}")
                    break

        return self._deduplicate(items)

    def _deduplicate(self, items: List[PolicyItem]) -> List[PolicyItem]:
        """同一数据源内部按 PolicyItem 唯一键去重。"""
        deduped: List[PolicyItem] = []
        seen = set()
        for it in items:
            key = it.unique_id
            if key not in seen:
                seen.add(key)
                deduped.append(it)

        logger.info(f"✅ [{self.city}-{self.source_name}] 解析出 {len(deduped)} 条公告")
        return deduped

    # ------------------------------------------------------------------
    def _parse_html_page(self, page_url: str) -> List[PolicyItem]:
        html_content = self.requester.get_text(page_url, forced_encoding=self.encoding)
        return self._parse_html_content(html_content, page_url)

    def _parse_html_content(self, html_content: str, page_url: str) -> List[PolicyItem]:
        soup = BeautifulSoup(html_content, "lxml")

        selectors = self.rule.get("selectors", {}) or {}
        list_selector = selectors.get("list_item")
        title_selector = selectors.get("title", "a")
        link_selector = selectors.get("link", "a")
        date_selector = selectors.get("date")

        items: List[PolicyItem] = []

        if list_selector:
            try:
                elements = soup.select(list_selector)
            except Exception as e:
                logger.warning(f"选择器语法错误 [{list_selector}]: {e}")
                elements = []

            for el in elements:
                item = self._build_item(el, page_url, title_selector, link_selector, date_selector)
                if item:
                    items.append(item)

        # 选择器没命中 → 启发式兜底
        if not items:
            logger.info(f"ℹ️ [{self.city}-{self.source_name}] 选择器未命中，启用自适应启发式提取...")
            from core.auto_extractor import AutoContentExtractor
            extractor = AutoContentExtractor(
                base_url=page_url, default_city=self.city,
                source_name=self.source_name, district=self.district,
            )
            items = extractor.extract_from_html(html_content, category=self.category.value)

        return items

    def _parse_json_html(self) -> List[PolicyItem]:
        """解析 JSON 接口返回的 HTML 片段（杭州政务站等常见实现）。"""
        params = self.rule.get("params") or None
        data = self.requester.get_json(self.url, params=params)
        json_path = self.rule.get("json_path", {}) or {}
        html_key = json_path.get("html", "data.html")

        current: Any = data
        for part in str(html_key).split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = ""
                break

        if not isinstance(current, str) or not current.strip():
            logger.warning(f"[{self.city}-{self.source_name}] JSON 路径 '{html_key}' 未定位到 HTML")
            return []
        return self._parse_html_content(current, self.url)

    def _build_item(
        self,
        el,
        base_url: str,
        title_selector: str,
        link_selector: str,
        date_selector: Optional[str],
    ) -> Optional[PolicyItem]:
        try:
            title_el = el.select_one(title_selector) if title_selector else el
            link_el = el.select_one(link_selector) if link_selector else el

            # 选择器没选中时，退回到行内第一个 <a>
            if title_el is None or link_el is None:
                anchor = el.find("a", href=True)
                if anchor is None:
                    return None
                title_el = title_el or anchor
                link_el = link_el or anchor

            # 标题优先取 title 属性（政务站长标题常被 CSS 截断，title 属性里才是全的）
            title = (link_el.get("title") or "").strip() if hasattr(link_el, "get") else ""
            if not title or len(title) < 4:
                title = title_el.get_text(strip=True)

            href = (link_el.get("href") or "").strip()
            if not self._is_valid_link(title, href):
                return None

            full_url = urljoin(base_url, href)

            publish_date = None
            if date_selector:
                try:
                    date_el = el.select_one(date_selector)
                    if date_el:
                        publish_date = parse_date_text(date_el.get_text(strip=True))
                except Exception:
                    pass
            # 兜底：从整行文本里找日期
            if not publish_date:
                publish_date = parse_date_text(el.get_text(" ", strip=True))

            return PolicyItem(
                title=title,
                url=full_url,
                city=self.city,
                district=self.district,
                source_name=self.source_name,
                category=self.category,
                publish_date=publish_date,
            )
        except Exception as e:
            logger.debug(f"单条解析失败（跳过）: {e}")
            return None

    @staticmethod
    def _is_valid_link(title: str, href: str) -> bool:
        if not title or not href:
            return False
        if len(title) < 6:
            return False
        if NAV_TITLE_PATTERNS.match(title.strip()):
            return False
        low = href.lower()
        if low.startswith(SKIP_HREF_PREFIXES):
            return False
        if low.split("?")[0].endswith(SKIP_HREF_EXTENSIONS):
            return False
        return True

    # ------------------------------------------------------------------
    def _parse_json(self) -> List[PolicyItem]:
        data = self.requester.get_json(self.url, params=self.rule.get("params") or None)
        json_path = self.rule.get("json_path", {}) or {}
        list_key = json_path.get("list", "data")
        title_key = json_path.get("title", "title")
        link_key = json_path.get("link", "url")
        date_key = json_path.get("date", "date")

        current = data
        for part in str(list_key).split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list):
                break
            else:
                current = []
                break

        if not isinstance(current, list):
            logger.warning(f"[{self.city}-{self.source_name}] JSON 路径 '{list_key}' 未定位到数组")
            return []

        items: List[PolicyItem] = []
        for raw in current:
            if not isinstance(raw, dict):
                continue
            title = str(raw.get(title_key, "")).strip()
            href = str(raw.get(link_key, "")).strip()
            if not title or not href or len(title) < 6:
                continue
            items.append(PolicyItem(
                title=title,
                url=urljoin(self.url, href),
                city=self.city,
                district=self.district,
                source_name=self.source_name,
                category=self.category,
                publish_date=parse_date_text(str(raw.get(date_key, ""))),
            ))
        return items


# ---------------------------------------------------------------------------
# 并发调度
# ---------------------------------------------------------------------------

class CollectionResult:
    """单个数据源的采集结果（成功或失败）"""

    def __init__(self, rule: Dict[str, Any]):
        self.rule = rule
        self.city = rule.get("city", "")
        self.source_name = rule.get("source_name", "")
        self.items: List[PolicyItem] = []
        self.error: Optional[str] = None
        self.error_type: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def status(self) -> str:
        return "SUCCESS" if self.ok else "FAILED"


def collect_all(
    rules: List[Dict[str, Any]],
    requester: Optional[BaseRequester] = None,
    max_workers: int = 5,
) -> List[CollectionResult]:
    """
    并发采集全部数据源。

    每个数据源都在独立的 try 中执行（沙盒隔离），单站失败不影响其他站；
    requester 内部按域名加锁，因此并发不会导致对同一政务站的请求变密集。
    """
    req = requester or default_requester
    results: List[CollectionResult] = []

    if not rules:
        return results

    workers = max(1, min(max_workers, len(rules)))
    logger.info(f"📋 开始并发采集 {len(rules)} 个数据源（并发度 {workers}）...")

    def run_one(rule: Dict[str, Any]) -> CollectionResult:
        result = CollectionResult(rule)
        try:
            collector = DeclarativeRuleCollector(rule=rule, requester=req)
            result.items = collector.fetch()
        except RequestNotFound as e:
            result.error, result.error_type = str(e), "NOT_FOUND"
        except RequestBlocked as e:
            result.error, result.error_type = str(e), "BLOCKED"
        except ValueError as e:
            result.error, result.error_type = str(e), "BAD_RULE"
        except Exception as e:
            result.error, result.error_type = f"{type(e).__name__}: {e}", "ERROR"
        return result

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_one, r): r for r in rules}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                rule = futures[future]
                failed = CollectionResult(rule)
                failed.error, failed.error_type = f"调度异常: {e}", "ERROR"
                results.append(failed)

    ok_count = sum(1 for r in results if r.ok)
    total_items = sum(len(r.items) for r in results)
    logger.info(f"📦 采集完成: {ok_count}/{len(results)} 个数据源成功，共 {total_items} 条原始公告")
    return results


def enrich_with_details(
    items: List[PolicyItem],
    requester: Optional[BaseRequester] = None,
    max_details: int = 20,
    max_workers: int = 3,
) -> List[PolicyItem]:
    """
    对命中的政策抓取详情页并提取干货字段。

    只对通过筛选的少量条目执行，且有硬上限 —— 既拿到关键信息，又不给政务服务器添负担。
    单条失败不影响其他条目，失败时该条保留标题层面已有的信息。
    """
    from core.extractor import detail_extractor

    if not items:
        return items

    req = requester or default_requester
    targets = items[:max_details]
    if len(items) > max_details:
        logger.info(f"详情页提取限额 {max_details} 条，其余 {len(items) - max_details} 条保留标题级信息")

    def fetch_one(item: PolicyItem) -> PolicyItem:
        try:
            html = req.get_text(item.url)
            return detail_extractor.enrich_from_html(item, html)
        except Exception as e:
            logger.debug(f"详情页抓取失败（保留标题信息）{item.url}: {e}")
            return item

    workers = max(1, min(max_workers, len(targets)))
    enriched_map = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, it): idx for idx, it in enumerate(targets)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                enriched_map[idx] = future.result()
            except Exception:
                enriched_map[idx] = targets[idx]

    result = [enriched_map.get(i, targets[i]) for i in range(len(targets))] + items[max_details:]
    success = sum(1 for it in result if it.detail_fetched)
    logger.info(f"🔎 详情页深度提取完成: {success}/{len(targets)} 条成功获取到关键信息")
    return result
