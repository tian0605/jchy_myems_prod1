import datetime
import json
import unittest
import threading
import time
from contextlib import ExitStack
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import falcon
from falcon import testing
import mysql.connector

from core.aichatquery import resolve_query, summarize_rows
from core.aichatcontext import chat_messages, context_is_visible, context_summary
from core import aichatdata, aiassistant


CATALOG = {
    'spaces': [{'id': 3, 'name': '汕头市泷跃纺织科技有限公司'}],
    'products': [{'id': 3, 'name': '废旧涤纶布料', 'unit': 't', 'space_id': 3}],
    'meters': [],
}


class ChatQueryTests(unittest.TestCase):
    def resolve(self, text, previous=None, selection=None, catalog=None):
        return resolve_query(text, catalog or CATALOG, previous, selection,
                             today=datetime.date(2026, 10, 18))

    def test_original_energy_question(self):
        result = self.resolve('帮我详细分析泷跃公司2026年6月的能耗和碳排放水平')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['scope']['space_id'], 3)
        self.assertEqual(result['scope']['product_ids'], [3])
        self.assertEqual(result['scope']['period_start'], '2026-06-01')
        self.assertEqual(result['scope']['period_end'], '2026-06-30')
        self.assertIn('energy', result['scope']['intents'])

    def test_original_footprint_question(self):
        result = self.resolve('帮我分析2026年的碳足迹数据看看')
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['scope']['product_ids'], [3])
        self.assertEqual(result['scope']['intents'], ['footprint'])
        self.assertEqual(result['scope']['year'], 2026)

    def test_partial_coverage_preserves_zero_and_missing_days(self):
        start = datetime.datetime(2026, 5, 31, 16)
        end = datetime.datetime(2026, 6, 30, 16)
        rows = [(start + datetime.timedelta(hours=i), Decimal(0)) for i in range(24)]
        result = summarize_rows(rows, start, end, datetime.timedelta(hours=8))
        self.assertEqual(result['total'], Decimal(0))
        self.assertEqual(result['coverage']['observed_days'], 1)
        self.assertEqual(result['coverage']['requested_days'], 30)
        self.assertEqual(len(result['daily']), 1)
        self.assertEqual(result['status'], 'partial')

    def test_five_page_questions(self):
        examples = {
            '分析泷跃公司2026年6月每吨产品的能耗，看看哪些日期偏高': 'production_energy',
            '解释一下6月节能评价报告，哪些指标没达到系统配置的要求': 'evaluation',
            '查一下6月已经录入的产量，哪些日期还没有记录': 'production',
            '查看2026年的碳配额、CCER、绿证和排放余额': 'assets',
        }
        for text, intent in examples.items():
            with self.subTest(text=text):
                result = self.resolve(text)
                self.assertEqual(result['status'], 'ready')
                self.assertIn(intent, result['scope']['intents'])

    def test_followup_inherits_product_and_changes_year(self):
        scope = self.resolve('分析2026年碳足迹')['scope']
        result = self.resolve('再看看这个产品去年情况', scope)
        self.assertEqual(result['scope']['year'], 2025)
        self.assertEqual(result['scope']['product_ids'], [3])

    def test_invalid_dates_and_cross_year_accounting(self):
        for text in ('2026年13月能耗', '2026年2月30日能耗',
                     '2025-12-01至2026-01-01碳足迹'):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text)['status'], 'input_required')

    def test_multiple_products_require_selection(self):
        catalog = {**CATALOG, 'products': CATALOG['products'] + [
            {'id': 4, 'space_id': 3, 'name': '产品乙', 'unit': 'kg'}]}
        self.assertEqual(self.resolve('2026年碳足迹', catalog=catalog)['field'], 'product_id')
        self.assertEqual(self.resolve('2026年全部产品碳足迹', catalog=catalog)['scope']['product_ids'], [3, 4])
        selected = self.resolve('2026年碳足迹', selection={'product_id': 4}, catalog=catalog)
        self.assertEqual(selected['scope']['product_ids'], [4])

    def test_meter_id_is_namespaced(self):
        meters = [{'id': 1, 'key': kind + ':1', 'type': kind, 'name': '电表甲',
                   'category_id': 1, 'space_ids': [3]} for kind in ('online', 'offline', 'virtual')]
        catalog = {**CATALOG, 'meters': meters}
        self.assertEqual(self.resolve('2026年6月计量表ID 1能耗', catalog=catalog)['field'], 'meter_key')
        scope = self.resolve('2026年6月离线表ID 1能耗', catalog=catalog)['scope']
        self.assertEqual(scope['meter_key'], 'offline:1')
        self.assertNotIn('products', scope)

    def test_revoked_previous_product_does_not_choose_another(self):
        previous = {'space_id': 3, 'product_ids': [99], 'intents': ['footprint'],
                    'period_start': '2026-01-01', 'period_end': '2026-12-31', 'year': 2026}
        self.assertEqual(self.resolve('继续分析', previous)['status'], 'input_required')

    def test_selection_types_are_validated(self):
        for selection in ([], False, {'space_id': True}, {'product_id': [3]}, {'meter_key': 1}):
            self.assertEqual(self.resolve('2026年能耗', selection=selection)['status'], 'input_required')

    def test_write_requests_never_resolve_to_read(self):
        for text in ('帮我录入产量', '修改配额', '修改评价规则', '导入行情', '删除这条产量记录',
                     '帮我录入2026年6月的产量', 'delete production records'):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text)['status'], 'read_only')

    def test_comparison_uses_reporting_month_not_current_month(self):
        result = self.resolve('2026年6月用电和上个月比较')
        self.assertEqual(result['scope']['period_start'], '2026-06-01')
        self.assertEqual(result['scope']['comparison']['period_start'], '2026-05-01')

    def test_cost_only_query_resolves(self):
        result = self.resolve('2026年6月电费和成本')
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['scope']['include_cost'])

    def test_year_followup_does_not_need_marker(self):
        scope = self.resolve('2026年碳足迹')['scope']
        result = self.resolve('2025年呢', scope)
        self.assertEqual(result['scope']['year'], 2025)

    def test_cost_followup_keeps_requested_metric(self):
        scope = self.resolve('2026年电费')['scope']
        self.assertTrue(self.resolve('2025年呢', scope)['scope']['include_cost'])

    def test_unknown_explicit_product_name_does_not_fall_back(self):
        self.assertEqual(self.resolve('分析产品“不存在”的2026年碳足迹')['status'], 'input_required')

    def test_full_zero_series_is_not_missing(self):
        start = datetime.datetime(2026, 6, 1)
        result = summarize_rows([(start + datetime.timedelta(hours=i), Decimal(0)) for i in range(24)],
                                start, start + datetime.timedelta(days=1), datetime.timedelta())
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['total'], 0)


