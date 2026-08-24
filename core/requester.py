"""
core/requester.py
面向中文政务网站的健壮 HTTP 客户端。

v2 相比 v1 的关键改进：
1. 【会话预热】首次访问某域名时先请求站点首页拿到 Cookie，再带着 Referer 访问目标页。
   大量政务站的 WAF（安恒/创宇/绿盟等）正是靠"无 Cookie + 无 Referer 直连内页"识别爬虫，
   预热能显著降低 403/412 概率 —— 这是 v1 硬扛 403 连试 3 次仍失败的根因。
2. 【按状态码分流重试】404/410 直接放弃（重试无意义、纯属浪费时间和对方资源）；
   403/412 换 UA + 重新预热后再试；5xx/超时才做指数退避。
3. 【按域名限速】并发抓取时对同一域名串行并保持间隔，做到"整体快、单站礼貌"。
4. 【编码探测修复】v1 对整页做 charset_normalizer 全量探测，慢且易误判。
   v2 改为：HTML meta charset > HTTP 头 > 采样探测，又快又准。
5. 【单轮内缓存】同一 URL 在一轮运行中只真正请求一次。
"""
import re
import time
import random
import logging
import threading
from typing import Optional, Dict, Any, Set
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger("YouthPolicyAlert.Requester")

# 轮换 UA：全部为真实存在的现代浏览器指纹
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)

# 不值得重试的状态码：重试也不会变，白白浪费时间和对方服务器资源
NON_RETRYABLE_STATUS = {400, 401, 404, 405, 410, 451}


class RequestBlocked(Exception):
    """明确被目标站点拒绝（403/412 等），与网络故障区分开，便于健康报告归因"""


class RequestNotFound(Exception):
    """目标页面不存在（404/410），通常意味着规则里的 URL 该更新了"""


