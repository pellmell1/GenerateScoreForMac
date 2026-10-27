# -*- coding: utf-8 -*-
"""기본 회귀 테스트 — 외부 악보 파일 없이, 작은 MusicXML을 직접 만들어 확인한다.

실행: python -m unittest discover -s tests   (E:\\project\\guitar\\GenerateScore에서)
"""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import events  # noqa: E402
import harmonize  # noqa: E402
import round_song  # noqa: E402
import musicxml_reader  # noqa: E402
import render  # noqa: E402
import synth  # noqa: E402


def _note(step, octave, dur, ntype="quarter", tie=None, extra=""):
    ties = ""
    if tie:
        ties = "".join(f'<tie type="{t}"/>' for t in tie.split(","))
    return (f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch>"
            f"<duration>{dur}</duration>{ties}<type>{ntype}</type>{extra}</note>")


def _score_xml(measures_p1, measures_p2=None, transpose=None, tempo=120, repeat=False):
    """measures_*: 마디별 음표 XML 문자열 목록. 4/4, divisions=1."""
    tr = (f"<transpose><diatonic>0</diatonic><chromatic>0</chromatic>"
          f"<octave-change>{transpose}</octave-change></transpose>") if transpose else ""

    def part(pid, ms, first_extra):
        out = [f'<part id="{pid}">']
        for i, body in enumerate(ms):
            head = ""
            if i == 0:
                head = (f"<attributes><divisions>1</divisions><time><beats>4</beats>"
                        f"<beat-type>4</beat-type></time>{tr}</attributes>{first_extra}")
            bar = ""
            if repeat and i == len(ms) - 1 and pid == "P1":
                bar = '<barline location="right"><repeat direction="backward"/></barline>'
            out.append(f'<measure number="{i + 1}">{head}{body}{bar}</measure>')
        out.append("</part>")
        return "".join(out)

    tempo_dir = (f'<direction><direction-type><dynamics><f/></dynamics></direction-type>'
                 f'<sound tempo="{tempo}"/></direction>')
    parts_list = ('<score-part id="P1"><part-name>Guitar I</part-name>'
                  '<midi-instrument id="I1"><midi-program>25</midi-program></midi-instrument></score-part>')
    body = part("P1", measures_p1, tempo_dir)
    if measures_p2 is not None:
        parts_list += ('<score-part id="P2"><part-name>Electric Bass</part-name>'
                       '<midi-instrument id="I2"><midi-program>34</midi-program></midi-instrument></score-part>')
        body += part("P2", measures_p2, "")
    return (f'<?xml version="1.0" encoding="UTF-8"?><score-partwise version="3.1">'
            f"<part-list>{parts_list}</part-list>{body}</score-partwise>")


