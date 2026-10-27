#!/bin/sh
# =============================================================================
# elmsamaras 一键安装引导器
#
#   curl -fsSL http://<面板地址>:8090/get.sh | sudo bash
#   curl -fsSL http://<面板地址>:8090/get.sh | sudo bash -s -- --full
#   curl -fsSL http://<面板地址>:8090/get.sh | sudo bash -s -- --full --port 8080
#
# 做的事：检查环境 → 下载源码包 → 解压 → 交给 install.sh（参数原样透传）。
# 下载地址优先级：--base 参数 > 面板注入 > GitHub Release
# =============================================================================
set -eu

REPO='ubui/elmsamaras'
GH_BASE="https://github.com/$REPO/releases/latest/download"

# 面板分发时注入；保留 @@ 表示未注入（GitHub / 本地直接运行）
BASE='@@BASE_URL@@'
VERSION='@@VERSION@@'
LAYOUT=dist        # dist=<base>/dist/<pkg>（面板/自建源）  flat=<base>/<pkg>（GitHub Release）

PKG_PREFIX="elmsamaras"

usage() {
  cat <<'EOF'
elmsamaras 一键安装引导器

用法:
  curl -fsSL https://github.com/ubui/elmsamaras/releases/latest/download/get.sh | sudo bash
  curl -fsSL http://<面板>:8090/get.sh | sudo bash        # 局域网自建源

自身选项（必须写在最前面）:
  --base URL       源码包所在地址（默认：面板注入值，否则 GitHub Release）
  --version VER    指定版本（默认最新）
  -h, --help       显示本帮助

其余选项原样透传给 install.sh:
  --full                 额外部署 Docker + Portainer + Jellyfin + qBittorrent
  --docker-mirror URL    配置 Docker 镜像加速
  --with pkg1,pkg2       额外安装组件
  --without-samba        不安装 Samba
  --port N               面板端口（默认 8090）
  --from URL|PATH        直接指定源码包（跳过下载）
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --base)    BASE=$2; shift 2 ;;
    --version) VERSION=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) break ;;          # 其余全部交给 install.sh
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf '[!] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "请用 root 运行：curl -fsSL <地址> | sudo bash"

case "$VERSION" in ""|*'@@'*) VERSION="" ;; esac

# BASE 未注入时的归属：本地 dist/（开发场景）> GitHub Release
case "$BASE" in
  ""|*'@@'*)
    BASE=""
    case "$0" in
      */*)
        _d=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
        if [ -n "${_d:-}" ] && [ -d "$_d/dist" ]; then BASE="file://$_d"; fi
        ;;
    esac
    if [ -z "$BASE" ]; then BASE="$GH_BASE"; LAYOUT=flat; fi
    ;;
esac

if [ "$LAYOUT" = flat ]; then
  # GitHub Release：不带版本号的 elmsamaras.tar.gz 恒指向最新版
  VERSION="${VERSION:-latest}"
  if [ "$VERSION" = latest ]; then
    PKG="${PKG_PREFIX}.tar.gz"
  else
    PKG="${PKG_PREFIX}-${VERSION}.tar.gz"
  fi
  URL="${BASE%/}/${PKG}"
else
  # 自建源：<base>/dist/<pkg>；版本未指定时从本地目录枚举
  if [ -z "$VERSION" ]; then
    case "$BASE" in
      file://*)
        _hit=$(ls -1 "${BASE#file://}"/dist/${PKG_PREFIX}-*.tar.gz 2>/dev/null \
               | grep -v -- '-install\.sh$' | sed 's/.*elmsamaras-//; s/\.tar\.gz$//' \
               | sort -V | tail -1)
        [ -n "${_hit:-}" ] && VERSION="$_hit"
        ;;
    esac
  fi
  [ -n "$VERSION" ] || die "没有可用的版本信息，请加 --version <版本> 或 --base <地址>"
  PKG="${PKG_PREFIX}-${VERSION}.tar.gz"
  URL="${BASE%/}/dist/${PKG}"
fi

say "=============================================="
say " elmsamaras ${VERSION} 一键安装"
say "=============================================="
say "源码包: $URL"

if [ -f /opt/nsuite/nsuite/__init__.py ]; then
  _old=$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' /opt/nsuite/nsuite/__init__.py 2>/dev/null | head -1)
  say "已安装: ${_old:-未知版本} → 本次为升级安装（配置与账号会保留）"
fi

# ---------- 准备下载工具 ----------
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  say "[*] 安装 curl ..."
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl >/dev/null 2>&1 || true
  fi
  command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1 \
    || die "需要 curl 或 wget，请先手动安装"
fi

# ---------- 下载 ----------
TMPD=$(mktemp -d "${TMPDIR:-/tmp}/elmsamaras-get.XXXXXX") || die "无法创建临时目录"
trap 'rm -rf "$TMPD"' EXIT INT TERM

say "[*] 下载中 ..."
case "$URL" in
  file://*)
    cp "${URL#file://}" "$TMPD/$PKG" || die "读取失败：${URL#file://}"
    ;;
  *)
    if command -v curl >/dev/null 2>&1; then
      curl -fL --retry 3 --connect-timeout 15 --progress-bar -o "$TMPD/$PKG" "$URL" \
        || die "下载失败：$URL"
    else
      wget -q --show-progress -O "$TMPD/$PKG" "$URL" || die "下载失败：$URL"
    fi
    ;;
esac

# ---------- 解压 ----------
say "[*] 解压 ..."
tar xzf "$TMPD/$PKG" -C "$TMPD" || die "解压失败（包可能损坏）"
SRCDIR=""
for _cand in "$TMPD"/*/install.sh "$TMPD"/install.sh; do
  [ -f "$_cand" ] || continue
  _dir=$(dirname "$_cand")
  if [ -f "$_dir/nsuite/__init__.py" ]; then SRCDIR=$_dir; break; fi
done
[ -n "$SRCDIR" ] || die "包里没有找到 install.sh"

# ---------- 交给安装脚本 ----------
say "[*] 开始安装 ..."
say ""
cd "$SRCDIR" || die "进入源码目录失败"
set +e
sh ./install.sh "$@"
rc=$?
exit "$rc"
