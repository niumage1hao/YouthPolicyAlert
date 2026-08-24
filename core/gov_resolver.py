"""
core/gov_resolver.py
官方政务数据源候选库 —— 为"规则实验室"提供起步 URL。

⚠️ 关于数据真实性的重要说明（v2 修正）：

v1 的 VERIFIED_CITY_SOURCES 声称"经过核实"，但其中多条是编造的：
  - 杭州"人社局"条目的 URL 填的是 fgj.hangzhou.gov.cn（住保房管局）
  - 武汉"人社局"和"政府门户"条目的 URL 都填的是 whrcgz.gov.cn
  - 郑州"人社局"和"政府门户"条目的 URL 都填的是 public.zhengzhou.gov.cn
  - 深圳"政府门户"条目的 URL 填的是 zjj.sz.gov.cn（住建局）
把 A 部门的网址挂在 B 部门名下，用户会以为在监控人社局补贴，实际抓的是房管局页面。

v2 的处理原则：
  1. 只保留能确定对应关系的域名，并明确标注 verified 状态
  2. 绝不用一个部门的 URL 冒充另一个部门
  3. 所有候选源都标注为"建议先在规则实验室测试"，由用户实测结果决定是否保存
  4. 未收录城市返回带空 URL 的占位提示，引导用户手动填写，而不是塞一个假链接
"""
import logging
from typing import List, Dict, Any

logger = logging.getLogger("YouthPolicyAlert.GovResolver")


def _src(type_: str, tag: str, title: str, source_name: str, url: str,
         desc: str, list_item: str = "ul li, div.news_list li, .list-item", verified: bool = False) -> Dict[str, Any]:
    return {
        "type": type_, "tag": tag, "title": title, "source_name": source_name,
        "url": url, "desc": desc, "list_item": list_item, "verified": verified,
    }


HOUSING_TAG = "🏠 住房保障/人才房专区"
SUBSIDY_TAG = "💰 高校毕业生补贴专区"
PORTAL_TAG = "🏢 政府门户通知公告"

