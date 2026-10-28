"""系统配置备份 / 恢复（参考 OMV 的 Backup 插件）。

只备份「配置」，不备份媒体文件——这类套件真正怕丢的是账号、共享定义、
计划任务和 smb.conf，这些几百 KB 的东西才值得每天快照一次。

包含内容：
  etc/samara/**          面板配置、账号口令、共享文件夹、计划任务定义、任务脚本
  etc/samba/smb.conf     Samba 主配置
  etc/cron.d/samara      生成的 cron 表
  manifest.json          版本、主机名、时间

安全约束：导入时逐条校验成员路径，只允许落在白名单目录内，拒绝绝对路径与 ..
"""

import io
import json
import os
import posixpath
import shutil
import sys
import tarfile
import tempfile
import time

BACKUP_ROOT = "/var/lib/samara/backups"
# 面板运行在 ProtectSystem=strict 下，/tmp 只读，临时文件必须落在可写目录
TMP_ROOT = "/var/lib/samara/tmp"
VERSION = "1.1"

# 自动备份：配置 + cron 表 + 最近一次运行结果
AUTO_STORE = "/etc/samara/backup.json"
AUTO_CROND = "/etc/cron.d/samara-backup"
STATUS = "/var/lib/samara/backup-status.json"
PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_AUTO = {"enabled": False, "schedule": "0 3 * * *", "keep": 10}

# (archive 前缀, 实际路径)
SOURCES = [
    ("etc/samara", "/etc/samara"),
    ("etc/samba/smb.conf", "/etc/samba/smb.conf"),
    ("etc/cron.d/samara", "/etc/cron.d/samara"),
    ("etc/cron.d/samara-backup", "/etc/cron.d/samara-backup"),
]

ALLOWED_PREFIXES = (
    "etc/samara/",
    "etc/samba/",
    "etc/cron.d/",
    "manifest.json",
)


# ---------------------------------------------------------------- 导出

def build(path=None):
    """生成备份归档，返回 (文件路径, 说明)。path 为空时写临时文件。"""
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    os.makedirs(TMP_ROOT, exist_ok=True)
    if not path:
        fd, path = tempfile.mkstemp(prefix="samara-cfg-", suffix=".tar.gz",
                                    dir=TMP_ROOT)
        os.close(fd)

    included = [arc for arc, real in SOURCES if os.path.exists(real)]
    manifest = {
        "app": "samara",
        "version": VERSION,
        "created": int(time.time()),
        "hostname": os.uname().nodename if hasattr(os, "uname") else "",
        "python": "%d.%d" % sys.version_info[:2],
        "included": included,
    }
    with tarfile.open(path, "w:gz") as tar:
        for arcname in included:
            real = dict(SOURCES)[arcname]
            tar.add(real, arcname=arcname, recursive=True)
        data = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return path, {"included": included, "manifest": manifest,
                  "size": os.path.getsize(path)}


def create_snapshot(keep=None):
    """在 /var/lib/samara/backups 下留一份带时间戳的快照。

    keep 为 None 时沿用自动备份里配置的保留份数（导入前的安全快照也走这里，
    不能因为「导入」把用户设置的 keep=50 悄悄压回 10）。
    """
    if not isinstance(keep, int) or keep <= 0:
        keep = load_auto()["keep"]
    name = "samara-backup-%s.tar.gz" % time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_ROOT, name)
    _, meta = build(path)
    _prune(keep)
    return name, meta


def list_snapshots():
    out = []
    try:
        for name in os.listdir(BACKUP_ROOT):
            if not name.endswith(".tar.gz"):
                continue
            full = os.path.join(BACKUP_ROOT, name)
            st = os.stat(full)
            out.append({
                "name": name,
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "manifest": _read_manifest(full),
            })
    except OSError:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _read_manifest(path):
    try:
        with tarfile.open(path, "r:gz") as tar:
            member = tar.getmember("manifest.json")
            f = tar.extractfile(member)
            if f:
                return json.loads(f.read().decode("utf-8"))
    except (OSError, KeyError, ValueError, tarfile.TarError):
        pass
    return {}


def _prune(keep=10):
    files = list_snapshots()
    for item in files[keep:]:
        try:
            os.remove(os.path.join(BACKUP_ROOT, item["name"]))
        except OSError:
            pass


