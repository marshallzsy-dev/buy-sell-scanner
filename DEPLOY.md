# 上线部署：GitHub Actions + GitHub Pages（免费）

部署后：每天由 GitHub 云端自动跑，**不需要你电脑开机**，结果是一个公网网址，手机也能看。

前置：一个 GitHub 账号；本机装了 git（`git --version` 能显示版本即可）。

---

## 步骤 1 · 在 GitHub 新建一个空仓库

打开 https://github.com/new ：
- Repository name：例如 `s1-scanner`
- 选 **Public**（公开）
- **不要**勾 “Add a README / .gitignore / license”（保持空仓库）
- 点 Create repository

记下仓库地址，形如 `https://github.com/你的用户名/s1-scanner.git`

## 步骤 2 · 把本地文件推上去

在 **本文件夹**（`s1_bot`）里打开命令行（cmd 或 PowerShell），依次执行（把 URL 换成你的）：

```bash
git init
git add .
git commit -m "S1 scanner 初始化"
git branch -M main
git remote add origin https://github.com/你的用户名/s1-scanner.git
git push -u origin main
```

> 说明：`.gitignore` 已配置——`dashboard.html`（CI 每次自动生成）和日志不入库，
> 但 `state.json`（消失检测的历史快照）会入库，把你本地已有的历史一起带上去。

## 步骤 3 · 开启 GitHub Pages

仓库页面 → **Settings** → 左侧 **Pages** → **Build and deployment** → Source 选 **GitHub Actions**。
（不用选分支，工作流会自动部署。）

## 步骤 4 · 允许工作流写回仓库

仓库 → **Settings** → **Actions** → **General** → 最下面 **Workflow permissions** →
选 **Read and write permissions** → Save。
（这样每天跑完才能把更新后的 `state.json` 提交回仓库。）

## 步骤 5 · 手动跑一次验证

仓库 → **Actions** 标签 → 左侧 **S1 daily scan** → 右侧 **Run workflow** → 绿色按钮。
等 1~2 分钟跑完（第一次会装依赖稍慢）。跑完后：

- 网页地址：**`https://你的用户名.github.io/s1-scanner/`**
- 之后每天按 `scan.yml` 里的 cron 自动更新。

---

## 步骤 6 · 开启每日邮件速递（可选）

跑完扫描后自动发一封邮件（B 名单 / S 名单 / 消失区 + 看板链接，并附上离线 `dashboard.html`）。
用 Gmail 发信，需要三个 secret。

**先拿 Gmail 应用专用密码**（不是你的登录密码）：
1. 打开 https://myaccount.google.com/security ，确保**两步验证**已开启。
2. 打开 https://myaccount.google.com/apppasswords ，起个名字（如 `s1-scanner`）→ 生成 →
   得到 16 位密码（形如 `abcd efgh ijkl mnop`），**去掉空格**记下来。

**再到仓库配置 secret**：仓库 → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**，加三个：

| Name | Value |
|------|-------|
| `MAIL_USERNAME` | 你的发信 Gmail，如 `you@gmail.com` |
| `MAIL_PASSWORD` | 上面拿到的 16 位应用专用密码（无空格） |
| `MAIL_TO` | 收件人邮箱；多个用英文逗号分隔，如 `a@x.com,b@y.com` |

配好后，下次定时（或手动 Run workflow）跑完就会收到邮件。
> 说明：只有 scan **成功**才发邮件；遇到 yfinance 限流中止时不会发。
> 若没配 `MAIL_TO`，发信步骤会自动跳过，不影响其余流程。

---

## 定时时间说明

`.github/workflows/scan.yml` 里：

```yaml
- cron: '7 16 * * 1-5'   # 16:07 UTC ≈ 美东中午12点（夏令时），周一到周五
```

- cron 用的是 **UTC**，且不随夏令时自动调整。
- 美东中午 12:00 ≈ **16:00 UTC**（夏令时）/ 17:00 UTC（冬令时）。
- **更推荐**：改成 `'0 21 * * 1-5'`（21:00 UTC ≈ 美股收盘后），这样能拿到**当天完整收盘的日线**，信号更新鲜。
- GitHub 的 cron 在整点负载高时可能延迟几到十几分钟，对每日收盘数据无影响。

## 和本地任务的关系

上云后，本机的 `S1DailyScan` 计划任务就多余了，可以删掉（也可留着当备份）：

```cmd
schtasks /delete /tn "S1DailyScan" /f
```

## 想改成私有仓库？

公开仓库的 Pages 免费。若要私有仓库 + 私有 Pages，需要 GitHub Pro；
或改用 Cloudflare Pages / Netlify 部署并加访问控制——需要的话告诉我，我再给方案。
