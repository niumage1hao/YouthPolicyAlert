# 📋 v2 升级说明

本文档记录本次重构修复的问题，以及每个修复对应的验证方式。

---

## 🔴 致命缺陷（会直接导致核心承诺失效）

### 1. 推送失败即永久丢失消息

**问题**
v1 的 `filter_and_save_new()` 一边入库一边返回"新政策"给调用方去发邮件。
一旦邮件发送失败（SMTP 抽风、授权码过期、网络波动），这批政策已经写进数据库了，
下一轮 `filter_and_save_new()` 不会再返回它们 —— 用户永远收不到，而且毫无察觉。

**现场证据**
你现有数据库里 7 条政策的 `notified` 字段全部是 `0`（从未推送成功），
它们在 v1 下永远不会再被发出来。

**修复**
把"发现"和"推送"彻底拆开，引入推送状态机：

```
record_discovered()         → 只入库，标记 PENDING
get_pending_notifications() → 取出待推送的（含上轮失败的）
mark_notified()             → 推送成功才置 SENT
mark_notify_failed()        → 失败保持 PENDING，下轮自动重试
```

超过 5 次仍失败才置为 `ABANDONED` 并计入健康报告，避免无限重试。

**验证**
`tests/test_storage.py::test_notification_failure_is_retried_not_lost`
`tests/test_e2e.py::test_push_failure_recovers_next_round`
另：升级后你原有的 7 条政策会自动进入待推送队列被补发。

---

### 2. 冷启动刷屏

**问题**
v1 首次运行时数据库为空，网站上现存的几百条历史公告全被判为"新政策"，
用户第一封邮件就会收到几百条几年前的公告，直接劝退。

**修复**
首次运行进入**基线模式**：全部记录为 `BASELINE` 只存不推，
并发送一封「监控已启动」确认信（顺带证明整条链路是通的）。
之后只推真正的增量。可通过 `crawler.cold_start_baseline` 关闭。

**验证**
`tests/test_storage.py::test_cold_start_does_not_notify_history`（200 条历史公告 → 推送队列为空）
`tests/test_e2e.py::test_cold_start_then_increment`

---

### 3. 去重数据库持久化不可靠

**问题**
v1 用 `actions/cache` 保存 `data/policy.db`，且 cache key 里带了每次都不同的 `run_id`。
GitHub 官方明确声明 Actions Cache **不保证保留**。缓存一旦丢失，
去重记录归零，所有历史政策会被当成新政策再次批量推送 —— 正好违背"绝不重复推送"的核心卖点。

**修复**
改为把数据库提交到独立的 orphan 分支 `policy-state`，force push 覆盖。
免费、可靠、不累积历史、不污染主分支。

**验证**
见 `.github/workflows/monitor.yml`，需你 push 到 GitHub 后手动触发一次确认。

---

## 🟠 严重缺陷

### 4. Web 控制台整个加载失败

`mounted()` 调用了从未定义的 `fetchCities()`，抛出 TypeError 后
`fetchStats` / `fetchPolicies` / `loadConfig` 全部不再执行，看板一片空白。
→ 已补上该方法，并让每个请求独立容错。

### 5. 依赖缺失

`web.py` 依赖 `fastapi` 和 `uvicorn`，但 `requirements.txt` 里没有。
用户按 README 装完依赖后启动后台直接 `ModuleNotFoundError`。
→ 已补全。

### 6. 数据源静默失效

政务网站改版后选择器失效，v1 只把失败写进本地日志表，
用户永远不会主动去查，于是"再也收不到提醒"被误以为"最近没有新政策"。

更隐蔽的是：**网页能打开但一条都没解析出来**，v1 会记为 `SUCCESS`，永远不告警。

→ v2 把"成功但 0 条"也计为失败，连续 3 次即主动发告警邮件（带 24 小时冷却），
并新增 `doctor` 命令与控制台健康大盘。

**验证** `tests/test_storage.py::test_success_with_zero_items_counts_as_failure`

### 7. 编造的数据源

`gov_search.py` 为所有未知城市返回**深圳/杭州的 URL**（代码注释里写着"演示连通测试"）。
`gov_resolver.py` 中多个城市的"人社局"条目 URL 实际指向房管局或人才网。
用户以为在监控本地人社局补贴，实际抓的是另一个城市的另一个部门。

→ 删除 `gov_search.py` 与 `city_catalog.py`；
重写 `gov_resolver.py`，未收录城市返回空 URL 占位引导手填，绝不用假链接冒充。

**验证**
`tests/test_config_and_web.py::test_no_cross_city_url_impersonation`
`tests/test_config_and_web.py::test_subsidy_source_is_really_hrss`

---

## 🟡 质量问题

### 8. 过滤器放行大量噪音

v1 只要标题命中任一白名单词就放行。你库里那条
「北京市人社局所属事业单位招聘退役大学生士兵拟聘用人员公示」
就是因为含"大学生"被放行的，跟安居补贴毫无关系。

→ 改为**加权打分 + 阈值**，并修复了子串重复计分的缺陷
（"高校毕业生"会同时命中"高校毕业生"和"毕业生"，让弱信号叠加越过阈值）。
黑名单补充了招聘/考试/拟聘用等政务噪音。

### 9. 没有时效过滤

