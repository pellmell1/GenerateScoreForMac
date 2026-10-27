# -*- coding: utf-8 -*-
"""멜로디에 화성을 붙인다 — 한 성부(멜로디)의 음표를 보고 코드 진행을 정한 뒤, 반주 성부(1~4개, 성부마다
악기를 고름 — 기본은 기타 화음 + 기타 저음)를 악보에 새 성부로 더해 MusicXML로 돌려준다.

악보 자체에 성부를 더하는 방식이라 그린 악보·재생·WAV 저장이 모두 기존 코드 그대로 동작한다.

흐름:
  1. 멜로디 성부의 음표(소리 높이 기준)와 마디 길이를 읽는다(붙임줄·장식음 무시, 마디 안 backup/forward 처리).
  2. 조를 정한다 — 조표(있으면 으뜸음 후보를 장조/나란한 단조 둘로 좁힘)와 음 길이 가중 음높이 분포를
     Krumhansl-Kessler 프로필에 맞추고, 마지막 음이 으뜸음이면 가산.
  3. 마디를 코드 칸으로 나눈다(박이 4개 이상이면 마디당 2칸, 아니면 1칸).
  4. 칸마다 후보 코드(조의 온음계 3화음 + 딸림7화음)를 Viterbi로 고른다 — 멜로디 음이 코드톤이면 가산,
     화성 밖 음이면 감산(방출), 코드 진행 선호(딸림→으뜸, 버금딸림→딸림 …)와 시작·끝 마침꼴 가산(전이).
  5. 반주 성부(1~4개)의 음을 voice_lead로 정한다 — 화성법으로 모든 성부를 함께 고르고(병행 5·8도·엇갈림 없이,
     이끎음·7음 해결, 근음 겹침, 공통음·순차 우선), 대위법으로 움직임을 마디마다 성부끼리 돌려 맡긴다(경과음·
     이웃음·이탈음·선행음·계류음 등 화음 밖 음도 과감하게, 멜로디가 바쁘면 느리게). 코드 이름(`<harmony>`)은 1성부
     위에 단다. 무작위 씨앗으로 만들 때마다 다른 반주(같은 씨앗이면 같은 반주). 결과 검사: voice_check.py.

한계: 조 바뀜(조표 변화)은 따라가지 않고 한 곡 한 조로 본다. 으뜸·버금딸림·딸림 중심의 단순한 진행이며
빌려온 화음·부속화음은 쓰지 않는다. 결과는 "초안" — 마음에 안 들면 악보 프로그램에서 고쳐 쓰면 된다.

명령줄:  python harmonize.py 곡.xml [-o 곡_화성.musicxml] [--melody 1] [--style arpeggio|block] [--no-bass]
        [--voices guitar:n4:f16,cello,contrabass_guitar:dropd:n2]   (반주 성부 1~4개, 위에서부터 악기 —
        :dropd 드롭 D, :n4 화음 음 수 최대 4(화음 악기만), :f16 가장 빠른 음표 16분; 모든 성부 — 저음은 한계로만)
"""
import argparse
import copy
import os
import random
import re
import xml.etree.ElementTree as ET

import musicxml_reader
import voice_lead

# ---------------------------------------------------------------- 음이론

# 5도권 위치: C=0, G=1, D=2 … F=-1. 음이름·변화표는 이 위치에서 바로 나온다(철자가 조에 맞게).
_LETTERS = "FCGDAEB"
_LETTER_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

# 으뜸음 기준 온음계 각 음의 5도권 위치(장조 / 자연 단조). 단조의 딸림·이끎 화음은 7음을 +5(으뜸음의 이끎음)로 올린다.
_MAJOR_SCALE = [0, 2, 4, -1, 1, 3, 5]
_MINOR_SCALE = [0, 2, -3, -1, 1, -4, -2]
_LEADING = 5

# 화성 선호(앞 화음 → 뒤 화음, 으뜸음 기준 도수 0~6). 클수록 자연스러운 진행.
_TRANSITION = [
    #  I     ii    iii   IV    V     vi    vii
    [-0.2, 0.7, 0.4, 1.0, 1.0, 0.8, 0.2],     # I
    [0.2, -0.2, 0.1, 0.2, 1.2, 0.1, 0.3],     # ii
    [0.0, 0.3, -0.2, 0.8, 0.2, 1.0, 0.0],     # iii
    [0.8, 0.6, 0.0, -0.2, 1.2, 0.3, 0.2],     # IV
    [1.5, 0.0, 0.2, 0.2, -0.2, 0.8, 0.1],     # V
    [0.2, 1.0, 0.3, 1.0, 0.8, -0.2, 0.0],     # vi
    [1.3, 0.0, 0.6, 0.0, 0.0, 0.6, -0.2],     # vii
]

_EMISSION_WEIGHT = 2.5   # 멜로디 맞춤 vs 코드 진행 선호의 비중
_V7_PENALTY = 0.25       # 딸림7화음은 4도음(7음)이 멜로디에 있을 때만 이기게
_CADENCE_TONIC = 3.0     # 마지막 칸이 으뜸화음이면
_CADENCE_APPROACH = {4: 1.0, 3: 0.5}   # 끝에서 둘째 칸이 딸림(1.0)·버금딸림(0.5)이면
_START_TONIC = 0.5

# 반주 음역(MIDI)
# 반주 악기별 음역(소리 나는 높이 MIDI)·자리표·성부 이름. 음역 밖의 음은 쓰지 않는다.
#  - 기타: 가장 낮은 음 E2(40, 6번 줄 개방현). 낮은음자리표를 쓰지 않고 높은음자리표 8vb(소리는 적힌 것보다 한 옥타브 아래)로
#    저음까지 한 보표에 적는다. 화음·저음 성부 둘 다 기타.
#  - 건반: 오른손 높은음자리표 / 왼손 낮은음자리표(두 성부로 나눠 적음), 음역은 넉넉히 E1~G6 안.
#  - 첼로: 낮은음자리표, C2(36)~. 활로 켜는 지속음(GM 43 → synth "sustain").
#  - 글로켄슈필: 적힌 것보다 두 옥타브 높게 소리 남 — 높은음자리표 15ma(옥타브 변화 +2), 소리 G5(79)~.
#  - 자일로폰: 적힌 것보다 한 옥타브 높게 소리 남 — 높은음자리표 8va(옥타브 변화 +1), 소리 F4(65)~.
#  첼로·글로켄슈필·자일로폰은 mono(한 번에 한 음) — 화음 성부를 맡으면 블록 화음 대신 분산화음으로 쓴다.
#  - 콘트라베이스 기타: 기타보다 한 옥타브 낮게(소리 E1(28)~). 기타처럼 읽히게 높은음자리표 15vb(옥타브 변화 -2) +
#    <transpose octave-change=-1>로 적는다 — musicxml_reader가 콘트라베이스 기타(이름·15vb 자리표)를 +12 보정하므로
#    적은 음(소리 높이) −12 +12 = 소리 높이 그대로 재생되고, Verovio는 적은 음을 15vb로 그려 6번 줄 E1이 E3 자리에 보인다.
#  - 드롭 D(기타·콘트라베이스 기타, 성부 지정 "guitar:dropd"): 6번 줄을 온음 내림 — 가장 낮은 음(lowest)에서 시작하는
#    음역만 2반음 아래로 넓히고 첫 마디에 "⑥=D"를 적는다(2026-10-27 사용자 요청).
#  (2026-10-27 사용자 요청: 반주 성부를 4개까지, 성부마다 악기를 따로 고르기)
# chord: 화음(아르페지오·블록) 음역, bass: 저음 성부 음역, inner: 동요 중간 성부·가운데 성부, low: 동요 낮은 성부
INSTRUMENTS = {
    "guitar": {
        "label": "기타", "short": "기타", "program": 25, "midi_name": "Classical Guitar", "lowest": 40, "drop_d": True,
        "chord": (45, 72), "bass": (40, 52), "inner": (50, 72), "low": (40, 52),
        "high_clef": ("G", 2, -1), "low_clef": ("G", 2, -1),
    },
    "contrabass_guitar": {
        "label": "콘트라베이스 기타", "short": "콘트라기타", "program": 25, "midi_name": "Contrabass Guitar", "lowest": 28, "drop_d": True,
        "transpose_octave": -1,
        "chord": (40, 60), "bass": (28, 43), "inner": (40, 60), "low": (28, 43),
        "high_clef": ("G", 2, -2), "low_clef": ("G", 2, -2),
    },
    "keyboard": {
        "label": "건반", "short": "오른손", "bass_short": "왼손", "program": 1, "midi_name": "Acoustic Grand Piano",
        "chord": (55, 79), "bass": (28, 52), "inner": (55, 79), "low": (33, 55),
        "high_clef": ("G", 2, 0), "low_clef": ("F", 4, 0),
    },
    "cello": {
        "label": "첼로", "short": "첼로", "program": 43, "midi_name": "Cello", "mono": True,
        "chord": (48, 72), "bass": (36, 55), "inner": (48, 69), "low": (36, 55),
        "high_clef": ("F", 4, 0), "low_clef": ("F", 4, 0),
    },
    "glockenspiel": {
        "label": "글로켄슈필", "short": "글로켄", "program": 10, "midi_name": "Glockenspiel", "mono": True,
        "chord": (79, 100), "bass": (79, 91), "inner": (79, 96), "low": (79, 91),
        "high_clef": ("G", 2, 2), "low_clef": ("G", 2, 2),
    },
    "xylophone": {
        "label": "자일로폰", "short": "자일로", "program": 14, "midi_name": "Xylophone", "mono": True,
        "chord": (67, 88), "bass": (65, 77), "inner": (65, 84), "low": (65, 77),
        "high_clef": ("G", 2, 1), "low_clef": ("G", 2, 1),
    },
}
# 고르는 화면에 붙이는 짧은 설명
INSTRUMENT_NOTES = {"guitar": "가장 낮은 음 E2, 높은음자리표 8vb",
                    "contrabass_guitar": "기타보다 한 옥타브 낮게(가장 낮은 음 E1), 높은음자리표 15vb", "keyboard": "오른손 높은음자리표 / 왼손 낮은음자리표",
                    "cello": "낮은음자리표, 한 음씩", "glockenspiel": "적힌 것보다 두 옥타브 높게, 한 음씩",
                    "xylophone": "적힌 것보다 한 옥타브 높게, 한 음씩"}

