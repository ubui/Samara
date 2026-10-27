#!/usr/bin/env python3
"""nsuite 账号管理 CLI。

用法:
  auth_cli.py <passwd文件> <用户> <密码>          # 设置/重置密码
  auth_cli.py <passwd文件> --del <用户>           # 删除用户
  auth_cli.py <passwd文件> --list                 # 列出用户
"""

import os
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from auth import hash_password, verify  # noqa: E402


def _write(path, lines):
    """写入口令文件并收紧权限（0644 会让同机任何用户读到哈希）。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    path = argv[0]
    args = argv[1:]

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f]
    except OSError:
        lines = ["# nsuite 登录账号: <user>:$pbkdf2$<迭代>$<salt>$<hash>"]

    if args[0] == "--list":
        for l in lines:
            if l and not l.startswith("#"):
                print(l.split(":", 1)[0])
        return 0

    if args[0] == "--del":
        if len(args) < 2:
            print("缺少用户名")
            return 1
        user = args[1]
        keep = [l for l in lines if not (l and not l.startswith("#")
                                         and l.split(":", 1)[0] == user)]
        _write(path, keep)
        print(f"已删除 {user}")
        return 0

    if len(args) < 2:
        print(__doc__)
        return 1
    user, password = args[0], args[1]

    out, replaced = [], False
    for l in lines:
        if l and not l.startswith("#") and l.split(":", 1)[0] == user:
            out.append(f"{user}:{hash_password(password)}")
            replaced = True
        else:
            out.append(l)
    if not replaced:
        out.append(f"{user}:{hash_password(password)}")

    _write(path, out)

    ok = verify(path, user, password)
    print(f"{'已更新' if replaced else '已创建'} {user}，校验{'通过' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
