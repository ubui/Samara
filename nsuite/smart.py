"""磁盘健康（SMART）：解析 smartctl 的 JSON 输出，给出人可读的健康结论。

不依赖 smartmontools 的 Python 绑定，只调用命令行：
- 优先 `smartctl -a -j`（smartmontools >= 7.0 的 JSON 输出）
- 老版本或解析失败时退回文本解析
- USB 桥接盘常需 `-d sat`，首次失败会自动重试

读取设备需要 root（面板以 root 运行），只读操作，不会写盘。
"""

import json
import os
import re
import shutil
import subprocess
import time

SYS_BLOCK = "/sys/block"

# 关注的 ATA 属性（id: 说明）。值越低越好的“寿命类”属性单独处理。
ATA_CRITICAL = {
    5: "重映射扇区",
    187: "上报不可纠正错误",
    188: "命令超时",
    196: "重映射事件",
    197: "当前待映射扇区",
    198: "离线不可纠正",
    199: "UDMA CRC 错误",
}
ATA_WEAR = {
    231: "SSD 剩余寿命",
    233: "介质磨损度",
    202: "寿命已用",
}

SKIP_PREFIX = ("loop", "ram", "zram", "sr", "dm-", "md", "nbd", "fd")
MIN_SIZE = 64 * 1024 * 1024        # 小于 64MiB 的块设备忽略
CACHE_TTL = 45                     # 秒，SMART 查询较慢，做短缓存

_cache = {}


# ---------------------------------------------------------------- 基础工具

def available():
    return shutil.which("smartctl") is not None


def _run(args, timeout=25):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return default


def _int(val, default=None):
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 设备枚举

def devices():
    """列出可用磁盘（不含分区、loop、ram 等虚拟设备）。"""
    out = []
    if not os.path.isdir(SYS_BLOCK):
        return out
    for name in sorted(os.listdir(SYS_BLOCK)):
        if name.startswith(SKIP_PREFIX):
            continue
        base = os.path.join(SYS_BLOCK, name)
        sectors = _int(_read(os.path.join(base, "size")), 0) or 0
        size = sectors * 512
        if size < MIN_SIZE:
            continue

        rotational = _read(os.path.join(base, "queue", "rotational"), "1")
        model = (_read(os.path.join(base, "device", "model"))
                 or _read(os.path.join(base, "device", "name")) or "")
        vendor = _read(os.path.join(base, "device", "vendor"))
        if vendor and vendor.lower() not in model.lower():
            model = f"{vendor} {model}".strip()
        removable = _read(os.path.join(base, "removable"), "0") == "1"

        out.append({
            "device": name,
            "path": f"/dev/{name}",
            "size": size,
            "model": model or "未知型号",
            "type": "hdd" if rotational == "1" else "ssd",
            "removable": removable,
        })
    return out


# ---------------------------------------------------------------- SMART 读取

def _smartctl_json(dev):
    """调用 smartctl 拿 JSON。返回 (dict|None, 提示文本)。"""
    last_err = ""
    for extra in ([], ["-d", "sat"], ["-d", "nvme"]):
        rc, out, err = _run(["smartctl", "-a", "-j", *extra, f"/dev/{dev}"], timeout=30)
        text = (out or "").strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                continue
            # 设备打不开时 smartctl 也会给 JSON，这时换下一种 -d 再试
            if data.get("smartctl", {}).get("messages"):
                msgs = data["smartctl"]["messages"]
                if any(m.get("severity") == "error" for m in msgs) and extra != ["-d", "nvme"]:
                    last_err = "; ".join(m.get("string", "") for m in msgs)[:200]
                    continue
            return data, ""
        last_err = (err or out or "").strip().splitlines()[-1:] or [""]
        last_err = last_err[0][:200]
    return None, last_err or "smartctl 无 JSON 输出"


