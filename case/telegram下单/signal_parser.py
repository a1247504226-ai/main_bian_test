"""
TG 频道消息解析器

真实消息长这样（来自傲冠社区）：
    #OGN    现价空（左侧摸顶，轻仓即可，预留好补仓位）
    补仓：0,0294
    止盈：0,0269-0,026-0,025
    止损：0,0301

难点：
1. 止盈止损消息里【没有 symbol】，只能靠「最近一个待更新的开仓信号」匹配
2. 止盈和止损经常分两条消息发，要缓存等齐
3. 大量干扰消息（战绩、广告、行情分析、TP 触发通知）
4. 欧洲小数逗号 0,425 → 0.425
"""
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional

# 事件类型
OPEN = "OPEN"        # 开仓
TPSL = "TPSL"        # 止盈止损（可能只有一半）
CLOSE = "CLOSE"      # 明确要求离场
TP_HIT = "TP_HIT"    # 到达某个止盈目标（TP1/TP2/TP3）
IGNORE = "IGNORE"    # 忽略

# 方向关键词
SHORT_KEYS = ("现价空", "挂单空", "市价空", "限价空")
LONG_KEYS = ("现价多", "挂单多", "市价多", "限价多")

# 干扰词：出现这些说明是结果通知/广告，不是新信号
NOISE_KEYS = (
    "盈利反馈", "战绩", "恭喜", "上电视", "会员", "VIP", "内部群",
    "名额", "活动", "点击此处", "私信", "小助理", "感谢信任",
    "已翻倍", "已到达", "已抵达", "手动止盈", "减仓", "离场",
    "带保本", "TP1", "TP2", "TP3", "浮盈", "小浮盈", "进场已",
)

# 真正要市价平仓的关键词
# 注意：
#  - 不包含「止盈」「TP1」「TP2」—— TP1/TP2 到达只是减仓，不平仓
#  - 不包含单独的「止损」—— 「止损：77500」是价位，不是平仓指令
#  - 不包含裸「平仓」—— 「现价多（平仓价参考 80000）」会被误判成平仓，
#    本该开仓却把仓位平了。只认明确的「已平仓 / 平仓了 / 手动平仓」
CLOSE_KEYS = (
    "离场", "全平", "出局", "扫损",
    "止损了", "已止损", "到达止损", "打止损",
    "已平仓", "平仓了", "手动平仓", "平仓止盈", "平仓离场", "全部平仓",
)

# 明确表示「这条别跟」的词，直接忽略，避免把讨论性内容当信号
SKIP_KEYS = ("不要跟", "别跟", "勿跟", "不要追", "仅供参考", "已经过了", "已错过")

SYMBOL_RE = re.compile(r"#([A-Z0-9]{2,15})")
# 止盈：0.425-0.41-0.3925  /  止盈: 74500-73000
TP_RE = re.compile(r"止盈\s*[：:]\s*([\d.,\-]+)")
SL_RE = re.compile(r"止损\s*[：:]\s*([\d.,]+)")
COMP_RE = re.compile(r"补仓\s*[：:]\s*([\d.,]+)")
# 纯数字列表，如 "0.425-0.41-0.3925"
NUM_LIST_RE = re.compile(r"^\s*([\d.,]+(?:\s*-\s*[\d.,]+)+)\s*$")


@dataclass
class ParsedEvent:
    kind: str
    symbol: str = ""
    direction: str = ""          # long / short
    entry: Optional[float] = None
    tp: Optional[float] = None
    sl: Optional[float] = None
    raw: str = ""
    note: str = ""               # 解析说明，写日志用
    level: int = 0               # TP 级别（TP_HIT 时有效）
    tp_all: List[float] = field(default_factory=list)   # 全部止盈目标
    is_limit: bool = False       # 挂单（限价）信号：频道让你在特定区间挂单等成交

    def __repr__(self):
        return (f"<{self.kind} {self.symbol} {self.direction} "
                f"entry={self.entry} tp={self.tp} sl={self.sl}>")