class ChatDataTests(unittest.TestCase):
    def build(self, text, capabilities=('ai_analysis',), selection=None):
        with patch.object(aichatdata, 'load_catalog', return_value=CATALOG):
            return aichatdata.build_chat_context(3, [3], capabilities, text, selection=selection)

    def test_original_question_fetches_three_business_domains(self):
        with patch.object(aichatdata, 'energy_query', return_value={'status': 'partial'}) as energy, \
                patch.object(aichatdata, 'carbon_query', return_value={'status': 'partial'}) as carbon, \
                patch.object(aichatdata, 'production_query', return_value={'status': 'partial'}) as production:
            result = self.build('泷跃公司2026年6月能耗和碳排放')
        self.assertEqual([s['type'] for s in result['sections']], ['energy', 'carbon', 'production'])
        for query in (energy, carbon, production):
            self.assertEqual(query.call_args.args[0]['product_ids'], [3])
            self.assertEqual(query.call_args.args[0]['enterprise_space_id'], 3)

    def test_one_failed_domain_is_not_no_records(self):
        with patch.object(aichatdata, 'energy_query', side_effect=mysql.connector.Error('test')), \
                patch.object(aichatdata, 'carbon_query', return_value={'status': 'available'}), \
                patch.object(aichatdata, 'production_query', return_value={'status': 'no_records'}), \
                self.assertLogs(aichatdata.logger, level='ERROR'):
            result = self.build('2026年6月能耗')
        self.assertEqual([s['status'] for s in result['sections']], ['failed', 'available', 'no_records'])
        self.assertEqual(result['status'], 'partial')

    def test_catalog_failure_is_explicit(self):
        with patch.object(aichatdata, 'load_catalog', side_effect=mysql.connector.Error('test')), \
                self.assertLogs(aichatdata.logger, level='ERROR'):
            result = aichatdata.build_chat_context(3, [3], ['ai_analysis'], '2026年能耗')
        self.assertEqual(result['status'], 'failed')

    def test_no_ai_permission_does_not_query(self):
        with patch.object(aichatdata, 'load_catalog') as catalog:
            result = aichatdata.build_chat_context(3, [3], [], '2026年能耗')
        self.assertEqual(result['status'], 'forbidden')
        catalog.assert_not_called()

    def test_market_requires_cost_permission_before_reading(self):
        with patch.object(aichatdata, 'market_query') as query:
            result = self.build('2026年碳价行情')
        query.assert_not_called()
        self.assertEqual(result['sections'][0]['status'], 'forbidden')

    def test_authorized_market_query(self):
        with patch.object(aichatdata, 'market_query', return_value={'status': 'no_records'}) as query:
            self.build('2026年碳价行情', ('ai_analysis', 'ai_cost_analysis'))
        query.assert_called_once()

    def test_cost_denied_but_energy_is_available(self):
        with patch.object(aichatdata, 'energy_query', return_value={'status': 'available'}), \
                patch.object(aichatdata, 'carbon_query', return_value={'status': 'available'}), \
                patch.object(aichatdata, 'production_query', return_value={'status': 'available'}), \
                patch.object(aichatdata, 'cost_query') as cost:
            result = self.build('2026年6月能耗费用')
        cost.assert_not_called()
        self.assertEqual(result['sections'][-1]['status'], 'forbidden')

    def test_cost_capability_does_not_override_business_route(self):
        with patch.object(aichatdata, 'load_catalog', return_value=CATALOG), \
                patch.object(aichatdata, 'market_query') as query:
            result = aichatdata.build_chat_context(3, [3], ['ai_analysis', 'ai_cost_analysis'],
                                                  '2026年碳价行情', web_routes=['/space/production'])
        query.assert_not_called()
        self.assertEqual(result['sections'][0]['status'], 'forbidden')

    def test_month_comparison_reads_both_windows(self):
        with patch.object(aichatdata, 'energy_query', return_value={'status': 'available'}) as query, \
                patch.object(aichatdata, 'carbon_query', return_value={'status': 'available'}), \
                patch.object(aichatdata, 'production_query', return_value={'status': 'available'}):
            result = self.build('2026年6月能耗环比')
        self.assertEqual(query.call_count, 2)
        self.assertEqual(result['sections'][1]['period_start'], '2026-05-01')

    def test_meter_hourly_query_uses_type_specific_table(self):
        for kind, table, column in [('online', 'tbl_meter_hourly', 'meter_id'),
                                    ('offline', 'tbl_offline_meter_hourly', 'offline_meter_id'),
                                    ('virtual', 'tbl_virtual_meter_hourly', 'virtual_meter_id')]:
            cursor = MagicMock()
            cursor.fetchall.side_effect = [[(1, '电', 'kWh', Decimal('.1229'), Decimal('.4419'))],
                                          [(datetime.datetime(2026, 5, 31, 16), Decimal(10))]]
            manager = MagicMock()
            manager.__enter__.return_value = cursor
            scope = {'space_id': 3, 'period_start': '2026-06-01', 'period_end': '2026-06-30',
                     'meter': {'type': kind, 'id': 12, 'category_id': 1}}
            with patch.object(aichatdata, 'db_cursor', return_value=manager):
                categories = aichatdata._energy(scope)
            sql, params = cursor.execute.call_args.args
            self.assertIn('FROM ' + table, sql)
            self.assertIn(column + '=%s', sql)
            self.assertEqual(params[0], 12)
            self.assertEqual(categories[0]['total'], 10)

    def test_footprint_year_and_enterprise_filter(self):
        scope = {'enterprise_space_id': 3, 'product_ids': [3], 'year': 2026}
        with patch.object(aichatdata, 'fetch', return_value=[]) as fetch:
            result = aichatdata.footprint_query(scope)
        sql, params = fetch.call_args.args[1:]
        self.assertIn('f.enterprise_space_id=%s', sql)
        self.assertEqual(params, (3, 3, 2026, 'active'))
        self.assertEqual(result['status'], 'no_records')

    def test_evaluation_reuses_page_builder(self):
        scope = {'enterprise_space_id': 3, 'space_id': 3, 'period_start': '2026-06-01',
                 'period_end': '2026-06-30', 'products': CATALOG['products']}
        report = {'summary': {'total_production': 2163}, 'evaluations': [{'grade_code': 'test'}],
                  'energy_categories': [], 'trends': {}}
        with patch.object(aichatdata, '_production', return_value={'status': 'partial'}), \
                patch.object(aichatdata, '_energy', return_value=[]), \
                patch.object(aichatdata, '_build_report', return_value=report) as builder:
            result = aichatdata.evaluation_query(scope)
        self.assertEqual(result['products'][0]['evaluations'], report['evaluations'])
        self.assertEqual(builder.call_args.args[2], 3)


