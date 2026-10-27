"""Docker 可视化：容器/镜像清单、启停重启、日志。

实现走 docker CLI（不依赖 docker SDK），面板只需要能访问 /var/run/docker.sock。
真正的图形化管理交给 Portainer（安装脚本会一并部署，面板里给入口）。
"""

import json
import os
import re
import shutil
import subprocess

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ACTIONS = {"start", "stop", "restart", "pause", "unpause"}

# 面板内置的一键部署模板（首次安装时由 install.sh 也用到同一份定义）
# container 必须和 install.sh 里 `docker run --name` 的名字一致，否则面板
# 检查不到已部署的容器，会反复"重新部署"或误报未安装
CATALOG = {
    "portainer": {
        "name": "Portainer",
        "container": "portainer",
        "image": "portainer/portainer-ce:latest",
        "port": 9000,
        "desc": "Docker 图形化管理",
    },
    "jellyfin": {
        "name": "Jellyfin",
        "container": "nsuite-jellyfin",
        "image": "nyanmisaka/jellyfin:latest",
        "port": 8096,
        "desc": "媒体中心（nyanmisaka 硬件加速版）",
    },
    "qbittorrent": {
        "name": "qBittorrent",
        "container": "nsuite-qbittorrent",
        "image": "lscr.io/linuxserver/qbittorrent:latest",
        "port": 8083,
        "desc": "BT/PT 下载",
    },
}


def _run(args, timeout=25):
    env = dict(os.environ)
    env.setdefault("HOME", "/root")
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _has_cli():
    return shutil.which("docker") is not None


def available():
    """CLI 存在且守护进程可用。"""
    if not _has_cli():
        return False
    rc, _o, _e = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=10)
    return rc == 0


def _socket_hint():
    if not _has_cli():
        return "未安装 Docker（apt install docker.io，或 install.sh --full）"
    rc, _o, err = _run(["docker", "info"], timeout=10)
    low = (err or "").lower()
    if "permission denied" in low:
        return "面板无权访问 /var/run/docker.sock（检查 nsuite.service 的运行用户与 socket 权限）"
    if "cannot connect" in low or "is the docker daemon running" in low:
        return "Docker 守护进程未运行：systemctl start docker"
    return "Docker 不可用：" + (err.strip().splitlines()[-1] if err.strip() else "未知原因")


# ---------------------------------------------------------------- 查询

def _split_paths(s):
    """`docker ps` 的 Ports 列 → 端口映射列表（去掉 IPv6 重复项，显示更清爽）。"""
    out = []
    seen = set()
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(?:([\d.]+|\[[0-9a-f:]+\]):)?(\d+)(?:-(\d+))?->(\d+)/(\w+)$", part)
        if m:
            host, hp, hp2, cp, proto = m.groups()
            key = (hp, cp, proto)
            if key in seen:          # 0.0.0.0 与 [::] 的重复映射只留一条
                continue
            seen.add(key)
            out.append({"host": hp + ("-" + hp2 if hp2 else ""), "container": cp,
                        "proto": proto, "addr": host or "0.0.0.0"})
        else:
            out.append({"host": "", "container": "", "proto": "", "addr": part})
    return out


def containers():
    rc, out, err = _run(["docker", "ps", "-a", "--no-trunc",
                         "--format", "{{json .}}"], timeout=20)
    if rc != 0:
        return []
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        name = row.get("Names") or ""
        status = row.get("Status") or ""
        items.append({
            "id": (row.get("ID") or "")[:12],
            "name": name,
            "image": row.get("Image") or "",
            "state": row.get("State") or "",
            "status": status,
            "running": (row.get("State") or "").lower() == "running",
            "unhealthy": "unhealthy" in status.lower(),
            "ports": _split_paths(row.get("Ports")),
            "created": row.get("CreatedAt") or "",
            "command": (row.get("Command") or "")[:80],
        })
    items.sort(key=lambda x: (not x["running"], x["name"].lower()))
    return items


def images():
    rc, out, _e = _run(["docker", "images", "--format", "{{json .}}"], timeout=20)
    if rc != 0:
        return []
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        items.append({
            "repo": row.get("Repository") or "",
            "tag": row.get("Tag") or "",
            "size": row.get("Size") or "",
            "created": row.get("CreatedSince") or "",
        })
    items.sort(key=lambda x: (x["repo"], x["tag"]))
    return items


def status():
    if not _has_cli():
        return {"installed": False, "running": False, "message": _socket_hint(),
                "containers": [], "images": []}
    rc, out, _e = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
    if rc != 0:
        return {"installed": True, "running": False, "message": _socket_hint(),
                "containers": [], "images": []}
    cons = containers()
    return {
        "installed": True,
        "running": True,
        "version": out.strip(),
        "message": "",
        "containers": cons,
        "images": images(),
        "stats": {
            "total": len(cons),
            "running": sum(1 for c in cons if c["running"]),
        },
        "catalog": CATALOG,
    }


def logs(name, lines=200):
    if not NAME_RE.match(name or ""):
        return False, "容器名不合法", ""
    try:
        lines = max(10, min(int(lines), 2000))
    except (TypeError, ValueError):
        lines = 200
    rc, out, err = _run(["docker", "logs", "--tail", str(lines), name], timeout=20)
    text = (out or "") + (err or "")
    return rc == 0, "" if rc == 0 else (err.strip()[:200] or "读取日志失败"), text[-20000:]


def control(name, action):
    if action not in ACTIONS:
        return False, "不支持的操作"
    if not NAME_RE.match(name or ""):
        return False, "容器名不合法"
    if not available():
        return False, _socket_hint()
    rc, _o, err = _run(["docker", action, name], timeout=60)
    if rc == 0:
        return True, {"start": "已启动", "stop": "已停止", "restart": "已重启",
                      "pause": "已暂停", "unpause": "已恢复"}[action]
    return False, (err.strip().splitlines()[-1] if err.strip() else "操作失败")[:200]


def remove(name, force=False):
    if not NAME_RE.match(name or ""):
        return False, "容器名不合法"
    args = ["docker", "rm"]
    if force:
        args.append("-f")
    args.append(name)
    rc, _o, err = _run(args, timeout=60)
    return (rc == 0), ("已删除容器" if rc == 0 else (err.strip()[:200] or "删除失败"))


def deploy(key, params=None):
    """按内置模板一键部署（幂等：已存在则直接启动）。"""
    key = (key or "").strip()
    spec = CATALOG.get(key)
    if not spec:
        return False, "未知的模板"
    if not available():
        return False, _socket_hint()

    params = params or {}
    name = spec.get("container") or ("nsuite-" + key)
    if not NAME_RE.match(name):
        return False, "容器名不合法"

    rc, out, _e = _run(["docker", "inspect", "-f", "{{.State.Status}}", name], timeout=10)
    if rc == 0:
        st = out.strip()
        if st != "running":
            return control(name, "start")
        return True, f"容器 {name} 已在运行"

    runner = shutil.which("nsuite-container") or "/usr/local/sbin/nsuite-container"
    if not os.path.exists(runner):
        return False, "缺少容器部署脚本（重新运行 install.sh 可修复）"
    rc, out, err = _run([runner, key], timeout=300)
    if rc == 0:
        return True, (out.strip().splitlines()[-1] if out.strip() else "已部署")
    return False, (err or out or "部署失败").strip()[-300:]
