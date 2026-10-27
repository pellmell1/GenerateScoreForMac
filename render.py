# -*- coding: utf-8 -*-
"""곡 전체를 소리로 — 성부별 합성 → 믹스(음량·좌우·음소거·독주) → WAV 저장. UI 독립.

성부별 버퍼를 따로 들고 있는 이유: 음소거·음량·좌우를 바꿀 때 다시 합성하지 않고
믹스만 다시 하면 된다(합성은 음 수천 개라 수 초, 믹스는 순식간). 다시 합성이 필요한
건 빠르기·음색을 바꿀 때뿐이다.

재생은 믹스 결과를 통째로 만들어 두지 않고 iter_mix가 조각(0.1초)마다 그때그때 섞는다 —
긴 곡(6분)의 스테레오 버퍼만 135MB라 아끼려는 것도 있고, 조각마다 설정을 새로 읽으므로
재생 중에 음소거·음량을 바꿔도 다시 시작할 필요 없이 바로(출력 대기분 뒤) 반영된다.

기타답게 하는 성부 단위 효과(2026-10-07, synth.TIMBRES의 body/reverb/ring 값):
- 울림 남기기(ring): 악보 길이가 끝나도 조금 더 울리게 — 실제 기타는 왼손을 떼기 전까지
  줄이 계속 울린다. 같은 성부에서 같은 높이가 다시 나오면(같은 줄을 다시 뜯음) 거기서 끊는다.
- 사람 손 느낌: 시작 시각을 몇 ms, 세기를 조금씩 무작위로 흔든다(화음이 살짝 흩어져
  손가락으로 뜯은 느낌). 같은 곡이면 늘 같게(난수 씨앗 고정).
- 몸통 공명(body): 기타 몸통의 공명 주파수(공기 공명 ~100Hz, 앞판 ~200Hz 등)에서
  울리는 짧은 충격 응답을 만들어 성부 전체에 곱한다(컨볼루션). "줄 소리"가 "기타 소리"가
  되는 가장 큰 요인.
- 방 울림(reverb): 작은 홀 잔향(약 1.4초) 충격 응답을 합성해 컨볼루션. 높은 소리는 더
  빨리 사라지게 두 대역으로 나눠 만든다. 성부 버퍼(모노) 안에 섞어 넣는다 — 성부별로 따로
  두면 음소거해도 그 성부 잔향까지 같이 꺼져서 자연스럽다(메모리도 성부 하나 분량 그대로).

전체 음량(master_gain)은 "모든 성부를 기본 설정으로 켰을 때" 기준으로 한 번 정한다 —
성부를 끌 때마다 다시 정규화하면 남은 성부가 갑자기 커져서 연습할 때 어색하다.
"""
import threading
from dataclasses import dataclass, replace

import numpy as np
import soundfile as sf
from scipy.signal import oaconvolve

from events import build_events, measure_times, total_seconds
from musicxml_reader import nominal_seconds, read_musicxml
import techniques
from synth import SR, TIMBRES, NoteSynth, cello_timbre, low_timbre, pizz_timbre, timbre_for_part

_TAIL_SEC = 2.5          # 마지막 음 뒤 여운(잔향 포함)
_GAIN_PERCENTILE = 99.5   # master_gain 기준 — 블록 최대치 중 상위 몇 %
_MAX_OVER = 2.2           # 가장 큰 순간이 목표 최대치의 몇 배까지 리미터에 맡길지
_BALANCE_STRENGTH = 0.6   # 성부 음량 차이를 얼마나 줄일지(1이면 완전히 같게)
_UPPER_BOOST = 1.3        # 1·2번 성부 추가 배율
_HEADROOM = 0.89        # 정규화 목표 최대치(약 -1dBFS)
_LIMIT_KNEE = 0.9        # 이 위로는 부드럽게 눌러서 찢어짐 방지

# 메트로놈 딸깍 — 마디 첫 박은 높게
_CLICK_HZ_DOWN = 1760.0
_CLICK_HZ_BEAT = 1320.0
_CLICK_SEC = 0.05
# 딸깍 소리 크기(최대치). 처음엔 0.5×믹스 0.45 = 0.22라 음악(최대 0.89)에 묻혔다(사용자 지적
# 2026-10-07) → 1.0. 음악과 합쳐 넘치는 부분은 mix의 부드러운 리미터가 누른다.
_CLICK_LEVEL = 1.0


def _click_wave(hz, sr):
    """메트로놈 딸깍 하나 — 나무 블록처럼: 짧은 잡음 어택 + 빨리 사라지는 두 배음."""
    n = int(_CLICK_SEC * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(int(hz))
    tone = (np.sin(2 * np.pi * hz * t) + 0.5 * np.sin(2 * np.pi * hz * 2.76 * t)) * np.exp(-t / 0.012)
    noise = rng.standard_normal(n) * np.exp(-t / 0.0015) * 0.6
    w = tone + noise
    return (w / np.max(np.abs(w)) * _CLICK_LEVEL).astype(np.float32)

_BLOCK = SR // 10        # 믹스 조각 크기(0.1초) — 재생 중 설정 변경이 반영되는 단위

# 사람 손 느낌
_HUMANIZE_SEC = 0.006    # 시작 시각 흔들기(표준편차, 초)
_HUMANIZE_MAX_SEC = 0.015
_HUMANIZE_VEL = 3.0      # 세기 흔들기(표준편차)
_HUMANIZE_SEED = 7

# 몸통 공명 모드 (주파수 Hz, 감쇠 시간 초, 크기) — 클래식 기타 실측 문헌값 언저리
BODY_MODES = {
    "guitar": [(100, 0.060, 0.8), (205, 0.045, 0.9), (290, 0.030, 0.35), (395, 0.030, 0.5),
               (550, 0.020, 0.35), (720, 0.015, 0.3), (1000, 0.010, 0.25), (1500, 0.007, 0.18),
               (2200, 0.005, 0.12)],
}
_BODY_IR_SEC = 0.25

# 방 울림
_REVERB_SEC = 1.6        # 충격 응답 길이
_REVERB_RT_LOW = 1.4     # 낮은 대역 잔향 시간(초, -60dB)
_REVERB_RT_HIGH = 0.6    # 높은 대역(약 3kHz 위) 잔향 시간
_REVERB_PREDELAY = 0.018


def _body_ir(kind, sr):
    """몸통 공명 충격 응답 — 모드마다 감쇠하는 사인파를 더한 것(에너지 1로 맞춤)."""
    n = int(_BODY_IR_SEC * sr)
    t = np.arange(n) / sr
    ir = np.zeros(n)
    rng = np.random.default_rng(3)
    for f, tau, g in BODY_MODES[kind]:
        ir += g * np.exp(-t / tau) * np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi))
    return ir / np.sqrt(np.sum(ir ** 2))


