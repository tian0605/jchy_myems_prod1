import json
import re

import falcon
import mysql.connector

import config
from core.useractivity import admin_control, get_request_context_value


REPORT_TYPES = ('ghg_emissions', 'product_carbon_footprint')
REPORT_MODES = ('real_data', 'estimate_draft')
TEMPLATE_STATUSES = ('draft', 'published', 'archived')
ALLOWED_TEMPLATE_VARIABLES = frozenset(('report_context', 'data_context'))
TEMPLATE_VARIABLE_PATTERN = re.compile(r'{{\s*([a-z][a-z0-9_]*)\s*}}')
MAX_TEMPLATE_PROMPT_LENGTH = 60000


def template_variables(prompt_content):
    return sorted(set(TEMPLATE_VARIABLE_PATTERN.findall(prompt_content)))


def validate_prompt_content(prompt_content):
    if not isinstance(prompt_content, str) or not prompt_content.strip() or \
            len(prompt_content) > MAX_TEMPLATE_PROMPT_LENGTH:
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_AI_REPORT_TEMPLATE_PROMPT')
    unknown_variables = set(template_variables(prompt_content)).difference(ALLOWED_TEMPLATE_VARIABLES)
    if unknown_variables:
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_AI_REPORT_TEMPLATE_VARIABLE')
    return prompt_content.strip()


def render_prompt(prompt_content, values):
    prompt_content = validate_prompt_content(prompt_content)

    def replace_variable(match):
        variable_name = match.group(1)
        value = values.get(variable_name)
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    return TEMPLATE_VARIABLE_PATTERN.sub(replace_variable, prompt_content)


def get_published_template(report_type, report_mode):
    if report_type not in REPORT_TYPES or report_mode not in REPORT_MODES:
        raise ValueError('Invalid report template selection')
    cnx = mysql.connector.connect(**config.myems_system_db)
    cursor = cnx.cursor()
    try:
        cursor.execute(
            " SELECT templates.id, templates.name, versions.id, versions.version_number, versions.prompt_content "
            " FROM tbl_ai_report_templates AS templates "
            " JOIN tbl_ai_report_template_versions AS versions "
            "   ON versions.id = templates.current_published_version_id "
            " WHERE templates.report_type = %s AND templates.report_mode = %s "
            "   AND templates.status = 'published' AND templates.is_deleted = 0 "
            "   AND versions.status = 'published' ",
            (report_type, report_mode))
        row = cursor.fetchone()
        if row is None:
            raise falcon.HTTPServiceUnavailable(title='API.SERVICE_UNAVAILABLE',
                                                description='API.AI_REPORT_TEMPLATE_IS_NOT_PUBLISHED')
        return {
            'id': row[0],
            'name': row[1],
            'version_id': row[2],
            'version_number': row[3],
            'prompt_content': row[4],
        }
    finally:
        cursor.close()
        cnx.close()


def _read_json(req):
    try:
        return json.loads(req.stream.read().decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_JSON')


def _validate_template_data(values, require_identity=False):
    if not isinstance(values, dict):
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_AI_REPORT_TEMPLATE')
    result = {'prompt_content': validate_prompt_content(values.get('prompt_content'))}
    if require_identity:
        for field in ('name', 'report_type', 'report_mode'):
            value = values.get(field)
            if not isinstance(value, str) or not value.strip():
                raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                       description='API.INVALID_AI_REPORT_TEMPLATE')
            result[field] = value.strip()
        if result['report_type'] not in REPORT_TYPES or result['report_mode'] not in REPORT_MODES:
            raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                   description='API.INVALID_AI_REPORT_TEMPLATE')
    return result


def _template_payload(row):
    return {
        'id': row[0], 'name': row[1], 'report_type': row[2], 'report_mode': row[3], 'status': row[4],
        'current_published_version_id': row[5], 'current_published_version_number': row[6],
        'created_datetime_utc': row[7].isoformat(), 'updated_datetime_utc': row[8].isoformat(),
    }


def _version_payload(row, include_prompt=False):
    payload = {
        'id': row[0], 'version_number': row[1], 'variables': json.loads(row[2]), 'status': row[3],
        'created_by_user_id': row[4], 'created_datetime_utc': row[5].isoformat(),
    }
    if include_prompt:
        payload['prompt_content'] = row[6]
    return payload


def _template_row(cursor, template_id):
    cursor.execute(" SELECT templates.id, templates.name, templates.report_type, templates.report_mode, templates.status, "
                   "        templates.current_published_version_id, versions.version_number, "
                   "        templates.created_datetime_utc, templates.updated_datetime_utc "
                   " FROM tbl_ai_report_templates AS templates "
                   " LEFT JOIN tbl_ai_report_template_versions AS versions "
                   "   ON versions.id = templates.current_published_version_id "
                   " WHERE templates.id = %s AND templates.is_deleted = 0 ", (template_id,))
    row = cursor.fetchone()
    if row is None:
        raise falcon.HTTPNotFound(title='API.NOT_FOUND', description='API.AI_REPORT_TEMPLATE_NOT_FOUND')
    return row


