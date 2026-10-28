#!/bin/sh
# =============================================================================
# samara - 轻量 NAS 套件（Debian Edition）
#
# 一条命令安装（推荐）:
#   curl -fsSL https://github.com/ubui/Samara/releases/latest/download/get.sh | sudo bash
#
# 局域网自建源（任一台已装机器可充当分发源）:
#   curl -fsSL http://<面板地址>:8090/get.sh | sudo bash
#
# 其它用法:
#   sudo ./install.sh                      源码目录内安装（面板 + Docker + SMART）
#   sudo ./install.sh --minimal            只装面板（不装 Docker / SMART）
#   sudo ./install.sh --docker-mirror https://docker.m.daocloud.io
#   sudo ./install.sh --with samba         顺带装其它 apt 包（逗号分隔）
#   sudo ./install.sh --port 8080          自定义面板端口
#   sudo sh samara-<ver>-install.sh        自解压单文件（离线，源码内嵌）
#   curl -fsSL http://host:8090/install.sh | sudo bash -s -- --from <tar.gz 地址>
#
# 应用一律用 Docker Compose 安装：面板 → 系统 → Docker → Deploy with Compose。
# 装好的应用自动出现在「状态 → 应用」与右上角快捷方式。
#
# 安装内容:
#   /opt/samara/{samara,web}     面板程序与前端（darkmatter 主题）
#   /etc/samara/                 配置、账号、计划任务、Compose 栈
#   samara.service               systemd 服务（开机自启，监听 :8090）
# =============================================================================
PREFIX=/opt/samara
CONFDIR=/etc/samara

# 分发端（面板 /get.sh /install.sh）会把下面这行替换成真实下载地址；
# 仍带 @@ 视为未配置，回退到 GitHub Release 的最新源码包。
DEFAULT_FROM='@@DEFAULT_SRC@@'
GITHUB_FROM='https://github.com/ubui/Samara/releases/latest/download/samara.tar.gz'
PAYLOAD_TAG='#__SAMARA_PAYLOAD__'

PORT=8090
BRAND="${SAMARA_BRAND:-Samara}"   # 面板显示名，安装时可用 SAMARA_BRAND="xxx" ./install.sh 自定义
WITH=""
FULL=0
MINIMAL=0
DOCKER_MIRROR=""
FROM="${SAMARA_FROM:-}"

usage() {
  cat <<'EOF'
samara - 轻量 NAS 套件（Debian Edition）

一条命令安装:
  curl -fsSL https://github.com/ubui/Samara/releases/latest/download/get.sh | sudo bash

局域网自建源（任一台已装机器都可充当）:
  curl -fsSL http://<面板地址>:8090/get.sh | sudo bash

选项:
  --minimal              只装面板（不装 Docker / SMART）
  --docker-mirror URL    配置 Docker 镜像加速（国内拉不到镜像时用）
  --with pkg1,pkg2       额外安装 apt 包，逗号分隔
  --port N               面板端口（默认 8090）
  --from URL|PATH        源码包地址或本地 tar.gz 路径（管道执行时自动推断）
  --full, --without-samba   兼容保留（无实际作用）
  -h, --help             显示本帮助

应用一律用 Docker Compose 安装：面板 → 系统 → Docker → Deploy with Compose。
装好的应用自动出现在「状态 → 应用」与右上角快捷方式。

环境变量:
  SAMARA_BRAND           面板显示名（默认 Samara）
  SAMARA_FROM            同 --from
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --with) WITH="$2"; shift 2 ;;
    --full|--without-samba) shift ;;   # 兼容保留：旧参数，无实际作用
    --minimal) MINIMAL=1; shift ;;
    --docker-mirror) DOCKER_MIRROR="$2"; shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1（用 --help 查看用法）" >&2; exit 1 ;;
  esac
done

# ---------- 定位安装源 ----------
# 三种运行方式都要能工作：
#   1. 源码目录内 ./install.sh        → 直接用当前目录
#   2. 自解压单文件 sh xxx-install.sh → 从脚本尾部解包
#   3. curl ... | sudo bash           → 用 --from / 内置 DEFAULT_FROM 下载
_have_source() { [ -f "$1/samara/__init__.py" ] && [ -f "$1/web/static/css/style.css" ]; }

