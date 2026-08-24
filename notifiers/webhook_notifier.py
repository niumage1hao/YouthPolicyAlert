"""
notifiers/webhook_notifier.py
多渠道 Webhook 推送器：PushPlus 微信 / Server酱 / 飞书 / 企业微信。

v2 改进：
1. 新增 Server酱、企业微信两个通道
2. 全部实现 send_plain()，支持系统通知（启动确认 / 数据源失效告警）
3. 推送内容补齐 v2 新提取的截止日期、年龄限制等干货字段
4. 严格判定"真正成功"：不再把 HTTP 200 但业务码报错的情况当成功（v1 的飞书通道有此问题）
5. 统一超时与异常处理，失败返回 False 交由 NotifyCenter 安排重试
"""
import logging
from typing import List, Dict, Any, Optional

import httpx

from core.models import PolicyItem
from notifiers.base import BaseNotifier

logger = logging.getLogger("YouthPolicyAlert.WebhookNotifier")

TIMEOUT = 15.0


def _policy_lines(items: List[PolicyItem]) -> List[str]:
    """把政策列表渲染成 Markdown 文本行（微信/Server酱通用）"""
    lines: List[str] = []
    for it in items:
        lines.append(f"### 📍 [{it.city}·{it.district}] {it.title}")
        lines.append(f"- **发布单位**: {it.source_name}")
        if it.publish_date:
            lines.append(f"- **发布时间**: {it.publish_date}")
        if it.deadline and it.deadline != "以官方公告为准":
            lines.append(f"- **⏰ 申报期限**: {it.deadline}")
        lines.append(f"- **适合人群**: {it.target_audience}")
        if it.amount_or_quota:
            lines.append(f"- **额度/房源**: {it.amount_or_quota}")
        if it.age_limit:
            lines.append(f"- **年龄要求**: {it.age_limit}")
        lines.append(f"- **原文链接**: [点击查看官方公告]({it.url})")
        lines.append("")
        lines.append("---")
        lines.append("")
    return lines


class PushPlusNotifier(BaseNotifier):
    """PushPlus 微信推送 (https://www.pushplus.plus/)"""

    name = "pushplus"

    def __init__(self, token: str, topic: Optional[str] = None):
        self.token = token
        self.topic = topic
        self.api_url = "https://www.pushplus.plus/send"

    def _post(self, title: str, content: str) -> bool:
        if not self.token:
            logger.warning("PushPlus token 为空，跳过推送")
            return False
        payload: Dict[str, Any] = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": "markdown",
        }
        if self.topic:
            payload["topic"] = self.topic
        try:
            resp = httpx.post(self.api_url, json=payload, timeout=TIMEOUT)
            data = resp.json()
            if data.get("code") == 200:
                logger.info("✅ PushPlus 推送成功")
                return True
            logger.warning(f"PushPlus 返回业务错误: {data}")
            return False
        except Exception as e:
            logger.error(f"❌ PushPlus 推送失败: {e}")
            return False

    def send(self, items: List[PolicyItem]) -> bool:
        if not items:
            return True
        title = f"🏠【政策提醒】{len(items)} 条新福利房/补贴通知"
        return self._post(title, "\n".join(_policy_lines(items)))

    def send_plain(self, title: str, lines: List[str]) -> bool:
        return self._post(title, "\n\n".join(l for l in lines if l is not None))


class ServerChanNotifier(BaseNotifier):
    """Server酱 Turbo 微信推送 (https://sct.ftqq.com/)"""

    name = "serverchan"

    def __init__(self, send_key: str):
        self.send_key = send_key

    def _post(self, title: str, content: str) -> bool:
        if not self.send_key:
            return False
        url = f"https://sctapi.ftqq.com/{self.send_key}.send"
        try:
            resp = httpx.post(url, data={"title": title[:100], "desp": content}, timeout=TIMEOUT)
            data = resp.json()
            if data.get("code") == 0:
                logger.info("✅ Server酱推送成功")
                return True
            logger.warning(f"Server酱返回业务错误: {data}")
            return False
        except Exception as e:
            logger.error(f"❌ Server酱推送失败: {e}")
            return False

    def send(self, items: List[PolicyItem]) -> bool:
        if not items:
            return True
        title = f"🏠 发现 {len(items)} 条新的青年安居政策"
        return self._post(title, "\n".join(_policy_lines(items)))

    def send_plain(self, title: str, lines: List[str]) -> bool:
        return self._post(title, "\n\n".join(l for l in lines if l is not None))