class ChatContextTests(unittest.TestCase):
    def snapshot(self):
        return {'status': 'partial', 'scope': {
            'enterprise_space_id': 3, 'space_id': 3, 'product_ids': [3]},
            'sections': [{'type': 'energy', 'status': 'partial', 'data': {
                'total': Decimal('993200.00'), 'source': 'test', 'daily': [{'value': 1}],
                'coverage': {'observed_days': 14, 'requested_days': 30}}}]}

    def test_persistent_summary_retains_totals_not_full_series(self):
        summary = context_summary(self.snapshot())
        data = summary['sections'][0]['data']
        self.assertEqual(data['total'], '993200.00')
        self.assertNotIn('daily', data)
        self.assertEqual(data['coverage']['observed_days'], 14)

    def test_permission_revalidation(self):
        snapshot = self.snapshot()
        self.assertTrue(context_is_visible(snapshot, 3, ['ai_analysis'], CATALOG))
        self.assertFalse(context_is_visible(snapshot, 5, ['ai_analysis'], CATALOG))
        self.assertFalse(context_is_visible(snapshot, 3, [], CATALOG))
        self.assertFalse(context_is_visible(snapshot, 3, ['ai_analysis'], {**CATALOG, 'products': []}))

    def test_stale_clarification_is_hidden(self):
        snapshot = {'status': 'input_required', 'field': 'space_id',
                    'options': [{'value': 99, 'label': 'revoked'}]}
        self.assertFalse(context_is_visible(snapshot, 3, ['ai_analysis'], CATALOG))

    def test_unresolved_space_clarification_survives_history_reload(self):
        snapshot = {'status': 'input_required', 'field': 'space_id',
                    'scope': {'enterprise_space_id': 3, 'intents': ['energy']},
                    'options': [{'value': 3, 'label': 'Space'}]}
        self.assertTrue(context_is_visible(snapshot, 3, ['ai_analysis'], CATALOG))

    def test_revoked_cost_evidence_is_hidden(self):
        snapshot = self.snapshot()
        snapshot['sections'][0]['type'] = 'cost'
        self.assertFalse(context_is_visible(snapshot, 3, ['ai_analysis'], CATALOG))

    def test_revoked_business_route_hides_answer(self):
        snapshot = self.snapshot()
        snapshot['sections'][0]['required_routes'] = ['/space/production']
        self.assertFalse(context_is_visible(snapshot, 3, ['ai_analysis'], CATALOG, []))
        self.assertTrue(context_is_visible(snapshot, 3, ['ai_analysis'], CATALOG, ['/space/production']))

    def test_model_receives_authoritative_current_data_and_calculation_policy(self):
        messages = chat_messages('分析', self.snapshot())
        self.assertIn('允许根据已有数据自行补算', messages[0]['content'])
        self.assertIn('993200.00', messages[1]['content'])
        self.assertEqual(messages[-1], {'role': 'user', 'content': '分析'})
        self.assertIn('不是排放因子', messages[0]['content'])
        self.assertIn('总量不能再次乘以能源用量', messages[0]['content'])

    def test_system_data_off_has_no_snapshot_or_old_history(self):
        messages = chat_messages('你好')
        self.assertEqual(len(messages), 2)
        self.assertIn('未启用系统数据查询', messages[0]['content'])
        self.assertNotIn('993200', str(messages))


class ChatAPITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(aiassistant.config.ai_assistant, {
            'chat_data_enabled': True, 'chat_timeout_seconds': 180, 'chat_disable_thinking': True}))
        self.stack.enter_context(patch.object(aiassistant, '_request_context', return_value=(4, 3)))
        self.stack.enter_context(patch.object(aiassistant, 'get_request_context_value',
                                             side_effect=lambda req, key, default=None: {
                                                 'authorized_space_ids': [3],
                                                 'ai_capabilities': ['ai_analysis']}.get(key, default)))
        self.stack.enter_context(patch.object(aiassistant, '_conversation_row', return_value=(
            9, 'New conversation', datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 1))))
        self.stack.enter_context(patch.object(aiassistant, '_report_input_requirements', return_value=None))
        self.stack.enter_context(patch.object(aiassistant, '_attachment_context', return_value=None))
        self.stack.enter_context(patch.object(aiassistant, 'previous_scope', return_value={}))
        self.save = self.stack.enter_context(patch.object(aiassistant, 'save_context'))
        self.audit = self.stack.enter_context(patch.object(aiassistant, '_write_audit'))
        self.cursor = MagicMock()
        self.cursor.fetchone.return_value = (0,)
        self.cursor.fetchall.return_value = [('assistant', '旧数据不得进入新请求')]
        self.cursor.lastrowid = 101
        connection = MagicMock()
        connection.cursor.return_value = self.cursor
        self.stack.enter_context(patch.object(aiassistant.mysql.connector, 'connect', return_value=connection))
        self.build = self.stack.enter_context(patch.object(aiassistant, 'build_chat_context',
                                                         return_value=ChatContextTests().snapshot()))
        self.settings = self.stack.enter_context(patch.object(aiassistant, '_provider_settings',
                                                              return_value={'provider': 'test', 'default_model': 'test'}))
        self.complete = self.stack.enter_context(patch.object(aiassistant, '_stream_completion',
                                                              return_value=iter(['已分析'])))
        app = falcon.App()
        app.add_route('/chat/{id_}', aiassistant.AIAssistantChat())
        app.add_route('/conversation/{id_}', aiassistant.AIAssistantConversationItem())
        self.client = testing.TestClient(app)

    def post(self, **extra):
        return self.client.simulate_post('/chat/9', json={
            'content': '2026年6月能耗', 'use_system_data': True, **extra})

    def test_stream_and_evidence_persistence_are_wired(self):
        result = self.post()
        self.assertEqual(result.status_code, 200)
        self.assertIn('event: data_context', result.text)
        self.assertIn('event: done', result.text)
        self.assertEqual(self.save.call_count, 2)
        messages = self.complete.call_args.args[1]
        self.assertIn('993200.00', str(messages))
        self.assertNotIn('旧数据', str(messages))
        effective_settings = self.complete.call_args.args[0]
        self.assertEqual(effective_settings['timeout_seconds'], 180)
        self.assertTrue(effective_settings['disable_thinking'])
        self.assertNotIn('disable_thinking', self.settings.return_value)
        self.assertIn('"timeout_seconds": 180', result.text)

    def test_clarification_does_not_call_external_provider(self):
        self.build.return_value = {'status': 'input_required', 'field': 'product_id',
                                   'message': '请选择产品', 'options': [{'value': 3, 'label': '产品'}]}
        result = self.post(chat_selection={'space_id': 3})
        self.assertIn('event: chat_input_required', result.text)
        self.assertIn('请选择产品', result.text)
        self.settings.assert_not_called()
        self.complete.assert_not_called()
        self.assertEqual(self.build.call_args.args[-1], {'space_id': 3})

    def test_disabled_feature_is_explicit(self):
        with patch.dict(aiassistant.config.ai_assistant, {'chat_data_enabled': False}):
            result = self.post()
        self.assertIn('AI_CHAT_DATA_ENABLED', result.text)
        self.build.assert_not_called()
        self.complete.assert_not_called()
        self.save.assert_not_called()

    def test_system_data_off_bypasses_lookup_and_saved_context(self):
        result = self.post(use_system_data=False)
        self.assertIn('event: done', result.text)
        self.build.assert_not_called()
        self.save.assert_not_called()
        self.assertNotIn('旧数据', str(self.complete.call_args.args[1]))

    def test_provider_failure_is_error_not_success(self):
        self.complete.side_effect = aiassistant.requests.RequestException('test')
        with self.assertLogs(aiassistant.logger, level='ERROR'):
            result = self.post()
        self.assertIn('event: error', result.text)
        self.assertNotIn('event: done', result.text)
        self.assertEqual(self.save.call_count, 1)

    def test_foreign_conversation_is_rejected_before_query(self):
        with patch.object(aiassistant, '_conversation_row', return_value=None):
            result = self.post()
        self.assertEqual(result.status_code, 404)
        self.build.assert_not_called()

    def test_history_restores_evidence_and_hides_revoked_scope(self):
        rows = [(101, 'assistant', 'analysis', 'test', 1, None, datetime.datetime(2026, 1, 1))]
        snapshot = context_summary(ChatContextTests().snapshot())
        with patch.object(aiassistant, 'load_catalog', return_value=CATALOG):
            self.cursor.fetchall.side_effect = [rows, [(101, json.dumps(snapshot))]]
            result = self.client.simulate_get('/conversation/9')
        self.assertEqual(result.json['messages'][0]['data_context'], snapshot)
        with patch.object(aiassistant, 'load_catalog', return_value={**CATALOG, 'products': []}):
            self.cursor.fetchall.side_effect = [rows, [(101, json.dumps(snapshot))]]
            result = self.client.simulate_get('/conversation/9')
        self.assertEqual(result.json['messages'][0]['data_context']['status'], 'unavailable')
        self.assertNotIn('analysis', result.json['messages'][0]['content'])

    def test_report_generation_remains_on_report_pipeline(self):
        template = {'id': 1, 'name': 'test', 'version_id': 1, 'version_number': 1}
        with patch.object(aiassistant, 'get_published_template', return_value=template), \
                patch.object(aiassistant, '_system_data_message', return_value=None), \
                patch.object(aiassistant, 'build_report_data_context', return_value={}) as report_data, \
                patch.object(aiassistant, 'report_instruction', return_value={'role': 'system', 'content': 'report'}):
            result = self.post(report={'type': 'ghg_emissions', 'mode': 'real_data'},
                               report_context={'space_id': 3, 'period_start': '2026-06-01',
                                               'period_end': '2026-06-30'})
        self.assertIn('event: report_done', result.text)
        report_data.assert_called_once()
        self.build.assert_not_called()
        self.save.assert_not_called()
        self.assertNotIn('disable_thinking', self.complete.call_args.args[0])
        self.assertEqual(self.complete.call_args.args[0], self.settings.return_value)

    def test_invalid_json_shape_is_bad_request(self):
        result = self.client.simulate_post('/chat/9', json=['not an object'])
        self.assertEqual(result.status_code, 400)
        self.build.assert_not_called()

    def test_provider_timeout_is_explicit_and_not_persisted_as_success(self):
        self.complete.side_effect = aiassistant.requests.Timeout('deadline exceeded')
        with self.assertLogs(aiassistant.logger, level='ERROR'):
            result = self.post()
        self.assertIn('AI_PROVIDER_TIMEOUT', result.text)
        self.assertNotIn('event: done', result.text)
        self.assertEqual(self.save.call_count, 1)

    def test_unusable_source_states_reply_locally_without_model(self):
        for status in ('no_records', 'forbidden', 'failed'):
            with self.subTest(status=status):
                self.build.return_value = {'status': status, 'message': '本轮没有可用于分析的数据。'}
                result = self.post()
                self.assertIn('event: done', result.text)
                self.assertIn('"status": "' + status + '"', result.text)
                self.settings.assert_not_called()
                self.complete.assert_not_called()


