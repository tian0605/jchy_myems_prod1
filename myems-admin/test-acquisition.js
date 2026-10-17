'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function fixture(user = {uuid: 'synthetic-user', token: 'synthetic-token'}) {
    let controller;
    const requests = [];
    const scope = {};
    function request(method, url, data, options) {
        let resolve, reject;
        const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
        requests.push({method, url, data, options, resolve, reject});
        return promise;
    }
    vm.runInNewContext(fs.readFileSync(path.join(__dirname,
        'app', 'controllers', 'settings', 'datasource', 'acquisition.controller.js'), 'utf8'), {
        app: {controller: (name, fn) => { controller = fn; }},
        getAPI: () => '/api/'
    });
    controller(scope, {localStorage: {getItem: () => JSON.stringify(user)}, crypto: {
        randomUUID: () => '126f669b-50d0-493c-8262-004f276e2f59'
    }}, {
        get: (url, options) => request('GET', url, null, options),
        post: (url, data, options) => request('POST', url, data, options)
    }, {when: () => Promise.resolve(), all: promises => Promise.all(promises)}, {instant: key => key});
    return {scope, state: scope.acquisition, requests};
}

async function settle() {
    await new Promise(resolve => setImmediate(resolve));
}

test('business jobs load their own source-scoped status and gate columns', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'business';
    f.scope.acquisitionReset();
    assert.equal(f.requests[1].url, '/api/datasources/4/acquisition/business');
    f.requests[1].resolve({data: {items: [{id: 1, run_id: 13, status: 'blocked',
        gate_code: 'five_quarter_hour_readings_including_end_boundary_required'}], has_more: false}});
    await settle();
    assert.ok(f.state.columns.includes('gate_code'));
    assert.ok(f.state.columns.includes('hour_insert_count'));
    assert.equal(f.scope.acquisitionGateLabel(null), '-');
    assert.equal(f.scope.acquisitionGateLabel('unknown_future_gate'), 'unknown_future_gate');
    assert.equal(f.state.rows[0].status, 'blocked');
    const template = fs.readFileSync(path.join(__dirname, 'views', 'settings', 'datasource', 'acquisition.html'), 'utf8');
    assert.ok(template.includes('value="business"'));
    assert.ok(template.includes('ACQUISITION.BUSINESS_NOTICE'));
    assert.ok(template.includes('<button type="button" class="btn btn-primary m-t-md" ng-if="acquisition.collection === \'tasks\'"'));
});

test('task completion summary and run actual execution times remain distinct from reading windows', async () => {
    const f = fixture();
    f.state.source = '4';
    f.scope.acquisitionReset();
    f.requests[1].resolve({data: {items: [], has_more: false}});
    await settle();
    assert.ok(f.state.columns.includes('last_success_finished_datetime_utc'));
    assert.ok(f.state.columns.includes('latest_run_status'));
    assert.ok(f.state.columns.includes('latest_run_finished_datetime_utc'));
    assert.equal(f.scope.acquisitionDisplayValue('latest_run_status', 'error'), 'error');
    assert.equal(f.scope.acquisitionDisplayValue('latest_run_status', null), '-');
    assert.equal(f.scope.acquisitionDisplayValue('last_success_finished_datetime_utc', '2026-10-17T15:40:03'),
        '2026-10-17 23:40:03 UTC+8');
    assert.equal(f.scope.acquisitionDisplayValue('finished_datetime_utc', null), '-');
    f.state.collection = 'runs';
    f.scope.acquisitionReset();
    f.requests[2].resolve({data: {items: [], has_more: false}});
    await settle();
    for (const column of ['window_start_utc', 'window_end_utc', 'started_datetime_utc', 'finished_datetime_utc']) {
        assert.ok(f.state.columns.includes(column));
    }
});

test('business retry uses explicit confirmation and source-scoped control endpoint', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'business';
    f.scope.acquisitionConfirm({id: 3, status: 'blocked'}, 'retry_business');
    assert.equal(f.requests.length, 1);
    f.scope.acquisitionExecute();
    assert.equal(f.requests[1].url, '/api/datasources/4/acquisition/business/3/control');
    assert.equal(f.requests[1].data.action, 'retry_business');
    f.requests[1].resolve({data: {action: 'retry_business'}});
    await settle();
    assert.equal(f.requests[2].url, '/api/datasources/4/acquisition/business');
});

test('energy run detail displays raw registers while archive results retain archive columns', async () => {
    const f = fixture();
    f.state.source = '4';
    f.scope.acquisitionItems({id: 11});
    f.requests[1].resolve({data: {items: [], has_more: false, detail_kind: 'energy_current_window'}});
    await settle();
    assert.equal(f.state.energyDetail, true);
    assert.ok(f.state.columns.includes('forward_active_register'));
    assert.ok(!f.state.columns.includes('meter_id'));
    f.scope.acquisitionItems({id: 10});
    f.requests[2].resolve({data: {items: [], has_more: false, detail_kind: 'archive_items'}});
    await settle();
    assert.equal(f.state.energyDetail, false);
    assert.ok(f.state.columns.includes('meter_id'));
    assert.ok(!f.state.columns.includes('forward_active_register'));
});

