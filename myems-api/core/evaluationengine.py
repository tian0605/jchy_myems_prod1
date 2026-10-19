from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import falcon
import mysql.connector

import config


VALID_METRIC_CODES = {
    'unit_comprehensive_energy_tce_per_t',
    'unit_carbon_tco2_per_t',
}


def _connect():
    return mysql.connector.connect(**config.myems_production_db)


def _decimal_or_none(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise falcon.HTTPError(status=falcon.HTTP_400,
                               title='API.BAD_REQUEST',
                               description='API.INVALID_EVALUATION_VALUE')


def _date_or_today(value=None):
    if value is None or value == '':
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except ValueError:
        raise falcon.HTTPError(status=falcon.HTTP_400,
                               title='API.BAD_REQUEST',
                               description='API.INVALID_REPORT_DATE')


def _split_advice(advice_text):
    if advice_text is None:
        return []
    return [line.strip() for line in str(advice_text).replace('\r', '').split('\n') if line.strip()]


def _row_to_rule_set(row):
    return {
        'id': row[0],
        'uuid': row[1],
        'enterprise_space_id': row[2],
        'space_id': row[3],
        'product_id': row[4],
        'rule_set_code': row[5],
        'name': row[6],
        'metric_code': row[7],
        'metric_unit': row[8],
        'benchmark_source': row[9],
        'benchmark_value': row[10],
        'benchmark_display_name': row[11],
        'scope_level': row[12],
        'sort_order': row[13],
        'is_active': bool(row[14]),
        'effective_date': row[15],
        'expiry_date': row[16],
        'expression': row[17],
        'remark': row[18],
        'updated_datetime_utc': row[19],
    }


def _row_to_detail(row):
    return {
        'id': row[0],
        'rule_set_id': row[1],
        'display_order': row[2],
        'min_value': row[3],
        'max_value': row[4],
        'min_inclusive': bool(row[5]),
        'max_inclusive': bool(row[6]),
        'comparison_side': row[7],
        'grade_code': row[8],
        'grade_label': row[9],
        'is_compliant': bool(row[10]),
        'status_text': row[11],
        'highlight_style': row[12],
        'evaluation_text': row[13],
        'advice_text': row[14],
        'remark': row[15],
    }


def _scope_candidates(enterprise_space_id, space_id, product_id):
    enterprise_space_id = int(enterprise_space_id) if enterprise_space_id is not None else 0
    return [
        (enterprise_space_id, space_id, product_id, 'enterprise_space_product'),
        (enterprise_space_id, None, product_id, 'enterprise_product'),
        (enterprise_space_id, None, None, 'enterprise_default'),
        (0, None, None, 'platform_default'),
    ]


def _query_rule_set(cursor, enterprise_space_id, space_id, product_id, metric_code, report_date):
    for candidate_enterprise_space_id, candidate_space_id, candidate_product_id, scope_level in \
            _scope_candidates(enterprise_space_id, space_id, product_id):
        cursor.execute(
            " SELECT id, uuid, enterprise_space_id, space_id, product_id, rule_set_code, name, metric_code, "
            "        metric_unit, benchmark_source, benchmark_value, benchmark_display_name, scope_level, sort_order, "
            "        is_active, effective_date, expiry_date, expression, remark, updated_datetime_utc "
            " FROM tbl_evaluation_rule_sets "
            " WHERE enterprise_space_id = %s "
            "   AND metric_code = %s "
            "   AND is_active = 1 "
            "   AND scope_level = %s "
            "   AND ((%s IS NULL AND space_id IS NULL) OR space_id = %s) "
            "   AND ((%s IS NULL AND product_id IS NULL) OR product_id = %s) "
            "   AND (effective_date IS NULL OR effective_date <= %s) "
            "   AND (expiry_date IS NULL OR expiry_date >= %s) "
            " ORDER BY effective_date DESC, sort_order ASC, updated_datetime_utc DESC, id DESC "
            " LIMIT 1 ",
            (candidate_enterprise_space_id, metric_code, scope_level,
             candidate_space_id, candidate_space_id, candidate_product_id, candidate_product_id,
             report_date, report_date)
        )
        row = cursor.fetchone()
        if row is not None:
            return _row_to_rule_set(row)
    return None


def _detail_matches(detail, actual_value):
    min_value = detail['min_value']
    max_value = detail['max_value']
    if min_value is not None:
        if detail['min_inclusive']:
            if actual_value < min_value:
                return False
        elif actual_value <= min_value:
            return False
    if max_value is not None:
        if detail['max_inclusive']:
            if actual_value > max_value:
                return False
        elif actual_value >= max_value:
            return False
    return True


def _query_matching_detail(cursor, rule_set_id, actual_value):
    cursor.execute(
        " SELECT id, rule_set_id, display_order, min_value, max_value, min_inclusive, max_inclusive, comparison_side, "
        "        grade_code, grade_label, is_compliant, status_text, highlight_style, evaluation_text, advice_text, remark "
        " FROM tbl_evaluation_rule_details "
        " WHERE rule_set_id = %s "
        " ORDER BY display_order ASC, id ASC ",
        (rule_set_id,)
    )
    rows = cursor.fetchall()
    for row in rows or []:
        detail = _row_to_detail(row)
        if detail['comparison_side'] != 'actual':
            continue
        if _detail_matches(detail, actual_value):
            return detail
    return None


def evaluate_metric(enterprise_space_id, space_id, product_id, metric_code, actual_value, report_date=None):
    if metric_code not in VALID_METRIC_CODES:
        raise falcon.HTTPError(status=falcon.HTTP_400,
                               title='API.BAD_REQUEST',
                               description='API.INVALID_METRIC_CODE')

    actual_decimal = _decimal_or_none(actual_value)
    report_date = _date_or_today(report_date)
    if actual_decimal is None:
        return {
            'metric_code': metric_code,
            'actual_value': None,
            'status': 'actual_value_missing',
            'is_compliant': None,
            'advice_list': [],
        }

    cnx = None
    cursor = None
    try:
        cnx = _connect()
        cursor = cnx.cursor()
        rule_set = _query_rule_set(cursor, enterprise_space_id, space_id, product_id, metric_code, report_date)
        if rule_set is None:
            return {
                'metric_code': metric_code,
                'actual_value': actual_decimal,
                'status': 'rule_missing',
                'is_compliant': None,
                'advice_list': [],
            }

        rule_context = {
            'metric_code': metric_code,
            'metric_unit': rule_set['metric_unit'],
            'rule_set_id': rule_set['id'],
            'rule_set_code': rule_set['rule_set_code'],
            'rule_set_name': rule_set['name'],
            'benchmark_source': rule_set['benchmark_source'],
            'scope_level': rule_set['scope_level'],
            'effective_date': rule_set['effective_date'].isoformat() if rule_set['effective_date'] else None,
            'expiry_date': rule_set['expiry_date'].isoformat() if rule_set['expiry_date'] else None,
            'rule_updated_datetime_utc': (rule_set['updated_datetime_utc'].isoformat()
                                         if rule_set['updated_datetime_utc'] else None),
            'benchmark_value': rule_set['benchmark_value'],
            'benchmark_display_name': rule_set['benchmark_display_name'],
            'actual_value': actual_decimal,
        }
        detail = _query_matching_detail(cursor, rule_set['id'], actual_decimal)
        if detail is None:
            return {
                **rule_context,
                'status': 'detail_missing',
                'is_compliant': None,
                'advice_list': [],
            }

        benchmark_value = rule_set['benchmark_value']
        gap_rate = None
        if benchmark_value is not None and benchmark_value != 0:
            gap_rate = (actual_decimal - benchmark_value) / benchmark_value

        return {
            **rule_context,
            'detail_id': detail['id'],
            'min_value': detail['min_value'],
            'max_value': detail['max_value'],
            'min_inclusive': detail['min_inclusive'],
            'max_inclusive': detail['max_inclusive'],
            'gap_rate': gap_rate,
            'grade_code': detail['grade_code'],
            'grade_label': detail['grade_label'],
            'status': 'matched',
            'status_text': detail['status_text'],
            'is_compliant': detail['is_compliant'],
            'highlight_style': detail['highlight_style'],
            'evaluation_text': detail['evaluation_text'],
            'advice_text': detail['advice_text'],
            'advice_list': _split_advice(detail['advice_text']),
        }
    finally:
        if cursor:
            cursor.close()
        if cnx:
            cnx.close()