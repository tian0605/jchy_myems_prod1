import datetime
import logging
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import falcon
import mysql.connector

import config
from core.aichatquery import INTENT_LABELS, encode_context, resolve_query, summarize_rows
from core.aiassistantdatacontext import PRODUCT_CARBON_STAGE_LABELS, _reporting_period_utc, _utc_offset
from core.carbonasset import _row_to_asset
from core.productcarbon import _footprint_total_expression
from reports.productreporting import get_dimension_hourly_rows, get_space_product_energy_sources
from reports.spaceevaluation import _build_report


logger = logging.getLogger(__name__)
METER_TYPES = {
    'online': ('tbl_meters', 'tbl_spaces_meters', 'meter_id', 'tbl_meter_hourly'),
    'offline': ('tbl_offline_meters', 'tbl_spaces_offline_meters', 'offline_meter_id', 'tbl_offline_meter_hourly'),
    'virtual': ('tbl_virtual_meters', 'tbl_spaces_virtual_meters', 'virtual_meter_id', 'tbl_virtual_meter_hourly'),
}
ASSET_FIELDS = (
    'id, uuid, space_id, accounting_year, government_quota, previous_year_quota, purchased_quota, sold_quota, '
    'own_ccer, purchased_ccer, sold_ccer, purchased_green_certificate, sold_green_certificate, '
    'retired_green_certificate, quota_total, ccer_total, green_certificate_total, data_status, remark'
)


@contextmanager
def db_cursor(database):
    connection = mysql.connector.connect(**database)
    try:
        cursor = connection.cursor()
        try:
            yield cursor
        finally:
            cursor.close()
    finally:
        connection.close()


def fetch(database, sql, params=()):
    with db_cursor(database) as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def load_catalog(authorized_space_ids):
    space_ids = sorted(set(authorized_space_ids or []))
    if not space_ids:
        return {'spaces': [], 'products': [], 'meters': []}
    placeholders = ','.join(['%s'] * len(space_ids))
    spaces = fetch(config.myems_system_db, 'SELECT id,name FROM tbl_spaces WHERE id IN (' + placeholders + ') ORDER BY id',
                   tuple(space_ids))
    bindings = fetch(config.myems_system_db, 'SELECT DISTINCT space_id,product_id FROM tbl_spaces_products '
                     'WHERE space_id IN (' + placeholders + ') ORDER BY space_id,product_id', tuple(space_ids))
    product_ids = sorted({row[1] for row in bindings})
    products = {}
    if product_ids:
        rows = fetch(config.myems_production_db, 'SELECT id,name,unit_of_measure FROM tbl_products WHERE id IN (' +
                     ','.join(['%s'] * len(product_ids)) + ')', tuple(product_ids))
        products = {row[0]: row for row in rows}
    meters = []
    for kind, (table, binding_table, id_column, _) in METER_TYPES.items():
        rows = fetch(config.myems_system_db,
                     'SELECT DISTINCT m.id,m.name,m.energy_category_id,b.space_id FROM ' + table + ' m JOIN ' +
                     binding_table + ' b ON m.id=b.' + id_column + ' WHERE b.space_id IN (' + placeholders +
                     ') ORDER BY m.id,b.space_id', tuple(space_ids))
        by_id = {}
        for mid, name, category, space_id in rows:
            item = by_id.setdefault(mid, {'id': mid, 'key': kind + ':' + str(mid), 'type': kind,
                                         'name': name, 'category_id': category, 'space_ids': []})
            item['space_ids'].append(space_id)
        meters.extend(by_id.values())
    return {
        'spaces': [{'id': row[0], 'name': row[1]} for row in spaces],
        'products': [{'space_id': sid, 'id': pid, 'name': products[pid][1], 'unit': products[pid][2]}
                     for sid, pid in bindings if pid in products],
        'meters': meters,
    }


def _series(rows, scope, unit, source):
    start, end = _reporting_period_utc(scope)
    result = summarize_rows(rows, start, end, _utc_offset())
    result.update(unit=unit, source=source)
    return result


