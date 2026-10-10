import os
import uuid
from datetime import datetime
from functools import wraps
import falcon
import mysql.connector
import simplejson as json
import config

try:
    from gunicorn.http.body import Body
except ImportError:
    class Body:
        def __init__(self, file_obj):
            self.stream = file_obj

        def read(self, *args, **kwargs):
            return self.stream.read(*args, **kwargs)

        def readline(self, *args, **kwargs):
            return self.stream.readline(*args, **kwargs)

        def readlines(self, *args, **kwargs):
            return self.stream.readlines(*args, **kwargs)

        def __iter__(self):
            return iter(self.stream)

        def __getattr__(self, item):
            return getattr(self.stream, item)


VALID_MENU_TEMPLATE_TYPES = {'admin', 'web', 'hybrid'}
AI_CAPABILITIES = {'ai_analysis', 'ai_cost_analysis', 'ai_attachment_upload'}
DEFAULT_AI_CAPABILITIES = {'ai_analysis', 'ai_attachment_upload'}


def _set_request_context_value(req, key, value):
    try:
        req.context[key] = value
    except (TypeError, AttributeError, KeyError):
        setattr(req.context, key, value)


def get_request_context_value(req, key, default=None):
    try:
        return req.context.get(key, default)
    except (AttributeError, TypeError):
        return getattr(req.context, key, default)


def _filter_privilege_data(raw_privilege_data):
    if raw_privilege_data is None:
        return None

    try:
        privilege_data = json.loads(raw_privilege_data)
    except (TypeError, ValueError):
        return None

    if not isinstance(privilege_data, dict):
        return None

    filtered_data = dict()
    if 'menu_routes' in privilege_data:
        filtered_data['menu_routes'] = privilege_data['menu_routes']
    if 'menus' in privilege_data:
        filtered_data['menus'] = privilege_data['menus']
    spaces = privilege_data.get('spaces')
    if isinstance(spaces, list):
        filtered_data['spaces'] = [space_id for space_id in spaces
                                   if isinstance(space_id, int) and space_id > 0]
    ai_capabilities = privilege_data.get('ai_capabilities')
    if isinstance(ai_capabilities, list):
        filtered_data['ai_capabilities'] = [capability for capability in ai_capabilities
                                            if capability in AI_CAPABILITIES]

    return filtered_data


def _filter_menu_template_data(raw_menu_template_data):
    if raw_menu_template_data is None:
        return None

    try:
        menu_template_data = json.loads(raw_menu_template_data)
    except (TypeError, ValueError):
        return None

    if not isinstance(menu_template_data, dict):
        return None

    filtered_data = dict()
    template_type = menu_template_data.get('template_type')
    if isinstance(template_type, str) and len(str.strip(template_type)) > 0:
        normalized_template_type = str.strip(template_type).lower()
        filtered_data['template_type'] = normalized_template_type if normalized_template_type in VALID_MENU_TEMPLATE_TYPES else 'admin'
    else:
        filtered_data['template_type'] = 'admin'

    admin_routes = menu_template_data.get('admin_routes')
    if isinstance(admin_routes, list):
        filtered_data['admin_routes'] = [route for route in admin_routes
                                         if isinstance(route, str) and len(str.strip(route)) > 0]
    else:
        filtered_data['admin_routes'] = list()

    web_routes = menu_template_data.get('web_routes')
    if isinstance(web_routes, list):
        filtered_data['web_routes'] = [route for route in web_routes
                                       if isinstance(route, str) and len(str.strip(route)) > 0]
    else:
        filtered_data['web_routes'] = list()

    return filtered_data


def _get_legacy_enterprise_space_id(raw_privilege_data):
    if raw_privilege_data is None:
        return None

    try:
        privilege_data = json.loads(raw_privilege_data)
    except (TypeError, ValueError):
        return None

    if not isinstance(privilege_data, dict):
        return None

    legacy_spaces = privilege_data.get('spaces')
    if isinstance(legacy_spaces, list) and len(legacy_spaces) > 0 and isinstance(legacy_spaces[0], int):
        return legacy_spaces[0]

    return None


def _get_all_space_rows():
    cnx = None
    cursor = None
    try:
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        cursor.execute(" SELECT id, parent_space_id "
                       " FROM tbl_spaces "
                       " ORDER BY id ")
        return cursor.fetchall()
    finally:
        if cursor:
            cursor.close()
        if cnx:
            cnx.close()


