"""
web.py
YouthPolicyAlert 可视化 Web 控制台。

提供：政策看板、订阅与推送配置、规则实验室（在线调试选择器）、数据源健康大盘。

v2 改进：
1. 适配新的存储状态机，看板能显示"已推送/待推送/基线"分布
2. 新增 /api/health 数据源健康接口与 /api/pending 待推送队列接口
3. 新增 /api/rules 规则管理（列出/删除），不再只能加不能删
4. 规则保存前强制校验，避免把无效规则写进 rules.yaml
5. 修正 v1 前端 mounted() 调用不存在的 fetchCities() 导致整个看板加载中断的问题
"""
import copy
import os
import logging
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from core.models import PolicyItem, PolicyCategory
from core.config_schema import load_config, load_rules, load_yaml, save_yaml, AppConfig
from core.requester import BaseRequester
from core.rule_engine import DeclarativeRuleCollector, collect_all, validate_rule, enrich_with_details
from core.pipeline import PolicyPipeline
from core.storage import PolicyStorage
from core.notify_center import NotifyCenter
from core.gov_resolver import resolve_official_gov_sources

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("YouthPolicyAlert.Web")

app = FastAPI(title="YouthPolicyAlert Web Console", version="2.0.0")

os.makedirs("data", exist_ok=True)
os.makedirs("static", exist_ok=True)

CONFIG_PATH = "config/config.yaml"
RULES_PATH = "config/rules.yaml"

# 这些字段可能包含可直接调用第三方服务的凭据。Web 控制台只需要知道
# “已经配置”，不应该把原文发送到浏览器；保存时再把掩码还原为原值。
CONFIG_SECRET_MASK = "********"
CONFIG_SECRET_PATHS = (
    ("notifications", "email", "password"),
    ("notifications", "pushplus", "token"),
    ("notifications", "serverchan", "send_key"),
    ("notifications", "feishu", "webhook_url"),
    ("notifications", "wecom", "webhook_url"),
)