SRCDIR=""            # 最终使用的源码目录
FETCHED_TGZ=""       # 手上的原始 tar.gz；安装完放进 dist/，让本机也能当分发源
CLEANUP=""           # 运行期产生的临时目录，退出时统一删除

_cleanup_tmp() {
  [ -n "$CLEANUP" ] || return 0
  for _d in $CLEANUP; do rm -rf "$_d"; done
}
trap '_cleanup_tmp' EXIT INT TERM HUP

_unpack_tar() {          # $1=tar.gz 路径；成功则设置 SRCDIR / FETCHED_TGZ
  _tgz=$1
  [ -f "$_tgz" ] || return 1
  _tmp=$(mktemp -d "${TMPDIR:-/tmp}/samara.XXXXXX") || return 1
  CLEANUP="$CLEANUP $_tmp"
  if ! tar xzf "$_tgz" -C "$_tmp" >&2; then
    rm -rf "$_tmp"; return 1
  fi
  _hit=$(find "$_tmp" -maxdepth 3 -type f -name install.sh 2>/dev/null | head -1)
  if [ -z "$_hit" ]; then rm -rf "$_tmp"; return 1; fi
  _dir=$(dirname "$_hit")
  if ! _have_source "$_dir"; then rm -rf "$_tmp"; return 1; fi
  SRCDIR=$_dir
  FETCHED_TGZ=$_tgz
}

_fetch_source() {        # $1=URL 或本地路径
  case "$1" in
    http://*|https://*)
      _d=$(mktemp -d "${TMPDIR:-/tmp}/samara-dl.XXXXXX") || return 1
      CLEANUP="$CLEANUP $_d"
      _out="$_d/pkg.tar.gz"
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 2 --connect-timeout 15 -o "$_out" "$1" >&2 \
          || { rm -rf "$_d"; return 1; }
      elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$_out" "$1" >&2 || { rm -rf "$_d"; return 1; }
      else
        echo "[!] 在线安装需要 curl 或 wget，请先 apt-get install -y curl" >&2
        rm -rf "$_d"; return 1
      fi
      _unpack_tar "$_out" ;;
    file://*) _unpack_tar "${1#file://}" ;;
    *)        _unpack_tar "$1" ;;
  esac
}

_payload_offset() {      # 打印内嵌源码包的起始行号；没有则失败
  [ -f "$0" ] && [ -r "$0" ] || return 1
  grep -an '^#__SAMARA_PAYLOAD__$' "$0" 2>/dev/null | head -1 | cut -d: -f1
}

# 1) 源码目录内直接运行
#    管道执行时 $0 是 sh/bash（无斜杠），此时没有任何路径线索，跳过
case "$0" in
  */*) _guess=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true) ;;
  *)   _guess="" ;;
esac
if [ -n "$_guess" ] && _have_source "$_guess"; then
  SRCDIR="$_guess"
fi

# 2) 自解压单文件：源码包追加在本脚本末尾
if [ -z "$SRCDIR" ]; then
  _off=$(_payload_offset || true)
  if [ -n "${_off:-}" ]; then
    echo "[*] 解出自带源码包 ..."
    _ptmp=$(mktemp -d "${TMPDIR:-/tmp}/samara-self.XXXXXX") || exit 1
    CLEANUP="$CLEANUP $_ptmp"
    # 先落成独立 tar.gz，既能解包，也能留着当本机的分发源
    if tail -n +$((_off + 1)) "$0" > "$_ptmp/pkg.tar.gz" 2>/dev/null; then
      _unpack_tar "$_ptmp/pkg.tar.gz" || true
    fi
  fi
fi

# 3) 远程 / 本地源码包（--from 优先，其次内置下载地址）
if [ -z "$SRCDIR" ]; then
  _from=${FROM:-}
  case "$_from" in ""|*'@@'*) _from=$DEFAULT_FROM ;; esac
  case "$_from" in ""|*'@@'*) _from=$GITHUB_FROM ;; esac
  case "$_from" in ""|*'@@'*) _from="" ;; esac
  if [ -n "$_from" ]; then
    echo "[*] 获取源码包: $_from"
    _fetch_source "$_from" || true
  fi
fi

if [ -z "$SRCDIR" ] || ! _have_source "$SRCDIR"; then
  cat >&2 <<'EOF'

[!] 找不到安装源，无法继续。

用 curl 管道执行时脚本没有自身路径（$0 是 sh），需要指定源码包在哪。
四种可行方式：

  1. 用引导器（最省事，自动处理下载与参数）：
       curl -fsSL https://github.com/ubui/Samara/releases/latest/download/get.sh \
         | sudo bash

  2. 直接指定源码包：
       curl -fsSL https://github.com/ubui/Samara/releases/latest/download/install.sh \
         | sudo bash -s -- --from https://github.com/ubui/Samara/releases/latest/download/samara.tar.gz

  3. 局域网自建源（已有机器装好 samara 时）：
       curl -fsSL http://<面板地址>:8090/get.sh | sudo bash

  4. 下载后本地执行：
       curl -fsSLO https://github.com/ubui/Samara/releases/latest/download/samara.tar.gz
       tar xzf samara.tar.gz && sudo ./samara-*/install.sh

