#!/usr/bin/env python3
"""
save-snapshot.py — PreCompact Hook (Python版)

在上下文压缩前确认可以继续。当前为空操作框架。

安装: 在 .claude/settings.json 中添加
  "PreCompact": [{
    "matcher": "",
    "hooks": [{
      "type": "command",
      "command": "python .claude/hooks/save-snapshot.py"
    }]
  }]
"""

import sys
import json


def main():
    # PreCompact hook 只需确认即可
    _output({})


def _output(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
