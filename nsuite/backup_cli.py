#!/usr/bin/env python3
"""自动备份的 cron 入口：python3 backup_cli.py --auto

单独成文件是因为 cron 环境没有 package import 路径，
这里手动把安装目录塞进 sys.path。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nsuite import backup  # noqa: E402


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--auto" not in argv:
        print("用法: backup_cli.py --auto")
        return 2
    ok, msg = backup.run_auto()
    print(("OK " if ok else "FAIL ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
