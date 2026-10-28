"""Samba 内置管理：共享目录读写、SMB 用户管理、配置校验与热加载。

设计原则：
- 直接操作 /etc/samba/smb.conf 原文，只改动目标 [share] 段，其余注释与顺序保持不变
- 每次写入前自动备份，写入后 testparm 校验，校验失败自动回滚
- 不引入 Samba 之外的依赖，全部走 Debian 自带命令（smbpasswd / pdbedit / testparm）
"""

import os
import re
import shutil
import subprocess
import time

CONF = "/etc/samba/smb.conf"
BACKUP_DIR = "/var/lib/samara/samba-backups"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

# 面板负责的键（其余键原样保留）
MANAGED = [
    "path", "comment", "browseable", "read only", "writable",
    "guest ok", "valid users", "invalid users", "create mask", "directory mask",
]

RESERVED = {"global", "homes", "printers", "print$"}

DEFAULT_GLOBAL = """[global]
   workgroup = WORKGROUP
   server string = {{BRAND}}
   security = user
   map to guest = Bad User
   dns proxy = no
   log file = /var/log/samba/log.%m
   max log size = 1000
   logging = file
   panic action = /usr/share/samba/panic-action %d
   server role = standalone server
   obey pam restrictions = yes
   unix password sync = no
   passwd program = /usr/bin/passwd %u
   pam password change = yes
   usershare allow guests = no
   load printers = no
   printing = bsd
   printcap name = /dev/null
   disable spoolss = yes

[printers]
   comment = All Printers
   browseable = no
   path = /var/tmp
   printable = yes
   guest ok = no
   read only = yes
   create mask = 0700
"""


# ---------------------------------------------------------------- 基础工具

def _run(args, timeout=20, input_text=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, input=input_text)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def available():
    """Samba 是否已安装。"""
    return shutil.which("smbd") is not None and os.path.exists(CONF)


def _read_lines(path=None):
    path = path or CONF
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _spans(lines):
    """返回 [(段名, 起始行, 结束行(不含), 键值dict)]"""
    marks = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]") and len(s) > 2:
            marks.append((i, s[1:-1].strip()))
    out = []
    for k, (start, name) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        body = {}
        for line in lines[start + 1:end]:
            t = line.strip()
            if not t or t.startswith("#") or t.startswith(";") or "=" not in t:
                continue
            key, _, val = t.partition("=")
            body[key.strip().lower()] = val.strip()
        out.append((name, start, end, body))
    return out


def _bool(val, default=False):
    if val is None:
        return default
    return str(val).strip().lower() in ("yes", "true", "1", "on")


# ---------------------------------------------------------------- 读取

def list_shares(path=None):
    """列出用户自建共享（排除 global/homes/printers/print$）。"""
    shares = []
    for name, _s, _e, body in _spans(_read_lines(path)):
        if name.lower() in RESERVED:
            continue
        ro_raw = body.get("read only")
        if ro_raw is not None:
            readonly = _bool(ro_raw, True)
        else:
            readonly = not _bool(body.get("writable"), False)
        shares.append({
            "name": name,
            "path": body.get("path", ""),
            "comment": body.get("comment", ""),
            "browseable": _bool(body.get("browseable"), True),
            "readonly": readonly,
            "guest_ok": _bool(body.get("guest ok"), False),
            "valid_users": body.get("valid users", ""),
        })
    shares.sort(key=lambda x: x["name"].lower())
    return shares


def list_smb_users():
    """系统里可以登录 SMB 的账号，以及是否已设置 Samba 密码。"""
    rc, out, _ = _run(["pdbedit", "-L"])
    configured = set()
    if rc == 0:
        for line in out.splitlines():
            if ":" in line:
                configured.add(line.split(":", 1)[0].strip())

    users = []
    try:
        with open("/etc/passwd", "r", encoding="utf-8") as f:
            for line in f:
                p = line.strip().split(":")
                if len(p) < 7:
                    continue
                name, uid, shell = p[0], p[2], p[6]
                if not USER_RE.match(name):
                    continue
                if name != "root" and (not uid.isdigit() or int(uid) < 1000):
                    continue
                if shell.endswith(("nologin", "false")):
                    continue
                users.append({"name": name, "has_password": name in configured})
    except OSError:
        pass
    users.sort(key=lambda x: (x["name"] != "root", x["name"]))
    return users


