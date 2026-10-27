# -*- coding: utf-8 -*-
"""재생 엔진(UI 독립) — 조각 iterator를 받아 백그라운드에서 sounddevice 출력에 채운다.

ExtractBeatForAndroid/audio_player.py에서 가져옴(2026-10-07). 안드로이드(AudioTrack)
백엔드는 이 프로젝트(PC 전용)엔 필요 없어 뺐다. 공용 파일이 아니다 — 독립적으로 고친다.

재생 위치는 played_sec() — 실제로 소리로 나간 시간(출력 지연 반영). 이 앱은 빠르기를
바꾸면 소리를 다시 합성하므로(늘이기 없음) 곡 안 시각 = 시작 위치 + played_sec()다.
"""
import threading
import time

import numpy as np

# 출력 쪽에 미리 채워둘 분량. 이 앱은 믹스 조각(0.1초)을 그때그때 만들어 넣고
# (render.iter_mix), 재생 중 믹서 변경이 이만큼 늦게 들리므로 원본(2초)보다 짧게 —
# 조각 섞기는 수 ms라 0.5초면 바닥날 일이 없다.
MAX_AHEAD_SEC = 0.5
WRITE_BLOCK_SEC = 0.1
# 장치 출력 버퍼. 소리 콜백도 파이썬이라 다른 일이 GIL을 잡으면 그동안 못 돈다 — 원본의
# "low"(이 PC에서 약 10~90ms)보다 넉넉히 잡아 짧은 멈춤(수십 ms)을 견디게 한다. 재생 위치는
# played_frames()가 이 지연을 빼서 계산하므로 악보 강조는 어긋나지 않는다.
OUTPUT_LATENCY_SEC = 0.12


class _SounddeviceBackend:
    def __init__(self, sr, channels):
        import collections
        import sounddevice as sd
        self._sr = sr
        self._channels = channels
        self._blocks = collections.deque()
        self._queued = 0
        self._consumed = 0
        self._ended = False
        self._underruns = 0
        self._device_underflows = 0  # 장치가 콜백을 제때 못 받아 소리가 끊긴 횟수
        self._got_data = False
        self._drained_at = None  # end() 뒤 큐가 비어 마지막 샘플을 넘긴 시각
        self._lock = threading.Lock()
        self._stream = None
        dev = wasapi_output_device()
        if dev is not None:
            try:
                self._stream = sd.OutputStream(
                    device=dev, samplerate=sr, channels=channels, dtype="float32", callback=self._callback,
                    latency=OUTPUT_LATENCY_SEC, extra_settings=sd.WasapiSettings(auto_convert=True),
                )
            except Exception:
                self._stream = None
        if self._stream is None:  # WASAPI가 안 되면 예전처럼 기본(MME)
            self._stream = sd.OutputStream(
                samplerate=sr, channels=channels, dtype="float32", callback=self._callback,
                latency=OUTPUT_LATENCY_SEC,
            )

    def _callback(self, outdata, frames, time_info, status):
        if status.output_underflow:
            self._device_underflows += 1
        outdata.fill(0)
        filled = 0
        with self._lock:
            before = self._consumed
            while filled < frames and self._blocks:
                block = self._blocks[0]
                n = min(frames - filled, len(block))
                outdata[filled:filled + n] = block[:n]
                filled += n
                if n == len(block):
                    self._blocks.popleft()
                else:
                    self._blocks[0] = block[n:]
            self._queued -= filled
            self._consumed += filled
            # 이 조각의 첫 샘플이 실제로 스피커에서 나올 시각(DAC 시각) — 재생 위치 계산의 기준.
            # 실제 소리를 넘긴 조각에서만 갱신한다(곡이 끝난 뒤 무음 조각에서 갱신하면 위치가 끝에
            # 영영 못 닿아 "끝"을 못 알아챘다).
            if filled > 0 and time_info.outputBufferDacTime > 0:
                self._dac_ref = (before, time_info.outputBufferDacTime)
            if filled < frames:
                if self._ended:
                    if self._drained_at is None:
                        self._drained_at = time.time()
                elif self._got_data:  # 첫 조각 오기 전(합성 중)의 공백은 언더런이 아님
                    self._underruns += 1

    def start(self):
        self._stream.start()

    def write(self, frames):
        with self._lock:
            self._blocks.append(np.asarray(frames, dtype=np.float32))
            self._queued += len(frames)
            self._got_data = True

    def end(self):
        self._ended = True

    def queued_frames(self):
        return self._queued

    def _latency_frames(self):
        return int((self._stream.latency or 0.0) * self._sr)

    def played_frames(self):
        """지금 스피커에서 나오고 있는 샘플 위치. 콜백마다 PortAudio가 알려 주는 "이 조각이 실제로
        나올 시각(DAC 시각)"을 기준으로 지금 시각(stream.time)까지 흐른 만큼 더한다 — 장치가 보고하는
        지연값(stream.latency)으로 빼던 예전 방식은 MME에선 커서가 앞서고(지연을 작게 알려 줌) WASAPI에선
        뒤처졌다(크게 알려 줌, 2026-10-07). DAC 시각을 못 주는 장치면 예전 방식."""
        ref = getattr(self, "_dac_ref", None)
        if ref is not None:
            try:
                base, dac = ref
                played = int(base + (self._stream.time - dac) * self._sr)
                return max(0, min(played, self._consumed))
            except Exception:
                pass
        # 콜백이 넘긴 프레임은 출력 지연만큼 뒤에 들린다. 다 넘긴 뒤엔 넘긴 걸로
        # 더 안 늘어나니, 그때부턴 흐른 시간만큼 지연분을 채워준다.
        played = self._consumed - self._latency_frames()
        if self._drained_at is not None:
            played += int((time.time() - self._drained_at) * self._sr)
        return max(0, min(played, self._consumed))

    def alive(self):
        """출력 스트림이 살아 있나 — 출력 장치가 사라지면(블루투스 끊김·기본 장치 변경) PortAudio가
        스트림을 멈춰 False가 된다. 이때 finished()는 False 그대로라 "재생 중"으로 보이면서 위치만
        멈춘다(2026-10-07 실측)."""
        try:
            return bool(self._stream.active)
        except Exception:
            return False

    def finished(self):
        return self._drained_at is not None and self.played_frames() >= self._consumed

    def underruns(self):
        return self._underruns

    def device_underflows(self):
        return self._device_underflows

    def close(self):
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass  # 장치가 사라진 스트림은 닫을 때 오류가 날 수 있다