def _clean(text: str) -> str:
    """统一标点：全角转半角、欧洲小数逗号转点

    这一步很关键：频道经常用全角逗号写价格（0，0269），
    不转换的话正则会截断成 0.0，止盈价直接算错。
    """
    t = text.replace("\u200b", "").replace("\ufeff", "")
    t = t.replace("，", ",").replace("、", ",").replace("。", ".")
    t = t.replace("：", ":").replace("；", ";")
    # 数字之间的逗号当小数点：0,0269 → 0.0269
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)
    return t


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s.strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _parse_num_list(s: str) -> List[float]:
    """'0.425-0.41-0.3925' → [0.425, 0.41, 0.3925]"""
    out = []
    for part in re.split(r"-", s):
        v = _to_float(part)
        if v is not None:
            out.append(v)
    return out


def extract_symbol(text: str) -> str:
    """取第一个像样的 #SYMBOL"""
    for m in SYMBOL_RE.finditer(text):
        sym = m.group(1).upper()
        if sym in ("USDT", "TP1", "TP2", "TP3", "SL", "VIP"):
            continue
        return sym
    return ""


def extract_symbol_near_direction(text: str) -> str:
    """取「方向词最近」的币种

    消息里常顺带提到别的币（#BTCUSDT 现价空，参考 #ETHUSDT 走势），
    按位置取最近的那个才不会开错仓。
    """
    for key in SHORT_KEYS + LONG_KEYS:
        p = text.find(key)
        if p < 0:
            continue
        before = text[:p]
        ms = [m for m in SYMBOL_RE.finditer(before)
              if m.group(1).upper() not in ("USDT", "TP1", "TP2", "TP3", "SL", "VIP")]
        if ms:
            return ms[-1].group(1).upper()
        m = SYMBOL_RE.search(text[p:])
        if m and m.group(1).upper() not in ("USDT", "TP1", "TP2", "TP3", "SL", "VIP"):
            return m.group(1).upper()
    return ""


def normalize_symbol(sym: str) -> str:
    """BTC → BTCUSDT（币安合约统一加 USDT）

    其他计价货币直接拒绝，避免拼成 BTCUSDCUSDT 这种不存在的交易对
    """
    if not sym:
        return ""
    s = sym.upper()
    if s.endswith("USDT"):
        return s
    if s.endswith(("USDC", "BUSD")):
        return ""       # 非 USDT 本位，不接
    return s + "USDT"


def detect_direction(text: str) -> str:
    """返回 long / short / 空字符串

    同时出现多空关键词时返回空 —— 宁可漏单，不能猜错方向反向开仓
    """
    has_short = any(k in text for k in SHORT_KEYS)
    has_long = any(k in text for k in LONG_KEYS)
    if has_short and has_long:
        return ""
    if has_short:
        return "short"
    if has_long:
        return "long"
    return ""


def is_noise(text: str) -> bool:
    return any(k in text for k in NOISE_KEYS)


