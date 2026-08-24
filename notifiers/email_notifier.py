"""
notifiers/email_notifier.py
响应式 HTML 邮件推送器。

v2 改进：
1. 卡片新增【申报截止日期】【年龄限制】【申报入口】等 v2 才提取到的干货字段，
   并把截止日期做成醒目高亮 —— 这是用户最怕错过的信息。
2. 邮件主题信息量更大：带上城市与最关键的一条政策标题。
3. 兼容 multipart/alternative（纯文本 + HTML），避免部分邮箱把 HTML 邮件判为垃圾邮件。
4. 新增 send_plain()，支持系统通知（启动确认 / 数据源失效告警）。
5. 明确区分"配置不全"与"发送失败"，日志能直接定位问题。
"""
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import formataddr, formatdate
from typing import List, Dict, Any

from jinja2 import Template

from core.models import PolicyItem
from notifiers.base import BaseNotifier

logger = logging.getLogger("YouthPolicyAlert.EmailNotifier")


EMAIL_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Roboto, sans-serif; background-color:#f4f6f9; margin:0; padding:16px; color:#333; }
  .container { max-width:660px; margin:0 auto; background:#fff; border-radius:12px; overflow:hidden; box-shadow:0 4px 15px rgba(0,0,0,.06); }
  .header { background:linear-gradient(135deg,#1890ff,#096dd9); color:#fff; padding:24px 28px; }
  .header h1 { margin:0; font-size:20px; font-weight:600; }
  .header p { margin:8px 0 0; opacity:.92; font-size:13px; }
  .content { padding:22px 28px; }
  .card { border:1px solid #e8e8e8; border-radius:8px; padding:16px 18px; margin-bottom:18px; background:#fafafa; border-left:4px solid #1890ff; }
  .card.urgent { border-left-color:#f5222d; background:#fff7f6; }
  .tag { display:inline-block; font-size:11px; font-weight:700; padding:3px 8px; border-radius:4px; margin-right:6px; }
  .tag-housing { background:#e6f7ff; color:#1890ff; }
  .tag-subsidy { background:#f6ffed; color:#52c41a; }
  .tag-other  { background:#f9f0ff; color:#722ed1; }
  .tag-city   { background:#fff7e6; color:#fa8c16; }
  .title { font-size:15px; font-weight:600; margin:10px 0 12px; color:#262626; line-height:1.45; }
  table.info { width:100%; border-collapse:collapse; font-size:13px; }
  table.info td { padding:3px 0; vertical-align:top; }
  td.label { color:#8c8c8c; width:76px; white-space:nowrap; }
  td.value { color:#595959; }
  .hl-deadline { color:#cf1322; font-weight:700; }
  .hl-amount { color:#d4380d; font-weight:700; }
  .summary { margin-top:10px; padding:9px 11px; background:#fff; border:1px dashed #d9d9d9; border-radius:6px; font-size:12px; color:#666; line-height:1.6; }
  .btn { display:inline-block; margin-top:12px; background:#1890ff; color:#fff !important; text-decoration:none; font-size:13px; font-weight:500; padding:8px 16px; border-radius:6px; }
  .footer { background:#f0f2f5; padding:18px 28px; text-align:center; font-size:12px; color:#8c8c8c; line-height:1.7; }
</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🏠 青年安居与政策通 · 最新提醒</h1>
      <p>为你监控到 {{ items|length }} 条新的福利房与补贴政策，请及时申报</p>
    </div>
    <div class="content">
      {% for item in items %}
      <div class="card {% if item.deadline and item.deadline != '以官方公告为准' %}urgent{% endif %}">
        <div>
          {% if item.category.value == 'housing' %}
            <span class="tag tag-housing">🏠 保障房 / 人才公寓</span>
          {% elif 'subsidy' in item.category.value %}
            <span class="tag tag-subsidy">💰 补贴发放</span>
          {% elif item.category.value == 'employment' %}
            <span class="tag tag-subsidy">💼 就业创业</span>
          {% else %}
            <span class="tag tag-other">📢 青年政策</span>
          {% endif %}
          <span class="tag tag-city">📍 {{ item.city }}{% if item.district and item.district != '全市' %} · {{ item.district }}{% endif %}</span>
        </div>

        <div class="title">{{ item.title }}</div>

        <table class="info">
          <tr><td class="label">发布单位</td><td class="value">{{ item.source_name }}</td></tr>
          {% if item.publish_date %}
          <tr><td class="label">发布时间</td><td class="value">{{ item.publish_date }}</td></tr>
          {% endif %}
          {% if item.deadline and item.deadline != '以官方公告为准' %}
          <tr><td class="label">⏰ 申报期限</td><td class="value hl-deadline">{{ item.deadline }}</td></tr>
          {% endif %}
          <tr><td class="label">适合人群</td><td class="value">{{ item.target_audience }}</td></tr>
          {% if item.age_limit %}
          <tr><td class="label">年龄要求</td><td class="value">{{ item.age_limit }}</td></tr>
          {% endif %}
          {% if item.amount_or_quota %}
          <tr><td class="label">额度 / 房源</td><td class="value hl-amount">{{ item.amount_or_quota }}</td></tr>
          {% endif %}
          {% if item.apply_channel %}
          <tr><td class="label">申报入口</td><td class="value">{{ item.apply_channel }}</td></tr>
          {% endif %}
          {% if item.notes %}
          <tr><td class="label">备注</td><td class="value">{{ item.notes }}</td></tr>
          {% endif %}
        </table>

        {% if item.raw_content %}
        <div class="summary">{{ item.raw_content }}</div>
        {% endif %}

        <a href="{{ item.url }}" target="_blank" class="btn">查看官方原文 / 立即申报 &rarr;</a>
      </div>
      {% endfor %}
    </div>
    <div class="footer">
      <p>本邮件由开源项目 <strong>YouthPolicyAlert</strong> 自动生成推送<br>
      数据来源于各地政府官方主动公开信息，申报要求请以官方最终文件为准。</p>
    </div>
  </div>
</body>
</html>
"""

PLAIN_HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;background:#f4f6f9;padding:16px;margin:0;">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 15px rgba(0,0,0,.06);">
    <div style="background:linear-gradient(135deg,#1890ff,#096dd9);color:#fff;padding:22px 26px;">
      <h1 style="margin:0;font-size:19px;font-weight:600;">{{ title }}</h1>
    </div>
    <div style="padding:22px 26px;font-size:14px;color:#444;line-height:1.85;">
      {% for line in lines %}
        {% if line %}<div>{{ line }}</div>{% else %}<div style="height:10px;"></div>{% endif %}
      {% endfor %}
    </div>
    <div style="background:#f0f2f5;padding:16px 26px;text-align:center;font-size:12px;color:#8c8c8c;">
      YouthPolicyAlert · 青年安居与政策通
    </div>
  </div>
</body>
</html>
"""


class EmailNotifier(BaseNotifier):
    """SMTP 邮件通知器"""

    name = "email"

    def __init__(self, config: Dict[str, Any]):
        self.smtp_host = config.get("smtp_host", "smtp.qq.com")
        self.smtp_port = int(config.get("smtp_port", 465))
        self.smtp_ssl = config.get("smtp_ssl", True)
        self.username = config.get("username", "") or ""
        self.password = config.get("password", "") or ""
        self.from_addr = config.get("from_addr") or self.username
        to = config.get("to_addrs", []) or []
        self.to_addrs = [to] if isinstance(to, str) else list(to)

    # ------------------------------------------------------------------
    def _config_problem(self) -> str:
        if not self.username:
            return "缺少发信邮箱账号 (username)"
        if not self.password:
            return "缺少邮箱 SMTP 授权码 (password)"
        if not self.to_addrs:
            return "缺少收件邮箱 (to_addrs)"
        return ""

    def _deliver(self, subject: str, html_body: str, text_body: str) -> bool:
        problem = self._config_problem()
        if problem:
            logger.warning(f"邮件配置不完整，跳过发送：{problem}")
            return False

        message = MIMEMultipart("alternative")
        message["From"] = formataddr((str(Header("青年政策通", "utf-8")), self.from_addr))
        message["To"] = ", ".join(self.to_addrs)
        message["Subject"] = Header(subject, "utf-8")
        message["Date"] = formatdate(localtime=True)
        # 纯文本版在前、HTML 版在后（RFC 规定后者优先展示），可显著降低进垃圾箱概率
        message.attach(MIMEText(text_body, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))

        server = None
        try:
            logger.info(f"正在通过 {self.smtp_host}:{self.smtp_port} 发送邮件至 {self.to_addrs} ...")
            if self.smtp_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=25)
                server.ehlo()
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=25)
                server.ehlo()
                # 只在服务器确实支持时才升级 TLS。
                # 无条件调用 starttls() 会让不支持该扩展的内网/自建 SMTP 中继直接报错。
                if server.has_extn("starttls"):
                    server.starttls()
                    server.ehlo()

            # 同理：服务器不提供 AUTH（如内网免认证中继）时不应强行登录
            if self.password and server.has_extn("auth"):
                server.login(self.username, self.password)
            elif self.password:
                logger.debug("SMTP 服务器未提供 AUTH 扩展，跳过登录直接投递")

            server.sendmail(self.from_addr, self.to_addrs, message.as_string())
            logger.info("✅ 邮件发送成功")
            return True
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"❌ 邮箱认证失败（授权码错误或未开启 SMTP 服务）: {e}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"❌ SMTP 发送失败: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ 邮件发送异常: {e}")
            return False
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    def send(self, items: List[PolicyItem]) -> bool:
        if not items:
            logger.info("无新政策，无需发送邮件。")
            return True

        html_body = Template(EMAIL_HTML_TEMPLATE).render(items=items)
        text_body = self._render_text(items)

        cities = list(dict.fromkeys(it.city for it in items if it.city))
        city_label = "/".join(cities[:3]) + ("等" if len(cities) > 3 else "")
        headline = items[0].title[:22]
        subject = f"【政策速递】{city_label} {len(items)} 条新政策 · {headline}…"

        return self._deliver(subject, html_body, text_body)

    @staticmethod
    def _render_text(items: List[PolicyItem]) -> str:
        lines = [f"青年安居与政策通 · 为你监控到 {len(items)} 条新政策", "=" * 40, ""]
        for i, it in enumerate(items, 1):
            lines.append(f"[{i}] {it.city}·{it.district} {it.title}")
            lines.append(f"    发布单位: {it.source_name}")
            if it.publish_date:
                lines.append(f"    发布时间: {it.publish_date}")
            if it.deadline and it.deadline != "以官方公告为准":
                lines.append(f"    申报期限: {it.deadline}")
            lines.append(f"    适合人群: {it.target_audience}")
            if it.amount_or_quota:
                lines.append(f"    额度/房源: {it.amount_or_quota}")
            if it.age_limit:
                lines.append(f"    年龄要求: {it.age_limit}")
            lines.append(f"    原文链接: {it.url}")
            lines.append("")
        lines.append("-" * 40)
        lines.append("本邮件由开源项目 YouthPolicyAlert 自动推送，请以官方文件为准。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    def send_plain(self, title: str, lines: List[str]) -> bool:
        html_body = Template(PLAIN_HTML_TEMPLATE).render(title=title, lines=lines)
        text_body = title + "\n" + "=" * 40 + "\n" + "\n".join(lines)
        return self._deliver(title, html_body, text_body)
