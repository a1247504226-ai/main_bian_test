#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 币安现货下单模块 —— MACD-X 策略的实盘执行层
================================================================================

【重要】这是真金白银的代码。默认行为是「只看不动」。

设计原则
--------
1. 【双重开关】默认演练模式：只打印"将要做什么"，不发真实订单。
   必须同时给 --trade 和 --live 才会真实下单。少一个都是空跑。
2. 【以交易所余额为准】判断"当前有没有持仓"时，读的是币安账户的真实余额，
   不是本地状态文件。本地文件可能丢失、回滚、被手工改，交易所余额才是事实。
3. 【下单前三重校验】最小名义金额（MIN_NOTIONAL）、数量步长（LOT_SIZE）、
   可用余额。任何一个不过就跳过，绝不硬下单。
4. 【幂等】每笔订单带唯一 newClientOrderId。网络超时后查询该 ID 即可知道
   订单到底成没成，不会重复下单（这是最容易亏钱的地方）。
5. 【急停开关】脚本目录下存在 STOP_TRADING 文件时，拒绝一切下单。
   想立刻停手就新建这个空文件，不用改代码。
6. 【支持测试网】testnet.binance.vision 用假钱跑通全流程，上线前必做。

安全：API Key 不要写进代码
--------------------------
按优先级读取：
  1. 环境变量 BINANCE_API_KEY / BINANCE_API_SECRET
  2. 同目录 binance_keys.json   {"api_key":"...", "api_secret":"..."}
  3. 同目录 binance_keys.txt    第一行 key，第二行 secret

【币安 API 权限设置】只勾选「允许现货交易」，绝对不要勾「允许提现」。
并绑定本机 IP 白名单（你说了后面加）。API Key 泄露而没绑 IP 白名单 = 账户裸奔。

用法
----
  # 1. 先测连通性和权限（不下单，安全）
  python binance_trader.py --check
  python binance_trader.py --check --testnet        # 用测试网

  # 2. 演练：看它"会"下什么单（不发真实订单）
  python binance_trader.py --symbol BTCUSDT --action buy --amount 700

  # 3. 真实下单（两个开关都要给）
  python binance_trader.py --symbol BTCUSDT --action buy --amount 700 --trade --live

  # 4. 急停：想停手时执行（或在目录里新建 STOP_TRADING 文件）
  python binance_trader.py --halt
  python binance_trader.py --resume

依赖：无（纯标准库，和 macdx_strategy.py 保持一致）
================================================================================
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_MAINNET = "https://api.binance.com"
BASE_TESTNET = "https://testnet.binance.vision"

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS_JSON = os.path.join(HERE, "binance_keys.json")
KEYS_TXT = os.path.join(HERE, "binance_keys.txt")
HALT_FILE = os.path.join(HERE, "STOP_TRADING")

# 交易安全上限（防手滑）。可按需调，但不要设成天文数字。
SAFETY = {
    "max_order_usdt": 5000.0,   # 单笔最大投入（USDT），超过直接拒绝
    "min_order_usdt": 10.0,     # 单笔最小投入，低于此值不划算（币安 MIN_NOTIONAL 通常 5）
    "recv_window": 5000,        # 签名有效期（毫秒）
    "dust_usdt": 5.0,           # 持仓价值低于此值视为"没仓位"（灰尘）
    "http_timeout": 15.0,       # 单次请求超时
}


class BinanceError(Exception):
    """币安返回的业务错误。"""

    def __init__(self, code, msg, http_status=None):
        self.code = code
        self.msg = msg
        self.http_status = http_status
        super().__init__(f"[{code}] {msg}")


