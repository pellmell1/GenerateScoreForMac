# -*- coding: utf-8 -*-
"""앱 화면용 둥근 위젯(Tkinter Canvas로 직접 그림) — 시안 A "연습 노트"(사용자 선택, 2026-10-07).

ttk 기본 위젯은 둥근 버튼·토글 칩·재생 진행 막대 모양을 낼 수 없어서 Canvas에 그린다.
크기는 모두 "논리 px"(100% 배율 기준)로 받아 화면 배율(u)을 곱한다 — DPI 인식을 켠 앱이라
그대로 쓰면 150% 화면에서 작아 보인다. 색은 시안(아티팩트 "악보 소리 생성 UI 시안"의 A — 앱 이름은 이후 "중주연습")과 같게.
"""
import tkinter as tk

# 시안 A 팔레트
BG = "#ECE8E0"          # 악보 바탕(따뜻한 회색)
PANEL = "#FBFAF7"       # 머리글·재생 막대 바탕
BORDER = "#DDD7CC"
TEXT = "#24221F"
MUTED = "#6B655C"
ACCENT = "#1F5F66"      # 청록 — 재생 버튼·진행·켜진 칩
ACCENT_SOFT = "#E6EEEE"
LOOP = "#D98E2E"        # 구간 반복(주황)
LOOP_SOFT = "#FFF6EA"
LOOP_TEXT = "#8A5414"
PILL = "#F1EDE6"
TRACK = "#E3DED4"
PLAYHEAD = "#C2462E"
DARK = "#24221F"
WHITE = "#FFFFFF"
DISABLED = "#B9B3A8"

FONT = "Malgun Gothic"
TITLE_FONT = "Batang"   # Windows 기본 명조(시안의 Gowun Batang 대신)


def ui_scale(widget):
    return widget.winfo_fpixels("1i") / 96.0


def round_rect(c, x0, y0, x1, y1, r, **kw):
    """Canvas에 둥근 사각형(다각형 + smooth)."""
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return c.create_polygon(pts, smooth=True, splinesteps=12, **kw)


# ---------------------------------------------------------------- 아이콘(선 그림)

