"""
批量下载币安全部 USDT 现货交易对的日线数据。
排除：杠杆代币(UP/DOWN/BULL/BEAR)、稳定币/法币计价对。
"""
import json, os, time, urllib.request, urllib.error

BASE = "https://data-api.binance.vision/api/v3"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0"}


def get(url, tries=5, timeout=90):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if k == tries - 1:
                raise
            time.sleep(3 + k * 4)
    return None


print("[1/3] 获取交易对列表 ...", flush=True)
info = get(f"{BASE}/exchangeInfo")
syms = info["symbols"]
print(f"    总交易对 {len(syms)}", flush=True)

bad = ("UP", "DOWN", "BULL", "BEAR")
stable = {"USDC", "BUSD", "FDUSD", "TUSD", "USDP", "DAI", "AEUR", "USD1",
          "BFUSD", "PAXG", "EUR", "GBP", "JPY", "TRY", "BRL", "ZAR", "RON",
          "PLN", "CZK", "MXN", "COP", "ARS", "UAH", "NGN", "IDRT", "VAI"}
cand = []
for s in syms:
    if str(s.get("status")) != "TRADING":
        continue
    if not s.get("isSpotTradingAllowed", False):
        continue
    if s.get("quoteAsset") != "USDT":
        continue
    b = s["baseAsset"]
    if b.endswith(bad) or b in stable:
        continue
    cand.append(s["symbol"])
cand.sort()
print(f"    候选交易对 {len(cand)}（已排除杠杆代币与稳定币）", flush=True)
json.dump(cand, open(os.path.join(OUT, "symbols_candidate.json"), "w"))

print("[2/3] 下载日线 K 线（并发）...", flush=True)
start_ms = int(time.mktime(time.strptime("2017-01-01", "%Y-%m-%d"))) * 1000
end_ms = int(time.time() * 1000)
ok, skip, fail = [], [], []
t0 = time.time()
lock = __import__("threading").Lock()


def fetch_one(sym):
    path = os.path.join(OUT, f"{sym}_1d.csv")
    if os.path.exists(path) and os.path.getsize(path) > 2000:
        with lock:
            skip.append(sym)
        return
    rows, cur = [], start_ms
    try:
        while cur < end_ms:
            url = (f"{BASE}/klines?symbol={sym}&interval=1d"
                   f"&startTime={cur}&endTime={end_ms}&limit=1000")
            batch = get(url, tries=3, timeout=45)
            if not batch:
                break
            rows.extend(batch)
            cur = batch[-1][0] + 1
            if len(batch) < 1000:
                break
            time.sleep(0.08)
    except Exception as e:
        with lock:
            fail.append((sym, str(e)[:60]))
        return

    if len(rows) < 300:          # 数据太少，没有回测价值
        with lock:
            fail.append((sym, f"仅{len(rows)}根"))
        return

    seen, out = set(), []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append(r)
    out.sort(key=lambda x: x[0])
    with open(path, "w", encoding="utf-8") as f:
        f.write("datetime,open,high,low,close,volume\n")
        for r in out:
            ds = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r[0] / 1000))
            f.write(f"{ds},{float(r[1])},{float(r[2])},{float(r[3])},{float(r[4])},{float(r[5])}\n")
    with lock:
        ok.append(sym)


from concurrent.futures import ThreadPoolExecutor, as_completed
done = 0
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(fetch_one, s): s for s in cand}
    for _ in as_completed(futs):
        done += 1
        if done % 100 == 0:
            with lock:
                print(f"    {done}/{len(cand)}  已存 {len(ok)}  跳过 {len(skip)}  失败 {len(fail)}"
                      f"  用时 {(time.time()-t0)/60:.1f} 分", flush=True)

print(f"[3/3] 完成：成功 {len(ok)}，跳过 {len(skip)}，失败 {len(fail)}，"
      f"总用时 {(time.time()-t0)/60:.1f} 分", flush=True)
json.dump({"ok": ok, "skip": skip, "fail": fail},
          open(os.path.join(OUT, "fetch_result.json"), "w"))
if fail:
    print("失败样例:", fail[:10], flush=True)