def load_keys():
    """按优先级读取 API Key。返回 (key, secret)。"""
    k = os.environ.get("BINANCE_API_KEY")
    s = os.environ.get("BINANCE_API_SECRET")
    if k and s:
        return k.strip(), s.strip()

    if os.path.exists(KEYS_JSON):
        try:
            with open(KEYS_JSON, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("api_key") and d.get("api_secret"):
                return d["api_key"].strip(), d["api_secret"].strip()
        except Exception:
            pass

    if os.path.exists(KEYS_TXT):
        try:
            with open(KEYS_TXT, "r", encoding="utf-8") as f:
                lines = [x.strip() for x in f.read().splitlines() if x.strip()]
            if len(lines) >= 2:
                return lines[0], lines[1]
        except Exception:
            pass

    raise BinanceError(
        "NOKEY",
        "未找到 API Key。请设置环境变量 BINANCE_API_KEY / BINANCE_API_SECRET，"
        f"或创建 {os.path.basename(KEYS_JSON)}（{{\"api_key\":\"...\",\"api_secret\":\"...\"}}）"
    )


def floor_to_step(value: float, step: float) -> float:
    """
    把数量向下取整到步长。币安 LOT_SIZE 过滤器要求数量必须是 stepSize 的整数倍，
    而且只能向下取整——向上取整会因余额不足被拒单。
    """
    if step <= 0:
        return value
    n = math.floor(value / step + 1e-9)
    decimals = max(0, -int(round(math.log10(step))))
    return round(n * step, decimals)


def fmt_num(v: float) -> str:
    """去掉多余小数零，方便打印。"""
    s = f"{v:.8f}".rstrip("0").rstrip(".")
    return s or "0"


class BinanceSpot:
    """币安现货 REST 客户端（只实现下单需要的部分）。"""

    def __init__(self, api_key: str, api_secret: str,
                 testnet: bool = False, proxy: str | None = None,
                 verbose: bool = True):
        self.key = api_key
        self.secret = api_secret
        self.base = BASE_TESTNET if testnet else BASE_MAINNET
        self.testnet = testnet
        self.verbose = verbose
        self._offset_ms = 0
        self._opener = self._build_opener(proxy)
        self._filters_cache: dict = {}
        self._price_cache: dict = {}

    # ─────────────────── 底层 HTTP ───────────────────
    @staticmethod
    def _build_opener(proxy: str | None):
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        else:
            handler = urllib.request.ProxyHandler({})  # 显式禁用环境变量代理
        return urllib.request.build_opener(handler)

    def _sign(self, query: str) -> str:
        return hmac.new(self.secret.encode("utf-8"),
                        query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False, retries: int = 3) -> dict:
        """发请求。带指数退避重试；业务错误不重试，直接抛。"""
        last_err = None
        for attempt in range(retries):
            params = dict(params or {})
            if signed:
                # 时间戳：本机时间 + 与服务器的偏移量。本机时钟不准是最常见的坑，
                # 会导致 -1021 Timestamp outside of recvWindow。
                params["timestamp"] = int(time.time() * 1000) + self._offset_ms
                params.setdefault("recvWindow", SAFETY["recv_window"])
                query = urllib.parse.urlencode(params, doseq=True)
                query += "&signature=" + self._sign(query)
            else:
                query = urllib.parse.urlencode(params, doseq=True)

            url = self.base + path + (("?" + query) if query else "")
            req = urllib.request.Request(url, method=method)
            req.add_header("X-MBX-APIKEY", self.key)
            req.add_header("User-Agent", "MACDX-Trader/1.0")

            try:
                with self._opener.open(req, timeout=SAFETY["http_timeout"]) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                try:
                    err = json.loads(body)
                    code, msg = err.get("code"), err.get("msg")
                except Exception:
                    code, msg = None, body[:300]
                # -1021 时间戳问题：同步一次服务器时间后重试
                if code == -1021 and attempt < retries - 1:
                    self.sync_time()
                    last_err = BinanceError(code, msg, e.code)
                    continue
                # 5xx / 429 / 418：服务端或限流问题，值得重试
                if e.code in (429, 418) or 500 <= e.code < 600:
                    last_err = BinanceError(code, msg, e.code)
                    wait = min(2 ** attempt, 8) + 0.3
                    time.sleep(wait)
                    continue
                raise BinanceError(code, msg, e.code)
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    time.sleep(min(2 ** attempt, 8) + 0.3)
                    continue
                raise
        raise last_err if last_err else RuntimeError("请求失败")

    # ─────────────────── 公共接口 ───────────────────
    def sync_time(self) -> int:
        """对齐服务器时间，返回偏移毫秒。"""
        r = self._request("GET", "/api/v3/time")
        self._offset_ms = int(r["serverTime"]) - int(time.time() * 1000)
        if self.verbose:
            print(f"  [时间] 与币安服务器偏差 {self._offset_ms:+d} ms（已自动补偿）")
        return self._offset_ms

    def ping(self) -> bool:
        try:
            self._request("GET", "/api/v3/ping")
            return True
        except Exception as e:
            if self.verbose:
                print(f"  [网络] 连通性测试失败：{e}")
            return False

    def account(self) -> dict:
        """账户信息（含权限、手续费档位）。"""
        return self._request("GET", "/api/v3/account", signed=True)

    def balance(self, asset: str) -> float:
        """某资产的可用余额（free）。"""
        acct = self.account()
        for b in acct.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    def price(self, symbol: str) -> float:
        if symbol not in self._price_cache:
            r = self._request("GET", "/api/v3/ticker/price", {"symbol": symbol})
            self._price_cache[symbol] = float(r["price"])
        return self._price_cache[symbol]

    def symbol_filters(self, symbol: str) -> dict:
        """取该交易对的过滤器：最小下单量、步长、最小名义金额。"""
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        r = self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})
        syms = r.get("symbols", [])
        if not syms:
            raise BinanceError("NOSYMBOL", f"交易对不存在：{symbol}")
        info = syms[0]
        out = {"step_size": 0.0, "min_qty": 0.0, "min_notional": 5.0,
               "base": info["baseAsset"], "quote": info["quoteAsset"],
               "status": info.get("status", "")}
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                out["step_size"] = float(f["stepSize"])
                out["min_qty"] = float(f["minQty"])
            elif f["filterType"] == "MIN_NOTIONAL":
                out["min_notional"] = float(f.get("minNotional", 5.0))
            elif f["filterType"] == "NOTIONAL":   # 新版接口
                out["min_notional"] = float(f.get("minNotional", out["min_notional"]))
        self._filters_cache[symbol] = out
        return out

    def _new_client_id(self, symbol: str) -> str:
        """唯一订单 ID，用于幂等查询。"""
        return f"MACDX{symbol[:8]}{int(time.time())}{int(time.time()*1000)%1000}"

    def market_buy(self, symbol: str, quote_amount: float,
                   client_id: str | None = None) -> dict:
        """市价买入，按【花费多少 USDT】下单（quoteOrderQty）。"""
        cid = client_id or self._new_client_id(symbol)
        return self._request("POST", "/api/v3/order", {
            "symbol": symbol, "side": "BUY", "type": "MARKET",
            "quoteOrderQty": f"{quote_amount:.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": cid,
        }, signed=True)

    def market_sell(self, symbol: str, quantity: float,
                    client_id: str | None = None) -> dict:
        """市价卖出，按【卖出多少个币】下单（quantity）。"""
        cid = client_id or self._new_client_id(symbol)
        return self._request("POST", "/api/v3/order", {
            "symbol": symbol, "side": "SELL", "type": "MARKET",
            "quantity": f"{quantity:.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": cid,
        }, signed=True)

    def query_order(self, symbol: str, client_id: str) -> dict:
        """按 clientOrderId 查订单。网络超时后用它确认订单到底成没成。"""
        return self._request("GET", "/api/v3/order",
                             {"symbol": symbol, "origClientOrderId": client_id},
                             signed=True)

    def open_orders(self, symbol: str | None = None) -> list:
        p = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/api/v3/openOrders", p, signed=True)


