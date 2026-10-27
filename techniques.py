# -*- coding: utf-8 -*-
"""기타 특수 주법 합성 — 아르모니코스(자연 하모닉스)·라스게아도·글리산도 (2026-10-08 사용자 요청).

아직 악보 표시와는 연결하지 않았다(사용자: 가진 악보에는 이 주법이 없음) — 소리를 먼저 들어 보고
정하려고 샘플을 만든다: python techniques.py → 비교/주법_*.wav

- harmonic: 12프렛 자연 하모닉스. 줄 한가운데(마디)를 살짝 짚으면 홀수 배음이 사라져 한 옥타브 위
  음의 배음열만 남는데, 그것도 위로 갈수록 빨리 약해진다 → 사인파에 가까운 맑은 종소리. 옥타브 위
  기본음(길게) + 2·3배음(빨리 사라짐) + 어두운 줄 소리 조금(KS, 뜯는 위치 0.5 = 짝수 배음만).
- rasgueado: 새끼(e)→약지(a)→중지(m)→검지(i) 순서로 손가락마다 줄을 저음→고음으로 훑는다(손가락 사이
  약 55ms, 줄 사이 약 6ms, 사람 손처럼 조금씩 흔들림). 같은 줄을 다시 치면 앞 울림은 끊기고, 손톱이
  줄을 긁는 소리(2~6kHz 잡음)를 손가락마다 넣는다. 검지로 갈수록 세게.
- glissando: 한 번만 뜯고 프렛을 넘을 때마다 반음씩 계단으로 바뀐다. 계단 간격은 처음과 끝이 넓고
  가운데가 좁다(빨라졌다 느려짐, smoothstep). 소리는 처음 뜯은 줄의 울림을 시간 축으로 늘였다 줄여
  높이만 바꿔서(비브라토와 같은 방법) 끊김 없이 이어지고, 프렛을 넘을 때 아주 작은 마찰음.
"""
import os

import numpy as np

from synth import SR, TIMBRES, NoteSynth, _karplus_strong, _velocity_amp, midi_to_hz

# 맑은 종소리(사용자 2026-10-12): 사인 기본음 + 옥타브·열두째 배음 + 종 특유의 어긋난 배음(2.76·5.4배, 금방 사라짐)
_HARM_PARTIALS = ((1.0, 1.0, 1.0), (2.0, 0.14, 0.4), (3.0, 0.05, 0.22), (2.76, 0.05, 0.12), (5.4, 0.02, 0.05))  # (배수, 크기, 감쇠 시간 배율)
HARM_PLUCK_MIX = 0.12   # 뜯는 소리(어두운 KS) 섞는 양 — 많으면 탁해짐
RASG_FINGERS = ("e", "a", "m", "i")
RASG_FINGER_GAP = 0.055   # 손가락 사이(초)
RASG_STRING_GAP = 0.006   # 한 손가락이 줄 하나 넘어가는 시간(초)
GLISS_STEP_SMOOTH = 0.003  # 반음 계단 하나가 바뀌는 시간(초) — 딸깍 방지
GLISS_ARRIVE_FLOOR = 0.45  # 도착 음 크기의 하한(처음 뜯은 소리 대비) — 0.3→0.6→0.45(2026-10-16: 두드러지게 했다가 조금 약하게)


def _release(out, n_hold, sr, rel_sec=0.2):
    """n_hold 뒤 rel_sec 동안 0으로 줄이고 그 뒤는 자른다."""
    n_rel = int(rel_sec * sr)
    end = min(len(out), n_hold + n_rel)
    if end > n_hold:
        env = np.exp(-np.linspace(0.0, 6.0, end - n_hold))
        out[n_hold:end] *= (env - env[-1]) / max(1e-9, 1.0 - env[-1])
    return out[:end]


def _fade_in(out, sec, sr):
    n = min(len(out), int(sec * sr))
    out[:n] *= np.linspace(0.0, 1.0, n, endpoint=False)
    return out