def _production(scope, product):
    start, end = _reporting_period_utc(scope)
    rows = fetch(config.myems_production_db,
                 'SELECT start_datetime_utc,product_count FROM tbl_space_hourly '
                 'WHERE space_id=%s AND product_id=%s AND start_datetime_utc>=%s AND start_datetime_utc<%s '
                 'ORDER BY start_datetime_utc', (scope['space_id'], product['id'], start, end))
    result = _series(rows, scope, product['unit'], 'myems_production_db.tbl_space_hourly')
    result.update(product_id=product['id'], product_name=product['name'])
    return result


def _energy(scope, product=None, database=None):
    database = database or config.myems_energy_db
    start, end = _reporting_period_utc(scope)
    meter = scope.get('meter') if product is None else None
    with db_cursor(config.myems_system_db) as cursor:
        if meter:
            _, _, _, table = METER_TYPES[meter['type']]
            sources = {
                'online_ids_by_dimension': {meter['category_id']: [meter['id']]},
                'offline_ids_by_dimension': {},
            }
        else:
            sources = get_space_product_energy_sources(cursor, scope['space_id'], product['id'])
            table = 'tbl_meter_hourly'
        category_ids = sorted(set(sources['online_ids_by_dimension']) | set(sources['offline_ids_by_dimension']))
        if not category_ids:
            return []
        cursor.execute('SELECT id,name,unit_of_measure,kgce,kgco2e FROM tbl_energy_categories WHERE id IN (' +
                       ','.join(['%s'] * len(category_ids)) + ')', tuple(category_ids))
        categories = cursor.fetchall()
    source_coverage = {}
    requested_hours = int((end - start).total_seconds() / 3600)
    with db_cursor(database) as cursor:
        if meter:
            id_column = METER_TYPES[meter['type']][2]
            cursor.execute('SELECT start_datetime_utc,actual_value FROM ' + table +
                           ' WHERE ' + id_column + '=%s AND start_datetime_utc>=%s AND start_datetime_utc<%s '
                           'ORDER BY start_datetime_utc', (meter['id'], start, end))
            rows_by_category = {meter['category_id']: cursor.fetchall()}
        else:
            rows_by_category = get_dimension_hourly_rows(cursor, sources, start, end)
            for kind in ('online', 'offline'):
                dimensions = sources[kind + '_ids_by_dimension']
                ids = sorted({mid for values in dimensions.values() for mid in values})
                if not ids:
                    continue
                _, _, id_column, source_table = METER_TYPES[kind]
                cursor.execute('SELECT ' + id_column + ',COUNT(DISTINCT start_datetime_utc) FROM ' + source_table +
                               ' WHERE ' + id_column + ' IN (' + ','.join(['%s'] * len(ids)) +
                               ') AND start_datetime_utc>=%s AND start_datetime_utc<%s AND actual_value IS NOT NULL '
                               'GROUP BY ' + id_column, tuple(ids) + (start, end))
                counts = dict(cursor.fetchall())
                for cid, bound_ids in dimensions.items():
                    source_coverage.setdefault(cid, []).extend(
                        {'meter_key': kind + ':' + str(mid), 'observed_hours': counts.get(mid, 0),
                         'requested_hours': requested_hours} for mid in bound_ids)
    result = []
    for cid, name, unit, kgce, kgco2e in categories:
        category = {
            'category_id': cid, 'name': name,
            'kgce_factor': kgce, 'kgco2e_factor': kgco2e,
            'factor_source': 'myems_system_db.tbl_energy_categories (current configuration)',
            'factor_effective_period': None,
            'binding_ids': {meter['type']: [meter['id']]} if meter else
                           {'online': sources['online_ids_by_dimension'].get(cid, []),
                            'offline': sources['offline_ids_by_dimension'].get(cid, [])},
            **_series(rows_by_category.get(cid, []), scope, unit,
                      database['database'] + '.' + table + ('' if meter else ' / tbl_offline_meter_hourly')),
        }
        if not meter:
            category['source_coverage'] = source_coverage.get(cid, [])
            if category['total'] is not None and any(
                    item['observed_hours'] < requested_hours for item in category['source_coverage']):
                category['status'] = 'partial'
            category['coverage_note'] = '分类有记录不代表全部绑定计量表完整，缺失来源不按零用量处理。'
        result.append(category)
    return result


