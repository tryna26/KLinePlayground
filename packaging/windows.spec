# -*- mode: python ; coding: utf-8 -*-
"""Allowlisted resources only. No runtime/user directories or local configuration."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

root = Path(SPECPATH).resolve().parent
data = [(str(root / "frontend"),"frontend"),(str(root / "LICENSE"),"."),
        (str(root / "THIRD_PARTY_NOTICES.md"),"."),(str(root / "build" / "licenses"),"LICENSES"),
        (str(root / "docs" / "USER_GUIDE.md"),"docs"),
        (str(root / "docs" / "PORTFOLIO_UI.md"),"docs"),(str(root / "docs" / "TROUBLESHOOTING.md"),"docs")]
for package in ("akshare","py_mini_racer","webview","pythonnet","clr_loader","certifi"):
    data += collect_data_files(package)
for package in ("akshare","mini-racer","pywebview","pandas","numpy"):
    data += copy_metadata(package)
a = Analysis([str(root / "webview_app" / "main_pywebview.py")],pathex=[str(root)],
    binaries=collect_dynamic_libs("py_mini_racer"),datas=data,
    hiddenimports=["webview.platforms.winforms","webview.platforms.edgechromium","lxml._elementpath"],
    excludes=["tests","mootdx","xtquant","pytest","IPython","matplotlib","PyQt5","PyQt6","PySide2","PySide6"],
    noarchive=False,optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz,a.scripts,[],exclude_binaries=True,name="KLinePlayground",console=False,
    debug=False,strip=False,upx=False,icon=str(root / "frontend" / "assets" / "favicon.ico"))
coll = COLLECT(exe,a.binaries,a.datas,strip=False,upx=False,name="KLinePlayground")
