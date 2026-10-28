"""NDM 同款界面渲染。

页面结构、类名、导航层级都照固件原版 darkmatter 模板来：
    common/header.html   → layout() 的 header + 左侧 nav
    status/*.html        → status_*()
    appcfg/*.html        → app_*()
    system/*.html        → system_*()
只用 Python 标准库拼 HTML，服务端渲染，跟前端框架无关。
"""

import html as _h
import os
import platform
import socket
import subprocess
import time

from . import cron as cron_mod
from . import dockerui, i18n, services, smart

APP_VERSION = "2.0.0"

# 原版导航：Status / Apps / System / Logout
NAV = [
    ("Status", [
        ("Overview", "/status/overview"),
        ("Apps", "/status/apps"),
        ("Disks", "/status/disks"),
        ("Network Traffic", "/status/traffic"),
        ("System Log", "/status/syslog"),
        ("Kernel Log", "/status/dmesg"),
        ("Processes", "/status/processes"),
        ("Failed Login", "/status/banned"),
        ("About", "/status/about"),
    ]),
    ("System", [
        ("System", "/admin/system/system"),
        ("Administration", "/admin/system/admin"),
        ("Startup", "/admin/system/startup"),
        ("Scheduled Tasks", "/admin/system/crontab"),
        ("Docker", "/admin/system/docker"),
        ("Tool", "/admin/system/tool"),
    ]),
]


CSS_VER = "samara-2.0.0"

# ---------------------------------------------------------------- 壁纸（原版 darkmatter 同款）
# 原版登录页背景走 /api/wallpaper 动态取图；素材在 web/static/img/wallpaper/（default + 1~5）
WALLPAPER_NAMES = ("default", "1", "2", "3", "4", "5")
WALLPAPER_CONF = "/etc/samara/wallpaper"


def wallpaper_path(name):
    """壁纸文件路径；name 必须在白名单内，防路径穿越。"""
    if name not in WALLPAPER_NAMES:
        name = "default"
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "web", "static", "img", "wallpaper", name + ".jpg")


def current_wallpaper():
    """当前壁纸名（/etc/samara/wallpaper 存一行，默认 default）。"""
    try:
        with open(WALLPAPER_CONF, encoding="utf-8") as f:
            name = f.read().strip()
        return name if name in WALLPAPER_NAMES else "default"
    except OSError:
        return "default"


def set_wallpaper(name):
    if name not in WALLPAPER_NAMES:
        return False
    try:
        with open(WALLPAPER_CONF, "w", encoding="utf-8") as f:
            f.write(name + "\n")
        return True
    except OSError:
        return False


def esc(value):
    return _h.escape("" if value is None else str(value))


def run(cmd, timeout=8):
    """跑一条命令，返回 stdout（失败给空串）。"""
    try:
        out = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                             timeout=timeout, text=True)
        return (out.stdout or "") + (out.stderr or "")
    except Exception:
        return ""


def bar(used, total, label=None):
    """原版总览里那种内联进度条。"""
    try:
        used = float(used)
        total = float(total)
        pct = int(100 * used / total) if total > 0 else 0
    except Exception:
        used, total, pct = 0, 0, 0
    pct = max(0, min(pct, 100))
    text = label or "%d%%" % pct
    return (
        '<div style="width:200px;position:relative;border:1px solid #999999;background:#24231f">'
        '<div style="background-color:#8fa32a;width:%d%%;height:15px"></div>'
        '<div style="position:absolute;left:0;top:0;text-align:center;width:100%%;'
        'color:#eee;font-size:11px;line-height:15px">%s</div></div>' % (pct, esc(text))
    )


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def human_rate(bps):
    return human_size(bps) + "/s"


def human_uptime(seconds):
    seconds = int(seconds or 0)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    mins = rest // 60
    if days:
        return "%d days, %d hours, %d minutes" % (days, hours, mins)
    if hours:
        return "%d hours, %d minutes" % (hours, mins)
    return "%d minutes" % mins


# ---------------------------------------------------------------- 布局

def _quicklinks(ctx, host):
    """顶栏快捷方式：装好且有 Web 入口的应用常驻（运行中绿色、停止灰色）。"""
    out = []
    try:
        items = services.list_with_state(ctx.apps(), host)
    except Exception:
        items = []
    for app in items:
        if not app.get("ui"):
            continue
        css = "label success" if app.get("active") else "label"
        out.append(
            '<a target="_blank" rel="noreferrer" href="%s" title="%s">'
            '<button class="%s"><span class="mobile-hide">%s</span></button></a>'
            % (esc(app["ui"]), esc(app.get("desc", "")), css, esc(app.get("name", ""))))
    try:
        for c in dockerui.containers():
            url = dockerui.web_url(c, host)
            if not url:
                continue
            css = "label success" if c.get("running") else "label"
            out.append(
                '<a target="_blank" rel="noreferrer" href="%s" title="%s">'
                '<button class="%s"><span class="mobile-hide">%s</span></button></a>'
                % (esc(url), esc(c.get("image", "")), css, esc(c.get("name", ""))))
    except Exception:
        pass
    return "".join(out)


