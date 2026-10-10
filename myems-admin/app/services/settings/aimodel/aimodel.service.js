'use strict';

app.factory('AIModelService', function($http) {
    return {
        getAll: function(headers, callback) {
            $http.get(getAPI() + 'ai-model-configs', { headers: headers }).then(callback, callback);
        },
        add: function(model, headers, callback) {
            $http.post(getAPI() + 'ai-model-configs', { data: model }, { headers: headers }).then(callback, callback);
        },
        edit: function(model, headers, callback) {
            $http.put(getAPI() + 'ai-model-configs/' + model.id, { data: model }, { headers: headers }).then(callback, callback);
        },
        setEnabled: function(model, headers, callback) {
            $http.put(getAPI() + 'ai-model-configs/' + model.id + '/enabled', {
                data: { is_enabled: model.is_enabled }
            }, { headers: headers }).then(callback, callback);
        },
        delete: function(model, headers, callback) {
            $http.delete(getAPI() + 'ai-model-configs/' + model.id, { headers: headers }).then(callback, callback);
        }
    };
});