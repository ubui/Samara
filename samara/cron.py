"""计划任务：把 cron 做成可视化管理（参考 OMV 的 Scheduled Jobs）。

设计要点：
- 任务定义存 /etc/samara/cron.json
- 每条任务的命令单独落成 /etc/samara/cron-jobs/<id>.sh，彻底避开 crontab 的引号/百分号转义地狱
- 统一由 /usr/local/lib/samara/cron-run 包裹执行，顺带记录退出码与最近输出
- /etc/cron.d/samara 由面板整份生成，文件头写明“请勿手改”
"""

import json
import os
import re
import shlex
import subprocess
import time
import uuid

STORE = "/etc/samara/cron.json"
JOBDIR = "/etc/samara/cron-jobs"
CROND = "/etc/cron.d/samara"
RUNDIR = "/var/lib/samara/cron-status"
# 由 install.sh 预置（面板运行在 ProtectSystem=strict 下，/usr 为只读）
RUNNER = "/usr/local/sbin/samara-cron-run"

FIELD_RE = re.compile(r"^[0-9A-Za-z*/,\-]+$")

PRESETS = [
    {"id": "hourly", "label": "每小时", "schedule": "0 * * * *"},
    {"id": "daily3", "label": "每天 03:00", "schedule": "0 3 * * *"},
    {"id": "daily4", "label": "每天 04:30", "schedule": "30 4 * * *"},
    {"id": "weekly", "label": "每周日 03:00", "schedule": "0 3 * * 0"},
    {"id": "monthly", "label": "每月 1 日 04:00", "schedule": "0 4 1 * *"},
    {"id": "boot", "label": "开机时", "schedule": "@reboot"},
]


# ---------------------------------------------------------------- 校验

def validate_schedule(expr):
    expr = (expr or "").strip()
    if not expr:
        return None, "执行周期不能为空"
    if expr.startswith("@"):
        if expr not in ("@reboot", "@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@annually"):
            return None, "不支持的特殊周期"
        return expr, ""
    parts = expr.split()
    if len(parts) != 5:
        return None, "cron 表达式需要 5 段（分 时 日 月 周），例如 0 3 * * *"
    for p in parts:
        if not FIELD_RE.match(p):
            return None, f"字段不合法：{p}"
    return " ".join(parts), ""


def describe(expr):
    """把 cron 表达式翻译成中文，方便用户确认。"""
    expr = (expr or "").strip()
    table = {"@reboot": "开机时执行", "@hourly": "每小时整点", "@daily": "每天 00:00",
             "@weekly": "每周日 00:00", "@monthly": "每月 1 日 00:00",
             "@yearly": "每年 1 月 1 日", "@annually": "每年 1 月 1 日"}
    if expr in table:
        return table[expr]
    parts = expr.split()
    if len(parts) != 5:
        return expr
    mi, hr, dom, mon, dow = parts
    if expr == "* * * * *":
        return "每分钟"
    # 步长写法
    if mi.startswith("*/") and hr == "*" and dom == "*" and mon == "*" and dow == "*":
        return f"每 {mi[2:]} 分钟"
    if mi.isdigit() and hr.startswith("*/") and dom == "*" and mon == "*" and dow == "*":
        return f"每 {hr[2:]} 小时的第 {int(mi)} 分"
    if mi.isdigit() and hr.isdigit() and dom == "*" and mon == "*" and dow == "*":
        return f"每天 {int(hr):02d}:{int(mi):02d}"
    if mi.isdigit() and hr == "*" and dom == "*" and mon == "*" and dow == "*":
        return f"每小时第 {int(mi)} 分"
    if mi.isdigit() and hr.isdigit() and dom == "*" and mon == "*" and dow.isdigit():
        names = "日一二三四五六"
        d = int(dow) % 7
        return f"每周{names[d]} {int(hr):02d}:{int(mi):02d}"
    if mi.isdigit() and hr.isdigit() and dom.isdigit() and mon == "*" and dow == "*":
        return f"每月 {int(dom)} 日 {int(hr):02d}:{int(mi):02d}"
    return expr


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


def _find(items, jid):
    for it in items:
        if it.get("id") == jid:
            return it
    return None