def _nav(path):
    rows = []
    for group, items in NAV:
        open_cls = "slide open" if any(path.startswith(p) for _, p in items) else "slide"
        subs = "".join(
            '<li%s><a data-title="%s" href="%s">%s</a></li>'
            % (' class="active"' if path == p else "", esc(t), esc(p), esc(t))
            for t, p in items)
        rows.append(
            '<li class="%s"><a class="menu" data-title="%s" href="#">%s</a>'
            '<ul class="slide-menu">%s</ul></li>' % (open_cls, esc(group), esc(group), subs))
    rows.append('<li><a data-title="Logout" id="logout-button" href="/logout">Logout</a></li>')
    return "".join(rows)


def luci_location(path):
    """原版 script.js 靠 luciLocation 高亮当前菜单，映射页面路径。"""
    parts = [x for x in path.strip("/").split("/") if x]
    if len(parts) == 2 and parts[0] == "status":
        return '["admin", "status", "%s"]' % parts[1]
    if parts and parts[0] == "appcfg":
        return '["admin", "status", "apps"]' 
    if len(parts) == 3 and parts[0] == "admin" and parts[1] == "system":
        return '["admin", "system", "%s"]' % parts[2]
    return "[]"


def layout(ctx, path, body, head=""):
    host = getattr(ctx, "host_header", "") or socket.gethostname()
    title = esc(ctx.title)
    _html_doc = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>%(title)s</title>
    <meta content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=0" name="viewport" />
    <meta name="format-detection" content="telephone=no, email=no"/>
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="theme-color" content="#0099CC">
    <meta name="msapplication-TileColor" content="#0099CC">
    <link rel="stylesheet" href="/static/css/style.css?v=%(ver)s">
    <link rel="shortcut icon" href="/static/img/favicon.ico">
    <link rel="stylesheet" href="/static/js/alertifyjs/css/alertify.min.css" />
    <link rel="stylesheet" href="/static/js/alertifyjs/css/themes/default.min.css" />
    <script src="/static/js/jquery.min.js"></script>
    <script src="/static/js/alertifyjs/alertify.min.js"></script>
    <script>alertify.set('notifier','position', 'top-center');</script>
    <script>var luciLocation = %(loc)s;</script>
    %(head)s
</head>
<body class="lang_en logged-in">

<header>
    <div class="container">
        <span class="showSide"></span>
        <a class="brand" href="/status/overview">%(title)s</a>
        <div class="pull-right">%(quick)s</div>
    </div>
</header>

<div class="main">
    <div style="display:none;" class="loading"><span><div class="loading-img"></div>Loading...</span></div>
    <div class="main-left">
        <ul class="nav">%(nav)s</ul>
    </div>
    <div class="main-right">
        <div class="darkMask"></div>
        <div id="maincontent">
            <div class="container">
%(body)s
            </div>
        </div>
    </div>
