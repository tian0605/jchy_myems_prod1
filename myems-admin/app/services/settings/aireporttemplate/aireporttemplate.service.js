'use strict';

app.factory('AIReportTemplateService', function($http) {
    return {
        getAll: function(headers, callback) {
            $http.get(getAPI() + 'ai-report-templates', { headers: headers }).then(callback, callback);
        },
        get: function(template, headers, callback) {
            $http.get(getAPI() + 'ai-report-templates/' + template.id, { headers: headers }).then(callback, callback);
        },
        add: function(template, headers, callback) {
            $http.post(getAPI() + 'ai-report-templates', { data: template }, { headers: headers }).then(callback, callback);
        },
        addVersion: function(template, headers, callback) {
            $http.put(getAPI() + 'ai-report-templates/' + template.id, { data: template }, { headers: headers }).then(callback, callback);
        },
        preview: function(template, headers, callback) {
            $http.post(getAPI() + 'ai-report-templates/' + template.id + '/preview', { data: template }, { headers: headers }).then(callback, callback);
        },
        publish: function(templateId, versionId, headers, callback) {
            $http.put(getAPI() + 'ai-report-templates/' + templateId + '/publish', {
                data: { version_id: versionId }
            }, { headers: headers }).then(callback, callback);
        }
    };
});