def _nested_get(data: Dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _nested_set(data: Dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current: Dict[str, Any] = data
    for key in path[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[path[-1]] = value


def _redact_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """返回可安全给浏览器的配置副本，不修改磁盘配置或调用方对象。"""
    safe = copy.deepcopy(config)
    for path in CONFIG_SECRET_PATHS:
        value = _nested_get(safe, path)
        if value:
            _nested_set(safe, path, CONFIG_SECRET_MASK)
    return safe


def _restore_redacted_secrets(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """前端回传 GET /api/config 的掩码时，保留磁盘中已有的真实凭据。"""
    merged = copy.deepcopy(incoming)
    for path in CONFIG_SECRET_PATHS:
        old_value = _nested_get(existing, path)
        new_value = _nested_get(merged, path)
        if old_value and (new_value is None or new_value == "" or new_value == CONFIG_SECRET_MASK):
            _nested_set(merged, path, old_value)
    return merged

_config = load_config(CONFIG_PATH)
storage = PolicyStorage(db_path=_config.crawler.db_path)
pipeline = PolicyPipeline(
    threshold=_config.crawler.relevance_threshold,
    max_age_days=_config.crawler.max_age_days,
)


def current_config() -> AppConfig:
    """每次读取最新配置，保证在网页上改完配置立刻生效"""
    return load_config(CONFIG_PATH)


def build_requester(cfg: AppConfig) -> BaseRequester:
    c = cfg.crawler
    return BaseRequester(
        timeout=c.timeout, max_retries=c.max_retries,
        min_delay=c.min_delay, max_delay=c.max_delay,
        verify_ssl=c.verify_ssl, proxy=c.proxy, enable_warmup=c.enable_warmup,
    )


# ============================================================================
# 1. 政策浏览
# ============================================================================
@app.get("/api/cities")
def get_supported_cities():
    """规则库中已支持的全部城市"""
    rules = load_rules(RULES_PATH)
    cities = list(dict.fromkeys([r.get("city") for r in rules if r.get("city")]))
    return ["全部"] + cities


@app.get("/api/policies")
def get_policies(
    city: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 100,
):
    """政策列表，支持城市、分类、关键词联合检索"""
    with storage._conn() as conn:
        query = "SELECT * FROM policies WHERE 1=1"
        params: List[Any] = []

        if city and city != "全部":
            query += " AND (city LIKE ? OR district LIKE ?)"
            params += [f"%{city}%", f"%{city}%"]

        if category and category != "all":
            query += " AND category = ?"
            params.append(category)

        if q:
            query += " AND (city LIKE ? OR district LIKE ? OR title LIKE ? OR source_name LIKE ?)"
            p = f"%{q.strip()}%"
            params += [p, p, p, p]

        query += " ORDER BY first_seen_at DESC, publish_date DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))

        rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    return {"total": len(rows), "data": rows}


@app.get("/api/stats")
def get_stats():
    """看板统计指标"""
    s = storage.stats()
    rules = load_rules(RULES_PATH)
    cfg = current_config()

    health = storage.get_health_report()
    broken = sum(1 for h in health if h.is_broken)

    return {
        **s,
        "total_rules": len(rules),
        "broken_sources": broken,
        "active_channels": cfg.active_channels(),
        "baseline_established": storage.is_initialized(),
        "status": "Healthy" if not broken else "Degraded",
    }


@app.get("/api/health")
def get_health():
    """数据源健康大盘：哪些数据源失效了，一目了然"""
    report = storage.get_health_report()
    return {
        "total": len(report),
        "broken": sum(1 for h in report if h.is_broken),
        "data": [h.model_dump() | {"is_broken": h.is_broken} for h in report],
    }


@app.get("/api/pending")
def get_pending():
    """当前待推送队列（包含上轮推送失败、等待重试的政策）"""
    items = storage.get_pending_notifications(limit=100)
    return {"total": len(items), "data": [i.to_brief_dict() for i in items]}


# ============================================================================
# 2. 采集调度
# ============================================================================
def run_scan_task(target_city: Optional[str] = None, notify: bool = False):
    """后台执行一轮扫描（可选是否推送）"""
    cfg = current_config()
    rules = load_rules(RULES_PATH)
    clean_target = (target_city or "").strip()

    if clean_target and clean_target != "全部":
        matched = [
            r for r in rules
            if clean_target in str(r.get("city", "")) or str(r.get("city", "")) in clean_target
            or clean_target in str(r.get("district", "")) or clean_target in str(r.get("source_name", ""))
        ]
        if matched:
            rules = matched
            logger.info(f"🎯 [{clean_target}] 匹配到 {len(rules)} 个已配置数据源")
        else:
            # 规则库没收录 → 动态探查官方候选源（仅本次使用，不写入规则库）
            logger.info(f"🌐 [{clean_target}] 未收录，正在动态探查官方数据源...")
            rules = []
            for s in resolve_official_gov_sources(clean_target):
                if not s.get("url"):
                    continue
                rules.append({
                    "id": f"dyn_{clean_target}_{s['type']}",
                    "city": clean_target, "district": "全市",
                    "source_name": s["source_name"], "category": s["type"],
                    "url": s["url"], "parser_type": "html",
                    "selectors": {
                        "list_item": s.get("list_item", "ul li"),
                        "title": "a", "link": "a", "date": "span",
                    },
                })

    if not rules:
        logger.warning(f"没有可执行的数据源: {clean_target}")
        return

    requester = build_requester(cfg)
    try:
        results = collect_all(rules, requester=requester, max_workers=cfg.crawler.max_workers)

        raw: List[PolicyItem] = []
        for r in results:
            if r.ok:
                raw.extend(r.items)
                storage.log_collector_run(r.source_name, r.city, "SUCCESS", items_found=len(r.items))
            else:
                storage.log_collector_run(r.source_name, r.city, "FAILED", error_message=r.error)

        clean = pipeline.process(raw)
        if clean and cfg.crawler.fetch_detail:
            clean = enrich_with_details(
                clean, requester=requester,
                max_details=cfg.crawler.max_detail_pages, max_workers=2,
            )

        # 网页端手动扫描默认不触发冷启动基线逻辑，除非数据库确实是空的
        as_baseline = not storage.is_initialized() and cfg.crawler.cold_start_baseline
        new_items = storage.record_discovered(clean, as_baseline=as_baseline)
        if as_baseline:
            storage.mark_initialized(len(new_items))

        logger.info(f"后台扫描完成：原始 {len(raw)} 条 → 清洗 {len(clean)} 条 → 新增 {len(new_items)} 条")

        if notify and not as_baseline:
            NotifyCenter(cfg, storage).dispatch_pending()
    finally:
        requester.close()


@app.post("/api/trigger_scan")
def trigger_scan(background_tasks: BackgroundTasks, city: Optional[str] = None, notify: bool = False):
    """一键触发后台扫描"""
    background_tasks.add_task(run_scan_task, city, notify)
    return {"status": "success", "message": "已触发后台抓取任务，数据正在刷新中！"}


@app.post("/api/dispatch_pending")
def dispatch_pending():
    """手动把待推送队列发出去（用于验证通道或补发上轮失败的政策）"""
    cfg = current_config()
    center = NotifyCenter(cfg, storage)
    if not center.has_channel:
        raise HTTPException(status_code=400, detail="没有可用的推送通道，请先在配置页填写邮箱")
    summary = center.dispatch_pending()
    return {"status": "success", "summary": summary}


# ============================================================================
# 3. 规则实验室
# ============================================================================
@app.get("/api/search_gov_sources")
def search_gov_sources(query: str):
    """根据城市名探查官方候选数据源"""
    return {"status": "success", "query": query, "candidates": resolve_official_gov_sources(query)}


@app.get("/api/preset_source")
def get_preset_source(region: str):
    sources = resolve_official_gov_sources(region)
    return {"status": "success", "preset": sources[0] if sources else {}}


class TestRuleRequest(BaseModel):
    url: str
    list_item: str = ""
    title: str = "a"
    link: str = "a"
    date: Optional[str] = "span"
    city: str = "测试城市"
    district: Optional[str] = "全市"
    source_name: str = "测试数据源"
    category: str = "housing"
    encoding: Optional[str] = None

    def to_rule(self) -> Dict[str, Any]:
        return {
            "city": self.city,
            "district": self.district or "全市",
            "source_name": self.source_name,
            "category": self.category,
            "url": self.url,
            "encoding": self.encoding,
            "parser_type": "html",
            "selectors": {
                "list_item": self.list_item,
                "title": self.title or "a",
                "link": self.link or "a",
                "date": self.date,
            },
        }


@app.post("/api/test_rule")
def test_scraping_rule(req: TestRuleRequest):
    """在线测试选择器提取效果，明确区分'抓到了多少条'与'命中了多少条青年政策'"""
    rule = req.to_rule()
    problems = validate_rule(rule)
    if problems:
        raise HTTPException(status_code=400, detail="规则配置有误: " + "; ".join(problems))

    cfg = current_config()
    requester = build_requester(cfg)
    try:
        collector = DeclarativeRuleCollector(rule=rule, requester=requester)
        raw_items = collector.fetch()
        processed = pipeline.process(list(raw_items))

        used_fallback = bool(req.list_item) and len(raw_items) > 0 and _selector_missed(rule, requester)

        samples = [it.to_brief_dict() for it in (processed or raw_items)[:10]]
        return {
            "status": "success",
            "total_raw": len(raw_items),
            "total_matched": len(processed),
            "used_fallback": used_fallback,
            "hint": _build_hint(len(raw_items), len(processed)),
            "samples": samples,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"解析失败: {e}")
    finally:
        requester.close()


def _selector_missed(rule: Dict[str, Any], requester) -> bool:
    """判断结果是否来自启发式兜底（说明用户填的选择器其实没生效）"""
    try:
        from bs4 import BeautifulSoup
        html = requester.get_text(rule["url"], forced_encoding=rule.get("encoding"))
        soup = BeautifulSoup(html, "lxml")
        return len(soup.select(rule["selectors"]["list_item"])) == 0
    except Exception:
        return False


def _build_hint(raw: int, matched: int) -> str:
    if raw == 0:
        return "❌ 一条都没抓到：请检查 URL 是否正确、选择器是否匹配该页面结构。"
    if matched == 0:
        return f"⚠️ 抓到 {raw} 条公告，但没有命中青年安居/补贴类政策。可能这个栏目本身就没有相关内容，换个专栏试试。"
    return f"✅ 抓到 {raw} 条公告，其中 {matched} 条是青年政策相关，规则有效！"


@app.get("/api/rules")
def list_rules():
    """列出规则库中的全部规则"""
    rules = load_rules(RULES_PATH)
    return {"total": len(rules), "data": rules}


@app.post("/api/save_rule")
def save_new_rule(req: TestRuleRequest, background_tasks: BackgroundTasks):
    """把测试通过的规则保存进规则库并立即抓取"""
    rule = req.to_rule()
    problems = validate_rule(rule)
    if problems:
        raise HTTPException(status_code=400, detail="规则配置有误: " + "; ".join(problems))

    data = load_yaml(RULES_PATH) or {}
    rules = data.get("rules", [])

    # 同 URL 已存在则更新而不是重复添加
    for existing in rules:
        if existing.get("url") == rule["url"]:
            existing.update(rule)
            existing.setdefault("id", f"rule_{len(rules)}_{req.city}")
            data["rules"] = rules
            save_yaml(RULES_PATH, data)
            background_tasks.add_task(run_scan_task, req.city, False)
            return {"status": "success", "message": f"已更新【{req.city}】的现有规则并重新抓取。"}

    rule["id"] = f"rule_{len(rules) + 1}_{req.city}"
    rules.append(rule)
    data["rules"] = rules
    save_yaml(RULES_PATH, data)

    background_tasks.add_task(run_scan_task, req.city, False)
    return {"status": "success", "message": f"🎉 已将【{req.city}】加入监控库，正在自动抓取数据！"}


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: str):
    """删除一条规则（v1 只能添加不能删除，写错了只能手改 yaml）"""
    data = load_yaml(RULES_PATH) or {}
    rules = data.get("rules", [])
    remaining = [r for r in rules if str(r.get("id")) != rule_id]
    if len(remaining) == len(rules):
        raise HTTPException(status_code=404, detail=f"未找到规则: {rule_id}")
    data["rules"] = remaining
    save_yaml(RULES_PATH, data)
    return {"status": "success", "message": f"已删除规则 {rule_id}"}


# ============================================================================
# 4. 配置与通知测试
# ============================================================================
@app.get("/api/config")
def get_config():
    cfg = load_yaml(CONFIG_PATH)
    if not cfg:
        cfg = load_yaml("config/config.example.yaml")
    return _redact_config(cfg)


@app.post("/api/config")
def update_config(data: Dict[str, Any]):
    existing = load_yaml(CONFIG_PATH)
    if not existing:
        existing = load_yaml("config/config.example.yaml")
    data = _restore_redacted_secrets(existing, data)
    try:
        AppConfig(**data)  # 保存前先校验，避免写进一份用不了的配置
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"配置格式有误: {e}")
    save_yaml(CONFIG_PATH, data)
    return {"status": "success", "message": "配置已保存成功！"}


class TestNotifyRequest(BaseModel):
    channel: str
    email_config: Optional[Dict[str, Any]] = None
    pushplus_token: Optional[str] = None
    serverchan_key: Optional[str] = None
    webhook_url: Optional[str] = None


def _sample_item() -> PolicyItem:
    return PolicyItem(
        title="【测试】2026年第三批青年人才公寓配租公告",
        url="https://github.com/",
        city="示例市", district="示例区",
        source_name="YouthPolicyAlert 通道测试",
        category=PolicyCategory.HOUSING,
        target_audience="本科 / 硕士 / 博士 / 应届毕业生",
        deadline="2026-09-30 截止",
        amount_or_quota="每月 1500 元 ｜ 房源 1200 套",
        age_limit="35周岁以下",
        apply_channel="示例市人才安居服务平台",
        publish_date="2026-08-24",
        raw_content="这是一条测试消息。你能看到它，说明推送通道已完全打通。",
    )


@app.post("/api/test_notify")
def test_notification(req: TestNotifyRequest):
    """发送一条样例通知，验证通道配置"""
    from notifiers.email_notifier import EmailNotifier
    from notifiers.webhook_notifier import (
        PushPlusNotifier, ServerChanNotifier, FeishuNotifier, WeComNotifier,
    )

    sample = _sample_item()

    if req.channel == "email":
        if not req.email_config:
            raise HTTPException(status_code=400, detail="缺少邮件配置")
        email_config = copy.deepcopy(req.email_config)
        saved_email = _nested_get(load_yaml(CONFIG_PATH), ("notifications", "email"))
        if not saved_email:
            saved_email = _nested_get(
                load_yaml("config/config.example.yaml"),
                ("notifications", "email"),
            )
        if isinstance(saved_email, dict) and email_config.get("password") in (None, "", CONFIG_SECRET_MASK):
            email_config["password"] = saved_email.get("password", "")
        if EmailNotifier(email_config).send([sample]):
            return {"status": "success", "message": "测试邮件发送成功，请前往邮箱查收！"}
        raise HTTPException(status_code=500, detail="邮件发送失败，请检查 SMTP 服务器、账号与授权码（不是登录密码）")

    if req.channel == "pushplus":
        if not req.pushplus_token:
            raise HTTPException(status_code=400, detail="缺少 PushPlus Token")
        if PushPlusNotifier(token=req.pushplus_token).send([sample]):
            return {"status": "success", "message": "PushPlus 推送已发出，请在微信查看！"}
        raise HTTPException(status_code=500, detail="PushPlus 推送失败，请检查 Token")

    if req.channel == "serverchan":
        if not req.serverchan_key:
            raise HTTPException(status_code=400, detail="缺少 Server酱 SendKey")
        if ServerChanNotifier(send_key=req.serverchan_key).send([sample]):
            return {"status": "success", "message": "Server酱推送已发出！"}
        raise HTTPException(status_code=500, detail="Server酱推送失败，请检查 SendKey")

    if req.channel in ("feishu", "wecom"):
        if not req.webhook_url:
            raise HTTPException(status_code=400, detail="缺少 Webhook 地址")
        notifier = FeishuNotifier(req.webhook_url) if req.channel == "feishu" else WeComNotifier(req.webhook_url)
        if notifier.send([sample]):
            return {"status": "success", "message": "机器人消息已发出，请在群里查看！"}
        raise HTTPException(status_code=500, detail="推送失败，请检查 Webhook 地址")

    raise HTTPException(status_code=400, detail=f"未知的推送通道: {req.channel}")


# ============================================================================
# 5. 静态页面
# ============================================================================
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index_page():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    print("🌐 YouthPolicyAlert 控制台: http://127.0.0.1:8000")
    uvicorn.run("web:app", host="127.0.0.1", port=8000, reload=False)
