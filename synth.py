# -*- coding: utf-8 -*-
"""음 하나를 소리(샘플 배열)로 만든다 — UI 독립, numpy·scipy만.

음색(timbre):
- "guitar" / "bass" / "piano": Karplus-Strong(뜯는 현). 잡음 한 주기를 넣고 "한 주기
  전 두 샘플의 평균"을 되먹이면, 고음 성분이 빨리 죽고 기본음이 오래 남는 현 소리가
  난다. 샘플 파일이 필요 없다.
- "xylophone" / "glockenspiel" / "metallophone": 채(말렛)로 치는 막대 악기 — 막대의 진동 모드(비조화 부분음)를
  각자 지수로 줄어드는 사인파로 합성(`kind: "mallet"`). 자일로폰은 나무 막대(2·3 번째 부분음이 기본음의 3·6배로
  조율돼 짧고 마른 소리), 글로켄슈필은 강철 막대(고정 막대의 비조화 부분음 1:2.76:5.40:8.93, 밝고 길게 울림),
  메탈로폰은 알루미늄 막대(비브라폰처럼 1:4:10 조율, 부드럽고 오래 울림).
- "sustain": 배음 합성 + ADSR(현악기·관악기처럼 소리가 유지되는 악기용).
- "sine": 순수 사인파 + ADSR(음높이 확인용, 가장 기계적).

Karplus-Strong 계산 방법(속도 때문에): 되먹임 지연이 N샘플이면 y[n]은 y[n-N]과
y[n-N-1]에만 의존하므로, N샘플짜리 블록을 한꺼번에 numpy로 계산할 수 있다(scipy
lfilter는 계수 배열 길이 N에 비례해 느려져서 저음(N≈1000)에서 너무 느렸다).
대신 지연이 정수라 음높이가 최대 반 샘플 어긋나므로(고음에서 수십 cent), 정수
주기로 조금 낮게 만든 뒤 np.interp로 정확한 높이까지 당겨 리샘플링한다.

같은 (음색, 높이, 세기 단계)는 캐시해 두고 길이만 잘라 쓴다 — 곡 하나에 같은 음이
수백 번 나오므로 합성 시간이 크게 준다. 대신 반복음이 완전히 똑같아지지 않게 잡음
변형을 몇 개(_VARIANTS) 만들어 돌려 쓴다.

기타답게 하는 장치(2026-10-07): 여기(음 하나)에서는 나일론 줄 음색(낮은 차단 주파수·
부드러운 시작)과 "줄 두 개 겹치기"(실제 줄은 두 방향으로 떨려 높이·감쇠가 아주 조금
다른 두 소리가 겹친다 → 살짝 맥놀이)를 한다. 사용자가 준 참고 자료(클래식 기타 KS 합성
안내)에서 세기별 감쇠(약한 음은 빨리 사라짐)와 손가락 마찰음을 더 가져왔다 — 그 자료의
예시 코드(음마다 np.append로 버퍼를 새로 만드는 표본 단위 루프, 지연 길이를 정수로 잘라
고음일수록 음정이 틀어짐)는 느리고 부정확해서 방법만 따르고 코드는 쓰지 않았다. 몸통 공명·방 울림(리버브)·울림 남기기·
사람 손 느낌은 성부 전체에 거는 게 효율적이라 render.py에서 한다(TIMBRES 값만 여기).
"""
import numpy as np
from scipy.signal import lfilter

SR = 44100

_VARIANTS = 3          # 같은 음의 잡음 변형 개수
# 세기를 3단계(여림/보통/셈)로만 묶어 밝기를 정한다 — 캐시 개수를 줄이려고. 음량은
# 단계와 무관하게 세기 그대로 곱하므로 셈여림 차이는 그대로 들린다(밝기만 3단계).
_VEL_TIERS = (59, 89, 127)
_VEL_TIER_REP = (45, 75, 105)
_FADE_IN_SEC = 0.002   # 시작 "딱" 방지
_DC_BLOCK_R = 0.995    # 직류 차단 필터 극(약 35Hz 아래를 완만히 깎음)
_FINGER_SEC = 0.012    # 손가락 마찰음 길이

