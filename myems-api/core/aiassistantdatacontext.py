import datetime
import re
from decimal import Decimal

import mysql.connector

import config
from reports.productreporting import get_dimension_hourly_rows, get_space_product_energy_sources


PRODUCT_CARBON_STAGE_LABELS = {
    'raw_material_acquisition': '原材料获取',
    'manufacturing': '生产制造',
    'distribution': '销售运输',
    'use': '使用阶段',
    'end_of_life': '废弃处理',
}


def _utc_offset():
    sign = 1 if config.utc_offset[0] == '+' else -1
    hours = int(config.utc_offset[1:3])
    minutes = int(config.utc_offset[4:6])
    return datetime.timedelta(minutes=sign * (hours * 60 + minutes))


def _reporting_period_utc(report_context):
    start_date = datetime.date.fromisoformat(report_context['period_start'])
    end_date = datetime.date.fromisoformat(report_context['period_end']) + datetime.timedelta(days=1)
    offset = _utc_offset()
    return (datetime.datetime.combine(start_date, datetime.time.min) - offset,
            datetime.datetime.combine(end_date, datetime.time.min) - offset)


def _number(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


def build_chat_product_footprint_context(enterprise_space_id, products, query_text):
    if not products or enterprise_space_id is None:
        return None
    if not re.search(r'排放|碳足迹|碳排|排碳|温室气体|carbon|emission|footprint', query_text, re.IGNORECASE):
        return None

    matched_products = [product for product in products
                        if product['name'] and product['name'].casefold() in query_text.casefold()]
    mentioned_product_ids = {
        int(match.group(1)) for match in re.finditer(
            r'(?:产品\s*)?(?:id|编号|产品号)\s*[:：#]?\s*(\d+)', query_text, re.IGNORECASE)
    }
    matched_product_ids = {product['id'] for product in matched_products}
    matched_product_ids.update(product_id for product_id in mentioned_product_ids
                               if any(product['id'] == product_id for product in products))
    if not matched_product_ids:
        return {
            'matched_products': [],
            'footprints': [],
            'missing_data': ['No authorized product could be matched to the product-emissions question.'],
            'sources': [],
        }

    product_by_id = {product['id']: product for product in products}
    production_connection = mysql.connector.connect(**config.myems_production_db)
    production_cursor = production_connection.cursor()
    try:
        placeholders = ', '.join(['%s'] * len(matched_product_ids))
        production_cursor.execute(
            ' SELECT id, product_id, accounting_year, name, unit, accounting_date, system_boundary, start_date, '
            '        end_date, production_quantity, functional_unit, total_carbon_footprint '
            ' FROM tbl_product_carbon_footprints '
            ' WHERE enterprise_space_id = %s AND product_id IN ({0}) AND data_status = %s '
            ' ORDER BY accounting_year DESC, accounting_date DESC, id DESC LIMIT 100 '.format(placeholders),
            tuple([enterprise_space_id] + sorted(matched_product_ids) + ['active']))
        rows = production_cursor.fetchall()
        year_match = re.search(r'(20\d{2})(?:年|\b)', query_text)
        requested_year = int(year_match.group(1)) if year_match else None
        selected_rows = []
        product_year_counts = {}
        for row in rows:
            if requested_year is not None and row[2] != requested_year:
                continue
            count = product_year_counts.get(row[1], 0)
            if requested_year is None and count >= 3:
                continue
            selected_rows.append(row)
            product_year_counts[row[1]] = count + 1

        footprint_ids = [row[0] for row in selected_rows]
        stage_totals = {}
        if footprint_ids:
            footprint_placeholders = ', '.join(['%s'] * len(footprint_ids))
            production_cursor.execute(
                ' SELECT footprint_id, stage, SUM(emission_amount) '
                ' FROM tbl_product_carbon_activities '
                ' WHERE enterprise_space_id = %s AND footprint_id IN ({0}) '
                ' GROUP BY footprint_id, stage ORDER BY footprint_id, stage '.format(footprint_placeholders),
                tuple([enterprise_space_id] + footprint_ids))
            for footprint_id, stage, emission_amount in production_cursor.fetchall():
                stage_totals.setdefault(footprint_id, []).append({
                    'stage': stage,
                    'stage_name': PRODUCT_CARBON_STAGE_LABELS.get(stage, stage),
                    'emission_amount_as_recorded': _number(emission_amount),
                    'emission_unit': None,
                })

        footprints = []
        missing_data = []
        for row in selected_rows:
            recorded_stage_emissions = stage_totals.get(row[0], [])
            recorded_stages = {stage['stage'] for stage in recorded_stage_emissions}
            missing_stages = [
                {'stage': stage, 'stage_name': stage_name}
                for stage, stage_name in PRODUCT_CARBON_STAGE_LABELS.items()
                if stage not in recorded_stages
            ]
            footprints.append({
                'product_id': row[1],
                'product_name': product_by_id[row[1]]['name'],
                'accounting_year': row[2],
                'record_name': row[3],
                'footprint_value_unit': row[4],
                'accounting_date': row[5].isoformat() if row[5] else None,
                'system_boundary': row[6],
                'period_start': row[7].isoformat() if row[7] else None,
                'period_end': row[8].isoformat() if row[8] else None,
                'production_quantity': _number(row[9]),
                'functional_unit': row[10],
                'footprint_per_functional_unit': _number(row[11]),
                'stage_emissions': recorded_stage_emissions,
                'missing_stages': missing_stages,
            })
            if missing_stages:
                missing_data.append(
                    'Product {0} {1} has no activity records for lifecycle stages: {2}. '
                    'These stages must not be treated as zero emissions.'.format(
                        product_by_id[row[1]]['name'], row[2],
                        ', '.join(stage['stage_name'] for stage in missing_stages)))
        if not footprints:
            missing_data.append('No active product carbon-footprint record matches the authorized product and requested year.')
        return {
            'matched_products': [product_by_id[product_id] for product_id in sorted(matched_product_ids)],
            'footprints': footprints,
            'missing_data': missing_data,
            'sources': [{
                'type': 'product_carbon_footprint_summary',
                'tables': ['myems_production_db.tbl_product_carbon_footprints',
                           'myems_production_db.tbl_product_carbon_activities'],
                'filter': 'current enterprise, authorized bound product, active record',
            }],
        }
    finally:
        production_cursor.close()
        production_connection.close()


def _space_products(cursor, space_id, selected_product_id):
    cursor.execute(' SELECT DISTINCT product_id FROM tbl_spaces_products '
                   ' WHERE space_id = %s ORDER BY product_id ', (space_id,))
    bound_product_ids = [row[0] for row in cursor.fetchall()]
    if selected_product_id is not None:
        if selected_product_id not in bound_product_ids:
            return [], bound_product_ids
        return [selected_product_id], bound_product_ids
    return bound_product_ids, bound_product_ids


def build_report_data_context(authorized_space_ids, report_request, report_context, enterprise_space_id=None,
                              include_cost_data=False):
    """Return an authorized, read-only data snapshot for a report request."""
    if report_request is None or report_context is None:
        return None

    space_id = report_context['space_id']
    if space_id not in set(authorized_space_ids or []):
        return None

    context = {
        'schema_version': 1,
        'report_type': report_request['type'],
        'period': {
            'start_date': report_context['period_start'],
            'end_date': report_context['period_end'],
            'timezone_offset': config.utc_offset,
        },
        'scope': {
            'space_id': space_id,
            'product_id': report_context['product_id'],
        },
        'metrics': {
            'energy_categories': [],
            'total_carbon_kgco2e': 0.0,
            'product_carbon_footprints': [],
            'production': [],
            'cost_categories': [],
            'total_cost': 0.0,
        },
        'sources': [],
        'missing_data': [],
        'limitations': [
            'This snapshot covers energy-related emissions only and is not a complete Scope 1, Scope 2, or Scope 3 inventory.'
        ],
    }
    system_connection = mysql.connector.connect(**config.myems_system_db)
    system_cursor = system_connection.cursor()
    energy_connection = None
    energy_cursor = None
    production_connection = None
    production_cursor = None
    try:
        system_cursor.execute(' SELECT name FROM tbl_spaces WHERE id = %s ', (space_id,))
        space_row = system_cursor.fetchone()
        if space_row is None:
            return None
        context['scope']['space_name'] = space_row[0]

        product_ids, bound_product_ids = _space_products(system_cursor, space_id,
                                                          report_context['product_id'])
        context['scope']['bound_product_ids'] = bound_product_ids
        if not product_ids:
            context['missing_data'].append('No bound product is available for the selected space and product scope.')
            return context

        if report_request['type'] == 'product_carbon_footprint':
            if enterprise_space_id is None:
                context['missing_data'].append('The current enterprise scope is not available.')
                return context
            production_connection = mysql.connector.connect(**config.myems_production_db)
            production_cursor = production_connection.cursor()
            placeholders = ', '.join(['%s'] * len(product_ids))
            production_cursor.execute(
                ' SELECT accounting_year, name, unit, accounting_date, system_boundary, start_date, end_date, '
                '        production_quantity, functional_unit, total_carbon_footprint, data_status '
                ' FROM tbl_product_carbon_footprints '
                ' WHERE enterprise_space_id = %s AND product_id IN ({0}) AND data_status = %s '
                '       AND start_date <= %s AND end_date >= %s '
                ' ORDER BY accounting_year DESC, accounting_date DESC '.format(placeholders),
                tuple([enterprise_space_id] + product_ids + [
                    'active', report_context['period_end'], report_context['period_start']]))
            for row in production_cursor.fetchall():
                context['metrics']['product_carbon_footprints'].append({
                    'accounting_year': row[0],
                    'name': row[1],
                    'unit': row[2],
                    'accounting_date': row[3].isoformat() if row[3] else None,
                    'system_boundary': row[4],
                    'start_date': row[5].isoformat() if row[5] else None,
                    'end_date': row[6].isoformat() if row[6] else None,
                    'production_quantity': _number(row[7]),
                    'functional_unit': row[8],
                    'total_carbon_footprint': _number(row[9]),
                    'data_status': row[10],
                })
            if not context['metrics']['product_carbon_footprints']:
                context['missing_data'].append('No active product carbon footprint record overlaps the selected period.')
            context['sources'].append({
                'type': 'product_carbon_footprint_summary',
                'table': 'myems_production_db.tbl_product_carbon_footprints',
                'filter': 'current enterprise, authorized bound product, active status, overlapping period',
            })
            return context

        production_connection = mysql.connector.connect(**config.myems_production_db)
        production_cursor = production_connection.cursor()
        start_datetime_utc, end_datetime_utc = _reporting_period_utc(report_context)
        placeholders = ', '.join(['%s'] * len(product_ids))
        production_cursor.execute(
            ' SELECT products.name, hourly.product_id, SUM(hourly.product_count) '
            ' FROM tbl_space_hourly AS hourly '
            ' JOIN tbl_products AS products ON products.id = hourly.product_id '
            ' WHERE hourly.space_id = %s AND hourly.product_id IN ({0}) '
            '       AND hourly.start_datetime_utc >= %s AND hourly.start_datetime_utc < %s '
            ' GROUP BY products.name, hourly.product_id ORDER BY products.name '.format(placeholders),
            tuple([space_id] + product_ids + [start_datetime_utc, end_datetime_utc]))
        for row in production_cursor.fetchall():
            context['metrics']['production'].append({
                'product_id': row[1],
                'product_name': row[0],
                'production_quantity': _number(row[2]),
            })
        if not context['metrics']['production']:
            context['missing_data'].append('No production data exists in the selected reporting period.')
        context['sources'].append({
            'type': 'production_hourly_aggregation',
            'table': 'myems_production_db.tbl_space_hourly',
            'aggregation': 'sum by authorized bound product over [start_datetime_utc, end_datetime_utc)',
        })

        energy_connection = mysql.connector.connect(**config.myems_energy_db)
        energy_cursor = energy_connection.cursor()
        source_ids_by_category = {}
        for product_id in product_ids:
            sources = get_space_product_energy_sources(system_cursor, space_id, product_id)
            for category_id, meter_ids in sources['online_ids_by_dimension'].items():
                source_ids_by_category.setdefault(category_id, {'online': set(), 'offline': set()})['online'].update(
                    meter_ids)
            for category_id, meter_ids in sources['offline_ids_by_dimension'].items():
                source_ids_by_category.setdefault(category_id, {'online': set(), 'offline': set()})['offline'].update(
                    meter_ids)

        if not source_ids_by_category:
            context['missing_data'].append('No energy meters are bound to the selected scope.')
            return context

        source_dict = {
            'online_ids_by_dimension': {
                category_id: sorted(source_ids['online'])
                for category_id, source_ids in source_ids_by_category.items()
            },
            'offline_ids_by_dimension': {
                category_id: sorted(source_ids['offline'])
                for category_id, source_ids in source_ids_by_category.items()
            },
        }
        hourly_rows = get_dimension_hourly_rows(energy_cursor, source_dict,
                                                start_datetime_utc, end_datetime_utc)
        category_ids = sorted(source_ids_by_category)
        placeholders = ', '.join(['%s'] * len(category_ids))
        system_cursor.execute(' SELECT id, name, unit_of_measure, kgco2e '
                              ' FROM tbl_energy_categories WHERE id IN ({0}) '.format(placeholders),
                              tuple(category_ids))
        categories = {row[0]: {'name': row[1], 'unit': row[2], 'kgco2e_factor': row[3]}
                      for row in system_cursor.fetchall()}

        total_carbon = Decimal(0)
        for category_id in category_ids:
            category = categories.get(category_id)
            rows = hourly_rows.get(category_id, [])
            if category is None:
                context['missing_data'].append('Energy category {0} is not configured.'.format(category_id))
                continue
            if not rows:
                continue
            energy_value = sum((row[1] for row in rows), Decimal(0))
            factor = category['kgco2e_factor']
            carbon_value = energy_value * factor if factor is not None else None
            if factor is None:
                context['missing_data'].append(
                    'Energy category {0} has no kgCO2e factor.'.format(category_id))
            else:
                total_carbon += carbon_value
            context['metrics']['energy_categories'].append({
                'id': category_id,
                'name': category['name'],
                'energy_value': _number(energy_value),
                'unit': category['unit'],
                'carbon_kgco2e': _number(carbon_value) if carbon_value is not None else None,
                'kgco2e_factor': _number(factor) if factor is not None else None,
                'factor_source': 'myems_system_db.tbl_energy_categories',
            })

        if not context['metrics']['energy_categories']:
            context['missing_data'].append('No hourly energy data exists in the selected reporting period.')
        context['metrics']['total_carbon_kgco2e'] = _number(total_carbon)
        context['sources'].append({
            'type': 'meter_hourly_aggregation',
            'tables': ['myems_energy_db.tbl_meter_hourly', 'myems_energy_db.tbl_offline_meter_hourly'],
            'aggregation': 'sum by energy category over [start_datetime_utc, end_datetime_utc)',
            'factor_table': 'myems_system_db.tbl_energy_categories',
        })
        if include_cost_data:
            billing_connection = mysql.connector.connect(**config.myems_billing_db)
            billing_cursor = billing_connection.cursor()
            try:
                cost_rows = get_dimension_hourly_rows(billing_cursor, source_dict,
                                                      start_datetime_utc, end_datetime_utc)
                total_cost = Decimal(0)
                for category_id in category_ids:
                    rows = cost_rows.get(category_id, [])
                    if not rows:
                        continue
                    category = categories.get(category_id)
                    if category is None:
                        continue
                    cost_value = sum((row[1] for row in rows), Decimal(0))
                    total_cost += cost_value
                    context['metrics']['cost_categories'].append({
                        'name': category['name'],
                        'cost_value': _number(cost_value),
                    })
                context['metrics']['total_cost'] = _number(total_cost)
                if not context['metrics']['cost_categories']:
                    context['missing_data'].append('No billing cost data exists in the selected reporting period.')
                context['sources'].append({
                    'type': 'billing_hourly_aggregation',
                    'tables': ['myems_billing_db.tbl_meter_hourly',
                               'myems_billing_db.tbl_offline_meter_hourly'],
                    'aggregation': 'sum by energy category over [start_datetime_utc, end_datetime_utc)',
                })
            finally:
                billing_cursor.close()
                billing_connection.close()
    finally:
        if production_cursor:
            production_cursor.close()
        if production_connection:
            production_connection.close()
        if energy_cursor:
            energy_cursor.close()
        system_cursor.close()
        if energy_connection:
            energy_connection.close()
        system_connection.close()
    return context


def data_context_message(data_context):
    if data_context is None:
        return None
    import json

    return {
        'role': 'system',
        'content': (
            'The following MyEMS data-context snapshot is authoritative for this response. Use only its metrics for '
            'source facts. You may calculate additional totals, means, shares, differences, intensities, growth rates '
            'and unit conversions from available compatible inputs. Distinguish recorded values from derived results; '
            'state the inputs, formula, units, scope and denominator for key calculations. Sensible rounding is allowed '
            'for derived results, but never overwrite source facts. Do not invent measurements, factors, baselines or '
            'sources, or treat missing records as zero. Explicitly disclose missing_data and limitations; '
            'estimates must follow the report mode and must never be presented as measurements.\n\n{0}'
        ).format(json.dumps(data_context, ensure_ascii=False)),
    }