def _category_summary(categories):
    observed = [row for row in categories if row['total'] is not None]
    result = {'current_factor_carbon_kgco2e': None, 'current_factor_energy_tce': None}
    if observed:
        for output, factor, scale in [('current_factor_carbon_kgco2e', 'kgco2e_factor', Decimal(1)),
                                     ('current_factor_energy_tce', 'kgce_factor', Decimal(1000))]:
            if all(row[factor] is not None for row in observed):
                result[output] = sum((row['total'] * row[factor] / scale for row in observed), Decimal(0))
    result['missing_categories'] = [row['name'] for row in categories if row['total'] is None]
    result['status'] = ('no_records' if not observed else
                        'available' if all(row['status'] == 'available' for row in categories) else 'partial')
    return result


def production_query(scope):
    products = [_production(scope, product) for product in scope['products']]
    return {'status': _combined_status(products), 'products': products,
            'note': '只读产量记录，未记录日期与已录入零值不同；录入人和历史修改信息未从此表取得。'}


def _combined_status(items):
    statuses = [item.get('status', 'available') for item in items]
    if not statuses or all(status == 'no_records' for status in statuses):
        return 'no_records'
    return 'available' if all(status == 'available' for status in statuses) else 'partial'


def energy_query(scope):
    products = []
    for product in scope['products']:
        categories = _energy(scope, product)
        products.append({'product': product, 'energy_categories': categories, **_category_summary(categories)})
    return {'status': _combined_status(products), 'products': products,
            'note': '分类因子折算是当前配置重算值，不是碳库已存值；不同产品结果不直接相加。'}


def carbon_query(scope):
    products = []
    for product in scope['products']:
        categories = _energy(scope, product, config.myems_carbon_db)
        for category in categories:
            category['unit'] = 'kgCO2e'
            for key in ('kgce_factor', 'kgco2e_factor', 'factor_source', 'factor_effective_period'):
                category.pop(key)
        totals = [c['total'] for c in categories if c['total'] is not None]
        products.append({'product': product, 'categories': categories, 'status': _combined_status(categories),
                         'recorded_carbon_kgco2e': sum(totals, Decimal(0)) if totals else None})
    return {'status': _combined_status(products), 'products': products,
            'note': '碳库已存小时排放合计；历史因子版本未记录，不推测其变更原因。'}


def cost_query(scope):
    products = []
    for product in scope['products']:
        categories = _energy(scope, product, config.myems_billing_db)
        for category in categories:
            category['unit'] = 'billing amount (currency not provided)'
            for key in ('kgce_factor', 'kgco2e_factor', 'factor_source', 'factor_effective_period'):
                category.pop(key)
        products.append({'product': product, 'categories': categories, 'status': _combined_status(categories)})
    return {'status': _combined_status(products), 'products': products,
            'note': '账单金额，币种未由该查询提供，不作跨币种合计。'}


def evaluation_query(scope):
    results = []
    for product in scope['products']:
        production = _production(scope, product)
        if product['unit'] not in ('t', '吨'):
            results.append({'product': product, 'production': production, 'status': 'partial',
                            'message': '现有评价按吨定义指标，当前产品单位不是吨；不自动换算后冒充页面等级。'})
            continue
        start, end = _reporting_period_utc(scope)
        report = _build_report(SimpleNamespace(context={'enterprise_space_id': scope['enterprise_space_id']}),
                               scope['space_id'], product['id'], 'daily', start, end,
                               int(_utc_offset().total_seconds() / 60))
        energy = _energy(scope, product)
        results.append({'product': product, 'production': production,
                        'status': _combined_status([production] + energy) if energy else 'partial',
                        'energy_coverage': energy,
                        'summary': report['summary'], 'evaluations': report['evaluations'],
                        'energy_categories': report['energy_categories'], 'trends': report['trends']})
    return {'status': _combined_status(results), 'products': results,
            'source': 'myems_production_db.tbl_evaluation_rule_sets / tbl_evaluation_rule_details',
            'note': '复用节能评价页面的规则和计算，并非行业认证；单位不是吨时不生成等级。覆盖不足须与等级同时说明。'}


