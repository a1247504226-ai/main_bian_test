# -*- coding: utf-8 -*-
"""测试网下单全流程自测：体检 -> 开多 -> 查仓 -> 平仓 -> 复核。

用法：
    python test_order.py              # 默认开多 150U 名义、2x
    python test_order.py short 120 2  # 开空 120U 名义、2x

只连测试网，不会碰真钱。
"""

import json
import os
import sys

from exchange import BinanceFutures
from trade import OrderLock          # 与 trade.py 共用同一把锁，避免测试与实盘撞单

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "trade.log")

# 仅作为兜底；正常应优先读 config.json 的 exchange 段
KEY = "riufKUh5kdZNF0eOiHo2eLRPVXOFlPAxbVIdePgK3e0EIbeOmFqGIaryTTT0u9fb"
SEC = "FG6lfFCmNnG0ieStXhQ9QTfaM3ix1Pb5tLksdwvnwq8aUVSinGuzyH6ID2gZJQpO"
SYMBOL = "BTCUSDT"


def load_credentials():
    """测试网专用 Key —— 绝不读 config.json 的 Key。

    [坑52] 本脚本强制连测试网（见 _run 里 testnet=True）。如果照旧从
    config 读 Key，那么用户把 config 切成主网配置（换上主网 Key）后，
    这里就会拿【主网 Key 去连测试网】——签名必然失败，测试完全失效。

    因此测试脚本的 Key 必须与其固定的测试网目标绑定，与 config 解耦。
    config 里专门放 testnet_api_key / testnet_api_secret 供这里读取；
    没有则用本脚本内置的测试网 Key 兜底。永远不去读 exchange.api_key
    （那是主网/实盘下单用的）。
    """
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
            e = json.load(f).get("exchange", {})
        tk = e.get("testnet_api_key") or e.get("testnet_key")
        ts = e.get("testnet_api_secret") or e.get("testnet_secret")
        if tk and ts:
            return tk, ts
    except Exception:                                # noqa: BLE001
        pass
    # 兜底：内置测试网 Key（与实盘 Key 严格分离）
    return KEY, SEC


def show(tag, obj):
    print(f"\n--- {tag} ---")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main():
    # [坑47] 测试脚本同样是下单入口，必须和 trade.py 共用一把锁
    lock = OrderLock()
    lock.acquire()
    try:
        return _run()
    finally:
        lock.release()


def _run():
    direction = sys.argv[1] if len(sys.argv) > 1 else "long"
    notional = float(sys.argv[2]) if len(sys.argv) > 2 else 150.0
    lev = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0

    key, sec = load_credentials()
    # [坑52] 显式写死 testnet=True：测试脚本无论 config 怎么改都只连测试网，
    # 绝不可能因为配置误改而连到主网。
    ex = BinanceFutures(key, sec, testnet=True, log_file=LOG_PATH)
    pre = ex.preflight(SYMBOL, leverage=lev)
    show("1) 下单前体检", pre)

    before = pre["position"]               # 复用体检结果，少发一次请求（省额度）
    if before["side"] != "flat":
        print(f"\n[注意] 已有持仓 {before['side']} {before['qty']}，先平掉再测")
        ex.close_position(SYMBOL)

    print(f"\n>>> 开仓 {direction} 名义 {notional}U / {lev:g}x")
    ok_open = ok_flat = False
    opened = False
    try:
        order = ex.open_position(SYMBOL, direction, notional, lev)
        opened = True
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
        print(f"    平仓校验: {'OK' if after['side'] == 'flat' else 'FAIL —— 仍有持仓！'}")

        bal = ex.balance()
        show("6) 平仓后余额", bal)
    finally:
        # [坑44] 开仓后任何一步失败（限流、异常、Ctrl+C）都不能留下裸仓。
        # 实测踩过：开仓成功但平仓被 -1003 打断，留了一个与策略相反的多单。
        try:
            left = ex.position(SYMBOL)
            if left["side"] != "flat":
                print(f"\n>>> [清理] 检测到遗留测试仓 {left['side']} {left['qty']}，强制平掉")
                ex.close_position(SYMBOL)
                left = ex.position(SYMBOL)
            ok_flat = left["side"] == "flat"
        except Exception as e:                       # noqa: BLE001
            ok_flat = False
            # [坑45] 区分「确实开过仓」和「开仓前就失败」：
            # 后者本就没有持仓，不该喊"请手动平仓"制造恐慌；
            # 但 POST 超时可能已成交，所以仍要提示手动确认。
            if opened:
                print(f"\n[严重] 清理失败：{e}")
                print("[严重] 请立刻手动执行 python trade.py close 平仓！")
            else:
                print(f"\n[提示] 开仓未完成（{e}），但无法自动确认持仓状态")
                print("[提示] 稳妥起见请手动执行 python trade.py status 确认无仓")

    print("\n==== 结论 ====")
    print(f"  开仓方向正确: {ok_open}")
    print(f"  平仓干净    : {ok_flat}")
    print("  全部通过 ✓" if (ok_open and ok_flat) else "  存在失败项 ✗")
    return 0 if (ok_open and ok_flat) else 1


if __name__ == "__main__":
    sys.exit(main())
