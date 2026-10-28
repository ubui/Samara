"""Docker 可视化：容器/镜像清单、启停重启、日志、Compose、镜像加速。

实现走 docker CLI（不依赖 docker SDK），面板只需要能访问 /var/run/docker.sock。
应用由使用者以 Compose 自行安装（面板提供部署入口），装好即进应用面板。
"""

import json
import os
import re
import shutil
import subprocess

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ACTIONS = {"start", "stop", "restart", "pause", "unpause"}


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
        return "未安装 Docker（apt install docker.io，或 install.sh 默认安装）"
    rc, _o, err = _run(["docker", "info"], timeout=10)
    low = (err or "").lower()
    if "permission denied" in low:
        return "面板无权访问 /var/run/docker.sock（检查 samara.service 的运行用户与 socket 权限）"
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
    # 停止的容器 docker ps 不显示端口 → 从 PortBindings 回填，保证停着也有快捷入口
    for it in items:
        if it["ports"] or not it["name"]:
            continue
        it["ports"] = _ports_from_inspect(it["name"])
    items.sort(key=lambda x: (not x["running"], x["name"].lower()))
    return items


def _ports_from_inspect(name):
    """从 HostConfig.PortBindings 回填端口映射（停止的容器用）。"""
    rc, out, _e = _run(["docker", "inspect", "-f",
                        "{{json .HostConfig.PortBindings}}", name], timeout=10)
    if rc != 0:
        return []
    try:
        bindings = json.loads(out.strip() or "{}") or {}
    except ValueError:
        return []
    out_list, seen = [], set()
    for key, lst in bindings.items():
        cport, _, proto = key.partition("/")
        for b in (lst or []):
            hp = str(b.get("HostPort") or "")
            if not hp:
                continue
            k = (hp, cport, proto)
            if k in seen:
                continue
            seen.add(k)
            out_list.append({"host": hp, "container": cport,
                             "proto": proto or "tcp", "addr": b.get("HostIp") or "0.0.0.0"})
    out_list.sort(key=lambda x: int(x["host"]) if x["host"].isdigit() else 0)
    return out_list


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


# ---------------------------------------------------------------- Compose

STACKS_DIR = "/var/lib/samara/stacks"
DAEMON_JSON = "/etc/docker/daemon.json"

WEB_PORTS = {"80", "443", "3000", "5000", "5678", "6800", "8000", "8080", "8083",
             "8096", "8443", "8384", "9000", "9091", "9090"}


def web_url(container, host):
    """猜一个 Web 入口：优先常见 Web 端口，其次第一个 TCP 映射；没有就空串。"""
    ports = [p for p in (container.get("ports") or [])
             if p.get("proto") == "tcp" and p.get("host")]
    if not ports:
        return ""

    def key(p):
        hp = str(p["host"]).split("-")[0]
        return (0 if str(p.get("container", "")) in WEB_PORTS else 1,
                int(hp) if hp.isdigit() else 99999)

    ports.sort(key=key)
    return "http://%s:%s" % (host, str(ports[0]["host"]).split("-")[0])


def restart_policy(name):
    """容器开机自启策略：always / unless-stopped / on-failure / no。"""
    if not NAME_RE.match(name or ""):
        return "no"
    rc, out, _e = _run(["docker", "inspect",
                        "-f", "{{.HostConfig.RestartPolicy.Name}}", name], timeout=10)
    return ((out or "").strip() or "no") if rc == 0 else "no"


def _compose_cmd():
    """优先 docker compose（v2 插件），回退 docker-compose。"""
    if _has_cli():
        rc, _o, _e = _run(["docker", "compose", "version"], timeout=10)
        if rc == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def stacks():
    """已部署的 Compose 栈：目录名 + 活跃服务数。"""
    out = []
    if not os.path.isdir(STACKS_DIR):
        return out
    cmd = _compose_cmd()
    for name in sorted(os.listdir(STACKS_DIR)):
        f = os.path.join(STACKS_DIR, name, "docker-compose.yml")
        if not os.path.isfile(f):
            continue
        active = 0
        if cmd:
            rc, o, _e = _run(cmd + ["-f", f, "ps", "-q"], timeout=15)
            if rc == 0:
                active = len([x for x in o.split() if x.strip()])
        out.append({"name": name, "file": f, "services": active})
    return out


