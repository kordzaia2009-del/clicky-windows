"""
Startup settings — a small window shown every time the app starts, with the
three main settings in large Georgian type:

  • which model answers (plus a button for the API keys),
  • whether the chat panel is on screen,
  • whether what the person said appears in the blue box at the bottom.

The choices are remembered in %LOCALAPPDATA%\\Clicky\\startup_settings.json
and applied at once on the next start, before the window even appears, so
closing it (× or „დაწყება“) always leaves the app in a sensible state.
CLICKY_STARTUP_SETTINGS=0 skips the window (the saved choices still apply).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from ui import strings_ka as S

log = logging.getLogger("clicky.startup")

FILE_NAME = "startup_settings.json"


@dataclass
class StartupSettings:
    model: Optional[str] = None          # None = the provider's default
    show_chat_panel: bool = False        # the mascot + island are the default UI
    show_said_caption: bool = True       # „თქვენ თქვით:“ box at the bottom
    extra: dict = field(default_factory=dict)   # unknown keys survive a save


def settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Clicky" / FILE_NAME


def load_settings(path: Optional[Path] = None) -> StartupSettings:
    path = path or settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return StartupSettings()
    except Exception:
        log.warning("could not read %s — using defaults", path, exc_info=True)
        return StartupSettings()
    if not isinstance(data, dict):
        return StartupSettings()
    known = {"model", "show_chat_panel", "show_said_caption"}
    model = data.get("model")
    return StartupSettings(
        model=model if isinstance(model, str) and model.strip() else None,
        show_chat_panel=bool(data.get("show_chat_panel", False)),
        show_said_caption=bool(data.get("show_said_caption", True)),
        extra={k: v for k, v in data.items() if k not in known},
    )


def save_settings(s: StartupSettings, path: Optional[Path] = None) -> bool:
    path = path or settings_path()
    data = dict(s.extra)
    data.update({k: v for k, v in asdict(s).items() if k != "extra"})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:
        log.warning("could not save %s", path, exc_info=True)
        return False


def enabled() -> bool:
    return os.getenv("CLICKY_STARTUP_SETTINGS", "1").strip().lower() not in ("0", "false", "no")


def _font(px: int, bold: bool = False) -> QFont:
    f = QFont()
    f.setFamilies(["Segoe UI", "Sylfaen"])        # Sylfaen: Georgian fallback
    f.setPixelSize(px)
    f.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    return f


class StartupSettingsDialog(QDialog):
    """Emits `applied(StartupSettings)` once, when the person presses
    „დაწყება“ or closes the window."""

    applied = pyqtSignal(object)
    api_keys_requested = pyqtSignal()

    def __init__(self, settings: StartupSettings,
                 models: Sequence[Tuple[str, str]] = (), provider: str = "",
                 parent=None):
        super().__init__(parent)
        self._settings = settings
        self._done = False
        self.setWindowTitle(S.STARTUP_TITLE)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumWidth(600)
        self.setStyleSheet(
            "QDialog { background: #1c1d24; color: #f2f3f7; }"
            "QLabel { color: #f2f3f7; }"
            "QComboBox { background: #2a2c36; color: #f2f3f7; border: 1px solid #4a4d5c;"
            " border-radius: 8px; padding: 8px 12px; }"
            "QComboBox QAbstractItemView { background: #2a2c36; color: #f2f3f7;"
            " selection-background-color: #3380ff; }"
            "QCheckBox { color: #f2f3f7; spacing: 14px; }"
            "QCheckBox::indicator { width: 28px; height: 28px; border-radius: 7px;"
            " border: 2px solid #8a8fa3; background: #2a2c36; }"
            # ticked = solid green, easy to tell apart at a glance
            "QCheckBox::indicator:checked { background: #2e9e57; border-color: #5fd68b; }"
            "QPushButton { background: #2a2c36; color: #f2f3f7; border: 1px solid #4a4d5c;"
            " border-radius: 10px; padding: 10px 18px; }"
            "QPushButton:hover { background: #343744; }"
            "QPushButton#start { background: #2e9e57; border: none; }"
            "QPushButton#start:hover { background: #36b465; }"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel(S.STARTUP_TITLE)
        title.setFont(_font(28, bold=True))
        root.addWidget(title)

        # 1) model / API key
        model_label = QLabel(S.STARTUP_MODEL_LABEL + (f"  ({provider})" if provider else ""))
        model_label.setFont(_font(20, bold=True))
        root.addWidget(model_label)
        row = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setFont(_font(19))
        self.model_combo.setMinimumHeight(48)
        row.addWidget(self.model_combo, stretch=1)
        self.keys_button = QPushButton(S.STARTUP_API_KEYS)
        self.keys_button.setFont(_font(18))
        self.keys_button.setMinimumHeight(48)
        self.keys_button.clicked.connect(self.api_keys_requested.emit)
        row.addWidget(self.keys_button)
        root.addLayout(row)
        hint = QLabel(S.STARTUP_MODEL_HINT)
        hint.setFont(_font(15))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #aab0c4;")
        root.addWidget(hint)
        self.set_models(models, settings.model)

        # 2) chat panel, 3) the blue box
        self.panel_check = QCheckBox(S.STARTUP_SHOW_PANEL)
        self.panel_check.setFont(_font(20))
        self.panel_check.setChecked(settings.show_chat_panel)
        root.addWidget(self.panel_check)
        self.caption_check = QCheckBox(S.STARTUP_SHOW_CAPTION)
        self.caption_check.setFont(_font(20))
        self.caption_check.setChecked(settings.show_said_caption)
        root.addWidget(self.caption_check)

        root.addSpacing(6)
        self.start_button = QPushButton(S.STARTUP_START)
        self.start_button.setObjectName("start")
        self.start_button.setFont(_font(24, bold=True))
        self.start_button.setMinimumHeight(64)
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self.accept)
        root.addWidget(self.start_button)

    # ── model list ────────────────────────────────────────────────────────────

    def set_models(self, models: Sequence[Tuple[str, str]], current: Optional[str] = None):
        """(model id, label) pairs. Keeps `current` (or the present pick)."""
        keep = current or self.model_combo.currentData()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for mid, label in models:
            self.model_combo.addItem(label or mid, userData=mid)
        if not models:
            self.model_combo.addItem(S.STARTUP_MODEL_DEFAULT, userData=None)
        idx = self.model_combo.findData(keep) if keep else -1
        self.model_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.model_combo.blockSignals(False)

    # ── result ────────────────────────────────────────────────────────────────

    def current(self) -> StartupSettings:
        mid = self.model_combo.currentData()
        return StartupSettings(
            model=mid if isinstance(mid, str) and mid else None,
            show_chat_panel=self.panel_check.isChecked(),
            show_said_caption=self.caption_check.isChecked(),
            extra=dict(self._settings.extra),
        )

    def _finish(self):
        if not self._done:
            self._done = True
            self.applied.emit(self.current())

    def accept(self):
        self._finish()
        super().accept()

    def reject(self):                    # × or Esc: keep what is selected
        self._finish()
        super().reject()


def model_choices(provider: str) -> List[Tuple[str, str]]:
    """The model list the panel offers, as (id, label) pairs."""
    if provider in ("claude", "openai", "gemini"):
        try:
            from ai.model_registry import cached_models
            out = []
            for m in cached_models(provider):
                label = m.get("label") or m["id"]
                if not m.get("vision"):
                    label += "  (ეკრანს ვერ ხედავს)"
                out.append((m["id"], label))
            return out
        except Exception:
            log.debug("model list for %s failed", provider, exc_info=True)
    return []
