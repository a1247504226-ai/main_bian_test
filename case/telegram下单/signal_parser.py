# -*- coding: utf-8 -*-
"""
Telegram 策略信号解析器（修复版）
================================

输出结构与 signal_executor.py 的 handle_signal() 完全对齐：

    {
        "type":   "OPEN" | "SET_TPSL" | "TP" | "CLOSE" | "CANCEL_ADD",
        "symbol": "BTCUSDT" | None,      # 不带币对时为 None，交给执行器走锚点逻辑
        "side":   "LONG" | "SHORT" | None,
        "add":    [float, ...],          # 补仓价
        "tp":     [float, ...],          # 止盈价
        "sl":     [float, ...],          # 止损价
        "tp_level": 1 | 2 | 3 | None,
    }

相比原版的 4 处修复
-------------------
1. 【致命】一条消息里「止盈 / 止损 / 补仓」三个冒号字段统一抽取后**合并**返回。
   原版是「谁先命中谁 return」，于是同一条消息里的止损、补仓被静默丢弃：
   真实日志里 72 条消息的止损、42 条消息的补仓凭空消失 → 止损裸奔。

2. 【致命】营销战绩播报**前置过滤**。
   原版会把「TP1  50x  +467.91%」「内部群多单TP2止盈拿下」当成真信号，
   直接去平掉真实仓位。真实日志里 20 条战绩播报被误判成 TP。

3. 【严重】补齐 CLOSE（离场/平仓/出局/清仓）与 CANCEL_ADD（出补仓）两类真实信号，
   与执行器已支持的类型对齐。原版这两类共 34 条全部被丢弃。

4. 【一般】币对合法性校验 + 兼容「NEAR/USDT」写法；排除「插破低点出局即可」
   这类行情建议，避免把分析文当成平仓指令。
"""

import re