test('source-wide readings include names and separate verified phase statuses with UTC+8 notes', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'readings';
    f.scope.acquisitionReset();
    assert.equal(f.requests[1].url, '/api/datasources/4/acquisition/readings');
    const details = [{start_utc: '2026-10-16T16:00:00+00:00', end_utc: '2026-10-16T17:00:00+00:00',
        energy: {status: 'processed', reason: null},
        settlement: {status: 'blocked', reason: 'settlement:exactly_one_applicable_hourly_tariff_required'},
        dimensions: {status: 'pending', reason: 'settlement_processing_required'}}];
    f.requests[1].resolve({data: {items: [{id: 30, meter_name: '<script>', processing_details: details}],
        has_more: true, next_before_id: 30}});
    await settle();
    for (const name of ['meter_name', 'quality', 'energy_status', 'settlement_status', 'dimensions_status',
        'processing_details', 'first_run_id', 'last_run_id']) {
        assert.ok(f.state.columns.includes(name));
    }
    assert.equal(f.scope.acquisitionDisplayValue('energy_status', 'partial'), 'ACQUISITION.PROCESSING_partial');
    const displayed = f.scope.acquisitionDisplayValue('processing_details', details);
    assert.ok(displayed.includes('2026-10-17 00:00:00 UTC+8'));
    assert.ok(displayed.includes('ACQUISITION.PROCESSING_blocked'));
    assert.ok(displayed.includes('exactly_one_applicable_hourly_tariff_required'));
    f.scope.acquisitionNext();
    assert.equal(f.requests[2].options.params.before_id, 30);
    f.requests[2].resolve({data: {items: [], has_more: false}});
    await settle();
    f.state.source = '2';
    f.scope.acquisitionReset();
    assert.equal(f.requests[3].url, '/api/datasources/2/acquisition/readings');
    assert.equal(f.requests[3].options.params.before_id, undefined);
    const template = fs.readFileSync(path.join(__dirname, 'views', 'settings', 'datasource', 'acquisition.html'), 'utf8');
    assert.ok(template.includes('value="readings"'));
    assert.ok(template.includes('ACQUISITION.PROCESSING_DETAILS'));
    assert.ok(!template.includes('ng-bind-html'));
});

test('quality labels and every processing state have translated explanations and versioned bundles', () => {
    const f = fixture();
    for (const value of ['valid', 'invalid', 'conflict']) {
        assert.equal(f.scope.acquisitionDisplayValue('quality', value), 'ACQUISITION.QUALITY_' + value);
    }
    assert.equal(f.scope.acquisitionDisplayValue('quality', 'future_quality'), 'future_quality');
    assert.equal(f.scope.acquisitionDisplayValue('quality', null), '-');
    const sandbox = {window: {}};
    for (const language of ['en', 'zh_CN', 'zh_TW']) {
        vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'app', 'translations', language + '.js'), 'utf8'), sandbox);
        const bundle = sandbox.window.myemsAdminTranslations[language].ACQUISITION;
        for (const value of ['valid', 'invalid', 'conflict']) {
            assert.ok(bundle['QUALITY_' + value]);
        }
        for (const status of ['processed', 'partial', 'pending', 'queued', 'running', 'blocked', 'error',
            'review_required', 'not_applicable']) {
            assert.ok(bundle['PROCESSING_' + status]);
            assert.ok(bundle['STATUS_HELP_' + status]);
        }
        assert.ok(bundle.STATUS_GUIDE);
        assert.ok(bundle.QUALITY_GUIDE);
    }
    assert.equal(sandbox.window.myemsAdminTranslations.zh_CN.ACQUISITION.QUALITY_valid, '有效');
    assert.equal(sandbox.window.myemsAdminTranslations.zh_CN.ACQUISITION.READINGS, '采集接口执行结果');
    const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
    for (const language of ['en', 'zh_CN', 'zh_TW']) {
        assert.ok(html.includes('app/translations/' + language + '.js?v=20261017-execution-times-3'));
    }
    const nginx = fs.readFileSync(path.join(__dirname, 'nginx.conf'), 'utf8');
    assert.match(nginx, /location = \/index\.html\s*\{[^}]*Cache-Control "no-cache"/);
    const template = fs.readFileSync(path.join(__dirname, 'views', 'settings', 'datasource', 'acquisition.html'), 'utf8');
    assert.ok(template.includes('ACQUISITION.STATUS_GUIDE'));
    assert.ok(template.includes('ACQUISITION.STATUS_HELP_'));
});

test('raw mapping enable requires saved range and explicit confirmation, then posts verified snapshot', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    f.scope.acquisitionMappingEdit({id: 9});
    const config = {id: 9, is_enabled: 0, configuration_status: 'pending',
        collection_start_utc: '2026-10-01T00:00:00', collection_end_utc: null};
    f.requests[1].resolve({data: {configuration: config, meter_name: 'Test', point_name: 'Register'}});
    await settle();
    f.scope.acquisitionMappingRawConfirm('enable_raw');
    assert.equal(f.state.pendingAction, null);
    f.state.mappingEditor.confirmKwhDirect = true;
    f.scope.acquisitionMappingRawConfirm('enable_raw');
    assert.equal(f.requests.length, 2);
    assert.equal(f.state.pendingAction.body.confirm_kwh_direct, true);
    assert.equal(JSON.stringify(f.state.pendingAction.body.expected_configuration), JSON.stringify(config));
    f.scope.acquisitionExecute();
    assert.equal(f.requests[2].url, '/api/datasources/4/acquisition/mappings/9/raw-collection');
    assert.equal(f.requests[2].data.action, 'enable_raw');
    f.requests[2].resolve({data: {changed: true, business_activation: false}});
    await settle();
    assert.equal(f.state.mappingEditor, null);
});

test('raw mapping control refuses unsaved range, source changes, wrong action and conflicts', () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    const editor = {id: 9, source: '4', confirmKwhDirect: true, expected: {}, enabled: false};
    f.state.mappingEditor = editor;
    f.scope.acquisitionMappingRawConfirm('enable_raw');
    assert.equal(f.state.pendingAction, undefined);
    editor.expected.collection_start_utc = '2026-10-01T00:00:00';
    for (const action of ['pause_raw', 'activate_business']) {
        f.scope.acquisitionMappingRawConfirm(action);
        assert.equal(f.state.pendingAction, undefined);
    }
    editor.conflict = true;
    f.scope.acquisitionMappingRawConfirm('enable_raw');
    assert.equal(f.state.pendingAction, undefined);
    editor.conflict = false;
    f.state.source = '1';
    f.scope.acquisitionMappingRawConfirm('enable_raw');
    assert.equal(f.state.pendingAction, undefined);
});