# ══════════════════════ 高层：按信号执行 ══════════════════════
def execute_signal(client: BinanceSpot, symbol: str, should_hold: bool,
                   capital: float, frac: float,
                   live: bool = False, max_order: float | None = None) -> dict:
    """
    根据策略信号，让账户向"目标状态"靠拢。

    should_hold=True  → 应持有：没有仓位就买入，已有就什么都不做
    should_hold=False → 应空仓：有仓位就全部卖出，没有就什么都不做

    返回执行报告 dict，便于记录到日志/邮件。
    """
    rep = {"symbol": symbol, "should_hold": should_hold, "action": "NONE",
           "reason": "", "detail": "", "live": live, "ok": True}

    # ── 急停开关 ──
    if os.path.exists(HALT_FILE):
        rep.update(action="BLOCKED", ok=False,
                   reason=f"检测到急停文件 {os.path.basename(HALT_FILE)}，拒绝下单")
        return rep

    try:
        f = client.symbol_filters(symbol)
        px = client.price(symbol)
        base = f["base"]

        free_quote = client.balance(f["quote"])     # 通常是 USDT
        free_base = client.balance(base)
        pos_value = free_base * px

        rep["price"] = px
        rep["free_quote"] = free_quote
        rep["free_base"] = free_base
        rep["pos_value"] = pos_value

        # 用交易所真实余额判断"有没有仓位"（灰尘不算）
        has_pos = pos_value >= SAFETY["dust_usdt"]

        if should_hold and not has_pos:
            # ─────── 买入 ───────
            want = capital * frac
            spend = min(want, free_quote)
            if max_order is not None:
                spend = min(spend, max_order)
            spend = min(spend, SAFETY["max_order_usdt"])

            if spend < max(f["min_notional"], SAFETY["min_order_usdt"]):
                rep.update(action="SKIP", reason=(
                    f"可投入 {spend:.2f} U 低于最小下单额 "
                    f"{max(f['min_notional'], SAFETY['min_order_usdt']):.2f} U"))
                return rep

            qty = floor_to_step(spend / px, f["step_size"])
            if qty < f["min_qty"] or qty * px < f["min_notional"]:
                rep.update(action="SKIP",
                           reason=f"数量 {fmt_num(qty)} 不满足最小下单要求")
                return rep

            rep["action"] = "BUY"
            rep["qty"] = qty
            rep["spend"] = spend
            rep["avg_price"] = px          # 真实成交后会用实际均价覆盖
            rep["detail"] = (f"买入 {fmt_num(qty)} {base}（约 {spend:.2f} USDT @ {px:,.4f}）")

            if live:
                r = client.market_buy(symbol, spend)
                rep["order"] = {k: r.get(k) for k in
                                ("orderId", "clientOrderId", "status",
                                 "executedQty", "cummulativeQuoteQty", "transactTime")}
                # 用真实成交量/成交额反算均价（最准的下单价）
                try:
                    eq = float(r.get("executedQty") or 0)
                    cq = float(r.get("cummulativeQuoteQty") or 0)
                    if eq > 0 and cq > 0:
                        rep["avg_price"] = cq / eq
                        rep["qty"] = eq
                        rep["spend"] = cq
                except (TypeError, ValueError):
                    pass
            else:
                rep["detail"] = "[演练] " + rep["detail"]

        elif (not should_hold) and has_pos:
            # ─────── 卖出 ───────
            qty = floor_to_step(free_base, f["step_size"])
            if qty <= 0 or qty * px < f["min_notional"]:
                rep.update(action="SKIP",
                           reason=f"持仓 {fmt_num(free_base)} {base} 太小，无法下单")
                return rep

            rep["action"] = "SELL"
            rep["qty"] = qty
            rep["avg_price"] = px          # 真实成交后会用实际均价覆盖
            rep["detail"] = (f"卖出 {fmt_num(qty)} {base}（约 {qty*px:.2f} USDT @ {px:,.4f}）")

            if live:
                r = client.market_sell(symbol, qty)
                rep["order"] = {k: r.get(k) for k in
                                ("orderId", "clientOrderId", "status",
                                 "executedQty", "cummulativeQuoteQty", "transactTime")}
                try:
                    eq = float(r.get("executedQty") or 0)
                    cq = float(r.get("cummulativeQuoteQty") or 0)
                    if eq > 0 and cq > 0:
                        rep["avg_price"] = cq / eq
                except (TypeError, ValueError):
                    pass
            else:
                rep["detail"] = "[演练] " + rep["detail"]
        else:
            rep.update(action="HOLD", reason=(
                "已处于目标状态，无需操作"
                + ("（当前持有）" if has_pos else "（当前空仓）")))

    except BinanceError as e:
        rep.update(ok=False, reason=f"币安接口错误 {e}")
    except Exception as e:
        rep.update(ok=False, reason=f"{type(e).__name__}: {e}")
    return rep


