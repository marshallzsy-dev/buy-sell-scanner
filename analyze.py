"""
analyze.py
==========
基于 state.json 的历史快照（git 版本），离线分析 S1 信号的重绘（消失）特征。

用法：
  python analyze.py          打印「重绘消失率 by 信号年龄」分析表
  python analyze.py --seed   把历史统计写入 state.json 的 repaint_stats，
                             让 dashboard 首次上线就有内容（之后由 scan.py 每日续累）

说明：
  - 重绘分析复用 scan.py 里的 tally_repaint / 分桶逻辑，保证与云端每日累积口径一致。
  - 前向收益（信号出现后的价格表现）需要更长的数据窗口 + 价格数据，
    数据攒够后会在本文件补上 forward-return 分析。
"""
from __future__ import annotations
import subprocess
import json
import sys
import os

import scan  # 复用分桶与统计逻辑

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, "state.json")


def _load_history():
    """按 run_date 去重，返回 (按时间排序的 tickers 快照列表, 日期列表)。"""
    shas = subprocess.run(
        ["git", "log", "--reverse", "--pretty=%H", "--", "state.json"],
        cwd=BASE, capture_output=True, text=True,
    ).stdout.split()
    snaps = {}
    for sha in shas:
        try:
            blob = subprocess.run(["git", "show", f"{sha}:state.json"],
                                  cwd=BASE, capture_output=True, text=True).stdout
            tk = json.loads(blob).get("tickers", {})
            rd = next((v.get("run_date") for v in tk.values() if v.get("run_date")), None)
            if rd:
                snaps[rd] = tk       # 同一天多次 commit 取最后一次
        except Exception:
            pass
    dates = sorted(snaps)
    return [snaps[d] for d in dates], dates


def compute_from_history():
    """对相邻交易日快照两两做重绘统计并累加，返回 (buckets, dates)。"""
    snaps, dates = _load_history()
    buckets = {}
    for a, b in zip(snaps, snaps[1:]):
        for k, (tot, gone) in scan.tally_repaint(a, b).items():
            c = buckets.get(k, [0, 0])
            buckets[k] = [c[0] + tot, c[1] + gone]
    return buckets, dates


def forward_all(hold=5, rt_min_bars=60):
    """一次抓数，算「出信号→次日开盘入场→持有 hold 交易日→末日收盘平仓」的收益率。
      B（做多）：(exit_close - entry_open)/entry_open
      S（做空）：(entry_open - exit_close)/entry_open
    同时给出两口径：
      repainted 全量：用当前全量历史算出的（已重绘定型）全部信号——含 lookahead，乐观上限。
      realtime 当天：walk-forward 逐根截断重算，只取「该K线为最新时当场就出的」信号——
                     实时真能下单的，剥掉了事后重绘冒出来的点，接近真实。
    base：任意日次日开盘做多持有 hold 日，作对照。"""
    tickers = scan.load_universe()
    data = scan.fetch_all(tickers)

    rp_b, rp_s, base, rt_b, rt_s = [], [], [], [], []
    for t, df in data.items():
        opens = [float(x) for x in df["Open"].tolist()]
        closes = [float(x) for x in df["Close"].tolist()]
        n = len(df)
        if n != len(opens) or n < rt_min_bars + hold + 2:
            continue

        # --- repainted 全量 + 基线 ---
        try:
            full = scan.compute_signals(df)
        except Exception:
            full = None
        if full and len(full["dates"]) == n:
            idx = {d: i for i, d in enumerate(full["dates"])}
            for i in range(0, n - hold - 1):
                eo = opens[i + 1]
                if eo:
                    base.append((closes[i + hold] - eo) / eo * 100)
            for d in full["b_dates"]:
                i = idx.get(d)
                if i is not None and i + hold < n and opens[i + 1]:
                    rp_b.append((closes[i + hold] - opens[i + 1]) / opens[i + 1] * 100)
            for d in full["s_dates"]:
                i = idx.get(d)
                if i is not None and i + hold < n and opens[i + 1]:
                    rp_s.append((opens[i + 1] - closes[i + hold]) / opens[i + 1] * 100)

        # --- realtime 当天：逐根截断重算，看最新那根是否当场出信号 ---
        for i in range(rt_min_bars - 1, n - hold - 1):
            eo = opens[i + 1]
            if not eo:
                continue
            try:
                cur = scan.compute_signals(df.iloc[:i + 1])
            except Exception:
                continue
            if not cur["dates"]:
                continue
            last = cur["dates"][-1]
            if last in set(cur["b_dates"]):
                rt_b.append((closes[i + hold] - eo) / eo * 100)
            if last in set(cur["s_dates"]):
                rt_s.append((eo - closes[i + hold]) / eo * 100)

    return dict(rp_b=rp_b, rp_s=rp_s, base=base, rt_b=rt_b, rt_s=rt_s,
                ok=len(data), tot=len(tickers))