def _reverb_ir(sr):
    """작은 홀 잔향 충격 응답(합성) — 무작위 잡음에 지수 감쇠, 높은 대역은 더 빨리 감쇠.
    초기 반사 몇 개를 앞에 둔다. 에너지 1로 맞춤."""
    from scipy.signal import butter, sosfilt
    n = int(_REVERB_SEC * sr)
    t = np.arange(n) / sr
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(n)
    sos_lo = butter(2, 3000, "low", fs=sr, output="sos")
    sos_hi = butter(2, 3000, "high", fs=sr, output="sos")
    tail = (sosfilt(sos_lo, noise) * np.exp(-6.9 * t / _REVERB_RT_LOW)
            + 0.5 * sosfilt(sos_hi, noise) * np.exp(-6.9 * t / _REVERB_RT_HIGH))
    tail *= np.clip(t / 0.05, 0, 1)            # 잔향은 서서히 쌓이게
    ir = np.zeros(n + int(_REVERB_PREDELAY * sr))
    ir[len(ir) - n:] = tail
    for delay, g in ((0.007, 0.6), (0.011, 0.45), (0.017, 0.35), (0.023, 0.3)):   # 초기 반사
        ir[int(delay * sr)] += g * np.sqrt(np.sum(tail ** 2)) * 0.05
    return ir / np.sqrt(np.sum(ir ** 2))


_IR_CACHE = {}


def _effect_ir(timbre, sr):
    """음색 하나의 몸통 공명 + 방 울림을 합친 충격 응답(없으면 None). 둘 다 선형이라
    h = (δ + body_mix·몸통) 다음에 (δ + reverb·잔향)을 이어 건 것을 미리 하나로 합쳐 두면
    성부마다 컨볼루션이 한 번으로 준다(따로 두 번 하던 처음 판이 성부당 2.6초 → 합쳐서
    0.6초쯤, 2026-10-07 실측). 몸통·잔향 응답은 에너지 1로 맞춰 둬서 *_mix 값이 곧
    원래 소리 대비 크기다."""
    p = TIMBRES[timbre]
    key = (timbre, sr)
    if key in _IR_CACHE:
        return _IR_CACHE[key]
    h = None
    if p.get("body") and p.get("body_mix", 0) > 0:
        b = _body_ir(p["body"], sr) * p["body_mix"]
        h = b.copy()
        h[0] += 1.0
    if p.get("reverb", 0) > 0:
        rv = _reverb_ir(sr) * p["reverb"]
        rv[0] += 1.0
        h = rv if h is None else oaconvolve(h, rv)
    if h is not None:
        h = h.astype(np.float32)
    _IR_CACHE[key] = h
    return h


def apply_part_effects(buf, timbre, sr=SR):
    """성부 버퍼 하나에 몸통 공명·방 울림을 건다(길이 그대로)."""
    h = _effect_ir(timbre, sr)
    if h is None or not np.any(buf):
        return buf
    return oaconvolve(buf, h)[:len(buf)].astype(np.float32)


def _note_timbre(ev, timbres):
    """음 하나의 음색 — pizz. 구간이면 그 성부 음색의 pizz. 판, 4번 줄 단음 멜로디면 첼로 판(기타만)."""
    if ev.pizz:
        return pizz_timbre(timbres[ev.part])
    if ev.low:
        return low_timbre(timbres[ev.part])
    if ev.cello:
        return cello_timbre(timbres[ev.part])
    return timbres[ev.part]


def _ring_sounds(events, timbres):
    """울림 남기기 — 음마다 실제로 소리 낼 길이(초). 스타카토처럼 일부러 짧게 한 음은
    안 늘린다. 같은 성부·같은 높이가 다음에 나오는 곳에서는 끊는다."""
    next_same = {}
    out = [0.0] * len(events)
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        key = (ev.part, ev.midi)
        ring = TIMBRES[_note_timbre(ev, timbres)].get("ring", 0.0)
        sound = ev.sound_sec
        if ring > 0 and ev.sound_sec >= ev.dur_sec - 1e-6:
            sound = ev.sound_sec + ring
            nxt = next_same.get(key)
            if nxt is not None:
                sound = max(ev.sound_sec, min(sound, nxt - ev.start_sec))
        out[i] = sound
        next_same[key] = ev.start_sec
    return out


@dataclass
class PartSetting:
    timbre: str = "guitar"
    volume: float = 1.0   # 0~1.5
    pan: float = 0.0      # -1(왼쪽) ~ 1(오른쪽)
    mute: bool = False
    solo: bool = False


def default_settings(score):
    """성부마다 악기에 맞는 음색, 좌우는 고르게 펼쳐서."""
    n = len(score["parts"])
    pans = np.linspace(-0.5, 0.5, n) if n > 1 else [0.0]
    instruments = score.get("instruments") or [{}] * n
    return [PartSetting(timbre=timbre_for_part(score["parts"][i], instruments[i]), pan=float(pans[i]))
            for i in range(n)]


def humanize(n):
    """사람 손 느낌 — 음 n개의 (시작 시각 흔들기 초, 세기 흔들기). 씨앗 고정이라 같은 곡이면 늘 같다
    (이어 합성 TempoStream도 곡 전체 합성과 같은 값을 쓰려고 따로 뺌)."""
    rng = np.random.default_rng(_HUMANIZE_SEED)
    jitter = np.clip(rng.normal(0, _HUMANIZE_SEC, n), -_HUMANIZE_MAX_SEC, _HUMANIZE_MAX_SEC)
    return jitter, rng.normal(0, _HUMANIZE_VEL, n)


