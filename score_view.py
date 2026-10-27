# -*- coding: utf-8 -*-
"""악보(MusicXML) 페이지를 그려서 이미지로 돌려준다 — PC(Tkinter) 전용.

ExtractAudio/score_view.py에서 가져옴(2026-10-07) — 공용 파일이 아니다. 이 프로젝트에서
마디 상자 좌표(measure_boxes, 재생 위치 강조용)와 로그 끄기를 더했다.

Verovio(엔그레이빙 엔진)로 MusicXML을 페이지별 SVG로 조판하고, resvg_py로 그 SVG를
PNG로 래스터라이즈해서 PIL.Image로 돌려준다. 빔 묶음(박자 단위)·기둥 방향(다수결)·
빔 기울기(상행/하행) 같은 표준 기보 규칙은 Verovio가 알아서 처리한다 — 원본 MusicXML에
빔/기둥 정보가 있으면 그대로 따르고, 없으면 Verovio의 자동 빔 묶기가 박자표 기준으로
처리한다(이 앱이 직접 음표를 그릴 필요가 없다).

SVG→PNG 변환에 cairosvg를 먼저 시도했으나 Windows에 시스템 libcairo-2.dll이 없어
런타임에 실패했고(설치는 되지만 import 시 OSError), svglib+reportlab은 텍스트 요소에서
ZeroDivisionError가 났다(글꼴 스케일 해석 문제로 보임). resvg_py(Rust 기반, 자체
내장 바이너리 휠이라 외부 DLL 불필요)가 둘 다 없이 안정적으로 동작해서 이걸 쓴다
(2026-10-07 확인).

verovio.toolkit().loadFile()은 한글이 섞인 경로에서 조용히 실패한다(내부 파일 I/O
문제로 보임) — score_follow._load_root와 같은 방식으로 직접 파일을 읽어 문자열을
loadData()에 넘긴다.
"""
import copy
import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile

import verovio
import resvg_py
from PIL import Image

import score_import

# 붙임줄·이음줄 짝이 안 맞는 등 사소한 가져오기 경고가 콘솔을 가득 채워서 끈다
verovio.enableLog(verovio.LOG_OFF)

# verovio 파이썬 패키지는 글꼴 리소스 경로를 "메인 스레드"에서만 제대로 잡는다 — 작업
# 스레드에서 만든 toolkit은 휠을 빌드한 서버의 임시 경로(C:/Users/RUNNER~1/...)를 갖고
# 있어서 loadData가 "font resources are not available"로 조용히 실패했다(2026-10-07,
# 이 앱은 조판을 작업 스레드에서 하므로). 그래서 패키지 안 data 폴더를 직접 지정한다.
#
# 또 verovio는 리소스 경로에 한글이 있어도 같은 식으로 실패한다 — exe 폴더 이름이
# "악보소리생성 0.1"이라 빌드본에서 걸렸다(2026-10-07). 경로에 ASCII 아닌 글자가 있으면
# 8.3 짧은 경로를 먼저 써 보고(D: 등은 꺼져 있는 경우가 많음), 안 되면 ASCII 경로인
# 임시 폴더·ProgramData로 data 폴더(약 6MB)를 한 번 복사해 쓴다.
_PACKAGE_RESOURCE_DIR = os.path.join(os.path.dirname(verovio.__file__), "data")
_resource_dir = None


def _short_path(path):
    if os.name != "nt":
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024):
            return buf.value
    except Exception:
        pass
    return path


def _ascii_resource_dir():
    global _resource_dir
    if _resource_dir is not None:
        return _resource_dir
    src = _PACKAGE_RESOURCE_DIR
    if src.isascii():
        _resource_dir = src
        return src
    short = _short_path(src)
    if short.isascii():
        _resource_dir = short
        return short
    import shutil
    import tempfile
    version = getattr(verovio.toolkit(False), "getVersion", lambda: "x")()
    folder = "verovio_data_" + "".join(c if c.isalnum() else "_" for c in version)
    for base in (tempfile.gettempdir(), os.environ.get("PROGRAMDATA", ""), os.environ.get("PUBLIC", "")):
        if not base or not base.isascii():
            continue
        dst = os.path.join(base, "GenerateScore", folder)
        try:
            if not os.path.isdir(dst):
                shutil.copytree(src, dst + ".tmp", dirs_exist_ok=True)
                os.replace(dst + ".tmp", dst)
            _resource_dir = dst
            return dst
        except OSError:
            continue
    _resource_dir = src  # 마지막 수단(실패할 수 있음)
    return src


def _new_toolkit():
    tk = verovio.toolkit(False)
    tk.setResourcePath(_ascii_resource_dir())
    return tk

# Verovio 내부 레이아웃 단위(약 1/100mm). 처음(고정 A4 비슷한 비율) 1400px 너비로
# 그렸을 때 음표 크기가 알맞아 보였던 기준을 그대로 비율(유닛/px)로 삼는다 — 칸
# 크기에 맞춰 쪽을 나눌 때도(ScoreRenderer 참고) 이 비율을 지켜야 음표 크기가
# 일정하게 유지된다. scale 옵션은 그려진 걸 통째로 늘렸다 줄였다만 할 뿐(최종
# 픽셀 너비로 다시 맞추므로 결과가 똑같음) 한 쪽에 들어가는 분량(쪽 나누기)엔
# 영향이 없다는 걸 실측으로 확인함(2026-10-07, scale 40~300 전부 같은 36쪽).
# 그래서 "음표를 크게"는 scale이 아니라 이 유닛/px 비율로 — 비율을 올리면(페이지가
# 유닛 기준으로 더 "커져서") 같은 화면 픽셀 안에 음표가 덜 들어가(쪽이 늘고 음표가
# 커짐).
_PAGE_WIDTH = 2100
_PAGE_HEIGHT = 2970
_UNITS_PER_PX = 2100 / 1400 * 1.6  # 기준(2100유닛/1400px)에 1.6배 — 음표가 작다는 지적(2026-10-07)
_SCALE = 40
PAGE_ASPECT = _PAGE_WIDTH / _PAGE_HEIGHT  # 너비/높이 — target_width_px/height_px가 없을 때 기본 비율

# 한 줄(시스템)에 4~6마디가 들어가게(표준 기보 관례) — 범위 가운데 값을 고정으로 씀
# (사용자 요청, 2026-10-07; 처음엔 5~7이었다가 4~6으로 조정). Verovio의 자동 쪽
# 나누기(너비 기준)에 맡기면 음표 밀도에 따라 줄마다 마디 수가 들쭉날쭉해지기 쉬워서,
# MusicXML에 직접 줄바꿈 표시를 꽂는다.
_MEASURES_PER_LINE = 5
# 성부(스태프) 수가 많을수록 한 페이지에 들어가는 줄 수를 줄인다 — 콰르텟(4성부 이상)은
# 명시적으로 2줄/페이지(사용자 요청). 성부가 적을수록 한 줄이 차지하는 세로 공간이 작아
# 더 많이 들어갈 수 있어 늘렸다(안 그러면 독주/듀오 악보가 페이지를 너무 휑하게 씀).
_SYSTEMS_PER_PAGE_BY_STAFF_COUNT = {1: 5, 2: 4, 3: 3}
_SYSTEMS_PER_PAGE_DEFAULT = 2  # 4성부 이상(콰르텟류)
# 줄 단위 조판(layout_systems_file / SystemSet.arrange)
_FIT_SLACK = 1.06          # 줄 높이 여유(넘침 방지)
_FIT_SHARE = 0.8           # 쪽당 줄 수: 펼침면의 이 비율 이상이 칸에 들어가면 채택
# 가로 간격: Verovio 기본(0.25)은 기존 출판 악보보다 촘촘해서 한 줄에 마디가 많이 들어갔다
# (슈베르트 48~62마디: 기존 악보 줄당 2~3마디, 기본값 4마디). 0.45면 슈베르트는 맞지만 마디가
# 긴 곡(비탈리 3/2)이 한 줄 1마디투성이(132줄)가 돼서, 곡들 사이 절충으로 0.3(평균 줄당
# 마디: 슈베르트 4.8·보케리니 3.6·하이든 3.1·할보르센 2.3·비탈리 2.2, 2026-10-07 실측).
_SPACING_LINEAR = 0.3
_SPACING_NONLINEAR = 0.6
_PAGE_MARGIN_UNITS = 50   # 쪽 위아래 여백(조판 설정 단위 — SVG에선 500으로 보임)