# ---------------------------------------------------------------- 状态

def _read_meta(jid):
    meta = {}
    try:
        with open(os.path.join(RUNDIR, jid + ".meta"), "r", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    meta[k] = v
    except OSError:
        return {}
    try:
        meta["rc"] = int(meta.get("rc", ""))
        meta["ts"] = int(meta.get("ts", ""))
    except ValueError:
        pass
    return meta


def _read_out(jid, limit=4000):
    try:
        with open(os.path.join(RUNDIR, jid + ".out"), "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-limit:]
    except OSError:
        return ""


def list_jobs():
    items = _load()
    out = []
    for it in items:
        job = dict(it)
        job["human"] = describe(it.get("schedule", ""))
        meta = _read_meta(it["id"])
        job["last_run"] = meta.get("ts")
        job["last_rc"] = meta.get("rc")
        if meta.get("ts"):
            job["last_ok"] = meta.get("rc") == 0
        out.append(job)
    out.sort(key=lambda x: (not x.get("enabled", True), x.get("name", "").lower()))
    return out


def job_output(jid):
    return {"output": _read_out(jid), "meta": _read_meta(jid)}


# ---------------------------------------------------------------- 写入

def _write_job_script(jid, command):
    os.makedirs(JOBDIR, exist_ok=True)
    path = os.path.join(JOBDIR, jid + ".sh")
    body = "#!/bin/sh\n# 面板生成的计划任务脚本（请勿手改）\n"
    body += "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
    body += "export HOME=/root\n"
    body += command.rstrip("\n") + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o700)
    return path


RUNNER_SCRIPT = """#!/bin/sh
# Samara cron 执行器：运行任务脚本并记录退出码与输出
# 用法: samara-cron-run <job-id>
id="$1"
[ -n "$id" ] || exit 2
dir=/var/lib/samara/cron-status
mkdir -p "$dir" || exit 1
job=/etc/samara/cron-jobs/$id.sh
[ -x "$job" ] || { echo "任务脚本不存在: $job" > "$dir/$id.out"; exit 3; }

out="$dir/$id.out"
"$job" > "$out" 2>&1
rc=$?
# 只保留末尾 8KB，避免无限增长
if [ -f "$out" ]; then
  tail -c 8192 "$out" > "$out.tmp" 2>/dev/null && mv "$out.tmp" "$out"
fi
{
  echo "ts=$(date +%s)"
  echo "rc=$rc"
} > "$dir/$id.meta"
# 失败时推一条通知（未配置通道时静默退出）
if [ "$rc" -ne 0 ]; then
  /usr/bin/python3 /opt/samara/samara/notify_cli.py --cron-job "$id" >/dev/null 2>&1 || true
fi
exit "$rc"
"""


def _ensure_runner():
    """执行器由 install.sh 预置；缺失或版本不一致时补写。

    面板跑在 ProtectSystem=strict 下，/usr 只读——写失败时只要旧执行器还在就继续用，
    避免因为"升级执行器"把整个计划任务功能拖挂（新版执行器由 install.sh 更新）。
    """
    current = ""
    exists = os.path.exists(RUNNER)
    if exists:
        try:
            with open(RUNNER, "r", encoding="utf-8") as f:
                current = f.read()
        except OSError:
            current = ""
    if exists and current == RUNNER_SCRIPT and os.access(RUNNER, os.X_OK):
        return True, ""
    try:
        os.makedirs(os.path.dirname(RUNNER), exist_ok=True)
        with open(RUNNER, "w", encoding="utf-8") as f:
            f.write(RUNNER_SCRIPT)
        os.chmod(RUNNER, 0o755)
        return True, ""
    except OSError as e:
        if exists and os.access(RUNNER, os.X_OK):
            return True, ""
        return False, f"缺少 cron 执行器 {RUNNER}，请重新运行 install.sh（{e}）"


def _render_crond(items):
    lines = [
        "# 由 Samara 面板生成 —— 请勿手工编辑，改动会被覆盖",
        "# 管理入口：面板 → 计划任务（或编辑 /etc/samara/cron.json）",
        "SHELL=/bin/sh",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        'MAILTO=""',
        "",
    ]
    for it in items:
        if not it.get("enabled", True):
            continue
        sched = it.get("schedule", "")
        if not sched:
            continue
        # 名称是写进 crontab 的注释行，必须压掉换行，否则等于允许往 cron.d 里注入行
        name = " ".join(str(it.get("name", "")).split())
        lines.append(f"# {name}")
        lines.append(f"{sched} root {RUNNER} {it['id']}")
    return "\n".join(lines) + "\n"


def _install_crond(items):
    try:
        os.makedirs(os.path.dirname(CROND), exist_ok=True)
        tmp = CROND + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_render_crond(items))
        os.chmod(tmp, 0o644)
        os.replace(tmp, CROND)
        return True, ""
    except OSError as e:
        return False, f"写入 {CROND} 失败：{e}"


