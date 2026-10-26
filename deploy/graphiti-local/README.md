# 本地 Graphiti 运行与恢复

本实现使用独立 Graphiti 0.31.2 服务、Neo4j Community 和 CPU BGE-M3，不需要 Zep Cloud 密钥。Flask 不持有异步数据库连接。FalkorDB 尚未启用。Neo4j 镜像固定为已验证的 Linux ARM64 digest；其他架构需另外验证镜像。

面向业务操作的完整步骤见 [操作手册](../../docs/MiroFish操作手册.md)，功能解释、实际结果与验收用例见 [案例与功能说明](../../docs/MiroFish案例与功能说明.md)。

## 安装与配置

在项目根目录执行：

```bash
uv sync --project services/graphiti_local --locked
uv sync --project services/embedding --locked
uv sync --project backend --locked
npm ci --prefix frontend
cp deploy/graphiti-local/env.example deploy/graphiti-local/.env
chmod 600 deploy/graphiti-local/.env
```

填写随机数据库密码、随机服务间 token、实际本机模型凭据。根 `.env` 设置 `GRAPH_BACKEND=graphiti_local`、同一个 `GRAPHITI_SERVICE_TOKEN`、`GRAPHITI_SERVICE_URL=http://127.0.0.1:5101`，并配置 MiroFish 的 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL_NAME`。两个应用使用不同环境配置；服务配置位于 `deploy/graphiti-local/.env`。凭据文件、模型权重、数据库卷和任务库不进 Git。

首次准备权重需要联网：

```bash
services/embedding/.venv/bin/python services/embedding/download_model.py
```

下载器将模型的确切 revision、1024 维、CLS 池化和 L2 归一化写入 `data/graphiti-local/bge-m3/provenance.json`。运行阶段只从本地加载权重。Embedding 在 CPU 上运行；Graphiti 显式使用本机生成和向量端点，关闭遥测，只启用 RRF。`GRAPH_LLM_ENABLE_THINKING=false` 可为支持该参数的本机 vLLM/Qwen 显式关闭思考；其他服务不设置此变量。MiroFish 对应配置为 `LLM_ENABLE_THINKING=false`。

## 启停与检查

```bash
./start.sh     # 启动 Neo4j、Embedding、Graphiti、Flask、Vue，等待全部就绪
./status.sh    # 查看五个服务的运行与就绪状态
./stop.sh      # 按依赖相反顺序停止，保留数据
./restart.sh   # 完整停止并重新启动
```

脚本可从任意工作目录通过绝对路径执行，提供 `--help`。底层入口仍为 `services/graphiti_local/.venv/bin/python deploy/graphiti-local/manage.py {start|stop|restart|status}`。已有共享 LLM 服务需事先启动，脚本使用配置中的生成端点，启停仅覆盖本项目的五个服务。运行前先完成上面的安装与环境配置；脚本不会自动安装依赖或下载权重。

启动顺序为 Neo4j → Embedding → Graphiti 索引与 Worker → Flask → Vue。默认端口分别为 7687/7474、5102、5101、5001、3000；默认均监听本机。管理脚本仅停止自身记录且命令匹配的进程，停止不会删除卷。重复启动复用已经运行且就绪的服务，并发启停会被锁拒绝。手工启动的同端口服务须用原生命周期管理，不能混用两个启动器。日志位于 `data/graphiti-local/{embedding,graphiti,backend,frontend}.log`；Neo4j 日志可通过 `docker compose --env-file deploy/graphiti-local/.env -f deploy/graphiti-local/compose.yaml logs neo4j` 查看。

### 局域网访问

在 `deploy/graphiti-local/.env` 中设置 `MIROFISH_WEB_HOST=0.0.0.0`，再执行 `./restart.sh`。同一局域网的设备通过 `http://<本机局域网IPv4>:3000` 访问首页和报告，接口请求使用该地址下的 `/api` 反向代理。浏览器无需直接访问 5001，也不要将 `VITE_API_BASE_URL` 设置成客户端自己的 `localhost`。其余本地服务继续只监听 loopback。前端固定使用 3000；端口冲突直接失败，不自动换端口。

