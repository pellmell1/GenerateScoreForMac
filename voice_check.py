# -*- coding: utf-8 -*-
"""화성 붙인 악보의 성부 진행 검사 — 대위법·화성법 규칙 위반 수와 성부별 움직임(2026-10-27).

harmonize로 반주 성부를 더한 악보에서 멜로디 + 반주 성부(id가 "Harm"으로 시작)를 보고 센다:
  - 병행 5도·8도(1도 포함): 두 성부가 같은 방향으로 움직여 완전 5도(또는 옥타브·같은 음)에서 다시 완전 5도
    (옥타브)로 — 모든 성부 쌍. 한 성부가 머물러 있으면(경과음 등) 셈하지 않는다.
  - 숨은 5도·8도: 바깥 성부(멜로디·맨 아래 성부)가 같은 방향으로 완전 음정에 들어가는데 멜로디가 도약할 때.
  - 성부 엇갈림: 반주 성부끼리 위 성부가 바로 아래 성부보다 낮아진 순간.
  - 멜로디 위: 반주 1성부가 멜로디보다 높아진 순간(멜로디가 낮은 곡은 피할 수 없을 때도 있어 따로 셈).
  - 간격 벌어짐: 위쪽 이웃 성부(맨 아래 성부 제외) 사이가 한 옥타브를 넘는 순간.
  - 움직임: 반주 성부마다 음을 새로 낸 횟수(화음은 한 번) — 고르게 나뉘었는지(가장 많은/적은 비율).
대표 음: 성부가 화음을 내면 위 성부들은 가장 높은 음, 맨 아래 성부는 가장 낮은 음으로 본다.

명령줄:  python voice_check.py 곡_화성.musicxml [--melody 1]
"""
import argparse

import musicxml_reader

_PERFECT = {0: "8도", 7: "5도"}


def _lines(score, part_indices):
    """성부별 [(시작 q, 끝 q, midi)] (시작 순)."""
    out = {pi: [] for pi in part_indices}
    for start, dur, midi, part, *_ in score["notes"]:
        if part in out:
            out[part].append((start, start + dur, midi))
    for v in out.values():
        v.sort()
    return out


def _onsets(notes):
    return sorted({round(s, 6) for s, _, _ in notes})


def _pitch_at(notes, t, low):
    """t에 울리는 음 중 대표 음(low면 가장 낮은 음, 아니면 가장 높은 음). 없으면 None."""
    ps = [m for s, e, m in notes if s <= t + 1e-9 < e]
    if not ps:
        return None
    return min(ps) if low else max(ps)


def analyze(score, melody_index, acc_indices):
    """score(read_musicxml 결과)에서 멜로디 성부 번호(0부터)와 반주 성부 번호들(위에서부터) → 검사 결과 dict."""
    voices = [melody_index] + list(acc_indices)
    lines = _lines(score, voices)
    times = sorted({t for v in voices for t in _onsets(lines[v])})
    low = {v: (i == len(voices) - 1) for i, v in enumerate(voices)}
    rows = [[_pitch_at(lines[v], t, low[v]) for v in voices] for t in times]
    res = {"병행 5도": 0, "병행 8도": 0, "숨은 5·8도": 0, "엇갈림": 0, "멜로디 위": 0, "간격 벌어짐": 0}
    n = len(voices)
    for prev, cur in zip(rows, rows[1:]):
        for a in range(n):
            for b in range(a + 1, n):
                pa, pb, ca, cb = prev[a], prev[b], cur[a], cur[b]
                if None in (pa, pb, ca, cb) or pa == ca or pb == cb:
                    continue
                if (ca - pa) * (cb - pb) <= 0:
                    continue                   # 반대·비껴 진행
                i1, i2 = abs(pa - pb) % 12, abs(ca - cb) % 12
                if i1 == i2 and i2 in _PERFECT:
                    res["병행 " + _PERFECT[i2]] += 1
                elif a == 0 and b == n - 1 and i2 in _PERFECT and abs(ca - pa) > 2:
                    res["숨은 5·8도"] += 1
    for row in rows:
        for a in range(n - 1):
            if row[a] is not None and row[a + 1] is not None and row[a] < row[a + 1]:
                res["멜로디 위" if a == 0 else "엇갈림"] += 1
        for a in range(1, n - 2):              # 반주 위쪽 이웃끼리(멜로디·맨 아래 제외)
            if row[a] is not None and row[a + 1] is not None and row[a] - row[a + 1] > 12:
                res["간격 벌어짐"] += 1
    moves = [len(_onsets(lines[v])) for v in acc_indices]
    res["움직임"] = moves
    res["움직임 비율"] = round(max(moves) / max(1, min(moves)), 2) if moves else 0.0
    res["검사 시점"] = len(times)
    return res


def analyze_file(path, melody_part=1):
    score = musicxml_reader.read_musicxml(path)
    root = musicxml_reader._load_root(path)
    ids = [p.get("id") or "" for p in root.findall("part")]
    acc = [i for i, pid in enumerate(ids) if pid.startswith("Harm")]
    return analyze(score, melody_part - 1, acc)


def main(argv=None):
    ap = argparse.ArgumentParser(description="화성 붙인 악보의 성부 진행 검사")
    ap.add_argument("score")
    ap.add_argument("--melody", type=int, default=1)
    args = ap.parse_args(argv)
    for k, v in analyze_file(args.score, args.melody).items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
