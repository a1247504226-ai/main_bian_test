# -*- coding: utf-8 -*-
"""run_all.py — 一次跑完 A/B/C 三档，合并成一封邮件（推荐每天只跑这个）

相比分别跑三个脚本，本脚本只拉一次K线数据，更快也更省请求。

用法：
    python run_all.py              # 正常：有任一档方向变化就发一封合并邮件
    python run_all.py --daily      # 每日日报：不管方向变没变，每天都发一封回执
    python run_all.py --dry-run    # 只算不发，打印结果
    python run_all.py --force      # 强制发邮件（即使无变化）
    python run_all.py --test       # 发一封测试邮件，验证配置
"""
import sys
import common

if __name__ == "__main__":
    sys.exit(common.main(sys.argv[1:], ["A", "B", "C"]))