test('raw mapping pause is separately confirmed and reports failed control without success fallback', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    f.state.mappingEditor = {id: 9, source: '4', confirmKwhDirect: true, enabled: true,
        expected: {is_enabled: 1, configuration_status: 'ready'}};
    f.scope.acquisitionMappingRawConfirm('pause_raw');
    assert.equal(f.requests.length, 1);
    f.scope.acquisitionExecute();
    assert.equal(f.requests[1].data.action, 'pause_raw');
    f.requests[1].reject({status: 409});
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.RAW_COLLECTION_ERROR');
    assert.equal(f.state.mappingEditor.enabled, true);
    assert.equal(f.state.pendingAction, null);
});

test('mapping range blocks timezone-free input before confirmation and accepts explicit offsets', () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    f.state.mappingEditor = {id: 89, source: '4', expected: {}, end: ''};
    for (const start of ['2026-10-01T00:00:00', 'bad', '2026-10-01T00:01:00Z']) {
        f.state.mappingEditor.start = start;
        f.scope.acquisitionMappingConfirm();
        assert.equal(f.state.pendingAction, null);
        assert.equal(f.state.error, 'ACQUISITION.MAPPING_TIME_ERROR');
        assert.equal(f.requests.length, 1);
    }
    f.state.mappingEditor.start = new Date('2026-10-01T00:00:00+08:00');
    f.scope.acquisitionMappingConfirm();
    assert.equal(f.state.pendingAction.body.collection_start_utc, '2026-10-01T00:00:00+08:00');
    assert.equal(f.state.error, null);
    f.state.mappingEditor.end = new Date('2026-09-30T00:00:00Z');
    f.scope.acquisitionMappingConfirm();
    assert.equal(f.state.pendingAction, null);
});

test('batch raw enable opens independently without opening or filling the range form', () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    f.scope.acquisitionMappingSelect({id: 89, collection_start_utc: '2026-10-01T00:00:00'});
    f.scope.acquisitionMappingBatchOpen('enable_raw');
    assert.equal(f.state.mappingBatch.mode, 'enable_raw');
    assert.equal(f.state.mappingBatch.start, null);
    f.scope.acquisitionMappingBatchConfirm('enable_raw');
    assert.equal(f.state.pendingAction, null);
    f.state.mappingBatch.confirmKwhDirect = true;
    f.scope.acquisitionMappingBatchConfirm('enable_raw');
    assert.equal(f.state.pendingAction.body.action, 'enable_raw');
    assert.equal(f.state.pendingAction.body.collection_start_utc, null);
    assert.equal(f.requests.length, 1);
    f.scope.acquisitionMappingBatchOpen();
    assert.equal(f.state.mappingBatch.mode, 'edit_range');
    assert.equal(f.state.mappingBatch.confirmKwhDirect, false);
    assert.equal(f.state.pendingAction, null);
    assert.equal(f.scope.acquisitionMappingSelectedCount(), 1);
});

test('mapping batch retains cross-page selection and confirms only chosen snapshots in one request', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    const first = {id: 89, external_address: '0001', external_id: 1, meter_id: 89, point_id: 89,
        configuration_status: 'pending', is_enabled: 0, collection_start_utc: null,
        collection_end_utc: null, last_complete_datetime_utc: null};
    f.state.rows = [first];
    f.scope.acquisitionMappingSelectPage();
    f.state.rows = [Object.assign({}, first, {id: 90, external_address: '0002'})];
    f.scope.acquisitionMappingSelectPage();
    assert.equal(f.scope.acquisitionMappingSelectedCount(), 2);
    f.scope.acquisitionMappingBatchOpen();
    f.state.mappingBatch.start = new Date('2026-10-01T00:00:00+08:00');
    f.scope.acquisitionMappingBatchConfirm('edit_range');
    const body = f.state.pendingAction.body;
    assert.equal(body.items.length, 2);
    assert.equal(body.collection_start_utc, '2026-10-01T00:00:00+08:00');
    assert.equal(body.items[0].expected_configuration.last_complete_datetime_utc, undefined);
    assert.equal(f.requests.length, 1);
    f.scope.acquisitionExecute();
    assert.equal(f.requests[1].url, '/api/datasources/4/acquisition/mapping-batch');
    f.requests[1].resolve({data: {items: []}});
    await settle();
    assert.equal(f.scope.acquisitionMappingSelectedCount(), 0);
    assert.equal(f.state.mappingBatch, null);
});

test('mapping selection caps at 200, clears on source change, enable needs explicit confirmation', () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    for (let id = 1; id <= 201; id++) { f.scope.acquisitionMappingSelect({id}); }
    assert.equal(f.scope.acquisitionMappingSelectedCount(), 200);
    assert.equal(f.state.error, 'ACQUISITION.MAPPING_SELECTION_LIMIT');
    f.scope.acquisitionMappingBatchOpen();
    f.scope.acquisitionMappingBatchConfirm('enable_raw');
    assert.equal(f.state.pendingAction, null);
    f.state.mappingBatch.confirmKwhDirect = true;
    f.scope.acquisitionMappingBatchConfirm('enable_raw');
    assert.equal(f.state.pendingAction.body.action, 'enable_raw');
    assert.equal(f.state.pendingAction.body.collection_start_utc, null);
    f.state.source = '1';
    f.scope.acquisitionReset();
    assert.equal(f.scope.acquisitionMappingSelectedCount(), 0);
    assert.equal(f.state.mappingBatch, null);
    assert.equal(f.state.pendingAction, null);
});

