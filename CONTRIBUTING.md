# 🤝 贡献指南：3 分钟为你的城市添加政策监控

本项目采用**声明式规则**架构 —— 添加新城市**无需编写任何 Python 代码**，
只需在 `config/rules.yaml` 里填一段配置。

---

## 🔍 第一步：找到官方发布源

在搜索引擎中按以下模式检索你所在城市的官方公告**列表页**：

| 目标 | 搜索关键词 |
|:---|:---|
| 保租房 / 人才公寓 | `[城市名] 住房和城乡建设局 通知公告` 或 `[城市名] 住房保障局` |
| 毕业生补贴 | `[城市名] 人力资源和社会保障局 通知公告` |
| 青年驿站 | `[城市名] 青年驿站` 或 `[城市名] 共青团委` |

**注意两点：**

1. 要找**列表页**（一屏能看到很多条公告标题），不是某一条公告的详情页
2. 认准 `.gov.cn` 官方域名，不要用第三方转载站

---

## 🛠️ 第二步：获取 CSS 选择器

1. 用 Chrome / Edge 打开该公告列表页
2. 按 `F12` 打开开发者工具，点击左上角的元素选择箭头 <kbd>⌖</kbd>
3. 点击第一条公告标题，观察高亮的 HTML 结构

你需要找出三样东西：

```html
<ul class="news_list">          ← 列表容器
  <li>                          ← 每一行公告（list_item 要选中它）
    <a href="/xxx.html">标题</a>  ← 标题与链接（通常都是 a）
    <span class="date">2026-08-20</span>  ← 发布日期
  </li>
</ul>
```

对应写成：

```yaml
selectors:
  list_item: "ul.news_list li"   # 选中每一行
  title: "a"
  link: "a"
  date: "span.date"
```

> 💡 **不确定就多写几个，用逗号分隔**：`"ul.news_list li, div.list li, tr"`
> 系统会依次尝试；即使全都没命中，也会自动降级到**启发式智能提取**兜底。

---

## 📝 第三步：添加规则

打开 `config/rules.yaml`，在末尾追加：

```yaml
  - id: cd_housing              # 唯一标识，建议用 城市拼音缩写_类型
    city: 成都                   # 必须与 config.yaml 里订阅的城市名一致
    district: 全市
    source_name: 成都市住房和城乡建设局 (人才公寓/保租房)
    category: housing           # housing / subsidy / employment / other
    url: http://cdzj.chengdu.gov.cn/cdzj/c132801/list.shtml
    parser_type: html
    verified: false             # 实测通过后改成 true
    selectors:
      list_item: "ul.cdzj_list li, .news_list li"
      title: "a"
      link: "a"
      date: "span.date"
```

**可选字段：**

```yaml
    encoding: gb18030           # 确定是 GBK 老站点时填写（一般可省略，会自动探测）
    max_pages: 3                # 抓取前 3 页
    page_pattern: "index_{page}.html"   # 配合 max_pages 使用
```

---

## 🧪 第四步：测试你的规则

### 方式 A：命令行（推荐）

```bash
python main.py doctor --city 成都
```

输出会明确告诉你三种结果之一：

- `✅ [成都] xxx: 15 条  例:「2026年第三批人才公寓配租公告…」` → 规则有效！
- `⚠️ 网页能打开，但一条都没解析出来` → 选择器不对，回第二步重新找
- `❌ NOT_FOUND / BLOCKED` → URL 错了或被反爬拦截

也可以看看实际会推送什么：

```bash
python main.py run --dry-run --city 成都
```

### 方式 B：可视化规则实验室

```bash
python web.py
```

打开 http://127.0.0.1:8000 → 「🧪 规则实验室」标签页：

1. 输入城市名，系统自动推荐官方候选数据源
2. 填入 URL 与选择器，点「运行在线提取测试」
3. 右侧实时显示提取到的公告，并给出诊断提示
4. 测试通过后点「一键保存至监控规则库」

---

## 📤 第五步：提交 PR

确认规则能稳定抓到数据后：

1. 把 `verified` 改为 `true`，并在规则上方加一行注释说明实测结果
2. 提交 Pull Request，在描述里贴上 `doctor` 的输出截图或文本

我们会尽快合并！🎉

---

## 🧭 编写规则的几条经验

**选栏目比选选择器更重要。**
优先选"通知公告""政策文件""住房保障"这类专栏，而不是网站首页——首页混杂大量新闻，
抓到的东西相关性低，还会给正文提取增加无谓的请求。

**先看这个栏目有没有你要的内容。**
如果 `doctor` 显示"抓到 20 条但 0 条命中青年政策"，说明这个栏目本身就不发布保租房/补贴类公告，
换个专栏，而不是去调低相关性阈值。

**政务网站改版是常态。**
规则失效不是你的错。系统内置了启发式兜底和连续失效告警，
但最可靠的还是社区定期维护——发现失效的规则，欢迎直接提 PR 修正。

**请保持礼貌抓取。**
不要把 `crawler.min_delay` 调到 0，也不要把 `max_workers` 调得过高。
这些是公共服务资源，我们的目标是消除信息差，不是给政务服务器添麻烦。

---

## 🐛 报告问题 / 提交代码

- 发现某个城市规则失效 → 提 Issue，附上 `python main.py doctor --city XX` 的输出
- 想改进过滤逻辑 → 请同时在 `tests/test_pipeline.py` 里补一个测试用例
- 修改核心模块 → 提交前请确保 `python -m pytest tests/ -q` 全部通过

```bash
pip install pytest aiosmtpd
python -m pytest tests/ -q          # 193 个测试，全部离线运行
python tests/acceptance_run.py      # 全系统端到端验收
```
