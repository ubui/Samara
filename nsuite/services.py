"""服务管理：把 N1 小钢炮的 Sxx 脚本映射为 systemd unit。

两种后端：
- kind 缺省/systemd  → systemctl 控制 unit
- kind == "docker"   → docker 控制容器（装着 Portainer/jellyfin/qB 这类容器应用）

只允许操作 /etc/nsuite/apps.json 白名单里的服务，动作限定在固定集合内。
"""

import json
import re
import shutil
import subprocess

UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]+$")
ACTIONS = {"start", "stop", "restart", "enable", "disable"}

# 与固件 Sxx 脚本一一对应的默认清单；kind=docker 的条目由容器承载
DEFAULT_APPS = [
    {"id": "aria2", "name": "Aria2", "unit": "aria2", "group": "download",
     "port": 6800, "ui": "http://{host}:8080", "pkg": "aria2", "desc": "下载核心"},
    {"id": "qbittorrent", "name": "qBittorrent", "unit": "nsuite-qbittorrent", "group": "download",
     "kind": "docker", "container": "nsuite-qbittorrent",
     "port": 8083, "ui": "http://{host}:8083", "pkg": "docker.io", "desc": "BT/PT 客户端（容器）"},
    {"id": "transmission", "name": "Transmission", "unit": "transmission-daemon", "group": "download",
     "port": 9091, "ui": "http://{host}:9091", "pkg": "transmission-daemon", "desc": "BT 客户端"},
    {"id": "samba", "name": "Samba", "unit": "smbd", "group": "share",
     "port": 445, "ui": "", "pkg": "samba", "desc": "SMB 共享"},
    {"id": "nfs", "name": "NFS", "unit": "nfs-server", "group": "share",
     "port": 2049, "ui": "", "pkg": "nfs-kernel-server", "desc": "NFS 共享"},
    {"id": "vsftpd", "name": "FTP", "unit": "vsftpd", "group": "share",
     "port": 21, "ui": "", "pkg": "vsftpd", "desc": "FTP 服务"},
    {"id": "syncthing", "name": "Syncthing", "unit": "syncthing@root", "group": "share",
     "port": 8384, "ui": "http://{host}:8384", "pkg": "syncthing", "desc": "文件同步"},
    {"id": "filebrowser", "name": "FileBrowser", "unit": "filebrowser", "group": "share",
     "port": 8081, "ui": "http://{host}:8081", "pkg": "filebrowser", "desc": "网页文件管理"},
    {"id": "jellyfin", "name": "Jellyfin", "unit": "nsuite-jellyfin", "group": "media",
     "kind": "docker", "container": "nsuite-jellyfin",
     "port": 8096, "ui": "http://{host}:8096", "pkg": "docker.io",
     "desc": "媒体中心（nyanmisaka 硬件加速版）"},
    {"id": "docker", "name": "Docker", "unit": "docker", "group": "other",
     "port": 0, "ui": "", "pkg": "docker.io", "desc": "容器运行时"},
    {"id": "portainer", "name": "Portainer", "unit": "portainer", "group": "other",
     "kind": "docker", "container": "portainer",
     "port": 9000, "ui": "http://{host}:9000", "pkg": "docker.io", "desc": "Docker 图形化管理"},
    {"id": "frp", "name": "FRP 客户端", "unit": "frpc", "group": "other",
     "port": 0, "ui": "", "pkg": "frp", "desc": "内网穿透"},
]


def load_apps(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except (OSError, ValueError):
        pass
    return list(DEFAULT_APPS)


def _has_systemd():
    return shutil.which("systemctl") is not None


def _run(args, timeout=25):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _docker_state(app):
    """容器应用的状态。"""
    name = app.get("container") or app.get("unit") or ""
    if not name or not UNIT_RE.match(name):
        return {"active": False, "enabled": False, "installed": False, "state": "invalid"}
    if not shutil.which("docker"):
        return {"active": False, "enabled": False, "installed": False, "state": "missing"}
    rc, out, _err = _run(["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=10)
    if rc != 0:
        return {"active": False, "enabled": False, "installed": False, "state": "missing"}
    state = (out or "").strip() or "unknown"
    return {
        "active": state == "running",
        "enabled": True,        # 容器用 restart 策略，视为常驻
        "installed": True,
        "state": state,
        "kind": "docker",
    }


def service_state(app):
    """返回 active / enabled / installed 三态。"""
    if (app.get("kind") or "systemd") == "docker":
        return _docker_state(app)

    unit = app.get("unit", "")
    if not unit or not UNIT_RE.match(unit):
        return {"active": False, "enabled": False, "installed": False, "state": "invalid"}

    if not _has_systemd():
        # 无 systemd 环境（容器/WSL 旧版）退化到 pgrep 判断
        rc, out, _ = _run(["pgrep", "-f", unit])
        active = rc == 0
        return {"active": active, "enabled": active, "installed": active,
                "state": "active" if active else "inactive"}

    rc, out, _ = _run(["systemctl", "is-active", unit], timeout=8)
    active = out == "active"
    rc2, out2, _ = _run(["systemctl", "is-enabled", unit], timeout=8)
    enabled = out2 in ("enabled", "static", "indirect", "enabled-runtime")
    installed = out2 != "not-found" and "No such file" not in out2
    if not installed:
        installed = shutil.which(app.get("pkg", "") or "") is not None

    state = "active" if active else ("inactive" if installed else "missing")
    return {"active": active, "enabled": enabled, "installed": installed, "state": state}


def control(app, action):
    if action not in ACTIONS:
        return False, "不支持的操作"

    if (app.get("kind") or "systemd") == "docker":
        from . import dockerui
        name = app.get("container") or app.get("unit") or ""
        if action in ("enable", "disable"):
            return True, "容器由重启策略控制，无需单独开启/禁用"
        return dockerui.control(name, action)

    unit = app.get("unit", "")
    if not unit or not UNIT_RE.match(unit):
        return False, "unit 名非法"
    if not _has_systemd():
        return False, "当前系统没有 systemd，无法控制服务"
    rc, out, err = _run(["systemctl", action, unit], timeout=40)
    if rc == 0:
        return True, out or "ok"
    msg = err or out or f"systemctl 返回 {rc}"
    return False, msg.splitlines()[0][:200]


def list_with_state(apps, host=""):
    out = []
    for app in apps:
        st = service_state(app)
        ui = app.get("ui", "") or ""
        if ui and host:
            ui = ui.replace("{host}", host)
        out.append({**app, "ui": ui, **st})
    return out
