# -*- coding: utf-8 -*-
"""악보(MusicXML)를 읽어 음표 목록(시작·길이·음높이)과 빠르기를 돌려준다 — UI 독립,
numpy만(폰 호환).

ExtractAudio(합주 연습 툴)의 score_follow.py에서 "음원과 악보를 맞춰 박을 찾는" 부분
(DTW 정렬·오디오 특징 추출)은 빼고, "MusicXML을 구조화된 데이터로 읽기" 부분만 그대로
가져왔다(2026-10-07) — 이 프로그램은 녹음된 음원에 맞추는 게 아니라, 악보의 음표대로
소리를 직접 생성하는 것이 목적이라 DTW 정렬이 필요 없다.

흐름:
1. read_musicxml(path) — .xml/.mxl에서 마디(시작·길이, 4분음표 단위)·음표(시작·길이·
   MIDI 음높이)·빠르기(sound tempo)를 읽는다. 도돌이·엔딩·D.S./D.C./Coda/Segno/Fine
   표시도 첫 파트에서 읽어 마디에 달아둔다(인쇄된 순서 기준 — 아직 실제 연주 순서로
   안 펼침).
2. expand_performance_order(score) — 도돌이·엔딩·D.S./D.C./Coda/Segno/Fine을 따라
   실제 연주 순서로 마디를 펼친 새 score를 만든다(음표/빠르기도 같이 옮김).
3. nominal_seconds(score, q) — 4분음표 위치 q를 악보 빠르기대로의 "명목 시각"(초)으로
   바꾼다 — 음표를 실제로 소리 낼 때 "언제" 재생할지 정하는 데 쓴다.

notes 항목 (start_q, dur_q, midi, part_index, is_onset, artic) 하나하나가 "이 성부
(part_index)가 언제(start_q) 몇 박(dur_q) 동안 이 음높이(midi)를 낸다"를 뜻한다 —
is_onset=False면 앞 음과 붙임줄로 이어진 것(새로 타격하지 않고 소리만 이어짐). artic은
아티큘레이션 이름 frozenset("staccato", "accent", "fermata" 등). 꾸밈음은 artic에 "grace"가
들어간 짧은 음으로, 본음 박 위에서 본음 시간을 빌려 소리 낸다(_place_graces). 이 앱은 events.py가
이걸 합성용 연주 이벤트(초 단위·붙임줄 합침·셈여림 반영)로 바꿔 쓴다.

midi는 **실제로 나는 소리 높이**다 — <transpose>(이조 악기, 예: 기타를 "한 옥타브 위로
적는" 악보)의 chromatic·octave-change를 반영한다. <clef-octave-change>(8vb 음자리표)는
반영하지 않는다: 실측한 파일들(music21·Claude 생성)이 그 경우 pitch를 이미 소리 높이로
적어 두었다(보케리니 기타 III 최저음이 A2(45) — 적힌 높이라면 기타로 못 내는 A1이 됨,
2026-10-07 확인).
"""
import io
import re
import zipfile

import score_import
import xml.etree.ElementTree as ET

import numpy as np

_STEP = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# 셈여림 기호 → MIDI 세기(velocity). MuseScore 기본값과 비슷하게.
DYNAMIC_VELOCITY = {
    "pppp": 10, "ppp": 23, "pp": 36, "p": 49, "mp": 64, "mf": 80, "f": 96, "ff": 112,
    "fff": 120, "ffff": 127, "sf": 112, "sfz": 112, "sffz": 120, "fz": 112, "rf": 96,
    "rfz": 96, "fp": 96, "sfp": 112,
}
DEFAULT_VELOCITY = 80  # 셈여림 표시가 하나도 없을 때(mf)
# <sound dynamics="N">의 N은 "forte(=MIDI 90)에 대한 백분율"(MusicXML 규격)
_SOUND_DYNAMICS_FORTE = 90.0
_ARTICULATIONS = ("staccato", "staccatissimo", "spiccato", "accent", "strong-accent", "tenuto")
# 메트로놈 표시(<metronome>)의 박 단위 → 4분음표 몇 개인지
_BEAT_UNIT_Q = {"whole": 4.0, "half": 2.0, "quarter": 1.0, "eighth": 0.5, "16th": 0.25}


def _direction_velocity(e):
    """<direction> 하나에서 셈여림 세기를 읽는다(없으면 None). 기호(<dynamics><mf/>)가
    있으면 그게 우선, 없을 때만 <sound dynamics>로. 작성 프로그램마다 <sound dynamics>
    값이 제각각이라(music21은 mp를 44 → 세기 40으로 적음, 2026-10-07 확인) 기호 쪽이
    곡마다 일정한 셈여림 폭을 준다."""
    for d in e.iter("dynamics"):
        for mark in d:
            v = DYNAMIC_VELOCITY.get(mark.tag)
            if v is not None:
                return v
    for s in e.iter("sound"):
        if s.get("dynamics"):
            try:
                return int(round(min(127.0, float(s.get("dynamics")) * _SOUND_DYNAMICS_FORTE / 100.0)))
            except ValueError:
                pass
    return None


def _metronome_bpm(e):
    """<direction>의 <metronome>(<beat-unit>+<per-minute>)을 4분음표 bpm으로. <sound
    tempo>가 없는 악보(손으로 만든 MusicXML 등)를 위한 대비책."""
    for mt in e.iter("metronome"):
        unit = _BEAT_UNIT_Q.get(mt.findtext("beat-unit") or "")
        try:
            per_min = float(mt.findtext("per-minute") or "")
        except ValueError:
            continue
        if unit is None:
            continue
        if mt.find("beat-unit-dot") is not None:
            unit *= 1.5
        return per_min * unit
    return None


# ---------------------------------------------------------------- 꾸밈음

# 음표 모양(<type>) → 4분음표 몇 개인지(빗금 없는 앞꾸밈음 길이용)
_TYPE_Q = {"whole": 4.0, "half": 2.0, "quarter": 1.0, "eighth": 0.5, "16th": 0.25,
           "32nd": 0.125, "64th": 0.0625}
# 빗금 꾸밈음(acciaccatura) 한 음 길이(초) — 빠르기와 상관없이 "짧게 스치는" 느낌이라 초로 정함
ACCIACCATURA_SEC = 0.08   # 0.06 → 0.08 (2026-10-12, 꾸밈음이 본음 앞에서 묻힌다는 지적 — 할보르센 69마디)
# 꾸밈음들이 본음에서 빌려 갈 수 있는 최대 비율
_GRACE_MAX_STEAL = 0.5


# ---------------------------------------------------------------- 떤꾸밈음(trill)
# 사용자가 들어보고 넣을지 정하기로 함(2026-10-08) — False면 본음만
PLAY_TRILLS = True
TRILL_NOTES_PER_SEC = 10.0  # 번갈아 치는 빠르기(약하게 — 너무 빠르지 않게)
_SHARP_ORDER = "FCGDAEB"
_ACCIDENTAL_MARK_ALTER = {"sharp": 1, "natural": 0, "flat": -1, "double-sharp": 2, "flat-flat": -2}