`ip -4 address` 可查看本机地址。如果已监听 `0.0.0.0:3000` 且本机使用局域网 IP 能访问，但其他设备仍超时，检查主机防火墙的 TCP 3000 入站规则和路由器的客户端隔离设置。`MIROFISH_WEB_HOST=127.0.0.1` 可切回仅本机访问。

`/health` 与 Graphiti `/healthz` 检查存活；Flask `/ready` 和经过 Bearer 认证的 Graphiti `/internal/v1/readyz` 检查数据库、SQLite、实际模型别名及向量维度。就绪检查不执行生成推理。API 协议版本为 `X-Graph-Protocol: 1`，返回请求 ID。模型调用记录写入 `data/graphiti-local/model-calls.jsonl`，不包含凭据。

Neo4j heap 初始 1 GiB、最大 2 GiB、page cache 1 GiB，容器内存上限 4 GiB。初期单摄入 Worker、Graphiti 并发 2。监控可用内存和 swap，不能按机器总内存分配缓存。

## 数据和模拟限制

新项目保存后端、构建版本、摄入 job ID、向量配置指纹。历史项目缺少 `graph_backend` 时明确按 Cloud 读取，不随全局默认切换。旧构建记录保存在 `graph_versions` 中。

首期采用方案允许的“一张图只运行一次模拟”限制。运行图与来源图标识相同，并保存 `run_id`；重跑或再次使用同一图会返回 409，需先重建。原模拟、报告和旧 namespace 保留。此版本没有声称提供不可变种子图克隆。

参与者默认按类型清单及角色后缀筛选，例如 `NoiseSensitiveResident`、`PublicLibrary`。在根 `.env` 显式设置 `GRAPH_SIMULATABLE_TYPES` 后，只使用该清单，不再匹配后缀。平台、地点和活动不应加入此清单。社区摘要为可选项，普通建图明确记录 `optional_not_built`；PoC 单独验证社区摘要能力。未使用的 Cloud Python 本体导出不适用于本地服务，本地仅接收本体 JSON。

OASIS 动作日志由数据库确认后写入，带 `trace_rowid`。初始配置帖标记为 `initial_configuration`，Agent 自主动作标记为 `agent_action`。同内容、不同动作位置不会被合并。动作先写入 SQLite outbox，再以持久化批次边界和操作键交给 Graphiti；批次时间采用持久化的模拟参考时钟，原始日志时间另行保留。

进程结束后先排空 outbox，再等待每项摄入确认。仅当 `expected_watermark == committed_watermark` 时写入 `memory-receipt.json` 并开放报告。报告保存运行、构建版本及完成水位，并直接读取原始动作证据。访谈需要运行中的 OASIS 环境，应在关闭环境前执行；关闭后仍可生成报告和进行报告对话。

## 中断与重建

SQLite 使用 WAL 与 FULL 同步。任务输入、item 状态、确定性 episode UUID、操作键和 payload 哈希先持久化，再执行图写入。相同操作键与输入返回同一个 job，不同输入返回冲突。Graphiti 0.31.2 的 `add_episode(uuid=...)` 读取既有 episode，本实现先保存确定性来源，再调用抽取。

Worker 停止时，未确认的 processing 项成为 `uncertain`。重启不会自动重放该图的后续任务，也不会发布完成状态。确定性 UUID 不等同于跨 SQLite/Neo4j 原子事务。保留原材料、outbox、任务库和 namespace，核对后在新 namespace 重建；不要手动把 uncertain 改成 completed。种子建图可通过 `/api/graph/build` 的 `force=true` 重建，活动租约和未完成任务会阻止此操作。

Flask 重启后的建图轮询直接读取保存的 `ingestion_job_id`。删除/重置同时检查应用消费者及服务端持久化租约；删除仅作用于目标 namespace。遗留租约不按时间自动清除，应先检查相应 PID、原任务和日志，再显式释放。写入未确认时，报告接口返回 409。

内部 `POST /internal/v1/jobs/{job_id}/cancel` 可持久化取消任务。已受理但未处理的任务立即取消；正在写图的任务停在当前 item 边界，期间状态为 `cancelling`，仍禁止删除或重建。部分写入保留，原图不再发布为完成，需在新 namespace 重建。

已停止的模拟可以从原始种子、outbox 的原批次和未分批尾部重建：

