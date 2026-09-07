"""
scan.py
=======
每日扫描器：
  1. 读取 universe.txt 股票池
  2. yfinance 拉取日线数据
  3. 用 s1_signals.compute_signals 计算 B / S 买卖点（忠实复刻 S1 指标，会重绘）
  4. 与上一次运行的快照对比，检测「近期消失的 B/S 买卖点」及其消失节点
  5. 生成 dashboard.html（B 名单 / S 名单 / Warning 消失区，代码可点击跳 TradingView）

状态文件 state.json 会自动创建并逐日累积——Warning 区需要有历史快照才会有内容，
所以第一天运行不会有「消失」记录，之后每天逐步显现。
"""

from __future__ import annotations
import json
import os
import sys
import io
import datetime as dt

import yfinance as yf

from s1_signals import compute_signals

BASE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_FILE = os.path.join(BASE, "universe.txt")
STATE_FILE = os.path.join(BASE, "state.json")
OUTPUT_HTML = os.path.join(BASE, "dashboard.html")
EMAIL_HTML = os.path.join(BASE, "email_body.html")     # CI 发信用的精简正文
EMAIL_SUBJECT = os.path.join(BASE, "email_subject.txt")  # CI 读取作为邮件主题
VENDOR_DIR = os.path.join(BASE, "vendor")
VENDOR_JS = os.path.join(VENDOR_DIR, "lightweight-charts.js")
LWC_CDN = "https://unpkg.com/lightweight-charts@5.0.4/dist/lightweight-charts.standalone.production.js"
LIVE_URL = "https://marshallzsy-dev.github.io/buy-sell-scanner/"  # 线上 dashboard 地址

# 重绘率统计：按「信号距最新K线的天数」分桶
REPAINT_BUCKET_ORDER = ["0-1", "2-3", "4-5", "6-10", "11-20", ">20"]
BUCKET_LABELS = {
    "0-1": "0–1天(最新边缘)", "2-3": "2–3天", "4-5": "4–5天",
    "6-10": "6–10天", "11-20": "11–20天", ">20": ">20天(老信号)",
}

RECENT_DAYS = 3          # 「近三日」窗口（交易日）
DISAPPEAR_LOOKBACK = 15  # 只对最近 N 个交易日内的信号消失发 Warning
WARN_KEEP_DAYS = 7       # Warning 在页面上保留的天数（按检测日历日）
HISTORY_PERIOD = "2y"    # 拉取历史长度
CHART_BARS = 250         # 弹层图表保留的最近 K 线根数（约 1 年）

# 每只上榜股票的 B 信号历史画像（前向收益 / 回撤 / 消失率）
STATS_HOLD = 5           # 前向持有交易日数（“B 后 5 日”）
STATS_MIN_BARS = 60      # walk-forward 评估起点（与 analyze.py 口径一致）
STATS_WIN = 504          # 滚动重算窗口（约 2 年，与 HISTORY_PERIOD 一致）
STATS_MAX_TICKERS = 150  # 单次最多为多少只上榜股算画像（防 CI 超时的兜底上限）


