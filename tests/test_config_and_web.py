"""
测试配置校验与 Web API。

v1 的配置是裸 dict，写错 key 不会报错，只会静默不发邮件；
Web 后台还因为前端调用了不存在的方法导致整个看板加载中断。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shutil
import tempfile

import pytest

from core.config_schema import AppConfig, CrawlerConfig, EmailConfig, load_config, _apply_env_overrides


class TestEmailConfigValidation:
    def test_complete_config_usable(self):
        cfg = EmailConfig(enabled=True, username="a@qq.com", password="code", to_addrs=["b@qq.com"])
        assert cfg.is_usable()[0] is True

    def test_missing_password_reports_reason(self):
        cfg = EmailConfig(enabled=True, username="a@qq.com", to_addrs=["b@qq.com"])
        ok, reason = cfg.is_usable()
        assert ok is False and "授权码" in reason

    def test_missing_recipient_reports_reason(self):
        cfg = EmailConfig(enabled=True, username="a@qq.com", password="x")
        ok, reason = cfg.is_usable()
        assert ok is False and "收件" in reason

    def test_disabled_is_not_usable(self):
        cfg = EmailConfig(enabled=False, username="a@qq.com", password="x", to_addrs=["b@qq.com"])
        assert cfg.is_usable()[0] is False

    def test_example_placeholders_are_not_usable(self):
        cfg = EmailConfig(
            enabled=True,
            username="your_email@qq.com",
            password="your_smtp_auth_code",
            to_addrs=["your_email@qq.com"],
        )
        ok, reason = cfg.is_usable()
        assert ok is False and "占位" in reason

    def test_string_recipient_coerced_to_list(self):
        """用户常把 to_addrs 写成一个字符串，不该因此静默失效"""
        cfg = EmailConfig(to_addrs="a@qq.com, b@qq.com")
        assert cfg.to_addrs == ["a@qq.com", "b@qq.com"]


class TestChannelDiagnostics:
    def test_no_channel_by_default(self):
        assert AppConfig().active_channels() == []

    def test_active_channel_listed(self):
        cfg = AppConfig(notifications={"email": {
            "enabled": True, "username": "a@qq.com", "password": "x", "to_addrs": ["b@qq.com"],
        }})
        assert "邮件" in cfg.active_channels()

    def test_enabled_but_broken_channel_reported(self):
        """已启用但配置不全的通道必须被明确报出来，而不是静默跳过"""
        cfg = AppConfig(notifications={"email": {"enabled": True, "username": "a@qq.com"}})
        problems = cfg.describe_channel_problems()
        assert problems and "邮件" in problems[0]


class TestEnvOverrides:
    def test_smtp_env_enables_email(self, monkeypatch):
        """
        ★ 回归测试 ★
        在 GitHub Actions 里配好了 Secrets，但 yaml 里 enabled=false 时，
        v1 会静默不发邮件。v2 只要三件套齐全就自动启用。
        """
        monkeypatch.setenv("SMTP_USERNAME", "a@qq.com")
        monkeypatch.setenv("SMTP_PASSWORD", "authcode")
        monkeypatch.setenv("TO_EMAIL", "b@qq.com")

        raw = _apply_env_overrides({"notifications": {"email": {"enabled": False}}})
        cfg = AppConfig(**raw)
        assert cfg.notifications.email.enabled is True
        assert cfg.notifications.email.is_usable()[0] is True

    def test_multiple_recipients(self, monkeypatch):
        monkeypatch.setenv("SMTP_USERNAME", "a@qq.com")
        monkeypatch.setenv("SMTP_PASSWORD", "x")
        monkeypatch.setenv("TO_EMAIL", "b@qq.com,c@qq.com")
        cfg = AppConfig(**_apply_env_overrides({}))
        assert cfg.notifications.email.to_addrs == ["b@qq.com", "c@qq.com"]

    def test_pushplus_env(self, monkeypatch):
        monkeypatch.setenv("PUSHPLUS_TOKEN", "tok123")
        cfg = AppConfig(**_apply_env_overrides({}))
        assert cfg.notifications.pushplus.enabled is True

    def test_subscribe_cities_env(self, monkeypatch):
        monkeypatch.setenv("SUBSCRIBE_CITIES", "深圳,杭州,北京")
        cfg = AppConfig(**_apply_env_overrides({}))
        assert cfg.subscriptions.city_names == ["深圳", "杭州", "北京"]


class TestConfigLoading:
    def test_ssl_verification_is_enabled_by_default(self):
        assert CrawlerConfig().verify_ssl is True

    def test_malformed_config_does_not_crash(self):
        """配置文件格式错误时应回退到默认值继续运行，而不是整个程序崩掉"""
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "bad.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("notifications:\n  email:\n    smtp_port: 这不是数字\n")
            cfg = load_config(path)
            assert isinstance(cfg, AppConfig)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_missing_file_falls_back(self):
        assert isinstance(load_config("config/definitely_not_exists.yaml"), AppConfig)

    def test_example_config_is_valid(self):
        """仓库自带的 config.example.yaml 必须是一份合法配置"""
        cfg = load_config("config/config.example.yaml")
        assert isinstance(cfg, AppConfig)
        assert cfg.crawler.cold_start_baseline is True


class TestGovResolver:
    def test_known_city_returns_sources(self):
        from core.gov_resolver import resolve_official_gov_sources
        sources = resolve_official_gov_sources("深圳")
        assert len(sources) >= 2
        assert all(s["url"].startswith("http") for s in sources if s["url"])

    def test_no_cross_city_url_impersonation(self):
        """
        ★ 回归测试 ★
        v1 把深圳住建局的 URL 挂在"海淀区住建委""亦庄管委会"等名下。
        v2 必须保证：每个城市返回的 URL 不能属于另一个城市的域名。
        """
        from core.gov_resolver import CITY_SOURCES
        # 一个城市可能有多个合法官方域名（如上海同时使用 sh.gov.cn 与 shanghai.gov.cn）
        domain_hints = {
            "深圳": ["sz.gov.cn"], "杭州": ["hangzhou.gov.cn"], "北京": ["beijing.gov.cn"],
            "广州": ["gz.gov.cn"], "武汉": ["wuhan.gov.cn"], "成都": ["chengdu.gov.cn", "sczwfw.gov.cn"],
            "南京": ["nanjing.gov.cn"], "郑州": ["zhengzhou.gov.cn"], "厦门": ["xm.gov.cn"],
            "长沙": ["changsha.gov.cn"], "西安": ["xa.gov.cn"],
            "上海": ["sh.gov.cn", "shanghai.gov.cn"],
        }
        for city, sources in CITY_SOURCES.items():
            hints = domain_hints.get(city)
            if not hints:
                continue
            for s in sources:
                if s["url"]:
                    assert any(h in s["url"] for h in hints), \
                        f"{city} 的数据源 {s['source_name']} 用了不属于该市的域名: {s['url']}"

    def test_subsidy_source_is_really_hrss(self):
        """
        ★ 回归测试 ★
        v1 里杭州/武汉/郑州的"人社局"条目 URL 指向的其实是房管局/人才网。
        标称人社局的数据源，域名里必须能看出是人社部门。
        """
        from core.gov_resolver import CITY_SOURCES
        for city, sources in CITY_SOURCES.items():
            for s in sources:
                if s["type"] != "subsidy" or not s["url"]:
                    continue
                assert any(k in s["url"] for k in ("rsj", "hrss", "rlzy", "cdhrss", "xahrss")), \
                    f"{city} 标称人社局的数据源 URL 看起来不是人社部门: {s['url']}"

    def test_unknown_city_returns_placeholder_not_fake(self):
        """未收录城市必须返回空 URL 占位，绝不能塞别的城市的链接冒充"""
        from core.gov_resolver import resolve_official_gov_sources
        sources = resolve_official_gov_sources("某个不存在的县城XYZ")
        assert all(not s["url"] for s in sources)
        assert all("暂未收录" in s["tag"] for s in sources)


class TestWebAPI:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        import web
        return TestClient(web.app)

    def test_stats_endpoint(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        for key in ("total_policies", "pending", "sent", "baseline", "total_rules", "active_channels"):
            assert key in data

    def test_cities_endpoint(self, client):
        r = client.get("/api/cities")
        assert r.status_code == 200 and r.json()[0] == "全部"

    def test_policies_endpoint(self, client):
        r = client.get("/api/policies")
        assert r.status_code == 200 and "data" in r.json()

    def test_health_endpoint(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200 and "broken" in r.json()

    def test_pending_endpoint(self, client):
        r = client.get("/api/pending")
        assert r.status_code == 200 and "data" in r.json()

    def test_rules_endpoint(self, client):
        r = client.get("/api/rules")
        assert r.status_code == 200 and r.json()["total"] > 0

    def test_gov_sources_endpoint(self, client):
        r = client.get("/api/search_gov_sources?query=深圳")
        assert r.status_code == 200 and r.json()["candidates"]

    def test_invalid_rule_rejected(self, client):
        """规则 URL 非法时必须明确报错，而不是写进 rules.yaml 造成静默失效"""
        r = client.post("/api/save_rule", json={
            "url": "not-a-url", "list_item": "ul li", "city": "测试市", "source_name": "测试局",
        })
        assert r.status_code == 400

    def test_invalid_config_rejected(self, client):
        r = client.post("/api/config", json={"crawler": {"max_workers": "很多个"}})
        assert r.status_code == 400

    def test_config_endpoint_redacts_notification_secrets(self, client, monkeypatch, tmp_path):
        """Web 控制台不能把 SMTP/Token/Webhook 等凭据原样返回给浏览器。"""
        import web

        config_path = tmp_path / "config.yaml"
        raw = {
            "subscriptions": {"cities": []},
            "notifications": {
                "email": {
                    "enabled": True,
                    "username": "owner@example.com",
                    "password": "smtp-secret",
                    "to_addrs": ["owner@example.com"],
                },
                "pushplus": {"enabled": True, "token": "pushplus-secret"},
                "serverchan": {"enabled": True, "send_key": "serverchan-secret"},
                "feishu": {"enabled": True, "webhook_url": "https://feishu.example/hook-secret"},
                "wecom": {"enabled": True, "webhook_url": "https://wecom.example/hook-secret"},
            },
            "crawler": {},
        }
        web.save_yaml(str(config_path), raw)
        monkeypatch.setattr(web, "CONFIG_PATH", str(config_path))

        response = client.get("/api/config")
        assert response.status_code == 200
        safe = response.json()
        assert safe["notifications"]["email"]["password"] == "********"
        assert safe["notifications"]["pushplus"]["token"] == "********"
        assert safe["notifications"]["serverchan"]["send_key"] == "********"
        assert safe["notifications"]["feishu"]["webhook_url"] == "********"
        assert safe["notifications"]["wecom"]["webhook_url"] == "********"
        assert "smtp-secret" not in response.text
        assert "pushplus-secret" not in response.text

    def test_config_update_preserves_redacted_notification_secrets(self, client, monkeypatch, tmp_path):
        """前端回传掩码配置时，不能把已有真实凭据覆盖成星号。"""
        import web

        config_path = tmp_path / "config.yaml"
        raw = {
            "notifications": {
                "email": {
                    "enabled": True,
                    "username": "owner@example.com",
                    "password": "smtp-secret",
                    "to_addrs": ["owner@example.com"],
                },
                "pushplus": {"enabled": True, "token": "pushplus-secret"},
            },
            "crawler": {},
        }
        web.save_yaml(str(config_path), raw)
        monkeypatch.setattr(web, "CONFIG_PATH", str(config_path))

        safe = client.get("/api/config").json()
        response = client.post("/api/config", json=safe)
        assert response.status_code == 200

        stored = web.load_yaml(str(config_path))
        assert stored["notifications"]["email"]["password"] == "smtp-secret"
        assert stored["notifications"]["pushplus"]["token"] == "pushplus-secret"

    def test_email_test_restores_saved_password_when_form_contains_mask(self, client, monkeypatch, tmp_path):
        """配置页加载掩码后点击“测试邮件”，仍应使用磁盘中的真实授权码。"""
        import web
        from notifiers.email_notifier import EmailNotifier

        config_path = tmp_path / "config.yaml"
        web.save_yaml(str(config_path), {
            "notifications": {"email": {
                "enabled": True,
                "username": "owner@example.com",
                "password": "smtp-secret",
                "to_addrs": ["owner@example.com"],
            }},
            "crawler": {},
        })
        monkeypatch.setattr(web, "CONFIG_PATH", str(config_path))
        captured = {}

        def fake_send(self, items):
            captured["password"] = self.password
            return True

        monkeypatch.setattr(EmailNotifier, "send", fake_send)
        response = client.post("/api/test_notify", json={
            "channel": "email",
            "email_config": {
                "enabled": True,
                "username": "owner@example.com",
                "password": "********",
                "to_addrs": ["owner@example.com"],
            },
        })
        assert response.status_code == 200
        assert captured["password"] == "smtp-secret"

    def test_index_page_served(self, client):
        r = client.get("/")
        assert r.status_code == 200 and "青年" in r.text