def render_parts(events, n_parts, timbres, length_sec, sr=SR, synth=None, progress=None, cancel=None):
    """성부별 모노 버퍼 목록(float32, 길이 같음). progress(완료 비율 0~1)로 진행 알림,
    cancel()이 True를 돌려주면 중간에 그만두고 None을 돌려준다."""
    synth = synth or NoteSynth(sr)
    n_total = int((length_sec + _TAIL_SEC) * sr)
    parts = [np.zeros(n_total, np.float32) for _ in range(n_parts)]
    jitter, vel_jitter = humanize(len(events))
    synth_share = 0.8  # 진행 막대: 합성 80%, 효과 20%
    ok = _synth_into(parts, events, _ring_sounds(events, timbres), jitter, vel_jitter, timbres, sr, synth,
                     progress=(lambda f: progress(synth_share * f)) if progress is not None else None,
                     cancel=cancel)
    if not ok or (cancel is not None and cancel()):
        return None
    # 성부들을 동시에(FFT는 계산 중 GIL을 놓아서 스레드로도 빨라진다)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(4, max(1, n_parts))) as ex:
        futures = [ex.submit(apply_part_effects, parts[pi], timbres[pi], sr) for pi in range(n_parts)]
        for done, pi in enumerate(range(n_parts)):
            parts[pi] = futures[pi].result()
            if progress is not None:
                progress(synth_share + (1 - synth_share) * (done + 1) / n_parts)
    return parts


def _synth_into(parts, events, sounds, jitter, vel_jitter, timbres, sr, synth, origin_sec=None, progress=None,
                cancel=None):
    """음들을 성부 버퍼(효과 전)에 더한다. sounds·jitter·vel_jitter는 events와 같은 순서.
    origin_sec: 버퍼 0번 샘플의 시각 — None이면 0이고 0보다 앞선 음은 0에서 시작(곡 전체 합성),
    주면 버퍼보다 앞서 시작한 음은 앞부분을 잘라 넣는다(이어 합성의 앞 음 여운). 취소되면 False."""
    n_total = len(parts[0]) if parts else 0
    rasg_groups, rasg_done = {}, set()
    for k, ev in enumerate(events):
        if ev.rasg:
            rasg_groups.setdefault((ev.part, round(ev.start_sec, 6)), []).append(k)
    for i, ev in enumerate(events):
        if cancel is not None and i % 64 == 0 and cancel():
            return False
        vel = int(np.clip(round(ev.velocity + vel_jitter[i]), 1, 127))
        if ev.rasg:
            # 라스게아도: 같은 성부·같은 시각 화음의 첫 음에서 한꺼번에(e-a-m-i), 나머지 음은 건너뜀
            key = (ev.part, round(ev.start_sec, 6))
            if key in rasg_done:
                continue
            rasg_done.add(key)
            group = [k for k in rasg_groups[key]]
            chord = sorted(events[k].midi for k in group)
            dur = max(sounds[k] for k in group)
            wave, _ = techniques.rasgueado(chord, max(events[k].velocity for k in group), dur, synth, sr,
                                           timbre=timbres[ev.part])
        elif ev.glide_to is not None:
            # 도착 음은 따로 뜯지 않고 한 번 뜯은 소리가 미끄러져 마지막 도착 음까지 이어 울린다(2026-10-16)
            path = ev.glide_path or [(ev.glide_at, ev.glide_sec, ev.glide_to)]
            after = max(0.05, sounds[i] - max(a + g for a, g, _ in path))
            wave, _ = techniques.glissando_path(ev.midi, path, vel, after, sr)
        elif ev.harmonic:
            wave = techniques.harmonic(ev.midi - 12, vel, sounds[i], sr)   # midi는 이미 옥타브 위 소리 높이
        else:
            wave = synth.render(ev.midi, vel, sounds[i], _note_timbre(ev, timbres))
        if origin_sec is None:
            s0 = max(0, int(round((ev.start_sec + jitter[i]) * sr)))
        else:
            s0 = int(round((ev.start_sec + jitter[i] - origin_sec) * sr))
            if s0 < 0:
                wave, s0 = wave[-s0:], 0
        s1 = min(n_total, s0 + len(wave))
        if s1 > s0:
            parts[ev.part][s0:s1] += wave[:s1 - s0]
        if progress is not None and i % 200 == 0:
            progress(i / max(1, len(events)))
    return True


# 겹박자(6/8·9/8·12/8)의 한 박 길이 선택(4분음표 몇 개): 점4분음표 1.5 / 8분음표 0.5 (사용자 요청 2026-10-13)
COMPOUND_DOTTED_QUARTER = 1.5
COMPOUND_EIGHTH = 0.5
_COMPOUND_BEATS = (6, 9, 12)


def is_compound(measure):
    """겹박자 마디인지 — 박자표가 6/8·9/8·12/8."""
    return measure["beat_type"] == 8 and measure["beats"] in _COMPOUND_BEATS


def click_unit_q(measure, compound_q=COMPOUND_DOTTED_QUARTER):
    """메트로놈 한 번의 길이(4분음표 단위) — 겹박자 마디는 compound_q(점4분음표 또는 8분음표), 나머지는 4분음표."""
    return compound_q if is_compound(measure) else 1.0


def click_track(played, tempo_scale, n_samples, sr=SR, volume=1.0, offset_sec=0.0,
                compound_q=COMPOUND_DOTTED_QUARTER):
    """4분음표마다 딸깍(마디 첫 박은 높은 음). played는 펼친 score. 박자표 단위(2/2의 2분음표)가 아니라
    늘 4분음표 단위로 치되(사용자 요청 2026-10-07), 6/8·9/8·12/8은 compound_q 단위(점4분음표 또는 8분음표)로 친다."""
    out = np.zeros(n_samples, np.float32)
    n_click = int(_CLICK_SEC * sr)
    clicks = {hz: _click_wave(hz, sr) * np.float32(volume) for hz in (_CLICK_HZ_DOWN, _CLICK_HZ_BEAT)}
    for m in played["measures"]:
        unit = click_unit_q(m, compound_q)
        n_beats = max(1, int(np.ceil(m["length_q"] / unit - 1e-6)))
        qs = m["start_q"] + np.arange(n_beats) * unit
        secs = nominal_seconds(played, qs) / tempo_scale - offset_sec
        for k, sec in enumerate(np.atleast_1d(secs)):
            c = clicks[_CLICK_HZ_DOWN if k == 0 else _CLICK_HZ_BEAT]
            s0 = int(round(sec * sr))
            s1 = min(n_samples, s0 + n_click)
            if s1 > s0:
                out[s0:s1] += c[:s1 - s0]
    return out