test('mapping batch rejection preserves selection with an explicit batch error', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    f.scope.acquisitionMappingSelect({id: 89});
    f.scope.acquisitionMappingBatchOpen();
    f.state.mappingBatch.confirmKwhDirect = true;
    f.scope.acquisitionMappingBatchConfirm('enable_raw');
    f.scope.acquisitionExecute();
    f.requests[1].reject({status: 409});
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.MAPPING_BATCH_ERROR');
    assert.equal(f.scope.acquisitionMappingSelectedCount(), 1);
});

test('all mapping date pickers use fixed UTC+8 Angular model options and 15-minute steps', () => {
    const html = fs.readFileSync(path.join(__dirname, 'views', 'settings', 'datasource', 'acquisition.html'), 'utf8');
    const fields = html.match(/<input[^>]+ng-model="acquisition\.mapping(?:Editor|Batch)\.(?:start|end)"[^>]*>/g);
    assert.equal(fields.length, 4);
    for (const field of fields) {
        assert.match(field, /type="datetime-local"/);
        assert.match(field, /step="900"/);
        assert.match(field, /ng-model-options="\{timezone: '\+0800'\}"/);
    }
});

test('batch pause is independent, requires confirmation, and posts untouched snapshots', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    const row = {id: 89, external_address: '0001', external_id: 1, meter_id: 89, point_id: 89,
        configuration_status: 'ready', is_enabled: 1, collection_start_utc: '2026-10-01T00:00:00',
        collection_end_utc: null};
    f.scope.acquisitionMappingSelect(row);
    f.scope.acquisitionMappingBatchOpen('pause_raw');
    assert.equal(f.state.mappingBatch.mode, 'pause_raw');
    f.scope.acquisitionMappingBatchConfirm('pause_raw');
    assert.equal(f.state.pendingAction, null);
    f.state.mappingBatch.confirmKwhDirect = true;
    f.scope.acquisitionMappingBatchConfirm('pause_raw');
    const body = f.state.pendingAction.body;
    assert.equal(body.action, 'pause_raw');
    assert.equal(body.collection_start_utc, null);
    assert.equal(body.collection_end_utc, null);
    assert.equal(body.items[0].expected_configuration.collection_start_utc, row.collection_start_utc);
    f.scope.acquisitionExecute();
    assert.equal(f.requests[1].url, '/api/datasources/4/acquisition/mapping-batch');
    f.requests[1].resolve({data: {items: [{id: 89, changed: true}]}});
    await settle();
    assert.equal(f.scope.acquisitionMappingSelectedCount(), 0);
    const html = fs.readFileSync(path.join(__dirname, 'views', 'settings', 'datasource', 'acquisition.html'), 'utf8');
    const toolbar = html.slice(html.indexOf('acquisitionMappingSelectPage()'), html.indexOf('<form name="mappingBatchForm"'));
    assert.ok(toolbar.includes("acquisitionMappingBatchOpen('enable_raw')"));
    assert.ok(toolbar.includes("acquisitionMappingBatchOpen('pause_raw')"));
});

test('all timestamps including nested confirmations display fixed UTC+8 without mutating snapshots', () => {
    const f = fixture();
    for (const [value, expected] of [
        ['2026-10-16T20:30:00', '2026-10-17 04:30:00 UTC+8'],
        ['2026-12-31T20:00:00Z', '2027-01-01 04:00:00 UTC+8'],
        ['2026-10-17T04:30:00+08:00', '2026-10-17 04:30:00 UTC+8'],
        [null, '-']
    ]) {
        assert.equal(f.scope.acquisitionDisplayValue('window_start_utc', value), expected);
    }
    assert.match(f.scope.acquisitionDisplayValue('reading_datetime_utc', 'broken'), /INVALID_TIME/);
    assert.equal(f.scope.acquisitionDisplayValue('delay_minutes', 30), 30);
    const body = {items: [{expected_configuration: {collection_start_utc: '2026-10-16T20:30:00'}}]};
    const before = JSON.stringify(body);
    assert.ok(f.scope.acquisitionDisplayDetails(body).includes('2026-10-17 04:30:00 UTC+8'));
    assert.equal(JSON.stringify(body), before);
});

test('backfill pickers submit UTC+8 and uncertain retries preserve the exact submitted window', async () => {
    const f = fixture();
    f.state.source = '4';
    f.scope.acquisitionBackfillNew({id: 9, task_type: 'energy_backfill', is_enabled: 1});
    const draft = f.state.backfillEditor;
    draft.start = new Date('2026-10-17T00:00:00+08:00');
    draft.end = new Date('2026-10-17T01:00:00+08:00');
    f.scope.acquisitionBackfillConfirm();
    assert.equal(f.state.pendingAction.body.window_start_utc, '2026-10-17T00:00:00+08:00');
    const body = JSON.stringify(f.state.pendingAction.body);
    f.scope.acquisitionExecute();
    f.requests[1].reject({status: 500});
    await settle();
    draft.start = new Date('2026-10-15T00:00:00Z');
    f.scope.acquisitionBackfillConfirm();
    assert.equal(JSON.stringify(f.state.pendingAction.body), body);
    const html = fs.readFileSync(path.join(__dirname, 'views', 'settings', 'datasource', 'acquisition.html'), 'utf8');
    const pickers = html.match(/<input[^>]+type="datetime-local"[^>]*>/g);
    assert.equal(pickers.length, 6);
    for (const picker of pickers) {
        assert.match(picker, /step="900"/);
        assert.match(picker, /ng-model-options="\{timezone: '\+0800'\}"/);
    }
    assert.ok(!html.includes('placeholder="2026-10-01T00:00:00Z"'));
    assert.ok(html.includes('acquisitionDisplayDetails(acquisition.batchDetail)'));
});