class FeishuNotifier(BaseNotifier):
    """飞书自定义机器人 Webhook"""

    name = "feishu"

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _post(self, payload: Dict[str, Any]) -> bool:
        if not self.webhook_url:
            return False
        try:
            resp = httpx.post(self.webhook_url, json=payload, timeout=TIMEOUT)
            # 飞书即使参数错误也返回 HTTP 200，必须看业务返回码
            try:
                data = resp.json()
            except Exception:
                data = {}
            if resp.status_code == 200 and data.get("code", 0) in (0, None):
                logger.info("✅ 飞书推送成功")
                return True
            logger.warning(f"飞书返回异常: HTTP {resp.status_code} {data}")
            return False
        except Exception as e:
            logger.error(f"❌ 飞书推送失败: {e}")
            return False

    def send(self, items: List[PolicyItem]) -> bool:
        if not items:
            return True

        elements: List[Dict[str, Any]] = []
        for it in items:
            parts = [f"**[{it.city}·{it.district}]** [{it.title}]({it.url})",
                     f"🏢 {it.source_name}"]
            if it.deadline and it.deadline != "以官方公告为准":
                parts.append(f"⏰ 期限: {it.deadline}")
            parts.append(f"👥 门槛: {it.target_audience}")
            if it.amount_or_quota:
                parts.append(f"🎯 额度: {it.amount_or_quota}")
            elements.append({
                "tag": "div",
                "text": {"tag": "lark_md", "content": "\n".join(parts)},
            })
            elements.append({"tag": "hr"})

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"📢 青年安居政策通 · {len(items)} 条新动态"},
                    "template": "blue",
                },
                "elements": elements,
            },
        }
        return self._post(payload)

    def send_plain(self, title: str, lines: List[str]) -> bool:
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {"tag": "div", "title": {"tag": "plain_text", "content": title}, "template": "orange"},
                "elements": [{
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(l for l in lines if l is not None)},
                }],
            },
        }
        return self._post(payload)


class WeComNotifier(BaseNotifier):
    """企业微信群机器人 Webhook"""

    name = "wecom"

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def _post(self, markdown: str) -> bool:
        if not self.webhook_url:
            return False
        # 企业微信 markdown 单条上限 4096 字节，超长要截断
        content = markdown
        if len(content.encode("utf-8")) > 3800:
            content = content.encode("utf-8")[:3800].decode("utf-8", errors="ignore") + "\n\n…（内容过长已截断）"
        try:
            resp = httpx.post(
                self.webhook_url,
                json={"msgtype": "markdown", "markdown": {"content": content}},
                timeout=TIMEOUT,
            )
            data = resp.json()
            if data.get("errcode") == 0:
                logger.info("✅ 企业微信推送成功")
                return True
            logger.warning(f"企业微信返回业务错误: {data}")
            return False
        except Exception as e:
            logger.error(f"❌ 企业微信推送失败: {e}")
            return False

    def send(self, items: List[PolicyItem]) -> bool:
        if not items:
            return True
        header = f"### 🏠 青年安居政策通 · {len(items)} 条新动态\n\n"
        body: List[str] = []
        for it in items:
            body.append(f"**[{it.city}·{it.district}]** [{it.title}]({it.url})")
            if it.deadline and it.deadline != "以官方公告为准":
                body.append(f"> ⏰ 期限: <font color=\"warning\">{it.deadline}</font>")
            body.append(f"> 👥 {it.target_audience}")
            if it.amount_or_quota:
                body.append(f"> 🎯 {it.amount_or_quota}")
            body.append("")
        return self._post(header + "\n".join(body))

    def send_plain(self, title: str, lines: List[str]) -> bool:
        content = f"### {title}\n\n" + "\n".join(l for l in lines if l is not None)
        return self._post(content)
