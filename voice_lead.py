# -*- coding: utf-8 -*-
"""반주 성부 진행 — 화성법으로 성부를 함께 고르고(뼈대), 대위법으로 움직임을 성부끼리 고르게 나눈다(2026-10-27).

사용자 요청: "멜로디에 화성 넣기 할 때 가능한 한 모든 성부가 대위법·화성법을 활용해 부담(움직임 — 음 개수·리듬)을
고르게 분배". 예전엔 성부마다 따로 음을 골라(1성부만 분산화음으로 바쁘고 나머지는 길게) 성부 사이 병행 5·8도·
엇갈림을 보지 않았다.

1. 뼈대(plan_skeleton) — 코드 칸마다 모든 반주 성부의 음(구성음 하나씩)을 함께 고르고 곡 전체를 빔 탐색으로 잇는다.
   - 칸 안 점수: 화음 갖춤(근음·3음 빠지면 감점), 겹침(근음 겹침은 좋고 이끎음·7음 겹침은 나쁨, 장3화음의 3음 겹침은
     조금 나쁨), 성부 차례(엇갈림 금지), 간격(위 이웃 성부끼리 한 옥타브 안), 1성부는 멜로디 아래(멜로디보다 높은
     음역이 기본인 악기는 예외), 저음은 근음(전위는 감점), 악기 편한 음역.
   - 칸 사이 점수: 성부마다 공통음 유지 > 순차 > 도약, 병행 5·8도(멜로디 포함 모든 쌍), 바깥 성부 숨은 5·8도,
     이끎음 → 으뜸음, 7음 → 아래로 해결, 모든 성부가 멜로디와 같은 방향이면 감점(반진행 권장).
2. 움직임(plan_motion) — 마디마다 "움직이는 성부"를 하나 정해(지금까지 음을 가장 적게 낸 성부부터, 바로 앞 마디와
   다른 성부를 우선) 화음 밖 음(경과음·반음계 경과음·이웃음·이탈음·선행음, 마디·성부마다 돌려 씀)과 분산화음으로
   다음 칸 음까지 잇고, 나머지 성부는 칸 길이만큼 받친다 — 받칠 때도 앞 칸 음을 붙들었다 풀어 계류음을 만든다.
   멜로디가 바쁜 마디(평균 8분음표 이상)는 움직임을 한 단계 느리게(상보 리듬), 마지막 마디(마침)는 모두 받친다.
   사용자가 정한 "가장 빠른 음표"·"화음 음 수"는 한계 — 곡에서 MIN_USES곳 넘게 나오면 되고 나머지는 내용에 따라
   탄력적으로(plan_motion 설명). 저음 성부는 보장도 없이 한계로만(unit_cap).
   자동 블록 방식의 화음 성부는 움직일 때 같은 화음을 되풀이한다.
"""
import bisect
from dataclasses import dataclass

STYLE_UNIT_Q = {"arpeggio": 0.5, "block": 1.0, "simple": 1.0}   # 움직이는 성부의 기본 음표(4분음표 단위)
BUSY_DENSITY = 2.0      # 멜로디가 4분음표당 이만큼 넘게 음을 내면 "바쁜 마디"
_BEAM = 24              # 빔 탐색에서 칸마다 남기는 경로 수
_CANDS = 48             # 칸마다 볼 성부 조합 수(칸 안 점수 좋은 순)
PARALLEL_COST = 15.0    # 병행 5·8도 감점 — 화음 갖춤(근음 6·3음 5)보다 무겁게
MIN_USES = 3            # 정한 화음 음 수·가장 빠른 음표가 곡에서 적어도 이만큼 나오면 됨(나머지는 탄력적으로)
SUSPEND_EVERY = 2       # 계류음을 쓸 수 있는 받침 칸 중 이만큼에 하나꼴로 씀
SKELETON_JITTER = 1.5   # 만들 때마다 다른 곡이 되도록 뼈대 조합 점수에 더하는 무작위 폭(병행 감점 15보다 훨씬 작게)


@dataclass
class VoiceSpec:
    """반주 성부 하나 — lo~hi 음역(소리 높이 MIDI), role "top"/"mid"/"bass", mono(한 번에 한 음),
    stack(받칠 때 쌓는 음 수), unit(정한 가장 빠른 음표, 4분음표 단위 — None이면 돌려 맡기기),
    drop_root(드롭 D 개방 D — 근음 D 코드의 저음), stack_hi(저음 화음을 쌓을 때 위 한계), above(멜로디 위가 기본),
    unit_cap(돌려 맡기기로 움직일 때 이보다 빠르지 않게 — 저음 성부에 정한 가장 빠른 음표),
    flex_stack(stack이 사용자가 정한 최대 — 곳곳에서 줄여 씀, plan_motion)."""
    lo: int
    hi: int
    role: str
    mono: bool = False
    stack: int = 1
    unit: float = None
    drop_root: int = None
    stack_hi: int = None
    above: bool = False
    unit_cap: float = None
    flex_stack: bool = False


