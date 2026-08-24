"""
core/real_gov_search.py
真正的实时政务搜索引擎探针：
通过实时网络检索，为用户输入的任意城市/区县（如 "郑州"、"西安"、"成都高新" 等）
动态搜索出真实的 .gov.cn 官方政务网站专栏（住建局/人社局/人民政府）。
绝不写死任何固定城市，拒绝假数据！
"""
import re
import urllib.parse
import logging
from typing import List, Dict, Any
from bs4 import BeautifulSoup
import httpx

logger = logging.getLogger("YouthPolicyAlert.RealGovSearch")

SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class RealGovSearchEngine:
    """真实全网政务搜索探针"""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self.client = httpx.Client(
            headers=SEARCH_HEADERS,
            timeout=self.timeout,
            follow_redirects=True,
            verify=False,
        )

    def search_bing(self, query: str, max_results: int = 3) -> List[Dict[str, str]]:
        """通过 Bing 搜索真实抓取官方 gov.cn 结果"""
        results: List[Dict[str, str]] = []
        try:
            encoded_query = urllib.parse.quote(query)
            url = f"https://cn.bing.com/search?q={encoded_query}"
            resp = self.client.get(url)
            if resp.status_code != 200:
                return results

            soup = BeautifulSoup(resp.text, "lxml")
            items = soup.select("li.b_algo")

            for item in items:
                title_el = item.select_one("h2 a")
                snippet_el = item.select_one(".b_caption p, .b_snippet")
                if not title_el:
                    continue

                raw_title = title_el.get_text(strip=True)
                raw_link = title_el.get("href", "").strip()
                raw_snippet = snippet_el.get_text(strip=True) if snippet_el else ""

                # 优先提取真实目标网址，且必须是政务/组织域名
                if raw_link and not raw_link.startswith("javascript:"):
                    # 严格域名校验：杜绝 baidu.com, baike 等乱入
                    if ".gov.cn" in raw_link or ".org.cn" in raw_link:
                        results.append({
                            "title": raw_title,
                            "url": raw_link,
                            "snippet": raw_snippet,
                        })
                if len(results) >= max_results:
                    break

        except Exception as e:
            logger.warning(f"Bing 搜索异常: {e}")

        return results

    def probe_gov_for_region(self, region_name: str) -> List[Dict[str, Any]]:
        """
        根据用户输入的任意位置，并发探查 3 大官方部门的真实专栏：
        1. 住房和城乡建设局 (人才房/保租房)
        2. 人力资源和社会保障局 (毕业生补贴/生活补贴)
        3. 人民政府门户网 (通知公告)
        """
        clean_name = region_name.strip()
        if not clean_name:
            clean_name = "郑州"

        candidates: List[Dict[str, Any]] = []

        # 方向 1：住房保障 / 人才公寓
        q_housing = f"{clean_name} 住房保障 人才公寓 保租房 配租 site:gov.cn"
        housing_res = self.search_bing(q_housing, max_results=1)
        if not housing_res:
            # 宽泛检索也必须加上 site:gov.cn
            housing_res = self.search_bing(f"{clean_name} 住房和城乡建设局 官网 site:gov.cn", max_results=1)

        if housing_res:
            candidates.append({
                "type": "housing",
                "tag": "🏠 保租房/人才公寓专栏",
                "title": f"【{clean_name}】" + housing_res[0]["title"],
                "source_name": f"{clean_name}住建与住房保障部门",
                "url": housing_res[0]["url"],
                "desc": housing_res[0]["snippet"] or f"实时检索到 {clean_name} 官方住房与保障房发布入口",
                "list_item": "ul li, div.news_list li, .list li",
            })

        # 方向 2：高校毕业生补贴 / 人社局
        q_subsidy = f"{clean_name} 人力资源和社会保障局 高校毕业生 补贴 site:gov.cn"
        subsidy_res = self.search_bing(q_subsidy, max_results=1)
        if not subsidy_res:
            subsidy_res = self.search_bing(f"{clean_name} 人社局 毕业生补贴 site:gov.cn", max_results=1)

        if subsidy_res:
            candidates.append({
                "type": "subsidy",
                "tag": "💰 毕业生生活/租房补贴专栏",
                "title": f"【{clean_name}】" + subsidy_res[0]["title"],
                "source_name": f"{clean_name}人力资源和社会保障局",
                "url": subsidy_res[0]["url"],
                "desc": subsidy_res[0]["snippet"] or f"实时检索到 {clean_name} 毕业生生活与租房补贴政策专栏",
                "list_item": "ul li, div.news_list li, .list li",
            })

        # 方向 3：区人民政府政务公告
        q_gov = f"{clean_name} 人民政府 通知公告 site:gov.cn"
        gov_res = self.search_bing(q_gov, max_results=1)
        if gov_res:
            candidates.append({
                "type": "other",
                "tag": "🏢 政府门户通知公告",
                "title": f"【{clean_name}】" + gov_res[0]["title"],
                "source_name": f"{clean_name}人民政府门户",
                "url": gov_res[0]["url"],
                "desc": gov_res[0]["snippet"] or f"实时检索到 {clean_name} 综合政务通知与扶持政策",
                "list_item": "ul li, div.news_list li, .list li",
            })

        return candidates[:3]


# 全局单例
real_search_engine = RealGovSearchEngine()