def compose_up(name, yaml_text):
    """把 compose YAML 落盘并 up -d（应用自此进入应用面板）。"""
    name = (name or "").strip()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
        return False, "栈名不合法（小写字母/数字/-/_）"
    if not (yaml_text or "").strip():
        return False, "compose 内容为空"
    cmd = _compose_cmd()
    if not cmd:
        return False, "未安装 docker compose（apt install docker-compose-v2）"
    if not available():
        return False, _socket_hint()
    d = os.path.join(STACKS_DIR, name)
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, "docker-compose.yml")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(yaml_text if yaml_text.endswith("\n") else yaml_text + "\n")
    rc, out, err = _run(cmd + ["-f", f, "up", "-d"], timeout=300)
    text = ((out or "") + (err or "")).strip()
    if rc == 0:
        return True, text.splitlines()[-1] if text else "已部署"
    return False, text[-300:] or "部署失败"


def compose_down(name):
    name = (name or "").strip()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
        return False, "栈名不合法"
    f = os.path.join(STACKS_DIR, name, "docker-compose.yml")
    if not os.path.isfile(f):
        return False, "栈不存在"
    cmd = _compose_cmd()
    if not cmd:
        return False, "未安装 docker compose"
    rc, _o, err = _run(cmd + ["-f", f, "down"], timeout=120)
    return rc == 0, ("已停止" if rc == 0 else (err.strip()[-200:] or "停止失败"))


def stack_delete(name):
    name = (name or "").strip()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
        return False, "栈名不合法"
    d = os.path.join(STACKS_DIR, name)
    if not os.path.isdir(d):
        return False, "栈不存在"
    ok, msg = compose_down(name)
    if not ok:
        return False, msg
    shutil.rmtree(d, ignore_errors=True)
    return True, "已删除"


# ---------------------------------------------------------------- 镜像加速

def mirrors():
    """读取 /etc/docker/daemon.json 的 registry-mirrors。"""
    try:
        cfg = json.load(open(DAEMON_JSON, encoding="utf-8"))
        return list(cfg.get("registry-mirrors") or [])
    except Exception:
        return []


def set_mirrors(urls):
    """写 registry-mirrors 并热加载 docker（SIGHUP，不中断容器）。"""
    urls = [u.strip() for u in (urls or []) if u.strip()]
    for u in urls:
        if not re.match(r"^https?://[^\s]+$", u):
            return False, "镜像地址不合法: " + u
    cfg = {}
    if os.path.exists(DAEMON_JSON):
        try:
            cfg = json.load(open(DAEMON_JSON, encoding="utf-8"))
        except Exception:
            cfg = {}
    if urls:
        cfg["registry-mirrors"] = urls
    else:
        cfg.pop("registry-mirrors", None)
    try:
        os.makedirs(os.path.dirname(DAEMON_JSON), exist_ok=True)
        with open(DAEMON_JSON, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError as e:
        return False, str(e)
    r = subprocess.run(["systemctl", "reload", "docker"],
                       capture_output=True, timeout=15)
    if r.returncode != 0:
        return True, "已写入 daemon.json，但热加载失败，请手动 systemctl reload docker"
    return True, "镜像加速已生效"


# ---------------------------------------------------------------- 镜像维护

def pull_image(ref):
    ref = (ref or "").strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}$", ref):
        return False, "镜像名不合法"
    if not available():
        return False, _socket_hint()
    rc, out, err = _run(["docker", "pull", ref], timeout=600)
    text = ((out or "") + (err or "")).strip()
    return rc == 0, (text.splitlines()[-1] if text else "已拉取") if rc == 0 else text[-300:]


def remove_image(ref):
    ref = (ref or "").strip()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}$", ref):
        return False, "镜像名不合法"
    rc, _o, err = _run(["docker", "rmi", ref], timeout=60)
    return rc == 0, ("已删除镜像" if rc == 0 else (err.strip()[-200:] or "删除失败"))
