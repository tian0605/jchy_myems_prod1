'use strict';

var ADMIN_MENU_SECTIONS = [
    {
        title: 'MENU.SETTINGS.SETTINGS',
        routes: [
            'settings.category', 'settings.tariff', 'settings.costcenter', 'settings.contact', 'settings.gateway',
            'settings.protocol', 'settings.datasource', 'settings.meter', 'settings.sensor', 'settings.equipment',
            'settings.combinedequipment', 'settings.space', 'settings.tenant', 'settings.store', 'settings.shopfloor',
            'settings.energyflowdiagram', 'settings.svg', 'settings.distributionsystem', 'settings.menu',
            'settings.knowledgefile', 'settings.workingcalendar'
        ]
    },
    {
        title: 'MENU.USERSETTING.USERSETTING',
        routes: [
            'users.user', 'users.privilege', 'users.menutemplate', 'users.apikey', 'users.log'
        ]
    },
    {
        title: 'MENU.SETTINGS.ADVANCED',
        routes: [
            'settings.command', 'settings.controlmode', 'settings.iotsimcard', 'settings.microgrid',
            'settings.virtualpowerplant', 'settings.energystoragecontainer', 'settings.energystoragepowerstation',
            'settings.photovoltaicpowerstation', 'settings.windfarm', 'settings.emailserver',
            'settings.advancedreport', 'settings.energyplanfile', 'settings.aimodels', 'settings.aireporttemplates'
        ]
    },
    {
        title: 'MENU.FDD.FDD',
        routes: [
            'fdd.rule', 'fdd.textmessage', 'fdd.emailmessage', 'fdd.webmessage', 'fdd.wechatmessage'
        ]
    }
];

var ADMIN_MENU_ROUTE_META = ADMIN_MENU_SECTIONS.reduce(function(result, section, sectionIndex) {
    (section.routes || []).forEach(function(routeName, routeIndex) {
        result[routeName] = {
            group_title: section.title,
            group_order: sectionIndex,
            route_order: routeIndex
        };
    });
    return result;
}, {});

