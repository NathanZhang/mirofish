#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<'HELP'
用法：./start.sh | ./stop.sh | ./restart.sh | ./status.sh
或：  ./scripts/local-stack.sh {start|stop|restart|status}

管理：Neo4j → CPU Embedding → Graphiti → Flask → Vue；停止时按相反顺序。
启动后访问：http://127.0.0.1:3000
局域网访问：在 deploy/graphiti-local/.env 设置 MIROFISH_WEB_HOST=0.0.0.0 后重启。
日志：data/graphiti-local/{embedding,graphiti,backend,frontend}.log
停止保留数据库卷、任务、人设、动作日志和报告。
生成模型使用已配置的现有 LLM 服务，需事先启动。
首次安装和环境配置见 deploy/graphiti-local/README.md。
HELP
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" || "${2:-}" == "--help" || "${2:-}" == "-h" ]]; then
    usage
    exit 0
fi
if [[ $# -ne 1 ]]; then
    usage >&2
    exit 2
fi
case "$1" in
    start|stop|restart|status) ;;
    *) usage >&2; exit 2 ;;
esac

PYTHON="$ROOT/services/graphiti_local/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo '缺少 Graphiti 虚拟环境，请先执行：uv sync --project services/graphiti_local --locked' >&2
    exit 1
fi
if [[ ! -f "$ROOT/deploy/graphiti-local/.env" ]]; then
    echo '缺少 deploy/graphiti-local/.env，请按 deploy/graphiti-local/README.md 配置。' >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo '未找到 Docker，请先安装 Docker 和 Compose 插件并启动 Docker 服务。' >&2
    exit 1
fi

cd -- "$ROOT"
exec "$PYTHON" "$ROOT/deploy/graphiti-local/manage.py" "$1"
