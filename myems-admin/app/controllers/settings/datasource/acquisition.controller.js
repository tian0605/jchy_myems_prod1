'use strict';

app.controller('AcquisitionController', function($scope, $window, $http, $q, $translate) {
    var currentUser = JSON.parse($window.localStorage.getItem('myems_admin_ui_current_user'));
    var generation = 0;
    $scope.acquisition = {source: '', collection: 'tasks', rows: [], columns: [], busy: false, error: null};
    var state = $scope.acquisition;
    state.initialization = {selected: {}, defaults: {}, categories: [], centers: [], keys: {}};
    state.backfillDrafts = {};
    state.mappingSelection = {};
    var cursor = null;
    var previousCursors = [];
    $scope.acquisitionSources = [];
    if (currentUser) {
        $http.get(getAPI() + 'datasources', {headers: headers()}).then(function(response) {
            $scope.acquisitionSources = response.data;
        }, function() {
            state.error = $translate.instant('ACQUISITION.LOAD_ERROR');
        });
    }
    var fields = {
        readings: ['id', 'external_address', 'meter_name', 'external_id', 'reading_datetime_utc',
            'forward_active_register', 'quality', 'energy_status', 'settlement_status', 'dimensions_status',
            'processing_details', 'revision', 'first_run_id', 'last_run_id'],
        business: ['id', 'run_id', 'actor_user_id', 'status', 'attempt_count', 'gate_code',
            'register_insert_count', 'hour_insert_count', 'next_attempt_datetime_utc', 'updated_datetime_utc'],
        batches: ['id', 'task_id', 'requested_by_user_id', 'window_start_utc', 'window_end_utc',
            'child_count', 'is_cancelled', 'cancelled_by_user_id', 'created_datetime_utc'],
        tasks: ['id', 'name', 'task_type', 'is_enabled', 'schedule_minute', 'delay_minutes', 'lookback_hours',
            'last_success_finished_datetime_utc', 'latest_run_id', 'latest_run_status', 'latest_run_finished_datetime_utc'],
        runs: ['id', 'task_id', 'status', 'window_start_utc', 'window_end_utc', 'expected_count', 'valid_count',
            'missing_count', 'invalid_count', 'unexpected_count', 'attempt_count', 'error_code',
            'started_datetime_utc', 'finished_datetime_utc'],
        alerts: ['id', 'external_address', 'reading_datetime_utc', 'alert_type', 'status',
            'acknowledged_by_user_id', 'recovered_datetime_utc'],
        events: ['id', 'run_id', 'alert_id', 'actor_user_id', 'event_type', 'created_datetime_utc'],
        mappings: ['id', 'external_address', 'external_id', 'meter_id', 'point_id', 'configuration_status',
            'is_enabled', 'collection_start_utc', 'collection_end_utc', 'last_complete_datetime_utc'],
        items: ['id', 'external_address', 'external_id', 'status', 'meter_id', 'point_id', 'error_message'],
        energy: ['id', 'external_address', 'external_id', 'reading_datetime_utc', 'forward_active_register',
            'quality', 'revision', 'first_run_id', 'last_run_id'],
        preview: ['id', 'external_address', 'external_id', 'name', 'area', 'status', 'existing_meter_id', 'existing_point_id']
    };
    $scope.acquisitionGateLabel = function(code) {
        if (!code) { return '-'; }
        if (/^(settlement|dimensions):/.test(code)) {
            var separator = code.indexOf(':');
            return $translate.instant('ACQUISITION.' +
                (code.slice(0, separator) === 'settlement' ? 'settlement_status' : 'dimensions_status')) +
                ': ' + $scope.acquisitionGateLabel(code.slice(separator + 1));
        }
        var key = 'ACQUISITION.GATE_' + code;
        var label = $translate.instant(key);
        return label === key ? code : label + ' (' + code + ')';
    };
    function beijingTime(value) {
        if (value === null || value === undefined || value === '') { return '-'; }
        if (typeof value !== 'string' ||
            !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/.test(value)) {
            return $translate.instant('ACQUISITION.INVALID_TIME') + ': ' + String(value);
        }
        var date = new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : value + 'Z');
        if (!isFinite(date.getTime())) {
            return $translate.instant('ACQUISITION.INVALID_TIME') + ': ' + value;
        }
        return new Date(date.getTime() + 480 * 60000).toISOString().slice(0, 19).replace('T', ' ') + ' UTC+8';
    }
    $scope.acquisitionDisplayValue = function(column, value) {
        if (/_utc$/.test(column)) { return beijingTime(value); }
        if (column === 'gate_code') { return $scope.acquisitionGateLabel(value); }
        if (column === 'latest_run_status' && value) {
            var statusKey = 'ACQUISITION.RUN_STATUS_' + value;
            var statusLabel = $translate.instant(statusKey);
            return statusLabel === statusKey ? value : statusLabel;
        }
        if (column === 'quality' && ['valid', 'invalid', 'conflict'].indexOf(value) >= 0) {
            return $translate.instant('ACQUISITION.QUALITY_' + value);
        }
        if (['energy_status', 'settlement_status', 'dimensions_status'].indexOf(column) >= 0) {
            return $translate.instant('ACQUISITION.PROCESSING_' + value);
        }
        if (column === 'processing_details') {
            return (value || []).map(function(hour) {
                var phases = ['energy', 'settlement', 'dimensions'].map(function(phase) {
                    var entry = hour[phase];
                    return $translate.instant('ACQUISITION.' + phase + '_status') + ': ' +
                        $translate.instant('ACQUISITION.PROCESSING_' + entry.status) +
                        (entry.reason ? ' — ' + $scope.acquisitionGateLabel(entry.reason) : '');
                });
                return beijingTime(hour.start_utc) + ' → ' + beijingTime(hour.end_utc) + '\n' + phases.join('\n');
            }).join('\n\n');
        }
        return value === null || value === undefined ? '-' : value;
    };
    $scope.acquisitionDisplayDetails = function(value) {
        function display(item, key) {
            if (/_utc$/.test(key)) { return beijingTime(item); }
            if (Array.isArray(item)) { return item.map(function(child) { return display(child, ''); }); }
            if (item && typeof item === 'object') {
                var result = {};
                Object.keys(item).forEach(function(name) { result[name] = display(item[name], name); });
                return result;
            }
            return item;
        }
        return JSON.stringify(display(value, ''), null, 2);
    };
    function headers() {
        return {'User-UUID': currentUser.uuid, 'Token': currentUser.token};
    }
    function base() {
        return getAPI() + 'datasources/' + encodeURIComponent(state.source) + '/acquisition/';
    }
    function load() {
        var ticket = ++generation;
        state.error = null;
        state.energyDetail = false;
        state.rows = [];
        state.hasMore = false;
        state.columns = fields[state.collection];
        if (!state.source || !currentUser) {
            if (!currentUser) { state.error = $translate.instant('ACQUISITION.LOAD_ERROR'); }
            state.busy = false;
            return $q.when();
        }
        state.busy = true;
        var path = state.collection === 'items' ? 'runs/' + state.runId + '/items' :
            state.collection === 'preview' ? 'initialization/' + state.runId : state.collection;
        return $http.get(base() + path, {headers: headers(), params: {
            limit: 50, before_id: cursor || undefined,
            status: state.collection === 'alerts' ? state.alertStatus || undefined : undefined
        }}).then(function(response) {
            if (ticket !== generation) { return; }
            state.rows = response.data.items;
            if (state.collection === 'items') {
                state.energyDetail = response.data.detail_kind === 'energy_current_window';
                state.columns = state.energyDetail ? fields.energy : fields.items;
            }
            state.hasMore = response.data.has_more;
            state.nextCursor = response.data.next_before_id;
            if (state.collection === 'preview') { state.previewStatus = response.data.status; }
        }, function() {
            if (ticket === generation) {
                state.error = $translate.instant('ACQUISITION.LOAD_ERROR');
            }
        }).finally(function() {
            if (ticket === generation) { state.busy = false; }
        });
    }
    $scope.acquisitionReset = function() {
        state.taskEditor = null;
        state.mappingEditor = null;
        state.mappingBatch = null;
        state.backfillEditor = null;
        state.batchDetail = null;
        var init = state.initialization;
        if (init.source !== state.source) {
            state.mappingSelection = {};
            init.source = state.source;
            init.selected = {};
            init.keys = {};
            init.approved = null;
            state.previewStatus = null;
            state.runId = null;
            if (['preview', 'items'].indexOf(state.collection) >= 0) { state.collection = 'tasks'; }
        }
        cursor = null;
        previousCursors = [];
        state.canPrevious = false;
        state.pendingAction = null;
        return load();
    };
    $scope.acquisitionRefresh = load;
    $scope.acquisitionNext = function() {
        if (!state.hasMore || state.busy) { return; }
        previousCursors.push(cursor);
        cursor = state.nextCursor;
        state.canPrevious = true;
        load();
    };
    $scope.acquisitionPrevious = function() {
        if (!previousCursors.length || state.busy) { return; }
        cursor = previousCursors.pop();
        state.canPrevious = previousCursors.length > 0;
        load();
    };
    $scope.acquisitionItems = function(row) {
        state.initialization.source = state.source;
        state.runId = row.id;
        state.collection = 'items';
        $scope.acquisitionReset();
    };
    $scope.acquisitionConfirm = function(row, action) {
        state.pendingAction = {row: row, action: action, source: state.source, collection: state.collection};
        if (action === 'fetch') {
            if (!state.initialization.keys[row.id]) {
                if (!$window.crypto || !$window.crypto.randomUUID) {
                    state.error = $translate.instant('ACQUISITION.UUID_ERROR');
                    state.pendingAction = null;
                    return;
                }
                state.initialization.keys[row.id] = $window.crypto.randomUUID();
            }
            state.pendingAction.body = {task_id: row.id, request_key: state.initialization.keys[row.id]};
        }
    };
    $scope.acquisitionExecute = function() {
        var pending = state.pendingAction;
        if (!pending || state.busy || pending.source !== state.source || pending.collection !== state.collection) { return; }
        state.busy = true;
        state.error = null;
        var ticket = ++generation;
        var path = pending.action === 'fetch' ? 'initialization' :
            pending.action === 'create_task' ? 'tasks' :
            pending.action === 'edit_task' ? 'tasks/' + pending.row.id :
            pending.action === 'edit_mapping' ? 'mappings/' + pending.row.id :
            pending.action === 'mapping_batch' ? 'mapping-batch' :
            ['enable_raw', 'pause_raw'].indexOf(pending.action) >= 0 ? 'mappings/' + pending.row.id + '/raw-collection' :
            pending.action === 'submit_backfill' ? 'backfill' :
            pending.action === 'resume_batch' ? 'batches/' + pending.row.id + '/resume' :
            pending.action === 'cancel_batch' ? 'batches/' + pending.row.id + '/cancel' :
            ['approve', 'submit'].indexOf(pending.action) >= 0 ? 'initialization/' + pending.row.id + '/' + pending.action :
            pending.collection + '/' + pending.row.id + '/control';
        if (pending.action === 'submit_backfill') {
            state.backfillDrafts[state.source].attempted = true;
        }
        $http.post(base() + path, pending.body || {action: pending.action}, {headers: headers()}).then(function(response) {
            if (ticket !== generation) { return; }
            state.pendingAction = null;
            if (pending.action === 'create_task' || pending.action === 'edit_task') {
                state.taskEditor = null;
            }
            if (['edit_mapping', 'enable_raw', 'pause_raw'].indexOf(pending.action) >= 0) { state.mappingEditor = null; }
            if (pending.action === 'mapping_batch') {
                state.mappingBatch = null;
                state.mappingSelection = {};
            }
            if (pending.action === 'submit_backfill') {
                delete state.backfillDrafts[state.source];
                state.backfillEditor = null;
                state.collection = 'batches';
                cursor = null;
                previousCursors = [];
                state.canPrevious = false;
            }
            if (['submit_backfill', 'resume_batch', 'cancel_batch'].indexOf(pending.action) >= 0) {
                state.batchDetail = response.data;
                var draft = state.backfillDrafts[state.source];
                if (draft && draft.body.request_key === response.data.request_key &&
                    (response.data.is_cancelled || response.data.unsubmitted_children === 0)) {
                    delete state.backfillDrafts[state.source];
                    state.backfillEditor = null;
                }
            }
            if (pending.action === 'fetch') {
                delete state.initialization.keys[pending.row.id];
                state.collection = 'runs';
            }
            if (pending.action === 'approve') {
                state.initialization.approved = Object.keys(state.initialization.selected).sort();
            }
            if (pending.action === 'submit') {
                state.initialization.selected = {};
                state.initialization.approved = null;
                $scope.acquisitionItems({id: response.data.run_id});
                return;
            }
            load();
        }, function(response) {
            if (ticket === generation) {
                if (pending.action === 'submit_backfill' && response.status === 400) {
                    // This endpoint validates HTTP input before creating a durable parent.
                    state.backfillDrafts[state.source].attempted = false;
                }
                state.error = $translate.instant(pending.action === 'submit_backfill' ?
                    'ACQUISITION.BACKFILL_SUBMIT_ERROR' : pending.action === 'mapping_batch' ?
                        'ACQUISITION.MAPPING_BATCH_ERROR' : ['enable_raw', 'pause_raw'].indexOf(pending.action) >= 0 ?
                        'ACQUISITION.RAW_COLLECTION_ERROR' : pending.action === 'edit_mapping' && response.status === 400 ?
                        'ACQUISITION.MAPPING_TIME_ERROR' : pending.action === 'edit_mapping' && response.status === 409 ?
                        'ACQUISITION.MAPPING_UPDATE_CONFLICT' : 'ACQUISITION.ACTION_ERROR');
                state.busy = false;
                state.pendingAction = null;
            }
        });
    };
    $scope.acquisitionPreview = function(row) {
        state.initialization.source = state.source;
        state.initialization.selected = {};
        state.initialization.approved = null;
        state.runId = row.id;
        state.collection = 'preview';
        $scope.acquisitionReset();
        if (!state.initialization.lookupsLoaded) {
            $q.all([
                $http.get(getAPI() + 'energycategories', {headers: headers()}),
                $http.get(getAPI() + 'costcenters', {headers: headers()})
            ]).then(function(responses) {
                state.initialization.categories = responses[0].data;
                state.initialization.centers = responses[1].data;
                state.initialization.lookupsLoaded = true;
            }, function() { state.error = $translate.instant('ACQUISITION.LOAD_ERROR'); });
        }
    };
    $scope.acquisitionSelect = function(row) {
        if (state.previewStatus !== 'preview') { return; }
        var selected = state.initialization.selected;
        if (selected[row.external_address]) { delete selected[row.external_address]; }
        else {
            selected[row.external_address] = {address: row.external_address,
                name: row.configuration ? row.configuration.name : row.name};
        }
        state.initialization.approved = null;
        state.pendingAction = null;
    };
    $scope.acquisitionSelectPage = function() {
        if (state.busy || state.previewStatus !== 'preview') { return; }
        state.rows.forEach(function(row) {
            if (!state.initialization.selected[row.external_address]) { $scope.acquisitionSelect(row); }
        });
    };
    $scope.acquisitionClearSelection = function() {
        state.initialization.selected = {};
        $scope.acquisitionConfigurationChanged();
    };
    $scope.acquisitionSelectedCount = function() { return Object.keys(state.initialization.selected).length; };
    $scope.acquisitionApprove = function() {
        var init = state.initialization;
        var addresses = Object.keys(init.selected).sort();
        if (!addresses.length || addresses.length > 200 || state.busy || state.previewStatus !== 'preview') { return; }
        var items = addresses.map(function(address) {
            return {address: address, configuration: {
                name: init.selected[address].name,
                energy_category_id: Number(init.defaults.energy_category_id),
                cost_center_id: Number(init.defaults.cost_center_id),
                is_counted: init.defaults.is_counted === true,
                hourly_low_limit: init.defaults.hourly_low_limit, hourly_high_limit: init.defaults.hourly_high_limit,
                units: init.defaults.units, point_high_limit: init.defaults.point_high_limit
            }};
        });
        state.pendingAction = {row: {id: state.runId}, action: 'approve', source: state.source,
            collection: 'preview', body: {items: items}};
    };
    $scope.acquisitionSubmit = function() {
        var init = state.initialization;
        if (!init.approved || !init.approved.length || state.busy || state.previewStatus !== 'preview') { return; }
        state.pendingAction = {row: {id: state.runId}, action: 'submit', source: state.source,
            collection: 'preview', body: {addresses: init.approved.slice()}};
    };
    $scope.acquisitionConfigurationChanged = function() {
        state.initialization.approved = null;
        state.pendingAction = null;
    };
    $scope.acquisitionTaskNew = function() {
        if (state.busy || !state.source || !currentUser) { return; }
        if (!$window.crypto || !$window.crypto.randomUUID) {
            state.error = $translate.instant('ACQUISITION.UUID_ERROR');
            return;
        }
        state.pendingAction = null;
        state.taskEditor = {source: state.source, requestKey: $window.crypto.randomUUID(), configuration: {
            name: '', task_type: '', schedule_minute: 40, delay_minutes: 40,
            lookback_hours: 3, provider_utc_offset_minutes: 480, encoding: 'json'
        }};
    };
    $scope.acquisitionTaskEdit = function(row) {
        if (state.busy || !state.source || !currentUser) { return; }
        var ticket = ++generation;
        var source = state.source;
        state.busy = true;
        state.error = null;
        state.pendingAction = null;
        state.taskEditor = null;
        $http.get(base() + 'tasks/' + row.id, {headers: headers()}).then(function(response) {
            if (ticket !== generation || source !== state.source) { return; }
            state.taskEditor = {id: row.id, source: source, enabled: response.data.is_enabled,
                hasRuns: response.data.has_runs, expected: response.data.configuration,
                configuration: JSON.parse(JSON.stringify(response.data.configuration))};
        }, function() {
            if (ticket === generation) { state.error = $translate.instant('ACQUISITION.LOAD_ERROR'); }
        }).finally(function() {
            if (ticket === generation) { state.busy = false; }
        });
    };
    $scope.acquisitionTaskConfirm = function() {
        var editor = state.taskEditor;
        if (!editor || state.busy || editor.source !== state.source || editor.enabled || state.collection !== 'tasks') { return; }
        if (editor.configuration.provider_utc_offset_minutes !== 480) {
            state.pendingAction = null;
            state.error = $translate.instant('ACQUISITION.TASK_TIMEZONE_ERROR');
            return;
        }
        state.pendingAction = {
            row: {id: editor.id || ''}, source: state.source, collection: 'tasks',
            action: editor.id ? 'edit_task' : 'create_task', body: {
                configuration: JSON.parse(JSON.stringify(editor.configuration))
            }
        };
        if (editor.id) { state.pendingAction.body.expected_configuration = editor.expected; }
        else { state.pendingAction.body.request_key = editor.requestKey; }
    };
    $scope.acquisitionTaskChanged = function() { state.pendingAction = null; };
    function mappingDate(value) {
        return value ? new Date(value + 'Z') : null;
    }
    function mappingTime(value) {
        if (!value || Object.prototype.toString.call(value) !== '[object Date]' ||
            !isFinite(value.getTime()) || value.getTime() % 900000) { return null; }
        return new Date(value.getTime() + 480 * 60000).toISOString().slice(0, 19) + '+08:00';
    }
    function mappingRange(editor) {
        var start = mappingTime(editor.start);
        var end = editor.end ? mappingTime(editor.end) : null;
        if (!start || (editor.end && (!end || editor.end.getTime() <= editor.start.getTime()))) {
            state.error = $translate.instant('ACQUISITION.MAPPING_TIME_ERROR');
            return null;
        }
        state.error = null;
        return {collection_start_utc: start, collection_end_utc: end};
    }
    $scope.acquisitionMappingSelect = function(row) {
        if (state.busy || state.collection !== 'mappings') { return; }
        state.pendingAction = null;
        state.mappingBatch = null;
        if (state.mappingSelection[row.id]) { delete state.mappingSelection[row.id]; }
        else if (Object.keys(state.mappingSelection).length < 200) {
            state.mappingSelection[row.id] = JSON.parse(JSON.stringify(row));
        } else { state.error = $translate.instant('ACQUISITION.MAPPING_SELECTION_LIMIT'); }
    };
    $scope.acquisitionMappingSelectPage = function() {
        if (state.busy || state.collection !== 'mappings') { return; }
        state.rows.forEach(function(row) {
            if (!state.mappingSelection[row.id]) { $scope.acquisitionMappingSelect(row); }
        });
    };
    $scope.acquisitionMappingClear = function() {
        if (state.busy) { return; }
        state.mappingSelection = {};
        state.mappingBatch = null;
        state.pendingAction = null;
    };
    $scope.acquisitionMappingSelectedCount = function() { return Object.keys(state.mappingSelection).length; };
    $scope.acquisitionMappingBatchOpen = function(mode) {
        if (state.busy || !state.source || !currentUser || state.collection !== 'mappings' ||
            !$scope.acquisitionMappingSelectedCount()) { return; }
        state.pendingAction = null;
        state.mappingEditor = null;
        state.mappingBatch = {source: state.source,
            mode: ['enable_raw', 'pause_raw'].indexOf(mode) >= 0 ? mode : 'edit_range',
            start: null, end: null, confirmKwhDirect: false};
    };
    $scope.acquisitionMappingBatchConfirm = function(action) {
        var editor = state.mappingBatch;
        if (!editor || state.busy || editor.source !== state.source || state.collection !== 'mappings' ||
            ['edit_range', 'enable_raw', 'pause_raw'].indexOf(action) < 0) { return; }
        var ids = Object.keys(state.mappingSelection);
        if (!ids.length || ids.length > 200 || (action !== 'edit_range' && !editor.confirmKwhDirect)) { return; }
        state.pendingAction = null;
        var range = action === 'edit_range' ? mappingRange(editor) :
            {collection_start_utc: null, collection_end_utc: null};
        if (!range) { return; }
        var columns = ['id', 'external_address', 'external_id', 'meter_id', 'point_id',
            'configuration_status', 'is_enabled', 'collection_start_utc', 'collection_end_utc'];
        var items = ids.map(function(id) {
            var expected = {};
            columns.forEach(function(key) { expected[key] = state.mappingSelection[id][key]; });
            return {id: Number(id), expected_configuration: expected};
        });
        state.pendingAction = {row: {id: ids.length}, action: 'mapping_batch', source: state.source,
            collection: 'mappings', body: {
                action: action, items: items, confirm_kwh_direct: action !== 'edit_range',
                collection_start_utc: range.collection_start_utc, collection_end_utc: range.collection_end_utc
            }};
    };
    $scope.acquisitionMappingEdit = function(row) {
        if (state.busy || !state.source || !currentUser || state.collection !== 'mappings') { return; }
        var ticket = ++generation;
        var source = state.source;
        state.busy = true;
        state.error = null;
        state.pendingAction = null;
        state.mappingEditor = null;
        $http.get(base() + 'mappings/' + row.id, {headers: headers()}).then(function(response) {
            if (ticket !== generation || source !== state.source) { return; }
            var config = response.data.configuration;
            state.mappingEditor = {id: row.id, source: source, expected: config,
                meterName: response.data.meter_name, pointName: response.data.point_name,
                enabled: !!config.is_enabled, conflict: config.configuration_status === 'conflict',
                start: mappingDate(config.collection_start_utc),
                end: mappingDate(config.collection_end_utc)};
        }, function(response) {
            if (ticket === generation) {
                state.error = $translate.instant(response.status === 409 ?
                    'ACQUISITION.MAPPING_OWNERSHIP_ERROR' : 'ACQUISITION.LOAD_ERROR');
            }
        }).finally(function() {
            if (ticket === generation) { state.busy = false; }
        });
    };
    $scope.acquisitionMappingConfirm = function() {
        var editor = state.mappingEditor;
        if (!editor || state.busy || editor.source !== state.source || editor.enabled ||
            editor.conflict || state.collection !== 'mappings') { return; }
        state.pendingAction = null;
        var range = mappingRange(editor);
        if (!range) { return; }
        state.pendingAction = {row: {id: editor.id}, source: state.source, collection: 'mappings',
            action: 'edit_mapping', body: {
                collection_start_utc: range.collection_start_utc, collection_end_utc: range.collection_end_utc,
                expected_configuration: JSON.parse(JSON.stringify(editor.expected))
            }};
    };
    $scope.acquisitionMappingRawConfirm = function(action) {
        var editor = state.mappingEditor;
        if (!editor || state.busy || editor.source !== state.source || editor.conflict ||
            state.collection !== 'mappings' || !editor.confirmKwhDirect ||
            ['enable_raw', 'pause_raw'].indexOf(action) < 0) { return; }
        if ((action === 'enable_raw' && (editor.enabled || !editor.expected.collection_start_utc)) ||
            (action === 'pause_raw' && !editor.enabled)) { return; }
        state.pendingAction = {row: {id: editor.id}, source: state.source, collection: 'mappings',
            action: action, body: {action: action, confirm_kwh_direct: true,
                expected_configuration: JSON.parse(JSON.stringify(editor.expected))}};
    };
    $scope.acquisitionBackfillNew = function(row) {
        if (state.busy || !state.source || !currentUser || state.collection !== 'tasks' ||
            row.task_type !== 'energy_backfill' || !row.is_enabled) { return; }
        if (!state.backfillDrafts[state.source]) {
            if (!$window.crypto || !$window.crypto.randomUUID) {
                state.error = $translate.instant('ACQUISITION.UUID_ERROR');
                return;
            }
            state.backfillDrafts[state.source] = {source: state.source, attempted: false, start: null, end: null, body: {
                task_id: row.id, request_key: $window.crypto.randomUUID(), window_start_utc: '', window_end_utc: ''
            }};
        }
        state.backfillEditor = state.backfillDrafts[state.source];
        state.pendingAction = null;
    };
    $scope.acquisitionBackfillConfirm = function() {
        var editor = state.backfillEditor;
        if (!editor || state.busy || editor.source !== state.source || state.collection !== 'tasks') { return; }
        state.pendingAction = null;
        if (!editor.attempted) {
            var start = mappingTime(editor.start);
            var end = mappingTime(editor.end);
            if (!start || !end || editor.end.getTime() <= editor.start.getTime() ||
                editor.end.getTime() - editor.start.getTime() > 31 * 86400000) {
                state.error = $translate.instant('ACQUISITION.BACKFILL_TIME_ERROR');
                return;
            }
            editor.body.window_start_utc = start;
            editor.body.window_end_utc = end;
        }
        if (!editor.body.window_start_utc || !editor.body.window_end_utc) {
            state.error = $translate.instant('ACQUISITION.BACKFILL_TIME_ERROR');
            return;
        }
        state.error = null;
        state.pendingAction = {row: {id: editor.body.task_id}, source: state.source, collection: 'tasks',
            action: 'submit_backfill', body: JSON.parse(JSON.stringify(editor.body))};
    };
    $scope.acquisitionBackfillDismiss = function() {
        var editor = state.backfillEditor;
        if (state.busy || !editor) { return; }
        if (!editor.attempted) { delete state.backfillDrafts[editor.source]; }
        state.backfillEditor = null;
        state.pendingAction = null;
    };
    $scope.acquisitionBatchDetail = function(row) {
        if (state.busy || !state.source || !currentUser || state.collection !== 'batches') { return; }
        var ticket = ++generation;
        var source = state.source;
        state.busy = true;
        state.error = null;
        state.pendingAction = null;
        state.batchDetail = null;
        $http.get(base() + 'batches/' + row.id, {headers: headers()}).then(function(response) {
            if (ticket === generation && source === state.source) { state.batchDetail = response.data; }
        }, function() {
            if (ticket === generation) { state.error = $translate.instant('ACQUISITION.LOAD_ERROR'); }
        }).finally(function() {
            if (ticket === generation) { state.busy = false; }
        });
    };
    $scope.acquisitionBatchConfirm = function(row, action) {
        if (state.busy || state.collection !== 'batches' || row.is_cancelled ||
            ['resume_batch', 'cancel_batch'].indexOf(action) < 0) { return; }
        state.pendingAction = {row: row, action: action, source: state.source, collection: 'batches', body: {}};
    };
});
