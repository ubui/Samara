#!/bin/sh
# elmsamaras 卸载脚本：停服务、删程序文件。
# 配置与数据（/etc/nsuite、/var/lib/nsuite）默认保留，末尾提示如何彻底清除。
# 容器与 Docker 本身不受影响；--purge-containers 可一并删除面板部署的容器。
set -e
[ "$(id -u)" = 0 ] || { echo "请用 root 运行"; exit 1; }

PURGE_CONTAINERS=0
[ "${1:-}" = "--purge-containers" ] && PURGE_CONTAINERS=1

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now nsuite.service 2>/dev/null || true
  rm -f /etc/systemd/system/nsuite.service
  systemctl daemon-reload
fi

if [ "$PURGE_CONTAINERS" = 1 ] && command -v docker >/dev/null 2>&1; then
  echo "[*] 删除面板部署的容器..."
  docker rm -f portainer nsuite-jellyfin nsuite-qbittorrent 2>/dev/null || true
  docker volume rm portainer_data 2>/dev/null || true
fi

rm -rf /opt/nsuite
rm -f /etc/cron.d/nsuite /etc/cron.d/nsuite-backup
rm -f /usr/local/sbin/nsuite-cron-run /usr/local/sbin/nsuite-container
rm -rf /etc/nsuite/cron-jobs

echo "已卸载 elmsamaras（程序、cron 任务、执行器）"
echo "Samba 共享配置保留在 /etc/samba/smb.conf；计划任务表已移除"
echo ""
echo "如需彻底清除："
echo "  rm -rf /etc/nsuite /var/lib/nsuite   # 面板配置、账号、共享文件夹、备份"
echo "  rm -rf /srv/share /srv/media /srv/downloads /srv/jellyfin /srv/qbittorrent  # 数据目录"
echo "  sh uninstall.sh --purge-containers   # 卸载时顺带删除容器（已执行则跳过）"
