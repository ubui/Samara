#!/usr/bin/env python3
"""命令行发通知（供 cron 执行器等无面板上下文的地方调用）。

用法:
  notify_cli.py <标题> <正文> [事件]
  notify_cli.py --cron-job <任务id>      # 计划任务失败提醒
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nsuite import notify  # noqa: E402


def _cron_job(jid):
    name = jid
    try:
        with open("/etc/nsuite/cron.json", "r", encoding="utf-8") as f:
            for item in json.load(f):
                if item.get("id") == jid:
                    name = item.get("name") or jid
                    break
    except (OSError, ValueError):
        pass
    out = ""
    try:
        with open("/var/lib/nsuite/cron-status/%s.out" % jid, "r",
                  encoding="utf-8", errors="replace") as f:
            out = f.read()[-600:].strip()
    except OSError:
        pass
    body = "任务「%s」执行失败" % name
    if out:
        body += "\n\n最近输出：\n" + out
    return "计划任务失败", body


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--cron-job":
        if len(argv) < 2:
            return 2
        title, body = _cron_job(argv[1])
        event = "cron"
    else:
        title = argv[0]
        body = argv[1] if len(argv) > 1 else ""
        event = argv[2] if len(argv) > 2 else ""
    result = notify.send(title, body, level="error", event=event)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
