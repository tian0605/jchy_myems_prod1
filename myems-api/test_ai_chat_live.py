"""Opt-in model acceptance using synthetic data only; no business queries or writes."""
import json
import os
from pathlib import Path
import time
import unittest

from core import aiassistant
import test_ai_chat as api_fixture


def synthetic_cases():
    product = {'id': 201, 'space_id': 101, 'name': '虚构产品', 'unit': 't'}
    scope = {'enterprise_space_id': 101, 'space_id': 101, 'space_name': '虚构验收企业',
             'product_ids': [201], 'products': [product], 'year': 2026,
             'period_start': '2026-06-01', 'period_end': '2026-06-30', 'timezone_offset': '+08:00'}

    def series(total, unit, values, requested=30):
        return {'status': 'partial', 'total': total, 'unit': unit, 'source': 'synthetic acceptance fixture',
                'daily': [{'date': '2026-06-%02d' % (i + 1), 'value': value} for i, value in enumerate(values)],
                'coverage': {'observed_days': len(values), 'requested_days': requested,
                             'missing_dates': ['2026-06-%02d' % day for day in range(len(values) + 1, requested + 1)]}}

    def snapshot(sections, selected_scope=None):
        return {'schema_version': 1, 'status': 'partial', 'scope': selected_scope or scope,
                'sections': [{'type': key, 'status': 'partial', 'data': value} for key, value in sections]}

    energy = {'products': [{'product': product, 'current_factor_carbon_kgco2e': 7000,
                            'energy_categories': [series(14000, 'kWh', [1000] * 14)]}]}
    carbon = {'products': [{'product': product, 'recorded_carbon_kgco2e': 7700,
                            'categories': [series(7700, 'kgCO2e', [550] * 14)]}]}
    yield 'A', '分析6月能耗和两种碳排口径，补算单位产量能耗、已存排放减当前因子折算排放的差额，及相对折算排放总量的差异百分比。', \
        snapshot([('energy', energy), ('carbon', carbon),
                  ('production', {'products': [series(140, 't', [10] * 14)]})]), \
        {'unit_energy_kwh_per_t': 100, 'carbon_difference_kgco2e': 700,
         'carbon_difference_percent': 10, 'observed_days': 14}

    yield 'B', '分析2026年产品碳足迹、制造阶段贡献及缺失阶段。', snapshot([
        ('footprint', {'source': 'synthetic acceptance fixture', 'footprints': [{
            'record_id': 1, 'product_id': 201, 'accounting_year': 2026, 'production_quantity': 10000,
            'functional_unit': 'kg', 'display_unit': 'kgCO2e/kg', 'footprint_per_functional_unit': .6,
            'period_start': '2026-01-01', 'period_end': '2026-06-30', 'recorded_activity_total': 6000,
            'stages': [{'name': '原材料', 'emission_amount_as_recorded': 1000},
                       {'name': '制造', 'emission_amount_as_recorded': 4000},
                       {'name': '运输', 'emission_amount_as_recorded': 1000}],
            'missing_stages': ['使用', '报废'], 'activities': []}]})]), \
        {'intensity_kgco2e_per_kg': .6, 'manufacturing_share_percent': 66.67, 'missing_stage_count': 2}

    yield 'C', '分析单位产量能耗，补算总强度，单列第三日产量为零时的日强度。', snapshot([
        ('energy', {'products': [{'product': product, 'energy_categories': [
            series(750, 'kWh', [100, 600, 50], 3)]}]}),
        ('production', {'products': [series(40, 't', [10, 30, 0], 3)]})]), \
        {'total_intensity_kwh_per_t': 18.75, 'third_day_intensity_kwh_per_t': None}

    yield 'D', '解释节能评价等级、规则来源并补算相对基准的差异百分比。', snapshot([
        ('evaluation', {'source': 'synthetic acceptance fixture', 'products': [{
            'product': product, 'evaluations': [{
                'status': 'matched', 'metric_unit': 'tce/t', 'actual_value': .12,
                'benchmark_value': .1, 'gap_rate': .2, 'grade_code': 'C',
                'benchmark_source': '虚构企业内部基准', 'rule_set_id': 1,
                'effective_date': '2026-01-01', 'expiry_date': '2026-12-31',
                'min_value': .1, 'max_value': .2, 'min_inclusive': True, 'max_inclusive': False}]}]})]), \
        {'gap_percent': 20, 'grade_code': 'C'}

    yield 'E', '核对6月1日至3日产量录入：总量、已录入零值天数和未记录天数。', snapshot([
        ('production', {'products': [series(10, 't', [0, 10], 3)],
                        'note': '只读记录；缺失日期不当作零产量。'})]), \
        {'total_production_t': 10, 'recorded_zero_days': 1, 'missing_days': 1}

    meter = {'id': 301, 'key': 'online:301', 'type': 'online', 'name': '虚构表'}
    yield 'F', '分析计量表区间能耗，说明能否把原始累计读数加进区间能耗。', snapshot([
        ('meter', {'meter': meter, 'energy_categories': [series(240, 'kWh', [240], 1)],
                   'points': [{'id': 901, 'unit': 'kWh', 'daily': [
                       {'date': '2026-06-01', 'samples': 1, 'min': 9000, 'max': 9000, 'mean': 9000}],
                               'note': '原始累计读数，不是区间用量，不能加进能耗。'}]})],
        {**scope, 'meter': meter, 'meter_key': meter['key']}), \
        {'interval_energy_kwh': 240, 'add_cumulative_reading': False}

    yield 'G', '分析年度碳资产：配额、CCER、绿证和页面余额，说明是否已经验证法定履约量。', snapshot([
        ('assets', {'assets': [{'asset': {'quota_total': 100, 'ccer_total': 20, 'green_certificate_total': 5},
                                'quota_and_ccer_unit': 'tCO2e', 'green_certificate_unit': 'certificate',
                                'recorded_emissions_tco2e': 110, 'page_balance_tco2e': 10}],
                    'emissions': {'status': 'partial', 'total': 110000, 'unit': 'kgCO2e',
                                  'coverage': {'observed_days': 1, 'requested_days': 365}},
                    'note': '年度排放覆盖不全；配额+CCER-排放是页面口径，不是已核实的法定可履约量；绿证单列。'})]), \
        {'page_balance_tco2e': 10, 'green_certificates': 5, 'verified_compliance_amount': None}