def master_gain(parts, settings):
    """모든 성부를 켰을 때(음소거·독주 무시) 최대치가 _HEADROOM이 되는 배율."""
    if not parts:
        return 1.0
    full = [PartSetting(timbre=s.timbre, volume=s.volume, pan=s.pan) for s in settings]
    peaks = []
    for i in range(0, len(parts[0]), _BLOCK):
        peaks.append(float(np.max(np.abs(mix(parts, full, 1.0, limit=False, start=i, end=i + _BLOCK)))))
    peaks = np.array(peaks)
    top = float(peaks.max()) if len(peaks) else 0.0
    if top <= 1e-9:
        return 1.0
    # 가장 큰 순간 하나(화음 겹침 등)에 맞추면 곡 대부분이 작아져서(1·2번 기타가 묻힘, 2026-10-12) 블록 최대치의
    # 상위 0.5% 값에 맞추고, 그 위 순간은 부드러운 리미터가 누른다(단 최대 순간이 목표의 _MAX_OVER배를 넘지 않게)
    ref = float(np.percentile(peaks, _GAIN_PERCENTILE))
    return min(_HEADROOM / max(ref, 1e-9), _HEADROOM * _MAX_OVER / top)


def balance_volumes(parts, n_min_parts=3):
    """성부별 기본 음량 배율 — 실제로 소리 나는 구간의 RMS를 서로 비슷하게 맞추고(위·아래 성부 차이를
    절반쯤 줄임), 1·2번 성부(멜로디·아르페지오)는 조금 더 키운다. 사용자 지적 2026-10-12: 1st·2nd가 너무
    작음(특히 아르페지오 — 아래 성부의 길게 울리는 화음에 묻힘)."""
    n = len(parts)
    rms = []
    for b in parts:
        act = np.abs(b) > 1e-4
        rms.append(float(np.sqrt(np.mean(b[act] ** 2))) if np.any(act) else 0.0)
    live = [r for r in rms if r > 1e-6]
    if len(live) < 2:
        return [1.0] * n
    target = float(np.exp(np.mean(np.log(live))))
    out = []
    for i, r in enumerate(rms):
        f = float(np.clip(target / r, 0.6, 2.0)) ** _BALANCE_STRENGTH if r > 1e-6 else 1.0
        if i < 2 and n >= n_min_parts:
            f *= _UPPER_BOOST
        out.append(f)
    return out


def _soft_limit(x):
    a = np.abs(x)
    over = a > _LIMIT_KNEE
    if np.any(over):
        k = 1.0 - _LIMIT_KNEE
        x[over] = np.sign(x[over]) * (_LIMIT_KNEE + k * np.tanh((a[over] - _LIMIT_KNEE) / k))
    return x


def mix(parts, settings, gain, click=None, click_volume=1.0, limit=True, start=0, end=None):
    """성부 버퍼의 [start, end) 구간 → 스테레오 (n, 2) float32. 독주가 하나라도 켜져
    있으면 독주 성부만."""
    total = len(parts[0]) if parts else 0
    end = total if end is None else min(end, total)
    start = max(0, min(start, end))
    out = np.zeros((end - start, 2), np.float32)
    any_solo = any(s.solo for s in settings)
    for buf, s in zip(parts, settings):
        if s.mute or (any_solo and not s.solo) or s.volume <= 0:
            continue
        # 일정 출력 좌우 배분(가운데에서 각 채널 -3dB)
        angle = (s.pan + 1.0) * np.pi / 4
        seg = buf[start:end]
        out[:, 0] += seg * np.float32(s.volume * gain * np.cos(angle))
        out[:, 1] += seg * np.float32(s.volume * gain * np.sin(angle))
    if click is not None and click_volume > 0:
        c = click[start:end] * np.float32(click_volume)
        out[:len(c), 0] += c
        out[:len(c), 1] += c
    return _soft_limit(out) if limit else out


def count_in(beats, beat_sec, sr=SR, volume=0.9, per_measure=None):
    """시작 전 예비박(beats번 딸깍) 스테레오 버퍼. 첫 박은 높게 — per_measure를 주면 그 박마다(마디 첫 박마다)."""
    n = int(round(beats * beat_sec * sr))
    out = np.zeros(n, np.float32)
    n_click = int(_CLICK_SEC * sr)
    for k in range(beats):
        hz = _CLICK_HZ_DOWN if k % (per_measure or beats + 1) == 0 else _CLICK_HZ_BEAT
        s0 = int(round(k * beat_sec * sr))
        s1 = min(n, s0 + n_click)
        out[s0:s1] += (_click_wave(hz, sr) * np.float32(volume))[:s1 - s0]
    return np.stack([out, out], axis=1)


def iter_mix(rendered, get_settings, get_click_volume, start_frame, end_frame=None, loop=False,
             prefix=None, loop_from=None, loop_prefix=None):
    """재생용 조각 iterator. 조각마다 get_settings()/get_click_volume()을 새로 불러서
    재생 중 바뀐 믹서 설정을 바로 반영한다. loop=True면 end_frame에 닿을 때마다
    loop_from(없으면 start_frame)으로 돌아가 끝없이 반복 — 구간 반복 중간에서 시작해도
    첫 바퀴는 거기서부터, 다음 바퀴부터 구간 처음부터. prefix(예비박 버퍼)가 있으면
    맨 앞에 한 번, loop_prefix가 있으면 구간 처음으로 돌아갈 때마다 내놓는다(반복 예비박)."""
    if prefix is not None and len(prefix):
        yield prefix
    total = len(rendered.parts[0])
    end_frame = total if end_frame is None else min(end_frame, total)
    if end_frame <= start_frame:
        return
    pos = start_frame
    while True:
        stop = min(end_frame, pos + _BLOCK)
        yield mix(rendered.parts, get_settings(), rendered.gain, rendered.click, get_click_volume(),
                  start=pos, end=stop)
        pos = stop
        if pos >= end_frame:
            if not loop:
                return
            pos = start_frame if loop_from is None else loop_from
            if loop_prefix is not None and len(loop_prefix):
                yield loop_prefix


def write_wav(path, rendered, click_volume=0.0):
    """조각 단위로 섞어 저장(긴 곡도 메모리를 적게). 확장자가 .mp3면 MP3(libsndfile 내장 인코더,
    VBR 고음질), 아니면 16비트 WAV."""
    total = len(rendered.parts[0])
    if str(path).lower().endswith(".mp3"):
        kw = dict(format="MP3", subtype="MPEG_LAYER_III")
    else:
        kw = dict(subtype="PCM_16")
    with sf.SoundFile(path, "w", samplerate=rendered.sr, channels=2, **kw) as f:
        for i in range(0, total, _BLOCK * 10):
            f.write(mix(rendered.parts, rendered.settings, rendered.gain, rendered.click, click_volume,
                        start=i, end=i + _BLOCK * 10))


