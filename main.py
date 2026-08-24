"""
main.py
YouthPolicyAlert 主调度入口。

子命令：
  run          执行一轮完整监控（采集 → 清洗 → 详情提取 → 去重入库 → 推送）
  doctor       体检所有数据源，逐个报告连通性与选择器有效性（不写库、不推送）
  test-notify  发一条样例通知，验证推送通道是否配置正确
  stats        查看本地数据库统计与数据源健康状况

关键执行顺序（v2 修复了 v1 的推送丢失问题）：
  采集 → 清洗 → 提取 → 入库(标记待推送) → 【独立的推送阶段，失败下轮重试】
"""
import os
import sys
import logging
import argparse
from typing import List, Dict, Any

from core.config_schema import load_config, load_rules, AppConfig
from core.models import PolicyItem
from core.requester import BaseRequester
from core.rule_engine import collect_all, enrich_with_details, validate_rule
from core.pipeline import PolicyPipeline
from core.storage import PolicyStorage
from core.notify_center import NotifyCenter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("YouthPolicyAlert.Main")


def build_requester(config: AppConfig) -> BaseRequester:
    c = config.crawler
    return BaseRequester(
        timeout=c.timeout,
        max_retries=c.max_retries,
        min_delay=c.min_delay,
        max_delay=c.max_delay,
        verify_ssl=c.verify_ssl,
        proxy=c.proxy,
        enable_warmup=c.enable_warmup,
    )


