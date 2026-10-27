# -*- coding: utf-8 -*-
"""MusicXML 밖의 악보 형식(MuseScore .mscz/.mscx, Guitar Pro .gp/.gp3~.gp7/.gpx 등)을 MusicXML로 바꾼다.

직접 해석하지 않고 설치된 MuseScore(3·4)의 명령줄 변환을 쓴다 — MuseScore가 위 형식을 다 열 수
있어서 한 가지 방법으로 모두 해결된다. 변환 결과는 임시 폴더에 캐시한다(원본 경로·수정 시각·크기가
같으면 다시 안 바꿈) — 읽기(musicxml_reader)와 조판(score_view, 다른 프로세스)이 같은 파일을 쓰게.

resolve(path)는 MusicXML이면 그대로, 아니면 변환한 .musicxml 경로를 돌려준다.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

# MusicXML로 바로 읽는 확장자
NATIVE_EXTS = (".xml", ".musicxml", ".mxl")
# MuseScore로 변환해 읽는 확장자
CONVERT_EXTS = (".mscz", ".mscx", ".gp", ".gpx", ".gp3", ".gp4", ".gp5", ".gp6", ".gp7", ".gp8", ".ptb")

# 열기 창 필터용
OPEN_PATTERNS = " ".join(f"*{e}" for e in NATIVE_EXTS + CONVERT_EXTS)

_CACHE_DIR = os.path.join(tempfile.gettempdir(), "GenerateScore_import")
_CONVERT_TIMEOUT_SEC = 180


class ImportError_(RuntimeError):
    """변환 실패(사용자에게 그대로 보여 줄 한국어 메시지)."""


def needs_conversion(path):
    return str(path).lower().endswith(CONVERT_EXTS)


def find_musescore():
    """설치된 MuseScore 실행 파일 경로(없으면 None). 환경 변수 MUSESCORE_PATH가 우선."""
    env = os.environ.get("MUSESCORE_PATH")
    if env and os.path.isfile(env):
        return env
    for name in ("MuseScore4", "MuseScore4.exe", "mscore", "musescore", "MuseScore3", "MuseScore3.exe"):
        found = shutil.which(name)
        if found:
            return found
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    names = ("MuseScore 4", "MuseScore 3", "MuseScore Studio")
    exes = ("bin\\MuseScore4.exe", "bin\\MuseScore3.exe", "bin\\MuseScoreStudio.exe", "MuseScore4.exe")
    for root in roots:
        if not root:
            continue
        for n in names:
            for exe in exes:
                p = os.path.join(root, n, exe)
                if os.path.isfile(p):
                    return p
    for p in ("/Applications/MuseScore 4.app/Contents/MacOS/mscore",
              "/Applications/MuseScore 3.app/Contents/MacOS/mscore", "/usr/bin/musescore"):
        if os.path.isfile(p):
            return p
    return None


def _cache_path(path):
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_mtime_ns}|{st.st_size}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    stem = "".join(c if c.isalnum() else "_" for c in os.path.splitext(os.path.basename(path))[0])[:40]
    return os.path.join(_CACHE_DIR, f"{stem}_{h}.musicxml")


def resolve(path):
    """MusicXML 경로를 돌려준다 — 이미 MusicXML이면 그대로, 아니면 MuseScore로 변환(캐시)."""
    path = str(path)
    if not needs_conversion(path):
        return path
    out = _cache_path(path)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    exe = find_musescore()
    if exe is None:
        ext = os.path.splitext(path)[1]
        raise ImportError_(f"{ext} 악보는 MuseScore로 변환해 읽습니다. MuseScore(무료, musescore.org)를 설치하거나 "
                           "악보 프로그램에서 MusicXML(.mxl/.xml)로 내보내 주세요.")
    os.makedirs(_CACHE_DIR, exist_ok=True)
    # 변환 중간 파일은 따로 만들고 끝나면 옮김 — 다른 프로세스가 덜 쓴 파일을 읽지 않게. MuseScore가 한글
    # 경로를 못 받는 일이 없도록 입력도 ASCII 이름으로 복사해서 넘긴다.
    work = tempfile.mkdtemp(dir=_CACHE_DIR)
    try:
        src = os.path.join(work, "in" + os.path.splitext(path)[1].lower())
        shutil.copyfile(path, src)
        dst = os.path.join(work, "out.musicxml")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            proc = subprocess.run([exe, "-o", dst, src], capture_output=True, timeout=_CONVERT_TIMEOUT_SEC,
                                  creationflags=flags)
        except subprocess.TimeoutExpired:
            raise ImportError_("MuseScore 변환이 너무 오래 걸려 멈췄습니다.")
        if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
            msg = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()[-300:]
            raise ImportError_("MuseScore가 이 악보를 MusicXML로 바꾸지 못했습니다." + (f"\n{msg}" if msg else ""))
        os.replace(dst, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out