@unittest.skipUnless(os.environ.get('AI_CHAT_LIVE_TESTS') == '1', 'Explicit opt-in required for model calls')
class SyntheticModelAcceptance(unittest.TestCase):
    def test_seven_scenarios_three_times(self):
        settings = aiassistant._provider_settings()
        completion = aiassistant._stream_completion
        output = Path(__file__).resolve().parent.parent / '.local' / 'ai-chat-synthetic-acceptance.jsonl'
        output.parent.mkdir(exist_ok=True)
        with output.open('w', encoding='utf-8') as log:
            for scenario, question, snapshot, expected in synthetic_cases():
                for repeat in range(1, 4):
                    with self.subTest(scenario=scenario, repeat=repeat):
                        fixture = api_fixture.ChatAPITests()
                        fixture.setUp()
                        fixture.settings.return_value = settings
                        fixture.build.return_value = snapshot
                        started = time.monotonic()
                        timing = {}

                        def stream(effective, messages):
                            for item in completion(effective, messages):
                                if isinstance(item, str):
                                    timing.setdefault('first_text_seconds', round(time.monotonic() - started, 3))
                                yield item

                        fixture.complete.side_effect = stream
                        result = {'scenario': scenario, 'repeat': repeat}
                        try:
                            request = (question + '全部是虚构测试数据。用150至300字给出有依据的分析与必要限制。'
                                       '最后单独一行输出 CHECK_JSON= 后接JSON对象，字段为：'
                                       + ', '.join(expected) + '。数值不带千位分隔符，百分比保留两位小数，'
                                       '不可计算的值用null，真假用JSON布尔值。')
                            response = fixture.post(content=request)
                            text = ''.join(json.loads(event.split('data: ', 1)[1])['content']
                                           for event in response.text.split('\n\n')
                                           if event.startswith('event: delta\n'))
                            result.update(text=text, **timing, elapsed_seconds=round(time.monotonic() - started, 3),
                                          completed='event: done' in response.text and 'event: error' not in response.text)
                            log.write(json.dumps(result, ensure_ascii=False) + '\n')
                            log.flush()
                            self.assertTrue(result['completed'])
                            self.assertEqual(fixture.save.call_count, 2)
                            self.assertIn('CHECK_JSON=', text)
                            actual, _ = json.JSONDecoder().raw_decode(text.split('CHECK_JSON=', 1)[1].strip())
                            for key, value in expected.items():
                                self.assertIn(key, actual)
                                if type(value) in (int, float):
                                    self.assertIsInstance(actual[key], (int, float))
                                    self.assertNotIsInstance(actual[key], bool)
                                    self.assertLessEqual(abs(actual[key] - value), .005, key)
                                else:
                                    self.assertEqual(actual[key], value, key)
                        finally:
                            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
