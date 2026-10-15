# -*- coding: utf-8 -*-
"""纯标准库自绘 SVG 工具（离线可看）"""
import math

# A 股配色习惯：红=涨/盈利，绿=跌/亏损
RED = "#c0392b"
GREEN = "#1e8449"
BLUE = "#2471a3"
ORANGE = "#d68910"
PURPLE = "#7d3c98"
GREY = "#7f8c8d"
DARK = "#1c2833"
LIGHT = "#eaf0f6"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _nice_log_ticks(lo, hi):
    """lo/hi 是 log10 值，返回实际值刻度"""
    ticks = []
    k0, k1 = int(math.floor(lo)), int(math.ceil(hi))
    for k in range(k0, k1 + 1):
        for m in (1, 2, 5):
            v = m * 10 ** k
            if lo <= math.log10(v) <= hi:
                ticks.append(v)
    return ticks


def _nice_lin_ticks(lo, hi, cnt=5):
    if hi <= lo:
        hi = lo + 1
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / cnt))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= cnt:
            step *= m
            break
    v = math.ceil(lo / step) * step
    out = []
    while v <= hi + 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def line_chart(dates, series, width=1060, height=430, logy=True, title="",
               ylab="", pad=(62, 170, 46, 58)):
    """
    series: [(label, values, color)]
    dates: 完整日期数组（图表自行等距抽样，避免硬编码步长）
    """
    pl, pr, pt, pb = pad
    W, H = width, height
    iw, ih = W - pl - pr, H - pt - pb
    allv = [v for _, vals, _ in series for v in vals if v is not None and v > 0]
    if not allv:
        return "<svg></svg>"
    lo, hi = min(allv), max(allv)
    if logy:
        lo = max(lo, 1e-3)
        llo, lhi = math.log10(lo), math.log10(hi)
        pad_l = (lhi - llo) * 0.06 or 0.1
        llo, lhi = llo - pad_l, lhi + pad_l
        ticks = _nice_log_ticks(llo, lhi)

        def yp(v):
            if v <= 0:
                v = 1e-6
            return pt + ih - (math.log10(v) - llo) / (lhi - llo) * ih
    else:
        span = hi - lo or 1
        lo, hi = lo - span * 0.06, hi + span * 0.06
        ticks = _nice_lin_ticks(lo, hi)

        def yp(v):
            return pt + ih - (v - lo) / (hi - lo) * ih

    n = len(dates)
    step = max(1, math.ceil(n / 12))

    def xp(i):
        return pl + (i / max(1, n - 1)) * iw

    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="100%%" '
         'style="max-width:%dpx;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
         % (W, H, W)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    if title:
        s.append('<text x="%d" y="26" font-size="15" font-weight="700" fill="%s">%s</text>'
                 % (pl, DARK, esc(title)))
    # 网格 + y 轴
    for t in ticks:
        y = yp(t)
        if y < pt - 2 or y > pt + ih + 2:
            continue
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e3e8ee" stroke-width="1"/>'
                 % (pl, y, pl + iw, y))
        lab = ("%g" % t) if t < 1000 else ("%.0f" % t)
        s.append('<text x="%d" y="%.1f" font-size="11" fill="%s" text-anchor="end">%s</text>'
                 % (pl - 8, y + 4, GREY, esc(lab)))
    if ylab:
        s.append('<text x="%d" y="%d" font-size="11" fill="%s">%s</text>'
                 % (pl, pt - 12, GREY, esc(ylab)))
    # 基准线 y=1
    if logy and lo <= 1 <= hi:
        y = yp(1)
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1" '
                 'stroke-dasharray="4,3"/>' % (pl, y, pl + iw, y, GREY))
    # x 轴标签
    for i in range(0, n, step):
        s.append('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                 % (xp(i), H - pb + 18, GREY, esc(dates[i][:7])))
    # 裁剪区
    s.append('<clipPath id="clip%d"><rect x="%d" y="%d" width="%d" height="%d"/></clipPath>'
             % (id(series) % 99999, pl, pt, iw, ih))
    cid = "clip%d" % (id(series) % 99999)
    for label, vals, color in series:
        pts = []
        for i, v in enumerate(vals):
            if v is None or v <= 0:
                continue
            pts.append("%.1f,%.1f" % (xp(i), yp(v)))
        if not pts:
            continue
        s.append('<polyline clip-path="url(#%s)" fill="none" stroke="%s" stroke-width="1.7" '
                 'points="%s"/>' % (cid, color, " ".join(pts)))
    # 图例（右侧留白内，避免出界）
    ly = pt + 6
    for label, vals, color in series:
        s.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="2.6"/>'
                 % (pl + iw + 14, ly, pl + iw + 34, ly, color))
        s.append('<text x="%d" y="%d" font-size="11.5" fill="%s">%s</text>'
                 % (pl + iw + 40, ly + 4, DARK, esc(label)))
        ly += 19
    # 边框
    s.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#cfd8e3"/>'
             % (pl, pt, iw, ih))
    s.append("</svg>")
    return "".join(s)


