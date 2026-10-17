"""P2: elderly-friendly panel — pure helpers plus widgets on Qt's offscreen
platform (nothing appears on the real screen)."""

import time
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication

from ui import strings_ka
from ui.agent_view import (
    CHECKLIST_FONT_PX, TARGET_CLEARANCE, _hit, _overlaps, checklist_html,
    panel_dock_position, progress_text,
)

STEPS = [SimpleNamespace(title=t) for t in ("გახსენით ბრაუზერი", "გადადით Gmail-ზე",
                                            "გახსენით წერილი", "წაიკითხეთ")]


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_progress_text_includes_the_total():
    assert progress_text(1, 4) == "ნაბიჯი 2 / 4"


def test_checklist_marks_done_current_and_todo():
    h = checklist_html(STEPS, 2)
    assert f"font-size:{CHECKLIST_FONT_PX}px" in h and CHECKLIST_FONT_PX >= 24
    assert "ნაბიჯი 3 / 4" in h
    assert "✓ გახსენით ბრაუზერი" in h and "✓ გადადით Gmail-ზე" in h
    assert "▶ გახსენით წერილი" in h and "background:#ffe14d" in h    # highlighted
    assert "○ წაიკითხეთ" in h


def test_checklist_shows_the_goal_when_there_are_several():
    h = checklist_html(STEPS, 0, goal="მეილის გახსნა", goal_index=1, goal_total=2)
    assert "მიზანი 2 / 2: მეილის გახსნა" in h


def test_checklist_escapes_html():
    h = checklist_html([SimpleNamespace(title="<b>x</b>")], 0)
    assert "<b>x</b>" not in h and "&lt;b&gt;" in h


SCREEN = (0, 0, 1280, 680)
SIZE = (440, 600)


@pytest.mark.parametrize("target", [(1200, 650), (60, 650), (1200, 40), (60, 40), (640, 340)])
def test_dock_never_covers_the_target(target):
    x, y = panel_dock_position(SCREEN, SIZE, target)
    assert not _hit((x, y, *SIZE), target, TARGET_CLEARANCE) or target == (640, 340)


def test_dock_goes_to_the_opposite_side():
    x, _ = panel_dock_position(SCREEN, SIZE, (1200, 650))    # target bottom-right
    assert x < 640                                            # panel on the left
    x, _ = panel_dock_position(SCREEN, SIZE, (60, 650))       # target bottom-left
    assert x > 640


def test_dock_keeps_the_stop_window_uncovered():
    stop = (16, 16, 300, 150)                                 # stop window top-left
    for target in [(1200, 650), (1200, 40), (640, 650)]:
        pos = panel_dock_position(SCREEN, (440, 400), target, keep_clear=[stop])
        assert not _overlaps((*pos, 440, 400), stop), target


# ── widgets (offscreen) ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app):
    from ui.panel import CompanionPanel
    p = CompanionPanel()
    p.show()
    yield p
    p.hide()


def test_mode_switch_emits_and_mirrors_without_echo(panel):
    seen = []
    panel.on_agent_mode_changed.connect(seen.append)
    panel._mode_buttons[False].click()                       # „მაჩვენე“
    panel._mode_buttons[True].click()                        # „გააკეთე“
    assert seen == [False, True]
    assert panel._mode_buttons[True].text() == strings_ka.MODE_DO
    panel.set_agent_mode(False)                              # from the tray
    assert panel._mode_buttons[False].isChecked() and seen == [False, True]


def test_default_mode_is_do(monkeypatch):
    monkeypatch.delenv("CLICKY_AGENT_MODE", raising=False)
    from config import Config
    assert Config().agent_mode == "do"