def _normalize(dev, data):
    """把 smartctl JSON 归一化成面板用的结构。"""
    st = data.get("smartctl", {}) or {}
    dev_info = {
        "device": dev,
        "model": data.get("model_name") or data.get("device", {}).get("name") or "未知型号",
        "serial": data.get("serial_number") or "",
        "firmware": data.get("firmware_version") or "",
        "protocol": data.get("device", {}).get("protocol") or "",
        "capacity": (data.get("user_capacity") or {}).get("bytes"),
        "temperature": None,
        "power_on_hours": _int((data.get("power_on_time") or {}).get("hours")),
        "power_cycles": _int(data.get("power_cycle_count")),
        "health": "unknown",
        "smart_available": bool(st.get("messages") is None),
        "attrs": [],
        "warnings": [],
        "errors": [m.get("string", "") for m in (st.get("messages") or [])
                   if m.get("severity") in ("error", "warning")],
    }

    # 健康状态
    status = (data.get("smart_status") or {}).get("passed")
    if status is True:
        dev_info["health"] = "PASSED"
    elif status is False:
        dev_info["health"] = "FAILED"
        dev_info["warnings"].append("SMART 整体健康自检未通过（FAILED）")

    # 温度（ATA 在 temperature.current，NVMe 在自己的日志里）
    temp = (data.get("temperature") or {}).get("current")
    if temp is None:
        temp = (data.get("nvme_smart_health_information_log") or {}).get("temperature")
    dev_info["temperature"] = _int(temp)

    # ATA 属性表
    table = ((data.get("ata_smart_attributes") or {}).get("table")) or []
    for row in table:
        aid = _int(row.get("id"))
        raw_obj = row.get("raw") or {}
        raw_val = _int(raw_obj.get("value"))
        attr = {
            "id": aid,
            "name": row.get("name") or "",
            "value": _int(row.get("value")),
            "worst": _int(row.get("worst")),
            "thresh": _int(row.get("thresh")),
            "raw": raw_obj.get("string") or str(raw_obj.get("value") or ""),
            "raw_value": raw_val,
            "failing": _int(row.get("when_failed"), 0) if row.get("when_failed") else 0,
        }
        dev_info["attrs"].append(attr)

        if aid in ATA_CRITICAL and (raw_val or 0) > 0:
            if aid in (5, 196, 197, 198):
                dev_info["warnings"].append(
                    f"{ATA_CRITICAL[aid]} = {raw_val}（{attr['name']}），建议尽快备份数据")
            elif aid == 199:
                dev_info["warnings"].append(
                    f"UDMA CRC 错误 {raw_val} 次，通常是数据线/接口接触问题，建议换 SATA 线")
            elif aid in (187, 188):
                dev_info["warnings"].append(f"{ATA_CRITICAL[aid]} = {raw_val}")
        if aid in ATA_WEAR and attr["value"] is not None:
            # 剩余寿命 100 为全新，低于 10 报警；202 反过来（已用百分比）
            if aid == 202:
                if attr["value"] >= 90:
                    dev_info["warnings"].append(f"{ATA_WEAR[aid]} {attr['value']}%")
            elif attr["value"] <= 10:
                dev_info["warnings"].append(f"{ATA_WEAR[aid]}仅剩 {attr['value']}%")

    if dev_info["temperature"] and dev_info["temperature"] >= 55:
        dev_info["warnings"].append(f"温度偏高：{dev_info['temperature']}°C")

    # NVMe 健康日志
    nvme = data.get("nvme_smart_health_information_log")
    if nvme:
        used = _int(nvme.get("percentage_used"))
        dev_info["nvme"] = {
            "percentage_used": used,
            "data_units_read": _int(nvme.get("data_units_read")),
            "data_units_written": _int(nvme.get("data_units_written")),
            "media_errors": _int(nvme.get("media_errors")),
            "unsafe_shutdowns": _int(nvme.get("unsafe_shutdowns")),
            "critical_warning": _int(nvme.get("critical_warning")),
        }
        if used is not None and used >= 90:
            dev_info["warnings"].append(f"NVMe 寿命已用 {used}%")
        if (dev_info["nvme"]["media_errors"] or 0) > 0:
            dev_info["warnings"].append(f"NVMe 介质错误 {dev_info['nvme']['media_errors']} 次")

    # 自检记录
    selftest = ((data.get("ata_smart_data") or {}).get("self_test") or {}).get("status") or {}
    if selftest:
        dev_info["selftest"] = {
            "passed": selftest.get("passed"),
            "status": (selftest.get("string") or "").strip(),
            "remaining": _int(selftest.get("remaining_percent")),
        }

    # SCSI 坏道计数
    grown = data.get("scsi_grown_defect_list")
    if grown is not None:
        dev_info["grown_defects"] = _int(grown)
        if (_int(grown) or 0) > 0:
            dev_info["warnings"].append(f"SCSI 增长坏道 {grown} 个")

    return dev_info