def footprint_query(scope):
    ids = scope['product_ids']
    placeholders = ','.join(['%s'] * len(ids))
    rows = fetch(config.myems_production_db,
                 'SELECT f.id,f.product_id,f.accounting_year,f.name,f.unit,f.accounting_date,f.system_boundary,'
                 'f.start_date,f.end_date,f.production_quantity,f.functional_unit,' + _footprint_total_expression() +
                 ' FROM tbl_product_carbon_footprints f WHERE f.enterprise_space_id=%s AND f.product_id IN (' +
                 placeholders + ') AND f.accounting_year=%s AND f.data_status=%s ORDER BY f.id',
                 tuple([scope['enterprise_space_id']] + ids + [scope['year'], 'active']))
    footprints = []
    for row in rows:
        activities = fetch(config.myems_production_db,
                           'SELECT id,stage,category,activity_name,activity_level,unit,factor,emission_amount,'
                           'factor_source FROM tbl_product_carbon_activities WHERE enterprise_space_id=%s '
                           'AND footprint_id=%s ORDER BY id', (scope['enterprise_space_id'], row[0]))
        details = []
        stages = defaultdict(lambda: Decimal(0))
        for aid, stage, category, name, level, unit, factor, amount, factor_source in activities:
            stages[stage] += amount
            details.append({'id': aid, 'stage': stage, 'category': category, 'name': name,
                            'activity_level': level, 'activity_unit': unit, 'factor': factor,
                            'emission_amount_as_recorded': amount, 'factor_source': factor_source})
        total = sum(stages.values(), Decimal(0))
        footprints.append({
            'record_id': row[0], 'product_id': row[1], 'accounting_year': row[2], 'name': row[3],
            'record_unit': row[4], 'accounting_date': row[5], 'system_boundary': row[6],
            'period_start': row[7], 'period_end': row[8], 'production_quantity': row[9],
            'functional_unit': row[10], 'footprint_per_functional_unit': row[11],
            'display_unit': 'kgCO2e/' + row[10],
            'recorded_activity_total': total if activities else None,
            'stages': [{'stage': stage, 'name': PRODUCT_CARBON_STAGE_LABELS.get(stage, stage),
                        'emission_amount_as_recorded': amount,
                        'share_of_recorded_total': amount / total if total else None} for stage, amount in stages.items()],
            'missing_stages': [label for stage, label in PRODUCT_CARBON_STAGE_LABELS.items() if stage not in stages],
            'activities': details[:100], 'activity_count': len(details), 'activities_truncated': len(details) > 100,
            'note': '排放量页面约定显示kgCO2e；原始活动表未独立存排放单位，需核对活动单位/因子。'
                    '核算期间不是必然完整自然年；缺失阶段不是零排放。贡献分母为已记录活动合计。',
        })
    return {'status': 'available' if footprints else 'no_records', 'footprints': footprints,
            'source': 'myems_production_db.tbl_product_carbon_footprints / tbl_product_carbon_activities'}