</div>
<script src="/static/js/script.js"></script>
</body>
</html>
""" % {
        "title": title,
        "ver": CSS_VER,
        "quick": _quicklinks(ctx, host),
        "nav": _nav(path),
        "loc": luci_location(path),
        "body": body,
        "head": head,
    }
    return i18n.translate(_html_doc)


def login_page(ctx, error=""):
    err = ('<div class="cbi-value"><div class="cbi-value-field" '
           'style="color:#e08a8a;padding-left:0">%s</div></div>' % esc(error)) if error else ""
    _doc = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Login - %(title)s</title>
    <meta content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=0" name="viewport" />
    <link rel="stylesheet" href="/static/css/style.css?v=%(ver)s">
    <link rel="shortcut icon" href="/static/img/favicon.ico">
    <style>
        .background{position:absolute;right:0;top:0;bottom:0;left:0;background-size:cover;background-position:center}
        .main{color:#CCCCCC}
        .container{position:relative}
        .btn{display:inline-block;padding:6px 32px;font-size:14px;line-height:12px;text-align:center;
             cursor:pointer;border:1px solid transparent;border-radius:4px}
        .btn-primary{color:#fafafa !important;border:1px solid rgb(121,144,55) !important;
             background:radial-gradient(ellipse at center,#7eaa0f 0%%,#6d8006 100%%) !important}
        input{background-color:#a7a3a3a3;box-shadow:inset 0 0 1px #000}
    </style>
</head>
<body class="lang_en">
<div class="background" style="background-image:url(/api/wallpaper)"></div>
<div class="main" style="text-align:center">
    <div class="container">
        <form id="login-form" method="post" action="/login"><div class="cbi-map">
            <h2 name="content" style="text-align:center">%(title)s</h2>
            %(err)s
            <div class="cbi-value">
                <label class="cbi-value-title">Username</label>
                <div class="cbi-value-field">
                    <input class="cbi-input-user" type="text" name="username" value="root" autocomplete="current-user"/>
                </div>
            </div>
            <div class="cbi-value cbi-value-last">
                <label class="cbi-value-title">Password</label>
                <div class="cbi-value-field">
                    <input class="cbi-input-password" type="password" name="password" autocomplete="current-password"/>
                </div>
            </div>
            <div style="text-align:center;margin-top:35px">
                <input type="submit" value="Login" class="btn btn-primary" />
            </div>
        </div></form>
    </div>
</div>
</body>
</html>
""" % {"title": esc(ctx.title), "ver": CSS_VER, "err": err}
    return i18n.translate(_doc)


def section(title, inner, descr=""):
    d = '<div class="cbi-map-descr">%s</div>' % esc(descr) if descr else ""
    return ('<fieldset class="cbi-section"><legend>%s</legend>%s%s</fieldset>'
            % (esc(title), d, inner))


def value_row(label, field_html):
    return ('<div class="cbi-value"><label class="cbi-value-title">%s</label>'
            '<div class="cbi-value-field">%s</div></div>' % (esc(label), field_html))


def kv_table(pairs):
    rows = "".join(
        '<tr><td style="width:34%%;color:#a9a49a">%s</td><td>%s</td></tr>' % (esc(k), v)
        for k, v in pairs)
    return '<table width="100%%" cellspacing="10">%s</table>' % rows


def table(headers, rows, empty="No data"):
    if not rows:
        return '<div class="cbi-section-descr">%s</div>' % esc(empty)
    head = "".join("<th>%s</th>" % esc(h) for h in headers)
    body = "".join(
        "<tr>%s</tr>" % "".join("<td>%s</td>" % ("" if c is None else c) for c in r)
        for r in rows)
    return '<table class="cbi-section-table"><tr class="cbi-section-table-titles">%s</tr>%s</table>' % (head, body)


# ---------------------------------------------------------------- 状态页

def status_overview(ctx):
    d = ctx.collector.snapshot()
    mem = d.get("memory") or {}
    sysinfo = kv_table([
        ("Hostname", esc(d.get("hostname", ""))),
        ("Model", esc(platform.machine() or "")),
        ("Operating System", esc(d.get("os", ""))),
        ("Kernel Version", esc(d.get("kernel", ""))),
        ("Local Time", esc(time.strftime("%Y-%m-%d %H:%M:%S"))),
        ("Uptime", esc(human_uptime(d.get("uptime")))),
    ])
    cpu = d.get("cpu") or {}
    load = d.get("load") or [0, 0, 0]
    res = kv_table([
        ("Load Average", esc("%.2f, %.2f, %.2f" % tuple(list(load) + [0] * (3 - len(load))))),
        ("CPU Usage", esc("%s%%" % cpu.get("percent", 0)) + " &nbsp;<small>(%s cores)</small>" % esc(cpu.get("cores", 0))),
        ("Memory", bar(mem.get("used", 0), mem.get("total", 1),
                       "%s / %s" % (human_size(mem.get("used")), human_size(mem.get("total"))))),
        ("Swap", bar(mem.get("swap_used", 0), mem.get("swap_total", 1) or 1,
                     "%s / %s" % (human_size(mem.get("swap_used")), human_size(mem.get("swap_total"))))),
        ("Temperature", esc("%s °C" % d.get("temperature")) if d.get("temperature") else "N/A"),
    ])
    net = d.get("net") or {}
    ifaces = net.get("interfaces") or {}
    netrows = []
    for name, info in ifaces.items():
        if name == "lo":
            continue
        netrows.append([
            esc(name),
            esc(net.get("ipv4") if name == net.get("iface") else "-"),
            human_rate(info.get("rx_rate", 0)),
            human_rate(info.get("tx_rate", 0)),
            human_size(info.get("rx_total", 0)),
            human_size(info.get("tx_total", 0)),
        ])
    body = ('<h2 name="content">Status</h2>'
            + section("System", sysinfo)
            + section("Resources", res)
            + section("Network", table(["Interface", "Address", "RX Rate", "TX Rate", "RX Total", "TX Total"], netrows))
            + section("Storage", table(
                ["Filesystem", "Mount Point", "Used", "Size", "Usage"],
                [[esc(x.get("device", "")), esc(x.get("mount", "")),
                  human_size(x.get("used", 0)), human_size(x.get("total", 0)),
                  bar(x.get("used", 0), x.get("total", 1))]
                 for x in (d.get("disks") or [])], "No mounts")))
    return layout(ctx, "/status/overview", body)