test('task UI never silently converts an existing non-Beijing schedule', () => {
    const f = fixture();
    f.state.source = '4';
    f.state.taskEditor = {id: 8, source: '4', configuration: {provider_utc_offset_minutes: 0}};
    f.scope.acquisitionTaskConfirm();
    assert.equal(f.state.pendingAction, null);
    assert.equal(f.state.error, 'ACQUISITION.TASK_TIMEZONE_ERROR');
});

test('mapping HTTP 400 gives time-specific guidance rather than task or alert uncertainty', async () => {
    const f = fixture();
    f.state.source = '4';
    f.state.collection = 'mappings';
    f.state.mappingEditor = {id: 89, source: '4', expected: {},
        start: new Date('2026-10-01T00:00:00Z'), end: null};
    f.scope.acquisitionMappingConfirm();
    f.scope.acquisitionExecute();
    f.requests[1].reject({status: 400});
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.MAPPING_TIME_ERROR');
    assert.equal(f.state.mappingEditor.id, 89);
});

test('backfill submission confirms a stable identity and locks retries after uncertain response', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionBackfillNew({id: 3, task_type: 'energy_backfill', is_enabled: 1});
    const draft = f.state.backfillEditor;
    draft.start = new Date('2026-10-01T00:00:00+08:00');
    draft.end = new Date('2026-10-03T00:00:00+08:00');
    f.scope.acquisitionBackfillConfirm();
    assert.equal(draft.attempted, false);
    assert.equal(f.requests.length, 1);
    const body = JSON.stringify(f.state.pendingAction.body);
    f.scope.acquisitionExecute();
    assert.equal(draft.attempted, true);
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/backfill');
    f.requests[1].reject({status: 500});
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.BACKFILL_SUBMIT_ERROR');
    f.scope.acquisitionBackfillNew({id: 4, task_type: 'energy_backfill', is_enabled: 1});
    assert.equal(f.state.backfillEditor, draft);
    f.scope.acquisitionBackfillConfirm();
    assert.equal(JSON.stringify(f.state.pendingAction.body), body);
    f.scope.acquisitionExecute();
    f.requests[2].resolve({data: {batch_id: 7, complete: false}});
    await settle();
    assert.equal(f.state.collection, 'batches');
    assert.equal(f.state.backfillEditor, null);
    assert.equal(f.state.backfillDrafts['1'], undefined);
    assert.equal(f.state.batchDetail.batch_id, 7);
    assert.equal(f.requests[3].url, '/api/datasources/1/acquisition/batches');
});

test('backfill draft rejects invalid ranges and cannot submit under another source', () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionBackfillNew({id: 3, task_type: 'energy_backfill', is_enabled: 0});
    assert.equal(f.state.backfillEditor, undefined);
    f.scope.acquisitionBackfillNew({id: 3, task_type: 'energy_backfill', is_enabled: 1});
    const draft = f.state.backfillEditor;
    for (const [start, end] of [
        ['2026-10-01T00:00:00', '2026-10-02T00:00:00Z'],
        ['2026-10-01T00:01:00Z', '2026-10-02T00:00:00Z'],
        ['2026-10-02T00:00:00Z', '2026-10-01T00:00:00Z'],
        ['2026-10-01T00:00:00Z', '2026-11-01T00:15:00Z']
    ]) {
        draft.start = start.endsWith('Z') ? new Date(start) : start;
        draft.end = new Date(end);
        f.scope.acquisitionBackfillConfirm();
        assert.equal(f.state.pendingAction, null);
        assert.equal(f.state.error, 'ACQUISITION.BACKFILL_TIME_ERROR');
    }
    f.state.source = '2';
    f.scope.acquisitionBackfillConfirm();
    assert.equal(f.state.pendingAction, null);
});

test('backfill HTTP input rejection unlocks correction without changing request identity', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionBackfillNew({id: 3, task_type: 'energy_backfill', is_enabled: 1});
    const draft = f.state.backfillEditor;
    draft.start = new Date('2026-10-01T00:00:00+08:00');
    draft.end = new Date('2026-10-02T00:00:00+08:00');
    const key = draft.body.request_key;
    f.scope.acquisitionBackfillConfirm();
    f.scope.acquisitionExecute();
    f.requests[1].reject({status: 400});
    await settle();
    assert.equal(draft.attempted, false);
    assert.equal(draft.body.request_key, key);
});

test('batch progress is source-scoped and cancellation uses confirmed empty-body operation', async () => {
    const f = fixture();
    f.state.source = '1';
    f.state.collection = 'batches';
    f.scope.acquisitionBatchDetail({id: 7});
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/batches/7');
    f.requests[1].resolve({data: {batch_id: 7, unsubmitted_children: 2, complete: false}});
    await settle();
    assert.equal(f.state.batchDetail.unsubmitted_children, 2);
    f.scope.acquisitionBatchConfirm({id: 7, is_cancelled: 0}, 'cancel_batch');
    assert.equal(f.requests.length, 2);
    f.scope.acquisitionExecute();
    assert.equal(f.requests[2].url, '/api/datasources/1/acquisition/batches/7/cancel');
    assert.equal(JSON.stringify(f.requests[2].data), '{}');
    f.requests[2].resolve({data: {batch_id: 7, is_cancelled: true}});
    await settle();
    assert.equal(f.state.batchDetail.is_cancelled, true);
});

