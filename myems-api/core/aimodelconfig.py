import re

import falcon
import mysql.connector
import simplejson as json
from cryptography.fernet import Fernet, InvalidToken
from decouple import config as environment_config

import config
from core.useractivity import admin_control


ENVIRONMENT_VARIABLE_PATTERN = re.compile(r'^[A-Z][A-Z0-9_]*$')
ENCRYPTION_KEY_ENVIRONMENT_VARIABLE = 'AI_MODEL_CONFIG_ENCRYPTION_KEY'


def _get_fernet():
    encryption_key = environment_config(ENCRYPTION_KEY_ENVIRONMENT_VARIABLE, default='')
    if not encryption_key:
        raise falcon.HTTPServiceUnavailable(title='API.SERVICE_UNAVAILABLE',
                                            description='API.AI_MODEL_CONFIG_ENCRYPTION_KEY_IS_NOT_CONFIGURED')
    try:
        return Fernet(encryption_key.encode('utf-8'))
    except (TypeError, ValueError):
        raise falcon.HTTPServiceUnavailable(title='API.SERVICE_UNAVAILABLE',
                                            description='API.INVALID_AI_MODEL_CONFIG_ENCRYPTION_KEY')


def encrypt_api_key(api_key):
    return _get_fernet().encrypt(api_key.encode('utf-8')).decode('utf-8')


def get_api_key_value(environment_variable_name, api_key_ciphertext=None):
    if api_key_ciphertext:
        try:
            return _get_fernet().decrypt(api_key_ciphertext.encode('utf-8')).decode('utf-8')
        except (InvalidToken, UnicodeDecodeError):
            raise falcon.HTTPServiceUnavailable(title='API.SERVICE_UNAVAILABLE',
                                                description='API.INVALID_ENCRYPTED_AI_API_KEY')
    return environment_config(environment_variable_name, default='') if environment_variable_name else ''


def _read_json(req):
    try:
        return json.loads(req.stream.read().decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise falcon.HTTPError(status=falcon.HTTP_400,
                               title='API.BAD_REQUEST',
                               description='API.INVALID_JSON')


def _validate_values(values):
    if not isinstance(values, dict):
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST', description='API.INVALID_DATA')

    required_text_fields = ('name', 'provider', 'base_url', 'model_name')
    result = dict()
    for field in required_text_fields:
        value = values.get(field)
        if not isinstance(value, str) or not value.strip():
            raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                   description='API.INVALID_AI_MODEL_CONFIG')
        result[field] = value.strip()
    if result['provider'] not in ('openai_compatible', 'ark_responses'):
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.UNSUPPORTED_AI_PROVIDER')
    if not result['base_url'].startswith(('http://', 'https://')):
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_AI_BASE_URL')
    api_key_env_name = values.get('api_key_env_name', '').strip() if isinstance(values.get('api_key_env_name', ''), str) else ''
    if api_key_env_name and not ENVIRONMENT_VARIABLE_PATTERN.match(api_key_env_name):
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_AI_API_KEY_ENV_NAME')
    result['api_key_env_name'] = api_key_env_name or None
    api_key = values.get('api_key', '')
    if not isinstance(api_key, str):
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_AI_API_KEY')
    result['api_key'] = api_key.strip()
    if not isinstance(values.get('is_enabled'), bool) or not isinstance(values.get('is_default'), bool):
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_IS_ENABLED')
    timeout_seconds = values.get('timeout_seconds')
    if not isinstance(timeout_seconds, int) or timeout_seconds < 1 or timeout_seconds > 600:
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_AI_TIMEOUT_SECONDS')
    result['is_enabled'] = values['is_enabled']
    result['is_default'] = values['is_default']
    result['timeout_seconds'] = timeout_seconds
    return result


def _row_to_dict(row):
    return {
        'id': row[0], 'name': row[1], 'provider': row[2], 'base_url': row[3],
        'model_name': row[4], 'api_key_env_name': row[5], 'is_api_key_encrypted': bool(row[6]),
        'is_enabled': bool(row[7]), 'is_default': bool(row[8]), 'timeout_seconds': row[9],
        'is_api_key_configured': bool(row[6]) or bool(get_api_key_value(row[5])),
        'created_datetime_utc': row[10].isoformat(), 'updated_datetime_utc': row[11].isoformat(),
    }


def _row_to_runtime_dict(row):
    model_config = _row_to_dict(row)
    model_config['api_key'] = get_api_key_value(row[5], row[6])
    return model_config


def get_enabled_model_configs():
    cnx = mysql.connector.connect(**config.myems_system_db)
    cursor = cnx.cursor()
    try:
        cursor.execute(" SELECT id, name, provider, base_url, model_name, api_key_env_name, api_key_ciphertext, "
                   "        is_enabled, is_default, timeout_seconds, created_datetime_utc, updated_datetime_utc "
                       " FROM tbl_ai_model_configs WHERE is_enabled = 1 ORDER BY is_default DESC, id ")
        return [_row_to_runtime_dict(row) for row in cursor.fetchall()]
    finally:
        cursor.close()
        cnx.close()


def get_default_model_config():
    models = get_enabled_model_configs()
    for model in models:
        if model['is_default']:
            return model
    return models[0] if models else None