class Harmony:
    """칸 하나의 화음 정보 — pcs(근음·3음·5음(·7음) 음이름 0~11), 딸림 기능인가, 7음."""

    def __init__(self, chord, line_pc):
        self.chord = chord
        self.pcs = [line_pc(n) for n in chord.lines]
        self.root, self.third = self.pcs[0], self.pcs[1]
        self.fifth = self.pcs[2] if len(self.pcs) > 2 else None
        self.seventh = self.pcs[3] if len(self.pcs) > 3 else None
        self.major = chord.quality in ("major", "dominant")
        self.dominant = chord.degree == 4 or chord.roman.startswith("vii")


# ---------------------------------------------------------------- 뼈대: 성부 함께 고르기

def _mel_at(mel_notes, q):
    """q에 울리는 멜로디 음(없으면 None)."""
    for st, d, p in mel_notes:
        if st <= q + 1e-9 < st + d:
            return p
    return None


def _mel_lo(mel_notes, a, b):
    ps = [p for st, d, p in mel_notes if st < b - 1e-9 and st + d > a + 1e-9]
    return min(ps) if ps else None


# 화음을 쌓는 성부(저음 제외)는 뼈대 음 = 화음 맨 위 — 그 아래로 쌓을 자리(반음)를 남긴다. 자리가 없으면 위로
# 쌓이면서 맨 위 음이 뼈대와 달라져 뼈대에서 막은 병행이 실제 소리에 생겼다(건반 블록 화음, 2026-10-27).
_STACK_ROOM = {2: 5, 3: 9, 4: 12, 5: 17, 6: 21}     # 맨 위가 근음일 때 아래 구성음들이 내려가는 최대 거리


def _options(v, h, mel_lo):
    """성부 하나의 후보 음 [(midi, 칸 안 점수)]."""
    out = []
    center = (v.lo + v.hi) / 2
    lo = v.lo
    if v.stack > 1 and v.role != "bass":
        lo = min(v.hi - 11, v.lo + _STACK_ROOM.get(v.stack, 21))
    for m in range(lo, v.hi + 1):
        pc = m % 12
        if pc not in h.pcs:
            continue
        c = 0.02 * abs(m - center)
        if v.role == "bass":
            if v.drop_root is not None and h.root == 2 and pc == 2:
                if m != v.drop_root:
                    continue          # 드롭 D: 근음 D 코드는 6번 줄 개방 D(드롭 D로 바꾸는 까닭)
            elif pc == h.third:
                c += 1.5              # 첫째 자리바꿈
            elif pc == h.fifth:
                c += 4.0              # 둘째 자리바꿈
            elif pc == h.seventh:
                c += 3.0
        if v.role == "top" and mel_lo is not None and not v.above and m >= mel_lo:
            # 1성부가 멜로디보다 높음(멜로디 위가 기본인 악기는 예외). 아래 성부들은 차례 규칙으로 1성부를 따라오므로
            # 따로 보지 않는다 — 멜로디가 낮아 1성부가 어쩔 수 없이 위로 가면 멜로디를 감싸는 게 자연스럽다
            # (가운데 성부까지 멜로디 아래로 끌어내리면 위 이웃과 간격이 벌어졌다, 콜 니드라이 2026-10-27).
            c += 2.0 + 0.5 * (m - mel_lo)
        out.append((m, c))
    return out


def _state_cost(state, h, mel, voices, leading_pc):
    pcs = [m % 12 for m in state] + ([mel % 12] if mel is not None else [])
    c = 0.0
    if h.root not in pcs:
        c += 6.0
    if h.third not in pcs:
        c += 5.0
    if h.seventh is not None and h.seventh not in pcs:
        c += 2.0
    if h.fifth is not None and h.fifth not in pcs:
        c += 0.5
    if pcs.count(leading_pc) > 1 and h.dominant:
        c += 6.0
    if h.seventh is not None and pcs.count(h.seventh) > 1:
        c += 5.0
    if h.major and pcs.count(h.third) > 1:
        c += 1.5
    if mel is not None and mel in state:
        c += 2.0                               # 멜로디와 같은 음(같은 높이)으로 겹침
    n = len(state)
    for i in range(n - 1):
        gap = state[i] - state[i + 1]
        lim = 24 if i + 1 == n - 1 and voices[i + 1].role == "bass" else 12
        if gap > lim:
            c += 1.0 * (gap - lim)
    return c