def area_chart(dates, vals, width=1060, height=210, title="", color=GREEN,
               pad=(62, 40, 40, 48)):
    pl, pr, pt, pb = pad
    W, H = width, height
    iw, ih = W - pl - pr, H - pt - pb
    lo, hi = min(vals), max(vals)
    hi = max(hi, 0.0)
    lo = min(lo, 0.0)
    if hi == lo:
        hi = lo + 0.01

    def yp(v):
        return pt + ih - (v - lo) / (hi - lo) * ih

    n = len(dates)
    step = max(1, math.ceil(n / 12))
    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="100%%" '
         'style="max-width:%dpx;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
         % (W, H, W)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    if title:
        s.append('<text x="%d" y="22" font-size="14" font-weight="700" fill="%s">%s</text>'
                 % (pl, DARK, esc(title)))
    for t in _nice_lin_ticks(lo, hi, 4):
        y = yp(t)
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e3e8ee"/>'
                 % (pl, y, pl + iw, y))
        s.append('<text x="%d" y="%.1f" font-size="10.5" fill="%s" text-anchor="end">%.0f%%</text>'
                 % (pl - 7, y + 4, GREY, t * 100))
    pts = ["%.1f,%.1f" % (pl + i / max(1, n - 1) * iw, yp(v)) for i, v in enumerate(vals)]
    s.append('<polygon fill="%s" fill-opacity="0.28" points="%d,%.1f %s %d,%.1f"/>'
             % (color, pl, yp(0), " ".join(pts), pl + iw, yp(0)))
    s.append('<polyline fill="none" stroke="%s" stroke-width="1.5" points="%s"/>'
             % (color, " ".join(pts)))
    for i in range(0, n, step):
        s.append('<text x="%.1f" y="%d" font-size="10" fill="%s" text-anchor="middle">%s</text>'
                 % (pl + i / max(1, n - 1) * iw, H - pb + 18, GREY, esc(dates[i][:7])))
    s.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#cfd8e3"/>'
             % (pl, pt, iw, ih))
    s.append("</svg>")
    return "".join(s)


