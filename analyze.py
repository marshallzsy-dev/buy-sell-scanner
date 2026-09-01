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


def repaint_in_analysis(rt_min_bars=60):
    """量化「补标」(repaint-in)：一根K线当天(它作为最新K线时)没出信号，
    但过 N 天后被重绘补标上 B/S。方法：walk-forward 逐根重算，记录每个信号
    首次出现时的最新K线 index i，与该信号自身K线 index j 之差 = 补标延迟。
      lag=0：当天就出（实时）；  lag>=1：补标（延迟 lag 个交易日画上去）。
    返回 {side: {'same':n, 'lags':[...]}}，只统计 own_index>=rt_min_bars 的可评估信号。"""
    tickers = scan.load_universe()
    data = scan.fetch_all(tickers)
    res = {"B": {"same": 0, "lags": []}, "S": {"same": 0, "lags": []}}
    for t, df in data.items():
        n = len(df)
        if n < rt_min_bars + 2:
            continue
        try:
            full = scan.compute_signals(df)
        except Exception:
            continue
        if not full["dates"] or len(full["dates"]) != n:
            continue
        gidx = {d: k for k, d in enumerate(full["dates"])}   # 日期 -> 全局 index
        first_seen = {"B": {}, "S": {}}
        for i in range(rt_min_bars - 1, n):
            try:
                cur = scan.compute_signals(df.iloc[:i + 1])
            except Exception:
                continue
            for side, key in (("B", "b_dates"), ("S", "s_dates")):
                for d in cur[key]:
                    if d not in first_seen[side]:
                        first_seen[side][d] = i          # 首次被画上去时的最新K线 index
        for side in ("B", "S"):
            for d, seen_i in first_seen[side].items():
                j = gidx.get(d)
                if j is None or j < rt_min_bars - 1:
                    continue                             # 早期bar无法评估当天
                lag = seen_i - j
                if lag <= 0:
                    res[side]["same"] += 1
                else:
                    res[side]["lags"].append(lag)
    return res, len(data), len(tickers)


def _argval(flag):
    """从命令行取 --flag 后紧跟的数值，取不到或非数值返回 None。"""
    if flag in sys.argv:
        k = sys.argv.index(flag)
        if k + 1 < len(sys.argv):
            try:
                return float(sys.argv[k + 1])
            except ValueError:
                return None
    return None


def _sim_bs(isB, isS, opens, highs, lows, closes, n, min_bars, stop_pct, take_pct):
    """单只股票、单套止损止盈配置的状态机模拟。
      B→次日开盘买入；持仓期每根K线先查止损/止盈（跳空按开盘价、否则按止损/止盈价，
      止损优先），未触发则遇 S 当天收盘卖出。stop_pct/take_pct 为 None 表示不设。
    返回 (rets, holds, reasons, eq_total_pct, left_open)。"""
    rets, holds = [], []
    reasons = {"S": 0, "止损": 0, "止盈": 0}
    pos = False
    entry = 0.0
    entry_i = -1
    stop_lv = target_lv = None
    eq = 1.0
    for i in range(min_bars - 1, n):
        if not pos:
            if isB[i] and i + 1 < n and opens[i + 1]:
                pos = True
                entry = opens[i + 1]
                entry_i = i + 1
                stop_lv = entry * (1 - stop_pct / 100) if stop_pct else None
                target_lv = entry * (1 + take_pct / 100) if take_pct else None
            continue
        exit_price = reason = None
        if i > entry_i:                                  # 跳空：开盘已穿越
            if stop_lv is not None and opens[i] <= stop_lv:
                exit_price, reason = opens[i], "止损"
            elif target_lv is not None and opens[i] >= target_lv:
                exit_price, reason = opens[i], "止盈"
        if exit_price is None:                           # 盘中触及（止损优先）
            if stop_lv is not None and lows[i] <= stop_lv:
                exit_price, reason = stop_lv, "止损"
            elif target_lv is not None and highs[i] >= target_lv:
                exit_price, reason = target_lv, "止盈"
        if exit_price is None and isS[i]:                # S 出场
            exit_price, reason = closes[i], "S"
        if exit_price is not None:
            ret = (exit_price - entry) / entry * 100
            rets.append(ret)
            holds.append(i - entry_i)
            reasons[reason] += 1
            eq *= (1 + ret / 100)
            pos = False
    return rets, holds, reasons, (eq - 1) * 100, (1 if pos else 0)