def status_disks(ctx):
    d = ctx.collector.snapshot()
    mounts = d.get("disks") or []
    rows = [[esc(m.get("device", "")), esc(m.get("mount", "")), esc(m.get("fs", "")),
             human_size(m.get("used", 0)), human_size(m.get("total", 0)),
             bar(m.get("used", 0), m.get("total", 1))] for m in mounts]
    health_rows = []
    try:
        ov = smart.overview()
        for disk in (ov.get("disks") or []):
            health_rows.append([
                esc(disk.get("device", "")),
                esc(disk.get("model", "") or "-"),
                esc(disk.get("health", "") or "-"),
                esc("%s °C" % disk.get("temperature") if disk.get("temperature") else "-"),
                esc(disk.get("power_on", "") or "-"),
            ])
    except Exception:
        pass
    body = ('<h2 name="content">Disks</h2>'
            + section("Mount Points", table(
                ["Device", "Mount Point", "Type", "Used", "Size", "Usage"], rows, "No mount points"))
            + section("Health (SMART)", table(
                ["Device", "Model", "Health", "Temperature", "Power On"], health_rows,
                "SMART not available (install smartmontools)")))
    return layout(ctx, "/status/disks", body)


def status_traffic(ctx):
    d = ctx.collector.snapshot()
    ifaces = ((d.get("net") or {}).get("interfaces") or {})
    rows = []
    for name, info in ifaces.items():
        if name == "lo":
            continue
        rows.append([esc(name), human_rate(info.get("rx_rate", 0)), human_rate(info.get("tx_rate", 0)),
                     human_size(info.get("rx_total", 0)), human_size(info.get("tx_total", 0))])
    body = ('<h2 name="content">Network Traffic</h2>'
            '<div class="cbi-map-descr">Live rates, refreshed every 3 seconds.</div>'
            + section("Interfaces", table(
                ["Interface", "RX Rate", "TX Rate", "RX Total", "TX Total"], rows)))
    head = '<meta http-equiv="refresh" content="3">'
    return layout(ctx, "/status/traffic", body, head)


def _log_text(kind, lines=400):
    if kind == "dmesg":
        return run(["dmesg", "--ctime"], 6) or run(["dmesg"], 6)
    return run(["journalctl", "-n", str(lines), "--no-pager", "-o", "short-iso"], 8)


def status_log(ctx, kind):
    title = "System Log" if kind != "dmesg" else "Kernel Log"
    path = "/status/syslog" if kind != "dmesg" else "/status/dmesg"
    text = _log_text(kind)
    body = ('<h2 name="content">%s</h2>' % title
            + section(title,
                      '<textarea class="cbi-input-textarea" readonly rows="32" '
                      'style="width:100%%;font-family:ui-monospace,Consolas,monospace">%s</textarea>'
                      % esc(text))
            + '<div class="cbi-page-actions"><a class="cbi-button cbi-button-reload" href="%s">Refresh</a></div>' % path)
    return layout(ctx, path, body)


def status_processes(ctx):
    text = run(["ps", "aux", "--sort=-%cpu"], 8)
    rows = []
    for line in text.splitlines()[1:41]:
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        rows.append([esc(parts[0]), esc(parts[1]), esc(parts[2]), esc(parts[3]),
                     esc(parts[5]), esc(parts[10][:80])])
    body = ('<h2 name="content">Processes</h2>'
            + section("Top 40 by CPU", table(
                ["User", "PID", "CPU%", "MEM%", "VSZ", "Command"], rows)))
    return layout(ctx, "/status/processes", body)


def status_banned(ctx):
    text = run(["journalctl", "-u", "ssh", "-u", "sshd", "--no-pager", "-n", "400"], 8)
    rows = [[esc(l)] for l in text.splitlines() if "Failed password" in l or "Invalid user" in l][-30:]
    body = ('<h2 name="content">Failed Login</h2>'
            '<div class="cbi-map-descr">Recent failed SSH logins (last 30).</div>'
            + section("Failed Login Attempts",
                      table(["Message"], rows, "No failed logins recorded")))
    return layout(ctx, "/status/banned", body)