class AIReportTemplateCollection:
    @staticmethod
    def on_options(req, resp):
        resp.status = falcon.HTTP_200

    @staticmethod
    def on_get(req, resp):
        admin_control(req)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(" SELECT templates.id, templates.name, templates.report_type, templates.report_mode, templates.status, "
                           "        templates.current_published_version_id, versions.version_number, "
                           "        templates.created_datetime_utc, templates.updated_datetime_utc "
                           " FROM tbl_ai_report_templates AS templates "
                           " LEFT JOIN tbl_ai_report_template_versions AS versions "
                           "   ON versions.id = templates.current_published_version_id "
                           " WHERE templates.is_deleted = 0 ORDER BY templates.report_type, templates.report_mode, templates.id ")
            resp.text = json.dumps([_template_payload(row) for row in cursor.fetchall()])
        finally:
            cursor.close()
            cnx.close()

    @staticmethod
    def on_post(req, resp):
        admin_control(req)
        user_id = get_request_context_value(req, 'user_id')
        values = _validate_template_data(_read_json(req).get('data'), require_identity=True)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            cursor.execute(" SELECT id FROM tbl_ai_report_templates "
                           " WHERE report_type = %s AND report_mode = %s AND is_deleted = 0 ",
                           (values['report_type'], values['report_mode']))
            if cursor.fetchone() is not None:
                raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                       description='API.AI_REPORT_TEMPLATE_ALREADY_EXISTS')
            cursor.execute(" INSERT INTO tbl_ai_report_templates (name, report_type, report_mode) "
                           " VALUES (%s, %s, %s) ",
                           (values['name'], values['report_type'], values['report_mode']))
            template_id = cursor.lastrowid
            cursor.execute(" INSERT INTO tbl_ai_report_template_versions "
                           "     (template_id, version_number, prompt_content, variables_json, created_by_user_id) "
                           " VALUES (%s, 1, %s, %s, %s) ",
                           (template_id, values['prompt_content'], json.dumps(template_variables(values['prompt_content'])),
                            user_id))
            version_id = cursor.lastrowid
            cnx.commit()
            resp.status = falcon.HTTP_201
            resp.location = '/ai-report-templates/' + str(template_id)
            resp.text = json.dumps({'id': template_id, 'version_id': version_id})
        finally:
            cursor.close()
            cnx.close()


class AIReportTemplateItem:
    @staticmethod
    def on_options(req, resp, id_):
        resp.status = falcon.HTTP_200

    @staticmethod
    def on_get(req, resp, id_):
        admin_control(req)
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            payload = _template_payload(_template_row(cursor, id_))
            cursor.execute(" SELECT id, version_number, variables_json, status, created_by_user_id, "
                           "        created_datetime_utc, prompt_content "
                           " FROM tbl_ai_report_template_versions WHERE template_id = %s "
                           " ORDER BY version_number DESC ", (id_,))
            payload['versions'] = [_version_payload(row, include_prompt=True) for row in cursor.fetchall()]
            resp.text = json.dumps(payload)
        finally:
            cursor.close()
            cnx.close()

    @staticmethod
    def on_put(req, resp, id_):
        admin_control(req)
        user_id = get_request_context_value(req, 'user_id')
        values = _validate_template_data(_read_json(req).get('data'))
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            _template_row(cursor, id_)
            cursor.execute(" SELECT COALESCE(MAX(version_number), 0) + 1 "
                           " FROM tbl_ai_report_template_versions WHERE template_id = %s ", (id_,))
            version_number = cursor.fetchone()[0]
            cursor.execute(" INSERT INTO tbl_ai_report_template_versions "
                           "     (template_id, version_number, prompt_content, variables_json, created_by_user_id) "
                           " VALUES (%s, %s, %s, %s, %s) ",
                           (id_, version_number, values['prompt_content'],
                            json.dumps(template_variables(values['prompt_content'])), user_id))
            version_id = cursor.lastrowid
            cnx.commit()
            resp.status = falcon.HTTP_201
            resp.text = json.dumps({'id': int(id_), 'version_id': version_id,
                                    'version_number': version_number})
        finally:
            cursor.close()
            cnx.close()


class AIReportTemplatePreview:
    @staticmethod
    def on_post(req, resp, id_):
        admin_control(req)
        values = _validate_template_data(_read_json(req).get('data'))
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            _template_row(cursor, id_)
            resp.text = json.dumps({
                'prompt_content': render_prompt(values['prompt_content'], {
                    'report_context': {'period_start': '2026-01-01', 'period_end': '2026-12-31', 'space_id': 1},
                    'data_context': {'schema_version': 1, 'scope': 'sample', 'metrics': [], 'sources': []},
                }),
                'variables': template_variables(values['prompt_content']),
            })
        finally:
            cursor.close()
            cnx.close()


class AIReportTemplatePublish:
    @staticmethod
    def on_put(req, resp, id_):
        admin_control(req)
        data = _read_json(req).get('data')
        if not isinstance(data, dict) or not isinstance(data.get('version_id'), int):
            raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                   description='API.INVALID_AI_REPORT_TEMPLATE_VERSION')
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        try:
            _template_row(cursor, id_)
            cursor.execute(" SELECT id, prompt_content FROM tbl_ai_report_template_versions "
                           " WHERE id = %s AND template_id = %s AND status = 'draft' ",
                           (data['version_id'], id_))
            version = cursor.fetchone()
            if version is None:
                raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                       description='API.AI_REPORT_TEMPLATE_DRAFT_NOT_FOUND')
            validate_prompt_content(version[1])
            cursor.execute(" UPDATE tbl_ai_report_template_versions SET status = 'archived' "
                           " WHERE template_id = %s AND status = 'published' ", (id_,))
            cursor.execute(" UPDATE tbl_ai_report_template_versions SET status = 'published' WHERE id = %s ",
                           (data['version_id'],))
            cursor.execute(" UPDATE tbl_ai_report_templates "
                           " SET current_published_version_id = %s, status = 'published' WHERE id = %s ",
                           (data['version_id'], id_))
            cnx.commit()
            resp.status = falcon.HTTP_200
        finally:
            cursor.close()
            cnx.close()