def meter_query(scope):
    categories = _energy(scope)
    result = {'meter': scope['meter'], 'energy_categories': categories, **_category_summary(categories)}
    if scope['meter']['type'] == 'online':
        start, end = _reporting_period_utc(scope)
        points = fetch(config.myems_system_db,
                       'SELECT DISTINCT p.id,p.name,p.units,p.object_type FROM tbl_meters_points mp '
                       'JOIN tbl_points p ON p.id=mp.point_id WHERE mp.meter_id=%s ORDER BY p.id',
                       (scope['meter']['id'],))
        result['point_count'] = len(points)
        result['points_truncated'] = len(points) > 20
        result['points'] = []
        tables = {'ENERGY_VALUE': 'tbl_energy_value', 'ANALOG_VALUE': 'tbl_analog_value', 'DIGITAL_VALUE': 'tbl_digital_value'}
        for pid, name, unit, kind in points[:20]:
            if kind not in tables:
                result['points'].append({'id': pid, 'name': name, 'status': 'unsupported'})
                continue
            rows = fetch(config.myems_historical_db,
                         'SELECT DATE(DATE_ADD(utc_date_time, INTERVAL %s MINUTE)),COUNT(actual_value),'
                         'MIN(actual_value),MAX(actual_value),AVG(actual_value) FROM ' + tables[kind] +
                         ' WHERE point_id=%s AND utc_date_time>=%s AND utc_date_time<%s '
                         'GROUP BY 1 ORDER BY 1', (int(_utc_offset().total_seconds() / 60), pid, start, end))
            result['points'].append({'id': pid, 'name': name, 'unit': unit, 'kind': kind,
                                     'daily': [{'date': day, 'samples': count, 'min': low, 'max': high, 'mean': mean}
                                               for day, count, low, high, mean in rows],
                                     'source': 'myems_historical_db.' + tables[kind],
                                     'note': '原始测点的日统计，不是区间用量；不得将累计读数或功率直接累加当成能耗。'})
    return result


def asset_query(scope):
    rows = fetch(config.myems_production_db, 'SELECT ' + ASSET_FIELDS +
                 ' FROM tbl_carbon_assets WHERE enterprise_space_id=%s AND space_id=%s AND accounting_year=%s ORDER BY id',
                 (scope['enterprise_space_id'], scope['space_id'], scope['year']))
    year_scope = {**scope, 'period_start': str(scope['year']) + '-01-01', 'period_end': str(scope['year']) + '-12-31'}
    start, end = _reporting_period_utc(year_scope)
    emissions = fetch(config.myems_carbon_db,
                      'SELECT start_datetime_utc,actual_value FROM tbl_space_input_category_hourly '
                      'WHERE space_id=%s AND start_datetime_utc>=%s AND start_datetime_utc<%s ORDER BY start_datetime_utc',
                      (scope['space_id'], start, end))
    # Multiple categories at one hour contribute to the space total, not extra observed hours.
    hourly = defaultdict(lambda: Decimal(0))
    for stamp, value in emissions:
        if value is not None:
            hourly[stamp] += value
    series = _series(sorted(hourly.items()), year_scope, 'kgCO2e', 'myems_carbon_db.tbl_space_input_category_hourly')
    monthly = defaultdict(lambda: Decimal(0))
    for entry in series['daily']:
        monthly[entry['date'][:7]] += entry['value'] / Decimal(1000)
    assets = []
    for row in rows:
        asset = _row_to_asset(row, scope['space_name'])
        quotas = fetch(config.myems_production_db,
                       'SELECT month_of_year,quota_amount,remark FROM tbl_carbon_asset_monthly_quotas '
                       'WHERE carbon_asset_id=%s ORDER BY month_of_year', (asset['id'],))
        recorded_tonnes = series['total'] / Decimal(1000) if series['total'] is not None else None
        assets.append({
            'asset': asset, 'quota_and_ccer_unit': 'tCO2e', 'green_certificate_unit': 'certificate',
            'monthly_quotas': [{'month': m, 'quota_tco2e': value, 'remark': remark} for m, value, remark in quotas],
            'recorded_emissions_tco2e': recorded_tonnes,
            'page_balance_tco2e': asset['quota_total'] + asset['ccer_total'] - (recorded_tonnes or Decimal(0)),
        })
    return {'status': _combined_status([{'status': 'available' if rows else 'no_records'}, series]), 'assets': assets,
            'emissions': series, 'monthly_recorded_emissions_tco2e': dict(monthly),
            'source': 'myems_production_db.tbl_carbon_assets / tbl_carbon_asset_monthly_quotas',
            'note': 'page_balance按页面将无排放记录视为0计算，仅展示该口径，并非已确认余额。'
                    '配额+CCER-排放不是法定可履约量，未核实抵销资格与比例。绿证单列；未录月不视为全年实际零排放。'}