def status():
    rc, out, _ = _run(["systemctl", "is-active", "smbd"], timeout=8)
    rc2, out2, _ = _run(["systemctl", "is-active", "nmbd"], timeout=8)
    return {
        "installed": available(),
        "smbd": out.strip() if rc == 0 else (out.strip() or "inactive"),
        "nmbd": out2.strip() if rc2 == 0 else (out2.strip() or "inactive"),
        "conf": CONF,
        "version": _version(),
    }


def _version():
    rc, out, _ = _run(["smbd", "--version"], timeout=8)
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""


# ---------------------------------------------------------------- 写入

def _backup(path=None):
    path = path or CONF
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, f"smb.conf.{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(path, dst)
    except OSError:
        return None
    # 只保留最近 30 份
    try:
        files = sorted(os.listdir(BACKUP_DIR))
        for old in files[:-30]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except OSError:
        pass
    return dst


def _validate(lines):
    """用 testparm 校验候选配置。返回 (ok, message)。"""
    if not shutil.which("testparm"):
        return True, ""
    # 必须写在可写目录内（systemd ProtectSystem=strict 下 /tmp 是只读的）
    tmpdir = "/var/lib/samara"
    try:
        os.makedirs(tmpdir, exist_ok=True)
    except OSError:
        tmpdir = "/tmp"
    tmp = os.path.join(tmpdir, ".smb-test.conf")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        rc, out, err = _run(["testparm", "-s", "--suppress-prompt", tmp])
        if rc != 0 or "ERROR" in out.upper() or "ERROR" in err.upper():
            bad = [l for l in (out + err).splitlines() if "ERROR" in l.upper()]
            return False, (bad[0] if bad else (err or out or "testparm 校验失败"))[:300]
        return True, ""
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _commit(lines):
    """备份 → 写入 → 校验 → 失败回滚。返回 (ok, msg)。"""
    global_text = "\n".join(lines)
    ok, msg = _validate(lines)
    if not ok:
        return False, f"配置校验失败：{msg}"

    backup = _backup()
    try:
        tmp = CONF + ".samara.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(global_text.rstrip("\n") + "\n")
        os.replace(tmp, CONF)
        os.chmod(CONF, 0o644)
    except OSError as e:
        return False, f"写入失败：{e}"

    reloaded, rmsg = reload_config()
    if reloaded:
        return True, "已保存并热加载"
    if not shutil.which("smbd"):
        return True, "已保存（Samba 服务未安装/未运行，配置已写入）"
    return True, f"已保存，但热加载失败：{rmsg}"


def _share_body(name, data):
    lines = [f"[{name}]"]
    order = ["comment", "path", "browseable", "read only",
             "guest ok", "valid users", "invalid users", "create mask", "directory mask"]
    for key in order:
        if key in data and data[key] not in (None, ""):
            lines.append(f"   {key} = {data[key]}")
    # 保留未管理但原有存在的键
    for key, val in (data.get("_extra") or {}).items():
        if key not in order and key != "writable":
            lines.append(f"   {key} = {val}")
    return lines


def upsert_share(name, payload):
    name = (name or "").strip()
    if not NAME_RE.match(name):
        return False, "共享名只能用字母、数字、空格、点、下划线、短横线（1-32 位）"
    if name.lower() in RESERVED:
        return False, f"{name} 是保留名称"

    path = (payload.get("path") or "").strip()
    if not path.startswith("/") or path == "/":
        return False, "路径必须是绝对路径，且不能是根目录"
    path = os.path.normpath(path)
    if ".." in path:
        return False, "路径不合法"

    if not os.path.isdir(path):
        try:
            os.makedirs(path, mode=0o755, exist_ok=True)
        except OSError as e:
            return False, f"目录不存在且无法创建：{e}"

    valid_users = (payload.get("valid_users") or "").strip()
    if valid_users:
        known = {u["name"] for u in list_smb_users()}
        unknown = [u for u in re.split(r"[,\s]+", valid_users) if u and u not in known]
        if unknown:
            return False, f"用户不存在：{', '.join(unknown)}"

    data = {
        "comment": (payload.get("comment") or "").strip()[:100],
        "path": path,
        "browseable": "yes" if payload.get("browseable", True) else "no",
        "read only": "no" if payload.get("writable", True) else "yes",
        "guest ok": "yes" if payload.get("guest_ok", False) else "no",
        "create mask": payload.get("create_mask") or "0664",
        "directory mask": payload.get("directory_mask") or "0775",
    }
    if valid_users:
        data["valid users"] = valid_users

    lines = _read_lines()
    spans = _spans(lines)
    body = None
    for sname, start, end, old in spans:
        if sname.lower() == name.lower():
            data["_extra"] = {k: v for k, v in old.items() if k not in MANAGED}
            body = lines[:start] + _share_body(name, data) + lines[end:]
            break
    if body is None:
        tail = list(lines)
        if tail and tail[-1].strip():
            tail.append("")
        body = tail + _share_body(name, data)

    ok, msg = _commit(body)
    if ok and payload.get("fix_perms"):
        # 让 Samba 用户能读写：把目录属组设为 sambashare（若存在）
        try:
            _run(["chgrp", "-R", "sambashare", path])
            _run(["chmod", "-R", "g+rwx", path])
        except OSError:
            pass
    return ok, msg