def _combos(voices, h, mel_lo, mel, leading_pc):
    """칸 하나의 성부 조합 [(state, 칸 안 점수)] — 위에서부터 차례(엇갈림 없이)로. 없으면 차례를 풀어 다시.
    반환: (점수 좋은 _CANDS개, 성부별 후보 음 opts)."""
    opts = [_options(v, h, mel_lo) for v in voices]
    if any(not o for o in opts):
        return [], opts
    out = []

    def rec(i, prev, acc, cost):
        if i == len(voices):
            out.append((tuple(acc), cost))
            return
        for m, c in opts[i]:
            if prev is not None and m >= prev:
                continue
            if prev is not None and prev - m > 24:
                continue
            acc.append(m)
            rec(i + 1, m, acc, cost + c)
            acc.pop()
    rec(0, None, [], 0.0)
    if not out:          # 음역 때문에 차례를 못 지킴(높은 악기가 저음 등) — 엇갈림을 감점으로만
        import itertools
        for combo in itertools.product(*opts):
            state = tuple(m for m, _ in combo)
            cross = sum(1 for a, b in zip(state, state[1:]) if b >= a)
            out.append((state, sum(c for _, c in combo) + 6.0 * cross))
    scored = [(s, c + _state_cost(s, h, mel, voices, leading_pc)) for s, c in out]
    scored.sort(key=lambda x: x[1])
    return scored[:_CANDS], opts


def _near(prev, opts, voices, h, mel, leading_pc):
    """앞 칸 조합 prev에서 성부마다 가장 가까운 구성음 둘씩으로 만든 조합(차례 지킴) [(state, 칸 안 점수)] —
    칸 안 점수만으로 고른 후보에 부드럽게 이어지는 조합이 빠지는 걸 막는다(음역이 넓은 4성부에서 병행이 남았음)."""
    import itertools
    picks = []
    for p, o in zip(prev, opts):
        picks.append(sorted(o, key=lambda mc: abs(mc[0] - p))[:2])
    out = []
    for combo in itertools.product(*picks):
        state = tuple(m for m, _ in combo)
        if all(a > b for a, b in zip(state, state[1:])):
            out.append((state, sum(c for _, c in combo) + _state_cost(state, h, mel, voices, leading_pc)))
    return out


def _move_cost(a, b, bass):
    d = abs(a - b)
    if d == 0:
        return -0.2
    if d <= 2:
        return 0.3
    if bass and d in (5, 7, 12):
        return 0.6
    if d <= 4:
        return 1.0
    if d <= 7:
        return 2.0
    return 2.0 + 0.5 * (d - 7)


def _trans_cost(p, q, mp, mq, hp, hq, voices, leading_pc, tonic_pc):
    c = 0.0
    n = len(p)
    for i in range(n):
        c += _move_cost(p[i], q[i], voices[i].role == "bass")
    # 병행 5·8도 — 멜로디 포함 모든 쌍
    vp = ([mp] if mp is not None and mq is not None else []) + list(p)
    vq = ([mq] if mp is not None and mq is not None else []) + list(q)
    k = len(vp)
    for a in range(k):
        da = vq[a] - vp[a]
        if da == 0:
            continue
        for b in range(a + 1, k):
            db = vq[b] - vp[b]
            if db == 0 or (da > 0) != (db > 0):
                continue
            i1, i2 = abs(vp[a] - vp[b]) % 12, abs(vq[a] - vq[b]) % 12
            if i1 == i2 and i2 in (0, 7):
                c += PARALLEL_COST
    # 바깥 성부 숨은 5·8도(멜로디가 도약하며 같은 방향으로 완전 음정에 들어감)
    if mp is not None and mq is not None and voices[-1].role == "bass":
        dm, dbs = mq - mp, q[-1] - p[-1]
        if dm and dbs and (dm > 0) == (dbs > 0) and abs(dm) > 2 and abs(mq - q[-1]) % 12 in (0, 7):
            c += 3.0
    # 이끎음 → 으뜸음, 딸림7화음의 7음 → 아래로 순차
    if hp.dominant and tonic_pc in hq.pcs:
        for i in range(n):
            if p[i] % 12 == leading_pc and q[i] - p[i] != 1:
                c += 4.0 if i in (0, n - 1) else 2.0
    if hp.seventh is not None:
        for i in range(n):
            if p[i] % 12 == hp.seventh and not (-2 <= q[i] - p[i] <= 0):
                c += 3.0
    # 모든 성부가 멜로디와 같은 방향(반진행 없음)
    if mp is not None and mq is not None and mq != mp:
        if all((q[i] - p[i]) * (mq - mp) > 0 for i in range(n)):
            c += 1.0
    return c


