"""共享文件夹（Shared Folders）—— 参考 OMV 的核心设计。

思路：先在面板里定义一个「共享文件夹」（名称 + 路径 + 属主/权限），
再把它「映射」给各个服务（Samba / NFS / Docker 容器挂载 …）。
好处是路径只在一处定义，改名或迁移目录时不用挨个服务去改。

实现：
- 定义存 /etc/samara/sharedfolders.json
- Samba 的实际引用关系按路径实时比对（不存冗余状态，避免不一致）
- 其他服务的引用（bindings）显式记录在文件夹条目里
"""

import json
import os
import re
import shutil
import time
import uuid

STORE = "/etc/samara/sharedfolders.json"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
PROTECTED = ("/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/boot", "/dev", "/proc", "/sys",
             "/var", "/root", "/home", "/opt", "/srv", "/mnt", "/media")


# ---------------------------------------------------------------- 存取

def _load():
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (OSError, ValueError):
        pass
    return []


def _save(items):
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)
    try:
        os.chmod(STORE, 0o644)
    except OSError:
        pass


def _find(items, fid):
    for it in items:
        if it.get("id") == fid:
            return it
    return None


# ---------------------------------------------------------------- 引用关系

def _samba_refs():
    """{realpath: [共享名...]} —— Samba 里指向某目录的共享。"""
    from . import samba
    refs = {}
    if not samba.available():
        return refs
    for sh in samba.list_shares():
        p = sh.get("path") or ""
        if not p:
            continue
        key = os.path.realpath(p) if os.path.exists(p) else p
        refs.setdefault(key, []).append({
            "service": "samba",
            "ref": sh.get("name", ""),
            "writable": not sh.get("readonly", True),
        })
    return refs


def _usages(folder, samba_refs):
    """某文件夹被哪些服务使用（Samba 实时比对 + 显式 bindings）。"""
    path = folder.get("path") or ""
    key = os.path.realpath(path) if os.path.exists(path) else path
    used = list(samba_refs.get(key, []))
    for b in folder.get("bindings") or []:
        if b.get("service") == "samba":
            continue          # Samba 以实时比对为准，避免重复
        used.append(b)
    return used


# ---------------------------------------------------------------- 查询

def list_folders():
    items = _load()
    samba_refs = _samba_refs()
    out = []
    for it in items:
        folder = dict(it)
        path = it.get("path", "") or ""
        folder["exists"] = bool(path) and os.path.isdir(path)
        folder["usages"] = _usages(it, samba_refs)
        try:
            st = os.stat(path)
            usage = shutil.disk_usage(path) if folder["exists"] else None
            folder["owner"] = f"{st.st_uid}"
            folder["mode"] = oct(st.st_mode & 0o777)[2:]
            folder["free"] = usage.free if usage else None
            folder["total"] = usage.total if usage else None
        except OSError:
            folder["owner"] = ""
            folder["mode"] = ""
        out.append(folder)
    out.sort(key=lambda x: x.get("name", "").lower())
    return out


def suggestions():
    """候选目录：/srv 下已有的子目录，方便快速纳管。"""
    out = []
    for base in ("/srv", "/mnt", "/media"):
        try:
            for name in sorted(os.listdir(base)):
                p = os.path.join(base, name)
                if os.path.isdir(p) and not os.path.islink(p):
                    out.append(p)
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- 写入

def _validate_name(name, items, skip_id=None):
    name = (name or "").strip()
    if not NAME_RE.match(name):
        return None, "名称只能用字母、数字、空格、点、下划线、短横线（1-40 位）"
    for it in items:
        if it.get("id") != skip_id and it.get("name", "").lower() == name.lower():
            return None, f"已存在同名文件夹：{name}"
    return name, ""


def _validate_path(path):
    path = (path or "").strip()
    if not path.startswith("/"):
        return None, "路径必须是绝对路径"
    path = os.path.normpath(path)
    if ".." in path.split("/"):
        return None, "路径不合法"
    if path in PROTECTED:
        return None, f"{path} 是系统目录，不能作为共享文件夹"
    return path, ""


