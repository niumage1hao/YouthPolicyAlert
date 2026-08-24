"""
core/notify_center.py
多通道推送编排中心。

核心职责（也是 v1 缺失的部分）：
1. 从存储层取出【待推送 + 上轮失败重试】的政策
2. 依次尝试所有已启用通道
3. **只要有任意一个通道成功**，才把政策标记为已推送；
   全部通道失败则保持待推送状态，下一轮自动重试 —— 政策绝不会静默丢失
4. 冷启动首次运行发送"监控已启动"确认信，让用户立刻知道链路是通的
5. 数据源连续失效时向用户发健康告警，避免"以为在监控其实早就抓不到了"
"""
import logging
from typing import List, Dict, Any, Tuple, Optional

from core.models import PolicyItem, CollectorHealth
from core.storage import PolicyStorage
from core.config_schema import AppConfig
from notifiers.email_notifier import EmailNotifier
from notifiers.webhook_notifier import (
    PushPlusNotifier, FeishuNotifier, WeComNotifier, ServerChanNotifier,
)

logger = logging.getLogger("YouthPolicyAlert.NotifyCenter")


class NotifyCenter:
    """推送编排器"""

    def __init__(self, config: AppConfig, storage: PolicyStorage):
        self.config = config
        self.storage = storage
        self.channels = self._build_channels()

    # ------------------------------------------------------------------
    def _build_channels(self) -> List[Tuple[str, Any]]:
        """根据配置构建所有可用通道"""
        built: List[Tuple[str, Any]] = []
        n = self.config.notifications

        ok, reason = n.email.is_usable()
        if ok:
            built.append(("邮件", EmailNotifier(n.email.model_dump())))
        elif n.email.enabled:
            logger.warning(f"⚠️ 邮件通道已启用但配置不完整：{reason}")

        ok, reason = n.pushplus.is_usable()
        if ok:
            built.append(("PushPlus微信", PushPlusNotifier(token=n.pushplus.token, topic=n.pushplus.topic)))
        elif n.pushplus.enabled:
            logger.warning(f"⚠️ PushPlus 已启用但配置不完整：{reason}")

        ok, _ = n.feishu.is_usable()
        if ok:
            built.append(("飞书", FeishuNotifier(webhook_url=n.feishu.webhook_url)))

        ok, _ = n.wecom.is_usable()
        if ok:
            built.append(("企业微信", WeComNotifier(webhook_url=n.wecom.webhook_url)))

        ok, _ = n.serverchan.is_usable()
        if ok:
            built.append(("Server酱", ServerChanNotifier(send_key=n.serverchan.send_key)))

        return built

    @property
    def has_channel(self) -> bool:
        return bool(self.channels)

    def channel_names(self) -> List[str]:
        return [name for name, _ in self.channels]

    # ------------------------------------------------------------------
    def dispatch_pending(self) -> Dict[str, Any]:
        """
        推送所有待发政策（含上轮失败重试的）。

        :return: 本次推送的统计结果
        """
        summary = {
            "attempted": 0, "succeeded": 0, "failed": 0,
            "channels_ok": [], "channels_failed": [], "skipped_reason": None,
        }

        limit = self.config.notifications.max_items_per_push
        pending = self.storage.get_pending_notifications(limit=limit)

        if not pending:
            logger.info("🎉 没有待推送的政策。")
            summary["skipped_reason"] = "no_pending"
            return summary

        summary["attempted"] = len(pending)

        if not self.has_channel:
            logger.warning(
                f"⚠️ 有 {len(pending)} 条政策待推送，但没有任何可用的推送通道！\n"
                f"   请检查配置：{'; '.join(self.config.describe_channel_problems()) or '所有通道均未启用'}"
            )
            summary["skipped_reason"] = "no_channel"
            # 注意：这里【不】标记为已推送，等用户配好通道后仍会推送出去
            return summary

        logger.info(f"📮 准备通过 {len(self.channels)} 个通道推送 {len(pending)} 条政策...")

        any_success = False
        errors: List[str] = []

        for name, notifier in self.channels:
            try:
                ok = notifier.send(pending)
                if ok:
                    any_success = True
                    summary["channels_ok"].append(name)
                    logger.info(f"✅ {name} 推送成功")
                else:
                    summary["channels_failed"].append(name)
                    errors.append(f"{name}: 返回失败")
                    logger.warning(f"❌ {name} 推送失败")
            except Exception as e:
                summary["channels_failed"].append(name)
                errors.append(f"{name}: {e}")
                logger.error(f"❌ {name} 推送异常: {e}")

        ids = [it.unique_id for it in pending]
        if any_success:
            # 只要有一个通道成功送达，就认为用户已经收到，不再重复打扰
            self.storage.mark_notified(ids)
            summary["succeeded"] = len(pending)
        else:
            # 全部通道失败 —— 保持待推送状态，下一轮继续重试（这正是 v1 丢消息的地方）
            self.storage.mark_notify_failed(ids, "; ".join(errors)[:400])
            summary["failed"] = len(pending)

        return summary

    # ------------------------------------------------------------------
    def send_baseline_welcome(self, baseline_count: int, rule_count: int, cities: List[str]) -> bool:
        """
        冷启动完成后发一封"监控已启动"确认信。

        作用有二：
        1. 让用户第一次运行就能确认"整条链路真的通了"（这正是原项目从未被验证的环节）
        2. 说明为什么第一次没有收到政策列表，避免用户误以为程序坏了
        """
        if not self.has_channel:
            logger.info("冷启动完成，但未配置推送通道，跳过欢迎通知。")
            return False

        city_text = "、".join(cities[:12]) if cities else "全部已配置城市"
        if len(cities) > 12:
            city_text += f" 等 {len(cities)} 个城市"

        for name, notifier in self.channels:
            try:
                if notifier.send_plain(
                    title="✅ 青年安居政策监控已启动",
                    lines=[
                        "你的政策监控已经成功跑通，这封邮件就是链路正常的证明。",
                        "",
                        f"📚 已建立历史基线：{baseline_count} 条现存公告（不会推送给你，避免刷屏）",
                        f"📡 正在监控数据源：{rule_count} 个",
                        f"📍 覆盖城市：{city_text}",
                        "",
                        "从现在开始，只要这些官方渠道发布【新的】保租房配租、人才公寓、",
                        "应届生租房/生活补贴等公告，你会在第一时间收到提醒。",
                        "",
                        "如果长时间没有收到任何提醒，可以运行 `python main.py doctor` 做一次体检。",
                    ],
                ):
                    logger.info(f"✅ 已通过 {name} 发送启动确认通知")
                    return True
            except Exception as e:
                logger.warning(f"发送启动确认通知失败 ({name}): {e}")
        return False

    # ------------------------------------------------------------------
    def send_health_alert(self, broken: List[CollectorHealth]) -> bool:
        """
        数据源失效告警。

        解决 v1 的"静默失效"问题：政府网站改版后选择器失效，
        v1 只把失败写进本地日志表，用户永远不会主动去查，
        于是"再也收不到提醒"被误以为"最近没有新政策"。
        """
        if not broken or not self.has_channel:
            return False

        threshold = self.config.notifications.health_alert_cooldown_hours
        if not self.storage.should_send_health_alert(cooldown_hours=threshold):
            logger.info("健康告警处于冷却期内，本次跳过。")
            return False

        lines = [
            "以下数据源已连续多次抓取失败或抓不到任何内容，很可能是官方网站改版导致规则失效：",
            "",
        ]
        for h in broken[:15]:
            lines.append(f"❌ [{h.city}] {h.source_name}")
            lines.append(f"    连续失败 {h.consecutive_failures} 次 | 最近一次状态: {h.last_status}")
            if h.last_error:
                lines.append(f"    错误: {h.last_error[:100]}")
            if h.last_success_at:
                lines.append(f"    最后一次成功: {h.last_success_at}")
            lines.append("")

        lines += [
            "建议处理方式：",
            "1. 运行 `python main.py doctor` 查看每个数据源的详细体检结果",
            "2. 打开对应官网确认公告页 URL 是否变更",
            "3. 在 config/rules.yaml 中更新该数据源的 url 与 selectors",
            "",
            "在修复之前，这些城市的政策将无法被监控到。",
        ]

        for name, notifier in self.channels:
            try:
                if notifier.send_plain(title=f"⚠️ 有 {len(broken)} 个政策数据源失效了", lines=lines):
                    self.storage.mark_health_alert_sent()
                    logger.info(f"✅ 已通过 {name} 发送数据源失效告警")
                    return True
            except Exception as e:
                logger.warning(f"发送健康告警失败 ({name}): {e}")
        return False
