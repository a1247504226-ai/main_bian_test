# -*- coding: utf-8 -*-
"""svgkit 的补充图型：数值 x 轴折线图、散点图。"""
from svgkit import esc, _nice_ticks


def curve_chart(xvals, series, W=1080, H=300, title="", xlabel="", ylabel="",
                colors=None, hline=None, hline_label="", xfmt="{:.0f}",
                yfmt="{:.2f}", pad=(64, 150, 44, 46)):
    """数值 x 轴折线图（参数网格用）。series: [(name, values)]"""
    pl, pr, pt, pb = pad
    iw, ih = W - pl - pr, H - pt - pb
    allv = [v for _, vs in series for v in vs]
    if hline is not None:
        allv = allv + [hline]
    lo, hi = min(allv), max(allv)
    span = (hi - lo) or 1
    lo -= span * 0.10
    hi += span * 0.10
    ticks = _nice_ticks(lo, hi, 5)
    lo, hi = ticks[0], ticks[-1]
    x0, x1 = min(xvals), max(xvals)

    def xp(x):
        return pl + (x - x0) / ((x1 - x0) or 1) * iw

    def yp(v):
        return pt + ih - (v - lo) / (hi - lo) * ih

    colors = colors or ["#c0392b", "#7f8c8d"]
    p = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
         'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">' % (W, H)]
    p.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    if title:
        p.append('<text x="%d" y="24" font-size="14" font-weight="700" fill="#15171a">%s</text>'
                 % (pl, esc(title)))
    for v in ticks:
        y = yp(v)
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#eceef1"/>' % (pl, y, pl + iw, y))
        p.append('<text x="%d" y="%.1f" font-size="10" fill="#6b7280" text-anchor="end">%s</text>'
                 % (pl - 8, y + 4, yfmt.format(v)))
    p.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#d0d4da"/>'
             % (pl, pt + ih, pl + iw, pt + ih))
    for x in xvals:
        p.append('<text x="%.0f" y="%d" font-size="10" fill="#374151" text-anchor="middle">%s</text>'
                 % (xp(x), pt + ih + 17, xfmt.format(x)))
    if xlabel:
        p.append('<text x="%.0f" y="%d" font-size="11" fill="#6b7280" text-anchor="middle">%s</text>'
                 % (pl + iw / 2, H - 6, esc(xlabel)))
    if hline is not None:
        y = yp(hline)
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#111827" '
                 'stroke-dasharray="5 4" stroke-width="1.2"/>' % (pl, y, pl + iw, y))
        if hline_label:
            p.append('<text x="%d" y="%.1f" font-size="10" fill="#111827" text-anchor="end">%s</text>'
                     % (pl + iw - 4, y - 7, esc(hline_label)))
    for k, (name, vs) in enumerate(series):
        pts = " ".join("%.1f,%.1f" % (xp(xvals[i]), yp(v)) for i, v in enumerate(vs))
        p.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2" '
                 'stroke-linejoin="round"/>' % (pts, colors[k % len(colors)]))
        for i, v in enumerate(vs):
            p.append('<circle cx="%.1f" cy="%.1f" r="3.4" fill="#fff" stroke="%s" stroke-width="1.8"/>'
                     % (xp(xvals[i]), yp(v), colors[k % len(colors)]))
    lx = pl + iw + 16
    for k, (name, vs) in enumerate(series):
        ly = pt + 8 + k * 22
        p.append('<rect x="%d" y="%d" width="12" height="3" rx="1.5" fill="%s"/>'
                 % (lx, ly - 8, colors[k % len(colors)]))
        p.append('<text x="%d" y="%d" font-size="11" fill="#374151">%s</text>'
                 % (lx + 18, ly - 3, esc(name)))
    p.append('</svg>')
    return "".join(p)


def scatter_chart(pts, W=1080, H=380, title="", xlabel="", ylabel="",
                  pad=(64, 150, 44, 46), hline=None, hline_label="",
                  xfmt="{:.0f}%", yfmt="{:.2f}", hi_idx=None, hi_color="#c0392b",
                  lo_color="#8a94a6", annotate=None):
    """散点图。pts = [(x, y, label)]；hi_idx = 高亮的点索引集合"""
    pl, pr, pt, pb = pad
    iw, ih = W - pl - pr, H - pt - pb
    xs = [a for a, _, _ in pts]
    ys = [b for _, b, _ in pts]
    if hline is not None:
        ys = ys + [hline]
    xt = _nice_ticks(min(xs), max(xs), 6)
    yt = _nice_ticks(min(ys), max(ys), 6)
    xlo, xhi = xt[0], xt[-1]
    ylo, yhi = yt[0], yt[-1]

    def xp(v):
        return pl + (v - xlo) / ((xhi - xlo) or 1) * iw

    def yp(v):
        return pt + ih - (v - ylo) / ((yhi - ylo) or 1) * ih

    p = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
         'font-family="-apple-system,Segoe UI,Microsoft YaHei,sans-serif">' % (W, H)]
    p.append('<rect width="%d" height="%d" fill="#ffffff"/>' % (W, H))
    if title:
        p.append('<text x="%d" y="22" font-size="14" font-weight="700" fill="#15171a">%s</text>'
                 % (pl, esc(title)))
    for v in yt:
        y = yp(v)
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#eceef1"/>' % (pl, y, pl + iw, y))
        p.append('<text x="%d" y="%.1f" font-size="10" fill="#6b7280" text-anchor="end">%s</text>'
                 % (pl - 8, y + 4, yfmt.format(v)))
    for v in xt:
        p.append('<text x="%.0f" y="%d" font-size="10" fill="#374151" text-anchor="middle">%s</text>'
                 % (xp(v), pt + ih + 17, xfmt.format(v)))
    p.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#d0d4da"/>'
             % (pl, pt + ih, pl + iw, pt + ih))
    p.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#d0d4da"/>'
             % (pl, pt, pl, pt + ih))
    if hline is not None:
        y = yp(hline)
        p.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#111827" '
                 'stroke-dasharray="5 4" stroke-width="1.2"/>' % (pl, y, pl + iw, y))
        if hline_label:
            p.append('<text x="%d" y="%.1f" font-size="10" fill="#111827" text-anchor="end">%s</text>'
                     % (pl + iw - 4, y - 6, esc(hline_label)))
    for i, (a, b, lab) in enumerate(pts):
        hot = hi_idx is not None and i in hi_idx
        col = hi_color if hot else lo_color
        r = 5.0 if hot else 3.0
        p.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="%s" fill-opacity="%.2f"/>'
                 % (xp(a), yp(b), r, col, 0.85 if hot else 0.6))
        if annotate and lab in annotate:
            cx = xp(a)
            if cx > pl + iw - 58:                      # 靠右边界，标签改画在左边
                p.append('<text x="%.1f" y="%.1f" font-size="9.5" fill="#374151" '
                         'text-anchor="end">%s</text>' % (cx - 7, yp(b) + 3.5, esc(lab)))
            else:
                p.append('<text x="%.1f" y="%.1f" font-size="9.5" fill="#374151">%s</text>'
                         % (cx + 7, yp(b) + 3.5, esc(lab)))
    if xlabel:
        p.append('<text x="%.0f" y="%d" font-size="11" fill="#6b7280" text-anchor="middle">%s</text>'
                 % (pl + iw / 2, H - 6, esc(xlabel)))
    if ylabel:
        p.append('<text x="%d" y="%d" font-size="11" fill="#6b7280">%s</text>'
                 % (pl - 46, pt - 14, esc(ylabel)))
    p.append('</svg>')
    return "".join(p)
