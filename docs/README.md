# MiroFish 本地版文档

本目录面向本仓库的 Graphiti、Neo4j 和 CPU BGE-M3 本地部署，基线为 `feat/graphiti-local` 的 `bee978d`，文档日期为 2026 年 10 月 26 日。

| 文档 | 适合谁阅读 | 包含内容 |
|---|---|---|
| [操作手册](MiroFish操作手册.md) | 业务用户、人工验收人员和管理员 | 安装配置、局域网访问、网页操作、三轮运行、API、报告、故障及备份恢复 |
| [案例与功能说明](MiroFish案例与功能说明.md) | 需要理解功能、结果和验证依据的用户 | 共享阅读室完整案例、功能行为、来源追溯、A01～A18 验证、局限及复现步骤 |
| [部署与恢复速查](../deploy/graphiti-local/README.md) | 已熟悉部署的管理员 | 命令、服务、配置、取消、重建和备份入口 |

## 从案例开始

上传 [shared-reading-room-seed.txt](examples/shared-reading-room-seed.txt)，把 [shared-reading-room-requirement.txt](examples/shared-reading-room-requirement.txt) 复制到模拟需求。查看 [完整案例报告](examples/shared-reading-room-report.md) 和 [运行结果 JSON](examples/shared-reading-room-result.json)。案例使用虚构材料。

严格三轮验收使用运行页参数 `?maxRounds=3` 或 API；当前网页滑块最低为 10 轮。双平台轮次结束后，先完成需要的 Agent 访谈，再关闭环境，等待记忆确认并生成报告。完整步骤见操作手册。

本机部署目录为 `/home/nathan/DEV/mirofish`，当前局域网入口为 http://192.168.1.229:3000 。地址随实际部署网络变化。新实验会产生新 ID 和不同生成结果，不要求复制已有案例的 36 条动作或 62 条关系。
