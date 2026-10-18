"""„რომელია ხრომი?“: the which-app gate, the model reply parser, picking the
best copy of an icon, and the overlay's circle lap. No model, no UIA —
Qt runs offscreen."""

import asyncio
import time

import pytest
from PyQt6.QtWidgets import QApplication

import agent.which_app as wa
from agent.show_icon import Icon, best_of, clean_taskbar_name

ICONS = [
    Icon("Google Chrome", (1155, 1020, 1210, 1080), "taskbar"),
    Icon("Microsoft Edge", (950, 493, 1044, 600), "desktop", visible=True),
    Icon("New Microsoft Word Document", (95, 5, 189, 112), "desktop", visible=True),
    Icon("Google Chrome", (1330, 493, 1424, 600), "desktop", visible=False),
]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("text", [
    "რომელია ხრომი?", "სად არის ვორდი", "ინტერნეტი სადაა?", "სად მაქვს წერილები",
    "ხრომი ვერ ვიპოვე", "რომელი პროგრამაა ეს", "where is chrome",
])
def test_which_questions_pass_the_gate(text):
    assert wa.is_which_question(text)


@pytest.mark.parametrize("text", [
    "რომელი საათია?", "რა ამინდია", "ტაქსი გამომიძახე", "მაჩვენე ხრომი", "გახსენი მეილი",
])
def test_other_questions_never_reach_the_model(text):
    assert not wa.is_which_question(text)


def test_taskbar_names_are_cleaned():
    assert clean_taskbar_name("Google Chrome pinned") == "Google Chrome"
    assert clean_taskbar_name("Claude - 1 running window") == "Claude"
    assert clean_taskbar_name("Word - 3 running windows") == "Word"


def test_best_copy_prefers_a_visible_desktop_icon_then_the_taskbar():
    covered_desktop = ICONS[3]
    assert best_of(ICONS, covered_desktop).where == "taskbar"      # desktop one is covered
    visible = [Icon("Google Chrome", (1, 1, 9, 9), "desktop", True)] + ICONS
    assert best_of(visible, ICONS[0]).where == "desktop"


def test_parse_pick():
    p = wa.parse_pick('```json\n{"asked_app": true, "index": 2, "explain": "აი, Edge."}\n```', 4)
    assert (p.asked_app, p.index, p.explain) == (True, 1, "აი, Edge.")
    p = wa.parse_pick('{"asked_app": true, "index": null, "explain": "ვერ ვხედავ"}', 4)
    assert p.index is None and p.explain == "ვერ ვხედავ"
    p = wa.parse_pick('{"asked_app": false, "index": 1, "explain": "x"}', 4)
    assert not p.asked_app and p.index is None and p.explain == ""
    assert wa.parse_pick('{"asked_app": true, "index": 9, "explain": "x"}', 4).index is None
    assert wa.parse_pick("not json", 4) is None
    assert wa.parse_pick('{"index": 1}', 4) is None


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def stream_response(self, user_text, screenshots_b64, history, system_prompt, model=None):
        self.calls.append((user_text, screenshots_b64))
        for part in (self.reply[:10], self.reply[10:]):
            yield part


def test_pick_app_sends_the_icon_names_only():
    llm = FakeLLM('{"asked_app": true, "index": 1, "explain": "აი, ეს არის Google Chrome."}')
    pick = asyncio.run(wa.pick_app("რომელია ხრომი?", ICONS, llm))
    assert pick.index == 0 and pick.explain.startswith("აი")
    (user, images), = llm.calls
    assert images == []                                  # text only — fast
    assert "1. Google Chrome" in user and "რომელია ხრომი?" in user


def test_overlay_laps_a_circle_with_the_emoji_hidden_then_returns(app, monkeypatch):
    import ui.overlay as ov
    for name in ("FLY_DURATION_MIN", "FLY_DURATION_MAX", "RETURN_DURATION", "ORBIT_SECONDS"):
        monkeypatch.setattr(ov, name, 0.05)
    o = ov.CursorOverlay()
    a = QApplication.primaryScreen().availableGeometry()
    x, y = a.center().x(), a.center().y()
    o.circle_around(x, y, 40.0, 0.05)
    seen = set()
    hidden_while = set()
    end = time.time() + 3
    while time.time() < end:
        o._tick()
        seen.add(o._flight_phase)
        if o._hide_emoji:
            hidden_while.add(o._flight_phase)
        if o._flight_phase == "orbiting":
            assert not o.grab().isNull()                 # paints the pen tip
            assert any(s["kind"] == "circle" and s["r"] == 40.0 for s in o._annotations)
        if "returning" in seen and o._flight_phase == "follow":
            break
        time.sleep(0.01)
    assert {"flying", "orbiting", "dwelling", "returning", "follow"} <= seen
    assert hidden_while <= {"orbiting", "dwelling"} and "orbiting" in hidden_while
    assert o._hide_emoji is False and o._orbit is None    # 🍇 back, ready for the cursor


def test_local_pick_finds_known_names_without_a_model():
    icons = ICONS + [Icon("Telegram", (190, 127, 284, 214), "desktop", True),
                     Icon("Microsoft Word", (5, 5, 50, 50), "desktop", True)]
    p = wa.local_pick("ბაბუ სად არის ხრომ", icons)          # real STT text
    assert icons[p.index].name == "Google Chrome" and p.explain == "აი, ეს არის Google Chrome."
    assert icons[wa.local_pick("ტელეგრამი სადაა?", icons).index].name == "Telegram"
    # the app, not „New Microsoft Word Document“
    assert icons[wa.local_pick("სად არის ვორდი", icons).index].name == "Microsoft Word"
    assert wa.local_pick("სად არის თბილისი?", icons) is None
