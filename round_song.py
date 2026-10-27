# -*- coding: utf-8 -*-
"""돌림노래(윤창) 악보 — 제목에 "돌림노래"가 들어 있으면 성부마다 노래를 시작하는 마디(처음 음표가 나오는 마디)를 알아내
그 성부 보표의 띠를 성부 색으로 칠할 수 있게 한다(칠하는 건 app.py — 재생 커서가 그 마디에 닿을 때).

read_round_map(path) → None(돌림노래 아님) 또는 {마디 인덱스: [(첫 보표 번호, 보표 수, 색, 성부 이름)]}.
마디 인덱스는 인쇄된 마디 순서(0부터).
"""
import os

import musicxml_reader

KEYWORD = "돌림노래"

# 성부 색(옅은 파스텔 — 검은 음표 위에 곱해 칠함). 성부 번호 순.
PART_COLORS = ["#C6DDF3", "#CDE8BC", "#FBDDAA", "#EBCDE6", "#F6C4BE", "#FFF0A8", "#D6D0F0", "#CFEAE6"]


def _titles(root):
    out = [root.findtext("movement-title"), root.findtext("work/work-title"), root.findtext("work/work-number")]
    out += [w.text for w in root.iter("credit-words")]
    return [t.strip() for t in out if t and t.strip()]


def is_round(root, path=""):
    """제목(작품·악장 제목, 표지 글자)이나 파일 이름에 "돌림노래"가 들어 있나."""
    stem = os.path.splitext(os.path.basename(str(path)))[0]
    return any(KEYWORD in t for t in _titles(root) + [stem])


def _starts_singing(measure):
    """마디에 소리 나는 음표(쉼표·꾸밈음 아님)가 있나."""
    return any(n.find("rest") is None and n.find("grace") is None and n.find("pitch") is not None
               for n in measure.findall("note"))


def read_round_info(path):
    """돌림노래면 {"entries": [(첫 보표 번호, 보표 수, 시작 마디 인덱스, 성부 이름)], "chords": 마디별 코드 구간 또는 None}.
    chords = 악보에 적힌 코드 이름(어느 성부에든 있으면)으로 만든 [(시작 비율, 끝 비율, 색, 코드)] 목록 — 있으면 앱이 노래하고 있는
    성부 보표를 코드 색으로 칠한다(harmonize.given_chord_rows). 돌림노래가 아니면 None."""
    import harmonize
    root = musicxml_reader._load_root(path)
    if not is_round(root, path):
        return None
    names = {sp.get("id"): (sp.findtext("part-name") or "").strip() for sp in root.findall("part-list/score-part")}
    entries, chords, staff0 = [], None, 0
    for pi, part in enumerate(root.findall("part")):
        n_staves = 1
        first = part.find("measure/attributes/staves")
        if first is not None and (first.text or "").strip().isdigit():
            n_staves = max(1, int(first.text))
        for mi, m in enumerate(part.findall("measure")):
            if _starts_singing(m):
                entries.append((staff0, n_staves, mi, names.get(part.get("id")) or f"성부 {pi + 1}"))
                break
        if chords is None:
            chords = harmonize.given_chord_rows(part)
        staff0 += n_staves
    return {"entries": entries, "chords": chords} if entries else None


def read_round_map(path):
    root = musicxml_reader._load_root(path)
    if not is_round(root, path):
        return None
    names = {sp.get("id"): (sp.findtext("part-name") or "").strip() for sp in root.findall("part-list/score-part")}
    result = {}
    staff0 = 0
    for pi, part in enumerate(root.findall("part")):
        n_staves = 1
        first = part.find("measure/attributes/staves")
        if first is not None and (first.text or "").strip().isdigit():
            n_staves = max(1, int(first.text))
        for mi, m in enumerate(part.findall("measure")):
            if _starts_singing(m):
                result.setdefault(mi, []).append((staff0, n_staves, PART_COLORS[pi % len(PART_COLORS)],
                                                  names.get(part.get("id")) or f"성부 {pi + 1}"))
                break
        staff0 += n_staves
    return result or None