class SignalParser:

    # ---- 开仓：  #ETH 现价空 ---------------------------------------------
    OPEN_RE = re.compile(
        r"#\s*(?P<symbol>[A-Za-z0-9]{1,20})\s*"
        r"(?:现价|市价)\s*"
        r"(?P<side>多|空)",
        re.I
    )

    # ---- 币对：优先 #BTC，回退 BTC/USDT ----------------------------------
    SYMBOL_RE = re.compile(r"#\s*(?P<symbol>[A-Za-z0-9]{2,20})", re.I)
    SLASH_SYMBOL_RE = re.compile(r"\b(?P<base>[A-Z0-9]{2,15})\s*/\s*USDT\b", re.I)

    # ---- 冒号字段：止盈 / 止损 / 补仓 ------------------------------------
    TP_RE = re.compile(r"^\s*止盈\s*[:：]\s*(?P<value>.+?)\s*$", re.I | re.M)
    SL_RE = re.compile(r"^\s*止损\s*[:：]\s*(?P<value>.+?)\s*$", re.I | re.M)
    ADD_RE = re.compile(r"^\s*补仓\s*[:：]\s*(?P<value>.+?)\s*$", re.I | re.M)

    # ---- 止盈事件：TP1 / TP2️⃣ / 手动TP3 ---------------------------------
    TP_EVENT_RE = re.compile(
        r"(?:手动\s*)?TP\s*(?P<level>[123])(?:️⃣)?(?:\s*止盈)?",
        re.I
    )

    NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")

    # ---- 营销 / 战绩播报特征：命中即判为噪音，绝不当成交易信号 ------------
    # 「50x」只有紧跟百分比（战绩格式 50x +467.91%）时才算噪音；
    # 真实信号里的「杠杆：30X-50X」不能误杀，所以要求 x 后面必须跟 %。
    MARKETING_RE = re.compile(
        r"\d+\s*[xX]\s*[+\-]?\s*\d+(?:\.\d+)?\s*%"   # 50x +467.91%
        r"|\+\s*\d+(?:\.\d+)?\s*%"                    # +467.91%
        r"|收米|内部群|社区VIP|战绩|盈利反馈|上电视|止盈第\s*\d|跟单",
    )

    # ---- 真实平仓 / 撤补仓 ------------------------------------------------
    CLOSE_RE = re.compile(r"离场|平仓|出局|清仓|全平")
    # 「插破低点出局即可」「可以等等回调」是行情建议，不是平仓指令
    CLOSE_NOISE_RE = re.compile(r"可以等等|给机会|可以尝试|即可尝试|像要(?:做多|做空)")
    CANCEL_ADD_RE = re.compile(r"出补仓|取消补仓|撤补仓|撤销补仓")

    # ==================================================================
    @classmethod
    def numbers(cls, text):
        """从文本里抽取数字，兼容 Telegram 的逗号小数点写法（0,108）。"""
        if not text:
            return []
        out = []
        for x in cls.NUMBER_RE.findall(text):
            try:
                out.append(float(x.replace(",", ".")))
            except ValueError:
                pass
        return out

    @classmethod
    def _norm_symbol(cls, raw):
        """校验并规范化币对：只允许 [A-Z0-9]{1,20}（#G 也是合法币对），
        不以 USDT 结尾则补后缀；非法字符一律返回 None。"""
        if not raw:
            return None
        s = str(raw).upper()
        if not re.fullmatch(r"[A-Z0-9]{1,20}", s):
            return None
        if not s.endswith("USDT"):
            s += "USDT"
        return s

    @classmethod
    def symbol(cls, text):
        text = text or ""
        m = cls.SYMBOL_RE.search(text)
        if m:
            return m.group("symbol").upper()
        m = cls.SLASH_SYMBOL_RE.search(text)      # 回退：XXX/USDT
        if m:
            return m.group("base").upper() + "USDT"
        return None

    # ==================================================================
    @classmethod
    def parse_signal(cls, text):
        if not text:
            return None
        text = text.strip()

        # 1) OPEN ------------------------------------------------------
        #    若同一条消息里还带了「止盈/止损/补仓」，一并带上，
        #    执行器会直接用它开仓（比默认 ±7% 更准）；没有则留空走默认逻辑。
        m = cls.OPEN_RE.search(text)
        if m:
            sym = cls._norm_symbol(m.group("symbol"))
            if not sym:
                return None
            side = "LONG" if m.group("side") == "多" else "SHORT"
            m_tp, m_sl, m_add = (cls.TP_RE.search(text), cls.SL_RE.search(text),
                                 cls.ADD_RE.search(text))
            return {"type": "OPEN", "symbol": sym, "side": side,
                    "add": cls.numbers(m_add.group("value")) if m_add else [],
                    "tp": cls.numbers(m_tp.group("value")) if m_tp else [],
                    "sl": cls.numbers(m_sl.group("value")) if m_sl else [],
                    "tp_level": None}

        # 2) 营销 / 战绩播报统一前置过滤 -------------------------------
        #    这类消息里常夹带「TP1」「离场」「成本离场」「补仓」等词，
        #    若不在最前面拦掉，会被下面的 CLOSE / TP 规则误判成真信号。
        if cls.MARKETING_RE.search(text):
            return None

        # 3) 冒号格式：三个字段一次性抽完再合并（关键修复） --------------
        m_tp, m_sl, m_add = cls.TP_RE.search(text), cls.SL_RE.search(text), cls.ADD_RE.search(text)
        tp = cls.numbers(m_tp.group("value")) if m_tp else []
        sl = cls.numbers(m_sl.group("value")) if m_sl else []
        add = cls.numbers(m_add.group("value")) if m_add else []
        if tp or sl or add:
            return {"type": "SET_TPSL", "symbol": cls._norm_symbol(cls.symbol(text)),
                    "side": None, "add": add, "tp": tp, "sl": sl, "tp_level": None}

        # 4) 离场 / 平仓（排除「出局即可」这类行情建议） -----------------
        if cls.CLOSE_RE.search(text) and not cls.CLOSE_NOISE_RE.search(text):
            return {"type": "CLOSE", "symbol": cls._norm_symbol(cls.symbol(text)),
                    "side": None, "add": [], "tp": [], "sl": [], "tp_level": None}

        # 5) 出补仓（撤销补仓单） ----------------------------------------
        if cls.CANCEL_ADD_RE.search(text):
            return {"type": "CANCEL_ADD", "symbol": cls._norm_symbol(cls.symbol(text)),
                    "side": None, "add": [], "tp": [], "sl": [], "tp_level": None}

        # 6) TP 事件（营销已在第 2 步拦掉） ------------------------------
        m = cls.TP_EVENT_RE.search(text)
        if m:
            return {"type": "TP", "symbol": cls._norm_symbol(cls.symbol(text)),
                    "side": None, "add": [], "tp": [], "sl": [],
                    "tp_level": int(m.group("level"))}

        return None


