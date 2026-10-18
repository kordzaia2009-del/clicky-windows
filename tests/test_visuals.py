"""Mascot, 🍇 pointer + trail, island and app icons — offline. Qt runs on the
offscreen platform; audio, clock and folders are faked."""

import random
import time

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication

from ui import strings_ka as S
from ui.mascot import (
    BLINK_LEN_S, BLINK_MAX_S, MascotAnimator, frame_name, load_frames,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ── mascot ───────────────────────────────────────────────────────────────────

def frames_over(anim, seconds, speaking=False, level=0.0, step=0.02, start=0.0):
    out, t = [], start
    while t < start + seconds:
        out.append(anim.frame(t, speaking, level))
        t += step
    return out


def test_idle_shows_open_eyes_and_blinks_randomly():
    anim = MascotAnimator(random.Random(1))
    frames = frames_over(anim, 20)
    assert set(frames) == {"idle_eyes_open", "idle_eyes_closed"}       # mouth never opens
    blinks = sum(1 for a, b in zip(frames, frames[1:]) if a != b and b == "idle_eyes_closed")
    assert 20 / BLINK_MAX_S - 1 <= blinks <= 20 / 2.5 * 1.6
    assert frames.count("idle_eyes_closed") / len(frames) < 0.15         # blinks are short


def test_blink_timing_is_random():
    first = []
    for seed in range(5):
        frames = frames_over(MascotAnimator(random.Random(seed)), 8)
        first.append(frames.index("idle_eyes_closed"))
    assert len(set(first)) > 1


def test_mouth_follows_speech_loudness():
    anim = MascotAnimator(random.Random(0))
    anim._next_blink = 1e9                                  # no blinks for this test
    assert anim.frame(0.0, True, 0.2) == "talk_eyes_open"   # loud -> open
    assert anim.frame(0.03, True, 0.0001) == "talk_eyes_open"   # held open a moment
    assert anim.frame(0.20, True, 0.0001) == "idle_eyes_open"   # quiet -> closed
    assert anim.frame(0.40, False, 0.3) == "idle_eyes_open"     # not speaking -> closed


def test_mouth_flaps_when_loudness_is_unknown():
    anim = MascotAnimator(random.Random(3))
    anim._next_blink = 1e9
    frames = frames_over(anim, 2, speaking=True, level=0.0)
    changes = sum(1 for a, b in zip(frames, frames[1:]) if a != b)
    assert 2 / 0.18 - 2 <= changes <= 2 / 0.10 + 2


def test_blinking_while_talking_uses_the_talk_blink_frame():
    assert frame_name(True, True) == "talk_eyes_closed"
    anim = MascotAnimator(random.Random(0))
    anim._next_blink = 0.0
    assert anim.frame(0.0, True, 0.3) == "talk_eyes_closed"
    assert anim.frame(BLINK_LEN_S + 0.05, True, 0.3) == "talk_eyes_open"


def test_mascot_frames_load(app):
    frames = load_frames(150, 1.5)
    assert set(frames) == {"idle_eyes_open", "idle_eyes_closed", "talk_eyes_open", "talk_eyes_closed"}
    assert all(abs(pm.width() / pm.devicePixelRatio() - 150) < 1 for pm in frames.values())


# ── playback loudness (mouth source) ─────────────────────────────────────────

def test_playback_reports_loudness_while_playing(monkeypatch):
    from audio import playback
    seen = []

    class FakeStream:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def write(self, block):
            seen.append((playback.is_playing(), playback.speaking_level()))

    monkeypatch.setattr(playback.sd, "OutputStream", FakeStream)
    playback._stop_event.clear()
    sr = 16000
    pcm = np.concatenate([np.zeros(sr // 10), 0.3 * np.ones(sr // 10)]).astype(np.float32)
    playback._blocking_play_chunked(pcm, sr)
    assert seen[0] == (True, 0.0)                      # silence: mouth closed
    assert seen[-1][0] and seen[-1][1] > 0.2           # speech: loud
    assert playback.is_playing() is False and playback.speaking_level() == 0.0


# ── 🍇 pointer + overlay ─────────────────────────────────────────────────────

def test_grape_pixmap(app):
    from ui.overlay import GRAPE_RADIUS, render_grape_pixmap
    pm = render_grape_pixmap(2.0)
    assert not pm.isNull() and pm.devicePixelRatio() == 2.0
    img = pm.toImage()
    c = img.pixelColor(img.width() // 2, img.height() // 2 + int(2 * 2))   # in the bunch
    assert c.blue() > c.green() and c.red() > c.green()                    # purple
    corner = img.pixelColor(0, 0)
    assert corner.alpha() == 0                                             # transparent outside


def test_overlay_draws_mascot_trail_and_grape(app):
    from ui.overlay import TRAIL_SECONDS, CursorOverlay
    o = CursorOverlay()
    r = o.mascot_rect()
    a = QApplication.primaryScreen().availableGeometry()
    assert a.contains(r)                                  # on screen, above the taskbar
    o.point_at(a.center().x(), a.center().y(), "Zoom")
    for _ in range(10):
        o._tick()
        time.sleep(0.01)
    assert len(o._trail) >= 5                             # flight leaves a trail
    img = o.grab()                                        # paints without errors
    assert not img.isNull()
    time.sleep(TRAIL_SECONDS + 0.05)
    o._flight_phase = "follow"
    o._tick()
    assert o._trail == []                                 # trail fades away


# ── app icons / Georgian names ───────────────────────────────────────────────

def test_georgian_app_names():
    from ui.app_icons import clean_label, georgian_name
    assert georgian_name("Zoom Workplace") == "ზუმი"
    assert georgian_name("Google Chrome - 1 running window") == "გუგლ ქრომი"
    assert georgian_name("Microsoft Edge") == "ეჯი"
    assert georgian_name("Password") is None              # "word" only as a whole word
    assert clean_label("Google Chrome - 1 running window") == "Google Chrome"


def test_find_shortcut_and_resolve(tmp_path):
    from ui.app_icons import find_shortcut, resolve
    (tmp_path / "Zoom Workplace.lnk").write_text("x")
    (tmp_path / "Uninstall Zoom.lnk").write_text("x")
    sub = tmp_path / "Accessories"
    sub.mkdir()
    (sub / "Notepad.lnk").write_text("x")
    assert find_shortcut("Zoom Workplace", [tmp_path]).endswith("Zoom Workplace.lnk")
    assert find_shortcut("Notepad", [tmp_path]).endswith("Notepad.lnk")
    assert find_shortcut("Inbox", [tmp_path]) is None
    info = resolve("Zoom Workplace", [tmp_path])
    assert info.name_ka == "ზუმი" and info.shortcut.endswith(".lnk")
    assert resolve("Inbox", [tmp_path]) is None
    known = resolve("Telegram", [tmp_path])                # known app, no shortcut
    assert known.name_ka == "ტელეგრამი" and known.shortcut is None


# ── island model ─────────────────────────────────────────────────────────────

def test_island_follows_listen_think_answer():
    from ui.island import Hide, IslandModel, SPINNER, WAVE
    m = IslandModel()
    assert m.app_state("LISTENING").text == S.ISLAND_LISTENING
    assert m.app_state("LISTENING").kind == WAVE
    assert m.app_state("THINKING").kind == SPINNER
    assert m.app_state("SPEAKING").text == S.ISLAND_SPEAKING
    assert isinstance(m.app_state("IDLE"), Hide)


def test_autopilot_owns_the_island_while_it_runs():
    from ui.island import IslandModel
    m = IslandModel()
    m.set_agent_running(True)
    assert m.app_state("IDLE") is None                     # no hiding mid-run
    assert m.app_state("THINKING") is None
    assert m.app_state("LISTENING").text == S.ISLAND_LISTENING   # user answering დიახ
    assert m.agent_state("planning").text == S.ISLAND_PLANNING
    assert m.agent_state("awaiting_confirm").text == S.ISLAND_CONFIRM
    done = m.agent_state("done")
    assert done.text == S.ISLAND_DONE and done.hide_after
    assert m.agent_state("cancelled").hide_after
    assert m.pointer("Zoom") is None


def test_action_lines():
    from ui.island import IslandModel, action_text
    assert action_text("double_click", "Zoom Workplace", "ვხსნი ზუმს", "ზუმი") == "იხსნება ზუმი"
    assert action_text("click", "Inbox", "ვაჭერ შემოსულებს", None) == "ვაჭერ შემოსულებს"
    assert action_text("click", "Inbox", "", None) == "ვაჭერ: Inbox"
    assert action_text("type", "Search", "", None) == S.ISLAND_TYPE   # never the typed text
    st = IslandModel().agent_action("double_click", "Zoom Workplace", "", "ზუმი", "Zoom Workplace")
    assert st.app_label == "Zoom Workplace"
    assert IslandModel().pointer("Save").text == "კურსორი მიდის: Save"


# ── island window (offscreen) ────────────────────────────────────────────────

def test_island_window_flags_show_close_and_hide(app):
    from PyQt6.QtTest import QTest
    from ui.island import IslandWindow, PULSE
    w = IslandWindow()
    f = w.windowFlags()
    assert f & Qt.WindowType.WindowDoesNotAcceptFocus and f & Qt.WindowType.WindowStaysOnTopHint
    assert not f & Qt.WindowType.WindowTransparentForInput     # × must be clickable
    assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    w.show_status(PULSE, "იხსნება ზუმი")
    assert w.isVisible() and w.text == "იხსნება ზუმი"
    QTest.qWait(500)                                            # slide-in finishes
    a = QApplication.primaryScreen().availableGeometry()
    assert abs(w.pill_rect_global().center().x() - a.center().x()) <= 2
    assert w.pill_rect_global().top() <= a.top() + 20           # at the top

    closes = []
    w.on_close.connect(lambda: closes.append(1))
    c = w._close_rect().center()
    QTest.mouseClick(w, Qt.MouseButton.LeftButton, pos=QPoint(int(c.x()), int(c.y())))
    assert closes == [1]

    w.show_status(PULSE, "ვაჭერ: Inbox")                        # text cross-fades in
    assert w.text == "ვაჭერ: Inbox"
    w.hide_island()
    QTest.qWait(450)
    assert not w.isVisible()


def test_island_moves_off_a_target_under_it(app):
    from PyQt6.QtTest import QTest
    from ui.island import IslandWindow, PULSE
    w = IslandWindow()
    w.show_status(PULSE, "ვფიქრობ…")
    QTest.qWait(500)
    top = w.pill_rect_global().center()
    w.avoid_target(top.x(), top.y())
    assert w.at_bottom
    QTest.qWait(450)
    assert not w.pill_rect_global().adjusted(-20, -20, 20, 20).contains(top)
    w.avoid_target(10, QApplication.primaryScreen().availableGeometry().bottom() - 300)
    assert not w.at_bottom
    w.hide_island()


def test_island_controller_shows_app_icon_line(app):
    from ui.app_icons import AppInfo
    from ui.island import IslandController, IslandWindow
    w = IslandWindow()
    icons = []
    ctl = IslandController(w, resolve_app=lambda label: AppInfo(label, "ზუმი", None),
                           icon_for=lambda info: icons.append(info.label) or None)
    ctl.on_agent_running(True)
    ctl.on_agent_action("double_click", "Zoom Workplace", "")
    assert w.text == "იხსნება ზუმი" and icons == ["Zoom Workplace"]
    ctl.on_agent_action("type", "Search", "")
    assert w.text == S.ISLAND_TYPE
    w.hide_island()


# ── words coming out of its mouth ────────────────────────────────────────────

def test_revealed_text_follows_the_audio():
    from ui.mascot import revealed_text
    line = "გამარჯობა, როგორ შემიძლია დაგეხმაროთ?"
    assert revealed_text(line, 0.0) == ""
    assert revealed_text(line, 0.01) == "გამარჯობა,"            # first word as soon as it starts
    half = revealed_text(line, 0.5)
    assert line.startswith(half) and 0 < len(half) < len(line)
    assert revealed_text(line, 1.0) == line
    assert revealed_text("", 0.7) == ""


def test_bubble_sits_left_of_the_mascot_on_screen_with_tail_at_the_mouth():
    from ui.mascot import MOUTH_X, MOUTH_Y, bubble_geometry
    avail = (0, 0, 1280, 680)
    mascot = (1280 - 18 - 150, 680 - 154, 150, 150)
    (x, y, w, h), (tx, ty) = bubble_geometry(avail, mascot, (300, 90))
    assert x >= 10 and y >= 10 and x + w <= 1270 and y + h <= 670
    assert x + w <= mascot[0] + 150 * 0.5                       # left of the face
    assert y + h < mascot[1] + 150 * MOUTH_Y                    # above the mouth
    assert abs(ty - (mascot[1] + 150 * MOUTH_Y)) < 1
    assert mascot[0] < tx < mascot[0] + 150 * MOUTH_X + 1       # tail ends at the mouth
    # a huge bubble is still kept on screen
    (x, y, w, h), _ = bubble_geometry(avail, mascot, (380, 600))
    assert y >= 10 and x >= 10


def test_reporting_tts_reports_each_line_and_passes_everything_through():
    import asyncio
    from audio.tts.reporting import ReportingTTS
    lines = []

    class Inner:
        voice = None

        async def speak(self, text):
            lines.append(("spoke", text))
            if text == "boom":
                raise RuntimeError("network")

        def set_voice(self, v):
            self.voice = v

    tts = ReportingTTS(Inner(), lambda t: lines.append(("line", t)))
    asyncio.run(tts.speak("გამარჯობა"))
    assert lines == [("line", "გამარჯობა"), ("spoke", "გამარჯობა"), ("line", "")]
    tts.set_voice("ka-GE-EkaNeural")
    assert tts.inner.voice == "ka-GE-EkaNeural" and hasattr(tts, "set_voice")
    lines.clear()
    with pytest.raises(RuntimeError):
        asyncio.run(tts.speak("boom"))
    assert lines[-1] == ("line", "")                           # bubble always told it ended


def test_playback_progress_and_clip_number(monkeypatch):
    from audio import playback
    progress = []

    class FakeStream:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def write(self, block): progress.append(playback.playback_progress()[1])

    monkeypatch.setattr(playback.sd, "OutputStream", FakeStream)
    playback._stop_event.clear()
    gen_before = playback.playback_progress()[0]
    playback._blocking_play_chunked(np.zeros(16000, dtype=np.float32), 16000)
    gen, done = playback.playback_progress()
    assert gen == gen_before + 1 and done == 1.0
    assert progress == sorted(progress) and progress[0] < 0.2   # grows as it plays


def test_overlay_bubble_reveals_words_with_the_voice(app, monkeypatch):
    import ui.overlay as ov
    state = {"gen": 5, "progress": 0.0}
    fake = type("P", (), {
        "is_playing": staticmethod(lambda: True),
        "speaking_level": staticmethod(lambda: 0.1),
        "playback_progress": staticmethod(lambda: (state["gen"], state["progress"])),
    })
    monkeypatch.setattr(ov, "_playback", fake)
    o = ov.CursorOverlay()
    line = "ზუმს ვხსნი ახლავე, ერთი წუთით"
    o.on_speaking(line)
    o._tick()
    assert o._say_text == ""                     # synthesising: the voice has not started
    state.update(gen=6, progress=0.5)            # this line's clip is half played
    o._tick()
    assert 0 < len(o._say_text) < len(line) and line.startswith(o._say_text)
    img = o.grab()                               # bubble paints
    assert not img.isNull() and o._say_rect is not None
    assert o._say_rect.toAlignedRect() in o.occupied_rects()
    o.on_speaking("")                            # line finished
    o._tick()
    assert o._say_text == line
    o._say_hide_at = time.monotonic() - 1        # linger + fade over
    o._tick()
    assert o._say_text == "" and o._say_line == ""


def test_mascot_lives_in_the_bottom_right_corner(app):
    from ui.overlay import CursorOverlay
    a = QApplication.primaryScreen().availableGeometry()
    r = CursorOverlay().mascot_rect()
    assert r.right() > a.center().x() and r.bottom() > a.center().y()
    assert a.contains(r)


def test_chat_panel_stays_hidden(app):
    from ui.panel import CompanionPanel
    p = CompanionPanel()
    p._on_question("სად არის ზუმი?")
    p.show_agent_say("ვხსნი")
    p.show_agent_prompt("confirm", "გავაგრძელო?")
    p._on_error("შეცდომა")
    assert not p.isVisible()                     # never pops up by itself
    p.auto_show = True
    p._on_question("სად?")
    assert p.isVisible()
    p.hide()
