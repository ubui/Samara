"""通知通道：Bark / 通用 Webhook / SMTP 邮件。

设计：
- 配置存 /etc/nsuite/notify.json，通道可以多条、可单独启停
- 事件开关控制"哪类事件值得打扰你"（备份、硬盘、计划任务、系统）
- 发送失败不影响主流程（一律 best-effort，异常吞掉并记录）
- 去重：同一 key 在 N 小时内只提醒一次，避免硬盘告警刷屏

全部使用标准库（urllib / smtplib / email），不引入第三方依赖。
"""

import json
import os
import smtplib
import ssl
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage

STORE = "/etc/nsuite/notify.json"
LOG = "/var/lib/nsuite/notify-log.json"
STATE = "/var/lib/nsuite/notify-state.json"
LOG_KEEP = 30

EVENTS = [
    ("backup", "配置备份"),
    ("smart", "硬盘健康"),
    ("cron", "计划任务"),
    ("system", "系统与磁盘"),
]

DEFAULT = {
    "enabled": False,
    "events": {"backup": True, "smart": True, "cron": True, "system": True},
    "channels": [],
}

TYPES = ("bark", "webhook", "smtp")


# ---------------------------------------------------------------- 存取

def _load():
    data = {}
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged = {
        "enabled": bool(data.get("enabled", DEFAULT["enabled"])),
        "events": dict(DEFAULT["events"]),
        "channels": [],
    }
    ev = data.get("events") or {}
    if isinstance(ev, dict):
        for key, _label in EVENTS:
            if key in ev:
                merged["events"][key] = bool(ev[key])
    chans = data.get("channels") or []
    if isinstance(chans, list):
        merged["channels"] = [c for c in chans if isinstance(c, dict) and c.get("type") in TYPES]
    return merged


def _save(data):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)
    try:
        os.chmod(STORE, 0o600)
    except OSError:
        pass


def public():
    """给前端的视图：口令一律打码。"""
    data = _load()
    channels = []
    for c in data["channels"]:
        item = {
            "id": c.get("id", ""),
            "type": c.get("type", ""),
            "name": c.get("name", ""),
            "enabled": bool(c.get("enabled", True)),
        }
        if c["type"] == "smtp":
            item.update({
                "host": c.get("host", ""),
                "port": c.get("port", 587),
                "user": c.get("user", ""),
                "sender": c.get("sender", ""),
                "to": c.get("to", ""),
                "tls": bool(c.get("tls", True)),
                "has_password": bool(c.get("password")),
            })
        else:
            item["url"] = c.get("url", "")
        channels.append(item)
    return {
        "enabled": data["enabled"],
        "events": data["events"],
        "channels": channels,
        "event_labels": [{"id": k, "label": v} for k, v in EVENTS],
        "types": list(TYPES),
        "log": history(),
    }


# ---------------------------------------------------------------- 校验 / 保存

def _valid_url(url):
    try:
        p = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _clean_channel(raw, old=None):
    """返回 (channel, error)。old 为原配置，用于口令留空时沿用旧值。"""
    ctype = (raw.get("type") or "").strip()
    if ctype not in TYPES:
        return None, "未知的通道类型"
    name = (raw.get("name") or "").strip() or {"bark": "Bark", "webhook": "Webhook", "smtp": "邮件"}[ctype]
    if len(name) > 40:
        return None, "通道名称过长"
    item = {
        "id": raw.get("id") or ("ch%d" % int(time.time() * 1000 % 10 ** 9)),
        "type": ctype,
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
    }
    if ctype in ("bark", "webhook"):
        url = (raw.get("url") or "").strip()
        if not _valid_url(url):
            return None, "请填写 http(s) 开头的地址"
        item["url"] = url
    else:
        host = (raw.get("host") or "").strip()
        if not host:
            return None, "请填写 SMTP 服务器地址"
        try:
            port = int(raw.get("port") or 587)
        except (TypeError, ValueError):
            port = 587
        if not (1 <= port <= 65535):
            return None, "SMTP 端口不合法"
        to = (raw.get("to") or "").strip()
        if "@" not in to:
            return None, "请填写收件人邮箱"
        password = raw.get("password") or ""
        if password in ("", "******"):
            password = (old or {}).get("password", "")
        item.update({
            "host": host,
            "port": port,
            "user": (raw.get("user") or "").strip(),
            "sender": (raw.get("sender") or "").strip() or (raw.get("user") or "").strip(),
            "to": to,
            "tls": bool(raw.get("tls", True)),
            "password": password,
        })
    return item, ""


def save(payload):
    data = _load()
    if "enabled" in payload:
        data["enabled"] = bool(payload.get("enabled"))
    ev = payload.get("events")
    if isinstance(ev, dict):
        for key, _label in EVENTS:
            if key in ev:
                data["events"][key] = bool(ev[key])

    # 通道整表替换（前端一次提交完整列表，简单可靠）
    chans = payload.get("channels")
    if isinstance(chans, list):
        olds = {c.get("id"): c for c in data["channels"]}
        cleaned = []
        for raw in chans:
            if not isinstance(raw, dict):
                continue
            item, err = _clean_channel(raw, olds.get(raw.get("id")))
            if err:
                return False, f"{raw.get('name') or '通道'}：{err}"
            cleaned.append(item)
        data["channels"] = cleaned

    _save(data)
    msg = "已保存"
    if data["enabled"] and not data["channels"]:
        msg = "已保存，但还没有任何通道，通知不会发出"
    return True, msg


