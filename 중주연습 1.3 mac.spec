# -*- mode: python ; coding: utf-8 -*-
# Mac용 .app 빌드(2026-10-27). Windows용은 "중주연습 1.3.spec".
# 빌드하는 Mac의 CPU용으로 나온다(인텔 Mac → x86_64, 애플실리콘 Mac → arm64). BUILD.md 참고.
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('verovio')
datas += [('assets/alhambra_logo.png', 'assets')]   # 시작 화면 푸터 로고
datas += [('assets/ensemble.png', 'assets')]   # 창 아이콘(app.py _set_window_icon)


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
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
    [],
    exclude_binaries=True,
    name='ensemble',   # 번들 안 실행 파일 이름 — 한글이면 codesign 검증이 "sealed resource" 오류로 깨진다(2026-10-27 실측)
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # Mac에서 UPX는 서명을 깨뜨린다
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/ensemble.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ensemble',
)
app = BUNDLE(
    coll,
    name='중주연습 1.3.app',
    icon='assets/ensemble.icns',
    bundle_identifier='com.generatescore.ensemble',
    info_plist={
        'CFBundleDisplayName': '중주연습 1.3',
        'CFBundleShortVersionString': '1.3',
        'NSHighResolutionCapable': True,
        # 소리 지연 자동 맞추기(latency_cal)가 마이크를 쓴다 — 없으면 권한 창 없이 무음만 들어온다
        'NSMicrophoneUsageDescription': '소리 지연을 자동으로 맞추려고 스피커 소리를 마이크로 듣습니다.',
    },
)