def _build_space_tree(rows):
    children_by_parent = dict()
    valid_space_ids = set()
    for row in rows:
        space_id = row[0]
        parent_space_id = row[1]
        valid_space_ids.add(space_id)
        children_by_parent.setdefault(parent_space_id, list()).append(space_id)

    return children_by_parent, valid_space_ids


def _collect_space_subtree(root_space_id, children_by_parent, valid_space_ids):
    if root_space_id not in valid_space_ids:
        raise falcon.HTTPError(status=falcon.HTTP_404,
                               title='API.NOT_FOUND',
                               description='API.ENTERPRISE_SPACE_NOT_FOUND')

    authorized_space_ids = set()
    pending_space_ids = [root_space_id]
    while pending_space_ids:
        current_space_id = pending_space_ids.pop()
        if current_space_id in authorized_space_ids:
            continue
        authorized_space_ids.add(current_space_id)
        pending_space_ids.extend(children_by_parent.get(current_space_id, list()))

    return authorized_space_ids


def _get_privilege_space_ids(privilege):
    if privilege is None:
        return None

    privilege_data = privilege.get('data') or dict()
    spaces = privilege_data.get('spaces')
    if not isinstance(spaces, list):
        return None

    privilege_space_ids = [space_id for space_id in spaces if isinstance(space_id, int) and space_id > 0]
    return privilege_space_ids if privilege_space_ids else None


def _get_ai_capabilities(privilege):
    if privilege is None:
        return sorted(DEFAULT_AI_CAPABILITIES)

    privilege_data = privilege.get('data') or dict()
    ai_capabilities = privilege_data.get('ai_capabilities')
    if not isinstance(ai_capabilities, list):
        return sorted(DEFAULT_AI_CAPABILITIES)
    return [capability for capability in ai_capabilities if capability in AI_CAPABILITIES]


def _get_authorized_space_ids(is_admin, enterprise_space_id, privilege=None):
    rows = _get_all_space_rows()
    if rows is None or len(rows) == 0:
        return list()

    children_by_parent, valid_space_ids = _build_space_tree(rows)

    if is_admin and enterprise_space_id is None:
        return sorted(valid_space_ids)

    if enterprise_space_id is None:
        return list()

    enterprise_space_ids = _collect_space_subtree(enterprise_space_id, children_by_parent, valid_space_ids)
    privilege_space_ids = _get_privilege_space_ids(privilege)
    if not privilege_space_ids:
        return sorted(enterprise_space_ids)

    privilege_authorized_space_ids = set()
    for privilege_space_id in privilege_space_ids:
        if privilege_space_id not in valid_space_ids:
            continue
        privilege_authorized_space_ids.update(
            _collect_space_subtree(privilege_space_id, children_by_parent, valid_space_ids)
        )

    if not privilege_authorized_space_ids:
        return sorted(enterprise_space_ids)

    return sorted(enterprise_space_ids.intersection(privilege_authorized_space_ids))


def _query_legacy_visible_cost_center_ids(cursor, authorized_space_ids):
    if authorized_space_ids is None:
        return None
    if len(authorized_space_ids) == 0:
        return set()

    placeholders = ', '.join(['%s'] * len(authorized_space_ids))
    cursor.execute((" SELECT DISTINCT cost_center_id "
                    " FROM tbl_spaces "
                    f" WHERE id IN ({placeholders}) AND cost_center_id IS NOT NULL "),
                   tuple(authorized_space_ids))
    rows = cursor.fetchall()
    if rows is None:
        return set()
    return {row[0] for row in rows}


def _query_visible_cost_center_ids(cursor, authorized_space_ids, enterprise_space_id):
    legacy_visible_cost_center_ids = _query_legacy_visible_cost_center_ids(cursor, authorized_space_ids)

    if enterprise_space_id is None:
        return legacy_visible_cost_center_ids

    cursor.execute(" SELECT id "
                   " FROM tbl_cost_centers "
                   " WHERE enterprise_space_id = %s ", (enterprise_space_id,))
    rows = cursor.fetchall()
    visible_cost_center_ids = {row[0] for row in rows} if rows is not None else set()

    if legacy_visible_cost_center_ids:
        placeholders = ', '.join(['%s'] * len(legacy_visible_cost_center_ids))
        cursor.execute((" SELECT id "
                        " FROM tbl_cost_centers "
                        " WHERE enterprise_space_id IS NULL "
                        f"   AND id IN ({placeholders}) "),
                       tuple(sorted(legacy_visible_cost_center_ids)))
        rows = cursor.fetchall()
        if rows is not None:
            visible_cost_center_ids.update(row[0] for row in rows)

    return visible_cost_center_ids


