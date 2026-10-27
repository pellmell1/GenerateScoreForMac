# -*- coding: utf-8 -*-
"""그려진 악보가 기보 규칙(빔 묶기·기둥 방향·빔 기울기)을 지키는지 검사한다 — UI 독립.

사용자가 준 기준(2026-10-07):
1. 박자 단위로 묶기 — 4/4는 한 박(또는 두 박: 1~2박·3~4박), 3/4는 한 박씩, 6/8·9/8·12/8은
   점4분음표(8분음표 3개)씩. 빔이 이 경계를 넘으면 위반. 같은 박 안의 8분음표 이하가 빔
   없이 낱개면 "안 묶임".
2. 기둥 방향 — 빔으로 묶인 음표 머리의 과반이 가운데 줄(3선)보다 위면 기둥은 아래, 아래면
   위. 한 보표에 성부가 둘 이상이면(위 성부 위·아래 성부 아래 관례) 검사에서 뺀다.
   음 높이 차가 너무 큰 묶음(_WIDE_STEPS 이상)은 "나눠 묶는 게 나음"으로 따로 알린다.
3. 빔 기울기 — 첫 음보다 끝 음이 높으면(상행) 빔도 오른쪽이 높게, 낮으면 낮게, 같으면 수평.
   반대로 기울면 위반, 높이가 다른데 수평이면 "참고"(가운데 음이 더 튀어나온 묶음은 수평이
   표준 조판 관례라 위반으로 치지 않음).
4. 프레이즈·아티큘레이션에 따른 의도적 묶기는 음악적 판단이라 검사하지 않는다.

검사 대상은 "실제로 그려진 것" — score_view.ScoreRenderer가 Verovio에 넘기는 그대로 조판해서
MEI(음표 길이·빔 묶음)와 SVG(머리·기둥·빔 좌표)를 같은 음표 ID로 맞춰 본다.

    python notation_check.py 악보.xml [악보2.mxl ...]
"""
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict

from score_view import ScoreRenderer

_EPS = 1e-6
_WIDE_STEPS = 11          # 묶음 안 최저~최고 음 차가 이 이상(온음계 단계, 11 = 옥타브+5도)이면 알림
_FLAT_TOL_RATIO = 0.06    # 빔 높이 차가 보표 칸 간격의 이 비율 이하면 "수평"
_SAME_PITCH_TOL = 0.25    # 음 높이 차가 칸 간격의 이 비율 이하면 "같은 높이"
_MAX_EXAMPLES = 12


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _cls(e):
    return (e.get("class") or "").split()


# ---------------------------------------------------------------- 1. 박자 묶기(MEI)

def beat_windows(count, unit):
    """박자표 → (기본 묶음 길이, 허용되는 더 큰 묶음 길이 목록) — 4분음표 단위."""
    if unit == 8 and count % 3 == 0 and count >= 6:
        return 1.5, []                    # 6/8·9/8·12/8: 점4분음표
    if unit == 8:
        return (count * 0.5 if count <= 4 else 1.0), []
    beat = 4.0 / unit
    if count == 4 and unit == 4:
        return beat, [2.0]                # 4/4: 한 박, 또는 반 마디(1~2박·3~4박)
    return beat, []


def _same_window(q0, q1, size, offset):
    # 셋잇단 길이(1/6 등)를 더하면 1.0이 0.9999…가 되므로 나누기 전에 오차를 보정
    return int((q0 + offset) / size + 1e-6) == int((q1 + offset) / size + 1e-6)


def _layer_events(layer, ppq):
    """층(layer) 안의 사건을 순서대로 [(종류, id, 시작q, 길이q, 빔id 또는 None, 꾸밈음?)]."""
    out = []
    pos = [0]

    def walk(e, beam_id):
        name = _local(e.tag)
        if name in ("note", "chord", "rest", "space"):
            dur = int(e.get("dur.ppq") or 0) / ppq
            grace = e.get("grace") is not None
            out.append((name, e.get("{http://www.w3.org/XML/1998/namespace}id"), pos[0], dur, beam_id, grace))
            pos[0] += 0 if grace else dur
            return  # 화음 안 음표는 화음이 대표
        if name in ("mRest", "mSpace", "multiRest"):
            out.append((name, None, pos[0], None, None, False))
            return
        if name == "beam":
            beam_id = e.get("{http://www.w3.org/XML/1998/namespace}id")
        for c in e:
            walk(c, beam_id)

    for c in layer:
        walk(c, None)
    return out


