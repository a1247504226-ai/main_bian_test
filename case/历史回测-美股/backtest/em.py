# -*- coding: utf-8 -*-
"""东财后复权数据（data/em）的统一读写与组合回测。

与 portfolio.py 的区别：
1. 数据源换成东财后复权（fqt=2），序列连续、已含分红再投资；
2. 组合只统计「所有标的都已有数据」的区间，不再用 fillna(10000) 假装持仓；
3. 指标预热期单独加载，不计入权益曲线。
"""
import os
import numpy as np
import pandas as pd
from engine import backtest, _stats

HERE = os.path.dirname(os.path.abspath(__file__))
EM = os.path.join(HERE, "data", "em")
COST = dict(fee=0.0, slip=0.0005)

CN = {
    "SPY": "标普500", "QQQ": "纳指100", "IWM": "罗素2000", "DIA": "道指", "MDY": "标普中盘",
    "XLK": "科技", "XLE": "能源", "XLF": "金融", "XLV": "医疗", "XLI": "工业",
    "XLP": "必需消费", "XLU": "公用事业", "XLB": "材料", "XLY": "可选消费", "XLC": "通信",
    "XLRE": "房地产", "XBI": "生物科技", "SMH": "半导体1x", "SOXX": "半导体1x",
    "KRE": "区域银行", "XOP": "油气开采", "ITA": "航空国防",
    "GLD": "黄金", "SLV": "白银", "GDX": "金矿", "GDXJ": "小盘金矿",
    "USO": "原油", "UNG": "天然气", "DBC": "大宗商品", "XME": "金属矿业",
    "TLT": "20年美债", "IEF": "7-10年美债", "HYG": "高收益债", "LQD": "投资级债",
    "EMB": "新兴市场债", "TIP": "通胀债", "JNK": "垃圾债",
    "EEM": "新兴市场", "EFA": "发达市场", "FXI": "中国大盘", "EWJ": "日本",
    "EWZ": "巴西", "INDA": "印度", "EWY": "韩国", "EWG": "德国", "EWU": "英国",
    "AAPL": "苹果", "MSFT": "微软", "NVDA": "英伟达", "GOOGL": "谷歌", "AMZN": "亚马逊",
    "META": "Meta", "TSLA": "特斯拉", "AVGO": "博通", "AMD": "AMD", "NFLX": "奈飞",
    "ORCL": "甲骨文", "CRM": "Salesforce", "ADBE": "Adobe", "QCOM": "高通",
    "TXN": "德州仪器", "MU": "美光", "INTC": "英特尔", "CSCO": "思科", "NOW": "ServiceNow",
    "COST": "好市多", "WMT": "沃尔玛", "PG": "宝洁", "KO": "可口可乐", "PEP": "百事",
    "MCD": "麦当劳", "NKE": "耐克", "HD": "家得宝", "JNJ": "强生", "LLY": "礼来",
    "PFE": "辉瑞", "UNH": "联合健康", "JPM": "摩根大通", "BAC": "美国银行",
    "GS": "高盛", "V": "Visa", "MA": "万事达", "AXP": "美国运通",
    "CAT": "卡特彼勒", "BA": "波音", "GE": "通用电气", "HON": "霍尼韦尔",
    "UPS": "UPS", "DIS": "迪士尼",
}


BREAK_TH = 0.55   # 单日涨跌超过这个幅度，视为后复权因子断裂（真实大市值个股几乎不可能）


def _cut_breaks(d):
    """截掉最后一处复权因子断裂之前的数据。

    东财后复权序列在 2005-01-03（以及个别标的的 2004/2007 年）存在因子不一致的接缝：
    AAPL 那天凭空 +91%、NFLX 2004-02-12 凭空 +107%。这些不是行情，是复权基准切换。
    只保留最后一处接缝之后的数据，序列内部才是自洽的。
    """
    r = d["close"].pct_change()
    bad = r.abs() > BREAK_TH
    if bad.any():
        i = int(np.flatnonzero(bad.values)[-1])
        d = d.iloc[i:].reset_index(drop=True)
    return d