def _enforce_report_resource_scope(req):
    if not str(getattr(req, 'path', '') or '').lower().startswith('/reports/'):
        return

    if bool(get_request_context_value(req, 'is_admin')):
        return

    authorized_space_ids = get_request_context_value(req, 'authorized_space_ids')
    enterprise_space_id = get_request_context_value(req, 'enterprise_space_id')

    requested_space_id = req.params.get('spaceid')
    if requested_space_id is not None:
        requested_space_id = str.strip(requested_space_id)
        if requested_space_id.isdigit() and int(requested_space_id) > 0:
            if authorized_space_ids is not None and int(requested_space_id) not in authorized_space_ids:
                raise falcon.HTTPError(status=falcon.HTTP_404,
                                       title='API.NOT_FOUND',
                                       description='API.SPACE_NOT_FOUND')

    requested_offline_meter_id = req.params.get('offlinemeterid')
    if requested_offline_meter_id is None:
        return

    requested_offline_meter_id = str.strip(requested_offline_meter_id)
    if not requested_offline_meter_id.isdigit() or int(requested_offline_meter_id) <= 0:
        return

    cnx = None
    cursor = None
    try:
        cnx = mysql.connector.connect(**config.myems_system_db)
        cursor = cnx.cursor()
        cursor.execute(" SELECT cost_center_id "
                       " FROM tbl_offline_meters "
                       " WHERE id = %s ", (requested_offline_meter_id,))
        row = cursor.fetchone()
        if row is None:
            raise falcon.HTTPError(status=falcon.HTTP_404,
                                   title='API.NOT_FOUND',
                                   description='API.OFFLINE_METER_NOT_FOUND')

        visible_cost_center_ids = _query_visible_cost_center_ids(cursor, authorized_space_ids, enterprise_space_id)
        if visible_cost_center_ids is not None and row[0] not in visible_cost_center_ids:
            raise falcon.HTTPError(status=falcon.HTTP_404,
                                   title='API.NOT_FOUND',
                                   description='API.OFFLINE_METER_NOT_FOUND')
    finally:
        if cursor:
            cursor.close()
        if cnx:
            cnx.close()