def info(dev, use_cache=True):
    """单块盘的 SMART 详情。"""
    now = time.time()
    hit = _cache.get(dev)
    if use_cache and hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    if not available():
        return {"device": dev, "error": "未安装 smartmontools（apt install smartmontools）"}

    data, err = _smartctl_json(dev)
    if data is None:
        result = {"device": dev, "error": f"无法读取 SMART：{err}"}
    else:
        result = _normalize(dev, data)
        if result.get("errors") and not result.get("attrs") and not result.get("nvme"):
            result["error"] = result["errors"][0]

    _cache[dev] = (now, result)
    return result


def overview():
    """面板主接口：设备清单 + 每块盘的健康摘要。"""
    if not available():
        return {
            "installed": False,
            "message": "未安装 smartmontools，执行 apt install smartmontools 后刷新",
            "disks": [],
        }

    disks = []
    for d in devices():
        detail = info(d["device"])
        merged = {**d, **{k: v for k, v in detail.items() if k != "device"}}
        merged["device"] = d["device"]
        # 综合等级：good / warn / bad / unknown
        has_data = bool(
            detail.get("attrs") or detail.get("nvme")
            or detail.get("grown_defects") is not None
            or detail.get("health") in ("PASSED", "FAILED")
        )
        warns = detail.get("warnings") or []
        fatal = any(k in w for w in warns
                    for k in ("FAILED", "重映射", "待映射", "不可纠正"))
        if not has_data:
            merged["level"] = "unknown"
        elif detail.get("health") == "FAILED" or fatal:
            merged["level"] = "bad"
        elif warns:
            merged["level"] = "warn"
        else:
            merged["level"] = "good"
        disks.append(merged)

    return {"installed": True, "disks": disks}


def selftest(dev, kind="short"):
    """启动自检：kind ∈ short / long / abort / conv。"""
    args = {
        "short": ["-t", "short"],
        "long": ["-t", "long"],
        "abort": ["-X"],
        "conv": ["-t", "conveyance"],
    }.get(kind)
    if not args:
        return False, "不支持的检测类型"
    if not available():
        return False, "未安装 smartmontools"
    # 设备名可能是 sda / nvme0n1 / mmcblk0 —— 必须允许数字，否则 NVMe 永远无法自检
    if not re.match(r"^[a-z]+[0-9]*[a-z]*[0-9]*$", dev or ""):
        return False, "设备名不合法"

    rc, out, err = _run(["smartctl", *args, f"/dev/{dev}"], timeout=30)
    if rc & 0x02:                       # bit1 = 命令失败
        return False, (err or out or "smartctl 执行失败").strip().splitlines()[-1][:200]
    _cache.pop(dev, None)
    label = {"short": "短自检（约 2 分钟）", "long": "长自检（可能数小时）",
             "abort": "已中止自检", "conv": "传输自检"}[kind]
    return True, f"{dev}：{label}已启动"