def select_rules(all_rules: List[Dict[str, Any]], config: AppConfig, city_filter: str = None) -> List[Dict[str, Any]]:
    """按订阅城市或命令行参数筛选要执行的规则"""
    if city_filter:
        target = city_filter.strip()
        matched = [
            r for r in all_rules
            if target in str(r.get("city", "")) or str(r.get("city", "")) in target
        ]
        logger.info(f"👉 指定城市 [{target}]，匹配到 {len(matched)} 个数据源")
        return matched

    subscribed = config.subscriptions.city_names
    if not subscribed:
        logger.info("未配置订阅城市，将监控规则库中的全部城市")
        return all_rules

    matched = [r for r in all_rules if r.get("city") in subscribed]
    logger.info(f"📍 已订阅城市: {', '.join(subscribed)} → 匹配 {len(matched)} 个数据源")
    return matched


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def cmd_run(args) -> int:
    logger.info("🚀 启动 YouthPolicyAlert 青年政策与安居监控引擎...")

    config = load_config(args.config)
    all_rules = load_rules(args.rules)

    if not all_rules:
        logger.error(f"❌ 未找到任何采集规则，请检查 {args.rules}")
        return 1

    rules = select_rules(all_rules, config, args.city)
    if not rules:
        logger.error("❌ 没有匹配到任何数据源，请检查订阅城市是否与 rules.yaml 中的城市一致")
        return 1

    storage = PolicyStorage(db_path=config.crawler.db_path)
    pipeline = PolicyPipeline(
        threshold=config.crawler.relevance_threshold,
        max_age_days=config.crawler.max_age_days,
    )
    requester = build_requester(config)

    try:
        # ---------- 1. 并发采集（沙盒隔离，单站失败不影响全局）----------
        results = collect_all(rules, requester=requester, max_workers=config.crawler.max_workers)

        raw_items: List[PolicyItem] = []
        for r in results:
            if r.ok:
                raw_items.extend(r.items)
                storage.log_collector_run(r.source_name, r.city, "SUCCESS", items_found=len(r.items))
            else:
                logger.error(f"❌ [{r.city}-{r.source_name}] 采集失败 ({r.error_type}): {r.error}")
                storage.log_collector_run(r.source_name, r.city, "FAILED", error_message=r.error)

        # ---------- 2. 清洗与相关性打分 ----------
        relevant = pipeline.process(raw_items)

        # ---------- 3. 详情页干货提取 ----------
        if relevant and config.crawler.fetch_detail:
            relevant = enrich_with_details(
                relevant, requester=requester,
                max_details=config.crawler.max_detail_pages,
                max_workers=max(2, config.crawler.max_workers // 2),
            )

        # ---------- 4. Dry-run：只看结果，不写库不推送 ----------
        if args.dry_run:
            _print_dry_run(relevant, results)
            return 0

        # ---------- 5. 冷启动判定 ----------
        is_first_run = not storage.is_initialized()
        use_baseline = is_first_run and config.crawler.cold_start_baseline

        if use_baseline:
            logger.info("🌱 检测到首次运行，进入【基线模式】：")
            logger.info("   本轮抓到的历史公告只入库、不推送，避免第一封邮件塞进几百条旧公告。")

        new_items = storage.record_discovered(relevant, as_baseline=use_baseline)

        notifier_center = NotifyCenter(config, storage)

        if use_baseline:
            storage.mark_initialized(len(new_items))
            cities = sorted({r.get("city", "") for r in rules if r.get("city")})
            notifier_center.send_baseline_welcome(
                baseline_count=len(new_items), rule_count=len(rules), cities=cities,
            )
            logger.info(f"✅ 基线建立完成，共记录 {len(new_items)} 条历史公告。下一轮起将只推送新增政策。")
        else:
            if is_first_run:
                storage.mark_initialized(len(new_items))
            # ---------- 6. 推送阶段（与入库解耦，失败下轮自动重试）----------
            summary = notifier_center.dispatch_pending()
            _log_notify_summary(summary, notifier_center)

        # ---------- 7. 健康检查与失效告警 ----------
        health = storage.get_health_report()
        broken = [h for h in health if h.consecutive_failures >= config.notifications.health_alert_threshold]
        if broken:
            logger.warning(f"⚠️ 有 {len(broken)} 个数据源连续失效，正在发送告警...")
            NotifyCenter(config, storage).send_health_alert(broken)

        # ---------- 8. 维护 ----------
        storage.prune_logs(keep_days=config.crawler.log_retention_days)

        states = storage.count_by_state()
        logger.info(
            f"🏁 执行完毕 | 库内累计 {storage.total_policies()} 条 "
            f"(已推送 {states['sent']} / 待推送 {states['pending']} / 基线 {states['baseline']})"
        )
        return 0

    finally:
        requester.close()


def _print_dry_run(items: List[PolicyItem], results):
    print("\n" + "=" * 72)
    print(f"🧪 [Dry-Run] 测试模式 —— 不写入数据库、不发送任何通知")
    print("=" * 72)

    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print(f"\n📡 数据源: {len(ok)} 个成功 / {len(failed)} 个失败")
    for r in ok:
        flag = "✅" if r.items else "⚠️ "
        note = "" if r.items else "  ← 连通正常但没解析出条目，选择器可能已失效"
        print(f"  {flag} [{r.city}] {r.source_name}: {len(r.items)} 条{note}")
    for r in failed:
        print(f"  ❌ [{r.city}] {r.source_name}: {r.error_type} - {(r.error or '')[:80]}")

    print(f"\n🎯 通过筛选的青年政策: {len(items)} 条\n")
    if not items:
        print("  （本轮没有匹配到相关政策。如果所有数据源都是 0 条，请运行 python main.py doctor 排查。）")
    for idx, it in enumerate(items, 1):
        print(f"--- [{idx}] {it.city} · {it.district}  (相关性 {it.relevance_score:.1f}) ---")
        print(f"  标题: {it.title}")
        print(f"  来源: {it.source_name}  发布: {it.publish_date or '未知'}")
        print(f"  人群: {it.target_audience}")
        print(f"  期限: {it.deadline}")
        if it.amount_or_quota:
            print(f"  额度: {it.amount_or_quota}")
        if it.age_limit:
            print(f"  年龄: {it.age_limit}")
        if it.apply_channel:
            print(f"  入口: {it.apply_channel}")
        print(f"  链接: {it.url}")
        print()
    print("=" * 72 + "\n")


def _log_notify_summary(summary: Dict[str, Any], center: NotifyCenter):
    if summary["skipped_reason"] == "no_pending":
        return
    if summary["skipped_reason"] == "no_channel":
        logger.warning(
            f"📭 {summary['attempted']} 条政策已入库但无法推送（没有可用通道），"
            f"配好通道后会自动补发，不会丢失。"
        )
        return
    if summary["succeeded"]:
        logger.info(
            f"📬 成功推送 {summary['succeeded']} 条政策，"
            f"送达通道: {', '.join(summary['channels_ok'])}"
        )
    if summary["failed"]:
        logger.error(
            f"📪 {summary['failed']} 条政策推送失败（通道: {', '.join(summary['channels_failed'])}），"
            f"已保留在待推送队列，下一轮将自动重试。"
        )


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    """逐个体检数据源，明确区分'连不上'与'连上了但选择器失效'"""
    print("\n" + "=" * 72)
    print("🩺 YouthPolicyAlert 系统体检")
    print("=" * 72)

    config = load_config(args.config)
    all_rules = load_rules(args.rules)

    # --- 1. 配置检查 ---
    print("\n【1/4】配置检查")
    channels = config.active_channels()
    if channels:
        print(f"  ✅ 可用推送通道: {', '.join(channels)}")
    else:
        print("  ❌ 没有任何可用的推送通道 —— 即使抓到政策也发不出去！")
    for problem in config.describe_channel_problems():
        print(f"  ⚠️  {problem}")

    subs = config.subscriptions.city_names
    print(f"  📍 订阅城市: {', '.join(subs) if subs else '(未设置，将监控全部规则)'}")

    # --- 2. 规则配置合法性 ---
    print(f"\n【2/4】规则库检查 (共 {len(all_rules)} 条规则)")
    bad_rules = 0
    rule_cities = sorted({r.get("city", "?") for r in all_rules})
    for r in all_rules:
        problems = validate_rule(r)
        if problems:
            bad_rules += 1
            print(f"  ❌ [{r.get('city')}] {r.get('source_name')}: {'; '.join(problems)}")
    if not bad_rules:
        print(f"  ✅ 全部规则配置格式合法")
    print(f"  🗺️  覆盖城市: {', '.join(rule_cities)}")

    if subs:
        missing = [c for c in subs if not any(c in str(r.get("city", "")) for r in all_rules)]
        if missing:
            print(f"  ⚠️  订阅了但规则库里没有的城市: {', '.join(missing)} —— 这些城市不会有任何数据")

    # --- 3. 数据源实测连通性 ---
    rules = select_rules(all_rules, config, args.city)
    print(f"\n【3/4】数据源实测 (本次测试 {len(rules)} 个)")
    if not rules:
        print("  ⚠️  没有要测试的数据源")
        return 1

    requester = build_requester(config)
    try:
        results = collect_all(rules, requester=requester, max_workers=config.crawler.max_workers)
    finally:
        requester.close()

    healthy, empty, failed = [], [], []
    for r in sorted(results, key=lambda x: (x.city, x.source_name)):
        if not r.ok:
            failed.append(r)
        elif not r.items:
            empty.append(r)
        else:
            healthy.append(r)

    for r in healthy:
        sample = r.items[0].title[:34] if r.items else ""
        print(f"  ✅ [{r.city}] {r.source_name}: {len(r.items)} 条  例:「{sample}…」")
    for r in empty:
        print(f"  ⚠️  [{r.city}] {r.source_name}: 网页能打开，但一条都没解析出来")
        print(f"       → 多半是官网改版导致选择器失效，请更新 rules.yaml 里的 selectors.list_item")
    for r in failed:
        hint = {
            "NOT_FOUND": "页面已不存在，请更新 url",
            "BLOCKED": "被网站反爬拦截，可尝试换网络环境或配置 crawler.proxy",
            "BAD_RULE": "规则配置有误",
        }.get(r.error_type, "网络异常或站点故障")
        print(f"  ❌ [{r.city}] {r.source_name}: {r.error_type} → {hint}")
        print(f"       {(r.error or '')[:100]}")

    # --- 4. 历史健康与队列状态 ---
    print("\n【4/4】本地数据库状态")
    storage = PolicyStorage(db_path=config.crawler.db_path)
    states = storage.count_by_state()
    print(f"  📚 累计政策: {storage.total_policies()} 条")
    print(f"     已推送 {states['sent']} | 待推送 {states['pending']} | 冷启动基线 {states['baseline']} | 放弃 {states['abandoned']}")
    print(f"  🌱 是否已建立基线: {'是' if storage.is_initialized() else '否（下次 run 会先建基线）'}")
    if states["pending"]:
        print(f"  ⏳ 有 {states['pending']} 条政策待推送，下次 run 会自动尝试发送")
    if states["abandoned"]:
        print(f"  ⚠️  有 {states['abandoned']} 条政策重试多次仍失败，请检查推送通道配置")

    # --- 总结 ---
    print("\n" + "=" * 72)
    total = len(results)
    print(f"体检结论: {len(healthy)}/{total} 个数据源工作正常")
    if empty:
        print(f"  ⚠️  {len(empty)} 个数据源选择器失效，需要更新规则")
    if failed:
        print(f"  ❌ {len(failed)} 个数据源无法访问")
    if not channels:
        print("  ❌ 推送通道未配置，请先配置邮箱后再正式运行")
    if healthy and channels:
        print("  ✅ 核心链路可用，可以正式运行 python main.py run")
    print("=" * 72 + "\n")

    return 0 if healthy else 1


# ---------------------------------------------------------------------------
# test-notify
# ---------------------------------------------------------------------------
def cmd_test_notify(args) -> int:
    """发一条样例通知，验证推送通道配置是否正确"""
    from core.models import PolicyCategory

    config = load_config(args.config)
    storage = PolicyStorage(db_path=config.crawler.db_path)
    center = NotifyCenter(config, storage)

    if not center.has_channel:
        print("❌ 没有可用的推送通道。问题如下：")
        for p in config.describe_channel_problems() or ["所有通道均未启用"]:
            print(f"   - {p}")
        print("\n请编辑 config/config.yaml 或设置环境变量后重试。")
        return 1

    print(f"📮 将通过以下通道发送测试消息: {', '.join(center.channel_names())}")

    sample = PolicyItem(
        title="【测试】2026年第三批青年人才公寓配租公告",
        url="https://github.com/",
        city="示例市",
        district="示例区",
        source_name="YouthPolicyAlert 通道测试",
        category=PolicyCategory.HOUSING,
        target_audience="本科 / 硕士 / 博士 / 应届毕业生",
        deadline="2026-09-30 截止",
        amount_or_quota="每月 1500 元 ｜ 房源 1200 套",
        age_limit="35周岁以下",
        apply_channel="示例市人才安居服务平台",
        publish_date="2026-08-24",
        raw_content="这是一条测试消息。你能看到它，说明推送通道已经完全打通，可以正式开始监控了。",
    )

    ok_any = False
    for name, notifier in center.channels:
        try:
            if notifier.send([sample]):
                print(f"  ✅ {name}: 发送成功")
                ok_any = True
            else:
                print(f"  ❌ {name}: 发送失败（详见上方日志）")
        except Exception as e:
            print(f"  ❌ {name}: 异常 {e}")

    if ok_any:
        print("\n🎉 测试消息已发出，请到你的邮箱/微信查收。")
        return 0
    print("\n❌ 所有通道均发送失败，请检查账号密码/授权码是否正确。")
    return 1


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def cmd_stats(args) -> int:
    config = load_config(args.config)
    storage = PolicyStorage(db_path=config.crawler.db_path)

    s = storage.stats()
    print("\n📊 YouthPolicyAlert 运行统计")
    print("-" * 50)
    print(f"  累计收录政策: {s['total_policies']} 条")
    print(f"  覆盖城市数量: {s['active_cities']} 个")
    print(f"  今日新增: {s['today_count']} 条")
    print(f"  已推送: {s['sent']} | 待推送: {s['pending']} | 基线: {s['baseline']} | 已放弃: {s['abandoned']}")

    health = storage.get_health_report()
    if health:
        print("\n🩺 数据源健康状况")
        print("-" * 50)
        for h in health[:25]:
            icon = "❌" if h.consecutive_failures >= 3 else ("⚠️ " if h.consecutive_failures else "✅")
            print(f"  {icon} [{h.city}] {h.source_name[:32]}")
            if h.consecutive_failures:
                print(f"        连续失败 {h.consecutive_failures} 次 | 最后成功: {h.last_success_at or '从未'}")
    print()
    return 0


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="YouthPolicyAlert - 青年政策与福利房智能监控系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
常用示例:
  python main.py doctor                  # 先体检，确认数据源和通道都正常
  python main.py test-notify             # 发一条测试消息，验证能收到
  python main.py run --dry-run           # 试跑，只看结果不写库不推送
  python main.py run                     # 正式执行一轮监控
  python main.py run --city 深圳          # 只跑深圳
  python main.py stats                   # 查看统计与健康状况
""",
    )
    parser.add_argument("--config", default="config/config.yaml", help="配置文件路径")
    parser.add_argument("--rules", default="config/rules.yaml", help="规则库路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="执行一轮完整监控")
    p_run.add_argument("--dry-run", action="store_true", help="只抓取并打印，不写库不推送")
    p_run.add_argument("--city", help="仅抓取指定城市")

    p_doctor = sub.add_parser("doctor", help="体检所有数据源与配置")
    p_doctor.add_argument("--city", help="仅体检指定城市")

    sub.add_parser("test-notify", help="发送测试通知，验证推送通道")
    sub.add_parser("stats", help="查看统计与数据源健康状况")

    # 兼容 v1 用法：python main.py --dry-run --city 深圳
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--city", help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("YouthPolicyAlert").setLevel(logging.DEBUG)
        for name in logging.root.manager.loggerDict:
            if name.startswith("YouthPolicyAlert"):
                logging.getLogger(name).setLevel(logging.DEBUG)

    command = args.command or "run"

    handlers = {
        "run": cmd_run,
        "doctor": cmd_doctor,
        "test-notify": cmd_test_notify,
        "stats": cmd_stats,
    }
    handler = handlers.get(command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except KeyboardInterrupt:
        logger.info("已中断。")
        return 130
    except Exception as e:
        logger.exception(f"❌ 执行过程中出现未捕获异常: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