def get_user_permission_context(user_uuid, cursor=None):
    cnx = None
    own_cursor = cursor is None
    permission_context = None
    try:
        if own_cursor:
            cnx = mysql.connector.connect(**config.myems_user_db)
            cursor = cnx.cursor()

        cursor.execute(" SELECT id, uuid, is_admin, is_read_only, privilege_id, menu_template_id, enterprise_space_id "
                       " FROM tbl_users "
                       " WHERE uuid = %s ", (user_uuid,))
        row = cursor.fetchone()
        if row is None:
            raise falcon.HTTPError(status=falcon.HTTP_404,
                                   title='API.NOT_FOUND',
                                   description='API.USER_NOT_FOUND')

        is_admin = True if row[2] else False
        is_read_only = (True if row[3] else False) if row[2] else None
        privilege_id = row[4]
        menu_template_id = row[5]
        stored_enterprise_space_id = row[6]
        enterprise_space_id = stored_enterprise_space_id
        privilege = None
        menu_template = None

        if privilege_id is not None:
            cursor.execute(" SELECT id, name, data "
                           " FROM tbl_privileges "
                           " WHERE id = %s ", (privilege_id,))
            row_privilege = cursor.fetchone()

            if row_privilege is None:
                raise falcon.HTTPError(status=falcon.HTTP_400,
                                       title='API.BAD_REQUEST',
                                       description='API.INVALID_PRIVILEGE')

            privilege = {
                'id': row_privilege[0],
                'name': row_privilege[1],
                'data': _filter_privilege_data(row_privilege[2]),
            }

            if enterprise_space_id is None and not is_admin:
                enterprise_space_id = _get_legacy_enterprise_space_id(row_privilege[2])

        if menu_template_id is not None:
            cursor.execute(" SELECT id, name, data "
                           " FROM tbl_menu_templates "
                           " WHERE id = %s ", (menu_template_id,))
            row_menu_template = cursor.fetchone()

            if row_menu_template is None:
                raise falcon.HTTPError(status=falcon.HTTP_400,
                                       title='API.BAD_REQUEST',
                                       description='API.INVALID_MENU_TEMPLATE_ID')

            menu_template = {
                'id': row_menu_template[0],
                'name': row_menu_template[1],
                'data': _filter_menu_template_data(row_menu_template[2]),
            }

        authorized_space_ids = _get_authorized_space_ids(is_admin, enterprise_space_id, privilege)
        ai_capabilities = _get_ai_capabilities(privilege)

        if not is_admin and enterprise_space_id is None:
            raise falcon.HTTPError(status=falcon.HTTP_400,
                                   title='API.BAD_REQUEST',
                                   description='API.INVALID_ENTERPRISE_SPACE_ID')

        permission_context = {
            'user_id': row[0],
            'user_uuid': row[1],
            'is_admin': is_admin,
            'is_read_only': is_read_only,
            'privilege_id': privilege_id,
            'menu_template_id': menu_template_id,
            'enterprise_space_id': enterprise_space_id,
            'authorized_space_ids': authorized_space_ids,
            'ai_capabilities': ai_capabilities,
            'privilege': privilege,
            'menu_template': menu_template,
            'admin_routes': menu_template.get('data', {}).get('admin_routes') if menu_template else None,
            'web_routes': menu_template.get('data', {}).get('web_routes') if menu_template else None,
        }

        return permission_context
    finally:
        if own_cursor and cursor:
            cursor.close()
        if cnx:
            cnx.close()


def set_request_permission_context(req, permission_context):
    _set_request_context_value(req, 'user_id', permission_context.get('user_id'))
    _set_request_context_value(req, 'user_uuid', permission_context.get('user_uuid'))
    _set_request_context_value(req, 'is_admin', permission_context.get('is_admin'))
    _set_request_context_value(req, 'privilege_id', permission_context.get('privilege_id'))
    _set_request_context_value(req, 'menu_template_id', permission_context.get('menu_template_id'))
    _set_request_context_value(req, 'enterprise_space_id', permission_context.get('enterprise_space_id'))
    _set_request_context_value(req, 'authorized_space_ids', permission_context.get('authorized_space_ids'))
    _set_request_context_value(req, 'ai_capabilities', permission_context.get('ai_capabilities'))
    _set_request_context_value(req, 'admin_routes', permission_context.get('admin_routes'))
    _set_request_context_value(req, 'web_routes', permission_context.get('web_routes'))
    _set_request_context_value(req, 'permission_context', permission_context)


def admin_control(req):
    """
    Check administrator privilege in request headers to protect resources from invalid access.

    This function validates that the request contains valid administrator credentials
    by checking the USER-UUID and TOKEN headers against the user database sessions.
    It ensures that only authenticated administrators can access protected resources.

    Args:
        req: HTTP request object containing headers with USER-UUID and TOKEN

    Raises:
        falcon.HTTPError: If invalid credentials or expired session

    Returns:
        None: If validation passes
    """
    # Validate USER-UUID header
    if 'USER-UUID' not in req.headers or \
            not isinstance(req.headers['USER-UUID'], str) or \
            len(str.strip(req.headers['USER-UUID'])) == 0:
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_USER_UUID')
    admin_user_uuid = str.strip(req.headers['USER-UUID'])

    # Validate TOKEN header
    if 'TOKEN' not in req.headers or \
            not isinstance(req.headers['TOKEN'], str) or \
            len(str.strip(req.headers['TOKEN'])) == 0:
        raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                               description='API.INVALID_TOKEN')
    admin_token = str.strip(req.headers['TOKEN'])

    cnx = None
    cursor = None
    try:
        # Check administrator session in user database
        cnx = mysql.connector.connect(**config.myems_user_db)
        try:
            cursor = cnx.cursor()
            query = (" SELECT utc_expires "
                     " FROM tbl_sessions "
                     " WHERE user_uuid = %s AND token = %s")
            cursor.execute(query, (admin_user_uuid, admin_token,))
            row = cursor.fetchone()

            if row is None:
                raise falcon.HTTPError(status=falcon.HTTP_404, title='API.NOT_FOUND',
                                       description='API.ADMINISTRATOR_SESSION_NOT_FOUND')
            else:
                utc_expires = row[0]
                # Check if session has expired
                if datetime.utcnow() > utc_expires:
                    raise falcon.HTTPError(status=falcon.HTTP_400, title='API.BAD_REQUEST',
                                           description='API.ADMINISTRATOR_SESSION_TIMEOUT')
            
            permission_context = get_user_permission_context(admin_user_uuid, cursor)
            set_request_permission_context(req, permission_context)

            if not permission_context['is_admin'] or permission_context['is_read_only']:
                raise falcon.HTTPError(status=falcon.HTTP_400,
                                       title='API.BAD_REQUEST',
                                       description='API.INVALID_PRIVILEGE')
        finally:
            if cursor:
                cursor.close()
    finally:
        if cnx:
            cnx.close()