# 음색별 설정
#   t60_ref: 110Hz 음이 -60dB까지 줄어드는 시간(초). 높은 음일수록 짧아진다.
#   cut_lo/hi: 여린 음/센 음의 밝기 — 들뜸 파형의 차단 주파수(Hz). 셀수록 밝게.
#   pluck: 뜯는 위치(줄 길이 대비) — 이 위치의 배음이 빠져 음색이 달라짐
#   release: 음이 끝날 때 줄어드는 시간(초)
#   gain: 음색 간 음량 맞춤
#   attack: (KS) 시작 부드럽게 올리는 시간(초) — 손가락 살로 뜯는 나일론 느낌
#   detune_cents/second_amp/second_t60: (KS) 줄 두 개 겹치기 — 둘째 소리의 높이 차(cent),
#       크기(첫째 대비), 감쇠 시간 배율. second_amp=0이면 끔.
#   tier_t60: (KS) 세기 단계(여림/보통/셈)별 감쇠 시간 배율 — 약하게 뜯은 음은 조금 빨리 사라짐
#   finger: (KS) 뜯는 순간 손가락·손톱이 줄을 스치는 짧은 마찰음 크기(음 크기 대비, 0이면 없음)
#   vib_*: (KS) 높은 음 비브라토 — vib_from(MIDI) 이상, vib_min_sec 이상 이어지는 음에만. 뜯고 vib_delay초
#       뒤부터 0.3초에 걸쳐 서서히 ±vib_cents, 초당 vib_rate번(음마다 ±8% 흔듦). 사용자 요청 2026-10-07.
#   high_*: (KS) 높은 음 음량 — high_from부터 high_full(MIDI)까지 0 → high_boost_db로 점점 키움.
#   --- 아래는 render.py가 성부 전체에 적용 ---
#   body: 몸통 공명 종류(render.BODY_MODES의 키, None이면 없음) / body_mix: 그 양
#   reverb: 방 울림 양(0이면 없음)
#   ring: 악보 길이가 끝나도 더 울리게 둘 시간(초) — 같은 음이 다시 나오면 거기서 끊김
TIMBRES = {
    # 나일론 줄 클래식 기타: 차단 주파수를 낮춰 부드럽게, 뜯는 위치는 줄 길이의 1/6쯤
    "guitar": {"kind": "ks", "t60_ref": 3.0, "cut_lo": 900.0, "cut_hi": 3200.0,
               "pluck": 0.17, "release": 0.15, "gain": 1.0, "attack": 0.004,
               "detune_cents": 1.5, "second_amp": 0.5, "second_t60": 0.6,
               "tier_t60": (0.8, 1.0, 1.1), "finger": 0.10,
               "vib_from": 64, "vib_cents": 17.0, "vib_rate": 5.5, "vib_delay": 0.15, "vib_min_sec": 0.3,
               "high_from": 60, "high_full": 72, "high_boost_db": 3.0,
               "body": "guitar", "body_mix": 0.45, "reverb": 0.22, "ring": 0.35},
    "bass": {"kind": "ks", "t60_ref": 5.0, "cut_lo": 350.0, "cut_hi": 1200.0,
             "tier_t60": (0.85, 1.0, 1.05), "finger": 0.05,
             "pluck": 0.2, "release": 0.12, "gain": 1.1, "attack": 0.003,
             "detune_cents": 0.8, "second_amp": 0.35, "second_t60": 0.7,
             "body": None, "body_mix": 0.0, "reverb": 0.12, "ring": 0.1},
    "piano": {"kind": "ks", "t60_ref": 7.0, "cut_lo": 1500.0, "cut_hi": 5000.0,
              "pluck": 0.11, "release": 0.15, "gain": 1.0, "attack": 0.002,
              "detune_cents": 1.0, "second_amp": 0.6, "second_t60": 0.8,
              "body": None, "body_mix": 0.0, "reverb": 0.25, "ring": 0.0},
    "sustain": {"kind": "additive", "harmonics": 10, "rolloff": 1.3,
                "attack": 0.04, "decay": 0.15, "sustain": 0.75, "release": 0.12, "gain": 0.6,
                "body": None, "body_mix": 0.0, "reverb": 0.2, "ring": 0.0},
    "sine": {"kind": "additive", "harmonics": 1, "rolloff": 1.0,
             "attack": 0.01, "decay": 0.05, "sustain": 0.9, "release": 0.05, "gain": 0.7,
             "body": None, "body_mix": 0.0, "reverb": 0.0, "ring": 0.0},
}
# 채로 치는 막대 악기(2026-10-25, 사용자 요청: 자일로폰·글로켄슈필·메탈로폰) — kind "mallet".
#   partials: [(기본음 대비 주파수 비, 크기, 감쇠 시간 배율)] — 막대는 진동 모드가 정수배가 아니라 종소리처럼 들림
#   t60_ref: 기본음 261.6Hz(C4) 음이 -60dB까지 줄어드는 시간(초). 높은 음일수록 (261.6/f)^t60_exp배로 짧아짐(t60_min 아래로는 안 줄임)
#   bright: 세게 칠수록 높은 부분음이 커지는 정도(부분음 번호 i에 (세기)^(bright·i) 곱함)
#   click: 채가 막대를 때리는 순간의 짧은 마찰음 크기(딱) / attack: 시작 부드럽게(초) / release: 손으로 막아 끄는 시간
#   ring: 악보 음 길이가 끝나도 더 울리게 둘 시간(초) — 막대는 안 막으면 계속 울림(render가 다음 같은 음에서 끊음)
TIMBRES.update({
    "xylophone": {"kind": "mallet", "partials": [(1.0, 1.0, 1.0), (3.0, 0.50, 0.35), (6.0, 0.22, 0.15)],
                  "t60_ref": 0.9, "t60_exp": 0.8, "t60_min": 0.10, "bright": 0.6, "click": 0.45,
                  "attack": 0.0005, "release": 0.04, "gain": 0.85,
                  "body": None, "body_mix": 0.0, "reverb": 0.18, "ring": 0.35},
    "glockenspiel": {"kind": "mallet", "partials": [(1.0, 1.0, 1.0), (2.76, 0.55, 0.65), (5.40, 0.32, 0.40),
                                                    (8.93, 0.18, 0.22)],
                     "t60_ref": 3.2, "t60_exp": 0.55, "t60_min": 0.5, "bright": 0.5, "click": 0.12,
                     "attack": 0.0005, "release": 0.12, "gain": 0.65,
                     "body": None, "body_mix": 0.0, "reverb": 0.28, "ring": 1.6},
    "metallophone": {"kind": "mallet", "partials": [(1.0, 1.0, 1.0), (4.0, 0.34, 0.45), (10.0, 0.12, 0.18)],
                     "t60_ref": 4.2, "t60_exp": 0.45, "t60_min": 0.8, "bright": 0.4, "click": 0.05,
                     "attack": 0.004, "release": 0.15, "gain": 0.45,
                     "body": None, "body_mix": 0.0, "reverb": 0.28, "ring": 2.2},
})
TIMBRE_NAMES = {"guitar": "기타", "bass": "베이스", "piano": "피아노", "sustain": "지속음(현·관)", "sine": "사인파",
                "xylophone": "자일로폰", "glockenspiel": "글로켄슈필", "metallophone": "메탈로폰"}

