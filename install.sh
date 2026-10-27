#!/bin/sh
# =============================================================================
# elmsamaras - 轻量 NAS/下载套件（Debian Edition）
#
# 一条命令安装（推荐）:
#   curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/get.sh | sudo bash
#   curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/get.sh | sudo bash -s -- --full
#
# 局域网自建源（任一台已装机器可充当分发源）:
#   curl -fsSL http://<面板地址>:8090/get.sh | sudo bash
#
# 其它用法:
#   sudo ./install.sh                      源码目录内安装（轻量，默认）
#   sudo ./install.sh --full               面板 + Samba + SMART + Docker 三件套
#   sudo ./install.sh --full --docker-mirror https://docker.m.daocloud.io
#   sudo ./install.sh --with aria2,transmission-daemon,nfs-kernel-server
#   sudo ./install.sh --without-samba      不装 Samba
#   sudo ./install.sh --port 8080          自定义面板端口
#   sudo sh elmsamaras-<ver>-install.sh    自解压单文件（离线，源码内嵌）
#   curl -fsSL http://host:8090/install.sh | sudo bash -s -- --from <tar.gz 地址>
#
# 安装内容:
#   /opt/nsuite/{nsuite,web}     面板程序与前端（复刻 darkmatter 主题）
#   /etc/nsuite/                 配置、账号、共享文件夹、计划任务
#   /etc/samba/smb.conf          Samba 配置（不存在才创建）
#   /usr/local/sbin/nsuite-container  容器部署器（Portainer/Jellyfin/qB）
#   nsuite.service               systemd 服务（开机自启，监听 :8090）
# =============================================================================
set -eu

PREFIX=/opt/nsuite
CONFDIR=/etc/nsuite

# 分发端（面板 /get.sh /install.sh）会把下面这行替换成真实下载地址；
# 仍带 @@ 视为未配置，回退到 GitHub Release 的最新源码包。
DEFAULT_FROM='@@DEFAULT_SRC@@'
GITHUB_FROM='https://github.com/ubui/elmsamaras/releases/latest/download/elmsamaras.tar.gz'
PAYLOAD_TAG='#__ELMSAMARAS_PAYLOAD__'

PORT=8090
BRAND="${NSUITE_BRAND:-elmsamaras}"   # 面板显示名，安装时可用 NSUITE_BRAND="xxx" ./install.sh 自定义
WITH=""
FULL=0
DOCKER_MIRROR=""
FROM="${ELMSAMARAS_FROM:-}"

usage() {
  cat <<'EOF'
elmsamaras - 轻量 NAS/下载套件（Debian Edition）

一条命令安装:
  curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/get.sh | sudo bash
  curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/get.sh | sudo bash -s -- --full

局域网自建源（任一台已装机器都可充当）:
  curl -fsSL http://<面板地址>:8090/get.sh | sudo bash

选项:
  --full                 额外部署 Docker + Portainer + Jellyfin + qBittorrent
  --docker-mirror URL    配置 Docker 镜像加速（国内拉不到镜像时用）
  --with pkg1,pkg2       额外安装组件，逗号分隔
  --without-samba        不安装 Samba
  --port N               面板端口（默认 8090）
  --from URL|PATH        源码包地址或本地 tar.gz 路径（管道执行时自动推断）
  -h, --help             显示本帮助

环境变量:
  NSUITE_BRAND           面板显示名（默认 elmsamaras）
  ELMSAMARAS_FROM        同 --from
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --with) WITH="$2"; shift 2 ;;
    --full) FULL=1; shift ;;
    --docker-mirror) DOCKER_MIRROR="$2"; shift 2 ;;
    --without-samba) WITH="${WITH:+$WITH,}no-samba"; shift ;;
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
_have_source() { [ -f "$1/nsuite/__init__.py" ] && [ -f "$1/web/index.html" ]; }

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
  _tmp=$(mktemp -d "${TMPDIR:-/tmp}/elmsamaras.XXXXXX") || return 1
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
      _d=$(mktemp -d "${TMPDIR:-/tmp}/elmsamaras-dl.XXXXXX") || return 1
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
  grep -an '^#__ELMSAMARAS_PAYLOAD__$' "$0" 2>/dev/null | head -1 | cut -d: -f1
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
    _ptmp=$(mktemp -d "${TMPDIR:-/tmp}/elmsamaras-self.XXXXXX") || exit 1
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
       curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/get.sh \
         | sudo bash

  2. 直接指定源码包：
       curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/install.sh \
         | sudo bash -s -- --from https://github.com/ubui/elmsamaras/releases/latest/download/elmsamaras.tar.gz

  3. 局域网自建源（已有机器装好 elmsamaras 时）：
       curl -fsSL http://<面板地址>:8090/get.sh | sudo bash

  4. 下载后本地执行：
       curl -fsSLO https://github.com/ubui/elmsamaras/releases/latest/download/elmsamaras.tar.gz
       tar xzf elmsamaras.tar.gz && sudo ./elmsamaras-*/install.sh

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
  echo "[!] 未检测到 systemd，将不注册开机自启（可用 /opt/nsuite/start.sh 手动启动）"
