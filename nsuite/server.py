"""nsuite HTTP 服务：状态 API、服务控制、登录会话、静态资源。"""

import html as html_mod
import json
import os
import posixpath
import re
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth, backup, cron, dockerui, notify, samba, services, shares, smart

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".webmanifest": "application/manifest+json",
}


class Context:
    def __init__(self, config, collector):
        self.config = config
        self.collector = collector
        self.sessions = auth.SessionStore(ttl=config.getint("auth", "session_ttl", 86400))
        self.password_file = config.get("paths", "password_file", "/etc/nsuite/passwd")
        self.apps_file = config.get("paths", "apps_file", "/etc/nsuite/apps.json")
        self.web_root = config.get("paths", "web_root", "/opt/nsuite/web")
        self.title = config.get("branding", "title", "elmsamaras")
        self.subtitle = config.get("branding", "subtitle", "Debian Edition")
        # 调试开关：同时要求监听回环地址，避免误开到公网
        self.allow_anonymous = (
            config.get("dev", "allow_anonymous", "false").lower() in ("1", "true", "yes")
            and config.listen in ("127.0.0.1", "localhost", "::1")
        )

    def apps(self):
        return services.load_apps(self.apps_file)


class Handler(BaseHTTPRequestHandler):
    server_version = "nsuite/1.3.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------

    @property
    def ctx(self):
        return self.server.ctx

    def log_message(self, fmt, *args):
        # 静默常规访问日志，只保留 4xx/5xx（args 形如 (requestline, code, size)）
        code = str(args[1]) if len(args) > 1 else ""
        if code.startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", cookies=None, headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if cookies:
            for c in cookies:
                self.send_header("Set-Cookie", c)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, data, filename, ctype="application/gzip"):
        """下载二进制（备份包等）。"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _redirect(self, to):
        self._send(303, b"", "text/plain", headers={"Location": to})

    def _cookies(self):
        raw = self.headers.get("Cookie", "")
        out = {}
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _current_user(self):
        return self.ctx.sessions.get(self._cookies().get(auth.COOKIE_NAME))

    def _require_auth(self):
        if self.ctx.allow_anonymous:
            return "anonymous"
        user = self._current_user()
        if user:
            return user
        if self.path.startswith("/api/"):
            self._json({"error": "unauthorized"}, 401)
        else:
            self._redirect("/login")
        return None

    # ---------- 路由 ----------

    def do_GET(self):
        try:
            self._route_get()
        except Exception as e:          # 兜底：任何未捕获异常都回 JSON，不留空响应
            self._fail(e)

    def do_POST(self):
        try:
            self._route_post()
        except Exception as e:
            self._fail(e)

    def _fail(self, exc):
        import traceback
        traceback.print_exc()
        try:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        except Exception:
            pass

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/login":
            return self._page("login.html")
        if path == "/logout":
            self.ctx.sessions.drop(self._cookies().get(auth.COOKIE_NAME, ""))
            return self._send(303, b"", "text/plain",
                              cookies=[f"{auth.COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"],
                              headers={"Location": "/login"})
        if path == "/favicon.ico":
            return self._static("static/img/favicon.ico")

        if path.startswith("/static/"):
            return self._static(path.lstrip("/"))

        # PWA：manifest 与 Service Worker 不需要登录态（浏览器首次获取时
        # 用户可能尚未登录），/offline 是无数据静态页，同理放开。
        if path == "/manifest.webmanifest":
            return self._manifest()
        if path == "/sw.js":
            return self._sw()
        if path == "/offline":
            return self._page("offline.html")

        # 安装分发：引导脚本 + 发布包。内容是安装程序本身，不含任何用户数据，
        # 因此放在登录校验之前，方便新机器直接 curl | bash 拉取。
        if path in ("/get.sh", "/install.sh", "/uninstall.sh", "/dist", "/dist/") or path.startswith("/dist/"):
            return self._do_dist(path)

        if not self._require_auth():
            return

        if path == "/":
            return self._page("index.html")
        if path == "/api/status":
            data = self.ctx.collector.snapshot()
            data["title"] = self.ctx.title
            data["subtitle"] = self.ctx.subtitle
            return self._json(data)
        if path == "/api/apps":
            host = self.headers.get("Host", "").split(":")[0] or "localhost"
            items = services.list_with_state(self.ctx.apps(), host)
            return self._json({"apps": items})
        if path == "/api/samba":
            if not samba.available():
                return self._json({
                    "status": {"installed": False},
                    "shares": [],
                    "users": [],
                    "message": "未检测到 Samba，请先安装 samba 软件包",
                })
            return self._json({
                "status": samba.status(),
                "shares": samba.list_shares(),
                "users": samba.list_smb_users(),
            })
        if path == "/api/logs":
            kind = (query.get("type") or ["syslog"])[0]
            try:
                lines = int((query.get("lines") or ["200"])[0])
            except ValueError:
                lines = 200
            lines = max(1, min(lines, 2000))
            text = self._logs(kind, lines)
            # 浏览器直接打开（Accept: text/html）时给个人能看的页面，脚本调用仍拿 JSON
            if "text/html" in (self.headers.get("Accept") or ""):
                title = {"dmesg": "内核日志", "syslog": "系统日志"}.get(kind, kind)
                body = (
                    "<!doctype html><meta charset=\"utf-8\">"
                    f"<title>{html_mod.escape(title)} · {html_mod.escape(self.ctx.title)}</title>"
                    "<style>body{background:#1e1d1a;color:#d8d3c4;margin:0;padding:18px;"
                    "font:12.5px/1.55 ui-monospace,Consolas,monospace}"
                    "h2{font:600 14px system-ui;color:#a8c93a;margin:0 0 12px}"
                    "pre{white-space:pre-wrap;word-break:break-all;margin:0}"
                    "a{color:#a8c93a}</style>"
                    f"<h2>{html_mod.escape(title)}（最近 {lines} 行）</h2>"
                    f"<pre>{html_mod.escape(text)}</pre>"
                )
                return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            return self._json({"type": kind, "text": text})

        # ---------------- SMART ----------------
        if path == "/api/smart":
            return self._json(smart.overview())

        # ---------------- 共享文件夹 ----------------
        if path == "/api/shares":
            return self._json({
                "folders": shares.list_folders(),
                "suggestions": shares.suggestions(),
                "users": [u["name"] for u in samba.list_smb_users()] if samba.available() else [],
            })

        # ---------------- 计划任务 ----------------
        if path == "/api/cron":
            return self._json({"jobs": cron.list_jobs(), "presets": cron.presets()})
        if path == "/api/cron/output":
            return self._json(cron.job_output((query.get("id") or [""])[0]))

        # ---------------- 备份 ----------------
        if path == "/api/backup":
            return self._json({
                "snapshots": backup.list_snapshots(),
                "sources": [a for a, _ in backup.SOURCES],
                "root": backup.BACKUP_ROOT,
                "version": backup.VERSION,
                "auto": backup.auto_status(),
            })
        if path == "/api/backup/export":
            tmp, _meta = backup.build()
            try:
                with open(tmp, "rb") as f:
                    data = f.read()
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            stamp = time.strftime("%Y%m%d-%H%M%S")
            return self._file(data, f"nsuite-backup-{stamp}.tar.gz")
        if path == "/api/backup/download":
            name = (query.get("name") or [""])[0]
            data = backup.read_snapshot(name)
            if data is None:
                return self._send(404, b"not found")
            return self._file(data, name)

        # ---------------- 通知 ----------------
        if path == "/api/notify":
            return self._json(notify.public())

        # ---------------- Docker ----------------
        if path == "/api/docker":
            return self._json(dockerui.status())
        if path == "/api/docker/logs":
            ok, err, text = dockerui.logs((query.get("name") or [""])[0],
                                          (query.get("lines") or ["200"])[0])
            return self._json({"ok": ok, "error": err, "text": text})

        return self._send(404, b"404 Not Found")

    def _route_post(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 64 * 1024 * 1024:
            return self._json({"error": "请求体过大（上限 64MB）"}, 413)
        raw = self.rfile.read(length) if length else b""

        if path == "/login":
            return self._do_login(raw)

        if not self._require_auth():
            return

        if path.startswith("/api/samba"):
            return self._do_samba(path, raw)

        if path == "/api/smart/test":
            data = self._payload(raw)
            ok, msg = smart.selftest(data.get("device", ""), data.get("kind", "short"))
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        if path.startswith("/api/shares"):
            return self._do_shares(path, raw)

        if path.startswith("/api/cron"):
            return self._do_cron(path, raw)

        if path.startswith("/api/backup"):
            return self._do_backup(path, raw)

        if path == "/api/notify/test":
            data = self._payload(raw)
            result = notify.send(
                "测试通知",
                "这是一条来自 %s 的测试消息。收到说明通知通道工作正常。" % self.ctx.title,
                level="info", only=(data.get("id") or None), force=True)
            if result.get("skipped"):
                return self._json({"ok": False, "message": result["skipped"]}, 400)
            detail = "；".join(
                "%s %s" % (r["name"], "成功" if r["ok"] else "失败：" + r["error"])
                for r in result.get("results", []))
            return self._json({"ok": bool(result.get("ok")),
                               "message": (result.get("message") or "") + ("　" + detail if detail else "")},
                              200 if result.get("ok") else 400)

        if path.startswith("/api/notify"):
            return self._do_notify(path, raw)

        if path.startswith("/api/docker"):
            return self._do_docker(path, raw)

        if path.startswith("/api/apps/"):
            parts = path.strip("/").split("/")
            if len(parts) != 4:
                return self._json({"error": "bad request"}, 400)
            _, _, app_id, action = parts
            app = next((a for a in self.ctx.apps() if a.get("id") == app_id), None)
            if not app:
                return self._json({"error": "unknown app"}, 404)
            ok, msg = services.control(app, action)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        return self._json({"error": "not found"}, 404)

    def _payload(self, raw):
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            try:
                data = json.loads(raw.decode("utf-8", "replace") or "{}")
                return data if isinstance(data, dict) else {}
            except ValueError:
                return {}
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        return {k: v[0] for k, v in form.items()}

    def _do_samba(self, path, raw):
        if not samba.available():
            return self._json({"ok": False, "message": "未检测到 Samba，请先安装 samba 软件包"}, 400)

        data = self._payload(raw)
        action = path.rstrip("/").split("/api/samba", 1)[1].strip("/")

        if action == "":
            return self._json({
                "status": samba.status(),
                "shares": samba.list_shares(),
                "users": samba.list_smb_users(),
            })
        if action == "share":
            ok, msg = samba.upsert_share(data.get("name", ""), data)
        elif action == "share/delete":
            ok, msg = samba.delete_share(data.get("name", ""))
        elif action == "user":
            ok, msg = samba.set_user_password(data.get("name", ""), data.get("password", ""))
        elif action == "user/delete":
            ok, msg = samba.delete_user(data.get("name", ""))
        elif action == "user/toggle":
            ok, msg = samba.toggle_user(data.get("name", ""), bool(data.get("enable", True)))
        elif action == "restart":
            ok, msg = samba.restart()
        elif action == "reload":
            ok, msg = samba.reload_config()
        elif action == "init":
            samba.init_conf(force=bool(data.get("force")))
            ok, msg = True, "已写入初始 smb.conf"
        else:
            return self._json({"error": "not found"}, 404)

        return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

    # ---------- 具体处理 ----------

    def _do_shares(self, path, raw):
        data = self._payload(raw)
        action = path.rstrip("/").split("/api/shares", 1)[1].strip("/")

        if action == "":
            return self._json({"folders": shares.list_folders(),
                               "suggestions": shares.suggestions()})
        if action == "save":
            ok, msg, folder = shares.save(data)
            return self._json({"ok": ok, "message": msg, "folder": folder},
                              200 if ok else 400)
        if action == "delete":
            ok, msg = shares.delete(data.get("id", ""), bool(data.get("remove_data")))
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if action == "unbind":
            ok, msg = shares.unbind(data.get("id", ""), data.get("service", ""),
                                    data.get("ref", ""))
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        return self._json({"error": "not found"}, 404)

    def _do_cron(self, path, raw):
        data = self._payload(raw)
        action = path.rstrip("/").split("/api/cron", 1)[1].strip("/")

        if action == "":
            return self._json({"jobs": cron.list_jobs(), "presets": cron.presets()})
        if action == "save":
            ok, msg, job = cron.save(data)
            return self._json({"ok": ok, "message": msg, "job": job},
                              200 if ok else 400)
        if action == "delete":
            ok, msg = cron.delete(data.get("id", ""))
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if action == "toggle":
            ok, msg = cron.toggle(data.get("id", ""), bool(data.get("enabled")))
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if action == "run":
            ok, msg, out = cron.run_now(data.get("id", ""))
            return self._json({"ok": ok, "message": msg, "output": out},
                              200 if ok else 400)
        return self._json({"error": "not found"}, 404)

    def _do_backup(self, path, raw):
        action = path.rstrip("/").split("/api/backup", 1)[1].strip("/")

        if action == "import":
            # 请求体就是 tar.gz 原始字节
            if not raw:
                return self._json({"ok": False, "message": "没有收到文件内容"}, 400)
            if not raw[:2] == b"\x1f\x8b":
                return self._json({"ok": False, "message": "文件不是 gzip 格式"}, 400)
            ok, msg = backup.restore(raw)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        if action == "create":
            try:
                name, meta = backup.create_snapshot()
            except OSError as e:
                return self._json({"ok": False, "message": f"创建快照失败：{e}"}, 400)
            return self._json({"ok": True, "message": f"已创建快照 {name}",
                               "name": name, "meta": meta})

        data = self._payload(raw)
        name = data.get("name", "")
        if action == "auto":
            ok, msg = backup.save_auto(data)
            return self._json({"ok": ok, "message": msg,
                               "auto": backup.auto_status()}, 200 if ok else 400)
        if action == "run-auto":
            ok, msg = backup.run_auto()
            return self._json({"ok": ok, "message": msg,
                               "auto": backup.auto_status()}, 200 if ok else 400)
        if action == "delete":
            ok, msg = backup.delete_snapshot(name)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if action == "restore":
            blob = backup.read_snapshot(name)
            if blob is None:
                return self._json({"ok": False, "message": "快照不存在"}, 404)
            ok, msg = backup.restore(blob)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        return self._json({"error": "not found"}, 404)

    def _do_notify(self, path, raw):
        data = self._payload(raw)
        action = path.rstrip("/").split("/api/notify", 1)[1].strip("/")

        if action == "":
            return self._json(notify.public())
        if action == "save":
            ok, msg = notify.save(data)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if action == "channel":
            ok, msg = notify.upsert(data)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if action == "channel/delete":
            ok, msg = notify.delete_channel(data.get("id", ""))
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        return self._json({"error": "not found"}, 404)

    def _do_docker(self, path, raw):
        data = self._payload(raw)
        rest = path.rstrip("/").split("/api/docker", 1)[1].strip("/")

        if rest == "deploy":
            ok, msg = dockerui.deploy(data.get("key", ""), data)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        if rest == "remove":
            ok, msg = dockerui.remove(data.get("name", ""), bool(data.get("force")))
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        parts = rest.split("/")
        if len(parts) == 2:
            name, action = parts
            ok, msg = dockerui.control(name, action)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)
        return self._json({"error": "not found"}, 404)

    def _do_login(self, raw):
        ip = self.client_address[0]
        wait = self.ctx.sessions.locked(ip)
        if wait:
            return self._login_page(f"尝试次数过多，请 {wait} 秒后再试")

        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        user = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]

        if auth.verify(self.ctx.password_file, user, password, upgrade=True):
            self.ctx.sessions.note_success(ip)
            token = self.ctx.sessions.create(user)
            cookie = (f"{auth.COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; "
                      f"Max-Age={self.ctx.sessions.ttl}")
            return self._send(303, b"", "text/plain", cookies=[cookie], headers={"Location": "/"})

        self.ctx.sessions.note_fail(ip)
        return self._login_page("用户名或密码错误")

    def _login_page(self, error=""):
        html = self._read_page("login.html")
        if html is None:
            return self._send(500, b"login template missing")
        if error:
            html = html.replace("<!--ERROR-->",
                                f'<div class="nsuite-error">{error}</div>')
        html = html.replace("<!--ERROR-->", "")
        body = html.replace("{{TITLE}}", self.ctx.title).encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def _read_page(self, name):
        path = os.path.join(self.ctx.web_root, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return None

    def _page(self, name):
        html = self._read_page(name)
        if html is None:
            return self._send(404, b"page not found")
        html = (html.replace("{{TITLE}}", self.ctx.title)
                    .replace("{{SUBTITLE}}", self.ctx.subtitle))
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    # ---------- PWA ----------

    def _manifest(self):
        title = self.ctx.title
        manifest = {
            "id": "/",
            "name": title,
            "short_name": title[:12],
            "description": f"{title} —— {self.ctx.subtitle} NAS 控制面板",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "display_override": ["standalone", "minimal-ui"],
            "orientation": "any",
            "lang": "zh-CN",
            "dir": "ltr",
            "background_color": "#14130f",
            "theme_color": "#1b1a17",
            "categories": ["utilities", "productivity"],
            "icons": [
                {"src": "/static/img/pwa/icon-192.png", "sizes": "192x192",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/img/pwa/icon-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "any"},
                {"src": "/static/img/pwa/icon-maskable-512.png", "sizes": "512x512",
                 "type": "image/png", "purpose": "maskable"},
            ],
            "shortcuts": [
                {"name": "状态总览", "url": "/#overview", "icons": [{"src": "/static/img/pwa/icon-192.png", "sizes": "192x192"}]},
                {"name": "存储与健康", "url": "/#storage", "icons": [{"src": "/static/img/pwa/icon-192.png", "sizes": "192x192"}]},
                {"name": "备份", "url": "/#backup", "icons": [{"src": "/static/img/pwa/icon-192.png", "sizes": "192x192"}]},
            ],
        }
        body = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/manifest+json; charset=utf-8",
                   headers={"Cache-Control": "no-cache"})

    def _sw(self):
        """Service Worker 必须从根作用域提供且禁缓存，否则更新不生效。"""
        text = self._read_page("sw.js")
        if text is None:
            return self._send(404, b"sw.js missing")
        text = text.replace("__VERSION__", self._sw_version())
        self._send(200, text.encode("utf-8"), "application/javascript; charset=utf-8",
                   headers={"Cache-Control": "no-cache",
                            "Service-Worker-Allowed": "/"})

    @staticmethod
    def _sw_version():
        from . import __version__
        return __version__

    # ---------- 安装分发（引导脚本与发布包） ----------

    DIST_NAME_RE = re.compile(r"^elmsamaras-[0-9A-Za-z._-]+\.(?:tar\.gz|sh)$")

    def _dist_dir(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.ctx.web_root)), "dist")

    def _dist_packages(self):
        d = self._dist_dir()
        try:
            names = [n for n in os.listdir(d)
                     if n.startswith("elmsamaras-") and n.endswith(".tar.gz")
                     and os.path.isfile(os.path.join(d, n))]
        except OSError:
            return []
        names.sort(key=lambda n: os.path.getmtime(os.path.join(d, n)), reverse=True)
        return names

    def _dist_version(self):
        for n in self._dist_packages():
            return n[len("elmsamaras-"):-len(".tar.gz")]
        return ""

    def _base_url(self):
        """按请求 Host 推断本机地址，供引导脚本拼接下载链接。"""
        host = self.headers.get("Host") or "localhost"
        if not re.match(r"^[A-Za-z0-9.\-\[\]:]+$", host):
            host = "localhost"
        scheme = "https" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else "http"
        return "%s://%s" % (scheme, host)

    def _do_dist(self, path):
        dist = self._dist_dir()
        prefix = os.path.dirname(dist)

        if path in ("/get.sh", "/install.sh", "/uninstall.sh"):
            try:
                with open(os.path.join(prefix, path.lstrip("/")), "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                return self._send(404, b"installer not found")
            ver = self._dist_version()
            base = self._base_url()
            text = text.replace("@@BASE_URL@@", base).replace("@@VERSION@@", ver)
            if ver:
                text = text.replace("@@DEFAULT_SRC@@",
                                    "%s/dist/elmsamaras-%s.tar.gz" % (base, ver))
            return self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                              headers={"Cache-Control": "no-cache"})

        if path in ("/dist", "/dist/"):
            return self._json({"version": self._dist_version(),
                               "packages": self._dist_packages()})

        name = path[len("/dist/"):]
        if not name or os.path.basename(name) != name or not self.DIST_NAME_RE.match(name):
            return self._send(404, b"not found")
        try:
            with open(os.path.join(dist, name), "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, b"not found")
        return self._file(data, name)

    def _static(self, rel):
        rel = posixpath.normpath("/" + rel).lstrip("/")
        if rel.startswith(".."):
            return self._send(403, b"forbidden")
        full = os.path.join(self.ctx.web_root, rel)
        if not os.path.abspath(full).startswith(os.path.abspath(self.ctx.web_root)):
            return self._send(403, b"forbidden")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, b"not found")
        ext = os.path.splitext(full)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        cache = "public, max-age=86400" if ext in (".css", ".js", ".woff", ".woff2", ".png", ".jpg") else "no-cache"
        self._send(200, data, ctype, headers={"Cache-Control": cache})

    @staticmethod
    def _logs(kind, lines):
        cmd = ["journalctl", "-n", str(lines), "--no-pager", "-o", "short"]
        if kind == "dmesg":
            cmd.append("-k")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout
        except (OSError, subprocess.SubprocessError):
            pass
        fallback = "/var/log/kern.log" if kind == "dmesg" else "/var/log/syslog"
        try:
            with open(fallback, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:])
        except OSError:
            return f"（无法读取 {kind} 日志：当前系统既无 journalctl 也无 {fallback}）"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, ctx):
        self.ctx = ctx
        super().__init__(addr, Handler)


def serve(config, collector):
    ctx = Context(config, collector)
    httpd = Server((config.listen, config.port), ctx)
    print(f"[nsuite] listening on http://{config.listen}:{config.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
