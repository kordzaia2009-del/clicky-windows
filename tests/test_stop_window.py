"""Stop window placement (pure) and the widget itself on Qt's offscreen
platform — nothing is shown on the real screen."""

import pytest
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtWidgets import QApplication

from ui import strings_ka
from ui.stop_window import (
    TARGET_CLEARANCE, StopWindow, _hit, _overlaps, candidate_positions, choose_position,
)

SCREEN = (0, 0, 1280, 680)          # available area above a 40 px taskbar
SIZE = (300, 150)


def rect(pos):
    return (pos[0], pos[1], *SIZE)


# ── choose_position ──────────────────────────────────────────────────────────

def test_default_is_top_centre():
    assert choose_position(SCREEN, SIZE) == candidate_positions(SCREEN, SIZE)[0]


def test_moves_away_from_a_target_under_it():
    top_centre = candidate_positions(SCREEN, SIZE)[0]
    target = (top_centre[0] + 150, top_centre[1] + 75)          # dead centre of it
    pos = choose_position(SCREEN, SIZE, target=target, current=top_centre)
    assert pos != top_centre
    assert not _hit(rect(pos), target, TARGET_CLEARANCE)


def test_keeps_its_place_when_the_target_is_elsewhere():
    current = candidate_positions(SCREEN, SIZE)[2]               # top left
    assert choose_position(SCREEN, SIZE, target=(900, 500), current=current) == current


def test_never_overlaps_the_panel():
    panel = (880, 100, 380, 560)                                 # docked bottom-right
    for target in [(640, 90), (100, 60), (60, 600), (640, 600), (1200, 400)]:
        pos = choose_position(SCREEN, SIZE, target=target, avoid=[panel])
        assert not _overlaps(rect(pos), panel), target
        assert not _hit(rect(pos), target, TARGET_CLEARANCE), target


def test_target_clearance_beats_the_panel_when_both_cannot_be_avoided():
    panel = (0, 0, 1280, 680)                                    # covers everything
    target = (640, 90)
    pos = choose_position(SCREEN, SIZE, target=target, avoid=[panel])
    assert not _hit(rect(pos), target, TARGET_CLEARANCE)


def test_stays_on_screen():
    for pos in candidate_positions(SCREEN, SIZE):
        x, y, w, h = rect(pos)
        assert 0 <= x and x + w <= 1280 and 0 <= y and y + h <= 680


# ── the widget (offscreen) ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_window_does_not_take_focus_and_is_not_click_through(app):
    w = StopWindow()
    flags = w.windowFlags()
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert not flags & Qt.WindowType.WindowTransparentForInput
    assert w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert not w.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert w.button.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert w.button.text() == strings_ka.STOP_WINDOW_BUTTON


def test_shown_only_while_running_and_button_emits_stop(app):
    w = StopWindow()
    stops = []
    w.on_stop.connect(lambda: stops.append(1))
    assert not w.isVisible()
    w.set_running(True)
    assert w.isVisible()
    w.button.click()
    assert stops == [1]
    w.set_running(False)
    assert not w.isVisible()


def test_moves_out_of_the_way_of_a_target(app):
    w = StopWindow()
    w.set_running(True)
    g = w.frameGeometry()
    w.avoid_target(g.center().x(), g.center().y())
    g2 = w.frameGeometry()
    assert not g2.adjusted(-TARGET_CLEARANCE, -TARGET_CLEARANCE,
                           TARGET_CLEARANCE, TARGET_CLEARANCE).contains(g.center())
    w.set_running(False)


def test_avoids_the_panel_rect_it_is_given(app):
    screen = QApplication.primaryScreen().availableGeometry()
    panel = QRect(screen.x(), screen.y(), screen.width(), 200)      # the whole top band
    w = StopWindow(avoid_rects=lambda: [panel])
    w.set_running(True)
    assert not w.frameGeometry().intersects(panel)
    w.set_running(False)


# ── panel button label ───────────────────────────────────────────────────────

def test_panel_button_is_a_stop_button_for_the_whole_run(app):
    from ui.panel import BUTTON_LABELS, AppState, CompanionPanel
    p = CompanionPanel()
    p.set_agent_running(True)
    for state in (AppState.IDLE, AppState.LISTENING, AppState.THINKING, AppState.IDLE):
        p.set_state(state)          # e.g. waiting for დიახ: the app itself is IDLE
        assert p._ptt_btn.text() == strings_ka.AGENT_STOP_BUTTON
    p.set_agent_running(False)
    assert p._ptt_btn.text() == BUTTON_LABELS[AppState.IDLE]