fi

# ---------- 组件开关 ----------
INSTALL_SAMBA=1
case ",$WITH," in
  *,no-samba,*) INSTALL_SAMBA=0; WITH=$(echo "$WITH" | sed 's/no-samba//; s/,,*/,/g; s/^,//; s/,$//') ;;
esac

apt-get update -qq || true

if [ -n "$WITH" ]; then
  echo "[*] 安装组件: $WITH"
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    $(echo "$WITH" | tr ',' ' ') || echo "[!] 部分组件安装失败，可稍后手动补装"
fi

# SMART 是内置功能（硬盘健康页），默认装上
echo "[*] 安装 smartmontools（硬盘健康）"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq smartmontools \
  || echo "[!] smartmontools 安装失败，硬盘健康页会显示未安装"

if [ "$INSTALL_SAMBA" = 1 ]; then
  echo "[*] 安装内置 Samba"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq samba samba-common-bin \
    && echo "    Samba 已就绪" \
    || echo "[!] Samba 安装失败，面板会显示“未安装”，稍后可手动 apt install samba"
fi

# ---------- 部署程序 ----------
echo "[*] 部署到 $PREFIX"
mkdir -p "$PREFIX" "$CONFDIR" /var/lib/nsuite/tmp /var/lib/nsuite/backups
rm -rf "$PREFIX/nsuite"
cp -r "$SRCDIR/nsuite"  "$PREFIX/nsuite"
rm -rf "$PREFIX/nsuite/__pycache__"
rm -rf "$PREFIX/web"
cp -r "$SRCDIR/web"  "$PREFIX/web"
# 安装/卸载/引导脚本一并落到 PREFIX，面板据此对外提供"一条命令安装"入口
mkdir -p "$PREFIX/dist"
for _f in install.sh uninstall.sh get.sh; do
  if [ -f "$SRCDIR/$_f" ]; then cp "$SRCDIR/$_f" "$PREFIX/$_f"; fi
done
chmod 755 "$PREFIX" "$PREFIX/nsuite"
if [ -f "$PREFIX/install.sh" ]; then chmod 755 "$PREFIX/install.sh"; fi
if [ -f "$PREFIX/get.sh" ]; then chmod 755 "$PREFIX/get.sh"; fi