def save(payload):
    """新建或更新。返回 (ok, msg, folder)。"""
    items = _load()
    fid = (payload.get("id") or "").strip()
    items_other = [i for i in items if i.get("id") != fid]
    is_new = not fid or _find(items, fid) is None

    name, err = _validate_name(payload.get("name"), items_other, skip_id=fid)
    if err:
        return False, err, None

    path, err = _validate_path(payload.get("path"))
    if err:
        return False, err, None

    # 目录创建 / 权限
    if not os.path.isdir(path):
        try:
            os.makedirs(path, mode=0o775, exist_ok=True)
        except OSError as e:
            return False, f"目录不存在且无法创建：{e}", None

    owner = (payload.get("owner") or "").strip()
    group = (payload.get("group") or "").strip()
    try:
        mode = int(str(payload.get("mode") or "0775"), 8)
        mode = mode & 0o777
    except ValueError:
        mode = 0o775
    try:
        os.chmod(path, mode)
        if owner or group:
            import pwd
            import grp
            uid = pwd.getpwnam(owner).pw_uid if owner else -1
            gid = grp.getgrnam(group).gr_gid if group else -1
            os.chown(path, uid, gid)
    except (OSError, KeyError) as e:
        return False, f"设置属主/权限失败：{e}", None

    now = int(time.time())
    if is_new:
        folder = {
            "id": uuid.uuid4().hex[:10],
            "name": name,
            "path": path,
            "comment": (payload.get("comment") or "").strip()[:120],
            "owner": owner,
            "group": group,
            "mode": oct(mode)[2:],
            "created": now,
            "bindings": [],
        }
        items.append(folder)
        msg = "已创建共享文件夹"
    else:
        folder = _find(items, fid)
        old_path = folder.get("path")
        folder.update({
            "name": name,
            "path": path,
            "comment": (payload.get("comment") or "").strip()[:120],
            "owner": owner,
            "group": group,
            "mode": oct(mode)[2:],
            "updated": now,
        })
        msg = "已保存"
        if old_path != path:
            msg += f"（路径已变更：{old_path} → {path}，请同步检查引用它的服务）"

    _save(items)

    # 可选：同时建/改同名 Samba 共享
    if payload.get("create_samba"):
        from . import samba
        if samba.available():
            ok, smsg = samba.upsert_share(name, {
                "path": path,
                "comment": folder.get("comment") or "共享文件夹",
                "writable": bool(payload.get("samba_writable", True)),
                "browseable": True,
                "guest_ok": False,
                "fix_perms": True,
            })
            msg += "；" + ("Samba 共享已同步" if ok else f"Samba 同步失败：{smsg}")
    return True, msg, folder


def delete(fid, remove_data=False):
    items = _load()
    folder = _find(items, fid)
    if not folder:
        return False, "文件夹不存在"

    samba_refs = _samba_refs()
    used = _usages(folder, samba_refs)
    if used and not remove_data:
        names = ", ".join(f"{u['service']}:{u['ref']}" for u in used)
        return False, f"该文件夹仍被引用（{names}），请先解除映射"

    path = folder.get("path", "")
    if remove_data and os.path.isdir(path):
        if path in PROTECTED or len(path.strip("/").split("/")) < 2:
            return False, "路径过浅，拒绝删除"
        try:
            shutil.rmtree(path)
        except OSError as e:
            return False, f"删除目录失败：{e}"

    items = [i for i in items if i.get("id") != fid]
    _save(items)
    return True, "已从清单移除" + ("（目录已删除）" if remove_data else "（磁盘上的文件保留）")


def bind(fid, service, ref, meta=None):
    """把文件夹映射给某个服务。"""
    items = _load()
    folder = _find(items, fid)
    if not folder:
        return False, "文件夹不存在"
    bindings = folder.setdefault("bindings", [])
    if any(b.get("service") == service and b.get("ref") == ref for b in bindings):
        return True, "已存在"
    bindings.append({"service": service, "ref": ref, **(meta or {})})
    _save(items)
    return True, "已映射"


def unbind(fid, service, ref):
    items = _load()
    folder = _find(items, fid)
    if not folder:
        return False, "文件夹不存在"
    folder["bindings"] = [b for b in folder.get("bindings") or []
                          if not (b.get("service") == service and b.get("ref") == ref)]
    _save(items)
    return True, "已解除"


def by_name(name):
    for it in _load():
        if it.get("name", "").lower() == (name or "").lower():
            return it
    return None


def by_path(path):
    target = os.path.realpath(path) if os.path.exists(path) else path
    for it in _load():
        p = it.get("path", "")
        key = os.path.realpath(p) if os.path.exists(p) else p
        if key == target:
            return it
    return None


def ensure_default():
    """安装阶段：把 /srv/share 纳管为默认共享文件夹。"""
    if by_path("/srv/share"):
        return False
    items = _load()
    items.append({
        "id": uuid.uuid4().hex[:10],
        "name": "share",
        "path": "/srv/share",
        "comment": "Samara 默认共享",
        "owner": "",
        "group": "",
        "mode": "775",
        "created": int(time.time()),
        "bindings": [],
    })
    _save(items)
    return True