def draw_icon(c, name, cx, cy, s, color):
    """s = 아이콘 한 변 길이(px)."""
    h = s / 2
    w = max(1.5, s / 9)
    if name == "play":
        c.create_polygon(cx - h * 0.55, cy - h * 0.75, cx + h * 0.8, cy, cx - h * 0.55, cy + h * 0.75,
                         fill=color, outline=color)
    elif name == "pause":
        bw = s * 0.2
        c.create_rectangle(cx - h * 0.6, cy - h * 0.7, cx - h * 0.6 + bw, cy + h * 0.7, fill=color, outline=color)
        c.create_rectangle(cx + h * 0.6 - bw, cy - h * 0.7, cx + h * 0.6, cy + h * 0.7, fill=color, outline=color)
    elif name == "home":
        c.create_line(cx - h * 0.6, cy - h * 0.65, cx - h * 0.6, cy + h * 0.65, fill=color, width=w,
                      capstyle="round")
        c.create_polygon(cx + h * 0.65, cy - h * 0.65, cx - h * 0.35, cy, cx + h * 0.65, cy + h * 0.65,
                         fill="", outline=color, width=w, joinstyle="round")
    elif name == "folder":
        c.create_line(cx - h * 0.8, cy - h * 0.5, cx - h * 0.25, cy - h * 0.5, cx - h * 0.05, cy - h * 0.3,
                      cx + h * 0.8, cy - h * 0.3, cx + h * 0.8, cy + h * 0.6, cx - h * 0.8, cy + h * 0.6,
                      cx - h * 0.8, cy - h * 0.5, fill=color, width=w, joinstyle="round")
    elif name == "clock":
        c.create_oval(cx - h * 0.75, cy - h * 0.75, cx + h * 0.75, cy + h * 0.75, outline=color, width=w)
        c.create_line(cx, cy - h * 0.4, cx, cy, cx + h * 0.3, cy + h * 0.2, fill=color, width=w,
                      capstyle="round", joinstyle="round")
    elif name == "download":
        c.create_line(cx, cy - h * 0.7, cx, cy + h * 0.2, fill=color, width=w, capstyle="round")
        c.create_line(cx - h * 0.4, cy - h * 0.15, cx, cy + h * 0.25, cx + h * 0.4, cy - h * 0.15, fill=color,
                      width=w, capstyle="round", joinstyle="round")
        c.create_line(cx - h * 0.7, cy + h * 0.7, cx + h * 0.7, cy + h * 0.7, fill=color, width=w,
                      capstyle="round")
    elif name == "down":
        c.create_line(cx - h * 0.45, cy - h * 0.2, cx, cy + h * 0.25, cx + h * 0.45, cy - h * 0.2, fill=color,
                      width=w, capstyle="round", joinstyle="round")
    elif name == "tri_down":   # 아래로 향한 꽉 찬 삼각형(최근 악보 목록 펼치기)
        c.create_polygon(cx - h * 0.5, cy - h * 0.25, cx + h * 0.5, cy - h * 0.25, cx, cy + h * 0.35,
                         fill=color, outline=color, joinstyle="round")
    elif name == "left":
        c.create_line(cx + h * 0.25, cy - h * 0.5, cx - h * 0.25, cy, cx + h * 0.25, cy + h * 0.5, fill=color,
                      width=w, capstyle="round", joinstyle="round")
    elif name == "right":
        c.create_line(cx - h * 0.25, cy - h * 0.5, cx + h * 0.25, cy, cx - h * 0.25, cy + h * 0.5, fill=color,
                      width=w, capstyle="round", joinstyle="round")
    elif name == "expand":
        k = h * 0.35
        for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            x, y = cx + sx * h * 0.7, cy + sy * h * 0.7
            c.create_line(x - sx * k, y, x, y, x, y - sy * k, fill=color, width=w, capstyle="round",
                          joinstyle="round")
    elif name == "gear":
        import math
        r1, r2 = h * 0.62, h * 0.85
        pts = []
        for i in range(16):
            ang = math.pi * 2 * i / 16
            r = r2 if i % 2 == 0 else r1
            pts += [cx + r * math.cos(ang), cy + r * math.sin(ang)]
        c.create_polygon(pts, fill="", outline=color, width=w, joinstyle="round")
        c.create_oval(cx - h * 0.28, cy - h * 0.28, cx + h * 0.28, cy + h * 0.28, outline=color, width=w)
    elif name == "note":
        c.create_line(cx - h * 0.15, cy + h * 0.45, cx - h * 0.15, cy - h * 0.6, cx + h * 0.55, cy - h * 0.75,
                      cx + h * 0.55, cy + h * 0.3, fill=color, width=w, joinstyle="round")
        c.create_oval(cx - h * 0.6, cy + h * 0.2, cx - h * 0.1, cy + h * 0.65, outline=color, width=w)
        c.create_oval(cx + h * 0.1, cy + h * 0.05, cx + h * 0.6, cy + h * 0.5, outline=color, width=w)


# ---------------------------------------------------------------- 둥근 버튼