test('batch detail ignores stale responses and resume uses the existing batch ID', async () => {
    const f = fixture();
    f.state.source = '1';
    f.state.collection = 'batches';
    f.scope.acquisitionBatchDetail({id: 7});
    f.state.source = '2';
    const reset = f.scope.acquisitionReset();
    f.requests[1].resolve({data: {batch_id: 7}});
    f.requests[2].resolve({data: {items: [], has_more: false}});
    await reset;
    await settle();
    assert.equal(f.state.batchDetail, null);
    f.scope.acquisitionBatchConfirm({id: 8, is_cancelled: 0}, 'resume_batch');
    f.scope.acquisitionExecute();
    assert.equal(f.requests[3].url, '/api/datasources/2/acquisition/batches/8/resume');
    f.requests[3].reject({status: 409});
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.ACTION_ERROR');
});

test('backfill drafts release unsent input or acknowledged cancelled identity, never uncertain submissions', async () => {
    const f = fixture();
    f.state.source = '1';
    const row = {id: 3, task_type: 'energy_backfill', is_enabled: 1};
    f.scope.acquisitionBackfillNew(row);
    f.scope.acquisitionBackfillDismiss();
    assert.equal(f.state.backfillDrafts['1'], undefined);
    f.scope.acquisitionBackfillNew(row);
    const draft = f.state.backfillEditor;
    draft.attempted = true;
    f.scope.acquisitionBackfillDismiss();
    assert.equal(f.state.backfillDrafts['1'], draft);
    f.state.collection = 'batches';
    f.scope.acquisitionBatchConfirm({id: 7, is_cancelled: 0}, 'cancel_batch');
    f.scope.acquisitionExecute();
    f.requests[1].resolve({data: {batch_id: 7, request_key: draft.body.request_key, is_cancelled: true}});
    await settle();
    assert.equal(f.state.backfillDrafts['1'], undefined);
});

test('mapping verification scopes requests and confirms only a range snapshot', async () => {
    const f = fixture();
    f.state.source = '1';
    f.state.collection = 'mappings';
    f.scope.acquisitionMappingEdit({id: 7});
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/mappings/7');
    const expected = {id: 7, is_enabled: 0, configuration_status: 'pending',
        collection_start_utc: '2026-10-01T00:00:00', collection_end_utc: null};
    f.requests[1].resolve({data: {configuration: expected, meter_name: 'meter', point_name: 'register'}});
    await settle();
    assert.equal(f.state.mappingEditor.start.toISOString(), '2026-10-01T00:00:00.000Z');
    assert.equal(f.state.mappingEditor.end, null);
    assert.equal(f.state.mappingEditor.meterName, 'meter');
    f.scope.acquisitionMappingConfirm();
    const body = f.state.pendingAction.body;
    assert.equal(body.collection_start_utc, '2026-10-01T08:00:00+08:00');
    assert.equal(body.collection_end_utc, null);
    assert.equal(body.expected_configuration.id, 7);
    assert.equal(body.is_enabled, undefined);
    f.scope.acquisitionExecute();
    assert.equal(f.requests[2].url, '/api/datasources/1/acquisition/mappings/7');
    f.requests[2].resolve({data: {changed: true}});
    await settle();
    assert.equal(f.state.mappingEditor, null);
    assert.equal(f.requests[3].url, '/api/datasources/1/acquisition/mappings');
});

test('mapping ownership errors are explicit and never show an editable successful fallback', async () => {
    const f = fixture();
    f.state.source = '1';
    f.state.collection = 'mappings';
    f.scope.acquisitionMappingEdit({id: 7});
    f.requests[1].reject({status: 409});
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.MAPPING_OWNERSHIP_ERROR');
    assert.equal(f.state.mappingEditor, null);
    assert.equal(f.state.busy, false);
    f.scope.acquisitionMappingConfirm();
    assert.equal(f.state.pendingAction, null);
});

test('mapping write conflicts explain the freeze and retain editable data for review', async () => {
    const f = fixture();
    f.state.source = '1';
    f.state.collection = 'mappings';
    f.state.mappingEditor = {id: 7, source: '1', start: new Date('2026-10-01T00:00:00Z'), end: null, expected: {}};
    f.scope.acquisitionMappingConfirm();
    f.scope.acquisitionExecute();
    f.requests[1].reject({status: 409});
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.MAPPING_UPDATE_CONFLICT');
    assert.equal(f.state.pendingAction, null);
    assert.equal(f.state.mappingEditor.start.toISOString(), '2026-10-01T00:00:00.000Z');
    assert.equal(f.state.busy, false);
});

test('mapping editor discards stale source responses and blocks enabled or conflicted confirmation', async () => {
    const f = fixture();
    f.state.source = '1';
    f.state.collection = 'mappings';
    f.scope.acquisitionMappingEdit({id: 7});
    f.state.source = '2';
    const reset = f.scope.acquisitionReset();
    f.requests[1].resolve({data: {configuration: {is_enabled: 0}}});
    f.requests[2].resolve({data: {items: [], has_more: false}});
    await reset;
    await settle();
    assert.equal(f.state.mappingEditor, null);
    for (const flags of [{enabled: true}, {conflict: true}]) {
        f.state.mappingEditor = Object.assign({id: 7, source: '2'}, flags);
        f.scope.acquisitionMappingConfirm();
        assert.equal(f.state.pendingAction, null);
    }
});

test('loads source list using existing authenticated API', async () => {
    const f = fixture();
    assert.equal(f.requests[0].url, '/api/datasources');
    assert.equal(f.requests[0].options.headers.Token, 'synthetic-token');
    f.requests[0].resolve({data: [{id: 1, name: 'synthetic'}]});
    await settle();
    assert.equal(f.scope.acquisitionSources[0].id, 1);
});