def _key_alter(fifths, step):
    """조표(fifths)에서 그 음이름이 받는 올림·내림."""
    if fifths > 0 and step in _SHARP_ORDER[:fifths]:
        return 1
    if fifths < 0 and step in _SHARP_ORDER[::-1][:-fifths]:
        return -1
    return 0


def _trill_upper_midi(p, fifths, ornaments, direction=1):
    """떤꾸밈·잔꾸밈 이웃음(온음계 한 단계 위, direction=-1이면 아래 — 조표 반영,
    <accidental-mark>가 있으면 그걸 따름). 마디 안 임시표는 반영하지 않는다."""
    steps = "CDEFGAB"
    i = steps.index(p.findtext("step"))
    octave = int(p.findtext("octave"))
    if direction > 0 and i == 6:
        octave += 1
    elif direction < 0 and i == 0:
        octave -= 1
    step = steps[(i + direction) % 7]
    alter = _key_alter(fifths, step)
    am = ornaments.find("accidental-mark")
    if am is not None and (am.text or "").strip() in _ACCIDENTAL_MARK_ALTER:
        alter = _ACCIDENTAL_MARK_ALTER[am.text.strip()]
    return (octave + 1) * 12 + _STEP[step] + alter


def _place_trill(start_q, dur_q, midi, upper, bpm, pi, is_onset, artic, notes):
    """본음↔위 이웃음을 TRILL_NOTES_PER_SEC로 번갈아 — 본음으로 시작해 본음으로 끝나게(홀수 개).
    첫 음만 원래 아티큘레이션, 나머지는 "trill"(events.py가 약하게)."""
    sec = dur_q * 60.0 / bpm
    n = max(3, int(round(sec * TRILL_NOTES_PER_SEC)))
    if n % 2 == 0:
        n -= 1
    step = dur_q / n
    for k in range(n):
        notes.append((start_q + k * step, step, upper if k % 2 else midi, pi,
                       is_onset if k == 0 else True, artic if k == 0 else frozenset({"trill"})))


# 잔결꾸밈음(mordent) — 본음 → 이웃음 → 본음. 사용자 표(2026-10-13): 빗금 없는 기호는 16분음표,
# 세로줄 있는 기호는 32분음표 두 개 + 나머지는 본음. 너무 느린 곡에서 늘어지지 않게 초 단위 상한을 둔다.
MORDENT_Q = {"inverted-mordent": 0.25, "mordent": 0.125}
ORNAMENT_MAX_SEC = 0.12


def _place_mordent(start_q, dur_q, midi, neighbor, bpm, pi, is_onset, artic, notes, unit_q=0.25):
    """잔결꾸밈음: 본음·이웃음을 unit_q(4분음표 몇 개)씩 짧게, 나머지는 본음. 첫 음 뒤 두 음은
    "ornament"(events.py가 약하게 — 기타에선 첫 음만 뜯고 해머링·풀링으로 치므로)."""
    short = min(unit_q, ORNAMENT_MAX_SEC * bpm / 60.0, dur_q / 3)
    soft = frozenset({"ornament"})
    notes.append((start_q, short, midi, pi, is_onset, artic))
    notes.append((start_q + short, short, neighbor, pi, True, soft))
    notes.append((start_q + 2 * short, dur_q - 2 * short, midi, pi, True, soft))


# 돈꾸밈음(turn) — 위·본·아래·본(turn) 또는 아래·본·위·본(inverted-turn), 앞 세 음은 16분음표,
# 마지막 본음이 나머지 시간을 채움(사용자 표 2026-10-13).
TURN_Q = 0.25


def _place_turn(start_q, dur_q, midi, first, second, bpm, pi, is_onset, artic, notes):
    """돈꾸밈음: first → 본음 → second → 본음. 본음이 짧아 4등분이 안 되면 본음 길이를 4등분해 줄인다."""
    unit = min(TURN_Q, ORNAMENT_MAX_SEC * bpm / 60.0, dur_q / 4)
    soft = frozenset({"ornament"})
    seq = [first, midi, second]
    for k, m in enumerate(seq):
        notes.append((start_q + k * unit, unit, m, pi, is_onset if k == 0 else True,
                      artic if k == 0 else soft))
    notes.append((start_q + 3 * unit, dur_q - 3 * unit, midi, pi, True, soft))


# 자연 하모닉스: 프렛 → 개방현보다 몇 반음 위가 나는지 (12프렛 1옥타브, 7·19프렛 1옥타브+완전5도,
# 5·24프렛 2옥타브, 4·9프렛 2옥타브+장3도). 사용자 정함(2026-10-12): 이 표에 없는 프렛의 하모닉스
# (프렛 정보 없음 포함)는 적힌 음보다 한 옥타브 위.
HARMONIC_ABOVE_OPEN = {12: 12, 7: 19, 19: 19, 5: 24, 24: 24, 4: 28, 9: 28}


def _measure_label(number):
    """마디 번호 글자 — "X1"처럼 글자가 붙은 번호(MuseScore의 불완전 마디)는 숫자만 "1"로(사용자 요청:
    "4번 마디가 처음으로 나와도 역추산이 되니 X1은 1번으로")."""
    digits = re.sub(r"\D", "", number or "")
    return digits if digits else number


def _tremolo_step(slashes):
    """트레몰로 빗금 수 → 한 번 뜯는 길이(q): 1줄 8분, 2줄 16분, 3줄 32분."""
    return 0.5 / 2 ** (max(1, min(4, slashes)) - 1)


def _place_tremolo(start_q, dur_q, chords, step, pi, first_onset, artic, notes):
    """트레몰로 — [start_q, start_q+dur_q)를 step 간격으로 빠르게 되뜯는다. chords가 둘 이상이면(두 음 트레몰로)
    번갈아. 처음에는 트레몰로 표시를 몰라 한 번만 울렸다(안드로이드판 사용자 지적 2026-10-11)."""
    n = max(1, int(round(dur_q / step)))
    each = dur_q / n
    for k in range(n):
        for midi in chords[k % len(chords)]:
            notes.append((start_q + k * each, each, midi, pi, True if k else first_onset,
                          artic if k == 0 else frozenset(a for a in artic if isinstance(a, tuple))))


def _is_contrabass_guitar(part, name, instrument):
    """콘트라베이스 기타 성부 — 이름·악기 이름(CB. Gtr., Contrabass Guitar)이나 15mb(8vb ×2) 음자리표."""
    text = f"{name} {(instrument or {}).get('name', '')}".lower()
    if ("contrabass" in text and "guitar" in text) or "cb. gtr" in text or "cb.gtr" in text or "콘트라베이스 기타" in text:
        return True
    return any((c.text or "").strip() == "-2" for c in part.iterfind("measure/attributes/clef/clef-octave-change"))


def _pitch_midi(p):
    """<pitch> → MIDI 번호(이조 반영 전)."""
    return (int(p.findtext("octave")) + 1) * 12 + _STEP[p.findtext("step")] + \
        int(round(float(p.findtext("alter") or 0)))


def _bpm_at(tempos, q):
    """q 시점의 4분음표 bpm(읽는 중인 빠르기 목록 기준, 없으면 100)."""
    bpm = 100.0
    for tq, b in sorted(tempos):
        if tq > q + 1e-9:
            break
        bpm = b
    return bpm