var WEB_MENU_SECTIONS = [
    {
        groupTitle: 'Dashboard',
        routes: [
            { state: '/dashboard', name: 'Dashboard' }
        ]
    },
    {
        groupTitle: 'Space Data',
        routes: [
            { state: '/space/energycategory', name: 'Energy Category Data' },
            { state: '/space/energyitem', name: 'Energy Item Data' },
            { state: '/space/carbon', name: 'Carbon' },
            { state: '/space/cost', name: 'Cost' },
            { state: '/space/output', name: 'Output' },
            { state: '/space/income', name: 'Income' },
            { state: '/space/efficiency', name: 'Efficiency' },
            { state: '/space/evaluation', name: 'MENU.WEB.EVALUATION_REPORT' },
            { state: '/space/load', name: 'Load' },
            { state: '/space/statistics', name: 'Statistics' },
            { state: '/space/saving', name: 'Saving' },
            { state: '/space/plan', name: 'Plan' },
            { state: '/space/prediction', name: 'Prediction' },
            { state: '/space/environmentmonitor', name: 'Environment Monitor' },
            { state: '/space/enterproduction', name: 'Enter Production' },
            { state: '/space/production', name: 'MENU.WEB.SPACE_PRODUCTION' },
            { state: '/space/comparison', name: 'Space Comparison' }
        ]
    },
    {
        groupTitle: 'Equipment Data',
        routes: [
            { state: '/equipment/energycategory', name: 'Energy Category Data' },
            { state: '/equipment/energyitem', name: 'Energy Item Data' },
            { state: '/equipment/carbon', name: 'Carbon' },
            { state: '/equipment/cost', name: 'Cost' },
            { state: '/equipment/output', name: 'Output' },
            { state: '/equipment/income', name: 'Income' },
            { state: '/equipment/efficiency', name: 'Efficiency' },
            { state: '/equipment/load', name: 'Load' },
            { state: '/equipment/statistics', name: 'Statistics' },
            { state: '/equipment/saving', name: 'Saving' },
            { state: '/equipment/plan', name: 'Plan' },
            { state: '/equipment/batch', name: 'Batch Analysis' },
            { state: '/equipment/tracking', name: 'Equipment Tracking' },
            { state: '/equipment/comparison', name: 'Equipment Comparison' }
        ]
    },
    {
        groupTitle: 'Meter Data',
        routes: [
            { state: '/meter/meterenergy', name: 'Meter Energy' },
            { state: '/meter/metercarbon', name: 'Meter Carbon' },
            { state: '/meter/metercost', name: 'Meter Cost' },
            { state: '/meter/metertrend', name: 'Meter Trend' },
            { state: '/meter/meterrealtime', name: 'Meter Realtime' },
            { state: '/meter/metersaving', name: 'Meter Saving' },
            { state: '/meter/meterplan', name: 'Meter Plan' },
            { state: '/meter/metersubmetersbalance', name: 'Master Meter Submeters Balance' },
            { state: '/meter/meterbatch', name: 'Meter Batch Analysis' },
            { state: '/meter/metercomparison', name: 'Meter Comparison' },
            { state: '/meter/metertracking', name: 'Meter Tracking' },
            { state: '/meter/powerquality', name: 'Power Quality' },
            { state: '/meter/virtualmeterenergy', name: 'Virtual Meter Energy' },
            { state: '/meter/virtualmetercarbon', name: 'Virtual Meter Carbon' },
            { state: '/meter/virtualmetercost', name: 'Virtual Meter Cost' },
            { state: '/meter/virtualmeterbatch', name: 'Virtual Meter Batch Analysis' },
            { state: '/meter/virtualmetersaving', name: 'Virtual Meter Saving' },
            { state: '/meter/virtualmeterplan', name: 'Virtual Meter Plan' },
            { state: '/meter/virtualmetercomparison', name: 'Virtual Meter Comparison' },
            { state: '/meter/offlinemeterenergy', name: 'Offline Meter Energy' },
            { state: '/meter/offlinemetercarbon', name: 'Offline Meter Carbon' },
            { state: '/meter/offlinemetercost', name: 'Offline Meter Cost' },
            { state: '/meter/offlinemeterbatch', name: 'Offline Meter Batch Analysis' },
            { state: '/meter/offlinemetersaving', name: 'Offline Meter Saving' },
            { state: '/meter/offlinemeterplan', name: 'Offline Meter Plan' },
            { state: '/meter/offlinemeterinput', name: 'Offline Meter Input' },
            { state: '/meter/baselinemeterinput', name: 'MENU.WEB.BASELINE_METER_INPUT' }
        ]
    },
    {
        groupTitle: 'Tenant Data',
        routes: [
            { state: '/tenant/energycategory', name: 'Energy Category Data' },
            { state: '/tenant/energyitem', name: 'Energy Item Data' },
            { state: '/tenant/carbon', name: 'Carbon' },
            { state: '/tenant/cost', name: 'Cost' },
            { state: '/tenant/load', name: 'Load' },
            { state: '/tenant/statistics', name: 'Statistics' },
            { state: '/tenant/saving', name: 'Saving' },
            { state: '/tenant/plan', name: 'Plan' },
            { state: '/tenant/bill', name: 'Tenant Bill' },
            { state: '/tenant/batch', name: 'Batch Analysis' },
            { state: '/tenant/comparison', name: 'Tenant Comparison' }
        ]
    },
    {
        groupTitle: 'Store Data',
        routes: [
            { state: '/store/energycategory', name: 'Energy Category Data' },
            { state: '/store/energyitem', name: 'Energy Item Data' },
            { state: '/store/carbon', name: 'Carbon' },
            { state: '/store/cost', name: 'Cost' },
            { state: '/store/load', name: 'Load' },
            { state: '/store/statistics', name: 'Statistics' },
            { state: '/store/saving', name: 'Saving' },
            { state: '/store/plan', name: 'Plan' },
            { state: '/store/batch', name: 'Batch Analysis' },
            { state: '/store/comparison', name: 'Store Comparison' }
        ]
    },
    {
        groupTitle: 'Shopfloor Data',
        routes: [
            { state: '/shopfloor/energycategory', name: 'Energy Category Data' },
            { state: '/shopfloor/energyitem', name: 'Energy Item Data' },
            { state: '/shopfloor/carbon', name: 'Carbon' },
            { state: '/shopfloor/cost', name: 'Cost' },
            { state: '/shopfloor/load', name: 'Load' },
            { state: '/shopfloor/statistics', name: 'Statistics' },
            { state: '/shopfloor/saving', name: 'Saving' },
            { state: '/shopfloor/plan', name: 'Plan' },
            { state: '/shopfloor/batch', name: 'Batch Analysis' },
            { state: '/shopfloor/comparison', name: 'Shopfloor Comparison' }
        ]
    },
    {
        groupTitle: 'Combined Equipment Data',
        routes: [
            { state: '/combinedequipment/energycategory', name: 'Energy Category Data' },
            { state: '/combinedequipment/energyitem', name: 'Energy Item Data' },
            { state: '/combinedequipment/carbon', name: 'Carbon' },
            { state: '/combinedequipment/cost', name: 'Cost' },
            { state: '/combinedequipment/output', name: 'Output' },
            { state: '/combinedequipment/income', name: 'Income' },
            { state: '/combinedequipment/efficiency', name: 'Efficiency' },
            { state: '/combinedequipment/load', name: 'Load' },
            { state: '/combinedequipment/statistics', name: 'Statistics' },
            { state: '/combinedequipment/saving', name: 'Saving' },
            { state: '/combinedequipment/plan', name: 'Plan' },
            { state: '/combinedequipment/batch', name: 'Batch Analysis' },
            { state: '/combinedequipment/comparison', name: 'Combined Equipment Comparison' }
        ]
    },
    {
        groupTitle: 'Auxiliary System',
        routes: [
            { state: '/auxiliarysystem/energyflowdiagram', name: 'Energy Flow Diagram' },
            { state: '/auxiliarysystem/distributionsystem', name: 'Distribution System' }
        ]
    },
    {
        groupTitle: 'Product Carbon Footprint',
        routes: [
            { state: '/carbon/supply', name: 'Supply Material Maintenance' },
            { state: '/carbon/dictionary', name: 'Product Carbon Dictionary' },
            { state: '/carbon/footprint', name: 'Product Carbon Footprint Accounting' },
            { state: '/carbon/asset', name: 'MENU.WEB.CARBON_ASSET_MANAGEMENT' }
        ]
    },
    {
        groupTitle: 'Knowledge Base',
        routes: [
            { state: '/knowledgebase', name: 'Knowledge Base' }
        ]
    },
    {
        groupTitle: 'MENU.AI.ASSISTANT',
        routes: [
            { state: '/ai-assistant', name: 'MENU.AI.ASSISTANT' }
        ]
    }
];