class RenderedScore:
    """악보 하나를 합성한 결과 묶음 — UI와 명령줄이 같이 쓴다.
    events/played/measure_starts·ends는 빠르기 배율(tempo_scale) 기준 초."""

    def __init__(self, score, tempo_scale=1.0, settings=None, sr=SR, progress=None, cancel=None,
                 compound_q=COMPOUND_DOTTED_QUARTER):
        self.score = score
        self.compound_q = compound_q
        self.sr = sr
        self.tempo_scale = tempo_scale
        self.settings = settings or default_settings(score)
        self.events, self.played = build_events(score, tempo_scale)
        self.length_sec = total_seconds(self.played, tempo_scale)
        self.measure_starts, self.measure_ends = measure_times(self.played, tempo_scale)
        self.parts = render_parts(self.events, len(score["parts"]), [s.timbre for s in self.settings],
                                  self.length_sec, sr, progress=progress, cancel=cancel)
        self.cancelled = self.parts is None
        if self.cancelled:
            return
        if settings is None or all(s.volume == 1.0 for s in self.settings):
            # 기본 음량이면 성부 균형을 자동으로(저장된 사용자 음량이 있으면 존중). 같은 값을 매번 새로 정하므로
            # 다시 합성해도 누적되지 않는다.
            for s, v in zip(self.settings, balance_volumes(self.parts)):
                s.volume = v
        self.gain = master_gain(self.parts, self.settings)
        self.click = click_track(self.played, tempo_scale, len(self.parts[0]), sr, compound_q=compound_q)

    def set_compound_q(self, compound_q):
        """겹박자 메트로놈 단위를 바꾸고 클릭만 다시 만든다(곡을 다시 합성하지 않음)."""
        self.compound_q = compound_q
        self.click = click_track(self.played, self.tempo_scale, len(self.parts[0]), self.sr, compound_q=compound_q)

    def frame_of(self, sec):
        return int(round(max(0.0, sec) * self.sr))


class SegmentRender:
    """반복 구간만 따로 합성한 것 — 구간 빠르기(곡 빠르기와 별개, 사용자 요청 2026-10-07)용.
    곡 전체를 다시 합성하면 수 초 걸리지만 몇 마디만이면 0.5초 안팎이라, 반복 중 빠르기를 바꿔도
    거의 바로 반영된다. iter_mix·mix가 쓰는 모양(parts·gain·click·sr·frame_of)은 RenderedScore와 같다.

    played(펼친 score)의 마디 k0~k1을, 그 구간 빠르기(tempo_scale)로. 음량 기준(gain)은 곡 전체
    합성의 것을 그대로 써서 곡 재생과 크기가 같게. 구간 끝에서 울리던 소리는 잘리므로 끝 15ms를
    줄여 이음매 딸깍을 막는다."""

    def __init__(self, score, played, settings, tempo_scale, k0, k1, gain, sr=SR, synth=None,
                 compound_q=COMPOUND_DOTTED_QUARTER):
        self.sr = sr
        self.tempo_scale = tempo_scale
        self.gain = gain
        ms = played["measures"]
        q0 = ms[k0]["start_q"]
        q1 = ms[k1]["start_q"] + ms[k1]["length_q"]
        self.start_nominal = float(nominal_seconds(played, q0))
        self.end_nominal = float(nominal_seconds(played, q1))
        t0 = self.start_nominal / tempo_scale
        self.length_sec = (self.end_nominal - self.start_nominal) / tempo_scale
        events, _ = build_events(score, tempo_scale)
        seg = []
        for e in events:
            if q0 - 1e-9 <= e.start_q < q1 - 1e-9:
                seg.append(replace(e, start_sec=e.start_sec - t0))
        n = int(round(self.length_sec * sr))
        parts = render_parts(seg, len(score["parts"]), [st.timbre for st in settings], self.length_sec, sr,
                             synth=synth)
        fade = min(n, int(0.015 * sr))
        ramp = np.linspace(1.0, 0.0, fade, dtype=np.float32)
        self.parts = []
        for p_ in parts:
            p_ = p_[:n].copy()
            p_[n - fade:] *= ramp
            self.parts.append(p_)
        # 메트로놈: 구간 마디들의 4분음표마다(구간 길이만큼만)
        sub = {"measures": ms[k0:k1 + 1], "tempos": played["tempos"]}
        self.click = click_track(sub, tempo_scale, n, sr, offset_sec=t0, compound_q=compound_q)

    def frame_of(self, sec):
        return int(round(max(0.0, sec) * self.sr))

    def song_nominal(self, seg_sec):
        """구간 안 시각(초, 구간 빠르기) → 악보 빠르기 기준 곡 위치(초)."""
        return self.start_nominal + seg_sec * self.tempo_scale



# ---------------------------------------------------------------- 재생 중 빠르기 바꾸기(이어 합성)
# 사용자 요청(2026-10-27): "중간에 빠르기를 바꾸면 모든 음을 다 바꾸는 게 아니라 앞으로 나올 음들을 바꾸는 식으로
# 자연스럽게". 예전엔 곡 전체를 새 빠르기로 다시 합성(수 초)하고 재생을 다시 시작해 끊겼다.
# → 재생은 곡 전체 합성으로 하다가, 빠르기를 바꾸면 조금 뒤 박에서 이어 합성(TempoStream)으로 갈아탄다.
#   이어 합성은 4분음표 하나씩 앞으로 나올 음만 그때그때 합성하므로, 그 뒤 빠르기를 또 바꿔도 아직 합성하지
#   않은 음부터 바로 바뀐다. 이미 울린 음의 여운은 그대로 이어지고, 빠르기는 한 박 동안 부드럽게 옮겨 간다.
LIVE_WINDOW_Q = 1.0           # 이어 합성 한 번 = 4분음표 하나만큼에서 시작하는 음들
LIVE_LEAD_SEC = 0.35          # 내보낼 곳보다 이만큼 앞까지 합성해 둠(이것 + 출력 대기 0.5초 뒤부터 새 빠르기)
LIVE_RAMP_Q = 1.0             # 빠르기를 바꾸면 4분음표 하나 동안 직선으로 옮겨 감(accel./rit.처럼)
LIVE_SWITCH_AHEAD_SEC = 0.6   # 갈아타는 박은 지금 내보내는 곳보다 이만큼 뒤 — 그 사이에 이어 합성을 준비
LIVE_XFADE_SEC = 0.02         # 곡 전체 합성 → 이어 합성 겹쳐 바꾸는 길이
LIVE_XFADE_LEAD_SEC = 0.025   # 겹쳐 바꾸기는 박보다 이만큼 앞에서 시작(박의 새 음 어택 전에 끝나게)
_LIVE_RING_AHEAD_Q = 2.0      # 울림 남기기 판정(같은 높이가 다시 나오면 끊기)에 함께 보는 뒤쪽 음 범위
_LIVE_CARRY_BACK_SEC = 30.0   # 갈아탈 때 아직 울리는지 볼 앞 음 범위(명목 초)
_LIVE_RELEASE_SEC = 0.6       # 음 끝 뒤 막는 소리·글리산도 여유


