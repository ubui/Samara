"""后台巡检：硬盘健康 + 磁盘用量。

面板本身就是"看板"，但没人会一直盯着看板——所以关键告警要主动推送。
策略：每 30 分钟检查一次，同类告警 24 小时内只发一次（notify.once 去重）。
"""

import os
import threading
import time

from . import notify, smart

DISK_PATHS = ("/", "/srv", "/mnt", "/media", "/srv/media", "/srv/downloads")
THRESHOLD = 90          # 使用率告警阈值（%）
INTERVAL = 1800         # 巡检间隔（秒）
START_DELAY = 120       # 启动后先等面板与采集器稳定


def _usage(path):
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return None
    free = st.f_bavail * st.f_frsize
    return 100.0 * (total - free) / total


def check_once(threshold=THRESHOLD):
    """跑一轮检查，返回本轮触发的告警 key 列表。"""
    fired = []

    try:
        data = smart.overview()
        for disk in data.get("disks", []):
            level = disk.get("level")
            if level not in ("bad", "warn"):
                continue
            key = "smart:%s:%s" % (disk.get("device", "?"), level)
            if not notify.once(key, hours=24):
                continue
            title = "硬盘%s：%s" % ("故障" if level == "bad" else "预警", disk.get("device", "?"))
            lines = ["型号：%s" % (disk.get("model") or "未知"),
                     "容量：%s" % (disk.get("size") or "未知"),
                     "健康：%s" % (disk.get("health") or "未知")]
            warns = disk.get("warnings") or []
            if warns:
                lines.append("提示：" + "；".join(warns[:6]))
            res = notify.send(title, "\n".join(lines), level="error", event="smart")
            if res.get("ok"):
                fired.append(key)
            else:
                # 没发出去就把去重标记撤掉，下一轮还能再试（否则这条告警等于被吞 24 小时）
                notify.forget(key)
    except Exception:
        pass

    seen = set()
    for path in DISK_PATHS:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        pct = _usage(real)
        if pct is None or pct < threshold:
            continue
        key = "disk:" + real
        if not notify.once(key, hours=24):
            continue
        res = notify.send("磁盘空间告警",
                          "%s 已使用 %.0f%%（阈值 %d%%），建议清理或扩容。"
                          % (path, pct, threshold),
                          level="warn", event="system")
        if res.get("ok"):
            fired.append(key)
        else:
            notify.forget(key)

    return fired


def start_watcher(stop_event=None, interval=INTERVAL):
    """起一个守护线程，返回 (thread, stop_event)。"""
    stop = stop_event or threading.Event()

    def loop():
        if stop.wait(START_DELAY):
            return
        while not stop.is_set():
            try:
                check_once()
            except Exception:
                pass
            if stop.wait(interval):
                return

    thread = threading.Thread(target=loop, name="alerts", daemon=True)
    thread.start()
    return thread, stop