app.controller('MenuTemplateController', function (
    $scope,
    $rootScope,
    $window,
    $uibModal,
    MenuTemplateService,
    toaster,
    $translate,
    SweetAlert,
    $state) {
    $scope.cur_user = JSON.parse($window.localStorage.getItem("myems_admin_ui_current_user"));
    $scope.menuTemplates = [];
    $scope.templateTypeOptions = [
        { value: 'admin', label: '管理端' },
        { value: 'web', label: '用户端' },
        { value: 'hybrid', label: '双端' }
    ];

    function translateLabel(label) {
        if (!angular.isString(label) || label.trim().length === 0) {
            return '';
        }

        var translated = $translate.instant(label);
        return translated || label;
    }

    function buildDisplayName(groupTitle, leafTitle) {
        var translatedGroupTitle = translateLabel(groupTitle);
        var translatedLeafTitle = translateLabel(leafTitle);
        if (translatedGroupTitle && translatedLeafTitle && translatedGroupTitle !== translatedLeafTitle) {
            return translatedGroupTitle + ' / ' + translatedLeafTitle;
        }
        return translatedLeafTitle || translatedGroupTitle;
    }

    function buildAdminMenus() {
        return ($state.get() || [])
            .map(function(state, index) {
                return {
                    state: state,
                    index: index
                };
            })
            .filter(function(entry) {
                var state = entry.state;
                if (!state || state.abstract === true || !angular.isString(state.name)) {
                    return false;
                }

                if (!ADMIN_MENU_ROUTE_META[state.name]) {
                    return false;
                }

                return !!(state.data && angular.isString(state.data.pageTitle) && state.data.pageTitle.trim().length > 0);
            })
            .sort(function(left, right) {
                var leftMeta = ADMIN_MENU_ROUTE_META[left.state.name];
                var rightMeta = ADMIN_MENU_ROUTE_META[right.state.name];
                var groupOrderDiff = leftMeta.group_order - rightMeta.group_order;
                if (groupOrderDiff !== 0) {
                    return groupOrderDiff;
                }
                var routeOrderDiff = leftMeta.route_order - rightMeta.route_order;
                if (routeOrderDiff !== 0) {
                    return routeOrderDiff;
                }
                return left.index - right.index;
            })
            .map(function(entry) {
                var state = entry.state;
                var groupTitle = ADMIN_MENU_ROUTE_META[state.name].group_title;
                return {
                    state: state.name,
                    group_title: groupTitle,
                    name: state.data.pageTitle,
                    display_name: buildDisplayName(groupTitle, state.data.pageTitle)
                };
            });
    }

    function buildWebMenus() {
        return WEB_MENU_SECTIONS.reduce(function(result, section) {
            var routes = section.routes || [];
            routes.forEach(function(route) {
                result.push({
                    state: route.state,
                    group_title: section.groupTitle,
                    name: route.name,
                    display_name: buildDisplayName(section.groupTitle, route.name)
                });
            });
            return result;
        }, []);
    }

    $scope.adminMenus = buildAdminMenus();
    $scope.webMenus = buildWebMenus();

    $scope.getMenuTemplateData = function(menuTemplate) {
        let data = {};
        try {
            data = JSON.parse(menuTemplate.data || '{}');
        } catch (e) {
            data = {};
        }

        return {
            template_type: data.template_type || 'admin',
            admin_routes: angular.isArray(data.admin_routes) ? data.admin_routes : [],
            web_routes: angular.isArray(data.web_routes) ? data.web_routes : []
        };
    };

    $scope.getTranslatedRouteNames = function(menus, routes) {
        const routeSet = new Set(routes || []);
        return (menus || []).filter(function(menu) {
            return routeSet.has(menu.state);
        }).map(function(menu) {
            return menu.display_name || buildDisplayName(menu.group_title, menu.name);
        }).join(' / ');
    };

    $scope.getTemplateTypeLabel = function(templateType) {
        if (templateType === 'web') {
            return '用户端';
        }
        if (templateType === 'hybrid') {
            return '双端';
        }
        return '管理端';
    };

    $scope.getRouteNames = function(menuTemplate) {
        const data = $scope.getMenuTemplateData(menuTemplate);
        const adminRouteNames = $scope.getTranslatedRouteNames($scope.adminMenus, data.admin_routes);
        const webRouteNames = $scope.getTranslatedRouteNames($scope.webMenus, data.web_routes);
        const sections = [];
        if (adminRouteNames) {
            sections.push('管理端: ' + adminRouteNames);
        }
        if (webRouteNames) {
            sections.push('用户端: ' + webRouteNames);
        }
        return sections.join(' | ');
    };

    $scope.getAllMenuTemplates = function () {
        let headers = { "User-UUID": $scope.cur_user.uuid, "Token": $scope.cur_user.token };
        MenuTemplateService.getAllMenuTemplates(headers, function (response) {
            if (angular.isDefined(response.status) && response.status === 200) {
                $scope.menuTemplates = response.data;
            } else {
                $scope.menuTemplates = [];
            }
        });
    };

    $scope.addMenuTemplate = function () {
        var modalInstance = $uibModal.open({
            templateUrl: 'views/users/menutemplate/menutemplate.model.html',
            controller: 'ModalAddMenuTemplateCtrl',
            windowClass: "animated fadeIn",
            resolve: {
                params: function () {
                    return {
                        adminMenus: angular.copy($scope.adminMenus),
                        webMenus: angular.copy($scope.webMenus)
                    };
                }
            }
        });

        modalInstance.result.then(function (menuTemplate) {
            let headers = { "User-UUID": $scope.cur_user.uuid, "Token": $scope.cur_user.token };
            MenuTemplateService.addMenuTemplate(menuTemplate, headers, function (response) {
                if (angular.isDefined(response.status) && response.status === 201) {
                    toaster.pop({
                        type: "success",
                        title: $translate.instant("TOASTER.SUCCESS_TITLE"),
                        body: $translate.instant("TOASTER.SUCCESS_ADD_BODY", { template: $translate.instant("USER.MENU_PERMISSION_TEMPLATE") }),
                        showCloseButton: true,
                    });
                    $scope.getAllMenuTemplates();
                } else {
                    toaster.pop({
                        type: "error",
                        title: $translate.instant("TOASTER.ERROR_ADD_BODY", { template: $translate.instant("USER.MENU_PERMISSION_TEMPLATE") }),
                        body: $translate.instant(response.data.description),
                        showCloseButton: true,
                    });
                }
            });
        });
        $rootScope.modalInstance = modalInstance;
    };

    $scope.editMenuTemplate = function (menuTemplate) {
        var modalInstance = $uibModal.open({
            windowClass: "animated fadeIn",
            templateUrl: 'views/users/menutemplate/menutemplate.model.html',
            controller: 'ModalEditMenuTemplateCtrl',
            resolve: {
                params: function () {
                    return {
                        menuTemplate: angular.copy(menuTemplate),
                        adminMenus: angular.copy($scope.adminMenus),
                        webMenus: angular.copy($scope.webMenus)
                    };
                }
            }
        });

        modalInstance.result.then(function (modifiedMenuTemplate) {
            let headers = { "User-UUID": $scope.cur_user.uuid, "Token": $scope.cur_user.token };
            MenuTemplateService.editMenuTemplate(modifiedMenuTemplate, headers, function (response) {
                if (angular.isDefined(response.status) && response.status === 200) {
                    toaster.pop({
                        type: "success",
                        title: $translate.instant("TOASTER.SUCCESS_TITLE"),
                        body: $translate.instant("TOASTER.SUCCESS_UPDATE_BODY", { template: $translate.instant("USER.MENU_PERMISSION_TEMPLATE") }),
                        showCloseButton: true,
                    });
                    $scope.getAllMenuTemplates();
                } else {
                    toaster.pop({
                        type: "error",
                        title: $translate.instant("TOASTER.ERROR_UPDATE_BODY", { template: $translate.instant("USER.MENU_PERMISSION_TEMPLATE") }),
                        body: $translate.instant(response.data.description),
                        showCloseButton: true,
                    });
                }
            });
        });
        $rootScope.modalInstance = modalInstance;
    };

    $scope.deleteMenuTemplate = function (menuTemplate) {
        SweetAlert.swal({
            title: $translate.instant("SWEET.TITLE"),
            text: $translate.instant("SWEET.TEXT"),
            type: "warning",
            showCancelButton: true,
            confirmButtonColor: "#DD6B55",
            confirmButtonText: $translate.instant("SWEET.CONFIRM_BUTTON_TEXT"),
            cancelButtonText: $translate.instant("SWEET.CANCEL_BUTTON_TEXT"),
            closeOnConfirm: true,
            closeOnCancel: true
        }, function (isConfirm) {
            if (isConfirm) {
                let headers = { "User-UUID": $scope.cur_user.uuid, "Token": $scope.cur_user.token };
                MenuTemplateService.deleteMenuTemplate(menuTemplate, headers, function (response) {
                    if (angular.isDefined(response.status) && response.status === 204) {
                        toaster.pop({
                            type: "success",
                            title: $translate.instant("TOASTER.SUCCESS_TITLE"),
                            body: $translate.instant("TOASTER.SUCCESS_DELETE_BODY", { template: $translate.instant("USER.MENU_PERMISSION_TEMPLATE") }),
                            showCloseButton: true,
                        });
                        $scope.getAllMenuTemplates();
                    } else {
                        toaster.pop({
                            type: "error",
                            title: $translate.instant("TOASTER.ERROR_DELETE_BODY", { template: $translate.instant("USER.MENU_PERMISSION_TEMPLATE") }),
                            body: $translate.instant(response.data.description),
                            showCloseButton: true,
                        });
                    }
                });
            }
        });
    };
    $scope.getAllMenuTemplates();
});