# pizz. 소리(2026-10-08): 뜯는 음색(KS)은 손바닥으로 줄 끝을 막은 듯 짧고 어둡게(팜뮤트), 활 현악기
# 음색(지속음)은 뜯는 소리로. 비브라토·울림 남기기는 없음.
# 사용자 요청(2026-10-08) "조금 더 둔탁하게": 차단 500~1500 → 300~900Hz, 울림 0.35 → 0.25초, 뜯는 위치를 줄
# 가운데 쪽으로(0.17 → 0.3, 높은 배음 줄어듦), 손가락 마찰음도 줄임 — 무게중심 1846 → 약 1300Hz.
_PIZZ_OVERRIDE = {"t60_ref": 0.25, "cut_lo": 300.0, "cut_hi": 900.0, "pluck": 0.3, "second_amp": 0.2,
                  "finger": 0.04, "tier_t60": (1.0, 1.0, 1.0), "vib_cents": 0.0, "ring": 0.0, "gain_mul": 0.8}


# 4번 줄 단음 멜로디를 첼로처럼(2026-10-08 사용자 요청: "비브라토가 많이 들어가고 중후한 느낌, 기타 소리 범위
# 안에서", "소리도 조금 크게"): 고음을 깎아 따뜻하게(차단 600~2000Hz), 뜯는 위치를 줄 가운데 쪽으로(지판 위에서
# 뜯는 sul tasto — 기본음이 두꺼움), 더 오래 울림(t60 4.5), 둘째 줄 겹침을 키워 두껍게, 비브라토는 모든 높이에서
# 더 깊게(±30cent)·더 일찍(0.08초)·조금 느리게(초당 5번). 크기는 첫소리 약 +3.5dB(둘째 줄 겹침 포함).
# → 사용자 요청(같은 날) "비브라토를 조금 줄이고 음색은 좀 더 첼로 소리에 가깝게": 비브라토 ±30 → ±22cent,
#   뜯은 소리(0.6배) 뒤로 활로 켠 듯한 지속음(NoteSynth._bowed, bow_mix)이 0.12초에 걸쳐 차올라 음 끝까지 이어짐.
#   음 전체 크기는 보통 기타 음보다 약 +3~4dB(지속음 0.45는 +7.8dB라 다른 성부를 덮어서 0.3·gain 1.0으로).
# → 사용자 요청(같은 날) "지속시간이 너무 길다, 일반 기타보다 조금 길게": 지속음도 줄어들게(bow_decay 0.3초 지수),
#   뜯은 소리 울림 t60 4.5 → 3.3. -20dB까지 보통 기타 0.55초 → 첼로 판 약 0.8초(약 1.5배). 줄어든 만큼 작아져
#   (-2.6dB) gain 1.0 → 1.9로 "조금 크게"(음 전체 약 +3dB) 유지.
# → 사용자 요청(같은 날) "음색을 좀 더 묵직하게": 뜯는 소리 차단 600~2000 → 400~1400Hz·뜯는 위치 0.28 → 0.35(기본음 두껍게),
#   활 지속음 몸통 봉우리 280 → 200Hz·1.6배, 고음 줄어드는 곳 3000 → 2000Hz. 커진 만큼 gain 1.9 → 1.75(크기 그대로).
_CELLO_OVERRIDE = {"t60_ref": 3.3, "cut_lo": 400.0, "cut_hi": 1400.0, "pluck": 0.35, "second_amp": 0.65,
                   "second_t60": 0.8, "finger": 0.03, "vib_from": 0, "vib_cents": 22.0, "vib_rate": 5.0,
                   "vib_delay": 0.08, "vib_min_sec": 0.2, "high_boost_db": 0.0, "gain_mul": 1.75,
                   "pluck_mix": 0.6, "bow_mix": 0.3, "bow_attack": 0.12, "bow_decay": 0.3, "release": 0.15,
                   "bow_low_hz": 200.0, "bow_low_amp": 1.6, "bow_roll_hz": 2000.0}


