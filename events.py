# -*- coding: utf-8 -*-
"""악보(score) → 합성용 연주 이벤트 — UI 독립.

musicxml_reader가 돌려준 score는 4분음표 단위(q)·붙임줄 조각·셈여림 표시가 따로따로다.
신시사이저는 "몇 초에, 몇 초 동안, 어떤 높이를, 얼마나 세게"만 알면 되므로 여기서 한 번에
정리한다:

1. 도돌이·D.C. 등을 실제 연주 순서로 펼친다(expand_performance_order).
2. 붙임줄(is_onset=False)로 이어진 조각을 앞 음에 합쳐 한 음으로 만든다.
3. 그 성부의 "지금 셈여림"과 악센트로 세기(velocity)를 정한다.
4. q를 악보 빠르기대로 초로 바꾸고(nominal_seconds), 빠르기 배율로 나눈다.

스타카토는 소리 나는 길이(sound_sec)만 줄이고 박자 위치(dur_sec)는 그대로 둔다 —
dur_sec는 "악보상 길이", sound_sec는 "실제로 소리를 유지할 길이"다.
"""
from dataclasses import dataclass

import numpy as np

from musicxml_reader import DEFAULT_VELOCITY, expand_performance_order, nominal_seconds

# 아티큘레이션별 소리 길이 비율(악보 길이 대비)
_GATE = {"staccatissimo": 0.25, "staccato": 0.5, "spiccato": 0.4}
_ACCENT_BOOST = {"accent": 16, "strong-accent": 24}
# 떤꾸밈·잔꾸밈의 첫 음 뒤 음들은 이만큼 약하게 — 본음이 묻히지 않게(기타의 해머링·풀링 느낌)
_TRILL_SOFTEN = 20
# 꾸밈음은 짧아 묻히기 쉬워 이만큼 세게(이음줄로 묶인 빠른 음들이 뭉개져 안 들린다는 지적, 2026-10-12)
_GRACE_BOOST = 12
# 글리산도를 더 두드러지게(사용자 요청 2026-10-16, 특히 반박자): 시작 음을 세게, 짧은 음은 거의 처음부터 미끄러짐
_GLIDE_BOOST = 7        # 14 → 7 (2026-10-16, 사용자: 조금 약하게)
_GLIDE_SHORT_SEC = 0.5      # 시작 음이 이 길이(초) 이하면 "짧은 글리산도" — 길이의 90%를 미끄러지는 데 씀
# 붙임줄 이음 판정 허용 오차(4분음표 단위) — 셋잇단 등 부동소수 반올림 흡수
_TIE_EPS = 1e-3


@dataclass
class NoteEvent:
    start_sec: float   # 소리 시작(초, 곡 처음 기준)
    dur_sec: float     # 악보상 길이(초)
    sound_sec: float   # 실제로 소리를 유지할 길이(초) — 스타카토면 짧아짐
    midi: int          # 소리 높이(MIDI 번호, 이조 반영됨)
    part: int          # 성부 번호(0부터)
    velocity: int      # 세기 1~127
    start_q: float     # 펼친 악보 기준 위치(4분음표 단위) — 악보 표시 연동용
    pizz: bool = False  # pizz. 구간 — 합성 때 약음(짧게 막힌) 소리로
    # 기타 특수 주법(techniques.py) — render가 보고 그 주법 소리로 합성
    harmonic: bool = False        # 12프렛 하모닉스(midi는 이미 소리 높이)
    rasg: bool = False            # 라스게아도 화음의 한 음(같은 성부·같은 시각끼리 묶어 e-a-m-i로)
    low: bool = False             # 5·6번 줄(또는 줄 표시 없는 D3 아래) — 거친 소리를 줄인 묵직한 저음(synth.low_timbre)
    cello: bool = False           # 4번 줄 단음 멜로디 — 첼로처럼 깊은 비브라토·중후한 소리(synth.cello_timbre)
    glide_to: int = None          # 글리산도 마지막 도착 음(None이면 없음) — 끝 음은 따로 뜯지 않고 이 음에 합침
    glide_at: float = 0.0         # 첫 미끄러짐이 시작하는 때(이 음 시작 기준 초)
    glide_sec: float = 0.0        # 첫 미끄러짐 시간(초)
    glide_path: list = None       # [(미끄럼 시작 초, 시간 초, 도착 음 MIDI), ...] — 이어진 글리산도는 여러 개(이 음 시작 기준)