def bar_chart(cats, series, width=1060, height=330, title="", ylab="", yfmt="{:.0f}",
              pad=(62, 170, 46, 56), show_values=True, hline=None, colors=None,
              hlines=None):
    """series: [(label, values, color)]  分组柱状图；hlines=[(值, 文本, 颜色)]"""
    pl, pr, pt, pb = pad
    W, H = width, height
    iw, ih = W - pl - pr, H - pt - pb
    allv = [v for _, vals, _ in series for v in vals]
    lo = min(0.0, min(allv))
    hi = max(0.0, max(allv))
    if hi == lo:
        hi = lo + 1
    pad_v = (hi - lo) * 0.12
    lo -= pad_v if lo < 0 else 0
    hi += pad_v

    def yp(v):
        return pt + ih - (v - lo) / (hi - lo) * ih

    n = len(cats)
    ng = len(series)
    gw = iw / max(1, n)
    bw = gw * 0.72 / ng
    s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="100%%" '
         'style="max-width:%dpx;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
         % (W, H, W)]
    s.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    if title:
        s.append('<text x="%d" y="24" font-size="15" font-weight="700" fill="%s">%s</text>'
                 % (pl, DARK, esc(title)))
    for t in _nice_lin_ticks(lo, hi, 5):
        y = yp(t)
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#e3e8ee"/>'
                 % (pl, y, pl + iw, y))
        s.append('<text x="%d" y="%.1f" font-size="10.5" fill="%s" text-anchor="end">%s</text>'
                 % (pl - 7, y + 4, GREY, esc(yfmt.format(t))))
    if lo < 0 < hi:
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>'
                 % (pl, yp(0), pl + iw, yp(0), GREY))
    for gi, (label, vals, color) in enumerate(series):
        for i, v in enumerate(vals):
            x = pl + gw * i + gw * 0.14 + bw * gi
            y0, y1 = yp(0), yp(v)
            top, hh = min(y0, y1), abs(y1 - y0)
            c = colors[i] if colors else color
            s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
                     % (x, top, bw * 0.92, max(1, hh), c))
            if show_values and n <= 14:
                s.append('<text x="%.1f" y="%.1f" font-size="10.5" fill="%s" '
                         'text-anchor="middle">%s</text>'
                         % (x + bw * 0.46, top - 5, DARK, esc(yfmt.format(v))))
    # 参考线画在柱子【之后】，否则会被柱子盖住
    if hline is not None:
        y = yp(hline)
        s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.4" '
                 'stroke-dasharray="5,4"/>' % (pl, y, pl + iw, y, ORANGE))
        s.append('<text x="%d" y="%.1f" font-size="10.5" fill="%s" text-anchor="start">%s</text>'
                 % (pl + 4, y - 5, ORANGE, esc(yfmt.format(hline))))
    if hlines:
        for j, (hv, htxt, hc) in enumerate(hlines):
            y = yp(hv)
            s.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1.3" '
                     'stroke-dasharray="6,4"/>' % (pl, y, pl + iw, y, hc))
            tw = sum(1.02 if ord(ch) > 0x2E80 else 0.56 for ch in htxt) * 10.5
            tx = pl + iw - 4 - tw
            s.append('<rect x="%.1f" y="%.1f" width="%.1f" height="14" fill="#ffffff" '
                     'fill-opacity="0.86" rx="3"/>' % (tx - 3, y - 14, tw + 6))
            s.append('<text x="%d" y="%.1f" font-size="10.5" fill="%s" text-anchor="end">%s</text>'
                     % (pl + iw - 4, y - 5, hc, esc(htxt)))
    for i, c in enumerate(cats):
        s.append('<text x="%.1f" y="%d" font-size="11" fill="%s" text-anchor="middle">%s</text>'
                 % (pl + gw * i + gw / 2, H - pb + 18, DARK, esc(c)))
    if ylab:
        s.append('<text x="%d" y="%d" font-size="11" fill="%s">%s</text>'
                 % (pl, pt - 12, GREY, esc(ylab)))
    ly = pt + 4
    for label, vals, color in series:
        s.append('<rect x="%d" y="%d" width="12" height="12" fill="%s" rx="2"/>'
                 % (pl + iw + 14, ly - 10, color))
        s.append('<text x="%d" y="%d" font-size="11.5" fill="%s">%s</text>'
                 % (pl + iw + 32, ly, DARK, esc(label)))
        ly += 20
    s.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#cfd8e3"/>'
             % (pl, pt, iw, ih))
    s.append("</svg>")
    return "".join(s)
