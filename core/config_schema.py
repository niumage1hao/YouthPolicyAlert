"""
core/config_schema.py
配置加载与校验。

v1 的配置是裸 dict，写错一个 key（比如把 to_addrs 写成 to_addr）不会有任何提示，
只会在运行时静默跳过邮件发送 —— 用户以为在监控，其实什么都收不到。
v2 用 pydantic 做结构化校验，配置有问题时立刻给出人话提示。
"""
import os
import logging
from typing import List, Optional, Dict, Any

import yaml
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("YouthPolicyAlert.Config")


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_ssl: bool = True
    username: str = ""
    password: str = ""
    from_addr: Optional[str] = None
    to_addrs: List[str] = Field(default_factory=list)

    @field_validator("to_addrs", mode="before")
    @classmethod
    def _coerce_list(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    def is_usable(self) -> tuple[bool, str]:
        """返回 (是否可用, 不可用原因)"""
        placeholders = {"your_email@qq.com", "your_smtp_auth_code", "your_email"}
        if self.username.strip().lower() in placeholders or self.password.strip().lower() in placeholders:
            return False, "邮件配置仍是示例占位值，请填写真实账号、SMTP 授权码和收件邮箱"
        if any(addr.strip().lower() in placeholders for addr in self.to_addrs):
            return False, "邮件配置仍是示例占位值，请填写真实账号、SMTP 授权码和收件邮箱"
        if not self.enabled:
            return False, "邮件通道未启用 (notifications.email.enabled = false)"
        if not self.username:
            return False, "缺少发信邮箱账号 (notifications.email.username)"
        if not self.password:
            return False, "缺少邮箱 SMTP 授权码 (notifications.email.password)"
        if not self.to_addrs:
            return False, "缺少收件邮箱 (notifications.email.to_addrs)"
        return True, ""


class PushPlusConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    topic: Optional[str] = None

    def is_usable(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "PushPlus 未启用"
        if not self.token:
            return False, "缺少 PushPlus token"
        return True, ""


class WebhookConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""

    def is_usable(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Webhook 未启用"
        if not self.webhook_url.startswith("http"):
            return False, "webhook_url 无效"
        return True, ""


class ServerChanConfig(BaseModel):
    enabled: bool = False
    send_key: str = ""

    def is_usable(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "Server酱未启用"
        if not self.send_key:
            return False, "缺少 Server酱 SendKey"
        return True, ""


class NotificationsConfig(BaseModel):
    email: EmailConfig = Field(default_factory=EmailConfig)
    pushplus: PushPlusConfig = Field(default_factory=PushPlusConfig)
    feishu: WebhookConfig = Field(default_factory=WebhookConfig)
    wecom: WebhookConfig = Field(default_factory=WebhookConfig)
    serverchan: ServerChanConfig = Field(default_factory=ServerChanConfig)

    # 单次推送最多包含多少条，避免一封邮件塞几百条
    max_items_per_push: int = 30
    # 健康告警：数据源连续失败多少次后通知维护者
    health_alert_threshold: int = 3
    health_alert_cooldown_hours: int = 24


class CitySubscription(BaseModel):
    name: str
    districts: List[str] = Field(default_factory=list)
    categories: List[str] = Field(default_factory=list)


class SubscriptionsConfig(BaseModel):
    cities: List[CitySubscription] = Field(default_factory=list)

    @property
    def city_names(self) -> List[str]:
        return [c.name for c in self.cities if c.name]


class CrawlerConfig(BaseModel):
    min_delay: float = 1.0
    max_delay: float = 2.5
    timeout: float = 20.0
    max_retries: int = 3
    max_workers: int = 5
    # 默认校验证书；确有老旧政务站证书问题时再在部署配置中显式关闭。
    verify_ssl: bool = True
    proxy: Optional[str] = None
    enable_warmup: bool = True
    db_path: str = "data/policy.db"

    # 详情页深度提取
    fetch_detail: bool = True
    max_detail_pages: int = 20

    # 过滤参数
    relevance_threshold: float = 2.5
    max_age_days: Optional[int] = 45

    # 冷启动：首次运行是否只建立基线不推送（强烈建议保持 true）
    cold_start_baseline: bool = True

    # 日志保留天数
    log_retention_days: int = 30


class AppConfig(BaseModel):
    subscriptions: SubscriptionsConfig = Field(default_factory=SubscriptionsConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    crawler: CrawlerConfig = Field(default_factory=CrawlerConfig)

    def active_channels(self) -> List[str]:
        """列出当前配置下真正可用的推送通道"""
        channels = []
        for name, cfg in [
            ("邮件", self.notifications.email),
            ("PushPlus微信", self.notifications.pushplus),
            ("飞书", self.notifications.feishu),
            ("企业微信", self.notifications.wecom),
            ("Server酱", self.notifications.serverchan),
        ]:
            ok, _ = cfg.is_usable()
            if ok:
                channels.append(name)
        return channels

    def describe_channel_problems(self) -> List[str]:
        """列出所有已启用但配置不完整的通道问题"""
        problems = []
        for name, cfg in [
            ("邮件", self.notifications.email),
            ("PushPlus微信", self.notifications.pushplus),
            ("飞书", self.notifications.feishu),
            ("企业微信", self.notifications.wecom),
            ("Server酱", self.notifications.serverchan),
        ]:
            ok, reason = cfg.is_usable()
            if not ok and getattr(cfg, "enabled", False):
                problems.append(f"{name}: {reason}")
        return problems


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def load_yaml(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _apply_env_overrides(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    环境变量覆写（GitHub Actions Secrets 场景）。
    注意：只要提供了 SMTP 三件套就自动把 email.enabled 置为 true，
    避免用户在 Actions 里配好了 Secrets 却因为 yaml 里 enabled=false 而收不到邮件。
    """
    notifications = raw.setdefault("notifications", {})
    email = notifications.setdefault("email", {})

    if os.environ.get("SMTP_HOST"):
        email["smtp_host"] = os.environ["SMTP_HOST"]
    if os.environ.get("SMTP_PORT"):
        try:
            email["smtp_port"] = int(os.environ["SMTP_PORT"])
        except ValueError:
            pass
    if os.environ.get("SMTP_USERNAME"):
        email["username"] = os.environ["SMTP_USERNAME"]
    if os.environ.get("SMTP_PASSWORD"):
        email["password"] = os.environ["SMTP_PASSWORD"]
    if os.environ.get("TO_EMAIL"):
        email["to_addrs"] = [x.strip() for x in os.environ["TO_EMAIL"].split(",") if x.strip()]

    if email.get("username") and email.get("password") and email.get("to_addrs"):
        email["enabled"] = True

    if os.environ.get("PUSHPLUS_TOKEN"):
        pp = notifications.setdefault("pushplus", {})
        pp["token"] = os.environ["PUSHPLUS_TOKEN"]
        pp["enabled"] = True

    if os.environ.get("SERVERCHAN_KEY"):
        sc = notifications.setdefault("serverchan", {})
        sc["send_key"] = os.environ["SERVERCHAN_KEY"]
        sc["enabled"] = True

    if os.environ.get("FEISHU_WEBHOOK"):
        fs = notifications.setdefault("feishu", {})
        fs["webhook_url"] = os.environ["FEISHU_WEBHOOK"]
        fs["enabled"] = True

    if os.environ.get("WECOM_WEBHOOK"):
        wc = notifications.setdefault("wecom", {})
        wc["webhook_url"] = os.environ["WECOM_WEBHOOK"]
        wc["enabled"] = True

    if os.environ.get("SUBSCRIBE_CITIES"):
        cities = [c.strip() for c in os.environ["SUBSCRIBE_CITIES"].split(",") if c.strip()]
        if cities:
            raw.setdefault("subscriptions", {})["cities"] = [{"name": c} for c in cities]

    if os.environ.get("HTTP_PROXY_URL"):
        raw.setdefault("crawler", {})["proxy"] = os.environ["HTTP_PROXY_URL"]

    return raw


def load_config(config_path: str = "config/config.yaml") -> AppConfig:
    """
    加载配置：config.yaml > config.example.yaml > 内置默认值，
    再套用环境变量覆写，最后做结构校验。
    """
    raw = load_yaml(config_path)
    if not raw:
        fallback = os.path.join(os.path.dirname(config_path) or ".", "config.example.yaml")
        raw = load_yaml(fallback)
        if raw:
            logger.warning(f"未找到 {config_path}，已回退使用 {fallback}（建议复制一份改名为 config.yaml）")

    raw = _apply_env_overrides(raw or {})

    try:
        return AppConfig(**raw)
    except Exception as e:
        logger.error(f"配置文件格式有误: {e}")
        logger.error("将使用内置默认配置继续运行，请修正配置文件后重试。")
        return AppConfig()


def load_rules(rules_path: str = "config/rules.yaml") -> List[Dict[str, Any]]:
    data = load_yaml(rules_path)
    rules = data.get("rules", []) if isinstance(data, dict) else []
    return [r for r in rules if isinstance(r, dict)]


def save_yaml(path: str, data: Dict[str, Any]):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