def status_about(ctx):
    d = ctx.collector.snapshot()
    info = kv_table([
        ("Version", esc(APP_VERSION)),
        ("Hostname", esc(d.get("hostname", ""))),
        ("Operating System", esc(d.get("os", ""))),
        ("Kernel", esc(d.get("kernel", ""))),
        ("Python", esc(platform.python_version())),
        ("Source", '<a href="https://github.com/ubui/Samara" target="_blank" rel="noreferrer">'
                   'github.com/ubui/Samara</a>'),
        ("Theme", "darkmatter"),
    ])
    body = ('<h2 name="content">About</h2>'
            + section("System", info)
            + section("Credits",
                      '<div class="cbi-section-descr" style="line-height:1.7">'
                      'Interface rebuilt to match <b>Nano Disk Manager (NDM)</b> — '
                      'darkmatter theme by chrono (GPLv2), original panel by HuangYeWuDeng.'
                      '</div>'))
    return layout(ctx, "/status/about", body)


# ---------------------------------------------------------------- 应用页

def status_apps(ctx):
    """状态 → 应用：控制面板 + 使用者自行安装的 Docker 应用。"""
    host = getattr(ctx, "host_header", "") or socket.gethostname()
    rows = [[
        "Dashboard",
        '<span class="label success">active</span>',
        "-",
        '<a href="/appcfg/dashboard"><button class="cbi-button cbi-button-apply">Open</button></a>',
    ]]
    try:
        cons = dockerui.containers()
    except Exception:
        cons = []
    for c in cons:
        url = dockerui.web_url(c, host)
        name = c.get("name", "")
        stat = '<span class="label %s">%s</span>' % (
            "success" if c.get("running") else "", esc(c.get("status", "")))
        open_btn = ('<a target="_blank" rel="noreferrer" href="%s">'
                    '<button class="cbi-button cbi-button-apply">Open</button></a>' % esc(url)) if url else ""
        act = ('<form method="post" action="/status/apps" style="margin:0">'
               '<input type="hidden" name="name" value="%s"/>'
               '<button class="cbi-button cbi-button-apply" name="action" value="start">Start</button> '
               '<button class="cbi-button cbi-button-reload" name="action" value="restart">Restart</button> '
               '<button class="cbi-button cbi-button-reset" name="action" value="stop">Stop</button>'
               '</form> %s') % (esc(name), open_btn)
        rows.append([esc(name), stat, esc(c.get("image", "")), act])
    body = ('<h2 name="content">Apps</h2>'
            '<div class="cbi-map-descr">Applications on this device. New apps are '
            'installed with Docker Compose.</div>'
            + section("Applications",
                      table(["Name", "Status", "Image", "Actions"], rows),
                      "Panel settings and Docker applications")
            + section("Install",
                      '<div class="cbi-section-descr">Deploy new applications with '
                      'Docker Compose: <a href="/admin/system/docker">System → Docker</a></div>',
                      "Compose / image mirrors / logs"))
    return layout(ctx, "/status/apps", body)


def appcfg_dashboard(ctx, config):
    host = getattr(ctx, "host_header", "") or socket.gethostname()
    info = kv_table([
        ("Brand", esc(ctx.title)),
        ("Listen", esc(config.listen)),
        ("HTTP Port", esc(config.port)),
        ("Session TTL", esc("%s s" % config.getint("auth", "session_ttl", 86400))),
        ("Config File", esc(config.path)),
        ("Password File", esc(ctx.password_file)),
        ("Web Root", esc(ctx.web_root)),
    ])
    cur = current_wallpaper()
    thumbs = "".join(
        '<label style="display:inline-block;margin:0 12px 12px 0;text-align:center;cursor:pointer">'
        '<input type="radio" name="wallpaper" value="%s"%s/> '
        '<img src="/static/img/wallpaper/%s.jpg" alt="%s" '
        'style="width:120px;height:68px;object-fit:cover;border-radius:4px;'
        'border:2px solid %s;display:block"/>'
        '<span style="font-size:12px;color:#a9a49a">%s</span></label>'
        % (n, " checked" if n == cur else "", n, n,
           "#7eaa0f" if n == cur else "#4f4e4a", n)
        for n in WALLPAPER_NAMES)
    wp_form = ('<form method="post" action="/appcfg/dashboard">'
               '<input type="hidden" name="action" value="wallpaper"/>'
               '<div style="padding:8px 0">%s</div>'
               '<div class="cbi-page-actions">'
               '<button class="cbi-button cbi-button-save">Save Wallpaper</button></div>'
               '</form>' % thumbs)
    body = ('<h2 name="content">Dashboard</h2>'
            + section("Panel", info, "Edit /etc/samara/samara.conf then restart samara.service")
            + section("Wallpaper", wp_form, "Login page background (original darkmatter wallpapers)")
            + section("Access", kv_table([
                ("Web UI", '<a href="http://%s:%s" target="_blank" rel="noreferrer">'
                           'http://%s:%s</a>' % (esc(host), config.port, esc(host), config.port)),
            ]))
            + section("Actions",
                      '<form method="post" action="/appcfg/dashboard">'
                      '<button type="submit" name="action" value="restart" '
                      'class="cbi-button cbi-button-reload">Restart Panel</button>'
                      '<button type="submit" name="action" value="clear-sessions" '
                      'class="cbi-button cbi-button-reset">Sign Out All Sessions</button>'
                      '</form>'))
    return layout(ctx, "/appcfg/dashboard", body)