EOF
  exit 1
fi

echo "[*] 源码目录: $SRCDIR"

# ---------- 环境检查 ----------
[ "$(id -u)" = 0 ] || { echo "请用 root 运行（sudo bash install.sh）" >&2; exit 1; }

if ! command -v python3 >/dev/null 2>&1; then
  echo "[*] 安装 python3 ..."
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3
fi
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "[*] python3 $PYV"

if command -v systemctl >/dev/null 2>&1; then
  INIT=systemd
else
  INIT=none
  echo "[!] 未检测到 systemd，将不注册开机自启（可用 /opt/samara/start.sh 手动启动）"
fi

# ---------- 组件安装 ----------
apt-get update -qq || true

if [ -n "$WITH" ]; then
  echo "[*] 安装组件: $WITH"
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    $(echo "$WITH" | tr ',' ' ') || echo "[!] 部分组件安装失败，可稍后手动补装"
fi

# SMART 是「状态 → 磁盘」页的底层能力，不算应用，默认装上
if [ "$MINIMAL" = 0 ]; then
  echo "[*] 安装 smartmontools（硬盘健康）"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq smartmontools \
    || echo "[!] smartmontools 安装失败，硬盘健康页会显示未安装"
fi

# ---------- 部署程序 ----------
echo "[*] 部署到 $PREFIX"
mkdir -p "$PREFIX" "$CONFDIR" /var/lib/samara/tmp /var/lib/samara/backups
rm -rf "$PREFIX/samara"
cp -r "$SRCDIR/samara"  "$PREFIX/samara"
rm -rf "$PREFIX/samara/__pycache__"
rm -rf "$PREFIX/web"
cp -r "$SRCDIR/web"  "$PREFIX/web"
# 安装/卸载/引导脚本一并落到 PREFIX，面板据此对外提供"一条命令安装"入口
mkdir -p "$PREFIX/dist"
for _f in install.sh uninstall.sh get.sh; do
  if [ -f "$SRCDIR/$_f" ]; then cp "$SRCDIR/$_f" "$PREFIX/$_f"; fi
done
chmod 755 "$PREFIX" "$PREFIX/samara"
if [ -f "$PREFIX/install.sh" ]; then chmod 755 "$PREFIX/install.sh"; fi
if [ -f "$PREFIX/get.sh" ]; then chmod 755 "$PREFIX/get.sh"; fi

