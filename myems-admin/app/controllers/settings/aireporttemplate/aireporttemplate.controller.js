'use strict';

app.controller('AIReportTemplateController', function($scope, $rootScope, $window, $uibModal, AIReportTemplateService, toaster) {
    $scope.cur_user = JSON.parse($window.localStorage.getItem('myems_admin_ui_current_user'));

    $scope.headers = function() {
        return { 'User-UUID': $scope.cur_user.uuid, 'Token': $scope.cur_user.token };
    };

    $scope.load = function() {
        AIReportTemplateService.getAll($scope.headers(), function(response) {
            $scope.templates = response.status === 200 ? response.data : [];
        });
    };

    $scope.openTemplate = function(template) {
        var openEditor = function(fullTemplate) {
            var modalInstance = $uibModal.open({
                templateUrl: 'views/settings/aireporttemplate/aireporttemplate.model.html',
                controller: 'AIReportTemplateModalController',
                windowClass: 'animated fadeIn',
                size: 'lg',
                resolve: {
                    params: function() {
                        return { template: angular.copy(fullTemplate), headers: $scope.headers() };
                    }
                }
            });
            modalInstance.result.then(function(result) {
                var save = fullTemplate.id ? AIReportTemplateService.addVersion : AIReportTemplateService.add;
                save(result.template, $scope.headers(), function(response) {
                    if (response.status !== 200 && response.status !== 201) {
                        toaster.pop({ type: 'error', title: '错误', body: response.data.description, showCloseButton: true });
                        return;
                    }
                    var templateId = fullTemplate.id || response.data.id;
                    var versionId = response.data.version_id;
                    if (result.publish) {
                        AIReportTemplateService.publish(templateId, versionId, $scope.headers(), function(publishResponse) {
                            if (publishResponse.status === 200) {
                                toaster.pop({ type: 'success', title: '成功', body: '模板草稿已发布。', showCloseButton: true });
                                $scope.load();
                            } else {
                                toaster.pop({ type: 'error', title: '错误', body: publishResponse.data.description, showCloseButton: true });
                            }
                        });
                    } else {
                        toaster.pop({ type: 'success', title: '成功', body: '模板草稿已保存。', showCloseButton: true });
                        $scope.load();
                    }
                });
            });
            $rootScope.modalInstance = modalInstance;
        };

        if (template && template.id) {
            AIReportTemplateService.get(template, $scope.headers(), function(response) {
                if (response.status === 200) {
                    openEditor(response.data);
                } else {
                    toaster.pop({ type: 'error', title: '错误', body: response.data.description, showCloseButton: true });
                }
            });
        } else {
            openEditor({ report_type: 'ghg_emissions', report_mode: 'real_data' });
        }
    };

    $scope.load();
});

app.controller('AIReportTemplateModalController', function($scope, $uibModalInstance, AIReportTemplateService, params, toaster) {
    $scope.template = params.template;
    var latestVersion = $scope.template.versions && $scope.template.versions.length ? $scope.template.versions[0] : null;
    $scope.template.prompt_content = $scope.template.prompt_content || (latestVersion ? latestVersion.prompt_content : '');
    $scope.versions = $scope.template.versions || [];
    $scope.previewResult = null;

    $scope.preview = function() {
        if (!$scope.template.id || !$scope.template.prompt_content) {
            return;
        }
        AIReportTemplateService.preview($scope.template, params.headers, function(response) {
            if (response.status === 200) {
                $scope.previewResult = response.data;
            } else {
                toaster.pop({ type: 'error', title: '错误', body: response.data.description, showCloseButton: true });
            }
        });
    };

    $scope.save = function(publish) {
        $uibModalInstance.close({ template: $scope.template, publish: publish });
    };

    $scope.cancel = function() {
        $uibModalInstance.dismiss('cancel');
    };
});