def _sim_lab(isB, isS, opens, closes, sma_val, n, min_bars, params):
    """通用做多状态机：
      entry：realtime B 次日开盘买入；可选趋势过滤(仅当 B 日收盘 > SMA 才入场)。
      exit ：以下先到者——S 信号(收盘)、或从入场后收盘最高点回撤 trail%(移动止损，收盘触发)。
      不设固定止盈(保住右尾)。params: {trail, sma, use_s}。"""
    trail = params.get("trail")
    sma = params.get("sma")
    use_s = params.get("use_s", True)
    rets, holds = [], []
    reasons = {"S": 0, "移动止损": 0}
    pos = False
    entry = 0.0
    entry_i = -1
    peak = 0.0
    eq = 1.0
    for i in range(min_bars - 1, n):
        if not pos:
            if isB[i] and i + 1 < n and opens[i + 1]:
                if sma is not None:
                    sv = sma_val[i]
                    if sv is None or closes[i] <= sv:
                        continue
                pos = True
                entry = opens[i + 1]
                entry_i = i + 1
                peak = 0.0
            continue
        c = closes[i]
        if c > peak:
            peak = c
        trailing = trail is not None and peak > 0 and c <= peak * (1 - trail / 100)
        s_trig = use_s and isS[i]
        if trailing or s_trig:
            reason = "移动止损" if trailing else "S"
            ret = (c - entry) / entry * 100
            rets.append(ret)
            holds.append(i - entry_i)
            reasons[reason] += 1
            eq *= (1 + ret / 100)
            pos = False
    return rets, holds, reasons, (eq - 1) * 100, (1 if pos else 0)


def strategy_lab(configs, min_bars=60):
    """一次抓数+walk-forward 判定 realtime B/S，对多套策略配置分别模拟。
      configs: [(label, params_dict), ...]"""
    tickers = scan.load_universe()
    data = scan.fetch_all(tickers)
    out = {lab: {"rets": [], "holds": [], "reasons": {"S": 0, "移动止损": 0},
                 "per": [], "left": 0} for lab, _ in configs}
    for t, df in data.items():
        n = len(df)
        if n < min_bars + 2:
            continue
        opens = [float(x) for x in df["Open"].tolist()]
        closes = [float(x) for x in df["Close"].tolist()]
        # SMA200（趋势过滤用）
        sma_val = [None] * n
        win = 200
        if n >= win:
            s = sum(closes[:win])
            sma_val[win - 1] = s / win
            for i in range(win, n):
                s += closes[i] - closes[i - win]
                sma_val[i] = s / win
        isB = [False] * n
        isS = [False] * n
        # 滚动窗口重算 realtime 信号：窗口=约2年(504交易日)，与线上扫描器 period=2y 一致，
        # 既更贴近实盘视角，又把每根重算成本降为常数、支持长历史回测。
        WIN = 504
        for i in range(min_bars - 1, n):
            lo = max(0, i + 1 - WIN)
            try:
                cur = scan.compute_signals(df.iloc[lo:i + 1])
            except Exception:
                continue
            if not cur["dates"]:
                continue
            last = cur["dates"][-1]
            if last in set(cur["b_dates"]):
                isB[i] = True
            if last in set(cur["s_dates"]):
                isS[i] = True
        c0, c1 = closes[min_bars - 1], closes[n - 1]
        bh = (c1 - c0) / c0 * 100 if c0 else 0.0
        for lab, params in configs:
            rets, holds, reasons, eq, left = _sim_lab(
                isB, isS, opens, closes, sma_val, n, min_bars, params)
            o = out[lab]
            o["rets"] += rets
            o["holds"] += holds
            for k in reasons:
                o["reasons"][k] += reasons[k]
            o["per"].append({"strat": eq, "bh": bh})
            o["left"] += left
    return out, len(data), len(tickers)