def check_grouping(mei_root, report):
    meter = {}          # 보표 번호 -> (count, unit)
    ppq = {}
    names = {}
    for e in mei_root.iter():
        name = _local(e.tag)
        if name == "staffDef":
            n = e.get("n")
            if e.get("ppq"):
                ppq[n] = int(e.get("ppq"))
            ms = next((c for c in e if _local(c.tag) == "meterSig"), None)
            if ms is not None and ms.get("count") and ms.get("unit"):
                meter[n] = (int(ms.get("count").split("+")[0]), int(ms.get("unit")))
            elif e.get("meter.count") and e.get("meter.unit"):
                meter[n] = (int(e.get("meter.count")), int(e.get("meter.unit")))
            for c in e:
                if _local(c.tag) == "label" and (c.text or "").strip():
                    names[n] = c.text.strip()
        elif name == "scoreDef":
            # 곡 중간 박자 변경은 scoreDef에 바로(meterSig 자식 또는 meter.* 속성) 온다 — 모든 보표에 적용
            ms = next((c for c in e if _local(c.tag) == "meterSig"), None)
            cu = None
            if ms is not None and ms.get("count") and ms.get("unit"):
                cu = (int(ms.get("count").split("+")[0]), int(ms.get("unit")))
            elif e.get("meter.count") and e.get("meter.unit"):
                cu = (int(e.get("meter.count")), int(e.get("meter.unit")))
            if cu:
                for n in list(meter) or ["1"]:
                    meter[n] = cu
                meter["_all"] = cu
        elif name == "measure":
            mnum = e.get("n")
            staves = [s for s in e if _local(s.tag) == "staff"]
            layers_ev = {}
            full_len = 0.0
            for st in staves:
                n = st.get("n")
                for ly in st:
                    if _local(ly.tag) != "layer":
                        continue
                    evs = _layer_events(ly, ppq.get(n, 1))
                    layers_ev[(n, ly.get("n"))] = evs
                    if evs and evs[-1][3] is not None:
                        full_len = max(full_len, evs[-1][2] + evs[-1][3])
            for (n, _ln), evs in layers_ev.items():
                count, unit = meter.get(n, meter.get("_all", (4, 4)))
                nominal = count * 4.0 / unit
                # 못갖춘마디: 박 위치는 마디 끝에 맞춘다
                offset = nominal - full_len if 0 < full_len < nominal - _EPS else 0.0
                size, bigger = beat_windows(count, unit)
                where = f"마디 {mnum} {names.get(n, '보표 ' + str(n))}"
                # 빔 묶음별
                groups = defaultdict(list)
                for kind, eid, q, dur, beam, grace in evs:
                    if beam and not grace and kind in ("note", "chord", "rest"):
                        groups[beam].append(q)
                for beam, qs in groups.items():
                    report["beam_groups"] += 1
                    ok = _same_window(qs[0], qs[-1], size, offset) or any(
                        _same_window(qs[0], qs[-1], b, offset) for b in bigger)
                    if not ok:
                        report["cross_beat"].append(f"{where}: {len(qs)}개 묶음이 박({size:g}) 경계를 넘음")
                # 빔 없이 낱개로 있는 짧은 음표(같은 박 안에서 바로 이웃)
                prev = None
                for kind, eid, q, dur, beam, grace in evs:
                    short = kind in ("note", "chord") and not grace and dur is not None and dur < 1.0 - _EPS
                    if short and beam is None:
                        if prev is not None and _same_window(prev, q, size, offset):
                            report["unbeamed"].append(f"{where}: 같은 박 안 짧은 음표가 안 묶임")
                        prev = q
                    elif not grace:
                        prev = None
    # 같은 마디·보표 반복 알림은 하나로
    report["unbeamed"] = sorted(set(report["unbeamed"]), key=report["unbeamed"].index)


# ---------------------------------------------------------------- 2·3. 기둥·기울기(SVG)

def _translate_y(use):
    tr = use.get("transform") or ""
    try:
        inside = tr.split("translate(", 1)[1].split(")", 1)[0]
        return float(inside.split(",")[1])
    except (IndexError, ValueError):
        return None