def _place_graces(graces, main_q, main_dur_q, bpm, pi, notes):
    """꾸밈음들을 본음 박 위에서(본음 시간을 빌려) 차례로 notes에 넣고, 본음이 늦춰질 양(q)을
    돌려준다. 박 위에 두는 이유: 꾸밈음이 늘 그 마디 안에 머물러 도돌이 펼치기·구간 합성이
    마디 단위로 음표를 나눌 때 어긋나지 않는다(앞 마디로 넘어가면 엉뚱한 회차에 붙음).
    빗금 꾸밈음은 음당 ACCIACCATURA_SEC초, 빗금 없는 앞꾸밈음은 하나면 본음의 절반, 여럿이면 적힌 음가대로 — 합계는 본음의
    _GRACE_MAX_STEAL을 넘지 않게 같은 비율로 줄인다."""
    sec_q = bpm / 60.0  # 1초 = 4분음표 몇 개
    lens = [ACCIACCATURA_SEC * sec_q if slash else type_q for _, slash, type_q in graces]
    if len(graces) == 1 and not graces[0][1]:
        lens = [main_dur_q * _GRACE_MAX_STEAL]   # 긴앞꾸밈음(appoggiatura): 본음의 절반(사용자 표 2026-10-13)
    total = sum(lens)
    limit = main_dur_q * _GRACE_MAX_STEAL
    if total > limit > 0:
        lens = [x * limit / total for x in lens]
        total = limit
    q = main_q
    for (midis, _, _), length in zip(graces, lens):
        for midi in midis:
            notes.append((q, length, midi, pi, True, frozenset({"grace"})))
        q += length
    return total


# ---------------------------------------------------------------- 표현: 빠르기·셈여림 변화, pizz.
# (2026-10-08 사용자 요청) 악보 글자·꺾쇠·페르마타를 소리에 반영한다. 다 읽은 뒤 한 번에:
#   - 빠르기: <sound tempo>가 없는 빠르기말(Andante 등)은 표로 숫자를 정하고, rit./rall./allargando는
#     점점 느리게, accel./string.은 점점 빠르게(다음 빠르기 표시까지, 최대 다음 마디 끝까지), 페르마타는
#     그 음 길이 동안 FERMATA_STRETCH배로 늘인다. 모두 tempos 목록의 잘게 나눈 구간으로 표현 — 재생·
#     메트로놈·마디 시각 계산(nominal_seconds)이 손대지 않고 그대로 따른다.
#   - 셈여림: 꺾쇠(<wedge>)와 cresc./dim. 글자를 점점 세게·여리게 — 다음 셈여림 기호가 맞는 쪽이면
#     거기까지, 아니면 DYN_RAMP_STEP만큼. dynamics 목록에 0.25박마다 점을 넣어 표현.
#   - pizz./arco: 그 사이 음표의 artic에 "pizz"(events·synth가 약음 소리로).

FERMATA_STRETCH = 1.8
DYN_RAMP_STEP = 20     # 목표 기호가 없을 때 cresc./dim.으로 바뀌는 세기
_RAMP_STEP_Q = 0.25    # 점점 바뀌는 곡선을 나누는 단위(4분음표의 1/4)
# 빠르기말(첫 낱말) → 4분음표 bpm — <sound tempo>가 없을 때만
_TEMPO_WORDS = {
    "grave": 40, "largo": 50, "lento": 56, "larghetto": 60, "adagio": 66, "adagietto": 72,
    "andante": 80, "andantino": 88, "moderato": 100, "allegretto": 112, "allegro": 132,
    "vivace": 150, "presto": 176, "prestissimo": 200,
}


def _technique(txt):
    t = txt.lower().strip()
    if t.startswith("pizz"):
        return "pizz"
    if t.startswith("arco") or t in ("ord.", "ord", "nat.", "naturale", "modo ordinario"):
        return "arco"
    return None


def _tempo_word_bpm(txt):
    first = txt.lower().strip().split()[0].strip(".,;:!()") if txt.strip() else ""
    return _TEMPO_WORDS.get(first)


def _tempo_ramp(txt):
    """글자 → 빠르기 곡선의 끝 배율(느려지면 <1), "a tempo"면 "atempo", 아니면 None."""
    t = txt.lower()
    if "2nd time" in t or "2a volta" in t:
        return None  # 도돌이 둘째에만 — 회차 구분은 아직 안 함
    if t.startswith("a tempo") or t.startswith("tempo i") or t.startswith("tempo primo"):
        return "atempo"
    strength = 1.35 if "molto" in t else (0.5 if "poco" in t else 1.0)
    if any(k in t for k in ("rit", "rall", "allarg", "slentando")):
        return 1.0 - 0.25 * strength
    if any(k in t for k in ("accel", "string", "stretto")):
        return 1.0 + 0.2 * strength
    return None


def _dyn_word(txt):
    t = txt.lower()
    if "cresc" in t:
        return 1
    if "decresc" in t or t.startswith("dim") or " dim" in t:
        return -1
    return None


def _measure_end_after(measures, q, extra):
    """q가 든 마디부터 extra마디 뒤의 끝(q)."""
    starts = [m["start_q"] for m in measures]
    i = max(0, int(np.searchsorted(starts, q + 1e-9, side="right")) - 1)
    j = min(len(measures) - 1, i + extra)
    return measures[j]["start_q"] + measures[j]["length_q"] if measures else q + 4.0 * (extra + 1)


def _shape_tempos(tempos, words, fermatas, measures):
    """tempos(<sound tempo> 점들) + 빠르기말·rit./accel.·페르마타 → 잘게 나눈 tempos."""
    explicit = [q for q, _ in tempos]
    base = list(tempos)
    first_bpm = tempos[0][1]
    ramps, marks = [], []  # ramps: (q0, 끝 배율), marks: 곡선을 끝내는 자리
    for q, _pi, txt, has_tempo in words:
        at_explicit = has_tempo or any(abs(q - e) < 1e-6 for e in explicit)
        bpm = _tempo_word_bpm(txt)
        if bpm and not at_explicit:
            base.append((q, float(bpm)))
            marks.append(q)
            continue
        r = _tempo_ramp(txt)
        if r == "atempo":
            if txt.lower().startswith("tempo"):
                base.append((q, first_bpm))
            marks.append(q)
        elif r is not None and not at_explicit:
            ramps.append((q, r))
    base.sort()
    marks += [q for q, _ in base] + [q for q, _ in ramps]
    spans = []
    for q0, target in ramps:
        later = [m for m in marks if m > q0 + 1e-6]
        end = min([_measure_end_after(measures, q0, 1)] + later)
        if end > q0:
            spans.append((q0, end, target))
    ferm = sorted(fermatas)
    if not spans and not ferm and len(base) == len(tempos):
        return tempos

    bq = np.array([q for q, _ in base])

    def base_at(q):
        i = int(np.searchsorted(bq, q + 1e-9, side="right")) - 1
        return base[max(0, i)][1]

    def factor_at(q):
        f = 1.0
        for q0, q1, target in spans:
            if q0 - 1e-9 <= q < q1 - 1e-9:
                f *= 1.0 + (target - 1.0) * min(1.0, (q - q0 + _RAMP_STEP_Q / 2) / (q1 - q0))
        if any(fq - 1e-9 <= q < fq + fd - 1e-9 for fq, fd in ferm):
            f /= FERMATA_STRETCH
        return f

    points = {0.0} | {q for q, _ in base}
    for q0, q1, _t in spans:
        points |= set(np.arange(q0, q1, _RAMP_STEP_Q).tolist()) | {q1}
    for fq, fd in ferm:
        points |= {fq, fq + fd}
    out = []
    for q in sorted(points):
        bpm = base_at(q) * factor_at(q)
        if not out or abs(out[-1][1] - bpm) > 1e-9:
            out.append((float(q), float(bpm)))
    return out