test('load errors are visible rather than empty success', async () => {
    const f = fixture();
    f.requests[0].reject(new Error('synthetic'));
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.LOAD_ERROR');
    f.state.source = '1';
    const loading = f.scope.acquisitionReset();
    f.requests[1].reject(new Error('synthetic'));
    await loading;
    assert.equal(f.state.error, 'ACQUISITION.LOAD_ERROR');
    assert.equal(f.state.busy, false);
});

test('uses source-scoped endpoint, active alert filter and bounded page', async () => {
    const f = fixture();
    f.state.source = '2';
    f.state.collection = 'alerts';
    f.state.alertStatus = 'active';
    const loading = f.scope.acquisitionReset();
    const req = f.requests[1];
    assert.equal(req.url, '/api/datasources/2/acquisition/alerts');
    assert.equal(req.options.params.status, 'active');
    assert.equal(req.options.params.limit, 50);
    req.resolve({data: {items: [{id: 10}], has_more: true, next_before_id: 10}});
    await loading;
    f.scope.acquisitionNext();
    assert.equal(f.requests[2].options.params.before_id, 10);
    f.requests[2].resolve({data: {items: [], has_more: false}});
    await settle();
    f.scope.acquisitionPrevious();
    assert.equal(f.requests[3].options.params.before_id, undefined);
});

test('ignores stale responses after source switch', async () => {
    const f = fixture();
    f.state.source = '1';
    const first = f.scope.acquisitionReset();
    f.state.source = '2';
    const second = f.scope.acquisitionReset();
    f.requests[2].resolve({data: {items: [{id: 2}], has_more: false}});
    await second;
    f.requests[1].resolve({data: {items: [{id: 1}], has_more: false}});
    await first;
    assert.equal(f.state.rows[0].id, 2);
});

test('controls require explicit confirmation and use server-side actor', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionExecute();
    assert.equal(f.requests.length, 1);
    f.scope.acquisitionConfirm({id: 7}, 'pause');
    f.scope.acquisitionExecute();
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/tasks/7/control');
    assert.equal(JSON.stringify(f.requests[1].data), '{"action":"pause"}');
    assert.equal(f.state.busy, true);
    f.requests[1].resolve({data: {}});
    await settle();
    assert.equal(f.requests[2].method, 'GET');
    f.requests[2].resolve({data: {items: [], has_more: false}});
    await settle();
    assert.equal(f.state.busy, false);
    assert.equal(f.state.pendingAction, null);
});

test('failed control reports uncertainty and never implies success', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionConfirm({id: 7}, 'pause');
    f.scope.acquisitionExecute();
    f.requests[1].reject(new Error('synthetic'));
    await settle();
    assert.equal(f.state.error, 'ACQUISITION.ACTION_ERROR');
    assert.equal(f.state.busy, false);
    assert.equal(f.requests.length, 2);
});

test('source change discards pending confirmation and prevents wrong-source writes', () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionConfirm({id: 7}, 'pause');
    f.state.source = '2';
    f.scope.acquisitionExecute();
    assert.equal(f.requests.length, 1);
});

test('run detail uses item endpoint and keeps run identity', () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionItems({id: 9});
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/runs/9/items');
    assert.equal(f.state.runId, 9);
});

test('missing login never sends a request', async () => {
    const f = fixture(null);
    f.state.source = '1';
    await f.scope.acquisitionReset();
    assert.equal(f.requests.length, 0);
    assert.equal(f.state.error, 'ACQUISITION.LOAD_ERROR');
});

test('feature translations and fallback exist without changing other namespaces', () => {
    const sandbox = {window: {localStorage: {getItem: () => 'de'}}, angular: {module: () => ({config() {}})}};
    for (const language of ['en', 'zh_CN', 'zh_TW']) {
        vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'app', 'translations', language + '.js'), 'utf8'), sandbox);
    }
    const bundles = sandbox.window.myemsAdminTranslations;
    assert.deepEqual(Object.keys(bundles.en.ACQUISITION).sort(), Object.keys(bundles.zh_CN.ACQUISITION).sort());
    assert.deepEqual(Object.keys(bundles.en.ACQUISITION).sort(), Object.keys(bundles.zh_TW.ACQUISITION).sort());
    bundles.de = {EXISTING: 'kept'};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'app', 'translations.js'), 'utf8'), sandbox);
    const registered = {};
    sandbox.config({translations: (lang, content) => { registered[lang] = content; }, preferredLanguage() {}});
    assert.equal(registered.de.ACQUISITION.TITLE, bundles.en.ACQUISITION.TITLE);
    assert.equal(registered.de.EXISTING, 'kept');
});

test('archive fetch requires confirmation and retains idempotency key after uncertain failure', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionConfirm({id: 3}, 'fetch');
    assert.equal(f.requests.length, 1);
    const key = f.state.pendingAction.body.request_key;
    f.scope.acquisitionExecute();
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/initialization');
    f.requests[1].reject(new Error('uncertain'));
    await settle();
    f.scope.acquisitionConfirm({id: 3}, 'fetch');
    assert.equal(f.state.pendingAction.body.request_key, key);
    f.scope.acquisitionExecute();
    f.requests[2].resolve({data: {run_id: 8}});
    await settle();
    assert.equal(f.state.collection, 'runs');
    assert.equal(f.requests[3].url, '/api/datasources/1/acquisition/runs');
});