def _path_points(path):
    nums = [float(x) for x in (path.get("d") or "").replace("M", " ").replace("L", " ").split()]
    return list(zip(nums[0::2], nums[1::2]))


def check_svg(svg_text, measure_names, staff_names, report, grace_ids=frozenset(), beam_layers=None):
    root = ET.fromstring(svg_text)
    parent = {c: p for p in root.iter() for c in p}

    def ancestor(e, cls):
        while e is not None:
            if cls in _cls(e):
                return e
            e = parent.get(e)
        return None

    for beam in root.iter():
        if "beam" not in _cls(beam) or not _local(beam.tag) == "g":
            continue
        staff = ancestor(parent.get(beam), "staff")
        measure = ancestor(parent.get(beam), "measure")
        if staff is None:
            continue
        line_ys = sorted(_path_points(p)[0][1] for p in staff if _local(p.tag) == "path" and len(_path_points(p)) == 2)
        if len(line_ys) != 5:
            continue
        mid = line_ys[2]
        space = (line_ys[4] - line_ys[0]) / 4
        mnum = measure_names.get(measure.get("id"), "?") if measure is not None else "?"
        staff_idx = [s for s in parent.get(staff) if "staff" in _cls(s)].index(staff) if parent.get(staff) is not None else 0
        where = f"마디 {mnum} {staff_names[staff_idx] if staff_idx < len(staff_names) else '보표'}"
        if report.get("_debug_ids"):
            where += f" [{beam.get('id')}]"

        # 묶음의 각 음(화음이면 화음 하나) → 머리 y들, 기둥 방향
        items = []

        def members(g):
            """이 빔에 직접 속한 음표·화음(잇단음표 묶음 안쪽까지, 안쪽 다른 빔은 제외)."""
            for c in g:
                if _local(c.tag) != "g":
                    continue
                cls = set(_cls(c))
                if {"note", "chord"} & cls:
                    yield c
                elif "beam" not in cls:
                    yield from members(c)

        for c in members(beam):
            heads = [_translate_y(u) for h in c.iter() if "notehead" in _cls(h) for u in h if _local(u.tag) == "use"]
            heads = [y for y in heads if y is not None]
            stem = next((s for s in c.iter() if "stem" in _cls(s)), None)
            if not heads or stem is None:
                continue
            pts = [pt for p in stem if _local(p.tag) == "path" for pt in _path_points(p)]
            if len(pts) < 2:
                continue
            stem_top = min(y for _, y in pts)
            stem_up = stem_top < min(heads) - space * 0.5
            items.append((heads, stem_up))
        if len(items) < 2:
            continue
        if any(c.get("id") in grace_ids for c in members(beam)):
            continue  # 꾸밈음 묶음은 기둥이 늘 위(관례) — 검사 안 함
        report["svg_groups"] += 1

        # 2. 기둥 방향(다수결) — 성부가 하나인 보표만
        all_heads = [y for heads, _ in items for y in heads]
        above = sum(1 for y in all_heads if y < mid - _EPS)
        below = sum(1 for y in all_heads if y > mid + _EPS)
        up = items[0][1]
        n_layers = (beam_layers or {}).get(beam.get("id"), 1)
        if n_layers <= 1 and above != below:
            want_up = below > above
            if up != want_up:
                report["stem_dir"].append(
                    f"{where}: 머리 위 {above}·아래 {below}개인데 기둥이 {'위' if up else '아래'}")
        span_steps = (max(all_heads) - min(all_heads)) / (space / 2)
        if span_steps >= _WIDE_STEPS:
            report["wide"].append(f"{where}: 묶음 안 음 높이 차 {span_steps:.0f}단계")

        # 3. 기울기 — 빔에 가까운 쪽 머리(기둥 위면 가장 높은 머리)로 선율을 본다
        def near(heads):
            return min(heads) if up else max(heads)
        first, last = near(items[0][0]), near(items[-1][0])
        polys = [pl for pl in beam if _local(pl.tag) == "polygon"]
        if not polys:
            continue
        best = None
        for pl in polys:
            pts = [tuple(map(float, xy.split(","))) for xy in (pl.get("points") or "").split()]
            if len(pts) < 2:
                continue
            xs = [x for x, _ in pts]
            if best is None or max(xs) - min(xs) > best[0]:
                left = min(pts, key=lambda p: (p[0], p[1]))
                right = max(pts, key=lambda p: (p[0], -p[1]))
                best = (max(xs) - min(xs), right[1] - left[1])
        if best is None:
            continue
        beam_dy = best[1]          # SVG y는 아래로 증가 — 음수면 오른쪽이 높음
        mel_dy = last - first
        flat = abs(beam_dy) <= space * _FLAT_TOL_RATIO
        same = abs(mel_dy) <= space * _SAME_PITCH_TOL
        if same:
            if not flat:
                report["slope_wrong"].append(f"{where}: 첫·끝 음이 같은 높이인데 빔이 기울어짐")
        elif flat:
            inner = [near(h) for h, _ in items[1:-1]]
            # 가운데 음이 양 끝보다 빔 쪽으로 더 튀어나왔으면 수평이 관례
            sticks_out = inner and (min(inner) < min(first, last) if up else max(inner) > max(first, last))
            if not sticks_out:
                report["slope_flat"].append(f"{where}: {'상행' if mel_dy < 0 else '하행'}인데 빔이 수평")
        elif (beam_dy < 0) != (mel_dy < 0):
            report["slope_wrong"].append(f"{where}: {'상행' if mel_dy < 0 else '하행'}인데 빔이 반대로 기울어짐")