# ======================================================================
# 自检：覆盖全部修复点，直接运行即可验证
# ======================================================================
if __name__ == "__main__":
    P = SignalParser.parse_signal

    # 1a) 开仓消息自带止盈止损补仓，价位不能丢
    s = P("#ETH 现价空\n\n止盈：1.2255 1.295 1.35\n\n止损：1\n\n补仓：0.9")
    assert s["type"] == "OPEN" and s["symbol"] == "ETHUSDT" and s["side"] == "SHORT", s
    assert s["tp"] == [1.2255, 1.295, 1.35] and s["sl"] == [1.0] and s["add"] == [0.9], s

    # 1b) 纯止盈止损消息（不带币对，靠执行器锚点匹配上一条下单）
    s = P("止盈：1.2255 1.295 1.35\n\n止损：1\n\n补仓：0.9")
    assert s["type"] == "SET_TPSL" and s["symbol"] is None, s
    assert s["tp"] == [1.2255, 1.295, 1.35] and s["sl"] == [1.0] and s["add"] == [0.9], s

    # 2) 逗号小数点 + 区间写法
    s = P("止盈：0,01172-0,01205-0,012475\n\n止损：0,0105")
    assert s["tp"] == [0.01172, 0.01205, 0.012475], s
    assert s["sl"] == [0.0105], s

    # 3) 营销战绩播报必须被丢弃（否则会平掉真实仓位）
    assert P("🏆傲冠社区VIP内部群今日依旧空单瀑布收米💵💵  #CHIPUSDT  TP1  50x  +467.91%🚀") is None
    assert P("#CYS   内部群多单TP2止盈拿下") is None
    assert P("#AKE   没这大针的话，已经TP2收米了") is None

    # 4) 真实 TP 不能被误杀
    assert P("#VELVET   7个点了，手动TP1️⃣止盈减仓")["tp_level"] == 1
    assert P("#SKYAI   TP1️⃣止盈已触发，涨幅10%📈")["tp_level"] == 1

    # 5) CLOSE / CANCEL_ADD
    s = P("#ETH   此单已离场")
    assert s["type"] == "CLOSE" and s["symbol"] == "ETHUSDT", s
    s = P("#GPS     分批进场后已回成本，出补仓")
    assert s["type"] == "CANCEL_ADD" and s["symbol"] == "GPSUSDT", s

    # 6) 单字符币对 #G、以及 NEAR/USDT 写法
    s = P("#G    现价空 ( 波动大，1%-2%仓位即可 )")
    assert s["type"] == "OPEN" and s["symbol"] == "GUSDT" and s["side"] == "SHORT", s
    s = P("NEAR/USDT    挂单空 📉\n\n杠杆：30X-50X\n\n止盈：2.77-2.58\n\n止损：3.1")
    assert s["type"] == "SET_TPSL" and s["symbol"] == "NEARUSDT", s

    # 7) 行情建议（「出局即可」）不能当成平仓
    assert P("#PONS  目前处于震荡箱体低位，可以等等回调，给机会即可尝试，插破低点出局即可") is None

    print("signal_parser 自检通过 ✓  (10/10)")
