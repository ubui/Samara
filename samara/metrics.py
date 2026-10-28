"""系统指标采集：只读 /proc 与 /sys，无第三方依赖。

后台线程每秒采样一次并缓存，HTTP 请求直接读缓存，避免阻塞。
"""

import os
import platform
import shutil
import socket
import threading
import time


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def _read_int(path, default=0):
    try:
        return int(_read(path).strip())
    except (ValueError, OSError):
        return default


class MetricsCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self._cache = {}
        self._prev_cpu = self._cpu_times()
        self._prev_net = self._net_bytes()
        self._prev_ts = time.time()
        self._stop = threading.Event()
        self._thread = None

    # ---------- 采样 ----------

    @staticmethod
    def _cpu_times():
        line = _read("/proc/stat").splitlines()
        if not line:
            return None
        parts = line[0].split()
        if len(parts) < 5:
            return None
        vals = [int(x) for x in parts[1:9]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return {"total": sum(vals), "idle": idle}

    @staticmethod
    def _net_bytes():
        out = {}
        for line in _read("/proc/net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo":
                continue
            f = rest.split()
            if len(f) < 9:
                continue
            out[iface] = {"rx": int(f[0]), "tx": int(f[8])}
        return out

    @staticmethod
    def _temperature():
        """返回 (摄氏度, 来源标签)。优先 CPU 热区，其次 hwmon。"""
        best = None
        base = "/sys/class/thermal"
        try:
            for zone in sorted(os.listdir(base)):
                if not zone.startswith("thermal_zone"):
                    continue
                t = _read_int(f"{base}/{zone}/temp", -1)
                if t <= 0:
                    continue
                label = _read(f"{base}/{zone}/type").strip() or zone
                c = t / 1000.0 if t > 1000 else float(t)
                if 0 < c < 150 and (best is None or c > best[0]):
                    best = (round(c, 1), label)
        except OSError:
            pass
        if best:
            return best
        for hw in ("/sys/class/hwmon",):
            try:
                for dev in os.listdir(hw):
                    p = f"{hw}/{dev}/temp1_input"
                    t = _read_int(p, -1)
                    if t > 0:
                        return (round(t / 1000.0, 1), _read(f"{hw}/{dev}/name").strip() or "hwmon")
            except OSError:
                pass
        return (None, None)

    @staticmethod
    def _memory():
        info = {}
        for line in _read("/proc/meminfo").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            num = v.strip().split()
            if num and num[0].isdigit():
                info[k] = int(num[0]) * 1024  # kB -> bytes
        total = info.get("MemTotal", 0)
        free = info.get("MemFree", 0)
        avail = info.get("MemAvailable", free + info.get("Buffers", 0) + info.get("Cached", 0))
        buffers = info.get("Buffers", 0)
        cached = info.get("Cached", 0)
        st = info.get("SwapTotal", 0)
        sf = info.get("SwapFree", 0)
        return {
            "total": total,
            "used": max(total - avail, 0),
            "available": avail,
            "free": free,
            "buffers": buffers,
            "cached": cached,
            "percent": round((total - avail) / total * 100, 1) if total else 0,
            "swap_total": st,
            "swap_used": max(st - sf, 0),
            "swap_percent": round((st - sf) / st * 100, 1) if st else 0,
        }

    @staticmethod
    def _disks():
        """列出真实块设备挂载点及用量。"""
        skip_fs = {
            "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
            "overlay", "squashfs", "ramfs", "debugfs", "securityfs", "pstore",
            "bpf", "tracefs", "configfs", "fusectl", "mqueue", "hugetlbfs",
            "autofs", "binfmt_misc", "rpc_pipefs", "nsfs", "efivarfs",
        }
        skip_mnt = ("/snap", "/var/lib/docker", "/run", "/sys", "/proc", "/dev")
        seen_dev, out = set(), []
        for line in _read("/proc/mounts").splitlines():
            f = line.split()
            if len(f) < 3:
                continue
            dev, mnt, fstype = f[0], f[1], f[2]
            if fstype in skip_fs or not dev.startswith("/dev/"):
                continue
            if mnt.startswith(skip_mnt):
                continue
            if dev in seen_dev:
                continue
            seen_dev.add(dev)
            try:
                u = shutil.disk_usage(mnt)
            except OSError:
                continue
            out.append({
                "device": os.path.basename(dev),
                "mount": mnt,
                "fs": fstype,
                "total": u.total,
                "used": u.used,
                "free": u.free,
                "percent": round(u.used / u.total * 100, 1) if u.total else 0,
            })
        out.sort(key=lambda d: d["mount"])
        return out

    @staticmethod
    def _uptime():
        try:
            up = float(_read("/proc/uptime").split()[0])
        except (ValueError, IndexError):
            return 0
        return int(up)

    @staticmethod
    def _loadavg():
        parts = _read("/proc/loadavg").split()
        if len(parts) < 3:
            return [0.0, 0.0, 0.0]
        return [float(p) for p in parts[:3]]

    @staticmethod
    def _osinfo():
        name = "Debian"
        ver = ""
        for line in _read("/etc/os-release").splitlines():
            if line.startswith("PRETTY_NAME="):
                name = line.split("=", 1)[1].strip().strip('"')
                break
        return name, ver

    @staticmethod
    def _ipv4():
        """取默认路由网卡的地址，不依赖外部命令。"""
        addr, iface = "", ""
        for line in _read("/proc/net/route").splitlines()[1:]:
            f = line.split()
            if len(f) > 2 and f[1] == "00000000":
                iface = f[0]
                break
        if iface:
            try:
                import fcntl
                import struct
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                packed = fcntl.ioctl(s.fileno(), 0x8915,
                                     struct.pack("256s", iface[:15].encode()))
                addr = socket.inet_ntoa(packed[20:24])
                s.close()
            except OSError:
                addr = ""
        return iface, addr

    # ---------- 对外 ----------

    def sample(self):
        now = time.time()
        cpu_now = self._cpu_times()
        net_now = self._net_bytes()

        cpu_pct = 0.0
        if cpu_now and self._prev_cpu:
            dt = cpu_now["total"] - self._prev_cpu["total"]
            di = cpu_now["idle"] - self._prev_cpu["idle"]
            if dt > 0:
                cpu_pct = round((1 - di / dt) * 100, 1)
        self._prev_cpu = cpu_now

        elapsed = max(now - self._prev_ts, 0.001)
        net_rates = {}
        for iface, cur in net_now.items():
            prev = self._prev_net.get(iface, {"rx": cur["rx"], "tx": cur["tx"]})
            net_rates[iface] = {
                "rx_total": cur["rx"],
                "tx_total": cur["tx"],
                "rx_rate": max(int((cur["rx"] - prev["rx"]) / elapsed), 0),
                "tx_rate": max(int((cur["tx"] - prev["tx"]) / elapsed), 0),
            }
        self._prev_net = net_now
        self._prev_ts = now

        temp, temp_src = self._temperature()
        iface, ip = self._ipv4()
        osname, _ = self._osinfo()
        mem = self._memory()

        data = {
            "hostname": socket.gethostname(),
            "os": osname,
            "kernel": platform.release(),
            "uptime": self._uptime(),
            "load": self._loadavg(),
            "cpu": {"percent": cpu_pct, "cores": os.cpu_count()},
            "memory": mem,
            "temperature": temp,
            "temperature_source": temp_src,
            "disks": self._disks(),
            "net": {"iface": iface, "ipv4": ip, "interfaces": net_rates},
            "time": int(now),
        }
        with self._lock:
            self._cache = data
        return data

    def start(self, interval=1.0):
        def loop():
            while not self._stop.is_set():
                try:
                    self.sample()
                except Exception:
                    pass
                self._stop.wait(interval)

        self.sample()
        self._thread = threading.Thread(target=loop, daemon=True, name="samara-metrics")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self._lock:
            return dict(self._cache) if self._cache else self.sample()