class BaseRequester:
    """政务网站请求器（线程安全，可用于并发采集）"""

    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        min_delay: float = 1.0,
        max_delay: float = 2.5,
        verify_ssl: bool = True,
        proxy: Optional[str] = None,
        enable_warmup: bool = True,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.verify_ssl = verify_ssl
        self.enable_warmup = enable_warmup

        self._ua = random.choice(USER_AGENTS)
        client_kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            "verify": self.verify_ssl,
            "follow_redirects": True,
            "headers": {**BASE_HEADERS, "User-Agent": self._ua},
            "limits": httpx.Limits(max_connections=10, max_keepalive_connections=5),
        }
        if proxy:
            client_kwargs["proxy"] = proxy
        self.client = httpx.Client(**client_kwargs)

        # 按域名的锁与最后访问时间：并发时对同一站点串行 + 保持礼貌间隔
        self._domain_locks: Dict[str, threading.Lock] = {}
        self._domain_last_hit: Dict[str, float] = {}
        self._warmed_domains: Set[str] = set()
        self._global_lock = threading.Lock()

        # 单轮运行内的响应缓存，避免重复请求同一 URL
        self._text_cache: Dict[str, str] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 域名级限速
    # ------------------------------------------------------------------
    def _get_domain_lock(self, domain: str) -> threading.Lock:
        with self._global_lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = threading.Lock()
            return self._domain_locks[domain]

    def _polite_wait(self, domain: str):
        """确保对同一域名的两次请求之间保持随机间隔"""
        last = self._domain_last_hit.get(domain)
        gap = random.uniform(self.min_delay, self.max_delay)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < gap:
                time.sleep(gap - elapsed)
        self._domain_last_hit[domain] = time.monotonic()

    # ------------------------------------------------------------------
    # 会话预热：拿 Cookie + 建立可信 Referer
    # ------------------------------------------------------------------
    def _warmup(self, url: str) -> Optional[str]:
        """
        访问站点首页，让 WAF 下发 Cookie。
        返回可用作 Referer 的首页地址；失败不抛异常（预热只是"尽力而为"的优化）。
        """
        parts = urlsplit(url)
        domain = parts.netloc
        if not domain or domain in self._warmed_domains:
            return urlunsplit((parts.scheme, domain, "/", "", "")) if domain else None

        home = urlunsplit((parts.scheme, domain, "/", "", ""))
        try:
            self.client.get(
                home,
                headers={"User-Agent": self._ua, "Sec-Fetch-Site": "none", "Referer": ""},
                timeout=httpx.Timeout(10.0),
            )
            logger.debug(f"会话预热成功: {home}")
        except Exception as e:
            logger.debug(f"会话预热未成功（不影响后续尝试）: {home} - {e}")
        finally:
            self._warmed_domains.add(domain)
        return home

    # ------------------------------------------------------------------
    # 编码处理
    # ------------------------------------------------------------------
    @staticmethod
    def _decode(raw: bytes, response: httpx.Response, forced: Optional[str] = None) -> str:
        """
        中文政务站编码处理顺序：显式指定 > HTML meta charset > HTTP 头 > 采样探测 > utf-8 兜底。
        """
        if forced:
            return raw.decode(forced, errors="replace")

        # 1. HTML meta charset（政务站最可靠的来源，很多站 HTTP 头是错的）
        match = _META_CHARSET_RE.search(raw[:4096])
        if match:
            enc = match.group(1).decode("ascii", errors="ignore").lower()
            # gb2312 声明的页面实际常含 GBK 字符，直接按 GBK 解码更安全（GBK 是 GB2312 超集）
            if enc in ("gb2312", "gb_2312-80", "gbk"):
                enc = "gb18030"
            try:
                return raw.decode(enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                pass

        # 2. HTTP 响应头声明的编码
        header_enc = (response.charset_encoding or "").lower()
        if header_enc and header_enc not in ("iso-8859-1", "ascii"):
            if header_enc in ("gb2312", "gbk"):
                header_enc = "gb18030"
            try:
                return raw.decode(header_enc, errors="replace")
            except (LookupError, UnicodeDecodeError):
                pass

        # 3. 采样自动探测（只取前 64KB，避免大页面拖慢整轮采集）
        try:
            import charset_normalizer
            guess = charset_normalizer.from_bytes(raw[:65536]).best()
            if guess and guess.encoding:
                enc = guess.encoding.lower()
                if enc in ("gb2312", "gbk"):
                    enc = "gb18030"
                return raw.decode(enc, errors="replace")
        except Exception:
            pass

        # 4. 兜底
        return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # 主请求方法
    # ------------------------------------------------------------------
    def get_text(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        forced_encoding: Optional[str] = None,
        use_cache: bool = True,
    ) -> str:
        """GET 并返回正确解码的 HTML 文本。失败会抛出带明确语义的异常。"""
        if use_cache:
            with self._cache_lock:
                if url in self._text_cache:
                    logger.debug(f"命中本轮缓存: {url}")
                    return self._text_cache[url]

        domain = urlsplit(url).netloc
        lock = self._get_domain_lock(domain)

        with lock:  # 同一域名串行，不同域名可并发
            text = self._get_text_locked(url, headers, forced_encoding)

        if use_cache:
            with self._cache_lock:
                self._text_cache[url] = text
        return text

    def _get_text_locked(
        self,
        url: str,
        headers: Optional[Dict[str, str]],
        forced_encoding: Optional[str],
    ) -> str:
        domain = urlsplit(url).netloc
        referer = None
        last_exception: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            # 首次尝试即预热（政务站几乎都吃这一套）
            if self.enable_warmup and attempt == 1:
                referer = self._warmup(url)

            self._polite_wait(domain)

            req_headers = {"User-Agent": self._ua}
            if referer:
                req_headers["Referer"] = referer
            if headers:
                req_headers.update(headers)

            try:
                response = self.client.get(url, headers=req_headers)

                if response.status_code in NON_RETRYABLE_STATUS:
                    if response.status_code in (404, 410):
                        raise RequestNotFound(
                            f"页面不存在 [{response.status_code}]: {url} —— 该数据源的 URL 可能已失效，请更新规则"
                        )
                    raise RequestBlocked(f"请求被拒绝 [{response.status_code}]: {url}")

                if response.status_code in (403, 412, 406):
                    # 被 WAF 拦了：换个 UA、清掉已预热标记，下一轮重新预热
                    last_exception = RequestBlocked(
                        f"疑似被网站反爬拦截 [{response.status_code}]: {url}"
                    )
                    logger.warning(
                        f"被拦截 [{attempt}/{self.max_retries}] {response.status_code} {url}，"
                        f"更换浏览器指纹后重试..."
                    )
                    self._rotate_identity(domain)
                    referer = None
                    time.sleep(attempt * 2.0 + random.uniform(0, 1.5))
                    continue

                response.raise_for_status()
                return self._decode(response.content, response, forced_encoding)

            except (RequestBlocked, RequestNotFound):
                raise
            except httpx.HTTPStatusError as e:
                last_exception = e
                wait = attempt * 2.0
                logger.warning(f"HTTP 错误 [{attempt}/{self.max_retries}] {url}: {e}，{wait:.1f}s 后重试")
                time.sleep(wait)
            except httpx.RequestError as e:
                last_exception = e
                wait = attempt * 2.0
                logger.warning(f"网络异常 [{attempt}/{self.max_retries}] {url}: {e}，{wait:.1f}s 后重试")
                time.sleep(wait)

        logger.error(f"连续 {self.max_retries} 次请求均失败: {url}")
        raise last_exception or RuntimeError(f"请求失败: {url}")

    def _rotate_identity(self, domain: str):
        """更换浏览器指纹并清空该域名的 Cookie，模拟"换个人重新访问" """
        candidates = [ua for ua in USER_AGENTS if ua != self._ua] or USER_AGENTS
        self._ua = random.choice(candidates)
        self.client.headers["User-Agent"] = self._ua
        self._warmed_domains.discard(domain)
        try:
            self.client.cookies.clear()
        except Exception:
            pass

    def get_json(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """GET 并解析 JSON 接口"""
        domain = urlsplit(url).netloc
        lock = self._get_domain_lock(domain)
        last_exception: Optional[Exception] = None

        with lock:
            for attempt in range(1, self.max_retries + 1):
                self._polite_wait(domain)
                req_headers = {
                    "User-Agent": self._ua,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                }
                if headers:
                    req_headers.update(headers)
                try:
                    response = self.client.get(url, headers=req_headers, params=params)
                    if response.status_code in NON_RETRYABLE_STATUS:
                        raise RequestBlocked(f"接口请求被拒绝 [{response.status_code}]: {url}")
                    response.raise_for_status()
                    return response.json()
                except RequestBlocked:
                    raise
                except Exception as e:
                    last_exception = e
                    logger.warning(f"JSON 请求异常 [{attempt}/{self.max_retries}] {url}: {e}")
                    time.sleep(attempt * 2.0)

        raise last_exception or RuntimeError(f"无法获取有效 JSON: {url}")

    def clear_cache(self):
        with self._cache_lock:
            self._text_cache.clear()

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# 全局共享默认客户端（供 web.py 等复用）
default_requester = BaseRequester()