def _dynamic_ramps(dynamics, wedges, words, measures):
    """꺾쇠·cresc./dim. 글자 → 0.25박마다 세기 점들(dynamics에 더할 것)."""
    ramps = []  # (성부, q0, q1 또는 None, 방향)
    open_w = {}
    for q, pi, kind, num in sorted(wedges, key=lambda w: w[0]):
        if kind in ("crescendo", "diminuendo"):
            open_w[(pi, num)] = (q, 1 if kind == "crescendo" else -1)
        elif kind == "stop" and (pi, num) in open_w:
            q0, d = open_w.pop((pi, num))
            ramps.append((pi, q0, max(q, q0 + 1.0), d))  # 길이 0 꺾쇠("p<")는 한 박으로
    for q, pi, txt, _ in words:
        d = _dyn_word(txt)
        if d is not None:
            ramps.append((pi, q, None, d))
    out = []
    ramps.sort(key=lambda r: (r[0], r[1]))
    for k, (pi, q0, q1, d) in enumerate(ramps):
        # 앞 곡선이 만든 점도 "지금 세기"에 넣는다(cresc.가 연달아 적힌 경우 이어서 커지게)
        marks = sorted([(q, v) for q, p, v in dynamics if p == pi] + [(q, v) for q, p, v in out if p == pi],
                       key=lambda m: m[0])  # 같은 자리는 적힌 순서 유지
        before = [v for q, v in marks if q <= q0 + 1e-6]
        v0 = before[-1] if before else DEFAULT_VELOCITY
        after = [(q, v) for q, p, v in dynamics if p == pi and q > q0 + 1e-6]
        nxt_ramp = next((r[1] for r in ramps[k + 1:] if r[0] == pi and r[1] > q0 + 1e-6), None)
        if q1 is None:  # 글자: 다음 기호가 맞는 쪽이고 4마디 안이면 거기까지, 아니면 2마디
            if after and (after[0][1] - v0) * d > 0 and after[0][0] <= _measure_end_after(measures, q0, 3):
                q1, target = after[0]
            else:
                q1, target = _measure_end_after(measures, q0, 1), v0 + d * DYN_RAMP_STEP
        else:           # 꺾쇠: 끝 바로 뒤(1박 안) 기호가 맞는 쪽이면 그 세기로
            nxt = [(q, v) for q, v in after if q <= q1 + 1.0]
            target = nxt[0][1] if nxt and (nxt[0][1] - v0) * d > 0 else v0 + d * DYN_RAMP_STEP
        # 도중에 다른 기호나 같은 성부의 다음 곡선이 시작하면 거기서 멈춤(그 자리까지 간 세기로)
        stops = [q for q, _ in after[:1]] + ([nxt_ramp] if nxt_ramp is not None else [])
        stop = min(stops) if stops else None
        if stop is not None and stop < q1 - 1e-6:
            target = v0 + (target - v0) * (stop - q0) / (q1 - q0)
            q1 = stop
        target = int(np.clip(target, 10, 120))
        steps = np.arange(q0 + _RAMP_STEP_Q, q1, _RAMP_STEP_Q)
        for q in steps:
            v = v0 + (target - v0) * (q - q0) / (q1 - q0)
            out.append((float(q), pi, int(round(v))))
        if not (after and abs(after[0][0] - q1) < 1e-6):
            out.append((float(q1), pi, target))
    return out


def _apply_techniques(notes, changes):
    """pizz./arco 전환 지점 → 그 사이 음표 artic에 "pizz"."""
    if not changes:
        return notes
    by_part = {}
    for q, pi, on in sorted(changes):
        by_part.setdefault(pi, []).append((q, on))
    out = []
    for n in notes:
        ch = by_part.get(n[3])
        on = False
        if ch:
            for q, flag in ch:
                if q <= n[0] + 1e-6:
                    on = flag
                else:
                    break
        out.append(n[:5] + (n[5] | {"pizz"},) if on else n)
    return out


CELLO_MIN_RUN = 3   # 1~4번 줄 단음이 이만큼 이어져야 "줄 위 멜로디"로 봄
LOW_STRING_MIDI = 50   # 줄 번호가 없을 때 이보다 낮은 음(4번 개방현 D3 아래)은 5·6번 줄 음으로 봄


def _apply_cello_runs(notes):
    """줄 위 단음 멜로디를 첼로처럼, 5·6번 줄은 묵직한 저음으로.
    - 처음엔 4번 줄만(사용자 요청 2026-10-08: "4번 줄로만 멜로디를 낼 때는 첼로처럼"), 2026-10-09 "4번 줄에 적용한
      소리 기법은 1, 2, 3, 4번 줄 모두에, 5·6번은 거친 소리를 줄이고 묵직한 저음으로":
      같은 성부에서 1~4번 줄 표시가 붙은 단음(화음 아님)이 CELLO_MIN_RUN개 이상 연달아 나오면 "cello"(줄이 섞여도 됨).
      꾸밈음·트릴 조각은 건너뛰고(줄을 끊지 않음) 센다.
    - 5·6번 줄 표시가 붙은 음, 또는 줄 표시가 없는 음 중 LOW_STRING_MIDI보다 낮은 음에 "low"(synth.low_timbre).
    음색이 기타인 성부에서만 소리가 바뀐다(render)."""
    skip = ("grace", "trill", "ornament")
    mark = set()
    if any("str_hi" in n[5] for n in notes):
        by_part = {}
        for i, n in enumerate(notes):
            if not any(s in n[5] for s in skip):
                by_part.setdefault(n[3], {}).setdefault(round(n[0], 6), []).append(i)
        for onsets in by_part.values():
            run = []
            for q in sorted(onsets):
                idx = onsets[q]
                if len(idx) == 1 and "str_hi" in notes[idx[0]][5]:
                    run.append(idx[0])
                    continue
                if len(run) >= CELLO_MIN_RUN:
                    mark.update(run)
                run = []
            if len(run) >= CELLO_MIN_RUN:
                mark.update(run)
    out = []
    for i, n in enumerate(notes):
        a = n[5]
        if i in mark:
            out.append(n[:5] + (a | {"cello"},))
        elif "str_lo" in a or ("str_hi" not in a and n[2] < LOW_STRING_MIDI):
            out.append(n[:5] + (a | {"low"},))
        else:
            out.append(n)
    return out