# ---------------------------------------------------------------------------
# 候选数据源库
# verified=True 表示该 URL 已在真实运行中抓到过数据（依据用户实际运行日志）；
# verified=False 表示仅为部门官网域名推断，需在规则实验室测试后再保存。
# ---------------------------------------------------------------------------
CITY_SOURCES: Dict[str, List[Dict[str, Any]]] = {
    "深圳": [
        _src("housing", HOUSING_TAG, "深圳市住房和建设局 (保租房/人才房配租)",
             "深圳市住房和建设局", "http://zjj.sz.gov.cn/xxgk/tzgg/index.html",
             "深圳市各批次保障性租赁住房、人才房认租通告", "ul.ftdt-list li", verified=True),
        _src("subsidy", SUBSIDY_TAG, "深圳市人力资源和社会保障局 (毕业生补贴/人才引进)",
             "深圳市人力资源和社会保障局", "http://hrss.sz.gov.cn/xxgk/qtxx/tzgg/index.html",
             "高校毕业生租房和生活补贴、新引进人才落户补贴",
             "div.AllListCon li, div.newsList li", verified=True),
        _src("other", PORTAL_TAG, "深圳市人民政府门户网站",
             "深圳市人民政府", "http://www.sz.gov.cn/cn/xxgk/zfxxgj/tzgg/",
             "深圳市综合政务公告、青年驿站与青年创新创业扶持政策"),
    ],
    "北京": [
        _src("housing", HOUSING_TAG, "北京市住房和城乡建设委员会 (公租房配租/保障房)",
             "北京市住房和城乡建设委员会", "https://zjw.beijing.gov.cn/bjjs/zfbz/bzxzlzf/index.shtml",
             "北京市各区公租房配租摇号、保障性住房资格审核公示"),
        _src("subsidy", SUBSIDY_TAG, "北京市人力资源和社会保障局 (通知公告)",
             "北京市人力资源和社会保障局", "https://rsj.beijing.gov.cn/xxgk/tzgg/",
             "北京高校毕业生就业见习补贴、求职补贴、实名登记服务",
             "ul.list_con li, div.news_list li", verified=True),
        _src("other", PORTAL_TAG, "首都之窗 - 北京市人民政府",
             "北京市人民政府", "https://www.beijing.gov.cn/fuwu/ggts/",
             "北京市政府公告提示、政策文件与青年人才扶持"),
    ],
    "上海": [
        _src("housing", HOUSING_TAG, "上海市住房和城乡建设管理委员会 (保障性租赁住房)",
             "上海市住房和城乡建设管理委员会", "https://zjw.sh.gov.cn/tzgg/index.html",
             "上海市保障性租赁住房、公租房配租与人才公寓公告"),
        _src("subsidy", SUBSIDY_TAG, "上海市人力资源和社会保障局",
             "上海市人力资源和社会保障局", "https://rsj.sh.gov.cn/tjypx_17728/index.html",
             "上海市高校毕业生就业创业补贴、落户与人才引进政策"),
        _src("other", PORTAL_TAG, "中国上海 - 上海市人民政府",
             "上海市人民政府", "https://www.shanghai.gov.cn/nw12344/index.html",
             "上海市综合政务公告与青年人才政策"),
    ],
    "杭州": [
        _src("housing", HOUSING_TAG, "杭州市住房保障和房产管理局 (保租房/人才安居)",
             "杭州市住房保障和房产管理局", "https://fgj.hangzhou.gov.cn/col/col1229268437/index.html",
             "杭州市保租房认定、人才专项租赁房源与公租房配租"),
        _src("subsidy", SUBSIDY_TAG, "杭州市人力资源和社会保障局 (毕业生补贴)",
             "杭州市人力资源和社会保障局", "https://hrss.hangzhou.gov.cn/col/col1229196651/index.html",
             "杭州应届毕业生生活补贴与每年租房补贴申报",
             "ul.news-list li, .list_con li"),
        _src("other", PORTAL_TAG, "杭州市人民政府门户网站",
             "杭州市人民政府", "http://www.hangzhou.gov.cn/col/col1256257/index.html",
             "青年友好型城市举措、青荷驿站免费住宿"),
    ],
    "广州": [
        _src("housing", HOUSING_TAG, "广州市住房和城乡建设局 (保租房/公租房配租)",
             "广州市住房和城乡建设局", "https://zfcj.gz.gov.cn/zjyw/zfbz/zwxx/zbwg/",
             "广州市保障性租赁住房、公租房配租与人才公寓"),
        _src("subsidy", SUBSIDY_TAG, "广州市人力资源和社会保障局",
             "广州市人力资源和社会保障局", "https://rsj.gz.gov.cn/ywzt/jycy/gxbys/gxbysrmzc/",
             "广州市高校毕业生就业创业补贴与人才引进"),
        _src("other", PORTAL_TAG, "广州市人民政府门户网站",
             "广州市人民政府", "http://www.gz.gov.cn/xw/tzgg/",
             "广州市综合政务公告与青年人才政策"),
    ],
    "武汉": [
        _src("housing", HOUSING_TAG, "武汉市住房和城市更新局 (大学毕业生租赁房)",
             "武汉市住房和城市更新局", "https://zgj.wuhan.gov.cn/zwdt/tzgg/",
             "留汉大学生租赁房、保障性租赁住房与人才公寓"),
        _src("subsidy", SUBSIDY_TAG, "武汉市人力资源和社会保障局",
             "武汉市人力资源和社会保障局", "http://rsj.wuhan.gov.cn/xxgk/tzgg/",
             "高校毕业生一次性就业补贴、创业资助与安居补助"),
        _src("other", PORTAL_TAG, "武汉市人民政府门户网站",
             "武汉市人民政府", "http://www.wuhan.gov.cn/zwgk/tzgg/",
             "百万大学生留汉就业创业工程综合扶持公告"),
    ],
    "成都": [
        _src("housing", HOUSING_TAG, "成都市住房和城乡建设局 (保租房/人才安居)",
             "成都市住房和城乡建设局", "https://cds.sczwfw.gov.cn/col/col15396/index.html?areaCode=510100000000&pageNum=-30&uid=9387",
             "成都市保障性租赁住房配租、人才安居资格认定"),
        _src("subsidy", SUBSIDY_TAG, "成都市人力资源和社会保障局",
             "成都市人力资源和社会保障局", "https://cdhrss.chengdu.gov.cn/cdrss/c130700/list.shtml",
             "成都市高校毕业生求职创业补贴、就业见习补贴"),
        _src("other", PORTAL_TAG, "成都市人民政府门户网站",
             "成都市人民政府", "https://www.chengdu.gov.cn/chengdu/c127344/gsgg.shtml",
             "成都市政府公告与青年友好城市建设政策"),
    ],
    "南京": [
        _src("housing", HOUSING_TAG, "南京市住房保障和房产局 (保障房/人才安居)",
             "南京市住房保障和房产局", "https://fcj.nanjing.gov.cn/dtxx/tzgg/",
             "南京市保障性住房配租、青年人才安居房源"),
        _src("subsidy", SUBSIDY_TAG, "南京市人力资源和社会保障局",
             "南京市人力资源和社会保障局", "https://rsj.nanjing.gov.cn/njsrlzyhshbzj/tzgg/",
             "南京市高校毕业生住房租赁补贴、面试补贴"),
        _src("other", PORTAL_TAG, "南京市人民政府门户网站",
             "南京市人民政府", "https://www.nanjing.gov.cn/njxxgkn/tzgg/",
             "紫金山英才计划等人才引进政策"),
    ],
    "西安": [
        _src("housing", HOUSING_TAG, "西安市住房和城乡建设局 (保障房/人才安居)",
             "西安市住房和城乡建设局", "http://zjj.xa.gov.cn/zw/zfxxgkml/zwxx/zxtz/1.html",
             "西安市保障性租赁住房配租、公租房摇号选房"),
        _src("subsidy", SUBSIDY_TAG, "西安市人力资源和社会保障局",
             "西安市人力资源和社会保障局", "http://xahrss.xa.gov.cn/tzgg.html",
             "西安市高校毕业生求职创业补贴、就业见习补贴"),
        _src("other", PORTAL_TAG, "西安市人民政府门户网站",
             "西安市人民政府", "https://www.xa.gov.cn/xw/gsgg/index.html",
             "西安市引才引智扶持与青年驿站政策"),
    ],
    "长沙": [
        _src("housing", HOUSING_TAG, "长沙市住房和城乡建设局 (保障房/人才公寓)",
             "长沙市住房和城乡建设局", "http://szjw.changsha.gov.cn/zfxxgk/tzgg/tzgg_37612/",
             "长沙市保障性租赁住房配租、人才公寓申请"),
        _src("subsidy", SUBSIDY_TAG, "长沙市人力资源和社会保障局",
             "长沙市人力资源和社会保障局", "http://rsj.changsha.gov.cn/xxgk_1/tzgg/",
             "长沙市高校毕业生租房和生活补贴、创业补贴"),
        _src("other", PORTAL_TAG, "长沙市人民政府门户网站",
             "长沙市人民政府", "https://www.changsha.gov.cn/zfxxgk/tzgg/",
             "长沙市青年人才政策与综合安居扶持"),
    ],
    "郑州": [
        _src("housing", HOUSING_TAG, "郑州市住房保障和房地产管理局 (人才公寓/保租房)",
             "郑州市住房保障和房地产管理局", "https://zfbzj.zhengzhou.gov.cn/",
             "郑州市人才公寓配租、保租房项目认定与选房方案",
             "ul.news-work li, ul.news-list li, .tab-node li", verified=True),
        _src("subsidy", SUBSIDY_TAG, "郑州市人力资源和社会保障局",
             "郑州市人力资源和社会保障局", "https://rsj.zhengzhou.gov.cn/",
             "郑州市青年人才生活补贴、应届生租房补贴申报"),
        _src("other", PORTAL_TAG, "郑州市人民政府门户网站",
             "郑州市人民政府", "https://public.zhengzhou.gov.cn/",
             "郑州市政府公报与各区县人才引进政策", "ul li", verified=True),
    ],
    "厦门": [
        _src("housing", HOUSING_TAG, "厦门市住房和建设局 (保租房/人才公寓)",
             "厦门市住房和建设局", "https://szjj.xm.gov.cn/ztzl/bzzf/",
             "厦门市保障性租赁住房配租、人才公寓申请、公租房摇号", verified=False),
        _src("subsidy", SUBSIDY_TAG, "厦门市人力资源和社会保障局",
             "厦门市人力资源和社会保障局", "https://hrss.xm.gov.cn/xxgk/tzgg/",
             "新引进人才生活补贴、求职补贴、就业见习", verified=True),
        _src("other", PORTAL_TAG, "厦门市人民政府门户网站",
             "厦门市人民政府", "https://www.xm.gov.cn/zwgk/",
             "厦门市综合政务公告、青年人才扶持与安居政策"),
    ],
}


