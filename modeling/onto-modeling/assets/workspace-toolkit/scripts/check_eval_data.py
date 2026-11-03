#!/usr/bin/env python3
"""已更名为 table_profile.py（表登记：schema 表格资料的表头、取值画像与三方对账）。

旧名保留一个版本做转发，命令行参数与函数都和 table_profile.py 相同，下个版本删除。
评测移出后（ADR-0020）不再读 .ontology-state 下的上传表，登记的数据表只有 schema 资料。
"""

from __future__ import annotations

import sys

from table_profile import *  # noqa: F401,F403  旧名 import 的调用方照常拿到同一组函数
from table_profile import main

if __name__ == "__main__":
    print("提示：check_eval_data.py 已更名为 table_profile.py，旧名下个版本删除", file=sys.stderr)
    sys.exit(main())