# Verovio는 빠르기표(♩ = 66)의 음표를 악보 글꼴(Leipzig)의 SMuFL 전용 문자로 글자 안에 쓰는데,
# 그 글꼴은 SVG에 CSS(woff2)로만 들어 있어 resvg가 못 읽고 한글 글꼴로 대신 그려 "揞" 같은 엉뚱한
# 글자가 나왔다(2026-10-07). 그려 넣기 직전에 Windows 기본 글꼴에 있는 유니코드 음표로 바꾼다.
_SMUFL_TEXT = {
    "": "𝅝", "": "𝅗𝅥", "": "𝅗𝅥",  # 온·2분음표
    "": "♩", "": "♩",                                  # 4분음표
    "": "♪", "": "♪",                                  # 8분음표
    "": "𝅘𝅥𝅯", "": "𝅘𝅥𝅯",                          # 16분음표
    "": "·",                                                       # 점
}
# 코드 이름(<harmony>)의 임시표: Verovio가 Leipzig 글꼴 사설 영역 글자(U+EA64 ♭ · EA65 ♮ · EA66 ♯)로 그리는데 resvg에
# 그 글꼴이 없어 엉뚱한 한자("C鋱dim")로 나왔다(사용자 지적 2026-10-25) → 유니코드 ♭♮♯로.
_SMUFL_TEXT.update({"": "♭", "": "♮", "": "♯"})
_ACCIDENTAL_TEXT_CHARS = set("♭♮♯")
_SMUFL_TEXT_FONT = "Segoe UI Symbol"


def _fix_smufl_text(svg):
    if 'font-family="Leipzig"' not in svg:
        return svg

    def repl(m):
        text = "".join(_SMUFL_TEXT.get(ch, ch) for ch in m.group(2))
        # 일반 글꼴의 음표가 SMuFL 글자보다 커 보여서 조금 줄임. 코드 이름 임시표는 SMuFL 글자가 720px(글자 405px의 1.8배)로
        # 오니 글자 크기쯤으로 줄임
        size = float(m.group(1)) * (0.58 if set(text) & _ACCIDENTAL_TEXT_CHARS else 0.75)
        return f'<tspan font-family="{_SMUFL_TEXT_FONT}" font-size="{size:g}px">{text}</tspan>'
    return re.sub(r'<tspan font-family="Leipzig" font-size="([\d.]+)px">([^<]*)</tspan>', repl, svg)


def rasterize_svg(svg, width):
    """SVG 문자열 → PNG 바이트(흰 배경, 너비 width px). 모듈 최상위 함수인 이유: 앱이
    이걸 별도 프로세스(ProcessPoolExecutor)에서 돌린다 — resvg_py는 변환하는 동안(쪽당
    0.6초쯤) 파이썬 GIL을 놓지 않아서, 같은 프로세스에서 하면 소리 출력 콜백이 그만큼
    멈춰 쪽이 넘어갈 때 소리가 찌그러졌다(2026-10-07 실측: 다른 스레드 최대 615ms 멈춤)."""
    return bytes(resvg_py.svg_to_bytes(svg_string=_fix_smufl_text(svg), background="#FFFFFF", width=width))


def png_to_image(png_bytes):
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def _systems_per_page(staff_count):
    return _SYSTEMS_PER_PAGE_BY_STAFF_COUNT.get(staff_count, _SYSTEMS_PER_PAGE_DEFAULT)


STRIP_OPENING_TEMPO = False


def _strip_opening_tempo_mark(parts):
    """첫머리 빠르기표(<direction><metronome>)를 지운다(사용자 요청, 2026-10-07) —
    제목·작곡가는 header="none" 옵션으로 없애지만(ScoreRenderer 참고), 빠르기표는
    머리글이 아니라 첫 마디의 악상 지시라 XML에서 직접 찾아 지워야 한다. 맨 앞
    2마디만 본다 — 요청이 "첫 페이지의"로 범위를 한정했고, 곡 중간의 다른 빠르기
    변화(리타르단도 등 템포 지시)는 건드리지 않으려는 것."""
    if not parts:
        return
    for measure in parts[0].findall("measure")[:2]:
        for direction in list(measure.findall("direction")):
            if direction.find(".//metronome") is not None:
                measure.remove(direction)


