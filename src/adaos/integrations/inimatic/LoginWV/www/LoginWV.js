var exec = require('cordova/exec')

module.exports = {
	login: function (url, checkLoginJs, success, failure) {
		exec(success, failure, 'LoginWV', 'login', [url, checkLoginJs])
	},
}
