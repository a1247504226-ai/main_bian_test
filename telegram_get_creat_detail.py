import re


class SignalParser:

    # =========================
    # 正则
    # =========================

    # 开仓：
    # #ETH 现价空
    # #BB 现价多（预留好补仓位）
    OPEN_RE = re.compile(
        r"#\s*(?P<symbol>[A-Za-z0-9]+)\s*"
        r"(?:现价|市价)\s*"
        r"(?P<side>多|空)",
        re.I
    )

    # 币种：
    # #ETHUSDT
    SYMBOL_RE = re.compile(
        r"#\s*(?P<symbol>[A-Za-z0-9]+)",
        re.I
    )

    # 止盈：
    # 止盈：2305-2250-2200
    TP_RE = re.compile(
        r"^\s*止盈\s*[:：]\s*(?P<value>.+?)\s*$",
        re.I | re.M
    )

    # 止损：
    # 止损：2435
    SL_RE = re.compile(
        r"^\s*止损\s*[:：]\s*(?P<value>.+?)\s*$",
        re.I | re.M
    )

    # 补仓：
    # 补仓：2400
    ADD_RE = re.compile(
        r"^\s*补仓\s*[:：]\s*(?P<value>.+?)\s*$",
        re.I | re.M
    )

    # TP：
    # TP1
    # TP1止盈
    # 手动TP1止盈
    # TP1️⃣
    TP_EVENT_RE = re.compile(
        r"(?:手动\s*)?"
        r"TP\s*(?P<level>[123])"
        r"(?:️⃣)?"
        r"(?:\s*止盈)?",
        re.I
    )

    # =========================
    # 数字
    # 支持：
    # 0.123
    # 0,123
    # 123.45
    # =========================
    NUMBER_RE = re.compile(
        r"\d+(?:[.,]\d+)?"
    )

    @classmethod
    def numbers(cls, text):
        """
        从字符串中提取价格数字

        0,123-0,1372-0,142
        ->
        [0.123, 0.1372, 0.142]
        """

        if not text:
            return []

        result = []

        for x in cls.NUMBER_RE.findall(text):
            # Telegram 里面大量使用逗号当小数点
            x = x.replace(",", ".")

            try:
                result.append(float(x))
            except ValueError:
                pass

        return result

    @classmethod
    def symbol(cls, text):
        """
        获取币种
        """

        m = cls.SYMBOL_RE.search(text or "")

        if not m:
            return None

        return m.group("symbol").upper()

    @classmethod
    def parse_signal(cls, text):
        """
        Telegram 消息统一解析入口

        返回：
        {
            "type": "OPEN",
            "symbol": "ETHUSDT",
            "side": "SHORT",
            "add": [],
            "tp": [],
            "sl": [],
            "tp_level": None
        }

        无关消息：
        None
        """

        if not text:
            return None

        text = text.strip()

        # ==========================================
        # 1. 开仓
        # ==========================================

        m = cls.OPEN_RE.search(text)

        if m:

            symbol = m.group("symbol").upper()

            # ETH -> ETHUSDT
            if not symbol.endswith("USDT"):
                symbol += "USDT"

            side = (
                "LONG"
                if m.group("side") == "多"
                else "SHORT"
            )

            return {
                "type": "OPEN",
                "symbol": symbol,
                "side": side,
                "add": [],
                "tp": [],
                "sl": [],
                "tp_level": None
            }

        # ==========================================
        # 2. TP1 / TP2 / TP3
        # ==========================================

        m = cls.TP_EVENT_RE.search(text)

        if m:

            level = int(m.group("level"))

            # 例如：
            # #BB TP2止盈
            # #AIN 手动TP1止盈
            # #NIL TP3️⃣止盈
            symbol = cls.symbol(text)

            if symbol and not symbol.endswith("USDT"):
                symbol += "USDT"

            return {
                "type": "TP",
                "symbol": symbol,
                "side": None,
                "add": [],
                "tp": [],
                "sl": [],
                "tp_level": level
            }

        # ==========================================
        # 3. 止盈
        # ==========================================

        m = cls.TP_RE.search(text)

        if m:

            values = cls.numbers(m.group("value"))

            if values:
                return {
                    "type": "SET_TPSL",
                    "symbol": cls.symbol(text),
                    "side": None,
                    "add": [],
                    "tp": values,
                    "sl": [],
                    "tp_level": None
                }

        # ==========================================
        # 4. 止损
        # ==========================================

        m = cls.SL_RE.search(text)

        if m:

            values = cls.numbers(m.group("value"))

            if values:
                return {
                    "type": "SET_TPSL",
                    "symbol": cls.symbol(text),
                    "side": None,
                    "add": [],
                    "tp": [],
                    "sl": values,
                    "tp_level": None
                }

        # ==========================================
        # 5. 补仓
        # ==========================================

        m = cls.ADD_RE.search(text)

        if m:

            values = cls.numbers(m.group("value"))

            if values:
                return {
                    "type": "SET_TPSL",
                    "symbol": cls.symbol(text),
                    "side": None,
                    "add": values,
                    "tp": [],
                    "sl": [],
                    "tp_level": None
                }

        # ==========================================
        # 6. 其他全部忽略
        # ==========================================

        return None