def plan_skeleton(slots, harmonies, mel_notes, voices, tonic_pc, leading_pc, rng=None):
    """칸마다 성부별 음(뼈대) [state, ...] — state는 위 성부부터의 midi 튜플.
    rng(random.Random): 조합마다 SKELETON_JITTER 안의 작은 점수를 더해 만들 때마다 다른 배치(병행 감점보다 훨씬 작아
    규칙은 그대로). None이면 늘 같은 결과."""
    noise = {}

    def jitter(t, q):
        if rng is None:
            return 0.0
        if (t, q) not in noise:
            noise[(t, q)] = rng.random() * SKELETON_JITTER
        return noise[(t, q)]
    layers, opts, mel_end = [], [], []
    for sl, h in zip(slots, harmonies):
        a, b = sl.start, sl.start + sl.length
        mel = _mel_at(mel_notes, a)
        combos, o = _combos(voices, h, _mel_lo(mel_notes, a, b), mel, leading_pc)
        layers.append((combos, mel, h))
        opts.append(o)
        mel_end.append(_mel_at(mel_notes, b - 1e-6))   # 칸 끝 멜로디 — 경계에서 멜로디는 여기서 다음 칸 첫 음으로 감
    if any(not combos for combos, _, _ in layers):
        raise ValueError("성부 음역 안에서 화음 구성음을 찾지 못했습니다(악기 음역을 확인하세요).")
    # 빔 탐색 — beam: [(누적 점수, 조합 번호, 앞 칸 경로 번호)]
    history = []
    beam = [(c + jitter(0, q), j, -1) for j, (q, c) in enumerate(layers[0][0])]
    beam.sort()
    beam = beam[:_BEAM]
    history.append(beam)
    for t in range(1, len(layers)):
        (prev_combos, _, hp), (combos, mq, hq) = layers[t - 1], layers[t]
        mp = mel_end[t - 1]
        index = {q: j for j, (q, _) in enumerate(combos)}
        n_static = len(combos)
        nxt = {}
        for bi, (cost, pj, _) in enumerate(beam):
            p = prev_combos[pj][0]
            mine = []
            for q, sc in _near(p, opts[t], voices, hq, mq, leading_pc):
                if q not in index:                 # 이웃 조합을 이 칸 후보에 더함(경로 되짚기용으로 같은 목록에)
                    index[q] = len(combos)
                    combos.append((q, sc))
                mine.append(index[q])
            for j in list(range(n_static)) + mine:
                q, sc = combos[j]
                total = cost + sc + jitter(t, q) + _trans_cost(p, q, mp, mq, hp, hq, voices, leading_pc, tonic_pc)
                if j not in nxt or total < nxt[j][0]:
                    nxt[j] = (total, j, bi)
        beam = sorted(nxt.values())[:_BEAM]
        history.append(beam)
    best = min(range(len(beam)), key=lambda i: beam[i][0])
    path = []
    for t in range(len(layers) - 1, -1, -1):
        _, j, back = history[t][best]
        path.append(layers[t][0][j][0])
        best = back
    return path[::-1]


# ---------------------------------------------------------------- 움직임: 성부끼리 고르게

def _stack(core, v, h, below, cap=None, k=None):
    """받칠 때 쌓는 음 k개(없으면 v.stack, 아래부터) — 저음은 근음 위로 5음·옥타브·10도, 그 밖은 core 아래로
    구성음을(음역 바닥이라 모자라면 cap(위 성부·멜로디 아래 한계)까지 위로도)."""
    k = max(1, k or v.stack)
    if k == 1:
        return [core]
    if v.role == "bass":
        out = [core]
        top = v.stack_hi if v.stack_hi is not None else v.hi + 12
        fifth = next((m for m in range(core + 1, core + 13) if h.fifth is not None and m % 12 == h.fifth), None)
        third = next((m for m in range(core + 13, core + 25) if m % 12 == h.third), None)
        for m in (fifth, core + 12, third):
            if m is not None and len(out) < k and m <= top:
                out.append(m)
        return sorted(out)
    span = 24 if k > 4 else 12
    floor = max(v.lo, core - span)
    # 빠진 음이름부터, 그다음 근음·5음·3음 겹침(core 아래로 가장 가까운 것)
    order = [pc for pc in h.pcs if pc != core % 12] + [h.root, h.fifth, h.third]
    out = [core]
    for pc in order:
        if len(out) >= k:
            break
        if pc is None:
            continue
        cand = [m for m in range(floor, core) if m % 12 == pc and m not in out and m not in below]
        if cand:
            out.append(max(cand))
    while len(out) < k:                           # 그래도 모자라면 아래로 구성음 아무거나
        cand = [m for m in range(floor, min(out)) if m % 12 in h.pcs and m not in below]
        if not cand:
            break
        out.append(max(cand))
    while len(out) < k:                           # 아래 자리를 다른 성부와 같은 높이로라도(위로 쌓으면 맨 위가 뼈대와 달라짐)
        cand = [m for m in range(floor, core) if m % 12 in h.pcs and m not in out]
        if not cand:
            break
        out.append(max(cand))
    top = min(v.hi, cap if cap is not None else v.hi)
    while len(out) < k:                           # 아래가 막혔으면 위로
        cand = [m for m in range(max(out) + 1, top + 1) if m % 12 in h.pcs]
        if not cand:
            break
        out.append(min(cand))
    while len(out) < k:                           # 그래도 모자라면 악기 음역 전체에서(다른 성부와 같은 높이·멜로디 위도)
        cand = [m for m in range(v.lo, v.hi + 1) if m % 12 in h.pcs and m not in out]
        if not cand:
            break
        out.append(min(cand, key=lambda m: abs(m - core)))
    return sorted(out)