MAX_VOICES = 4  # 반주 성부 최대 개수(멜로디 제외) — 건반이 혼자면 오른손·왼손으로 나뉘어 악보 성부는 더 많아짐
DROP_D = ":dropd"   # 성부 지정 끝에 붙이면 드롭 D(예: "guitar:dropd")
HANDS = ("rh", "lh")   # 건반 손(expand_hands가 붙임) — 오른손 높은음자리표·왼손 낮은음자리표


# 성부 설정(2026-10-27 사용자 요청) — 성부 지정 끝에 붙인다. 모든 성부에 적용하되 저음 성부(2성부 이상일 때
# 맨 아래)는 특수한 경우라 꼭 지키지 않는다(한계로만 — 화음은 BASS_MAX_STACK음까지, 빠른 음표는 "이보다 빠르지 않게").
#  ":n4" 화음 음 수 최대 4(1~MAX_CHORD_NOTES, 없으면 자동 = 코드 구성음 수) — 화음 악기(mono가 아닌 것)만
#  ":f8" 가장 빠른 음표 8분(FASTEST_CHOICES, 없으면 자동) — 한 음씩 내는 악기(첼로 등)도 정할 수 있다.
#  두 값 다 늘 지키는 게 아니라 한계 — 곡에서 voice_lead.MIN_USES(3)곳 넘게 나오면 되고 나머지는 성부 내용에 따라
#  탄력적으로(사용자 요청 2026-10-27). 반주 방식(자동 판정된 동요식·블록 포함)과 상관없이 그만큼은 꼭 나온다
#  (사용자 지적 2026-10-27: "8분음표를 골랐는데 안 나옴" — 동요식은 길게만 끌었음).
MAX_CHORD_NOTES = 6
FASTEST_CHOICES = (16, 8, 4, 2)     # 음표 이름의 분모(16분·8분·4분·2분)
DEFAULT_FASTEST = 0                 # 자동 — 분산화음 방식이면 8분, 그 밖은 칸 길이대로
BASS_MAX_STACK = 2                  # 저음 성부 화음은 근음+5음까지(낮은 화음은 탁함)


def parse_voice_opts(spec):
    """성부 지정("guitar", "guitar:dropd:n4:f16" …) → (악기 키, {"drop", "notes"(0 = 자동), "fastest"})."""
    key, *tokens = spec.split(":")
    if key not in INSTRUMENTS:
        raise ValueError(f"반주 악기는 {', '.join(INSTRUMENTS)} 중에서 고르세요.")
    ins = INSTRUMENTS[key]
    opts = {"drop": False, "notes": 0, "fastest": DEFAULT_FASTEST, "hand": None}
    for t in tokens:
        if t == DROP_D[1:]:
            opts["drop"] = True
        elif t in HANDS and key == "keyboard":
            opts["hand"] = t
        elif t[:1] == "n" and t[1:].isdigit() and 1 <= int(t[1:]) <= MAX_CHORD_NOTES:
            opts["notes"] = int(t[1:])
        elif t[:1] == "f" and t[1:].isdigit() and int(t[1:]) in FASTEST_CHOICES:
            opts["fastest"] = int(t[1:])
        else:
            raise ValueError(f"성부 지정을 알 수 없습니다: {spec} (예: guitar:dropd:n4:f16)")
    if opts["drop"] and not ins.get("drop_d"):
        raise ValueError(f"드롭 D는 기타·콘트라베이스 기타만 됩니다({ins['label']}).")
    if ins.get("mono") and opts["notes"]:
        raise ValueError(f"화음 음 수는 화음을 내는 악기만 정할 수 있습니다({ins['label']}).")
    return key, opts


def parse_voice(spec):
    """성부 지정 → (악기 키, 드롭 D?)."""
    key, opts = parse_voice_opts(spec)
    return key, opts["drop"]


def make_voice(key, drop=False, notes=0, fastest=DEFAULT_FASTEST, hand=None):
    """parse_voice_opts의 반대 — 기본값인 항목은 붙이지 않는다."""
    return key + (DROP_D if drop else "") + (f":n{notes}" if notes else "") + \
        (f":f{fastest}" if fastest != DEFAULT_FASTEST else "") + (f":{hand}" if hand else "")


def expand_hands(specs):
    """건반 성부에 손(":rh" 오른손 / ":lh" 왼손)을 붙인다 — 건반을 고르면 오른손·왼손이 다 나와야 함(사용자 요청
    2026-10-27). 이웃한 건반 성부가 둘 이상이면 이미 한 대의 피아노라 맨 아래가 왼손·나머지 오른손, 건반이 혼자면
    오른손(높은음자리표)·왼손(낮은음자리표) 두 성부로 나눈다(설정은 두 손 다 같게). 손이 이미 붙은 건 그대로."""
    out, i, n = [], 0, len(specs)
    while i < n:
        key, o = parse_voice_opts(specs[i])
        if key != "keyboard" or o["hand"]:
            out.append(specs[i])
            i += 1
            continue
        j = i + 1
        while j < n and parse_voice_opts(specs[j])[0] == "keyboard" and not parse_voice_opts(specs[j])[1]["hand"]:
            j += 1
        run = specs[i:j]
        if len(run) == 1:
            out += [specs[i] + ":rh", specs[i] + ":lh"]
        else:
            out += [sp + (":lh" if k == len(run) - 1 else ":rh") for k, sp in enumerate(run)]
        i = j
    return out


def voice_instrument(spec):
    """성부 지정 → 그 성부에 쓸 악기 정보(dict). 드롭 D면 가장 낮은 음에서 시작하는 음역을 2반음 아래로."""
    key, opts = parse_voice_opts(spec)
    drop = opts["drop"]
    ins = dict(INSTRUMENTS[key])
    ins["max_notes"] = opts["notes"]
    ins["hand"] = opts["hand"]
    ins["unit"] = DIVISIONS * 4 // opts["fastest"] if opts["fastest"] else None   # 정한 음표 길이(divisions), 8분 = 2
    if drop:
        for role in ("chord", "bass", "inner", "low"):
            lo, hi = ins[role]
            if lo == ins["lowest"]:
                ins[role] = (lo - 2, hi)
        ins["label"] += "(드롭 D)"
        ins["dropped"] = True
    return ins


def voice_part_names(specs):
    """성부 지정 목록(위에서부터) → 악보에 적을 성부 이름(사용자 요청 2026-10-27: 기타1·기타2·콘트라기타·자일로·
    글로켄·오른손·왼손 …). 건반은 손(expand_hands)대로, 손이 없으면 맨 아래(저음, 2성부 이상)면 왼손, 나머지는
    오른손. 같은 이름이 둘 이상이면 1부터 번호."""
    n = len(specs)
    base = []
    for i, sp in enumerate(specs):
        key, o = parse_voice_opts(sp)
        ins = INSTRUMENTS[key]
        lh = o["hand"] == "lh" if o["hand"] else (n >= 2 and i == n - 1)
        base.append(ins["bass_short"] if "bass_short" in ins and lh else ins["short"])
    seen = {}
    out = []
    for b in base:
        if base.count(b) > 1:
            seen[b] = seen.get(b, 0) + 1
            out.append(f"{b}{seen[b]}")
        else:
            out.append(b)
    return out


def voice_note(spec):
    """고르는 화면에 붙이는 설명 — 드롭 D면 가장 낮은 음을 바꿔 적는다."""
    key, drop = parse_voice(spec)
    text = INSTRUMENT_NOTES[key]
    if drop:
        text = text.replace("E2", "D2(드롭 D)").replace("E1", "D1(드롭 D)")
    return text


# ---------------------------------------------------------------- 성부 악기 고르기 가이드
# 설정 창에서 고를 때마다 보여 주는 안내(2026-10-27 사용자 요청: "성부의 악기를 잘못 설정하면 가이드").
# 막지는 않는다 — 일부러 고른 것일 수 있으니 무엇이 이상하게 들릴지와 고치는 법만 알려 준다.
INVERT_TOLERANCE = 5     # 아래 성부 음역 가운데가 위 성부보다 이만큼(반음) 넘게 높으면 "뒤집힘"
HIGH_BASS_FLOOR = 60     # 저음 성부 가장 낮은 음이 이 이상(C4)이면 저음 구실을 못 함
BASS_FRIENDLY = "기타·콘트라베이스 기타·첼로·건반"


def _role(i, n):
    """n개 성부 중 i번째(0부터)의 역할 — 맨 위 "chord", 맨 아래 "bass", 그 사이 "mid"."""
    return "chord" if i == 0 else ("bass" if i == n - 1 else "mid")


def _role_range(spec, role):
    ins = voice_instrument(spec)
    if role == "mid":                 # _build_mid_part와 같은 음역
        return min(ins["inner"][0], ins["low"][0]), ins["inner"][1]
    return ins[role]


def _center(spec, role):
    lo, hi = _role_range(spec, role)
    return (lo + hi) / 2


def _inverted(specs, i):
    """i+1번째 성부가 i번째보다 확실히 높게 울리나."""
    n = len(specs)
    return _center(specs[i + 1], _role(i + 1, n)) > _center(specs[i], _role(i, n)) + INVERT_TOLERANCE


def _high_bass(specs):
    n = len(specs)
    return n >= 2 and _role_range(specs[-1], "bass")[0] >= HIGH_BASS_FLOOR


