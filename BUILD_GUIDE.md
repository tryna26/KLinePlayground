# Windows 正式版构建

发布版本：4.0.0。使用 Windows 10/11 x64、标准 CPython **3.13.17**、PyInstaller **6.22.3**、Inno Setup **6.7.3**。依赖固定在 requirements.txt / requirements-build.txt。发布构建使用标准 CPython，避免特定嵌入式 Python 的冻结 HTTPS 问题。

## 干净环境

```powershell
git clone https://github.com/tryna26/KLinePlayground.git
cd KLinePlayground
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m pip check
```

从 https://jrsoftware.org/ 下载 Inno Setup 6.7.3 并安装。构建工具的许可与应用许可分开，见 THIRD_PARTY_NOTICES.md。也可使用官方 Python NuGet 包的隔离运行时创建环境，不修改系统 PATH 或原 Python。

## 构建与验收

```powershell
./scripts/build_windows.ps1 -Iscc 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

`-Python` 可指向独立构建虚拟环境，`-Iscc` 也可通过 `ISCC_EXE` 环境变量指定。脚本依次检查依赖、收集许可、按 packaging/windows.spec 打包、用 packaging/windows.iss 编译安装器、生成便携包和清单。旧 install.bat 已改为调用此脚本；根目录旧 .spec 与 installer_source 属于上游历史，不用于新版发布。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
node --test tests/js/portfolio_ui_math.test.cjs
.\.venv\Scripts\python.exe scripts/verify_baseline.py
.\.venv\Scripts\python.exe scripts/verify_portfolio_api.py
.\.venv\Scripts\python.exe scripts/verify_real_portfolio.py
.\.venv\Scripts\python.exe scripts/verify_windows_package.py --installer dist/release/KLinePlayground-4.0.0-windows-x64-setup.exe --output .runtime/package-check-v4
.\.venv\Scripts\python.exe scripts/verify_release_features.py --exe .runtime/package-check-v4/installed/bin/KLinePlayground.exe --output .runtime/features-check-v4
```

安装验收实际安装到全新测试目录，使用打包后的程序下载真实数据、创建三股组合、下一开盘成交、部分卖出、保留待成交后重启，核对状态、结束报告和原单股 T+1。`verify_release_features.py` 使用明确标注的离线人工样本，实际调用打包程序验证自由选股、非交易日对齐、委托撤销与重提、盲盒、已结束训练重置、重试与重新启动。它还检查随包前端包含持仓、委托分组和重置入口；便携包解压后也应对其 EXE 运行该脚本。测试输出目录必须不存在，结果保留供检查。联网验收依赖外部数据服务。

## 产物与运行

dist/release 包含安装程序、便携 ZIP、bundle-manifest.json 和 SHA256SUMS.txt。安装程序默认仅安装到当前用户目录，无需管理员权限；便携包解压后运行 `KLinePlayground/bin/KLinePlayground.exe`。均需要 Microsoft Edge WebView2 Runtime。支持 `--portfolio` 直达组合入口、`--home` 指定数据目录；详见 docs/TROUBLESHOOTING.md。

数据和用户目录不打进包内；不包含预下载真实行情、开发者配置、API 凭据或测试用户。首次使用时创建本地用户，从“新建训练”选择单股或组合、指定模式或随机盲盒。组合支持自由选择 1–20 只股票并自动加载日线，无需先接入旧版三股核验区间。更新与卸载保留运行数据。

发布包没有代码签名。下载后先核对发布页 SHA256；安装包与便携包通过 GitHub Releases 分发，不提交进源码仓库。
