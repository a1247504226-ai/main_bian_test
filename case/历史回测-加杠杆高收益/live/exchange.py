# -*- coding: utf-8 -*-
"""币安 U 本位合约下单客户端（纯标准库，默认测试网）。

设计原则
--------
1. **默认只连测试网**。要连主网必须显式传 testnet=False 且 allow_mainnet=True，
   防止误把真钱当测试跑。
2. 所有"隐藏坑"都在这里统一处理，调用方不需要关心：
   [坑1] 本机时钟与服务器偏差 -> 用服务器时间校准 offset，并自动处理 -1021
   [坑2] 数量/价格精度 -> 按 stepSize / tickSize 用 Decimal 向下取整
   [坑3] 最小名义值 -> 下单前校验 MIN_NOTIONAL
   [坑4] 持仓模式（单向/双向）-> 自动探测；双向必须传 positionSide 且不能传 reduceOnly
   [坑5] 平仓必须 reduceOnly -> 否则会反手开出反向仓（最致命）
   [坑6] 杠杆不设就是默认值 -> 开仓前强制设置
   [坑7] 网络抖动 / SSL 握手超时 -> 指数退避重试
   [坑8] 超时重试导致重复下单 -> 每次带唯一 newClientOrderId
   [坑9] 市价单 avgPrice 可能为 0 -> 成交后回查订单/持仓确认真实均价
   [坑10] 保证金不足 -2019 -> 下单前用可用余额预估

用法
----
    from exchange import BinanceFutures
    ex = BinanceFutures(key, secret)          # 默认测试网
    ex.set_leverage("BTCUSDT", 2)
    ex.open_position("BTCUSDT", "long", notional_usdt=150, leverage=2)
    ex.close_position("BTCUSDT")
"""

import datetime
import hashlib
import hmac
import http.client
import json
import os
import random
import re
import shutil
import socket
import ssl
import string
import subprocess
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_DOWN, ROUND_UP

BASE_TESTNET = "https://testnet.binancefuture.com"
BASE_MAINNET = "https://fapi.binance.com"
CST = datetime.timezone(datetime.timedelta(hours=8))


def find_curl():
    """跨平台定位 curl：PATH 优先；Windows 再查 System32（部分环境 PATH 不含它）。"""
    p = shutil.which("curl")
    if p:
        return p
    if sys.platform == "win32":
        for c in (r"C:\Windows\System32\curl.exe", r"C:\Windows\SysWOW64\curl.exe"):
            if os.path.exists(c):
                return c
    return None


def resolve_ips(host, port=443):
    """解析出全部 IPv4 地址（去重、保序）。"""
    ips = []
    for fam, _, _, _, sa in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        if sa[0] not in ips:
            ips.append(sa[0])
    return ips


def pick_alive_ip(host, port=443, timeout=6.0, context=None):
    """[坑11] DNS 返回多个 IP，且**每个 IP 的可用性会波动**（实测同一域名
    三个 IP 里常常只有一两个能完成 TLS 握手，且每次不同）。

    因此：
      1. 必须做**完整 TLS 握手**才算可用（只测 TCP 会选到假活 IP）；
      2. **并行探测**，谁先握手成功就用谁（顺序探测会被超时 IP 拖死）。
    """
    ctx = context or ssl.create_default_context()
    ips = resolve_ips(host, port)
    if not ips:
        return None

    def _probe(ip):
        s = socket.create_connection((ip, port), timeout=timeout)
        ss = ctx.wrap_socket(s, server_hostname=host)
        ss.close()
        return ip

    pool = ThreadPoolExecutor(max_workers=len(ips))
    try:
        futs = [pool.submit(_probe, ip) for ip in ips]
        for f in as_completed(futs, timeout=timeout + 2):
            try:
                return f.result()
            except Exception:                           # noqa: BLE001
                continue
        return None
    except Exception:                                   # noqa: BLE001
        return None
    finally:
        pool.shutdown(wait=False)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """直连指定 IP，但 TLS SNI / Host 仍用域名（证书校验不受影响）。"""

    def __init__(self, host, ip, port=443, timeout=20, context=None):
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class BinanceError(Exception):
    def __init__(self, code, msg, raw=None):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.raw = raw