def check_voices(specs):
    """성부 지정 목록(위에서부터) → [(수준 "warn"/"info", 안내 글)]. 문제가 없으면 빈 목록."""
    out = []
    n = len(specs)
    label = lambda i: f"{i + 1}성부({voice_instrument(specs[i])['label']})"
    if _high_bass(specs):
        lo = _role_range(specs[-1], "bass")[0]
        out.append(("warn", f"저음 성부 {label(n - 1)}는 저음을 맡기에 너무 높습니다(가장 낮은 음 {_midi_name(lo)}) — "
                            f"맨 아래에는 {BASS_FRIENDLY}을 권합니다."))
    for i in range(n - 1):
        if i + 1 == n - 1 and _high_bass(specs):
            continue                  # 위에서 이미 알림
        if _inverted(specs, i):
            out.append(("warn", f"{label(i + 1)}가 바로 위 {label(i)}보다 높게 울려 성부가 뒤집힙니다 — "
                                "높은 악기를 위 성부로 올리세요('순서 바로잡기')."))
    for i, sp in enumerate(specs):
        key, drop = parse_voice(sp)
        ins = INSTRUMENTS[key]
        if key == "contrabass_guitar" and (n == 1 or i < n - 1):
            out.append(("info", f"{label(i)}: 콘트라베이스 기타는 저음 성부(맨 아래)에서 제 소리가 납니다 — "
                                "위·가운데 성부에서는 이 악기치고 높은 음을 냅니다."))
        if drop and (n == 1 or i < n - 1):
            out.append(("info", f"{label(i)}: 드롭 D는 저음 성부에서만 6번 줄 D를 씁니다 — "
                                "이 성부에서는 악보에 ⑥=D만 붙고 소리는 거의 같습니다."))
        o = parse_voice_opts(sp)[1]
        notes = o["notes"]
        bass = n >= 2 and i == n - 1
        if bass and (notes > BASS_MAX_STACK or o["fastest"] != DEFAULT_FASTEST):
            out.append(("info", f"{label(i)}: 저음 성부는 화음 음 수({BASS_MAX_STACK}음까지)·가장 빠른 음표를 "
                                "한계로만 씁니다 — 저음은 더 적은 음·느린 음표로 받칠 수 있습니다."))
        if not bass and notes >= 5:
            lo, hi = _role_range(sp, _role(i, n))
            if hi - lo < 24:            # 5~6음은 두 옥타브까지 벌려 쌓는다(voice_lead._stack)
                out.append(("info", f"{label(i)}: 음역이 좁아 화음 {notes}음을 다 못 쌓는 곳은 더 적은 음으로 나옵니다."))
        if key == "keyboard" and not any(0 <= j < n and parse_voice(specs[j])[0] == "keyboard" for j in (i - 1, i + 1)):
            out.append(("info", f"{label(i)}: 건반은 오른손(높은음자리표)·왼손(낮은음자리표) 두 성부로 나옵니다."))
        if i == 0 and ins.get("mono"):
            out.append(("info", f"{label(i)}: 한 번에 한 음만 내는 악기라 화음이 분산화음(한 음씩)으로 나옵니다."))
    return out


def fix_voice_order(specs):
    """뒤집힌 이웃 성부만 맞바꿔 높은 악기가 위로 오게 한 목록(뒤집힘이 없으면 그대로). 저음 성부가 너무 높은
    악기면 가장 낮게 울리는 성부와 맞바꾼다."""
    specs = list(specs)
    n = len(specs)
    if _high_bass(specs):
        best = min(range(n), key=lambda i: _role_range(specs[i], "bass")[0])
        specs[best], specs[-1] = specs[-1], specs[best]
    for _ in range(n * n):
        swapped = False
        for i in range(n - 1):
            if _inverted(specs, i):
                specs[i], specs[i + 1] = specs[i + 1], specs[i]
                swapped = True
        if not swapped:
            break
    return specs


def _midi_name(m):
    return ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B"][m % 12] + str(m // 12 - 1)


def _mark_drop_d(part):
    """첫 마디 attributes 뒤에 "⑥=D"(6번 줄을 D로) 글자."""
    me = part.find("measure")
    d = ET.Element("direction", {"placement": "below"})
    _sub(_sub(d, "direction-type"), "words", "⑥=D")
    me.insert(list(me).index(me.find("attributes")) + 1, d)

DIVISIONS = 4   # 새 성부의 divisions(4분음표 = 4) — 16분음표까지 표현
# 길이(divisions) → (type, 점 개수), 큰 것부터
_PIECES = [(16, "whole", 0), (12, "half", 1), (8, "half", 0), (6, "quarter", 1), (4, "quarter", 0),
           (3, "eighth", 1), (2, "eighth", 0), (1, "16th", 0)]


def line_name(n):
    """5도권 위치 → (음이름, 변화표 수: 샤프 +, 플랫 -)."""
    return _LETTERS[(n + 1) % 7], (n + 1) // 7


def line_pc(n):
    return (n * 7) % 12


def _note_label(n):
    letter, alt = line_name(n)
    return letter + ("#" * alt if alt > 0 else "b" * -alt)


class Chord:
    """후보 화음 — roman: 로마 숫자, degree: 으뜸음 기준 도수(0~6), lines: 구성음의 5도권 위치(근음부터)."""

    def __init__(self, roman, degree, lines, quality):
        self.roman, self.degree, self.lines, self.quality = roman, degree, lines, quality
        self.is_v7 = quality == "dominant"

    @property
    def root(self):
        return self.lines[0]

    @property
    def pcs(self):
        return [line_pc(n) for n in self.lines]

    def name(self):
        suffix = {"major": "", "minor": "m", "diminished": "dim", "dominant": "7"}[self.quality]
        return _note_label(self.root) + suffix


def candidate_chords(tonic, minor):
    """조의 후보 화음 — 으뜸음 5도권 위치 tonic, 단조 여부."""
    sc = list(_MINOR_SCALE if minor else _MAJOR_SCALE)
    sc_lead = sc[:6] + [_LEADING] if minor else sc    # 단조 V·vii°에 쓰는 올린 7음

    def triad(d, scale, roman, quality):
        lines = [tonic + scale[(d + k) % 7] for k in (0, 2, 4)]
        return Chord(roman, d, lines, quality)

    if minor:
        chords = [triad(0, sc, "i", "minor"), triad(1, sc, "ii°", "diminished"), triad(2, sc, "III", "major"),
                  triad(3, sc, "iv", "minor"), triad(4, sc_lead, "V", "major"), triad(5, sc, "VI", "major"),
                  triad(6, sc, "VII", "major"), triad(6, sc_lead, "vii°", "diminished")]
    else:
        chords = [triad(0, sc, "I", "major"), triad(1, sc, "ii", "minor"), triad(2, sc, "iii", "minor"),
                  triad(3, sc, "IV", "major"), triad(4, sc, "V", "major"), triad(5, sc, "vi", "minor"),
                  triad(6, sc, "vii°", "diminished")]
    v = next(c for c in chords if c.degree == 4)
    v7_lines = v.lines + [tonic + sc[3]]    # 딸림7화음: 딸림 3화음 + 4도음
    chords.append(Chord("V7", 4, v7_lines, "dominant"))
    return chords


def scale_pcs(tonic, minor):
    """조에서 '화성 안 음'으로 보는 음이름(0~11) 집합 — 단조는 올린 7음 포함."""
    sc = _MINOR_SCALE if minor else _MAJOR_SCALE
    s = {line_pc(tonic + n) for n in sc}
    if minor:
        s.add(line_pc(tonic + _LEADING))
    return s


# ---------------------------------------------------------------- 멜로디 읽기

class Melody:
    def __init__(self):
        self.notes = []        # (시작 q, 길이 q, 소리 높이 midi) — 곡 처음부터의 4분음표 수
        self.measures = []     # {"el": 마디 요소, "number", "start", "length", "beats", "beat_type", "time_el"}
        self.fifths = 0
        self.mode = None       # "major" / "minor" / None
        self.chromatic = 0     # 이조 악기면 소리 높이 = 적힌 높이 + chromatic
        self.first_attributes = None


def _step_midi(pitch_el):
    step = pitch_el.findtext("step")
    alter = float(pitch_el.findtext("alter") or 0)
    octave = int(pitch_el.findtext("octave"))
    return 12 * (octave + 1) + _LETTER_PC[step] + int(round(alter))


def read_melody(part):
    mel = Melody()
    div, beats, beat_type = 1, 4, 4
    cursor_q = 0.0
    shift = 0
    for m in part.findall("measure"):
        attrs = m.find("attributes")
        time_el = None
        if attrs is not None:
            d = attrs.findtext("divisions")
            if d:
                div = max(1, int(float(d)))
            key = attrs.find("key")
            if key is not None and mel.first_attributes is None:
                mel.fifths = int(float(key.findtext("fifths") or 0))
                mode = (key.findtext("mode") or "").strip().lower()
                mel.mode = "minor" if mode == "minor" else ("major" if mode == "major" else None)
            t = attrs.find("time")
            if t is not None:
                beats = int(float((t.findtext("beats") or "4").split("+")[0]))
                beat_type = int(float(t.findtext("beat-type") or 4))
                time_el = t
            tr = attrs.find("transpose")
            if tr is not None:
                mel.chromatic = int(float(tr.findtext("chromatic") or 0))
                shift = mel.chromatic + 12 * int(float(tr.findtext("octave-change") or 0))
            if mel.first_attributes is None:
                mel.first_attributes = attrs
        pos = 0.0          # 마디 안 위치(division)
        end = 0.0
        last_start = 0.0
        harms = []         # 이 마디에 이미 적혀 있는 코드 이름 [(마디 안 위치 q, Chord)]
        for el in m:
            if el.tag == "harmony":
                ch = _chord_from_harmony(el)
                if ch is not None:     # <offset>: 지금 위치에서 떨어진 거리(division) — 음표가 없는 자리의 코드
                    harms.append(((pos + float(el.findtext("offset") or 0)) / div, ch))
            elif el.tag == "note":
                if el.find("grace") is not None:
                    continue
                dur = float(el.findtext("duration") or 0)
                is_chord = el.find("chord") is not None
                start = last_start if is_chord else pos
                if el.find("rest") is None and el.find("pitch") is not None:
                    tie_stop = any(t.get("type") == "stop" for t in el.findall("tie"))
                    mel.notes.append([cursor_q + start / div, dur / div, _step_midi(el.find("pitch")) + shift, tie_stop])
                if not is_chord:
                    last_start = pos
                    pos += dur
                    end = max(end, pos)
            elif el.tag == "backup":
                pos -= float(el.findtext("duration") or 0)
            elif el.tag == "forward":
                pos += float(el.findtext("duration") or 0)
                end = max(end, pos)
        mel.measures.append({"el": m, "number": m.get("number", str(len(mel.measures) + 1)),
                             "start": cursor_q, "actual": end / div, "implicit": m.get("implicit") == "yes",
                             "beats": beats, "beat_type": beat_type, "time_el": time_el, "harms": harms})
        cursor_q += end / div          # 일단 실제 길이로 — 아래에서 약박 시작(못갖춘마디)만 가려 고쳐 계산
    _fix_lengths(mel)
    # 붙임줄로 이어진 뒷음은 앞 음에 합침(코드 판정을 길이로 가중하니 쪼개져 있어도 같지만 시작 가중이 중복되지 않게)
    merged = []
    for n in mel.notes:
        if n[3] and merged and merged[-1][2] == n[2] and abs(merged[-1][0] + merged[-1][1] - n[0]) < 1e-6:
            merged[-1][1] += n[1]
        else:
            merged.append(n)
    mel.notes = [(s, d, p) for s, d, p, _ in merged]
    return mel