def _apply_rasgueado(notes, marks, measures):
    """"rasg." 글자 뒤 그 마디 끝까지, 그 성부에서 3음 이상 동시에 시작하는 화음에 "rasg"."""
    if not marks:
        return notes
    spans = [(q, _measure_end_after(measures, q, 0), pi) for q, pi in marks]
    count = {}
    for n in notes:
        count[(n[3], round(n[0], 6))] = count.get((n[3], round(n[0], 6)), 0) + 1
    out = []
    for n in notes:
        hit = count[(n[3], round(n[0], 6))] >= 3 and any(
            pi == n[3] and q0 - 1e-6 <= n[0] < q1 - 1e-6 for q0, q1, pi in spans)
        out.append(n[:5] + (n[5] | {"rasg"},) if hit else n)
    return out


# ---------------------------------------------------------------- 1. MusicXML 읽기

def _load_root(path):
    path = score_import.resolve(path)  # mscz·Guitar Pro 등은 MuseScore로 MusicXML로 바꾼 파일
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
            data = z.read(target)
        return ET.parse(io.BytesIO(data)).getroot()
    return ET.parse(path).getroot()


def read_musicxml(path):
    """반환: {"measures": [{"number", "start_q", "length_q", "beats", "beat_type",
             "repeat_start", "repeat_end_times", "ending", "dacapo", "dalsegno", "tocoda",
             "coda", "segno", "fine"}],
             "notes": [(start_q, dur_q, midi, part_index, is_onset, artic)],
             "tempos": [(q, 4분음표_bpm)], "dynamics": [(q, part_index, velocity)],
             "parts": [이름], "part_abbrevs": [줄인 이름 또는 ""], "instruments": [{"name", "program"}]}.
    instruments의 program은 GM 악기 번호(1~128, <midi-program>; 없으면 None).
    q는 곡 처음부터의 4분음표 수(인쇄된 순서 기준 — 도돌이 펼치기 전). 붙임줄로 이어진 음은
    is_onset=False(소리는 이어짐). 도돌이·엔딩·D.S./D.C./Coda/Segno/Fine 표시는 첫 파트에서만
    읽어 각 마디에 달아둔다(expand_performance_order가 실제 연주 순서로 펼치는 데 씀)."""
    root = _load_root(path)
    if root.tag.endswith("score-timewise"):
        raise ValueError("score-timewise 형식은 아직 지원하지 않습니다(악보 프로그램에서 partwise로 내보내기).")
    part_names = {}
    part_abbrevs = {}  # 줄인 이름(예: "Gtr.1") — 화면의 성부 칩용
    part_instruments = {}
    pl = root.find("part-list")
    if pl is not None:
        for sp in pl.findall("score-part"):
            part_names[sp.get("id")] = (sp.findtext("part-name") or sp.get("id")).strip()
            part_abbrevs[sp.get("id")] = (sp.findtext("part-abbreviation") or "").strip()
            # 한 성부에 악기가 여럿(기타 + 피치카토 판, MuseScore가 pizz.용으로 붙임)이면 기타를 우선 — 첫 악기인
            # 피치카토(46)를 따르면 기타 성부가 지속음(현악기) 소리로 나왔다(EnsemblePracticeForAndroid 2026-10-11)
            progs = []
            for mi_ in sp.findall("midi-instrument"):
                try:
                    progs.append(int(mi_.findtext("midi-program") or ""))
                except ValueError:
                    pass
            program = next((g for g in progs if 25 <= g <= 32), progs[0] if progs else None)
            names_ = [(x.findtext("instrument-name") or "").strip() for x in sp.findall("score-instrument")]
            iname = next((n_ for n_ in names_ if "guitar" in n_.lower() or "기타" in n_), names_[0] if names_ else "")
            part_instruments[sp.get("id")] = {"name": iname, "program": program}
    parts = root.findall("part")
    measures, notes, tempos, dynamics = [], [], [], []
    # 다 읽은 뒤 셈여림·빠르기·주법 변화로 바꿀 표시들: 꺾쇠 [(q, 성부, type, number)],
    # 글자 [(q, 성부, 글, 같은 표시에 sound tempo 있음?)], 페르마타 [(q, 길이 q)],
    # pizz./arco 전환 [(q, 성부, pizz?)]
    wedges, words, fermatas, tech_changes = [], [], [], []
    rasg_marks = []  # [(q, 성부)] "rasg." 글자 자리
    metronome_tempos = []  # <sound tempo>가 하나도 없을 때만 쓰는 대비책
    for pi, part in enumerate(parts):
        div = 1
        transpose = 0  # 적힌 높이 → 소리 높이(반음)
        cb_shift = 0
        if _is_contrabass_guitar(part, part_names.get(part.get("id"), ""), part_instruments.get(part.get("id"))):
            cb_shift = 12
            transpose = cb_shift   # 콘트라베이스 기타: 악보 음이 기타보다 12~24반음 낮아 너무 깊었다 → 기타보다 1옥타브 낮게
        fifths = 0     # 조표(떤꾸밈 위 이웃음용)
        beats, beat_type = 4, 4
        q0 = 0.0
        ending_events = []  # [(numbers:set, type)] — 이 마디에서 시작/끝나는 1·2번 괄호
        pending_graces = []  # 아직 본음을 못 만난 꾸밈음 [([midi…], 빗금?, 적힌 음가 q)]
        grace_shift = 0.0    # 방금 본음이 꾸밈음 때문에 늦춰진 양(q) — 뒤따르는 화음 음표도 같이
        tech_q, notes_since_tech = None, 0  # 마지막 pizz./arco 글자 위치, 그 뒤 음표 수
        trem2 = None         # 두 음 트레몰로(start~stop) 진행 중: {"q", "end", "a": [midi], "b": [midi], "step", "art", "onset"}

        def flush_trem2():
            nonlocal trem2
            t, trem2 = trem2, None
            if t and t["b"] and t["end"] > t["q"]:
                _place_tremolo(t["q"], t["end"] - t["q"], [t["a"], t["b"]], t["step"], pi, t["onset"], t["art"], notes)
            elif t:
                for m_ in t["a"] + t["b"]:      # 짝이 없으면 그냥 한 음
                    notes.append((t["q"], max(t["end"] - t["q"], 0.0), m_, pi, t["onset"], t["art"]))
        for mi, m in enumerate(part.findall("measure")):
            cur = 0          # 마디 안 위치(divisions)
            last_start = 0
            longest = 0
            repeat_start = False
            repeat_end_times = None
            ending_events = []
            jumps = {}
            for e in m:
                tag = e.tag
                if tag == "attributes":
                    if e.findtext("divisions"):
                        div = int(e.findtext("divisions"))
                    tr = e.find("transpose")
                    if tr is not None:
                        try:
                            transpose = int(tr.findtext("chromatic") or 0) + \
                                12 * int(tr.findtext("octave-change") or 0) + cb_shift
                        except ValueError:
                            pass
                    if e.findtext("key/fifths"):
                        try:
                            fifths = int(e.findtext("key/fifths"))
                        except ValueError:
                            pass
                    t = e.find("time")
                    if t is not None and t.findtext("beats"):
                        try:
                            beats = int(t.findtext("beats").split("+")[0])
                            beat_type = int(t.findtext("beat-type"))
                        except ValueError:
                            pass
                elif tag == "backup":
                    cur -= int(e.findtext("duration") or 0)
                    pending_graces = []  # 성부가 바뀜 — 본음 없이 남은 꾸밈음(뒤꾸밈음 등)은 버림
                elif tag == "forward":
                    cur += int(e.findtext("duration") or 0)
                elif tag == "direction":
                    # 셈여림은 성부마다(각 파트에 따로 적힘), 빠르기·점프는 첫 파트에서만
                    off = int(e.findtext("offset") or 0)
                    q_dir = q0 + (cur + off) / div
                    vel = _direction_velocity(e)
                    if vel is not None:
                        dynamics.append((q_dir, pi, vel))
                    has_tempo = any(s.get("tempo") for s in e.iter("sound"))
                    for w in e.iter("wedge"):
                        wedges.append((q_dir, pi, w.get("type"), w.get("number", "1")))
                    for w in e.iter("words"):
                        txt = (w.text or "").strip()
                        if not txt:
                            continue
                        words.append((q_dir, pi, txt, has_tempo))
                        if "rasg" in txt.lower():
                            rasg_marks.append((q_dir, pi))   # 그 마디 안 뒤따르는 화음을 라스게아도로
                        tech = _technique(txt)
                        if tech is None:
                            continue
                        if tech_q is not None and abs(tech_q - q_dir) < 1e-6 and not notes_since_tech:
                            # 같은 자리에 arco·pizz.가 겹쳐 적힘(변환 중 위치가 뭉개진 것 — 할보르센
                            # 29~32마디) → 나중 것은 마디 후반부터
                            tech_changes.append((q0 + beats * 2.0 / beat_type, pi, tech == "pizz"))
                        else:
                            tech_changes.append((q_dir, pi, tech == "pizz"))
                        tech_q, notes_since_tech = q_dir, 0
                    if pi != 0:
                        continue
                    bpm = _metronome_bpm(e)
                    if bpm:
                        metronome_tempos.append((q0 + (cur + off) / div, bpm))
                    for s in e.iter("sound"):
                        if s.get("tempo"):
                            tempos.append((q0 + (cur + off) / div, float(s.get("tempo"))))
                        for key in ("dacapo", "dalsegno", "tocoda", "coda", "segno", "fine"):
                            if s.get(key):
                                jumps[key] = s.get(key)
                elif tag == "sound" and pi == 0:
                    if e.get("tempo"):
                        tempos.append((q0 + cur / div, float(e.get("tempo"))))
                    for key in ("dacapo", "dalsegno", "tocoda", "coda", "segno", "fine"):
                        if e.get(key):
                            jumps[key] = e.get(key)
                elif tag == "barline" and pi == 0:
                    rep = e.find("repeat")
                    if rep is not None:
                        if rep.get("direction") == "forward":
                            repeat_start = True
                        elif rep.get("direction") == "backward":
                            try:
                                repeat_end_times = int(rep.get("times")) if rep.get("times") else 2
                            except ValueError:
                                repeat_end_times = 2
                    end = e.find("ending")
                    if end is not None and end.get("number"):
                        try:
                            nums = {int(x) for x in end.get("number").replace(" ", "").split(",") if x}
                        except ValueError:
                            nums = set()
                        if nums:
                            ending_events.append((nums, end.get("type")))
                elif tag == "note":
                    if e.find("cue") is not None:
                        continue
                    g = e.find("grace")
                    if g is not None:
                        # 꾸밈음: 길이가 없으니 모아 뒀다가 다음 본음 자리에서 소리 낸다
                        p = e.find("pitch")
                        if p is not None:
                            midi = _pitch_midi(p) + transpose
                            if e.find("chord") is not None and pending_graces:
                                pending_graces[-1][0].append(midi)  # 꾸밈 화음
                            else:
                                pending_graces.append(([midi], g.get("slash") == "yes",
                                                       _TYPE_Q.get(e.findtext("type") or "", 0.25)))
                        continue
                    dur = int(e.findtext("duration") or 0)
                    if (trem2 is not None and trem2["b"] and e.find("chord") is None
                            and e.find("notations/ornaments/tremolo[@type='stop']") is None):
                        flush_trem2()
                    if e.find("chord") is not None:
                        start = last_start
                    else:
                        start = cur
                        cur += dur
                        grace_shift = 0.0
                        if pending_graces:
                            if e.find("pitch") is not None:
                                bpm = _bpm_at(tempos or metronome_tempos, q0 + start / div)
                                grace_shift = _place_graces(pending_graces, q0 + start / div, dur / div,
                                                            bpm, pi, notes)
                            pending_graces = []
                    last_start = start
                    notes_since_tech += 1
                    if e.find("notations/fermata") is not None and dur > 0:
                        fermatas.append((q0 + start / div, dur / div))  # 쉼표 위 페르마타도
                    p = e.find("pitch")
                    if p is not None:
                        midi = _pitch_midi(p) + transpose
                        tie_stop = any(t.get("type") == "stop" for t in e.findall("tie"))
                        artic = frozenset(a.tag for a in e.iterfind("notations/articulations/*")
                                          if a.tag in _ARTICULATIONS)
                        if e.find("notations/fermata") is not None:
                            artic = artic | {"fermata"}
                        # 기타 특수 주법(techniques.py) — 하모닉스는 소리 높이를 midi에, 글리산도는 시작·끝 짝 표시
                        string_no = (e.findtext("notations/technical/string") or "").strip()
                        if string_no in ("1", "2", "3", "4"):
                            artic = artic | {"str_hi"}    # 1~4번 줄 — 단음으로 이어지면 첼로 같은 소리(_apply_cello_runs)
                        elif string_no in ("5", "6"):
                            artic = artic | {"str_lo"}    # 5·6번 줄 — 묵직한 저음(_apply_cello_runs)
                        harm = e.find("notations/technical/harmonic")
                        if harm is not None:
                            artic = artic | {"harmonic"}
                            if harm.find("sounding-pitch") is None and harm.find("touching-pitch") is None:
                                # <fret>이 있으면 적힌 음 = 그 프렛을 누른 음으로 보고 개방현 = 적힌 음 − 프렛 위에
                                # 하모닉스 마디 간격을 더함(사용자 2026-10-12: 7프렛은 개방현보다 1옥타브+완전5도,
                                # 5프렛은 2옥타브 높음). 프렛 정보가 없으면 예전처럼 적힌 음 + 12(12프렛).
                                try:
                                    fret = int((e.findtext("notations/technical/fret") or "").strip())
                                except ValueError:
                                    fret = None
                                if fret in HARMONIC_ABOVE_OPEN:
                                    midi += HARMONIC_ABOVE_OPEN[fret] - fret
                                else:
                                    midi += 12   # 적힌 음(줄 높이)보다 한 옥타브 위가 나는 12프렛 하모닉스
                        for gl in e.iterfind("notations/*"):
                            if gl.tag in ("glissando", "slide") and gl.get("type") in ("start", "stop"):
                                kind = "glide" if gl.get("type") == "start" else "glide_end"
                                artic = artic | {(kind, gl.get("number", "1"))}
                        # 꾸밈음이 앞에 있었으면 그만큼 늦게 시작(화음 음표도 같이)
                        nq, ndur = q0 + start / div + grace_shift, dur / div - grace_shift
                        orn = e.find("notations/ornaments")
                        if PLAY_TRILLS and orn is not None and orn.find("trill-mark") is not None and ndur > 0:
                            upper = _trill_upper_midi(p, fifths, orn) + transpose
                            _place_trill(nq, ndur, midi, upper, _bpm_at(tempos or metronome_tempos, nq),
                                         pi, not tie_stop, artic, notes)
                        elif orn is not None and ndur > 0 and (orn.find("inverted-mordent") is not None
                                                               or orn.find("mordent") is not None):
                            tag = "inverted-mordent" if orn.find("inverted-mordent") is not None else "mordent"
                            neighbor = _trill_upper_midi(p, fifths, orn, 1 if tag == "inverted-mordent" else -1) + transpose
                            _place_mordent(nq, ndur, midi, neighbor, _bpm_at(tempos or metronome_tempos, nq),
                                           pi, not tie_stop, artic, notes, MORDENT_Q[tag])
                        elif orn is not None and ndur > 0 and (orn.find("turn") is not None
                                                               or orn.find("inverted-turn") is not None):
                            inverted = orn.find("inverted-turn") is not None
                            up = _trill_upper_midi(p, fifths, orn, 1) + transpose
                            down = _trill_upper_midi(p, fifths, orn, -1) + transpose
                            first, second = (down, up) if inverted else (up, down)
                            _place_turn(nq, ndur, midi, first, second, _bpm_at(tempos or metronome_tempos, nq),
                                        pi, not tie_stop, artic, notes)
                        else:
                            trem = orn.find("tremolo") if orn is not None else None
                            ttype = (trem.get("type") or "single") if trem is not None else None
                            try:
                                slashes = int((trem.text or "3").strip()) if trem is not None else 0
                            except ValueError:
                                slashes = 3
                            if trem is not None and ttype == "single" and ndur > 0 and slashes > 0:
                                _place_tremolo(nq, ndur, [[midi]], _tremolo_step(slashes), pi, not tie_stop, artic, notes)
                            elif trem is not None and ttype == "start" and ndur > 0:
                                if trem2 is not None and e.find("chord") is None:
                                    flush_trem2()
                                if trem2 is None:
                                    trem2 = {"q": nq, "end": nq + ndur, "a": [], "b": [], "step": _tremolo_step(slashes),
                                             "art": artic, "onset": not tie_stop}
                                trem2["a"].append(midi)
                            elif trem is not None and ttype == "stop" and trem2 is not None:
                                trem2["b"].append(midi)
                                trem2["end"] = max(trem2["end"], nq + ndur)
                            else:
                                notes.append((nq, ndur, midi, pi, not tie_stop, artic))
                longest = max(longest, cur)
            length_q = longest / div if longest else beats * 4.0 / beat_type
            if trem2 is not None and trem2["b"]:
                flush_trem2()
            if pi == 0:
                measures.append({"number": _measure_label(m.get("number")), "start_q": q0, "length_q": length_q,
                                 "beats": beats, "beat_type": beat_type,
                                 "repeat_start": repeat_start, "repeat_end_times": repeat_end_times,
                                 "_ending_events": ending_events, **jumps})
                q0 += length_q
            else:
                # 다른 파트는 첫 파트의 마디 시작을 따른다(마디 수가 같다고 가정)
                q0 = measures[mi + 1]["start_q"] if mi + 1 < len(measures) else q0 + length_q
    # 1·2번 괄호: "시작"부터 "끝"까지(둘 다 포함) 사이 모든 마디에 번호 집합을 단다.
    current_ending = None
    for m in measures:
        for nums, etype in m["_ending_events"]:
            if etype == "start":
                current_ending = frozenset(nums)
        m["ending"] = current_ending
        for nums, etype in m["_ending_events"]:
            if etype in ("stop", "discontinue"):
                current_ending = None
        del m["_ending_events"]
    if not tempos:
        tempos = metronome_tempos
    tempos.sort()
    if not tempos or tempos[0][0] > 0:
        tempos.insert(0, (0.0, tempos[0][1] if tempos else 100.0))
    tempos = _shape_tempos(tempos, [w for w in words if w[1] == 0], fermatas, measures)
    # 같은 자리 기호가 둘이면 나중에 적힌 것이 이기도록 위치·성부로만 정렬(안정 정렬 — 세기 크기로
    # 정렬하면 "f 다음 p"에서 f가 남았다)
    dynamics.sort(key=lambda d: (d[0], d[1]))
    dynamics = sorted(dynamics + _dynamic_ramps(dynamics, wedges, words, measures), key=lambda d: (d[0], d[1]))
    notes = _apply_techniques(notes, tech_changes)
    notes = _apply_rasgueado(notes, rasg_marks, measures)
    notes = _apply_cello_runs(notes)
    return {"measures": measures, "notes": notes, "tempos": tempos, "dynamics": dynamics,
            "parts": [part_names.get(p.get("id"), p.get("id")) for p in parts],
            "part_abbrevs": [part_abbrevs.get(p.get("id"), "") for p in parts],
            "instruments": [part_instruments.get(p.get("id"), {"name": "", "program": None})
                            for p in parts]}


