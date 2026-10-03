# -*- coding: utf-8 -*-
"""strategy_c.py — C 激进档（目标日波动 5%）

用法：
    python strategy_c.py              # 正常：方向变化时才发邮件
    python strategy_c.py --dry-run    # 只算不发，打印结果
    python strategy_c.py --force      # 强制发邮件（即使无变化）
    python strategy_c.py --test       # 发一封测试邮件，验证配置
"""
import sys
import common

if __name__ == "__main__":
    sys.exit(common.main(sys.argv[1:], ["C"]))