def _fix_lengths(mel):
    """마디 길이 정하기 — 박자표 길이가 기본, 못갖춘마디(첫 마디가 짧고 끝 마디가 나머지)면 실제 길이."""
    ms = mel.measures
    nominal = [m["beats"] * 4.0 / m["beat_type"] for m in ms]
    short_first = bool(ms) and ms[0]["actual"] > 0 and (
        ms[0]["implicit"] or (ms[0]["actual"] < nominal[0] * 0.9 and len(ms) > 1 and
                              abs(ms[0]["actual"] + ms[-1]["actual"] - nominal[0]) < 1e-3))
    cursor = 0.0
    for i, m in enumerate(ms):
        length = m["actual"] if (i == 0 and short_first) else max(nominal[i], m["actual"])
        if m["implicit"] and i > 0:
            length = m["actual"] or nominal[i]
        m["start"], m["length"] = cursor, length
        cursor += length
    # 음 시작 시각도 새 마디 시작 기준으로 다시 맞춤(실제 길이로 누적했던 값 보정)
    old_starts = []
    c = 0.0
    for m in ms:
        old_starts.append(c)
        c += m["actual"]
    if all(abs(o - m["start"]) < 1e-9 for o, m in zip(old_starts, ms)):
        return
    fixed = []
    for n in mel.notes:
        k = max(i for i, o in enumerate(old_starts) if o <= n[0] + 1e-9)
        fixed.append([ms[k]["start"] + (n[0] - old_starts[k])] + n[1:])
    mel.notes = fixed


# ---------------------------------------------------------------- 조 정하기