# ---------------------------------------------------------------- 묶어서 실행

def check_file(path):
    r = ScoreRenderer(path)
    mei_root = ET.fromstring(r.toolkit.getMEI())
    report = defaultdict(list)
    report["beam_groups"] = 0
    report["svg_groups"] = 0
    check_grouping(mei_root, report)
    xml_id = "{http://www.w3.org/XML/1998/namespace}id"
    measure_names = {m.get(xml_id): m.get("n") for m in mei_root.iter() if _local(m.tag) == "measure"}
    staff_names = []
    for sd in mei_root.iter():
        if _local(sd.tag) == "staffDef":
            label = next((c.text.strip() for c in sd if _local(c.tag) == "label" and c.text), f"보표 {sd.get('n')}")
            if label not in staff_names:
                staff_names.append(label)
    grace_ids = frozenset(e.get(xml_id) for e in mei_root.iter()
                          if _local(e.tag) in ("note", "chord") and e.get("grace") is not None)
    # 빔마다 "그 마디·보표에 음표가 있는 성부(layer) 수" — 2 이상이면 기둥 방향은 성부 관례를 따름
    beam_layers = {}
    for st in mei_root.iter():
        if _local(st.tag) != "staff":
            continue
        layers = [ly for ly in st if _local(ly.tag) == "layer"
                  and any(_local(x.tag) in ("note", "chord") for x in ly.iter())]
        for ly in layers:
            for b in ly.iter():
                if _local(b.tag) == "beam":
                    beam_layers[b.get(xml_id)] = len(layers)
    for page in range(1, r.page_count + 1):
        check_svg(r.page_svg(page), measure_names, staff_names, report, grace_ids, beam_layers)
    return report


_LABELS = [
    ("cross_beat", "1. 박 경계를 넘는 빔"),
    ("unbeamed", "1. 같은 박인데 안 묶인 짧은 음표(마디·보표 단위)"),
    ("stem_dir", "2. 다수결과 다른 기둥 방향"),
    ("wide", "2. 음 높이 차가 너무 큰 묶음(나눠 묶기 권장, 참고)"),
    ("slope_wrong", "3. 기울기 방향이 틀린 빔"),
    ("slope_flat", "3. 높이가 다른데 수평인 빔(참고)"),
]


def format_report(path, report):
    lines = [f"■ {path}", f"  빔 묶음 {report['beam_groups']}개(악보 데이터), 그려진 묶음 {report['svg_groups']}개"]
    for key, label in _LABELS:
        items = report[key]
        lines.append(f"  {label}: {len(items)}")
        for x in items[:_MAX_EXAMPLES]:
            lines.append(f"      - {x}")
        if len(items) > _MAX_EXAMPLES:
            lines.append(f"      … 외 {len(items) - _MAX_EXAMPLES}개")
    return "\n".join(lines)


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print(format_report(p, check_file(p)))
        print()
