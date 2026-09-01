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


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