def harmonic(string_midi, velocity, sound_sec, sr=SR, rng=None):
    """줄(string_midi)의 12프렛 자연 하모닉스 — 소리는 한 옥타브 위."""
    rng = rng or np.random.default_rng(7)
    f = float(midi_to_hz(string_midi + 12))
    n_hold = int(sound_sec * sr)
    n = n_hold + int(0.6 * sr)
    t = np.arange(n) / sr
    t60 = float(np.clip(4.5 * (220.0 / f) ** 0.3, 1.5, 6.0))  # 보통 음보다 오래 울림
    tau = t60 / 6.9
    out = np.zeros(n)
    for mult, amp, tau_mul in _HARM_PARTIALS:
        if f * mult < sr * 0.45:
            out += amp * np.sin(2 * np.pi * f * mult * t + rng.uniform(0, 2 * np.pi)) * np.exp(-t / (tau * tau_mul))
    out += 0.02 * np.sin(2 * np.pi * f * 1.003 * t) * np.exp(-t / tau)   # 아주 느린 맥놀이(종 느낌)
    ks = _karplus_strong(f, n, t60 * 0.8, 900.0, 0.5, rng, sr)
    out = 0.85 * out + HARM_PLUCK_MIX * ks
    out = _fade_in(out, 0.006, sr)   # 손가락을 떼며 울리는 부드러운 시작
    out = _release(out, n_hold, sr, 0.5)
    return (out * _velocity_amp(velocity) * 1.07).astype(np.float32)   # 뜯는 소리를 줄인 만큼(-2dB) 키워 이전과 같은 크기


def _nail_noise(n, sr, rng):
    """손톱이 줄 여러 개를 긁는 소리 — 2~6kHz 잡음이 빨리 사라짐."""
    from scipy.signal import butter, sosfilt
    sos = butter(2, (2000.0, 6000.0), "bandpass", fs=sr, output="sos")
    t = np.arange(n) / sr
    x = sosfilt(sos, rng.standard_normal(n)) * np.exp(-t / 0.012)
    return x / max(1e-9, np.max(np.abs(x)))


def rasgueado(chord, velocity, total_sec, synth=None, sr=SR, rng=None, timbre="guitar"):
    """화음(줄마다 MIDI, 저음→고음)을 e-a-m-i 순서로 훑은 소리(길이 total_sec + 여운).
    반환: (float32 버퍼, [(시각, midi, 세기)] 친 자리 목록 — 확인용)."""
    synth = synth or NoteSynth(sr)
    rng = rng or np.random.default_rng(11)
    plucks = []   # (시각, 줄 번호, 세기)
    for k, _finger in enumerate(RASG_FINGERS):
        t_f = k * RASG_FINGER_GAP + rng.normal(0, 0.003)
        vel = velocity * (0.72 + 0.28 * k / (len(RASG_FINGERS) - 1))   # 새끼 약하게 → 검지 세게
        for j in range(len(chord)):
            t = max(0.0, t_f + j * RASG_STRING_GAP * rng.uniform(0.8, 1.2))
            plucks.append((t, j, int(np.clip(vel * rng.uniform(0.92, 1.05), 1, 127))))
    n_total = int((total_sec + 1.5) * sr)
    buf = np.zeros(n_total, np.float32)
    last_t = {}
    for t, j, _v in plucks:
        last_t.setdefault(j, []).append(t)
    for t, j, v in plucks:
        later = [x for x in last_t[j] if x > t]
        sound = (later[0] - t) if later else max(0.1, total_sec - t)   # 같은 줄을 다시 치면 끊김
        wave = synth.render(chord[j], v, sound, timbre)
        s0 = int(t * sr)
        s1 = min(n_total, s0 + len(wave))
        buf[s0:s1] += wave[:s1 - s0]
    for k in range(len(RASG_FINGERS)):
        t_f = min(t for t, j, _ in plucks[k * len(chord):(k + 1) * len(chord)])
        nz = _nail_noise(int(0.05 * sr), sr, rng) * 0.05 * (velocity / 127.0) * (0.8 + 0.2 * k)
        s0 = int(t_f * sr)
        buf[s0:s0 + len(nz)] += nz[:max(0, min(len(nz), n_total - s0))].astype(np.float32)
    return buf, [(round(t, 4), chord[j], v) for t, j, v in sorted(plucks)]


def _step_times(n_steps, glide_sec):
    """반음 계단 n_steps개가 바뀌는 시각(글리산도 시작 기준) — 진행도가 smoothstep(빨라졌다 느려짐)을
    따라 (k-0.5)/n을 지나는 때."""
    u = np.linspace(0.0, 1.0, 2001)
    s = u * u * (3 - 2 * u)
    levels = (np.arange(1, n_steps + 1) - 0.5) / n_steps
    return np.interp(levels, s, u) * glide_sec