def load(sym):
    d = pd.read_csv(os.path.join(EM, f"{sym}.csv"), parse_dates=["date"])
    d["date"] = pd.to_datetime(d["date"]).astype("datetime64[ns]")
    return _cut_breaks(d.sort_values("date").reset_index(drop=True))


def ma_sig(df, n=250):
    return (df["close"] > df["close"].rolling(n).mean()).astype(float).fillna(0).values


def one(sym, n=250, vt=0.025, start=None, warmup_days=420):
    """单标的回测，返回 Result（指标已用 start 之前的数据预热）。"""
    d = load(sym)
    if start:
        s0 = pd.Timestamp(start) - pd.Timedelta(days=warmup_days)
        d = d[d.date >= s0].reset_index(drop=True)
        cut = int((d.date < pd.Timestamp(start)).sum())
    else:
        cut = 0
    r = backtest(d, ma_sig(d, n), vol_target=vt, vol_cap=1.0, **COST)
    eq = np.asarray(r.equity, float)
    if cut > 0:
        sub = eq[cut:] / eq[cut - 1] * 10000 if cut < len(eq) else eq[-1:]
        st = _stats(sub, None)
        st["equity"] = sub
        st["in_market"] = float((np.asarray(r.exposure)[cut:] > 0.01).mean())
        st["avg_expo"] = float(np.asarray(r.exposure)[cut:].mean())
        st["n"] = r.n
        return Result(st)
    return r


class Result(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def _series(sym, n, vt, start, mode, warmup_days=420):
    d = load(sym)
    s0 = pd.Timestamp(start) - pd.Timedelta(days=warmup_days)
    d = d[d.date >= s0].reset_index(drop=True)
    if mode == "bh":
        r = backtest(d, np.ones(len(d)), **COST)
    else:
        r = backtest(d, ma_sig(d, n), vol_target=vt, vol_cap=1.0, **COST)
    ser = pd.Series(np.asarray(r.equity, float), index=pd.DatetimeIndex(d.date))
    ser = ser[~ser.index.duplicated(keep="last")]
    ser = ser[ser.index >= pd.Timestamp(start)]
    # 关键：预热期（start 之前）的权益增长必须剔除，否则各子账户起点资金不等，
    # 组合就变成了「按 2002-2004 表现加权」而不是等权。
    if len(ser):
        ser = ser / ser.iloc[0] * 10000.0
    return ser


def port(syms, n=250, vt=0.025, start="2004-01-01", mode="ma"):
    """等权子账户组合。要求所有标的在 start 之前都有足够历史（否则抛错）。"""
    curves = {}
    for s in syms:
        curves[s] = _series(s, n, vt, start, mode)
    df = pd.DataFrame(curves)
    df = df.ffill().dropna()          # 只保留全部标的都有数据的交易日
    total = df.sum(axis=1) / len(syms)
    total.index = total.index.astype("datetime64[ns]")
    return total


def stats(eq):
    return _stats(np.asarray(eq, float), None)


def show(name, syms, n=250, vt=0.025, start="2004-01-01"):
    tr = port(syms, n, vt, start, "ma")
    bh = port(syms, n, vt, start, "bh")
    a = stats(tr); b = stats(bh)
    print(f"{name:<16}{start}  {len(tr)/252:5.1f}年  "
          f"趋势 {a['total']:8.2f}x/{a['cagr']*100:5.1f}%/{a['mdd']*100:6.1f}%/Cal {a['calmar']:.2f}/Sh {a['sharpe']:.2f}  "
          f"| 买入 {b['total']:8.2f}x/{b['cagr']*100:5.1f}%/{b['mdd']*100:6.1f}%/Cal {b['calmar']:.2f}/Sh {b['sharpe']:.2f}  "
          f"| ΔCal {a['calmar']-b['calmar']:+.2f}")
    return tr, bh, a, b


if __name__ == "__main__":
    show("5只核心", ["AAPL", "MSFT", "NVDA", "AMZN", "NFLX"], start="2004-01-01")