def _stats(name, arr):
    import statistics as st
    if not arr:
        print(f"{name:<24} 无样本")
        return
    n = len(arr)
    mean = sum(arr) / n
    med = st.median(arr)
    win = sum(1 for x in arr if x > 0) / n * 100
    print(f"{name:<24}{n:>7}{mean:>9.2f}%{med:>9.2f}%{win:>8.1f}%")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if "--forward" in sys.argv:
        args = sys.argv[sys.argv.index("--forward") + 1:]
        hold = int(args[0]) if args and args[0].isdigit() else 5
        r = forward_all(hold)
        print(f"\n== 出信号→次日开盘入场→持有 {hold} 个交易日→末日收盘平仓 ==")
        print(f"数据覆盖：{r['ok']}/{r['tot']} 只\n")
        hdr = f"{'':<24}{'样本':>7}{'平均':>10}{'中位':>10}{'胜率':>9}"
        print("【realtime 当天出的信号 · 实时可交易，剥掉重绘】")
        print(hdr)
        _stats("  B 买点 (做多)", r["rt_b"])
        _stats("  S 卖点 (做空)", r["rt_s"])
        print("\n【repainted 全量历史信号 · 含 lookahead，乐观上限】")
        print(hdr)
        _stats("  B 买点 (做多)", r["rp_b"])
        _stats("  S 卖点 (做空)", r["rp_s"])
        print("\n【基线对照】")
        print(hdr)
        _stats("  任意日做多", r["base"])
        print("\n注：realtime 组才是接近真实的收益（信号在该K线为最新时当场就出）；")
        print("    repainted 组把事后重绘冒出来的点也算进去，会显著高估。均未扣手续费/滑点。")
        return

    buckets, dates = compute_from_history()

    if "--seed" in sys.argv:
        if len(dates) < 2:
            print("历史快照不足两天，无法 seed。")
            return
        with open(STATE, "r", encoding="utf-8") as f:
            state = json.load(f)
        state["repaint_stats"] = {
            "buckets": buckets,
            "transitions": len(dates) - 1,
            "since": dates[0],
            "updated": dates[-1],
        }
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        print(f"已写入 repaint_stats：{len(dates)-1} 个交易日转换，区间 {dates[0]} ~ {dates[-1]}")
        return

    print(f"分析用交易日快照（{len(dates)} 天）：{dates}\n")
    print("== 信号重绘消失率 · 按信号年龄分桶 ==")
    print(f"{'年龄':<14}{'样本':>8}{'消失':>8}{'消失率':>10}")
    tin = tg = 0
    for k in scan.REPAINT_BUCKET_ORDER:
        if k in buckets:
            n, g = buckets[k]
            tin += n; tg += g
            print(f"{scan.BUCKET_LABELS[k]:<14}{n:>8}{g:>8}{g/n*100:>9.1f}%")
    if tin:
        print(f"{'合计':<14}{tin:>8}{tg:>8}{tg/tin*100:>9.1f}%")
    print("\n[前向收益] 数据窗口不足（信号出现后尚无足够交易日），待累积后补充。")


if __name__ == "__main__":
    main()