# ---------- 分发源：让装好的这台机器也能给别的机器一键安装 ----------
stage_dist() {
  _ver=$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$PREFIX/samara/__init__.py" 2>/dev/null | head -1)
  if [ -z "${_ver:-}" ]; then
    echo "    [!] 读不到版本号，跳过分发源准备（不影响安装）"
    return 0
  fi
  _dst="$PREFIX/dist/samara-$_ver.tar.gz"
  # 手上正好有原始下载包 → 直接用它覆盖，保证分发内容与官方包逐字节一致
  if [ -n "${FETCHED_TGZ:-}" ] && [ -f "$FETCHED_TGZ" ]; then
    cp "$FETCHED_TGZ" "$_dst"
    echo "    分发源: samara-$_ver.tar.gz（来自下载包）"
    return 0
  fi
  # 源码目录安装：按当前源码重打一份，覆盖同版本号的旧缓存
  _stage=$(mktemp -d "${TMPDIR:-/tmp}/samara-dist.XXXXXX") || return 0
  _root="$_stage/samara-$_ver"
  mkdir -p "$_root"
  for _item in install.sh uninstall.sh get.sh README.md etc samara web screenshots; do
    if [ -e "$SRCDIR/$_item" ]; then cp -r "$SRCDIR/$_item" "$_root/"; fi
  done
  rm -rf "$_root/samara/__pycache__" || true
  rm -f "$_root"/samara/*.pyc || true
  if ( cd "$_stage" && tar czf "$_dst" "samara-$_ver" ); then
    echo "    分发源: 已生成 samara-$_ver.tar.gz（本地打包）"
  else
    echo "    [!] 分发源打包失败（不影响安装）"
  fi
  rm -rf "$_stage"
}
stage_dist

# ---------- 生成配置 ----------
# 旧系列默认显示名 → Samara（仅当用户未自定义品牌时迁移）
if [ -f "$CONFDIR/samara.conf" ] && [ -z "${SAMARA_BRAND:-}" ]; then
  sed -i 's/^title = \(samara\|elmsamaras\|Elmsamaras\)$/title = Samara/' "$CONFDIR/samara.conf"
fi
if [ ! -f "$CONFDIR/samara.conf" ]; then
  cat > "$CONFDIR/samara.conf" <<EOF
[server]
listen = 0.0.0.0
port = $PORT

[auth]
session_ttl = 86400

[paths]
prefix = $PREFIX
password_file = $CONFDIR/passwd
apps_file = $CONFDIR/apps.json
web_root = $PREFIX/web

[branding]
title = $BRAND
subtitle = Debian Edition

# 仅供本地调试：跳过登录（且仅限回环监听时生效）
[dev]
allow_anonymous = false
EOF
  echo "[*] 已写入 $CONFDIR/samara.conf"
else
  echo "[*] 保留既有 $CONFDIR/samara.conf"
fi

if [ ! -f "$CONFDIR/apps.json" ]; then
  cp "$SRCDIR/etc/apps.json" "$CONFDIR/apps.json" 2>/dev/null \
    || echo '[]' > "$CONFDIR/apps.json"
  echo "[*] 已写入 $CONFDIR/apps.json（自定义快捷方式清单，默认为空）"
else
  # 升级：清掉旧版预置应用条目（应用改由 Docker Compose 安装，自动进应用面板）
  python3 - "$CONFDIR/apps.json" <<'PY' || true
import json, sys
path = sys.argv[1]
try:
    cur = json.load(open(path, encoding="utf-8"))
except Exception:
    cur = []
LEGACY = {"aria2", "qbittorrent", "transmission", "samba", "vsftpd", "syncthing",
          "filebrowser", "jellyfin", "docker", "portainer", "frp"}
kept = [a for a in cur if a.get("id") not in LEGACY]
if kept != cur:
    json.dump(kept, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("[*] 应用清单已更新（移除旧预置条目 %d 个）" % (len(cur) - len(kept)))
PY
fi

# ---------- 初始化账号 ----------
# 只在没有账号文件时创建默认账号；升级重装绝不能把用户改过的密码冲掉
NEW_ACCOUNT=0
if [ ! -s "$CONFDIR/passwd" ]; then
  python3 "$PREFIX/samara/auth_cli.py" "$CONFDIR/passwd" root root \
    && echo "[*] 默认账号已创建: root / root  （务必尽快修改！）" && NEW_ACCOUNT=1
else
  chmod 600 "$CONFDIR/passwd" 2>/dev/null || true
  echo "[*] 保留既有面板账号（未重置密码）"
fi

# ---------- 计划任务 ----------
# ---------- 计划任务执行器 ----------
cat > /usr/local/sbin/samara-cron-run <<'CRONRUN'
#!/bin/sh
# samara cron 执行器：运行任务脚本并记录退出码与输出
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
CRONRUN
chmod 755 /usr/local/sbin/samara-cron-run
systemctl enable --now cron >/dev/null 2>&1 || true

# 计划任务：空表 + 生成 /etc/cron.d/samara
python3 - <<'PY' || true
import sys
sys.path.insert(0, "/opt/samara")
from samara import cron
ok, err = cron.sync()
print("    计划任务：cron.d 已就绪" if ok else "    计划任务初始化失败: " + err)
PY

# 自动备份：全新安装默认开启（每天 03:00，保留 10 份）；升级时不动既有设置
python3 - <<'PY' || true
import json, os, sys
sys.path.insert(0, "/opt/samara")
from samara import backup
path = "/etc/samara/backup.json"
if not os.path.exists(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"enabled": True, "schedule": "0 3 * * *", "keep": 10}, f, indent=1)
    ok, err = backup._write_crond(backup.load_auto())
    print("    自动备份：已开启（每天 03:00，保留 10 份）" if ok else "    自动备份初始化失败: " + err)
else:
    ok, err = backup._write_crond(backup.load_auto())
    print("    自动备份：沿用既有设置" + ("" if ok else "（cron 写入失败: %s）" % err))
PY

# ---------- Docker（应用运行时） ----------
# 应用统一经 Docker Compose 安装；Docker 本身是平台，默认装上
if [ "$MINIMAL" = 0 ]; then
  echo "[*] 安装 Docker（应用统一走 Docker Compose）"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 \
    || echo "[!] Docker 安装失败，容器相关功能将不可用"
  systemctl enable --now docker >/dev/null 2>&1 || true
  mkdir -p /etc/docker /var/lib/samara/stacks /var/lib/samara/tmp
fi

# 镜像加速（面板 系统 → Docker → Image Mirrors 里也能改）
if [ -n "$DOCKER_MIRROR" ]; then
  echo "[*] 配置 Docker 镜像加速: $DOCKER_MIRROR"
  mkdir -p /etc/docker
  python3 - "$DOCKER_MIRROR" <<'PY' || true
import json, os, sys
p = "/etc/docker/daemon.json"
cfg = {}
if os.path.exists(p):
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:
        cfg = {}
mirrors = cfg.get("registry-mirrors") or []
url = sys.argv[1]
if url not in mirrors:
    mirrors.insert(0, url)
cfg["registry-mirrors"] = mirrors
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2)
print("    已写入", p)
PY
  systemctl restart docker >/dev/null 2>&1 || true
fi

# ---------- systemd ----------
if [ "$INIT" = systemd ]; then
  # 沙箱 ReadWritePaths 需要文件真实存在（-/etc/exports 仅容忍缺失，不能新建）
  [ -f /etc/exports ] || : > /etc/exports
  cat > /etc/systemd/system/samara.service <<EOF
[Unit]
Description=samara - 轻量 NAS / 下载套件
Documentation=file://$PREFIX/README.md
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m samara --conf $CONFDIR/samara.conf
WorkingDirectory=$PREFIX
Environment=PYTHONUNBUFFERED=1
# 沙箱下 /tmp 只读，临时文件统一落到数据目录
Environment=TMPDIR=/var/lib/samara/tmp
Restart=on-failure
RestartSec=3
# 面板需要写：自身配置、Samba 配置、计划任务、数据目录、共享目录
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$CONFDIR /etc/cron.d -/etc/docker /var/lib/samara /srv -/media -/mnt
ProtectHome=read-only
EOF
  systemctl daemon-reload
  # 升级场景必须 restart：enable --now 对已在运行的服务不会重新加载代码
  systemctl enable samara.service >/dev/null 2>&1 || true
  systemctl restart samara.service
  echo "[*] samara.service 已启动并设为开机自启"
fi

# ---------- 登录横幅（仿 fnOS：每次交互登录显示面板地址） ----------
# 用 profile.d 而不是 /etc/motd：IP/状态每次登录时动态取，DHCP 变了也不会显示旧地址
# 先清掉旧品牌名留下的横幅（带本安装器标记、但文件名与当前品牌不符的），避免双横幅
for _old in /etc/profile.d/*-motd.sh; do
  [ -f "$_old" ] || continue
  [ "$_old" = "/etc/profile.d/${BRAND}-motd.sh" ] && continue
  grep -qs "install.sh 生成" "$_old" && rm -f "$_old"
done 2>/dev/null || true
_MOTD="/etc/profile.d/${BRAND}-motd.sh"
cat > "$_MOTD" <<EOF
# ${BRAND} 登录横幅 —— 由 install.sh 生成；删除本文件即可去掉横幅
case \$- in *i*) ;; *) return 2>/dev/null || exit 0 ;; esac   # 只在交互式 shell 显示
[ -r $CONFDIR/samara.conf ] || return 2>/dev/null || exit 0

_ns_title=\$(grep -m1 '^title *=' $CONFDIR/samara.conf | cut -d= -f2- | sed 's/^ *//;s/ *\$//')
_ns_port=\$(grep -m1 '^port *=' $CONFDIR/samara.conf | cut -d= -f2- | tr -d '[:space:]')
_ns_ver=\$(sed -n 's/^__version__ = "\(.*\)"/\1/p' $PREFIX/samara/__init__.py 2>/dev/null | head -1)
_ns_ip=\$(hostname -I 2>/dev/null | awk '{print \$1}')
_ns_state=stopped
command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet samara.service && _ns_state=running

echo
echo "=============================================================="
echo
echo "  \${_ns_title:-$BRAND} v\${_ns_ver:-?} (Debian Edition)"
echo "  Hostname:    \$(hostname)"
echo "  IPv4:        \${_ns_ip:-127.0.0.1}"
echo
echo "  \${_ns_title:-$BRAND} Web UI can be directly accessed at: http://\${_ns_ip:-127.0.0.1}:\${_ns_port:-8090}"
if [ "\$_ns_state" = running ]; then
  echo "  面板状态:    运行中"
else
  echo "  面板状态:    未运行（sudo systemctl start samara）"
fi
echo
echo "  面板日志:    journalctl -u samara -f"
echo "  文档:        https://github.com/ubui/Samara"
echo
echo "=============================================================="
unset _ns_title _ns_port _ns_ver _ns_ip _ns_state
EOF
chmod 644 "$_MOTD"
echo "[*] 登录横幅已写入 $_MOTD（删除该文件即不再显示）"

# ---------- 手动启动脚本（无 systemd 时） ----------
cat > "$PREFIX/start.sh" <<EOF
#!/bin/sh
exec python3 -m samara --conf $CONFDIR/samara.conf
EOF
chmod +x "$PREFIX/start.sh"

echo
echo "=============================================================="
echo "  $BRAND 安装完成！"
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1)
echo "  面板地址:  http://$IP:$PORT"
if [ "$NEW_ACCOUNT" = 1 ]; then
  echo "  默认账号:  root / root   （$CONFDIR/passwd，请尽快修改）"