def iter_buffer(samples, start_frame=0, block_frames=None):
    """버퍼를 start_frame부터 끝까지 조각내어 한 번 내놓는다."""
    block_frames = block_frames or 32768
    for i in range(start_frame, len(samples), block_frames):
        yield samples[i:i + block_frames]


def iter_loop(samples, start_frame=0, block_frames=None):
    """버퍼를 start_frame부터 끝없이 이어서 반복한다.
    반복 이음매는 버퍼 끝→처음이 그대로 이어지므로, 이음매 처리는 버퍼를 만드는
    쪽(예비박 합성 등) 책임이다."""
    if len(samples) == 0:
        return
    block_frames = block_frames or 32768
    pos = start_frame % len(samples)
    while True:
        end = min(len(samples), pos + block_frames)
        yield samples[pos:end]
        pos = end % len(samples)


class Player:
    """조각 iterator를 출력으로 흘려보내는 재생기. play()마다 새 출력 스트림을 연다."""

    def __init__(self):
        self._backend = None
        self._thread = None
        self._stop_event = None
        self._sr = 0
        self.error = None  # 생산자 스레드에서 난 예외(있으면) — UI가 표시용으로 읽는다

    def play(self, chunks, sr, channels):
        """chunks: (frames,) 또는 (frames, channels) float 배열을 내놓는 iterator."""
        self.stop()
        self.error = None
        self._sr = sr
        backend = _SounddeviceBackend(sr, channels)
        stop_event = threading.Event()
        self._backend = backend
        self._stop_event = stop_event
        backend.start()

        def producer():
            max_ahead = int(MAX_AHEAD_SEC * sr)
            block = max(1, int(WRITE_BLOCK_SEC * sr))
            try:
                for chunk in chunks:
                    chunk = np.asarray(chunk, dtype=np.float32)
                    if chunk.ndim == 1:
                        chunk = chunk.reshape(-1, 1)
                    if chunk.shape[1] != channels:
                        chunk = np.repeat(chunk[:, :1], channels, axis=1)
                    # 큰 조각(수 초 분량)도 작은 블록으로 쪼개 넣어야 정지 시
                    # 버릴 양이 적고 backpressure가 촘촘하게 걸린다.
                    for i in range(0, len(chunk), block):
                        while backend.queued_frames() > max_ahead:
                            if stop_event.wait(0.02):
                                return
                        if stop_event.is_set():
                            return
                        backend.write(chunk[i:i + block])
            except Exception as e:  # 생산자가 죽어도 이미 넣은 분량까지는 재생된다
                self.error = e
                import traceback
                traceback.print_exc()
            backend.end()

        self._thread = threading.Thread(target=producer, daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._backend is not None:
            self._backend.close()
        self._backend = None
        self._thread = None
        self._stop_event = None

    def played_sec(self):
        """재생 시작 후 실제로 소리로 나간 시간(초)."""
        if self._backend is None or not self._sr:
            return 0.0
        return self._backend.played_frames() / self._sr

    def lost_device(self):
        """재생 중인데 출력이 끊겼나(곡 끝이 아닌데 스트림이 멈춤)."""
        return self._backend is not None and not self._backend.finished() and not self._backend.alive()

    def is_playing(self):
        return self._backend is not None and not self._backend.finished()

    def stats(self):
        if self._backend is None:
            return {}
        return {
            "queued_sec": self._backend.queued_frames() / self._sr,
            "underruns": self._backend.underruns(),
            "device_underflows": self._backend.device_underflows(),
        }


def wasapi_output_device():
    """WASAPI 기본 출력 장치 번호(없으면 None). MME(sounddevice 기본)는 실제 출력 지연을 제대로
    알려 주지 않아, 노트북 스피커에서도 커서가 소리보다 조금 앞섰다(비탈리, 2026-10-07). WASAPI는
    요청 지연과 보고 지연이 거의 같다(요청 0.1초 → 보고 0.11초, 콜백의 DAC 시각 차 0.099초 실측).
    장치 샘플레이트(48000 등)와 달라도 auto_convert로 PortAudio가 바꿔 준다."""
    import sounddevice as sd
    try:
        for h in sd.query_hostapis():
            if "WASAPI" in h["name"] and h["default_output_device"] >= 0:
                return h["default_output_device"]
    except Exception:
        pass
    return None


def output_device_name():
    """실제로 재생할 출력 장치 이름(WASAPI 기본, 없으면 sounddevice 기본)."""
    import sounddevice as sd
    try:
        dev = wasapi_output_device()
        return sd.query_devices(dev if dev is not None else sd.default.device[1])["name"]
    except Exception:
        return ""


def refresh_devices():
    """PortAudio 장치 목록을 새로 읽는다(재생 중이 아닐 때만 부를 것) — PortAudio는 처음 켤 때의
    목록·기본 장치를 계속 써서, 블루투스 스피커에서 노트북 스피커로 바꿔도 새 기본 장치를 모른다.
    다시 초기화하는 데 약 0.1초."""
    import sounddevice as sd
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass
