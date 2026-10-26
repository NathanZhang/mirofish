"""Emit only database-confirmed actions and advance the trace cursor once."""


def log_committed_actions(fetch, db_path, last_rowid, agent_names, round_num, logger):
    actions, following = fetch(db_path, last_rowid, agent_names)
    for action in actions:
        if logger:
            logger.log_action(round_num=round_num, agent_id=action['agent_id'],
                agent_name=action['agent_name'], action_type=action['action_type'],
                action_args=action['action_args'], source_record={
                    'database': db_path, 'trace_rowid': action['trace_rowid'],
                    'origin': 'initial_configuration' if round_num == 0 else 'agent_action'})
    return len(actions), following