# ---------------------------------------------------------------- 系统页

def system_system(ctx, config):
    d = ctx.collector.snapshot()
    hostname = d.get("hostname", "")
    tz = ""
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            tz = f.read().strip()
    except OSError:
        pass
    form = ('<form method="post" action="/admin/system/system">'
            + value_row("Hostname", '<input class="cbi-input-text" name="hostname" value="%s"/>' % esc(hostname))
            + value_row("Timezone", '<input class="cbi-input-text" name="timezone" value="%s"/>' % esc(tz))
            + value_row("System Time", '<input class="cbi-input-text" readonly value="%s"/>'
                        % esc(time.strftime("%Y-%m-%d %H:%M:%S")))
            + value_row("Uptime", '<input class="cbi-input-text" readonly value="%s"/>'
                        % esc(human_uptime(d.get("uptime"))))
            + '<div class="cbi-page-actions"><button class="cbi-button cbi-button-save">Save &amp; Apply</button></div>'
            '</form>')
    body = ('<h2 name="content">System</h2>' + section("General", form))
    return layout(ctx, "/admin/system/system", body)


def system_admin(ctx):
    pw = ('<form method="post" action="/admin/system/admin">'
          '<input type="hidden" name="action" value="password"/>'
          + value_row("Username", '<input class="cbi-input-text" name="username" value="root" readonly/>')
          + value_row("New Password", '<input class="cbi-input-password" type="password" name="password" required/>')
          + value_row("Confirm", '<input class="cbi-input-password" type="password" name="confirm" required/>')
          + '<div class="cbi-page-actions"><button class="cbi-button cbi-button-save">Change Password</button></div>'
          '</form>')
    reboot = ('<form method="post" action="/admin/system/admin" '
              'onsubmit="return confirm(\'Reboot now?\')">'
              '<input type="hidden" name="action" value="reboot"/>'
              '<button class="cbi-button cbi-button-reset">Reboot</button></form>')
    body = ('<h2 name="content">Administration</h2>'
            + section("Password", pw)
            + section("Reboot", reboot, "Host will go down for a moment."))
    return layout(ctx, "/admin/system/admin", body)


def system_startup(ctx):
    """开机自启：Docker 容器的重启策略（应用全部经 Docker 安装）。"""
    try:
        cons = dockerui.containers()
    except Exception:
        cons = []
    rows = []
    for c in cons:
        name = c.get("name", "")
        policy = dockerui.restart_policy(name) if name else "no"
        enabled = policy not in ("no", "")
        stat = '<span class="label %s">%s</span>' % (
            "success" if c.get("running") else "", "running" if c.get("running") else "stopped")
        auto = '<span class="label %s">%s</span>' % (
            "success" if enabled else "", "enabled" if enabled else "disabled")

        def btn(act, label, cls):
            return ('<button class="cbi-button cbi-button-%s" name="action" value="%s:%s">%s</button>'
                    % (cls, act, name, label))
        actions = (
            '<form method="post" action="/admin/system/startup" style="margin:0">'
            + (btn("stop", "Stop", "reset") if c.get("running") else btn("start", "Start", "apply"))
            + (btn("disable", "DISABLE AUTOSTART", "reset") if enabled
               else btn("enable", "ENABLE AUTOSTART", "apply"))
            + '</form>')
        rows.append([esc(name), esc(c.get("image", "")), stat, auto, actions])
    body = ('<h2 name="content">Startup</h2>'
            '<div class="cbi-map-descr">Control autostart of Docker containers at boot.</div>'
            + section("Containers", table(["Name", "Image", "Status", "Autostart", ""], rows,
                                          "No containers")))
    return layout(ctx, "/admin/system/startup", body)


