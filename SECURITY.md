# 安全说明

感谢帮助改进 YouthPolicyAlert 的安全性。

## 报告安全问题

请不要在公开 Issue、Pull Request 或日志中提交密码、SMTP 授权码、PushPlus Token、Server酱 SendKey、群机器人 Webhook 或完整的运行数据库。请使用 GitHub 的私密 Security Advisory 联系维护者；如果仓库尚未启用该功能，请先通过私下渠道联系维护者，并提供复现步骤、受影响版本和最小必要日志。

如果凭据曾经进入 Git 历史，即使后来删除文件，也应立即在对应服务商后台撤销并重新生成凭据。

## 本地使用安全边界

- `config/config.yaml` 只应存在于本地，或由部署平台的 Secret/环境变量生成；不要复制为公开文件。
- `data/policy.db`、日志和 `acceptance_output/` 可能包含本地运行历史，默认不应发布。
- `python web.py` 启动的控制台默认只监听 `127.0.0.1:8000`，且没有用户认证。不要把它直接暴露到公网、反向代理到公共地址，或在不可信网络中使用。
- 控制台会对配置接口中的密码、Token、SendKey 和 Webhook 返回掩码；更新配置时会保留已保存的凭据。仍应把控制台视为本地管理界面，而不是多用户服务。
- GitHub Actions 中请使用 Repository Secrets/Variables，并检查工作流日志，避免用 `echo` 打印环境变量。

## 开源发布前检查

```bash
git status --short
git diff -- config/config.yaml data/ acceptance_output/
```

确认没有真实配置、数据库、日志、备份压缩包或包含凭据的截图后，再提交和推送代码。