class RoundButton(tk.Canvas):
    """둥근 버튼. kind: "outline"(흰 바탕·테두리), "solid"(색 바탕), "ghost"(테두리 없음).
    icon과 text를 둘 다 주면 아이콘이 왼쪽. circle=True면 원형(아이콘만)."""

    def __init__(self, parent, text="", icon=None, command=None, width=None, height=40, radius=10,
                 kind="outline", color=None, circle=False, font_size=10, bg=PANEL, icon_size=16):
        self.u = ui_scale(parent)
        u = self.u
        self._text = text
        self._icon = icon
        self._command = command
        self._kind = kind
        self._color = color or (ACCENT if kind == "solid" else TEXT)
        self._circle = circle
        self._font = (FONT, font_size)
        self._radius = radius
        self._icon_size = icon_size
        self._enabled = True
        self._hover = False
        h = int(height * u)
        if width is None:
            probe = tk.Label(parent, text=text, font=self._font)
            tw = probe.winfo_reqwidth() if text else 0
            probe.destroy()
            width = (h / u if circle else (tw / u + (icon_size + 8 if icon else 0) + 32))
        w = int(width * u)
        super().__init__(parent, width=w, height=h, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        self._draw()

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def set_label(self, text):
        """버튼 글자만 바꿈(폭은 그대로)."""
        self._text = text
        self._draw()

    def _click(self, _e):
        if self._enabled and self._command:
            self._command()

    def set_text(self, text):
        self._text = text
        self._draw()

    def set_icon(self, icon):
        self._icon = icon
        self._draw()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self.configure(cursor="hand2" if on else "arrow")
        self._draw()

    def _colors(self):
        k = self._kind
        if not self._enabled:
            if k == "solid":
                return DISABLED, DISABLED, WHITE
            return WHITE if k == "outline" else self["bg"], BORDER, DISABLED
        if k == "solid":
            fill = self._color
            return fill, fill, WHITE
        if k == "outline":
            return ("#F6F3EE" if self._hover else WHITE), "#D3CCBF", self._color
        return ("#EDE8E0" if self._hover else self["bg"]), ("#EDE8E0" if self._hover else self["bg"]), self._color

    def _draw(self):
        self.delete("all")
        u = self.u
        w, h = int(self["width"]), int(self["height"])
        fill, outline, fg = self._colors()
        if self._circle:
            self.create_oval(1, 1, w - 2, h - 2, fill=fill, outline=outline, width=max(1, u))
        else:
            round_rect(self, 1, 1, w - 2, h - 2, self._radius * u, fill=fill, outline=outline, width=max(1, u))
        isz = self._icon_size * u
        if self._icon and not self._text:
            draw_icon(self, self._icon, w / 2, h / 2, isz, fg)
            return
        x = w / 2
        if self._icon:
            probe_w = self._text_width()
            total = isz + 8 * u + probe_w
            left = (w - total) / 2
            draw_icon(self, self._icon, left + isz / 2, h / 2, isz, fg)
            x = left + isz + 8 * u + probe_w / 2
        self.create_text(x, h / 2, text=self._text, fill=fg, font=self._font)

    def _text_width(self):
        tid = self.create_text(0, 0, text=self._text, font=self._font)
        x0, _y0, x1, _y1 = self.bbox(tid)
        self.delete(tid)
        return x1 - x0


class ToggleChip(RoundButton):
    """켜고 끄는 알약 모양 칩 — variable(BooleanVar)과 연결. tone="accent"|"loop"."""

    def __init__(self, parent, text, variable, command=None, tone="accent", **kw):
        self._var = variable
        self._tone = tone
        self._user_command = command
        super().__init__(parent, text=text, command=self._toggle, height=40, radius=20, kind="outline", **kw)
        variable.trace_add("write", lambda *_: self._draw())

    def _toggle(self):
        self._var.set(not self._var.get())
        if self._user_command:
            self._user_command()

    def _colors(self):
        on = bool(self._var.get())
        if not on:
            return ("#F6F3EE" if self._hover else WHITE), "#D3CCBF", TEXT
        if self._tone == "loop":
            return LOOP_SOFT, LOOP, LOOP_TEXT
        return ACCENT_SOFT, ACCENT, ACCENT

    def set_text(self, text):
        """글자가 길어지면 폭도 맞춘다."""
        self._text = text
        need = int(self._text_width() + 32 * self.u)
        if need > int(self["width"]):
            self.configure(width=need)
        self._draw()


# ---------------------------------------------------------------- 재생 진행 막대

class Timeline(tk.Canvas):
    """재생 진행 막대 — 진행 부분(청록), 반복 구간(주황 테두리), 손잡이.
    합성 중이면 진행률을 대신 보여 준다. 누르거나 끌면 on_seek(0~1 비율)을 부르고, 끄는 동안
    tip_text(비율) 글자를 손잡이 위 말풍선으로 보여 준다(사용자 요청 2026-10-13: 움직이는 마디를 알려줌)."""

    def __init__(self, parent, on_seek, height=30, bg=PANEL, tip_text=None, mark_at=None):
        self.u = ui_scale(parent)
        super().__init__(parent, height=int(height * self.u), bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._on_seek = on_seek
        self._tip_text = tip_text
        self._mark_at = mark_at      # 비율 → 그 위치가 20·40·60… 마디 앞이면 True(끄는 동안 손잡이를 주황으로)
        self._dragging = False
        self._tip = None
        self._tip_label = None
        self._pos = 0.0
        self._loop = None
        self._synth = None
        self._enabled = False
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._seek)
        self.bind("<B1-Motion>", self._seek)
        self.bind("<ButtonRelease-1>", self._release)

    def _frac(self, e):
        pad = 10 * self.u
        return min(1.0, max(0.0, (e.x - pad) / max(1, self.winfo_width() - 2 * pad)))

    def _seek(self, e):
        if not self._enabled or self._synth is not None:
            return
        frac = self._frac(e)
        self._dragging = True
        self._on_seek(frac)
        self._show_tip(frac)

    def _show_tip(self, frac):
        text = self._tip_text(frac) if self._tip_text else ""
        if not text:
            return
        if self._tip is None:
            self._tip = tk.Toplevel(self)
            self._tip.overrideredirect(True)
            self._tip.attributes("-topmost", True)
            self._tip.configure(bg=DARK)
            self._tip_label = tk.Label(self._tip, bg=DARK, fg=WHITE, font=(FONT, 12, "bold"),
                                       padx=int(10 * self.u), pady=int(4 * self.u))
            self._tip_label.pack()
        self._tip_label.config(text=text)
        self._tip.update_idletasks()
        pad = 10 * self.u
        x = self.winfo_rootx() + pad + (self.winfo_width() - 2 * pad) * frac - self._tip.winfo_reqwidth() / 2
        y = self.winfo_rooty() - self._tip.winfo_reqheight() - 6 * self.u
        self._tip.geometry(f"+{int(x)}+{int(y)}")

    def _release(self, _e):
        self._dragging = False
        self._hide_tip()
        self._draw()

    def _hide_tip(self):
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def set_state(self, pos=None, loop="keep", synth="keep", enabled=None):
        changed = False
        if pos is not None and abs(pos - self._pos) > 1e-4:
            self._pos = pos
            changed = True
        if loop != "keep" and loop != self._loop:
            self._loop = loop
            changed = True
        if synth != "keep" and synth != self._synth:
            self._synth = synth
            changed = True
        if enabled is not None and enabled != self._enabled:
            self._enabled = enabled
            changed = True
        if changed:
            self._draw()

    def _draw(self):
        self.delete("all")
        u = self.u
        w, h = self.winfo_width(), self.winfo_height()
        pad = 10 * u
        x0, x1 = pad, w - pad
        cy = h / 2
        th = 8 * u
        round_rect(self, x0, cy - th / 2, x1, cy + th / 2, th / 2, fill=TRACK, outline="")
        if self._synth is not None:
            xs = x0 + (x1 - x0) * self._synth
            if xs > x0 + 2:
                round_rect(self, x0, cy - th / 2, xs, cy + th / 2, th / 2, fill=LOOP, outline="")
            return
        if self._loop:
            a, b = self._loop
            round_rect(self, x0 + (x1 - x0) * a, cy - th / 2 - 5 * u, x0 + (x1 - x0) * b, cy + th / 2 + 5 * u,
                       5 * u, fill="", outline=LOOP, width=2 * u)
        xp = x0 + (x1 - x0) * self._pos
        if xp > x0 + 2:
            round_rect(self, x0, cy - th / 2, xp, cy + th / 2, th / 2, fill=ACCENT, outline="")
        if self._enabled:
            r = 9 * u
            red = self._dragging and self._mark_at is not None and self._mark_at(self._pos)
            self.create_oval(xp - r, cy - r, xp + r, cy + r, fill=LOOP if red else WHITE,
                             outline=LOOP if red else ACCENT, width=2 * u)


class CycleChip(RoundButton):
    """누를 때마다 상태가 차례로 바뀌는 칩 — states=[(값, 글자), ...], 첫 상태가 "꺼짐"(흰 바탕),
    나머지는 켜짐 색. 오른쪽 클릭하면 상태 목록 메뉴. variable은 StringVar."""

    def __init__(self, parent, states, variable, command=None, **kw):
        self._states = states
        self._var = variable
        self._user_command = command
        longest = max((t for _v, t in states), key=len)
        height = kw.pop("height", 40)
        super().__init__(parent, text=longest, command=self._next, height=height, radius=height // 2,
                         kind="outline", **kw)
        variable.trace_add("write", lambda *_: self._sync())
        self.bind("<Button-3>", self._menu)
        self._sync()

    def _label(self):
        return dict(self._states).get(self._var.get(), self._states[0][1])

    def _sync(self):
        self._text = self._label()
        self._draw()

    def _set(self, value):
        self._var.set(value)
        if self._user_command:
            self._user_command()

    def _next(self):
        values = [v for v, _t in self._states]
        cur = self._var.get()
        i = values.index(cur) if cur in values else 0
        self._set(values[(i + 1) % len(values)])

    def _menu(self, e):
        m = tk.Menu(self, tearoff=0, font=(FONT, 10))
        for v, t in self._states:
            m.add_radiobutton(label=t, value=v, variable=self._var, command=lambda v=v: self._set(v))
        m.tk_popup(e.x_root, e.y_root)

    def _colors(self):
        on = self._var.get() != self._states[0][0]
        if not on:
            return ("#F6F3EE" if self._hover else WHITE), "#D3CCBF", TEXT
        return ACCENT_SOFT, ACCENT, ACCENT