def delete_share(name):
    lines = _read_lines()
    for sname, start, end, _body in _spans(lines):
        if sname.lower() == name.lower():
            if sname.lower() in RESERVED:
                return False, "不能删除系统保留段"
            new = lines[:start] + lines[end:]
            return _commit(new)
    return False, "共享不存在"


def _existing_share(name):
    for sname, _s, _e, body in _spans(_read_lines()):
        if sname.lower() == name.lower():
            return body
    return None


# ---------------------------------------------------------------- 用户

def set_user_password(name, password, create=True):
    if not USER_RE.match(name or ""):
        return False, "用户名不合法"
    if len(password or "") < 4:
        return False, "密码至少 4 位"

    if create:
        rc, _o, err = _run(["smbpasswd", "-a", "-s", name],
                           input_text=f"{password}\n{password}\n")
    else:
        rc, _o, err = _run(["smbpasswd", "-s", name],
                           input_text=f"{password}\n{password}\n")
    if rc != 0:
        msg = (err or "").strip().splitlines()
        return False, (msg[-1] if msg else "smbpasswd 执行失败")
    _run(["smbpasswd", "-e", name])
    return True, "已设置 Samba 密码"


def delete_user(name):
    if name == "root":
        return False, "不允许删除 root 的 Samba 账号"
    rc, _o, err = _run(["smbpasswd", "-x", name])
    if rc != 0:
        return False, (err or "").strip() or "删除失败"
    return True, "已删除 Samba 账号（系统账号保留）"


def toggle_user(name, enable):
    rc, _o, err = _run(["smbpasswd", "-e" if enable else "-d", name])
    if rc != 0:
        return False, (err or "").strip() or "操作失败"
    return True, "已启用" if enable else "已停用"


# ---------------------------------------------------------------- 服务

def reload_config():
    if shutil.which("smbcontrol"):
        rc, _o, _e = _run(["smbcontrol", "all", "reload-config"], timeout=10)
        if rc == 0:
            return True, ""
    rc, _o, err = _run(["systemctl", "reload", "smbd"], timeout=20)
    return rc == 0, (err or "").strip()


def restart():
    """重启 Samba。smbd 与 nmbd 分开跑：nmbd 在 disable netbios 或未启用时
    会失败，但那是正常现象，不该让整个"重启"报错。"""
    rc1, _o, err1 = _run(["systemctl", "restart", "smbd"], timeout=40)
    rc2, _o2, err2 = _run(["systemctl", "restart", "nmbd"], timeout=40)
    if rc1 != 0:
        return False, (err1 or "smbd 重启失败").strip()
    if rc2 != 0:
        return True, "smbd 已重启（nmbd 未启用或被 disable netbios 跳过，属正常）"
    return True, "smbd 与 nmbd 均已重启"


def init_conf(force=False):
    """安装阶段：smb.conf 不存在则写一份最小可用配置（品牌名跟随面板配置）。"""
    os.makedirs("/etc/samba", exist_ok=True)
    if os.path.exists(CONF) and not force:
        return False
    brand = "Samara"
    try:
        from .config import Config
        brand = Config().get("branding", "title", brand) or brand
    except Exception:
        pass
    body = DEFAULT_GLOBAL.replace("{{BRAND}}", brand)
    ok, msg = _validate(body.splitlines())
    with open(CONF, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(CONF, 0o644)
    return True
