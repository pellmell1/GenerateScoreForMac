# -*- coding: utf-8 -*-
"""중주연습 아이콘 만들기 — assets/ensemble.png(256px), assets/ensemble.ico(16~256px, Windows),
assets/ensemble.icns(16~1024px, Mac .app).

모양: 시안 A 청록(#1F5F66) 둥근 사각형 바탕 위에, 크림색 음표 네 개가 하나의 빔으로 이어진 그림.
네 음표 = 4중주의 네 성부(높이가 다른 머리), 하나로 이은 빔 = 함께 맞춰 연주. 아래 왼쪽에서 오른쪽으로
올라가는 선율 모양이라 작은 크기(16px)에서도 "음표 묶음"으로 읽히게 단순하게 그린다.

    python make_icon.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
TEAL = (31, 95, 102, 255)
TEAL_DARK = (22, 70, 76, 255)
CREAM = (251, 246, 234, 255)
AMBER = (217, 142, 46, 255)   # 구간 반복 주황 — 빔 위 작은 강조


def draw(size=1024):
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(s * 0.22)
    # 바탕(살짝 아래 그림자)
    d.rounded_rectangle((int(s * 0.04), int(s * 0.06), int(s * 0.96), int(s * 0.98)), r, fill=TEAL_DARK)
    d.rounded_rectangle((int(s * 0.04), int(s * 0.03), int(s * 0.96), int(s * 0.95)), r, fill=TEAL)

    # 음표 네 개: 머리 위치(가운데 x, 가운데 y) — 왼쪽 아래에서 오른쪽 위로 올라가는 선율
    head_w, head_h = s * 0.155, s * 0.115
    xs = [0.235, 0.415, 0.595, 0.775]
    ys = [0.735, 0.665, 0.700, 0.600]
    stem_w = s * 0.042
    beam_top_l, beam_top_r = s * 0.235, s * 0.165   # 빔 윗선(왼쪽·오른쪽 끝 y) — 오른쪽이 높게
    beam_h = s * 0.095
    stem_xs = []
    for cx, cy in zip(xs, ys):
        cx, cy = cx * s, cy * s
        # 기울어진 타원 머리(음표다운 모양) — 크게 그려 회전한 뒤 붙인다
        hw, hh = int(head_w * 2), int(head_h * 2)
        head = Image.new("RGBA", (hw * 2, hw * 2), (0, 0, 0, 0))
        ImageDraw.Draw(head).ellipse((hw - hw / 2, hw - hh / 2, hw + hw / 2, hw + hh / 2), fill=CREAM)
        head = head.rotate(22, resample=Image.BICUBIC).resize((hw, hw), Image.LANCZOS)
        img.alpha_composite(head, (int(cx - hw / 2), int(cy - hw / 2)))
        stem_xs.append(cx + head_w * 0.43)
    # 기둥: 머리 오른쪽에서 빔까지
    x0, x1 = stem_xs[0], stem_xs[-1]

    def beam_y(x):
        return beam_top_l + (beam_top_r - beam_top_l) * (x - x0) / (x1 - x0)

    for sx, cy in zip(stem_xs, ys):
        d.rectangle((sx - stem_w / 2, beam_y(sx), sx + stem_w / 2, cy * s - head_h * 0.15), fill=CREAM)
    # 빔(평행사변형) — 기둥 바깥으로 조금 나오게
    pad = stem_w / 2
    d.polygon([(x0 - pad, beam_y(x0)), (x1 + pad, beam_y(x1)), (x1 + pad, beam_y(x1) + beam_h),
               (x0 - pad, beam_y(x0) + beam_h)], fill=CREAM)
    # 둘째 빔(16분음표 느낌, 짧게) 대신 주황 점 하나로 "연습 중" 표시 — 너무 복잡해지지 않게
    rr = s * 0.055
    cx, cy = s * 0.80, s * 0.80
    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=AMBER)
    return img


def main():
    big = draw(1024)
    assets = os.path.join(HERE, "assets")
    os.makedirs(assets, exist_ok=True)
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(assets, "ensemble.png"))
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(assets, "ensemble.ico"), sizes=sizes)
    big.save(os.path.join(assets, "ensemble.icns"))   # Pillow가 1024px에서 작은 크기들을 줄여 담는다
    print("assets/ensemble.png, assets/ensemble.ico, assets/ensemble.icns")


if __name__ == "__main__":
    main()