# ---------------------------------------------------------------- 1b. 도돌이 펼치기

def _resolve_play_order(measures):
    """도돌이(backward repeat)·1/2번 괄호(ending)·D.C./D.S./Coda/Segno/Fine을 따라
    실제 연주되는 마디 인덱스 순서를 만든다. 흔한 단일 구조(겹친 도돌이, 중첩 안 된
    괄호, 점프 한 번)만 다룬다.

    규칙(흔히 쓰는 대로):
    - backward repeat는 가장 가까운 앞쪽 forward repeat(없으면 처음)로 되돌아간다 —
      그 구간은 기본 2번(= 1번 되돌아감), times="n"이면 n번 연주.
    - 괄호 번호는 "지금까지 그 되돌아감이 몇 번째냐"(1부터)와 맞는 마디만 연주하고
      안 맞으면 건너뛴다(예: 1번 괄호는 첫 번째만, 2번 괄호는 그다음부터).
    - 한 마디에 backward repeat와 dacapo/dalsegno가 같이 있으면(흔함 — "반복 다
      끝나면 처음으로") repeat가 먼저 소진된 뒤에야 dacapo/dalsegno가 작동한다.
    - D.C./D.S.는 한 번만 작동한다(두 번째로 그 마디에 닿으면 그냥 지나간다).
    - fine/tocoda는 D.C./D.S.가 이미 한 번 일어난 뒤에만 유효하다(그 전엔 지나친다
      — 흔히 Fine이 1절 끝에 적혀 있어도 2절(D.C. 뒤)에만 거기서 끝나야 하므로).
    """
    n = len(measures)
    segno_idx = next((i for i, m in enumerate(measures) if m.get("segno")), None)
    coda_idx = next((i for i, m in enumerate(measures) if m.get("coda")), None)

    order = []
    taken = {}          # repeat_end 마디 인덱스 -> 이미 되돌아간 횟수
    pass_no = {}         # repeat_end 마디 인덱스 -> 그 되돌아감이 지금 몇 번째 통과인지(괄호용)
    last_start = 0
    jumped = False
    i = 0
    guard = 0
    while i < n and guard < 4 * n + 20:   # 무한루프 방지(악보가 이상하면 일찍 끝냄)
        guard += 1
        m = measures[i]
        cur_pass = pass_no.get(_nearest_repeat_end(measures, i), 1)
        if m.get("ending") is not None and cur_pass not in m["ending"]:
            i += 1
            continue  # 지금 통과 번호와 안 맞는 괄호 — 건너뜀(연주 안 함)
        if jumped and m.get("tocoda") and coda_idx is not None:
            # "To Coda"는 보통 그 마디 맨 앞에서 바로 건너뛰는 자리라(마디 내용을 듣고
            # 넘어가는 게 아니라), order에 넣지 않고 바로 코다로 점프한다.
            i = coda_idx
            continue
        order.append(i)
        if jumped and m.get("fine"):
            break
        if m.get("repeat_end_times") and taken.get(i, 0) < m["repeat_end_times"] - 1:
            taken[i] = taken.get(i, 0) + 1
            pass_no[i] = taken[i] + 1
            i = last_start
            continue
        if not jumped and m.get("dacapo"):
            jumped = True
            i = 0
            continue
        if not jumped and m.get("dalsegno") and segno_idx is not None:
            jumped = True
            i = segno_idx
            continue
        if m.get("repeat_start"):
            last_start = i
        i += 1
    return order