app.controller('ModalAddMenuTemplateCtrl', function ($scope, $uibModalInstance, params) {
    $scope.operation = "USER.ADD_MENU_PERMISSION_TEMPLATE";
    $scope.adminMenus = params.adminMenus || [];
    $scope.webMenus = params.webMenus || [];
    $scope.menuTemplate = {
        template_type: 'admin',
        selected_admin_routes: [],
        selected_web_routes: []
    };

    $scope.templateTypeOptions = [
        { value: 'admin', label: '管理端' },
        { value: 'web', label: '用户端' },
        { value: 'hybrid', label: '双端' }
    ];

    $scope.getMenuDisplayName = function(menu) {
        if (!menu) {
            return '';
        }
        return menu.display_name || menu.name || menu.state || '';
    };

    $scope.showAdminMenus = function() {
        return $scope.menuTemplate.template_type === 'admin' || $scope.menuTemplate.template_type === 'hybrid';
    };

    $scope.showWebMenus = function() {
        return $scope.menuTemplate.template_type === 'web' || $scope.menuTemplate.template_type === 'hybrid';
    };

    $scope.isHybridSelected = function() {
        return $scope.menuTemplate.template_type === 'hybrid';
    };

    $scope.isRouteSelected = function(menu, routeType) {
        const routeKey = routeType === 'web' ? 'selected_web_routes' : 'selected_admin_routes';
        return ($scope.menuTemplate[routeKey] || []).indexOf(menu.state) >= 0;
    };

    $scope.toggleRoute = function(menu, routeType) {
        const routeKey = routeType === 'web' ? 'selected_web_routes' : 'selected_admin_routes';
        const selectedRoutes = $scope.menuTemplate[routeKey] || [];
        const existingIndex = selectedRoutes.indexOf(menu.state);
        if (existingIndex >= 0) {
            selectedRoutes.splice(existingIndex, 1);
        } else {
            selectedRoutes.push(menu.state);
        }
        $scope.menuTemplate[routeKey] = selectedRoutes;
    };

    $scope.ok = function () {
        $scope.menuTemplate.data = JSON.stringify({
            template_type: $scope.menuTemplate.template_type || 'admin',
            admin_routes: $scope.menuTemplate.selected_admin_routes || [],
            web_routes: $scope.menuTemplate.selected_web_routes || []
        });
        delete $scope.menuTemplate.template_type;
        delete $scope.menuTemplate.selected_admin_routes;
        delete $scope.menuTemplate.selected_web_routes;
        $uibModalInstance.close($scope.menuTemplate);
    };

    $scope.cancel = function () {
        $uibModalInstance.dismiss('cancel');
    };
});