def _prepare_tree(xml_text):
    """Verovio에 넘기기 전에 한 번만 손봐 두는 손질 — 첫머리 빠르기표 삭제
    (_strip_opening_tempo_mark, 사용자 요청) + 원본에 악보 프로그램이 구워 넣은 줄/쪽
    나눔 표시 제거(이걸 안 지우고 우리가 넣을 표시와 섞으면 둘 중 먼저 오는 표시가
    각각 적용돼 줄마다 마디 수가 더 들쭉날쭉해진다 — 실측으로 확인, 2026-10-07).

    줄/쪽 나눔 자체는 여기서 안 넣는다 — 칸 높이에 맞춰 페이지당 줄 수를 몇 번이고
    다시 시험해볼 수 있어야 해서(ScoreRenderer, 전체보기 등 칸 크기가 바뀔 때마다),
    그 단계는 _inject_breaks가 (이 함수가 돌려준 트리를 copy.deepcopy해서) 따로 맡는다.

    score-partwise 형식에서만 동작한다(score_follow.py와 같은 전제). 아니거나 파싱이
    안 되면 (None, None)을 돌려줘서 호출부가 Verovio의 기본 자동 나누기로 돌아가게
    한다."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, None
    if not root.tag.endswith("score-partwise"):
        return None, None
    parts = root.findall("part")
    if not parts:
        return None, None

    # 첫머리 빠르기표 지우기(ExtractAudio 사용자 요청)는 이 앱에선 하지 않는다 — 원본 악보 첫 쪽처럼
    # "♩ = 66" 등을 보여 준다(사용자 지적 2026-10-07: 첫 쪽이 실제 악보와 다름).
    if STRIP_OPENING_TEMPO:
        _strip_opening_tempo_mark(parts)
    for part in parts:
        for measure in part.findall("measure"):
            for pr in list(measure.findall("print")):
                has_other_purpose = (
                    any(k not in ("new-system", "new-page") for k in pr.attrib) or len(list(pr))
                )
                pr.attrib.pop("new-system", None)
                pr.attrib.pop("new-page", None)
                if not has_other_purpose and not pr.attrib:
                    measure.remove(pr)

    _add_measure_marks(parts[0])
    for part in parts:
        _slur_grace_groups(part)
        _balance_slurs(part)
        _string_numbers_to_fingering(part)
        _stretch_zero_wedges(part)
        if part.find(".//time-modification") is not None and part.find(".//tuplet") is None:
            _auto_tuplet_part(part)
        if not RESPECT_SOURCE_BEAMS:
            _strip_beams_and_stems(part)
        if part.find(".//beam[@number='1']") is None or not RESPECT_SOURCE_BEAMS:
            _auto_beam_part(part)
        # 꾸밈음 손질은 자동 빔 뒤에 — 자동 빔이 본음 기둥 방향을 다시 정하므로
        _beam_grace_groups(part)
        _slurred_grace_targets_stem_up(part)

    return root, parts


# 리허설 마크처럼 마디 번호를 네모 안에 적는 간격(사용자 요청 2026-10-13: 20마디마다)
MEASURE_MARK_EVERY = 20


def _add_measure_marks(part):
    """첫 파트에서 마디 번호가 MEASURE_MARK_EVERY의 배수인 마디에 리허설 마크(네모 안 번호)를 단다 —
    악보에서 마디를 빨리 찾게. 이미 리허설 마크가 있는 마디, 같은 번호가 또 나오는 마디(엔딩)는 건너뜀."""
    seen = set()
    for measure in part.findall("measure"):
        digits = re.sub(r"\D", "", measure.get("number") or "")
        if not digits:
            continue
        n = int(digits)
        if n <= 0 or n % MEASURE_MARK_EVERY or n in seen:
            continue
        seen.add(n)
        if measure.find("direction/direction-type/rehearsal") is not None:
            continue
        direction = ET.Element("direction", {"placement": "above"})
        ET.SubElement(ET.SubElement(direction, "direction-type"), "rehearsal",
                      {"enclosure": "rectangle"}).text = str(n)
        notes = [i for i, c in enumerate(measure) if c.tag == "note"]
        measure.insert(notes[0] if notes else len(measure), direction)


# ---------------------------------------------------------------- 빔 자동 묶기
# 악보 파일에 적힌 빔·기둥을 따를지(True) 무시하고 규칙대로 다시 묶을지(False). 사용자
# 결정(2026-10-07): 규칙대로 — music21(보케리니)이 3/4에서 한 마디 통째로 묶거나 가운데
# 줄 위 음에 기둥을 위로 단 것 등을 기준(박 단위·다수결)에 맞게 바로잡는다. 꾸밈음의 빔·
# 기둥은 관례(기둥 늘 위)라 그대로 둔다.
RESPECT_SOURCE_BEAMS = False


def _strip_beams_and_stems(part):
    for note in part.iter("note"):
        if note.find("grace") is not None:
            continue
        for tag in ("beam", "stem"):
            for el in note.findall(tag):
                note.remove(el)

# Verovio는 MusicXML에 <beam>이 없으면 8분음표를 하나도 묶지 않고 낱개 꼬리로 그린다
# (Claude가 만든 MusicXML들이 그랬음 — 할보르센·비탈리·슈베르트, 2026-10-07 확인). 그래서
# 기보 규칙대로 빔을 넣는다(RESPECT_SOURCE_BEAMS=False면 파일에 적힌 빔·기둥은 지우고 모든
# 성부를 다시 묶음, True면 빔이 하나도 없는 성부만):
#   - 박 단위로 묶기: x/4·x/2는 한 박(4/4는 반 마디까지도 허용하지만 여기선 한 박으로만),
#     6/8·9/8·12/8은 점4분음표(8분음표 3개), 3/8 이하 x/8은 마디 전체.
#   - 쉼표·4분음표 이상·꾸밈음이 끼면 끊는다. 성부(voice)마다 따로.
#   - 그 마디·보표에 성부가 하나뿐이면 묶음의 기둥 방향을 다수결로 정해 <stem>을 넣는다
#     (머리 과반이 가운데 줄보다 위면 아래, 아래면 위, 같으면 Verovio에 맡김). 성부가 둘
#     이상이면 위 성부 위·아래 성부 아래 관례가 우선이라 손대지 않는다.
# 못갖춘마디는 박 위치를 마디 끝에 맞춘다(첫 박이 마디 끝 쪽에 있음).

# ---------------------------------------------------------------- 줄 번호·짧은 꺾쇠
# 원본 악보(PDF)와 첫 쪽을 비교해 빠진 것을 채운다(사용자 지적 2026-10-07):
#   - 줄 번호(④ 등): MusicXML <technical><string>을 Verovio가 그리지 않는다 → 같은 자리에
#     <fingering>으로 동그라미 숫자(①~⑥)를 넣는다(fret은 그리지 않음 — 원본도 줄 번호만).
#   - "pp<", "p>" 같은 짧은 꺾쇠: 크레셴도 시작과 끝이 같은 시점에 붙어 있어(길이 0) Verovio가
#     안 그린다 → 끝 표시를 다음 음표 뒤로 옮겨 그 음 길이만큼의 짧은 꺾쇠가 되게.

_CIRCLED = {str(i): ch for i, ch in enumerate("①②③④⑤⑥⑦⑧⑨", start=1)}


def _string_numbers_to_fingering(part):
    for note in part.iter("note"):
        tech = note.find("notations/technical")
        if tech is None:
            continue
        st = tech.find("string")
        if st is None or (st.text or "").strip() not in _CIRCLED:
            continue
        if tech.find("fingering") is not None:
            continue
        f = ET.Element("fingering", placement="above")
        f.text = _CIRCLED[st.text.strip()]
        tech.append(f)


def _slur_grace_groups(part):
    """꾸밈음 묶음(한 본음 앞에 잇달아 나오는 꾸밈음)마다 첫 꾸밈음에서 본음까지 이음줄 하나로 통일
    (사용자 요청 2026-10-12: 모든 꾸밈음을 이음줄로 묶기, 할보르센 69마디). 파일에 적힌 꾸밈음
    이음줄은 묶음 안 음마다 같은 번호로 시작이 거듭되고 끝이 어긋나 있기도 해서(_balance_slurs가 중복
    시작을 지워도 모양이 들쭉날쭉) 꾸밈음에서 시작한 이음줄은 모두 지우고 새로 단다. 본음에서 끝나는
    바깥 프레이즈 이음줄(다른 번호)은 그대로 둔다."""
    for measure in part.findall("measure"):
        group = []   # 이어지는 꾸밈음(화음 음표 포함)
        for e in measure:
            if e.tag in ("backup", "forward"):
                group = []
                continue
            if e.tag != "note":
                continue
            if e.find("grace") is not None:
                group.append(e)
                continue
            if e.find("chord") is not None:
                continue   # 본음의 화음 음표 — 본음(첫 음)에 이미 달았음
            if group and e.find("rest") is None and e.find("cue") is None:
                # 본음에서 끝나는 번호의 꾸밈음 시작만 지운다(본음 뒤로 이어지는 긴 프레이즈 이음줄은 보존)
                ends_here = {s.get("number", "1") for s in e.findall("notations/slur") if s.get("type") == "stop"}
                removed = set()
                for g in group:
                    for s in g.findall("notations/slur"):
                        if s.get("type") == "start" and s.get("number", "1") in ends_here:
                            removed.add(s.get("number", "1"))
                            g.find("notations").remove(s)
                for s in e.findall("notations/slur"):   # 짝을 잃은 끝도 지움
                    if s.get("type") == "stop" and s.get("number", "1") in removed:
                        e.find("notations").remove(s)
                used = {s.get("number", "1") for n in group + [e] for s in n.findall("notations/slur")}
                num = min(removed) if removed else next(str(k) for k in range(8, 20) if str(k) not in used)
                first = next(g for g in group if g.find("chord") is None)
                _add_notation(first, ET.Element("slur", {"type": "start", "number": num}))
                _add_notation(e, ET.Element("slur", {"type": "stop", "number": num}))
                for n in group + [e]:
                    nots = n.find("notations")
                    if nots is not None and len(nots) == 0:
                        n.remove(nots)
            group = []


def _balance_slurs(part):
    """이음줄 시작·끝 짝을 파트 안에서 맞춘다 — 짝 없는 끝, 이미 열린 번호의 시작(중복), 끝내
    안 닫힌 시작은 지운다. Verovio는 짝 없는 끝을 **다른 파트**의 시작과 이어 버려(나중에 나오는
    시작을 기다림) 기타2에서 기타1로 넘어가는 이음줄이 생겼고(할보르센 29마디), 꾸밈음 묶음마다
    같은 번호 시작이 두 번 + 짝 없는 끝이 있어 이음줄이 마디를 가로질러 길게 그려졌다(69마디,
    2026-10-08). 중복 시작은 앞의 것을 남긴다(꾸밈음 첫 음 → 본음으로 이어지게)."""
    open_ = {}  # 번호 → (notations, slur)
    for note in part.iter("note"):
        nots = note.find("notations")
        if nots is None:
            continue
        slurs = nots.findall("slur")
        # 한 음에 끝과 시작이 같이 있으면 끝을 먼저(같은 번호로 이어 가는 경우)
        for s in sorted(slurs, key=lambda s: s.get("type") != "stop"):
            num = s.get("number", "1")
            kind = s.get("type")
            if kind == "stop":
                if open_.pop(num, None) is None:
                    nots.remove(s)
            elif kind == "start":
                if num in open_:
                    nots.remove(s)
                else:
                    open_[num] = (nots, s)
            elif kind == "continue" and num not in open_:
                nots.remove(s)
    for nots, s in open_.values():
        nots.remove(s)
    for note in part.iter("note"):
        nots = note.find("notations")
        if nots is not None and len(nots) == 0:
            note.remove(nots)


def _stretch_zero_wedges(part):
    """시작과 끝이 같은 시점인(길이 0) 꺾쇠의 끝 표시를 마디 끝으로 — 같은 마디에 다음 셈여림이
    있으면 그 바로 앞으로 옮긴다. 다음 음표 하나만큼만 늘렸더니 꺾쇠가 셈여림 글자("pp")보다
    짧아서, Verovio가 꺾쇠 시작을 글자 뒤로 미는 바람에 둘이 겹쳐 "pp≡"처럼 보였다."""
    for measure in part.findall("measure"):
        children = list(measure)
        for i, e in enumerate(children):
            w = e.find("direction-type/wedge") if e.tag == "direction" else None
            if w is None or w.get("type") not in ("crescendo", "diminuendo"):
                continue
            number = w.get("number", "1")
            stop = None
            for e2 in children[i + 1:]:
                if e2.tag == "note":
                    break
                w2 = e2.find("direction-type/wedge") if e2.tag == "direction" else None
                if w2 is not None and w2.get("type") == "stop" and w2.get("number", "1") == number:
                    stop = e2
                    break
            if stop is None:
                continue
            measure.remove(stop)
            children = list(measure)
            # 꺾쇠 시작 뒤 첫 음표 다음부터, 다음 셈여림(같은 성부 흐름 — backup 전까지) 앞 또는 마디 끝
            k = children.index(e) + 1
            while k < len(children) and children[k].tag != "note":
                k += 1
            insert_at = len(children)
            for j in range(k + 1, len(children)):
                c = children[j]
                if c.tag == "backup":
                    insert_at = j
                    break
                if c.tag == "direction" and c.find("direction-type/dynamics") is not None:
                    insert_at = j
                    break
            measure.insert(insert_at, stop)
            children = list(measure)


# ---------------------------------------------------------------- 잇단음표 표시 넣기
# <time-modification>(길이 비율)만 있고 <tuplet>(그리기 표시)이 없으면 Verovio는 잇단음표를
# 보통 음표로 그린다 — 셋잇단 16분음표 6개가 "3" 없이 16분음표로 그려져 2/4 마디에 16분음표
# 12개가 들어간 틀린 악보로 보였다(Claude가 만든 MusicXML들, 2026-10-07). 같은 성부에서 같은
# 비율의 음표를 길이로 더해 "정상 음표 normal-notes개 길이"가 차면 한 잇단음표로 묶어
# start/stop 표시를 넣는다(예: 셋잇단 16분음표 3개 = 16분음표 2개 길이).

_TYPE_QUARTERS = {"whole": 4.0, "half": 2.0, "quarter": 1.0, "eighth": 0.5, "16th": 0.25,
                  "32nd": 0.125, "64th": 0.0625, "128th": 0.03125}


def _add_notation(note, child):
    nots = note.find("notations")
    if nots is None:
        nots = ET.Element("notations")
        _insert_in_order(note, nots)
    nots.insert(0, child)


def _auto_tuplet_part(part):
    """잇단음표 비율은 <time-modification> 글자 대신 **실제 길이**로 정한다 — Claude가 만든
    파일에 "6:4 16분음표"라 적고 길이는 48(16분음표 120의 2/5, 즉 5:2)인 음표가 있었다
    (할보르센 84마디). 적힌 비율대로 묶으면 길이가 안 맞아 박 경계를 넘는 "6"이 생겼다.
    또 한 잇단음표가 박 경계를 넘지 않게 한다(넘으면 거기서 끊음)."""
    from fractions import Fraction
    div = 1
    beats, beat_type = 4, 4
    for measure in part.findall("measure"):
        attrs = measure.find("attributes")
        if attrs is not None and attrs.findtext("divisions"):
            div = int(attrs.findtext("divisions"))
        t = attrs.find("time") if attrs is not None else None
        if t is not None and t.findtext("beats") and t.findtext("beat-type"):
            try:
                beats = int(t.findtext("beats").split("+")[0])
                beat_type = int(t.findtext("beat-type"))
            except ValueError:
                pass
        window = _beam_window(beats, beat_type) * div
        open_by_voice = {}  # 성부 -> [비율, 목표 길이, 지금까지 길이, 음표들, 시작 위치]
        pos_by_voice = {}

        def close(v):
            st = open_by_voice.pop(v, None)
            if not st or len(st[3]) < 2:
                return
            first, last = st[3][0], st[3][-1]
            all_short = all(_TYPE_QUARTERS.get(n.findtext("type") or "", 1.0) < 1.0 and n.find("rest") is None
                            for n in st[3])
            # 빔으로 묶이는 잇단음표는 괄호 없이 숫자만(관례), 아니면 괄호
            _add_notation(first, ET.Element("tuplet", type="start", bracket="no" if all_short else "yes"))
            _add_notation(last, ET.Element("tuplet", type="stop"))

        pos = 0
        for e in measure:
            if e.tag == "backup":
                pos -= int(e.findtext("duration") or 0)
                continue
            if e.tag == "forward":
                pos += int(e.findtext("duration") or 0)
                continue
            if e.tag != "note":
                continue
            note = e
            if note.find("chord") is not None or note.find("grace") is not None:
                continue
            v = note.findtext("voice") or "1"
            dur = int(note.findtext("duration") or 0)
            start = pos
            pos += dur
            tm = note.find("time-modification")
            base = _TYPE_QUARTERS.get(note.findtext("type") or "")
            if tm is None or base is None or dur <= 0:
                close(v)
                continue
            # 실제 비율 = 적힌 음표 종류 길이 : 실제 길이 (점음표는 1.5배)
            dots = len(note.findall("dot"))
            written = Fraction(base * div).limit_denominator(1000) * (2 - Fraction(1, 2 ** dots))
            ratio = written / dur
            if ratio <= 1:
                close(v)
                continue
            actual, normal = ratio.numerator, ratio.denominator
            try:   # 적힌 비율이 실제 길이와 값이 같으면 적힌 숫자를 그대로(6:4는 "6"으로 보이게)
                w_act, w_norm = int(tm.findtext("actual-notes")), int(tm.findtext("normal-notes"))
                if w_norm > 0 and Fraction(w_act, w_norm) == ratio:
                    actual, normal = w_act, w_norm
            except (TypeError, ValueError):
                pass
            if normal == 1:   # 3:1 같은 꼴은 관례대로 3:2(정상 음표 2개 길이)로
                actual, normal = actual * 2, 2
            for tag, val in (("actual-notes", actual), ("normal-notes", normal)):
                el = tm.find(tag)
                if el is None:
                    el = ET.SubElement(tm, tag)
                el.text = str(val)
            st = open_by_voice.get(v)
            if st is not None and (st[0] != (actual, normal)
                                   or int(st[4] // window) != int(start // window)):
                close(v)
                st = None
            if st is None:
                st = [(actual, normal), normal * written / (2 - Fraction(1, 2 ** dots)), 0, [], start]
                open_by_voice[v] = st
            st[2] += dur
            st[3].append(note)
            if st[2] >= st[1]:
                close(v)
        for v in list(open_by_voice):
            close(v)


_STEP_INDEX = {"C": 0, "D": 1, "E": 2, "F": 3, "G": 4, "A": 5, "B": 6}
# 음자리표 기준음(온음계 번호 = 옥타브*7 + 음이름)과 그 음이 놓인 줄
_CLEF_REF = {"G": 4 * 7 + 4, "F": 3 * 7 + 3, "C": 4 * 7 + 0}
# MusicXML 음표 안 원소 순서(규격) — 새 원소를 제자리에 끼우려고
_NOTE_ORDER = ["grace", "chord", "pitch", "unpitched", "rest", "duration", "tie", "cue", "instrument",
               "voice", "type", "dot", "accidental", "time-modification", "stem", "notehead",
               "notehead-text", "staff", "beam", "notations", "lyric", "play", "listen"]


def _insert_in_order(note, elem):
    rank = _NOTE_ORDER.index(elem.tag)
    for i, child in enumerate(list(note)):
        if child.tag in _NOTE_ORDER and _NOTE_ORDER.index(child.tag) > rank:
            note.insert(i, elem)
            return
    note.append(elem)


def _beam_window(beats, beat_type):
    """박자표 → 묶음 단위(4분음표 단위)."""
    if beat_type == 8 and beats % 3 == 0 and beats >= 6:
        return 1.5
    if beat_type == 8:
        return beats * 0.5 if beats <= 4 else 1.0
    return 4.0 / beat_type


def _auto_beam_part(part):
    div = 1
    beats, beat_type = 4, 4
    clefs = {}  # 보표 번호 -> 가운데 줄 온음계 번호(옥타브 자리표 반영: 표시 위치 기준)
    for measure in part.findall("measure"):
        attrs = measure.find("attributes")
        if attrs is not None:
            if attrs.findtext("divisions"):
                div = int(attrs.findtext("divisions"))
            t = attrs.find("time")
            if t is not None and t.findtext("beats") and t.findtext("beat-type"):
                try:
                    beats = int(t.findtext("beats").split("+")[0])
                    beat_type = int(t.findtext("beat-type"))
                except ValueError:
                    pass
            for c in attrs.findall("clef"):
                sign = c.findtext("sign")
                if sign not in _CLEF_REF:
                    continue
                line = int(c.findtext("line") or {"G": 2, "F": 4, "C": 3}[sign])
                octv = int(c.findtext("clef-octave-change") or 0)
                # 8vb 자리표: Verovio는 pitch를 한 옥타브 위에 그린다(= 가운데 줄이 한 옥타브 낮은 음)
                clefs[c.get("number", "1")] = _CLEF_REF[sign] + (3 - line) * 2 + 7 * octv

        # 음표를 성부별로 [시작, 길이, 대표 음표, 화음 음표들, 쉼표?, 꾸밈음?, 보표]
        events = {}
        pos = 0
        last = None
        end = 0
        for e in measure:
            if e.tag == "backup":
                pos -= int(e.findtext("duration") or 0)
            elif e.tag == "forward":
                pos += int(e.findtext("duration") or 0)
            elif e.tag == "note":
                if e.find("chord") is not None and last is not None:
                    last[3].append(e)
                    continue
                grace = e.find("grace") is not None or e.find("cue") is not None
                dur = 0 if grace else int(e.findtext("duration") or 0)
                ev = [pos, dur, e, [e], e.find("rest") is not None, grace, e.findtext("staff") or "1"]
                events.setdefault(e.findtext("voice") or "1", []).append(ev)
                last = ev
                pos += dur
            end = max(end, pos)
        nominal = beats * div * 4.0 / beat_type
        offset = nominal - end if 0 < end < nominal else 0.0
        window = _beam_window(beats, beat_type) * div

        # (보표) → 음표 있는 성부 수
        voices_per_staff = {}
        for v, evs in events.items():
            for ev in evs:
                if not ev[4]:
                    voices_per_staff.setdefault(ev[6], set()).add(v)

        for v, evs in events.items():
            group = []

            def flush():
                if len(group) >= 2:
                    _apply_beam(group, clefs, voices_per_staff)
                group.clear()

            graces_before = 0
            for ev in evs:
                start, dur, _note, _members, is_rest, grace, _staff = ev
                beamable = not is_rest and not grace and 0 < dur < div
                if grace:
                    graces_before += 1
                    continue  # 꾸밈음 하나는 건너뛰되 묶음을 끊지는 않음
                if graces_before >= 2:
                    # 여러 음 꾸밈음 묶음이 끼면 끊는다 — 본음 빔이 그 위를 가로질러 겹쳤다(할보르센 69마디)
                    flush()
                graces_before = 0
                if not beamable:
                    flush()
                    continue
                if group:
                    prev = group[-1]
                    same_window = int((prev[0] + offset) // window) == int((start + offset) // window)
                    contiguous = prev[0] + prev[1] == start
                    if not (same_window and contiguous and prev[6] == ev[6]):
                        flush()
                group.append(ev)
            flush()


_GRACE_BEAM_LEVELS = {"eighth": 1, "16th": 2, "32nd": 3, "64th": 4}


def _beam_grace_groups(part):
    """잇달아 나오는 꾸밈음(2개 이상)을 빔으로 묶고 기둥을 위로 — 원본에 꾸밈음 빔이 없으면
    Verovio가 낱개 꼬리로 그려 서로 겹쳤다(할보르센 69마디, 3개씩 묶음, 2026-10-08). 여러 음
    꾸밈음은 빗금 없이 쓰는 게 관례라 그림에서만 slash를 뺀다(소리는 원본 파일을 따로 읽음).
    빔이 이미 적힌 묶음은 그대로 둔다."""
    for measure in part.findall("measure"):
        group = []

        def flush():
            if len(group) >= 2 and all(g[0].find("beam") is None for g in group) and \
                    all(g[0].findtext("type") in _GRACE_BEAM_LEVELS for g in group):
                n = len(group)
                for i, members in enumerate(group):
                    levels = _GRACE_BEAM_LEVELS[members[0].findtext("type")]
                    for lv in range(1, levels + 1):
                        b = ET.Element("beam", number=str(lv))
                        b.text = "begin" if i == 0 else ("end" if i == n - 1 else "continue")
                        _insert_in_order(members[0], b)
                    for note in members:
                        note.find("grace").attrib.pop("slash", None)
                        old = note.find("stem")
                        if old is not None:
                            note.remove(old)
                        st = ET.Element("stem")
                        st.text = "up"
                        _insert_in_order(note, st)
            group.clear()

        for e in measure:
            if e.tag != "note":
                if e.tag in ("backup", "forward"):
                    flush()
                continue
            if e.find("grace") is None:
                if e.find("chord") is None:
                    flush()
                continue
            if e.find("chord") is not None and group:
                group[-1].append(e)
            else:
                group.append([e])
        flush()


def _slurred_grace_targets_stem_up(part):
    """꾸밈음에서 이음줄로 이어지는 본음은 기둥을 위로(빔으로 묶인 짝도 같이). 꾸밈음 기둥은
    늘 위라 이음줄이 머리 아래로 지나가는데, 본음 기둥이 아래면 이음줄이 그 기둥 끝까지 내려가
    크게 휘었다(할보르센 69마디, 2026-10-08). 한 보표에 성부가 둘 이상인 마디는 위·아래 성부
    관례가 우선이라 손대지 않는다."""
    for measure in part.findall("measure"):
        notes = [e for e in measure if e.tag == "note"]
        if len({n.findtext("voice") or "1" for n in notes if n.find("rest") is None}) > 1:
            continue
        targets = set()
        open_from_grace = set()  # 꾸밈음에서 시작해 아직 안 닫힌 이음줄 번호
        for n in notes:
            slurs = n.findall("notations/slur")
            if n.find("grace") is not None:
                open_from_grace |= {s.get("number", "1") for s in slurs if s.get("type") == "start"}
                continue
            if n.find("chord") is None and any(s.get("type") == "stop" and s.get("number", "1") in open_from_grace
                                               for s in slurs):
                targets.add(id(n))
            open_from_grace.clear()
        if not targets:
            continue
        # 빔 묶음 단위로 펼침(묶음 안 한 음이라도 대상이면 묶음 전체) + 화음 음표
        groups, cur, beam_open = [], [], False
        for n in notes:
            if n.find("grace") is not None:
                continue
            if n.find("chord") is not None and cur:
                cur.append(n)
                continue
            if cur and not beam_open:
                groups.append(cur)
                cur = []
            cur.append(n)
            beam_open = n.findtext("beam[@number='1']") in ("begin", "continue")
        if cur:
            groups.append(cur)
        for g in groups:
            if any(id(n) in targets for n in g):
                for n in g:
                    old = n.find("stem")
                    if old is not None:
                        n.remove(old)
                    st = ET.Element("stem")
                    st.text = "up"
                    _insert_in_order(n, st)


def _apply_beam(group, clefs, voices_per_staff):
    n = len(group)
    for i, ev in enumerate(group):
        b = ET.Element("beam", number="1")
        b.text = "begin" if i == 0 else ("end" if i == n - 1 else "continue")
        _insert_in_order(ev[2], b)
    staff = group[0][6]
    mid = clefs.get(staff)
    if mid is None or len(voices_per_staff.get(staff, ())) > 1:
        return
    above = below = 0
    for ev in group:
        for note in ev[3]:
            p = note.find("pitch")
            if p is None:
                continue
            d = int(p.findtext("octave")) * 7 + _STEP_INDEX[p.findtext("step")]
            if d > mid:
                above += 1
            elif d < mid:
                below += 1
    if above == below:
        return
    direction = "down" if above > below else "up"
    for ev in group:
        for note in ev[3]:
            old = note.find("stem")
            if old is not None:
                note.remove(old)
            st = ET.Element("stem")
            st.text = direction
            _insert_in_order(note, st)


def _inject_breaks(root, parts, systems_per_page, measures_per_line=_MEASURES_PER_LINE):
    """줄마다 마디 수(measures_per_line)·페이지마다 줄 수(systems_per_page)를
    고정하려고 MusicXML에 직접 <print new-system="yes"/>·<print new-page="yes"/>를
    꽂는다(Verovio가 breaks="encoded"로 그대로 따름). root/parts는 _prepare_tree가
    돌려준 트리(또는 그 deepcopy) — 이 함수가 그 자리에서 수정한다."""
    if parts is None:
        parts = root.findall("part")
    measures_per_page = measures_per_line * systems_per_page
    measures = parts[0].findall("measure")
    for i, measure in enumerate(measures):
        if i == 0 or i % measures_per_line != 0:
            continue
        pr = ET.Element("print")
        pr.set("new-page" if i % measures_per_page == 0 else "new-system", "yes")
        measure.insert(0, pr)
    return ET.tostring(root, encoding="unicode")


def _read_xml_text(path):
    """.xml/.mxl에서 원문 텍스트를 읽는다(score_follow._load_root와 같은 방식). mscz·Guitar Pro 등은
    musicxml_reader가 이미 바꿔 둔 캐시 파일을 쓴다(score_import.resolve)."""
    path = score_import.resolve(path)
    if str(path).lower().endswith(".mxl"):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            target = None
            if "META-INF/container.xml" in names:
                c = ET.fromstring(z.read("META-INF/container.xml"))
                rf = c.find(".//{*}rootfile")
                if rf is not None:
                    target = rf.get("full-path")
            if target is None:
                target = next(n for n in names if n.endswith(".xml") and not n.startswith("META-INF"))
            return z.read(target).decode("utf-8")
    with open(path, encoding="utf-8") as f:
        return f.read()


class ScoreRenderer:
    """한 악보 파일을 열어 페이지 수를 세고, 페이지를 PIL 이미지로 그려준다. 페이지를
    여러 번 그릴 때(넘길 때마다) 매번 새로 읽지 않도록 Verovio toolkit을 들고 있는다.

    보통은(score-partwise면, 거의 항상) 줄당 마디 수·쪽당 줄 수를 MusicXML에 직접
    고정해둔다(_prepare_tree/_inject_breaks). Verovio 레이아웃 너비는 target_width_px와
    무관하게 늘 여유 있는 고정 폭(_PAGE_WIDTH)으로 잡는다(음표가 겹치지 않게,
    2026-10-07) — 실제 칸 너비에 맞추는 건 render_page가 호출될 때 resvg_py의 최종
    픽셀 폭 지정으로 처리한다(비율 유지 축소/확대라 겹침 없음).

    이 클래스는 Verovio에 쪽 나누기를 맡기는 단순한 판이다(악보 검사 notation_check가 씀).
    앱 화면은 칸 크기에 맞춰 줄 단위로 조판해 쪽을 짜 맞추는 layout_score를 쓴다 — Verovio
    쪽 나누기로는 옆 쪽과 줄 높이가 맞지 않았다(justifyVertically를 써도 최대 75px 어긋남).

    score-timewise 등 _prepare_tree가 못 먹는 형식일 때만 예전 방식(target_width_px/
    height_px로 Verovio 쪽 나누기 자체를 칸에 맞추는 _UNITS_PER_PX 비율)으로 돌아간다 —
    안 주면 A4 비슷한 기본 비율(_PAGE_WIDTH:_PAGE_HEIGHT)을 쓴다."""

    def __init__(self, path, target_width_px=None, target_height_px=None, scale=_SCALE):
        self._target_width_px = target_width_px
        self.toolkit = _new_toolkit()
        text = _read_xml_text(path)
        root, parts = _prepare_tree(text)
        breaks_applied = root is not None

        if not breaks_applied:
            if target_width_px and target_height_px:
                page_width = int(target_width_px * _UNITS_PER_PX)
                page_height = int(target_height_px * _UNITS_PER_PX)
            else:
                page_width, page_height = _PAGE_WIDTH, _PAGE_HEIGHT
            self.toolkit.setOptions({
                "pageWidth": page_width,
                "pageHeight": page_height,
                "scale": scale,
                "header": "none",
                "footer": "none",
                "adjustPageHeight": True,
                "breaks": "auto",
            })
            if not self.toolkit.loadData(text):
                raise ValueError(f"악보를 읽지 못했습니다: {path}")
            self.page_count = self.toolkit.getPageCount()
            return

        systems_per_page = _systems_per_page(len(parts))
        justify_vertically = False
        page_width, page_height = _PAGE_WIDTH, _PAGE_HEIGHT
        adjust_page_height = True

        final_text = _inject_breaks(root, parts, systems_per_page)
        self.toolkit.setOptions({
            "pageWidth": page_width,
            "pageHeight": page_height,
            "scale": scale,
            "header": "none",  # 1페이지 제목·작곡가 안 보이게(사용자 요청, 2026-10-07)
            "footer": "none",  # Verovio 기본 꼬리말("engraved with Verovio") 안 보이게
            "adjustPageHeight": adjust_page_height,
            "justifyVertically": justify_vertically,
            "breaks": "encoded",
        })
        if not self.toolkit.loadData(final_text):
            raise ValueError(f"악보를 읽지 못했습니다: {path}")
        self.page_count = self.toolkit.getPageCount()

    def render_page(self, page_num, target_width_px=None):
        """1부터 시작하는 페이지 번호 → PIL.Image(RGB, 흰 배경). target_width_px로
        최종 픽셀 해상도를 정한다(안 주면 생성할 때 준 target_width_px, 그것도
        없으면 1400px) — Verovio 레이아웃 단위와 무관하게 resvg_py가 맞춰 늘린다."""
        if not (1 <= page_num <= self.page_count):
            raise ValueError(f"페이지 번호 범위 밖: {page_num}(1~{self.page_count})")
        width = target_width_px or self._target_width_px or 1400
        return png_to_image(rasterize_svg(self.page_svg(page_num), width))

    def page_svg(self, page_num):
        """그 쪽의 SVG 문자열(캐시). Verovio 레이아웃은 그림 너비와 무관해서 한 번만 만든다
        — 창 크기가 바뀌어도 rasterize_svg만 다시 하면 된다."""
        if not hasattr(self, "_svgs"):
            self._svgs = {}
        svg = self._svgs.get(page_num)
        if svg is None:
            svg = self.toolkit.renderToSVG(page_num)
            self._svgs[page_num] = svg
        return svg

    def measure_boxes(self):
        """모든 쪽의 마디 상자 — measure_boxes_from_svgs 참고."""
        return measure_boxes_from_svgs([self.page_svg(p) for p in range(1, self.page_count + 1)])

    def timemap(self):
        """[{"tstamp": ms, "qstamp": 4분음표위치, "on": [음표id,...], "off": [...]}...] —
        score_follow.py의 q 단위와 같다(qstamp가 바로 그 q). 4단계(음표 위치 표)에서 씀."""
        return self.toolkit.renderToTimemap()


def measure_boxes_from_svgs(svgs, with_note_start=False):
    """모든 쪽의 마디 상자 [(쪽 번호(1부터), x0, y0, x1, y1)] — 인쇄 순서(=MusicXML
    마디 순서)대로. 좌표는 "쪽 너비 = 1"로 나눈 값이라 그린 이미지 너비(px)를 곱하면
    바로 픽셀이 된다. 상자 = 그 마디 안 모든 보표 5선의 가로·세로 범위(재생 위치
    강조·마디 클릭 판정용, 2026-10-07).

    Verovio SVG 구조: 바깥 <svg>(px 크기) 안에 viewBox가 레이아웃 단위인
    definition-scale <svg>, 그 안에 page-margin 이동(translate) 뒤 <g class="measure">
    → <g class="staff"> → 5선 <path d="M x y L x2 y">.

    with_note_start=True면 6번째 값으로 "그 마디 첫 음표·쉼표의 x"(없으면 x0)를 붙인다 — 줄의 첫 마디는
    왼쪽에 음자리표·조표·박자표가 있어 상자 왼쪽 끝이 첫 음표보다 한참 앞이라, 재생선을 상자 왼쪽에서
    출발시키면 "소리가 음표에 오기 전에 난다"(사용자 지적 2026-10-08). 재생선은 이 x에서 출발한다."""
    boxes = []
    for page, svg in enumerate(svgs, start=1):
        root = ET.fromstring(svg)
        inner = next((e for e in root.iter() if e.tag.endswith("svg") and e.get("viewBox")), None)
        if inner is None:
            continue
        vb_w = float(inner.get("viewBox").split()[2])
        mx = my = 0.0
        for g in root.iter():
            if g.tag.endswith("g") and g.get("class") == "page-margin":
                m = re.match(r"translate\(\s*([-\d.]+)\s*,\s*([-\d.]+)", g.get("transform") or "")
                if m:
                    mx, my = float(m.group(1)), float(m.group(2))
                break
        for g in root.iter():
            if not (g.tag.endswith("g") and g.get("class") == "measure"):
                continue
            xs, ys = [], []
            staff_ys = []      # 보표마다 (위 선 y, 아래 선 y) — 성부별 띠 칠하기용
            for staff in g:
                if not (staff.tag.endswith("g") and staff.get("class") == "staff"):
                    continue
                sy = []
                for path in staff:
                    if not path.tag.endswith("path"):
                        continue
                    nums = re.findall(r"[-\d.]+", path.get("d") or "")
                    if len(nums) == 4:
                        xs += [float(nums[0]), float(nums[2])]
                        ys += [float(nums[1]), float(nums[3])]
                        sy += [float(nums[1]), float(nums[3])]
                if sy:
                    staff_ys.append((min(sy), max(sy)))
            if xs:
                box = (page, (min(xs) + mx) / vb_w, (min(ys) + my) / vb_w,
                       (max(xs) + mx) / vb_w, (max(ys) + my) / vb_w)
                if with_note_start:
                    nx = _first_event_x(g)
                    box = box + (((nx + mx) / vb_w) if nx is not None else box[1],
                                 tuple((hx + mx) / vb_w for hx in _harm_xs(g)),
                                 tuple(((a + my) / vb_w, (b + my) / vb_w) for a, b in _staff_bands(staff_ys)))
                boxes.append(box)
    return boxes


def _staff_bands(staff_ys):
    """보표별 (위, 아래) 선 y → 보표마다 칠할 가로띠 [(위, 아래)] — 이웃 보표 사이는 가운데에서 갈라 보표 위아래의 음표·글자까지 포함,
    맨 위·아래는 보표 간격의 절반만큼 바깥으로."""
    if not staff_ys:
        return []
    gaps = [staff_ys[i + 1][0] - staff_ys[i][1] for i in range(len(staff_ys) - 1)]
    half = (sum(gaps) / len(gaps) if gaps else (staff_ys[0][1] - staff_ys[0][0]) * 2) / 2
    bands = []
    for i, (top, bot) in enumerate(staff_ys):
        a = top - half if i == 0 else (staff_ys[i - 1][1] + top) / 2
        b = bot + half if i == len(staff_ys) - 1 else (bot + staff_ys[i + 1][0]) / 2
        bands.append((a, b))
    return bands


def _harm_xs(measure_g):
    """마디 안 코드 이름(<harmony> → class="harm")의 왼쪽 x들(레이아웃 단위, 시간 순) — 코드 구간 경계를 실제 음표
    위치에 맞추는 데 씀(마디 시간 비율로 가르면 간격이 고르지 않은 마디에서 코드 색이 이웃 코드 위로 번짐)."""
    xs = []
    for el in measure_g.iter():
        if el.tag.endswith("g") and (el.get("class") or "").split()[:1] == ["harm"]:
            t = next((u for u in el.iter() if u.tag.endswith("text") and u.get("x")), None)
            if t is not None:
                xs.append(float(t.get("x")))
    return xs


def _first_event_x(measure_g):
    """마디 안 음표·쉼표 그림 중 가장 왼쪽 x(레이아웃 단위, page-margin 이동 전). 온마디 쉼표(mRest)는
    마디 가운데에 그려지므로 빼고, 음표는 머리(notehead)의 위치를 쓴다(임시표는 머리 왼쪽이라 제외)."""
    xs = []
    for el in measure_g.iter():
        cls = (el.get("class") or "").split()
        if not cls or not el.tag.endswith("g"):
            continue
        if cls[0] == "notehead" or cls[0] == "rest":
            for u in el.iter():
                if u.tag.endswith("use"):
                    m = re.match(r"translate\(\s*([-\d.]+)", u.get("transform") or "")
                    if m:
                        xs.append(float(m.group(1)))
    return min(xs) if xs else None


class LaidOutScore:
    """보이는 쪽 배치 결과 — SystemSet.arrange가 만든다(사용자 요청 2026-10-07: 최대화 시 한 쪽에
    4성부 묶음 2~3줄, 옆 쪽과 줄 높이 일치, 음표 크기는 기존 출판 악보와 같게).

    Verovio에 쪽을 맡기면 줄 위 글자(빠르기말·지시어) 양에 따라 줄 위치가 쪽마다 수십 px씩
    어긋났다(justifyVertically를 써도 — 실측 최대 75px). 그래서 줄마다 SVG 하나로 조판해 두고
    (layout_systems_file), 쪽 안 k번째 자리마다 "첫 보표 맨 윗선" 높이를 하나로 정해 모든 쪽의
    k번째 줄을 거기 놓는다 — 그러면 모든 쪽에서 k번째 줄의 오선이 정확히 같은 높이다.

    좌표는 모두 "쪽 너비 = 1" 비율(그릴 때 픽셀 너비를 곱함).
      systems[i] = (svg, 보이는 쪽 번호(1부터), 쪽 안 y 위치)
      page_ratios[p] = 쪽 p의 높이 / 너비 — 펼침면(나란히 보이는 두 쪽)끼리는 같다. 보통은 칸
        비율, 유난히 큰 줄이 있는 펼침면만 더 길다(앱이 그 펼침면을 칸 높이에 맞춰 줄여 그림)."""

    def __init__(self, systems, page_count, systems_per_page, page_ratios, slot):
        self.systems = systems
        self.page_count = page_count
        self.systems_per_page = systems_per_page
        self.page_ratios = page_ratios
        self.slot = slot  # 배치한 칸 크기(px)

    def page_ratio(self, page):
        return self.page_ratios.get(page, self.slot[1] / self.slot[0])

    def page_systems(self, page):
        return [i for i, (_svg, p, _y) in enumerate(self.systems) if p == page]

    def page_svg(self, page_num):
        """그 쪽 첫 줄의 SVG(쪽 단위 도구 호환용)."""
        idx = self.page_systems(page_num)
        return self.systems[idx[0]][0] if idx else ""


MELODY_ONLY_FIELD = "GenerateScore-melody-only-measures"
MEASURES_PER_LINE = 4          # 동요 모드: 한 줄에 마디 4개
SINGLE_PAGE_MAX_STAVES = 12     # 동요 모드에서 보표(줄 수 × 성부 수)가 이 이하면 한 쪽에 다 담음 — 더 많으면 음표가 너무 작아져 쪽을 나눔


def song_layout_mode(root, path=""):
    """"한 줄 4마디·줄 끝까지 펼침·한 쪽에 꽉" 조판을 쓸 악보인가 — 이 앱이 만든 화성·색깔 악보(`GenerateScore-…` 필드나 HarmGuitar 성부)
    이거나 돌림노래(제목·파일 이름). 일반 악보는 Verovio 자동 줄 나눔 그대로(기존 악보 모양을 바꾸지 않으려고)."""
    if any((f.get("name") or "").startswith("GenerateScore-") for f in root.iter("miscellaneous-field")):
        return True
    if any((p.get("id") or "").startswith("HarmGuitar") for p in root.findall("part")):
        return True
    texts = [root.findtext("movement-title"), root.findtext("work/work-title")] + [w.text for w in root.iter("credit-words")]
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    return any("돌림노래" in (t or "") for t in texts + [stem])


def melody_only_measures(root):
    """화성을 붙일 때 "처음 N마디는 멜로디만"으로 만든 악보면 N(harmonize가 <miscellaneous-field>로 적어 둠), 아니면 0."""
    for f in root.iter("miscellaneous-field"):
        if f.get("name") == MELODY_ONLY_FIELD:
            try:
                return max(0, int((f.text or "0").strip()))
            except ValueError:
                return 0
    return 0


def _split_solo_intro(root, n):
    """멜로디만 있는 앞 n마디를 따로 떼어 (앞부분 악보, 나머지 악보)로 만든다. 앞부분은 첫 파트(멜로디) 한 성부만,
    나머지는 모든 성부의 n번째 마디부터 — 조표·박자표·자리표가 첫 마디에 있으므로 나머지 첫 마디에 복사해 넣는다.
    (Verovio에는 줄마다 쉬는 보표를 숨기는 믿을 만한 방법이 없어서 — optimize는 MusicXML·MEI 모두 안 먹었음 — 두 악보로 따로
    조판해 줄을 이어 붙인다. 마디 번호는 그대로.)"""
    parts = root.findall("part")
    total = len(parts[0].findall("measure"))
    intro = copy.deepcopy(root)
    for p in intro.findall("part")[1:]:
        intro.remove(p)
    for sp in intro.findall("part-list/score-part")[1:]:
        intro.find("part-list").remove(sp)
    ip = intro.find("part")
    for m in ip.findall("measure")[n:]:
        ip.remove(m)
    main = copy.deepcopy(root)
    for p in main.findall("part"):
        ms = p.findall("measure")
        first_attr = ms[0].find("attributes")
        for m in ms[:n]:
            p.remove(m)
        head = ms[n]
        if first_attr is not None:
            attr = head.find("attributes")
            if attr is None:
                attr = copy.deepcopy(first_attr)
                head.insert(0, attr)
            else:
                have = {c.tag for c in attr}
                for c in first_attr:
                    if c.tag not in have:
                        attr.insert(0, copy.deepcopy(c))
    return (intro, main) if 0 < n < total else (None, None)


def _system_starts(root, page_width, scale):
    """Verovio 자동 줄 나눔(내용 밀도에 따라 줄마다 마디 수가 다름 — 16분음표가 빽빽한
    마디는 한 줄에 적게, 성긴 마디는 많이)으로 한 번 조판해, 각 줄이 몇 번째 마디(인쇄 순서,
    0부터)에서 시작하는지 돌려준다. 쪽 높이는 보통(A4 비율)으로 — 아주 긴 쪽 하나로 하면
    Verovio가 느려지는 경우가 있었다."""
    tk = _new_toolkit()
    tk.setOptions({
        "pageWidth": int(page_width), "pageHeight": int(page_width * 1.414), "scale": scale,
        "header": "none", "footer": "none", "adjustPageHeight": True,
        "breaks": "auto",
        "spacingLinear": _SPACING_LINEAR, "spacingNonLinear": _SPACING_NONLINEAR,
    })
    if not tk.loadData(ET.tostring(root, encoding="unicode")):
        raise ValueError("악보를 읽지 못했습니다")
    starts = []
    index = 0
    for page in range(1, tk.getPageCount() + 1):
        svg_root = ET.fromstring(tk.renderToSVG(page))
        for g in svg_root.iter():
            if not (g.tag.endswith("g") and (g.get("class") or "").split()[:1] == ["system"]):
                continue
            n = sum(1 for m in g.iter() if m.tag.endswith("g") and m.get("class") == "measure")
            if n:
                starts.append(index)
                index += n
    return starts


def _layout_systems(root, page_width, scale=_SCALE, fixed_lines=False):
    """줄마다 SVG 하나로 조판한다 — 자동 줄 나눔으로 줄 경계를 정한 뒤(_system_starts), 그
    경계마다 새 쪽 표시를 꽂아 다시 조판(breaks=encoded)해서 쪽 하나 = 줄 하나가 되게."""
    n_solo = melody_only_measures(root)
    if n_solo:
        intro, main = _split_solo_intro(root, n_solo)
        if intro is not None:
            # 화성을 붙일 때 "처음 N마디는 멜로디만" — 그 앞부분은 멜로디 한 보표 줄로, 나머지는 모든 성부 줄로 따로 조판
            for r in (intro, main):
                for f in list(r.iter("miscellaneous-field")):
                    if f.get("name") == MELODY_ONLY_FIELD:
                        f.set("name", MELODY_ONLY_FIELD + "-done")
            return (_layout_systems(intro, page_width, scale, fixed_lines)
                    + _layout_systems(main, page_width, scale, fixed_lines))
    if fixed_lines:
        # 동요 모드 — 한 줄에 MEASURES_PER_LINE마디씩(자동 줄 나눔 대신)
        starts = set(range(0, len(root.findall("part")[0].findall("measure")), MEASURES_PER_LINE))
    else:
        starts = set(_system_starts(root, page_width, scale))
    tree = copy.deepcopy(root)
    measures = tree.findall("part")[0].findall("measure")
    for i, measure in enumerate(measures):
        if i in starts and i != 0:
            pr = ET.Element("print")
            pr.set("new-page", "yes")
            measure.insert(0, pr)
    tk = _new_toolkit()
    tk.setOptions({
        "pageWidth": int(page_width), "pageHeight": 60000, "scale": scale,
        "header": "none", "footer": "none", "adjustPageHeight": True, "breaks": "encoded",
        "spacingLinear": _SPACING_LINEAR, "spacingNonLinear": _SPACING_NONLINEAR,
        # 쪽마다 줄 하나라서 "마지막 줄"로 취급돼 자연 폭이 쪽의 80% 아래면 늘리지 않는다 → 동요 모드는 늘 쪽 너비까지 펼침
        **({"minLastJustification": 0} if fixed_lines else {}),
    })
    if not tk.loadData(ET.tostring(tree, encoding="unicode")):
        raise ValueError("악보를 읽지 못했습니다")
    return [tk.renderToSVG(p) for p in range(1, tk.getPageCount() + 1)]


class SystemSet:
    """한 곡을 줄 단위로 조판한 결과 — 창 크기와 무관하게 곡마다 한 번만 만든다.
      svgs[i]  : 줄 i의 SVG
      tops[i]  : 줄 i의 첫 보표 맨 윗선 y (너비 = 1 비율)
      heights[i]: 줄 i 전체 높이
      boxes[i] : 줄 i 안의 마디 상자들 [(x0, y0, x1, y1, 첫 음표 x)] (줄 안 좌표)"""

    def __init__(self, svgs, tops, heights, boxes, base_systems, single_page=False):
        self.single_page = single_page      # 줄이 적은 동요 모드 — 모든 줄을 한 쪽에 담고 창 너비를 다 씀
        self.svgs = svgs
        self.tops = tops
        self.heights = heights
        self.boxes = boxes
        self.base = base_systems

    def _rows_need(self, idx, n):
        """줄 목록 idx(한 펼침면의 줄들, 순서대로)를 쪽당 n줄로 놓을 때 k번째 자리 줄들의
        (위로 튀어나온 최대, 아래로 내려간 최대)와 그걸 다 담는 쪽 높이(너비 비율)."""
        tops = [max([self.tops[i] for j, i in enumerate(idx) if j % n == k] or [0.0]) for k in range(n)]
        bels = [max([self.heights[i] - self.tops[i] for j, i in enumerate(idx) if j % n == k] or [0.0])
                for k in range(n)]
        return tops, bels, (sum(tops) + sum(bels)) * _FIT_SLACK

    def _spreads(self, n):
        """쪽당 n줄일 때 펼침면별 줄 번호 목록."""
        per = 2 * n
        return [list(range(a, min(len(self.svgs), a + per))) for a in range(0, len(self.svgs), per)]

    def arrange(self, slot_w, slot_h, max_systems=None):
        """칸(slot_w×slot_h px)에 맞춰 쪽을 짠다 → (LaidOutScore, 보이는 쪽 기준 마디 상자).
        Verovio를 다시 돌리지 않으므로 순식간 — 창 크기가 바뀔 때마다 불러도 된다.

        줄 높이 맞추기는 펼침면(나란히 보이는 두 쪽) 단위 — 요청은 "옆 쪽과 일치"라서, 곡 전체에서
        유난히 큰 줄 하나가 모든 쪽의 줄 칸을 키우지 않게 한다(처음엔 곡 전체 기준이라 줄 사이가
        휑하고 쪽이 칸보다 작아졌다, 2026-10-07).

        쪽당 줄 수 = 펼침면의 8할 이상이 칸 높이에 들어가는 최대값(최대 기준+1 — 4성부 3줄,
        최소 기준 — 4성부 2줄). 안 들어가는 펼침면은 쪽을 칸보다 세로로 길게 잡는다 — 앱이 그
        펼침면만 칸 높이에 맞춰 통째로 줄여 그리므로 아래가 잘리지 않는다."""
        ratio = slot_h / slot_w
        lo = min(self.base, len(self.svgs))
        n = lo
        if self.single_page:
            n = max(1, len(self.svgs))      # 한 쪽에 모든 줄(쪽이 칸보다 세로로 길면 앱이 통째로 줄여 그림)
        for c in ([] if self.single_page else range(max_systems or self.base + 1, lo - 1, -1)):
            spreads = self._spreads(c)
            fits = sum(1 for idx in spreads if self._rows_need(idx, c)[2] <= ratio)
            if fits >= _FIT_SHARE * len(spreads):
                n = c
                break
        systems = [None] * len(self.svgs)
        boxes = []
        page_ratios = {}
        for si, idx in enumerate(self._spreads(n)):
            tops, bels, need = self._rows_need(idx, n)
            page_ratio = max(ratio, need)
            gap = max(0.0, (page_ratio - need / _FIT_SLACK) / n)   # 남는 높이를 자리마다 고르게
            row_y = []                                              # k번째 자리 "첫 보표 맨 윗선"
            y = 0.0
            for k in range(n):
                y += gap / 2 + tops[k]
                row_y.append(y)
                y += bels[k] + gap / 2
            for j, i in enumerate(idx):
                page = 2 * si + j // n + 1
                page_ratios[page] = page_ratio
                dy = row_y[j % n] - self.tops[i]
                systems[i] = (self.svgs[i], page, dy)
        for i, (_svg, page, dy) in enumerate(systems):
            for x0, y0, x1, y1, nx, hx, bands in self.boxes[i]:
                boxes.append((page, x0, y0 + dy, x1, y1 + dy, nx, hx, tuple((a + dy, c + dy) for a, c in bands)))
        page_count = (len(self.svgs) + n - 1) // n
        return LaidOutScore(systems, page_count, n, page_ratios, (int(slot_w), int(slot_h))), boxes


def layout_systems_file(path):
    """악보 파일 → SystemSet. 앱이 ProcessPoolExecutor로 부른다 — Verovio 조판은 GIL을 오래
    잡아서(곡 길이에 따라 1~5초) 같은 프로세스에서 하면 재생 소리가 끊길 수 있다.

    음표 크기는 기존 출판 악보에 맞춰 고정 — 오선 한 칸이 줄 너비의 약 1/105, 우리 단위로
    쪽 너비 _PAGE_WIDTH(2100)일 때와 같다(사용자가 보여준 슈베르트 출판본 실측, 2026-10-07).
    줄마다 마디 수는 내용 밀도에 따라 Verovio 자동 줄 나눔(가로 간격은 _SPACING_*)."""
    root, parts = _prepare_tree(_read_xml_text(path))
    if root is None:
        raise ValueError("score-partwise 형식만 지원합니다")
    song_mode = song_layout_mode(root, path)
    svgs = _layout_systems(root, _PAGE_WIDTH, fixed_lines=song_mode)
    tops, heights, boxes = [], [], []
    for svg in svgs:
        m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
        b = [(x0, y0, x1, y1, nx, hx, bands) for _p, x0, y0, x1, y1, nx, hx, bands in measure_boxes_from_svgs([svg], True)]
        tops.append(min(y0 for _x0, y0, _x1, _y1, _nx, _hx, _bands in b) if b else 0.0)
        heights.append(float(m.group(2)) / float(m.group(1)))
        boxes.append(b)
    title, composer = _title_and_composer(root)
    if svgs and (title or composer):
        svgs[0], extra = _add_title_block(svgs[0], title, composer)
        tops[0] += extra
        heights[0] += extra
        boxes[0] = [(x0, y0 + extra, x1, y1 + extra, nx, hx, tuple((a + extra, c + extra) for a, c in bands))
                    for x0, y0, x1, y1, nx, hx, bands in boxes[0]]
    return SystemSet(svgs, tops, heights, boxes, _systems_per_page(len(parts)),
                     single_page=song_mode and 0 < len(svgs) and
                     sum(len(b[0][6]) for b in boxes if b) <= SINGLE_PAGE_MAX_STAVES)


# ---------------------------------------------------------------- 첫 쪽 제목·작곡가
# 원본 악보(PDF) 첫 쪽처럼 맨 위 가운데 제목, 오른쪽에 작곡가(사용자 지적 2026-10-07). Verovio
# 머리글(header="auto")은 작곡가를 안 넣고, 제목 가운데 정렬이 resvg에서 깨지고, 둘째 쪽부터
# 쪽 번호 대시(–)가 깨진 글자로 나와서 쓰지 않고 첫 줄 SVG 위에 직접 글자를 얹는다.
_TITLE_BLOCK_PX = 64        # 첫 줄 SVG(가로 840px 기준) 위에 덧붙이는 높이
_TITLE_FONT = "Times New Roman, Malgun Gothic, serif"


def _title_and_composer(root):
    title = (root.findtext("movement-title") or root.findtext("work/work-title") or "").strip()
    composer = ""
    for c in root.findall("identification/creator"):
        if c.get("type") == "composer" and (c.text or "").strip():
            composer = c.text.strip()
            break
    return title, composer


def _add_title_block(svg, title, composer):
    """줄 SVG 맨 위에 제목·작곡가 띠를 붙인다 → (새 SVG, 늘어난 높이(너비 = 1 비율)).
    바깥 <svg>(px 크기, viewBox 없음)의 높이를 늘리고 안쪽 악보 <svg>를 그만큼 아래로 내린다."""
    from xml.sax.saxutils import escape
    m = re.search(r'<svg width="([\d.]+)px" height="([\d.]+)px"', svg)
    if not m:
        return svg, 0.0
    w, h = float(m.group(1)), float(m.group(2))
    add = _TITLE_BLOCK_PX
    svg = svg.replace(m.group(0), f'<svg width="{w:g}px" height="{h + add:g}px"', 1)
    svg = svg.replace('<svg class="definition-scale"',
                      f'<svg y="{add}" width="{w:g}" height="{h:g}" class="definition-scale"', 1)
    texts = ""
    if title:
        texts += (f'<text x="{w / 2:g}" y="{add * 0.55:g}" text-anchor="middle" font-size="{add * 0.42:g}" '
                  f'font-family="{_TITLE_FONT}">{escape(title)}</text>')
    if composer:
        texts += (f'<text x="{w * 0.97:g}" y="{add * 0.95:g}" text-anchor="end" font-size="{add * 0.2:g}" '
                  f'font-family="{_TITLE_FONT}">{escape(composer)}</text>')
    i = svg.index('<svg y="')
    svg = svg[:i] + texts + svg[i:]
    return svg, add / w


def layout_score(path, slot_w, slot_h, max_systems=None):
    """한 번에: 줄 단위 조판 + 칸에 맞춘 배치 → (LaidOutScore, 마디 상자). 시험·도구용."""
    return layout_systems_file(path).arrange(slot_w, slot_h, max_systems)