def _correlation(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else 0.0


def detect_key(mel):
    """(으뜸음 5도권 위치, 단조?) — 조표가 있고 이조 악기가 아니면 장조/나란한 단조 둘 중에서만 고른다."""
    hist = [0.0] * 12
    for s, d, p in mel.notes:
        hist[p % 12] += d
    if not any(hist):
        raise ValueError("멜로디에 음표가 없습니다.")
    last_pc = max(mel.notes, key=lambda n: n[0])[2] % 12
    if mel.chromatic % 12 == 0 and mel.first_attributes is not None:
        major_t = mel.fifths
        cands = []
        if mel.mode != "minor":
            cands.append((major_t, False))
        if mel.mode != "major":
            cands.append((major_t + 3, True))
    else:
        cands = [(t, mn) for t in range(-7, 8) for mn in (False, True)]
    best, best_score = None, -9.0
    for tonic, minor in cands:
        tpc = line_pc(tonic)
        prof = _MINOR_PROFILE if minor else _MAJOR_PROFILE
        rotated = [prof[(i - tpc) % 12] for i in range(12)]
        score = _correlation(hist, rotated) + (0.15 if tpc == last_pc else 0.0)
        # 조표가 없는 후보끼리는 조표 수 적은 쪽(5도권 가까운 쪽) 우선 — 조표 경계(F#/Gb 등)에서 안정적으로
        score -= 0.001 * abs(tonic - (3 if minor else 0))
        if score > best_score:
            best, best_score = (tonic, minor), score
    return best


_LETTER_LINE = {"F": -1, "C": 0, "G": 1, "D": 2, "A": 3, "E": 4, "B": 5}


def _chord_from_harmony(el):
    """MusicXML <harmony> → Chord(근음 + 종류). 종류: dim·half-diminished → 감, minor 계열(minor-seventh·major-minor 포함) → 단,
    dominant 계열(7·9·13) → 딸림7, 나머지 → 장. 근음이 없으면(N.C. 등) None."""
    step = el.findtext("root/root-step")
    if step not in _LETTER_LINE:
        return None
    alt = int(float(el.findtext("root/root-alter") or 0))
    root = _LETTER_LINE[step] + 7 * alt
    kind_el = el.find("kind")
    kind = ((kind_el.text or "") if kind_el is not None else "").strip().lower()
    text = ((kind_el.get("text") or "") if kind_el is not None else "").strip().lower()
    if "diminished" in kind or text.startswith("dim") or text.endswith("°"):
        quality, lines = "diminished", [root, root - 3, root - 6]
    elif kind.startswith("minor") or kind.startswith("major-minor") or (text.startswith("m") and not text.startswith("maj")):
        quality, lines = "minor", [root, root - 3, root + 1]
    elif "dominant" in kind or kind in ("7", "9", "13") or text in ("7", "9", "13"):
        quality, lines = "dominant", [root, root + 4, root + 1, root - 2]
    else:
        quality, lines = "major", [root, root + 4, root + 1]
    return Chord("", 0, lines, quality)


def given_slots(mel):
    """멜로디 성부에 이미 적혀 있는 코드 이름대로 코드 칸을 만든다(없으면 None). 코드 이름이 나온 자리마다 새 칸,
    코드가 안 적힌 마디는 앞 코드를 이어 가고, 맨 앞에 코드 없는 마디는 처음 나오는 코드로 채운다. 위치는 1/4박 단위로 맞춤."""
    allh = [h for m in mel.measures for h in m["harms"]]
    if not allh:
        return None
    cur = allh[0][1]
    slots = []
    for mi, m in enumerate(mel.measures):
        marks = {}
        for off, ch in m["harms"]:
            off = round(off * 4) / 4
            if 0 <= off < m["length"] - 1e-6:
                marks[off] = ch
        if 0.0 not in marks:
            marks[0.0] = cur
        offs = sorted(marks)
        for i, off in enumerate(offs):
            end = offs[i + 1] if i + 1 < len(offs) else m["length"]
            sl = Slot(mi, m["start"] + off, end - off)
            sl.chord = cur = marks[off]
            if sl.length > 1e-6:
                slots.append(sl)
    return _merge_same_chord(slots)


# ---------------------------------------------------------------- 코드 칸 나누기

class Slot:
    def __init__(self, measure, start, length):
        self.measure, self.start, self.length = measure, start, length     # start: 곡 처음부터의 q
        self.chord = None


def make_slots(mel, per_measure=None):
    """마디를 코드 칸으로 — per_measure: 1·2 또는 None(자동: 박 4개 이상이면 2칸)."""
    slots = []
    for mi, m in enumerate(mel.measures):
        beats, bt = m["beats"], m["beat_type"]
        compound = bt == 8 and beats >= 6 and beats % 3 == 0
        beat_q = 1.5 if compound else 4.0 / bt
        n_beats = max(1, int(round(m["length"] / beat_q)))
        pickup = abs(m["length"] - beats * 4.0 / bt) > 1e-6
        n_slots = per_measure or (2 if n_beats >= 4 else 1)
        if pickup or n_beats < 2:
            n_slots = 1
        if n_slots == 2:
            first = (n_beats + 1) // 2
            parts = [first * beat_q, m["length"] - first * beat_q]
        else:
            parts = [m["length"]]
        t = m["start"]
        for L in parts:
            if L > 1e-6:
                slots.append(Slot(mi, t, L))
                t += L
    return slots


# ---------------------------------------------------------------- 코드 고르기

def _emission(chord, slot, notes, scale):
    """칸 안 멜로디가 이 코드에 얼마나 맞는지(-1~1). 멜로디가 없으면 0."""
    tones = set(chord.pcs[:3])
    seventh = {chord.pcs[3]} if len(chord.pcs) > 3 else set()
    total = score = 0.0
    end = slot.start + slot.length
    for s, d, p in notes:
        ov = min(s + d, end) - max(s, slot.start)
        if ov <= 1e-6:
            continue
        w = ov * (1.5 if slot.start - 1e-6 <= s < slot.start + 1e-6 or abs(s - round(s)) < 1e-6 and s >= slot.start else 1.0)
        if d <= 0.25:
            w *= 0.5
        pc = p % 12
        if pc in tones:
            v = 1.0
        elif pc in seventh:
            v = 0.6
        elif pc in scale:
            v = -0.3
        else:
            v = -0.8
        total += w
        score += w * v
    return score / total if total else 0.0


def choose_chords(slots, notes, tonic, minor, same_measure_penalty=0.0):
    """Viterbi — 칸마다 화음을 정해 slot.chord에 넣는다."""
    chords = candidate_chords(tonic, minor)
    scale = scale_pcs(tonic, minor)
    n = len(slots)
    first_full = next((i for i, s in enumerate(slots) if s.length >= 2 - 1e-6), 0)
    emit = []
    for i, s in enumerate(slots):
        row = []
        for c in chords:
            e = _EMISSION_WEIGHT * _emission(c, s, notes, scale)
            if c.is_v7:
                e -= _V7_PENALTY
            if i == first_full and c.degree == 0:
                e += _START_TONIC
            if i == n - 1 and c.degree == 0:
                e += _CADENCE_TONIC
            if i == n - 2 and i > 0:
                e += _CADENCE_APPROACH.get(c.degree, 0.0)
            row.append(e)
        emit.append(row)
    k = len(chords)
    best = [emit[0][:]]
    back = []
    for i in range(1, n):
        row, brow = [], []
        for j, c in enumerate(chords):
            cand = []
            for h, p in enumerate(chords):
                t = _TRANSITION[p.degree][c.degree]
                if p.is_v7 and c.degree == 0:
                    t += 0.3
                if same_measure_penalty and slots[i - 1].measure == slots[i].measure and p.name() != c.name():
                    t -= same_measure_penalty        # 한 마디 안에서 코드가 바뀌는 건 멜로디가 분명히 원할 때만
                if p is not c and p.degree == c.degree:
                    t = -0.2 if p.is_v7 != c.is_v7 else -0.5     # V ↔ V7 는 사실상 같은 화음
                cand.append(best[i - 1][h] + t)
            h = max(range(k), key=cand.__getitem__)
            row.append(cand[h] + emit[i][j])
            brow.append(h)
        best.append(row)
        back.append(brow)
    j = max(range(k), key=best[-1].__getitem__)
    seq = [j]
    for brow in reversed(back):
        j = brow[j]
        seq.append(j)
    seq.reverse()
    for s, j in zip(slots, seq):
        s.chord = chords[j]
    return chords


# ---------------------------------------------------------------- 성부 진행(voice_lead) 준비

def _voice_specs(voices, style, mel):
    """성부 지정 목록 → (voice_lead.VoiceSpec 목록, 성부별 악기 정보). 역할: 맨 위 화음(top)·맨 아래 저음(bass)·
    그 사이 가운데(mid). 음역은 역할·방식별(동요식은 inner/low), 쌓는 음 수는 정한 화음 음 수(모든 성부, 화음
    악기만) — 자동이면 블록 방식 1성부만 3음. 저음 성부는 화음 음 수·가장 빠른 음표를 한계로만 쓴다.
    멜로디 가운데 음보다 음역 바닥이 높은 악기는 멜로디 위가 기본."""
    n = len(voices)
    pitches = sorted(p for _, _, p in mel.notes)
    mel_mid = pitches[len(pitches) // 2] if pitches else 67
    specs, inss = [], []
    for k, spec in enumerate(voices):
        ins = voice_instrument(spec)
        role = "top" if k == 0 else ("bass" if k == n - 1 else "mid")
        if role == "top":
            rng = ins["inner"] if style == "simple" else ins["chord"]
        elif role == "bass":
            rng = ins["low"] if style == "simple" else ins["bass"]
        elif ins.get("hand") == "lh":
            rng = ins["low"]       # 건반 왼손 가운데 성부 — 낮은 음역(오른손은 아래 일반 가운데 성부 음역: 좁히면
                                   # 화음을 쌓을 자리 때문에 위 성부가 멜로디 쪽으로 밀려 병행 8도가 났음)
        else:
            rng = (min(ins["inner"][0], ins["low"][0]), ins["inner"][1])
        mono = bool(ins.get("mono"))
        unit = ins["unit"] / DIVISIONS if ins.get("unit") else None
        unit_cap = None
        if mono:
            stack = 1
        elif ins.get("max_notes"):
            stack = ins["max_notes"]
        else:
            stack = 3 if style == "block" and role == "top" else 1
        if role == "bass" and n >= 2:
            # 저음은 특수한 경우 — 정한 값을 꼭 지키지 않고 한계로만(사용자 요청 2026-10-27): 낮은 화음은 탁하니
            # BASS_MAX_STACK음까지, 가장 빠른 음표는 "이보다 빠르지 않게"만 두고 돌려 맡기기에 그대로 낀다.
            stack = min(stack, BASS_MAX_STACK)
            unit, unit_cap = None, unit
        specs.append(voice_lead.VoiceSpec(
            lo=rng[0], hi=rng[1], role=role, mono=mono, stack=stack, unit=unit, unit_cap=unit_cap,
            flex_stack=bool(ins.get("max_notes")) and not mono,
            drop_root=ins["lowest"] - 2 if ins.get("dropped") else None,
            stack_hi=ins["chord"][1], above=rng[0] > mel_mid))
        inss.append(ins)
    return specs, inss


# ---------------------------------------------------------------- MusicXML 쓰기

def _sub(parent, tag, text=None, **attrs):
    e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        e.text = str(text)
    return e


def _spell(midi, chord_lines):
    """chord_lines 중 이 음높이 반음 값을 가진 5도권 위치로 철자(조에 맞게) → (step, alter, octave).
    목록에 없는 음이면 올림표 쪽 철자."""
    n = next((x for x in chord_lines if line_pc(x) == midi % 12), None)
    if n is None:
        n = next(x for x in range(-1, 11) if line_pc(x) == midi % 12)
    step, alt = line_name(n)
    return step, alt, (midi - alt) // 12 - 1


def _pieces(div):
    out = []
    while div > 0:
        p = next(x for x in _PIECES if x[0] <= div)
        out.append(p)
        div -= p[0]
    return out


def _add_notes(measure, tones, lines, length_div, voice=1, staff_chord=True):
    """한 시간 구간에 tones(midi 목록, 동시에 울림)를 길이 length_div로 쓴다 — 표현 안 되는 길이는 붙임줄로 이음."""
    pieces = _pieces(length_div)
    for i, (dv, typ, dots) in enumerate(pieces):
        for k, midi in enumerate(tones):
            note = _sub(measure, "note")
            if k:
                _sub(note, "chord")
            step, alt, octave = _spell(midi, lines)
            p = _sub(note, "pitch")
            _sub(p, "step", step)
            if alt:
                _sub(p, "alter", alt)
            _sub(p, "octave", octave)
            _sub(note, "duration", dv)
            if i > 0:
                _sub(note, "tie", type="stop")
            if i < len(pieces) - 1:
                _sub(note, "tie", type="start")
            _sub(note, "voice", voice)
            _sub(note, "type", typ)
            for _ in range(dots):
                _sub(note, "dot")
            if len(pieces) > 1:
                nt = _sub(note, "notations")
                if i > 0:
                    _sub(nt, "tied", type="stop")
                if i < len(pieces) - 1:
                    _sub(nt, "tied", type="start")


def _add_rest(measure, length_div):
    """멜로디만 두는 마디 — 반주 성부는 마디 전체 쉼표(<rest measure="yes"/> — Verovio가 온마디 쉼표(mRest)로 읽어야
    그 줄에서 빈 보표를 숨길 수 있음)."""
    note = _sub(measure, "note")
    _sub(note, "rest", measure="yes")
    _sub(note, "duration", length_div)
    _sub(note, "voice", 1)


def _chord_symbol(measure, chord):
    return _fill_chord_symbol(_sub(measure, "harmony"), chord)


def _fill_chord_symbol(h, chord):
    root = _sub(h, "root")
    letter, alt = line_name(chord.root)
    _sub(root, "root-step", letter)
    if alt:
        _sub(root, "root-alter", alt)
    text = {"major": "", "minor": "m", "diminished": "dim", "dominant": "7"}[chord.quality]
    kind = _sub(h, "kind", chord.quality)
    kind.set("text", text)
    return h


def _first_attributes(measure, fifths, minor, clef, time_el, transpose_octave=0):
    """clef: (기호, 줄, 옥타브 변화) — 기타는 높은음자리표 8vb(옥타브 변화 -1).
    transpose_octave: 0이 아니면 <transpose octave-change>(콘트라베이스 기타 -1, INSTRUMENTS 설명 참고)."""
    a = _sub(measure, "attributes")
    _sub(a, "divisions", DIVISIONS)
    k = _sub(a, "key")
    _sub(k, "fifths", fifths)
    _sub(k, "mode", "minor" if minor else "major")
    if time_el is not None:
        a.append(copy.deepcopy(time_el))
    c = _sub(a, "clef")
    _sub(c, "sign", clef[0])
    _sub(c, "line", clef[1])
    if len(clef) > 2 and clef[2]:
        _sub(c, "clef-octave-change", clef[2])
    if transpose_octave:
        t = _sub(a, "transpose")
        _sub(t, "diatonic", 0)
        _sub(t, "chromatic", 0)
        _sub(t, "octave-change", transpose_octave)


def _write_voice_part(pid, mel, slots, events, tonic, minor, clef, transpose_octave, chord_symbols, scale_lines):
    """voice_lead.plan_motion이 정한 한 성부(칸마다 [(길이 q, [midi …])])를 마디별 <part>로. 칸이 없는 마디(전주)는
    온마디 쉼표, 멜로디와 같은 마디 번호·박자표 바뀜·도돌이 막대선."""
    part = ET.Element("part", {"id": pid})
    fifths = tonic - 3 if minor else tonic
    by_measure = {}
    for k, sl in enumerate(slots):
        by_measure.setdefault(sl.measure, []).append(k)
    for mi, m in enumerate(mel.measures):
        me = ET.SubElement(part, "measure", {"number": m["number"]})
        if m["implicit"]:
            me.set("implicit", "yes")
        if mi == 0:
            _first_attributes(me, fifths, minor, clef, m["time_el"], transpose_octave)
        elif m["time_el"] is not None:
            _sub(me, "attributes").append(copy.deepcopy(m["time_el"]))
        if not by_measure.get(mi):
            _add_rest(me, int(round(m["length"] * DIVISIONS)))
        for k in by_measure.get(mi, []):
            sl = slots[k]
            if chord_symbols:
                _chord_symbol(me, sl.chord)
            lines = list(sl.chord.lines) + scale_lines     # 경과음·이웃음은 음계 철자로
            for dur_q, notes in events[k]:
                _add_notes(me, sorted(notes), lines, int(round(dur_q * DIVISIONS)))
        for bl in m["el"].findall("barline"):
            me.append(copy.deepcopy(bl))
    return part


def is_simple(mel):
    """동요처럼 단순한 곡인지 — 음이 짧지 않고(16분음표 거의 없음) 음역이 한 옥타브 반 안, 쓰인 음이름 8개 이하."""
    ns = mel.notes
    if not ns:
        return False
    fast = sum(1 for _, d, _ in ns if d < 0.5 - 1e-6)
    pitches = [p for _, _, p in ns]
    return fast <= 0.05 * len(ns) and max(pitches) - min(pitches) <= 18 and len({p % 12 for p in pitches}) <= 8


SIMPLE_CHANGE_PENALTY = 1.2    # 동요에서 한 마디 안 코드 바꿈에 주는 감점


def _merge_same_chord(slots):
    """같은 마디에서 이어진 같은 코드 칸을 하나로 합친다."""
    out = []
    for sl in slots:
        if out and out[-1].measure == sl.measure and out[-1].chord.name() == sl.chord.name():
            out[-1].length += sl.length
        else:
            out.append(sl)
    return out




def _add_score_part(root, pid, name, abbrev, program, instrument_name):
    pl = root.find("part-list")
    sp = ET.SubElement(pl, "score-part", {"id": pid})
    _sub(sp, "part-name", name)
    _sub(sp, "part-abbreviation", abbrev)
    si = _sub(sp, "score-instrument", id=f"{pid}-I1")
    _sub(si, "instrument-name", instrument_name)
    mi = _sub(sp, "midi-instrument", id=f"{pid}-I1")
    _sub(mi, "midi-channel", 1)
    _sub(mi, "midi-program", program)


_CHORDS_FROM = "GenerateScore-chords-from"
MELODY_ONLY_FIELD = "GenerateScore-melody-only-measures"     # 처음 N마디는 전주(멜로디만·색 없음)
COLOR_ALL_FIELD = "GenerateScore-color-all"                  # 재생 커서와 상관없이 처음부터 색을 칠함(멜로디에 색깔 넣기)
SEED_FIELD = "GenerateScore-harmonize-seed"                  # 반주를 만든 무작위 씨앗(같은 반주를 다시 만들 때)
RETRY_SEEDS = 5          # 병행이 남으면 다음 씨앗으로 다시 해 보는 횟수
PRELUDE_FIELD = "GenerateScore-color-prelude-measures"       # 색깔 넣기의 전주 마디 수 — 색만 빼고 성부는 그대로 보임


def prelude_measures(root):
    """색을 칠하지 않는 앞 마디 수 — 화성 붙이기의 "멜로디만" 마디(MELODY_ONLY_FIELD)나 색깔 넣기의 전주(PRELUDE_FIELD)."""
    return max(_int_field(root, MELODY_ONLY_FIELD), _int_field(root, PRELUDE_FIELD))


def has_prelude_field(root):
    return has_field(root, MELODY_ONLY_FIELD) or has_field(root, PRELUDE_FIELD)


def _int_field(root, name):
    for f in root.iter("miscellaneous-field"):
        if f.get("name") == name:
            try:
                return max(0, int((f.text or "0").strip()))
            except ValueError:
                return 0
    return 0


def has_field(root, name):
    return any(f.get("name") == name for f in root.iter("miscellaneous-field"))


def _mark_chords_from(root, part_id):
    """코드 이름을 새로 만들지 않고 멜로디 성부(part_id)의 것을 썼다는 표시 — 색칠이 그 코드를 읽도록(MusicXML
    <identification><miscellaneous>의 사용자 필드)."""
    _add_misc_field(root, _CHORDS_FROM, part_id)


def _add_misc_field(root, name, value):
    ident = root.find("identification")
    if ident is None:
        ident = ET.Element("identification")
        anchor = next((i for i, c in enumerate(root) if c.tag in ("defaults", "credit", "part-list")), len(root))
        root.insert(anchor, ident)
    misc = ident.find("miscellaneous")
    if misc is None:
        misc = ET.SubElement(ident, "miscellaneous")
    for old in [f for f in misc.findall("miscellaneous-field") if f.get("name") == name]:
        misc.remove(old)                     # 다시 만들 때 같은 필드가 겹치지 않게
    f = ET.SubElement(misc, "miscellaneous-field", {"name": name})
    f.text = value


def _remove_accompaniment(root):
    """앞서 붙인 반주 성부(id가 Harm으로 시작)와 그 표시 필드를 뺀다 — 화성 붙인 악보에서 다시 만들 때 반주가 쌓이지 않게.
    멜로디 성부들은 그대로(반주는 늘 맨 뒤에 붙으므로 멜로디 성부 번호도 그대로)."""
    pl = root.find("part-list")
    old = [p for p in root.findall("part") if (p.get("id") or "").startswith("Harm")]
    if not old:
        return
    for p in old:
        root.remove(p)
        if pl is not None:
            for sp in [sp for sp in pl.findall("score-part") if sp.get("id") == p.get("id")]:
                pl.remove(sp)
    for misc in list(root.iter("miscellaneous")):
        for f in [f for f in misc.findall("miscellaneous-field")
                  if f.get("name") in (MELODY_ONLY_FIELD, SEED_FIELD, _CHORDS_FROM)]:
            misc.remove(f)


def harmonize_root(root, melody_part=1, style="auto", bass=True, per_measure=None, chord_symbols=True,
                   use_chords=True, instrument="guitar", skip_measures=0, voices=None, seed=None):
    """MusicXML 루트(score-partwise)에 반주 성부를 더한다(복사본을 돌려줌).
    voices: 반주 성부별 악기(INSTRUMENTS 키) 목록, 위에서부터 1~MAX_VOICES개 — 1성부는 화음(코드 이름이 붙음),
    2개 이상이면 맨 아래는 저음, 그 사이는 가운데 성부. None이면 [instrument, instrument](bass=False면 [instrument]).

    seed: 성부 진행의 무작위 씨앗 — None이면 새로 뽑아 같은 조건에서도 만들 때마다 다른 반주(사용자 요청 2026-10-27),
    정수면 그 반주를 그대로 다시 만든다. 쓴 씨앗은 정보와 악보(SEED_FIELD)에 남긴다.
    이미 화성을 붙인 악보(Harm… 성부)면 앞 반주 성부를 빼고 새로 붙인다.

    반환: (새 루트, 정보 dict{"key": 조 이름, "chords": [(마디 번호, 코드 이름, 로마 숫자)], "melody_name": 이름, "seed"})"""
    if root.tag.endswith("score-timewise"):
        raise ValueError("score-timewise 형식은 지원하지 않습니다(악보 프로그램에서 partwise로 내보내기).")
    root = copy.deepcopy(root)
    _remove_accompaniment(root)
    if seed is None:
        seed = random.randrange(1 << 30)
    parts = root.findall("part")
    if not 1 <= melody_part <= len(parts):
        raise ValueError(f"멜로디 성부 번호는 1~{len(parts)} 사이여야 합니다.")
    part = parts[melody_part - 1]
    mel = read_melody(part)
    tonic, minor = detect_key(mel)
    if voices is None:
        voices = [instrument, instrument] if bass else [instrument]
    voices = list(voices)
    if not 1 <= len(voices) <= MAX_VOICES:
        raise ValueError(f"반주 성부는 1~{MAX_VOICES}개여야 합니다.")
    for spec in voices:
        parse_voice(spec)       # 모르는 악기·안 되는 드롭 D면 여기서 ValueError
    voices = expand_hands(voices)       # 건반은 오른손·왼손 둘 다
    instrument = parse_voice(voices[0])[0]
    if style == "auto":
        style = "simple" if is_simple(mel) else ("block" if instrument == "keyboard" else "arpeggio")
    given = given_slots(mel) if use_chords else None
    skip = max(0, int(skip_measures))      # 처음 skip마디는 멜로디만(반주 성부는 쉼표, 코드 색도 없음)
    if given:
        slots = [sl for sl in given if sl.measure >= skip]   # 악보에 코드 이름이 이미 있으면 그대로 씀(새로 정하지 않음)
        chord_symbols = False     # 같은 글자를 반주 성부 위에 또 달지 않음
    else:
        slots = [sl for sl in make_slots(mel, per_measure) if sl.measure >= skip]
        if not slots:
            raise ValueError("화성을 붙일 마디가 없습니다(처음 멜로디만 두는 마디 수가 곡 길이 이상입니다).")
        # 동요(simple)는 마디당 코드 하나가 기본, 마디 안에서 멜로디가 확실히 바뀔 때만 둘로(같은 코드면 다시 합침)
        choose_chords(slots, mel.notes, tonic, minor, SIMPLE_CHANGE_PENALTY if style == "simple" else 0.0)
        if style == "simple":
            slots = _merge_same_chord(slots)

    used = {p.get("id") for p in parts}
    # 성부 id — 1성부 HarmGuitar(read_chord_map·song_layout_mode가 이 id로 화성 악보를 알아봄), 맨 아래 HarmBass
    n_mid = max(0, len(voices) - 2)
    ids = ["HarmGuitar"] + [f"HarmMid{j + 1}" for j in range(n_mid)] + (["HarmBass"] if len(voices) > 1 else [])
    while any(i in used for i in ids):
        ids = [i + "_" for i in ids]
    if root.find("part-list") is None:
        raise ValueError("part-list가 없는 악보입니다.")

    # 성부 진행 — 화성법으로 함께 고른 뼈대에 대위법으로 움직임을 고르게 나눈다(voice_lead)
    specs, inss = _voice_specs(voices, style, mel)
    harmonies = [voice_lead.Harmony(sl.chord, line_pc) for sl in slots]
    # 무작위라 드물게 뼈대에 병행이 남으면 다음 씨앗으로 몇 번 더 — 가장 적은 것(쓴 씨앗을 남김)
    best = None
    for s in range(seed, seed + RETRY_SEEDS):
        rng = random.Random(s)
        skeleton = voice_lead.plan_skeleton(slots, harmonies, mel.notes, specs, line_pc(tonic),
                                            line_pc(tonic + _LEADING), rng)
        p = voice_lead.plan_motion(slots, harmonies, skeleton, mel.notes, mel.measures, specs, style,
                                   scale_pcs(tonic, minor), rng)
        bad = voice_lead.count_parallels(p, slots, mel.notes)
        if best is None or bad < best[0]:
            best = (bad, s, p)
        if not bad:
            break
    _bad, seed, plan = best
    scale_lines = [tonic + d for d in (_MINOR_SCALE if minor else _MAJOR_SCALE)] + ([tonic + _LEADING] if minor else [])
    part_names = voice_part_names(voices)
    for k, (pid, key) in enumerate(zip(ids, voices)):
        p_ins = inss[k]
        name = abbrev = part_names[k]
        lh = p_ins["hand"] == "lh" if p_ins.get("hand") else specs[k].role == "bass"
        clef = p_ins["low_clef"] if lh else p_ins["high_clef"]
        part_el = _write_voice_part(pid, mel, slots, plan[k], tonic, minor, clef, p_ins.get("transpose_octave", 0),
                                    chord_symbols and k == 0, scale_lines)
        if p_ins.get("dropped"):
            _mark_drop_d(part_el)
        _add_score_part(root, pid, name, abbrev, p_ins["program"], p_ins["midi_name"])
        root.append(part_el)

    if skip:
        _add_misc_field(root, MELODY_ONLY_FIELD, str(skip))
    if given:
        _mark_chords_from(root, part.get("id"))
    _add_misc_field(root, SEED_FIELD, str(seed))
    pl = root.find("part-list")
    names = {sp.get("id"): (sp.findtext("part-name") or "").strip() for sp in pl.findall("score-part")}
    key_name = _note_label(tonic) + (" 단조" if minor else " 장조")
    info = {"key": key_name, "style": style, "instrument": instrument, "voices": voices, "chords_given": bool(given),
            "skip_measures": skip, "seed": seed,
            "chords": [(mel.measures[s.measure]["number"], s.chord.name(), s.chord.roman) for s in slots],
            "melody_name": names.get(part.get("id"), "")}
    return root, info


def _insert_chord_symbols(measure, marks):
    """마디에 코드 이름 marks=[(마디 안 위치 division, Chord)]을 끼운다 — 그 위치에서 시작하는 음표(쉼표 포함, 어느
    목소리든)가 있으면 그 앞에, 없으면(온마디 쉼표·긴 음 중간 — 콜 니드라이 첫 성부, 2026-10-27 사용자 지적: 코드가
    음표와 틀어짐 — 다음 음표 앞이나 마디 끝에 붙었음) 마디 첫 음표 앞에 `<offset>`(그 위치까지 거리)을 붙여 끼운다."""
    for off, chord in sorted(marks, key=lambda x: -x[0]):          # 뒤에서부터 끼워야 앞 위치가 안 밀림
        pos, idx_at, idx_first = 0.0, None, None
        for i, el in enumerate(list(measure)):
            if el.tag == "note":
                if el.find("grace") is not None or el.find("chord") is not None:
                    continue
                if idx_first is None:
                    idx_first = i
                if idx_at is None and abs(pos - off) < 1e-6:
                    idx_at = i
                pos += float(el.findtext("duration") or 0)
            elif el.tag == "backup":
                pos -= float(el.findtext("duration") or 0)
            elif el.tag == "forward":
                pos += float(el.findtext("duration") or 0)
        h = _fill_chord_symbol(ET.Element("harmony"), chord)
        if idx_at is not None:
            measure.insert(idx_at, h)
        elif idx_first is not None:
            _sub(h, "offset", int(round(off)) if abs(off - round(off)) < 1e-6 else off)
            measure.insert(idx_first, h)
        else:
            measure.append(h)


def color_melody_root(root, melody_part=1, skip_measures=0, use_chords=True):
    """"멜로디에 색깔 넣기" — 성부를 더하지 않고(단선율 그대로) 멜로디 성부 위에 코드 이름을 달고, 앱이 처음부터 코드별 색을
    칠하도록 표시한다. 악보에 코드 이름이 이미 있으면 그대로 쓰고, 없으면 자동으로 정해 멜로디 성부에 `<harmony>`로 끼운다.
    처음 skip_measures마디는 전주(코드·색 없음). 반환: (새 루트, 정보 dict — harmonize_root와 같은 키).

    melody_part=None — "코드별 색깔"(사용자 요청 2026-10-27): 단선율이든 화음(여러 성부·한 성부 안 화음)이든 악보 전체로.
    어느 성부에든 코드 이름이 적혀 있으면 그 성부의 코드를 따르고, 없으면 모든 성부의 음으로 코드를 정해 맨 위 성부
    (오선 위)에 코드 이름을 단다."""
    if root.tag.endswith("score-timewise"):
        raise ValueError("score-timewise 형식은 지원하지 않습니다(악보 프로그램에서 partwise로 내보내기).")
    root = copy.deepcopy(root)
    parts = root.findall("part")
    if melody_part is None:
        if not parts:
            raise ValueError("성부가 없는 악보입니다.")
        reads = [read_melody(p) for p in parts]
        k = next((i for i, m in enumerate(reads) if any(mm["harms"] for mm in m.measures)), 0)
        part, mel = parts[k], reads[k]
        allnotes = sorted(n for r in reads for n in r.notes)        # 코드 정하기·조 정하기는 모든 성부 음으로
        # 화음 악보는 저음이 코드를 크게 좌우 — 시작 시각마다 가장 낮은 음을 한 번 더 셈(Adagio 3마디 C·E·G가 Em으로 나왔음)
        lowest = {}
        for n in allnotes:
            key = round(n[0], 6)
            if key not in lowest or n[2] < lowest[key][2]:
                lowest[key] = n
        mel.notes = sorted(allnotes + list(lowest.values()))
    else:
        if not 1 <= melody_part <= len(parts):
            raise ValueError(f"멜로디 성부 번호는 1~{len(parts)} 사이여야 합니다.")
        part = parts[melody_part - 1]
        mel = read_melody(part)
    tonic, minor = detect_key(mel)
    skip = max(0, int(skip_measures))
    given = given_slots(mel) if use_chords else None
    if given:
        slots = [sl for sl in given if sl.measure >= skip]
    else:
        slots = [sl for sl in make_slots(mel, None) if sl.measure >= skip]
        if not slots:
            raise ValueError("색을 칠할 마디가 없습니다(전주 마디 수가 곡 길이 이상입니다).")
        choose_chords(slots, mel.notes, tonic, minor, SIMPLE_CHANGE_PENALTY if is_simple(mel) else 0.0)
        slots = _merge_same_chord(slots)
        by_measure = {}
        for sl in slots:
            by_measure.setdefault(sl.measure, []).append(sl)
        measures = part.findall("measure")
        # 멜로디 성부의 divisions는 마디마다 바뀔 수 있어 읽은 마디 정보로 환산
        div = 1.0
        for mi, m_el in enumerate(measures):
            d = m_el.findtext("attributes/divisions")
            if d:
                div = float(d)
            marks = [((sl.start - mel.measures[mi]["start"]) * div, sl.chord) for sl in by_measure.get(mi, [])]
            if marks:
                _insert_chord_symbols(m_el, marks)
    _add_misc_field(root, _CHORDS_FROM, part.get("id"))
    _add_misc_field(root, COLOR_ALL_FIELD, "1")
    # 전주 마디 수는 0이어도 적어 둔다 — 사용자가 정한 값이 가사 기준 자동 전주 판정보다 우선하도록(앱이 이 필드가 있으면 가사 판정 안 함).
    # MELODY_ONLY_FIELD는 조판이 앞 마디를 멜로디만으로 떼어 내므로(score_view) 화음 악보에는 못 씀 —
    # 모든 성부 색깔은 PRELUDE_FIELD(색만 뺌, 성부는 그대로 보임) — 예전에 남은 "멜로디만" 값은 지움
    if melody_part is None:
        for misc in root.iter("miscellaneous"):
            for f in [f for f in misc.findall("miscellaneous-field") if f.get("name") == MELODY_ONLY_FIELD]:
                misc.remove(f)
    _add_misc_field(root, MELODY_ONLY_FIELD if melody_part is not None else PRELUDE_FIELD, str(skip))
    key_name = _note_label(tonic) + (" 단조" if minor else " 장조")
    info = {"key": key_name, "style": "color", "instrument": None, "chords_given": bool(given), "skip_measures": skip,
            "chords": [(mel.measures[sl.measure]["number"], sl.chord.name(), sl.chord.roman) for sl in slots],
            "melody_name": ""}
    return root, info


def color_file(path, out=None, **kwargs):
    """악보 파일 → 코드 이름을 달고 색칠 표시를 한 MusicXML(`이름_색깔.musicxml`, 이미 `_색깔`이면 그 파일) → (경로, 정보)."""
    root = musicxml_reader._load_root(path)
    new_root, info = color_melody_root(root, **kwargs)
    if out is None:
        stem = os.path.splitext(str(path))[0]
        out = (stem if stem.endswith("_색깔") else stem + "_색깔") + ".musicxml"
    ET.indent(new_root)
    ET.ElementTree(new_root).write(out, encoding="utf-8", xml_declaration=True)
    return out, info


def harmonize_file(path, out=None, **kwargs):
    """악보 파일(MusicXML·mxl·MuseScore·Guitar Pro)을 읽어 반주를 더한 MusicXML을 쓰고 (경로, 정보)를 돌려준다.
    out이 없으면 원본 옆에 `이름_화성.musicxml` — 이미 있으면 덮어쓰지 않고 `이름_화성_2.musicxml`, `_3` …
    (만들 때마다 다른 반주라 앞에 만든 것도 남겨 둠). 화성 붙인 악보에서 다시 만들면 원래 이름 기준."""
    root = musicxml_reader._load_root(path)
    new_root, info = harmonize_root(root, **kwargs)
    if out is None:
        out = next_harmonized_path(path)
    ET.indent(new_root)
    ET.ElementTree(new_root).write(out, encoding="utf-8", xml_declaration=True)
    return out, info


def next_harmonized_path(path):
    """`곡.xml` → 비어 있는 `곡_화성.musicxml` / `곡_화성_2.musicxml` … (`곡_화성_3.musicxml`에서 다시 만들어도 `곡` 기준)."""
    stem = re.sub(r"_화성(_\d+)?$", "", os.path.splitext(str(path))[0])
    out, k = stem + "_화성.musicxml", 2
    while os.path.exists(out):
        out, k = f"{stem}_화성_{k}.musicxml", k + 1
    return out


def part_names(path):
    """악보 파일의 성부 이름 목록(멜로디 성부를 고르는 화면용)."""
    root = musicxml_reader._load_root(path)
    names = {sp.get("id"): (sp.findtext("part-name") or "").strip() for sp in root.findall("part-list/score-part")}
    return [names.get(p.get("id")) or p.get("id") for p in root.findall("part")]


# 화성별 바탕색 — 코드 이름의 으뜸 글자(A~G)가 색상, 코드 종류가 밝기·채도(곡·조와 상관없이 같은 코드는 늘 같은 색).
# 장조: 옅은 파스텔 / 딸림7: 같은 색상을 진하게 / 단조: 어둡고 가라앉은(푸른 회색 기운) 색 / 감화음: 더 어둡고 탁한 색.
# 같은 글자 코드(D·D7·D#7 …)는 같은 색상이되 농도를 크게 달리해 구분(사용자 요청 2026-10-27 — 예전엔 D와 D7이 같은
# 색, 올림·내림표는 색이 안 바뀌었음): 올림·내림표가 붙으면 ACCIDENTAL_TONE만큼 더 진하고 어둡게.
# 검은 음표 위에 곱해 칠하므로 너무 어둡지 않게.
NOTE_HUES = {"C": 0, "D": 30, "E": 52, "F": 115, "G": 210, "A": 252, "B": 300}   # 도
_QUALITY_TONE = {              # (채도, 밝기, 푸른 회색 섞는 비율)
    "major": (0.20, 1.00, 0.0), "dominant": (0.55, 0.96, 0.0),
    "minor": (0.30, 0.74, 0.30), "diminished": (0.22, 0.56, 0.45),
}
ACCIDENTAL_TONE = (0.22, -0.10)  # 올림·내림 코드: 채도 +, 밝기 −
_GLOOM = (0x6F, 0x7B, 0x94)    # 우울한 계통 색에 섞는 푸른 회색


def chord_color(letter, quality, alter=0):
    """코드 근음 글자(A~G)·종류(major·minor·dominant·diminished)·올림(+)/내림(−) → 바탕색 '#RRGGBB'."""
    import colorsys
    sat, val, gloom = _QUALITY_TONE.get(quality, _QUALITY_TONE["major"])
    if alter:
        sat, val = min(0.9, sat + ACCIDENTAL_TONE[0]), max(0.3, val + ACCIDENTAL_TONE[1])
    r, g, b = colorsys.hsv_to_rgb(NOTE_HUES[letter] / 360.0, sat, val)
    rgb = [round(255 * (c * (1 - gloom) + gc / 255 * gloom)) for c, gc in zip((r, g, b), _GLOOM)]
    return "#%02X%02X%02X" % tuple(rgb)


NOTE_COLORS = {k: chord_color(k, "major") for k in NOTE_HUES}    # 장조 색(참고용)


def given_chord_rows(part):
    """성부(멜로디)에 적힌 코드 이름 → 마디마다 [(시작 비율, 끝 비율, 색, 코드 이름)](없으면 None). 색칠에 쓰는 형식."""
    mel = read_melody(part)
    slots = given_slots(mel)
    if not slots:
        return None
    rows = [[] for _ in mel.measures]
    for sl in slots:
        m = mel.measures[sl.measure]
        rows[sl.measure].append(((sl.start - m["start"]) / m["length"],
                                 (sl.start + sl.length - m["start"]) / m["length"],
                                 chord_color(sl.chord.name()[0], sl.chord.quality, line_name(sl.chord.root)[1]),
                                 sl.chord.name()))
    return rows


def first_lyric_measure(root):
    """가사(<lyric>)가 처음 나오는 마디 인덱스(0부터) — 어느 성부에든. 가사가 하나도 없으면 None.
    그 앞 마디는 "가사 없는 전주"라 색을 칠하지 않는다(앱이 씀)."""
    best = None
    for part in root.findall("part"):
        for i, m in enumerate(part.findall("measure")):
            if m.find("note/lyric") is not None:
                best = i if best is None else min(best, i)
                break
    return best


def read_chord_map(path):
    """화성을 붙여 만든 악보(`HarmGuitar` 성부가 있는)면 마디마다 [(시작 비율, 끝 비율, 색 #RRGGBB, 코드 이름)](색은 코드 이름의 A~G 글자 + 종류별, chord_color)
    목록을 돌려준다(비율은 마디 길이 대비). 아니면 None."""
    root = musicxml_reader._load_root(path)
    skip = prelude_measures(root)                      # 처음 N마디는 전주 — 색 없음
    field = next((f for f in root.iter("miscellaneous-field") if f.get("name") == _CHORDS_FROM), None)
    if field is not None:
        # 악보에 적힌(또는 "멜로디에 색깔 넣기"로 멜로디에 달아 둔) 코드 이름을 쓰는 경우 — 그 성부의 코드로 칸을 다시 만든다
        src = next((p for p in root.findall("part") if p.get("id") == (field.text or "").strip()), None)
        if src is not None:
            rows = given_chord_rows(src)
            if rows:
                return [[] if k < skip else row for k, row in enumerate(rows)]
    part = next((p for p in root.findall("part") if (p.get("id") or "").startswith("HarmGuitar")), None)
    if part is None:
        return None
    result = []
    for m in part.findall("measure"):
        pos, total, marks = 0, 0, []
        for el in m:
            if el.tag == "harmony":
                step = el.findtext("root/root-step")
                alt = int(float(el.findtext("root/root-alter") or 0))
                marks.append((pos, (step, el.findtext("kind") or "major", alt),
                              step + ("#" * alt if alt > 0 else "b" * -alt) + (el.find("kind").get("text") or "")))
            elif el.tag == "note" and el.find("chord") is None:
                d = int(el.findtext("duration") or 0)
                pos += d
                total = max(total, pos)
            elif el.tag in ("backup",):
                pos -= int(el.findtext("duration") or 0)
        row = []
        for i, (start, (letter, quality, alt), name) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else total
            color = chord_color(letter, quality, alt)
            if total:
                row.append((start / total, end / total, color, name))
        result.append(row)
    return result


def summarize(info, per_line=8):
    """코드 진행을 사람이 읽는 줄로 — 마디 번호: 코드 | …"""
    lines, cur, last_m = [], [], None
    for num, name, _roman in info["chords"]:
        if num == last_m:
            cur[-1] += f" {name}"
        else:
            cur.append(f"{num}) {name}")
            last_m = num
        if len(cur) == per_line:
            lines.append("  ".join(cur))
            cur, last_m = [], None
    if cur:
        lines.append("  ".join(cur))
    return f"조: {info['key']}\n" + "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="멜로디에 화성(기타 반주 + 베이스) 붙이기")
    ap.add_argument("score", help="멜로디가 들어 있는 악보(.xml/.musicxml/.mxl 등)")
    ap.add_argument("-o", "--out", help="저장할 MusicXML 경로(기본: 이름_화성.musicxml)")
    ap.add_argument("--melody", type=int, default=1, help="멜로디 성부 번호(1부터, 기본 1)")
    ap.add_argument("--style", choices=("auto", "simple", "arpeggio", "block"), default="auto",
                    help="반주 모양 — simple: 동요용 3성부(멜로디+기타 둘), auto: 단순한 곡이면 simple, 아니면 arpeggio")
    ap.add_argument("--instrument", choices=tuple(INSTRUMENTS), default="guitar",
                    help="반주 악기(화음 + 저음 두 성부) — " + ", ".join(f"{k}: {v['label']}" for k, v in INSTRUMENTS.items()))
    ap.add_argument("--voices", help=f"반주 성부별 악기를 위에서부터 쉼표로(1~{MAX_VOICES}개, 예: guitar,cello,keyboard; "
                                     f"기타·콘트라베이스 기타 뒤에 {DROP_D}를 붙이면 드롭 D, 화음 악기 뒤에 :n4 = 화음 음 수 최대 4, "
                                     ":f16 = 가장 빠른 음표 16분) "
                                     "— 주면 --instrument·--no-bass 대신 씀")
    ap.add_argument("--skip", type=int, default=0, metavar="N",
                    help="처음 N마디는 멜로디만 두고(단선율·색 없음) 그 뒤부터 화성을 붙임")
    ap.add_argument("--no-bass", action="store_true", help="베이스 성부를 더하지 않음")
    ap.add_argument("--per-measure", type=int, choices=(1, 2), help="마디당 코드 수(기본: 자동)")
    args = ap.parse_args(argv)
    for level, text in check_voices(args.voices.split(",")) if args.voices else []:
        print(("주의: " if level == "warn" else "참고: ") + text)
    out, info = harmonize_file(args.score, args.out, melody_part=args.melody, style=args.style,
                               bass=not args.no_bass, per_measure=args.per_measure, instrument=args.instrument, skip_measures=args.skip,
                               voices=args.voices.split(",") if args.voices else None)
    print(summarize(info))
    print(f"→ {out}")


if __name__ == "__main__":
    main()