def cello_timbre(timbre):
    """기타 음색의 "4번 줄 첼로" 판 이름(기타가 아니면 그대로 — 베이스 4번 줄 등은 손대지 않음)."""
    if timbre != "guitar":
        return timbre
    name = "guitar_cello"
    if name not in TIMBRES:
        p = dict(TIMBRES["guitar"])
        p.update(_CELLO_OVERRIDE)
        p["gain"] = p["gain"] * p.pop("gain_mul")
        TIMBRES[name] = p
    return name


# 5·6번 줄 저음(2026-10-09 사용자 요청 "5·6번은 거친 소리를 줄이고 묵직한 저음으로, 메모리는 많이 차지 않게"):
# 고음을 깎고(차단 900~3200 → 450~1500Hz) 줄 가운데 쪽을 뜯은 듯(뜯는 위치 0.17 → 0.3, 기본음이 두꺼움), 손가락
# 마찰음(0.10 → 0.03)과 둘째 줄 겹침·어긋남(0.5·1.5cent → 0.3·0.8cent, 맥놀이가 거칠게 들림)을 줄이고, 시작을 조금
# 부드럽게(4 → 7ms). 울림 길이(t60)는 그대로라 음 하나 캐시 크기도 같다. 기본음이 두꺼워져 그대로도 +0.5~3dB.
_LOW_OVERRIDE = {"cut_lo": 450.0, "cut_hi": 1500.0, "pluck": 0.3, "finger": 0.03, "second_amp": 0.3,
                 "detune_cents": 0.8, "attack": 0.007, "gain_mul": 1.0}   # 1.15는 최대 +4dB라 다른 성부를 덮을 수 있어 1.0


def low_timbre(timbre):
    """기타 음색의 "5·6번 줄 저음" 판 이름(기타가 아니면 그대로)."""
    if timbre != "guitar":
        return timbre
    name = "guitar_low"
    if name not in TIMBRES:
        p = dict(TIMBRES["guitar"])
        p.update(_LOW_OVERRIDE)
        p["gain"] = p["gain"] * p.pop("gain_mul")
        TIMBRES[name] = p
    return name


def pizz_timbre(timbre):
    """그 음색의 pizz. 판 이름(TIMBRES에 없으면 만들어 넣음)."""
    if TIMBRES[timbre]["kind"] == "mallet":
        return timbre            # 막대 악기는 pizz.가 없음
    base = timbre if TIMBRES[timbre]["kind"] == "ks" else "guitar"
    name = base + "_pizz"
    if name not in TIMBRES:
        p = dict(TIMBRES[base])
        p.update(_PIZZ_OVERRIDE)
        p["gain"] = p["gain"] * p.pop("gain_mul")  # 에너지가 짧게 몰려 첫소리가 커지는 만큼 줄여 보통 음과 비슷하게
        TIMBRES[name] = p
    return name

def midi_to_hz(midi):
    return 440.0 * 2.0 ** ((np.asarray(midi, dtype=float) - 69.0) / 12.0)