def _beat_grid(played, compound_q=COMPOUND_DOTTED_QUARTER):
    """펼친 곡의 박 위치(4분음표 단위) 배열 — 메트로놈과 같은 단위."""
    out = []
    for m in played["measures"]:
        unit = click_unit_q(m, compound_q)
        n = max(1, int(np.ceil(m["length_q"] / unit - 1e-6)))
        out.extend(m["start_q"] + np.arange(n) * unit)
    return np.array(out, dtype=float)


class TempoMap:
    """명목 시각 n(악보 빠르기 100% 기준 초) → 실제 시각(초, origin = 0, 앞은 음수). 빠르기 배율 s(n)은 매듭
    사이를 직선으로 잇고(부드러운 변화) 첫 매듭 앞·마지막 매듭 뒤는 그 값 그대로. 실제 시각 = ∫ dn / s(n)."""

    def __init__(self, origin, scale):
        self.origin = float(origin)
        self.knots = [(self.origin, float(scale))]

    def scale_at(self, n):
        ks = self.knots
        if n <= ks[0][0]:
            return ks[0][1]
        for (a, sa), (b, sb) in zip(ks, ks[1:]):
            if n <= b:
                return sa + (sb - sa) * (n - a) / (b - a)
        return ks[-1][1]

    def change(self, n_from, n_to, scale):
        """n_from부터 n_to까지 그때 빠르기에서 scale로 옮겨 가고 그 뒤로 scale. n_from 앞은 그대로 둔다."""
        s_from = self.scale_at(n_from)
        ks = [k for k in self.knots if k[0] < n_from]
        self.knots = ks + [(float(n_from), s_from), (float(max(n_to, n_from + 1e-6)), float(scale))]

    @staticmethod
    def _seg(a, b, sa, sb, x):
        """매듭 a~b 구간에서 a부터 x까지 걸리는 실제 시간."""
        if abs(sb - sa) < 1e-12:
            return (x - a) / sa
        sx = sa + (sb - sa) * (x - a) / (b - a)
        return (b - a) / (sb - sa) * float(np.log(sx / sa))

    def _from_first(self, n):
        ks = self.knots
        if n <= ks[0][0]:
            return (n - ks[0][0]) / ks[0][1]
        t = 0.0
        for (a, sa), (b, sb) in zip(ks, ks[1:]):
            if n <= b:
                return t + self._seg(a, b, sa, sb, n)
            t += self._seg(a, b, sa, sb, b)
        a, sa = ks[-1]
        return t + (n - a) / sa

    def real(self, n):
        return self._from_first(n) - self._from_first(self.origin)

    def nominal(self, t):
        """실제 시각 t → 명목 시각(real의 역)."""
        T = t + self._from_first(self.origin)
        ks = self.knots
        if T <= 0:
            return ks[0][0] + T * ks[0][1]
        acc = 0.0
        for (a, sa), (b, sb) in zip(ks, ks[1:]):
            d = self._seg(a, b, sa, sb, b)
            if T <= acc + d:
                u = T - acc
                if abs(sb - sa) < 1e-12:
                    return a + u * sa
                sx = sa * float(np.exp(u * (sb - sa) / (b - a)))
                return a + (sx - sa) * (b - a) / (sb - sa)
            acc += d
        a, sa = ks[-1]
        return a + (T - acc) * sa