def access_control(req):
    """
        Check user privilege in request headers to protect resources from invalid access
        :param req: HTTP request
        :return: HTTPError if invalid else None
        """
    if 'USER-UUID' not in req.headers or \
            not isinstance(req.headers['USER-UUID'], str) or \
            len(str.strip(req.headers['USER-UUID'])) == 0:
        raise falcon.HTTPError(status=falcon.HTTP_400,
                               title='API.BAD_REQUEST',
                               description='API.INVALID_USER_UUID')
    user_uuid = str.strip(req.headers['USER-UUID'])

    if 'TOKEN' not in req.headers or \
            not isinstance(req.headers['TOKEN'], str) or \
            len(str.strip(req.headers['TOKEN'])) == 0:
        raise falcon.HTTPError(status=falcon.HTTP_400,
                               title='API.BAD_REQUEST',
                               description='API.INVALID_TOKEN')
    ordinary_token = str.strip(req.headers['TOKEN'])

    cnx = None
    cursor = None
    try:
        # Check user session
        cnx = mysql.connector.connect(**config.myems_user_db)
        try:
            cursor = cnx.cursor()
            query = (" SELECT utc_expires "
                     " FROM tbl_sessions "
                     " WHERE user_uuid = %s AND token = %s")
            cursor.execute(query, (user_uuid, ordinary_token,))
            row = cursor.fetchone()

            if row is None:
                raise falcon.HTTPError(status=falcon.HTTP_404,
                                       title='API.NOT_FOUND',
                                       description='API.USER_SESSION_NOT_FOUND')
            else:
                utc_expires = row[0]
                if datetime.utcnow() > utc_expires:
                    raise falcon.HTTPError(status=falcon.HTTP_400,
                                           title='API.BAD_REQUEST',
                                           description='API.USER_SESSION_TIMEOUT')
            
            permission_context = get_user_permission_context(user_uuid, cursor)
            set_request_permission_context(req, permission_context)
            _enforce_report_resource_scope(req)
        finally:
            if cursor:
                cursor.close()
    finally:
        if cnx:
            cnx.close()


def api_key_control(req):
    """
        Check API privilege in request headers to protect resources from invalid access
        :param req: HTTP request
        :return: HTTPError if invalid else None
    """
    api_key = str.strip(req.headers['API-KEY'])
    
    cnx = None
    cursor = None
    try:
        cnx = mysql.connector.connect(**config.myems_user_db)
        try:
            cursor = cnx.cursor()
            query = (" SELECT expires_datetime_utc "
                     " FROM tbl_api_keys "
                     " WHERE token = %s ")
            cursor.execute(query, (api_key,))
            row = cursor.fetchone()
            
            if row is None:
                raise falcon.HTTPError(status=falcon.HTTP_404,
                                       title='API.NOT_FOUND',
                                       description='API.API_KEY_NOT_FOUND')
            else:
                expires_datetime_utc = row[0]
                if datetime.utcnow() > expires_datetime_utc:
                    raise falcon.HTTPError(status=falcon.HTTP_400,
                                           title='API.BAD_REQUEST',
                                           description='API.API_KEY_HAS_EXPIRED')

            set_request_permission_context(req, {
                'user_id': None,
                'user_uuid': None,
                'is_admin': True,
                'is_read_only': None,
                'privilege_id': None,
                'enterprise_space_id': None,
                'authorized_space_ids': None,
                'privilege': None,
            })
        finally:
            if cursor:
                cursor.close()
    finally:
        if cnx:
            cnx.close()