def bs_strategy(configs, min_bars=60):
    """事件驱动策略回测（全用 realtime 信号，剥掉重绘）。
    一次抓数+walk-forward 判定 realtime B/S，然后对多套止损止盈配置分别模拟。
      configs: [(label, stop_pct, take_pct), ...]
    返回 {label: dict(rets,holds,reasons,per,left)} 与覆盖数。"""
    tickers = scan.load_universe()
    data = scan.fetch_all(tickers)
    out = {lab: {"rets": [], "holds": [], "reasons": {"S": 0, "止损": 0, "止盈": 0},
                 "per": [], "left": 0} for lab, _, _ in configs}
    for t, df in data.items():
        n = len(df)
        if n < min_bars + 2:
            continue
        opens = [float(x) for x in df["Open"].tolist()]
        highs = [float(x) for x in df["High"].tolist()]
        lows = [float(x) for x in df["Low"].tolist()]
        closes = [float(x) for x in df["Close"].tolist()]
        isB = [False] * n
        isS = [False] * n
        # 滚动窗口重算 realtime 信号：窗口=约2年(504交易日)，与线上扫描器 period=2y 一致，
        # 既更贴近实盘视角，又把每根重算成本降为常数、支持长历史回测。
        WIN = 504
        for i in range(min_bars - 1, n):
            lo = max(0, i + 1 - WIN)
            try:
                cur = scan.compute_signals(df.iloc[lo:i + 1])
            except Exception:
                continue
            if not cur["dates"]:
                continue
            last = cur["dates"][-1]
            if last in set(cur["b_dates"]):
                isB[i] = True
            if last in set(cur["s_dates"]):
                isS[i] = True
        c0, c1 = closes[min_bars - 1], closes[n - 1]
        bh = (c1 - c0) / c0 * 100 if c0 else 0.0
        for lab, sp, tp in configs:
            rets, holds, reasons, eq, left = _sim_bs(
                isB, isS, opens, highs, lows, closes, n, min_bars, sp, tp)
            o = out[lab]
            o["rets"] += rets
            o["holds"] += holds
            for k in reasons:
                o["reasons"][k] += reasons[k]
            o["per"].append({"strat": eq, "bh": bh})
            o["left"] += left
    return out, len(data), len(tickers)


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

    yrs = _argval("--years")
    if yrs:
        scan.HISTORY_PERIOD = f"{int(yrs)}y"      # 覆盖抓取历史长度（默认 scan.py 里的 2y）
        print(f"抓取历史长度覆盖为 {scan.HISTORY_PERIOD}")

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

    if "--lab" in sys.argv:
        import statistics as st
        configs = [
            ("B→S 基线(参考)",                {}),
            ("趋势跟随 10%移动止损",          {"trail": 10, "use_s": False}),
            ("趋势跟随 15%移动止损",          {"trail": 15, "use_s": False}),
            ("趋势跟随 20%移动止损",          {"trail": 20, "use_s": False}),
            ("趋势跟随 25%移动止损",          {"trail": 25, "use_s": False}),
            ("趋势跟随 20%+SMA200过滤",       {"trail": 20, "use_s": False, "sma": 200}),
        ]
        out, ok, tot = strategy_lab(configs)
        print("\n== 策略实验室：B次日开盘买入，多种出场/过滤对照（全 realtime 信号）==")
        print(f"数据覆盖：{ok}/{tot} 只\n")
        print(f"{'配置':<26}{'笔数':>6}{'胜率':>7}{'平均':>8}{'中位':>8}{'最差':>8}{'累计':>8}{'跑赢BH':>8}")
        for lab, _ in configs:
            o = out[lab]
            r = o["rets"]
            if not r:
                print(f"{lab:<26} 无样本"); continue
            n = len(r)
            win = sum(1 for x in r if x > 0) / n * 100
            per = o["per"]
            avg_strat = sum(p["strat"] for p in per) / len(per)
            beat = sum(1 for p in per if p["strat"] > p["bh"])
            print(f"{lab:<26}{n:>6}{win:>6.1f}%{sum(r)/n:>7.2f}%{st.median(r):>7.2f}%"
                  f"{min(r):>7.1f}%{avg_strat:>7.1f}%{beat:>5}/{len(per)}")
        bh_avg = sum(p["bh"] for p in out[configs[0][0]]["per"]) / len(out[configs[0][0]]["per"])
        print(f"\n买入持有平均总收益（对照基准）：{bh_avg:+.1f}%")
        print("列说明：平均/中位=每笔收益，最差=单笔最大亏损，累计=每股复利平均总收益。")
        print("全 realtime 信号；移动止损=收盘从入场后最高收盘回撤N%；不设固定止盈；未扣费。")
        return

    if "--strategy" in sys.argv:
        import statistics as st
        stop = _argval("--stop")
        take = _argval("--take")
        stop = 10.0 if stop is None else stop
        take = 20.0 if take is None else take
        configs = [("无止损止盈（基线）", None, None),
                   (f"{stop:g}%止损 / {take:g}%止盈", stop, take)]
        out, ok, tot = bs_strategy(configs)
        print("\n== 策略：B出现次日开盘买入 → 持有到S出现当天收盘卖出（全 realtime 信号）==")
        print(f"数据覆盖：{ok}/{tot} 只\n")
        for lab, _, _ in configs:
            o = out[lab]
            rets, holds, per = o["rets"], o["holds"], o["per"]
            print(f"—— {lab} ——")
            if rets:
                n = len(rets)
                win = sum(1 for x in rets if x > 0) / n * 100
                avg_strat = sum(p["strat"] for p in per) / len(per)
                avg_bh = sum(p["bh"] for p in per) / len(per)
                beat = sum(1 for p in per if p["strat"] > p["bh"])
                rs = o["reasons"]
                print(f"  完成交易 {n} 笔 | 期末未平 {o['left']} 只 | 平均持有 {sum(holds)/len(holds):.1f} 交易日")
                print(f"  胜率 {win:.1f}% | 平均每笔 {sum(rets)/n:+.2f}% | 中位 {st.median(rets):+.2f}% | 区间 {min(rets):+.1f}%~{max(rets):+.1f}%")
                print(f"  出场构成：S {rs['S']} / 止盈 {rs['止盈']} / 止损 {rs['止损']}")
                print(f"  累计(每股复利)：策略 {avg_strat:+.1f}% vs 买入持有 {avg_bh:+.1f}% | 跑赢 {beat}/{len(per)} 只")
            print()
        print("注：全用 realtime 信号；止损止盈用当日高低价、跳空按开盘价、止损优先；")
        print("    未扣手续费/滑点；只做多、单一仓位；期末未平仓不计入交易统计。")
        return

    if "--repaint-in" in sys.argv:
        res, ok, tot = repaint_in_analysis()
        print(f"\n== 补标(repaint-in)分析：信号是当天就出，还是过几天才被画上去 ==")
        print(f"数据覆盖：{ok}/{tot} 只\n")
        for side, label in (("B", "B 买点"), ("S", "S 卖点")):
            same = res[side]["same"]
            lags = res[side]["lags"]
            total = same + len(lags)
            if not total:
                print(f"{label}: 无样本"); continue
            ratein = len(lags) / total * 100
            import statistics as st
            med = st.median(lags) if lags else 0
            mx = max(lags) if lags else 0
            # 分桶
            b = {"1天": 0, "2-3天": 0, "4-5天": 0, ">5天": 0}
            for L in lags:
                if L == 1: b["1天"] += 1
                elif L <= 3: b["2-3天"] += 1
                elif L <= 5: b["4-5天"] += 1
                else: b[">5天"] += 1
            print(f"{label}：共 {total} 个 | 当天就出 {same}（{same/total*100:.1f}%） | "
                  f"补标 {len(lags)}（{ratein:.1f}%）")
            print(f"    补标延迟：中位 {med} 天 / 最长 {mx} 天 | "
                  f"1天={b['1天']} 2-3天={b['2-3天']} 4-5天={b['4-5天']} >5天={b['>5天']}")
        print("\n注：lag=首次被画上信号时的最新K线，距该信号自身K线的交易日数；")
        print("    lag=0 当天就出（实时可见）；lag>=1 即当天没出、后来重绘补标。")
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