def _scale_steps(a, b, scale_pcs):
    """a에서 b 쪽으로 음계 음을 하나씩(a·b 제외)."""
    step = 1 if b > a else -1
    return [m for m in range(a + step, b, step) if m % 12 in scale_pcs]


def _chord_tones_near(core, h, lo, hi):
    return sorted(m for m in range(max(lo, core - 9), min(hi, core + 9) + 1) if m % 12 in h.pcs)


def _arp(core, h, lo, hi, n):
    """core에서 시작해 가까운 구성음을 오르내리는 분산화음 n개."""
    tones = _chord_tones_near(core, h, lo, hi)
    if core not in tones:
        tones = sorted(set(tones + [core]))
    i = tones.index(core)
    seq = tones[i:i + 3] if len(tones[i:i + 3]) >= 2 else tones[max(0, i - 2):i + 1][::-1]
    if len(seq) < 2:
        return [core] * n
    cycle = seq + seq[-2:0:-1]
    return [cycle[k % len(cycle)] for k in range(n)]


def _passing(core, target, h, scale_pcs, lo, hi, n):
    """경과음 — 다음 칸 음(target)까지 음계로 잇고 남는 앞부분은 분산화음. 못 하면 None."""
    if target is None or target == core:
        return None
    steps = [m for m in _scale_steps(core, target, scale_pcs) if lo <= m <= hi]
    if not steps or len(steps) > n - 1:
        return None
    head = _arp(core, h, lo, hi, n - len(steps))
    if head and head[-1] != core and abs(head[-1] - steps[0]) > 2:
        head = [core] * len(head)
    return head + steps


def _chromatic(core, target, lo, hi, n):
    """반음계 경과음 — target까지 반음씩(3반음 안). 못 하면 None."""
    if target is None or not 2 <= abs(target - core) <= 3:
        return None
    step = 1 if target > core else -1
    steps = [m for m in range(core + step, target, step) if lo <= m <= hi]
    if len(steps) != abs(target - core) - 1 or len(steps) > n - 1:
        return None
    return [core] * (n - len(steps)) + steps


def _neighbor(core, h, scale_pcs, lo, hi, n, up):
    """이웃음 — 구성음에서 음계 한 칸 위(아래)로 갔다 돌아옴, 남는 뒤는 분산화음. 못 하면 None."""
    step = 1 if up else -1
    nb = next((m for m in range(core + step, core + 3 * step, step) if m % 12 in scale_pcs), None)
    if nb is None or not lo <= nb <= hi or n < 3:
        return None
    return [core, nb, core] + _arp(core, h, lo, hi, n - 3 + 1)[1:]


def _escape(core, target, h, scale_pcs, lo, hi, n):
    """이탈음 — 마지막 음이 target과 반대쪽으로 음계 한 칸 비껴 나갔다가 도약으로 다음 칸 음에 들어감. 못 하면 None."""
    if target is None or target == core:
        return None
    body = _arp(core, h, lo, hi, n - 1)
    last = body[-1]
    step = -1 if target > last else 1
    esc = next((m for m in range(last + step, last + 3 * step, step) if m % 12 in scale_pcs), None)
    if esc is None or not lo <= esc <= hi or abs(target - esc) > 7:
        return None
    return body + [esc]


def _anticipation(core, target, h, lo, hi, n):
    """선행음 — 마지막 음에 다음 칸 음을 미리 냄(앞 음에서 순차일 때). 못 하면 None."""
    if target is None or target == core or not lo <= target <= hi or n < 2:
        return None
    body = _arp(core, h, lo, hi, n - 1)
    if abs(target - body[-1]) > 2:
        body[-1] = core
        if abs(target - core) > 2:
            return None
    return body + [target]


def _line(core, target, h, scale_pcs, lo, hi, n, variant=0):
    """움직이는 성부 한 칸 n음 — 화음 밖 음을 과감하게(사용자 요청 2026-10-27: "완벽한 화성만이 아니라 경과음·
    이탈음 등 여러 음을"): 경과음·반음계 경과음·이웃음(위·아래)·이탈음·선행음 중 variant(칸·성부마다 돌림)
    차례로 먼저 되는 것을 쓰고, 다 안 되면 분산화음."""
    figures = [
        lambda: _passing(core, target, h, scale_pcs, lo, hi, n),
        lambda: _neighbor(core, h, scale_pcs, lo, hi, n, True),
        lambda: _escape(core, target, h, scale_pcs, lo, hi, n),
        lambda: _chromatic(core, target, lo, hi, n),
        lambda: _neighbor(core, h, scale_pcs, lo, hi, n, False),
        lambda: _anticipation(core, target, h, lo, hi, n),
    ]
    for j in range(len(figures)):
        seq = figures[(variant + j) % len(figures)]()
        if seq is not None and len(seq) == n:
            return seq
    return _arp(core, h, lo, hi, n)