def timbre_for_part(part_name, instrument):
    """성부 이름·악기 정보(musicxml_reader의 instruments 항목)로 기본 음색을 고른다.
    GM 악기 번호(program, 1~128) 기준: 1~8 피아노, 25~32 기타, 33~40 베이스,
    41~80 현·관악기(지속음). 번호가 없으면 이름으로 짐작한다."""
    program = (instrument or {}).get("program")
    name = f"{part_name} {(instrument or {}).get('name', '')}".lower()
    if ("contrabass" in name and ("guitar" in name or "gtr" in name)) or "cb. gtr" in name or "cb.gtr" in name             or "콘트라베이스 기타" in name:
        return "guitar"     # 콘트라베이스 기타는 기타의 일종 — 일반 기타보다 한 옥타브 낮을 뿐, 같은 뜯는 소리(사용자 지적 2026-10-11)
    if "bass" in name or "베이스" in name or "contrabass" in name or "cb." in name:
        # 콘트라베이스 "현악기"(활)와 베이스 "기타"(뜯음)를 구분 — 둘 다 GM 33~44 언저리
        if program is not None and 41 <= program <= 48 and "guitar" not in name:
            return "sustain"
        return "bass"
    # 채로 치는 막대 악기 — 이름 우선(영어·한글), 없으면 GM 번호(10 글로켄슈필·12 비브라폰=메탈로폰·14 자일로폰)
    if "xylophone" in name or "자일로폰" in name or "실로폰" in name:
        return "xylophone"
    if "glockenspiel" in name or "글로켄슈필" in name or "글로켄쉬필" in name:
        return "glockenspiel"
    if "metallophone" in name or "메탈로폰" in name or "vibraphone" in name or "비브라폰" in name:
        return "metallophone"
    if program is not None:
        if program == 14:
            return "xylophone"
        if program == 10:
            return "glockenspiel"
        if program == 12:
            return "metallophone"
        if 1 <= program <= 8:
            return "piano"
        if 25 <= program <= 32:
            return "guitar"
        if 33 <= program <= 40:
            return "bass"
        if 41 <= program <= 80:
            return "sustain"
    if "guitar" in name or "기타" in name or "gtr" in name:
        return "guitar"
    if "piano" in name or "피아노" in name:
        return "piano"
    return "guitar"


def _velocity_amp(velocity):
    """세기 → 진폭. 사람 귀(로그) 감각에 맞게 대략 제곱 곡선."""
    return (max(1, min(127, velocity)) / 127.0) ** 1.6


