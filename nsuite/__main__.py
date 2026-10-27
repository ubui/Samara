"""nsuite 入口：python3 -m nsuite [--conf /etc/nsuite/nsuite.conf]"""

import argparse
import sys

from .config import Config
from .metrics import MetricsCollector
from .server import serve
from . import alerts
from . import __version__


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nsuite", description="Elmsamaras 面板（Debian NAS 套件）")
    ap.add_argument("--conf", default="/etc/nsuite/nsuite.conf", help="配置文件路径")
    ap.add_argument("--port", type=int, default=None, help="覆盖监听端口")
    ap.add_argument("--listen", default=None, help="覆盖监听地址")
    ap.add_argument("--version", action="store_true", help="打印版本")
    args = ap.parse_args(argv)

    if args.version:
        print("Elmsamaras %s" % __version__)
        return 0

    cfg = Config(args.conf)
    if args.port:
        cfg._cp.set("server", "port", str(args.port))
    if args.listen:
        cfg._cp.set("server", "listen", args.listen)

    collector = MetricsCollector()
    collector.start()
    _thread, stop_alerts = alerts.start_watcher()
    try:
        serve(cfg, collector)
    finally:
        stop_alerts.set()
        collector.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
