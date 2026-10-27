"""
중주연습 — 메인 창(Tkinter UI). (2026-10-08에 "악보 소리 생성"에서 이름을 바꿈)

MusicXML 악보를 열면 (1) 악보를 쪽 단위로 보여주고(score_view), (2) 음표대로 소리를
합성해(render) 재생·저장한다. 재생 중엔 지금 연주 중인 마디를 악보 위에 강조하고,
마디를 클릭하면 거기서부터 재생한다. 악보는 두 쪽을 나란히, 창 높이에 맞춰 보여준다.
마디 구간 반복·빠르기 조절·메트로놈·예비박을 지원한다. (성부 믹서 패널은 사용자 요청으로
뺐다(2026-10-07). 성부 켜고 끄기만 머리글의 성부 칩으로 다시 넣었다(2026-10-08, 구간 반복 때 자기
음만 듣기 등) — 명령줄 --mute/--solo도 그대로.)

무거운 일(악보 읽기·조판·합성)은 작업 스레드에서 하고, 결과는 큐(_ui_q)로 받아 UI
스레드의 주기 함수(_tick)가 처리한다 — Tkinter는 다른 스레드에서 위젯을 만지면 안 된다.

실행:
    python app.py   또는   python E:\\project\\guitar\\GenerateScore
"""

import datetime
import json
import multiprocessing
import os
import queue
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont

from PIL import Image, ImageChops, ImageDraw, ImageTk

from audio_player import Player, output_device_name, refresh_devices
import musicxml_reader
from musicxml_reader import nominal_seconds, read_musicxml
from render import (COMPOUND_DOTTED_QUARTER, COMPOUND_EIGHTH, LivePlayback, PartSetting, RenderedScore, SegmentRender,
                    click_unit_q, count_in, default_settings, is_compound, iter_mix, write_wav)
from synth import NoteSynth
import harmonize
import round_song
import score_import
from help_text import DEFAULT_HELP
from score_view import MEASURE_MARK_EVERY, layout_systems_file, png_to_image, rasterize_svg
import ui_widgets as W
from ui_widgets import ui_scale


def _enable_dpi_awareness():
    """Windows 화면 배율(125%·150% 등)에서 창 전체가 비트맵으로 늘려져 흐릿해지는 걸
    막는다 — Tk 창을 만들기 전에 불러야 한다. 악보 이미지도 실제 픽셀로 그려져 선명해짐."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


# ---------------------------------------------------------------- 재생 중 꺼짐 방지
# ExtractAudio/app.py에서 가져옴(2026-10-07). 재생하는 동안 화면·시스템이 절전으로 넘어가지
# 않게 하고, 배터리로 도는 중 남은 양이 이보다 적으면 막지 않는다. Windows SetThreadExecutionState는
# 프로세스가 끝나면 Windows가 원래대로 되돌린다.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002
KEEP_AWAKE_MIN_BATTERY_PERCENT = 20


def _battery_ok_for_keep_awake(min_percent=KEEP_AWAKE_MIN_BATTERY_PERCENT):
    """전원에 연결돼 있거나 배터리가 충분하면 True. 배터리가 없거나 알 수 없으면 True."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        class _PowerStatus(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        st = _PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.pointer(st)):
            return True
        if st.ACLineStatus == 1:
            return True
        pct = st.BatteryLifePercent & 0xFF
        return pct == 255 or pct >= min_percent
    except Exception:
        return True


def _set_keep_awake(enabled):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        if enabled and _battery_ok_for_keep_awake():
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED)
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass


def _user_data_dir():
    """기록·설정 파일을 둘 폴더 — exe(또는 이 파일) 바로 옆(ExtractAudio와 같은 방식).
    Mac .app은 실행 파일이 번들 안(Contents/MacOS)이라 거기 쓰면 서명이 깨지고, 내려받은 앱은 읽기 전용
    위치에서 돌 수 있어 ~/Library/Application Support/중주연습에 둔다(2026-10-27)."""
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            d = os.path.join(os.path.expanduser("~/Library/Application Support"), "중주연습")
            os.makedirs(d, exist_ok=True)
            return d
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_NAME = "중주연습"
APP_TITLE = "중주연습 1.3"
HISTORY_PATH = os.path.join(_user_data_dir(), "generate_history.json")
SETTINGS_PATH = os.path.join(_user_data_dir(), "generate_settings.json")
HELP_TAB_CHARS = 4   # 도움말에서 줄이 넘어갈 때 들여 쓰는 글자 수
HELP_PATH = os.path.join(_user_data_dir(), "generate_help.txt")   # 사용자가 고쳐 저장한 "?" 도움말(없으면 기본 글)
ERROR_LOG_PATH = os.path.join(_user_data_dir(), "generate_error.log")
HISTORY_MAX = 10
LOOP_HISTORY_MAX = 10   # 곡마다 저장해 두는 반복 구간 수("저장" 버튼)
LOOP_BLOCK_MEASURES = 20   # "구간" 방식에서 시작·끝을 고르는 마디 단위
COUNTIN_MAX_MEASURES = 2   # 예비박 최대 마디 수(설정 0~2)

TICK_MS = 30                # 재생 위치 갱신·작업 결과 처리 주기
TEMPO_DEBOUNCE_MS = 600     # 빠르기 숫자를 바꾸고 이만큼 멈추면 다시 합성(재생 중이면 멈춘 뒤)
TEMPO_STEP = 10             # 빠르기 −/+ 한 번 = 10%(사용자 요청 2026-10-27, 예전 5%)
TEMPO_MIN, TEMPO_MAX = 30, 200
RESIZE_DEBOUNCE_MS = 250
SAVE_DEBOUNCE_MS = 1000
CLICK_VOLUME = 0.9          # 메트로놈 딸깍 음량(믹스 기준) — 0.45는 음악에 묻혔다(2026-10-07)
RASTER_WORKERS = 3          # 악보 쪽 그림 변환 프로세스 수
PAGE_GAP = 16               # 나란히 놓은 두 쪽 사이 간격(px)
DRAG_MIN_PX = 8             # 이만큼 넘게 움직여야 "끌기"(반복 구간 고르기)로 친다
BLUETOOTH_DELAY_MS = 200    # 블루투스 출력일 때 소리 지연 보정 처음 값(사용자가 ⚙에서 조정)
# 소리 지연 보정 범위 — 음수면 커서를 앞당긴다(WASAPI에서 커서가 소리보다 늦게 따라온다는 지적, 2026-10-07)
DELAY_MIN_MS = -300
DELAY_MAX_MS = 1000
SCORE_MARGIN = 12           # 악보 이미지 둘레 여백(px)

CURSOR_COLOR = "#1F5F66"    # 현재 마디 상자(시안 A 청록 — ui_widgets.ACCENT)
PLAYHEAD_COLOR = "#E0201B"  # 마디 안 재생 위치 세로선(선명한 빨강)
PLAYHEAD_WIDTH = 5          # 재생선 굵기(px) — 흰 테두리가 양쪽에 2px씩 더 붙음
LOOP_COLOR = "#D98E2E"      # 반복 구간 마디 표시(시안 A 주황 — 진행 막대·칩과 같은 색)



# ---------------------------------------------------------------- 기록·설정 파일

def _norm(path):
    return os.path.normcase(os.path.normpath(path))



def _strong_color(color):
    """옅은 바탕색 → 같은 색 계열의 진한 색(재생 커서용)."""
    import colorsys
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, s, _v = colorsys.rgb_to_hsv(r, g, b)
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * 3.2), 0.72 if _v > 0.9 else _v * 0.7)
    return "#%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255))


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 기록 저장은 편의 기능 — 실패해도 앱 동작엔 영향 없게


def _log_error(what, err):
    """오류 자세한 내용(traceback)을 exe 옆 로그에 남긴다 — 창(windowed) 빌드는 콘솔이
    없어서 대화상자 한 줄 말고는 원인을 볼 길이 없다."""
    import datetime
    import traceback
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {what}\n")
            f.write("".join(traceback.format_exception(err)) + "\n")
    except Exception:
        pass


def _fmt_when(iso):
    """'2026-10-07T14:05' → '오늘 14:05' / '어제' / '10월 4일'."""
    if not iso:
        return ""
    try:
        t = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    today = datetime.date.today()
    if t.date() == today:
        return f"오늘 {t:%H:%M}"
    if (today - t.date()).days == 1:
        return "어제"
    return f"{t.month}월 {t.day}일"


