import json

from core.aichatquery import CHAT_INSTRUCTION, encode_context


def context_summary(snapshot):
    """Persist scope, aggregates and provenance, not another copy of all business rows."""
    excluded = {'daily', 'activities', 'trends', 'items'}

    def compact(value):
        if isinstance(value, dict):
            return {key: compact(item) for key, item in value.items() if key not in excluded}
        if isinstance(value, list):
            return [compact(item) for item in value]
        return value

    return json.loads(encode_context(compact(snapshot)))


def save_context(cursor, message_id, snapshot):
    cursor.execute('INSERT INTO tbl_ai_chat_contexts (message_id, context_json) VALUES (%s, %s)',
                   (message_id, encode_context(context_summary(snapshot))))


def previous_scope(cursor, conversation_id):
    cursor.execute('SELECT m.use_system_data, c.context_json FROM tbl_ai_assistant_messages m '
                   'LEFT JOIN tbl_ai_chat_contexts c ON c.message_id=m.id '
                   'WHERE m.conversation_id=%s ORDER BY m.id DESC LIMIT 1', (conversation_id,))
    row = cursor.fetchone()
    if not row or not row[0] or not row[1]:
        return {}
    return json.loads(row[1]).get('scope', {})


def context_is_visible(snapshot, enterprise_space_id, capabilities, catalog, web_routes=None):
    scope = snapshot.get('scope', {})
    field = snapshot.get('field')
    options = snapshot.get('options', [])
    allowed = {
        'space_id': {s['id'] for s in catalog['spaces']},
        'product_id': {p['id'] for p in catalog['products'] if p['space_id'] == scope.get('space_id')},
        'meter_key': {m['key'] for m in catalog['meters'] if scope.get('space_id') in m['space_ids']},
    }
    if field in allowed and any(option['value'] not in allowed[field] for option in options):
        return False
    if not scope:
        return True
    if 'ai_analysis' not in capabilities or scope.get('enterprise_space_id') != enterprise_space_id:
        return False
    sid = scope.get('space_id')
    if sid is not None and sid not in {space['id'] for space in catalog['spaces']}:
        return False
    if sid is None and (scope.get('product_ids') or scope.get('meter_key')):
        return False
    bound = {p['id'] for p in catalog['products'] if p['space_id'] == sid}
    if not set(scope.get('product_ids', [])).issubset(bound):
        return False
    if scope.get('meter_key') and not any(
            meter['key'] == scope['meter_key'] and sid in meter['space_ids'] for meter in catalog['meters']):
        return False
    if 'ai_cost_analysis' not in capabilities and any(
            section['type'] in ('market', 'cost') and section.get('data')
            for section in snapshot.get('sections', [])):
        return False
    if web_routes is not None and any(
            section.get('data') and section.get('required_routes')
            and not set(section['required_routes']).intersection(web_routes)
            for section in snapshot.get('sections', [])):
        return False
    return True


def chat_messages(content, snapshot=None, attachment_message=None):
    # Historical answers can contain revoked scopes or stale figures. Only structured
    # scope is inherited; this turn's numbers must come from this turn's authorized query.
    instruction = CHAT_INSTRUCTION
    if snapshot is None:
        instruction += '本轮未启用系统数据查询，仅依据本轮用户材料回答，不声称已经查询数据库。'
    messages = [{'role': 'system', 'content': instruction}]
    if snapshot is not None:
        messages.append({'role': 'system', 'content': '本轮授权数据快照：\n' + encode_context(snapshot)})
    if attachment_message:
        messages.append(attachment_message)
    messages.append({'role': 'user', 'content': content})
    return messages