def glissando_path(midi_from, segs, velocity, after_sec, sr=SR, rng=None):
    """midi_from을 한 번 뜯고, segs = [(미끄럼 시작 초, 미끄러지는 시간 초, 도착 음 MIDI), ...]를 차례로 반음씩 미끄러진 뒤
    마지막 도착 음으로 after_sec 더 울린다. 도착 음은 따로 뜯지 않는다(사용자 요청 2026-10-16: "글리산도 마지막 음을 없앰"),
    글리산도가 이어진 악보(A→B♭→A)는 가운데 음도 뜯지 않고 한 줄기로 이어 간다.
    반환: (float32 버퍼, 계단 바뀌는 시각 목록(초) — 확인용)."""
    rng = rng or np.random.default_rng(5)
    p = TIMBRES["guitar"]
    f0 = float(midi_to_hz(midi_from))
    end_t = max(at + gl for at, gl, _ in segs)
    n_hold = int((end_t + after_sec) * sr)
    n = n_hold + int(0.3 * sr)
    t = np.arange(n) / sr
    semis = np.zeros(n)
    times, cur = [], midi_from
    for at, gl, to in segs:
        steps = int(to - cur)
        if steps:
            direction = 1 if steps > 0 else -1
            for tk in at + _step_times(abs(steps), gl):   # 계단마다 GLISS_STEP_SMOOTH초에 걸쳐 반음씩(올림 코사인)
                x = np.clip((t - tk) / GLISS_STEP_SMOOTH + 0.5, 0.0, 1.0)
                semis += direction * (0.5 - 0.5 * np.cos(np.pi * x))
                times.append(float(tk))
        cur = to
    ratio = 2.0 ** (semis / 12.0)
    idx = np.concatenate([[0.0], np.cumsum(ratio[:-1])])
    tier_cut = p["cut_lo"] + (p["cut_hi"] - p["cut_lo"]) * (velocity / 127.0)
    t60 = float(np.clip(p["t60_ref"] * (110.0 / f0) ** 0.4, 0.6, 10.0))
    base = _karplus_strong(f0, int(idx[-1]) + 4, t60, tier_cut, p["pluck"], rng, sr)
    out = np.interp(idx, np.arange(len(base)), base)
    # 미끄러지는 동안 줄 손실(조금 작아짐, 6%) — 예전 12% → 0 → 6%(2026-10-16 "조금 약하게")
    env = np.ones(n)
    for k, (at, gl, _to) in enumerate(segs):
        g0, g1 = int(at * sr), int((at + gl) * sr)
        nxt = int(segs[k + 1][0] * sr) if k + 1 < len(segs) else n
        env[g0:g1] = 1.0 - 0.06 * np.sin(np.linspace(0, np.pi, max(1, g1 - g0)))
        env[g1:nxt] = 0.94 + 0.06 * np.exp(-(t[g1:nxt] - t[g1]) / 0.2)
    out = out * env
    # 도착 음이 묻히지 않게: 시작 음을 오래 붙든 뒤 미끄러지면(2분음표 등) 줄 울림이 이미 많이 줄고, 높아질수록
    # 더 빨리 줄어 도착 음이 거의 안 들렸다(악보 시험에서 -36dB, 2026-10-08). 실제로는 왼손이 줄을 누른 채
    # 이어 가 분명히 들리므로, 마지막 도착 크기가 처음의 GLISS_ARRIVE_FLOOR 아래면 미끄러지는 동안 서서히 끌어올림.
    # 도착 음을 따로 뜯지 않으므로(2026-10-16) 끝 음의 울림은 이 한 번의 소리가 맡는다.
    g0 = int(segs[0][0] * sr)
    g1 = int(end_t * sr)
    w = int(0.03 * sr)
    first = np.sqrt(np.mean(out[int(0.01 * sr):int(0.01 * sr) + w] ** 2))
    arrive = np.sqrt(np.mean(out[g1:g1 + w] ** 2)) if g1 + w <= n else first
    if first > 0 and arrive < GLISS_ARRIVE_FLOOR * first and g1 > g0:
        boost = GLISS_ARRIVE_FLOOR * first / max(arrive, 1e-9)
        ramp = np.ones(n)
        ramp[g0:g1] = boost ** (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, g1 - g0)))
        ramp[g1:] = boost
        out = out * ramp
    for tk in times:
        # 프렛 넘는 마찰음은 원래 크기(0.045로 키웠더니 딸깍거려 반음 계단 사이에 다른 음이 낀 것처럼 들림 — 2026-10-16)
        nz = _nail_noise(int(0.004 * sr), sr, rng) * 0.015
        s0 = int(tk * sr)
        out[s0:s0 + len(nz)] += nz[:max(0, min(len(nz), n - s0))]
    out = _fade_in(out, p["attack"], sr)
    out = _release(out, n_hold, sr, 0.2)
    return (out * _velocity_amp(velocity)).astype(np.float32), [round(float(x), 4) for x in times]