class TempoStream:
    """이어 합성 — 곡 위치 q0(박)부터 4분음표 하나씩, 앞으로 나올 음만 그때의 빠르기 지도(TempoMap)로 합성해
    성부 버퍼에 겹쳐 더한다. 조각마다 그 음들의 여운·잔향까지 담는다(효과가 선형이라 곡을 한꺼번에 합성한 것과
    같다). q0 앞에서 시작해 아직 울리는 음은 이전 빠르기로 다시 합성해 넣는다(갈아탈 때 이어지는 여운).
    음 목록·사람 손 흔들기·음량 기준은 곡 전체 합성(rendered)의 것을 그대로 쓴다. 실제 시각·프레임 0 = q0."""

    def __init__(self, rendered, q0, new_scale, synth=None):
        self.sr = rendered.sr
        self.gain = rendered.gain
        self.played = rendered.played
        self.compound_q = rendered.compound_q
        self.timbres = [s.timbre for s in rendered.settings]
        self.n_parts = len(self.timbres)
        self.synth = synth or NoteSynth(self.sr)
        self.base_scale = rendered.tempo_scale
        self.events = rendered.events
        self.jitter, self.vel_jitter = humanize(len(self.events))
        self._ev_q = np.array([e.start_q for e in self.events], dtype=float)
        ms = self.played["measures"]
        self.end_q = (ms[-1]["start_q"] + ms[-1]["length_q"]) if ms else 0.0
        self._beats = _beat_grid(self.played, self.compound_q)
        self._clicks = {hz: _click_wave(hz, self.sr) for hz in (_CLICK_HZ_DOWN, _CLICK_HZ_BEAT)}
        self._downbeats = {round(m["start_q"], 6) for m in ms}
        self._lock = threading.Lock()
        self.map = TempoMap(self._nom(q0), self.base_scale)
        self.target = self.base_scale
        self.q_front = float(q0)          # 여기(4분음표 위치)부터 아직 합성 안 함
        self.acc0 = 0                     # 버퍼 0번 샘플의 프레임 번호
        self.acc = [np.zeros(0, np.float32) for _ in range(self.n_parts + 1)]   # 성부들 + 메트로놈
        self.set_tempo(new_scale)
        self._carry(q0)

    def _nom(self, q):
        return float(nominal_seconds(self.played, q))

    def set_tempo(self, scale):
        """아직 합성하지 않은 곳부터 scale로(LIVE_RAMP_Q 동안 부드럽게). 다른 스레드에서 불러도 된다."""
        with self._lock:
            self.target = scale
            self.map.change(self._nom(self.q_front), self._nom(self.q_front + LIVE_RAMP_Q), scale)

    def nominal_at(self, t):
        """이어 합성 안 실제 시각 t → 곡 위치(명목 초)."""
        with self._lock:
            return self.map.nominal(t)

    # --- 합성
    def _real(self, idx):
        """곡 전체 합성의 음(빠르기 base_scale) → 지금 빠르기 지도로 바꾼 시각·길이."""
        out = []
        for i in idx:
            e = self.events[i]
            n0, n1 = e.start_sec * self.base_scale, (e.start_sec + e.dur_sec) * self.base_scale
            t0, t1 = self.map.real(n0), self.map.real(n1)
            k = (t1 - t0) / e.dur_sec if e.dur_sec > 1e-9 else self.base_scale / self.map.scale_at(n0)
            gp = [(a * k, g * k, to) for a, g, to in e.glide_path] if e.glide_path else e.glide_path
            out.append(replace(e, start_sec=t0, dur_sec=t1 - t0, sound_sec=e.sound_sec * k, glide_path=gp,
                               glide_at=e.glide_at * k, glide_sec=e.glide_sec * k))
        return out

    def _span(self, qa, qb):
        lo = int(np.searchsorted(self._ev_q, qa - 1e-9, side="left"))
        hi = int(np.searchsorted(self._ev_q, qb - 1e-9, side="left"))
        return list(range(lo, hi))

    def _render(self, idx, evs, clicks):
        """음 idx(evs = 그 음들 + 울림 판정용 뒤쪽 음을 _real로 바꾼 것, 잠금 안에서 만듦)·딸깍
        [(실제 시각, 첫 박?)]을 합성해 버퍼에 겹쳐 더한다."""
        sounds = _ring_sounds(evs, self.timbres)[:len(idx)]
        evs = evs[:len(idx)]
        sr = self.sr
        starts = [e.start_sec for e in evs] + [t for t, _ in clicks]
        if not starts:
            return
        origin = min(starts) - 2 * _HUMANIZE_MAX_SEC
        end = max([e.start_sec + snd for e, snd in zip(evs, sounds)] + [t + _CLICK_SEC for t, _ in clicks])
        n = int((end - origin + _LIVE_RELEASE_SEC + _TAIL_SEC) * sr)
        parts = [np.zeros(n, np.float32) for _ in range(self.n_parts)]
        if evs:
            _synth_into(parts, evs, sounds, self.jitter[idx], self.vel_jitter[idx], self.timbres, sr, self.synth,
                        origin_sec=origin)
            parts = [apply_part_effects(p_, t_, sr) for p_, t_ in zip(parts, self.timbres)]
        click = np.zeros(n, np.float32)
        for t, down in clicks:
            c = self._clicks[_CLICK_HZ_DOWN if down else _CLICK_HZ_BEAT]
            s0 = int(round((t - origin) * sr))
            click[s0:s0 + len(c)] += c[:max(0, n - s0)]
        self._add(int(round(origin * sr)), parts + [click])

    def _add(self, f0, bufs):
        n = len(bufs[0])
        cur_end = self.acc0 + len(self.acc[0])
        if not len(self.acc[0]):
            self.acc0, cur_end = f0, f0
        if f0 < self.acc0:
            pad = self.acc0 - f0
            self.acc = [np.concatenate([np.zeros(pad, np.float32), a]) for a in self.acc]
            self.acc0 = f0
        if f0 + n > cur_end:
            grow = f0 + n - cur_end
            self.acc = [np.concatenate([a, np.zeros(grow, np.float32)]) for a in self.acc]
        o = f0 - self.acc0
        for a, b in zip(self.acc, bufs):
            a[o:o + n] += b

    def _carry(self, q0):
        """q0 앞에서 시작해 q0에 아직 울리는 음 — 이전 빠르기 그대로 다시 합성(앞부분은 버퍼 앞에서 잘림)."""
        lo = int(np.searchsorted(self._ev_q, q0 - 1e-9, side="left"))
        n0 = self._nom(q0)
        idx = []
        for i in range(lo - 1, -1, -1):
            e = self.events[i]
            if e.start_sec * self.base_scale < n0 - _LIVE_CARRY_BACK_SEC:
                break
            idx.append(i)
        idx.sort()
        with self._lock:
            evs = self._real(idx)
            ring = TIMBRES.get("guitar", {}).get("ring", 0.0)
            idx = [i for i, e in zip(idx, evs) if e.start_sec + e.sound_sec + ring + _REVERB_SEC > -0.1]
            evs = self._real(idx + self._span(q0, q0 + _LIVE_RING_AHEAD_Q))
        self._render(idx, evs, [])

    def _next_window(self):
        with self._lock:
            qa = self.q_front
            qb = min(qa + LIVE_WINDOW_Q, self.end_q)
            if qb <= qa:
                return False
            self.q_front = qb
            idx = self._span(qa, qb)
            evs = self._real(idx + self._span(qb, qb + _LIVE_RING_AHEAD_Q))
            bs = self._beats[(self._beats >= qa - 1e-9) & (self._beats < qb - 1e-9)]
            clicks = [(self.map.real(self._nom(q)), round(q, 6) in self._downbeats) for q in bs]
        self._render(idx, evs, clicks)
        return True

    def front_frame(self):
        with self._lock:
            return int(self.map.real(self._nom(self.q_front)) * self.sr)

    def ensure(self, frame):
        """frame(이어 합성 프레임)까지 시작하는 음을 다 합성해 둔다(+ LIVE_LEAD_SEC 여유)."""
        need = frame + int(LIVE_LEAD_SEC * self.sr)
        while self.q_front < self.end_q - 1e-9 and self.front_frame() < need:
            self._next_window()

    def mix_block(self, start, n, settings, click_volume):
        """프레임 [start, start+n) 스테레오 — 곡이 끝났으면 None."""
        self.ensure(start + n)
        end_frame = self.acc0 + len(self.acc[0])
        if self.q_front >= self.end_q - 1e-9 and start >= end_frame:
            return None
        sl = []
        for a in self.acc:
            out = np.zeros(n, np.float32)
            i0, i1 = max(start, self.acc0), min(start + n, end_frame)
            if i1 > i0:
                out[i0 - start:i1 - start] = a[i0 - self.acc0:i1 - self.acc0]
            sl.append(out)
        drop = start - self.acc0 - self.sr     # 1초 넘게 지난 앞부분은 버림(메모리)
        if drop > 10 * self.sr:
            self.acc = [a[drop:].copy() for a in self.acc]
            self.acc0 += drop
        return mix(sl[:-1], settings, self.gain, sl[-1], click_volume)