def _suspension(prev, core, i, state, v, lo, hi, t_mid, mel_notes):
    """계류음 — 받치는 성부가 앞 칸 음(prev)을 새 화음까지 붙들었다가 칸 가운데에서 음계 한 칸 아래 화음 음(core)으로
    풀린다(4-3·7-6·9-8). 풀리는 음을 다른 위 성부가 같이 내거나 멜로디와 병행이 생기면 안 씀."""
    if prev is None or not 1 <= prev - core <= 2 or not lo <= prev <= hi or v.role == "bass":
        return False
    if any(m % 12 == core % 12 for j, m in enumerate(state) if j != i and j < len(state) - 1):
        return False
    return not _parallel(prev, core, _mel_at(mel_notes, t_mid - 1e-6), _mel_at(mel_notes, t_mid))


def _parallel(p1, p2, q1, q2):
    """두 성부가 p1→p2, q1→q2로 갈 때 병행 5·8도인가."""
    if None in (p1, p2, q1, q2) or p1 == p2 or q1 == q2 or (p2 - p1) * (q2 - q1) <= 0:
        return False
    i1, i2 = abs(p1 - q1) % 12, abs(p2 - q2) % 12
    return i1 == i2 and i2 in (0, 7)


def _fix_entry(seq, i, k, slots, skeleton, mel_notes, u):
    """움직이는 성부 i의 칸 k 음들(seq) — 마지막 음에서 다음 칸 음으로 갈 때 다른 성부·멜로디와 병행이 생기면
    마지막 음을 앞 음(없으면 뼈대 음)으로 머물게 한다. 칸 안에서 멜로디와 병행이 생기는 음도 앞 음으로."""
    sl = slots[k]
    t0 = sl.start
    for j in range(1, len(seq)):                       # 칸 안: 멜로디와(멜로디는 이 음 바로 직전 → 이 음 시점)
        b = t0 + j * u
        if _parallel(seq[j - 1], seq[j], _mel_at(mel_notes, b - 1e-6), _mel_at(mel_notes, b)):
            seq[j] = seq[j - 1]
    if k + 1 >= len(slots) or len(seq) < 2:
        return seq
    cur, nxt = skeleton[k], skeleton[k + 1]
    end = sl.start + sl.length
    last = seq[-1]
    others = [(cur[j], nxt[j]) for j in range(len(cur)) if j != i]
    others.append((_mel_at(mel_notes, end - 1e-6), _mel_at(mel_notes, end)))
    if any(_parallel(last, nxt[i], a, b) for a, b in others):
        seq[-1] = seq[-2]
        if any(_parallel(seq[-1], nxt[i], a, b) for a, b in others):
            seq[-1] = cur[i]
    return seq


def _fits(length, u):
    """칸 길이를 u 음표로 나눠 2개 이상 딱 맞나 → 개수(안 맞으면 0)."""
    n = int(round(length / u)) if u else 0
    return n if n >= 2 and abs(n * u - length) <= 1e-6 else 0


def _spread(cands, need):
    """후보(곡 순서) 중 need개를 곡 전체에 고르게 퍼지게 고른다."""
    if need >= len(cands):
        return list(cands)
    return [cands[int((j + 0.5) * len(cands) / need)] for j in range(need)]


