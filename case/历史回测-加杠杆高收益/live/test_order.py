# -*- coding: utf-8 -*-
"""测试网下单全流程自测：体检 -> 开多 -> 查仓 -> 平仓 -> 复核。

用法：
    python test_order.py              # 默认开多 150U 名义、2x
    python test_order.py short 120 2  # 开空 120U 名义、2x

只连测试网，不会碰真钱。
"""

import json
import sys

from exchange import BinanceFutures

KEY = "riufKUh5kdZNF0eOiHo2eLRPVXOFlPAxbVIdePgK3e0EIbeOmFqGIaryTTT0u9fb"
SEC = "FG6lfFCmNnG0ieStXhQ9QTfaM3ix1Pb5tLksdwvnwq8aUVSinGuzyH6ID2gZJQpO"
SYMBOL = "BTCUSDT"


def show(tag, obj):
    print(f"\n--- {tag} ---")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main():
    direction = sys.argv[1] if len(sys.argv) > 1 else "long"
    notional = float(sys.argv[2]) if len(sys.argv) > 2 else 150.0
    lev = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0

    ex = BinanceFutures(KEY, SEC)          # 默认测试网
    pre = ex.preflight(SYMBOL, leverage=lev)
    show("1) 下单前体检", pre)

    before = pre["position"]               # 复用体检结果，少发一次请求（省额度）
    if before["side"] != "flat":
        print(f"\n[注意] 已有持仓 {before['side']} {before['qty']}，先平掉再测")
        ex.close_position(SYMBOL)

    print(f"\n>>> 开仓 {direction} 名义 {notional}U / {lev:g}x")
    order = ex.open_position(SYMBOL, direction, notional, lev)
    show("2) 开仓回执", {
        "orderId": order.get("orderId"),
        "status": order.get("status"),
        "side": order.get("side"),
        "executedQty": order.get("executedQty"),
        "avgPrice": order.get("avgPrice"),
    })

    pos = ex.position(SYMBOL)
    show("3) 开仓后持仓", {k: v for k, v in pos.items() if k != "raw"})
    ok_open = pos["side"] == direction
    print(f"    方向校验: {'OK' if ok_open else 'FAIL'} (期望 {direction}, 实际 {pos['side']})")

    print("\n>>> 平仓")
    close_order = ex.close_position(SYMBOL)
    if close_order:
        show("4) 平仓回执", {
            "orderId": close_order.get("orderId"),
            "status": close_order.get("status"),
            "side": close_order.get("side"),
            "executedQty": close_order.get("executedQty"),
            "avgPrice": close_order.get("avgPrice"),
        })

    after = ex.position(SYMBOL)
    show("5) 平仓后持仓", {k: v for k, v in after.items() if k != "raw"})
    ok_flat = after["side"] == "flat"
    print(f"    平仓校验: {'OK' if ok_flat else 'FAIL —— 仍有持仓！'}")

    bal = ex.balance()
    show("6) 平仓后余额", bal)

    print("\n==== 结论 ====")
    print(f"  开仓方向正确: {ok_open}")
    print(f"  平仓干净    : {ok_flat}")
    print("  全部通过 ✓" if (ok_open and ok_flat) else "  存在失败项 ✗")
    return 0 if (ok_open and ok_flat) else 1


if __name__ == "__main__":
    sys.exit(main())