def market_query(scope):
    rows = fetch(config.myems_production_db,
                 'SELECT trade_date,market_code,variety_code,open_price,close_price,high_price,low_price,'
                 'trading_volume,trading_amount,source_name FROM tbl_carbon_market_histories '
                 'WHERE trade_date>=%s AND trade_date<=%s ORDER BY trade_date DESC,market_code,variety_code LIMIT 101',
                 (scope['period_start'], scope['period_end']))
    return {'status': 'available' if rows else 'no_records',
            'included_record_count': min(len(rows), 100),
            'latest_trade_date': rows[0][0] if rows else None,
            'earliest_included_trade_date': rows[min(len(rows), 100) - 1][0] if rows else None,
            'included_sources': sorted({row[9] for row in rows[:100] if row[9]}),
            'items': [dict(zip(('trade_date', 'market', 'variety', 'open', 'close', 'high', 'low',
                               'volume', 'amount', 'source'), row)) for row in rows[:100]],
            'truncated': len(rows) > 100, 'limit': 100, 'currency': None, 'price_unit': None,
            'source': 'myems_production_db.tbl_carbon_market_histories',
            'note': '共享历史行情，不是企业成交或实时价；币种和计价单位未提供，不能无依据估值。'}


def _limit_details(value):
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key == 'daily' and isinstance(item, list) and len(item) > 31:
                value['daily_count'] = len(item)
                value['daily_truncated'] = True
                value[key] = item[:15] + item[-16:]
            else:
                _limit_details(item)
    elif isinstance(value, list):
        for item in value:
            _limit_details(item)