def test_run_view_checklist_progress_and_lost_button(panel):
    goals = [SimpleNamespace(goal="მეილი", steps=STEPS)]
    panel.set_agent_running(True)
    assert panel._lost_btn.isVisible() and panel._checklist_scroll.isVisible()
    assert panel._ptt_btn.text() == strings_ka.AGENT_STOP_BUTTON
    panel.show_agent_plan(goals)
    panel.show_agent_step(0, 1, STEPS[1].title)
    assert "ნაბიჯი 2 / 4" in panel._checklist.text()
    panel.set_agent_state("acting", "")
    assert panel._status_label.text() == "ნაბიჯი 2 / 4"
    lost = []
    panel.on_agent_lost.connect(lambda: lost.append(1))
    panel._lost_btn.click()
    assert lost == [1]
    # a replan swaps in a new step list
    panel.show_agent_steps(0, STEPS[:3])
    assert "ნაბიჯი 1 / 3" in panel._checklist.text()
    panel.set_agent_running(False)
    assert not panel._lost_btn.isVisible() and not panel._checklist_scroll.isVisible()


def test_confirm_banner_is_red_and_above_the_buttons(panel):
    panel.set_agent_running(True)
    panel.show_agent_prompt("confirm", "ეს მნიშვნელოვანი მოქმედებაა: გაგზავნა. გავაგრძელო?")
    assert panel._confirm_banner.isVisible()
    assert strings_ka.CONFIRM_BANNER_TITLE in panel._confirm_banner.text()
    assert "გაგზავნა" in panel._confirm_banner.text()
    assert "rgb(190,25,25)" in panel._confirm_banner.styleSheet()
    assert panel._agent_buttons["yes"].isVisible() and panel._agent_buttons["no"].isVisible()
    banner_y = panel._confirm_banner.geometry().top()
    panel.layout().activate()
    assert banner_y <= panel._agent_buttons["yes"].mapTo(panel, panel._agent_buttons["yes"].rect().topLeft()).y()
    panel.show_agent_prompt("private", "აკრიფე")
    assert not panel._confirm_banner.isVisible() and panel._agent_buttons["ready"].isVisible()
    panel.show_agent_prompt("", "")
    assert not any(b.isVisible() for b in panel._agent_buttons.values())
    panel.set_agent_running(False)


def test_panel_and_stop_window_never_cover_the_target_or_each_other(panel):
    # The real sequence from main.py: the panel docks first, then the stop
    # window re-places itself, for targets in every corner and the middle.
    from ui.stop_window import StopWindow
    stop = StopWindow(avoid_rects=lambda: [panel.frameGeometry()])
    panel.set_keep_clear_provider(lambda: [stop.frameGeometry()])
    stop.set_running(True)
    panel.set_agent_running(True)
    a = QApplication.primaryScreen().availableGeometry()
    assert panel.height() <= a.height() * 0.8 + 1
    for target in [(a.right() - 40, a.bottom() - 40), (a.left() + 40, a.bottom() - 40),
                   (a.right() - 40, a.top() + 40), (a.left() + 40, a.top() + 40),
                   (a.center().x(), a.bottom() - 40)]:
        panel.dock_away_from(*target)
        stop.avoid_target(*target)
        pg, sg = panel.frameGeometry(), stop.frameGeometry()
        pad = TARGET_CLEARANCE
        assert not pg.adjusted(-pad, -pad, pad, pad).contains(int(target[0]), int(target[1])), target
        assert not sg.adjusted(-pad, -pad, pad, pad).contains(int(target[0]), int(target[1])), target
        assert not pg.intersects(sg), (target, pg, sg)
    stop.set_running(False)
    panel.set_agent_running(False)


def test_dock_does_nothing_outside_a_run(panel):
    before = panel.pos()
    panel.dock_away_from(10, 10)
    assert panel.pos() == before


# ── overlay caption for the autopilot's words ────────────────────────────────

def test_agent_caption_uses_its_own_header_and_times_out(app):
    from ui.overlay import CAPTION_HEADER, CursorOverlay
    o = CursorOverlay()
    o.show_agent_caption("ვხსნი ბრაუზერს")
    assert o._caption_text == "ვხსნი ბრაუზერს"
    assert o._caption_header == strings_ka.AGENT_CAPTION_HEADER
    assert time.monotonic() < o._caption_hide_at < time.monotonic() + 30
    o.show_caption("სად არის ღილაკი?")                      # the user's words: unchanged
    assert o._caption_header == CAPTION_HEADER and o._caption_hide_at == float("inf")