v1 不看发布日期，网站列表里躺着的 2019 年公告一样会被当新政策推送。
→ 新增 `max_age_days`（默认 45 天）。无法解析日期的条目放行，靠去重保证只推一次。

### 10. "干货提取"名不副实

v1 的 `enrich_item()` 只从**标题**做正则匹配，而政务公告标题几乎从不包含
"补贴多少钱""什么时候截止""什么学历能申请"。
结果推送卡片上这些字段永远是"详见官方正文"—— 用户还得自己点进去逐条读，
信息差问题并没有真正解决。

→ 新增 `core/extractor.py`，对命中的政策抓取详情页正文，提取：
申报截止日期、补贴金额（支持分学历档位）、房源套数、学历门槛、年龄限制、申报入口、摘要。

**实际效果**（验收脚本真实发出的邮件）：

```
申报期限: 2026-09-01 至 2026-09-28
适合人群: 博士 / 硕士 / 本科 / 毕业3年内
额度/房源: 博士3000元 / 硕士2000元 / 本科1200元 ｜ 房源 860 套
年龄要求: 35周岁以下
```

### 11. 去重键不稳定

v1 直接哈希原始 URL。政务站链接常带 `?t=时间戳`，
同一条公告每次抓到的 URL 都不同 → 被反复当成新政策推送。

→ 新增 URL 归一化（去时间戳/埋点/锚点、统一协议与大小写、折叠 index.html），
并增加"城市+标题"二级去重键，应对官网改版换链接结构的情况。

### 12. 反爬能力弱

v1 遇到 403 就用同样的请求头硬重试 3 次，必然还是 403。

→ 新增会话预热（先访问首页拿 Cookie 再带 Referer 请求内页）、
被拦截时轮换浏览器指纹、按状态码分流（404 直接放弃不浪费重试）。

### 13. 其他

- **串行采集慢** → 线程池并发（同域名仍串行且保持礼貌间隔）
- **编码探测慢且易误判** → 改为 meta charset > HTTP 头 > 采样探测
- **配置写错无提示** → pydantic 结构化校验 + `doctor` 明确报出问题
- **Actions Secrets 配好了却不发邮件** → 三件套齐全时自动启用邮件通道
- **飞书通道误判成功** → 飞书参数错误也返回 HTTP 200，改为检查业务返回码
- **STARTTLS 无条件调用** → 服务器不支持时直接报错，改为按能力协商
- **规则只能加不能删** → 新增删除接口
- **启发式提取会抓到导航栏** → 引入列表评分（日期覆盖率+标题长度分布）挑最佳容器

---

## ✅ 如何验证这次重构

```bash
pip install -r requirements.txt
pip install pytest aiosmtpd

# 193 个自动化测试，默认离线运行
python -m pytest tests/ -v

# 全系统验收：起本地假政务站 + 真实 SMTP，跑完整流程
python tests/acceptance_run.py
```

验收脚本会真实走完 `doctor → test-notify → 冷启动 → 无变化 → 增量推送` 全流程，
断言每一步行为正确，并把实际发出的邮件保存到 `acceptance_output/`。

---

## ⚠️ 仍需你亲自确认的事

代码回归与官方来源实测已完成。2026-08-24 在可联网环境运行 `doctor`，规则库 18/18 个来源均能打开并解析到公告；以下事项仍需部署者确认：

1. **推送通道配置**
   项目不会替用户填写邮箱、授权码或 Webhook。当前示例配置会被识别为占位值，必须复制 `config/config.example.yaml` 为 `config/config.yaml` 并填入真实凭据，再运行 `python main.py doctor`。

2. **GitHub Actions 部署**
   你的项目目录还没有 `.git`，说明从未推送到 GitHub 跑过。
   请 push 后手动触发一次 workflow（可先勾选 `dry_run`），确认云端也能抓到数据。
   注意：GitHub Actions 的 IP 段有可能被政务网站的 WAF 拦截，
   若云端持续 403 而本机正常，则需改用本机定时任务或国内服务器。

---

## 📦 数据迁移

你原有的 `data/policy.db` **会被自动升级**，不会丢数据：

- 自动补齐新增字段并回填归一化 URL 与去重键
- 原有 7 条 `notified=0` 的政策会进入待推送队列，在下次运行时补发出去
- 若不想补发这 7 条，运行前执行：
  ```bash
  sqlite3 data/policy.db "UPDATE policies SET notified=2 WHERE notified=0;"
  ```
  （2 = BASELINE，只记录不推送）

---

## 🗄️ 关于 SQLite 与网络盘 / 同步盘

交付过程中发现一个值得注意的兼容性问题：**SQLite 在部分挂载文件系统上无法工作**
（FUSE 挂载点、网络驱动器、OneDrive/坚果云等同步目录），会直接报 `disk I/O error`。

已做的处理：
- `PolicyStorage` 现在会探测 WAL 是否可用，不可用时自动降级为默认日志模式并给出提示
- 你的 `data/policy.db` 已完成 v2 结构升级（7 条历史记录完整保留）

如果你把项目放在同步盘里并遇到 `disk I/O error`，请把数据库改到本地盘：

```yaml
crawler:
  db_path: "C:/YouthPolicyAlert/policy.db"   # 指向非同步的本地路径
```

在普通 Windows NTFS 分区（如你的 `F:` 盘）上运行不受此影响。