def plan_motion(slots, harmonies, skeleton, mel_notes, measures, voices, style, scale_pcs, rng=None):
    """성부별·칸별 [(길이 q, [midi …])] — 받침·움직임을 정해 리듬을 붙인다. rng(random.Random)가 있으면 움직임
    차례의 동점 정리·꾸밈 음형·계류음 자리를 무작위로(만들 때마다 다른 곡), None이면 늘 같은 결과.

    사용자가 정한 "화음 음 수"(flex_stack)·"가장 빠른 음표"(unit)는 늘 지키는 값이 아니라 한계 — 곡에서
    MIN_USES곳 넘게 나오면 되고 나머지는 내용에 따라(사용자 요청 2026-10-27):
      - 가장 빠른 음표: 그 성부도 돌려 맡기기에 끼어, 움직이는 마디에서 그 음표로(멜로디가 바쁘면 한 단계 느리게).
        그렇게 해서 MIN_USES곳이 안 되면 곡 전체에 고르게 퍼진 칸을 더 골라 그 음표로 움직인다.
      - 화음 음 수: 다 채운 화음은 프레이즈 첫머리(4마디마다)·처음·마침 칸에, 나머지는 절반 정도. 그래도 MIN_USES곳이
        안 되면 길게 받치는 칸부터 채운다. 움직이는 칸은 첫 음만 화음, 나머지는 한 음씩(경과음·분산화음)."""
    nv, ns = len(voices), len(slots)
    base_unit = STYLE_UNIT_Q.get(style, 1.0)
    onsets = [0] * nv
    plan = [[None] * ns for _ in range(nv)]          # 칸마다 움직이는 음표 길이(None = 받침)
    by_measure = {}
    for k, sl in enumerate(slots):
        by_measure.setdefault(sl.measure, []).append(k)
    order = sorted(by_measure)
    last_measure = order[-1] if order else -1
    busy = {}
    for mi in order:
        m = measures[mi]
        a, b = m["start"], m["start"] + m["length"]
        n_mel = sum(1 for st, d, p in mel_notes if a - 1e-9 <= st < b - 1e-9)
        busy[mi] = n_mel / max(1e-9, m["length"]) > BUSY_DENSITY - 1e-9
    # 1) 돌려 맡기기 — 마디마다 지금까지 음을 가장 적게 낸 성부가 움직인다
    prev_mover = None
    for mi in order:
        mover = None
        if mi != last_measure:
            tie = [rng.random() for _ in range(nv)] if rng else [(-i if voices[i].role == "bass" else i) for i in range(nv)]
            mover = min(range(nv), key=lambda i: (onsets[i], i == prev_mover, tie[i]))
            prev_mover = mover
        slow = 2 if busy[mi] else 1
        for k in by_measure[mi]:
            for i, v in enumerate(voices):
                u = None
                if i == mover:
                    u = (v.unit or base_unit) * slow
                    u = max(u, v.unit_cap or 0)
                n = _fits(slots[k].length, u)
                if n:
                    plan[i][k] = u
                onsets[i] += n or 1
    # 2) 가장 빠른 음표를 정한 성부 — MIN_USES곳 보장(마지막 마디 빼고, 멜로디가 한가한 칸 먼저)
    for i, v in enumerate(voices):
        if v.unit is None:
            continue
        have = sum(1 for k in range(ns) if plan[i][k] == v.unit)
        if have >= MIN_USES:
            continue
        cands = [k for k in range(ns) if plan[i][k] != v.unit and slots[k].measure != last_measure
                 and _fits(slots[k].length, v.unit)]
        calm = [k for k in cands if not busy[slots[k].measure]]
        pick = _spread(calm, MIN_USES - have)
        pick += _spread([k for k in cands if k not in calm], MIN_USES - have - len(pick))
        for k in pick:
            plan[i][k] = v.unit
    # 3) 칸마다 화음 음 수 — 다 채운 화음은 처음·마침·프레이즈 첫머리, 나머지는 절반 정도
    first_mi = order[0] if order else 0
    strong = {0, ns - 1} | {k for k, sl in enumerate(slots) if (sl.measure - first_mi) % 4 == 0
                            and sl.start <= measures[sl.measure]["start"] + 1e-6}
    if ns >= 2:
        strong.add(ns - 2)
    size = [[max(1, v.stack)] * ns for v in voices]
    for i, v in enumerate(voices):
        if not v.flex_stack or v.stack < 2:
            continue
        reduced = (v.stack + 1) // 2
        size[i] = [v.stack if k in strong else reduced for k in range(ns)]
        full = sum(1 for s in size[i] if s == v.stack)
        if full < MIN_USES:
            rest = sorted((k for k in range(ns) if size[i][k] != v.stack),
                          key=lambda k: (plan[i][k] is not None, -slots[k].length, k))
            for k in rest[:MIN_USES - full]:
                size[i][k] = v.stack
    # 4) 음 쓰기
    out = [[None] * ns for _ in range(nv)]
    holds = [[None] * ns for _ in range(nv)]          # 칸마다 그냥 받칠 때의 음(꾸밈을 되돌릴 때)
    for k, sl in enumerate(slots):
        h, state = harmonies[k], skeleton[k]
        nxt = skeleton[k + 1] if k + 1 < ns else None
        mel_lo = _mel_lo(mel_notes, sl.start, sl.start + sl.length)
        for i, v in enumerate(voices):
            core = state[i]
            # 움직여도 이웃 성부와 엇갈리지 않게(멜로디 위가 기본이 아니면 멜로디 아래로)
            lo = max(v.lo, state[i + 1] + 1) if i + 1 < nv else v.lo
            hi = min(v.hi, state[i - 1] - 1) if i > 0 else v.hi
            if i == 0 and mel_lo is not None and not v.above:
                hi = min(hi, mel_lo - 1)
            if not lo <= core <= hi:
                lo, hi = min(lo, core), max(hi, core)
            stack = _stack(core, v, h, set(state[i + 1:]), hi, size[i][k])
            holds[i][k] = stack
            u = plan[i][k]
            if u is None:
                # 계류음: 앞 칸 끝에 울린 음이 한 칸 위 음이면 붙들었다 풀기(받침 칸 둘에 하나꼴)
                prev = out[i][k - 1][-1][1] if k > 0 and out[i][k - 1] else None
                half = sl.length / 2
                pick = rng.random() * SUSPEND_EVERY < 1 if rng else (k + i) % SUSPEND_EVERY == 0
                if (len(stack) == 1 and prev and len(prev) == 1 and sl.length >= 2 - 1e-6 and pick
                        and _suspension(prev[0], core, i, state, v, lo, hi, sl.start + half, mel_notes)):
                    out[i][k] = [(half, [prev[0]]), (half, stack)]
                    continue
                out[i][k] = [(sl.length, stack)]
                continue
            n = _fits(sl.length, u)
            if len(stack) >= 2 and not v.flex_stack:
                seq = [stack] * n                    # 블록 방식(자동) — 같은 화음 되풀이
            else:
                target = nxt[i] if nxt is not None else None
                variant = rng.randrange(6) if rng else sl.measure + 2 * i
                line = _fix_entry(_line(core, target, h, scale_pcs, lo, hi, n, variant), i, k, slots,
                                  skeleton, mel_notes, u)
                seq = [[p] for p in line]
                if len(stack) >= 2:
                    seq[0] = stack                   # 첫 음은 화음, 나머지는 한 음씩
            out[i][k] = [(u, notes) for notes in seq]
    _repair_parallels(out, holds, slots, mel_notes)
    return out


