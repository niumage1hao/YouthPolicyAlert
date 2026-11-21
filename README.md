# policy-state 分支

本分支由 GitHub Actions 自动维护，仅用于持久化 YouthPolicyAlert 的
去重数据库 `data/policy.db`，**请勿手动修改或合并到主分支**。

它记录了"哪些政策已经推送过"，是保证不重复骚扰用户的唯一状态来源。
删除本分支会导致下次运行时重新建立基线（历史政策不会被补推）。
