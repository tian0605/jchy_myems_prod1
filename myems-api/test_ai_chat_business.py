import datetime
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from core import aichatdata, evaluationengine
from core.aichatcontext import context_summary
from core.aichatquery import resolve_query


CATALOG = {
    'spaces': [{'id': 101, 'name': '测试甲公司'}],
    'products': [{'id': 201, 'name': '测试产品', 'space_id': 101, 'unit': 't'}],
    'meters': [{'id': 301, 'name': '测试表', 'key': kind + ':301',
                'type': kind, 'category_id': 1, 'space_ids': [101]}
               for kind in ('online', 'offline', 'virtual')],
}


class BusinessScopeTests(unittest.TestCase):
    def test_cross_page_followup_keeps_period_and_changes_domain(self):
        previous = {}
        for text, intent in [('2026年6月产量', 'production'), ('看看单位产量能耗', 'production_energy'),
                             ('再看节能评价', 'evaluation'), ('查看2026年配额', 'assets')]:
            result = resolve_query(text, CATALOG, previous)
            self.assertEqual(result['status'], 'ready')
            scope = result['scope']
            self.assertEqual(scope['space_id'], 101)
            self.assertEqual(scope['intents'], [intent])
            if intent == 'assets':
                self.assertEqual(scope['period_start'], '2026-01-01')
                self.assertNotIn('products', scope)
            else:
                self.assertEqual(scope['period_start'], '2026-06-01')
                self.assertEqual(scope['product_ids'], [201])
            previous = scope

    def test_leap_month_comparison_has_valid_inclusive_boundaries(self):
        scope = resolve_query('2024年2月能耗同比', CATALOG)['scope']
        self.assertEqual(scope['period_end'], '2024-02-29')
        self.assertEqual(scope['comparison']['period_end'], '2023-02-28')

    def test_meter_followup_changes_type_without_carrying_old_meter(self):
        scope = resolve_query('2026年6月在线计量表ID301能耗', CATALOG)['scope']
        result = resolve_query('再看离线表ID301', CATALOG, scope)
        self.assertEqual(result['scope']['meter_key'], 'offline:301')
        self.assertEqual(result['scope']['period_start'], '2026-06-01')

    def test_annual_source_labels_do_not_claim_only_inherited_month(self):
        with patch.object(aichatdata, 'load_catalog', return_value=CATALOG), \
                patch.object(aichatdata, 'asset_query', return_value={'status': 'available'}) as query:
            result = aichatdata.build_chat_context(101, [101], ['ai_analysis'], '2026年6月碳资产')
        self.assertEqual(result['scope']['period_start'], '2026-06-01')
        section = result['sections'][0]
        self.assertEqual(section['period_start'], '2026-01-01')
        self.assertEqual(section['period_end'], '2026-12-31')
        self.assertEqual(section['accounting_year'], 2026)
        self.assertEqual(query.call_args.args[0]['period_start'], '2026-01-01')

    def test_all_sources_empty_have_no_records_status(self):
        with patch.object(aichatdata, 'load_catalog', return_value=CATALOG), \
                patch.object(aichatdata, 'energy_query', return_value={'status': 'no_records'}), \
                patch.object(aichatdata, 'carbon_query', return_value={'status': 'no_records'}), \
                patch.object(aichatdata, 'production_query', return_value={'status': 'no_records'}):
            result = aichatdata.build_chat_context(101, [101], ['ai_analysis'], '2026年6月能耗')
        self.assertEqual(result['status'], 'no_records')
        self.assertTrue(result['message'])

    def test_all_sources_forbidden_do_not_become_partial_data(self):
        with patch.object(aichatdata, 'load_catalog', return_value=CATALOG), \
                patch.object(aichatdata, 'energy_query') as query:
            result = aichatdata.build_chat_context(101, [101], ['ai_analysis'], '2026年6月能耗', web_routes=[])
        query.assert_not_called()
        self.assertEqual(result['status'], 'forbidden')

    def test_long_daily_series_keeps_full_totals_with_explicit_sampling(self):
        data = {'status': 'available', 'total': Decimal('366'), 'source': 'synthetic',
                'daily': [{'date': str(day), 'value': 1, 'note': 'x' * 400} for day in range(366)]}
        with patch.object(aichatdata, 'load_catalog', return_value=CATALOG), \
                patch.object(aichatdata, 'production_query', return_value=data):
            result = aichatdata.build_chat_context(101, [101], ['ai_analysis'], '2024年产量')
        saved = result['sections'][0]['data']
        self.assertEqual(saved['total'], 366)
        self.assertEqual(saved['daily_count'], 366)
        self.assertEqual(len(saved['daily']), 31)
        self.assertTrue(saved['daily_truncated'])
        self.assertTrue(result['detail_notice'])

    def test_oversized_non_series_requests_clarification(self):
        with patch.object(aichatdata, 'load_catalog', return_value=CATALOG), \
                patch.object(aichatdata, 'production_query', return_value={
                    'status': 'available', 'note': 'x' * 180001}):
            result = aichatdata.build_chat_context(101, [101], ['ai_analysis'], '2026年产量')
        self.assertEqual(result['status'], 'input_required')
        self.assertEqual(result['field'], 'period')
        self.assertNotIn('sections', result)


class AssetAndMeterTests(unittest.TestCase):
    def setUp(self):
        self.scope = {'enterprise_space_id': 101, 'space_id': 101, 'space_name': '测试甲公司',
                      'year': 2026, 'period_start': '2026-06-01', 'period_end': '2026-06-30'}
        self.asset = (1, 'synthetic', 101, 2026, Decimal(100), Decimal(0), Decimal(0), Decimal(0),
                      Decimal(20), Decimal(0), Decimal(0), Decimal(5), Decimal(0), Decimal(0),
                      Decimal(100), Decimal(20), Decimal(5), 'active', 'test')

    def test_asset_balance_separates_green_certificates_and_missing_months(self):
        rows = [(datetime.datetime(2026, 5, 31, 16), Decimal(60000)),
                (datetime.datetime(2026, 5, 31, 16), Decimal(50000))]
        with patch.object(aichatdata, 'fetch', side_effect=[[self.asset], rows, [(6, Decimal(100), '')]]) as fetch:
            result = aichatdata.asset_query(self.scope)
        asset = result['assets'][0]
        self.assertEqual(asset['page_balance_tco2e'], 10)
        self.assertEqual(asset['asset']['green_certificate_total'], 5)
        self.assertEqual(result['emissions']['coverage']['observed_hours'], 1)
        self.assertEqual(result['emissions']['coverage']['requested_days'], 365)
        self.assertEqual(result['monthly_recorded_emissions_tco2e'], {'2026-06': Decimal(110)})
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(fetch.call_args_list[0].args[2], (101, 101, 2026))

    def test_asset_record_missing_does_not_hide_existing_emissions(self):
        with patch.object(aichatdata, 'fetch', side_effect=[
                [], [(datetime.datetime(2026, 5, 31, 16), Decimal(1000))]]):
            result = aichatdata.asset_query(self.scope)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['assets'], [])
        self.assertEqual(result['emissions']['total'], 1000)

    def test_missing_emissions_are_not_confirmed_zero(self):
        with patch.object(aichatdata, 'fetch', side_effect=[[self.asset], [], []]):
            result = aichatdata.asset_query(self.scope)
        self.assertIsNone(result['assets'][0]['recorded_emissions_tco2e'])
        self.assertEqual(result['assets'][0]['page_balance_tco2e'], 120)
        self.assertEqual(result['status'], 'partial')

    def test_market_limit_preserves_dates_and_unknown_price_units(self):
        day = datetime.date(2026, 6, 1)
        rows = [(day, 'test', 'test', 1, 2, 3, 1, 10, 20, 'synthetic') for _ in range(101)]
        with patch.object(aichatdata, 'fetch', return_value=rows):
            result = aichatdata.market_query(self.scope)
        self.assertEqual(len(result['items']), 100)
        self.assertTrue(result['truncated'])
        summary = context_summary(result)
        self.assertEqual(summary['included_sources'], ['synthetic'])
        self.assertEqual(summary['latest_trade_date'], '2026-06-01')
        self.assertIsNone(summary['price_unit'])
        self.assertNotIn('items', summary)

    def test_online_point_reference_survives_compaction_without_series(self):
        self.scope['meter'] = CATALOG['meters'][0]
        with patch.object(aichatdata, '_energy', return_value=[]), \
                patch.object(aichatdata, 'fetch', side_effect=[
                    [(901, '测试测点', 'kW', 'ANALOG_VALUE')],
                    [(datetime.date(2026, 6, 1), 3, Decimal(1), Decimal(3), Decimal(2))]]):
            result = aichatdata.meter_query(self.scope)
        summary = context_summary(result)
        self.assertEqual(summary['points'][0]['id'], 901)
        self.assertEqual(summary['points'][0]['unit'], 'kW')
        self.assertIn('tbl_analog_value', summary['points'][0]['source'])
        self.assertNotIn('daily', summary['points'][0])


