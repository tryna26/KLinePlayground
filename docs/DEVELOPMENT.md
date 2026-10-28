# 开发基线与启动

本目录基于 [fat7/KLinePlayground](https://github.com/fat7/KLinePlayground) 的 v3.2。
多股票历史模拟已包含组合账户、HTTP 接口、保存恢复、操作页面、真实数据接入和 Windows 候选包。选择用户后点击“组合训练”；初学者见 [USER_GUIDE.md](USER_GUIDE.md)，页面说明见 [PORTFOLIO_UI.md](PORTFOLIO_UI.md)，数据层见 [DATA_REPLAY.md](DATA_REPLAY.md)，引擎见 [PORTFOLIO_ENGINE.md](PORTFOLIO_ENGINE.md)，HTTP 用法见 [PORTFOLIO_API.md](PORTFOLIO_API.md)。阶段六验收见 [STEP6_VERIFICATION.md](STEP6_VERIFICATION.md)。

## Windows 环境

使用标准 64 位 CPython **3.13.17**（见 `.python-version`）。`requirements.txt` 固定所有直接和间接依赖。阶段一至五原始验收使用 3.12.14；阶段六在独立 3.13.17 环境重新验证并打包，避免某嵌入式 Python 冻结后 HTTPS 不可用的问题。
不要使用系统中的 Python 3.7。Web 版和 PyWebView 桌面版共用这一环境；桌面窗口需要 Windows WebView2 Runtime。

在项目根目录运行 PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_dev.py
```

浏览器打开 <http://127.0.0.1:5050>。关闭服务使用 Ctrl+C。也可以直接运行 `./start_dev.ps1`。
如果 Python 没有注册进 `py` 启动器，用已安装的 Python 3.13 完整路径替换 `py -3.13`。

桌面窗口：

```powershell
.\.venv\Scripts\python.exe run_dev.py --desktop
```

桌面模式自动分配本地端口。Web 模式通过 `--port 5051` 更换端口；两种模式都只监听 `127.0.0.1`，不启用调试器。

## 数据与用户目录

源代码和前端资源位置由程序文件定位，不依赖当前工作目录。开发默认写入：

| 内容 | 默认路径 |
| --- | --- |
| 行情、股票列表、复权因子、离线行情、AI 配置和 API 信息 | `.runtime/data/` |
| 用户设置、交易记录和训练历史 | `.runtime/users/` |

支持分别指定目录（可放在源码目录之外）：

```powershell
.\.venv\Scripts\python.exe run_dev.py --data-dir .runtime/my-quotes --users-dir .runtime/my-users
```

也支持 `--home .runtime/practice`，以及环境变量 `KLINE_TRAINER_HOME`、`KLINE_TRAINER_DATA_DIR`、`KLINE_TRAINER_USERS_DIR`。
优先级：同名命令行参数覆盖环境变量；独立目录设置覆盖 home 下的 `data`、`users`；最后使用默认值。
相对的显式目录按启动时的工作目录解析；默认目录始终在本开发副本中。行情和用户目录不能相同或互相包含。
桌面、Web、AI 配置信息和交易数据库使用同一套目录规则。

旧安装布局为 `<安装根目录>/bin/程序.exe`，冻结程序默认写入安装根目录下的 `data` 和 `users`；`KLINE_TRAINER_HOME` 可覆盖该根目录。
Windows 候选包的构建与验收见 [BUILD_GUIDE.md](../BUILD_GUIDE.md)。不要把开发数据目录指向原安装目录。新环境首次启动没有原用户；在界面创建新的本地用户。

## 行情与原功能

默认界面选择腾讯日线；请求设有超时与有限重试，腾讯请求单独绕过环境代理，不修改系统代理。
原 AKShare 数据源保留其腾讯备用逻辑。mootdx、XTQuant 为可选依赖，本基线没有安装，界面显示不可用。
Python `openai`、`lightweight-charts` 包不是本入口的必要依赖：AI 请求使用 requests，前端图表使用仓库自带的 JavaScript 文件。
原项目的独立演示及旧构建脚本不在本阶段的运行验收范围内。

在线下载需要网络。随机在线训练还需要股票列表；股票列表使用 AKShare 下载，失败时会报错，不能以测试股票池替代完整市场。
原单股在线行情缓存沿用 v3.2 文件格式。阶段二的组合模块使用独立 `portfolio_replay/` 缓存、来源元信息、公司行为准入检查和未来数据隔离，不自动信任或导入旧缓存。
原单股交易规则保持原样；组合页面已接入独立成交、T+1、冻结、历史费率和公司行为核算规则。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts/verify_baseline.py
.\.venv\Scripts\python.exe scripts/verify_baseline.py --online
.\.venv\Scripts\python.exe scripts/verify_replay.py
.\.venv\Scripts\python.exe scripts/verify_replay.py --online
.\.venv\Scripts\python.exe scripts/verify_portfolio.py
```

回归脚本创建临时目录、独立用户并启动真实 HTTP 服务，退出时终止该服务。默认使用清楚标记的合成行情，检查指定与随机训练、买入、T+1、下一根 K 线、卖出、复盘保存，以及在外部工作目录启动。
`--online` 额外从腾讯下载 000001 的 2024 年上半年行情到临时缓存并通过同一 HTTP 服务启动和推进训练。在线验证依赖外部服务，不作为离线测试的一部分。
仓库不分发下载到的真实行情或真实用户记录。

`verify_replay.py` 验证三股同日状态、切换日期不变及未来数据拒绝访问；`--online` 额外检查真实公共日历、腾讯三股原始行情，以及未核实区间的准入阻断。全部使用临时目录。组合训练不会因取得有效行情而自动开放缺少公司行为或历史状态完整性证明的真实区间。

`verify_portfolio.py` 展示阶段三效果：两股共用现金、下一开盘成交、T+1、部分卖出与成本结转、重复推进去重、SQLite 一致提交和账本重算。使用临时目录和合成行情。当前网页另有“组合训练”入口，完整浏览器操作与重启验收见 [STEP5_VERIFICATION.md](STEP5_VERIFICATION.md)。

阶段四运行 `python scripts/verify_portfolio_api.py`：在临时目录启动真实本机 HTTP，完成部分卖出、两股持仓、保留待成交订单、强制重启和事务中断，逐字段验证恢复与重试。恢复后的服务关闭合成创建入口；原训练仍可继续。手动调用合成接口可用 `python run_dev.py --portfolio-demo --data-dir .runtime/step4-quotes --users-dir .runtime/step4-users`，用法见 [PORTFOLIO_API.md](PORTFOLIO_API.md)。不开此开关时默认拒绝合成创建；真实数据仍需后端核验证明。

详细来源与验收结果见 [BASELINE.md](BASELINE.md)、[STEP1_VERIFICATION.md](STEP1_VERIFICATION.md)、[STEP2_VERIFICATION.md](STEP2_VERIFICATION.md)、[STEP3_VERIFICATION.md](STEP3_VERIFICATION.md)、[STEP4_VERIFICATION.md](STEP4_VERIFICATION.md) 和 [STEP5_VERIFICATION.md](STEP5_VERIFICATION.md)。
