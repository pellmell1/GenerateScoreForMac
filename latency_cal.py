# -*- coding: utf-8 -*-
"""소리 지연 자동 맞추기 — 스피커로 DTMF 소리(두 주파수 동시)를 내고 마이크로 들어서, "장치가 알려 준
소리 나는 시각"과 "실제로 마이크에 들어온 시각"의 차이를 잰다(사용자 요청 2026-10-07: 노트북·기기마다
지연이 달라서).

재생 위치는 audio_player가 PortAudio의 DAC 시각(이 샘플이 스피커에서 나올 시각)으로 계산하는데,
장치가 알려 주는 그 시각이 실제와 수십 ms씩 다를 때가 있다. 같은 PortAudio 시계로 출력 쪽 DAC 시각과
입력 쪽 ADC 시각(마이크 샘플이 들어온 시각)을 함께 받는 한 스트림(full duplex)으로 재면, 둘의 차이가
곧 "커서를 얼마나 늦춰야 하나"(= 소리 지연 보정 값)다. 소리가 공기로 30cm 가는 데 1ms쯤은 무시.

측정 소리는 DTMF(전화 버튼음) 방식(사용자 제안 2026-10-08): 낮은 주파수 하나 + 높은 주파수 하나를
동시에 60ms. 두 주파수가 "동시에" 있어야만 인정하므로 잡음·말소리에 강하고, 소리마다 다른 버튼
(주파수 쌍)을 써서 메아리나 옆 소리를 잘못 짝짓지 않는다(단일 2kHz 딸깍일 땐 8번 중 2~3번이 엉뚱했다).
시작 시각은 내보낸 원래 신호에도 똑같은 검출을 해서 비교하므로 검출 방법 자체의 치우침이 상쇄된다.

블루투스 이어폰처럼 마이크가 못 듣는 출력이면 소리를 못 찾아 실패한다 — 그땐 탭으로 맞추기.
"""
import sys
import threading
import time

import numpy as np

SR = 48000            # 측정은 장치 기본(대개 48000)에 가깝게 — auto_convert로 다른 값도 됨
INTERVAL_SEC = 0.5
LEAD_SEC = 0.6        # 시작 후 첫 소리까지(장치가 안정되도록)
SEARCH_SEC = 0.6      # 소리마다 이 시간 안에서 마이크 소리를 찾음
BURST_SEC = 0.06      # DTMF 한 번 길이(표준은 40ms 이상)
FADE_SEC = 0.004
CLUSTER_SEC = 0.012   # 측정값이 이만큼 안에 모여야 같은 무리
MIC_MIN_DBFS = -85.0  # 측정 구간 전체(1kHz 위) 크기가 이보다 작으면 마이크가 사실상 안 들림

# DTMF 주파수(Hz) — 버튼 = (낮은 줄, 높은 칸). 표준 DTMF의 2배: 노트북 스피커는 1kHz 아래를 거의 못 내서
# 표준 낮은 줄(697~941Hz)이 약해 두 주파수 판정이 통째로 약해졌다(신호/잡음 2~3배, 2026-10-08 실측).
# 2배(1.4~3.3kHz)는 작은 스피커·마이크가 잘 내고 잘 듣는 대역. 비율 관계(서로 배음이 겹치지 않음)는 그대로.
_FREQ_SCALE = 2.0
_LOW = tuple(f * _FREQ_SCALE for f in (697, 770, 852, 941))
_HIGH = tuple(f * _FREQ_SCALE for f in (1209, 1336, 1477, 1633))
# 쓰는 버튼 순서(1, 5, 9, D, 3, 7, 0, B): 이웃한 소리끼리 두 주파수가 모두 다르게
BUTTONS = [(_LOW[0], _HIGH[0]), (_LOW[1], _HIGH[1]), (_LOW[2], _HIGH[2]), (_LOW[3], _HIGH[3]),
           (_LOW[0], _HIGH[2]), (_LOW[2], _HIGH[0]), (_LOW[3], _HIGH[1]), (_LOW[1], _HIGH[3])]