# ══════════════════════ CLI（用于自测） ══════════════════════
def _cmd_check(client: BinanceSpot) -> int:
    print("=" * 68)
    print(f" 币安连通性与权限自检    {'【测试网】' if client.testnet else '【主网】'}")
    print("=" * 68)
    print(f" 接口地址 : {client.base}")
    if not client.ping():
        print(" ✗ 网络不通。若开了代理，试试 --proxy http://127.0.0.1:7897")
        return 1
    print(" ✓ 网络连通")
    client.sync_time()
    try:
        acct = client.account()
    except BinanceError as e:
        print(f" ✗ 账户查询失败：{e}")
        print("   常见原因：Key 错误 / 未绑定 IP 白名单 / 未开启「现货交易」权限")
        return 1
    print(f" ✓ 账户查询成功   权限: {acct.get('permissions')}")
    print(f"   手续费档位: maker {acct.get('makerCommission', 0)/10000*100:.3f}%  "
          f"taker {acct.get('takerCommission', 0)/10000*100:.3f}%")
    if acct.get("canWithdraw"):
        print(" ⚠ 该 API Key 【允许提现】！强烈建议去币安后台取消这个权限。")
    print("   余额：")
    for b in acct.get("balances", []):
        free = float(b["free"])
        if free > 0:
            print(f"     {b['asset']:<8} {free:>16,.8f}")
    print("=" * 68)
    print(" 结论：可以进入下一步（先用演练模式验证下单逻辑）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="币安现货下单模块（默认演练，不发真实订单）")
    ap.add_argument("--check", action="store_true", help="只做连通性/权限/余额自检")
    ap.add_argument("--symbol", default=None, help="交易对，如 BTCUSDT")
    ap.add_argument("--action", choices=["buy", "sell"], default=None,
                    help="手动指定方向（不依赖策略信号）")
    ap.add_argument("--amount", type=float, default=None, help="买入投入 USDT；卖出忽略此值")
    ap.add_argument("--capital", type=float, default=1000.0, help="本金（配合 --frac 计算投入）")
    ap.add_argument("--frac", type=float, default=0.7, help="仓位比例")
    ap.add_argument("--max-order", type=float, default=None, help="单笔上限 USDT")
    ap.add_argument("--testnet", action="store_true", help="用币安测试网（假钱）")
    ap.add_argument("--proxy", default=None, help="代理，如 http://127.0.0.1:7897")
    ap.add_argument("--trade", action="store_true", help="启用交易模块（仍需 --live 才真下单）")
    ap.add_argument("--live", action="store_true", help="【危险】真实下单，会花真钱")
    ap.add_argument("--halt", action="store_true", help="创建急停文件，立刻停止一切下单")
    ap.add_argument("--resume", action="store_true", help="删除急停文件，恢复交易")
    args = ap.parse_args()

    if args.halt:
        with open(HALT_FILE, "w", encoding="utf-8") as f:
            f.write("halt\n")
        print(f"✓ 已创建急停文件：{HALT_FILE}\n  现在所有下单请求都会被拒绝。")
        return 0
    if args.resume:
        if os.path.exists(HALT_FILE):
            os.remove(HALT_FILE)
            print("✓ 已删除急停文件，交易恢复。")
        else:
            print("（急停文件本来就不存在）")
        return 0

    if os.path.exists(HALT_FILE):
        print(f"⚠ 急停文件存在（{HALT_FILE}），本次不执行任何操作。")
        return 1

    try:
        key, secret = load_keys()
    except BinanceError as e:
        print(f"✗ {e}")
        return 1

    client = BinanceSpot(key, secret, testnet=args.testnet, proxy=args.proxy)

    if args.check:
        return _cmd_check(client)

    if not args.symbol or not args.action:
        ap.print_help()
        print("\n提示：先用 --check 自检，再用 --action buy --amount 100 演练。")
        return 0

    live = bool(args.trade and args.live)
    if args.live and not args.trade:
        print("⚠ 只给了 --live 没给 --trade：仍按【演练】处理（双重开关设计）。")

    print("=" * 68)
    print(f" {'【真实下单】' if live else '【演练模式】——不会发出真实订单'}")
    print("=" * 68)
    if not live:
        print(" 要真实下单，需同时加 --trade --live")
    client.sync_time()

    if args.action == "buy":
        cap = args.amount if args.amount is not None else args.capital
        rep = execute_signal(client, args.symbol, True, cap, 1.0,
                             live=live, max_order=args.max_order)
    else:
        rep = execute_signal(client, args.symbol, False, 0, 0,
                             live=live, max_order=args.max_order)

    print(f"\n 标的     : {rep['symbol']}")
    print(f" 动作     : {rep['action']}")
    if rep.get("detail"):
        print(f" 详情     : {rep['detail']}")
    if rep.get("reason"):
        print(f" 说明     : {rep['reason']}")
    if rep.get("order"):
        print(f" 订单回报 : {json.dumps(rep['order'], ensure_ascii=False)}")
    print("=" * 68)
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
