# Samara

把荒野无灯 N1「小钢炮」的 All-in-One 下载/NAS 体验，移植成 **Debian/Ubuntu 上可一键安装的套件**。界面与 **Nano Disk Manager (NDM)** 一比一：同样的 darkmatter 主题、同样的页面结构与导航。**应用统一用 Docker Compose 安装**，面板内置 Compose 部署、镜像加速、容器日志三项 Docker 管理能力。

## 快速开始

### 一条命令安装（OMV 同款风格）

任意 Debian 11 / 12 / 13、Ubuntu 22.04 / 24.04 机器，root 执行：

```sh
wget -O - https://raw.githubusercontent.com/ubui/Samara/main/install.sh | sudo bash
```

默认安装：**面板 + Docker（应用运行时）+ SMART**。不再预装任何应用——后期应用一律通过 **Status → Applications** 里的 Compose 部署自行安装（面板内置镜像加速与容器日志）。只要面板（不含 Docker）的话：

```sh
wget -O - https://raw.githubusercontent.com/ubui/Samara/main/install.sh | sudo bash -s -- --minimal
```

**重复执行即升级**，不会重置密码与配置。`raw.githubusercontent.com` 不通时改用 Release 通道：

```sh
curl -fsSL https://github.com/ubui/Samara/releases/latest/download/get.sh | sudo bash
```

### 一键卸载

```sh
wget -O - https://raw.githubusercontent.com/ubui/Samara/main/uninstall.sh | sudo bash
```

卸载程序、服务与计划任务；**配置和数据默认保留**（`/etc/samara`、`/var/lib/samara` 含 Compose 栈定义），结束时提示如何彻底清除。加 `--purge-containers` 可顺带下线 Compose 栈部署的应用容器。

### 其它用法

```sh
sudo ./install.sh --minimal                # 只装面板
sudo ./install.sh --minimal --port 8080    # 自定义面板端口
sudo ./install.sh --docker-mirror https://docker.m.daocloud.io   # 安装时顺带配好镜像加速
SAMARA_BRAND="我的NAS" sudo -E ./install.sh  # 自定义面板显示名
```

（`--full` / `--without-samba` 仍可传入，但已无作用——应用不再预装，仅为兼容保留。）

## 界面（与 NDM 同构）

| 菜单 | 页面 |
|---|---|
| **Status** | Overview（系统/资源/网络/存储）、Disks（挂载点 + SMART）、Network Traffic、System Log、Kernel Log、Processes、Failed Login、About、**Applications**（应用面板：控制面板 + 已装容器应用 + Compose 安装引导） |
| **System** | System（主机名/时区）、Administration（改口令/重启）、**Docker**（Status / Compose 部署 / 编排栈 / 镜像加速 / 容器+日志 / 镜像管理）、Startup（容器开机自启）、Scheduled Tasks（计划任务）、Tool（**语言切换** + df/free/ip/ss/lsblk） |

应用模型：**面板只保留 Dashboard（控制面板），其余应用全部用 Docker Compose 安装**，装好后自动出现在 Status → Applications 列表；带 Web 入口的容器自动出现在**顶栏右侧快捷方式**（运行中绿色、停止灰色），与原版行为一致。**中英文切换**在 System → Tool 里（单行 [中文][English] 双按钮）。

**System → Docker** 页内置三项能力（无需外部面板）：
- **Compose 部署**：粘贴 `docker-compose.yml` 一键 up -d，栈定义落盘 `/var/lib/samara/stacks/<name>/`
- **镜像加速**：维护 `/etc/docker/daemon.json` 的 `registry-mirrors`，保存即热加载（不中断容器）
- **容器日志**：容器列表一键查看 `docker logs` 输出

## 一条命令安装的内容

- `/opt/samara` — 面板程序与前端（darkmatter 主题）
- `/etc/samara` — 配置、账号、应用清单
- `samara.service` — systemd 服务（开机自启，监听 :8090）
- `/etc/profile.d/Samara-motd.sh` — 登录横幅（仿 fnOS，显示面板地址）
- Docker + docker-compose-v2 — 应用运行时（`--minimal` 不装）

## 目录结构

```
samara/
├── install.sh            一键安装（默认面板 + Docker + SMART）
├── get.sh                一键安装引导器
├── uninstall.sh          一键卸载
├── etc/apps.json         应用清单（默认空，装好容器应用自动收录）
├── samara/               Python 后端（标准库实现，无第三方依赖）
└── web/                  前端（NDM darkmatter 主题）
```

## 安全

- 口令 PBKDF2-HMAC-SHA256（12 万轮），`/etc/samara/passwd` 权限 600
- 登录失败限速锁定；会话存内存，重启即失效
- systemd 沙箱：`ProtectSystem=strict` + 白名单可写路径
- 备份导入路径穿越校验 + `tarfile` data filter

## 发布新版本

1. 改 `samara/__init__.py` 的 `__version__`
2. 打包：`install.sh` 末段自建 `dist/`，或用 `tools/` 下打包脚本
3. 建 Release，6 个资产名字必须一致，否则 `latest/download` 断链：

| 资产 | 用途 |
|---|---|
| `get.sh` | 一键安装入口 |
| `install.sh` | 安装脚本本体 |
| `uninstall.sh` | 一键卸载入口 |
| `samara.tar.gz` | **最新版固定别名** |
| `samara-<ver>.tar.gz` | 带版本号的源码包 |
| `samara-<ver>-install.sh` | 离线自解压单文件 |

装好的机器本身也是局域网分发源：`curl -fsSL http://<IP>:8090/get.sh | sudo bash`。

## 致谢

界面复刻 **Nano Disk Manager (NDM)**（HuangYeWuDeng，2018-2022），
主题 **darkmatter**（chrono，GPLv2）。除品牌名外，观感与页面结构与原版一致。

## License

MIT
