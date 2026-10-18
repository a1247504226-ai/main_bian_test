# -*- coding: utf-8 -*-
"""自绘 SVG 图表工具（无外部依赖）。"""
import math


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _nice_log_ticks(lo, hi):
    """lo/hi 是 log10 值，返回实际值刻度。"""
    out = []
    for k in range(int(math.floor(lo)), int(math.ceil(hi)) + 1):
        for m in (1, 2, 5):
            v = m * 10 ** k
            if lo <= math.log10(v) <= hi:
                out.append(v)
    return out


def _fmt(v):
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:.0f}"
    if v >= 1:
        return f"{v:.1f}"
    return f"{v:.2f}"


def line_chart(series, dates, W=1080, H=420, logy=True, title="", ylabel="",
               pad=(64, 150, 46, 42), colors=None, ref=None):
    """series: [(name, values)]；dates: 日期数组（用于 x 轴标签）"""
    pl, pr, pt, pb = pad
    iw, ih = W - pl - pr, H - pt - pb
    allv = [v for _, vs in series for v in vs if v and v > 0]
    if logy:
        lo, hi = math.log10(min(allv)), math.log10(max(allv))
        span = hi - lo or 1
        lo -= span * 0.04
        hi += span * 0.06
        def yp(v):
            return pt + ih - (math.log10(max(v, 10 ** lo)) - lo) / (hi - lo) * ih
        ticks = _nice_log_ticks(lo, hi)
    else:
        lo, hi = min(allv), max(allv)
        span = hi - lo or 1
        lo -= span * 0.05
        hi += span * 0.06
        def yp(v):
            return pt + ih - (v - lo) / (hi - lo) * ih
        ticks = [lo + (hi - lo) * k / 5 for k in range(6)]

    n = len(series[0][1])
    def xp(i):
        return pl + (i / max(n - 1, 1)) * iw

    colors = colors or ["#c0392b", "#d98880", "#e67e22", "#2c3e50"]
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    if title:
        p.append(f'<text x="{pl}" y="24" font-size="15" font-weight="700" fill="#15171a">{esc(title)}</text>')
    # 网格 + y 轴
    for t in ticks:
        y = yp(t)
        if not (pt - 1 <= y <= pt + ih + 1):
            continue
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+iw}" y2="{y:.1f}" '
                 f'stroke="#eceef1" stroke-width="1"/>')
        p.append(f'<text x="{pl-8}" y="{y+4:.1f}" font-size="11" fill="#6b7280" '
                 f'text-anchor="end">{_fmt(t)}</text>')
    p.append(f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{pt+ih}" stroke="#d0d4da"/>')
    p.append(f'<line x1="{pl}" y1="{pt+ih}" x2="{pl+iw}" y2="{pt+ih}" stroke="#d0d4da"/>')
    if ylabel:
        p.append(f'<text x="{pl-46}" y="{pt-14}" font-size="11" fill="#6b7280">{esc(ylabel)}</text>')
    # x 轴标签（等距抽样，最多 12 个）
    step = max(1, math.ceil(n / 12))
    for i in range(0, n, step):
        d = str(dates[i])[:7]
        p.append(f'<text x="{xp(i):.0f}" y="{pt+ih+18}" font-size="10" fill="#6b7280" '
                 f'text-anchor="middle">{esc(d)}</text>')
    # clip
    cid = f"clip{abs(hash(title)) % 99999}"
    p.append(f'<clipPath id="{cid}"><rect x="{pl}" y="{pt}" width="{iw}" height="{ih}"/></clipPath>')
    p.append(f'<g clip-path="url(#{cid})">')
    if ref:
        y = yp(ref[0])
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+iw}" y2="{y:.1f}" '
                 f'stroke="#9ca3af" stroke-dasharray="4 4"/>')
    for k, (name, vs) in enumerate(series):
        pts = " ".join(f"{xp(i):.1f},{yp(v):.1f}" for i, v in enumerate(vs) if v > 0)
        p.append(f'<polyline points="{pts}" fill="none" stroke="{colors[k % len(colors)]}" '
                 f'stroke-width="2" stroke-linejoin="round"/>')
    p.append('</g>')
    if ref:
        p.append(f'<text x="{pl+iw-4}" y="{yp(ref[0])-6:.1f}" font-size="10" fill="#9ca3af" '
                 f'text-anchor="end">{esc(ref[1])}</text>')
    # 图例
    lx = pl + iw + 16
    for k, (name, vs) in enumerate(series):
        ly = pt + 8 + k * 22
        p.append(f'<rect x="{lx}" y="{ly-8}" width="12" height="3" rx="1.5" '
                 f'fill="{colors[k % len(colors)]}"/>')
        p.append(f'<text x="{lx+18}" y="{ly-3}" font-size="11" fill="#374151">{esc(name)}</text>')
    p.append('</svg>')
    return "".join(p)


