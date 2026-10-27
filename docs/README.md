# MiroFish 本地版文档

本目录面向本仓库的 Graphiti、Neo4j 和 CPU BGE-M3 本地部署。技术说明基线为 `6aecba7`，操作与案例基线为 `bee978d`，以上文档日期为 2026 年 10 月 26 日；应用场景分析基于 `30cc3fa`，日期为 2026 年 10 月 27 日。

| 文档 | 适合谁阅读 | 包含内容 |
|---|---|---|
| [操作手册](MiroFish操作手册.md) | 业务用户、人工验收人员和管理员 | 安装配置、局域网访问、网页操作、三轮运行、API、报告、故障及备份恢复 |
| [案例与功能说明](MiroFish案例与功能说明.md) | 需要理解功能、结果和验证依据的用户 | 共享阅读室完整案例、功能行为、来源追溯、A01～A18 验证、局限及复现步骤 |
| [技术架构与主流程说明](MiroFish技术架构与主流程说明.md) | 开发、维护和技术评审人员 | 组件和部署架构、数据模型、时序、状态机、完成屏障、幂等和恢复边界 |
| [应用场景分析报告](MiroFish应用场景分析报告.md) | 应用选型、业务试点和产品规划人员 | 八类场景适配、优先级、输入产物、验证方法、技术补齐及研究依据 |
| [部署与恢复速查](../deploy/graphiti-local/README.md) | 已熟悉部署的管理员 | 命令、服务、配置、取消、重建和备份入口 |

## 从案例开始

上传 [shared-reading-room-seed.txt](examples/shared-reading-room-seed.txt)，把 [shared-reading-room-requirement.txt](examples/shared-reading-room-requirement.txt) 复制到模拟需求。查看 [完整案例报告](examples/shared-reading-room-report.md) 和 [运行结果 JSON](examples/shared-reading-room-result.json)。案例使用虚构材料。

严格三轮验收使用运行页参数 `?maxRounds=3` 或 API；当前网页滑块最低为 10 轮。双平台轮次结束后，先完成需要的 Agent 访谈，再关闭环境，等待记忆确认并生成报告。完整步骤见操作手册。

本机部署目录为 `/home/nathan/DEV/mirofish`，当前局域网入口为 http://192.168.1.229:3000 。地址随实际部署网络变化。新实验会产生新 ID 和不同生成结果，不要求复制已有案例的 36 条动作或 62 条关系。
