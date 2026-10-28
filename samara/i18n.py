"""中英文切换。

页面是服务端渲染，翻译就用「渲染完按词典整体替换」：不需要前端框架，
也不会动到代码/属性之外的东西。产品名（Samba/Docker/Portainer 等）不译，
英文键按长度从长到短替换 + 单词边界，避免 "Port" 误伤 "Portainer"。
"""

import re
import threading

_local = threading.local()

# EN -> ZH（只列界面上真实出现的字符串；产品名不进表）
DICT = {
    # 导航
    "System": "系统",
    "Apps": "应用",
    "Overview": "总览",
    "Disks": "磁盘",
    "Network Traffic": "网络流量",
    "System Log": "系统日志",
    "Kernel Log": "内核日志",
    "Processes": "进程",
    "Failed Login": "登录失败",
    "About": "关于",
    "Dashboard": "控制面板",
    "Administration": "管理",
    "Startup": "启动项",
    "Scheduled Tasks": "计划任务",
    "Tool": "工具",
    "Logout": "退出登录",
    "Other": "其他",
    # 登录页
    "Login - ": "登录 - ",
    "Username": "用户名",
    "Password": "密码",
    "Login": "登录",
    # 总览
    "Resources": "资源",
    "Storage": "存储",
    "Hostname": "主机名",
    "Model": "型号",
    "Operating System": "操作系统",
    "Kernel Version": "内核版本",
    "Local Time": "本地时间",
    "Uptime": "运行时长",
    "Load Average": "平均负载",
    "CPU Usage": "CPU 使用率",
    "cores": "核",
    "Memory": "内存",
    "Swap": "交换分区",
    "Temperature": "温度",
    "N/A": "无",
    "Interface": "网卡",
    "Address": "地址",
    "RX Rate": "下行速率",
    "TX Rate": "上行速率",
    "RX Total": "下行总量",
    "TX Total": "上行总量",
    "Filesystem": "文件系统",
    "Mount Point": "挂载点",
    "Used": "已用",
    "Size": "容量",
    "Usage": "使用率",
    "No mounts": "暂无挂载点",
    # 磁盘
    "Mount Points": "挂载点",
    "Type": "类型",
    "Health (SMART)": "健康状态（SMART）",
    "Health": "健康状态",
    "Power On": "通电时长",
    "SMART not available (install smartmontools)": "SMART 不可用（请安装 smartmontools）",
    "No mount points": "暂无挂载点",
    # 流量
    "Live rates, refreshed every 3 seconds.": "实时速率，每 3 秒自动刷新。",
    "Interfaces": "网卡列表",
    # 日志/进程/失败登录
    "Refresh": "刷新",
    "Top 40 by CPU": "按 CPU 排序前 40",
    "User": "用户",
    "PID": "PID",
    "MEM%": "内存%",
    "VSZ": "虚拟内存",
    "Command": "命令",
    "Recent failed SSH logins (last 30).": "最近的 SSH 登录失败记录（30 条）。",
    "Failed Login Attempts": "失败的登录尝试",
    "Message": "内容",
    "No failed logins recorded": "暂无失败登录记录",
    # 关于
    "Version": "版本",
    "Python": "Python",
    "Source": "源码",
    "Theme": "主题",
    "Credits": "致谢",
    "Interface rebuilt to match ": "界面复刻 ",
    " - darkmatter theme by chrono (GPLv2), original panel by HuangYeWuDeng.":
        " —— darkmatter 主题 by chrono（GPLv2），原版面板 by HuangYeWuDeng。",
    # 通用（服务卡/表单）
    "Service": "服务",
    "Status": "状态",
    "Port": "端口",
    "Web UI": "Web 界面",
    "Start": "启动",
    "Stop": "停止",
    "Restart": "重启",
    "Log": "日志",
    "no log available": "暂无日志",
    "Not Found": "未找到",
    "running": "运行中",
    "stopped": "已停止",
    "not installed": "未安装",
    "Brand": "品牌",
    "Listen": "监听",
    "HTTP Port": "HTTP 端口",
    "Session TTL": "会话有效期",
    "Config File": "配置文件",
    "Password File": "口令文件",
    "Web Root": "前端目录",
    "Edit /etc/samara/samara.conf then restart samara.service":
        "编辑 /etc/samara/samara.conf 后重启 samara.service 生效",
    "Access": "访问",
    "Actions": "操作",
    "Restart Panel": "重启面板",
    "Sign Out All Sessions": "登出全部会话",
    "Samba is not installed. ": "未检测到 Samba，请先安装（",
    "apt install samba": "apt install samba",
    "Shares": "共享",
    "Name": "名称",
    "Path": "路径",
    "Comment": "备注",
    "Browseable": "可浏览",
    "Writable": "可写",
    "Add Share": "添加共享",
    "Users": "用户",
    "Enabled": "已启用",
    "Add User": "添加用户",
    "Exports": "导出表",
    "Save": "保存",
    "Save & Apply": "保存并应用",
    "General": "常规",
    "Timezone": "时区",
    "System Time": "系统时间",
    "New Password": "新口令",
    "Confirm": "确认口令",
    "Change Password": "修改口令",
    "Reboot": "重启主机",
    "Host will go down for a moment.": "主机将短暂离线重启。",
    "Services started automatically at boot.": "开机自动启动的服务。",
    "Services": "服务列表",
    "Unit": "单元",
    "Autostart": "开机自启",
    "Enable": "启用",
    "Disable": "禁用",
    "Tasks": "任务",
    "No tasks": "暂无任务",
    "Add Task": "添加任务",
    "Schedule": "执行周期",
    "minute hour day month weekday": "分 时 日 月 周",
    "Run": "运行",
    "Delete": "删除",
    "Disk Usage": "磁盘占用",
    "Listening Ports": "监听端口",
    "Block Devices": "块设备",
    # Docker 页
    "Containers": "容器",
    "Images": "镜像",
    "Image": "镜像",
    "Deploy": "部署",
    "Deploy with Compose": "Compose 部署",
    "Stacks": "编排栈",
    "Stack Name": "栈名称",
    "No stacks": "暂无编排栈",
    "Image Mirrors": "镜像加速",
    "Registry Mirrors": "Registry 镜像",
    "Pull": "拉取",
    "Logs": "日志",
    "Open": "打开",
    "Ports": "端口",
    "Repository": "仓库",
    "Tag": "标签",
    "Created": "创建时间",
    "Remove": "删除",
    "No containers": "暂无容器",
    "No images": "暂无镜像",
    "No data": "暂无数据",
    "Total": "合计",
    # 应用面板
    "Applications": "应用",
    "Application": "应用",
    "Install": "安装",
    # 壁纸
    "Wallpaper": "壁纸",
    "Save Wallpaper": "保存壁纸",
    # 顶栏
    "Loading...": "加载中...",
    "ENABLE AUTOSTART": "启用自启",
    "DISABLE AUTOSTART": "禁用自启",
    "enabled": "已启用",
    "disabled": "已禁用",
    "Start/stop services and control autostart at boot.": "启动/停止服务，并控制开机自启。",
    # Tool 语言块
    "Language": "语言",
    "Interface Language": "界面语言",
}

_PAT_CACHE = {k: re.compile(r"(?<![A-Za-z])" + re.escape(k) + r"(?![A-Za-z])") for k in DICT}


def set_lang(lang):
    _local.lang = "zh" if lang == "zh" else "en"


def get_lang():
    return getattr(_local, "lang", "zh")


def lang_label():
    """顶栏切换按钮：点它切到另一种语言。"""
    return "EN" if get_lang() == "zh" else "中文"


def translate(html):
    if get_lang() != "zh":
        return html
    for k in sorted(DICT, key=len, reverse=True):
        html = _PAT_CACHE[k].sub(lambda _m, _k=k: DICT[_k], html)
    return html
