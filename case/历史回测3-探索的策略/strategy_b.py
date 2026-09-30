# -*- coding: utf-8 -*-
"""strategy_b.py — B 推荐档（目标日波动 3%）

用法：
    python strategy_b.py              # 正常：方向变化时才发邮件
    python strategy_b.py --dry-run    # 只算不发，打印结果
    python strategy_b.py --force      # 强制发邮件（即使无变化）
    python strategy_b.py --test       # 发一封测试邮件，验证配置
"""
import sys
import common

if __name__ == "__main__":
    sys.exit(common.main(sys.argv[1:], ["B"]))