def _nearest_repeat_end(measures, i):
    """i가 속한 괄호(ending)의 "몇 번째 통과냐"를 알려줄 backward-repeat 마디 인덱스를
    찾는다. 1번 괄호는 보통 그 되돌아감 표시를 직접 갖고 있어 자기 자신이 바로 잡히지만,
    2번 괄호(되돌아간 뒤의 마디들)는 자기 자신엔 표시가 없고 "방금 지나온" 되돌아감이
    그 통과 번호를 정하므로, 뒤가 아니라 **앞(자기 포함)** 을 먼저 찾는다 — 못 찾으면
    (드묾) 뒤도 찾아본다."""
    for j in range(i, -1, -1):
        if measures[j].get("repeat_end_times"):
            return j
    for j in range(i, len(measures)):
        if measures[j].get("repeat_end_times"):
            return j
    return i


def expand_performance_order(score):
    """도돌이·엔딩·D.S./D.C./Coda/Segno/Fine을 실제 연주 순서로 펼친 새 score를 만든다 —
    반환 모양이 read_musicxml과 같아서(measures/notes/tempos/parts) nominal_seconds를
    손 안 대고 그대로 쓸 수 있다. 펼친 각 마디는 원래 마디 정보(orig_index, number,
    occurrence — 몇 번째 연주인지)를 같이 가진다.

    도돌이 표시가 하나도 없으면(압도적으로 흔한 경우) score를 그대로 돌려준다(복사 없음)."""
    measures = score["measures"]
    if not any(m.get("repeat_end_times") or m.get("dacapo") or m.get("dalsegno") for m in measures):
        return score
    play_order = _resolve_play_order(measures)

    new_measures = []
    occurrence = {}
    q_cursor = 0.0
    for orig_idx in play_order:
        m = measures[orig_idx]
        occurrence[orig_idx] = occurrence.get(orig_idx, 0) + 1
        new_measures.append({
            "number": m["number"], "start_q": q_cursor, "length_q": m["length_q"],
            "beats": m["beats"], "beat_type": m["beat_type"],
            "orig_index": orig_idx, "occurrence": occurrence[orig_idx],
        })
        q_cursor += m["length_q"]

    def _shift_events(events):
        out = []
        for new_m, orig_idx in zip(new_measures, play_order):
            orig_m = measures[orig_idx]
            shift = new_m["start_q"] - orig_m["start_q"]
            lo, hi = orig_m["start_q"], orig_m["start_q"] + orig_m["length_q"]
            for ev in events:
                q = ev[0]
                if lo <= q < hi:
                    out.append((q + shift,) + tuple(ev[1:]))
        return out

    new_notes = _shift_events(score["notes"])
    new_tempos = _shift_events(score["tempos"])
    new_tempos.sort()
    if not new_tempos or new_tempos[0][0] > 0:
        new_tempos.insert(0, (0.0, new_tempos[0][1] if new_tempos else 100.0))
    new_dynamics = _carry_dynamics(score.get("dynamics", []), measures, new_measures, play_order)
    return {**score, "measures": new_measures, "notes": new_notes, "tempos": new_tempos,
            "dynamics": new_dynamics}


