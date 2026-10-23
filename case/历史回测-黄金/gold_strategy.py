# -*- coding: utf-8 -*-
"""
黄金 AU 趋势策略 —— 实盘信号脚本（自包含，可被定时任务调用）

策略：鱼身趋势 + 波动率目标仓位 + 吊灯止损
  · 趋势过滤：收盘价在慢均线(MA60)之上视为多头环境，之下视为空头环境
  · 入场（吃鱼身）：价格回调后重新上穿快均线(MA10) → 做多；下穿 → 做空
  · 失效离场：入场后 2 根 K 线内收盘价重新跌破/上穿快均线 → 平仓
  · 趋势末端止盈：收盘价跌破/上穿慢均线 → 平仓
  · 吊灯止损：多头止损 = 持仓期最高价 - 3×ATR；空头对称
  · 仓位：按年化目标波动率(target_vol)反比定仓，使风险恒定

运行：python gold_strategy.py
输出：控制台 + output/signal_latest.csv + logs/strategy_YYYY-MM-DD.log
"""
import os, sys, json, datetime, traceback
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'config.json')
EMAIL_CONFIG_PATH = os.path.join(BASE, 'email_config.json')
STATE_PATH = os.path.join(BASE, 'state.json')
OUT_DIR = os.path.join(BASE, 'output')
LOG_DIR = os.path.join(BASE, 'logs')
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

DEFAULT_CFG = {
    "symbol": "AU0", "contract_multiplier": 1000, "account_equity": 1000000,
    "ma_slow": 60, "ma_fast": 10, "fail_bars": 2, "stop_mult": 0.0,
    "target_vol": 0.15, "max_size": 3.0, "vol_win": 60,
    "trail_atr": 3.0, "adx_min": 0.0, "pyramid_units": 1, "pyramid_atr": 1.0,
    "gap_guard": 0.04, "cost": 0.0004
}

def load_config():
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg

def load_email_config():
    if not os.path.exists(EMAIL_CONFIG_PATH):
        return None
    try:
        with open(EMAIL_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    try:
        with open(STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def maybe_notify(st, sig, log):
    """基金模式：推送"买入(进入多头)"与"卖出(离开多头→赎回)"两个信号。
    最近一次信号在 recent_days 天内且未通知过才发，state.json 去重。"""
    econf = load_email_config()
    if not econf or not econf.get('enabled', False):
        return
    pos = st['pos']; dates = st['date']
    state = load_state()
    today = pd.Timestamp.today().normalize()
    recent_days = int(econf.get('recent_days', 7))
    intraday = bool(sig.get('intraday', False))

    def _emit(idx, action):
        d = pd.Timestamp(dates[idx])
        if (today - d).days > recent_days:
            return
        key = 'last_notified_buy_date' if action == 'buy' else 'last_notified_sell_date'
        if state.get(key) == d.strftime('%Y-%m-%d'):
            return  # 已通知过
        px = float(st['close'][idx])
        if action == 'buy':
            subject = f"[黄金基金] 买入信号 {d:%Y-%m-%d}"
            act = "建议【申购/加仓】黄金基金"
        else:
            subject = f"[黄金基金] 卖出信号 {d:%Y-%m-%d}"
            act = "建议【赎回/减仓】黄金基金（转为空仓）"
        body = (f"黄金趋势策略触发{'买入' if action=='buy' else '卖出'}信号\n\n"
                f"{act}\n\n"
                f"信号日期 : {d:%Y-%m-%d}\n"
                f"信号价   : {px:.2f}\n"
                f"最新价   : {sig['price']:.2f}\n"
                f"慢/快均线: {sig['ma_slow']} / {sig['ma_fast']}\n"
                f"生成时间 : {sig['run_time']}\n"
                + ("注意：基于盘中实时价预判，收盘前请自行确认。\n" if intraday else "")
                + "\n（基金只能做多：空仓=赎回，不做空。自动推送，仅供参考，不构成投资建议）")
        try:
            from email_notify import send_email
            ok, msg = send_email(subject, body, econf)
            log(f"邮件推送      : {'成功' if ok else '失败'} -> {econf['to']} ({msg})")
            if ok:
                state[key] = d.strftime('%Y-%m-%d')
                save_state(state)
        except Exception as e:
            log(f"邮件推送异常  : {e}")

    buys = [i for i in range(1, len(pos)) if pos[i] == 1 and pos[i-1] != 1]
    sells = [i for i in range(1, len(pos)) if pos[i] != 1 and pos[i-1] == 1]
    if econf.get('notify_on_new_long', True) and buys:
        _emit(buys[-1], 'buy')
    if econf.get('notify_on_long_exit', True) and sells:
        _emit(sells[-1], 'sell')

def atr(h, l, c, n=14):
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    out = np.full(len(tr), np.nan); out[n - 1] = np.mean(tr[:n])
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out

def adx(h, l, c, n=14):
    up = np.diff(h, prepend=h[0]); dn = -np.diff(l, prepend=l[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = pd.Series(atr(h, l, c, n))
    pdi = 100 * pd.Series(plus_dm).ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * pd.Series(minus_dm).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().values

def _standardize(df):
    df = df.rename(columns={'日期': 'date', '开盘价': 'open', '最高价': 'high',
                            '最低价': 'low', '收盘价': 'close',
                            'd': 'date', 'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close'})
    for c in ['open', 'high', 'low', 'close']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
    df['date'] = pd.to_datetime(df['date'])
    return df[['date', 'open', 'high', 'low', 'close']]

def fetch_gold_daily(cfg):
    """优先 akshare，失败则直连新浪期货日线接口。"""
    symbol = cfg.get('symbol', 'AU0')
    today = datetime.date.today().strftime('%Y%m%d')
    try:
        import akshare as ak
        df = ak.futures_main_sina(symbol=symbol, start_date='20100101', end_date=today)
        if df is not None and len(df) > 200:
            return _standardize(df), 'akshare'
    except Exception:
        pass
    # 兜底：新浪直连
    import urllib.request
    url = ('https://stock2.finance.sina.com.cn/futures/api/json.php/'
           'InnerFuturesNewService.getDailyKLine?symbol=' + symbol)
    raw = urllib.request.urlopen(url, timeout=25).read().decode('gbk', 'ignore')
    arr = json.loads(raw)
    df = pd.DataFrame(arr)
    return _standardize(df), 'sina-direct'

def fetch_gold_realtime(cfg):
    """获取黄金盘中实时价。akshare spot 优先，失败回退新浪 nf_。"""
    symbol = cfg.get('symbol', 'AU0')
    try:
        import akshare as ak
        spot = ak.futures_zh_spot(symbol=symbol, market='CF', adjust='0')
        if spot is not None and len(spot):
            r = spot.iloc[0]
            price = float(r['current_price'])
            if price > 0:
                return dict(open=float(r['open']), high=float(r['high']),
                            low=float(r['low']), price=price)
    except Exception:
        pass
    import urllib.request
    url = 'https://hq.sinajs.cn/list=nf_' + symbol
    req = urllib.request.Request(url, headers={'Referer': 'https://finance.sina.com.cn',
                                               'User-Agent': 'Mozilla/5.0'})
    s = urllib.request.urlopen(req, timeout=15).read().decode('gbk', 'ignore')
    f = s.split('"')[1].split(',')
    price = float(f[8])
    if price <= 0:
        raise ValueError('bad realtime price')
    return dict(open=float(f[2]), high=float(f[3]), low=float(f[4]), price=price)

def simulate(df, p):
    """运行状态机，返回逐K线状态与最终状态。与回测逻辑完全一致。"""
    close = df['close'].values.astype(float); high = df['high'].values.astype(float)
    low = df['low'].values.astype(float); open_ = df['open'].values.astype(float)
    n = len(close)
    ma_s = df['close'].rolling(int(p['ma_slow'])).mean().values.astype(float)
    ma_f = df['close'].rolling(int(p['ma_fast'])).mean().values.astype(float)
    A = atr(high, low, close, 14)
    ADX = adx(high, low, close, 14)
    r = np.zeros(n); r[1:] = close[1:] / close[:-1] - 1
    prev_c = np.roll(close, 1); prev_c[0] = close[0]
    gap = np.abs(open_ - prev_c) / prev_c
    r[gap > 0.05] = 0.0
    vol = pd.Series(r).rolling(int(p['vol_win'])).std().shift(1).values * np.sqrt(252)
    if p['target_vol'] > 0:
        base = np.where(vol > 1e-6, p['target_vol'] / vol, 0.0)
        base = np.clip(base, 0, p['max_size']); base = np.nan_to_num(base, 0.0)
    else:
        base = np.ones(n)

    pos = np.zeros(n, int); units = np.zeros(n, int)
    stop = np.full(n, np.nan); entry = np.full(n, np.nan)
    cur = 0; u = 0; ep = 0.0; ei = -1; ex = 0.0
    for i in range(1, n):
        exited = False
        if cur != 0:
            stop_dist = (p['stop_mult'] * A[ei]
                         if (p['stop_mult'] > 0 and not np.isnan(A[ei])) else np.inf)
            if p['trail_atr'] > 0 and not np.isnan(A[i]):
                if cur == 1 and close[i] < ex - p['trail_atr'] * A[i]:
                    exited = True
                elif cur == -1 and close[i] > ex + p['trail_atr'] * A[i]:
                    exited = True
            if not exited:
                if cur == 1 and low[i] <= ep - stop_dist:
                    exited = True
                elif cur == -1 and high[i] >= ep + stop_dist:
                    exited = True
            if not exited and not np.isnan(ma_s[i]):
                if cur == 1 and close[i] < ma_s[i]:
                    exited = True
                elif cur == -1 and close[i] > ma_s[i]:
                    exited = True
            if not exited and (i - ei) <= p['fail_bars'] and not np.isnan(ma_f[i]):
                if cur == 1 and close[i] < ma_f[i]:
                    exited = True
                elif cur == -1 and close[i] > ma_f[i]:
                    exited = True
            if exited:
                cur = 0; u = 0
        if cur == 0 and i >= p['ma_slow'] and not np.isnan(ma_s[i]) and not np.isnan(ma_f[i]):
            gap_ok = gap[i] <= p['gap_guard']
            adx_ok = (p['adx_min'] <= 0) or (not np.isnan(ADX[i]) and ADX[i] >= p['adx_min'])
            if gap_ok and adx_ok:
                up = close[i] > ma_s[i]
                cu = (close[i - 1] < ma_f[i - 1]) and (close[i] > ma_f[i])
                cd = (close[i - 1] > ma_f[i - 1]) and (close[i] < ma_f[i])
                if up and cu:
                    cur = 1; u = 1; ep = close[i]; ei = i; ex = close[i]
                elif (not up) and cd:
                    cur = -1; u = 1; ep = close[i]; ei = i; ex = close[i]
        if cur != 0:
            if cur == 1:
                ex = max(ex, high[i])
            else:
                ex = min(ex, low[i])
            if p['pyramid_units'] > 1 and u < p['pyramid_units'] and not np.isnan(A[i]):
                if cur == 1 and close[i] >= ep + p['pyramid_atr'] * A[i] * u:
                    u += 1
                elif cur == -1 and close[i] <= ep - p['pyramid_atr'] * A[i] * u:
                    u += 1
        pos[i] = cur; units[i] = u; entry[i] = ep
        if not np.isnan(A[i]) and p['trail_atr'] > 0:
            if cur == 1:
                stop[i] = ex - p['trail_atr'] * A[i]
            elif cur == -1:
                stop[i] = ex + p['trail_atr'] * A[i]
    return dict(pos=pos, units=units, stop=stop, entry=entry, base=base,
                A=A, ma_s=ma_s, ma_f=ma_f, close=close, high=high, low=low,
                open=open_, date=df['date'].values)

def equity_curve(df, st, cost=0.0004):
    close = st['close']; n = len(close)
    r = np.zeros(n); r[1:] = close[1:] / close[:-1] - 1
    notional = st['base'] * st['units'] * st['pos']
    eq = 1e6; eqs = [eq]
    for i in range(1, n):
        pnl = notional[i - 1] * r[i]
        c = cost * abs(notional[i] - notional[i - 1])
        eq *= (1 + pnl - c)
        eqs.append(eq)
    return np.array(eqs)

def metrics_from_eq(eqs):
    yrs = (len(eqs) - 1) / 252
    total = eqs[-1] / 1e6 - 1
    cagr = (eqs[-1] / 1e6) ** (1 / yrs) - 1 if yrs > 0 else 0
    dd = (eqs - np.maximum.accumulate(eqs)) / np.maximum.accumulate(eqs)
    rets = np.diff(eqs) / eqs[:-1]
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if rets.std() > 0 else 0
    return dict(total=total, cagr=cagr, maxdd=dd.min(), sharpe=sharpe)

def make_signal(cfg, df, st):
    i = len(df) - 1
    pos = int(st['pos'][i]); units = int(st['units'][i])
    price = float(st['close'][i]); base = float(st['base'][i])
    mult = float(cfg['contract_multiplier']); equity = float(cfg['account_equity'])
    direction = {1: '做多', -1: '做空', 0: '空仓/观望'}[pos]
    exposure = equity * base * units if pos != 0 else 0.0
    lots = exposure / (price * mult) if (pos != 0 and price > 0) else 0.0
    stop = st['stop'][i]
    vol_ann = (cfg['target_vol'] / base) if (base > 1e-9 and cfg['target_vol'] > 0) else float('nan')
    return dict(
        run_time=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        data_date=pd.Timestamp(df['date'].iloc[i]).strftime('%Y-%m-%d'),
        price=price, direction=direction, pos=pos, units=units,
        base_size=base, exposure=exposure, lots=lots,
        stop_price=(None if (stop is None or np.isnan(stop)) else float(stop)),
        ann_vol=vol_ann,
        ma_slow=float(st['ma_s'][i]) if not np.isnan(st['ma_s'][i]) else None,
        ma_fast=float(st['ma_f'][i]) if not np.isnan(st['ma_f'][i]) else None,
    )

def main():
    cfg = load_config()
    log_lines = []
    def log(s):
        print(s); log_lines.append(s)
    try:
        df, src = fetch_gold_daily(cfg)
        intraday_used = False
        if cfg.get('intraday', False):
            try:
                rt = fetch_gold_realtime(cfg)
                if rt and rt['price'] > 0:
                    today_d = pd.Timestamp(datetime.date.today())
                    last_d = pd.Timestamp(df['date'].iloc[-1]).normalize()
                    if last_d == today_d:
                        # 已有今日日线 → 用盘中实时价覆盖收盘
                        df.loc[df.index[-1], ['open', 'high', 'low', 'close']] = \
                            [rt['open'], rt['high'], rt['low'], rt['price']]
                        intraday_used = True
                    elif today_d.weekday() < 5:
                        # 追加今日临时K线（盘中预判）
                        df = pd.concat([df, pd.DataFrame([{
                            'date': today_d, 'open': rt['open'], 'high': rt['high'],
                            'low': rt['low'], 'close': rt['price']}])], ignore_index=True)
                        intraday_used = True
                    if intraday_used:
                        src = src + '+实时预判'
            except Exception as e:
                log(f"实时价获取失败，改用日线: {e}")
        st = simulate(df, cfg)
        sig = make_signal(cfg, df, st)
        sig['intraday'] = intraday_used
        m = metrics_from_eq(equity_curve(df, st, cfg['cost']))
        # 数据新鲜度检查
        last = pd.Timestamp(df['date'].iloc[-1])
        stale_days = (pd.Timestamp.today().normalize() - last).days
        log('=' * 56)
        log(f"黄金AU趋势策略信号  |  运行 {sig['run_time']}  数据源:{src}")
        log('-' * 56)
        log(f"数据日期      : {sig['data_date']}  (距今 {stale_days} 天)")
        log(f"最新收盘价    : {sig['price']:.2f}")
        log(f"慢/快均线     : {sig['ma_slow']:.2f} / {sig['ma_fast']:.2f}" if sig['ma_slow'] and sig['ma_fast'] else "慢/快均线     : 数据不足")
        log(f"当前信号      : 【{sig['direction']}】")
        if sig['pos'] != 0:
            log(f"建议名义敞口  : {sig['exposure']:,.0f} 元  (目标波动率 {cfg['target_vol']*100:.0f}%)")
            log(f"建议手数      : {sig['lots']:.2f} 手  (合约乘数 {int(cfg['contract_multiplier'])})")
            if sig['lots'] < 1:
                log(f"提示          : 不足1手，至少需约 {sig['price']*cfg['contract_multiplier']:,.0f} 元名义/保证金")
            if sig['stop_price'] is not None:
                log(f"吊灯止损价    : {sig['stop_price']:.2f}")
        else:
            log("建议          : 空仓等待，等价格回调后重新上穿/下穿快均线再入场")
        log('-' * 56)
        log(f"历史回测参考(同配置): 年化 {m['cagr']*100:.1f}% | 最大回撤 {m['maxdd']*100:.1f}% | 夏普 {m['sharpe']:.2f}")
        if stale_days > 5:
            log("⚠️ 数据已超过5天未更新，可能休市或接口异常，请检查。")
        log('=' * 56)
        # 新买入/卖出信号 → 邮件推送
        maybe_notify(st, sig, log)
        # 写输出
        out = pd.DataFrame([sig])
        out.to_csv(os.path.join(OUT_DIR, 'signal_latest.csv'), index=False, encoding='utf-8-sig')
        with open(os.path.join(LOG_DIR, f"strategy_{datetime.date.today():%Y-%m-%d}.log"),
                  'a', encoding='utf-8') as f:
            f.write('\n'.join(log_lines) + '\n')
        return 0
    except Exception as e:
        err = f"[ERROR] {datetime.datetime.now():%Y-%m-%d %H:%M:%S} {e}\n{traceback.format_exc()}"
        print(err)
        with open(os.path.join(LOG_DIR, f"strategy_{datetime.date.today():%Y-%m-%d}.log"),
                  'a', encoding='utf-8') as f:
            f.write(err + '\n')
        return 1

if __name__ == '__main__':
    sys.exit(main())