def _karplus_strong(freq, n_out, t60, cutoff_hz, pluck, rng, sr):
    """뜯는 현 소리 n_out샘플(정확히 freq Hz)."""
    period = sr / freq                       # 원하는 주기(샘플, 실수)
    # 되먹임 지연 N + 평균 필터의 반 샘플 = 실제 주기. 원하는 주기보다 조금 긴 정수
    # 주기로 만들어(=조금 낮은 음) 나중에 빨리 읽어 높이를 맞춘다.
    n = max(2, int(np.ceil(period - 0.5)))
    gen_period = n + 0.5
    ratio = gen_period / period              # ≥ 1: 생성음을 이만큼 빨리 읽음
    n_gen = int(np.ceil(n_out * ratio)) + 2

    # 들뜸(excitation): 한 주기짜리 파형을 배음 단위로 직접 만든다 — 각 배음 k의 크기 =
    # 저역통과(cutoff_hz 위로 -12dB/옥타브) × 뜯는 위치 빗살(|sin(πkβ)|), 위상은 무작위.
    # 샘플 단위 1차 필터로 하면 차단 주파수가 "주기 길이"에 묶여 저음일수록 지나치게
    # 밝아졌다(41Hz 음의 최대 성분이 1.7kHz, 2026-10-07 실측) — Hz 기준으로 정해야
    # 음높이와 무관하게 같은 밝기가 된다.
    k = np.arange(n // 2 + 1)
    fk = k * sr / n
    mag = 1.0 / (1.0 + (fk / cutoff_hz) ** 2)
    mag *= np.abs(np.sin(np.pi * k * pluck)) + 0.05
    mag[0] = 0.0                              # 직류 없음
    spec = mag * np.exp(2j * np.pi * rng.random(len(k)))
    x = np.fft.irfft(spec, n)
    x = np.append(x, x[0])                    # 되먹임 첫 블록이 n+1샘플을 읽으므로
    x /= max(1e-9, np.max(np.abs(x)))

    # 주기당 손실 g: t60초에 -60dB(평균 필터 자체 감쇠는 기본음엔 거의 없어 무시)
    g = 10.0 ** (-3.0 * gen_period / (t60 * sr))
    y = np.zeros(n_gen + n + 1)
    y[:n + 1] = x
    pos = n + 1
    half_g = 0.5 * g
    while pos < len(y):
        end = min(pos + n, len(y))
        k = end - pos
        y[pos:end] = half_g * (y[pos - n:pos - n + k] + y[pos - n - 1:pos - n - 1 + k])
        pos = end
    # 음높이 맞추기(리샘플링) → 직류 차단(되먹임 첫 몇 주기에 생기는 치우침 제거)
    t = np.arange(n_out) * ratio
    out = np.interp(t, np.arange(len(y)), y)
    return lfilter([1.0, -1.0], [1.0, -_DC_BLOCK_R], out)


def _additive(freq, n_out, harmonics, rolloff, sr):
    t = np.arange(n_out) / sr
    out = np.zeros(n_out)
    nyq = sr / 2
    for k in range(1, harmonics + 1):
        if freq * k >= nyq * 0.9:
            break
        out += np.sin(2 * np.pi * freq * k * t) / k ** rolloff
    peak = np.max(np.abs(out)) if n_out else 1.0
    return out / max(peak, 1e-9)


def _adsr(n_hold, n_total, p, sr):
    """n_hold샘플 동안 attack→decay→sustain, 그 뒤 n_total까지 release."""
    env = np.full(n_total, p["sustain"])
    na = max(1, int(p["attack"] * sr))
    nd = max(1, int(p["decay"] * sr))
    a_end = min(na, n_total)
    env[:a_end] = np.linspace(0, 1, na, endpoint=False)[:a_end]
    d_end = min(na + nd, n_total)
    if d_end > na:
        env[na:d_end] = np.linspace(1, p["sustain"], nd, endpoint=False)[:d_end - na]
    level = env[min(n_hold, n_total) - 1] if n_hold > 0 else 0.0
    if n_total > n_hold:
        env[n_hold:] = np.linspace(level, 0, n_total - n_hold)
    return env


class NoteSynth:
    """음 하나 → float32 샘플 배열. 같은 (음색, 높이, 세기)는 캐시해 둔다."""

    def __init__(self, sr=SR, seed=1234):
        self.sr = sr
        self._rng = np.random.default_rng(seed)
        self._cache = {}
        self._counter = {}

    def render(self, midi, velocity, sound_sec, timbre="guitar"):
        p = TIMBRES[timbre]
        sr = self.sr
        n_hold = max(1, int(round(sound_sec * sr)))
        n_rel = max(1, int(round(p["release"] * sr)))
        n_total = n_hold + n_rel
        amp = _velocity_amp(velocity) * p["gain"]
        if p.get("high_boost_db"):
            k = min(1.0, max(0.0, (midi - p["high_from"]) / max(1, p["high_full"] - p["high_from"])))
            amp *= 10.0 ** (p["high_boost_db"] * k / 20.0)
        freq = float(midi_to_hz(midi))

        if p["kind"] == "ks":
            base = self._ks_cached(timbre, midi, velocity, freq, n_total)
            inst = None
            if p.get("vib_cents") and midi >= p["vib_from"] and sound_sec >= p["vib_min_sec"]:
                inst = self._vib_curve(n_total, p)
                out = self._vibrato(base, n_total, p, inst)
            else:
                out = base[:n_total].copy()
            if p.get("bow_mix", 0) > 0:
                # 활로 켠 듯한 지속음을 뜯은 소리 뒤로 차오르게(4번 줄 첼로) — 비브라토는 뜯은 소리와 같은 곡선
                out = out * p.get("pluck_mix", 1.0) + p["bow_mix"] * self._bowed(freq, n_hold, n_total, p, inst)
            # 음 끝: n_hold 뒤 release 동안 0으로(손으로 줄을 막는 것) — 직선보다 지수형이
            # 실제 줄 멈춤에 가깝다(처음에 빨리 줄고 끝이 짧게 남음)
            env = np.exp(-np.linspace(0.0, 6.0, n_total - n_hold))
            out[n_hold:] *= (env - env[-1]) / max(1e-9, 1.0 - env[-1])  # 끝은 정확히 0
            fade_sec = p.get("attack", _FADE_IN_SEC)
            if p.get("finger", 0) > 0:
                burst = self._finger_noise(timbre)
                k = min(len(burst), len(out))
                # 세게 뜯을수록 마찰음도 커짐(음 자체보다 완만하게)
                out[:k] += burst[:k] * p["finger"] * (max(1, velocity) / 127.0) ** 0.5
        elif p["kind"] == "mallet":
            out = self._mallet_cached(timbre, midi, velocity, freq, n_total)
            if n_total > n_hold:      # 손으로 막아 끔 — 지수로 줄여 끝은 정확히 0
                env = np.exp(-np.linspace(0.0, 6.0, n_total - n_hold))
                out[n_hold:] *= (env - env[-1]) / max(1e-9, 1.0 - env[-1])
            fade_sec = p["attack"]
        else:
            out = _additive(freq, n_total, p["harmonics"], p["rolloff"], sr) * _adsr(n_hold, n_total, p, sr)
            fade_sec = _FADE_IN_SEC
        n_fade = min(len(out), int(fade_sec * sr))
        out[:n_fade] *= np.linspace(0, 1, n_fade, endpoint=False)
        return (out * amp).astype(np.float32)

    def _mallet_cached(self, timbre, midi, velocity, freq, n_total):
        """막대 소리 n_total샘플(복사본) — (음색, 높이, 세기 단계)별로 한 번만 합성해 캐시."""
        tier = min(range(len(_VEL_TIERS)), key=lambda i: abs(velocity - _VEL_TIER_REP[i]))
        key = ("mallet", timbre, midi, tier)
        base = self._cache.get(key)
        if base is None:
            base = self._mallet_base(TIMBRES[timbre], freq, _VEL_TIER_REP[tier] / 127.0, timbre)
            self._cache[key] = base
        if n_total <= len(base):
            return base[:n_total].copy()
        out = np.zeros(n_total)
        out[:len(base)] = base
        return out

    def _mallet_base(self, p, freq, vel, timbre):
        sr = self.sr
        # 높은 음일수록 빨리 사라짐(막대가 작아 에너지가 빨리 빠짐)
        pitch_scale = max(p["t60_min"] / p["t60_ref"], (261.63 / freq) ** p["t60_exp"])
        t60_base = p["t60_ref"] * pitch_scale
        t60s = [t60_base * ds for _r, _a, ds in p["partials"]]
        n = int(min(8.0, max(t60s)) * sr) + 1
        t = np.arange(n) / sr
        out = np.zeros(n)
        nyq = sr / 2 * 0.95
        for i, ((ratio, amp, _ds), t60) in enumerate(zip(p["partials"], t60s)):
            f = freq * ratio
            if f >= nyq:
                continue
            a = amp * vel ** (p["bright"] * i)
            # 막대마다 부분음 위상은 제각각이지만 시작은 같이 부딪히므로 0 위상 + 아주 작은 높이 어긋남(변화감)
            out += a * np.sin(2 * np.pi * f * t) * 10.0 ** (-3.0 * t / max(1e-3, t60))
        click = p.get("click", 0.0)
        if click > 0:
            k = min(n, int(0.006 * sr))
            noise = np.diff(self._rng.standard_normal(k + 1)) * np.exp(-np.arange(k) / (0.0015 * sr))
            out[:k] += click * vel * 0.5 * noise
        peak = np.max(np.abs(out))
        return (out / max(peak, 1e-9)).astype(np.float64)

    def _vib_curve(self, n, p):
        """비브라토 순간 주파수 배율 1 + d·e(t)·sin — e(t)는 뜯은 뒤 vib_delay초부터 0.3초에 걸쳐 0→1
        (실제 연주처럼 비브라토가 서서히 걸림), 빠르기는 음마다 ±8%."""
        t = np.arange(n) / self.sr
        e = np.clip((t - p["vib_delay"]) / 0.3, 0.0, 1.0)
        rate = p["vib_rate"] * self._rng.uniform(0.92, 1.08)
        d = 2.0 ** (p["vib_cents"] / 1200.0) - 1.0
        return 1.0 + d * e * np.sin(2 * np.pi * rate * t + self._rng.uniform(0, 2 * np.pi))

    def _bowed(self, freq, n_hold, n, p, inst=None):
        """활로 켠 현 소리(4번 줄 첼로 판용) — 배음이 고르게 많은 톱니파 꼴에 첼로 몸통 울림(약 280Hz·
        1100Hz 부근 봉우리, 3kHz 위로 줄어듦)을 입히고, bow_attack초에 걸쳐 차올라 음 길이 동안 유지, 끝은
        0.15초에 줄임. 활 마찰음(1.5~5kHz 잡음)을 조금. 크기는 최대 1."""
        sr = self.sr
        t = np.arange(n) / sr
        f_inst = freq * (inst if inst is not None else np.ones(n))
        # 한 주기 파형표(높이마다 한 번, 캐시)를 비브라토 위상대로 읽는다 — 배음마다 사인을 음 길이만큼
        # 계산하면 오펜바흐 합성이 7.9 → 13.6초로 늘었다
        table = self._bow_table(freq, {"low_hz": p.get("bow_low_hz", 280.0), "low_amp": p.get("bow_low_amp", 1.0),
                                       "roll_hz": p.get("bow_roll_hz", 3000.0)})
        cycles = np.cumsum(f_inst) / sr
        L = len(table)
        pos = (cycles % 1.0) * L
        out = np.interp(pos, np.arange(L + 1), np.append(table, table[0]))
        noise = self._cache.get("bow_noise")   # 미리 걸러 둔 8초 잡음에서 잘라 씀(음마다 거르면 느림)
        if noise is None:
            from scipy.signal import butter, sosfilt
            sos = butter(2, (1500.0, 5000.0), "bandpass", fs=sr, output="sos")
            noise = sosfilt(sos, self._rng.standard_normal(8 * sr))
            noise = self._cache["bow_noise"] = noise / np.max(np.abs(noise))
        if n <= len(noise):
            s0 = int(self._rng.integers(0, len(noise) - n + 1))
            out += 0.04 * noise[s0:s0 + n]
        else:
            out += 0.04 * np.resize(noise, n)
        env = np.clip(t / p.get("bow_attack", 0.12), 0.0, 1.0) ** 1.5 * np.exp(-t / p.get("bow_decay", 4.0))
        n_rel = n - n_hold
        if n_rel > 0:
            env[n_hold:] *= np.linspace(1.0, 0.0, n_rel)
        return out * env

    def _bow_table(self, freq, bp=None):
        """활 소리 한 주기(2048샘플) — 배음 1/k 크기에 첼로 몸통 곡선(약 280Hz·1100Hz 봉우리, 3kHz 위
        줄어듦), 4kHz까지. 최대 1."""
        bp = bp or {"low_hz": 280.0, "low_amp": 1.0, "roll_hz": 3000.0}
        key = ("bow", round(freq, 2), tuple(sorted(bp.items())))
        table = self._cache.get(key)
        if table is None:
            L = 2048
            ph = 2 * np.pi * np.arange(L) / L
            table = np.zeros(L)
            k = 1
            while k * freq < min(4000.0, self.sr * 0.45):
                fk = k * freq
                body = (0.35 + bp["low_amp"] * np.exp(-np.log(fk / bp["low_hz"]) ** 2 / (2 * 0.35 ** 2))
                        + 0.6 * np.exp(-np.log(fk / 1100.0) ** 2 / (2 * 0.3 ** 2))) / (1.0 + (fk / bp["roll_hz"]) ** 2)
                table += body / k * np.sin(k * ph + self._rng.uniform(0, 2 * np.pi))
                k += 1
            table /= max(1e-9, np.max(np.abs(table)))
            self._cache[key] = table
        return table

    def _vibrato(self, base, n, p, inst=None):
        """시간 축을 살짝 늘였다 줄였다 해서(순간 주파수 = f0·inst) 음높이를 흔든다(inst: _vib_curve)."""
        if inst is None:
            inst = self._vib_curve(n, p)
        idx = np.concatenate([[0.0], np.cumsum(inst[:-1])])
        return np.interp(idx, np.arange(len(base)), base).astype(np.float32)

    def _finger_noise(self, timbre):
        """뜯는 순간의 손가락 마찰음 — 1.5~5kHz 대역 잡음을 수 ms 만에 사라지게. 변형 몇 개를
        돌려 써서 매번 똑같지 않게(붙여 준 참고 자료의 "줄을 누르거나 뗄 때의 미세한 노이즈")."""
        key = ("finger", timbre)
        bursts = self._cache.get(key)
        if bursts is None:
            from scipy.signal import butter, sosfilt
            n = int(_FINGER_SEC * self.sr)
            t = np.arange(n) / self.sr
            sos = butter(2, (1500.0, 5000.0), "bandpass", fs=self.sr, output="sos")
            bursts = []
            for _ in range(_VARIANTS):
                b = sosfilt(sos, self._rng.standard_normal(n)) * np.exp(-t / (_FINGER_SEC / 5))
                bursts.append((b / max(1e-9, np.max(np.abs(b)))).astype(np.float32))
            self._cache[key] = bursts
        i = self._counter.get(key, 0)
        self._counter[key] = (i + 1) % len(bursts)
        return bursts[i]

    def _ks_cached(self, timbre, midi, velocity, freq, n_needed):
        """n_needed샘플 이상 길이의 KS 소리(캐시). -60dB(t60) 뒤는 무음으로 채운다."""
        p = TIMBRES[timbre]
        tier = next(i for i, top in enumerate(_VEL_TIERS) if velocity <= top)
        cutoff = p["cut_lo"] + (p["cut_hi"] - p["cut_lo"]) * (_VEL_TIER_REP[tier] / 127.0)
        t60 = float(np.clip(p["t60_ref"] * (110.0 / freq) ** 0.4, 0.6, 10.0))
        t60 *= p.get("tier_t60", (1.0, 1.0, 1.0))[tier]
        key = (timbre, midi, tier)
        variant = self._counter.get(key, 0)
        self._counter[key] = (variant + 1) % _VARIANTS
        ck = key + (variant,)
        wave = self._cache.get(ck)
        if wave is None or len(wave) < n_needed:
            # 짧은 음 위주라 필요한 만큼만(1초 단위로 올림) 만든다 — 더 긴 음이 오면 다시 만듦
            n_live = int(t60 * self.sr)
            n_make = min(n_live, int(np.ceil(n_needed / self.sr)) * self.sr)
            wave = _karplus_strong(freq, n_make, t60, cutoff, p["pluck"], self._rng, self.sr)
            if p.get("second_amp", 0) > 0:
                # 줄 두 개 겹치기 — 아주 조금 높고 더 빨리 죽는 둘째 소리
                freq2 = freq * 2.0 ** (p["detune_cents"] / 1200.0)
                wave2 = _karplus_strong(freq2, n_make, t60 * p["second_t60"], cutoff, p["pluck"],
                                        self._rng, self.sr)
                wave = (wave + p["second_amp"] * wave2) / (1.0 + 0.7 * p["second_amp"])
            wave = wave.astype(np.float32)
            self._cache[ck] = wave
        if len(wave) < n_needed:
            wave = np.concatenate([wave, np.zeros(n_needed - len(wave), np.float32)])
        return wave