else
  echo "  登录账号:  沿用既有账号（$CONFDIR/passwd，本次未改动）"
fi
command -v smartctl >/dev/null 2>&1 && echo "  硬盘健康:  面板 → 状态 → 磁盘（SMART）"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "  安装应用:  面板 → 系统 → Docker → Deploy with Compose"
  echo "             （装好自动进「状态 → 应用」与右上角快捷方式）"
  echo "  镜像加速:  面板 → 系统 → Docker → Image Mirrors"
fi
echo
echo "  修改面板密码: python3 $PREFIX/samara/auth_cli.py $CONFDIR/passwd root 新密码"
echo "  登录横幅:     每次 SSH/终端登录显示面板地址（/etc/profile.d/${BRAND}-motd.sh，删除即关）"
echo "  配置备份:     面板 → 备份（导出/导入 tar.gz，含账号/计划任务）"
echo "  自动备份:     面板 → 备份 → 自动备份（默认每天 03:00，保留 10 份）"
echo "  告警通知:     面板 → 通知（Bark / Webhook / SMTP，硬盘与备份异常主动推送）"
echo "  查看日志:     journalctl -u samara -f"
echo
echo "  任意 Debian/Ubuntu 机器一条命令安装（走 GitHub）："
echo "    curl -fsSL https://github.com/ubui/Samara/releases/latest/download/get.sh | sudo bash"
echo "  本机也是局域网分发源（离线可用）："
echo "    curl -fsSL http://$IP:$PORT/get.sh | sudo bash"
echo "=============================================================="