N_CLICKS = len(BUTTONS)

last_debug = {}


def _burst(f1, f2, sr):
    n = int(BURST_SEC * sr)
    t = np.arange(n) / sr
    w = 0.42 * (np.sin(2 * np.pi * f1 * t) + np.sin(2 * np.pi * f2 * t))
    nf = int(FADE_SEC * sr)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, nf))
    w[:nf] *= ramp
    w[-nf:] *= ramp[::-1]
    return w.astype(np.float32)


def _pair_envelope(x, f1, f2, sr):  # (예전 검출 방식 — 지금은 정합 필터를 씀)
    """두 주파수 각각의 좁은 대역 포락선의 기하평균 — 둘 다 있어야 커진다(DTMF 판정과 같은 생각).
    영위상(sosfiltfilt)이라 포락선이 시간 방향으로 치우치지 않는다."""
    from scipy.signal import butter, sosfiltfilt, hilbert
    envs = []
    for f in (f1, f2):
        sos = butter(3, (f * 0.97, f * 1.03), "bandpass", fs=sr, output="sos")
        envs.append(np.abs(hilbert(sosfiltfilt(sos, x))))
    return np.sqrt(envs[0] * envs[1])


def _onset(env, lo, hi):
    """[lo, hi)에서 가장 큰 봉우리를 찾고, 그 앞쪽에서 최대치의 절반을 처음 넘는 곳."""
    seg = env[lo:hi]
    if len(seg) == 0:
        return None, 0.0
    k = int(np.argmax(seg))
    peak = float(seg[k])
    below = np.where(seg[:k + 1] < peak * 0.5)[0]
    start = (below[-1] + 1) if len(below) else 0
    return lo + start, peak


def mic_privacy_allowed():
    """Windows 개인 정보 → 마이크 설정을 읽기만 한다(바꾸지 않음 — 사용자가 일부러 막은 보호 설정이라
    앱이 몰래 켜지 않는다, 2026-10-08). 읽을 수 없으면 허용으로 본다."""
    try:
        import winreg
        base = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
        for root, path in ((winreg.HKEY_LOCAL_MACHINE, base), (winreg.HKEY_CURRENT_USER, base),
                           (winreg.HKEY_CURRENT_USER, base + r"\NonPackaged")):
            try:
                with winreg.OpenKey(root, path) as k:
                    if str(winreg.QueryValueEx(k, "Value")[0]).lower() == "deny":
                        return False
            except OSError:
                pass
    except Exception:
        pass
    return True


def open_mic_settings(privacy):
    """마이크 관련 설정 화면 열기 — 개인 정보에서 막혔으면 그 화면, 아니면 소리(입력) 화면.
    Mac은 시스템 설정 → 개인정보 보호 및 보안 → 마이크 / 사운드 → 입력."""
    import os
    import sys
    try:
        if sys.platform == "darwin":
            import subprocess
            url = ("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone" if privacy
                   else "x-apple.systempreferences:com.apple.preference.sound?input")
            subprocess.Popen(["open", url])
            return
        os.startfile("ms-settings:privacy-microphone" if privacy else "ms-settings:sound")
    except Exception:
        pass


def _devices():
    import sounddevice as sd
    for h in sd.query_hostapis():
        if "WASAPI" in h["name"] and h["default_output_device"] >= 0 and h["default_input_device"] >= 0:
            return h["default_input_device"], h["default_output_device"], True
    return sd.default.device[0], sd.default.device[1], False


