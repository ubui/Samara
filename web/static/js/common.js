
var isRestartSysFadein = false;
var checkSysRestartStatus = function () {
    if (!isRestartSysFadein) {
        $(".main > .loading").fadeIn("fast");
        isRestartSysFadein = true;
    }
    //https://developer.mozilla.org/en-US/docs/Web/API/WindowOrWorkerGlobalScope/fetch
    var external = location.href;
    if (window.fetch) {
        // must be chrome or firefox which have native fetch
        fetch(external, {'mode':'no-cors'})
            .then(function (r) {
                // external is reachable; but failed due to cors
                // fetch will pass though if it's a cors error
                alertify.notify('Reboot successfully.', 'success', 5, function(){  console.log('Reboot Done.'); });
                console.log(r);
                location.reload();
            })
            .catch(function () {
                console.log('external is _not_ reachable');
                setTimeout('checkSysRestartStatus()', 1000);
            });
    } else {
        // must be non-updated safari or older IE...
        // I don't know how to find error type in this case
        alert("your browser does not support native fetch api. please reload page manually.");
    }
};

var processCheck = function (process_name, target_ele) {
    let targetBtn = $(target_ele);
    let fail_class = 'cbi-input-remove';
    let succ_class = 'cbi-input-save';
    $.get("/api/system/process/status?name="+process_name, function (resp) {
        if (resp.error == 0 && resp.data.is_running == 1) {
            targetBtn.removeClass(fail_class);
            targetBtn.addClass(succ_class);
            targetBtn.val(process_name + " Running");
        } else {
            targetBtn.removeClass(succ_class);
            targetBtn.addClass(fail_class);
            targetBtn.val(process_name + " Stopped");
        }
    });
};

// from Underscore.js
var isFunction = function(obj) {
    return !!(obj && obj.constructor && obj.call && obj.apply);
};

var statusCheck = function (api_url, target_ele, cb) {
    let targetBtn = $(target_ele);
    let fail_class = 'cbi-input-remove';
    let succ_class = 'cbi-input-save';
    let that = this;
    $.get(api_url, function (resp) {
        targetBtn.val(resp.status);
        let succ = false;
        if (/work|ok|success/.test(resp.status)) {
            targetBtn.removeClass(fail_class);
            targetBtn.addClass(succ_class);
            succ = true;
        } else {
            targetBtn.removeClass(succ_class);
            targetBtn.addClass(fail_class);
        }
        if (isFunction(cb)) {
            cb.call(that, succ);
        }
    });
};