def parse(text: str) -> ParsedEvent:
    """解析一条消息"""
    raw = text
    t = _clean(text).strip()
    if not t:
        return ParsedEvent(IGNORE, raw=raw, note="空消息")

    # 明确说「不要跟 / 仅供参考 / 已经过了」的直接忽略
    if any(k in t for k in SKIP_KEYS):
        return ParsedEvent(IGNORE, raw=raw, note="消息明确说不要跟")

    direction = detect_direction(t)
    symbol = extract_symbol_near_direction(t) or extract_symbol(t)

    # 消息里顺带提到别的币是常态，只记一笔，不跳过（避免误伤正常信号）
    all_syms = [s.upper() for s in SYMBOL_RE.findall(t)
                if s.upper() not in ("USDT", "TP1", "TP2", "TP3", "SL", "VIP")]
    multi_note = ""
    if direction and len(set(all_syms)) > 1:
        multi_note = f"消息含多币种 {sorted(set(all_syms))}，已取 {symbol}"

    tp_m = TP_RE.search(t)
    sl_m = SL_RE.search(t)
    comp_m = COMP_RE.search(t)

    # ---------- 1. 开仓信号：必须有 symbol + 方向 ----------
    # 含「离场/止损了」的不当开仓信号（频道有时会写「现价多 已离场」）
    if symbol and direction and not any(k in t for k in CLOSE_KEYS):
        norm = normalize_symbol(symbol)
        if not norm:
            # 非 USDT 本位（BTCUSDC/BTCBUSD 之类），不接
            return ParsedEvent(IGNORE, raw=raw,
                               note=f"{symbol} 不是 USDT 本位合约，跳过")
        ev = ParsedEvent(OPEN, symbol=norm,
                         direction=direction, raw=raw, note=multi_note)
        # 挂单 = 限价单，频道给的「分批进场：3.13-3.08」是挂单区间。
        # 市价开仓会开在当前价，可能远离区间，所以单独标记出来交给上层决定
        ev.is_limit = "挂单" in t
        # 少数情况开仓消息里直接带了止盈止损（取最远目标 TP3）
        if tp_m:
            nums = _parse_num_list(tp_m.group(1))
            if nums:
                ev.tp = nums[-1]
        if sl_m:
            ev.sl = _to_float(sl_m.group(1))
        if comp_m:
            ev.entry = _to_float(comp_m.group(1))
            ev.note = "补仓价作入场参考"
        return ev

    # ---------- 2. 止盈止损：有冒号+数字，且不是结果通知 ----------
    if (tp_m or sl_m) and not is_noise(t):
        # 战绩统计消息形如「获利：24单 止损：4单」，真实价格不会带「单」字。
        # 不拦的话止损价会被解析成 4.0，等于把止损设在 4 美元（形同裸奔）
        if re.search(r"[\d.,]+\s*单", t):
            return ParsedEvent(IGNORE, raw=raw, note="战绩统计消息，不是价格")
        # 少数消息会带币种名（#BTCUSDT 止盈：...），有的话就带上，匹配更准
        ev = ParsedEvent(TPSL, raw=raw)
        if symbol:
            norm = normalize_symbol(symbol)
            if not norm:
                # 带了币种但不是 USDT 本位（BTCUSDC/BTCBUSD），
                # 不能当成「无币种」去分配给别的仓位
                return ParsedEvent(IGNORE, raw=raw,
                                   note=f"{symbol} 不是 USDT 本位合约，跳过")
            ev.symbol = norm
        if tp_m:
            nums = _parse_num_list(tp_m.group(1))
            if nums:
                # 取最后一个 = TP3（最远目标）挂单，同时保留全部用于移动止损
                ev.tp = nums[-1]
                ev.tp_all = nums
                if len(nums) > 1:
                    ev.note = f"止盈多目标 {nums}，挂 TP3={nums[-1]}"
        if sl_m:
            ev.sl = _to_float(sl_m.group(1))
        if ev.tp is not None or ev.sl is not None:
            return ev

    # ---------- 3. 纯数字列表（上一行是 symbol 的拆分消息）----------
    m = NUM_LIST_RE.match(t)
    if m:
        return ParsedEvent(IGNORE, raw=raw, note="裸数字列表，缺上下文")

    # ---------- 4. 平仓通知 ----------
    # 只有「离场 / 平仓 / 止损了」这类明确指令才市价平仓。
    # TP1/TP2 到达只是减仓通知，不平仓 —— 止盈挂的是 TP3，让利润跑完。
    if symbol and any(k in t for k in CLOSE_KEYS):
        norm = normalize_symbol(symbol)
        if norm:
            return ParsedEvent(CLOSE, symbol=norm, raw=raw)

    # ---------- 5. 到达某个止盈目标 ----------
    # 「TP1 止盈减仓」「TP2 已抵达」—— 不平仓，但要用来移动止损
    tp_hit = re.search(r"TP\s*([123])", t, re.IGNORECASE)
    if symbol and tp_hit:
        norm = normalize_symbol(symbol)
        if norm:
            return ParsedEvent(TP_HIT, symbol=norm,
                               level=int(tp_hit.group(1)), raw=raw)

    return ParsedEvent(IGNORE, raw=raw, note="非信号消息")