def _merge_ties(notes):
    """붙임줄 조각을 앞 음에 합친다. 반환: [[start_q, dur_q, midi, part, artic], ...]
    (붙임줄 시작 음의 아티큘레이션을 쓴다). 이을 앞 음을 못 찾으면(도돌이 경계에서
    끊긴 붙임줄 등) 새 음으로 친다."""
    merged = []
    open_by_key = {}  # (part, midi) -> merged 안의 마지막 음 인덱스
    for start_q, dur_q, midi, part, is_onset, artic in sorted(notes, key=lambda n: (n[0], n[3], n[2])):
        key = (part, midi)
        idx = open_by_key.get(key)
        if not is_onset and idx is not None:
            prev = merged[idx]
            if abs(prev[0] + prev[1] - start_q) < _TIE_EPS:
                prev[1] = start_q + dur_q - prev[0]
                continue
        open_by_key[key] = len(merged)
        merged.append([start_q, dur_q, midi, part, artic])
    return merged


def _merge_glides(merged):
    """글리산도 시작 음(("glide", n))에 같은 성부의 다음 끝 음(("glide_end", n), 시작 음이 끝나는 자리에서 시작)을 합친다 —
    끝 음은 따로 뜯지 않고(사용자 요청 2026-10-16: "글리산도 마지막 음을 없앰") 시작 음이 끝 음 높이로 미끄러져 그 길이까지 울린다.
    끝 음이 다시 글리산도 시작이면(A→B♭→A 같은 연쇄) 이어서 따라가 하나의 소리로 합친다. 도착 정보는 6번째 값
    {"path": [(도착 음 시작 q, 도착 음 MIDI), ...]}, 길이(2번째 값)는 마지막 끝 음이 끝나는 곳까지 늘린다.
    짝을 못 찾으면 보통 음."""
    out, used = [], set()
    by_part = {}
    for i, m in enumerate(merged):
        by_part.setdefault(m[3], []).append(i)
    for i, m in enumerate(merged):
        if i in used:
            continue
        m = m + [None] if len(m) == 5 else m
        cur, k, path = m, i, []
        last_end = m[0] + m[1]
        while True:
            nums = [a[1] for a in cur[4] if isinstance(a, tuple) and a[0] == "glide"]
            if not nums:
                break
            end = cur[0] + cur[1]
            nxt = None
            for j in by_part[cur[3]]:
                b = merged[j]
                if j == k or j in used or b[0] < end - _TIE_EPS:
                    continue
                if abs(b[0] - end) < _TIE_EPS and any(isinstance(a, tuple) and a[0] == "glide_end" and a[1] in nums
                                                      for a in b[4]) and b[2] != cur[2]:
                    nxt = j
                    break
            if nxt is None:
                break
            b = merged[nxt]
            used.add(nxt)
            path.append((b[0], b[2]))
            last_end = b[0] + b[1]
            cur, k = b, nxt
        if path:
            m = [m[0], last_end - m[0], m[2], m[3], m[4], {"path": path}]
        out.append(m)
    return out


def _velocity_lookup(dynamics, n_parts):
    """성부별 (q 배열, 세기 배열) — q 시점의 셈여림을 찾는 데 쓴다."""
    table = []
    for pi in range(n_parts):
        pts = [(q, v) for q, p, v in dynamics if p == pi]
        table.append((np.array([q for q, _ in pts]), np.array([v for _, v in pts])))
    return table


def _velocity_at(table, part, q):
    qs, vs = table[part]
    if len(qs) == 0:
        return DEFAULT_VELOCITY
    i = np.searchsorted(qs, q + 1e-9, side="right") - 1
    return int(vs[i]) if i >= 0 else DEFAULT_VELOCITY


