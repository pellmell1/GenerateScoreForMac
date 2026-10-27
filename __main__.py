"""
폴더 경로만으로 실행할 수 있게 해주는 진입점.

    python E:\\project\\guitar\\GenerateScore                      → 창(UI)으로 실행
    python E:\\project\\guitar\\GenerateScore 곡.musicxml -o 곡.wav  → 창 없이 WAV만 만들기(-o 곡.mp3면 MP3)

명령줄 옵션: --tempo 1.2(악보 빠르기의 1.2배), --mute 4(4번 성부 끄기, 여러 번 가능),
--solo 1(1번 성부만), --click 0.5(메트로놈 딸깍 음량, 0이면 없음), --harmonize [N](N번 성부 멜로디에 화성 붙이기).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _cli(argv):
    from render import render_file

    ap = argparse.ArgumentParser(prog="GenerateScore", description="악보 → WAV/MP3 소리 만들기")
    ap.add_argument("score", help="악보 파일(.xml/.musicxml/.mxl, MuseScore .mscz, Guitar Pro .gp/.gp5 등 — "
                                  "MusicXML 아닌 것은 MuseScore가 설치돼 있어야 함)")
    ap.add_argument("-o", "--out", help="저장할 경로(.wav 또는 .mp3, 없으면 악보 이름.wav)")
    ap.add_argument("--tempo", type=float, default=1.0, help="빠르기 배율(기본 1.0)")
    ap.add_argument("--mute", type=int, action="append", default=[], help="끌 성부 번호(1부터)")
    ap.add_argument("--solo", type=int, action="append", default=[], help="독주할 성부 번호(1부터)")
    ap.add_argument("--harmonize", type=int, metavar="N", nargs="?", const=1,
                    help="N번 성부(기본 1)를 멜로디로 보고 화성(기타+베이스)을 붙여서 만듦 — 붙인 악보는 이름_화성.musicxml로도 저장")
    ap.add_argument("--harm-instrument", choices=("guitar", "keyboard"), default="guitar",
                    help="--harmonize 반주 악기(기타: 가장 낮은 음 E2, 건반: 오른손·왼손)")
    ap.add_argument("--click", type=float, default=0.0, help="메트로놈 음량(0~1)")
    args = ap.parse_args(argv)
    out = args.out or os.path.splitext(args.score)[0] + ".wav"
    if args.harmonize:
        import harmonize
        args.score, info = harmonize.harmonize_file(args.score, melody_part=args.harmonize, instrument=args.harm_instrument)
        print(harmonize.summarize(info))
        print(f"화성 붙인 악보 → {args.score}")

    def progress(f):
        print(f"\r합성 중 {f * 100:5.1f}%", end="", flush=True)

    r = render_file(args.score, out, args.tempo, args.mute, args.solo, args.click, progress)
    print()
    names = ", ".join(f"{i + 1}.{n}({s.timbre})" for i, (n, s) in enumerate(zip(r.score["parts"], r.settings)))
    print(f"성부: {names}")
    print(f"음 {len(r.events)}개, 길이 {r.length_sec:.1f}초 → {out}")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    if len(sys.argv) > 1:
        _cli(sys.argv[1:])
    else:
        from app import GenerateApp
        GenerateApp().mainloop()