class BinanceFutures:
    def __init__(self, api_key, api_secret, testnet=True, allow_mainnet=False,
                 recv_window=10000, timeout=20, max_retries=3, verbose=True,
                 log_file=None):
        if not testnet and not allow_mainnet:
            raise RuntimeError(
                "拒绝连接主网：如需真实下单，请显式传 testnet=False, allow_mainnet=True")
        self.key = api_key
        self.secret = api_secret
        self.testnet = testnet
        self.base = BASE_TESTNET if testnet else BASE_MAINNET
        self.recv_window = recv_window
        self.timeout = timeout
        self.max_retries = max_retries
        self.verbose = verbose
        self.log_file = log_file      # 订单日志文件路径（None 则不落盘）
        self._offset_ms = 0          # server_time - local_time
        self._ip = None              # 当前选定的可用出口 IP
        self._info_cache = {}
        self._done = set()           # 本实例内已设置过的 (杠杆/保证金类型)，避免重复请求
        self._dual_side = None
        self._ctx = ssl.create_default_context()
        # [坑11] 本机到 testnet 的 DNS 会轮换到多个 CDN 节点，部分节点握手必挂。
        # curl 自带多 IP 重试（happy eyeballs），实测 3/3 稳定；Python 直连像抽奖。
        # 因此默认用 curl 传输，找不到 curl 才退回 http.client。
        self._curl = find_curl()
        # [坑12] 请求过密会被 -1003 封 IP（实盘时等于下不了单）。
        # 用最小请求间隔做节流；exchangeInfo 等静态数据已缓存，不会重复拉。
        self.min_interval = 0.35
        self._last_req_at = 0.0
        self.sync_time()

    def _throttle(self):
        gap = time.time() - self._last_req_at
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_req_at = time.time()

    def _ban_remaining(self, msg):
        """从 -1003 报错里解析「banned until」剩余秒数。"""
        m = re.search(r"banned until (\d+)", msg or "")
        if not m:
            return None
        return max(0.0, (int(m.group(1)) - self._ts()) / 1000.0)

    # ---------------------------------------------------------------- 基础设施
    def _log(self, msg, to_console=None):
        """写日志：**文件必写**，控制台是否显示由 to_console 决定。

        to_console=False 用于「出口 IP / 时间校准 / 持仓模式」这类只在排查时
        才需要的信息——落盘留痕，但不刷屏。
        """
        line = f"[{datetime.datetime.now(CST):%Y-%m-%d %H:%M:%S}] {msg}"
        show = self.verbose if to_console is None else to_console
        if show:
            print(line, flush=True)
        self._write_file(line)

    def _write_file(self, line):
        """追加到订单日志；超过 2MB 自动轮转一次，避免无限增长。"""
        if not self.log_file:
            return
        try:
            if os.path.exists(self.log_file) and os.path.getsize(self.log_file) > 2 * 1024 * 1024:
                os.replace(self.log_file, self.log_file + ".1")
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:                            # noqa: BLE001
            pass

    def _audit(self, action, symbol, **kv):
        """订单审计行：只落文件，不进控制台（避免刷屏）。"""
        parts = "  ".join(f"{k}={v}" for k, v in kv.items())
        self._log(f"{action:<6} {symbol:<10} {parts}", to_console=False)

    def sync_time(self):
        """[坑1] 用服务器时间校准本机时钟偏差（本机曾慢 12.8 秒）。"""
        best = None
        for _ in range(2):
            t0 = int(time.time() * 1000)
            try:
                # retry=True：初始化阶段网络抖动不能让程序直接起不来
                r = self._raw_request("GET", "/fapi/v1/time", {}, signed=False, retry=True)
            except BinanceError:
                continue
            t1 = int(time.time() * 1000)
            if not isinstance(r, dict) or "serverTime" not in r:
                continue
            # 用往返中点估计，抵消一半网络延迟
            local_mid = (t0 + t1) // 2
            off = r["serverTime"] - local_mid
            if best is None or abs(off) < abs(best):
                best = off
            time.sleep(0.2)
        if best is None:
            raise BinanceError(-1, "无法获取服务器时间，检查网络")
        self._offset_ms = best
        self._log(f"时间校准完成，本机偏移 {best:+d} ms", to_console=False)
        return best

    def _ts(self):
        return int(time.time() * 1000) + self._offset_ms

    def _sign(self, query):
        return hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _host(self):
        return urllib.parse.urlparse(self.base).netloc

    def _ensure_ip(self):
        """[坑11] 选定一个可连通的出口 IP；失败后下次会重新选。"""
        if not self._ip:
            self._ip = pick_alive_ip(self._host(), context=self._ctx)
            if not self._ip:
                raise BinanceError(-1, f"{self._host()} 的所有解析 IP 均不可达")
            self._log(f"出口 IP 选定 {self._ip}", to_console=False)
        return self._ip

    def _build(self, method, path, params, signed):
        """生成 (target, body, headers)。GET 用 query，POST 用 body。"""
        qs = urllib.parse.urlencode(params)
        if signed:
            qs = qs + "&signature=" + self._sign(qs)
        headers = {"X-MBX-APIKEY": self.key, "User-Agent": "Mozilla/5.0"}
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            return path, qs, headers
        return path + ("?" + qs if qs else ""), None, headers

    def _transport(self, method, target, body, headers):
        if self._curl:
            return self._via_curl(method, target, body, headers)
        return self._via_httpclient(method, target, body, headers)

    def _via_curl(self, method, target, body, headers):
        """用 curl 发请求：自带多 IP 重试，规避 Python 直连抽奖问题。"""
        cmd = [self._curl, "-s", "-S", "--max-time", str(self.timeout),
               "-w", "\n__HTTP__%{http_code}", "-X", method]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        if body is not None:
            cmd += ["--data-raw", body]
        cmd.append(self.base + target)
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=self.timeout + 10)
        except subprocess.TimeoutExpired:
            raise RuntimeError("curl 超时")
        out = p.stdout.decode(errors="replace")
        text, sep, code = out.rpartition("\n__HTTP__")
        if not sep:
            err = p.stderr.decode(errors="replace")[:200]
            raise RuntimeError(f"curl 无响应(exit={p.returncode}): {err}")
        return int(code.strip()), text

    def _via_httpclient(self, method, target, body, headers):
        """兜底：直连已探测可用的 IP。"""
        conn = _PinnedHTTPSConnection(self._host(), self._ensure_ip(),
                                      timeout=self.timeout, context=self._ctx)
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode(errors="replace")
        status = resp.status
        conn.close()
        return status, text

    def _raw_request(self, method, path, params, signed=False, retry=True):
        """[坑7][坑11] 网络失败自动换 IP 重试；[坑1] -1021 自动校时重试。"""
        params = dict(params or {})
        if signed:
            params["timestamp"] = self._ts()
            params["recvWindow"] = self.recv_window
        target, body, headers = self._build(method, path, params, signed)

        # [坑29] 下单请求网络异常时**绝不自动重试**：
        # POST 超时只代表"没收到响应"，订单可能已经在交易所成交了，
        # 盲目重试 = 双开仓位。宁可报错让人工核对，也不错下单。
        # 注意：-1021 / -1003 属于"明确被拒"，订单未创建，仍可安全重试。
        is_order_post = (method == "POST" and path == "/fapi/v1/order")

        attempts = self.max_retries if retry else 1
        last_err = None
        for i in range(attempts):
            try:
                self._throttle()
                status, text = self._transport(method, target, body, headers)

                if status == 200:
                    return json.loads(text)

                try:
                    j = json.loads(text)
                except Exception:
                    j = {"code": status, "msg": text[:200]}

                # -1021 时间戳超窗：重新校时后立刻重试
                # [坑30] 但校时接口自身不能触发重新校时，否则 sync_time 会自我递归
                if j.get("code") == -1021 and i == 0 and path != "/fapi/v1/time":
                    self._log("-1021 时间戳超窗，重新校时后重试")
                    self.sync_time()
                    params["timestamp"] = self._ts()
                    target, body, headers = self._build(method, path, params, signed)
                    continue

                # [坑12] -1003 限流：按报错里的 banned until 精确等待后重试
                if j.get("code") == -1003:
                    wait_s = self._ban_remaining(j.get("msg", ""))
                    if wait_s is not None and wait_s <= 120:
                        self._log(f"IP 被限流，等待 {wait_s:.0f}s 解封")
                        time.sleep(wait_s + 1.0)
                        continue
                    hint = f"（约 {wait_s:.0f}s 后解封）" if wait_s else ""
                    raise BinanceError(-1003, f"请求被限流{hint}: {j.get('msg')}")
                # 其余业务类 4xx 不重试，直接抛
                if status < 500:
                    raise BinanceError(j.get("code"), j.get("msg"), text)
                last_err = BinanceError(j.get("code"), j.get("msg"), text)
            except BinanceError:
                raise
            except Exception as e:                      # noqa: BLE001  网络/SSL/超时
                self._ip = None                         # 强制下一轮换 IP
                last_err = BinanceError(-1, f"{type(e).__name__}: {e}")
                if is_order_post:
                    # 订单请求网络异常：可能已成交，不重试，直接抛
                    raise last_err
            if i < attempts - 1:
                wait = 1.5 * (2 ** i)
                self._log(f"请求失败({last_err})，{wait:.1f}s 后重试 {i+1}/{attempts-1}")
                time.sleep(wait)
        raise last_err

    def _request(self, method, path, params=None, signed=False):
        return self._raw_request(method, path, params or {}, signed=signed, retry=True)

    # ---------------------------------------------------------------- 合约参数
    @staticmethod
    def normalize_symbol(symbol):
        """[坑24] 统一大写、去空格。交易所对大小写敏感，
        配置里写成 "btcusdt" 会直接查不到合约。"""
        s = str(symbol or "").strip().upper()
        if not s:
            raise BinanceError(-1, "symbol 不能为空")
        return s

    def symbol_info(self, symbol):
        """[坑13] exchangeInfo 响应近 1MB，每次拉都吃带宽和权重。
        缓存到系统临时目录（24 小时内复用），显著降低被 -1003 的概率。"""
        if symbol not in self._info_cache:
            info = None
            cache = os.path.join(tempfile.gettempdir(), "bnfut_exchange_info.json")
            try:
                if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400:
                    with open(cache, encoding="utf-8") as f:
                        info = json.load(f)
            except Exception:                       # noqa: BLE001
                info = None
            if info is None:
                info = self._request("GET", "/fapi/v1/exchangeInfo")
                try:
                    with open(cache, "w", encoding="utf-8") as f:
                        json.dump(info, f)
                except Exception:                   # noqa: BLE001
                    pass
            for s in info["symbols"]:
                if s["symbol"] == symbol:
                    self._info_cache[symbol] = s
                    break
            else:
                raise BinanceError(-1, f"找不到交易对 {symbol}")
        return self._info_cache[symbol]

    def filters(self, symbol):
        s = self.symbol_info(symbol)
        out = {}
        for f in s["filters"]:
            out[f["filterType"]] = f
        return out

    def round_qty(self, symbol, qty):
        """[坑2] 数量按 stepSize 向下取整，用 Decimal 避免浮点误差。"""
        step = Decimal(self.filters(symbol)["LOT_SIZE"]["stepSize"])
        q = (Decimal(str(qty)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return q.quantize(step)

    def round_price(self, symbol, price):
        """[坑2] 价格按 tickSize 向下取整。"""
        tick = Decimal(self.filters(symbol)["PRICE_FILTER"]["tickSize"])
        p = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        return p.quantize(tick)

    def min_notional(self, symbol):
        f = self.filters(symbol).get("MIN_NOTIONAL")
        return Decimal(f["notional"]) if f else Decimal("5")

    # ---------------------------------------------------------------- 账户
    def dual_side(self):
        """[坑4] 双向持仓模式下必须传 positionSide，且不能传 reduceOnly。"""
        if self._dual_side is None:
            r = self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
            self._dual_side = bool(r.get("dualSidePosition"))
            self._log(f"持仓模式：{'双向(hedge)' if self._dual_side else '单向(one-way)'}", to_console=False)
        return self._dual_side

    def balance(self, asset="USDT"):
        rows = self._request("GET", "/fapi/v2/balance", signed=True)
        for r in rows:
            if r["asset"] == asset:
                return {
                    "asset": asset,
                    "balance": float(r["balance"]),
                    "available": float(r["availableBalance"]),
                    "unrealized": float(r["crossUnPnl"]),
                }
        return {"asset": asset, "balance": 0.0, "available": 0.0, "unrealized": 0.0}

    def set_leverage(self, symbol, leverage):
        """[坑6] 不设置就用默认杠杆（可能 20x），风险失控。

        [坑15] 同一实例内只设置一次：sync/反手会连续调用，重复发请求会加剧 -1003。
        """
        key = ("lev", symbol, int(leverage))
        if key in self._done:
            return {"leverage": int(leverage), "cached": True}
        r = self._request("POST", "/fapi/v1/leverage",
                          {"symbol": symbol, "leverage": int(leverage)}, signed=True)
        self._done.add(key)
        self._log(f"{symbol} 杠杆设为 {r.get('leverage')}x", to_console=False)
        return r

    def set_margin_type(self, symbol, margin_type="CROSSED"):
        """已设置过会报 -4046，属正常，忽略。同一实例内只发一次。"""
        key = ("margin", symbol, margin_type)
        if key in self._done:
            return {"msg": "cached"}
        try:
            r = self._request("POST", "/fapi/v1/marginType",
                              {"symbol": symbol, "marginType": margin_type}, signed=True)
        except BinanceError as e:
            if e.code == -4046:                     # 已经是该类型
                self._done.add(key)
                return {"msg": "保证金类型未变化"}
            raise
        self._done.add(key)
        return r

    def position(self, symbol):
        """返回净持仓：{'side': 'long'/'short'/'flat', 'qty': float, 'entry': float, 'upnl': float}"""
        # 注意：不是 /fapi/v2/position（不存在，会 -5000），正确端点是 positionRisk
        rows = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True)
        for r in rows:
            if r["symbol"] != symbol:
                continue
            amt = float(r["positionAmt"])
            if abs(amt) < 1e-12:
                continue
            return {
                "side": "long" if amt > 0 else "short",
                "qty": abs(amt),
                "entry": float(r["entryPrice"]),
                "upnl": float(r["unRealizedProfit"]),
                "leverage": int(float(r.get("leverage", 0))),
                "margin_type": r.get("marginType"),
                "raw": r,
            }
        return {"side": "flat", "qty": 0.0, "entry": 0.0, "upnl": 0.0,
                "leverage": 0, "margin_type": None, "raw": None}

    # ---------------------------------------------------------------- 下单
    @staticmethod
    def _client_id(prefix="st"):
        """[坑8] 唯一 clientOrderId：超时重试也不会重复下单（交易所侧去重）。"""
        rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
        return f"{prefix}{int(time.time()*1000)%10**10}{rnd}"[:36]

    def market_order(self, symbol, side, qty, reduce_only=False,
                     position_side=None, client_id=None, require_full=True):
        """市价单。side: BUY/SELL；qty: 正数数量（已按精度取整）。

        require_full=True：成交不足 95% 视为异常并抛错（开仓用，宁可报错）。
        require_full=False：允许部分成交（平仓用，外层会循环把残余平掉）。
        """
        symbol = self.normalize_symbol(symbol)

        # [坑22] side 必须严格校验：写成 "buy"/"sell"/"LONG" 都会让交易所拒单，
        # 但更怕的是"猜"——所以不做任何猜测，非法值直接报错。
        side = str(side).strip().upper()
        if side not in ("BUY", "SELL"):
            raise BinanceError(-1, f"side 必须是 BUY / SELL，收到 {side!r}")

        req_qty = Decimal(str(qty))
        if req_qty <= 0:
            raise BinanceError(-1, f"下单数量必须大于 0，收到 {qty!r}")

        dual = self.dual_side()
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": format(req_qty, "f"),
            "newClientOrderId": client_id or self._client_id(),
        }
        if dual:
            # [坑4] 双向模式：必须指定 positionSide；[坑5] 且不能带 reduceOnly（会 -4104）
            # [坑17] **绝不能靠 side 反推 positionSide**：平多时 side=SELL，
            # 反推会得到 SHORT —— 那不是平多，而是在开一个新的空仓（持仓翻倍反向）。
            if position_side:
                params["positionSide"] = position_side
            elif reduce_only:
                raise BinanceError(
                    -1, "双向持仓模式平仓必须显式指定 position_side，"
                        "否则会被当作开反向仓（平多→开空）")
            else:
                params["positionSide"] = "LONG" if side == "BUY" else "SHORT"
        elif reduce_only:
            # [坑5] 单向模式平仓必须 reduceOnly，否则反向开仓
            params["reduceOnly"] = "true"

        r = self._request("POST", "/fapi/v1/order", params, signed=True)
        r = self._confirm_fill(symbol, r)

        # [坑23] 部分成交检测：市价单在流动性差时可能只成交一部分。
        # 开仓宁可报错让人工核对；平仓允许部分成交（外层会循环清尾）。
        if require_full:
            filled = Decimal(str(r.get("executedQty") or 0))
            if filled < req_qty * Decimal("0.95"):
                raise BinanceError(
                    -1, f"部分成交：请求 {req_qty}，实际成交 {filled}。"
                        f"请立即核对持仓，勿盲目重试")
        return r

    def _confirm_fill(self, symbol, order):
        """[坑9] 市价单返回的 avgPrice 有时是 0，回查一次订单确认真实成交均价。

        [坑20] 被拒/过期/撤销的订单必须抛错——否则会返回一个 executedQty=0、
        avgPrice=0 的"成功"回执，上层误以为成交，实际是空仓。
        """
        status = str(order.get("status") or "").upper()
        if status in ("REJECTED", "EXPIRED", "CANCELED"):
            raise BinanceError(-1, f"订单未成交（{status}）：{order.get('msg', '')}")
        if status == "FILLED" and float(order.get("executedQty") or 0) <= 0:
            raise BinanceError(-1, "订单状态为 FILLED 但成交量为 0，数据异常")
        if float(order.get("avgPrice") or 0) > 0:
            return order
        oid = order.get("orderId")
        for _ in range(3):
            time.sleep(0.4)
            try:
                q = self._request("GET", "/fapi/v1/order",
                                  {"symbol": symbol, "orderId": oid}, signed=True)
                if float(q.get("avgPrice") or 0) > 0:
                    return q
            except BinanceError:
                break
        return order

    def open_position(self, symbol, direction, notional_usdt, leverage,
                      price_hint=None, margin_type="CROSSED", require_flat=True):
        """按「名义价值」开仓。direction: long/short。

        安全原则：**任何一项校验不过就不下单**——宁可不下单，也不错下单。

        [坑3]  数量必须满足 MIN_NOTIONAL，否则 -4164
        [坑10] 先估算保证金，不足直接报错，避免 -2019
        [坑25] 已有持仓时拒绝开仓，避免"以为反手、实际加仓"
        [坑26] 校验 minQty / maxQty
        [坑27] 下单后以「实际持仓」为准复核，不信任订单回执
        """
        symbol = self.normalize_symbol(symbol)

        # ---------------- 参数合法性 ----------------
        direction = str(direction).strip().lower()
        if direction not in ("long", "short"):
            raise BinanceError(-1, f"direction 必须是 long / short，收到 {direction!r}")
        try:
            notional_usdt = float(notional_usdt)
            leverage = int(leverage)
        except (TypeError, ValueError):
            raise BinanceError(
                -1, f"notional_usdt / leverage 必须是数字，收到 {notional_usdt!r} / {leverage!r}")
        if notional_usdt <= 0:
            raise BinanceError(-1, f"notional_usdt 必须大于 0，收到 {notional_usdt}")
        if not 1 <= leverage <= 125:
            raise BinanceError(-1, f"杠杆必须在 1~125 之间，收到 {leverage}")
        if not symbol.endswith("USDT"):
            self._log(f"注意：{symbol} 不是 USDT 本位合约，请确认交易对")

        # ---------------- [坑25] 已有持仓则拒绝 ----------------
        if require_flat:
            pos = self.position(symbol)
            if pos["side"] != "flat":
                raise BinanceError(
                    -1, f"{symbol} 已有 {pos['side']} 持仓 {pos['qty']}，"
                        f"拒绝叠加开仓（先平仓，或用 trade.py sync）")

        # 先探测持仓模式，让日志顺序更自然（否则会出现在下单那一刻）
        self.dual_side()
        self.set_margin_type(symbol, margin_type)
        self.set_leverage(symbol, leverage)

        # 参考价（优先调用方给的，否则用最新成交价）
        if price_hint is None:
            t = self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
            price_hint = float(t["price"])
        if not price_hint or price_hint <= 0:
            raise BinanceError(-1, f"参考价异常：{price_hint!r}，拒绝下单")

        lot = self.filters(symbol)["LOT_SIZE"]
        step = Decimal(lot["stepSize"])
        min_qty = Decimal(lot["minQty"])
        max_qty = Decimal(lot["maxQty"])
        min_notional = self.min_notional(symbol)

        qty = self.round_qty(symbol, Decimal(str(notional_usdt)) / Decimal(str(price_hint)))

        # [坑14] 取整后名义可能跌破 MIN_NOTIONAL，自动补一档
        need_qty = (min_notional / Decimal(str(price_hint))).quantize(step, rounding=ROUND_UP)
        if qty < need_qty:
            self._log(f"名义不足，数量由 {qty} 上调至 {need_qty}（满足最小 {min_notional}U）")
            qty = need_qty

        # ---------------- [坑26] 数量上下限 ----------------
        if qty <= 0:
            raise BinanceError(-1, f"数量取整后为 0（notional={notional_usdt}, price={price_hint}）")
        if qty < min_qty:
            raise BinanceError(-1, f"数量 {qty} 低于最小下单量 {min_qty}")
        if qty > max_qty:
            raise BinanceError(-1, f"数量 {qty} 超过最大下单量 {max_qty}，请调小 notional_usdt")

        notional = float(qty) * price_hint
        need_margin = notional / leverage
        bal = self.balance()
        if bal["available"] < need_margin * 1.02:
            raise BinanceError(
                -2019, f"可用余额 {bal['available']:.2f} < 需要保证金 {need_margin:.2f}")

        side = "BUY" if direction == "long" else "SELL"
        self._log(f"开{direction} {symbol} qty={qty} 名义≈{notional:.2f}U "
                  f"保证金≈{need_margin:.2f}U 杠杆{leverage}x")
        try:
            order = self.market_order(
                symbol, side, qty,
                position_side=("LONG" if direction == "long" else "SHORT"))
        except BinanceError as e:
            self._audit("FAIL", symbol, act=f"open-{direction}", qty=str(qty),
                        notional=f"{notional:.2f}", err=str(e)[:100])
            raise

        # ---------------- [坑27] 以实际持仓复核 ----------------
        after = self.position(symbol)
        if after["side"] != direction:
            self._audit("FAIL", symbol, act="verify-dir", expect=direction,
                        actual=after["side"], order=order.get("orderId"))
            raise BinanceError(
                -1, f"下单后复核失败：期望 {direction}，实际 {after['side']}，请立即人工检查")
        if Decimal(str(after["qty"])) < qty * Decimal("0.95"):
            self._audit("FAIL", symbol, act="verify-qty", expect=str(qty),
                        actual=after["qty"], order=order.get("orderId"))
            raise BinanceError(
                -1, f"下单后持仓不足：期望 {qty}，实际 {after['qty']}")

        # 成功审计（只落文件）
        self._audit("OPEN", symbol,
                    side=side, qty=str(qty), notional=f"{notional:.2f}",
                    lev=f"{leverage}x", margin=f"{need_margin:.2f}",
                    order=order.get("orderId"), status=order.get("status"),
                    avg=f"{float(order.get('avgPrice') or 0):.2f}",
                    filled=order.get("executedQty"),
                    pos=f"{after['side']} {after['qty']}", avail=f"{bal['available']:.2f}")
        return order

    def close_position(self, symbol, max_rounds=2):
        """[坑5] 平掉当前持仓。单向用 reduceOnly；双向用反向 side + positionSide。

        [坑28] 平仓后必须复核持仓：部分成交或数量取整误差都可能留下残余，
        残余仓位等于反向暴露，必须循环清干净；清不干净就报错让人工处理。
        """
        symbol = self.normalize_symbol(symbol)
        self.dual_side()                     # 提前探测，日志顺序更自然
        order = None
        for i in range(max_rounds):
            pos = self.position(symbol)
            if pos["side"] == "flat":
                return None if i == 0 else order

            qty = self.round_qty(symbol, pos["qty"])
            if qty <= 0:
                raise BinanceError(
                    -1, f"持仓 {pos['qty']} 小于最小步长，无法用市价单平仓，请人工处理")

            side = "SELL" if pos["side"] == "long" else "BUY"
            self._log(f"平仓 {pos['side']} {symbol} qty={qty}")
            try:
                order = self.market_order(
                    symbol, side, qty,
                    reduce_only=True,
                    position_side=("LONG" if pos["side"] == "long" else "SHORT"),
                    require_full=False)
            except BinanceError as e:
                self._audit("FAIL", symbol, act="close", side=pos["side"],
                            qty=str(qty), err=str(e)[:100])
                raise

            after = self.position(symbol)
            if after["side"] == "flat":
                self._audit("CLOSE", symbol, side=pos["side"], qty=str(qty),
                            order=order.get("orderId"), status=order.get("status"),
                            avg=f"{float(order.get('avgPrice') or 0):.2f}",
                            filled=order.get("executedQty"), pos="flat")
                return order
            self._log(f"平仓后仍有残余 {after['qty']}，重试 {i + 1}/{max_rounds}")

        pos = self.position(symbol)
        raise BinanceError(-1, f"平仓未彻底，仍有 {pos['side']} {pos['qty']}，请人工处理")

    # ---------------------------------------------------------------- 风控自检
    def preflight(self, symbol="BTCUSDT", leverage=2.0):
        """下单前自检，返回一份体检报告。"""
        report = {"testnet": self.testnet, "base": self.base}
        report["time_offset_ms"] = self._offset_ms
        report["dual_side"] = self.dual_side()
        report["balance"] = self.balance()
        report["position"] = self.position(symbol)
        f = self.filters(symbol)
        report["filters"] = {
            "stepSize": f["LOT_SIZE"]["stepSize"],
            "tickSize": f["PRICE_FILTER"]["tickSize"],
            "minNotional": f["MIN_NOTIONAL"]["notional"],
        }
        try:
            t = self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})
            report["price"] = float(t["price"])
        except BinanceError:
            report["price"] = None
        if report["price"]:
            min_qty = self.round_qty(symbol, Decimal(f["MIN_NOTIONAL"]["notional"]) / Decimal(str(report["price"])))
            report["min_qty_for_notional"] = str(min_qty)
            report["margin_for_min_notional"] = round(
                float(min_qty) * report["price"] / leverage, 2)
        return report