def upsert(payload):
    data = _load()
    raw = payload.get("channel") or {}
    cid = raw.get("id") or ""
    olds = {c.get("id"): c for c in data["channels"]}
    item, err = _clean_channel(raw, olds.get(cid))
    if err:
        return False, err
    if cid and cid in olds:
        data["channels"] = [item if c.get("id") == cid else c for c in data["channels"]]
    else:
        if len(data["channels"]) >= 10:
            return False, "通道数量上限 10 条"
        data["channels"].append(item)
    _save(data)
    return True, "已保存通道"


def delete_channel(cid):
    data = _load()
    before = len(data["channels"])
    data["channels"] = [c for c in data["channels"] if c.get("id") != cid]
    if len(data["channels"]) == before:
        return False, "通道不存在"
    _save(data)
    return True, "已删除"


# ---------------------------------------------------------------- 发送

def _deliver(ch, title, body, level="info"):
    ctype = ch.get("type")
    if ctype == "bark":
        base = (ch.get("url") or "").rstrip("/")
        url = "%s/%s/%s?group=%s&level=%s" % (
            base,
            urllib.parse.quote(title),
            urllib.parse.quote(body),
            urllib.parse.quote("Elmsamaras"),
            "timeSensitive" if level == "error" else "active",
        )
        req = urllib.request.Request(url, headers={"User-Agent": "elmsamaras/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(2048)
        return True

    if ctype == "webhook":
        payload = json.dumps({
            "title": title, "body": body, "level": level,
            "host": os.uname().nodename if hasattr(os, "uname") else "",
            "ts": int(time.time()),
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            ch.get("url", ""), data=payload,
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "elmsamaras/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read(2048)
        return True

    if ctype == "smtp":
        msg = EmailMessage()
        msg["Subject"] = "[Elmsamaras] " + title
        msg["From"] = ch.get("sender") or ch.get("user") or "elmsamaras@localhost"
        msg["To"] = ch.get("to", "")
        msg.set_content(body + "\n\n-- \nElmsamaras · %s" %
                        (os.uname().nodename if hasattr(os, "uname") else ""))
        host = ch.get("host", "")
        port = int(ch.get("port") or 587)
        ctx = ssl.create_default_context()
        # 465 是「隐式 TLS」（连上就加密），对它发 STARTTLS 必然失败；
        # 587/25 才是先明文再 STARTTLS。这里按端口自动选对方式。
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=12, context=ctx) as s:
                if ch.get("user") and ch.get("password"):
                    s.login(ch["user"], ch["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=12) as s:
                s.ehlo()
                if ch.get("tls", True):
                    s.starttls(context=ctx)
                    s.ehlo()
                if ch.get("user") and ch.get("password"):
                    s.login(ch["user"], ch["password"])
                s.send_message(msg)
        return True

    raise ValueError("未实现的通道类型")


def send(title, body, level="info", event="", only=None, force=False):
    """发通知。only 指定单个通道 id（测试用）；force=False 时受总开关与事件开关约束。"""
    data = _load()
    if only:
        targets = [c for c in data["channels"] if c.get("id") == only]
    elif force:
        # 面板上的「测试」按钮：即使通知总开关没打开也要能验证通道是否可用
        targets = [c for c in data["channels"] if c.get("enabled", True)]
    else:
        if not data["enabled"]:
            return {"ok": False, "skipped": "通知未启用", "results": []}
        if event and not data["events"].get(event, True):
            return {"ok": False, "skipped": "该事件已关闭提醒", "results": []}
        targets = [c for c in data["channels"] if c.get("enabled", True)]

    results = []
    for ch in targets:
        try:
            _deliver(ch, title, body, level)
            results.append({"name": ch.get("name", ""), "type": ch.get("type", ""),
                            "ok": True, "error": ""})
        except Exception as e:  # 网络/认证失败都归到这里，不能影响主流程
            results.append({"name": ch.get("name", ""), "type": ch.get("type", ""),
                            "ok": False, "error": "%s: %s" % (type(e).__name__, e)})
    if not results:
        return {"ok": False, "skipped": "没有可用的通道", "results": []}
    ok = any(r["ok"] for r in results)
    if not only:
        _append_log(title, body, level, event, results)
    return {"ok": ok, "results": results,
            "message": "已发送 %d/%d 个通道" % (sum(1 for r in results if r["ok"]), len(results))}


def _append_log(title, body, level, event, results):
    try:
        items = []
        try:
            with open(LOG, "r", encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
        if not isinstance(items, list):
            items = []
        items.insert(0, {
            "ts": int(time.time()), "title": title, "body": body[:400],
            "level": level, "event": event,
            "ok": any(r["ok"] for r in results),
            "results": results,
        })
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        tmp = LOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items[:LOG_KEEP], f, ensure_ascii=False, indent=1)
        os.replace(tmp, LOG)
    except OSError:
        pass


def history(limit=20):
    try:
        with open(LOG, "r", encoding="utf-8") as f:
            items = json.load(f)
        if isinstance(items, list):
            return items[:limit]
    except (OSError, ValueError):
        pass
    return []


# ---------------------------------------------------------------- 去重

def _load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
    except OSError:
        pass


def once(key, hours=24):
    """同一 key 在 hours 内只返回 True 一次（用于告警去重）。"""
    state = _load_state()
    now = int(time.time())
    last = int(state.get(key, 0) or 0)
    if now - last < hours * 3600:
        return False
    state[key] = now
    _save_state(state)
    return True


def forget(key):
    """撤销去重标记——发送失败时调用，下一轮巡检可以重试。"""
    state = _load_state()
    if key in state:
        state.pop(key, None)
        _save_state(state)