def glissando(midi_from, midi_to, velocity, hold_sec, glide_sec, after_sec, sr=SR, rng=None):
    """midi_from을 뜯어 hold_sec 울린 뒤 glide_sec 동안 반음씩 midi_to로 미끄러지고 after_sec 더 울림(한 번 미끄러지는 경우)."""
    return glissando_path(midi_from, [(hold_sec, glide_sec, midi_to)], velocity, after_sec, sr, rng)


# ---------------------------------------------------------------- 샘플 만들기

def _finish(buf, sr=SR):
    """몸통 공명·방 울림(합성 앱과 같은 효과) → -1dB로 맞춤."""
    from render import apply_part_effects
    y = apply_part_effects(buf.astype(np.float32), "guitar", sr)
    return (y * 0.89 / max(1e-9, np.max(np.abs(y)))).astype(np.float32)


def _place(buf, wave, sec, sr=SR):
    s0 = int(sec * sr)
    end = min(len(buf), s0 + len(wave))
    buf[s0:end] += wave[:end - s0]


def make_samples(out_dir):
    import soundfile as sf
    sr = SR
    synth = NoteSynth(sr)
    os.makedirs(out_dir, exist_ok=True)
    report = {}

    # 1. 아르모니코스: 줄마다 개방현(보통) → 12프렛 하모닉스(한 옥타브 위), 끝에 하모닉스로 짧은 선율
    strings = (40, 45, 50, 55, 59, 64)  # E2 A2 D3 G3 B3 E4
    buf = np.zeros(int(22 * sr), np.float32)
    t = 0.3
    for s in strings:
        _place(buf, synth.render(s, 80, 1.1, "guitar"), t)
        _place(buf, harmonic(s, 85, 1.6), t + 1.3)
        t += 3.0
    for s in (64, 59, 55, 59, 64):            # E5 B4 G4 B4 E5(하모닉스)
        _place(buf, harmonic(s, 80, 0.9), t)
        t += 0.55
    sf.write(os.path.join(out_dir, "주법_아르모니코스.wav"), _finish(buf[:int((t + 2.0) * sr)]), sr)

    # 2. 라스게아도: 한 번 쓸어내리기(비교용) → e-a-m-i, 그다음 Em-Am-B7-Em 진행
    em = (40, 47, 52, 55, 59, 64)
    am = (40, 45, 52, 57, 60, 64)
    b7 = (40, 47, 51, 57, 59, 66)
    buf = np.zeros(int(16 * sr), np.float32)
    for j, m in enumerate(em):                 # 비교: 엄지로 한 번 쓸기(줄 사이 12ms)
        _place(buf, synth.render(m, 85, 1.6, "guitar"), 0.3 + j * 0.012)
    seq = [(em, 2.4), (em, 4.4), (am, 6.4), (b7, 8.4), (em, 10.4)]
    first_plucks = None
    for chord, at in seq:
        w, plucks = rasgueado(chord, 100, 1.6, synth)
        first_plucks = first_plucks or plucks
        _place(buf, w, at)
    report["라스게아도 손가락별 첫 줄 시각(초)"] = [first_plucks[k * 6][0] for k in range(4)]
    sf.write(os.path.join(out_dir, "주법_라스게아도.wav"), _finish(buf[:int(13.5 * sr)]), sr)

    # 3. 글리산도: A3→E4 올림, E4→A3 내림, G3→G4 한 옥타브(느리게)
    buf = np.zeros(int(12 * sr), np.float32)
    w, times = glissando(57, 64, 90, 0.5, 0.45, 1.0)
    report["글리산도 A3→E4 계단 시각(초)"] = times
    _place(buf, w, 0.3)
    w, _ = glissando(64, 57, 90, 0.5, 0.45, 1.0)
    _place(buf, w, 3.0)
    w, times = glissando(55, 67, 90, 0.6, 0.9, 1.4)
    report["글리산도 G3→G4 계단 간격(초)"] = [round(b - a, 3) for a, b in zip(times, times[1:])]
    _place(buf, w, 5.8)
    sf.write(os.path.join(out_dir, "주법_글리산도.wav"), _finish(buf[:int(10.5 * sr)]), sr)
    return report


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    for k, v in make_samples(os.path.join(here, "비교")).items():
        print(k, v)