app.controller('ModalEditMenuTemplateCtrl', function ($scope, $uibModalInstance, params) {
    $scope.operation = "USER.EDIT_MENU_PERMISSION_TEMPLATE";
    $scope.adminMenus = params.adminMenus || [];
    $scope.webMenus = params.webMenus || [];
    $scope.menuTemplate = params.menuTemplate;
    let data = {};
    try {
        data = JSON.parse($scope.menuTemplate.data || '{}');
    } catch (e) {
        data = {};
    }
    $scope.menuTemplate.template_type = data.template_type || 'admin';
    $scope.menuTemplate.selected_admin_routes = data.admin_routes || [];
    $scope.menuTemplate.selected_web_routes = data.web_routes || [];

    $scope.templateTypeOptions = [
        { value: 'admin', label: '管理端' },
        { value: 'web', label: '用户端' },
        { value: 'hybrid', label: '双端' }
    ];

    $scope.getMenuDisplayName = function(menu) {
        if (!menu) {
            return '';
        }
        return menu.display_name || menu.name || menu.state || '';
    };

    $scope.showAdminMenus = function() {
        return $scope.menuTemplate.template_type === 'admin' || $scope.menuTemplate.template_type === 'hybrid';
    };

    $scope.showWebMenus = function() {
        return $scope.menuTemplate.template_type === 'web' || $scope.menuTemplate.template_type === 'hybrid';
    };

    $scope.isHybridSelected = function() {
        return $scope.menuTemplate.template_type === 'hybrid';
    };

    $scope.isRouteSelected = function(menu, routeType) {
        const routeKey = routeType === 'web' ? 'selected_web_routes' : 'selected_admin_routes';
        return ($scope.menuTemplate[routeKey] || []).indexOf(menu.state) >= 0;
    };

    $scope.toggleRoute = function(menu, routeType) {
        const routeKey = routeType === 'web' ? 'selected_web_routes' : 'selected_admin_routes';
        const selectedRoutes = $scope.menuTemplate[routeKey] || [];
        const existingIndex = selectedRoutes.indexOf(menu.state);
        if (existingIndex >= 0) {
            selectedRoutes.splice(existingIndex, 1);
        } else {
            selectedRoutes.push(menu.state);
        }
        $scope.menuTemplate[routeKey] = selectedRoutes;
    };

    $scope.ok = function () {
        $scope.menuTemplate.data = JSON.stringify({
            template_type: $scope.menuTemplate.template_type || 'admin',
            admin_routes: $scope.menuTemplate.selected_admin_routes || [],
            web_routes: $scope.menuTemplate.selected_web_routes || []
        });
        delete $scope.menuTemplate.template_type;
        delete $scope.menuTemplate.selected_admin_routes;
        delete $scope.menuTemplate.selected_web_routes;
        $uibModalInstance.close($scope.menuTemplate);
    };

    $scope.cancel = function () {
        $uibModalInstance.dismiss('cancel');
    };
});