# ---------- 分发源：让装好的这台机器也能给别的机器一键安装 ----------
stage_dist() {
  _ver=$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$PREFIX/nsuite/__init__.py" 2>/dev/null | head -1)
  if [ -z "${_ver:-}" ]; then
    echo "    [!] 读不到版本号，跳过分发源准备（不影响安装）"
    return 0
  fi
  _dst="$PREFIX/dist/elmsamaras-$_ver.tar.gz"
  # 手上正好有原始下载包 → 直接用它覆盖，保证分发内容与官方包逐字节一致
  if [ -n "${FETCHED_TGZ:-}" ] && [ -f "$FETCHED_TGZ" ]; then
    cp "$FETCHED_TGZ" "$_dst"
    echo "    分发源: elmsamaras-$_ver.tar.gz（来自下载包）"
    return 0
  fi
  # 源码目录安装：按当前源码重打一份，覆盖同版本号的旧缓存
  _stage=$(mktemp -d "${TMPDIR:-/tmp}/elmsamaras-dist.XXXXXX") || return 0
  _root="$_stage/elmsamaras-$_ver"
  mkdir -p "$_root"
  for _item in install.sh uninstall.sh get.sh README.md etc nsuite web screenshots; do
    if [ -e "$SRCDIR/$_item" ]; then cp -r "$SRCDIR/$_item" "$_root/"; fi
  done
  rm -rf "$_root/nsuite/__pycache__" || true
  rm -f "$_root"/nsuite/*.pyc || true
  if ( cd "$_stage" && tar czf "$_dst" "elmsamaras-$_ver" ); then
    echo "    分发源: 已生成 elmsamaras-$_ver.tar.gz（本地打包）"
  else
    echo "    [!] 分发源打包失败（不影响安装）"
  fi
  rm -rf "$_stage"
}
stage_dist

# ---------- 生成配置 ----------
if [ ! -f "$CONFDIR/nsuite.conf" ]; then
  cat > "$CONFDIR/nsuite.conf" <<EOF
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
  echo "[*] 已写入 $CONFDIR/nsuite.conf"
else
  echo "[*] 保留既有 $CONFDIR/nsuite.conf"
fi

if [ ! -f "$CONFDIR/apps.json" ]; then
  cp "$SRCDIR/etc/apps.json" "$CONFDIR/apps.json" 2>/dev/null \
    || python3 - "$CONFDIR/apps.json" <<'PY'
import json, sys
sys.path.insert(0, "/opt/nsuite")
from nsuite import services
json.dump(services.DEFAULT_APPS, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
PY
  echo "[*] 已写入 $CONFDIR/apps.json（可自行增删服务条目）"
else
  # 升级：把新版清单里缺少的条目补进去，并同步从 systemd 切到容器的结构性变更
  python3 - "$CONFDIR/apps.json" "$SRCDIR/etc/apps.json" <<'PY' || true
import json, sys
cur_path, new_path = sys.argv[1], sys.argv[2]
try:
    cur = json.load(open(cur_path, encoding="utf-8"))
    new = json.load(open(new_path, encoding="utf-8"))
except Exception:
    sys.exit(0)
by_id = {a.get("id"): a for a in cur}
added, switched = [], []
for item in new:
    old = by_id.get(item["id"])
    if old is None:
        cur.append(item)
        by_id[item["id"]] = item
        added.append(item["id"])
    elif item.get("kind") == "docker" and old.get("kind") != "docker":
        for k in ("kind", "container", "unit", "ui", "desc"):
            if k in item:
                old[k] = item[k]
        switched.append(item["id"])
if added or switched:
    json.dump(cur, open(cur_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if added:
        print("[*] 服务清单新增条目:", ", ".join(added))
    if switched:
        print("[*] 服务改为容器承载:", ", ".join(switched))
PY
fi

# ---------- 初始化账号 ----------
# 只在没有账号文件时创建默认账号；升级重装绝不能把用户改过的密码冲掉
NEW_ACCOUNT=0
if [ ! -s "$CONFDIR/passwd" ]; then
  python3 "$PREFIX/nsuite/auth_cli.py" "$CONFDIR/passwd" root root \
    && echo "[*] 默认账号已创建: root / root  （务必尽快修改！）" && NEW_ACCOUNT=1
else
  chmod 600 "$CONFDIR/passwd" 2>/dev/null || true
  echo "[*] 保留既有面板账号（未重置密码）"
fi

# ---------- 共享文件夹 / 计划任务 ----------
if [ "$INSTALL_SAMBA" = 1 ] && command -v smbd >/dev/null 2>&1; then
  echo "[*] 初始化 Samba"
  python3 - <<'PY'
import sys
sys.path.insert(0, "/opt/nsuite")
try:
    from nsuite import samba, shares
except Exception as e:
    print("    跳过 Samba 初始化:", e)
    sys.exit(0)

samba.init_conf()

# 升级场景：已有 [share] 段就原样保留，不要用默认值覆盖用户的设置
existing = {s["name"].lower() for s in samba.list_shares()}
if "share" in existing:
    print("    默认共享 /srv/share : 已存在，保持原样")
else:
    ok, msg = samba.upsert_share("share", {
        "path": "/srv/share",
        "comment": "elmsamaras 默认共享",
        "writable": True,
        "browseable": True,
        "guest_ok": False,
        "valid_users": "root",
    })
    print("    默认共享 /srv/share :", msg)

# 同理：root 已有 SMB 密码时不重置（否则用户改过的 SMB 密码会被装回 root）
have_smb = {u["name"] for u in samba.list_smb_users() if u.get("has_password")}
if "root" in have_smb:
    print("    SMB 账号 root : 已存在，未改密码")
else:
    ok, msg = samba.set_user_password("root", "root")
    print("    SMB 账号 root :", msg)

try:
    if shares.ensure_default():
        print("    共享文件夹：已纳管 /srv/share")
except Exception as e:
    print("    共享文件夹初始化跳过:", e)
PY
  systemctl enable --now smbd nmbd >/dev/null 2>&1 || true
fi

# ---------- 计划任务执行器 ----------
cat > /usr/local/sbin/nsuite-cron-run <<'CRONRUN'
#!/bin/sh
# elmsamaras cron 执行器：运行任务脚本并记录退出码与输出
# 用法: nsuite-cron-run <job-id>
id="$1"
[ -n "$id" ] || exit 2
dir=/var/lib/nsuite/cron-status
mkdir -p "$dir" || exit 1
job=/etc/nsuite/cron-jobs/$id.sh
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
  /usr/bin/python3 /opt/nsuite/nsuite/notify_cli.py --cron-job "$id" >/dev/null 2>&1 || true
fi
exit "$rc"
CRONRUN
chmod 755 /usr/local/sbin/nsuite-cron-run
systemctl enable --now cron >/dev/null 2>&1 || true

# 计划任务：空表 + 生成 /etc/cron.d/nsuite
python3 - <<'PY' || true
import sys
sys.path.insert(0, "/opt/nsuite")
from nsuite import cron
ok, err = cron.sync()
print("    计划任务：cron.d 已就绪" if ok else "    计划任务初始化失败: " + err)
PY

# 自动备份：全新安装默认开启（每天 03:00，保留 10 份）；升级时不动既有设置
python3 - <<'PY' || true
import json, os, sys
sys.path.insert(0, "/opt/nsuite")
from nsuite import backup
path = "/etc/nsuite/backup.json"
if not os.path.exists(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"enabled": True, "schedule": "0 3 * * *", "keep": 10}, f, indent=1)
    ok, err = backup._write_crond(backup.load_auto())
    print("    自动备份：已开启（每天 03:00，保留 10 份）" if ok else "    自动备份初始化失败: " + err)
else:
    ok, err = backup._write_crond(backup.load_auto())
    print("    自动备份：沿用既有设置" + ("" if ok else "（cron 写入失败: %s）" % err))
PY

# ---------- 容器部署器 ----------
cat > /usr/local/sbin/nsuite-container <<'CONTAINER'
#!/bin/sh
# elmsamaras 容器部署器 —— 由面板"一键部署"或 install.sh --full 调用
# 用法: nsuite-container <portainer|jellyfin|qbittorrent|all>
set -eu

DATA=/srv
MEDIA=${NSUITE_MEDIA:-$DATA/media}
DOWNLOADS=${NSUITE_DOWNLOADS:-$DATA/downloads}
TZ_VAL=${TZ:-Asia/Shanghai}

need_docker() {
  command -v docker >/dev/null 2>&1 || { echo "未安装 docker" >&2; exit 1; }
  docker info >/dev/null 2>&1 || {
    systemctl start docker >/dev/null 2>&1 || true
    docker info >/dev/null 2>&1 || { echo "Docker 守护进程未运行" >&2; exit 1; }
  }
}

exists() { docker inspect "$1" >/dev/null 2>&1; }

deploy_portainer() {
  exists portainer && { echo "portainer 已存在"; return 0; }
  docker volume create portainer_data >/dev/null
  docker run -d --name portainer --restart=always \
    -p 9000:9000 -p 9443:9443 \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v portainer_data:/data \
    portainer/portainer-ce:latest >/dev/null
  echo "Portainer 已启动： http://<IP>:9000 （首次访问需设置管理员密码）"
}

deploy_jellyfin() {
  exists nsuite-jellyfin && { echo "jellyfin 已存在"; return 0; }
  mkdir -p "$DATA/jellyfin/config" "$DATA/jellyfin/cache" "$MEDIA"
  # 有核显就挂进去，用于硬件转码
  DEV=""
  [ -d /dev/dri ] && DEV="--device /dev/dri:/dev/dri"
  # shellcheck disable=SC2086
  docker run -d --name nsuite-jellyfin --restart=always \
    -p 8096:8096 \
    -e TZ="$TZ_VAL" \
    -e PUID=0 -e PGID=0 \
    -v "$DATA/jellyfin/config:/config" \
    -v "$DATA/jellyfin/cache:/cache" \
    -v "$MEDIA:/media" \
    $DEV \
    nyanmisaka/jellyfin:latest >/dev/null
  echo "Jellyfin 已启动： http://<IP>:8096 （媒体库目录 /srv/media）"
}

deploy_qbittorrent() {
  exists nsuite-qbittorrent && { echo "qbittorrent 已存在"; return 0; }
  mkdir -p "$DATA/qbittorrent/config" "$DOWNLOADS"
  docker run -d --name nsuite-qbittorrent --restart=always \
    -e PUID=0 -e PGID=0 -e TZ="$TZ_VAL" -e WEBUI_PORT=8083 \
    -p 8083:8083 -p 6881:6881 -p 6881:6881/udp \
    -v "$DATA/qbittorrent/config:/config" \
    -v "$DOWNLOADS:/downloads" \
    lscr.io/linuxserver/qbittorrent:latest >/dev/null
  echo "qBittorrent 已启动： http://<IP>:8083 （初始账号 admin / adminadmin，登录后请修改）"
}

need_docker
case "${1:-}" in
  portainer) deploy_portainer ;;
  jellyfin) deploy_jellyfin ;;
  qbittorrent) deploy_qbittorrent ;;
  all)
    deploy_portainer || echo "Portainer 部署失败（可能是网络拉取镜像超时），可稍后在面板重试"
    deploy_qbittorrent || echo "qBittorrent 部署失败，可稍后在面板重试"
    deploy_jellyfin || echo "Jellyfin 部署失败，可稍后在面板重试"
    ;;
  *) echo "用法: nsuite-container <portainer|jellyfin|qbittorrent|all>"; exit 2 ;;
esac
CONTAINER
chmod 755 /usr/local/sbin/nsuite-container

# ---------- Docker + 容器（--full） ----------
if [ "$FULL" = 1 ]; then
  echo "[*] 安装 Docker"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io docker-compose-v2 \
    || echo "[!] Docker 安装失败，容器相关功能将不可用"
  systemctl enable --now docker >/dev/null 2>&1 || true
  mkdir -p /srv/media /srv/downloads
fi

# 镜像加速（国内网络直连 Docker Hub 常超时）
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

if [ "$FULL" = 1 ]; then
  if docker info >/dev/null 2>&1; then
    echo "[*] 部署容器：Portainer / qBittorrent / Jellyfin(nyanmisaka)"
    echo "    （首次需要拉镜像，视网络情况可能需要几分钟）"
    /usr/local/sbin/nsuite-container all || true
  else
    echo "[!] Docker 守护进程未就绪，跳过容器部署（可在面板 Docker 页一键部署）"
  fi
fi

# ---------- systemd ----------
if [ "$INIT" = systemd ]; then
  cat > /etc/systemd/system/nsuite.service <<EOF
[Unit]
Description=elmsamaras - 轻量 NAS / 下载套件
Documentation=file://$PREFIX/README.md
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m nsuite --conf $CONFDIR/nsuite.conf
WorkingDirectory=$PREFIX
Environment=PYTHONUNBUFFERED=1
# 沙箱下 /tmp 只读，临时文件统一落到数据目录
Environment=TMPDIR=/var/lib/nsuite/tmp
Restart=on-failure
RestartSec=3
# 面板需要写：自身配置、Samba 配置、计划任务、数据目录、共享目录
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$CONFDIR /etc/samba /etc/cron.d /var/lib/nsuite /var/lib/samba /srv -/media -/mnt
ProtectHome=read-only
EOF
  systemctl daemon-reload
  # 升级场景必须 restart：enable --now 对已在运行的服务不会重新加载代码
  systemctl enable nsuite.service >/dev/null 2>&1 || true
  systemctl restart nsuite.service
  echo "[*] nsuite.service 已启动并设为开机自启"
fi

# ---------- 手动启动脚本（无 systemd 时） ----------
cat > "$PREFIX/start.sh" <<EOF
#!/bin/sh
exec python3 -m nsuite --conf $CONFDIR/nsuite.conf
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
[ "$INSTALL_SAMBA" = 1 ] && command -v smbd >/dev/null 2>&1 && \
  echo "  Samba:     已内置，共享 \\\\$IP\\share" || true
command -v smartctl >/dev/null 2>&1 && echo "  硬盘健康:  面板 → 存储与健康（SMART）"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "  Docker:    面板 → Docker（容器启停 / 日志 / 一键部署）"
  docker inspect portainer >/dev/null 2>&1 && echo "  Portainer: http://$IP:9000"
  docker inspect nsuite-jellyfin >/dev/null 2>&1 && echo "  Jellyfin:  http://$IP:8096"
  docker inspect nsuite-qbittorrent >/dev/null 2>&1 && echo "  qBittorrent: http://$IP:8083  (admin / adminadmin)"
fi
echo
echo "  修改面板密码: python3 $PREFIX/nsuite/auth_cli.py $CONFDIR/passwd root 新密码"
echo "  配置备份:     面板 → 备份（导出/导入 tar.gz，含账号/共享/计划任务）"
echo "  自动备份:     面板 → 备份 → 自动备份（默认每天 03:00，保留 10 份，可在面板关闭）"
echo "  告警通知:     面板 → 通知（Bark / Webhook / SMTP，硬盘与备份异常主动推送）"
echo "  查看日志:     journalctl -u nsuite -f"
echo
echo "  任意 Debian/Ubuntu 机器一条命令安装（走 GitHub）："
echo "    curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/get.sh | sudo bash"
echo "  本机也是局域网分发源（离线可用）："
echo "    curl -fsSL http://$IP:$PORT/get.sh | sudo bash"
echo "  加装 Docker 三件套："
echo "    curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/get.sh | sudo bash -s -- --full"
echo "=============================================================="
