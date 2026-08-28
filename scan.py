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
VENDOR_DIR = os.path.join(BASE, "vendor")
VENDOR_JS = os.path.join(VENDOR_DIR, "lightweight-charts.js")
LWC_CDN = "https://unpkg.com/lightweight-charts@5.0.4/dist/lightweight-charts.standalone.production.js"

RECENT_DAYS = 3          # 「近三日」窗口（交易日）
DISAPPEAR_LOOKBACK = 15  # 只对最近 N 个交易日内的信号消失发 Warning
WARN_KEEP_DAYS = 7       # Warning 在页面上保留的天数（按检测日历日）
HISTORY_PERIOD = "2y"    # 拉取历史长度
CHART_BARS = 250         # 弹层图表保留的最近 K 线根数（约 1 年）


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


def render_html(b_list, s_list, warnings, meta, chart_data):
    et = meta["run_et"]
    stamp = et.strftime("%Y-%m-%d %H:%M")

    def row_bs(item):
        code = item["ticker"]
        has = code in chart_data
        cell = (f'<a class="chart-link" data-sym="{code}">{code}</a>' if has
                else f'<a href="{tv_url(code)}" target="_blank" rel="noopener">{code}</a>')
        return (
            f'<tr>'
            f'<td class="code">{cell}</td>'
            f'<td>{item["last_date"]}</td>'
            f'<td><span class="pill {item["recency_cls"]}">{item["recency"]}</span></td>'
            f'<td class="num">{item["price"]:.2f}</td>'
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

    b_rows = "\n".join(row_bs(x) for x in b_list) or '<tr><td colspan="4" class="empty">近三日无 B 买点</td></tr>'
    s_rows = "\n".join(row_bs(x) for x in s_list) or '<tr><td colspan="4" class="empty">近三日无 S 卖点</td></tr>'
    warn_rows = "\n".join(row_warn(w) for w in warnings) or \
        '<tr><td colspan="4" class="empty">暂无消失记录（需累积历史快照，运行几天后逐步显现）</td></tr>'

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

  <section class="warn-sec">
    <div class="stitle"><span class="dot a"></span> ⚠ Warning · 近期消失的买卖点 <span class="cnt">（{len(warnings)}）</span></div>
    <table>
      <thead><tr><th>代码</th><th>类型</th><th>消失节点(K线日期)</th><th>检测于</th></tr></thead>
      <tbody>{warn_rows}</tbody>
    </table>
  </section>

  <section>
    <div class="stitle"><span class="dot g"></span> B 买点 · 当日/近三日 <span class="cnt">（{len(b_list)}）</span></div>
    <table>
      <thead><tr><th>代码</th><th>最近B日期</th><th>时点</th><th>现价</th></tr></thead>
      <tbody>{b_rows}</tbody>
    </table>
  </section>

  <section>
    <div class="stitle"><span class="dot p"></span> S 卖点 · 当日/近三日 <span class="cnt">（{len(s_list)}）</span></div>
    <table>
      <thead><tr><th>代码</th><th>最近S日期</th><th>时点</th><th>现价</th></tr></thead>
      <tbody>{s_rows}</tbody>
    </table>
  </section>

  <footer>
    · 点击代码弹出该股 K 线图，B/S 买卖点已标在图上（可缩放、拖动）；无图表数据的代码则跳 TradingView。<br>
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
    chart_data = {}   # {ticker: {'bars':[...], 'markers':[...]}}，仅上榜股票

    for t, df in data.items():
        try:
            cur = compute_signals(df)
        except Exception as e:
            print(f"  {t} 计算失败: {e}", flush=True)
            continue

        dates = cur["dates"]
        if not dates:
            continue
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
        new_warnings.extend(detect_disappearances(t, prev_tickers.get(t), cur, today_str))

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

    meta = {
        "run_et": et,
        "data_last": data_last or "-",
        "universe_n": len(tickers),
        "ok_n": len(data),
        "skipped": skipped,
        "lwc_lib": load_lwc_lib(),
    }
    html = render_html(b_list, s_list, warnings, meta, chart_data)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    # 更新状态
    state["last_run"] = et.strftime("%Y-%m-%d %H:%M ET")
    state["tickers"] = new_tickers_state
    state["warnings"] = warnings
    save_state(state)

    print(f"完成：B {len(b_list)} 只 / S {len(s_list)} 只 / 消失告警 {len(warnings)} 条", flush=True)
    print(f"已生成 {OUTPUT_HTML}", flush=True)


if __name__ == "__main__":
    main()