def _repair_parallels(out, holds, slots, mel_notes, rounds=6):
    """다 쓴 뒤 곡 전체를 시점마다 훑어(voice_check와 같은 대표 음: 맨 아래 성부는 가장 낮은 음, 나머지는 가장 높은 음)
    병행 5·8도를 만든 꾸밈(화음 밖 음·계류음) 칸을 그냥 받침으로 되돌린다 — 성부마다 따로 고른 꾸밈이 같은 순간에
    겹쳐 생기는 병행까지 막는다."""
    for _ in range(rounds):
        _count, blame = _parallel_blame(out, slots, mel_notes)
        if not blame:
            return
        for v, k in blame:
            out[v][k] = [(slots[k].length, holds[v][k])]


def count_parallels(out, slots, mel_notes):
    """plan_motion 결과에 남은 병행 5·8도 수(멜로디 포함, voice_check와 같은 대표 음)."""
    return _parallel_blame(out, slots, mel_notes)[0]


def _parallel_blame(out, slots, mel_notes):
    """→ (병행 수, 꾸밈 탓인 {(성부, 칸)})."""
    nv = len(out)
    lines = []                                    # 성부별 [(시작, 끝, 대표 음, 칸)]
    for i in range(nv):
        ev = []
        for k, sl in enumerate(slots):
            t = sl.start
            for d, notes in out[i][k]:
                ev.append((t, t + d, min(notes) if i == nv - 1 else max(notes), k))
                t += d
        lines.append(ev)
    mel = sorted(mel_notes)
    lines.append([(st, st + d, p, None) for st, d, p in mel])      # 멜로디는 맨 끝(칸 없음)
    starts = [[e[0] for e in ev] for ev in lines]
    times = sorted({round(e[0], 6) for ev in lines for e in ev})

    def at(j, t):                     # 성부 j에서 t에 울리는 (대표 음, 칸) — 곡이 길어도 빠르게(이분 탐색)
        x = bisect.bisect_right(starts[j], t + 1e-9) - 1
        while x >= 0:
            s, e, p, k = lines[j][x]
            if t + 1e-9 < e:
                return p, k
            if j < nv:                # 반주 성부는 겹치지 않음 — 바로 앞이 끝났으면 없음
                break
            x -= 1                    # 멜로디는 겹칠 수 있어 조금 더 앞까지
            if t - s > 16:
                break
        return None, None

    blame, count = set(), 0
    for t0, t1 in zip(times, times[1:]):
        a = [at(j, t0) for j in range(nv + 1)]
        b = [at(j, t1) for j in range(nv + 1)]
        for x in range(nv + 1):
            for y in range(x + 1, nv + 1):
                if _parallel(a[x][0], b[x][0], a[y][0], b[y][0]):
                    count += 1
                    for v in (x, y):
                        if v < nv:
                            # 꾸밈이 있는 칸 탓 — 움직인 시점의 칸이 받침이면 앞 칸(경계로 들어가는 음)
                            k = b[v][1] if len(out[v][b[v][1]]) > 1 else a[v][1]
                            if k is not None and len(out[v][k]) > 1:
                                blame.add((v, k))
    return count, blame