def _fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class GenerateApp(tk.Tk):
    def __init__(self):
        _enable_dpi_awareness()
        super().__init__()
        self.title(APP_TITLE)
        self._set_window_icon()
        scale = self.winfo_fpixels("1i") / 96.0  # 화면 배율(1.0 = 100%)
        # 화면보다 크면 아래 상태 표시줄이 작업 표시줄에 가려지므로 화면의 90%까지만
        w = min(int(1280 * scale), int(self.winfo_screenwidth() * 0.9))
        h = min(int(860 * scale), int(self.winfo_screenheight() * 0.85))
        self.geometry(f"{w}x{h}+20+20")
        self.minsize(900, 600)
        self.option_add("*Font", ("Malgun Gothic", 10))

        self._ui_q = queue.Queue()
        self.player = Player()

        # 곡 상태
        self.path = None
        self.score = None
        self.renderer = None      # score_view.LaidOutScore(줄 단위 조판 결과)
        self.boxes = []           # 인쇄 마디 순서대로 (쪽, x0, y0, x1, y1)
        self._chord_map = None    # 화성을 붙여 만든 악보면 마디별 [(시작비율, 끝비율, 색, 코드)] — 바탕색용
        self._painted = set()     # 재생 커서가 지나가 색이 칠해진 (마디 인덱스, 코드 구간 번호)
        self._round_map = None    # 돌림노래 — {마디 인덱스: [(첫 보표, 보표 수, 색, 성부 이름)]}, 성부가 노래를 시작하는 마디
        self._round_painted = set()   # 커서가 닿아 색이 칠해진 돌림노래 시작 마디 인덱스
        self._color_all = False       # 멜로디에 색깔 넣기로 만든 악보 — 재생 커서와 상관없이 처음부터 색
        self._round_entries = []      # 돌림노래 + 코드 색칠: [(첫 보표, 보표 수, 시작 마디 인덱스, 이름)] — 그 마디부터 그 성부 보표만 칠함
        self._sys_items = {}      # 줄 번호 → (캔버스 그림 항목, 줄 위쪽 px, 줄 높이 px, self._photos 위치)
        self._last_cursor_sec = 0.0
        self.rendered = None      # RenderedScore(합성 결과)
        self.settings = []        # 성부별 PartSetting — 믹서가 직접 고친다(재생 중에도 바로 반영)
        self.cursor_nominal = 0.0 # 재생 시작 위치(악보 빠르기 기준 초 — 빠르기 배율과 무관)
        self._render_gen = 0      # 합성 작업 번호 — 새 작업이 시작되면 이전 것은 버린다
        self._resume_after_render = False

        # 재생 상태
        self.playing = False
        self._live = None              # 구간 반복이 아닌 재생의 LivePlayback(재생 중 빠르기 바꾸기)
        self._play_start_sec = 0.0
        self._play_start_nominal = 0.0
        self._prefix_sec = 0.0
        self._loop_secs = None

        # 악보 표시 상태
        self.page = 1
        # 쪽 그림: SVG→PNG 변환은 별도 프로세스에서(score_view.rasterize_svg 주석 — 같은
        # 프로세스에서 하면 GIL 때문에 쪽 넘길 때 소리가 찌그러졌다). 곡을 열면 모든 쪽을
        # 지금 창 너비로 미리 그려 둬서 재생 중 쪽 넘김은 다 그린 PNG를 펼치기만 한다.
        self._pool = None
        self._png_cache = {}      # (쪽, 너비) -> PNG 바이트
        self._png_pending = set()
        self._score_gen = 0       # 곡을 새로 열 때마다 +1 — 이전 곡의 그림 결과는 버림
        self._photos = []
        self._sysset = None       # score_view.SystemSet(곡마다 한 번 줄 단위 조판)
        self._shown_measure = None

        self._after_ids = {}
        self.history = [p for p in _load_json(HISTORY_PATH, []) if isinstance(p, str)]
        self.all_settings = _load_json(SETTINGS_PATH, {})

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(TICK_MS, self._tick)
        self.after(150, self._show_start_screen)

    # ------------------------------------------------------------ UI 구성

    def _set_window_icon(self):
        """창·작업 표시줄 아이콘(assets/ensemble.png — make_icon.py로 만듦). exe(PyInstaller)에선 _internal 안."""
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "assets", "ensemble.png")
        try:
            self._icon_img = tk.PhotoImage(file=path)
            self.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _build_ui(self):
        """시안 A "연습 노트"(사용자 선택, 2026-10-07): 위 머리글(곡 제목·정보·쪽 넘김·열기/최근/
        저장), 가운데 따뜻한 회색 바탕의 종이 두 쪽, 아래 재생 막대(진행 막대·둥근 재생 버튼·
        빠르기·토글 칩·지연 보정). 둥근 위젯은 ui_widgets.py."""
        u = ui_scale(self)
        self.configure(bg=W.PANEL)
        style = ttk.Style(self)
        style.configure("Muted.TLabel", foreground=W.MUTED)

        # ---------------- 머리글
        head = tk.Frame(self, bg=W.PANEL, height=int(68 * u))
        head.pack(side="top", fill="x")
        self._head = head
        head.pack_propagate(False)
        # 머리글 로고 = 앱 아이콘(assets/ensemble.png)을 줄여서. 파일이 없으면 예전처럼 그려서.
        logo = tk.Canvas(head, width=int(40 * u), height=int(40 * u), bg=W.PANEL, highlightthickness=0)
        try:
            from PIL import Image
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            im = Image.open(os.path.join(base, "assets", "ensemble.png")).convert("RGBA")
            self._logo_img = ImageTk.PhotoImage(im.resize((int(40 * u), int(40 * u)), Image.LANCZOS))
            logo.create_image(0, 0, image=self._logo_img, anchor="nw")
        except Exception:
            W.round_rect(logo, 0, 0, int(36 * u) - 1, int(36 * u) - 1, 10 * u, fill=W.ACCENT, outline="")
            W.draw_icon(logo, "note", 18 * u, 18 * u, 20 * u, W.WHITE)
        logo.pack(side="left", padx=(int(24 * u), int(12 * u)))
        right = tk.Frame(head, bg=W.PANEL)
        right.pack(side="right", padx=int(24 * u))
        # 사용 안내 "?" — 원 없이 글자만, 머리글 맨 오른쪽(사용자 요청 2026-10-14, 나머지 버튼이 왼쪽으로 한 칸씩 밀림)
        W.RoundButton(right, text="?", width=32, height=40, kind="ghost", font_size=16,
                      command=self._show_help).pack(side="right", padx=(int(4 * u), 0))
        self.save_btn = W.RoundButton(right, text="WAV·MP3 저장", icon="download", kind="solid", color=W.DARK,
                                      command=self._save_wav)
        self.save_btn.pack(side="right", padx=(int(10 * u), 0))
        # 성부 칩(사용자 요청 2026-10-08: 아래 재생 막대 칸이 모자라 "최근 악보" 버튼을 빼고 그 자리에 —
        # 최근 악보는 시작 화면에 이미 나온다). 곡을 열 때 _build_part_chips가 채운다.
        self.parts_bar = tk.Frame(right, bg=W.PANEL)
        self.parts_bar.pack(side="right", padx=(int(10 * u), 0))
        # 악보 열기 바로 오른쪽 ▼ — 누르면 최근 악보 목록(사용자 요청 2026-10-08)
        self.history_btn = W.RoundButton(right, icon="tri_down", width=30, color="#1A1A1A",
                                         command=self._show_recent_menu)
        self.history_btn.pack(side="right", padx=(int(4 * u), 0))
        W.RoundButton(right, text="악보 열기", icon="folder", command=self._ask_open).pack(side="right")
        W.RoundButton(right, icon="expand", circle=True, kind="ghost",
                      command=self.toggle_fullscreen).pack(side="right", padx=(0, int(6 * u)))  # 전체화면(F11)

        nav = tk.Frame(head, bg=W.PANEL)
        nav.pack(side="right", padx=int(20 * u))
        W.RoundButton(nav, icon="left", circle=True, height=36, kind="ghost",
                      command=lambda: self.show_page(self.page - 2)).pack(side="left")
        self.page_var = tk.StringVar(value="")
        tk.Label(nav, textvariable=self.page_var, bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 10),
                 width=12).pack(side="left")
        W.RoundButton(nav, icon="right", circle=True, height=36, kind="ghost",
                      command=lambda: self.show_page(self.page + 2)).pack(side="left")
        self.follow_var = tk.BooleanVar(value=True)
        W.ToggleChip(nav, "따라가기", self.follow_var, bg=W.PANEL).pack(side="left", padx=(int(10 * u), 0))
        # 제목·곡 정보는 버튼·쪽 넘김이 자리를 잡은 뒤 남는 자리에(길면 잘림) — 먼저 pack하면 긴 안내
        # 문구가 자리를 다 차지해 쪽 넘김이 밀려났다(2026-10-07)
        titles = tk.Frame(head, bg=W.PANEL)
        titles.pack(side="left", fill="both", expand=True, pady=int(12 * u))
        self.title_var = tk.StringVar(value=APP_NAME)
        tk.Label(titles, textvariable=self.title_var, bg=W.PANEL, fg=W.TEXT,
                 font=(W.TITLE_FONT, 15, "bold"), anchor="w").pack(anchor="w")
        self.status_var = tk.StringVar(value="악보(MusicXML)를 열어 주세요.")
        tk.Label(titles, textvariable=self.status_var, bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9),
                 anchor="w").pack(anchor="w")

        self._head_sep = tk.Frame(self, bg=W.BORDER, height=1)
        self._head_sep.pack(side="top", fill="x")

        # ---------------- 재생 막대(아래)
        foot = tk.Frame(self, bg=W.PANEL)
        foot.pack(side="bottom", fill="x")
        self._foot = foot
        self._foot_sep = tk.Frame(self, bg=W.BORDER, height=1)
        self._foot_sep.pack(side="bottom", fill="x")
        row1 = tk.Frame(foot, bg=W.PANEL)
        row1.pack(side="top", fill="x", padx=int(28 * u), pady=(int(12 * u), 0))
        self.time_var = tk.StringVar(value="0:00")
        self.total_var = tk.StringVar(value="0:00")
        tk.Label(row1, textvariable=self.time_var, bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9),
                 width=6, anchor="w").pack(side="left")
        tk.Label(row1, textvariable=self.total_var, bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9),
                 width=6, anchor="e").pack(side="right")
        self.timeline = W.Timeline(row1, on_seek=self._on_timeline_seek, tip_text=self._timeline_tip,
                                 mark_at=self._timeline_is_mark)
        self.timeline.pack(side="left", fill="x", expand=True)

        row2 = tk.Frame(foot, bg=W.PANEL)
        row2.pack(side="top", fill="x", padx=int(28 * u), pady=(int(8 * u), int(14 * u)))
        self.home_btn = W.RoundButton(row2, icon="home", circle=True, height=44, command=self.go_home)
        self.home_btn.pack(side="left")
        self.play_btn = W.RoundButton(row2, icon="play", circle=True, height=52, kind="solid",
                                      command=self.toggle_play, icon_size=20)
        self.play_btn.pack(side="left", padx=(int(12 * u), int(14 * u)))
        mbox = tk.Frame(row2, bg=W.PANEL)
        mbox.pack(side="left")
        self.measure_var = tk.StringVar(value="마디 -")
        self.occ_var = tk.StringVar(value="")
        tk.Label(mbox, textvariable=self.measure_var, bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 13, "bold"),
                 width=8, anchor="w").pack(anchor="w")
        tk.Label(mbox, textvariable=self.occ_var, bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9),
                 anchor="w").pack(anchor="w")
        tk.Frame(row2, bg=W.BORDER, width=1, height=int(36 * u)).pack(side="left", padx=int(14 * u))

        pill = tk.Frame(row2, bg=W.PILL)
        pill.pack(side="left")
        # 구간 반복이 켜져 있으면 이 알약은 "구간 빠르기"(곡 빠르기와 따로, 구간만 합성해 바로 반영)
        self.tempo_label_var = tk.StringVar(value="빠르기")
        tk.Label(pill, textvariable=self.tempo_label_var, bg=W.PILL, fg=W.MUTED, font=(W.FONT, 10),
                 width=10, anchor="e").pack(side="left", padx=(int(6 * u), int(4 * u)))
        self.tempo_var = tk.IntVar(value=100)
        self.loop_tempo_var = tk.IntVar(value=100)
        W.RoundButton(pill, text="−", width=32, height=32, radius=8, kind="outline", bg=W.PILL, font_size=12,
                      command=lambda: self._step_tempo(-TEMPO_STEP)).pack(side="left", pady=int(4 * u))
        self.tempo_text = tk.StringVar(value="100%")
        tk.Label(pill, textvariable=self.tempo_text, bg=W.PILL, fg=W.TEXT, font=(W.FONT, 11, "bold"),
                 width=5).pack(side="left")
        W.RoundButton(pill, text="+", width=32, height=32, radius=8, kind="outline", bg=W.PILL, font_size=12,
                      command=lambda: self._step_tempo(TEMPO_STEP)).pack(side="left")
        self.bpm_var = tk.StringVar(value="")
        tk.Label(pill, textvariable=self.bpm_var, bg=W.PILL, fg=W.MUTED, font=(W.FONT, 9),
                 width=8).pack(side="left", padx=(int(4 * u), int(8 * u)))
        self.tempo_var.trace_add("write", lambda *_: self._refresh_tempo_pill())
        self.loop_tempo_var.trace_add("write", lambda *_: self._refresh_tempo_pill())
        for wdg in (pill,) + tuple(pill.winfo_children()):
            wdg.bind("<MouseWheel>", lambda e: self._step_tempo(TEMPO_STEP if e.delta > 0 else -TEMPO_STEP))

        # 메트로놈 세 상태: off(음악만) / with(음악 + 메트로놈) / only(메트로놈만) — 사용자 요청 2026-10-07
        self.metro_var = tk.StringVar(value="off")
        # 예비박 마디 수 0~2(⚙ 설정에서 고름, 0이면 없음) — 재생: 그냥 재생할 때 시작 전, 반복: 구간 반복 매 바퀴 전
        self.countin_var = tk.IntVar(value=1)
        self.countin_loop_var = tk.IntVar(value=1)
        self.loop_var = tk.BooleanVar(value=False)
        self.loop_a_var = tk.StringVar()
        self.loop_b_var = tk.StringVar()
        self._measure_numbers = []
        # 메트로놈: 켜면 음악을 끄고 메트로놈만(구간 반복도), 끄면 음악 — 재생 중에도 바로 바뀜
        # (사용자 요청 2026-10-07). 예비박은 그냥 재생용과 구간 반복용을 따로.
        W.CycleChip(row2, [("off", "메트로놈 끔"), ("with", "메트로놈 + 음악"), ("only", "메트로놈만")],
                    self.metro_var, command=self._schedule_save).pack(side="left", padx=(int(14 * u), int(8 * u)))
        # 6/8·9/8·12/8 메트로놈 박 단위(점4분음표 "dq" / 8분음표 "e") — ⚙ 설정 창에서 고른다(사용자 요청 2026-10-13)
        self.metro_unit_var = tk.StringVar(value="dq")
        self.loop_chip = W.ToggleChip(row2, "구간 반복", self.loop_var, command=self._on_loop_chip, tone="loop")
        self.loop_chip.bind("<Button-3>", self._edit_loop_range)
        # 구간 지정 방식 토글: 마디(스핀박스로 한 마디씩) ↔ 구간(20마디 단위, 사용자 요청 2026-10-13)
        self.loop_mode_var = tk.StringVar(value="measure")
        W.CycleChip(row2, [("measure", "마디"), ("block", "구간")], self.loop_mode_var, height=30, font_size=9,
                    command=self._on_loop_mode).pack(side="left", padx=(int(14 * u), int(6 * u)))
        holder = tk.Frame(row2, bg=W.PANEL)
        holder.pack(side="left", padx=(0, int(12 * u)))
        self._loop_holder = holder
        # 반복 구간 시작·끝 마디 — 화살표·휠로 한 마디씩, 숫자 입력 후 Enter. 숫자를 바꿔도 구간 반복은
        # 저절로 켜지지 않는다(사용자 요청 2026-10-13: 숫자를 맞춘 뒤 "구간 반복"을 눌러야 시작).
        loopbox = tk.Frame(holder, bg=W.PANEL)
        loopbox.pack(side="left")
        self._loopbox = loopbox
        self.loop_a_spin = self._loop_spin(loopbox, self.loop_a_var)
        self.loop_a_spin.pack(side="left")
        tk.Label(loopbox, text="~", bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 11)).pack(side="left", padx=int(4 * u))
        self.loop_b_spin = self._loop_spin(loopbox, self.loop_b_var)
        self.loop_b_spin.pack(side="left")
        tk.Label(loopbox, text="마디", bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 10)).pack(side="left",
                                                                                padx=(int(4 * u), 0))
        # "구간" 방식: 시작(0, 20, 40…)을 고르면 그 뒤의 끝 값들이 애니메이션으로 나타나는 선택 창
        blockbox = tk.Frame(holder, bg=W.PANEL)
        self._blockbox = blockbox
        self.block_a_btn = W.RoundButton(blockbox, text="0마디 ▾", width=84, command=self._show_block_picker)
        self.block_a_btn.pack(side="left")
        tk.Label(blockbox, text="~", bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 11)).pack(side="left", padx=int(4 * u))
        self.block_b_btn = W.RoundButton(blockbox, text="20마디 ▾", width=84, command=self._show_block_picker)
        self.block_b_btn.pack(side="left")
        self.loop_a_var.trace_add("write", lambda *_: self._sync_block_buttons())
        self.loop_b_var.trace_add("write", lambda *_: self._sync_block_buttons())
        # 저장(사용자 요청 2026-10-13): 지금 구간을 저장 목록(최대 10개)에 넣고, 히스토리에서 고르거나 하나씩 지움
        self.save_loop_btn = W.RoundButton(row2, text="저장", height=40, command=self._save_preset)
        self.save_loop_btn.pack(side="left", padx=(0, int(6 * u)))
        self.loop_chip.pack(side="left", padx=(0, int(6 * u)), before=self.save_loop_btn)  # 저장 버튼 바로 앞(사용자 요청 2026-10-13)
        self.presets_btn = W.RoundButton(row2, text="히스토리 ▾", height=40, command=self._show_presets_menu)
        self.presets_btn.pack(side="left", padx=(0, int(12 * u)))
        self.loop_history = []  # 저장한 반복 구간 [{"a", "b"}] 최신순, 최대 LOOP_HISTORY_MAX — 곡별 저장 키 "loop_history"

        # 소리 지연 보정 등 자주 안 바꾸는 설정은 ⚙ 안으로(재생 막대 폭이 모자랐다, 2026-10-07)
        self.delay_var = tk.IntVar(value=self._load_device_delay())
        self.settings_btn = W.RoundButton(row2, icon="gear", circle=True, height=40, command=self._show_settings)
        self.settings_btn.pack(side="right")
        self.part_vars = []  # 성부별 BooleanVar(켜짐 = 소리 남) — 머리글 성부 칩과 연결

        # 화면에 안 보이지만 시험·호환용으로 남겨 둔 위치 문자열(예: "마디 31   1:50 / 6:21")
        self.pos_var = tk.StringVar(value="")

        # ---------------- 악보(두 쪽 나란히)
        self.canvas = tk.Canvas(self, background=W.BG, highlightthickness=0)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._debounce("resize", RESIZE_DEBOUNCE_MS, self._on_resize))
        # 두 쪽이 다 보이므로 스크롤 대신 마우스 휠로 쪽 넘김
        self.canvas.bind("<MouseWheel>", self._on_canvas_wheel)
        # 누르기: 그 마디부터 재생 / 누른 채 끌기: 끈 범위를 반복 구간으로(사용자 요청 2026-10-07)
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Button-3>", self._on_canvas_menu)
        self.bind("<space>", lambda e: self._key_play(e))
        self.bind("<Prior>", lambda e: self.show_page(self.page - 2))
        self.bind("<Next>", lambda e: self.show_page(self.page + 2))
        self.bind("<Home>", lambda e: self.go_home())
        self.bind("<F11>", lambda e: self.toggle_fullscreen())
        # 들으면서 커서 맞추기: [ 커서 10ms 빠르게, ] 10ms 느리게(장치별 저장)
        self.bind("<bracketleft>", lambda e: self._nudge_delay(-10, e))
        self.bind("<bracketright>", lambda e: self._nudge_delay(10, e))
        self.bind("<Escape>", lambda e: self.toggle_fullscreen(False))
        self._fullscreen = False
        self._set_controls(False)

    def toggle_fullscreen(self, on=None):
        """전체화면: 머리글·재생 막대를 숨기고 화면 전체에 악보만(사용자 요청 2026-10-07). Esc·F11로
        원래대로. 캔버스 크기가 바뀌므로 _on_resize가 쪽 배치를 새 크기에 맞춘다."""
        on = (not self._fullscreen) if on is None else on
        if on == self._fullscreen:
            return
        self._fullscreen = on
        if on:
            for w in (self._head, self._head_sep, self._foot, self._foot_sep):
                w.pack_forget()
            self.attributes("-fullscreen", True)
            self.after(800, self._show_fullscreen_hint)  # 크기 변경 후 다시 그리기(캔버스 지움)가 끝난 뒤에
        else:
            self.attributes("-fullscreen", False)
            self._head.pack(side="top", fill="x", before=self.canvas)
            self._head_sep.pack(side="top", fill="x", before=self.canvas)
            self._foot.pack(side="bottom", fill="x", before=self.canvas)
            self._foot_sep.pack(side="bottom", fill="x", before=self.canvas)
            self.canvas.delete("fshint")

    def _show_fullscreen_hint(self):
        """전체화면에 들어가면 잠깐 안내를 띄운다."""
        c = self.canvas
        c.delete("fshint")
        u = ui_scale(self)
        x, y = c.winfo_width() / 2, int(28 * u)
        text = "Esc: 전체화면 나가기 · 스페이스: 재생/멈춤 · 휠·PageUp/Down: 쪽 넘김"
        tid = c.create_text(x, y, text=text, fill=W.WHITE, font=(W.FONT, 10), tags="fshint")
        x0, y0, x1, y1 = c.bbox(tid)
        bg = W.round_rect(c, x0 - 16 * u, y0 - 8 * u, x1 + 16 * u, y1 + 8 * u, 14 * u, fill=W.DARK, outline="",
                          tags="fshint")
        c.tag_raise(tid, bg)
        self.after(2500, lambda: c.delete("fshint"))

    def _loop_spin(self, parent, var):
        sp = ttk.Spinbox(parent, textvariable=var, values=self._measure_numbers or [""], width=4,
                         justify="center", font=(W.FONT, 12, "bold"), wrap=False,
                         command=self._on_loop_spin)
        sp.bind("<Return>", lambda e: self._on_loop_spin())
        sp.bind("<FocusOut>", lambda e: self._on_loop_spin())
        sp.bind("<MouseWheel>", lambda e, w=sp: (w.event_generate("<<Increment>>" if e.delta > 0 else "<<Decrement>>"),
                                                 self._on_loop_spin(), "break")[-1])
        return sp

    def _on_loop_spin(self):
        """스핀박스로 시작·끝 마디를 바꿈 — 없는 번호는 되돌리고, 시작>끝이면 맞바꾼다. 구간 반복은 켜지 않는다."""
        nums = self._measure_numbers
        if not nums:
            return
        a, b = self.loop_a_var.get().strip(), self.loop_b_var.get().strip()
        prev = getattr(self, "_loop_prev", (nums[0], nums[min(len(nums) - 1, 7)]))
        if a not in nums:
            a = prev[0]
        if b not in nums:
            b = prev[1]
        if nums.index(a) > nums.index(b):
            a, b = b, a
        self.loop_a_var.set(a)
        self.loop_b_var.set(b)
        if (a, b) == prev:
            return  # 그대로면(포커스만 빠져나감 등) 아무것도 안 함 — 켜기·끄기는 칩으로
        self._loop_prev = (a, b)
        if self.loop_var.get():
            self._on_loop_change()   # 이미 반복 중이면 새 구간으로 바로 — 꺼져 있으면 숫자만 바꿈
        else:
            self._schedule_save()

    def _show_settings(self):
        """⚙ 설정 창 — 소리 지연 보정(출력 장치별). 커서가 소리보다 앞서면 늘린다(블루투스 이어폰은
        0.1~0.3초 늦게 들리는데 Windows가 그 지연을 알려 주지 않는다, 2026-10-07)."""
        top = tk.Toplevel(self)
        top.title("설정")
        top.transient(self)
        top.configure(bg=W.PANEL)
        u = ui_scale(self)
        # 예비박 마디 수(0~2) — 재생: 그냥 재생·이동·마디 누르기로 시작할 때, 반복: 구간 반복의 첫 바퀴와 매 바퀴 전
        pre = tk.Frame(top, bg=W.PANEL)
        pre.pack(padx=int(20 * u), pady=(int(16 * u), 0), anchor="w")
        tk.Label(pre, text="예비박 (마디 수, 0이면 없음)", bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 11, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        for r_, (label, var, cmd) in enumerate((("재생 예비박", self.countin_var, self._schedule_save),
                                                 ("구간 반복 예비박", self.countin_loop_var, self._on_countin_loop)), 1):
            tk.Label(pre, text=label, bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 10)).grid(
                row=r_, column=0, sticky="w", pady=(6, 0))
            sp_ = ttk.Spinbox(pre, from_=0, to=COUNTIN_MAX_MEASURES, increment=1, width=3, justify="center",
                              textvariable=var, command=cmd, font=(W.FONT, 12), state="readonly", wrap=False)
            sp_.grid(row=r_, column=1, padx=(12, 6), pady=(6, 0))
            tk.Label(pre, text="마디", bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 10)).grid(row=r_, column=2,
                                                                                    sticky="w", pady=(6, 0))
        tk.Frame(top, bg=W.BORDER, height=1).pack(fill="x", padx=int(20 * u), pady=(int(12 * u), 0))
        # 겹박자(6/8·9/8·12/8) 메트로놈 박 단위 — 점4분음표 / 8분음표(곡별 저장, 바꾸면 클릭만 다시 만듦)
        unit = tk.Frame(top, bg=W.PANEL)
        unit.pack(padx=int(20 * u), pady=(int(12 * u), 0), anchor="w", fill="x")
        tk.Label(unit, text="겹박자 메트로놈 박 (6/8·9/8·12/8)", bg=W.PANEL, fg=W.TEXT,
                 font=(W.FONT, 11, "bold")).pack(anchor="w")
        urow = tk.Frame(unit, bg=W.PANEL)
        urow.pack(anchor="w", pady=(6, 0))
        for value, label in (("dq", "점4분음표"), ("e", "8분음표")):
            ttk.Radiobutton(urow, text=label, value=value, variable=self.metro_unit_var,
                            command=self._on_metro_unit).pack(side="left", padx=(0, 16))
        has = bool(self.score) and any(is_compound(m) for m in self.score["measures"])
        if not has:
            tk.Label(unit, text="지금 악보에는 겹박자가 없어 영향이 없습니다(다른 박자표는 늘 4분음표 박).",
                     bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9), justify="left").pack(anchor="w", pady=(4, 0))
        tk.Frame(top, bg=W.BORDER, height=1).pack(fill="x", padx=int(20 * u), pady=(int(12 * u), 0))
        frm = tk.Frame(top, bg=W.PANEL)
        frm.pack(padx=int(20 * u), pady=int(16 * u))
        name, _bt = self._output_device()
        tk.Label(frm, text="소리 지연 보정", bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 11, "bold")).grid(
            row=0, column=0, sticky="w")
        tk.Label(frm, text=f"출력 장치: {name or '알 수 없음'}\n커서가 소리보다 앞서면 늘리고, 늦으면 줄이세요(음수 가능).\n재생 중 [ 키: 커서 10ms 빠르게, ] 키: 10ms 느리게.",
                 bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9), justify="left").grid(row=1, column=0, columnspan=3,
                                                                                sticky="w", pady=(2, 8))
        spin = ttk.Spinbox(frm, from_=DELAY_MIN_MS, to=DELAY_MAX_MS, increment=10, width=6, justify="center",
                           textvariable=self.delay_var, command=self._save_device_delay, font=(W.FONT, 12))
        spin.grid(row=2, column=0, sticky="w")
        spin.bind("<Return>", lambda e: self._save_device_delay())
        spin.bind("<FocusOut>", lambda e: self._save_device_delay())
        tk.Label(frm, text="ms", bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 10)).grid(row=2, column=1, sticky="w",
                                                                                 padx=(6, 0))
        ttk.Button(frm, text="닫기", command=lambda: (self._save_device_delay(), top.destroy())).grid(
            row=2, column=2, padx=(16, 0))

        # 자동 맞추기(마이크) / 탭으로 맞추기 — 노트북·기기마다 지연이 달라서(사용자 요청 2026-10-07)
        tk.Frame(frm, bg=W.BORDER, height=1).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 10))
        tk.Label(frm, text="자동으로 맞추기", bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 11, "bold")).grid(
            row=4, column=0, columnspan=3, sticky="w")
        tk.Label(frm, text="마이크: 스피커로 전화 버튼음(DTMF) 8번을 내고 노트북 마이크로 들어 잽니다(약 5초).\n"
                           "탭: 마이크가 못 듣거나(블루투스 이어폰·마이크 꺼짐) 실패하면, 딸깍에 맞춰 스페이스를 누르세요.",
                 bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9), justify="left").grid(row=5, column=0, columnspan=3,
                                                                                sticky="w", pady=(2, 8))
        btns = tk.Frame(frm, bg=W.PANEL)
        btns.grid(row=6, column=0, columnspan=3, sticky="w")
        cal_msg = tk.StringVar(value="")
        mic_btn = ttk.Button(btns, text="자동 맞추기(마이크)", command=lambda: self._calibrate_mic(top, cal_msg))
        mic_btn.pack(side="left")
        ttk.Button(btns, text="탭으로 맞추기", command=lambda: self._calibrate_tap(top, cal_msg)).pack(
            side="left", padx=(8, 0))
        tk.Label(frm, textvariable=cal_msg, bg=W.PANEL, fg=W.ACCENT, font=(W.FONT, 10), justify="left",
                 wraplength=int(380 * u)).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        # 마이크가 막혀 측정을 못 하면 나타나는 "마이크 설정 열기"(앱이 직접 켜지는 않는다 — 보호 설정이라)
        self._mic_settings_btn = ttk.Button(frm, text="마이크 설정 열기", command=lambda: None)
        self._mic_settings_row = 8
        top.update_idletasks()
        bx = self.settings_btn.winfo_rootx() + self.settings_btn.winfo_width() - top.winfo_reqwidth()
        by = self.settings_btn.winfo_rooty() - top.winfo_reqheight() - int(40 * u)
        top.geometry(f"+{max(0, bx)}+{max(0, by)}")
        spin.focus_set()

    # ------------------------------------------------------------ "?" 도움말(편집·저장 가능)

    @staticmethod
    def _load_help_text():
        """사용자가 저장한 도움말이 있으면 그것, 없으면 기본 글."""
        try:
            with open(HELP_PATH, encoding="utf-8") as f:
                text = f.read()
            if text.strip():
                return text
        except OSError:
            pass
        return DEFAULT_HELP

    def _show_help(self):
        """"?" — 사용 안내 창. 읽기 상태로 열리고 [편집]을 누르면 고칠 수 있으며 [저장]하면 다음에도 그 글이 나온다.
        [기본값으로]는 앱에 들어 있는 기본 글로 되돌린다(저장은 따로 눌러야 확정)."""
        old = getattr(self, "_help_top", None)
        if old is not None and old.winfo_exists():
            old.lift()
            old.focus_force()
            return
        u = ui_scale(self)
        top = tk.Toplevel(self)
        self._help_top = top
        top.title("사용 안내")
        top.transient(self)
        top.configure(bg=W.PANEL)
        top.geometry(f"{int(760 * u)}x{int(700 * u)}")
        bar = tk.Frame(top, bg=W.PANEL)
        bar.pack(side="bottom", fill="x", padx=int(16 * u), pady=int(10 * u))
        body = tk.Frame(top, bg=W.PANEL)
        body.pack(side="top", fill="both", expand=True, padx=int(16 * u), pady=(int(14 * u), 0))
        sb = ttk.Scrollbar(body, orient="vertical")
        txt = tk.Text(body, wrap="word", font=(W.FONT, 11), bg=W.PANEL, fg=W.TEXT, relief="flat", padx=8, pady=6,
                      yscrollcommand=sb.set, undo=True, spacing1=2, spacing3=2)
        sb.config(command=txt.yview)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", self._load_help_text())
        hang_font = tkfont.Font(font=txt.cget("font"))

        def retag(_e=None):
            """줄이 화면 폭을 넘어 다음 줄로 이어질 때 HELP_TAB_CHARS글자만큼 들여 쓴다(사용자 요청 2026-10-14).
            각 줄의 앞 공백은 그대로 두고 이어지는 줄만 그만큼 더 들여 쓰므로 글을 고치면 다시 표시한다."""
            for tag in [t for t in txt.tag_names() if t.startswith("hang")]:
                txt.tag_remove(tag, "1.0", "end")
            sp = hang_font.measure(" ")
            for i in range(1, int(txt.index("end-1c").split(".")[0]) + 1):
                line = txt.get(f"{i}.0", f"{i}.end")
                if not line.strip() or line.lstrip().startswith("■"):
                    continue
                lead = len(line) - len(line.lstrip(" "))
                tag = f"hang{lead}"
                txt.tag_configure(tag, lmargin1=0, lmargin2=sp * (lead + HELP_TAB_CHARS))
                txt.tag_add(tag, f"{i}.0", f"{i}.end+1c")
        retag()
        txt.bind("<KeyRelease>", retag)
        txt.configure(state="disabled")
        state = {"editing": False, "saved": self._load_help_text()}
        msg = tk.StringVar(value="")
        tk.Label(bar, textvariable=msg, bg=W.PANEL, fg=W.ACCENT, font=(W.FONT, 10)).pack(side="left")

        def current():
            return txt.get("1.0", "end-1c")

        def set_editing(on):
            state["editing"] = on
            txt.configure(state="normal" if on else "disabled", bg="#FFFFFF" if on else W.PANEL)
            edit_btn.configure(text="편집 끝" if on else "편집")
            if on:
                txt.focus_set()

        def toggle_edit():
            set_editing(not state["editing"])
            msg.set("고친 뒤 [저장]을 누르세요." if state["editing"] else "")

        def save():
            text = current()
            try:
                if text.strip() == DEFAULT_HELP.strip():
                    if os.path.exists(HELP_PATH):
                        os.remove(HELP_PATH)   # 기본 글과 같으면 저장 파일을 두지 않음(앱 업데이트 때 새 기본 글이 보이게)
                else:
                    with open(HELP_PATH, "w", encoding="utf-8") as f:
                        f.write(text)
            except OSError as e:
                msg.set(f"저장하지 못했습니다: {e}")
                return
            state["saved"] = text
            set_editing(False)
            msg.set("저장했습니다.")

        def reset():
            if not messagebox.askyesno(APP_TITLE, "도움말을 기본 글로 되돌릴까요?\n(되돌린 뒤 [저장]을 눌러야 확정됩니다)", parent=top):
                return
            set_editing(True)
            txt.delete("1.0", "end")
            txt.insert("1.0", DEFAULT_HELP)
            retag()
            msg.set("기본 글로 되돌렸습니다. [저장]을 누르면 확정됩니다.")

        def close():
            if current() != state["saved"] and not messagebox.askyesno(
                    APP_TITLE, "저장하지 않은 수정이 있습니다. 그래도 닫을까요?", parent=top):
                return
            top.destroy()

        ttk.Button(bar, text="닫기", command=close).pack(side="right")
        ttk.Button(bar, text="기본값으로", command=reset).pack(side="right", padx=(0, 8))
        ttk.Button(bar, text="저장", command=save).pack(side="right", padx=(0, 8))
        edit_btn = ttk.Button(bar, text="편집", command=toggle_edit)
        edit_btn.pack(side="right", padx=(0, 8))
        top.protocol("WM_DELETE_WINDOW", close)
        top.bind("<Escape>", lambda e: None if state["editing"] else close())

    def _solo_part(self, k):
        """k번 성부만 켬(None이면 모두 켬)."""
        for i, var in enumerate(self.part_vars):
            var.set(k is None or i == k)
        self._on_parts_change()

    def _on_parts_change(self):
        """성부 칩이 바뀜 → 믹서 설정(mute)에 반영. self.settings 목록은 재생 중인 iter_mix·WAV 저장이
        같은 것을 보고 있어 그 자리에서 고치면 바로 들린다."""
        for s, var in zip(self.settings, self.part_vars):
            s.mute = not var.get()
        self._schedule_save()

    def _build_part_chips(self):
        """머리글 성부 칩(곡마다 성부 수가 달라 다시 만듦) — 누르면 켜고 끔, 오른쪽 클릭은 그 성부만,
        칩 줄 왼쪽 "성부" 글자를 누르면 모두 켜기. 켜진 칩은 청록, 꺼진 칩은 흰색이라 지금 들리는
        성부가 늘 보인다(예전 재생 막대의 "들리는 성부: 2" 표시를 대신함)."""
        u = ui_scale(self)
        for w in self.parts_bar.winfo_children():
            w.destroy()
        if not self.part_vars:
            return
        head = tk.Label(self.parts_bar, text="성부", bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 10), cursor="hand2")
        head.pack(side="left", padx=(0, int(6 * u)))
        head.bind("<Button-1>", lambda e: self._solo_part(None))
        abbrevs = self.score.get("part_abbrevs") or [""] * len(self.part_vars)
        for i, (name, ab, var) in enumerate(zip(self.score["parts"], abbrevs, self.part_vars)):
            label = ab or (name if len(name) <= 10 else name[:9] + "…")
            chip = W.ToggleChip(self.parts_bar, label, var, command=self._on_parts_change)
            chip.pack(side="left", padx=(0, int(6 * u)))
            chip.bind("<Button-3>", lambda e, k=i: self._solo_part(k))

    def _calibrate_mic(self, top, msg):
        """마이크로 지연 재기(latency_cal) — 재생 중이면 멈추고, 작업 스레드에서 잰 뒤 결과를 적용."""
        import latency_cal
        from audio_player import OUTPUT_LATENCY_SEC
        if getattr(self, "_calibrating", False):
            return
        self._calibrating = True
        self.pause()
        refresh_devices()
        msg.set("재는 중… 조용히 해 주세요(딸깍 소리가 납니다)")

        def work():
            # 작업 스레드에서 WASAPI를 열려면 COM을 먼저 초기화해야 한다(안 하면 "Unanticipated host
            # error"로 실패, 2026-10-07 실측). 메인 스레드는 Tk가 이미 해 둠.
            com = False
            try:
                import ctypes
                com = ctypes.windll.ole32.CoInitializeEx(None, 0) in (0, 1)
            except Exception:
                pass
            try:
                ms, note = latency_cal.measure(OUTPUT_LATENCY_SEC)
            except Exception as e:
                ms, note = None, f"마이크·스피커를 열 수 없습니다: {e}"
            finally:
                if com:
                    ctypes.windll.ole32.CoUninitialize()
            self._post(done, ms, note)

        def done(ms, note):
            self._calibrating = False
            if ms is None:
                if isinstance(note, tuple):   # 마이크가 막힘 — 설정 화면 여는 버튼
                    kind, text = note
                    msg.set(f"자동 맞추기 실패 — {text}")
                    self._mic_settings_btn.configure(
                        command=lambda: latency_cal.open_mic_settings(kind == "MIC_PRIVACY"))
                    self._mic_settings_btn.grid(row=self._mic_settings_row, column=0, columnspan=3, sticky="w",
                                                pady=(8, 0))
                else:
                    msg.set(f"자동 맞추기 실패 — {note}")
                return
            self._mic_settings_btn.grid_remove()
            ms = max(DELAY_MIN_MS, min(DELAY_MAX_MS, ms))
            self.delay_var.set(ms)
            self._save_device_delay()
            name, _bt = self._output_device()
            msg.set(f"맞춤: {ms:+d}ms ({name}) — {note}\n측정이 끝났습니다. 마이크를 다시 막아도 됩니다.")
            self._set_status(f"소리 지연 보정을 {ms:+d}ms로 맞췄습니다 ({name})")

        threading.Thread(target=work, daemon=True).start()

    def _calibrate_tap(self, top, msg):
        """탭으로 맞추기 — 0.6초 간격 딸깍 10번(처음 2번은 준비), 들리는 순간 스페이스. 누른 순간의
        재생 위치(보정 전)와 그 딸깍의 시각 차이의 중앙값을 보정값으로. 사람은 박자에 맞춰 누를 때
        조금 일찍 누르는 경향이 있어(수십 ms) 마이크보다 덜 정확하다."""
        from render import count_in
        if getattr(self, "_calibrating", False):
            return
        self._calibrating = True
        self.pause()
        refresh_devices()
        sr, gap, n = 44100, 0.6, 10
        lead = 0.5
        buf = count_in(1, gap, sr, volume=0.9)                    # 딸깍 하나 + 남은 간격
        one = np.concatenate([buf, np.zeros((int(gap * sr) - len(buf), 2), np.float32)]) if len(buf) < gap * sr else buf
        audio = np.concatenate([np.zeros((int(lead * sr), 2), np.float32)] + [one] * n
                               + [np.zeros((int(0.8 * sr), 2), np.float32)])
        click_times = [lead + i * gap for i in range(n)]
        taps = []

        def on_tap(_e=None):
            taps.append(self.player.played_sec())
            msg.set(f"탭 {len(taps)}번 — 딸깍이 들리는 순간 스페이스(셋째 딸깍부터)")
            return "break"

        top.bind("<space>", on_tap)
        top.focus_force()
        msg.set("딸깍이 들리는 순간마다 스페이스를 누르세요(셋째 딸깍부터, 8번)")
        from audio_player import iter_buffer
        self.player.play(iter_buffer(audio), sr, 2)

        def finish():
            self.player.stop()
            top.unbind("<space>")
            self._calibrating = False
            diffs = []
            for t in taps:
                k = int(round((t - lead) / gap))
                if 2 <= k < n and abs(t - click_times[k]) < gap * 0.45:
                    diffs.append(t - click_times[k])
            if len(diffs) < 5:
                msg.set(f"탭이 {len(diffs)}번만 맞아서 계산할 수 없습니다. 다시 해 보세요.")
                return
            ms = int(round(float(np.median(diffs)) * 1000 / 5.0)) * 5
            ms = max(DELAY_MIN_MS, min(DELAY_MAX_MS, ms))
            self.delay_var.set(ms)
            self._save_device_delay()
            name, _bt = self._output_device()
            msg.set(f"맞춤: {ms:+d}ms ({name}) — 탭 {len(diffs)}번")
            self._set_status(f"소리 지연 보정을 {ms:+d}ms로 맞췄습니다 ({name})")

        self.after(int((lead + n * gap + 0.8) * 1000), finish)

    def _nudge_delay(self, ms, e=None):
        if e is not None and isinstance(e.widget, (ttk.Entry, tk.Entry, ttk.Spinbox)):
            return
        v = max(DELAY_MIN_MS, min(DELAY_MAX_MS, int(self._delay_sec() * 1000) + ms))
        self.delay_var.set(v)
        self._save_device_delay()
        name, _bt = self._output_device()
        self._set_status(f"소리 지연 보정 {v:+d}ms ({name}) — 커서가 늦으면 [ , 앞서면 ] ")
        return "break"

    def _set_controls(self, ready):
        for b in (self.play_btn, self.home_btn, self.save_btn):
            b.set_enabled(ready)
        self.timeline.set_state(enabled=ready)

    @staticmethod
    def _next_tempo(pct, delta):
        """빠르기 한 단계(TEMPO_STEP %) — 10의 배수가 아니면(예전 5% 단위로 저장된 85% 등) 그 방향의 다음 배수로."""
        step = TEMPO_STEP
        nxt = (pct // step + 1) * step if delta > 0 else ((pct - 1) // step) * step
        return max(TEMPO_MIN, min(TEMPO_MAX, nxt))

    def _step_tempo(self, delta):
        if self.loop_var.get():
            # 구간 빠르기 — 구간만 다시 합성하므로 바로(재생 중이면 지금 위치에서 이어서)
            self.loop_tempo_var.set(self._next_tempo(self._loop_tempo_pct(), delta))
            self._schedule_save()
            self._debounce("looptempo", 250, self._restart_if_playing)
            return
        self.tempo_var.set(self._next_tempo(int(round(self.tempo_scale * 100)), delta))
        self._on_tempo_change()

    def _loop_tempo_pct(self):
        try:
            return max(TEMPO_MIN, min(TEMPO_MAX, int(self.loop_tempo_var.get())))
        except (tk.TclError, ValueError):
            return 100

    def _refresh_tempo_pill(self):
        """빠르기 알약 글자 — 구간 반복 중이면 구간 빠르기, 아니면 곡 빠르기와 그 ♩ 값."""
        looping = self.loop_var.get()
        pct = self._loop_tempo_pct() if looping else int(self.tempo_scale * 100)
        self.tempo_label_var.set("구간 빠르기" if looping else "빠르기")
        self.tempo_text.set(f"{pct}%")
        if self.score is not None:
            q = 0.0
            if looping and self.rendered is not None:
                ks = self._loop_indices()
                if ks:
                    q = self.rendered.played["measures"][ks[0]]["start_q"]
            tempos = self.score["tempos"]
            base = next((b for qq, b in reversed(tempos) if qq <= q + 1e-9), tempos[0][1]) if tempos else 100
            self.bpm_var.set(f"♩ = {base * pct / 100:.0f}")

    def _restart_if_playing(self):
        if self.playing and self.rendered is not None:
            self.cursor_nominal = self._current_sec() * self.rendered.tempo_scale
            self.stop()
            self.play()

    def _timeline_tip(self, frac):
        """진행 막대를 끄는 동안 말풍선에 보일 글자 — 그 위치의 마디 번호."""
        r = self.rendered
        if r is None or not self.score:
            return ""
        k = self._played_index_at(frac * r.length_sec)
        pm = r.played["measures"][k]
        m = self.score["measures"][pm.get("orig_index", k)]
        occ = pm.get("occurrence", 1)
        return f"마디 {m['number']}" + (f" ({occ}번째)" if occ > 1 else "")

    def _timeline_is_mark(self, frac):
        """진행 막대 위치가 번호 20의 배수(20·40·60…) 마디 앞인지 — 악보의 리허설 마크와 같은 기준."""
        r = self.rendered
        if r is None or not self.score:
            return False
        k = self._played_index_at(frac * r.length_sec)
        num = str(self.score["measures"][r.played["measures"][k].get("orig_index", k)]["number"])
        digits = "".join(ch for ch in num if ch.isdigit())
        n = int(digits) if digits else 0
        return n > 0 and n % MEASURE_MARK_EVERY == 0 and abs(frac * r.length_sec - r.measure_starts[k]) < 1e-3

    def _on_timeline_seek(self, frac):
        r = self.rendered
        if r is not None:
            # 끄는 위치가 속한 마디의 맨 앞으로(사용자 요청 2026-10-13: 재생 커서가 늘 마디 앞에)
            k = self._played_index_at(frac * r.length_sec)
            self.seek(float(r.measure_starts[k]))

    def _show_recent_menu(self):
        """▼ — 최근 악보 목록(곡 · 마지막으로 연 때 · 멈춘 마디), 누르면 열림."""
        menu = tk.Menu(self, tearoff=0, font=(W.FONT, 10))
        if not self.history:
            menu.add_command(label="(아직 연 악보가 없습니다)", state="disabled")
        for path in self.history:
            saved = self.all_settings.get(_norm(path), {})
            extra = " · ".join(t for t in (_fmt_when(saved.get("opened_at", "")),
                                           f"마디 {saved['last_measure']}" if saved.get("last_measure") else "") if t)
            label = os.path.splitext(os.path.basename(path))[0] + (f"   ({extra})" if extra else "")
            menu.add_command(label=label, command=lambda p=path: self.open_path(p))
        menu.add_separator()
        menu.add_command(label="다른 악보 열기…", command=self._ask_open)
        menu.add_command(label="멜로디에 화성 및 색깔 넣기…", command=self._ask_harmonize)
        x = self.history_btn.winfo_rootx()
        y = self.history_btn.winfo_rooty() + self.history_btn.winfo_height()
        menu.tk_popup(x, y)

    def _edit_loop_range(self, _e=None):
        """구간 반복 칩 오른쪽 클릭 — 시작·끝 마디를 고르는 작은 창."""
        if not self._measure_numbers:
            return
        top = tk.Toplevel(self)
        top.title("반복 구간")
        top.transient(self)
        top.configure(bg=W.PANEL)
        u = ui_scale(self)
        frm = tk.Frame(top, bg=W.PANEL)
        frm.pack(padx=int(18 * u), pady=int(14 * u))
        tk.Label(frm, text="마디", bg=W.PANEL, font=(W.FONT, 10)).grid(row=0, column=0, padx=(0, 6))
        a = ttk.Spinbox(frm, values=self._measure_numbers, textvariable=self.loop_a_var, width=6,
                        state="readonly", justify="center")
        a.grid(row=0, column=1)
        tk.Label(frm, text="~", bg=W.PANEL, font=(W.FONT, 10)).grid(row=0, column=2, padx=6)
        b = ttk.Spinbox(frm, values=self._measure_numbers, textvariable=self.loop_b_var, width=6,
                        state="readonly", justify="center")
        b.grid(row=0, column=3)

        def done():
            self.loop_var.set(True)
            self._on_loop_change()
            top.destroy()
        ttk.Button(frm, text="반복", command=done).grid(row=0, column=4, padx=(10, 0))
        top.geometry(f"+{self.loop_chip.winfo_rootx()}+{self.loop_chip.winfo_rooty() - int(70 * u)}")

    # ------------------------------------------------------------ 작은 도우미

    def _debounce(self, key, ms, fn):
        old = self._after_ids.pop(key, None)
        if old is not None:
            self.after_cancel(old)
        self._after_ids[key] = self.after(ms, lambda: (self._after_ids.pop(key, None), fn()))

    def _post(self, fn, *args):
        """작업 스레드 → UI 스레드로 일 넘기기."""
        self._ui_q.put((fn, args))

    def _set_status(self, text):
        self.status_var.set(text)

    def _show_progress(self, frac):
        """합성 진행률(0~1, None이면 끝) — 재생 진행 막대 자리에 주황으로 보여 준다."""
        self.timeline.set_state(synth=frac)

    @property
    def tempo_scale(self):
        try:
            return max(TEMPO_MIN, min(TEMPO_MAX, int(self.tempo_var.get()))) / 100.0
        except (tk.TclError, ValueError):
            return 1.0

    # ------------------------------------------------------------ 파일 열기

    def _refresh_recent(self):
        if self.renderer is None:
            self._show_start_screen()

    def _ask_open(self):
        initial = os.path.dirname(self.history[0]) if self.history else None
        path = filedialog.askopenfilename(
            title="악보 열기", initialdir=initial,
            filetypes=[("악보 (MusicXML·MuseScore·Guitar Pro)", score_import.OPEN_PATTERNS),
                       ("MusicXML", "*.xml *.musicxml *.mxl"),
                       ("MuseScore", "*.mscz *.mscx"),
                       ("Guitar Pro", "*.gp *.gpx *.gp3 *.gp4 *.gp5 *.gp6 *.gp7"),
                       ("모든 파일", "*.*")])
        if path:
            self.open_path(path)

    def _ask_harmonize(self):
        """"멜로디에 화성 및 색깔 넣기" — 지금 열린 악보(없으면 고르기)에 설정 창에서 고른 대로
        화성(반주 성부를 더함) 또는 색깔(단선율·화음 모두 코드별 색, harmonize.color_file melody_part=None)을 넣은
        새 악보를 만들어 연다."""
        src = self.path
        if not src:
            src = filedialog.askopenfilename(
                title="화성·색깔을 넣을 악보 고르기",
                initialdir=os.path.dirname(self.history[0]) if self.history else None,
                filetypes=[("악보 (MusicXML·MuseScore·Guitar Pro)", score_import.OPEN_PATTERNS), ("모든 파일", "*.*")])
            if not src:
                return
        try:
            names = harmonize.part_names(src)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"악보를 읽지 못했습니다:\n{e}")
            return
        choice = self._harmonize_dialog(names)
        if choice is None:
            return
        mode, melody, voices, skip = choice
        color_only = mode == "color"
        self._harm_mode = mode
        if not color_only:
            self._harm_voices = voices
        self._set_status("색깔 넣는 중…" if color_only else "화성 만드는 중…")
        make = (lambda path, out=None: harmonize.color_file(path, out, melody_part=None, skip_measures=skip)) \
            if color_only else (lambda path, out=None: harmonize.harmonize_file(path, out, melody_part=melody,
                                                                              voices=voices))
        suffix = "_색깔" if color_only else "_화성"

        def work():
            try:
                try:
                    out, info = make(src)
                except OSError:      # 원본 폴더에 쓸 수 없으면 임시 폴더에
                    tmp = os.path.join(tempfile.gettempdir(), "GenerateScore_import")
                    os.makedirs(tmp, exist_ok=True)
                    stem = os.path.splitext(os.path.basename(src))[0]
                    out, info = make(src, harmonize.next_harmonized_path(os.path.join(tmp, stem)) if not color_only
                                     else os.path.join(tmp, stem + suffix + ".musicxml"))
            except Exception as e:
                self._post(self._on_load_error, src, e)
                return
            self._post(self._on_harmonized, out, info)

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _int_or_zero(var):
        try:
            return max(0, int(var.get()))
        except (tk.TclError, ValueError):
            return 0

    def _harmonize_dialog(self, names):
        """"멜로디에 화성 및 색깔 넣기" 설정 창 — 맨 위 라디오 버튼으로 화성/색깔을 고른다(사용자 요청 2026-10-27).
          화성: 반주 성부 수(1~4)와 성부마다 악기·드롭 D·화음 음 수·가장 빠른 음표, 가이드, 악보 성부가 여럿이면 멜로디 성부.
          색깔: 단선율이든 화음이든 코드별 색(적힌 코드가 있으면 따르고 없으면 정해 오선 위에), "처음 N마디는 전주(색 제외)".
        돌려주는 값: (모드 "harm"/"color", 멜로디 번호, 위에서부터 성부 지정 목록, 전주 마디 수). 취소하면 None."""
        top = tk.Toplevel(self)
        top.title("멜로디에 화성 및 색깔 넣기")
        top.transient(self)
        top.configure(bg=W.PANEL)
        top.resizable(False, False)
        u = ui_scale(self)
        frm = tk.Frame(top, bg=W.PANEL)
        frm.pack(padx=int(20 * u), pady=int(16 * u))
        mode_var = tk.StringVar(value=getattr(self, "_harm_mode", "harm"))
        modes = tk.Frame(frm, bg=W.PANEL)
        modes.grid(row=0, column=0, sticky="w")
        for value, text in (("harm", "화성"), ("color", "색깔")):
            ttk.Radiobutton(modes, text=text, value=value, variable=mode_var).pack(side="left", padx=(0, 16))
        harm_box = tk.Frame(frm, bg=W.PANEL)      # 화성 — 지금까지 만든 성부 설정
        color_box = tk.Frame(frm, bg=W.PANEL)     # 색깔 — 전주(색 제외)
        for box_ in (harm_box, color_box):
            box_.grid(row=1, column=0, sticky="nw", pady=(10, 0))

        # ---- 화성
        prev_voices = list(getattr(self, "_harm_voices", ["guitar", "guitar"]))
        keys = list(harmonize.INSTRUMENTS)
        labels = [harmonize.INSTRUMENTS[k]["label"] for k in keys]
        n_max = harmonize.MAX_VOICES
        count_var = tk.IntVar(value=len(prev_voices))
        prev_parsed = [harmonize.parse_voice_opts(prev_voices[min(i, len(prev_voices) - 1)]) for i in range(n_max)]
        name_vars = [tk.StringVar(value=harmonize.INSTRUMENTS[k]["label"]) for k, _o in prev_parsed]
        drop_vars = [tk.BooleanVar(value=o["drop"]) for _k, o in prev_parsed]     # 드롭 D(기타·콘트라베이스 기타만)
        # 모든 성부: 화음 음 수 최대(화음 악기만), 가장 빠른 음표 — 저음 성부는 한계로만 쓰인다(harmonize._voice_specs)
        notes_values = ["자동"] + [str(k) for k in range(1, harmonize.MAX_CHORD_NOTES + 1)]
        fast_values = ["자동"] + [f"{d}분" for d in harmonize.FASTEST_CHOICES]
        fast_text = lambda d: f"{d}분" if d else "자동"
        notes_vars = [tk.StringVar(value=str(o["notes"]) if o["notes"] else "자동") for _k, o in prev_parsed]
        fast_vars = [tk.StringVar(value=fast_text(o["fastest"])) for _k, o in prev_parsed]

        def key_of(i):
            return keys[labels.index(name_vars[i].get())]

        def notes_on(i, n):
            """i번째 줄에서 화음 음 수를 정할 수 있나 — 켜진 줄의 화음 악기(한 음씩 내는 악기는 화음이 없음)."""
            return i < n and not harmonize.INSTRUMENTS[key_of(i)].get("mono")

        def spec(i):
            """i번째 줄의 성부 지정("guitar", "guitar:dropd:n4:f16" …) — 지금 정할 수 없는 항목은 뺀다."""
            key = key_of(i)
            n = count_var.get()
            notes, fast = notes_vars[i].get(), fast_vars[i].get()
            return harmonize.make_voice(
                key, drop=drop_vars[i].get() and bool(harmonize.INSTRUMENTS[key].get("drop_d")),
                notes=int(notes) if notes_on(i, n) and notes != "자동" else 0,
                fastest=int(fast[:-1]) if i < n and fast != "자동" else harmonize.DEFAULT_FASTEST)

        head = tk.Frame(harm_box, bg=W.PANEL)
        head.grid(row=0, column=0, sticky="w")
        tk.Label(head, text="반주 성부", bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 10, "bold")).pack(side="left")
        for n in range(1, n_max + 1):
            ttk.Radiobutton(head, text=f"{n}성부", value=n, variable=count_var).pack(side="left", padx=(10 if n == 1 else 4, 0))
        voices = tk.Frame(harm_box, bg=W.PANEL)
        voices.grid(row=1, column=0, sticky="w", pady=(6, 0))
        for c, text in ((2, "악기"), (4, "화음 음 수"), (5, "가장 빠른 음표")):
            tk.Label(voices, text=text, bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9)).grid(row=0, column=c, sticky="w",
                                                                                       padx=(8, 0))
        rows = []   # (번호 이름표, 역할 이름표, 악기 목록, 드롭 D, 화음 음 수, 가장 빠른 음표, 설명)
        for i in range(n_max):
            num = tk.Label(voices, text=f"{i + 1}성부", bg=W.PANEL, font=(W.FONT, 10))
            role = tk.Label(voices, bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 10), width=8, anchor="w")
            box = ttk.Combobox(voices, values=labels, textvariable=name_vars[i], state="readonly", width=15)
            drop = ttk.Checkbutton(voices, text="드롭 D", variable=drop_vars[i])
            notes_box = ttk.Combobox(voices, values=notes_values, textvariable=notes_vars[i], state="readonly", width=5)
            fast_box = ttk.Combobox(voices, values=fast_values, textvariable=fast_vars[i], state="readonly", width=5)
            note = tk.Label(voices, bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9))
            for c, w in enumerate((num, role, box, drop, notes_box, fast_box, note)):
                w.grid(row=i + 1, column=c, sticky="w", padx=(0 if c == 0 else 8, 0), pady=2)
            rows.append((num, role, box, drop, notes_box, fast_box, note))
        # 가이드 — 고를 때마다 harmonize.check_voices로 이상한 구성을 알려 주고, 순서가 뒤집혔으면 바로잡는 버튼
        guide = tk.Frame(harm_box, bg=W.PANEL)
        guide.grid(row=2, column=0, sticky="we", pady=(6, 0))

        def apply_order(new_specs):
            for i, sp in enumerate(new_specs):     # 화음 음 수·빠른 음표·드롭 D도 악기를 따라 옮긴다
                key, o = harmonize.parse_voice_opts(sp)
                name_vars[i].set(harmonize.INSTRUMENTS[key]["label"])
                drop_vars[i].set(o["drop"])
                notes_vars[i].set(str(o["notes"]) if o["notes"] else "자동")
                fast_vars[i].set(fast_text(o["fastest"]))

        def show_guide():
            for w in guide.winfo_children():
                w.destroy()
            specs = [spec(i) for i in range(count_var.get())]
            for level, text in harmonize.check_voices(specs):
                tk.Label(guide, text=("⚠ " if level == "warn" else "ℹ ") + text, bg=W.PANEL,
                         fg=W.LOOP_TEXT if level == "warn" else W.MUTED, font=(W.FONT, 9),
                         wraplength=int(640 * u), justify="left", anchor="w").pack(fill="x", anchor="w", pady=1)
            fixed = harmonize.fix_voice_order(specs)
            if fixed != specs:
                names_ = " → ".join(harmonize.voice_instrument(sp)["label"] for sp in fixed)
                ttk.Button(guide, text=f"순서 바로잡기 ({names_})",
                           command=lambda: apply_order(fixed)).pack(anchor="w", pady=(4, 0))

        def refresh(*_a):
            """성부 수만큼 줄을 켜고, 줄마다 역할(위 화음·가운데·맨 아래 저음)과 악기 설명을 맞춘다."""
            n = count_var.get()
            for i, (num, role, box, drop, notes_box, fast_box, note) in enumerate(rows):
                on = i < n
                if not on:
                    text = ""
                elif i == 0:
                    text = "화음·코드"
                elif i == n - 1:
                    text = "저음"
                else:
                    text = "중간" + (str(i) if n > 3 else "")
                role.configure(text=text)
                num.configure(fg=W.TEXT if on else W.DISABLED)
                box.configure(state="readonly" if on else "disabled")
                can_drop = on and harmonize.INSTRUMENTS[key_of(i)].get("drop_d")
                drop.configure(state="normal" if can_drop else "disabled")
                notes_box.configure(state="readonly" if notes_on(i, n) else "disabled")
                fast_box.configure(state="readonly" if on else "disabled")
                note.configure(text=harmonize.voice_note(spec(i)) if on else "")
            show_guide()
        count_var.trace_add("write", refresh)
        for v in name_vars + drop_vars + notes_vars + fast_vars:
            v.trace_add("write", refresh)
        refresh()
        melody_var = tk.IntVar(value=1)
        if len(names) > 1:
            tk.Label(harm_box, text="멜로디 성부", bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 10, "bold")).grid(row=3, column=0, sticky="w",
                                                                                               pady=(10, 0))
            for i, n in enumerate(names, 1):
                ttk.Radiobutton(harm_box, text=f"{i}. {n}", value=i, variable=melody_var).grid(row=3 + i, column=0,
                                                                                              sticky="w", pady=1)
        # 화성 붙이기는 처음부터 모든 성부(사용자 지적 2026-10-27: "화음에는 그냥 멜로디는 없다" — "처음 N마디는
        # 멜로디만"을 모르고 남겨 첫 줄에 반주가 안 나옴). 전주 마디 수는 색깔에만.

        # ---- 색깔
        # 글자색을 꼭 정한다 — 안 정하면 Mac 다크 모드에서 흰 글자가 밝은 바탕에 묻혀 안 보였음(사용자 지적 2026-10-27)
        tk.Label(color_box, text="단선율이든 화음이든 코드마다 다른 색으로 칠합니다(처음부터 칠해짐).\n"
                                 "악보에 코드가 있으면 그 코드를 따르고, 없으면 코드를 정해 오선 위에 적습니다.",
                 bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 10), justify="left").pack(anchor="w")
        skip_var = tk.IntVar(value=0)
        skip_row = tk.Frame(color_box, bg=W.PANEL)
        skip_row.pack(anchor="w", pady=(10, 0))
        tk.Label(skip_row, text="전주곡", bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 10, "bold")).pack(side="left", padx=(0, 8))
        tk.Label(skip_row, text="처음", bg=W.PANEL, fg=W.TEXT, font=(W.FONT, 10)).pack(side="left")
        ttk.Spinbox(skip_row, from_=0, to=64, width=4, textvariable=skip_var, justify="center").pack(side="left", padx=4)
        tk.Label(skip_row, text="마디(색 제외, 0 = 처음부터 색)", bg=W.PANEL, fg=W.TEXT,
                 font=(W.FONT, 10)).pack(side="left")

        def show_mode(*_a):
            harm = mode_var.get() == "harm"
            (harm_box if harm else color_box).grid()
            (color_box if harm else harm_box).grid_remove()
        mode_var.trace_add("write", show_mode)
        show_mode()

        result = []

        def done(_e=None):
            harm = mode_var.get() == "harm"
            result.append((mode_var.get(), melody_var.get(),
                           [spec(i) for i in range(count_var.get())] if harm else None,
                           0 if harm else self._int_or_zero(skip_var)))
            top.destroy()

        btns = tk.Frame(frm, bg=W.PANEL)
        btns.grid(row=2, column=0, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="만들기", command=done).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="취소", command=top.destroy).pack(side="left")
        top.bind("<Escape>", lambda _e: top.destroy())
        top.bind("<Return>", done)
        top.grab_set()
        self.wait_window(top)
        return result[0] if result else None

    def _on_harmonized(self, out, info):
        self.open_path(out)
        how = "악보에 적힌 코드 이름대로" if info.get("chords_given") else f"자동으로 (조: {info['key']})"
        if info.get("style") == "color":
            self._set_status(f"코드별 색깔을 넣었습니다 — {how} · 저장: {out}")
            return
        voices = info.get("voices") or [info.get("instrument", "guitar")]
        label = f"{len(voices)}성부(" + " · ".join(harmonize.voice_instrument(v)["label"] for v in voices) + ")"
        self._set_status(f"화성을 붙였습니다 — {label} · {how} · 저장: {out}")

    def open_path(self, path):
        if not os.path.exists(path):
            messagebox.showerror(APP_TITLE, f"파일이 없습니다:\n{path}")
            self.history = [p for p in self.history if _norm(p) != _norm(path)]
            self._refresh_recent()
            _save_json(HISTORY_PATH, self.history)
            return
        self.stop()
        self._save_song_settings()
        self._render_gen += 1  # 이전 곡 합성 중이면 버림
        self.rendered = None
        self._set_controls(False)
        self._set_status(f"MuseScore로 변환 중… {os.path.basename(path)}" if score_import.needs_conversion(path)
                         else f"악보 읽는 중… {os.path.basename(path)}")
        slot = self._slot_size(False)
        slot_single = self._slot_size(True)     # 동요 모드(한 쪽)면 창 너비를 다 쓰는 칸 — 스레드에서 winfo를 부르지 않으려고 미리 계산
        pool = self._get_pool()

        def work():
            try:
                score = read_musicxml(path)
                # 조판은 별도 프로세스에서(Verovio도 GIL을 오래 잡음 — score_view.layout_systems_file).
                # 창 크기와 무관하게 곡마다 한 번 — 칸에 맞춘 쪽 배치는 arrange로 순식간.
                sysset = pool.submit(layout_systems_file, path).result()
                renderer, boxes = sysset.arrange(*(slot_single if sysset.single_page else slot))
            except Exception as e:
                self._post(self._on_load_error, path, e)
                return
            self._post(self._on_loaded, path, score, renderer, boxes, sysset)

        threading.Thread(target=work, daemon=True).start()

    def _on_load_error(self, path, err):
        _log_error(f"악보 열기: {path}", err)
        self._set_status("악보를 읽지 못했습니다.")
        messagebox.showerror(APP_TITLE, f"악보를 읽지 못했습니다:\n{path}\n\n{err}")

    def _on_loaded(self, path, score, renderer, boxes, sysset):
        self.path = path
        self.score = score
        self.renderer = renderer
        self._sysset = sysset
        # 마디 상자 수가 악보 마디 수와 다르면(조판 엔진이 마디를 합치거나 나눔) 강조를 끈다
        self.boxes = boxes if len(boxes) == len(score["measures"]) else []
        try:
            cm = harmonize.read_chord_map(path)
        except Exception:
            cm = None
        self._chord_map = cm if cm and len(cm) == len(score["measures"]) else None
        self._painted = set()
        self._last_cursor_sec = 0.0
        self._round_entries = []
        rm = None
        if not self._chord_map:
            try:
                info = round_song.read_round_info(path)
            except Exception:
                info = None
            n_m = len(score["measures"])
            if info and info["chords"] and len(info["chords"]) == n_m and self.boxes:
                # 돌림노래 + 악보에 코드 이름이 있음 — 노래하고 있는 성부의 보표를 코드 색으로(성부는 시작 마디부터)
                self._chord_map = info["chords"]
                self._round_entries = [e for e in info["entries"] if e[2] < n_m]
            elif info:
                try:
                    rm = round_song.read_round_map(path)       # 코드 이름이 없으면 성부 시작 마디만 성부 색으로
                except Exception:
                    rm = None
        self._round_map = rm if rm and self.boxes and all(k < len(score["measures"]) for k in rm) else None
        # 가사가 있는 곡의 "가사 없는 전주"(처음 가사가 나오는 마디 앞)는 색을 칠하지 않음
        self._color_all = False
        explicit_prelude = False
        try:
            xml_root = musicxml_reader._load_root(path)
            lyric_at = harmonize.first_lyric_measure(xml_root)
            self._color_all = harmonize.has_field(xml_root, harmonize.COLOR_ALL_FIELD)
            explicit_prelude = harmonize.has_prelude_field(xml_root)
        except Exception:
            lyric_at, explicit_prelude = None, False
        if lyric_at and not explicit_prelude:      # 사용자가 전주 마디 수를 정해 둔 악보는 가사 판정을 따르지 않음(가사 인식이 엉터리일 수 있음)
            if self._chord_map:
                self._chord_map = [[] if k < lyric_at else row for k, row in enumerate(self._chord_map)]
            if self._round_map:
                self._round_map = {k: v for k, v in self._round_map.items() if k >= lyric_at} or None
        if self._color_all and self._chord_map:        # 처음부터 모든 코드 구간을 칠함
            self._painted = {(k, j) for k, row in enumerate(self._chord_map) for j in range(len(row))}
        self._round_painted = set()
        self._score_gen += 1
        self._png_cache.clear()
        self._png_pending.clear()
        self.page = 1
        self.canvas.delete("all")
        self._shown_measure = None

        self.history = [path] + [p for p in self.history if _norm(p) != _norm(path)]
        self.history = self.history[:HISTORY_MAX]
        _save_json(HISTORY_PATH, self.history)
        self._refresh_recent()
        self.title(f"{os.path.basename(path)} — {APP_TITLE}")

        numbers = [str(m["number"]) for m in score["measures"]]
        self._measure_numbers = numbers
        self.loop_a_spin.configure(values=numbers)
        self.loop_b_spin.configure(values=numbers)
        self.title_var.set(os.path.splitext(os.path.basename(path))[0].replace("_", " "))
        self._apply_song_settings()
        saved = self.all_settings.get(_norm(path), {})
        self.cursor_nominal = float(saved.get("last_nominal", 0.0) or 0.0)
        self._resume_measure = saved.get("last_measure", "") if self.cursor_nominal > 0 else ""
        saved = dict(saved)
        saved["opened_at"] = datetime.datetime.now().isoformat(timespec="minutes")
        self.all_settings[_norm(path)] = saved
        _save_json(SETTINGS_PATH, self.all_settings)
        self.show_page(1)
        self._start_render()

    # ------------------------------------------------------------ 곡별 설정 저장

    def _apply_song_settings(self):
        saved = self.all_settings.get(_norm(self.path), {})
        # 성부 음색·음량은 늘 기본값. 켜고 끄기는 ⚙ 창에서 되돌릴 수 있게 된 뒤로(2026-10-08) 곡마다
        # "parts_on"으로 저장·복원한다(예전 "끄기" 저장값은 되돌릴 화면이 없어 읽지 않았었음).
        self.settings = default_settings(self.score)
        parts_on = saved.get("parts_on")
        if not (isinstance(parts_on, list) and len(parts_on) == len(self.settings)):
            parts_on = [True] * len(self.settings)
        self.part_vars = [tk.BooleanVar(value=bool(v)) for v in parts_on]
        for s, var in zip(self.settings, self.part_vars):
            s.mute = not var.get()
        self._build_part_chips()
        self.tempo_var.set(int(saved.get("tempo", 100)))
        mode = saved.get("metro")
        if mode not in ("off", "with", "only"):
            mode = "only" if saved.get("click") else "off"   # 예전 저장값(click=True는 "메트로놈만"이었음)
        self.metro_var.set(mode)
        self.metro_unit_var.set("e" if saved.get("metro_unit") == "e" else "dq")
        # 예전 저장값은 켬/끔(True/False) → 1/0마디
        self.countin_var.set(int(min(2, max(0, int(saved.get("countin", 1))))))
        self.countin_loop_var.set(int(min(2, max(0, int(saved.get("countin_loop", 1))))))
        self.loop_tempo_var.set(int(saved.get("loop_tempo", saved.get("tempo", 100))))
        numbers = [str(m["number"]) for m in self.score["measures"]]
        a = saved.get("loop_a") if saved.get("loop_a") in numbers else numbers[0]
        b = saved.get("loop_b") if saved.get("loop_b") in numbers else numbers[min(len(numbers) - 1, 7)]
        self.loop_a_var.set(a)
        self.loop_b_var.set(b)
        self.loop_var.set(bool(saved.get("loop", False)))
        ok = lambda p: isinstance(p, dict) and str(p.get("a")) in numbers and str(p.get("b")) in numbers
        history = [{"a": str(p["a"]), "b": str(p["b"])} for p in saved.get("loop_history", []) or [] if ok(p)]
        # 예전 "저장한 구간"(이름 붙은 loop_presets)은 저장 목록 뒤에 합침 — 이제 저장 목록 하나로 통일
        for p in saved.get("loop_presets", []) or []:
            if ok(p) and not any((h["a"], h["b"]) == (str(p["a"]), str(p["b"])) for h in history):
                history.append({"a": str(p["a"]), "b": str(p["b"])})
        self.loop_history = history[:LOOP_HISTORY_MAX]
        self._sync_block_buttons()

    def _schedule_save(self):
        self._debounce("save", SAVE_DEBOUNCE_MS, self._save_song_settings)

    def _save_song_settings(self):
        if not self.path or not self.score:
            return
        old = self.all_settings.get(_norm(self.path), {})
        entry = {
            "tempo": int(self.tempo_scale * 100),
            "metro": self.metro_var.get(),
            "metro_unit": self.metro_unit_var.get(),   # 겹박자 박 단위: "dq" 점4분 / "e" 8분
            "countin": self.countin_var.get(),              # 예비박 마디 수(0~2)
            "countin_loop": self.countin_loop_var.get(),
            "loop_history": list(self.loop_history),
            "loop_tempo": self._loop_tempo_pct(),
            "loop": self.loop_var.get(),
            "loop_a": self.loop_a_var.get(),
            "loop_b": self.loop_b_var.get(),
            "parts_on": [bool(v.get()) for v in self.part_vars],
            # 열기 기록(히스토리): 마지막으로 연 때·마지막 위치 — 다시 열면 거기서 이어 보기
            "opened_at": old.get("opened_at", ""),
            "last_nominal": old.get("last_nominal", 0.0),
            "last_measure": old.get("last_measure", ""),
        }
        if self.rendered is not None:
            sec = self._current_sec() if self.playing else self._cursor_sec()
            entry["last_nominal"] = round(sec * self.rendered.tempo_scale, 3)
            k = self._played_index_at(sec)
            orig = self.rendered.played["measures"][k].get("orig_index", k)
            entry["last_measure"] = str(self.score["measures"][orig]["number"])
        self.all_settings[_norm(self.path)] = entry
        _save_json(SETTINGS_PATH, self.all_settings)

    # ------------------------------------------------------------ 합성

    def _start_render(self):
        """지금 빠르기·음색으로 다시 합성(작업 스레드). 재생 중이었으면 끝난 뒤 이어서."""
        if self.score is None:
            return
        if self.playing:
            self._resume_after_render = True
            self.stop()
        self._render_gen += 1
        gen = self._render_gen
        scale = self.tempo_scale
        self._render_scale = scale       # 이 빠르기로 합성 중(_apply_tempo가 같은 합성을 또 시작하지 않게)
        score = self.score
        settings = self.settings
        self._set_controls(False)
        self._set_status("소리 합성 중…")
        self._show_progress(0.0)

        last = [0.0]

        def progress(f):
            if f - last[0] >= 0.02 or f >= 1.0:
                last[0] = f
                self._post(self._on_render_progress, gen, f)

        compound_q = self._compound_q()

        def work():
            try:
                r = RenderedScore(score, scale, settings, progress=progress,
                                  cancel=lambda: gen != self._render_gen, compound_q=compound_q)
            except Exception as e:
                self._post(self._on_render_error, gen, e)
                return
            if not r.cancelled:
                self._post(self._on_rendered, gen, r)

        threading.Thread(target=work, daemon=True).start()

    def _on_render_progress(self, gen, frac):
        if gen == self._render_gen:
            self._show_progress(frac)

    def _on_render_error(self, gen, err):
        if gen != self._render_gen:
            return
        self._render_scale = None
        _log_error("합성", err)
        self._show_progress(None)
        self._set_status("합성 실패")
        messagebox.showerror(APP_TITLE, f"소리를 만들지 못했습니다:\n{err}")

    def _on_rendered(self, gen, r):
        if gen != self._render_gen:
            return
        self._render_scale = None
        r.settings = self.settings  # WAV 저장도 지금 믹서 설정을 따르게
        self.rendered = r
        self._show_progress(None)
        self._set_controls(True)
        self._seg_key = None  # 곡을 다시 합성하면 구간 합성도 새로
        self._refresh_tempo_pill()
        n_parts = len(self.score["parts"])
        self._set_status(f"{os.path.basename(self.path)} · {n_parts}성부 · {len(self.score['measures'])}마디 · "
                         f"음 {len(r.events)}개 · {_fmt_time(r.length_sec)}")
        self._update_cursor_display(self._cursor_sec())
        self._draw_loop_marks()
        if getattr(self, "_resume_measure", ""):
            # 처음 연 뒤 한 번: 지난번 멈춘 마디가 있는 쪽으로
            self._show_cursor_page()
            self._update_cursor_display(self._cursor_sec())
            self._set_status(self.status_var.get() + f" · 지난번 위치 마디 {self._resume_measure}에서 이어 보기")
            self._resume_measure = ""
        if self._resume_after_render:
            self._resume_after_render = False
            self.play()

    def _on_tempo_change(self):
        if self.playing and self._live is not None:
            # 재생 중 — 다시 시작하지 않고 앞으로 나올 음부터 새 빠르기로(render.LivePlayback). 곡 전체 다시
            # 합성은 멈춘 뒤에(stop) — 재생 중에 하면 CPU를 다퉈 소리가 끊길 수 있다.
            self._live.set_tempo(self.tempo_scale)
            self._schedule_save()
            return
        self._debounce("tempo", TEMPO_DEBOUNCE_MS, self._apply_tempo)

    def _apply_tempo(self):
        if self.rendered is not None and abs(self.rendered.tempo_scale - self.tempo_scale) < 1e-9:
            return
        if getattr(self, "_render_scale", None) == self.tempo_scale and not self.playing:
            return      # 이미 이 빠르기로 합성 중
        if self.playing and self._live is not None:
            self._live.set_tempo(self.tempo_scale)
            return
        if self.playing:
            self.cursor_nominal = self._current_sec() * self.rendered.tempo_scale
        self._schedule_save()
        self._start_render()

    # ------------------------------------------------------------ 재생

    # ------------------------------------------------------------ 소리 지연 보정(출력 장치별)

    @staticmethod
    def _output_device():
        """(기본 출력 장치 이름, 블루투스인가). 블루투스 여부는 같은 이름이 붙은 WDM-KS 장치 경로에
        블루투스 오디오 드라이버(btha2dp/bthhfenum)가 보이는지로 짐작한다."""
        try:
            import sounddevice as sd
            name = output_device_name()
            key = name[name.find("(") + 1:name.rfind(")")] if "(" in name and ")" in name else name
            bt = any(("btha2dp" in d["name"].lower() or "bthhfenum" in d["name"].lower()) and key in d["name"]
                     for d in sd.query_devices())
            return name, bt
        except Exception:
            return "", False

    def _load_device_delay(self):
        name, bt = self._output_device()
        saved = self.all_settings.get("_audio_delay_ms", {})
        if name in saved:
            return int(saved[name])
        return BLUETOOTH_DELAY_MS if bt else 0

    def _save_device_delay(self):
        name, _bt = self._output_device()
        if not name:
            return
        self.all_settings.setdefault("_audio_delay_ms", {})[name] = int(self._delay_sec() * 1000)
        _save_json(SETTINGS_PATH, self.all_settings)

    def _delay_sec(self):
        try:
            return max(DELAY_MIN_MS, min(DELAY_MAX_MS, int(self.delay_var.get()))) / 1000.0
        except (tk.TclError, ValueError, AttributeError):
            return 0.0

    def _cursor_sec(self):
        """재생 시작 위치(지금 빠르기 기준 초)."""
        scale = self.rendered.tempo_scale if self.rendered else self.tempo_scale
        return self.cursor_nominal / scale

    def _loop_range(self):
        """반복 구간 (시작 초, 끝 초) — 펼친 연주 순서에서 시작 마디의 첫 연주, 그 뒤
        처음 나오는 끝 마디까지. 없거나 순서가 거꾸로면 None."""
        r = self.rendered
        if r is None:
            return None
        numbers = [str(m["number"]) for m in self.score["measures"]]
        try:
            a = numbers.index(self.loop_a_var.get())
            b = numbers.index(self.loop_b_var.get())
        except ValueError:
            return None
        played = r.played["measures"]
        ka = next((k for k, m in enumerate(played) if m.get("orig_index", k) == a), None)
        if ka is None:
            return None
        kb = next((k for k in range(ka, len(played)) if played[k].get("orig_index", k) == b), None)
        if kb is None:
            return None
        return float(r.measure_starts[ka]), float(r.measure_ends[kb])

    def _loop_indices(self):
        """반복 구간의 펼친 마디 번호 (ka, kb) — _loop_range와 같은 규칙. 없으면 None."""
        rng = self._loop_range()
        if rng is None:
            return None
        r = self.rendered
        ka = self._played_index_at(rng[0])
        kb = max(ka, int(np.searchsorted(r.measure_ends, rng[1] - 1e-6, side="left")))
        return ka, min(kb, len(r.measure_ends) - 1)

    def _segment(self, ka, kb, pct):
        """반복 구간 합성(같은 구간·빠르기·음색이면 다시 쓰기)."""
        key = (id(self.rendered), ka, kb, pct, tuple(st.timbre for st in self.settings), self._compound_q())
        if getattr(self, "_seg_key", None) != key:
            if getattr(self, "_seg_synth", None) is None:
                self._seg_synth = NoteSynth()  # 캐시가 쌓여 두 번째부터 더 빠름
            r = self.rendered
            self._seg = SegmentRender(self.score, r.played, self.settings, pct / 100.0, ka, kb, r.gain,
                                      r.sr, self._seg_synth, compound_q=self._compound_q())
            self._seg_key = key
        return self._seg

    def _played_index_at(self, sec):
        r = self.rendered
        k = int(np.searchsorted(r.measure_starts, sec + 1e-6, side="right")) - 1
        return max(0, min(len(r.measure_starts) - 1, k))

    def _key_play(self, e):
        # 스핀박스·콤보에 글자를 치는 중이면 스페이스를 가로채지 않는다
        if isinstance(e.widget, (ttk.Entry, tk.Entry, ttk.Spinbox, ttk.Combobox)):
            return
        self.toggle_play()
        return "break"

    def toggle_play(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        r = self.rendered
        if r is None:
            return
        self._refresh_output_device()
        start = self._cursor_sec()
        if start >= r.length_sec - 0.05:
            start = 0.0
        loop = self._loop_range() if self.loop_var.get() else None
        if loop is not None and not (loop[0] <= start < loop[1]):
            start = loop[0]

        self._seg_active = None
        self._live = None
        if loop:
            # 구간 반복 — 구간만 "구간 빠르기"로 합성해 처음부터 끝까지 돌린다
            ka, kb = self._loop_indices()
            seg = self._segment(ka, kb, self._loop_tempo_pct())
            first = (start * r.tempo_scale - seg.start_nominal) / seg.tempo_scale
            first = min(max(0.0, first), seg.length_sec - 0.05)
            n_in = self._countin_measures(self.countin_loop_var)
            loop_prefix = self._count_in_at(loop[0], seg.tempo_scale, n_in) if n_in else None
            prefix = loop_prefix
            chunks = iter_mix(seg, self._live_settings, self._live_click, seg.frame_of(first), None, loop=True,
                              prefix=prefix, loop_from=0, loop_prefix=loop_prefix)
            self._seg_active = seg
            self._seg_first = first
        else:
            # 예비박: 그냥 재생이면 "재생 예비박"(시작 전 한 번), 구간 반복이면 "반복 예비박"(매 바퀴 전)
            loop_prefix = None
            n_in = self._countin_measures(self.countin_var)
            prefix = self._count_in_at(start, None, n_in) if n_in else None
            # 재생 중 빠르기를 바꾸면 앞으로 나올 음부터 새 빠르기로 이어 합성(render.LivePlayback)
            self._live = LivePlayback(r, self._live_settings, self._live_click, r.frame_of(start), prefix)
            if abs(r.tempo_scale - self.tempo_scale) > 1e-9:
                self._live.set_tempo(self.tempo_scale)   # 곡 전체 다시 합성이 아직이면 곧 새 빠르기로 갈아탐
            chunks = self._live.chunks()
        self.player.play(chunks, r.sr, 2)
        self.playing = True
        _set_keep_awake(True)
        self._awake_checked = time.time()
        self._play_start_sec = start
        self._play_start_nominal = start * r.tempo_scale
        self._prefix_sec = len(prefix) / r.sr if prefix is not None else 0.0
        self._loop_prefix_sec = len(loop_prefix) / r.sr if loop_prefix is not None else 0.0
        self._loop_secs = loop
        self.play_btn.set_icon("pause")

    @staticmethod
    def _countin_measures(var):
        try:
            return max(0, min(COUNTIN_MAX_MEASURES, int(var.get())))
        except (tk.TclError, ValueError):
            return 0

    def _count_in_at(self, sec, tempo_scale=None, measures=1):
        """sec가 든 마디 길이 × measures(0~2마디)만큼 4분음표마다 딸깍(마디 첫 박 높게) — 예비박 버퍼.
        tempo_scale을 주면 그 빠르기로(구간 빠르기)."""
        r = self.rendered
        k = self._played_index_at(sec)
        m = r.played["measures"][k]
        q = m["start_q"]
        scale = tempo_scale or r.tempo_scale
        quarter_sec = float(nominal_seconds(r.played, q + 1.0) - nominal_seconds(r.played, q)) / scale
        if is_compound(m):
            # 겹박자는 고른 박 단위(점4분음표 또는 8분음표)로 — 한 마디 길이 ÷ 박 길이
            unit = click_unit_q(m, self._compound_q())
            n = max(1, int(round(m["length_q"] / unit)))
            return count_in(n * max(1, measures), quarter_sec * unit, r.sr, per_measure=n)
        n = max(1, int(round(m["beats"] * 4.0 / m["beat_type"])))
        return count_in(n * max(1, measures), quarter_sec, r.sr, per_measure=n)

    def _live_settings(self):
        """재생 조각마다 불림 — "메트로놈만"이면 모든 성부를 끈 설정(메트로놈만 들리게)."""
        if self.metro_var.get() == "only":
            if getattr(self, "_muted_settings_src", None) is not self.settings:
                self._muted_settings_src = self.settings
                self._muted_settings = [PartSetting(timbre=s.timbre, volume=s.volume, pan=s.pan, mute=True)
                                        for s in self.settings]
            return self._muted_settings
        return self.settings

    def _compound_q(self):
        return COMPOUND_EIGHTH if self.metro_unit_var.get() == "e" else COMPOUND_DOTTED_QUARTER

    def _on_metro_unit(self):
        """박 단위 바꿈 — 클릭만 다시 만들고(곡 재합성 없음), 재생 중이면 새 클릭으로 이어서 재생."""
        self._schedule_save()
        if self.rendered is not None:
            self.rendered.set_compound_q(self._compound_q())
            self._seg_key = None
            self._restart_if_playing()

    def _live_click(self):
        return CLICK_VOLUME if self.metro_var.get() != "off" else 0.0

    def _on_countin_loop(self):
        self._schedule_save()
        if self.playing and self._loop_secs is not None:
            # 반복 중이면 지금 위치에서 새 설정으로 다시(예비박 마디 수가 바뀜)
            self.cursor_nominal = self._current_sec() * self.rendered.tempo_scale
            self.stop()
            self.play()

    def _refresh_output_device(self):
        """재생 시작 전 — 장치 목록을 새로 읽어 지금 기본 출력 장치로 재생하고, 장치가 바뀌었으면 그
        장치의 소리 지연 보정 값을 쓴다(블루투스 ↔ 노트북 스피커, 2026-10-07)."""
        refresh_devices()
        name, _bt = self._output_device()
        if name and name != getattr(self, "_device_name", None):
            if getattr(self, "_device_name", None) is not None:
                self.delay_var.set(self._load_device_delay())
            self._device_name = name

    def _recover_lost_device(self):
        """재생 중 출력이 끊김(장치 변경·블루투스 끊김) — 그 자리에서 새 기본 장치로 이어서 재생.
        짧은 시간에 거듭 실패하면(새 장치도 안 됨) 멈춘다."""
        now = time.time()
        tries = [t for t in getattr(self, "_recover_times", []) if now - t < 15]
        pos = getattr(self, "_last_pos", self._play_start_sec)
        self.stop()
        self.cursor_nominal = pos * self.rendered.tempo_scale
        if len(tries) >= 3:
            self._set_status("출력 장치에 연결할 수 없어 재생을 멈췄습니다. 장치를 확인한 뒤 다시 재생하세요.")
            return
        self._recover_times = tries + [now]
        self.play()
        name, _bt = self._output_device()
        self._set_status(f"출력 장치가 바뀌어 이어서 재생합니다 ({name})")

    def stop(self):
        _set_keep_awake(False)
        self.player.stop()
        self.playing = False
        self._live = None
        self.play_btn.set_icon("play")
        if self.rendered is not None and abs(self.rendered.tempo_scale - self.tempo_scale) > 1e-9:
            # 재생 중 바꾼 빠르기 — 이제 곡 전체를 새 빠르기로(바로 다시 재생하면 _apply_tempo가 그냥 넘어감)
            self._debounce("tempo", TEMPO_DEBOUNCE_MS, self._apply_tempo)

    def pause(self):
        """멈추고 그 자리를 다음 재생 시작 위치로."""
        if self.playing and self.rendered:
            self.cursor_nominal = self._current_sec() * self.rendered.tempo_scale
        self.stop()

    def go_home(self):
        was = self.playing
        self.stop()
        loop = self._loop_range() if self.loop_var.get() else None
        self.cursor_nominal = (loop[0] * self.rendered.tempo_scale) if (loop and self.rendered) else 0.0
        if self.rendered:
            self._show_cursor_page()
            self._update_cursor_display(self._cursor_sec())
        if was:
            self.play()

    def _show_cursor_page(self):
        """멈춰 있을 때 커서(다음 재생 시작 위치) 마디가 있는 펼침면으로 — 재생 중엔 "따라가기"가
        알아서 넘기지만, 멈춰 있을 땐 커서만 옮기고 쪽은 그대로여서 "처음"을 눌러도 첫 쪽으로
        안 갔다(사용자 지적 2026-10-07)."""
        if not (self.rendered and self.boxes):
            return
        k = self._played_index_at(self._cursor_sec())
        orig = self.rendered.played["measures"][k].get("orig_index", k)
        page = self.boxes[orig][0]
        if page not in self._visible_pages():
            self.show_page(page)

    def seek(self, sec):
        """곡 안 위치(지금 빠르기 기준 초)로 이동. 재생 중이면 거기서 다시 재생."""
        if self.rendered is None:
            return
        was = self.playing
        self.stop()
        self.cursor_nominal = sec * self.rendered.tempo_scale
        self._update_cursor_display(sec)
        if was:
            self.play()

    def _current_sec(self):
        """지금 들리는 곳(곡 안 초). 예비박 중엔 시작 위치. 장치가 알려 주지 않는 지연(블루투스
        등)은 사용자가 정한 보정값(_delay_sec)만큼 더 뺀다."""
        live = self._live
        if live is not None:
            # 곡 전체 합성 → 이어 합성으로 갈아탔으면 시각이 달라지므로 LivePlayback이 곡 위치(명목 초)를 알려 줌
            nominal = live.nominal_at(max(0.0, self.player.played_sec() - self._delay_sec()))
            return min(nominal / self.rendered.tempo_scale, self.rendered.length_sec)
        t = self.player.played_sec() - self._prefix_sec - self._delay_sec()
        seg = getattr(self, "_seg_active", None)
        if seg is not None:
            # 구간 합성 재생: 구간 안 시각 → 곡 위치(곡 빠르기 기준 초)
            if t <= 0:
                s_ = self._seg_first
            else:
                s_ = self._seg_first + t
                if s_ >= seg.length_sec:
                    lp = getattr(self, "_loop_prefix_sec", 0.0)
                    r_ = (s_ - seg.length_sec) % (lp + seg.length_sec)
                    s_ = 0.0 if r_ < lp else r_ - lp
            return seg.song_nominal(min(s_, seg.length_sec - 1e-3)) / self.rendered.tempo_scale
        if t <= 0:
            return self._play_start_sec
        pos = self._play_start_sec + t
        if self._loop_secs is not None:
            a, b = self._loop_secs
            if pos >= b and b > a:
                # 첫 바퀴 뒤로는 바퀴마다 [반복 예비박 + 구간]이 돈다 — 예비박 중엔 구간 시작에 머문다
                lp = getattr(self, "_loop_prefix_sec", 0.0)
                r_ = (pos - b) % (lp + (b - a))
                pos = a if r_ < lp else a + (r_ - lp)
        return pos

    def _on_loop_chip(self):
        """"구간 반복" 칩을 누름 — 켜지는 순간부터 그 구간이 바로 재생된다(사용자 요청 2026-10-13).
        이미 재생 중이면 _on_loop_change가 새 설정으로 다시 재생."""
        self._on_loop_change()
        if self.loop_var.get() and not self.playing and self.rendered is not None:
            self.play()

    def _on_loop_change(self):
        self._schedule_save()
        self._draw_loop_marks()
        if self.playing:
            # 구간이 바뀌면 지금 위치에서 새 구간 설정으로 다시 재생
            self.cursor_nominal = self._current_sec() * self.rendered.tempo_scale
            self.stop()
            self.play()

    # ------------------------------------------------------------ 주기 처리

    def _tick(self):
        try:
            while True:
                fn, args = self._ui_q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        if self.playing and self.rendered is not None:
            if self.player.lost_device():
                self._recover_lost_device()
            elif self.player.error is not None or not self.player.is_playing():
                # 끝까지 재생함 — 시작했던 자리로 돌아간다(연습할 때 같은 곳부터 다시 듣기 쉽게)
                self.stop()
                self.cursor_nominal = self._play_start_nominal
                self._update_cursor_display(self._play_start_nominal / self.rendered.tempo_scale)
            else:
                self._last_pos = self._current_sec()
                self._update_cursor_display(self._last_pos)
                if time.time() - getattr(self, "_awake_checked", 0) > 60:
                    # 재생 중 배터리가 떨어지면 꺼짐 방지를 푼다(1분마다 확인)
                    self._awake_checked = time.time()
                    _set_keep_awake(True)
        self.after(TICK_MS, self._tick)

    # ------------------------------------------------------------ 악보 표시(두 쪽 나란히)
    # 왼쪽 쪽 번호(self.page, 늘 홀수)와 그 다음 쪽을 나란히 보여준다. 두 쪽 모두 캔버스
    # 높이 안에 통째로 들어가도록 너비를 정한다(아래가 가려지지 않게, 사용자 요청 2026-10-07)
    # — 쪽마다 높이가 달라서(마지막 쪽 등) 두 쪽 중 더 긴 쪽 기준으로 같은 너비를 쓴다.

    def show_page(self, page):
        if self.renderer is None:
            return
        page = max(1, min(self.renderer.page_count, page))
        self.page = page - (page - 1) % 2   # 펼침면의 왼쪽(홀수 쪽)
        self._redraw_page()

    def _visible_pages(self):
        n = self.renderer.page_count if self.renderer else 0
        return [p for p in (self.page, self.page + 1) if 1 <= p <= n]

    def _slot_size(self, single=None):
        """두 쪽 보기의 한 쪽 칸 크기(px). 줄이 적은 동요 모드(single_page)는 한 쪽만 쓰므로 창 너비를 다 준다."""
        cw = max(400, self.canvas.winfo_width())
        ch = max(300, self.canvas.winfo_height())
        if single is None:
            single = bool(self._sysset is not None and self._sysset.single_page)
        if single:
            return (int(cw - 2 * SCORE_MARGIN), int(ch - 2 * SCORE_MARGIN))
        return (int((cw - 2 * SCORE_MARGIN - PAGE_GAP) / 2), int(ch - 2 * SCORE_MARGIN))

    def _spread_width(self, left=None):
        """펼침면(left, left+1) 쪽 그림 너비(px) — 칸 안에 다 들어가게 너비·높이 중 빡빡한
        쪽에 맞춘다(유난히 큰 줄이 있는 펼침면은 쪽이 세로로 길어서 줄여 그려짐)."""
        left = self.page if left is None else left - (left - 1) % 2
        slot_w, slot_h = self._slot_size()
        return int(max(150, min(slot_w, slot_h / self.renderer.page_ratio(left))))

    def _page_origin(self, page):
        """보이는 쪽의 왼쪽 위 캔버스 좌표(안 보이면 None). 두 쪽을 가운데 정렬."""
        if page not in self._visible_pages():
            return None
        w = self._spread_width()
        if self._sysset is not None and self._sysset.single_page:      # 한 쪽만 — 가운데
            return (max(SCORE_MARGIN, (self.canvas.winfo_width() - w) / 2), SCORE_MARGIN)
        total = 2 * w + PAGE_GAP
        x0 = max(SCORE_MARGIN, (self.canvas.winfo_width() - total) / 2)
        return (x0 + (0 if page == self.page else w + PAGE_GAP), SCORE_MARGIN)

    def _on_resize(self):
        """창 크기가 바뀜 — 줄 단위 조판은 그대로 두고 새 칸에 맞춰 쪽만 다시 짠다(순식간).
        쪽당 줄 수가 바뀌면 지금 보던(재생 중이면 연주 중인) 마디가 있는 쪽으로 간다."""
        if self.renderer is None or self._sysset is None:
            self._show_start_screen()
            return
        slot = self._slot_size()
        if slot != self.renderer.slot:
            keep = None
            if self.rendered is not None and self.boxes:
                sec = self._current_sec() if self.playing else self._cursor_sec()
                k = self._played_index_at(sec)
                keep = self.rendered.played["measures"][k].get("orig_index", k)
            renderer, boxes = self._sysset.arrange(*slot)
            self.renderer = renderer
            self.boxes = boxes if len(boxes) == len(self.score["measures"]) else []
            self._shown_page = None
            # 줄 그림(PNG)은 줄 번호·너비로 캐시돼 있어 너비가 같으면 그대로 다시 쓴다
            page = self.boxes[keep][0] if keep is not None and self.boxes else self.page
            self.show_page(page)
            return
        self._redraw_page()

    def _get_pool(self):
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=RASTER_WORKERS)
        return self._pool

    def _request_pages(self):
        """보이는 쪽의 줄부터 시작해 모든 줄을 지금 너비로 그려 달라고 프로세스에 맡긴다.
        이미 있거나 맡긴 건 건너뛰고, 너비가 바뀐(창 크기 변경) 옛 그림은 버린다."""
        widths = {i: self._spread_width(page) for i, (_svg, page, _y) in enumerate(self.renderer.systems)}
        for key in [k for k in self._png_cache if widths.get(k[0]) != k[1]]:
            del self._png_cache[key]
        n = len(self.renderer.systems)
        first = (self.renderer.page_systems(self.page) or [0])[0]
        gen = self._score_gen
        for i in [(first + j) % n for j in range(n)]:
            key = (i, widths[i])
            if key in self._png_cache or key in self._png_pending:
                continue
            self._png_pending.add(key)
            fut = self._get_pool().submit(rasterize_svg, self.renderer.systems[i][0], widths[i])
            fut.add_done_callback(lambda f, key=key: self._post(self._on_png, gen, key, f))

    def _visible_systems(self):
        return [i for p in self._visible_pages() for i in self.renderer.page_systems(p)]

    def _on_png(self, gen, key, fut):
        self._png_pending.discard(key)
        if gen != self._score_gen:
            return
        try:
            self._png_cache[key] = fut.result()
        except Exception as e:
            _log_error(f"악보 줄 그리기 {key}", e)
            self._set_status("악보를 그리지 못했습니다(generate_error.log 참고).")
            return
        if key[0] in self._visible_systems() and key[1] == self._spread_width():
            if all((i, key[1]) in self._png_cache for i in self._visible_systems()):
                self._redraw_page()

    def _on_canvas_wheel(self, e):
        """악보가 열려 있으면 쪽 넘김, 시작 화면(최근 악보 목록)이면 목록 스크롤 — 목록이 창보다 길면
        맨 아래 곡까지 올라오지 않던 문제(사용자 지적 2026-10-12)."""
        if self.renderer is None:
            self.canvas.yview_scroll(-3 if e.delta > 0 else 3, "units")
        else:
            self.show_page(self.page + (2 if e.delta < 0 else -2))

    def _reset_canvas_scroll(self):
        """악보 화면은 스크롤하지 않으므로 시작 화면에서 쓴 스크롤 영역·위치를 되돌림."""
        c = self.canvas
        c.configure(scrollregion=(0, 0, max(1, c.winfo_width()), max(1, c.winfo_height())))
        c.xview_moveto(0)
        c.yview_moveto(0)

    def _show_start_screen(self):
        """악보를 열기 전 화면 — 최근 악보 목록(곡 이름·폴더·마지막으로 연 때·마지막 위치).
        누르면 바로 연다(사용자 요청 2026-10-07: 문서를 열 때 히스토리)."""
        if self.renderer is not None:
            return
        c = self.canvas
        c.delete("all")
        u = self.winfo_fpixels("1i") / 96.0   # 화면 배율 — 글자는 pt라 배율만큼 커지므로 간격도 같이 키움
        cw = max(400, c.winfo_width())
        box_w = int(760 * u)
        x0 = max(int(40 * u), (cw - box_w) // 2)
        y = int(60 * u)
        c.create_text(x0, y, text="최근 악보", anchor="nw", font=(W.TITLE_FONT, 18, "bold"), fill=W.TEXT)
        y += int(56 * u)
        if not self.history:
            c.create_text(x0, y, text="아직 연 악보가 없습니다. 위의 \"악보 열기…\"로 MusicXML 파일을 여세요.",
                          anchor="nw", font=("Malgun Gothic", 11), fill="#555555")
            bottom = self._draw_start_footer(x0, box_w, y + int(60 * u), u)
            self._set_start_scroll(bottom + int(40 * u))
            return
        for i, path in enumerate(self.history):
            saved = self.all_settings.get(_norm(path), {})
            folder = os.path.basename(os.path.dirname(path))
            parent = os.path.basename(os.path.dirname(os.path.dirname(path)))
            when = _fmt_when(saved.get("opened_at", ""))
            meta = " · ".join(t for t in (f"{parent} / {folder}" if parent else folder, when,
                                           f"마디 {saved['last_measure']}에서 멈춤" if saved.get("last_measure") else "",
                                           f"빠르기 {saved['tempo']}%" if saved.get("tempo", 100) != 100 else "") if t)
            tag = f"recent{i}"
            missing = not os.path.exists(path)
            c.create_rectangle(x0, y, x0 + box_w, y + int(64 * u), fill="#FFFFFF", outline=W.BORDER,
                               tags=(tag, "recent"))
            c.create_text(x0 + int(18 * u), y + int(10 * u), text=os.path.splitext(os.path.basename(path))[0],
                          anchor="nw", font=("Malgun Gothic", 12, "bold"), fill="#999999" if missing else "#1E1E1E",
                          tags=(tag, "recent"))
            c.create_text(x0 + int(18 * u), y + int(38 * u), text=meta + (" · 파일 없음" if missing else ""),
                          anchor="nw", font=("Malgun Gothic", 9), fill="#666666", tags=(tag, "recent"))
            c.tag_bind(tag, "<Button-1>", lambda e, p=path: self.open_path(p))
            c.tag_bind(tag, "<Enter>", lambda e: self.canvas.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: self.canvas.configure(cursor=""))
            y += int(74 * u)
        c.create_text(x0, y + int(16 * u), anchor="nw", fill=W.MUTED, font=(W.FONT, 9),
                      text="악보에서 마디를 누르면 거기서 재생 · 오른쪽 클릭으로 반복 구간 지정 · 스페이스: 재생/멈춤")
        # 목록이 창보다 길면 휠로 스크롤(맨 아래 푸터까지 보이게 여백 포함). 짧으면 스크롤 안 됨.
        bottom = self._draw_start_footer(x0, box_w, y + int(56 * u), u)
        self._set_start_scroll(bottom + int(40 * u))

    def _draw_start_footer(self, x0, box_w, y, u):
        """시작 화면 맨 아래 푸터 — 알함브라 로고 + "알함브라 기타 앙상블"(사용자 요청 2026-10-12, 안드로이드판과 같은
        자리·모양). 아래 y를 돌려준다."""
        c = self.canvas
        cx = x0 + box_w // 2
        c.create_line(x0, y, x0 + box_w, y, fill=W.BORDER)
        y += int(26 * u)
        # 작은 로고를 "알함브라 기타 앙상블" 글자 앞에 한 줄로(사용자 요청 2026-10-12), 둘을 합쳐 가운데 정렬
        font = tkfont.Font(family=W.FONT, size=11, weight="bold")
        text_w = font.measure("알함브라 기타 앙상블")
        h = int(34 * u)
        logo_w, gap = 0, int(10 * u)
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            im = Image.open(os.path.join(base, "assets", "alhambra_logo.png")).convert("RGBA")
            im = im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS)
            self._start_logo = ImageTk.PhotoImage(im)
            logo_w = im.width
        except Exception:
            self._start_logo = None   # 로고 파일이 없으면 글자만
        left = cx - (logo_w + (gap if logo_w else 0) + text_w) // 2
        if self._start_logo is not None:
            c.create_image(left, y, image=self._start_logo, anchor="w")
            left += logo_w + gap
        c.create_text(left, y, text="알함브라 기타 앙상블", anchor="w", font=font, fill=W.MUTED)
        return y + h // 2 + int(30 * u)

    def _set_start_scroll(self, content_h):
        c = self.canvas
        h = max(1, c.winfo_height())
        c.configure(scrollregion=(0, 0, max(1, c.winfo_width()), max(h, content_h)))

    def _redraw_page(self):
        if self.renderer is None:
            self._show_start_screen()
            return
        self._request_pages()
        vis = self._visible_pages()
        w = self._spread_width()
        n = self.renderer.page_count
        self.page_var.set(f"{vis[0]}–{vis[-1]} / {n}쪽" if len(vis) > 1 else f"{vis[0]} / {n}쪽")
        systems = self._visible_systems()
        if any((i, w) not in self._png_cache for i in systems):
            # 아직 그리는 중 — 같은 펼침면이 떠 있으면 그대로 두고(창 크기만 바뀐 경우),
            # 다른 펼침면이면 안내만 띄운다. 다 그려지면 _on_png가 다시 부른다.
            if getattr(self, "_shown_page", None) != self.page:
                self.canvas.delete("all")
                self.canvas.create_text(40, 40, text="악보 그리는 중…", anchor="nw", fill="#555555")
                self._shown_page = None
            return
        self._shown_page = self.page
        self._photos = []
        self._sys_items = {}
        self._reset_canvas_scroll()
        self.canvas.delete("all")
        for p in vis:
            ox, oy = self._page_origin(p)
            # 쪽 바탕(흰 종이 + 옅은 그림자) — 줄 그림들 사이 빈 곳도 종이처럼 보이게
            ph = w * self.renderer.page_ratio(p)
            self.canvas.create_rectangle(ox + 2, oy + 4, ox + w + 2, oy + ph + 4, fill="#D9D3C8", outline="",
                                         tags="page")
            self.canvas.create_rectangle(ox, oy, ox + w, oy + ph, fill="#FFFFFF", outline="", tags="page")
            for i in self.renderer.page_systems(p):
                img = self._tint_chords(png_to_image(self._png_cache[(i, w)]), p, i, w)
                photo = ImageTk.PhotoImage(img)
                self._photos.append(photo)
                item = self.canvas.create_image(ox, oy + self.renderer.systems[i][2] * w, image=photo,
                                                anchor="nw", tags="page")
                self._sys_items[i] = (item, self.renderer.systems[i][2] * w, img.height, len(self._photos) - 1, p)
        self._shown_measure = None
        self._draw_loop_marks()
        if self.rendered is not None:
            self._update_cursor_display(self._current_sec() if self.playing else self._cursor_sec(), force=True)

    def _chord_span_x(self, k, j, x0, x1, nx, hx):
        """마디 k의 코드 구간 j의 [왼쪽 x, 오른쪽 x](px). 코드 이름 글자의 실제 위치(hx, 쪽 너비 대비 비율)가 구간 수와
        맞으면 그 위치로 가르고, 아니면 마디 시간 비율로 근사한다."""
        segs = self._chord_map[k]
        w = self._spread_width()
        if hx and len(hx) == len(segs):
            starts = [x0] + [hx_ * w - 4 for hx_ in hx[1:]]          # 글자는 음표 머리 왼쪽 가장자리 — 조금 앞에서
        else:
            starts = [x0] + [nx + s[0] * (x1 - nx) for s in segs[1:]]
        ends = starts[1:] + [x1]
        return starts[j], ends[j]

    def _tint_chords(self, img, page, system, w):
        """화성을 붙인 악보면 재생 커서가 지나간 코드 구간에 코드별 옅은 바탕색을 곱해 칠한다(음표는 검게 남음)."""
        if self._round_map and self.boxes:
            return self._tint_round(img, page, system, w)
        if not (self._chord_map and self.boxes):
            return img
        top = self.renderer.systems[system][2] * w
        for k, b in enumerate(self.boxes):
            if b[0] != page or not (top <= (b[2] + b[4]) / 2 * w < top + img.height):
                continue
            x0, x1 = b[1] * w, b[3] * w
            nx = b[5] * w if len(b) > 5 else x0
            hx = b[6] if len(b) > 6 else ()
            for j, (f0, f1, color, _name) in enumerate(self._chord_map[k]):
                if (k, j) not in self._painted:
                    continue
                xa, xb = self._chord_span_x(k, j, x0, x1, nx, hx)
                xa, xb = max(0, int(xa)), min(img.width, int(xb))
                if xb <= xa:
                    continue
                for ya, yb in self._chord_rows(k, b, w, top, img.height):
                    region = img.crop((xa, ya, xb, yb))
                    img.paste(ImageChops.multiply(region, Image.new("RGB", region.size, color)), (xa, ya))
                    if f0 > 0:      # 한 마디 안 코드 경계 — 색이 비슷해도(V·V7 등) 갈리도록 얇은 진한 선
                        ImageDraw.Draw(img).line([(xa, ya), (xa, yb)], fill=_strong_color(color), width=2)
        return img

    def _chord_rows(self, k, box, w, top, img_h):
        """마디 k에서 코드 색을 칠할 세로 구간 [(위, 아래)](줄 그림 안 px). 보통은 줄 전체. 돌림노래면 그 마디에서 이미 노래를
        시작한 성부의 보표 띠만(아직 쉬는 성부는 칠하지 않음)."""
        if not self._round_entries:
            return [(0, img_h)]
        bands = box[7] if len(box) > 7 else ()
        rows = []
        for staff0, n_staves, entry, _name in self._round_entries:
            if entry > k or staff0 >= len(bands):
                continue
            last = min(len(bands), staff0 + n_staves) - 1
            ya = max(0, int(bands[staff0][0] * w - top))
            yb = min(img_h, int(bands[last][1] * w - top))
            if yb > ya:
                rows.append((ya, yb))
        return rows

    def _tint_round(self, img, page, system, w):
        """돌림노래 — 커서가 닿은 "성부 시작 마디"에서 그 성부 보표 띠(위아래 이웃 보표 사이 가운데까지)를 성부 색으로 곱해 칠한다."""
        top = self.renderer.systems[system][2] * w
        for k in self._round_painted:
            b = self.boxes[k]
            if b[0] != page or not (top <= (b[2] + b[4]) / 2 * w < top + img.height):
                continue
            bands = b[7] if len(b) > 7 else ()
            xa, xb = max(0, int(b[1] * w)), min(img.width, int(b[3] * w))
            for staff0, n_staves, color, _name in self._round_map.get(k, []):
                if staff0 >= len(bands) or xb <= xa:
                    continue
                last = min(len(bands), staff0 + n_staves) - 1
                ya = max(0, int(bands[staff0][0] * w - top))
                yb = min(img.height, int(bands[last][1] * w - top))
                if yb <= ya:
                    continue
                region = img.crop((xa, ya, xb, yb))
                img.paste(ImageChops.multiply(region, Image.new("RGB", region.size, color)), (xa, ya))
        return img

    def _retint_system_of(self, k, page):
        """마디 k가 있는 줄 그림만 다시 칠해 캔버스에 바꿔 끼운다(한 코드가 칠해질 때마다 — 쪽 전체를 다시 그리지 않음)."""
        w = self._spread_width()
        yc = (self.boxes[k][2] + self.boxes[k][4]) / 2 * w
        for i, (item, top, h, idx, p) in self._sys_items.items():
            if p == page and top <= yc < top + h and (i, w) in self._png_cache:
                photo = ImageTk.PhotoImage(self._tint_chords(png_to_image(self._png_cache[(i, w)]), p, i, w))
                self._photos[idx] = photo      # 참조를 유지해야 사라지지 않음
                self.canvas.itemconfigure(item, image=photo)
                return

    def _box_px(self, orig_index):
        """마디 상자의 캔버스 좌표 — 지금 펼침면에 안 보이면 좌표는 None."""
        page, x0, y0, x1, y1 = self.boxes[orig_index][:5]
        origin = self._page_origin(page)
        if origin is None:
            return page, None
        w = self._spread_width()
        ox, oy = origin
        pad = 6
        return page, (ox + x0 * w - 2, oy + y0 * w - pad, ox + x1 * w + 2, oy + y1 * w + pad)

    def _update_cursor_display(self, sec, force=False):
        """재생 위치 → 상태 표시줄 + 악보 위 현재 마디 상자·재생선."""
        r = self.rendered
        if r is None:
            return
        k = self._played_index_at(sec)
        pm = r.played["measures"][k]
        orig = pm.get("orig_index", k)
        occ = pm.get("occurrence", 1)
        m = self.score["measures"][orig]
        occ_text = f" ({occ}번째)" if occ > 1 else ""
        self.pos_var.set(f"마디 {m['number']}{occ_text}   {_fmt_time(sec)} / {_fmt_time(r.length_sec)}")
        self.measure_var.set(f"마디 {m['number']}")
        self.occ_var.set(f"{occ}번째 반복" if occ > 1 else "")
        self.time_var.set(_fmt_time(sec))
        self.total_var.set(_fmt_time(r.length_sec))
        self.timeline.set_state(pos=min(1.0, sec / max(1e-6, r.length_sec)))
        if not self.boxes:
            return
        page, box = self._box_px(orig)
        if box is None:
            if self.playing and self.follow_var.get():
                self.show_page(page)  # 그 쪽이 있는 펼침면으로 — 다시 그리면서 이 함수를 다시 부른다
            else:
                self.canvas.delete("cursor")
            return
        x0, y0, x1, y1 = box
        span = max(1e-6, r.measure_ends[k] - r.measure_starts[k])
        frac = min(1.0, max(0.0, (sec - r.measure_starts[k]) / span))
        # 재생선은 상자 왼쪽 끝이 아니라 그 마디 첫 음표에서 출발(줄 첫 마디의 음자리표·조표 구간을 건너뜀)
        b = self.boxes[orig]
        nx = x0 + 2
        if len(b) > 5:
            origin = self._page_origin(page)
            nx = origin[0] + b[5] * self._spread_width() if origin else nx
        x = nx + frac * (x1 - 2 - nx)
        self.canvas.delete("cursor")
        box_color, line_color = CURSOR_COLOR, PLAYHEAD_COLOR
        if self._round_map:
            if sec < self._last_cursor_sec - 0.3:       # 뒤로 감김 — 색을 지우고 다시 칠함
                self._last_cursor_sec = sec
                self._round_painted.clear()
                self._redraw_page()
                return
            self._last_cursor_sec = sec
            if self.playing and orig in self._round_map and orig not in self._round_painted:
                self._round_painted.add(orig)           # 성부가 노래를 시작하는 마디에 닿음 — 그 성부 색으로
                self._retint_system_of(orig, page)
        if self._chord_map:
            if sec < self._last_cursor_sec - 0.3 and not self._color_all:   # 뒤로 감김 — 색을 지우고 다시 칠함(처음부터 칠한 악보는 그대로)
                self._last_cursor_sec = sec
                self._painted.clear()
                self._redraw_page()
                return
            self._last_cursor_sec = sec
            seg = None
            if self.playing:
                # 지금 코드 구간 — 처음 닿으면 그 구간 바탕을 칠하고, 커서 상자·재생선도 그 코드의 진한 색으로
                idx = next((j for j, s in enumerate(self._chord_map[orig]) if s[0] - 1e-6 <= frac < s[1] + 1e-6), None)
                if idx is not None:
                    seg = self._chord_map[orig][idx]
                    box_color = line_color = _strong_color(seg[2])
                    if (orig, idx) not in self._painted:
                        self._painted.add((orig, idx))
                        self._retint_system_of(orig, page)
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=box_color, width=3, tags="cursor")
        # 재생선 — 잘 보이게 굵은 빨강(+흰 테두리). 사용자 요청 2026-10-25: 선을 굵게, 빨갛게
        self.canvas.create_line(x, y0, x, y1, fill="#FFFFFF", width=PLAYHEAD_WIDTH + 4, tags="cursor")
        self.canvas.create_line(x, y0, x, y1, fill=PLAYHEAD_COLOR, width=PLAYHEAD_WIDTH, tags="cursor")
        self._shown_measure = orig

    def _draw_loop_marks(self):
        self.canvas.delete("loop")
        self._refresh_tempo_pill()
        a, b = self.loop_a_var.get(), self.loop_b_var.get()
        self._loop_prev = (a, b)
        rng = self._loop_range() if (self.rendered is not None and self.loop_var.get()) else None
        if rng and self.rendered.length_sec > 0:
            self.timeline.set_state(loop=(rng[0] / self.rendered.length_sec, rng[1] / self.rendered.length_sec))
        else:
            self.timeline.set_state(loop=None)
        if not (self.boxes and self.score and self.loop_var.get()):
            return
        numbers = [str(m["number"]) for m in self.score["measures"]]
        try:
            a = numbers.index(self.loop_a_var.get())
            b = numbers.index(self.loop_b_var.get())
        except ValueError:
            return
        for i in range(a, b + 1):
            _page, box = self._box_px(i)
            if box is not None:
                x0, y0, x1, y1 = box
                # 옅은 주황 덧칠 + 밑줄 — 반복 구간이 한눈에 보이게
                self.canvas.create_rectangle(x0, y0, x1, y1, fill=LOOP_COLOR, stipple="gray12", outline="",
                                             tags="loop")
                self.canvas.create_line(x0, y1 + 4, x1, y1 + 4, fill=LOOP_COLOR, width=4, tags="loop")

    def _measure_at(self, event):
        """클릭한 곳의 인쇄 마디 번호(인덱스) — 없으면 None."""
        if not self.boxes:
            return None
        for i in range(len(self.boxes)):
            _page, box = self._box_px(i)
            if box is not None and box[0] <= event.x <= box[2] and box[1] - 10 <= event.y <= box[3] + 10:
                return i
        return None

    def _on_canvas_press(self, event):
        self.canvas.focus_set()
        self._drag = {"x": event.x, "y": event.y, "start": self._measure_at(event), "end": None, "moved": False}

    def _on_canvas_drag(self, event):
        d = getattr(self, "_drag", None)
        if not d or d["start"] is None or not self.boxes:
            return
        if not d["moved"] and abs(event.x - d["x"]) + abs(event.y - d["y"]) < DRAG_MIN_PX:
            return
        d["moved"] = True
        i = self._measure_at(event)
        if i is not None:
            d["end"] = i
        a, b = sorted((d["start"], d["end"] if d["end"] is not None else d["start"]))
        self._draw_range(a, b, tag="dragsel")
        self.canvas.configure(cursor="sb_h_double_arrow")

    def _on_canvas_release(self, event):
        d = getattr(self, "_drag", None)
        self._drag = None
        self.canvas.configure(cursor="")
        self.canvas.delete("dragsel")
        if not d:
            return
        if d["moved"] and d["start"] is not None:
            # 끌어서 고른 범위 → 반복 구간(켜고, 재생 중이면 새 구간으로 바로)
            a, b = sorted((d["start"], d["end"] if d["end"] is not None else d["start"]))
            numbers = [str(m["number"]) for m in self.score["measures"]]
            self.loop_a_var.set(numbers[a])
            self.loop_b_var.set(numbers[b])
            self.loop_var.set(True)
            self._on_loop_change()
            self._set_status(f"반복 구간: {numbers[a]}–{numbers[b]}마디 — 다시 끌면 바꾸고, "
                             f"아래 '구간 반복' 칩을 누르면 끕니다")
            return
        self._on_canvas_click(event)

    def _draw_range(self, a, b, tag):
        """마디 a~b(인쇄 순서 인덱스)를 주황 반투명으로 칠한다(보이는 쪽만)."""
        self.canvas.delete(tag)
        for i in range(a, b + 1):
            _page, box = self._box_px(i)
            if box is not None:
                self.canvas.create_rectangle(*box, fill=LOOP_COLOR, stipple="gray25", outline=LOOP_COLOR,
                                             width=2, tags=tag)

    def _on_canvas_click(self, event):
        i = self._measure_at(event)
        r = self.rendered
        if i is None or r is None:
            return
        self._seek_measure(i)

    def _seek_measure(self, i):
        """악보 순서 i번째 마디로 이동(재생 중이면 거기서 다시 재생 — 예비박 설정을 따름)."""
        r = self.rendered
        if r is None:
            return
        # 도돌이로 같은 마디가 여러 번 연주되면 지금 위치에서 가장 가까운 회차로
        now = self._current_sec() if self.playing else self._cursor_sec()
        ks = [k for k, m in enumerate(r.played["measures"]) if m.get("orig_index", k) == i]
        if not ks:
            return
        k = min(ks, key=lambda k: abs(r.measure_starts[k] - now))
        self.seek(float(r.measure_starts[k]))
        if self.boxes:
            self._show_cursor_page()

    def _on_canvas_menu(self, event):
        i = self._measure_at(event)
        if i is None:
            return
        number = str(self.score["measures"][i]["number"])
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=f"마디 {number}부터 반복", command=lambda: self._set_loop(a=number))
        menu.add_command(label=f"마디 {number}까지 반복", command=lambda: self._set_loop(b=number))
        menu.add_separator()
        menu.add_command(label="구간 반복 끄기", command=lambda: (self.loop_var.set(False), self._on_loop_change()))
        menu.tk_popup(event.x_root, event.y_root)

    def _show_presets_menu(self):
        """히스토리 — 저장한 반복 구간 목록(최대 LOOP_HISTORY_MAX개). 구간을 누르면 그 구간으로 반복, ✕로 하나씩 지움."""
        old = getattr(self, "_hist_top", None)
        if old is not None and old.winfo_exists():
            old.destroy()
            return
        top = tk.Toplevel(self)
        self._hist_top = top
        top.overrideredirect(True)
        top.configure(bg=W.BORDER)
        body = tk.Frame(top, bg=W.PANEL)
        body.pack(padx=1, pady=1)
        self._hist_body = body
        self._fill_history(body)
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<FocusOut>", lambda e: self.after(150, lambda: top.winfo_exists() and top.destroy()))
        self._place_above(top, self.presets_btn)
        top.focus_force()

    def _place_above(self, top, anchor):
        """재생 막대가 맨 아래라 팝업은 anchor 위로 펼친다."""
        top.update_idletasks()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() - top.winfo_reqheight() - 4
        top.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _fill_history(self, body):
        u = ui_scale(self)
        for w in body.winfo_children():
            w.destroy()
        a, b = self.loop_a_var.get(), self.loop_b_var.get()
        on = self.loop_var.get()
        tk.Label(body, text=f"저장한 구간  {len(self.loop_history)}/{LOOP_HISTORY_MAX}", bg=W.PANEL, fg=W.MUTED,
                 font=(W.FONT, 9)).pack(anchor="w", padx=int(10 * u), pady=(int(8 * u), int(4 * u)))
        if not self.loop_history:
            tk.Label(body, text="아직 없습니다 — 구간을 정하고 \"저장\"을 누르세요", bg=W.PANEL, fg=W.TEXT,
                     font=(W.FONT, 10)).pack(padx=int(10 * u), pady=(0, int(10 * u)))
            return
        for h in self.loop_history:
            row = tk.Frame(body, bg=W.PANEL)
            row.pack(fill="x", padx=int(8 * u), pady=int(2 * u))
            now = on and (h["a"], h["b"]) == (a, b)
            W.RoundButton(row, text=f"{'✓ ' if now else ''}{h['a']} ~ {h['b']}마디", width=150, height=34,
                          kind="solid" if now else "outline",
                          command=lambda h=h: (self._hist_top.destroy(), self._set_loop(h["a"], h["b"]))
                          ).pack(side="left")
            W.RoundButton(row, text="✕", width=36, height=34, kind="ghost",
                          command=lambda h=h: self._delete_history(h)).pack(side="left", padx=(int(4 * u), 0))
        tk.Frame(body, height=int(6 * u), bg=W.PANEL).pack()

    def _save_preset(self):
        """저장 — 지금 구간을 저장 목록 맨 앞에(같은 구간이면 앞으로), 최대 LOOP_HISTORY_MAX개(넘으면 가장 오래된 것부터 밀려남)."""
        if not self._measure_numbers:
            self._set_status("악보를 열면 구간을 저장할 수 있습니다")
            return
        a, b = self.loop_a_var.get(), self.loop_b_var.get()
        if a not in self._measure_numbers or b not in self._measure_numbers:
            return
        again = any((h["a"], h["b"]) == (a, b) for h in self.loop_history)
        self.loop_history = [{"a": a, "b": b}] + [h for h in self.loop_history if (h["a"], h["b"]) != (a, b)]
        dropped = len(self.loop_history) > LOOP_HISTORY_MAX
        del self.loop_history[LOOP_HISTORY_MAX:]
        self._set_status(f"{'이미 저장돼 있어 맨 앞으로 옮김' if again else '저장함'}: {a}~{b}마디"
                         + (f" (가장 오래된 구간은 {LOOP_HISTORY_MAX}개를 넘어 지움)" if dropped else ""))
        self._schedule_save()

    def _delete_history(self, h):
        self.loop_history = [x for x in self.loop_history if x is not h]
        self._set_status(f"지움: {h['a']}~{h['b']}마디")
        self._schedule_save()
        top = getattr(self, "_hist_top", None)
        if top is not None and top.winfo_exists():
            self._fill_history(self._hist_body)
            self._place_above(top, self.presets_btn)

    # ------------------------------------------------------------ 구간 지정: 마디 ↔ 구간(20마디 단위)

    def _on_loop_mode(self):
        """토글: "마디"(스핀박스) ↔ "구간"(20마디 단위 선택 창)."""
        if self.loop_mode_var.get() == "block":
            self._loopbox.pack_forget()
            self._blockbox.pack(side="left")
            self._snap_to_blocks()
        else:
            self._blockbox.pack_forget()
            self._loopbox.pack(side="left")
            top = getattr(self, "_block_top", None)
            if top is not None and top.winfo_exists():
                top.destroy()

    def _num_of(self, i):
        """i번째 마디의 번호(숫자) — 숫자가 없는 번호면 순서."""
        digits = "".join(ch for ch in self._measure_numbers[i] if ch.isdigit())
        return int(digits) if digits else i

    def _block_labels(self):
        """구간 방식의 값들 — 마디 번호를 20 단위로 내린 값(0, 20, 40…). 번호 20부터 시작하는 덩어리는 "20"."""
        B = LOOP_BLOCK_MEASURES
        return sorted({self._num_of(i) // B * B for i in range(len(self._measure_numbers))})

    def _block_edges(self):
        """지금 구간의 (시작 값, 끝 값) — 각각 시작·끝 마디 번호를 20 단위로 내린 값. 없으면 None."""
        nums, B = self._measure_numbers, LOOP_BLOCK_MEASURES
        try:
            ia, ib = nums.index(self.loop_a_var.get()), nums.index(self.loop_b_var.get())
        except ValueError:
            return None
        return self._num_of(ia) // B * B, self._num_of(ib) // B * B

    def _sync_block_buttons(self):
        s, e = self._block_edges() or (0, 0)
        self.block_a_btn.set_label(f"{s}마디 ▾")
        self.block_b_btn.set_label(f"{e}마디 ▾")

    def _set_block(self, s, e):
        """시작 값 s·끝 값 e로 구간을 정함 — 시작 s는 번호 s인 마디(0이면 첫 마디)부터, 끝 e는 번호가 e+19인 마디까지
        (사용자 요청 2026-10-13: "20"을 고르면 20번 마디부터). 반복이 이미 켜져 있으면 바로 적용, 아니면 값만 바꿈."""
        nums, B = self._measure_numbers, LOOP_BLOCK_MEASURES
        if not nums:
            return
        vals = [self._num_of(i) for i in range(len(nums))]
        ia = next((i for i, v in enumerate(vals) if v >= s), len(nums) - 1)
        ib = max([i for i, v in enumerate(vals) if v <= e + B - 1] or [ia])
        ib = max(ia, ib)
        self.loop_a_var.set(nums[ia])
        self.loop_b_var.set(nums[ib])
        self._loop_prev = (nums[ia], nums[ib])
        if self.loop_var.get():
            self._on_loop_change()
        else:
            self._schedule_save()

    def _snap_to_blocks(self):
        """구간 방식으로 바꿀 때 지금 구간을 20마디 단위로 맞춤(시작·끝이 든 덩어리 전체)."""
        if not self._measure_numbers:
            return
        self._set_block(*(self._block_edges() or (0, 0)))

    def _wheel_list(self, parent, rows, row_h, width):
        """세로 목록 칸 — rows줄까지 보이고 넘치면 마우스 휠로 스크롤. (canvas, inner)를 돌려줌."""
        u = ui_scale(self)
        cv = tk.Canvas(parent, bg=W.PANEL, highlightthickness=0, bd=0, width=int(width * u),
                       height=int(rows * row_h * u), yscrollincrement=int(row_h * u))
        inner = tk.Frame(cv, bg=W.PANEL)
        cv.create_window(0, 0, window=inner, anchor="nw")
        cv._wheel = True
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=(0, 0, inner.winfo_reqwidth(),
                                                                       inner.winfo_reqheight())))
        return cv, inner

    def _show_block_picker(self):
        """구간 방식 선택 창 — 시작(0, 20, 40…)과 종료를 세로 목록으로(5줄 넘으면 마우스 휠로 아래가 나타남).
        시작을 고르면 종료 목록은 그 값부터 차례로 나타난다(애니메이션)."""
        nums, B = self._measure_numbers, LOOP_BLOCK_MEASURES
        if not nums:
            return
        old = getattr(self, "_block_top", None)
        if old is not None and old.winfo_exists():
            old.destroy()
            return
        u = ui_scale(self)
        ROWS, ROW_H = 5, 40
        starts = self._block_labels()
        state = {"s": (self._block_edges() or (0, 0))[0], "job": None}
        top = tk.Toplevel(self)
        self._block_top = top
        top.overrideredirect(True)
        top.configure(bg=W.BORDER)
        body = tk.Frame(top, bg=W.PANEL)
        body.pack(padx=1, pady=1)
        pad = int(10 * u)
        cols = tk.Frame(body, bg=W.PANEL)
        cols.pack(padx=pad, pady=pad)
        tk.Label(cols, text="시작", bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9)).grid(row=0, column=0, sticky="w")
        tk.Label(cols, text="종료", bg=W.PANEL, fg=W.MUTED, font=(W.FONT, 9)).grid(row=0, column=1, sticky="w",
                                                                               padx=(pad, 0))
        scv, srow = self._wheel_list(cols, ROWS, ROW_H, 70)
        scv.grid(row=1, column=0)
        ecv, erow = self._wheel_list(cols, ROWS, ROW_H, 70)
        ecv.grid(row=1, column=1, padx=(pad, 0))

        def build_starts():
            for w in srow.winfo_children():
                w.destroy()
            for s in starts:
                W.RoundButton(srow, text=str(s), width=60, height=34, kind="solid" if s == state["s"] else "outline",
                              command=lambda s=s: pick_start(s)).pack(side="top", pady=int(3 * u))
            top.update_idletasks()
            if state["s"] in starts:   # 고른 시작이 보이게
                scv.yview_moveto(max(0.0, (starts.index(state["s"]) - 1) / max(1, len(starts))))

        def show_ends(animate):
            if state["job"] is not None:
                self.after_cancel(state["job"])
                state["job"] = None
            for w in erow.winfo_children():
                w.destroy()
            ecv.yview_moveto(0)
            cur_e = (self._block_edges() or (0, 0))[1]
            # 종료 목록은 시작에서 고른 값부터(사용자 요청 2026-10-13): 종료 X = "X에서 시작하는 20마디 덩어리까지" —
            # 시작 20이면 20·40·60, 20을 고르면 20마디만 반복
            ends = [x for x in starts if x >= state["s"]]
            chips = [W.RoundButton(erow, text=str(e), width=60, height=34, kind="solid" if e == cur_e else "outline",
                                   command=lambda e=e: pick_end(e)) for e in ends]

            def reveal(i, slide):
                state["job"] = None
                if not top.winfo_exists() or i >= len(chips):
                    return
                # 위에서 내려오듯 — 간격을 줄여 가며 자리를 잡음
                chips[i].pack_forget()
                chips[i].pack(side="top", pady=(int(3 * u) + slide * int(8 * u), int(3 * u)))
                if slide > 0:
                    state["job"] = self.after(30, lambda: reveal(i, slide - 1))
                else:
                    state["job"] = self.after(40, lambda: reveal(i + 1, 3))
            if animate:
                reveal(0, 3)
            else:
                for c in chips:
                    c.pack(side="top", pady=int(3 * u))

        def wheel(e):
            w = top.winfo_containing(e.x_root, e.y_root)
            while w is not None and not getattr(w, "_wheel", False):
                w = w.master
            if w is not None and w.winfo_exists():
                w.yview_scroll(-1 if e.delta > 0 else 1, "units")

        def pick_start(s):
            state["s"] = s
            edges = self._block_edges()
            e = edges[1] if edges and edges[1] >= s else s
            self._set_block(s, e)
            build_starts()
            show_ends(True)

        def pick_end(e):
            self._set_block(state["s"], e)
            top.destroy()

        build_starts()
        show_ends(False)
        top.bind("<MouseWheel>", wheel)
        top.bind("<Escape>", lambda e: top.destroy())
        top.bind("<FocusOut>", lambda e: self.after(150, lambda: top.winfo_exists() and top.destroy()))
        self._place_above(top, self._loop_holder)
        top.focus_force()

    def _set_loop(self, a=None, b=None):
        if a is not None:
            self.loop_a_var.set(a)
        if b is not None:
            self.loop_b_var.set(b)
        self.loop_var.set(True)
        self._on_loop_change()

    # ------------------------------------------------------------ 저장·종료

    def _save_wav(self):
        r = self.rendered
        if r is None:
            return
        base = os.path.splitext(os.path.basename(self.path))[0]
        suffix = f"_{int(r.tempo_scale * 100)}%" if abs(r.tempo_scale - 1.0) > 1e-9 else ""
        out = filedialog.asksaveasfilename(
            title="소리 파일로 저장", defaultextension=".wav", initialdir=os.path.dirname(self.path),
            initialfile=f"{base}{suffix}.wav", filetypes=[("WAV", "*.wav"), ("MP3", "*.mp3")])
        if not out:
            return
        click = CLICK_VOLUME if self.metro_var.get() != "off" else 0.0
        settings = self._live_settings()  # 들리는 그대로 저장("메트로놈만"이면 메트로놈만)
        self._set_status("MP3 저장 중…" if out.lower().endswith(".mp3") else "WAV 저장 중…")

        def work():
            try:
                keep = r.settings
                r.settings = settings
                try:
                    write_wav(out, r, click)
                finally:
                    r.settings = keep
            except Exception as e:
                self._post(lambda: messagebox.showerror(APP_TITLE, f"저장하지 못했습니다:\n{e}"))
                return
            self._post(self._set_status, f"저장함: {out}")

        threading.Thread(target=work, daemon=True).start()

    def _on_close(self):
        self._render_gen += 1
        self.stop()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        self._save_song_settings()
        self.destroy()


if __name__ == "__main__":
    multiprocessing.freeze_support()  # exe(PyInstaller)에서 그림 변환 프로세스가 앱을 또 띄우지 않게
    app = GenerateApp()
    if len(sys.argv) > 1:
        app.after(100, lambda: app.open_path(sys.argv[1]))
    app.mainloop()