class ProviderStreamTests(unittest.TestCase):
    def setUp(self):
        self.settings = {'provider': 'ark_responses', 'base_url': 'http://localhost',
                         'default_model': 'synthetic', 'api_key': 'synthetic', 'timeout_seconds': 60}
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.post = patch.object(aiassistant.requests, 'post', return_value=self.response).start()
        self.addCleanup(patch.stopall)

    def events(self, *payloads):
        self.response.iter_lines.return_value = iter(
            b'data: ' + json.dumps(payload).encode() for payload in payloads)

    def test_ark_completion_terminates_without_waiting_for_socket_eof(self):
        def lines():
            yield b'data: {"type":"response.output_text.delta","delta":"answer"}'
            yield b'data: {"type":"response.completed","response":{"status":"completed"}}'
            raise AssertionError('Must not read beyond the terminal event')
        self.response.iter_lines.return_value = lines()
        self.assertEqual(list(aiassistant._stream_ark_response(self.settings, [])), ['answer'])
        self.response.__exit__.assert_called_once()

    def test_ark_incomplete_is_not_success(self):
        self.events({'type': 'response.output_text.delta', 'delta': 'partial'},
                    {'type': 'response.incomplete', 'response': {'status': 'incomplete'}})
        with self.assertRaises(aiassistant.requests.RequestException):
            list(aiassistant._stream_ark_response(self.settings, []))

    def test_ark_premature_eof_is_not_success(self):
        self.events({'type': 'response.output_text.delta', 'delta': 'partial'})
        with self.assertRaises(aiassistant.requests.RequestException):
            list(aiassistant._stream_ark_response(self.settings, []))

    def test_heartbeat_does_not_extend_generation_deadline(self):
        self.response.iter_lines.return_value = iter([b': heartbeat', b': heartbeat'])
        with patch.object(aiassistant.time, 'monotonic', side_effect=[0, 10, 61]):
            with self.assertRaises(aiassistant.requests.Timeout):
                list(aiassistant._stream_ark_response(self.settings, []))
        self.response.__exit__.assert_called_once()

    def test_reasoning_emits_only_progress_not_internal_text(self):
        self.events({'type': 'response.reasoning_summary_text.delta', 'delta': 'private reasoning'},
                    {'type': 'response.output_text.delta', 'delta': 'answer'},
                    {'type': 'response.completed'})
        result = list(aiassistant._stream_ark_response(self.settings, []))
        self.assertEqual(result[0].stage, 'model_reasoning')
        self.assertNotIn('private reasoning', str(result))
        self.assertEqual(result[-1], 'answer')

    def test_openai_terminal_and_response_cleanup(self):
        self.settings['provider'] = 'openai_compatible'
        self.events({'choices': [{'delta': {'content': 'answer'}, 'finish_reason': 'stop'}]})
        self.assertEqual(list(aiassistant._stream_completion(self.settings, [])), ['answer'])
        self.response.__exit__.assert_called_once()
        self.assertNotIn('thinking', self.post.call_args.kwargs['json'])

    def test_ark_disables_thinking_only_when_explicitly_selected(self):
        self.settings['disable_thinking'] = True
        self.events({'type': 'response.completed'})
        list(aiassistant._stream_ark_response(self.settings, []))
        self.assertEqual(self.post.call_args.kwargs['json']['thinking'], {'type': 'disabled'})
        self.settings.pop('disable_thinking')
        self.events({'type': 'response.completed'})
        list(aiassistant._stream_ark_response(self.settings, []))
        self.assertNotIn('thinking', self.post.call_args.kwargs['json'])

    def test_malformed_provider_event_is_error(self):
        self.response.iter_lines.return_value = iter([b'data: not-json'])
        with self.assertRaises(aiassistant.requests.RequestException):
            list(aiassistant._stream_ark_response(self.settings, []))


class ProviderSocketTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.send_completed = False
        self.silent = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                try:
                    if owner.send_completed:
                        self.wfile.write(
                            b'data: {"type":"response.output_text.delta","delta":"synthetic"}\n\n'
                            b'data: {"type":"response.completed"}\n\n')
                        self.wfile.flush()
                    while not owner.stop.wait(.02):
                        if not owner.silent:
                            self.wfile.write(b': heartbeat\n\n')
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # The client closes a completed or timed-out stream.
                self.close_connection = True

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.close_server)
        self.settings = {'base_url': 'http://127.0.0.1:' + str(self.server.server_port),
                         'default_model': 'synthetic', 'api_key': 'synthetic', 'timeout_seconds': .2}

    def close_server(self):
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def test_real_socket_heartbeats_cannot_keep_request_alive(self):
        start = time.monotonic()
        with self.assertRaises(aiassistant.requests.Timeout):
            list(aiassistant._stream_ark_response(self.settings, []))
        self.assertLess(time.monotonic() - start, 1)

    def test_real_socket_completion_does_not_require_http_close(self):
        self.send_completed = True
        self.silent = True
        self.settings['timeout_seconds'] = 2
        start = time.monotonic()
        result = list(aiassistant._stream_ark_response(self.settings, []))
        self.assertEqual(result, ['synthetic'])
        self.assertLess(time.monotonic() - start, 1)

    def test_real_socket_silence_is_bounded(self):
        self.silent = True
        start = time.monotonic()
        with self.assertRaises(aiassistant.requests.Timeout):
            list(aiassistant._stream_ark_response(self.settings, []))
        self.assertLess(time.monotonic() - start, 1)


if __name__ == '__main__':
    unittest.main()