def _placeholder(region_name: str) -> List[Dict[str, Any]]:
    """未收录城市：返回引导用户手填的占位项，绝不塞假链接"""
    return [
        _src("housing", "⚠️ 暂未收录", f"暂未收录【{region_name}】的住建局数据源",
             f"{region_name}住建局 (待录入)", "",
             f"请手动填入【{region_name}】住房和城乡建设局（或住房保障局）的通知公告页 URL。"),
        _src("subsidy", "⚠️ 暂未收录", f"暂未收录【{region_name}】的人社局数据源",
             f"{region_name}人社局 (待录入)", "",
             f"请手动填入【{region_name}】人力资源和社会保障局的通知公告页 URL。"),
        _src("other", "⚠️ 暂未收录", f"暂未收录【{region_name}】的政府门户数据源",
             f"{region_name}人民政府 (待录入)", "",
             f"请手动填入【{region_name}】市人民政府门户的通知公告页 URL。"),
    ]


def resolve_official_gov_sources(region_name: str) -> List[Dict[str, Any]]:
    """
    根据城市/区县名返回官方候选数据源。

    优先级：内置候选库 > 实时搜索探查 > 占位提示（引导手填）
    任何情况下都不会返回其他城市的 URL 来冒充。
    """
    if not region_name or not region_name.strip():
        return _placeholder("未指定区域")

    raw = region_name.strip()
    clean = raw.replace("省", "").replace("市", "").replace("区", "").replace("县", "")

    # 1. 内置候选库精确/包含匹配
    for city_key, sources in CITY_SOURCES.items():
        if city_key in clean or clean in city_key:
            logger.info(f"✅ [{raw}] 命中内置候选库 [{city_key}]，返回 {len(sources)} 个官方源")
            return sources

    # 2. 实时搜索探查（结果不保证准确，仅作为起点）
    logger.info(f"🌐 [{raw}] 未收录，尝试实时检索官方站点...")
    try:
        from core.real_gov_search import real_search_engine
        live = real_search_engine.probe_gov_for_region(raw)
        if live:
            logger.info(f"🔍 [{raw}] 检索到 {len(live)} 个候选源（请务必先测试再保存）")
            return live
    except Exception as e:
        logger.warning(f"⚠️ [{raw}] 实时检索失败: {e}")

    # 3. 引导手填
    logger.warning(f"❌ [{raw}] 暂无可用候选源，需手动录入")
    return _placeholder(raw)


# 兼容旧名称
VERIFIED_CITY_SOURCES = CITY_SOURCES