def delete_snapshot(name):
    if "/" in name or not name.endswith(".tar.gz"):
        return False, "名称不合法"
    try:
        os.remove(os.path.join(BACKUP_ROOT, name))
        return True, "已删除"
    except OSError as e:
        return False, str(e)


# ---------------------------------------------------------------- 导入

def _safe_members(tar):
    """校验成员路径，返回 (ok, 错误, 成员列表)。

    注意：必须校验「原始成员名」而不是清洗后的名字。早先用 name.lstrip("./")
    会把 `..//etc/samara/x` 洗成看起来合法的 `etc/samara/x`，实际解包时仍然
    会跳出暂存目录。这里先把名字规范化为相对路径，再判定是否越界。
    """
    members = []
    for m in tar.getmembers():
        raw = m.name
        # 规范化：去掉开头的 ./、折叠重复斜杠，但不改变 .. 的语义
        name = posixpath.normpath(raw)
        if name in (".", ""):
            continue
        if name == ".." or name.startswith("../") or "/../" in name or name.startswith("/"):
            return False, f"归档包含非法路径：{raw}", []
        if not any(name == p.rstrip("/") or name.startswith(p) for p in ALLOWED_PREFIXES):
            return False, f"归档包含不允许的路径：{raw}", []
        if m.issym() or m.islnk():
            return False, f"归档包含链接文件，已拒绝：{raw}", []
        members.append(m)
    if not members:
        return False, "归档是空的", []
    return True, "", members


def _extract(tar, dest, members):
    """解包到暂存目录。

    Python 3.12+ 支持 filter="data"（PEP 706），会额外拒绝越界路径与特殊文件；
    Debian 12 自带 3.11 没有该参数，需要退回普通解包（路径已由 _safe_members 校验）。
    """
    try:
        tar.extractall(dest, members=members, filter="data")
    except TypeError:
        tar.extractall(dest, members=members)


def _copy_tree(src, dst):
    """递归覆盖复制（不删除目标目录中已有的其它文件）。"""
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target, exist_ok=True)
        for name in files:
            s = os.path.join(root, name)
            d = os.path.join(target, name)
            try:
                shutil.copy2(s, d)
            except OSError:
                pass


def restore(data, make_safety_copy=True):
    """从字节流恢复。返回 (ok, msg)。"""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    except tarfile.TarError as e:
        return False, f"不是有效的 tar.gz 归档：{e}"

    with tar:
        ok, err, members = _safe_members(tar)
        if not ok:
            return False, err
        if not any(m.name == "manifest.json" or "samara" in m.name for m in members):
            return False, "归档里没有面板配置，拒绝导入"

        safety = ""
        if make_safety_copy:
            try:
                name, _ = create_snapshot()
                safety = f"（导入前已自动快照：{name}）"
            except OSError as e:
                safety = f"（快照失败：{e}）"

        os.makedirs(TMP_ROOT, exist_ok=True)
        staging = tempfile.mkdtemp(prefix="samara-restore-", dir=TMP_ROOT)
        try:
            _extract(tar, staging, members)
            copied = []
            for rel, _real in SOURCES:
                src = os.path.join(staging, rel)
                dst = "/" + rel
                if not os.path.exists(src):
                    continue
                if os.path.isdir(src):
                    # 逐文件覆盖，不删除目录本身：
                    # 面板以 ProtectSystem=strict 运行，/etc/samara 是挂载点，
                    # 删掉再建会让可写挂载失效（后续写入报 EROFS）
                    os.makedirs(dst, exist_ok=True)
                    _copy_tree(src, dst)
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                copied.append(rel)
            if not copied:
                return False, "归档里没有可恢复的文件"
            # 恢复后重建 cron 表，保证 /etc/cron.d/samara 与 JSON 一致
            try:
                from . import cron as cronmod
                cronmod.sync()
            except Exception:
                pass
            # 自动备份的 cron 表也按恢复出来的配置重建
            try:
                _write_crond(load_auto())
            except Exception:
                pass
            return True, ("已恢复 " + "、".join(copied) + safety +
                          "；面板服务需重启后生效：systemctl restart samara")
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def read_snapshot(name):
    if "/" in name or not name.endswith(".tar.gz"):
        return None
    path = os.path.join(BACKUP_ROOT, name)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------- 自动备份

