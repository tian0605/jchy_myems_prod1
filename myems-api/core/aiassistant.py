import datetime
import json

import falcon
import mysql.connector
import requests

import config
from core.aimodelconfig import get_default_model_config, get_enabled_model_configs
from core.useractivity import access_control, admin_control, get_request_context_value


def _read_json(req):
    try:
        return json.loads(req.stream.read().decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise falcon.HTTPError(status=falcon.HTTP_400,
                               title='API.BAD_REQUEST',
                               description='API.INVALID_JSON')


def _request_context(req):
    access_control(req)
    return (get_request_context_value(req, 'user_id'),
            get_request_context_value(req, 'enterprise_space_id'))


def _conversation_row(cursor, conversation_id, user_id, enterprise_space_id):
    cursor.execute(" SELECT id, title, created_datetime_utc, updated_datetime_utc "
                   " FROM tbl_ai_assistant_conversations "
                   " WHERE id = %s AND user_id = %s AND enterprise_space_id = %s "
                   "       AND is_deleted = 0 ",
                   (conversation_id, user_id, enterprise_space_id))
    return cursor.fetchone()


def _event(event_name, data):
    return 'event: {0}\ndata: {1}\n\n'.format(
        event_name,
        json.dumps(data, ensure_ascii=False)
    ).encode('utf-8')


def _provider_settings():
    model_config = get_default_model_config()
    if model_config is not None:
        if not model_config['is_api_key_configured']:
            raise falcon.HTTPServiceUnavailable(title='API.SERVICE_UNAVAILABLE',
                                                description='API.AI_ASSISTANT_API_KEY_IS_NOT_CONFIGURED')
        return {
            'provider': model_config['provider'],
            'base_url': model_config['base_url'],
            'api_key': model_config['api_key'],
            'default_model': model_config['model_name'],
            'timeout_seconds': model_config['timeout_seconds'],
        }
    settings = config.ai_assistant
    if not settings['is_enabled']:
        raise falcon.HTTPServiceUnavailable(title='API.SERVICE_UNAVAILABLE',
                                            description='API.AI_ASSISTANT_IS_DISABLED')
    if settings['provider'] != 'openai_compatible' or not settings['base_url'] or \
            not settings['api_key'] or not settings['default_model']:
        raise falcon.HTTPServiceUnavailable(title='API.SERVICE_UNAVAILABLE',
                                            description='API.AI_ASSISTANT_IS_NOT_CONFIGURED')
    return settings


def _system_data_message(enterprise_space_id, authorized_space_ids):
    if not authorized_space_ids:
        return None

    placeholders = ', '.join(['%s'] * len(authorized_space_ids))
    cnx = mysql.connector.connect(**config.myems_system_db)
    cursor = cnx.cursor()
    try:
        cursor.execute(' SELECT id, name FROM tbl_spaces WHERE id IN ({0}) ORDER BY name '.format(placeholders),
                       tuple(authorized_space_ids))
        spaces = [{'id': row[0], 'name': row[1]} for row in cursor.fetchall()]
        cursor.execute(' SELECT DISTINCT product_id FROM tbl_spaces_products '
                       ' WHERE space_id IN ({0}) ORDER BY product_id '.format(placeholders),
                       tuple(authorized_space_ids))
        product_ids = [row[0] for row in cursor.fetchall()]
    finally:
        cursor.close()
        cnx.close()

    return {
        'role': 'system',
        'content': (
            'The following MyEMS scope metadata is authoritative and may be used only for this response. '
            'Do not claim energy, carbon, or product-footprint measurements that are absent from this context. '
            'Ask the user for a period and scope before requesting numeric analysis. '
            'Enterprise space id: {0}. Authorized spaces: {1}. Bound product ids: {2}.'
        ).format(enterprise_space_id, json.dumps(spaces, ensure_ascii=False), json.dumps(product_ids)),
    }


def _write_audit(conversation_id, user_id, enterprise_space_id, provider, model_name, status, error_code=None):
    cnx = mysql.connector.connect(**config.myems_system_db)
    cursor = cnx.cursor()
    try:
        cursor.execute(" INSERT INTO tbl_ai_assistant_audits "
                       "     (conversation_id, user_id, enterprise_space_id, provider, model_name, status, error_code) "
                       " VALUES (%s, %s, %s, %s, %s, %s, %s) ",
                       (conversation_id, user_id, enterprise_space_id, provider, model_name, status, error_code))
        cnx.commit()
    finally:
        cursor.close()
        cnx.close()


def _stream_completion(settings, messages):
    if settings['provider'] == 'ark_responses':
        yield from _stream_ark_response(settings, messages)
        return

    response = requests.post(
        settings['base_url'].rstrip('/') + '/chat/completions',
        headers={
            'Authorization': 'Bearer ' + settings['api_key'],
            'Content-Type': 'application/json',
        },
        json={
            'model': settings['default_model'],
            'messages': messages,
            'stream': True,
        },
        stream=True,
        timeout=settings['timeout_seconds'],
    )
    response.raise_for_status()
    for raw_line in response.iter_lines(decode_unicode=False):
        line = raw_line.decode('utf-8') if raw_line else ''
        if not line or not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if payload == '[DONE]':
            break
        try:
            delta = json.loads(payload)['choices'][0]['delta'].get('content', '')
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if delta:
            yield delta


def _stream_ark_response(settings, messages):
    input_messages = []
    for message in messages:
        input_messages.append({
            'role': message['role'],
            'content': [{'type': 'input_text', 'text': message['content']}],
        })

    base_url = settings['base_url'].rstrip('/')
    endpoint = base_url if base_url.endswith('/responses') else base_url + '/responses'
    response = requests.post(
        endpoint,
        headers={
            'Authorization': 'Bearer ' + settings['api_key'],
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
        },
        json={
            'model': settings['default_model'],
            'input': input_messages,
            'stream': True,
        },
        stream=True,
        timeout=settings['timeout_seconds'],
    )
    response.raise_for_status()
    event_name = None
    for raw_line in response.iter_lines(decode_unicode=False):
        line = raw_line.decode('utf-8') if raw_line else ''
        if not line:
            event_name = None
            continue
        if line.startswith('event:'):
            event_name = line[6:].strip()
            continue
        if not line.startswith('data:'):
            continue
        try:
            payload = json.loads(line[5:].strip())
        except ValueError:
            continue
        response_type = payload.get('type', event_name)
        if response_type == 'response.output_text.delta':
            delta = payload.get('delta', '')
            if delta:
                yield delta
        elif response_type in ('response.failed', 'error'):
            raise requests.HTTPError('Ark Responses API returned an error event.')


class AIAssistantConversationCollection:
    @staticmethod
    def on_options(req, resp):
        resp.status = falcon.HTTP_200

    @staticmethod
    def on_get(req, resp):
        user_id, enterprise_space_id = _request_context(req)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(" SELECT id, title, created_datetime_utc, updated_datetime_utc "
                           " FROM tbl_ai_assistant_conversations "
                           " WHERE user_id = %s AND enterprise_space_id = %s AND is_deleted = 0 "
                           " ORDER BY updated_datetime_utc DESC ",
                           (user_id, enterprise_space_id))
            resp.text = json.dumps([
                {'id': row[0], 'title': row[1], 'created_datetime_utc': row[2].isoformat(),
                 'updated_datetime_utc': row[3].isoformat()}
                for row in cursor.fetchall()
            ])
        finally:
            cursor.close()
            cnx.close()

    @staticmethod
    def on_post(req, resp):
        user_id, enterprise_space_id = _request_context(req)
        values = _read_json(req)
        title = str(values.get('title', '')).strip() or 'New conversation'
        title = title[:128]
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(" INSERT INTO tbl_ai_assistant_conversations "
                           "     (user_id, enterprise_space_id, title) VALUES (%s, %s, %s) ",
                           (user_id, enterprise_space_id, title))
            conversation_id = cursor.lastrowid
            cnx.commit()
            resp.status = falcon.HTTP_201
            resp.location = '/ai-assistant/conversations/' + str(conversation_id)
            resp.text = json.dumps({'id': conversation_id, 'title': title})
        finally:
            cursor.close()
            cnx.close()


class AIAssistantConversationItem:
    @staticmethod
    def on_options(req, resp, id_):
        resp.status = falcon.HTTP_200

    @staticmethod
    def on_get(req, resp, id_):
        user_id, enterprise_space_id = _request_context(req)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            conversation = _conversation_row(cursor, id_, user_id, enterprise_space_id)
            if conversation is None:
                raise falcon.HTTPNotFound(title='API.NOT_FOUND', description='API.AI_CONVERSATION_NOT_FOUND')
            cursor.execute(" SELECT id, role, content, model_name, use_system_data, created_datetime_utc "
                           " FROM tbl_ai_assistant_messages WHERE conversation_id = %s "
                           " ORDER BY id ", (id_,))
            messages = [
                {'id': row[0], 'role': row[1], 'content': row[2], 'model_name': row[3],
                 'use_system_data': bool(row[4]), 'created_datetime_utc': row[5].isoformat()}
                for row in cursor.fetchall()
            ]
            resp.text = json.dumps({
                'id': conversation[0], 'title': conversation[1],
                'created_datetime_utc': conversation[2].isoformat(),
                'updated_datetime_utc': conversation[3].isoformat(), 'messages': messages,
            })
        finally:
            cursor.close()
            cnx.close()

    @staticmethod
    def on_delete(req, resp, id_):
        user_id, enterprise_space_id = _request_context(req)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(" UPDATE tbl_ai_assistant_conversations SET is_deleted = 1 "
                           " WHERE id = %s AND user_id = %s AND enterprise_space_id = %s ",
                           (id_, user_id, enterprise_space_id))
            if cursor.rowcount == 0:
                raise falcon.HTTPNotFound(title='API.NOT_FOUND', description='API.AI_CONVERSATION_NOT_FOUND')
            cnx.commit()
            resp.status = falcon.HTTP_204
        finally:
            cursor.close()
            cnx.close()


class AIAssistantChat:
    @staticmethod
    def on_options(req, resp, id_):
        resp.status = falcon.HTTP_200

    @staticmethod
    def on_post(req, resp, id_):
        user_id, enterprise_space_id = _request_context(req)
        authorized_space_ids = get_request_context_value(req, 'authorized_space_ids', [])
        values = _read_json(req)
        content = str(values.get('content', '')).strip()
        if not content or len(content) > config.ai_assistant['max_message_length']:
            raise falcon.HTTPError(status=falcon.HTTP_400,
                                   title='API.BAD_REQUEST',
                                   description='API.INVALID_AI_MESSAGE')
        use_system_data = bool(values.get('use_system_data', False))
        settings = _provider_settings()

        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            if _conversation_row(cursor, id_, user_id, enterprise_space_id) is None:
                raise falcon.HTTPNotFound(title='API.NOT_FOUND', description='API.AI_CONVERSATION_NOT_FOUND')
            cursor.execute(" INSERT INTO tbl_ai_assistant_messages "
                           "     (conversation_id, role, content, use_system_data) VALUES (%s, 'user', %s, %s) ",
                           (id_, content, use_system_data))
            cursor.execute(" UPDATE tbl_ai_assistant_conversations SET updated_datetime_utc = %s "
                           " WHERE id = %s ", (datetime.datetime.utcnow(), id_))
            cursor.execute(" SELECT role, content FROM tbl_ai_assistant_messages "
                           " WHERE conversation_id = %s ORDER BY id ", (id_,))
            messages = [{'role': row[0], 'content': row[1]} for row in cursor.fetchall()]
            if use_system_data:
                system_data_message = _system_data_message(enterprise_space_id, authorized_space_ids)
                if system_data_message is not None:
                    messages.insert(0, system_data_message)
            cnx.commit()
        finally:
            cursor.close()
            cnx.close()

        def stream():
            response_content = ''
            yield _event('meta', {'conversation_id': int(id_), 'model': settings['default_model'],
                                  'use_system_data': use_system_data})
            try:
                for delta in _stream_completion(settings, messages):
                    response_content += delta
                    yield _event('delta', {'content': delta})
                if response_content:
                    connection = mysql.connector.connect(**config.myems_system_db)
                    database_cursor = connection.cursor()
                    try:
                        database_cursor.execute(" INSERT INTO tbl_ai_assistant_messages "
                                                "     (conversation_id, role, content, model_name, use_system_data) "
                                                " VALUES (%s, 'assistant', %s, %s, %s) ",
                                                (id_, response_content, settings['default_model'], use_system_data))
                        database_cursor.execute(" UPDATE tbl_ai_assistant_conversations "
                                                " SET updated_datetime_utc = %s WHERE id = %s ",
                                                (datetime.datetime.utcnow(), id_))
                        connection.commit()
                    finally:
                        database_cursor.close()
                        connection.close()
                _write_audit(id_, user_id, enterprise_space_id, settings['provider'],
                             settings['default_model'], 'succeeded')
                yield _event('done', {'conversation_id': int(id_)})
            except requests.RequestException:
                _write_audit(id_, user_id, enterprise_space_id, settings['provider'],
                             settings['default_model'], 'failed', 'UPSTREAM_PROVIDER_ERROR')
                yield _event('error', {'code': 'UPSTREAM_PROVIDER_ERROR',
                                       'message': 'The configured AI provider is unavailable.'})

        resp.status = falcon.HTTP_200
        resp.content_type = 'text/event-stream'
        resp.set_header('Cache-Control', 'no-cache')
        resp.set_header('X-Accel-Buffering', 'no')
        resp.stream = stream()


class AIAssistantModelCollection:
    @staticmethod
    def on_get(req, resp):
        _request_context(req)
        models = [{'id': model['id'], 'name': model['name'], 'model_name': model['model_name'],
                   'is_default': model['is_default']} for model in get_enabled_model_configs()]
        if not models and config.ai_assistant['is_enabled'] and config.ai_assistant['default_model']:
            models.append({'id': config.ai_assistant['default_model'], 'name': config.ai_assistant['default_model'],
                           'model_name': config.ai_assistant['default_model'], 'is_default': True})
        resp.text = json.dumps(models)


class AIAssistantStatus:
    @staticmethod
    def on_get(req, resp):
        admin_control(req)
        settings = config.ai_assistant
        resp.text = json.dumps({
            'is_enabled': settings['is_enabled'],
            'provider': settings['provider'],
            'default_model': settings['default_model'],
            'is_configured': bool(settings['base_url'] and settings['api_key'] and settings['default_model']),
        })