# ---------------------------------------------------------------------------
# lightweight-charts 库：首次运行下载到 vendor/，之后读取内联（离线自包含）
# ---------------------------------------------------------------------------
def load_lwc_lib():
    """返回 lightweight-charts standalone JS 源码字符串，供内联进 HTML。"""
    if not os.path.exists(VENDOR_JS):
        try:
            import urllib.request
            os.makedirs(VENDOR_DIR, exist_ok=True)
            print(f"首次运行，下载 lightweight-charts 库 ...", flush=True)
            urllib.request.urlretrieve(LWC_CDN, VENDOR_JS)
        except Exception as e:
            print(f"⚠ 下载图表库失败：{e}，图表将回退到 CDN 加载。", flush=True)
            return None
    try:
        with open(VENDOR_JS, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 时间：美东时区
# ---------------------------------------------------------------------------
def now_et():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # 退化：用 UTC-4（美东夏令时）近似，仅用于显示
        return dt.datetime.utcnow() - dt.timedelta(hours=4)


# ---------------------------------------------------------------------------
# 股票池
# ---------------------------------------------------------------------------
def load_universe():
    tickers = []
    with open(UNIVERSE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            tickers.append(s.upper())
    # 去重保序
    seen = set()
    out = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# 数据抓取
# ---------------------------------------------------------------------------
def _extract(data, t, single):
    try:
        sub = data if single else data[t]
        # yfinance 有时返回 MultiIndex 列（尤其单只回退时），展平成单级，
        # 否则 sub["Open"] 会是 DataFrame 而非 Series，下游 .tolist() 报错。
        if hasattr(sub.columns, "nlevels") and sub.columns.nlevels > 1:
            lvl0 = set(sub.columns.get_level_values(0))
            # 取含 OHLCV 的那一级作为列名
            if {"Open", "High", "Low", "Close"} <= lvl0:
                sub.columns = sub.columns.get_level_values(0)
            else:
                sub.columns = sub.columns.get_level_values(-1)
        sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if len(sub) >= 60:
            return sub
    except Exception:
        pass
    return None


def fetch_all(tickers):
    """返回 {ticker: DataFrame(OHLCV)}。批量抓取 + 失败重试 + 单只回退，抗限流。"""
    import time
    print(f"下载 {len(tickers)} 只股票日线数据 ...", flush=True)
    result = {}
    pending = list(tickers)

    # 批量尝试（含重试，应对偶发限流导致的整体空返回）
    for attempt in range(3):
        if not pending:
            break
        try:
            data = yf.download(pending, period=HISTORY_PERIOD, interval="1d",
                               auto_adjust=False, group_by="ticker",
                               threads=True, progress=False)
            single = len(pending) == 1
            got = 0
            for t in list(pending):
                sub = _extract(data, t, single)
                if sub is not None:
                    result[t] = sub
                    pending.remove(t)
                    got += 1
            print(f"  批量第 {attempt+1} 次：新增 {got}，剩余 {len(pending)}", flush=True)
            if got > 0 and not pending:
                break
        except Exception as e:
            print(f"  批量第 {attempt+1} 次异常：{e}", flush=True)
        if pending:
            time.sleep(8 * (attempt + 1))

    # 单只回退：对仍缺失的逐个重试
    for t in list(pending):
        for _ in range(2):
            try:
                d = yf.download(t, period=HISTORY_PERIOD, interval="1d",
                                auto_adjust=False, progress=False)
                sub = _extract(d, t, True)
                if sub is not None:
                    result[t] = sub
                    pending.remove(t)
                    break
            except Exception:
                pass
            time.sleep(2)

    return result


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_run": None, "tickers": {}, "warnings": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------------
# 消失检测
# ---------------------------------------------------------------------------
def detect_disappearances(ticker, prev, cur, today_str):
    """比较上次(prev)与本次(cur)的 b/s 日期，返回新发现的消失记录列表。"""
    warnings = []
    if not prev:
        return warnings
    cur_dates = cur["dates"]
    if not cur_dates:
        return warnings
    cur_date_set = set(cur_dates)
    # 最近 DISAPPEAR_LOOKBACK 个交易日范围（只关注近期消失）
    recent_window = set(cur_dates[-DISAPPEAR_LOOKBACK:])

    for side, key in (("B", "b_dates"), ("S", "s_dates")):
        prev_dates = set(prev.get(key, []))
        cur_side = set(cur.get(key, []))
        for d in prev_dates:
            # 该 K 线仍在当前窗口内（没被数据窗口滚出去），但信号不见了
            if d in cur_date_set and d not in cur_side and d in recent_window:
                warnings.append({
                    "ticker": ticker,
                    "side": side,
                    "bar_date": d,          # 消失的买卖点所在 K 线日期（消失节点）
                    "detected_on": today_str,
                })
    return warnings


def repaint_age_bucket(age_days):
    if age_days <= 1: return "0-1"
    if age_days <= 3: return "2-3"
    if age_days <= 5: return "4-5"
    if age_days <= 10: return "6-10"
    if age_days <= 20: return "11-20"
    return ">20"


def _pdate(s):
    try:
        return dt.date.fromisoformat(s)
    except Exception:
        return None


def tally_repaint(prev_tickers, cur_tickers):
    """比较相邻两日快照：prev 里已出现的 B/S 点，在 cur 里是否还在（消失=重绘）。
    按信号距 prev 那天最新K线的天数分桶，返回 {bucket: [样本数, 消失数]}。
    只统计两天都成功抓到数据的股票，避免把"当天抓取失败"误判为消失。"""
    out = {}
    for t, pv in prev_tickers.items():
        cv = cur_tickers.get(t)
        if not cv:
            continue
        pdl = _pdate(pv.get("data_last"))
        cdl = _pdate(cv.get("data_last"))
        if not pdl:
            continue
        for side, key in (("B", "b_dates"), ("S", "s_dates")):
            cur_set = set(cv.get(key, []))
            for bd in pv.get(key, []):
                d = _pdate(bd)
                if not d:
                    continue
                age = (pdl - d).days
                if age < 0:
                    continue
                if cdl and d > cdl:   # 该K线已滚出当前数据窗口，不计
                    continue
                slot = out.setdefault(repaint_age_bucket(age), [0, 0])
                slot[0] += 1
                if bd not in cur_set:
                    slot[1] += 1
    return out


def merge_warnings(existing, new_ones, today):
    """合并去重，丢弃超过 WARN_KEEP_DAYS 天的旧告警。"""
    def key(w):
        return (w["ticker"], w["side"], w["bar_date"])

    kept = {}
    for w in existing + new_ones:
        try:
            det = dt.date.fromisoformat(w["detected_on"])
        except Exception:
            continue
        if (today - det).days > WARN_KEEP_DAYS:
            continue
        k = key(w)
        # 保留最早检测到的那条
        if k not in kept or w["detected_on"] < kept[k]["detected_on"]:
            kept[k] = w
    return list(kept.values())


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------
def tv_url(ticker):
    return f"https://www.tradingview.com/chart/?symbol={ticker}"


def days_ago_label(bar_date, ref_dates):
    """bar_date 在 ref_dates（交易日列表）中距最新的第几个交易日。"""
    try:
        idx = ref_dates.index(bar_date)
        n = len(ref_dates) - 1 - idx
        return "今日" if n == 0 else f"{n}日前"
    except ValueError:
        return bar_date


def build_chart_data(df, cur):
    """把一只股票的信号结果压成弹层图表所需的数据：
       最近 CHART_BARS 根 K 线 OHLC + 落在该窗口内的 B/S 标记。
       返回 {'bars': [...], 'markers': [...]}。"""
    dates = cur["dates"]
    n = len(dates)
    start = max(0, n - CHART_BARS)

    opens = [float(x) for x in df["Open"].tolist()]
    highs = [float(x) for x in df["High"].tolist()]
    lows = [float(x) for x in df["Low"].tolist()]
    closes = cur["closes"]

    bars = []
    for i in range(start, n):
        bars.append({
            "time": dates[i],
            "open": round(opens[i], 2),
            "high": round(highs[i], 2),
            "low": round(lows[i], 2),
            "close": round(closes[i], 2),
        })

    window = set(dates[start:])
    markers = []
    for d in cur["b_dates"]:
        if d in window:
            markers.append({"time": d, "side": "B"})
    for d in cur["s_dates"]:
        if d in window:
            markers.append({"time": d, "side": "S"})
    markers.sort(key=lambda m: m["time"])
    return {"bars": bars, "markers": markers}


def b_symbol_stats(df, final_b_dates, hold=STATS_HOLD,
                   min_bars=STATS_MIN_BARS, win=STATS_WIN):
    """单只股票的 B 信号历史画像，walk-forward 逐根重算 realtime B 信号后计算：
      - fwd_avg : realtime B「次日开盘入场→持有 hold 交易日→末日收盘平仓」的平均收益率(%)
      - dd_avg  : 上述持有期内「每日相对入场价回撤(仅计跌破入场价部分)」的平均值(%，≤0)
      - dis_rate: realtime 原生出现的 B 信号里，最终在定型(重绘后)信号中消失的比例(%)
    收益/回撤仅统计「熬过至少一天」的 B（bar i 出现、次日 i+1 重算仍在图上）；
    一日闪现(次日即消失)的 B 实盘抓不住，从收益/回撤剔除（但仍计入 dis_rate 消失率）。
    realtime 判定 / 入场出场口径与 analyze.py 的 forward_all 完全一致（剥掉重绘 lookahead）。
    只对上榜股票调用（成本 O(bars) 每根重算 × 有信号的股票），返回 dict 或 None（历史不足）。"""
    opens = [float(x) for x in df["Open"].tolist()]
    lows = [float(x) for x in df["Low"].tolist()]
    closes = [float(x) for x in df["Close"].tolist()]
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    n = len(closes)
    if n < min_bars + 2:
        return None

    final_set = set(final_b_dates)
    native = []          # [(bar_index, date_str)] realtime B 首次以最新K线当场出现的点
    seen = set()
    step_b = [None] * n  # 每根K线为最新时的 realtime B 集合，用于「次日是否仍在」的存活判定
    for i in range(min_bars - 1, n):
        lo = max(0, i + 1 - win)
        try:
            cur = compute_signals(df.iloc[lo:i + 1])
        except Exception:
            continue
        if not cur["dates"]:
            continue
        step_b[i] = set(cur["b_dates"])
        if cur["dates"][-1] in step_b[i]:             # 最新那根当场就是 B → 实时可见
            d = dates[i]
            if d not in seen:
                seen.add(d)
                native.append((i, d))
    if not native:
        return None

    # 收益/回撤只统计「熬过至少一天」的 B：bar i 出现、bar i+1 重算仍在图上。
    # 一日闪现（次日即消失）的 B 实盘根本抓不住，剔除以免拉低画像。
    fwd, dd, flash = [], [], 0
    for i, d in native:
        if not (i + 1 < n and step_b[i + 1] is not None and d in step_b[i + 1]):
            flash += 1
            continue                                  # 一日闪现，剔除出收益/回撤
        eo = opens[i + 1] if i + 1 < n else 0.0
        if i + hold < n and eo:
            fwd.append((closes[i + hold] - eo) / eo * 100)
            # 持有 hold 日内「每日相对入场价的回撤」（仅计跌破入场价部分，≤0），再取均值——
            # 反映典型回撤水平，而非最坏单点。
            daily_dd = [min(0.0, (lw - eo) / eo) for lw in lows[i + 1:i + hold + 1]]
            dd.append(sum(daily_dd) / len(daily_dd) * 100)
    total = len(native)
    gone = sum(1 for _, d in native if d not in final_set)
    return {
        "fwd_avg": (sum(fwd) / len(fwd)) if fwd else None,
        "fwd_n": len(fwd),
        # 剔除一日闪现后，B 持有 hold 日末收为正的比例（%）
        "win_rate": (sum(1 for x in fwd if x > 0) / len(fwd) * 100) if fwd else None,
        "dd_avg": (sum(dd) / len(dd)) if dd else None,
        "dis_rate": (gone / total * 100) if total else None,
        "dis_gone": gone,
        "dis_total": total,
        "flash": flash,          # 一日闪现、已从收益/回撤剔除的 B 数
    }


def s_symbol_stats(df, final_s_dates, hold=STATS_HOLD,
                   min_bars=STATS_MIN_BARS, win=STATS_WIN):
    """单只股票的 S 卖点历史画像（把 S 当作见顶/回落信号），walk-forward 逐根重算：
      - dd_avg   : S 出现后「次日开盘为基准，持有 hold 交易日内每日相对基准的回撤
                   (仅计跌破基准部分，≤0)」的平均值(%)——S 后股价平均能回撤多深。
      - down_rate: S 出现后持有 hold 交易日、末日收盘 < 次日开盘的比例(%)——即「下跌概率/反向胜率」。
      - dis_rate : realtime 原生出现的 S 信号里，最终在定型(重绘后)信号中消失的比例(%)。
    回撤/下跌概率仅统计「熬过至少一天」的 S（bar i 出现、次日 i+1 重算仍在图上）；
    一日闪现(次日即消失)的 S 实盘抓不住，剔除（但仍计入 dis_rate 消失率）。口径与 B 画像镜像一致。"""
    opens = [float(x) for x in df["Open"].tolist()]
    lows = [float(x) for x in df["Low"].tolist()]
    closes = [float(x) for x in df["Close"].tolist()]
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    n = len(closes)
    if n < min_bars + 2:
        return None

    final_set = set(final_s_dates)
    native = []          # [(bar_index, date_str)] realtime S 首次以最新K线当场出现的点
    seen = set()
    step_s = [None] * n  # 每根K线为最新时的 realtime S 集合，用于「次日是否仍在」的存活判定
    for i in range(min_bars - 1, n):
        lo = max(0, i + 1 - win)
        try:
            cur = compute_signals(df.iloc[lo:i + 1])
        except Exception:
            continue
        if not cur["dates"]:
            continue
        step_s[i] = set(cur["s_dates"])
        if cur["dates"][-1] in step_s[i]:             # 最新那根当场就是 S → 实时可见
            d = dates[i]
            if d not in seen:
                seen.add(d)
                native.append((i, d))
    if not native:
        return None

    dd, down, flash = [], [], 0
    for i, d in native:
        if not (i + 1 < n and step_s[i + 1] is not None and d in step_s[i + 1]):
            flash += 1
            continue                                  # 一日闪现，剔除出回撤/下跌概率
        eo = opens[i + 1] if i + 1 < n else 0.0
        if i + hold < n and eo:
            # 持有 hold 日内「每日相对次日开盘的回撤」（仅计跌破部分，≤0）取均值——S 后典型回撤深度。
            daily_dd = [min(0.0, (lw - eo) / eo) for lw in lows[i + 1:i + hold + 1]]
            dd.append(sum(daily_dd) / len(daily_dd) * 100)
            down.append(1 if closes[i + hold] < eo else 0)   # 末日收盘 < 基准 → 下跌
    total = len(native)
    gone = sum(1 for _, d in native if d not in final_set)
    return {
        "dd_avg": (sum(dd) / len(dd)) if dd else None,
        "down_rate": (sum(down) / len(down) * 100) if down else None,
        "n": len(dd),
        "dis_rate": (gone / total * 100) if total else None,
        "dis_gone": gone,
        "dis_total": total,
        "flash": flash,
    }


def render_html(b_list, s_list, warnings, meta, chart_data, bstats, sstats):
    et = meta["run_et"]
    stamp = et.strftime("%Y-%m-%d %H:%M")

    def _fwd_cell(st):
        if not st or st.get("fwd_avg") is None:
            return '<td class="num" style="color:var(--muted)">—</td>'
        v = st["fwd_avg"]
        color = "#5fd98a" if v > 0 else ("#f07fce" if v < 0 else "var(--muted)")
        return (f'<td class="num" style="color:{color}" '
                f'title="{st["fwd_n"]} 个存活B样本（已剔除 {st.get("flash", 0)} 个一日闪现）">{v:+.1f}%</td>')

    def _win_cell(st):
        if not st or st.get("win_rate") is None:
            return '<td class="num" style="color:var(--muted)">—</td>'
        r = st["win_rate"]
        color = "#5fd98a" if r >= 55 else ("#f0a020" if r >= 45 else "#f07fce")
        return (f'<td class="num" style="color:{color}" '
                f'title="剔除一日闪现B后，{st["fwd_n"]} 个存活B里持有5日末收为正的比例">'
                f'{r:.0f}%</td>')

    def _dd_cell(st):
        if not st or st.get("dd_avg") is None:
            return '<td class="num" style="color:var(--muted)">—</td>'
        return (f'<td class="num" style="color:var(--amber)" '
                f'title="持有5日内每日相对入场价回撤的平均值（仅计跌破入场价的部分）">'
                f'{st["dd_avg"]:.1f}%</td>')

    def _dis_cell(st, side="B"):
        if not st or st.get("dis_rate") is None:
            return '<td class="num" style="color:var(--muted)">—</td>'
        r = st["dis_rate"]
        color = "#5fd98a" if r < 20 else ("#f0a020" if r < 50 else "#f07fce")
        return (f'<td class="num" style="color:{color}" '
                f'title="{st["dis_gone"]}/{st["dis_total"]} 个原生{side}最终被重绘抹掉">{r:.0f}%</td>')

    # —— S 卖点画像单元格（S 当见顶/回落信号看：回撤越深、下跌概率越高越有效）——
    def _sdd_cell(st):
        if not st or st.get("dd_avg") is None:
            return '<td class="num" style="color:var(--muted)">—</td>'
        return (f'<td class="num" style="color:var(--amber)" '
                f'title="剔除一日闪现S后，{st["n"]} 个存活S：次日开盘为基准、持有5日内每日回撤（仅计跌破部分）的平均值">'
                f'{st["dd_avg"]:.1f}%</td>')

    def _sdown_cell(st):
        if not st or st.get("down_rate") is None:
            return '<td class="num" style="color:var(--muted)">—</td>'
        r = st["down_rate"]
        color = "#5fd98a" if r >= 55 else ("#f0a020" if r >= 45 else "#f07fce")
        return (f'<td class="num" style="color:{color}" '
                f'title="剔除一日闪现S后，{st["n"]} 个存活S里持有5日末收低于次日开盘的比例（反向胜率）">'
                f'{r:.0f}%</td>')

    def row_bs(item):
        code = item["ticker"]
        st = bstats.get(code)
        has = code in chart_data
        cell = (f'<a class="chart-link" data-sym="{code}">{code}</a>' if has
                else f'<a href="{tv_url(code)}" target="_blank" rel="noopener">{code}</a>')
        return (
            f'<tr>'
            f'<td class="code">{cell}</td>'
            f'<td>{item["last_date"]}</td>'
            f'<td><span class="pill {item["recency_cls"]}">{item["recency"]}</span></td>'
            f'<td class="num">{item["price"]:.2f}</td>'
            f'{_fwd_cell(st)}{_win_cell(st)}{_dd_cell(st)}{_dis_cell(st, "B")}'
            f'</tr>'
        )

    def row_s(item):
        code = item["ticker"]
        st = sstats.get(code)
        has = code in chart_data
        cell = (f'<a class="chart-link" data-sym="{code}">{code}</a>' if has
                else f'<a href="{tv_url(code)}" target="_blank" rel="noopener">{code}</a>')
        return (
            f'<tr>'
            f'<td class="code">{cell}</td>'
            f'<td>{item["last_date"]}</td>'
            f'<td><span class="pill {item["recency_cls"]}">{item["recency"]}</span></td>'
            f'<td class="num">{item["price"]:.2f}</td>'
            f'{_sdd_cell(st)}{_sdown_cell(st)}{_dis_cell(st, "S")}'
            f'</tr>'
        )

    def row_warn(w):
        code = w["ticker"]
        badge = "B" if w["side"] == "B" else "S"
        bcls = "b" if w["side"] == "B" else "s"
        has = code in chart_data
        cell = (f'<a class="chart-link" data-sym="{code}">{code}</a>' if has
                else f'<a href="{tv_url(code)}" target="_blank" rel="noopener">{code}</a>')
        return (
            f'<tr>'
            f'<td class="code">{cell}</td>'
            f'<td><span class="tag {bcls}">{badge} 消失</span></td>'
            f'<td class="node">{w["bar_date"]}</td>'
            f'<td>{w["detected_on"]}</td>'
            f'</tr>'
        )

    b_rows = "\n".join(row_bs(x) for x in b_list) or '<tr><td colspan="8" class="empty">近三日无 B 买点</td></tr>'
    s_rows = "\n".join(row_s(x) for x in s_list) or '<tr><td colspan="7" class="empty">近三日无 S 卖点</td></tr>'
    warn_rows = "\n".join(row_warn(w) for w in warnings) or \
        '<tr><td colspan="4" class="empty">暂无消失记录（需累积历史快照，运行几天后逐步显现）</td></tr>'

    # 重绘率表（信号可靠性）
    rs = meta.get("repaint_stats") or {}
    rbk = rs.get("buckets") or {}
    if rbk and rs.get("transitions"):
        rr = []
        tin = tg = 0
        for k in REPAINT_BUCKET_ORDER:
            if k in rbk:
                n, g = rbk[k]
                tin += n; tg += g
                rate = g / n * 100 if n else 0
                color = "#5fd98a" if rate < 2 else ("#f0a020" if rate < 12 else "#f07fce")
                rr.append(f'<tr><td>{BUCKET_LABELS[k]}</td><td class="num">{n}</td>'
                          f'<td class="num">{g}</td>'
                          f'<td class="num" style="color:{color}">{rate:.1f}%</td></tr>')
        trate = tg / tin * 100 if tin else 0
        rr.append(f'<tr><td style="font-weight:600">合计</td>'
                  f'<td class="num" style="font-weight:600">{tin}</td>'
                  f'<td class="num" style="font-weight:600">{tg}</td>'
                  f'<td class="num" style="font-weight:600">{trate:.1f}%</td></tr>')
        rp_rows = "\n".join(rr)
        rp_note = f"{rs.get('transitions')} 个交易日累积 · {rs.get('since')}~{rs.get('updated')}"
    else:
        rp_rows = '<tr><td colspan="4" class="empty">重绘统计累积中（需多日快照，运行几天后逐步显现）</td></tr>'
        rp_note = "累积中"

    skipped = meta["skipped"]
    skipped_txt = "、".join(skipped) if skipped else "无"

    # 图表数据内联（紧凑 JSON）
    chart_json = json.dumps(chart_data, ensure_ascii=False, separators=(",", ":"))

    # lightweight-charts 库：内联优先，失败回退 CDN
    lib = meta.get("lwc_lib")
    if lib:
        lib_script = f"<script>{lib}</script>"
    else:
        lib_script = f'<script src="{LWC_CDN}"></script>'

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>S1 买卖点扫描 · {stamp} ET</title>
<style>
  :root {{
    --bg:#0d1117; --panel:#161b22; --line:#232a34; --muted:#8b98a9;
    --text:#e6edf3; --green:#2fb35a; --pink:#d63c9c; --amber:#f0a020;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
  .wrap {{ max-width:1000px; margin:0 auto; padding:18px 14px 60px; }}
  header {{ display:flex; align-items:baseline; justify-content:space-between; flex-wrap:wrap; gap:8px; margin-bottom:6px; }}
  h1 {{ font-size:20px; margin:0; letter-spacing:.5px; }}
  .sub {{ color:var(--muted); font-size:12px; }}
  section {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:14px 14px 6px; margin-top:16px; }}
  .stitle {{ display:flex; align-items:center; gap:8px; font-size:15px; font-weight:600; margin:0 0 8px; }}
  .dot {{ width:9px; height:9px; border-radius:50%; display:inline-block; }}
  .dot.g {{ background:var(--green); }} .dot.p {{ background:var(--pink); }} .dot.a {{ background:var(--amber); }}
  .cnt {{ color:var(--muted); font-size:12px; font-weight:400; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:500; font-size:11px; text-transform:uppercase; letter-spacing:.4px; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.code a {{ color:var(--text); text-decoration:none; font-weight:600; border-bottom:1px dotted var(--muted); }}
  td.code a:hover {{ color:#58a6ff; }}
  .node {{ color:var(--amber); font-variant-numeric:tabular-nums; }}
  .pill {{ font-size:11px; padding:1px 7px; border-radius:10px; }}
  .pill.today {{ background:rgba(47,179,90,.18); color:#5fd98a; }}
  .pill.recent {{ background:rgba(139,152,169,.15); color:var(--muted); }}
  .tag {{ font-size:11px; padding:1px 7px; border-radius:6px; font-weight:600; }}
  .tag.b {{ background:rgba(47,179,90,.18); color:#5fd98a; }}
  .tag.s {{ background:rgba(214,60,156,.18); color:#f07fce; }}
  .warn-sec {{ border-color:rgba(240,160,32,.4); }}
  .empty {{ color:var(--muted); text-align:center; padding:16px; }}
  footer {{ color:var(--muted); font-size:11px; margin-top:22px; line-height:1.6; }}
  td.code a.chart-link {{ cursor:pointer; }}
  /* 图表弹层 */
  .modal {{ position:fixed; inset:0; background:rgba(0,0,0,.72); display:none;
    align-items:center; justify-content:center; z-index:50; padding:16px; }}
  .modal.open {{ display:flex; }}
  .modal-box {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
    width:min(920px,96vw); max-height:92vh; overflow:hidden; display:flex; flex-direction:column; }}
  .modal-head {{ display:flex; align-items:center; justify-content:space-between;
    padding:12px 14px; border-bottom:1px solid var(--line); }}
  .modal-title {{ font-size:16px; font-weight:600; letter-spacing:.5px; }}
  .modal-title .lg {{ margin-left:8px; font-size:11px; font-weight:400; }}
  .modal-title .lg .b {{ color:#5fd98a; }} .modal-title .lg .s {{ color:#f07fce; }}
  .modal-actions {{ display:flex; align-items:center; gap:12px; }}
  .modal-actions a {{ color:#58a6ff; font-size:12px; text-decoration:none; }}
  .modal-close {{ cursor:pointer; color:var(--muted); font-size:22px; line-height:1;
    background:none; border:none; }}
  .modal-close:hover {{ color:var(--text); }}
  #chart {{ width:100%; height:460px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>S1 买卖点扫描</h1>
    <div class="sub">数据截至 {meta['data_last']} · 生成于 {stamp} 美东 · 股票池 {meta['universe_n']} 只（成功 {meta['ok_n']}）</div>
  </header>

  <section>
    <div class="stitle"><span class="dot g"></span> B 买点 · 当日/近三日 <span class="cnt">（{len(b_list)}）</span></div>
    <table>
      <thead><tr><th>代码</th><th>最近B日期</th><th>时点</th><th>现价</th>
        <th class="num" title="历史实时B信号：次日开盘入场、持有5交易日、末日收盘平仓的平均收益">B后5日均收益</th>
        <th class="num" title="剔除一日闪现B后，持有5交易日末收为正的比例">B后5日胜率</th>
        <th class="num" title="历史实时B信号：持有5日内每日相对入场价回撤的平均值（仅计跌破入场价的部分）">B后5日均回撤</th>
        <th class="num" title="历史实时B信号中，最终被重绘抹掉（消失）的比例">B消失率</th></tr></thead>
      <tbody>{b_rows}</tbody>
    </table>
  </section>

  <section>
    <div class="stitle"><span class="dot p"></span> S 卖点 · 当日/近三日 <span class="cnt">（{len(s_list)}）</span></div>
    <table>
      <thead><tr><th>代码</th><th>最近S日期</th><th>时点</th><th>现价</th>
        <th class="num" title="剔除一日闪现S后：S次日开盘为基准、持有5日内每日回撤（仅计跌破部分）的平均值，越深说明S后越易回落">S后5日均回撤</th>
        <th class="num" title="剔除一日闪现S后：持有5日末收低于次日开盘的比例（反向胜率），越高说明S越可靠">S后5日下跌概率</th>
        <th class="num" title="该股历史实时S信号中，最终被重绘抹掉（消失）的比例">S消失率</th></tr></thead>
      <tbody>{s_rows}</tbody>
    </table>
  </section>

  <section class="warn-sec">
    <div class="stitle"><span class="dot a"></span> ⚠ Warning · 近期消失的买卖点 <span class="cnt">（{len(warnings)}）</span></div>
    <table>
      <thead><tr><th>代码</th><th>类型</th><th>消失节点(K线日期)</th><th>检测于</th></tr></thead>
      <tbody>{warn_rows}</tbody>
    </table>
  </section>

  <section>
    <div class="stitle"><span class="dot" style="background:#58a6ff"></span> 信号可靠性 · 重绘消失率 by 信号年龄 <span class="cnt">（{rp_note}）</span></div>
    <table>
      <thead><tr><th>信号年龄</th><th>样本</th><th>消失</th><th>消失率</th></tr></thead>
      <tbody>{rp_rows}</tbody>
    </table>
    <div style="color:var(--muted);font-size:11px;padding:8px 2px 2px;line-height:1.6;">
      · 统计「相邻交易日之间，已出现的 B/S 点是否重绘消失」，按信号距最新 K 线的天数分桶，云端每日累积。<br>
      · 越新的信号越易重绘：<b>0–1 天最不稳，熬过约 5 个交易日基本稳定</b>——可当作「信号可信度」参考，别追当天新点。
    </div>
  </section>

  <footer>
    · 点击代码弹出该股 K 线图，B/S 买卖点已标在图上（可缩放、拖动）；无图表数据的代码则跳 TradingView。<br>
    · <b>B后5日均收益 / B后5日胜率 / B后5日均回撤 / B消失率</b>：均为该股<b>历史</b>画像（非本次信号预测），基于 walk-forward 逐根重算的
      <b>实时(realtime)B 信号</b>（剥掉重绘 lookahead）。收益=次日开盘入场、持有 5 交易日、末日收盘平仓的平均值；
      均回撤=持有 5 日内每日相对入场价回撤（仅计跌破入场价的部分）的平均值；胜率=持有 5 日末收为正的比例；
      <b>收益/胜率/回撤均已剔除「次日即消失」的一日闪现 B</b>（实盘抓不住）；
      消失率=实时出现过的 B 里最终被重绘抹掉的比例（越低越可信，仍含一日闪现）。<br>
    · <b>S后5日均回撤 / S后5日下跌概率 / S消失率</b>：把 S 当见顶/回落信号的<b>历史</b>画像。
      均回撤=S 次日开盘为基准、持有 5 日内每日回撤（仅计跌破部分）的平均值（越深说明 S 后越易回落）；
      下跌概率=持有 5 日末收 < 次日开盘的比例（<b>反向胜率</b>，越高 S 越可靠）；两者均已剔除一日闪现 S；
      S消失率=实时出现过的 S 里最终被重绘抹掉的比例。
      单只样本量有限、未扣手续费，仅供横向参考。<br>
    · 本工具复刻 “S1 Formula v34” 指标，<b>该算法会重绘</b>：历史 K 线上的买卖点会随新数据变动/消失，Warning 区即用于追踪这一现象。<br>
    · 抓取失败/跳过的代码：{skipped_txt}
  </footer>
</div>

<div class="modal" id="modal">
  <div class="modal-box">
    <div class="modal-head">
      <div class="modal-title"><span id="m-sym"></span><span class="lg">
        <span class="b">▲ B 买点</span> · <span class="s">▼ S 卖点</span></span></div>
      <div class="modal-actions">
        <a id="m-tv" href="#" target="_blank" rel="noopener">在 TradingView 打开 ↗</a>
        <button class="modal-close" id="m-close" aria-label="关闭">×</button>
      </div>
    </div>
    <div id="chart"></div>
  </div>
</div>

{lib_script}
<script>
const CHART_DATA = {chart_json};
const $ = (id) => document.getElementById(id);
let _chart = null, _ro = null;

function openChart(sym) {{
  const d = CHART_DATA[sym];
  if (!d || !window.LightweightCharts) return;
  $('m-sym').textContent = sym;
  $('m-tv').href = 'https://www.tradingview.com/chart/?symbol=' + encodeURIComponent(sym);
  $('modal').classList.add('open');

  const box = $('chart');
  box.innerHTML = '';
  const LWC = window.LightweightCharts;
  _chart = LWC.createChart(box, {{
    layout: {{ background: {{ color: '#161b22' }}, textColor: '#8b98a9' }},
    grid: {{ vertLines: {{ color: '#232a34' }}, horzLines: {{ color: '#232a34' }} }},
    rightPriceScale: {{ borderColor: '#232a34' }},
    timeScale: {{ borderColor: '#232a34', timeVisible: false }},
    crosshair: {{ mode: 0 }},
    autoSize: false,
    width: box.clientWidth, height: box.clientHeight,
  }});
  const series = _chart.addSeries(LWC.CandlestickSeries, {{
    upColor: '#2fb35a', downColor: '#d63c9c', borderVisible: false,
    wickUpColor: '#2fb35a', wickDownColor: '#d63c9c',
  }});
  series.setData(d.bars);

  const markers = d.markers.map(m => m.side === 'B'
    ? {{ time: m.time, position: 'belowBar', color: '#2fb35a', shape: 'arrowUp', text: 'B' }}
    : {{ time: m.time, position: 'aboveBar', color: '#d63c9c', shape: 'arrowDown', text: 'S' }});
  LWC.createSeriesMarkers(series, markers);
  _chart.timeScale().fitContent();

  _ro = new ResizeObserver(() => {{
    if (_chart) _chart.applyOptions({{ width: box.clientWidth, height: box.clientHeight }});
  }});
  _ro.observe(box);
}}

function closeChart() {{
  $('modal').classList.remove('open');
  if (_ro) {{ _ro.disconnect(); _ro = null; }}
  if (_chart) {{ _chart.remove(); _chart = null; }}
}}

document.querySelectorAll('a.chart-link').forEach(a => {{
  a.addEventListener('click', () => openChart(a.dataset.sym));
}});
$('m-close').addEventListener('click', closeChart);
$('modal').addEventListener('click', (e) => {{ if (e.target === $('modal')) closeChart(); }});
document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') closeChart(); }});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 邮件正文（精简版，邮箱客户端友好：内联样式 + 纯表格 + 浅色主题）
# ---------------------------------------------------------------------------
def render_email(b_list, s_list, warnings, meta, bstats):
    """生成每日邮件 HTML 正文。与 dashboard 不同：无脚本/无图表，
    用内联样式和简单表格，兼容 Gmail/Outlook 等客户端。"""
    stamp = meta["run_et"].strftime("%Y-%m-%d %H:%M")

    def _num_td(txt, color):
        return (f'<td style="padding:8px;border-top:1px solid #edf0f4;text-align:right;'
                f'font-variant-numeric:tabular-nums;color:{color};">{txt}</td>')

    def _stat_tds(st):
        if not st:
            m = _num_td("—", "#9aa3b2")
            return m + m + m
        fwd = st.get("fwd_avg")
        dd = st.get("dd_avg")
        dis = st.get("dis_rate")
        fwd_td = (_num_td(f"{fwd:+.1f}%", "#1a8f45" if fwd > 0 else "#b8348a")
                  if fwd is not None else _num_td("—", "#9aa3b2"))
        dd_td = (_num_td(f"{dd:.1f}%", "#d97706") if dd is not None
                 else _num_td("—", "#9aa3b2"))
        if dis is None:
            dis_td = _num_td("—", "#9aa3b2")
        else:
            dc = "#1a8f45" if dis < 20 else ("#d97706" if dis < 50 else "#b8348a")
            dis_td = _num_td(f"{dis:.0f}%", dc)
        return fwd_td + dd_td + dis_td

    def bs_rows(items, empty_txt):
        if not items:
            return (f'<tr><td colspan="7" style="padding:10px 8px;color:#8a94a6;'
                    f'text-align:center;">{empty_txt}</td></tr>')
        out = []
        for it in items:
            hot = it["recency_cls"] == "today"
            pill_bg = "#e6f7ec" if hot else "#eef1f5"
            pill_fg = "#1a8f45" if hot else "#6b7280"
            out.append(
                f'<tr>'
                f'<td style="padding:8px;border-top:1px solid #edf0f4;font-weight:600;">{it["ticker"]}</td>'
                f'<td style="padding:8px;border-top:1px solid #edf0f4;color:#4b5563;font-variant-numeric:tabular-nums;">{it["last_date"]}</td>'
                f'<td style="padding:8px;border-top:1px solid #edf0f4;">'
                f'<span style="font-size:12px;padding:2px 8px;border-radius:10px;background:{pill_bg};color:{pill_fg};">{it["recency"]}</span></td>'
                f'<td style="padding:8px;border-top:1px solid #edf0f4;text-align:right;font-variant-numeric:tabular-nums;">{it["price"]:.2f}</td>'
                f'{_stat_tds(bstats.get(it["ticker"]))}'
                f'</tr>'
            )
        return "\n".join(out)

    def warn_rows_html(ws):
        if not ws:
            return ('<tr><td colspan="4" style="padding:10px 8px;color:#8a94a6;'
                    'text-align:center;">暂无消失记录</td></tr>')
        out = []
        for w in ws:
            is_b = w["side"] == "B"
            tag_bg = "#e6f7ec" if is_b else "#fbe8f4"
            tag_fg = "#1a8f45" if is_b else "#b8348a"
            out.append(
                f'<tr>'
                f'<td style="padding:8px;border-top:1px solid #edf0f4;font-weight:600;">{w["ticker"]}</td>'
                f'<td style="padding:8px;border-top:1px solid #edf0f4;">'
                f'<span style="font-size:12px;padding:2px 8px;border-radius:6px;background:{tag_bg};color:{tag_fg};font-weight:600;">{w["side"]} 消失</span></td>'
                f'<td style="padding:8px;border-top:1px solid #edf0f4;color:#d97706;font-variant-numeric:tabular-nums;">{w["bar_date"]}</td>'
                f'<td style="padding:8px;border-top:1px solid #edf0f4;color:#4b5563;">{w["detected_on"]}</td>'
                f'</tr>'
            )
        return "\n".join(out)

    def section(title, dot, count, headers, rows_html):
        # 数值列（现价及其后的统计列，索引 >=3）右对齐，与单元格一致
        ths = "".join(
            f'<th style="text-align:{"right" if (i>=3 or i==len(headers)-1) else "left"};'
            f'padding:6px 8px;color:#8a94a6;font-size:11px;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:.4px;">{h}</th>'
            for i, h in enumerate(headers)
        )
        return f"""
      <tr><td style="padding:22px 24px 0;">
        <div style="font-size:15px;font-weight:700;color:#111827;margin-bottom:8px;">
          <span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{dot};margin-right:7px;"></span>
          {title} <span style="color:#9aa3b2;font-weight:400;font-size:13px;">（{count}）</span>
        </div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;color:#1f2937;">
          <tr>{ths}</tr>
          {rows_html}
        </table>
      </td></tr>"""

    b_sec = section("B 买点 · 当日/近三日", "#2fb35a", len(b_list),
                    ["代码", "最近B日期", "时点", "现价", "B后5日均收益", "B后5日均回撤", "B消失率"],
                    bs_rows(b_list, "近三日无 B 买点"))
    s_sec = section("S 卖点 · 当日/近三日", "#d63c9c", len(s_list),
                    ["代码", "最近S日期", "时点", "现价", "B后5日均收益", "B后5日均回撤", "B消失率"],
                    bs_rows(s_list, "近三日无 S 卖点"))
    w_sec = section("⚠ 近期消失的买卖点", "#f0a020", len(warnings),
                    ["代码", "类型", "消失节点", "检测于"],
                    warn_rows_html(warnings))

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f9;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:20px 0;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e6e9ef;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;">
        <tr><td style="padding:22px 24px 6px;">
          <div style="font-size:19px;font-weight:800;color:#0f172a;letter-spacing:.3px;">S1 买卖点扫描 · 每日速递</div>
          <div style="font-size:12px;color:#8a94a6;margin-top:4px;">数据截至 {meta['data_last']} · 生成于 {stamp} 美东 · 股票池 {meta['universe_n']} 只（成功 {meta['ok_n']}）</div>
        </td></tr>
        <tr><td style="padding:14px 24px 0;">
          <a href="{LIVE_URL}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;font-size:13px;font-weight:600;padding:9px 16px;border-radius:8px;">打开完整看板（可点开 K 线图）↗</a>
        </td></tr>
        {b_sec}
        {s_sec}
        {w_sec}
        <tr><td style="padding:20px 24px 24px;">
          <div style="border-top:1px solid #edf0f4;padding-top:12px;font-size:11px;color:#9aa3b2;line-height:1.6;">
            · 附件 dashboard.html 为离线完整版，双击打开可点代码弹出 K 线图（B/S 已标注）。<br>
            · 本工具复刻 “S1 Formula v34” 指标，<b>该算法会重绘</b>：历史买卖点会随新数据变动/消失，消失区即用于追踪。<br>
            · 抓取跳过的代码：{("、".join(meta["skipped"]) if meta["skipped"] else "无")}
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    # Windows 控制台中文输出
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    et = now_et()
    today_str = et.strftime("%Y-%m-%d")
    today = dt.date.fromisoformat(today_str)

    tickers = load_universe()
    data = fetch_all(tickers)
    skipped = [t for t in tickers if t not in data]
    print(f"成功 {len(data)} / {len(tickers)}，跳过：{skipped}", flush=True)

    state = load_state()
    prev_tickers = state.get("tickers", {})

    # 抓取成功率过低（多半是限流/断网）时中止，避免用空快照覆盖历史、清掉消失记录
    if len(data) < max(1, len(tickers)) * 0.30:
        print(f"⚠ 仅抓到 {len(data)}/{len(tickers)}，疑似限流/断网。"
              f"保留上次的 state.json 与 dashboard.html，不做覆盖。", flush=True)
        sys.exit(2)

    new_tickers_state = {}
    b_list = []
    s_list = []
    new_warnings = []
    data_last = ""
    chart_data = {}   # {ticker: {'bars':[...], 'markers':[...]}}
    computed = {}     # {ticker: cur}，缓存本次算好的信号，供 warning 补图

    for t, df in data.items():
        try:
            cur = compute_signals(df)
        except Exception as e:
            print(f"  {t} 计算失败: {e}", flush=True)
            continue

        dates = cur["dates"]
        if not dates:
            continue
        computed[t] = cur
        data_last = max(data_last, dates[-1])
        recent = set(dates[-RECENT_DAYS:])
        price = cur["closes"][-1]

        # 近三日 B / S 名单
        recent_b = [d for d in cur["b_dates"] if d in recent]
        recent_s = [d for d in cur["s_dates"] if d in recent]
        if recent_b:
            last_d = max(recent_b)
            b_list.append({
                "ticker": t, "last_date": last_d, "price": price,
                "recency": days_ago_label(last_d, dates),
                "recency_cls": "today" if last_d == dates[-1] else "recent",
                "sort": dates.index(last_d),
            })
        if recent_s:
            last_d = max(recent_s)
            s_list.append({
                "ticker": t, "last_date": last_d, "price": price,
                "recency": days_ago_label(last_d, dates),
                "recency_cls": "today" if last_d == dates[-1] else "recent",
                "sort": dates.index(last_d),
            })
        # 上榜股票（B 或 S）额外保存画图数据
        if recent_b or recent_s:
            chart_data[t] = build_chart_data(df, cur)

        # 消失检测
        tw = detect_disappearances(t, prev_tickers.get(t), cur, today_str)
        new_warnings.extend(tw)
        # 本次产生消失告警的股票也存图（方便直接看消失节点原本所在的 K 线）
        if tw and t not in chart_data:
            chart_data[t] = build_chart_data(df, cur)

        # 保存本次快照
        new_tickers_state[t] = {
            "b_dates": cur["b_dates"],
            "s_dates": cur["s_dates"],
            "run_date": today_str,
            "data_last": dates[-1],
        }

    # 最新的排最前，其次按代码
    b_list.sort(key=lambda x: (-x["sort"], x["ticker"]))
    s_list.sort(key=lambda x: (-x["sort"], x["ticker"]))

    warnings = merge_warnings(state.get("warnings", []), new_warnings, today)
    # 展示排序：检测日新的在前，其次消失节点新的在前
    warnings.sort(key=lambda w: (w["detected_on"], w["bar_date"], w["ticker"]), reverse=True)

    # 为 warning 区里仍缺图的股票补图（含往日保留的旧告警，只要本次抓到了数据）
    for w in warnings:
        t = w["ticker"]
        if t not in chart_data and t in computed and t in data:
            chart_data[t] = build_chart_data(data[t], computed[t])

    # 上榜股票的历史画像（walk-forward 逐根重算，成本较高）：
    #   B 榜 → B 画像（均收益/胜率/均回撤/消失率）；S 榜 → S 画像（均回撤/下跌概率/消失率）。
    # 共用 STATS_MAX_TICKERS 预算兜底防 CI 超时（每只每侧一次 walk）。
    def _dedup(seq):
        s, out = set(), []
        for t in seq:
            if t not in s:
                s.add(t); out.append(t)
        return out
    b_tickers = _dedup(x["ticker"] for x in b_list)
    s_tickers = _dedup(x["ticker"] for x in s_list)
    budget = STATS_MAX_TICKERS
    if len(b_tickers) + len(s_tickers) > budget:
        print(f"⚠ 上榜 B{len(b_tickers)}/S{len(s_tickers)} 只，画像计算超预算 {budget}，"
              f"按 B 优先、达上限即止。", flush=True)
    bstats, sstats = {}, {}
    print(f"计算上榜股画像：B {len(b_tickers)} 只 / S {len(s_tickers)} 只 ...", flush=True)
    for t in b_tickers:
        if budget <= 0:
            break
        if t in data and t in computed:
            budget -= 1
            try:
                st = b_symbol_stats(data[t], computed[t]["b_dates"])
            except Exception as e:
                print(f"  {t} B画像计算失败: {e}", flush=True)
                st = None
            if st:
                bstats[t] = st
    for t in s_tickers:
        if budget <= 0:
            break
        if t in data and t in computed:
            budget -= 1
            try:
                st = s_symbol_stats(data[t], computed[t]["s_dates"])
            except Exception as e:
                print(f"  {t} S画像计算失败: {e}", flush=True)
                st = None
            if st:
                sstats[t] = st

    # 重绘率统计：把「昨日→今日」这一步的信号存活情况累积进 state（云端逐日累积，供 dashboard 展示）。
    # 用 prev_run < today 作闸：同一天重复跑（如手动 dispatch 多次）不会重复计入。
    prev_run = next((v.get("run_date") for v in prev_tickers.values() if v.get("run_date")), None)
    rstats = state.get("repaint_stats") or {"buckets": {}, "transitions": 0, "since": None, "updated": None}
    if prev_run and prev_run < today_str:
        day_t = tally_repaint(prev_tickers, new_tickers_state)
        if day_t:
            for b, (tot, gone) in day_t.items():
                cur = rstats["buckets"].get(b, [0, 0])
                rstats["buckets"][b] = [cur[0] + tot, cur[1] + gone]
            rstats["transitions"] = rstats.get("transitions", 0) + 1
            rstats["since"] = rstats.get("since") or prev_run
            rstats["updated"] = today_str

    meta = {
        "run_et": et,
        "data_last": data_last or "-",
        "universe_n": len(tickers),
        "ok_n": len(data),
        "skipped": skipped,
        "lwc_lib": load_lwc_lib(),
        "repaint_stats": rstats,
    }
    html = render_html(b_list, s_list, warnings, meta, chart_data, bstats, sstats)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    # 更新状态
    state["last_run"] = et.strftime("%Y-%m-%d %H:%M ET")
    state["tickers"] = new_tickers_state
    state["warnings"] = warnings
    state["repaint_stats"] = rstats
    save_state(state)

    # 邮件正文 + 主题（供 CI 发信；本地跑也会生成，无害）
    with open(EMAIL_HTML, "w", encoding="utf-8") as f:
        f.write(render_email(b_list, s_list, warnings, meta, bstats))
    subject = f"S1 扫描 {today_str} · B{len(b_list)} / S{len(s_list)} / 消失{len(warnings)}"
    with open(EMAIL_SUBJECT, "w", encoding="utf-8") as f:
        f.write(subject)

    print(f"完成：B {len(b_list)} 只 / S {len(s_list)} 只 / 消失告警 {len(warnings)} 条", flush=True)
    print(f"已生成 {OUTPUT_HTML}", flush=True)


if __name__ == "__main__":
    main()