class AIModelConfigCollection:
    @staticmethod
    def on_options(req, resp):
        resp.status = falcon.HTTP_200

    @staticmethod
    def on_get(req, resp):
        admin_control(req)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(" SELECT id, name, provider, base_url, model_name, api_key_env_name, api_key_ciphertext, "
                           "        is_enabled, is_default, timeout_seconds, created_datetime_utc, updated_datetime_utc "
                           " FROM tbl_ai_model_configs ORDER BY is_default DESC, id ")
            resp.text = json.dumps([_row_to_dict(row) for row in cursor.fetchall()])
        finally:
            cursor.close()
            cnx.close()

    @staticmethod
    def on_post(req, resp):
        admin_control(req)
        values = _validate_values(_read_json(req).get('data'))
        if not values['api_key'] and not values['api_key_env_name']:
            raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST', description='API.AI_API_KEY_IS_REQUIRED')
        api_key_ciphertext = encrypt_api_key(values['api_key']) if values['api_key'] else None
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(' SELECT id FROM tbl_ai_model_configs WHERE name = %s ', (values['name'],))
            if cursor.fetchone() is not None:
                raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                       description='API.AI_MODEL_CONFIG_NAME_IS_ALREADY_IN_USE')
            if values['is_default']:
                cursor.execute(' UPDATE tbl_ai_model_configs SET is_default = 0 ')
            cursor.execute(" INSERT INTO tbl_ai_model_configs "
                           "     (name, provider, base_url, model_name, api_key_env_name, api_key_ciphertext, is_enabled, is_default, timeout_seconds) "
                           " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ",
                           (values['name'], values['provider'], values['base_url'], values['model_name'],
                            values['api_key_env_name'], api_key_ciphertext, values['is_enabled'], values['is_default'],
                            values['timeout_seconds']))
            model_id = cursor.lastrowid
            cnx.commit()
            resp.status = falcon.HTTP_201
            resp.location = '/ai-model-configs/' + str(model_id)
        finally:
            cursor.close()
            cnx.close()


class AIModelConfigItem:
    @staticmethod
    def on_options(req, resp, id_):
        resp.status = falcon.HTTP_200

    @staticmethod
    def on_get(req, resp, id_):
        admin_control(req)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(" SELECT id, name, provider, base_url, model_name, api_key_env_name, api_key_ciphertext, "
                           "        is_enabled, is_default, timeout_seconds, created_datetime_utc, updated_datetime_utc "
                           " FROM tbl_ai_model_configs WHERE id = %s ", (id_,))
            row = cursor.fetchone()
            if row is None:
                raise falcon.HTTPNotFound(title='API.NOT_FOUND', description='API.AI_MODEL_CONFIG_NOT_FOUND')
            resp.text = json.dumps(_row_to_dict(row))
        finally:
            cursor.close()
            cnx.close()

    @staticmethod
    def on_put(req, resp, id_):
        admin_control(req)
        values = _validate_values(_read_json(req).get('data'))
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(' SELECT id, api_key_ciphertext FROM tbl_ai_model_configs WHERE id = %s ', (id_,))
            existing_model = cursor.fetchone()
            if existing_model is None:
                raise falcon.HTTPNotFound(title='API.NOT_FOUND', description='API.AI_MODEL_CONFIG_NOT_FOUND')
            cursor.execute(' SELECT id FROM tbl_ai_model_configs WHERE name = %s AND id != %s ',
                           (values['name'], id_))
            if cursor.fetchone() is not None:
                raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                       description='API.AI_MODEL_CONFIG_NAME_IS_ALREADY_IN_USE')
            if values['is_default']:
                cursor.execute(' UPDATE tbl_ai_model_configs SET is_default = 0 WHERE id != %s ', (id_,))
            api_key_ciphertext = encrypt_api_key(values['api_key']) if values['api_key'] else existing_model[1]
            if not api_key_ciphertext and not values['api_key_env_name']:
                raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST', description='API.AI_API_KEY_IS_REQUIRED')
            cursor.execute(" UPDATE tbl_ai_model_configs "
                           " SET name = %s, provider = %s, base_url = %s, model_name = %s, api_key_env_name = %s, "
                           "     api_key_ciphertext = %s, is_enabled = %s, is_default = %s, timeout_seconds = %s WHERE id = %s ",
                           (values['name'], values['provider'], values['base_url'], values['model_name'],
                            values['api_key_env_name'], api_key_ciphertext, values['is_enabled'], values['is_default'],
                            values['timeout_seconds'], id_))
            cnx.commit()
            resp.status = falcon.HTTP_200
        finally:
            cursor.close()
            cnx.close()

    @staticmethod
    def on_delete(req, resp, id_):
        admin_control(req)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(' DELETE FROM tbl_ai_model_configs WHERE id = %s ', (id_,))
            if cursor.rowcount == 0:
                raise falcon.HTTPNotFound(title='API.NOT_FOUND', description='API.AI_MODEL_CONFIG_NOT_FOUND')
            cnx.commit()
            resp.status = falcon.HTTP_204
        finally:
            cursor.close()
            cnx.close()


class AIModelConfigEnabled:
    @staticmethod
    def on_put(req, resp, id_):
        admin_control(req)
        values = _read_json(req).get('data')
        if not isinstance(values, dict) or not isinstance(values.get('is_enabled'), bool):
            raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                   description='API.INVALID_IS_ENABLED')
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(' UPDATE tbl_ai_model_configs SET is_enabled = %s WHERE id = %s ',
                           (values['is_enabled'], id_))
            if cursor.rowcount == 0:
                raise falcon.HTTPNotFound(title='API.NOT_FOUND', description='API.AI_MODEL_CONFIG_NOT_FOUND')
            cnx.commit()
            resp.status = falcon.HTTP_200
        finally:
            cursor.close()
            cnx.close()