def _carry_dynamics(dynamics, measures, new_measures, play_order):
    """셈여림을 펼친 순서로 옮긴다. 마디별로 옮기기만 하면 도돌이로 되돌아간 자리에서
    "지금 셈여림"이 틀어진다(예: 곡 끝이 f인데 처음으로 돌아가면, 처음 마디의 p 표시가
    마디 맨 앞이 아니라 중간에 있을 경우 그 앞까지 f가 이어짐). 그래서 펼친 각 마디
    시작에 "인쇄 순서상 그 마디 직전까지 유효했던 셈여림"을 성부마다 다시 찍어 둔다."""
    if not dynamics:
        return []
    n_parts = max(d[1] for d in dynamics) + 1
    out = []
    for new_m, orig_idx in zip(new_measures, play_order):
        orig_m = measures[orig_idx]
        shift = new_m["start_q"] - orig_m["start_q"]
        lo, hi = orig_m["start_q"], orig_m["start_q"] + orig_m["length_q"]
        for pi in range(n_parts):
            prev = [d for d in dynamics if d[1] == pi and d[0] < lo]
            if prev:
                out.append((new_m["start_q"], pi, prev[-1][2]))
        for q, pi, vel in dynamics:
            if lo <= q < hi:
                out.append((q + shift, pi, vel))
    out.sort()
    return out


def nominal_seconds(score, q):
    """4분음표 위치 q(배열 가능) → 악보 빠르기대로 연주한 명목 시각(초)."""
    tq = np.array([t[0] for t in score["tempos"]] + [1e12])
    bpm = np.array([t[1] for t in score["tempos"]])
    seg_sec = np.diff(tq[:-1]) * 60.0 / bpm[:-1]
    start_sec = np.concatenate([[0.0], np.cumsum(seg_sec)])
    q = np.asarray(q, dtype=float)
    i = np.clip(np.searchsorted(tq, q, side="right") - 1, 0, len(bpm) - 1)
    return start_sec[i] + (q - tq[i]) * 60.0 / bpm[i]


def beat_positions(score):
    """박(박자표 분모 단위) 위치들 [(q, 마디 인덱스, 마디 안 몇 번째 박(1부터))]."""
    out = []
    for mi, m in enumerate(score["measures"]):
        step = 4.0 / m["beat_type"]
        n = max(1, int(round(m["length_q"] / step)))
        for k in range(n):
            out.append((m["start_q"] + k * step, mi, k + 1))
    return out
