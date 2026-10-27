"""nsuite 配置读取：/etc/nsuite/nsuite.conf"""

import configparser
import os

DEFAULT_CONF = """# elmsamaras - Debian NAS 套件（内部代号 nsuite）
[server]
listen = 0.0.0.0
port = 8090

[auth]
session_ttl = 86400

[paths]
prefix = /opt/nsuite
password_file = /etc/nsuite/passwd
apps_file = /etc/nsuite/apps.json
web_root = /opt/nsuite/web

[branding]
title = Elmsamaras
subtitle = Debian Edition

# 仅供本地调试/嵌入看板：跳过登录。默认关闭，且仅监听回环地址时生效。
[dev]
allow_anonymous = false
"""


class Config:
    def __init__(self, path="/etc/nsuite/nsuite.conf"):
        self.path = path
        cp = configparser.ConfigParser()
        cp.read_string(DEFAULT_CONF)
        if os.path.exists(path):
            cp.read(path)
        self._cp = cp

    def get(self, section, key, fallback=""):
        return self._cp.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=0):
        try:
            return self._cp.getint(section, key, fallback=fallback)
        except ValueError:
            return fallback

    @property
    def listen(self):
        return self.get("server", "listen", "0.0.0.0")

    @property
    def port(self):
        return self.getint("server", "port", 8090)

    @property
    def prefix(self):
        return self.get("paths", "prefix", "/opt/nsuite")
