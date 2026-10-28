	(function () {
		'use strict';
//$rootScope.taskContext.rpcStatus = 'Not Connected';
		angular.module('ariaNg').run(['$rootScope', '$window', '$location', 'ariaNgSettingService', 'ariaNgConstants', function ($rootScope, $window, $location, ariaNgSettingService, ariaNgConstants) {
			var getDefaultRpcHost = function () {
				var currentHost = $location.$$host;
				if (currentHost) {
					return currentHost;
				}
				return ariaNgConstants.defaultHost;
			};
			var protocol = ariaNgSettingService.getCurrentRpcProtocol();
			var host = ariaNgSettingService.getRpcHost();
			var port = ariaNgSettingService.getRpcPort();
			var secret = ariaNgSettingService.getCurrentRpcSecret();
			ariaNgSettingService.setRpcProtocol("http");
			// ariaNgSettingService.setRpcProtocol("ws");
			ariaNgSettingService.setRpcHost(getDefaultRpcHost());
			ariaNgSettingService.setRpcPort(6800);
			ariaNgSettingService.setRpcSecret('Passw0rd');
			ariaNgSettingService.setLanguage('zh_Hans');
			if (secret == "") {
                $window.location.reload();
			}
            // if ($rootScope.taskContext.rpcStatus != 'Connected') {
            //     setTimeout(function() { $window.location.reload(); }, 1000 * 30);
            // }
		}]);
	}());