class _TmpScore:
    def __init__(self, xml):
        self.xml = xml

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(suffix=".xml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.xml)
        return self.path

    def __exit__(self, *a):
        os.remove(self.path)


class ReaderTest(unittest.TestCase):
    def test_basic_notes_tempo_dynamics_instruments(self):
        xml = _score_xml([_note("C", 4, 1) + _note("E", 4, 1) + _note("G", 4, 2, "half")],
                         [_note("C", 2, 4, "whole")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        self.assertEqual(len(s["notes"]), 4)
        self.assertEqual(s["tempos"][0], (0.0, 120.0))
        self.assertEqual([n[2] for n in s["notes"] if n[3] == 0], [60, 64, 67])
        self.assertIn((0.0, 0, musicxml_reader.DYNAMIC_VELOCITY["f"]), s["dynamics"])
        self.assertEqual([i["program"] for i in s["instruments"]], [25, 34])

    def test_transpose_octave(self):
        """<transpose octave-change=-1>이면 소리 높이는 적힌 것보다 한 옥타브 아래."""
        xml = _score_xml([_note("A", 4, 4, "whole")], transpose=-1)
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        self.assertEqual(s["notes"][0][2], 57)

    def test_articulation(self):
        stac = "<notations><articulations><staccato/></articulations></notations>"
        xml = _score_xml([_note("C", 4, 1, extra=stac) + _note("D", 4, 3, "half")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        self.assertEqual(s["notes"][0][5], frozenset({"staccato"}))
        self.assertEqual(s["notes"][1][5], frozenset())

    def test_grace_notes(self):
        """빗금 꾸밈음 둘 → 본음 박 위에서 음당 ACCIACCATURA_SEC초, 본음은 그만큼 늦게·짧게."""
        grace = ('<note><grace slash="yes"/><pitch><step>{}</step><octave>4</octave></pitch>'
                 '<type>16th</type></note>')
        xml = _score_xml([_note("C", 4, 1) + grace.format("E") + grace.format("D")
                          + _note("C", 4, 3, "half")])  # 120bpm → 1초 = 2박
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        g = musicxml_reader.ACCIACCATURA_SEC * 2
        got = [(round(n[0], 6), round(n[1], 6), n[2], "grace" in n[5]) for n in sorted(s["notes"])]
        self.assertEqual(got, [(0.0, 1.0, 60, False), (1.0, round(g, 6), 64, True),
                               (round(1 + g, 6), round(g, 6), 62, True),
                               (round(1 + 2 * g, 6), round(3 - 2 * g, 6), 60, False)])


    def test_trill(self):
        """조표 ♭1에서 A4 트릴 → 위 이웃음 B♭4, 본음으로 시작·끝(홀수 개), 번갈음 음은 "trill"."""
        orn = "<notations><ornaments><trill-mark/></ornaments></notations>"
        xml = _score_xml([_note("A", 4, 4, "whole", extra=orn)]).replace(
            "<time>", "<key><fifths>-1</fifths></key><time>", 1)
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        ns = sorted(s["notes"])
        self.assertEqual(len(ns) % 2, 1)
        self.assertEqual({n[2] for n in ns}, {69, 70})
        self.assertEqual((ns[0][2], ns[-1][2]), (69, 69))
        self.assertTrue(all("trill" in n[5] for n in ns[1:]) and "trill" not in ns[0][5])
        self.assertAlmostEqual(sum(n[1] for n in ns), 4.0)

    def test_inverted_mordent(self):
        """B4 잔꾸밈(조표 ♭1) → B4·C5 짧게 + B4 나머지. accidental-mark가 있으면 그 임시표로."""
        orn = "<notations><ornaments><inverted-mordent/>{}</ornaments></notations>"
        for mark, upper in (("", 72), ("<accidental-mark>sharp</accidental-mark>", 73)):
            xml = _score_xml([_note("B", 4, 4, "whole", extra=orn.format(mark))]).replace(
                "<time>", "<key><fifths>-1</fifths></key><time>", 1)
            with _TmpScore(xml) as p:
                s = musicxml_reader.read_musicxml(p)
            ns = sorted(s["notes"])
            self.assertEqual([n[2] for n in ns], [71, upper, 71])  # 본음은 적힌 pitch(alter 없음) 그대로 B4
            # 120bpm → 1초 = 2박. 16분음표(0.25박)가 초 상한(0.12초 = 0.24박)보다 길어 상한이 적용됨
            short = musicxml_reader.ORNAMENT_MAX_SEC * 2
            self.assertAlmostEqual(ns[1][0], short)
            self.assertAlmostEqual(ns[2][1], 4 - 2 * short)
            self.assertEqual([("ornament" in n[5]) for n in ns], [False, True, True])

    def test_mordent_with_line_is_32nd(self):
        """세로줄 있는 잔결꾸밈(mordent)은 아래 이웃음 + 32분음표(0.125박)씩."""
        orn = "<notations><ornaments><mordent/></ornaments></notations>"
        xml = _score_xml([_note("B", 4, 4, "whole", extra=orn)])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        ns = sorted(s["notes"])
        self.assertEqual([n[2] for n in ns], [71, 69, 71])
        self.assertAlmostEqual(ns[1][0], 0.125)

    def test_turn(self):
        """돈꾸밈음: turn = 위·본·아래·본, inverted-turn = 아래·본·위·본. 합계 길이는 본음 그대로."""
        for tag, pitches in (("turn", [74, 72, 71, 72]), ("inverted-turn", [71, 72, 74, 72])):
            orn = f"<notations><ornaments><{tag}/></ornaments></notations>"
            xml = _score_xml([_note("C", 5, 4, "whole", extra=orn)])
            with _TmpScore(xml) as p:
                s = musicxml_reader.read_musicxml(p)
            ns = sorted(s["notes"])
            self.assertEqual([n[2] for n in ns], pitches)
            self.assertAlmostEqual(sum(n[1] for n in ns), 4.0)
            self.assertAlmostEqual(ns[1][0], min(0.25, 0.24))

    def test_appoggiatura_takes_half(self):
        """빗금 없는 단독 앞꾸밈음(긴앞꾸밈음)은 본음의 절반을 차지."""
        grace = ('<note><grace/><pitch><step>D</step><octave>4</octave></pitch>'
                 '<type>eighth</type></note>')
        xml = _score_xml([grace + _note("C", 4, 4, "whole")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        ns = sorted(s["notes"])
        self.assertAlmostEqual(ns[0][1], 2.0)
        self.assertAlmostEqual(ns[1][0], 2.0)

    def test_measure_marks_every_20(self):
        """악보 조판 전에 마디 번호가 20의 배수인 마디(첫 파트)에 리허설 마크를 단다."""
        import xml.etree.ElementTree as ET
        import score_view
        xml = _score_xml([_note("C", 4, 4, "whole")] * 45)
        root = ET.fromstring(xml)
        for i, m in enumerate(root.find("part").findall("measure")):
            m.set("number", str(i + 1))
        score_view._add_measure_marks(root.find("part"))
        marks = [m.get("number") for m in root.find("part").findall("measure")
                 if m.find("direction/direction-type/rehearsal") is not None]
        self.assertEqual(marks, ["20", "40"])

    def test_compound_click_unit(self):
        """6/8 메트로놈: 점4분음표 박은 마디당 2번(1.5박 간격), 8분음표 박은 6번(0.5박 간격). 4/4는 4분음표 4번."""
        import numpy as np
        import render
        played = {"measures": [{"start_q": 0.0, "length_q": 3.0, "beats": 6, "beat_type": 8},
                               {"start_q": 3.0, "length_q": 4.0, "beats": 4, "beat_type": 4}],
                  "tempos": [(0.0, 60.0)]}   # 60bpm → 1박 = 1초

        def starts(compound_q):
            out = render.click_track(played, 1.0, 8 * render.SR, compound_q=compound_q)
            nz = out != 0
            idx = np.flatnonzero(nz & ~np.concatenate([[False], nz[:-1]]))
            return [round(i / render.SR, 2) for i in idx]
        self.assertEqual(starts(render.COMPOUND_DOTTED_QUARTER), [0.0, 1.5, 3.0, 4.0, 5.0, 6.0])
        self.assertEqual(starts(render.COMPOUND_EIGHTH), [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0])

    @staticmethod
    def _words(txt):
        return f"<direction><direction-type><words>{txt}</words></direction-type></direction>"

    def test_tempo_word_rit_fermata(self):
        """빠르기말(숫자 없음) → 표의 bpm, rit. → 다음 표시까지 점점 느리게, 페르마타 → 그 음 동안 늦게."""
        ferm = "<notations><fermata/></notations>"
        xml = _score_xml([_note("C", 4, 4, "whole"), self._words("rit.") + _note("C", 4, 4, "whole"),
                          self._words("Andante") + _note("C", 4, 2, "half", extra=ferm) + _note("C", 4, 2, "half")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        bpm = dict(s["tempos"])
        self.assertAlmostEqual(bpm[0.0], 120.0)
        self.assertAlmostEqual(bpm[4.0], 120 * (1 + (0.75 - 1) * 0.125 / 4), places=3)  # rit. 첫 조각
        slow = [b for q, b in s["tempos"] if 4 <= q < 8]
        self.assertTrue(slow == sorted(slow, reverse=True) and slow[-1] < 95)              # 점점 느려짐
        self.assertAlmostEqual(bpm[8.0], 80 / musicxml_reader.FERMATA_STRETCH)            # Andante + 페르마타
        self.assertAlmostEqual(bpm[10.0], 80.0)

    def test_cresc_word_and_wedge(self):
        """cresc. 글자 → 다음 f까지 점점 세게, 꺾쇠(diminuendo) → 다음 p까지 점점 여리게."""
        def dyn(mark):
            return f"<direction><direction-type><dynamics><{mark}/></dynamics></direction-type></direction>"

        def wedge(kind):
            return f'<direction><direction-type><wedge type="{kind}"/></direction-type></direction>'
        xml = _score_xml([dyn("p") + self._words("cresc.") + _note("C", 4, 4, "whole"),
                          dyn("f") + wedge("diminuendo") + _note("C", 4, 4, "whole") + wedge("stop"),
                          dyn("p") + _note("C", 4, 4, "whole")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        v = [(q, vel) for q, pi, vel in s["dynamics"] if pi == 0]
        p_, f_ = musicxml_reader.DYNAMIC_VELOCITY["p"], musicxml_reader.DYNAMIC_VELOCITY["f"]
        up = [vel for q, vel in v if 0 < q < 4]
        down = [vel for q, vel in v if 4 < q < 8]
        self.assertTrue(up == sorted(up) and p_ < up[0] and up[-1] < f_)
        self.assertTrue(down == sorted(down, reverse=True) and p_ < down[-1] < f_)

    def test_pizz_arco(self):
        """pizz.~arco 사이 음표만 "pizz", 같은 자리에 둘이 겹치면 나중 것은 마디 후반부터."""
        xml = _score_xml([self._words("pizz.") + _note("C", 4, 2, "half") + self._words("arco")
                          + _note("D", 4, 2, "half"),
                          self._words("arco") + self._words("pizz.") + _note("E", 4, 2, "half")
                          + _note("F", 4, 2, "half")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        self.assertEqual([("pizz" in n[5]) for n in sorted(s["notes"])], [True, False, False, True])


class TechniqueTest(unittest.TestCase):
    """techniques.py — 글리산도 계단(반음 수·빨라졌다 느려짐), 라스게아도 손가락 순서."""

    def test_glissando_steps(self):
        import techniques
        _w, times = techniques.glissando(55, 67, 90, 0.5, 0.9, 0.5)
        self.assertEqual(len(times), 12)                       # 반음 12개
        gaps = np.diff(times)
        self.assertTrue(gaps[0] > gaps[len(gaps) // 2] < gaps[-1])  # 처음·끝 느리고 가운데 빠름
        self.assertAlmostEqual(gaps[0], gaps[-1], places=3)    # 대칭

    def test_rasgueado_order(self):
        import techniques
        _w, plucks = techniques.rasgueado((40, 47, 52, 55, 59, 64), 100, 1.0)
        self.assertEqual(len(plucks), 24)                      # 손가락 4 × 줄 6
        firsts = [min(t for t, m, v in plucks[k * 6:(k + 1) * 6]) for k in range(4)]
        self.assertEqual(firsts, sorted(firsts))


class TechniqueReadTest(unittest.TestCase):
    """악보 표시 → 주법: 하모닉스(한 옥타브 위), 글리산도(끝 음은 합침), rasg.(그 마디 3음 이상 화음)."""

    def test_read_techniques(self):
        harm = "<notations><technical><harmonic><natural/></harmonic></technical></notations>"
        g_start = '<notations><glissando type="start"/></notations>'
        g_stop = '<notations><glissando type="stop"/></notations>'
        rasg = "<direction><direction-type><words>rasg.</words></direction-type></direction>"
        chord = "".join(_note(s, o, 4, "whole", extra="").replace("<note>", "<note><chord/>" if k else "<note>", 1)
                        for k, (s, o) in enumerate((("E", 2), ("B", 2), ("E", 3), ("G", 3))))
        xml = _score_xml([_note("E", 3, 4, "whole", extra=harm),
                          _note("A", 3, 2, "half", extra=g_start) + _note("E", 4, 2, "half", extra=g_stop),
                          rasg + chord, chord])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        evs, _ = events.build_events(s)
        self.assertEqual((evs[0].midi, evs[0].harmonic), (64, True))           # E3 하모닉스 → E4 소리
        gl = [e for e in evs if e.glide_to is not None]
        self.assertEqual(len(gl), 1)
        self.assertEqual((gl[0].midi, gl[0].glide_to), (57, 64))
        self.assertAlmostEqual(gl[0].dur_sec, 2.0)                             # 끝 음까지 합친 길이(시작 1.0 + 끝 1.0)
        self.assertAlmostEqual(gl[0].glide_at + gl[0].glide_sec, 1.0)          # 끝 음 시작 자리에 도착
        self.assertFalse(any(e.midi == 64 and abs(e.start_sec - 3.0) < 1e-6 and e.glide_to is None
                             for e in evs))                                    # 끝 음은 따로 뜯지 않음
        self.assertEqual([e.rasg for e in evs if e.start_sec >= 4.0], [True] * 4 + [False] * 4)


    def test_glissando_chain_is_one_sound(self):
        """A→B♭→A처럼 글리산도가 이어진 악보: 가운데·끝 음을 따로 뜯지 않고 한 소리가 두 번 미끄러진다."""
        def gl(kind, extra=""):
            return f'<notations><glissando type="{kind}" number="1"/>{extra}</notations>'
        n1 = _note("A", 4, 2, "half", extra=gl("start"))
        n2 = _note("B", 4, 2, "half", extra=('<notations><glissando type="stop" number="1"/>'
                                            '<glissando type="start" number="1"/></notations>')).replace(
            "<pitch><step>B</step>", "<pitch><step>B</step><alter>-1</alter>", 1)
        n3 = _note("A", 4, 2, "half", extra=gl("stop"))
        xml = _score_xml([n1 + n2, n3 + _note("C", 5, 2, "half")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        evs, _ = events.build_events(s)
        g = [e for e in evs if e.glide_to is not None]
        self.assertEqual(len(g), 1)
        self.assertEqual([x[2] for x in g[0].glide_path], [70, 69])            # A4→B♭4→A4
        self.assertAlmostEqual(g[0].dur_sec, 3.0)                              # 세 음 길이를 합침
        self.assertEqual(sorted(e.midi for e in evs), [69, 72])                # 따로 뜯는 소리는 A4(글리산도)와 C5뿐

class CelloRunTest(unittest.TestCase):
    """4번 줄 단음이 3개 이상 이어지면 "cello", 2개뿐이거나 화음이면 아님."""

    def test_cello_runs(self):
        s4 = "<notations><technical><string>4</string></technical></notations>"
        run3 = "".join(_note(st, 3, 1, "quarter", extra=s4) for st in "DEF") + _note("G", 3, 1, "quarter")
        run2 = "".join(_note(st, 3, 1, "quarter", extra=s4) for st in "DE") + _note("G", 3, 2, "half")
        chord = (_note("D", 3, 4, "whole", extra=s4)
                 + _note("A", 3, 4, "whole").replace("<note>", "<note><chord/>", 1))
        xml = _score_xml([run3, run2, chord, chord])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        evs, _ = events.build_events(s)
        self.assertEqual([e.cello for e in evs][:7], [True, True, True, False, False, False, False])
        self.assertFalse(any(e.cello for e in evs[7:]))


class HarmonicFretTest(unittest.TestCase):
    def test_fret_sets_harmonic_interval(self):
        def h(fret):
            return (f"<notations><technical><harmonic><natural/></harmonic><string>4</string>"
                    f"<fret>{fret}</fret></technical></notations>")
        # 적힌 음 = 누른 음(D선 12프렛 D4=62, 7프렛 A3=57, 5프렛 G3=55) → 개방현 D3(50) 기준 +12 / +19 / +24
        body = (_note("D", 4, 1, "quarter", extra=h(12)) + _note("A", 3, 1, "quarter", extra=h(7))
                + _note("G", 3, 1, "quarter", extra=h(5)) + _note("A", 3, 1, "quarter", extra=
                "<notations><technical><harmonic><natural/></harmonic></technical></notations>"))
        with _TmpScore(_score_xml([body])) as p:
            s = musicxml_reader.read_musicxml(p)
        self.assertEqual([n[2] for n in sorted(s["notes"])], [62, 69, 74, 69])


class TremoloAndLabelTest(unittest.TestCase):
    def test_single_tremolo_repeats(self):
        trem = "<notations><ornaments><tremolo type=\"single\">2</tremolo></ornaments></notations>"
        with _TmpScore(_score_xml([_note("C", 4, 4, "whole", extra=trem)])) as p:
            s = musicxml_reader.read_musicxml(p)
        self.assertEqual(len(s["notes"]), 16)   # 2줄 = 16분음표 간격, 온음표 4박

    def test_measure_label(self):
        self.assertEqual(musicxml_reader._measure_label("X1"), "1")
        self.assertEqual(musicxml_reader._measure_label("12"), "12")


class LowStringTest(unittest.TestCase):
    """5·6번 줄 표시 음, 줄 표시 없는 D3 아래 음은 "low"(묵직한 저음), 1~4번 줄 3연속은 cello."""

    def test_low_and_cello(self):
        s6 = "<notations><technical><string>6</string></technical></notations>"
        s2 = "<notations><technical><string>2</string></technical></notations>"
        body = (_note("E", 2, 1, "quarter", extra=s6) + _note("C", 3, 1, "quarter") + _note("A", 3, 1, "quarter")
                + _note("E", 4, 1, "quarter", extra=s2) + _note("F", 4, 1, "quarter", extra=s2)
                + _note("G", 4, 1, "quarter", extra=s2) + _note("A", 4, 1, "quarter"))
        with _TmpScore(_score_xml([body, body])) as p:
            s = musicxml_reader.read_musicxml(p)
        evs, _ = events.build_events(s, expand=False)
        self.assertEqual([e.low for e in evs][:7], [True, True, False, False, False, False, False])
        self.assertEqual([e.cello for e in evs][:7], [False, False, False, True, True, True, False])
        self.assertEqual(synth.low_timbre("guitar"), "guitar_low")
        self.assertEqual(synth.low_timbre("bass"), "bass")

    def test_contrabass_guitar_is_guitar_timbre(self):
        self.assertEqual(synth.timbre_for_part("CB. Gtr.", {"name": "Contrabass Guitar", "program": 44}), "guitar")


class EventsTest(unittest.TestCase):
    def test_tie_merge_and_seconds(self):
        # 온음표 C4가 다음 마디 2박까지 붙임줄로 이어짐 → 한 음, 6박 = 3초(120bpm)
        xml = _score_xml([_note("C", 4, 4, "whole", tie="start"),
                          _note("C", 4, 2, "half", tie="stop") + _note("E", 4, 2, "half")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        evs, played = events.build_events(s)
        self.assertEqual(len(evs), 2)
        self.assertAlmostEqual(evs[0].dur_sec, 3.0)
        self.assertAlmostEqual(evs[1].start_sec, 3.0)
        self.assertAlmostEqual(events.total_seconds(played), 4.0)

    def test_tempo_scale(self):
        xml = _score_xml([_note("C", 4, 4, "whole")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        evs, played = events.build_events(s, tempo_scale=2.0)
        self.assertAlmostEqual(evs[0].dur_sec, 1.0)
        self.assertAlmostEqual(events.total_seconds(played, 2.0), 1.0)

    def test_repeat_expands(self):
        xml = _score_xml([_note("C", 4, 4, "whole"), _note("D", 4, 4, "whole")], repeat=True)
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        evs, played = events.build_events(s)
        self.assertEqual([e.midi for e in evs], [60, 62, 60, 62])
        self.assertEqual([m["occurrence"] for m in played["measures"]], [1, 1, 2, 2])

    def test_staccato_gate(self):
        stac = "<notations><articulations><staccato/></articulations></notations>"
        xml = _score_xml([_note("C", 4, 4, "whole", extra=stac)])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        evs, _ = events.build_events(s)
        self.assertAlmostEqual(evs[0].sound_sec, evs[0].dur_sec * 0.5)


class SynthTest(unittest.TestCase):
    def _pitch(self, y, target):
        n = len(y)
        spec = np.abs(np.fft.rfft(y * np.hanning(n), 8 * n))
        fr = np.fft.rfftfreq(8 * n, 1 / synth.SR)
        w = (fr > target * 0.9) & (fr < target * 1.1)
        return fr[w][np.argmax(spec[w])]

    def test_ks_pitch_accuracy(self):
        s = synth.NoteSynth()
        for midi in (40, 57, 76, 93):
            y = s.render(midi, 80, 1.0, "guitar")[:synth.SR].astype(float)
            target = float(synth.midi_to_hz(midi))
            cents = 1200 * np.log2(self._pitch(y, target) / target)
            self.assertLess(abs(cents), 5.0, f"midi {midi}: {cents:.1f} cent")

    def test_length_and_no_nan(self):
        s = synth.NoteSynth()
        for timbre in synth.TIMBRES:
            y = s.render(60, 100, 0.5, timbre)
            self.assertFalse(np.isnan(y).any())
            expected = int(round(0.5 * synth.SR)) + int(round(synth.TIMBRES[timbre]["release"] * synth.SR))
            self.assertEqual(len(y), expected)
            self.assertLess(abs(y[-1]), 1e-3)  # 끝은 0으로 줄어들어야(딸깍 방지)

    def test_timbre_for_part(self):
        self.assertEqual(synth.timbre_for_part("Guitar I", {"program": 25}), "guitar")
        self.assertEqual(synth.timbre_for_part("Electric Bass", {"program": 34}), "bass")
        self.assertEqual(synth.timbre_for_part("Violin", {"program": 41}), "sustain")
        self.assertEqual(synth.timbre_for_part("Piano", {"program": None}), "piano")


class NotationTest(unittest.TestCase):
    """score_view의 빔 자동 묶기·잇단음표 표시(빔·tuplet 정보 없는 MusicXML용)."""

    def _prepared(self, notes_xml, beats=2, beat_type=4, clef_oct=None):
        import xml.etree.ElementTree as ET
        import score_view
        clef = ("<clef><sign>G</sign><line>2</line>"
                + (f"<clef-octave-change>{clef_oct}</clef-octave-change>" if clef_oct else "") + "</clef>")
        xml = ('<?xml version="1.0"?><score-partwise><part-list><score-part id="P1"><part-name>G</part-name>'
               '</score-part></part-list><part id="P1"><measure number="1"><attributes><divisions>12</divisions>'
               f"<time><beats>{beats}</beats><beat-type>{beat_type}</beat-type></time>{clef}</attributes>"
               f"{notes_xml}</measure></part></score-partwise>")
        root, parts = score_view._prepare_tree(xml)
        return root.findall(".//note")

    @staticmethod
    def _n(step, octave, dur, ntype, tm=None):
        t = (f"<time-modification><actual-notes>{tm[0]}</actual-notes><normal-notes>{tm[1]}</normal-notes>"
             "</time-modification>") if tm else ""
        return (f"<note><pitch><step>{step}</step><octave>{octave}</octave></pitch><duration>{dur}</duration>"
                f"<voice>1</voice><type>{ntype}</type>{t}</note>")

    def test_beam_by_beat_2_4(self):
        notes = self._prepared("".join(self._n("A", 4, 6, "eighth") for _ in range(4)))
        self.assertEqual([n.findtext("beam") for n in notes], ["begin", "end", "begin", "end"])

    def test_beam_6_8_dotted_quarter(self):
        notes = self._prepared("".join(self._n("A", 4, 6, "eighth") for _ in range(6)), 6, 8)
        self.assertEqual([n.findtext("beam") for n in notes],
                         ["begin", "continue", "end", "begin", "continue", "end"])

    def test_stem_majority(self):
        # 높은 음(가운데 줄 B4보다 위) 둘 → 기둥 아래 / 낮은 음 둘 → 위
        hi = self._prepared(self._n("E", 5, 6, "eighth") + self._n("D", 5, 6, "eighth")
                            + self._n("C", 4, 12, "quarter"))
        self.assertEqual([n.findtext("stem") for n in hi[:2]], ["down", "down"])
        lo = self._prepared(self._n("E", 4, 6, "eighth") + self._n("D", 4, 6, "eighth")
                            + self._n("C", 4, 12, "quarter"))
        self.assertEqual([n.findtext("stem") for n in lo[:2]], ["up", "up"])

    def test_stem_octave_clef(self):
        # 8vb 자리표: pitch E4는 한 옥타브 위(E5 자리)에 그려지므로 가운데 줄보다 위 → 기둥 아래
        notes = self._prepared(self._n("E", 4, 6, "eighth") + self._n("D", 4, 6, "eighth")
                               + self._n("C", 4, 12, "quarter"), clef_oct=-1)
        self.assertEqual([n.findtext("stem") for n in notes[:2]], ["down", "down"])

    def test_triplet_markers(self):
        # 셋잇단 16분음표 6개(길이 2씩, 16분음표=3) → 3개씩 두 잇단음표, 한 박에 한 빔
        notes = self._prepared("".join(self._n("A", 4, 2, "16th", (3, 2)) for _ in range(6))
                               + self._n("A", 4, 12, "quarter"))
        kinds = [[t.get("type") for t in n.iter("tuplet")] for n in notes[:6]]
        self.assertEqual(kinds, [["start"], [], ["stop"], ["start"], [], ["stop"]])
        self.assertEqual([n.findtext("beam") for n in notes[:6]],
                         ["begin", "continue", "continue", "continue", "continue", "end"])


    def test_source_beams_rebeamed_by_rule(self):
        # 파일에 3/4 한 마디 통째 빔(music21식)이 있어도 규칙대로 한 박씩 다시 묶는다
        notes_xml = "".join(
            self._n("A", 4, 6, "eighth").replace("<type>", f"<beam number=\"1\">{b}</beam><type>", 1)
            for b in ["begin", "continue", "continue", "continue", "continue", "end"])
        notes = self._prepared(notes_xml, 3, 4)
        self.assertEqual([n.findtext("beam") for n in notes], ["begin", "end"] * 3)

    def test_tuplet_written_ratio_kept(self):
        # 6:4로 적힌 16분음표(실제 길이도 6:4) → "6" 하나로(3:2 둘로 쪼개지 않음)
        notes = self._prepared("".join(self._n("A", 4, 2, "16th", (6, 4)) for _ in range(6))
                               + self._n("A", 4, 12, "quarter"))
        kinds = [[t.get("type") for t in n.iter("tuplet")] for n in notes[:6]]
        self.assertEqual(kinds, [["start"], [], [], [], [], ["stop"]])

    def test_grace_group_slur_and_beam(self):
        # 할보르센 69마디 모양: 꾸밈음 3개(시작 6번이 두 번) + 본음(짝 없는 끝 1번, 끝 6번), 두 묶음
        def g(step, slur=""):
            return (f'<note><grace slash="yes"/><pitch><step>{step}</step><octave>5</octave></pitch>'
                    f'<voice>1</voice><type>16th</type>{slur}</note>')
        start = '<notations><slur type="start" number="6"/></notations>'
        main = ('<note><pitch><step>F</step><octave>5</octave></pitch><duration>6</duration><voice>1</voice>'
                '<type>eighth</type><notations><slur type="stop" number="1"/><slur type="stop" number="6"/>'
                '</notations></note>')
        grp = g("G", start) + g("F") + g("E", start) + main
        notes = self._prepared(grp + grp + self._n("A", 4, 12, "quarter"))
        slurs = [[(s.get("type"), s.get("number")) for s in n.iter("slur")] for n in notes[:8]]
        self.assertEqual(slurs, [[("start", "6")], [], [], [("stop", "6")]] * 2)
        self.assertEqual([n.findtext("beam[@number='1']") for n in notes[:3]], ["begin", "continue", "end"])
        self.assertEqual([n.find("grace").get("slash") for n in notes[:3]], [None] * 3)
        # 본음: 꾸밈음 묶음 사이라 빔으로 묶지 않고, 이음줄이 짧게 붙도록 기둥 위
        self.assertEqual([(n.findtext("beam"), n.findtext("stem")) for n in (notes[3], notes[7])],
                         [(None, "up"), (None, "up")])


    def test_unslurred_graces_get_slur(self):
        # 이음줄이 하나도 안 적힌 꾸밈음 묶음과 홑 꾸밈음도 본음까지 이음줄 하나로 묶인다
        def g(step):
            return (f'<note><grace/><pitch><step>{step}</step><octave>5</octave></pitch>'
                    f'<voice>1</voice><type>16th</type></note>')
        main = ('<note><pitch><step>F</step><octave>5</octave></pitch><duration>6</duration><voice>1</voice>'
                '<type>eighth</type></note>')
        notes = self._prepared(g("G") + g("F") + main + g("E") + main + self._n("A", 4, 6, "eighth"))
        slurs = [[(s.get("type"), s.get("number")) for s in n.iter("slur")] for n in notes[:5]]
        self.assertEqual(slurs, [[("start", "8")], [], [("stop", "8")], [("start", "8")], [("stop", "8")]])


class ImportAndSaveTest(unittest.TestCase):
    def test_extension_routing(self):
        import score_import
        for name in ("a.mscz", "b.GP5", "c.gpx", "d.gp"):
            self.assertTrue(score_import.needs_conversion(name), name)
        for name in ("a.xml", "b.mxl", "c.musicxml"):
            self.assertFalse(score_import.needs_conversion(name), name)
            self.assertEqual(score_import.resolve(name), name)

    def test_save_mp3(self):
        import soundfile as sf
        import tempfile
        xml = _score_xml([_note("C", 4, 4, "whole")])
        with _TmpScore(xml) as p:
            r = render.RenderedScore(musicxml_reader.read_musicxml(p))
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "x.mp3")
            render.write_wav(out, r)
            info = sf.info(out)
            self.assertEqual(info.format, "MP3")
            self.assertGreater(info.duration, 1.0)


class RenderTest(unittest.TestCase):
    def _rendered(self):
        xml = _score_xml([_note("C", 4, 4, "whole")], [_note("C", 2, 4, "whole")])
        with _TmpScore(xml) as p:
            s = musicxml_reader.read_musicxml(p)
        return render.RenderedScore(s)

    def test_mix_mute_solo(self):
        r = self._rendered()
        self.assertEqual(len(r.parts), 2)
        full = render.mix(r.parts, r.settings, r.gain)
        self.assertLessEqual(float(np.abs(full).max()), 1.0)
        r.settings[0].mute = True
        r.settings[1].mute = True
        self.assertEqual(float(np.abs(render.mix(r.parts, r.settings, r.gain)).max()), 0.0)
        r.settings[1].mute = False
        r.settings[0].mute = False
        r.settings[0].solo = True
        solo = render.mix(r.parts, r.settings, r.gain)
        only0 = render.mix([r.parts[0], np.zeros_like(r.parts[1])],
                           [r.settings[0], render.PartSetting()], r.gain)
        np.testing.assert_allclose(solo, only0, atol=1e-6)

    def test_iter_mix_loop(self):
        r = self._rendered()
        a, b = r.frame_of(0.5), r.frame_of(1.0)
        it = render.iter_mix(r, lambda: r.settings, lambda: 0.0, r.frame_of(0.8), b, loop=True, loop_from=a)
        # 첫 바퀴는 시작점(0.8초)부터 구간 끝까지, 그다음 바퀴부터는 구간 처음(0.5초)부터
        lengths = [len(next(it)) for _ in range(20)]
        first_lap = b - r.frame_of(0.8)
        lap = b - a
        cum = np.cumsum(lengths)
        self.assertIn(first_lap, cum)
        self.assertIn(first_lap + lap, cum)
        self.assertIn(first_lap + 2 * lap, cum)

    def test_write_wav(self):
        import soundfile as sf
        r = self._rendered()
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            render.write_wav(path, r)
            y, sr = sf.read(path)
            self.assertEqual(sr, synth.SR)
            self.assertEqual(y.shape, (len(r.parts[0]), 2))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()


class HarmonizeTests(unittest.TestCase):
    def _melody(self):
        # C 장조 4마디: 도레미파 / 솔라솔미 / 파레솔시 / 도 — 마지막은 으뜸음
        ms = [_note("C", 4, 1) + _note("D", 4, 1) + _note("E", 4, 1) + _note("F", 4, 1),
              _note("G", 4, 1) + _note("A", 4, 1) + _note("G", 4, 1) + _note("E", 4, 1),
              _note("F", 4, 1) + _note("D", 4, 1) + _note("G", 4, 1) + _note("B", 4, 1),
              _note("C", 5, 4, "whole")]
        return _score_xml(ms)

    def test_adds_parts_and_ends_on_tonic(self):
        import xml.etree.ElementTree as ET
        root, info = harmonize.harmonize_root(ET.fromstring(self._melody()), style="arpeggio")
        self.assertEqual(info["key"], "C 장조")
        self.assertEqual(info["chords"][-1][1], "C")
        self.assertIn(info["chords"][-2][2], ("V", "V7", "IV"))
        self.assertEqual(len(root.findall("part")), 3)       # 멜로디 + 기타 + 베이스
        self.assertEqual(len(info["chords"]), 8)             # 4/4는 마디당 2칸
        # 새 성부도 마디 길이를 정확히 채운다(4/4 = 16 divisions)
        for part in root.findall("part")[1:]:
            for m in part.findall("measure"):
                total = sum(int(n.findtext("duration")) for n in m.findall("note") if n.find("chord") is None)
                self.assertEqual(total, 16, m.get("number"))

    def test_reader_and_render_accept_result(self):
        with _TmpScore(self._melody()) as src:
            out = src + "_h.musicxml"
            try:
                path, info = harmonize.harmonize_file(src, out, style="arpeggio")
                score = musicxml_reader.read_musicxml(path)
                self.assertEqual(len(score["parts"]), 3)
                self.assertEqual(len(score["measures"]), 4)
                r = render.render_file(path, path + ".wav", 1.0, [], [], 0.0, None)
                self.assertGreater(r.length_sec, 1.0)
                self.assertEqual([s.timbre for s in r.settings][1:], ["guitar", "guitar"])    # 기본 악기 = 기타(저음 성부도 기타)
            finally:
                for p in (out, out + ".wav"):
                    if os.path.exists(p):
                        os.remove(p)

    def test_each_voice_gets_its_own_instrument(self):
        # 위(화음)·아래(저음) 성부마다 악기를 따로 — 한 음씩 내는 악기는 block이어도 동시음 없이, 음역·자리표·음색도 악기대로
        with _TmpScore(self._melody()) as src:
            out = src + "_h.musicxml"
            try:
                path, info = harmonize.harmonize_file(src, out, style="block", voices=["cello", "glockenspiel"])
                self.assertEqual(info["voices"], ["cello", "glockenspiel"])
                import xml.etree.ElementTree as ET
                root = ET.parse(path).getroot()
                for part, key in zip(root.findall("part")[1:], ("cello", "glockenspiel")):
                    ins = harmonize.INSTRUMENTS[key]
                    self.assertEqual(part.find(".//clef/sign").text, ins["high_clef"][0])
                    self.assertEqual(sum(1 for n in part.iter("note") if n.find("chord") is not None), 0)
                    for m in part.findall("measure"):
                        total = sum(int(n.findtext("duration")) for n in m.findall("note"))
                        self.assertEqual(total, 16)
                self.assertEqual(root.findall("part")[2].findtext(".//clef/clef-octave-change"), "2")   # 글로켄슈필 15ma
                self.assertEqual(len(musicxml_reader.read_musicxml(path)["parts"]), 3)
                r = render.render_file(path, path + ".wav", 1.0, [], [], 0.0, None)
                self.assertEqual([s.timbre for s in r.settings][1:], ["sustain", "glockenspiel"])
            finally:
                for p in (out, out + ".wav"):
                    if os.path.exists(p):
                        os.remove(p)

    def test_up_to_four_voices(self):
        # 반주 성부 1~4개 — 1성부는 코드 이름(HarmGuitar), 맨 아래는 저음, 그 사이 가운데 성부는 칸마다 구성음 하나
        import xml.etree.ElementTree as ET
        for style in ("simple", "arpeggio"):
            for voices in (["guitar"], ["keyboard", "cello", "cello"],
                           ["xylophone", "guitar", "keyboard", "cello"], ["guitar"] * 4):
                root, info = harmonize.harmonize_root(ET.fromstring(self._melody()), style=style, voices=voices)
                parts = root.findall("part")[1:]
                self.assertEqual(len(parts), len(voices) + voices.count("keyboard"))   # 혼자인 건반은 두 손
                self.assertEqual(parts[0].get("id"), "HarmGuitar")
                self.assertTrue(parts[0].findall(".//harmony"))
                names = [sp.findtext("part-name") for sp in root.findall("part-list/score-part")][1:]
                self.assertEqual(len(set(names)), len(names), names)     # 성부 이름이 겹치지 않음
                for part in parts:
                    for m in part.findall("measure"):
                        total = sum(int(n.findtext("duration")) for n in m.findall("note") if n.find("chord") is None)
                        self.assertEqual(total, 16)
                if voices == ["guitar"] * 4:     # 가운데 두 성부는 같은 순간에 대체로 서로 다른 음이름을 맡는다
                    def timeline(part):
                        out, t = [], 0
                        for n in part.iter("note"):
                            if n.find("chord") is not None:
                                continue
                            d = int(n.findtext("duration"))
                            out.append((t, t + d, n.findtext("pitch/step"), n.findtext("pitch/alter")))
                            t += d
                        return out
                    a, b = timeline(parts[1]), timeline(parts[2])
                    times = sorted({x[0] for x in a} | {x[0] for x in b})
                    at = lambda line, t: next((x[2:] for x in line if x[0] <= t < x[1]), None)
                    same = sum(at(a, t) == at(b, t) for t in times)
                    self.assertLess(same, len(times) // 2, (style, voices))
        with self.assertRaises(ValueError):
            harmonize.harmonize_root(ET.fromstring(self._melody()), voices=["guitar"] * 5)

    def test_drop_d_and_contrabass_guitar(self):
        # 드롭 D: 근음 D 코드는 6번 줄 개방 D(기타 D2=38·콘트라베이스 기타 D1=26), 첫 마디에 ⑥=D.
        # 콘트라베이스 기타: 15vb 자리표 + transpose -1 — 읽기(+12 보정) 뒤 적은 음 그대로(소리 높이) 재생
        import xml.etree.ElementTree as ET
        fs = lambda o, d: _note("F", o, d).replace("<step>F</step>", "<step>F</step><alter>1</alter>")
        ms = [_note("D", 4, 1) + fs(4, 1) + _note("A", 4, 1) + fs(4, 1),
              _note("G", 4, 1) + _note("B", 4, 1) + _note("A", 4, 1) + fs(4, 1),
              _note("E", 4, 1) + _note("G", 4, 1) + fs(4, 1) + _note("E", 4, 1),
              _note("D", 4, 4, "whole")]
        xml = _score_xml(ms).replace("</time>", "</time><key><fifths>2</fifths></key>", 1)
        with _TmpScore(xml) as src:
            out = src + "_h.musicxml"
            try:
                path, info = harmonize.harmonize_file(src, out, style="arpeggio",
                                                      voices=["guitar:dropd", "contrabass_guitar:dropd"])
                self.assertEqual(info["key"], "D 장조")
                root = ET.parse(path).getroot()
                gtr, cb = root.findall("part")[1:]
                self.assertEqual(gtr.findtext("measure/direction/direction-type/words"), "⑥=D")
                self.assertEqual(cb.findtext("measure/attributes/clef/clef-octave-change"), "-2")
                self.assertEqual(cb.findtext("measure/attributes/transpose/octave-change"), "-1")
                score = musicxml_reader.read_musicxml(path)
                cb_notes = [n[2] for n in score["notes"] if n[3] == 2]
                self.assertEqual(min(cb_notes), 26)                   # D1 — 드롭 D 개방현
                self.assertTrue(all(26 <= p <= 60 for p in cb_notes))
                path, _ = harmonize.harmonize_file(src, out, style="arpeggio", voices=["keyboard", "guitar:dropd"])
                gtr_notes = [n[2] for n in musicxml_reader.read_musicxml(path)["notes"] if n[3] == 3]  # 오른손·왼손 다음
                self.assertEqual(min(gtr_notes), 38)                  # D2
            finally:
                if os.path.exists(out):
                    os.remove(out)
        with self.assertRaises(ValueError):
            harmonize.harmonize_root(ET.fromstring(xml), voices=["cello:dropd"])

    def test_chord_notes_and_fastest_note(self):
        # 화음 악기 성부 설정 — ":nK" 화음 음 수 최대 K(1성부 블록 화음·저음 성부 묶음), ":fD" 가장 빠른 음표(분산화음 단위)
        import xml.etree.ElementTree as ET

        def onsets(part):
            """[(동시에 울리는 음 수, 길이 divisions)] — 붙임줄 뒤쪽 조각은 같은 음의 이어짐이라 뺌."""
            out = []
            for n in part.iter("note"):
                if n.find("pitch") is None or any(t.get("type") == "stop" for t in n.findall("tie")):
                    continue
                if n.find("chord") is not None:
                    out[-1][0] += 1
                else:
                    out.append([1, int(n.findtext("duration"))])
            return out

        # 정한 값은 한계 — 넘지 않고, 곡에서 적어도 MIN_USES곳은 그대로 나오면 됨(나머지는 탄력적으로)
        need = __import__("voice_lead").MIN_USES
        mel = ET.fromstring(self._melody())
        for k in (2, 3, 4, 5, 6):
            root, _ = harmonize.harmonize_root(mel, style="block", voices=[f"guitar:n{k}", "keyboard:n2"])
            top, *_, bass = root.findall("part")[1:]          # 건반은 오른손·왼손
            cs = [c for c, _ in onsets(top)]
            self.assertEqual(max(cs), k, k)
            self.assertGreaterEqual(cs.count(k), need, k)
            self.assertTrue(all(c <= 2 for c, _ in onsets(bass)))     # 근음 + 5음까지
        for d, div in ((16, 1), (8, 2), (4, 4)):
            root, _ = harmonize.harmonize_root(mel, style="arpeggio", voices=[f"guitar:f{d}", "guitar"])
            lns = [ln for _, ln in onsets(root.findall("part")[1])]
            self.assertEqual(min(lns), div, d)
            self.assertGreaterEqual(lns.count(div), need, d)
        # 가장 빠른 음표를 정하면 자동 판정(동요식)·블록이어도 그 음표가 나옴(8분을 골랐는데 안 나오던 문제)
        for style in ("simple", "block", "arpeggio"):
            root, _ = harmonize.harmonize_root(mel, style=style, voices=["guitar:f8", "keyboard:f4"])
            top, *_, bass = root.findall("part")[1:]
            lns = [ln for _, ln in onsets(top)]
            self.assertEqual(min(lns), 2, style)                      # 8분 = 2 divisions
            self.assertGreaterEqual(lns.count(2), need, style)
            # 저음은 특수한 경우 — 정한 음표는 한계로만(4분보다 빠르지 않게, 길게 받쳐도 됨)
            self.assertTrue(all(ln >= 4 for _, ln in onsets(bass)), style)
        # 모든 성부에 해당 — 가운데 성부도, 한 음씩 내는 악기도 빠른 음표를 정함
        root, _ = harmonize.harmonize_root(mel, style="simple",
                                           voices=["guitar:f8", "keyboard:n2:f4", "xylophone:f8", "guitar:n6:f16"])
        top, mid, _lh, mid2, bass = root.findall("part")[1:]
        self.assertGreaterEqual([c for c, _ in onsets(mid)].count(2), need)
        self.assertTrue(all(c <= 2 for c, _ in onsets(mid)))
        self.assertGreaterEqual([ln for _, ln in onsets(mid2)].count(2), need)
        self.assertTrue(all(c <= harmonize.BASS_MAX_STACK for c, _ in onsets(bass)))   # 저음 화음은 2음까지
        self.assertTrue(any("한계로만" in t for _, t in harmonize.check_voices(["guitar", "guitar:n4"])))
        # 동요식(simple)도 화음 음 수를 정하면 1성부가 멜로디 아래 블록 화음(적어도 MIN_USES곳)
        root, _ = harmonize.harmonize_root(mel, style="simple", voices=["guitar:n3", "guitar"])
        self.assertGreaterEqual([c for c, _ in onsets(root.findall("part")[1])].count(3), need)
        # 콘트라베이스 기타 6음은 음역이 좁으면 줄여서라도 만든다
        root, _ = harmonize.harmonize_root(mel, style="block", voices=["contrabass_guitar:n6"])
        cs = [c for c, _ in onsets(root.findall("part")[1])]
        self.assertTrue(all(c <= 6 for c in cs) and max(cs) >= 2)
        self.assertEqual(harmonize.parse_voice_opts("xylophone:f16")[1]["fastest"], 16)
        for bad in ("cello:n3", "guitar:n7", "guitar:f32"):
            with self.assertRaises(ValueError):
                harmonize.parse_voice_opts(bad)
        self.assertEqual(harmonize.make_voice("guitar", True, 4, 16), "guitar:dropd:n4:f16")
        self.assertEqual(harmonize.parse_voice_opts("guitar:dropd:n4:f16"),
                         ("guitar", {"drop": True, "notes": 4, "fastest": 16, "hand": None}))

    def test_color_by_chord_all_parts(self):
        # 코드별 색깔 — 단선율이든 화음이든 악보 전체로. 코드 이름이 없으면 모든 성부 음으로 정해 맨 위 오선 위에,
        # 있으면(어느 성부든) 그대로 따름. 전주 필드는 안 씀(조판이 앞 마디를 멜로디만으로 떼어 내지 않게)
        import xml.etree.ElementTree as ET
        ms1 = [_note("C", 5, 4, "whole"), _note("B", 4, 4, "whole"), _note("C", 5, 4, "whole")]
        ms2 = [_note("E", 4, 1) + _note("G", 4, 1) + _note("C", 4, 2, "half"),
               _note("D", 4, 1) + _note("F", 4, 1) + _note("G", 3, 2, "half"),
               _note("E", 4, 1) + _note("G", 4, 1) + _note("C", 4, 2, "half")]
        with _TmpScore(_score_xml(ms1, ms2)) as src:
            out = None
            try:
                out, info = harmonize.color_file(src, melody_part=None)
                root = ET.parse(out).getroot()
                p1, p2 = root.findall("part")
                self.assertTrue(p1.findall(".//harmony"))
                self.assertFalse(p2.findall(".//harmony"))
                self.assertEqual(info["chords"][0][1], "C")
                self.assertEqual(info["chords"][-1][1], "C")
                self.assertFalse(harmonize.has_field(root, harmonize.MELODY_ONLY_FIELD))
                rows = harmonize.read_chord_map(out)
                self.assertEqual(len(rows), 3)
                self.assertTrue(all(rows))
                # 다시 누르면 같은 파일에(이미 _색깔) — 이번엔 적힌 코드를 따름
                again, info2 = harmonize.color_file(out, melody_part=None)
                self.assertEqual(again, out)
                self.assertTrue(info2["chords_given"])
                self.assertEqual(len(ET.parse(again).getroot().findall("part")[0].findall(".//harmony")),
                                 len(p1.findall(".//harmony")))
                # 전주(색 제외) — 색만 빼고 성부는 그대로(조판이 앞 마디를 멜로디만으로 떼어 내지 않음)
                harmonize.color_file(out, melody_part=None, skip_measures=1)
                r3 = ET.parse(out).getroot()
                self.assertEqual(harmonize.prelude_measures(r3), 1)
                self.assertEqual(__import__("score_view").melody_only_measures(r3), 0)
                rows = harmonize.read_chord_map(out)
                self.assertEqual(rows[0], [])
                self.assertTrue(rows[1] and rows[2])
            finally:
                if out and os.path.exists(out):
                    os.remove(out)

    def test_new_arrangement_each_time_and_saved(self):
        # 같은 조건이라도 만들 때마다 다른 반주(씨앗), 같은 씨앗이면 같은 반주. 저장은 덮어쓰지 않고 _화성_2 …,
        # 화성 붙인 악보에서 다시 만들면 앞 반주를 빼고 새로 붙임
        import xml.etree.ElementTree as ET
        mel = ET.fromstring(self._melody())
        notes = lambda r: [ET.tostring(p) for p in r.findall("part")[1:]]
        a, ia = harmonize.harmonize_root(mel, voices=["guitar", "guitar", "guitar"], seed=1)
        b, _ = harmonize.harmonize_root(mel, voices=["guitar", "guitar", "guitar"], seed=1)
        self.assertEqual(notes(a), notes(b))
        self.assertEqual(ia["seed"], 1)
        outs = {tuple(notes(harmonize.harmonize_root(mel, voices=["guitar", "guitar", "guitar"], seed=s)[0]))
                for s in range(2, 8)}
        self.assertGreater(len(outs), 1)
        with _TmpScore(self._melody()) as src:
            made = []
            try:
                for _ in range(3):
                    made.append(harmonize.harmonize_file(src, voices=["guitar", "guitar"])[0])
                stem = os.path.splitext(src)[0]
                self.assertEqual(made, [stem + "_화성.musicxml", stem + "_화성_2.musicxml", stem + "_화성_3.musicxml"])
                again, _ = harmonize.harmonize_file(made[1], voices=["keyboard", "cello"])
                self.assertEqual(again, stem + "_화성_4.musicxml")
                root = ET.parse(again).getroot()
                self.assertEqual([p.get("id") for p in root.findall("part")][1:], ["HarmGuitar", "HarmMid1", "HarmBass"])
                seeds = [f for f in root.iter("miscellaneous-field") if f.get("name") == harmonize.SEED_FIELD]
                self.assertEqual(len(seeds), 1)
            finally:
                for p in made + [stem + "_화성_4.musicxml"]:
                    if os.path.exists(p):
                        os.remove(p)

    def test_voice_part_names(self):
        # 성부 이름: 기타1·기타2·콘트라기타·자일로·글로켄·오른손·왼손 — 같은 이름이 둘 이상이면 번호, 건반은 저음이면 왼손
        names = harmonize.voice_part_names
        self.assertEqual(names(["guitar", "guitar:dropd"]), ["기타1", "기타2"])
        self.assertEqual(names(["keyboard", "keyboard"]), ["오른손", "왼손"])
        self.assertEqual(names(["keyboard", "keyboard", "keyboard"]), ["오른손1", "오른손2", "왼손"])
        self.assertEqual(names(["xylophone", "glockenspiel", "cello", "contrabass_guitar:n2"]),
                         ["자일로", "글로켄", "첼로", "콘트라기타"])
        self.assertEqual(names(["keyboard"]), ["오른손"])
        import xml.etree.ElementTree as ET
        root, _ = harmonize.harmonize_root(ET.fromstring(self._melody()), voices=["guitar", "keyboard", "guitar"])
        # 건반을 고르면 오른손·왼손이 다 나온다 — 혼자인 건반은 두 성부(높은음·낮은음자리표)로 나뉨
        part_names = lambda r: [sp.findtext("part-name") for sp in r.findall("part-list/score-part")][1:]
        self.assertEqual(part_names(root), ["기타1", "오른손", "왼손", "기타2"])
        clefs = [p.findtext("measure/attributes/clef/sign") for p in root.findall("part")[1:]]
        self.assertEqual(clefs, ["G", "G", "F", "G"])
        for voices, want in ((["keyboard"], ["오른손", "왼손"]), (["keyboard", "keyboard"], ["오른손", "왼손"]),
                             (["guitar", "keyboard:n3"], ["기타", "오른손", "왼손"]),
                             (["keyboard", "keyboard", "cello"], ["오른손", "왼손", "첼로"])):
            r, info = harmonize.harmonize_root(ET.fromstring(self._melody()), voices=voices)
            self.assertEqual(part_names(r), want, voices)
            self.assertEqual(len(info["voices"]), len(want))
        self.assertEqual(harmonize.expand_hands(["guitar", "keyboard:n3"]),
                         ["guitar", "keyboard:n3:rh", "keyboard:n3:lh"])
        self.assertTrue(any("오른손" in t for _, t in harmonize.check_voices(["guitar", "keyboard"])))

    def test_voice_guide(self):
        # 성부 악기 가이드 — 저음에 높은 악기·성부 뒤집힘은 경고(+순서 바로잡기), 무난한 구성은 조용히
        warns = lambda v: [t for lvl, t in harmonize.check_voices(v) if lvl == "warn"]
        self.assertEqual(harmonize.check_voices(["guitar", "guitar"]), [])
        self.assertEqual(warns(["guitar", "cello", "guitar", "contrabass_guitar:dropd"]), [])
        self.assertEqual(len(warns(["guitar", "glockenspiel"])), 1)                  # 저음에 글로켄슈필
        self.assertEqual(len(warns(["keyboard", "cello", "xylophone", "guitar"])), 1)   # 자일로폰이 첼로 위로
        for v in (["guitar", "glockenspiel"], ["keyboard", "cello", "xylophone", "guitar"],
                  ["cello", "xylophone", "glockenspiel", "guitar:dropd"]):
            fixed = harmonize.fix_voice_order(v)
            self.assertEqual(sorted(fixed), sorted(v))            # 악기는 그대로, 순서만
            self.assertEqual(warns(fixed), [], fixed)
        self.assertEqual(harmonize.fix_voice_order(["guitar", "cello"]), ["guitar", "cello"])   # 고칠 것 없으면 그대로
        infos = [t for lvl, t in harmonize.check_voices(["guitar:dropd", "contrabass_guitar", "cello"]) if lvl == "info"]
        self.assertEqual(len(infos), 2)                       # 위 성부 드롭 D, 가운데 콘트라베이스 기타

    def test_minor_key_uses_raised_leading_tone(self):
        import xml.etree.ElementTree as ET
        # A 단조: 라시도미 / 파#… 대신 솔#(올린 7음)이 나오는 멜로디
        ms = [_note("A", 4, 1) + _note("B", 4, 1) + _note("C", 5, 1) + _note("E", 5, 1),
              _note("D", 5, 1) + _note("B", 4, 1) + _note("G", 4, 1, extra="") + _note("E", 4, 1),
              _note("G", 4, 4, "whole", extra="").replace("<step>G</step>", "<step>G</step><alter>1</alter>"),
              _note("A", 4, 4, "whole")]
        xml = _score_xml(ms).replace("</time>", "</time><key><fifths>0</fifths><mode>minor</mode></key>", 1)
        root, info = harmonize.harmonize_root(ET.fromstring(xml))
        self.assertEqual(info["key"], "A 단조")
        self.assertEqual(info["chords"][-1][1], "Am")

    def test_simple_song_gets_three_guitar_parts(self):
        import xml.etree.ElementTree as ET
        root, info = harmonize.harmonize_root(ET.fromstring(self._melody()))   # style="auto" → 단순한 곡은 simple
        self.assertEqual(info["style"], "simple")
        self.assertEqual(len(root.findall("part")), 3)       # 멜로디 + 기타 둘
        self.assertGreaterEqual(len(info["chords"]), 4)      # 마디당 코드 하나가 기본, 멜로디가 원하면 마디 안에서 바뀜
        self.assertEqual(info["chords"][-1][1], "C")
        for part in root.findall("part")[1:]:
            for m in part.findall("measure"):
                total = sum(int(n.findtext("duration")) for n in m.findall("note") if n.find("chord") is None)
                self.assertEqual(total, 16)
        with _TmpScore(self._melody()) as src:
            out = src + "_h.musicxml"
            try:
                path, _ = harmonize.harmonize_file(src, out)
                score = musicxml_reader.read_musicxml(path)
                r = render.render_file(path, path + ".wav", 1.0, [], [], 0.0, None)
                self.assertEqual([s.timbre for s in r.settings], ["guitar"] * 3)
            finally:
                for q in (out, out + ".wav"):
                    if os.path.exists(q):
                        os.remove(q)

    def test_skip_first_measures_melody_only(self):
        import xml.etree.ElementTree as ET
        root, info = harmonize.harmonize_root(ET.fromstring(self._melody()), skip_measures=2)
        self.assertEqual(info["skip_measures"], 2)
        self.assertTrue(all(int(c[0]) >= 3 for c in info["chords"]))           # 3마디부터 코드(마디 번호 1부터)
        for part in root.findall("part")[1:]:                                   # 반주 성부: 앞 두 마디는 쉼표, 길이는 그대로
            for i, m in enumerate(part.findall("measure")):
                total = sum(int(n.findtext("duration")) for n in m.findall("note") if n.find("chord") is None)
                self.assertEqual(total, 16)
                if i < 2:
                    self.assertTrue(all(n.find("rest") is not None for n in m.findall("note")))
        with _TmpScore(self._melody()) as src:
            out = src + "_h.musicxml"
            try:
                harmonize.harmonize_file(src, out, skip_measures=2)
                cm = harmonize.read_chord_map(out)
                self.assertEqual([len(r) for r in cm[:2]], [0, 0])               # 앞 두 마디는 색 없음
                self.assertGreater(len(cm[2]), 0)
                score = musicxml_reader.read_musicxml(out)
                firsts = [min(n[0] for n in score["notes"] if n[3] == pi) for pi in (0, 1, 2)]
                self.assertEqual(firsts[0], 0.0)
                self.assertEqual(firsts[1:], [8.0, 8.0])                         # 반주는 3마디(=8박) 뒤부터 소리
            finally:
                if os.path.exists(out):
                    os.remove(out)
        with self.assertRaises(ValueError):
            harmonize.harmonize_root(ET.fromstring(self._melody()), skip_measures=4)

    def test_solo_intro_is_laid_out_as_single_staff_lines(self):
        import xml.etree.ElementTree as ET
        import score_view
        root, _ = harmonize.harmonize_root(ET.fromstring(self._melody()), skip_measures=2)
        self.assertEqual(score_view.melody_only_measures(root), 2)              # 표시가 악보에 적힘
        intro, main = score_view._split_solo_intro(root, 2)
        self.assertEqual(len(intro.findall("part")), 1)                          # 앞부분은 멜로디 한 보표
        self.assertEqual(len(intro.find("part").findall("measure")), 2)
        self.assertEqual(len(main.findall("part")), 3)                           # 나머지는 모든 성부
        self.assertEqual(len(main.find("part").findall("measure")), 2)
        for i, p in enumerate(main.findall("part")):                             # 줄이 바뀌어도 첫 마디 속성(박자표 등)이 복사돼 있어야 함
            attr = p.find("measure/attributes")
            self.assertIsNotNone(attr.find("time"))
            self.assertIsNotNone(attr.find("divisions"))
            if i:                                                                # 반주 성부는 자리표·조표도
                self.assertIsNotNone(attr.find("clef"))
                self.assertIsNotNone(attr.find("key"))
        svgs = score_view._layout_systems(root, 2100)
        self.assertGreaterEqual(len(svgs), 2)                                    # 앞부분 줄 + 나머지 줄
        no_marker, _ = harmonize.harmonize_root(ET.fromstring(self._melody()))
        self.assertEqual(score_view.melody_only_measures(no_marker), 0)

    def test_first_lyric_measure_marks_the_prelude(self):
        import xml.etree.ElementTree as ET
        lyr = '<lyric number="1"><syllable>single</syllable><text>가</text></lyric>'
        ms = [_note("C", 4, 4, "whole"), _note("D", 4, 4, "whole"), _note("E", 4, 4, "whole", extra=lyr), _note("C", 4, 4, "whole")]
        self.assertEqual(harmonize.first_lyric_measure(ET.fromstring(_score_xml(ms))), 2)   # 앞 두 마디 = 가사 없는 전주
        self.assertIsNone(harmonize.first_lyric_measure(ET.fromstring(self._melody())))       # 가사가 없으면 None(전주 개념 없음)

    def test_chord_symbol_accidentals_become_unicode(self):
        import score_view
        svg = ('<tspan font-size="405px">C</tspan><tspan font-family="Leipzig" font-size="720px"></tspan>'
               '<tspan font-size="405px">dim</tspan><tspan font-family="Leipzig" font-size="720px"></tspan>')
        out = score_view._fix_smufl_text(svg)
        self.assertIn("♯", out)                    # ♯ — 예전엔 사설 영역 글자가 그대로 나가 한자로 보였음
        self.assertIn("♭", out)                    # ♭
        self.assertNotIn("", out)
        self.assertNotIn("", out)

    def test_color_melody_keeps_single_staff_and_colors_from_start(self):
        import xml.etree.ElementTree as ET
        root, info = harmonize.color_melody_root(ET.fromstring(self._melody()), skip_measures=1)
        self.assertEqual(len(root.findall("part")), 1)                          # 성부를 더하지 않음(단선율)
        self.assertEqual(info["style"], "color")
        self.assertTrue(harmonize.has_field(root, harmonize.COLOR_ALL_FIELD))   # 앱이 처음부터 칠함
        self.assertEqual(harmonize._int_field(root, harmonize.MELODY_ONLY_FIELD), 1)
        measures = root.find("part").findall("measure")
        self.assertIsNone(measures[0].find("harmony"))                           # 전주 마디에는 코드 이름 없음
        self.assertIsNotNone(measures[1].find("harmony"))                        # 그 뒤부터 코드 이름을 멜로디 성부에 끼움
        # 코드 이름은 그 칸 첫 음표 앞에 놓임
        kids = list(measures[1])
        h_idx = next(i for i, e in enumerate(kids) if e.tag == "harmony")
        self.assertEqual(kids[h_idx + 1].tag, "note")
        with _TmpScore(self._melody()) as src:
            out = src + "_c.musicxml"
            try:
                harmonize.color_file(src, out, skip_measures=1)
                cm = harmonize.read_chord_map(out)
                self.assertEqual(len(cm[0]), 0)                                   # 전주는 색 없음
                self.assertTrue(all(len(r) > 0 for r in cm[1:]))                  # 이후는 전부 코드 색
                score = musicxml_reader.read_musicxml(out)
                self.assertEqual(len(score["parts"]), 1)                          # 소리도 멜로디뿐
                _, info0 = harmonize.color_file(src, out, skip_measures=0)
                self.assertEqual(len(harmonize.read_chord_map(out)[0]) > 0, True)  # 0이면 처음부터
            finally:
                if os.path.exists(out):
                    os.remove(out)
        with self.assertRaises(ValueError):
            harmonize.color_melody_root(ET.fromstring(self._melody()), skip_measures=4)

    def test_song_layout_four_measures_per_line(self):
        import xml.etree.ElementTree as ET
        import score_view
        plain = ET.fromstring(self._melody())
        self.assertFalse(score_view.song_layout_mode(plain))                      # 일반 악보는 기존 자동 줄 나눔 그대로
        colored, _ = harmonize.color_melody_root(plain, skip_measures=0)
        self.assertTrue(score_view.song_layout_mode(colored))                     # 이 앱이 만든 화성·색깔 악보는 동요 조판
        harmonized, _ = harmonize.harmonize_root(plain)
        self.assertTrue(score_view.song_layout_mode(harmonized))
        ms = [_note("C", 4, 4, "whole")] * 10
        root = ET.fromstring(_score_xml(ms))
        svgs = score_view._layout_systems(root, 2100, fixed_lines=True)
        counts = [s.count('class="measure"') for s in svgs]
        self.assertEqual(counts, [4, 4, 2])                                       # 줄마다 4마디씩

    def test_chord_map_for_background_colors(self):
        with _TmpScore(self._melody()) as src:
            out = src + "_h.musicxml"
            try:
                self.assertIsNone(harmonize.read_chord_map(src))        # 화성 안 붙인 악보는 색 없음
                harmonize.harmonize_file(src, out, style="arpeggio")
                cm = harmonize.read_chord_map(out)
                self.assertEqual(len(cm), 4)
                self.assertEqual(len(cm[0]), 2)                          # 4/4 마디당 2칸
                self.assertAlmostEqual(cm[0][0][1], 0.5)
                self.assertEqual(cm[3][-1][2], harmonize.chord_color("C", "major"))   # 끝은 C 화음 색
            finally:
                if os.path.exists(out):
                    os.remove(out)

    def test_major_bright_minor_dark(self):
        def lum(c):
            return sum(int(c[i:i + 2], 16) for i in (1, 3, 5))
        for letter in "ABCDEFG":
            maj, dom = harmonize.chord_color(letter, "major"), harmonize.chord_color(letter, "dominant")
            mnr, dim = harmonize.chord_color(letter, "minor"), harmonize.chord_color(letter, "diminished")
            self.assertNotEqual(maj, mnr)
            self.assertGreater(lum(maj), lum(mnr))
            self.assertGreater(lum(mnr), lum(dim))
            self.assertGreater(lum(dom), lum(mnr))
            # 같은 글자 코드(D·D7·D#7)는 같은 색상, 농도를 크게 달리해 구분
            sharp7 = harmonize.chord_color(letter, "dominant", 1)
            self.assertEqual(len({maj, dom, sharp7, harmonize.chord_color(letter, "major", -1)}), 4)
            sat = lambda c: (lambda r, g, b: (max(r, g, b) - min(r, g, b)) / max(1, max(r, g, b)))(
                *(int(c[i:i + 2], 16) for i in (1, 3, 5)))
            self.assertGreater(sat(dom) - sat(maj), 0.25)
            self.assertGreater(sat(sharp7) - sat(dom), 0.15)

    def test_chord_symbol_where_top_part_rests(self):
        # 맨 위 성부가 온마디 쉼표면 마디 가운데 코드는 <offset>으로(예전엔 마디 끝에 붙어 음표와 틀어짐 — 콜 니드라이)
        import xml.etree.ElementTree as ET
        ms1 = ['<note><rest measure="yes"/><duration>4</duration><voice>1</voice></note>'] * 2
        ms2 = [_note("D", 4, 2, "half") + _note("B", 3, 2, "half").replace("<step>B</step>", "<step>B</step><alter>-1</alter>"),
               _note("G", 3, 2, "half") + _note("D", 4, 2, "half")]
        with _TmpScore(_score_xml(ms1, ms2)) as src:
            out = None
            try:
                out, info = harmonize.color_file(src, melody_part=None)
                m1 = ET.parse(out).getroot().findall("part")[0].find("measure")
                hs = m1.findall("harmony")
                self.assertEqual(len(hs), 2)
                self.assertEqual(sorted(h.findtext("offset") or "0" for h in hs), ["0", "2"])
                self.assertTrue(all(list(m1).index(h) < [e.tag for e in m1].index("note") for h in hs))
                rows = harmonize.read_chord_map(out)
                self.assertEqual([round(a, 2) for a, _b, _c, _n in rows[0]], [0.0, 0.5])
            finally:
                if out and os.path.exists(out):
                    os.remove(out)

    def test_uses_chord_names_already_in_the_score(self):
        import xml.etree.ElementTree as ET

        def harm(step, kind, text=""):
            return (f'<harmony><root><root-step>{step}</root-step></root>'
                    f'<kind text="{text}">{kind}</kind></harmony>')
        ms = [harm("G", "major") + _note("G", 4, 2, "half") + harm("D", "dominant", "7") + _note("A", 4, 2, "half"),
              _note("B", 4, 4, "whole"),
              harm("E", "minor", "m") + _note("E", 4, 4, "whole"),
              harm("G", "major") + _note("G", 4, 4, "whole")]
        with _TmpScore(_score_xml(ms)) as src:
            out = src + "_h.musicxml"
            try:
                path, info = harmonize.harmonize_file(src, out)
                self.assertTrue(info["chords_given"])
                self.assertEqual([c[1] for c in info["chords"]], ["G", "D7", "D7", "Em", "G"])  # 둘째 마디는 앞 코드 이어감
                cm = harmonize.read_chord_map(path)
                self.assertEqual([[n for _a, _b, _c, n in r] for r in cm], [["G", "D7"], ["D7"], ["Em"], ["G"]])
                self.assertAlmostEqual(cm[0][1][0], 0.5)
                # 같은 이름은 같은 색, 단조는 장조보다 어둡다
                self.assertEqual(cm[0][0][2], cm[3][0][2])
                _, info2 = harmonize.harmonize_file(src, out, use_chords=False)
                self.assertFalse(info2["chords_given"])
            finally:
                if os.path.exists(out):
                    os.remove(out)

    def _all_midi(self, root, part_index):
        out = []
        for n in root.findall("part")[part_index].iter("note"):
            p = n.find("pitch")
            if p is not None:
                out.append(12 * (int(p.findtext("octave")) + 1) + {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[p.findtext("step")]
                           + int(p.findtext("alter") or 0))
        return out

    def test_instrument_ranges_and_clefs(self):
        import xml.etree.ElementTree as ET
        # 낮은 멜로디(C3부터)로 음역 한계를 시험
        ms = [_note("C", 3, 1) + _note("D", 3, 1) + _note("E", 3, 1) + _note("F", 3, 1),
              _note("G", 3, 1) + _note("A", 3, 1) + _note("G", 3, 1) + _note("E", 3, 1),
              _note("F", 3, 1) + _note("D", 3, 1) + _note("G", 3, 1) + _note("B", 3, 1),
              _note("C", 3, 4, "whole")]
        for style in ("arpeggio", "block", "simple"):
            root, _ = harmonize.harmonize_root(ET.fromstring(_score_xml(ms)), style=style, instrument="guitar")
            names = [sp.findtext("part-name") for sp in root.findall("part-list/score-part")]
            self.assertTrue(all("기타" in n for n in names[1:]), names)
            for i in (1, 2):
                notes = self._all_midi(root, i)
                self.assertGreaterEqual(min(notes), 40, (style, i))    # 기타 가장 낮은 음 E2
                self.assertLessEqual(max(notes), 88)
                clef = root.findall("part")[i].find("measure/attributes/clef")
                self.assertEqual(clef.findtext("sign"), "G")            # 낮은음자리표 안 씀
                self.assertEqual(clef.findtext("clef-octave-change"), "-1")
        for style in ("block", "arpeggio", "simple"):
            root, info = harmonize.harmonize_root(ET.fromstring(_score_xml(ms)), style=style, instrument="keyboard")
            self.assertEqual(info["instrument"], "keyboard")
            rh, lh = self._all_midi(root, 1), self._all_midi(root, 2)
            self.assertGreaterEqual(min(rh), 55)
            self.assertGreaterEqual(min(lh), 28)
            self.assertLessEqual(max(lh), 55)
            lh_clef = root.findall("part")[2].find("measure/attributes/clef")
            self.assertEqual(lh_clef.findtext("sign"), "F")
            self.assertEqual(root.findall("part-list/score-part")[1].findtext("midi-instrument/midi-program"), "1")
        with self.assertRaises(ValueError):
            harmonize.harmonize_root(ET.fromstring(self._melody()), instrument="drums")

    def test_keyboard_renders_as_piano(self):
        with _TmpScore(self._melody()) as src:
            out = src + "_h.musicxml"
            try:
                path, _ = harmonize.harmonize_file(src, out, instrument="keyboard")
                r = render.render_file(path, path + ".wav", 1.0, [], [], 0.0, None)
                self.assertEqual([s.timbre for s in r.settings][1:], ["piano", "piano"])
            finally:
                for q in (out, out + ".wav"):
                    if os.path.exists(q):
                        os.remove(q)


class RoundSongTests(unittest.TestCase):
    def _xml(self, title):
        rest = '<note><rest measure="yes"/><duration>4</duration></note>'

        def part(pid, start):
            ms = []
            for i in range(6):
                head = ('<attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>'
                        if i == 0 else "")
                ms.append(f'<measure number="{i + 1}">{head}{rest if i < start else _note("C", 4, 4, "whole")}</measure>')
            return f'<part id="{pid}">{"".join(ms)}</part>'
        sp = "".join(f'<score-part id="P{i}"><part-name>성부{i}</part-name></score-part>' for i in (1, 2, 3))
        return (f'<?xml version="1.0"?><score-partwise version="3.1"><work><work-title>{title}</work-title></work>'
                f'<part-list>{sp}</part-list>{part("P1", 0)}{part("P2", 2)}{part("P3", 4)}</score-partwise>')

    def test_entries_by_part(self):
        with _TmpScore(self._xml("개구리 돌림노래")) as src:
            m = round_song.read_round_map(src)
            self.assertEqual(sorted(m), [0, 2, 4])                       # 성부마다 처음 음표가 나오는 마디
            self.assertEqual([m[k][0][0] for k in (0, 2, 4)], [0, 1, 2])  # 첫 보표 번호 = 성부 순서
            self.assertEqual(len({m[k][0][2] for k in m}), 3)             # 성부마다 다른 색

    def test_not_round_without_keyword(self):
        with _TmpScore(self._xml("반짝반짝 작은 별")) as src:
            self.assertIsNone(round_song.read_round_map(src))


    def test_round_with_chord_names_gives_chord_rows(self):
        harm = '<harmony><root><root-step>G</root-step></root><kind text="">major</kind></harmony>'
        xml = self._xml("돌림노래").replace('<note><rest measure="yes"/><duration>4</duration></note>',
                                          harm + '<note><rest measure="yes"/><duration>4</duration></note>', 1)
        with _TmpScore(xml) as src:
            info = round_song.read_round_info(src)
            self.assertEqual([e[2] for e in info["entries"]], [0, 2, 4])     # 성부별 시작 마디
            self.assertIsNotNone(info["chords"])
            self.assertEqual(len(info["chords"]), 6)                          # 마디마다(코드 없는 마디는 앞 코드 이어감)
            self.assertEqual(info["chords"][0][0][3], "G")
            self.assertEqual(info["chords"][5][0][3], "G")
        with _TmpScore(self._xml("돌림노래")) as src:
            self.assertIsNone(round_song.read_round_info(src)["chords"])      # 코드 이름 없으면 성부 색 방식


class MalletTimbreTests(unittest.TestCase):
    def test_instrument_name_and_gm_program_mapping(self):
        pick = synth.timbre_for_part
        self.assertEqual(pick("Xylophone", None), "xylophone")
        self.assertEqual(pick("자일로폰", None), "xylophone")
        self.assertEqual(pick("Glockenspiel", {"name": "", "program": None}), "glockenspiel")
        self.assertEqual(pick("글로켄슈필", None), "glockenspiel")
        self.assertEqual(pick("메탈로폰", None), "metallophone")
        self.assertEqual(pick("x", {"name": "", "program": 14}), "xylophone")      # GM 14
        self.assertEqual(pick("x", {"name": "", "program": 10}), "glockenspiel")   # GM 10
        self.assertEqual(pick("x", {"name": "", "program": 12}), "metallophone")   # GM 12(비브라폰)
        self.assertEqual(pick("Guitar I", {"name": "", "program": 25}), "guitar")  # 기존 판정은 그대로

    def test_bar_sounds_ring_decay_and_stop(self):
        s = synth.NoteSynth()
        sr = synth.SR
        for name in ("xylophone", "glockenspiel", "metallophone"):
            x = s.render(72, 90, 0.5, name)
            self.assertTrue(np.all(np.isfinite(x)), name)
            self.assertGreater(np.max(np.abs(x)), 0.1, name)
            self.assertEqual(float(x[-1]), 0.0)                                # 끝은 정확히 0
            early = np.sqrt(np.mean(x[: int(0.1 * sr)] ** 2))
            late = np.sqrt(np.mean(x[int(0.4 * sr): int(0.5 * sr)] ** 2))
            self.assertLess(late, early, name)                                 # 울림이 줄어듦
        # 자일로폰은 글로켄슈필보다 빨리 사라진다
        x1, x2 = s.render(72, 90, 1.5, "xylophone"), s.render(72, 90, 1.5, "glockenspiel")
        tail = lambda x: np.sqrt(np.mean(x[int(1.0 * sr): int(1.4 * sr)] ** 2))
        self.assertLess(tail(x1), tail(x2))
        # 막대 악기는 pizz.로 기타 음색이 되지 않음
        self.assertEqual(synth.pizz_timbre("xylophone"), "xylophone")

    def test_glockenspiel_partials_are_inharmonic(self):
        s = synth.NoteSynth()
        y = s.render(72, 100, 1.0, "glockenspiel")[: int(0.4 * synth.SR)]
        spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        freqs = np.fft.rfftfreq(len(y), 1 / synth.SR)
        f0 = float(synth.midi_to_hz(72))
        near = lambda f: spec[np.argmin(np.abs(freqs - f))]
        self.assertGreater(near(f0 * 2.76), 3 * near(f0 * 2.0))                # 2.76배가 있고 정수배(2배)는 없음

    def test_mallet_part_renders_in_score(self):
        xml = _score_xml([_note("C", 5, 1) + _note("E", 5, 1) + _note("G", 5, 1) + _note("C", 6, 1)]).replace(
            "<part-name>Guitar I</part-name>", "<part-name>Glockenspiel</part-name>").replace(
            "<midi-program>25</midi-program>", "<midi-program>10</midi-program>")
        with _TmpScore(xml) as src:
            out = src + ".wav"
            try:
                r = render.render_file(src, out, 1.0, [], [], 0.0, None)
                self.assertEqual(r.settings[0].timbre, "glockenspiel")
                self.assertGreater(r.length_sec, 1.0)
            finally:
                if os.path.exists(out):
                    os.remove(out)


class LiveTempoTest(unittest.TestCase):
    """재생 중 빠르기 바꾸기(render.TempoMap·TempoStream·LivePlayback, 2026-10-27)."""

    def test_tempo_map_real_and_nominal_are_inverse(self):
        m = render.TempoMap(10.0, 1.0)
        m.change(12.0, 13.0, 2.0)            # 12~13초(명목) 동안 1.0 → 2.0으로
        self.assertAlmostEqual(m.real(12.0), 2.0)
        self.assertAlmostEqual(m.real(13.0) - m.real(12.0), np.log(2.0), places=9)   # ∫ dn / (1 + n)
        self.assertAlmostEqual(m.real(15.0) - m.real(13.0), 1.0)                   # 2배 빠르기면 절반
        self.assertAlmostEqual(m.real(8.0), -2.0)                                   # 앞은 처음 빠르기
        for n in np.linspace(5.0, 20.0, 61):
            self.assertAlmostEqual(m.nominal(m.real(n)), n, places=9)
        m.change(14.0, 14.5, 0.5)            # 다시 바꾸면 그 앞은 그대로
        self.assertAlmostEqual(m.real(13.0) - m.real(12.0), np.log(2.0), places=9)

    def test_cursor_matches_sound_after_tempo_changes(self):
        # 메트로놈 딸깍만 켜고 재생 출력을 그대로 받아, 딸깍이 실제로 난 시각에 커서 계산(nominal_at)이 가리키는
        # 곡 위치가 박 위치(명목 0.5초 단위, ♩=120)와 같은지 — 빠르기를 두 번 바꾸고 한 박 동안 옮겨 가는 곳까지.
        quarter = "".join(_note(s, 4, 1) for s in "CDEF")
        with _TmpScore(_score_xml([quarter] * 10)) as path:
            score = musicxml_reader.read_musicxml(path)
        r = render.RenderedScore(score, 1.0)
        muted = [render.PartSetting(timbre=s.timbre, mute=True) for s in r.settings]
        live = render.LivePlayback(r, lambda: muted, lambda: 1.0, 0)
        sr, out, chunks, changes = r.sr, 0, [], [(1.0, 1.5), (5.0, 0.7)]
        for ch in live.chunks():
            chunks.append(ch)
            out += len(ch)
            while changes and out >= changes[0][0] * sr:
                live.set_tempo(changes.pop(0)[1])
        audio = np.abs(np.concatenate(chunks)[:, 0])
        onsets, last = [], -sr
        for i in np.flatnonzero(audio > 0.2):
            if i - last > 0.1 * sr:
                onsets.append(i)
            last = i
        self.assertTrue(live.switched)
        self.assertGreaterEqual(len(onsets), 38)       # 40박 거의 다(마지막 박 근처 잘림 허용)
        errs = []
        for i in onsets:
            nominal = live.nominal_at(i / sr)
            errs.append(abs(nominal - round(nominal / 0.5) * 0.5))
        self.assertLess(max(errs), 0.004, max(errs))   # 4ms(명목) 안 — 커서와 소리가 같은 곳
        gaps = np.diff(onsets) / sr
        self.assertAlmostEqual(gaps[0], 0.5, delta=0.003)            # 처음 100%
        self.assertTrue(any(abs(g - 0.5 / 1.5) < 0.003 for g in gaps))   # 150%
        self.assertAlmostEqual(gaps[-1], 0.5 / 0.7, delta=0.003)     # 마지막 70%
        self.assertTrue(all(g > 0.3 for g in gaps))                  # 박이 겹치거나 빠지지 않음


class VoiceLeadingTest(unittest.TestCase):
    """화성 붙이기 성부 진행(voice_lead, 2026-10-27) — 병행 5·8도 없이, 움직임은 성부끼리 고르게."""

    def _melody(self):
        # G 장조 8마디 — 4분음표·8분음표·긴 음이 섞인 멜로디(으뜸음으로 끝)
        ms = [_note("D", 5, 1) + _note("B", 4, 1) + _note("G", 4, 1) + _note("B", 4, 1),
              _note("C", 5, 1) + _note("A", 4, 1) + _note("F", 4, 1, extra="").replace(
                  "<step>F</step>", "<step>F</step><alter>1</alter>") + _note("A", 4, 1),
              _note("B", 4, 2, "half") + _note("G", 4, 2, "half"),
              _note("E", 5, 1) + _note("D", 5, 1) + _note("C", 5, 1) + _note("B", 4, 1),
              _note("A", 4, 1) + _note("B", 4, 1) + _note("C", 5, 1) + _note("A", 4, 1),
              _note("G", 4, 1) + _note("B", 4, 1) + _note("D", 5, 2, "half"),
              _note("C", 5, 1) + _note("A", 4, 1) + _note("D", 5, 1) + _note("C", 5, 1),
              _note("B", 4, 4, "whole")]
        return _score_xml(ms).replace("</time>", "</time><key><fifths>1</fifths></key>", 1)

    def test_no_parallels_and_even_motion(self):
        import voice_check
        cases = [("simple", ["guitar", "guitar"]), ("arpeggio", ["guitar"] * 4),
                 ("block", ["keyboard", "cello", "guitar", "contrabass_guitar"]), ("arpeggio", ["guitar", "guitar", "keyboard"])]
        with _TmpScore(self._melody()) as src:
            out = src + "_h.musicxml"
            try:
                for style, voices in cases:
                    harmonize.harmonize_file(src, out, style=style, voices=voices)
                    r = voice_check.analyze_file(out)
                    self.assertEqual((r["병행 5도"], r["병행 8도"], r["엇갈림"]), (0, 0, 0), (style, voices, r))
                    self.assertLessEqual(r["움직임 비율"], 1.6, (style, voices, r))   # 한 성부에 움직임이 몰리지 않음
            finally:
                if os.path.exists(out):
                    os.remove(out)

    def test_rules_in_skeleton(self):
        # 뼈대: 이끎음(F#)은 다음 으뜸화음에서 G로, 성부 차례(엇갈림 없음)
        import xml.etree.ElementTree as ET
        import voice_lead
        part = ET.fromstring(self._melody()).findall("part")[0]
        mel = harmonize.read_melody(part)
        tonic, minor = harmonize.detect_key(mel)
        slots = harmonize.make_slots(mel)
        harmonize.choose_chords(slots, mel.notes, tonic, minor)
        specs, _ = harmonize._voice_specs(["guitar"] * 4, "arpeggio", mel)
        hs = [voice_lead.Harmony(sl.chord, harmonize.line_pc) for sl in slots]
        lead, tpc = harmonize.line_pc(tonic + harmonize._LEADING), harmonize.line_pc(tonic)
        sk = voice_lead.plan_skeleton(slots, hs, mel.notes, specs, tpc, lead)
        self.assertTrue(all(a > b for s in sk for a, b in zip(s, s[1:])))
        for k in range(len(sk) - 1):
            if hs[k].dominant and tpc in hs[k + 1].pcs:
                for i, p in enumerate(sk[k]):
                    if p % 12 == lead and i in (0, len(sk[k]) - 1):     # 바깥 성부의 이끎음은 반드시 해결
                        self.assertEqual(sk[k + 1][i] - p, 1, (k, sk[k], sk[k + 1]))
