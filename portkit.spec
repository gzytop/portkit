# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 portkit GUI 打成单个 exe。

构建方式（推荐用 build_exe.bat，它会自动准备虚拟环境）：
    .venv\\Scripts\\pyinstaller.exe portkit.spec --noconfirm

exe 文件名用 ASCII，避免在 cmd/bat、PATH 和各类工具里出现编码问题；
面向用户的中文名称通过 Windows 版本信息（文件说明）呈现，
在任务管理器和文件属性里会显示为「端口占用管理器」。
"""

APPLICATION_NAME = "PortKit"
ICON_FILENAME = "app_icon.ico"
VERSION_INFO_FILENAME = "version_info.txt"

analysis = Analysis(
    ["portkit_gui.py"],
    pathex=[],
    binaries=[],
    # 图标一并打进包内，运行时通过 sys._MEIPASS 定位，用于设置窗口/任务栏图标。
    datas=[(ICON_FILENAME, ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 这些是 GUI 用不到的重型标准库模块，排除后体积明显更小。
    excludes=[
        "unittest",
        "pydoc",
        "doctest",
        "pdb",
        "email",
        "html",
        "http",
        "xmlrpc",
        "test",
        "lib2to3",
        "distutils",
        "setuptools",
        "pip",
    ],
    noarchive=False,
    optimize=2,
)

pure_python_archive = PYZ(analysis.pure)

executable = EXE(
    pure_python_archive,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name=APPLICATION_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=False 表示窗口程序，双击不会出现黑色控制台。
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_FILENAME,
    version=VERSION_INFO_FILENAME,
)