class EvaluationRuleTests(unittest.TestCase):
    def setUp(self):
        self.rule = {'id': 1, 'metric_unit': 'tce/t', 'rule_set_code': 'test', 'name': '测试规则',
                     'benchmark_source': 'synthetic benchmark', 'scope_level': 'enterprise_space_product',
                     'effective_date': datetime.date(2026, 1, 1), 'expiry_date': datetime.date(2026, 12, 31),
                     'updated_datetime_utc': datetime.datetime(2026, 1, 1), 'benchmark_value': Decimal('.1'),
                     'benchmark_display_name': '测试基准'}
        self.detail = {'id': 2, 'min_value': Decimal('.1'), 'max_value': Decimal('.2'),
                       'min_inclusive': True, 'max_inclusive': False, 'grade_code': 'test',
                       'grade_label': '测试等级', 'status_text': 'test', 'is_compliant': False,
                       'highlight_style': 'test', 'evaluation_text': 'test', 'advice_text': 'test'}

    def evaluate(self, actual):
        with patch.object(evaluationengine, '_connect', return_value=MagicMock()), \
                patch.object(evaluationengine, '_query_rule_set', return_value=self.rule), \
                patch.object(evaluationengine, '_query_matching_detail', return_value=self.detail):
            return evaluationengine.evaluate_metric(101, 101, 201, 'unit_comprehensive_energy_tce_per_t',
                                                     actual, datetime.date(2026, 6, 30))

    def test_boundary_and_inclusivity_match_page_engine(self):
        self.assertTrue(evaluationengine._detail_matches(self.detail, Decimal('.1')))
        self.assertFalse(evaluationengine._detail_matches(self.detail, Decimal('.2')))
        self.detail.update(min_inclusive=False, max_inclusive=True)
        self.assertFalse(evaluationengine._detail_matches(self.detail, Decimal('.1')))
        self.assertTrue(evaluationengine._detail_matches(self.detail, Decimal('.2')))

    def test_matched_rule_provenance_and_twenty_percent_gap(self):
        result = self.evaluate(Decimal('.12'))
        self.assertEqual(result['gap_rate'], Decimal('.2'))
        self.assertEqual(result['benchmark_source'], 'synthetic benchmark')
        self.assertEqual(result['effective_date'], '2026-01-01')
        self.assertEqual(result['max_value'], Decimal('.2'))

    def test_missing_detail_still_exposes_selected_rule_source(self):
        self.detail = None
        result = self.evaluate(Decimal('.12'))
        self.assertEqual(result['status'], 'detail_missing')
        self.assertNotIn('grade_code', result)
        self.assertEqual(result['benchmark_source'], 'synthetic benchmark')
        self.assertEqual(result['expiry_date'], '2026-12-31')

    def test_missing_or_zero_actual_never_invents_a_grade_or_ratio(self):
        self.rule = None
        self.assertEqual(self.evaluate(Decimal('.12'))['status'], 'rule_missing')
        self.assertEqual(self.evaluate(None)['status'], 'actual_value_missing')

    def test_rule_selection_checks_report_date_and_scope_fallback(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        day = datetime.date(2026, 6, 30)
        self.assertIsNone(evaluationengine._query_rule_set(
            cursor, 101, 101, 201, 'unit_comprehensive_energy_tce_per_t', day))
        self.assertEqual(cursor.execute.call_count, 4)
        for call in cursor.execute.call_args_list:
            sql, params = call.args
            self.assertIn('effective_date <= %s', sql)
            self.assertIn('expiry_date >= %s', sql)
            self.assertEqual(params[-2:], (day, day))
        self.assertEqual(cursor.execute.call_args.args[1][0], 0)


if __name__ == '__main__':
    unittest.main()