```bash
backend/.venv/bin/python scripts/rebuild_graphiti_run.py sim_xxxxxxxxxxxx
# 核对 rebuilds/<新 graph_id>/ 下的结果后，对没有报告的失败运行发布：
backend/.venv/bin/python scripts/rebuild_graphiti_run.py sim_xxxxxxxxxxxx --publish
```

默认只做隔离重建，不改原模拟或报告。工具校验种子指纹、原始动作与 OASIS SQLite trace 的身份和内容、outbox 输入及事件总数；缺失已落库来源标识的旧日志需要人工核对，不能自动冒充已验证数据。每次执行都会新建 namespace；`--publish` 会再执行一次完整重建，仅在全部摄入确认和水位一致后切换引用，并保留旧 namespace、来源和 receipt。有报告的运行拒绝切换。发布后重启 Flask，清除旧失败状态的进程内缓存。本次验收实测默认隔离重建；发布分支未做破坏性切换验收。

## 备份与版本升级

备份前停止新任务，完成或持久化当前工作，依次停止应用、Worker、Embedding、Neo4j。协调备份 `backend/uploads/`、`data/graphiti-local/`、Neo4j 数据卷及受限凭据文件；凭据备份须单独保管，不能放入公开证据包。运行中的 SQLite 可用其 backup API，但不能把它与不一致时间点的图数据库副本混用。

恢复时先使用同一依赖锁、镜像 digest、模型 revision 和配置。先恢复数据卷和任务库，再按启动顺序启动，核对 job 状态、节点/边数量、来源与水位。更换向量模型、维度或归一化方式必须新建 namespace 并重算向量。更新 Graphiti 前在隔离环境跑完整验收，禁止直接跟随 main。

协调备份和隔离恢复验证入口：

```bash
services/graphiti_local/.venv/bin/python deploy/graphiti-local/manage.py stop
services/graphiti_local/.venv/bin/python deploy/graphiti-local/backup.py create --backup /absolute/private/backup --graph-id graph_xxx --expected-nodes 15 --expected-edges 62
services/graphiti_local/.venv/bin/python deploy/graphiti-local/backup.py verify --backup /absolute/private/backup
services/graphiti_local/.venv/bin/python deploy/graphiti-local/manage.py start
```

`create` 要求应用和数据库完全停止，备份包括模型权重，因此需要数 GiB 空间。`verify` 在临时卷和 17687/17474 端口真实恢复 Neo4j，核对目标图、episode、任务及完成水位，然后清理临时容器和卷；不会覆盖原卷。备份目录含受限凭据，勿上传。

## 验证入口

```bash
MIROFISH_ENV_FILE=/dev/null backend/.venv/bin/python -m pytest backend/tests tests -q
uv run --project services/graphiti_local --locked pytest services/graphiti_local/tests -q
services/graphiti_local/.venv/bin/python services/graphiti_local/validate_queries.py
backend/.venv/bin/python scripts/validate_graphiti_local.py build --evidence /absolute/evidence/path
backend/.venv/bin/python scripts/validate_graphiti_local.py prepare --fixed-activity --evidence /absolute/evidence/path
backend/.venv/bin/python scripts/validate_graphiti_local.py run --evidence /absolute/evidence/path
backend/.venv/bin/python scripts/validate_graphiti_local.py report --evidence /absolute/evidence/path
backend/.venv/bin/python scripts/validate_graphiti_local.py chat --evidence /absolute/evidence/path
```

验收脚本默认使用本机评估目录的 `seed.txt`。`--fixed-activity` 在启动前保存原配置，再设置每平台每轮两个真实 Agent，避免 0 点低活跃系数取整后无人行动。它不生成输出动作。查询验证使用独立合成 namespace 并清理，仅报告和截图使用真实业务输出。

2026-10-26 实际验收：8 个参与者、双平台 3 轮，Twitter 13 条和 Reddit 23 条已落库动作，36/36 写回；其中初始配置动作 14 条、自主动作 22 条。报告 `report_605fe858947b` 完成，Agent 访谈及报告对话均收到实际回复。备份在隔离卷恢复后得到 15 节点、62 条关系、6 个 episode、水位 36。证据和 A01–A18 的测试方式、限制见同级工作区 `DEV/artifacts/mirofish-20261024/graphiti-local-acceptance-20261026/验收结果.md`。