# ----------------------------------------------------------------------
class TPSLBuffer:
    """止盈和止损经常分两条发，这里缓存等齐

    匹配策略：频道一次只推一个信号，所以分配给「最早的一个待更新仓位」。
    """

    def __init__(self, ttl_sec: int = 90):
        self.ttl = ttl_sec
        self._buf = {}   # symbol -> {"tp":x, "sl":y, "ts":time}

    def feed(self, ev: ParsedEvent, target_symbol: str, now: float):
        if not target_symbol:
            return None
        b = self._buf.setdefault(target_symbol,
                                 {"tp": None, "sl": None, "tp_all": [], "ts": now})
        if ev.tp is not None:
            b["tp"] = ev.tp
        if ev.tp_all:
            b["tp_all"] = ev.tp_all
        if ev.sl is not None:
            b["sl"] = ev.sl
        b["ts"] = now
        if b["tp"] is not None and b["sl"] is not None:
            out = dict(b)
            self._buf.pop(target_symbol, None)
            return out
        return None

    def flush_expired(self, now: float):
        """返回超时但只有一半的数据，供告警"""
        out = []
        for sym, b in list(self._buf.items()):
            if now - b["ts"] > self.ttl:
                out.append((sym, dict(b)))
                self._buf.pop(sym, None)
        return out

    def restore(self, symbol: str, tp=None, sl=None, tp_all=None,
                relaxed: bool = False):
        """amend 失败时把数据放回缓存，等 housekeeping 重试

        relaxed=True 表示这是「移动止损」，重试时不能再走「止损必须在入场价外侧」
        的严格校验（那时止损已经移到盈利区了）
        """
        if not symbol:
            return
        b = self._buf.get(symbol) or {"tp": None, "sl": None, "ts": time.time(), "retry": 0}
        if tp is not None:
            b["tp"] = tp
        if sl is not None:
            b["sl"] = sl
        if tp_all:
            b["tp_all"] = tp_all
        if relaxed:
            b["relaxed"] = True
        b["ts"] = time.time()
        b["retry"] = b.get("retry", 0) + 1
        self._buf[symbol] = b

    def ready(self):
        """tp 和 sl 都齐了的，供重试"""
        return {s: dict(b) for s, b in self._buf.items()
                if b.get("tp") is not None and b.get("sl") is not None}

    def drop(self, symbol: str):
        self._buf.pop(symbol, None)

    def pending(self):
        return dict(self._buf)


# ----------------------------------------------------------------------
if __name__ == "__main__":
    cases = [
        "#OGN    现价空（左侧摸顶，轻仓即可，预留好补仓位）",
        "止盈：0,0269-0,026-0,025",
        "止损：0,0301",
        "#BTCUSDT   挂单空 📉",
        "止盈：74500-73000-71500",
        "止损：77500",
        "#OGN    手动TP1止盈减仓拿下",
        "#傲冠VIP群发会员做单盈利反馈 （11/02）🏆",
        "恭喜会员盈利，感谢反馈，上电视咯",
        "#UAI    现价多（波动大，轻仓即可）",
        "补仓：0,0386",
        "#BTCUSDT   现价空（预留好补仓位）",
    ]
    for c in cases:
        e = parse(c)
        print(f"{e.kind:7s} {e.symbol:12s} {e.direction:6s} "
              f"tp={e.tp} sl={e.sl} entry={e.entry}  <- {c[:40]}")