class LivePlayback:
    """곡 재생 조각(iter_mix 대신) — 처음엔 곡 전체 합성(rendered)을 내보내다가 set_tempo가 오면 조금 뒤 박에서
    TempoStream으로 갈아탄다(준비는 뒤 스레드에서). 갈아탄 뒤의 빠르기 변경은 이어 합성이 바로 받는다.
    nominal_at으로 지금 들리는 곳(곡 위치, 명목 초)을 알려 준다 — 갈아탄 뒤엔 곡 전체 합성의 시각과 다르므로."""

    def __init__(self, rendered, get_settings, get_click_volume, start_frame, prefix=None):
        self.r = rendered
        self.get_settings = get_settings
        self.get_click_volume = get_click_volume
        self.start_frame = int(start_frame)
        self.prefix = prefix
        self.prefix_frames = len(prefix) if prefix is not None else 0
        self.target = rendered.tempo_scale
        self.stream = None
        self.switch_out = None        # 이어 합성으로 바뀐 출력 프레임(예비박 포함, 출력 처음 기준)
        self.switch_stream = None     # 그때의 이어 합성 프레임
        self._prep = None
        self._synth = None

    @property
    def switched(self):
        return self.stream is not None

    def set_tempo(self, scale):
        """재생 중 빠르기 바꾸기(UI 스레드에서) — 갈아탔으면 바로, 아니면 다음 조각에서 갈아탈 준비."""
        self.target = scale
        st = self.stream
        if st is not None and abs(st.target - scale) > 1e-9:
            st.set_tempo(scale)

    def nominal_at(self, out_sec):
        """출력 시작 뒤 out_sec초(예비박 포함)에 들리는 곡 위치(명목 초)."""
        f = out_sec * self.r.sr
        st = self.stream
        if st is not None and f >= self.switch_out:
            return st.nominal_at((f - self.switch_out + self.switch_stream) / self.r.sr)
        t = max(0.0, (f - self.prefix_frames) / self.r.sr)
        return (self.start_frame / self.r.sr + t) * self.r.tempo_scale

    def _switch_point(self, pos):
        """pos(곡 전체 합성 프레임) + LIVE_SWITCH_AHEAD_SEC 뒤 첫 박 → (박 q, 갈아탈 곡 전체 합성 프레임). 없으면 None."""
        r = self.r
        n_min = (pos / r.sr + LIVE_SWITCH_AHEAD_SEC) * r.tempo_scale
        beats = _beat_grid(r.played, r.compound_q)
        if not len(beats):
            return None
        ns = nominal_seconds(r.played, beats)
        k = int(np.searchsorted(ns, n_min))
        if k >= len(beats):
            return None
        f = int(round(ns[k] / r.tempo_scale * r.sr)) - int(LIVE_XFADE_LEAD_SEC * r.sr)
        return float(beats[k]), f

    def _start_prep(self, q_b):
        r, target, res = self.r, self.target, {}
        lead, xf = int(LIVE_XFADE_LEAD_SEC * r.sr), int(LIVE_XFADE_SEC * r.sr)

        def work():
            try:
                if self._synth is None:
                    self._synth = NoteSynth(r.sr)
                st = TempoStream(r, q_b, target, self._synth)
                st.ensure(-lead + xf)
                res["stream"] = st
            except Exception as e:      # 준비 실패 — 곡 전체 합성으로 계속(빠르기는 멈춘 뒤 반영)
                res["error"] = e
        th = threading.Thread(target=work, daemon=True)
        th.start()
        self._prep = (th, res)

    def chunks(self):
        r = self.r
        sr = r.sr
        if self.prefix_frames:
            yield self.prefix
        out = self.prefix_frames
        pos = self.start_frame
        total = len(r.parts[0])
        switch_at = None
        while pos < total:
            if self._prep is None and abs(self.target - r.tempo_scale) > 1e-9:
                sp = self._switch_point(pos)
                if sp is not None:
                    self._start_prep(sp[0])
                    switch_at = sp[1]
                else:
                    self._prep = (None, {})          # 곡 끝이 가까움 — 갈아타지 않음
            stop = min(total, pos + _BLOCK)
            if switch_at is not None:
                stop = min(stop, max(pos, switch_at))
            if stop > pos:
                yield mix(r.parts, self.get_settings(), r.gain, r.click, self.get_click_volume(), start=pos, end=stop)
                out += stop - pos
                pos = stop
            if switch_at is not None and pos >= switch_at:
                th, res = self._prep
                th.join()
                st = res.get("stream")
                if st is None:
                    switch_at = None
                    continue
                lead, xf = int(LIVE_XFADE_LEAD_SEC * sr), int(LIVE_XFADE_SEC * sr)
                base = mix(r.parts, self.get_settings(), r.gain, r.click, self.get_click_volume(),
                           start=pos, end=pos + xf)
                if len(base) < xf:
                    base = np.concatenate([base, np.zeros((xf - len(base), 2), np.float32)])
                new = st.mix_block(-lead, xf, self.get_settings(), self.get_click_volume())
                ramp = np.linspace(0.0, 1.0, xf, dtype=np.float32)[:, None]
                self.switch_out, self.switch_stream = out, -lead
                self.stream = st
                if abs(st.target - self.target) > 1e-9:   # 준비하는 동안 또 바뀜
                    st.set_tempo(self.target)
                yield base * (1.0 - ramp) + new * ramp
                spos = -lead + xf
                while True:
                    blk = st.mix_block(spos, _BLOCK, self.get_settings(), self.get_click_volume())
                    if blk is None:
                        return
                    yield blk
                    spos += _BLOCK

def render_file(in_path, out_path, tempo_scale=1.0, mute=(), solo=(), click_volume=0.0, progress=None):
    """명령줄용: 악보 파일 → WAV 또는 MP3(out_path 확장자). mute/solo는 성부 번호(1부터)."""
    score = read_musicxml(in_path)
    settings = default_settings(score)
    for i in mute:
        settings[i - 1].mute = True
    for i in solo:
        settings[i - 1].solo = True
    r = RenderedScore(score, tempo_scale, settings, progress=progress)
    write_wav(out_path, r, click_volume)
    return r