def system_crontab(ctx):
    tasks = cron_mod.list_jobs()
    rows = []
    for job in tasks:
        rows.append([
            esc(job.get("name", "")),
            esc(job.get("schedule", "")),
            '<code>%s</code>' % esc((job.get("command", "") or "")[:70]),
            '<form method="post" action="/admin/system/crontab" style="margin:0">'
            '<input type="hidden" name="id" value="%s"/>'
            '<button class="cbi-button cbi-button-apply" name="action" value="run">Run</button> '
            '<button class="cbi-button cbi-button-remove" name="action" value="delete">Delete</button>'
            '</form>' % esc(job.get("id", "")),
        ])
    form = ('<form method="post" action="/admin/system/crontab">'
            '<input type="hidden" name="action" value="add"/>'
            + value_row("Name", '<input class="cbi-input-text" name="name" required/>')
            + value_row("Schedule", '<input class="cbi-input-text" name="schedule" value="0 3 * * *" required/>'
                                    '<div class="cbi-section-descr">minute hour day month weekday</div>')
            + value_row("Command", '<textarea class="cbi-input-textarea" name="command" rows="3" '
                                   'style="width:100%" required></textarea>')
            + '<div class="cbi-page-actions"><button class="cbi-button cbi-button-add">Add Task</button></div>'
            '</form>')
    body = ('<h2 name="content">Scheduled Tasks</h2>'
            + section("Tasks", table(["Name", "Schedule", "Command", "Actions"], rows, "No tasks") + form))
    return layout(ctx, "/admin/system/crontab", body)


def system_tool(ctx):
    cmds = [
        ("Disk Usage", ["df", "-h"]),
        ("Memory", ["free", "-m"]),
        ("Interfaces", ["ip", "-brief", "addr"]),
        ("Listening Ports", ["ss", "-lntup"]),
        ("Block Devices", ["lsblk"]),
    ]
    blocks = []
    for title, cmd in cmds:
        blocks.append(section(title,
                              '<textarea class="cbi-input-textarea" readonly rows="10" '
                              'style="width:100%%;font-family:ui-monospace,Consolas,monospace">%s</textarea>'
                              % esc(run(cmd, 6))))
    cur = i18n.get_lang()
    zh_cls = "label success" if cur == "zh" else "label"
    en_cls = "label success" if cur == "en" else "label"
    # 单行：语言 | [中文] [English]，当前语言高亮，点另一边切换
    lang_block = ('<div class="cbi-value" style="margin:12px 0 4px">'
                  '<label class="cbi-value-title">Language</label>'
                  '<div class="cbi-value-field">'
                  '<a href="/lang?set=zh"><button class="%s">中文</button></a> '
                  '<a href="/lang?set=en"><button class="%s">English</button></a>'
                  '</div></div>') % (zh_cls, en_cls)
    body = ('<h2 name="content">Tool</h2>' + lang_block + "".join(blocks))
    return layout(ctx, "/admin/system/tool", body)


# ---------------------------------------------------------------- Docker 管理页