def area_chart(values, dates, W=1080, H=260, title="", color="#1e8449",
               pad=(64, 24, 40, 38), ymax=0.0, ymin=None, nlab=12):
    pl, pr, pt, pb = pad
    iw, ih = W - pl - pr, H - pt - pb
    lo = ymin if ymin is not None else min(values)
    lo = min(lo, -0.01)
    ticks = _nice_ticks(lo, ymax, 5)
    lo, hi = ticks[0], ticks[-1]
    span = (hi - lo) or 1

    def yp(v):
        return pt + ih - (v - lo) / span * ih
    n = len(values)

    def xp(i):
        return pl + (i / max(n - 1, 1)) * iw
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    if title:
        p.append(f'<text x="{pl}" y="22" font-size="14" font-weight="700" fill="#15171a">{esc(title)}</text>')
    for v in ticks:
        y = yp(v)
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+iw}" y2="{y:.1f}" stroke="#eceef1"/>')
        p.append(f'<text x="{pl-8}" y="{y+4:.1f}" font-size="10" fill="#6b7280" '
                 f'text-anchor="end">{v*100:.0f}%</text>')
    pts = " ".join(f"{xp(i):.1f},{yp(v):.1f}" for i, v in enumerate(values))
    p.append(f'<polygon points="{pl},{yp(0):.1f} {pts} {pl+iw},{yp(0):.1f}" '
             f'fill="{color}" fill-opacity="0.18"/>')
    p.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"/>')
    step = max(1, math.ceil(n / nlab))
    for i in range(0, n, step):
        p.append(f'<text x="{xp(i):.0f}" y="{pt+ih+16}" font-size="10" fill="#6b7280" '
                 f'text-anchor="middle">{esc(str(dates[i])[:7])}</text>')
    p.append('</svg>')
    return "".join(p)


def _nice_ticks(lo, hi, k=5):
    """返回覆盖 [lo,hi] 的整齐刻度（步长取 1/2/2.5/5/10 × 10^n）。"""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / k
    mag = 10 ** math.floor(math.log10(raw))
    step = 10 * mag
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            step = m * mag
            break
    start = math.floor(lo / step) * step
    end = math.ceil(hi / step) * step
    n = int(round((end - start) / step))
    return [start + i * step for i in range(n + 1)]


def bar_chart(labels, series, W=1080, H=320, title="", yfmt="{:.0f}%",
              colors=None, pad=(56, 24, 44, 44), hline=None, hline_label=""):
    pl, pr, pt, pb = pad
    iw, ih = W - pl - pr, H - pt - pb
    allv = [v for _, vs in series for v in vs]
    if hline is not None:
        allv = allv + [hline]
    lo, hi = min(0, min(allv)), max(allv)
    span = (hi - lo) or 1
    lo = lo - span * 0.05 if lo < 0 else 0.0
    hi += span * 0.10
    ticks = _nice_ticks(lo, hi, 5)
    lo, hi = ticks[0], ticks[-1]
    span = hi - lo

    def yp(v):
        return pt + ih - (v - lo) / span * ih
    n = len(labels)
    gw = iw / n
    bw = gw * 0.70 / len(series)
    colors = colors or ["#c0392b", "#7f8c8d", "#2980b9", "#e67e22"]
    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">']
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    if title:
        p.append(f'<text x="{pl}" y="24" font-size="14" font-weight="700" fill="#15171a">{esc(title)}</text>')
    for v in ticks:
        y = yp(v)
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+iw}" y2="{y:.1f}" stroke="#eceef1"/>')
        p.append(f'<text x="{pl-8}" y="{y+4:.1f}" font-size="10" fill="#6b7280" '
                 f'text-anchor="end">{yfmt.format(v)}</text>')
    for i, lab in enumerate(labels):
        x0 = pl + i * gw
        p.append(f'<text x="{x0+gw/2:.0f}" y="{pt+ih+17}" font-size="10" fill="#374151" '
                 f'text-anchor="middle">{esc(lab)}</text>')
        start = x0 + (gw - bw * len(series)) / 2      # 柱子组居中于标签
        for k, (name, vs) in enumerate(series):
            v = vs[i]
            x = start + k * bw
            y0, y1 = yp(0), yp(v)
            top, hgt = min(y0, y1), abs(y1 - y0)
            p.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw*0.88:.1f}" '
                     f'height="{max(hgt,0.6):.1f}" fill="{colors[k % len(colors)]}" rx="1.5"/>')
    if hline is not None:
        y = yp(hline)
        p.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{pl+iw}" y2="{y:.1f}" '
                 f'stroke="#111827" stroke-dasharray="5 4" stroke-width="1.2"/>')
        if hline_label:
            p.append(f'<text x="{pl+6}" y="{y-6:.1f}" font-size="10" fill="#111827">{esc(hline_label)}</text>')
    # 图例放右上，避免与 x 轴标签重叠
    lw = 150
    lx = W - pr - lw * len(series)
    for k, (name, vs) in enumerate(series):
        x = lx + k * lw
        p.append(f'<rect x="{x}" y="{16}" width="11" height="11" rx="2" fill="{colors[k % len(colors)]}"/>')
        p.append(f'<text x="{x+15}" y="{25}" font-size="11" fill="#374151">{esc(name)}</text>')
    p.append('</svg>')
    return "".join(p)
