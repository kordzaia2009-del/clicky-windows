"""Startup settings window and its saved file — offscreen, nothing shown."""

import json

import pytest
from PyQt6.QtWidgets import QApplication

from ui import startup_settings as ss


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_defaults_when_nothing_is_saved(tmp_path):
    s = ss.load_settings(tmp_path / "missing.json")
    assert s.model is None and s.show_chat_panel is False and s.show_said_caption is True


def test_save_and_load_round_trip_keeps_unknown_keys(tmp_path):
    path = tmp_path / "startup_settings.json"
    path.write_text(json.dumps({"future_option": 7}), encoding="utf-8")
    s = ss.load_settings(path)
    s.model, s.show_chat_panel, s.show_said_caption = "claude-haiku-5-5", True, False
    assert ss.save_settings(s, path)
    back = ss.load_settings(path)
    assert (back.model, back.show_chat_panel, back.show_said_caption) == ("claude-haiku-5-5", True, False)
    assert json.loads(path.read_text(encoding="utf-8"))["future_option"] == 7


def test_broken_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "startup_settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert ss.load_settings(path) == ss.StartupSettings()


def test_dialog_reports_what_is_selected(app):
    models = [("claude-sonnet-5-5", "Claude Sonnet 5.5"), ("claude-haiku-5-5", "Claude Haiku 5.5")]
    dlg = ss.StartupSettingsDialog(ss.StartupSettings(model="claude-haiku-5-5"), models, "claude")
    got = []
    dlg.applied.connect(got.append)
    assert dlg.model_combo.currentData() == "claude-haiku-5-5"
    dlg.panel_check.setChecked(True)
    dlg.caption_check.setChecked(False)
    dlg.accept()
    dlg.reject()                                   # a second close does not apply twice
    assert len(got) == 1
    assert (got[0].model, got[0].show_chat_panel, got[0].show_said_caption) == \
        ("claude-haiku-5-5", True, False)


def test_closing_with_x_applies_the_current_choices(app):
    dlg = ss.StartupSettingsDialog(ss.StartupSettings(), [("m1", "M1")], "claude")
    got = []
    dlg.applied.connect(got.append)
    dlg.reject()
    assert got and got[0].model == "m1" and got[0].show_said_caption is True


def test_model_list_can_be_refreshed_after_new_keys(app):
    dlg = ss.StartupSettingsDialog(ss.StartupSettings(), [], "")
    assert dlg.current().model is None                     # "default" only
    dlg.set_models([("a", "A"), ("b", "B")], "b")
    assert dlg.current().model == "b"


def test_panel_model_choices_and_select(app):
    from ui.panel import CompanionPanel
    p = CompanionPanel()
    p._model_combo.blockSignals(True)
    p._model_combo.clear()
    p._model_combo.addItem("Claude Sonnet 5.5", userData="claude-sonnet-5-5")
    p._model_combo.addItem("Claude Haiku 5.5", userData="claude-haiku-5-5")
    p._model_combo.blockSignals(False)
    picked = []
    p.on_model_changed.connect(picked.append)
    assert p.model_choices()[1] == ("claude-haiku-5-5", "Claude Haiku 5.5")
    assert p.select_model("claude-haiku-5-5") and picked[-1] == "claude-haiku-5-5"
    assert p.select_model("claude-haiku-5-5") and picked[-1] == "claude-haiku-5-5"   # already selected
    assert not p.select_model("nope")
