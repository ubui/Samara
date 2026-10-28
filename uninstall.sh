#!/bin/sh
# samara 卸载脚本：停服务、删程序文件。
# 配置与数据（/etc/samara、/var/lib/samara）默认保留，末尾提示如何彻底清除。
# 容器与 Docker 本身不受影响；--purge-containers 可一并下线 Compose 栈部署的应用。
set -e
[ "$(id -u)" = 0 ] || { echo "请用 root 运行"; exit 1; }

PURGE_CONTAINERS=0
[ "${1:-}" = "--purge-containers" ] && PURGE_CONTAINERS=1

if command -v systemctl >/dev/null 2>&1; then
  systemctl disable --now samara.service 2>/dev/null || true
  rm -f /etc/systemd/system/samara.service
  systemctl daemon-reload
fi

# 逐个下线 Compose 栈（应用统一用 docker compose 部署，栈定义在 /var/lib/samara/stacks）
if [ "$PURGE_CONTAINERS" = 1 ] && command -v docker >/dev/null 2>&1; then
  echo "[*] 下线 Compose 栈部署的应用..."
  for _yml in /var/lib/samara/stacks/*/docker-compose.yml; do
    [ -f "$_yml" ] || continue
    if docker compose version >/dev/null 2>&1; then
      docker compose -f "$_yml" down --remove-orphans 2>/dev/null || true
    elif command -v docker-compose >/dev/null 2>&1; then
      docker-compose -f "$_yml" down --remove-orphans 2>/dev/null || true
    fi
  done
fi

rm -rf /opt/samara
rm -f /etc/cron.d/samara /etc/cron.d/samara-backup
# 登录横幅：只删带本安装器标记的 motd 文件（品牌名可自定义，不能按固定名删）
for _f in /etc/profile.d/*-motd.sh; do
  [ -f "$_f" ] && grep -qs "install.sh 生成" "$_f" && rm -f "$_f"
done 2>/dev/null || true
rm -f /usr/local/sbin/samara-cron-run
rm -rf /etc/samara/cron-jobs

echo "已卸载 samara（程序、cron 任务、执行器）"
echo "Compose 栈定义保留在 /var/lib/samara/stacks"
echo ""
echo "如需彻底清除："
echo "  rm -rf /etc/samara /var/lib/samara   # 面板配置、账号、Compose 栈、备份"
echo "  sh uninstall.sh --purge-containers   # 卸载时顺带下线应用容器（已执行则跳过）"