def system_docker(ctx, log_name=""):
    """System → Docker：Compose 部署 / 镜像加速 / 容器与镜像 / 日志。"""
    host = getattr(ctx, "host_header", "") or socket.gethostname()
    st = dockerui.status()
    info = kv_table([
        ("Docker", esc(st.get("version") or st.get("message") or "not available")),
        ("Containers", "%d total / %d running" % (
            (st.get("stats") or {}).get("total", 0), (st.get("stats") or {}).get("running", 0))),
    ])

    compose = (
        '<form method="post" action="/admin/system/docker">'
        '<input type="hidden" name="action" value="compose"/>'
        + value_row("Stack Name", '<input class="cbi-input-text" name="name" '
                                  'placeholder="myapp" required/>')
        + value_row("docker-compose.yml",
                    '<textarea class="cbi-input-textarea" name="yaml" rows="10" '
                    'style="width:100%;font-family:ui-monospace,Consolas,monospace" '
                    'placeholder="services:\n  app:\n    image: nginx:latest\n    ports:\n'
                    '      - 8080:80" required></textarea>')
        + '<div class="cbi-page-actions">'
          '<button class="cbi-button cbi-button-add">Deploy</button></div>'
        '</form>')

    stack_rows = []
    for t in dockerui.stacks():
        stack_rows.append([
            esc(t.get("name", "")),
            esc(str(t.get("services", 0))),
            '<form method="post" action="/admin/system/docker" style="margin:0">'
            '<input type="hidden" name="name" value="%s"/>'
            '<button class="cbi-button cbi-button-reset" name="action" value="stack-down">Stop</button> '
            '<button class="cbi-button cbi-button-remove" name="action" value="stack-delete">Delete</button>'
            '</form>' % esc(t.get("name", "")),
        ])

    mirrors_txt = "\n".join(dockerui.mirrors())
    mirrors_form = (
        '<form method="post" action="/admin/system/docker">'
        '<input type="hidden" name="action" value="mirrors"/>'
        + value_row("Registry Mirrors",
                    '<textarea class="cbi-input-textarea" name="mirrors" rows="4" '
                    'style="width:100%%;font-family:ui-monospace,Consolas,monospace" '
                    'placeholder="https://docker.m.daocloud.io">%s</textarea>'
                    '<div class="cbi-section-descr">One URL per line. Written to '
                    '/etc/docker/daemon.json and hot-reloaded (containers keep running).</div>'
                    % esc(mirrors_txt))
        + '<div class="cbi-page-actions">'
          '<button class="cbi-button cbi-button-save">Save</button></div>'
        '</form>')

    con_rows = []
    for c in (st.get("containers") or []):
        name = c.get("name", "")
        log_link = '<a href="/admin/system/docker?log=%s"><button class="cbi-button cbi-button-apply">Logs</button></a>' % esc(name)
        con_rows.append([
            esc(name),
            esc(c.get("image", "")),
            '<span class="label %s">%s</span>' % ("success" if c.get("running") else "",
                                                  esc(c.get("status", ""))),
            " / ".join(esc("%s->%s" % (x.get("host") or "-", x.get("container") or "-"))
                       for x in (c.get("ports") or [])) or "-",
            '<form method="post" action="/admin/system/docker" style="margin:0">'
            '<input type="hidden" name="name" value="%s"/>'
            '<button class="cbi-button cbi-button-apply" name="action" value="start">Start</button> '
            '<button class="cbi-button cbi-button-reload" name="action" value="restart">Restart</button> '
            '<button class="cbi-button cbi-button-reset" name="action" value="stop">Stop</button> '
            '<button class="cbi-button cbi-button-remove" name="action" value="remove">Remove</button>'
            '</form> %s' % (esc(name), log_link),
        ])

    img_rows = []
    for i in (st.get("images") or []):
        ref = (i.get("repo", "") + ":" + i.get("tag", "")).strip(":")
        img_rows.append([
            esc(i.get("repo", "")), esc(i.get("tag", "")),
            esc(i.get("size", "")), esc(i.get("created", "")),
            '<form method="post" action="/admin/system/docker" style="margin:0">'
            '<input type="hidden" name="ref" value="%s"/>'
            '<button class="cbi-button cbi-button-remove" name="action" value="rmi">Delete</button>'
            '</form>' % esc(ref),
        ])
    pull_form = (
        '<form method="post" action="/admin/system/docker" style="margin-top:8px">'
        '<input type="hidden" name="action" value="pull"/>'
        + value_row("Image", '<input class="cbi-input-text" name="ref" '
                             'placeholder="nginx:latest" required/> '
                             '<button class="cbi-button cbi-button-add">Pull</button>')
        + '</form>')

    body = ('<h2 name="content">Docker</h2>'
            + section("Status", info)
            + section("Deploy with Compose", compose,
                      "Paste a docker-compose.yml and deploy. Stacks land in /var/lib/samara/stacks")
            + section("Stacks", table(["Name", "Services", "Actions"], stack_rows, "No stacks"))
            + section("Image Mirrors", mirrors_form,
                      "Registry mirrors speed up image pulls in some regions")
            + section("Containers", table(
                ["Name", "Image", "Status", "Ports", "Actions"], con_rows, "No containers"))
            + section("Images", table(
                ["Repository", "Tag", "Size", "Created", ""], img_rows, "No images") + pull_form))

    if log_name:
        ok, err, text = dockerui.logs(log_name, 300)
        body += section("Logs: " + log_name,
                        '<textarea class="cbi-input-textarea" readonly rows="16" '
                        'style="width:100%%;font-family:ui-monospace,Consolas,monospace">%s</textarea>'
                        '<div class="cbi-page-actions"><a href="/admin/system/docker?log=%s">'
                        '<button class="cbi-button cbi-button-reload">Refresh</button></a></div>'
                        % (esc(text or err), esc(log_name)))
    return layout(ctx, "/admin/system/docker", body)
