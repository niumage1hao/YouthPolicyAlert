"""
全系统验收脚本（非 pytest，模拟真实用户的完整使用流程）。

它会：
  1. 起一个假政务网站 + 一个真实的本地 SMTP 服务器
  2. 用真实的 config.yaml 走完 doctor → test-notify → run(冷启动) → run(增量) 全流程
  3. 把真正发出的邮件原文落盘，供人工检查排版与内容
  4. 断言每一步的行为符合预期

运行： python tests/acceptance_run.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import shutil
import asyncio
import tempfile
import threading
import subprocess
from datetime import date, timedelta
from email import message_from_bytes
from email.policy import default as email_default
from http.server import HTTPServer, BaseHTTPRequestHandler

TODAY = date.today()
D0 = TODAY.strftime("%Y-%m-%d")
D1 = (TODAY - timedelta(days=3)).strftime("%Y-%m-%d")
D_OLD = (TODAY - timedelta(days=500)).strftime("%Y-%m-%d")

DETAIL = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div class="TRS_Editor">
<p>为做好我市青年人才安居保障工作，现将有关事项公告如下：</p>
<p>一、房源情况：本批次共计推出人才公寓 860 套，位于高新区。</p>
<p>二、申请条件：具有全日制本科及以上学历，硕士、博士优先；年龄不超过 35 周岁；毕业 3 年内。</p>
<p>三、补贴标准：博士 3000 元/月，硕士 2000 元/月，本科 1200 元/月。</p>
<p>四、申报时间：本批次受理时间为 2026年9月1日 至 2026年9月28日，逾期不再受理。</p>
<p>五、申报方式：请登录“示范市人才安居服务平台”（https://rcaj.demo.gov.cn/apply）提交材料。</p>
</div></body></html>"""

LIST_TPL = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>通知公告</title></head><body>
<div id="nav"><a href="/">首页</a><a href="/a">机构概况</a><a href="/b">互动交流</a></div>
<ul class="news-list">
{rows}
  <li><a href="/n/noise.html">2026年物业管理服务项目公开招标中标结果公告</a><span class="date">%s</span></li>
  <li><a href="/n/hire.html">关于公开招聘事业单位工作人员拟聘用人员公示</a><span class="date">%s</span></li>
  <li><a href="/n/old.html">2019年第一批人才公寓配租公告</a><span class="date">%s</span></li>
</ul></body></html>""" % (D1, D1, D_OLD)

ROW = '  <li><a href="/n/{s}.html">{t}</a><span class="date">{d}</span></li>'

BATCH1 = [("a1", "示范市2026年第三批青年人才公寓配租公告", D0),
          ("a2", "关于开展2026年高校毕业生租房补贴申报工作的通知", D1)]
BATCH2 = BATCH1 + [("a3", "示范市第八批保障性租赁住房认租公告", D0)]


class Gov(BaseHTTPRequestHandler):
    listing = ""

    def do_GET(self):
        path = self.path.split("?")[0]
        body = (DETAIL if path.startswith("/n/") else Gov.listing).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def build(entries):
    return LIST_TPL.format(rows="\n".join(ROW.format(s=s, t=t, d=d) for s, t, d in entries))


# --- 真实 SMTP 服务器 ---------------------------------------------------------
class MailSink:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope.content)
        return "250 Message accepted for delivery"


def start_smtp(sink, port):
    from aiosmtpd.controller import Controller
    controller = Controller(sink, hostname="127.0.0.1", port=port)
    controller.start()
    return controller


def run_cli(workdir, *args):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=workdir, capture_output=True, text=True, timeout=300, env=env,
    )
    return proc


def section(title):
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def check(label, condition, detail=""):
    status = "✅ PASS" if condition else "❌ FAIL"
    print(f"  {status}  {label}")
    if detail:
        print(f"           {detail}")
    if not condition:
        check.failures += 1
    return condition


check.failures = 0


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workdir = tempfile.mkdtemp(prefix="ypa_acceptance_")

    # 复制项目到临时工作目录，避免污染源码目录
    for item in ("main.py", "core", "notifiers", "collectors", "config", "static"):
        src = os.path.join(root, item)
        dst = os.path.join(workdir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    os.makedirs(os.path.join(workdir, "data"), exist_ok=True)
    outdir = os.path.join(root, "acceptance_output")
    os.makedirs(outdir, exist_ok=True)

    # 起假政务网站
    Gov.listing = build(BATCH1)
    gov = HTTPServer(("127.0.0.1", 0), Gov)
    threading.Thread(target=gov.serve_forever, daemon=True).start()
    gov_url = f"http://127.0.0.1:{gov.server_port}"

    # 起真实 SMTP
    sink = MailSink()
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); smtp_port = s.getsockname()[1]; s.close()
    smtp = start_smtp(sink, smtp_port)

    # 写入测试用配置与规则
    with open(os.path.join(workdir, "config", "rules.yaml"), "w", encoding="utf-8") as f:
        f.write(f"""rules:
  - id: demo_housing
    city: 示范市
    district: 全市
    source_name: 示范市住房保障局
    category: housing
    url: {gov_url}/list
    parser_type: html
    selectors:
      list_item: ul.news-list li
      title: a
      link: a
      date: span.date
""")

    with open(os.path.join(workdir, "config", "config.yaml"), "w", encoding="utf-8") as f:
        f.write(f"""subscriptions:
  cities:
    - name: "示范市"