def write_log(user_uuid, request_method, resource_type, resource_id, request_body):
    """
    :param user_uuid: user_uuid
    :param request_method: 'POST', 'PUT', 'DELETE'
    :param resource_type: class_name
    :param resource_id: int
    :param request_body: json in raw string
    """
    cnx = None
    cursor = None
    try:
        cnx = mysql.connector.connect(**config.myems_user_db)
        cursor = cnx.cursor()
        add_row = (" INSERT INTO tbl_logs "
                   "    (user_uuid, request_datetime_utc, request_method, resource_type, resource_id, request_body) "
                   " VALUES (%s, %s, %s, %s, %s , %s) ")
        cursor.execute(add_row, (user_uuid,
                                 datetime.utcnow(),
                                 request_method,
                                 resource_type,
                                 resource_id if resource_id else None,
                                 request_body if request_body else None,
                                 ))
        cnx.commit()
    except InterfaceError as e:
        print("Failed to connect request")
    except OperationalError as e:      
        print("Failed to SQL operate request")
    except ProgrammingError as e:
        print("Failed to SQL request")
    except DataError as e:
        print("Failed to SQL Data request")
    except Exception as e:
        print('write_log:' + str(e))
    finally:
        if cursor:
            cursor.close()
        if cnx:
            cnx.close()


def user_logger(func):
    """
    Decorator for logging user activities
    :param func: the decorated function
    :return: the decorator
    """
    @wraps(func)
    def logger(*args, **kwargs):
        qualified_name = func.__qualname__
        class_name = qualified_name.split(".")[0]
        func_name = qualified_name.split(".")[1]

        if func_name not in ("on_post", "on_put", "on_delete"):
            # do not log for other HTTP Methods
            func(*args, **kwargs)
            return
        req, resp = args
        headers = req.headers
        if headers is not None and 'USER-UUID' in headers.keys():
            user_uuid = headers['USER-UUID']
        else:
            # todo: deal with requests with NULL user_uuid
            print('user_logger: USER-UUID is NULL')
            # do not log for NULL user_uuid
            func(*args, **kwargs)
            return

        if func_name == "on_post":
            try:
                file_name = str(uuid.uuid4())
                with open(file_name, "wb") as fw:
                    reads = req.stream.read()
                    fw.write(reads)
                raw_json = reads.decode('utf-8')
                with open(file_name, "rb") as fr:
                    req.stream = Body(fr)
                    func(*args, **kwargs)
                    write_log(user_uuid=user_uuid, request_method='POST', resource_type=class_name,
                              resource_id=kwargs.get('id_'), request_body=raw_json)
                os.remove(file_name)
            except OSError as e:
                print("Failed to stream request")
            except UnicodeDecodeError as e:
                print("Failed to decode request")
            except Exception as e:
                if isinstance(e, falcon.HTTPError):
                    raise e
                else:
                    print('user_logger:' + str(e))
            return
        elif func_name == "on_put":
            try:
                file_name = str(uuid.uuid4())

                with open(file_name, "wb") as fw:
                    reads = req.stream.read()
                    fw.write(reads)
                raw_json = reads.decode('utf-8')
                with open(file_name, "rb") as fr:
                    req.stream = Body(fr)
                    func(*args, **kwargs)
                    write_log(user_uuid=user_uuid, request_method='PUT', resource_type=class_name,
                              resource_id=kwargs.get('id_'), request_body=raw_json)
                os.remove(file_name)
            except OSError as e:
                print("Failed to stream request")
            except UnicodeDecodeError as e:
                print("Failed to decode request")
            except Exception as e:
                if isinstance(e, falcon.HTTPError):
                    raise e
                else:
                    print('user_logger:' + str(e))

            return
        elif func_name == "on_delete":
            try:
                func(*args, **kwargs)
                write_log(user_uuid=user_uuid, request_method="DELETE", resource_type=class_name,
                          resource_id=kwargs.get('id_'), request_body=json.dumps(kwargs))
            except (TypeError, ValueError) as e:
                print("Failed to decode JSON")
            except Exception as e:
                if isinstance(e, falcon.HTTPError):
                    raise e
                else:
                    print('user_logger:' + str(e))
            return

    return logger
