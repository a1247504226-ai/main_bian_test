# -*- coding: utf-8 -*-
"""生成第二轮研究的报告数据 report_data.json（供 build_report2.py 使用）。"""
import os, io, json, statistics, math
import ft
import research2 as R

HERE = ft.HERE


def cum_eq(dates, rets, init=1.0):
    eq, out = init, []
    for d, r in zip(dates, rets):
        eq *= (1 + r)
        out.append([d, round(eq, 5)])
    return out


def downsample(curve, step=5):
    """净值曲线降采样，避免 HTML 过大。"""
    if len(curve) <= 2:
        return curve
    return curve[::step] + ([curve[-1]] if (len(curve) - 1) % step else [])


def main():
    syms = ft.universe()
    print("构建面板…")
    P = R.build_panel(syms)
    print("面板 %s ~ %s，%d 品种" % (P["dates"][0], P["dates"][-1], len(P["syms"])))

    D = {}
    D["panel"] = {"start": P["dates"][0], "end": P["dates"][-1],
                  "days": len(P["dates"]), "syms": P["syms"]}

    # ── 1. 横截面 carry 参数网格 ────────────────────────────────
    print("跑 XC-carry 参数网格…")
    grid = []
    for win in [126, 252, 504]:
        for K in [2, 3, 5]:
            d, r, t = R.run_weights(P, R.make_xcarry(P, win=win, K=K))
            s = R.perf(r)
            grid.append({"win": win, "K": K, "cagr": s["cagr"], "mdd": s["mdd"],
                         "calmar": s["calmar"], "sharpe": s["sharpe"],
                         "win_act": s["win_act"]})
            print("  win=%d K=%d  年化 %.2f%%  Calmar %.2f" % (win, K, s["cagr"] * 100, s["calmar"]))
    D["xcarry_grid"] = grid

    # ── 2. 最优配置深度验证 ───────────────────────────────────
    print("深度验证 XC-carry(252,5)…")
    wfn = R.make_xcarry(P, win=252, K=5)
    d, r, t = R.run_weights(P, wfn)
    s = R.perf(r)

    by_year = {}
    for y in sorted({x[:4] for x in d}):
        sub = [(dd, x) for dd, x in zip(d, r) if dd[:4] == y]
        if len(sub) < 20:
            continue
        by_year[y] = R.perf([x for _, x in sub])["cagr"]

    inn = R.seg(d, r, "2009-01-01", "2018-12-31")
    out = R.seg(d, r, "2019-01-01", "2099-12-31")
    b = R.boot(d, r)

    dl, rl, _ = R.run_weights(P, R.make_xcarry(P, win=252, K=5, side="long"))
    ds, rs, _ = R.run_weights(P, R.make_xcarry(P, win=252, K=5, side="short"))
    sl, ss = R.perf(rl), R.perf(rs)

    reb = {}
    for rb in [1, 5, 10, 21]:
        d2, r2, t2 = R.run_weights(P, R.make_xcarry(P, win=252, K=5), rebal=rb)
        s2 = R.perf(r2)
        reb[str(rb)] = {"cagr": s2["cagr"], "mdd": s2["mdd"], "calmar": s2["calmar"],
                        "turn": statistics.mean(t2)}

    D["xc"] = {
        "stat": s, "turn": statistics.mean(t),
        "by_year": by_year, "in": inn, "out": out, "boot": b,
        "long": sl, "short": ss, "rebal": reb,
    }

    # ── 3. 与现有 FG+HC 策略的关系 ────────────────────────────
    print("对照 FG+HC 与组合…")
    d3, r3, c3 = ft.run_portfolio(["FG", "HC"], ft.sig_carry_trend)
    st3 = ft.stats(d3, r3, c3)
    cd, cr1, cr2 = R._align(d, r, d3, r3)
    rho = R._corr(cr1, cr2)
    combos = {}
    for wgt in [0.3, 0.5, 0.7]:
        mix = [(1 - wgt) * a + wgt * b2 for a, b2 in zip(cr1, cr2)]
        sc = R.perf(mix)
        combos[str(wgt)] = sc
    D["fghc"] = st3
    D["corr"] = rho
    D["combos"] = combos

    # ── 4. 三条净值曲线（降采样）──────────────────────────────
    D["curves"] = {
        "xc": downsample(cum_eq(cd, cr1)),
        "fghc": downsample(cum_eq(cd, cr2)),
        "mix50": downsample(cum_eq(cd, [(a + b2) / 2 for a, b2 in zip(cr1, cr2)])),
    }
    D["curve_range"] = [cd[0], cd[-1]]

    # ── 5. 策略族横评（来自 scan.json）────────────────────────
    sp = os.path.join(HERE, "scan.json")
    if os.path.exists(sp):
        with io.open(sp, encoding="utf-8") as f:
            D["scan"] = json.load(f)
    print("scan 信号数:", len(D.get("scan", {})))

    # ── 6. 品种归因（来自 rank.json）──────────────────────────
    rp = os.path.join(HERE, "rank.json")
    if os.path.exists(rp):
        with io.open(rp, encoding="utf-8") as f:
            rk = json.load(f)
        D["rank"] = rk["rows"]
    print("rank 品种数:", len(D.get("rank", [])))

    # ── 7. 配对结果 ──────────────────────────────────────────
    print("跑配对…")
    pairs = []
    for a, b2, desc in R.PAIRS:
        if a not in P["syms"] or b2 not in P["syms"]:
            continue
        dd, rr, tt = R.run_pair(P, a, b2)
        ss2 = R.perf(rr)
        pairs.append({"pair": "%s-%s" % (a, b2), "desc": desc,
                      "cagr": ss2["cagr"], "mdd": ss2["mdd"],
                      "calmar": ss2["calmar"], "sharpe": ss2["sharpe"],
                      "win_act": ss2["win_act"], "active": ss2["active"]})
    D["pairs"] = pairs

    # ── 8. 横截面动量网格 ────────────────────────────────────
    print("跑 XC-mom…")
    xm = []
    for N in [20, 60, 120, 250]:
        for K in [2, 3, 5]:
            dd, rr, tt = R.run_weights(P, R.make_xmom(P, N=N, K=K))
            ss3 = R.perf(rr)
            xm.append({"N": N, "K": K, "cagr": ss3["cagr"], "mdd": ss3["mdd"],
                       "calmar": ss3["calmar"], "sharpe": ss3["sharpe"]})
    D["xmom"] = xm

    # ── 9. 资金阶梯：各 K 档收益 vs 保证金 ───────────────────
    print("算资金阶梯…")
    ladder = []
    for K in [1, 2, 3, 4, 5]:
        wf = R.make_xcarry(P, 252, K)
        dd, rr, tt = R.run_weights(P, wf)
        ss4 = R.perf(rr)
        ms = []
        for i in range(300, len(P["dates"]), 5):
            w = wf(i - 1)
            if not w:
                continue
            tot, ok = 0.0, True
            for sy in w:
                p = P["px"][sy][i - 1]
                if not p:
                    ok = False
                    break
                m, mr = ft.SPEC[sy]
                tot += p * m * (mr + 0.04)
            if ok:
                ms.append(tot)
        ms.sort()
        med = ms[len(ms) // 2] if ms else 0.0
        ladder.append({"K": K, "legs": 2 * K, "cagr": ss4["cagr"], "mdd": ss4["mdd"],
                       "calmar": ss4["calmar"], "sharpe": ss4["sharpe"],
                       "margin_med": med, "capital": med / 0.5})
        print("  K=%d 腿%d  年化 %.2f%%  保证金中位 %s" % (K, 2 * K, ss4["cagr"] * 100, format(med, ",.0f")))
    D["ladder"] = ladder

    with io.open(os.path.join(HERE, "report_data.json"), "w", encoding="utf-8") as f:
        json.dump(D, f, ensure_ascii=False)
    print("\n已写出 report_data.json  (%.0f KB)"
          % (os.path.getsize(os.path.join(HERE, "report_data.json")) / 1024))


if __name__ == "__main__":
    main()