def build_chat_context(enterprise_space_id, authorized_space_ids, capabilities, text, previous=None, selection=None,
                       web_routes=None):
    if 'ai_analysis' not in capabilities:
        return {'status': 'forbidden', 'message': '当前账号没有AI系统数据分析权限，未执行查询。'}
    if enterprise_space_id is None:
        return {'status': 'input_required', 'field': 'enterprise', 'options': [],
                'message': '请使用具有明确企业范围的账号进行系统数据分析。'}
    try:
        catalog = load_catalog(authorized_space_ids)
    except mysql.connector.Error:
        logger.exception('AI chat catalog query failed')
        return {'status': 'failed', 'message': '授权数据目录查询失败，请检查API日志后重试；不代表没有数据。'}
    local_today = (datetime.datetime.utcnow() + _utc_offset()).date()
    result = resolve_query(text, catalog, previous, selection, today=local_today)
    if result['status'] != 'ready':
        if result.get('scope'):
            result['scope']['enterprise_space_id'] = enterprise_space_id
        return result
    scope = result['scope']
    if len(scope.get('products', [])) > 10:
        scope['enterprise_space_id'] = enterprise_space_id
        return {'status': 'input_required', 'field': 'product_id', 'scope': scope,
                'message': '一次最多分析10个产品，请缩小范围。',
                'options': [{'value': p['id'], 'label': p['name']} for p in scope['products']]}
    scope.update(enterprise_space_id=enterprise_space_id, timezone_offset=config.utc_offset)
    snapshot = {'schema_version': 1, 'status': 'available', 'scope': scope,
                'generated_at_utc': datetime.datetime.utcnow().isoformat(), 'sections': []}
    jobs = []
    intents = scope['intents']
    if set(intents) & {'energy', 'production_energy'}:
        jobs.extend([('energy', energy_query), ('carbon', carbon_query), ('production', production_query)])
    if 'production' in intents and not any(key == 'production' for key, _ in jobs):
        jobs.append(('production', production_query))
    for intent, function in [('evaluation', evaluation_query), ('footprint', footprint_query),
                             ('meter', meter_query), ('assets', asset_query), ('market', market_query)]:
        if intent in intents:
            jobs.append((intent, function))
    if scope['include_cost'] and scope.get('products') and 'market' not in intents:
        jobs.append(('cost', cost_query))
    routes = {
        'energy': ['/space/production', '/space/energycategory'],
        'carbon': ['/space/production', '/space/carbon'],
        'production': ['/space/production', '/space/enterproduction', '/space/evaluation'],
        'evaluation': ['/space/evaluation'],
        'footprint': ['/carbon/footprint'],
        'assets': ['/carbon/asset'],
        'market': ['/carbon/asset'],
        'cost': ['/space/cost'],
        'meter': [{'online': '/meter/meterenergy', 'offline': '/meter/offlinemeterenergy',
                   'virtual': '/meter/virtualmeterenergy'}.get((scope.get('meter') or {}).get('type'))],
    }
    for key, function in jobs:
        for period, current in [('reporting', scope)] + (
                [('comparison', {**scope, **scope['comparison']})]
                if scope.get('comparison') and key not in ('assets', 'market', 'footprint') else []):
            if key in ('assets', 'footprint'):
                current = {**current, 'period_start': str(current['year']) + '-01-01',
                           'period_end': str(current['year']) + '-12-31'}
            section = {'type': key, 'label': INTENT_LABELS.get(key, {'carbon': '碳库已存排放', 'cost': '账单费用'}.get(key, key)),
                       'period': period, 'period_start': current['period_start'], 'period_end': current['period_end'],
                       'required_routes': routes[key]}
            if key in ('assets', 'footprint'):
                section.update(accounting_year=current['year'], requested_period_start=scope['period_start'],
                               requested_period_end=scope['period_end'],
                               scope_note='该数据源按核算年度查询，不是仅查询请求月份；具体记录期间和实际覆盖另列。')
            if key in ('cost', 'market') and 'ai_cost_analysis' not in capabilities:
                section.update(status='forbidden', message='当前账号未授权AI费用/行情分析，未查询这些数据。')
            elif web_routes is not None and not set(routes[key]).intersection(web_routes):
                section.update(status='forbidden', message='当前账号没有对应业务页面的读取权限，未查询这些数据。')
            else:
                try:
                    data = function(current)
                    section.update(status=data.get('status', 'available'), data=data)
                except (mysql.connector.Error, falcon.HTTPError):
                    logger.exception('AI chat read-only query failed: section=%s space=%s', key, scope['space_id'])
                    section.update(status='failed', message='该数据源查询失败，请检查API日志后重试；不代表无记录。')
                    snapshot['status'] = 'partial'
            snapshot['sections'].append(section)
    statuses = {section['status'] for section in snapshot['sections']}
    if statuses == {'available'}:
        snapshot['status'] = 'available'
    elif statuses.intersection({'available', 'partial'}):
        snapshot['status'] = 'partial'
    elif 'failed' in statuses:
        snapshot.update(status='failed', message='本次没有取得可用于分析的数据，部分数据源查询失败；请查看来源状态后重试，不代表没有业务记录。')
    elif statuses == {'forbidden'}:
        snapshot.update(status='forbidden', message='当前账号没有本次所需业务数据的读取权限，未查询这些数据。')
    else:
        snapshot.update(status='no_records', message='本次允许查询的数据源在指定范围内无记录，不代表实际值为零；未获授权的数据源请查看来源说明。')
    if len(encode_context(snapshot)) > 100000:
        _limit_details(snapshot)
        snapshot['detail_notice'] = '部分长日期序列仅保留首尾样本；总量、覆盖及来源保留，不能把样本相加当成全量。'
    if len(encode_context(snapshot)) > 180000:
        return {'status': 'input_required', 'field': 'period', 'options': [], 'scope': scope,
                'message': '本次查询结果过大，请缩短期间或减少产品范围；没有向模型发送截断的无标识数据。'}
    return snapshot