def load_auto():
    data = {}
    try:
        with open(AUTO_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = dict(DEFAULT_AUTO)
    out["enabled"] = bool(data.get("enabled", False))
    if isinstance(data.get("schedule"), str) and data["schedule"].strip():
        out["schedule"] = data["schedule"].strip()
    try:
        keep = int(data.get("keep", DEFAULT_AUTO["keep"]))
        out["keep"] = max(1, min(keep, 200))
    except (TypeError, ValueError):
        pass
    return out


def _write_crond(cfg):
    try:
        if not cfg["enabled"]:
            if os.path.exists(AUTO_CROND):
                os.remove(AUTO_CROND)
            return True, ""
        os.makedirs(os.path.dirname(AUTO_CROND), exist_ok=True)
        body = "\n".join([
            "# 由 Samara 面板生成 —— 请勿手工编辑，改动会被覆盖",
            "# 管理入口：面板 → 备份 → 自动备份",
            "SHELL=/bin/sh",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            'MAILTO=""',
            "",
            "# 自动配置快照",
            "%s root /usr/bin/python3 %s/samara/backup_cli.py --auto" % (cfg["schedule"], PKG_ROOT),
        ]) + "\n"
        tmp = AUTO_CROND + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, AUTO_CROND)
        return True, ""
    except OSError as e:
        return False, "写入 %s 失败：%s" % (AUTO_CROND, e)


def save_auto(payload):
    from . import cron as cronmod

    cfg = load_auto()
    if "enabled" in payload:
        cfg["enabled"] = bool(payload.get("enabled"))
    if payload.get("schedule") is not None:
        sched, err = cronmod.validate_schedule(payload.get("schedule"))
        if err:
            return False, err
        cfg["schedule"] = sched
    if payload.get("keep") is not None:
        try:
            cfg["keep"] = max(1, min(int(payload.get("keep")), 200))
        except (TypeError, ValueError):
            return False, "保留份数需要是 1-200 的整数"

    try:
        os.makedirs(os.path.dirname(AUTO_STORE), exist_ok=True)
        tmp = AUTO_STORE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, AUTO_STORE)
    except OSError as e:
        return False, "保存配置失败：%s" % e

    ok, err = _write_crond(cfg)
    if not ok:
        return False, err
    desc = cronmod.describe(cfg["schedule"])
    if cfg["enabled"]:
        return True, "自动备份已开启（%s，保留 %d 份）" % (desc, cfg["keep"])
    return True, "自动备份已关闭"


def auto_status():
    from . import cron as cronmod

    cfg = load_auto()
    status = {}
    try:
        with open(STATUS, "r", encoding="utf-8") as f:
            status = json.load(f)
    except (OSError, ValueError):
        status = {}
    return {
        "config": cfg,
        "human": cronmod.describe(cfg["schedule"]),
        "cron_exists": os.path.exists(AUTO_CROND),
        "last": status if isinstance(status, dict) else {},
    }


def _write_status(data):
    try:
        os.makedirs(os.path.dirname(STATUS), exist_ok=True)
        tmp = STATUS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATUS)
    except OSError:
        pass


def run_auto():
    """供 cron 调用：建快照 → 写状态 → 失败时发通知。返回 (ok, msg)。"""
    cfg = load_auto()
    stamp = int(time.time())
    try:
        name, meta = create_snapshot(keep=cfg["keep"])
    except Exception as e:
        msg = "自动备份失败：%s" % e
        _write_status({"ts": stamp, "ok": False, "name": "", "error": msg,
                       "schedule": cfg["schedule"], "keep": cfg["keep"]})
        try:
            from . import notify
            notify.send("自动备份失败", msg, level="error", event="backup")
        except Exception:
            pass
        return False, msg

    size = meta.get("size", 0)
    _write_status({"ts": stamp, "ok": True, "name": name, "error": "",
                   "size": size, "schedule": cfg["schedule"], "keep": cfg["keep"]})
    left = len(list_snapshots())
    return True, "已创建快照 %s（%.0f KB），当前共 %d 份" % (name, size / 1024.0, left)