def measure(output_latency=0.12, progress=None):
    """→ (보정값 ms 또는 None, 설명 문자열). progress(0~1)로 진행 알림."""
    import sounddevice as sd
    in_dev, out_dev, wasapi = _devices()
    total = int((LEAD_SEC + N_CLICKS * INTERVAL_SEC + SEARCH_SEC) * SR)
    starts = [int((LEAD_SEC + i * INTERVAL_SEC) * SR) for i in range(N_CLICKS)]
    out_sig = np.zeros(total, np.float32)
    for s0, (f1, f2) in zip(starts, BUTTONS):
        b = _burst(f1, f2, SR)
        out_sig[s0:s0 + len(b)] = b
    rec = np.zeros(total, np.float32)
    out_times = {}   # 출력 샘플 번호 → DAC 시각(블록 첫 샘플)
    in_times = []    # (입력 샘플 번호, ADC 시각)
    pos = {"out": 0, "in": 0}
    done = threading.Event()

    def out_cb(outdata, frames, time_info, status):
        o = pos["out"]
        chunk = out_sig[o:o + frames]
        outdata[:len(chunk), 0] = chunk
        outdata[len(chunk):, 0] = 0
        if outdata.shape[1] > 1:
            outdata[:, 1] = outdata[:, 0]
        out_times[o] = time_info.outputBufferDacTime
        pos["out"] = o + frames
        if progress:
            progress(min(1.0, pos["out"] / total))
        if pos["out"] >= total:
            done.set()

    def in_cb(indata, frames, time_info, status):
        i = pos["in"]
        k = min(frames, total - i)
        if k > 0:
            rec[i:i + k] = indata[:k].mean(axis=1)
            in_times.append((i, time_info.inputBufferAdcTime))
        pos["in"] = i + frames

    # 한 스트림(full duplex, 공유 모드)으로 — 출력 DAC 시각과 입력 ADC 시각이 같은 시계.
    # (독점 모드로 Windows 음향 처리를 피해 보려 했으나 이 노트북의 인텔 마이크 배열은 독점 모드에서
    # 소리가 아닌 값(천천히 커지기만 하는 숫자)을 줘서 못 씀, 2026-10-08 실측.)
    def cb(indata, outdata, frames, time_info, status):
        out_cb(outdata, frames, time_info, status)
        in_cb(indata, frames, time_info, status)

    kw = dict(device=(in_dev, out_dev), samplerate=SR, channels=(1, 2), dtype="float32", callback=cb,
              latency=(0.05, output_latency))
    if wasapi:
        kw["extra_settings"] = (sd.WasapiSettings(auto_convert=True), sd.WasapiSettings(auto_convert=True))
    with sd.Stream(**kw):
        done.wait(timeout=total / SR + 5)
    time.sleep(0.05)

    if not out_times or not in_times or max(out_times.values()) <= 0:
        return None, "이 장치는 소리 나는 시각을 알려 주지 않아 잴 수 없습니다."
    ob = np.array(sorted(out_times.items()))
    ib = np.array(in_times)

    def dac_time(sample):
        j = max(0, np.searchsorted(ob[:, 0], sample, side="right") - 1)
        return ob[j, 1] + (sample - ob[j, 0]) / SR

    def adc_time(sample):
        j = max(0, np.searchsorted(ib[:, 0], sample, side="right") - 1)
        return ib[j, 1] + (sample - ib[j, 0]) / SR

    # 정합 필터: 보낸 DTMF 파형 그대로를 녹음에서 찾는다(상관이 가장 큰 곳) — 60ms 전체를 겹쳐 맞추므로
    # 마이크 소리가 배경과 비슷할 만큼 작아도 찾는다. 소리마다 주파수 쌍이 달라 다른 소리와 헷갈리지 않는다.
    from scipy.signal import butter, fftconvolve, sosfiltfilt
    rec = sosfiltfilt(butter(4, 1000.0, "highpass", fs=SR, output="sos"), rec.astype(np.float64))
    # 마이크 소리가 거의 안 들어오면(입력 음량 0·마이크 끄기 키·Windows 개인정보에서 마이크 막힘) 원인을 알려 준다.
    # 이 노트북에서 실측한 상태가 배경 약 −96dBFS(2026-10-08) — 정상 마이크는 조용한 방에서도 −70dBFS 안팎.
    level_db = 20 * np.log10(float(np.sqrt(np.mean(rec ** 2))) + 1e-12)
    if level_db < MIC_MIN_DBFS:
        # Mac은 권한이 없으면 오류 없이 무음(0)만 들어온다 — 개인정보 설정에서 막혔을 가능성이 가장 크다.
        if sys.platform == "darwin":
            return None, ("MIC_PRIVACY", "마이크 소리가 들어오지 않습니다 — '마이크 설정 열기'에서 이 앱의 마이크 "
                                         "접근을 허용하고, 사운드 → 입력의 음량도 확인한 뒤 다시 누르세요.")
        if not mic_privacy_allowed():
            return None, ("MIC_PRIVACY", "Windows 개인 정보 설정에서 마이크가 막혀 있습니다. '마이크 설정 열기'에서 "
                                         "'데스크톱 앱이 마이크에 액세스하도록 허용'을 켜고 다시 누르세요.")
        return None, ("MIC_SILENT", f"마이크 소리가 거의 들어오지 않습니다({level_db:.0f}dB) — 마이크가 꺼져(음소거) "
                                    "있거나 입력 음량이 0일 수 있습니다. '마이크 설정 열기'에서 입력 장치의 음량·음소거를 "
                                    "확인하거나 노트북의 마이크 끄기 키를 누른 뒤 다시 누르세요. 안 되면 '탭으로 맞추기'.")
    adc_grid = np.array([adc_time(i) for i in range(0, total, 256)])
    deltas = []
    snrs = []
    for s0, (f1, f2) in zip(starts, BUTTONS):
        tmpl = _burst(f1, f2, SR).astype(np.float64)
        t_dac = dac_time(s0)
        guess = int(np.searchsorted(adc_grid, t_dac) * 256)
        lo, hi = max(0, guess - int(0.25 * SR)), min(total, guess + int(SEARCH_SEC * SR))
        seg = rec[lo:hi]
        if len(seg) <= len(tmpl):
            continue
        corr = np.abs(fftconvolve(seg, tmpl[::-1], mode="valid"))
        # 상관의 포락선(반송파 떨림 제거) — 정점 앞뒤 1ms 최대값으로 매끄럽게
        from scipy.ndimage import maximum_filter1d
        env = maximum_filter1d(corr, size=int(0.001 * SR))
        k = int(np.argmax(env))
        noise = float(np.median(env)) + 1e-15
        snr = float(env[k]) / noise
        snrs.append(snr)
        if snr < 4.0:
            continue
        # 정점 근처에서 실제 최대 상관 위치(샘플 단위)
        a, b = max(0, k - int(0.001 * SR)), min(len(corr), k + int(0.001 * SR))
        k = a + int(np.argmax(corr[a:b]))
        deltas.append(adc_time(lo + k) - t_dac)

    global last_debug
    last_debug = {"deltas_ms": [round(float(x) * 1000, 1) for x in deltas], "snr": [round(s, 1) for s in snrs],
                  "rec_peak": float(np.abs(rec).max()), "level_db": round(level_db, 1)}
    if len(deltas) < 4:
        return None, (f"마이크에서 측정 소리를 {len(deltas)}/{N_CLICKS}번만 찾았습니다. 소리를 키우거나, "
                      "블루투스 이어폰이면 '탭으로 맞추기'를 쓰세요.")
    # 그래도 섞일 수 있는 엉뚱한 값은 버린다 — ±CLUSTER_SEC 안에 가장 많이 모인 무리만
    d = np.sort(np.array(deltas))
    counts = [int(np.sum(np.abs(d - x) <= CLUSTER_SEC)) for x in d]
    best = d[int(np.argmax(counts))]
    group = d[np.abs(d - best) <= CLUSTER_SEC]
    if len(group) < max(4, N_CLICKS // 2):
        return None, (f"측정값이 고르지 않습니다(모인 값 {len(group)}/{len(deltas)}개). 조용한 곳에서 소리를 "
                      "조금 키우고 다시 해 보세요.")
    med = float(np.median(group))
    return (int(round(med * 1000 / 5.0)) * 5,
            f"DTMF {len(group)}/{N_CLICKS}번이 {np.ptp(group) * 1000:.0f}ms 안에 모임")