test('preview loads existing business choices and selections survive paging but not source changes', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionPreview({id: 8});
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/initialization/8');
    assert.equal(f.requests[2].url, '/api/energycategories');
    assert.equal(f.requests[3].url, '/api/costcenters');
    f.requests[1].resolve({data: {items: [], status: 'preview', has_more: true, next_before_id: 50}});
    f.requests[2].resolve({data: [{id: 1}]});
    f.requests[3].resolve({data: [{id: 2}]});
    await settle();
    f.scope.acquisitionSelect({external_address: '0001', name: 'Meter'});
    f.scope.acquisitionNext();
    assert.ok(f.state.initialization.selected['0001']);
    f.requests[4].resolve({data: {items: [], status: 'preview'}});
    await settle();
    f.state.source = '2';
    f.scope.acquisitionReset();
    assert.deepEqual(Object.keys(f.state.initialization.selected), []);
    assert.equal(f.state.collection, 'tasks');
    assert.equal(f.state.runId, null);
});

test('approval snapshots selected names and settings; submit requires successful approval', async () => {
    const f = fixture();
    f.state.source = '1';
    f.state.initialization.source = '1';
    f.state.runId = 8;
    f.state.collection = 'preview';
    f.state.previewStatus = 'preview';
    f.scope.acquisitionSelect({external_address: '0001', name: 'Meter'});
    f.state.initialization.defaults = {energy_category_id: 1, cost_center_id: 2, is_counted: false,
        hourly_low_limit: '0', hourly_high_limit: '100', units: 'kWh', point_high_limit: '999999999999.000001'};
    f.scope.acquisitionSubmit();
    assert.equal(f.state.pendingAction, null);
    f.scope.acquisitionApprove();
    const configuration = f.state.pendingAction.body.items[0].configuration;
    assert.equal(configuration.is_counted, false);
    assert.equal(configuration.point_high_limit, '999999999999.000001');
    f.scope.acquisitionExecute();
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/initialization/8/approve');
    f.requests[1].resolve({data: {approved_count: 1}});
    await settle();
    f.requests[2].resolve({data: {items: [], status: 'preview'}});
    await settle();
    f.scope.acquisitionSubmit();
    assert.equal(JSON.stringify(f.state.pendingAction.body), '{"addresses":["0001"]}');
    f.scope.acquisitionConfigurationChanged();
    assert.equal(f.state.pendingAction, null);
    assert.equal(f.state.initialization.approved, null);
});

test('preview cannot approve stale or completed runs', () => {
    const f = fixture();
    f.state.collection = 'preview';
    f.state.previewStatus = 'done';
    f.scope.acquisitionSelect({external_address: '0001', name: 'Meter'});
    f.scope.acquisitionApprove();
    assert.equal(f.state.pendingAction, undefined);
    assert.deepEqual(Object.keys(f.state.initialization.selected), []);
});

test('new task uses paused server creation, stable retry key and a confirmed immutable snapshot', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionTaskNew();
    const editor = f.state.taskEditor;
    editor.configuration.name = 'Synthetic';
    editor.configuration.task_type = 'energy_collect';
    f.scope.acquisitionTaskConfirm();
    assert.equal(f.requests.length, 1);
    assert.equal(f.state.pendingAction.body.request_key, editor.requestKey);
    assert.equal(f.state.pendingAction.body.configuration.name, 'Synthetic');
    editor.configuration.name = 'Not confirmed';
    assert.equal(f.state.pendingAction.body.configuration.name, 'Synthetic');
    f.scope.acquisitionExecute();
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/tasks');
    assert.equal(f.requests[1].data.configuration.is_enabled, undefined);
    f.requests[1].reject(new Error('uncertain'));
    await settle();
    assert.equal(f.state.taskEditor.requestKey, editor.requestKey);
    assert.equal(f.state.error, 'ACQUISITION.ACTION_ERROR');
    editor.configuration.name = 'Synthetic';
    f.scope.acquisitionTaskConfirm();
    f.scope.acquisitionExecute();
    f.requests[2].resolve({data: {id: 10}});
    await settle();
    assert.equal(f.state.taskEditor, null);
    f.requests[3].resolve({data: {items: [], has_more: false}});
    await settle();
    assert.equal(f.state.busy, false);
});

test('edit task retrieves a safe expected snapshot and blocks enabled tasks', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionTaskEdit({id: 3});
    assert.equal(f.requests[1].url, '/api/datasources/1/acquisition/tasks/3');
    f.requests[1].resolve({data: {configuration: {name: 'Original', provider_utc_offset_minutes: 480}, is_enabled: false, has_runs: true}});
    await settle();
    f.state.taskEditor.configuration.name = 'Changed';
    f.scope.acquisitionTaskConfirm();
    assert.equal(f.state.pendingAction.body.expected_configuration.name, 'Original');
    f.scope.acquisitionExecute();
    assert.equal(f.requests[2].url, '/api/datasources/1/acquisition/tasks/3');
    f.requests[2].reject(new Error('conflict'));
    await settle();
    f.state.taskEditor.enabled = true;
    f.scope.acquisitionTaskConfirm();
    assert.equal(f.state.pendingAction, null);
});

test('task editors discard stale responses and cannot submit across source or view changes', async () => {
    const f = fixture();
    f.state.source = '1';
    f.scope.acquisitionTaskEdit({id: 3});
    f.state.source = '2';
    f.scope.acquisitionReset();
    f.requests[1].resolve({data: {configuration: {name: 'Wrong source'}, is_enabled: false}});
    await settle();
    assert.equal(f.state.taskEditor, null);
    f.requests[2].resolve({data: {items: [], has_more: false}});
    await settle();
    f.scope.acquisitionTaskNew();
    f.state.source = '1';
    f.scope.acquisitionTaskConfirm();
    assert.equal(f.state.pendingAction, null);
    f.state.source = '2';
    f.state.collection = 'runs';
    f.scope.acquisitionTaskConfirm();
    assert.equal(f.state.pendingAction, null);
});