def sync():
    """把当前任务表同步到 /etc/cron.d/samara。"""
    ok, err = _ensure_runner()
    if not ok:
        return False, err
    items = _load()
    try:
        for it in items:
            if not it.get("id"):
                continue
            _write_job_script(it["id"], it.get("command", ""))
    except OSError as e:
        return False, f"写入任务脚本失败：{e}"
    return _install_crond(items)


def save(payload):
    items = _load()
    jid = (payload.get("id") or "").strip()
    existing = _find(items, jid) if jid else None
    is_new = existing is None

    name = (payload.get("name") or "").strip()
    if not name:
        return False, "任务名称不能为空", None
    if len(name) > 60:
        return False, "任务名称过长", None
    if "\n" in name or "\r" in name:
        return False, "任务名称不能包含换行", None

    sched, err = validate_schedule(payload.get("schedule"))
    if err:
        return False, err, None

    command = (payload.get("command") or "").strip()
    if not command:
        return False, "执行的命令不能为空", None
    if len(command) > 4000:
        return False, "命令过长", None

    now = int(time.time())
    if is_new:
        job = {
            "id": uuid.uuid4().hex[:10],
            "name": name,
            "schedule": sched,
            "command": command,
            "enabled": bool(payload.get("enabled", True)),
            "created": now,
        }
        items.append(job)
        msg = "已创建计划任务"
    else:
        job = existing
        job.update({
            "name": name,
            "schedule": sched,
            "command": command,
            "enabled": bool(payload.get("enabled", existing.get("enabled", True))),
            "updated": now,
        })
        msg = "已保存"

    _save(items)
    ok, err = sync()
    if not ok:
        return False, err, None
    return True, f"{msg}（{describe(sched)}）", job


def delete(jid):
    items = _load()
    job = _find(items, jid)
    if not job:
        return False, "任务不存在"
    items = [i for i in items if i["id"] != jid]
    _save(items)
    sync()
    for suffix in (".sh",):
        try:
            os.remove(os.path.join(JOBDIR, jid + suffix))
        except OSError:
            pass
    for suffix in (".meta", ".out"):
        try:
            os.remove(os.path.join(RUNDIR, jid + suffix))
        except OSError:
            pass
    return True, "已删除"


def toggle(jid, enabled):
    items = _load()
    job = _find(items, jid)
    if not job:
        return False, "任务不存在"
    job["enabled"] = bool(enabled)
    _save(items)
    ok, err = sync()
    if not ok:
        return False, err
    return True, "已启用" if enabled else "已停用"


def run_now(jid):
    """立即执行一次（同步，最多 60 秒）。"""
    items = _load()
    job = _find(items, jid)
    if not job:
        return False, "任务不存在", ""
    ok, err = _ensure_runner()
    if not ok:
        return False, err, ""
    _write_job_script(jid, job.get("command", ""))
    try:
        p = subprocess.run([RUNNER, jid], capture_output=True, text=True, timeout=60)
        out = (p.stdout or "") + (p.stderr or "")
        if not out.strip():
            out = _read_out(jid) or "（无输出）"
        return p.returncode == 0, f"执行完成，退出码 {p.returncode}", out[-4000:]
    except subprocess.TimeoutExpired:
        return False, "执行超时（超过 60 秒），已转为后台继续运行", _read_out(jid)
    except OSError as e:
        return False, f"执行失败：{e}", ""


def presets():
    return PRESETS