notifications:
  email:
    enabled: true
    smtp_host: "127.0.0.1"
    smtp_port: {smtp_port}
    smtp_ssl: false
    username: "monitor@demo.local"
    password: "dummy"
    to_addrs: ["me@demo.local"]
  max_items_per_push: 30
crawler:
  min_delay: 0
  max_delay: 0
  timeout: 10
  max_retries: 1
  max_workers: 2
  enable_warmup: false
  db_path: "data/policy.db"
  fetch_detail: true
  max_detail_pages: 10
  relevance_threshold: 2.5
  max_age_days: 45
  cold_start_baseline: true
""")

    try:
        # ------------------------------------------------------------------
        section("步骤 1 / 5：doctor 体检")
        p = run_cli(workdir, "doctor")
        out = p.stdout
        print(out[-1500:] if len(out) > 1500 else out)
        check("doctor 正常退出", p.returncode == 0)
        check("识别出可用推送通道", "邮件" in out)
        check("数据源连通性检测通过", "1/1 个数据源工作正常" in out or "✅ [示范市]" in out)

        # ------------------------------------------------------------------
        section("步骤 2 / 5：test-notify 验证推送通道")
        before = len(sink.messages)
        p = run_cli(workdir, "test-notify")
        check("test-notify 正常退出", p.returncode == 0, p.stdout.strip()[-200:])
        check("真实收到测试邮件", len(sink.messages) > before,
              f"邮件数 {before} → {len(sink.messages)}")

        # ------------------------------------------------------------------
        section("步骤 3 / 5：首次 run（冷启动基线）")
        before = len(sink.messages)
        p = run_cli(workdir, "run")
        out = p.stdout + p.stderr
        check("run 正常退出", p.returncode == 0)
        check("进入基线模式", "基线模式" in out)
        check("基线建立完成", "基线建立完成" in out)

        new_mails = sink.messages[before:]
        check("冷启动只发 1 封确认信，不刷屏", len(new_mails) == 1,
              f"实际发出 {len(new_mails)} 封")
        if new_mails:
            msg = message_from_bytes(new_mails[0], policy=email_default)
            subject = str(msg["Subject"])
            check("确认信标题正确", "监控已启动" in subject, subject)
            body = msg.get_body(preferencelist=("plain",)).get_content()
            check("确认信说明了基线条数", "历史基线" in body)

        # ------------------------------------------------------------------
        section("步骤 4 / 5：第二次 run（官网无变化）")
        before = len(sink.messages)
        p = run_cli(workdir, "run")
        out = p.stdout + p.stderr
        check("run 正常退出", p.returncode == 0)
        check("未发现新政策", "本轮未发现新政策" in out or "没有待推送" in out)
        check("没有骚扰用户", len(sink.messages) == before,
              f"意外发出 {len(sink.messages) - before} 封邮件")

        # ------------------------------------------------------------------
        section("步骤 5 / 5：第三次 run（官网新增一条政策）")
        Gov.listing = build(BATCH2)
        before = len(sink.messages)
        p = run_cli(workdir, "run")
        out = p.stdout + p.stderr
        check("run 正常退出", p.returncode == 0)
        check("成功推送", "成功推送" in out)

        new_mails = sink.messages[before:]
        check("恰好发出 1 封政策提醒", len(new_mails) == 1, f"实际 {len(new_mails)} 封")

        if new_mails:
            msg = message_from_bytes(new_mails[0], policy=email_default)
            subject = str(msg["Subject"])
            plain = msg.get_body(preferencelist=("plain",)).get_content()
            html = msg.get_body(preferencelist=("html",)).get_content()

            with open(os.path.join(outdir, "policy_email.html"), "w", encoding="utf-8") as f:
                f.write(html)
            with open(os.path.join(outdir, "policy_email.txt"), "w", encoding="utf-8") as f:
                f.write(f"Subject: {subject}\n\n{plain}")

            print(f"\n  📧 邮件主题: {subject}\n")
            print("  --- 纯文本正文 ---")
            for line in plain.split("\n")[:22]:
                print(f"  | {line}")

            check("只包含新增的那一条政策", "第八批" in plain)
            check("不包含已推送过的旧政策", "第三批" not in plain)
            check("不包含招标噪音", "招标" not in plain)
            check("不包含招聘噪音", "拟聘用" not in plain)
            check("不包含过期公告", "2019年" not in plain)

            # ★ 核心：干货字段真的被提取出来了 ★
            check("提取到申报期限", "2026-09" in plain, "期限行: " + next((l for l in plain.split('\n') if '期限' in l), '未找到'))
            check("提取到额度/房源", ("860" in plain or "3000" in plain))
            check("提取到学历门槛", "本科" in plain)
            check("提取到年龄要求", "35周岁" in plain)
            check("HTML 版含申报按钮", "立即申报" in html)

        # ------------------------------------------------------------------
        section("步骤 6 / 6：stats 统计")
        p = run_cli(workdir, "stats")
        print(p.stdout)
        check("stats 正常退出", p.returncode == 0)
        check("统计显示已推送记录", re.search(r"已推送:\s*[1-9]", p.stdout) is not None)

    finally:
        gov.shutdown()
        smtp.stop()
        shutil.rmtree(workdir, ignore_errors=True)

    section("验收结论")
    if check.failures == 0:
        print("  🎉 全部验收项通过！系统可以交付。")
        print(f"  📂 实际发出的邮件已保存到: {outdir}/")
        return 0
    print(f"  ❌ 有 {check.failures} 项未通过，需要修复。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
