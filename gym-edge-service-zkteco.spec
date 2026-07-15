# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['run_edge_zkteco.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'zk',
        'zk.base',
        'zk.attendance',
        'zk.user',
        'zk.finger',
        'zk.const',
        'zk.exception',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='gym-edge-service-zkteco',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
