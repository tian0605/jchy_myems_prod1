'use strict';

app.controller('AIModelController', function($scope, $rootScope, $window, $uibModal, AIModelService, toaster, SweetAlert) {
    $scope.cur_user = JSON.parse($window.localStorage.getItem('myems_admin_ui_current_user'));

    $scope.headers = function() {
        return { 'User-UUID': $scope.cur_user.uuid, 'Token': $scope.cur_user.token };
    };

    $scope.load = function() {
        AIModelService.getAll($scope.headers(), function(response) {
            $scope.models = response.status === 200 ? response.data : [];
        });
    };

    $scope.openModel = function(model) {
        var modalInstance = $uibModal.open({
            templateUrl: 'views/settings/aimodel/aimodel.model.html',
            controller: 'AIModelModalController',
            windowClass: 'animated fadeIn',
            resolve: {
                params: function() {
                    return { model: angular.copy(model) };
                }
            }
        });
        modalInstance.result.then(function(updatedModel) {
            var callback = function(response) {
                if (response.status === 200 || response.status === 201) {
                    toaster.pop({ type: 'success', title: '成功', body: 'AI 模型配置已保存。', showCloseButton: true });
                    $scope.load();
                } else {
                    toaster.pop({ type: 'error', title: '错误', body: response.data.description, showCloseButton: true });
                }
            };
            if (updatedModel.id) {
                AIModelService.edit(updatedModel, $scope.headers(), callback);
            } else {
                AIModelService.add(updatedModel, $scope.headers(), callback);
            }
        });
        $rootScope.modalInstance = modalInstance;
    };

    $scope.toggleEnabled = function(model) {
        var updatedModel = angular.copy(model);
        updatedModel.is_enabled = !updatedModel.is_enabled;
        AIModelService.setEnabled(updatedModel, $scope.headers(), function(response) {
            if (response.status === 200) {
                $scope.load();
            } else {
                toaster.pop({ type: 'error', title: '错误', body: response.data.description, showCloseButton: true });
            }
        });
    };

    $scope.deleteModel = function(model) {
        SweetAlert.swal({
            title: '删除 AI 模型配置？',
            text: model.name,
            type: 'warning',
            showCancelButton: true,
            confirmButtonColor: '#DD6B55',
            confirmButtonText: '删除',
            cancelButtonText: '取消',
            closeOnConfirm: true
        }, function(isConfirm) {
            if (isConfirm) {
                AIModelService.delete(model, $scope.headers(), function(response) {
                    if (response.status === 204) {
                        $scope.load();
                    } else {
                        toaster.pop({ type: 'error', title: '错误', body: response.data.description, showCloseButton: true });
                    }
                });
            }
        });
    };

    $scope.load();
});

app.controller('AIModelModalController', function($scope, $uibModalInstance, params) {
    $scope.model = params.model || {
        provider: 'openai_compatible',
        is_enabled: true,
        is_default: false,
        timeout_seconds: 60
    };

    $scope.ok = function() {
        $uibModalInstance.close($scope.model);
    };

    $scope.cancel = function() {
        $uibModalInstance.dismiss('cancel');
    };
});