def build_events(score, tempo_scale=1.0, expand=True):
    """score → (NoteEvent 목록(시작 시각순), 펼친 score). tempo_scale=1.2면 악보 빠르기의
    1.2배로(시간은 1/1.2). 펼친 score는 마디 시각 계산(measure_times)에 그대로 쓴다."""
    played = expand_performance_order(score) if expand else score
    merged = _merge_glides(_merge_ties(played["notes"]))
    vel_table = _velocity_lookup(played.get("dynamics", []), len(played["parts"]))
    if not merged:
        return [], played
    starts = np.array([m[0] for m in merged])
    ends = np.array([m[0] + m[1] for m in merged])
    start_sec = nominal_seconds(played, starts) / tempo_scale
    end_sec = nominal_seconds(played, ends) / tempo_scale
    events = []
    glides = [m[5] for m in merged]
    for m, s0, s1, g in zip(merged, start_sec, end_sec, glides):
        start_q, _, midi, part, artic = m[:5]
        vel = _velocity_at(vel_table, part, start_q)
        vel += max((_ACCENT_BOOST.get(a, 0) for a in artic), default=0)
        if g:
            vel += _GLIDE_BOOST
        if "grace" in artic:
            vel += _GRACE_BOOST
        if "trill" in artic or "ornament" in artic:
            vel -= _TRILL_SOFTEN
        gate = min((_GATE.get(a, 1.0) for a in artic), default=1.0)
        dur = float(s1 - s0)
        events.append(NoteEvent(
            start_sec=float(s0), dur_sec=dur, sound_sec=dur * gate, midi=int(midi),
            part=int(part), velocity=int(np.clip(vel, 1, 127)), start_q=float(start_q),
            pizz="pizz" in artic, harmonic="harmonic" in artic, rasg="rasg" in artic,
            cello="cello" in artic, low="low" in artic,
        ))
        if g:
            # 각 도착 음 시작에 닿도록 그 직전에 미끄러짐 — 반음당 약 65ms(0.15~0.6초), 앞 음 길이의 8할 이하.
            # 짧은 음(반박자 등)은 길이의 9할을 미끄러져서 거의 처음부터 "쓰윽" 하고 들리게 한다.
            sec_pts = np.atleast_1d(nominal_seconds(played, np.array([q for q, _ in g["path"]]))) / tempo_scale
            segs, prev_t, prev_pitch = [], float(s0), int(midi)
            for (q_arr, to), t_arr in zip(g["path"], sec_pts):
                hold_total = max(0.02, float(t_arr) - prev_t)
                if hold_total <= _GLIDE_SHORT_SEC:
                    glide = 0.9 * hold_total
                else:
                    glide = min(float(np.clip(0.065 * abs(int(to) - prev_pitch), 0.15, 0.6)), 0.8 * hold_total)
                segs.append((prev_t - float(s0) + hold_total - glide, glide, int(to)))
                prev_t, prev_pitch = float(t_arr), int(to)
            ev = events[-1]
            ev.glide_path = segs
            ev.glide_to, ev.glide_at, ev.glide_sec = segs[-1][2], segs[0][0], segs[0][1]
    events.sort(key=lambda e: (e.start_sec, e.part, e.midi))
    return events, played


def measure_times(played, tempo_scale=1.0):
    """펼친 score의 마디별 (시작 초, 끝 초) 배열 — 재생 위치 ↔ 마디 대응용."""
    ms = played["measures"]
    starts = np.array([m["start_q"] for m in ms])
    ends = starts + np.array([m["length_q"] for m in ms])
    return nominal_seconds(played, starts) / tempo_scale, nominal_seconds(played, ends) / tempo_scale


def total_seconds(played, tempo_scale=1.0):
    """곡 전체 길이(초, 펼친 순서 기준)."""
    if not played["measures"]:
        return 0.0
    last = played["measures"][-1]
    return float(nominal_seconds(played, last["start_q"] + last["length_q"])) / tempo_scale
