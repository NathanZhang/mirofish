"""Read original action records with stable provenance for report generation."""
import json
from collections import Counter
from pathlib import Path

from ...config import Config


def read_run_evidence(simulation_id, limit=40):
    root = Path(Config.OASIS_SIMULATION_DATA_DIR) / simulation_id
    counts, samples = Counter(), []
    for platform in ['twitter', 'reddit']:
        path = root / platform / 'actions.jsonl'
        if not path.exists():
            continue
        with path.open(encoding='utf-8') as f:
            while True:
                offset = f.tell()
                line = f.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                row = json.loads(line)
                if 'event_type' in row:
                    continue
                action = row.get('action_type', 'unknown')
                origin = (row.get('source_record') or {}).get('origin', 'initial_configuration' if row.get('round') == 0 else 'agent_action')
                counts[f'{platform}:{origin}:{action}'] += 1
                if len(samples) < limit and row.get('success', True) and action in {'CREATE_POST','CREATE_COMMENT','QUOTE_POST','REPOST'}:
                    samples.append({'event_id':f'{simulation_id}:{platform}:actions.jsonl:{offset}',
                                    'platform':platform,'source':str(path),'offset':offset,'origin':origin,**row})
    receipt = root / 'memory-receipt.json'
    return {'run_id':simulation_id,'counts':dict(counts),'sample_limit':limit,'samples':samples,
            'memory_receipt':json.loads(receipt.read_text()) if receipt.exists() else None,
            'limitations':'动作日志记录的是本次模拟行为，不能推断真实社区的民意比例。样本按文件顺序读取。'}


def bind_report_provenance(report):
    from . import is_local_graph
    from .protocol import GraphBusy
    from ...models.project import ProjectManager
    if not is_local_graph(report.graph_id):
        return
    evidence = read_run_evidence(report.simulation_id)
    receipt = evidence['memory_receipt']
    if not receipt or receipt.get('graph_id') != report.graph_id:
        raise GraphBusy('报告缺少对应图谱的已确认运行水位')
    report.run_id = receipt['run_id']
    report.memory_watermark = receipt['committed_watermark']
    for project in ProjectManager.list_projects(limit=None):
        if report.graph_id in project.graph_versions:
            report.build_revision = project.graph_versions[report.graph_id]['build_revision']
            break
    if report.status.value != 'completed' or '\n## 数据来源与模拟局限\n' in report.markdown_content:
        return
    root = Path(Config.OASIS_SIMULATION_DATA_DIR) / report.simulation_id
    state = json.loads((root/'state.json').read_text())
    lines = ['\n\n## 数据来源与模拟局限\n',
             f"运行 `{report.run_id}`；图谱 `{report.graph_id}`；版本 `{report.build_revision}`。",
             f"参与者 {state.get('profiles_count',0)} 位；记忆完成水位 {receipt['committed_watermark']}/{receipt['expected_watermark']}。\n",
             '| 平台 | 初始配置动作 | Agent 自主动作 | 总记录 |', '|---|---:|---:|---:|']
    for platform in ['twitter','reddit']:
        initial = sum(v for k,v in evidence['counts'].items() if k.startswith(platform+':initial_configuration:'))
        actions = sum(v for k,v in evidence['counts'].items() if k.startswith(platform+':agent_action:'))
        lines.append(f'| {platform} | {initial} | {actions} | {initial+actions} |')
    lines += ['\n第 0 轮来自预先生成的配置帖，不能当作自主形成的意见；后续轮次为 Agent 实际动作。原始记录分别保存在本运行的 `twitter/actions.jsonl` 与 `reddit/actions.jsonl`，每条记录带有对应平台数据库的 `trace_rowid`。',
              '\n本次为虚构材料驱动的小样本、三轮模拟，自动人设、初始帖与生成模型会影响讨论。未做多次随机重复或真实人群校准；文中的趋势分析不能视为真实民意比例、统计置信度或外部事件事实。\n',
              '可核对的自主发言来源：\n']
    samples = [row for row in evidence['samples'] if row['origin']=='agent_action'][:6]
    for row in samples:
        trace = (row.get('source_record') or {}).get('trace_rowid')
        lines.append(f"- `{row['event_id']}`：{row['agent_name']}，{row['action_type']}，数据库 trace #{trace}。")
    report.markdown_content += '\n'.join(lines)+'\n'
