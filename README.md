# S1 每日买卖点扫描机器人

复刻 TradingView 指标 **“S1 Formula v34”**（`S1.txt`）的 **B 买点 / S 卖点** 逻辑，
每日自动扫描 100 只美股股票池，生成一个 H5 网页 `dashboard.html`：

- **B 买点名单**：当日或近三日出现 B 的股票
- **S 卖点名单**：当日或近三日出现 S 的股票
- **⚠ Warning 消失区**：近期 B/S 买卖点「消失」的股票，并标注**消失的 K 线节点**
- 每个代码可**点击直接跳转 TradingView** 该股走势图

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `s1_signals.py` | 信号引擎，一比一移植 Pine 的 B/S 算法 |
| `universe.txt`  | 股票池（每行一个代码，`#` 注释）。**改股票池只需编辑这里** |
| `scan.py`       | 主程序：抓数据 → 算信号 → 检测消失 → 生成网页 |
| `run.bat`       | 运行入口（双击手动跑，或被任务计划调用） |
| `dashboard.html`| 生成的结果网页（用浏览器打开） |
| `state.json`    | 历史快照，自动累积，用于「消失」对比。**勿手动删**（删了 Warning 历史会清空） |
| `run.log`       | 定时运行的日志 |

数据源：**yfinance（免费，无需 API key）**。

---

> **想上线到云端**（不用本机开机、生成公网网址）？见 **`DEPLOY.md`**：一键 GitHub Actions + Pages，免费。

## 手动运行

双击 `run.bat`，或命令行：

```
cd /d "C:\Users\shuyongzhang\claude materials\s1_bot"
python scan.py
```

跑完自动打开 `dashboard.html`。

---

## 关于「消失区」（重要）

原指标开头就声明 **它会重绘（repaint）**：历史 K 线上的买卖点会随后续新数据的加入而
变化甚至消失。这正是本工具 Warning 区要追踪的东西。

机制：每天把算出的 B/S 信号存进 `state.json`，第二天用新结果和昨天的快照对比，
把「昨天还在、今天没了」的信号列为消失，并标出它原本所在的 K 线日期（消失节点）。

> 因此 **第一天运行 Warning 区是空的**（没有历史可比），之后每天逐步显现。
> 默认只追踪最近 15 个交易日内的信号消失，告警在页面保留 7 天。
> 这些参数可在 `scan.py` 顶部调整（`DISAPPEAR_LOOKBACK` / `WARN_KEEP_DAYS` / `RECENT_DAYS`）。

---

## 设置每日自动运行（Windows 任务计划程序）

⚠ 本机是**中国时区 (UTC+8)**。美东中午 12:00 ≈ 中国 **00:00（夏令时）/ 01:00（冬令时）**。

**说明**：该指标使用的是「已完成的日线」，中午 12:00 当天 K 线尚未收盘，用的仍是前一交易日
数据；所以**运行的具体时刻不影响信号结果**，只要在美股收盘后跑即可。凌晨要求开机不便，
下面默认给一个更实用的中国时间 **每天 08:00**（此时美股已收盘，抓到的是完整的上一交易日）。

在 **cmd** 里执行一次（无需管理员）：

```cmd
schtasks /create /tn "S1DailyScan" /tr "\"C:\Users\shuyongzhang\claude materials\s1_bot\run.bat\"" /sc daily /st 08:00 /f
```

- 想严格按「美东中午 12:00」→ 把 `/st 08:00` 改成 `/st 00:00`（夏令时）。
- 查看任务：`schtasks /query /tn "S1DailyScan"`
- 立即测试触发：`schtasks /run /tn "S1DailyScan"`
- 删除任务：`schtasks /delete /tn "S1DailyScan" /f`

> 前提：到点时电脑需处于开机状态（可在「任务计划程序」里勾选
> “如果错过计划的开始时间，则尽快启动任务” 以补跑）。

---

## 修改股票池

直接编辑 `universe.txt`，每行一个美股代码，`#` 开头是注释。
抓不到数据的代码会在 `run.log` 和网页底部列出，按需增删即可。
