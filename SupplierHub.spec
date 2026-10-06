# -*- mode: python ; coding: utf-8 -*-
# Сборка SupplierHub.exe: pyinstaller --noconfirm --clean SupplierHub.spec
# (проще запустить build_windows.bat — он сам поставит всё нужное).
#
# Собирается папка dist/SupplierHub/ с SupplierHub.exe внутри. Рядом с exe
# приложение хранит config.toml, state.json, stock/ и bot.log, поэтому
# папку целиком можно переносить и архивировать.

datas = [
    ("supplierhub/web", "supplierhub/web"),
    ("supplierhub/assets", "supplierhub/assets"),
]

a = Analysis(
    ["supplierhub/__main__.py"],
    pathex=["."],
    datas=datas,
    # Хук pywebview (webview/__pyinstaller) сам добавит WebView2 и свои js-файлы.
    # socksio httpx грузит лениво — без подсказки SOCKS-прокси в exe не заработают.
    hiddenimports=["socksio"],
    excludes=["tkinter", "pytest", "playwright", "PIL", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SupplierHub",
    console=False,
    icon="supplierhub/assets/icon.ico",
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="SupplierHub",
    upx=False,
)
