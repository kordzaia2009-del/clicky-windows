import asyncio
import time
from enum import Enum, auto
from typing import Callable, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy, QComboBox, QFrame, QLineEdit,
    QButtonGroup,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPropertyAnimation, QEasingCurve
from PyQt6.QtGui import QColor, QPainter, QPen, QBrush, QFont, QCursor, QPixmap

from ui.design import (
    PANEL_QSS, PANEL_WIDTH, PANEL_HEIGHT, PANEL_RADIUS, RUN_PANEL_WIDTH, RUN_PANEL_HEIGHT,
    STATE_IDLE, STATE_LISTENING, STATE_THINKING, STATE_SPEAKING,
    FONT_TITLE, FONT_STATUS, FONT_RESPONSE, FONT_LABEL,
    SURFACE, TEXT_SECONDARY, BORDER, ANIM_FAST_MS
)
from config import cfg
from ui import strings_ka


class AppState(Enum):
    IDLE      = auto()
    LISTENING = auto()
    THINKING  = auto()
    SPEAKING  = auto()


def _hotkey_label() -> str:
    """Human-readable hotkey from config, e.g. 'ctrl+win' -> 'Ctrl+Win'."""
    try:
        from config import cfg
        return "+".join(p.strip().capitalize() for p in cfg.hotkey.split("+"))
    except Exception:
        return "Ctrl+Win"


STATE_LABELS = {
    AppState.IDLE:      f"თქვით „კლიპიტაური“ ან დააჭირეთ {_hotkey_label()}",
    AppState.LISTENING: "გისმენთ...",
    AppState.THINKING:  "ვფიქრობ...",
    AppState.SPEAKING:  "გპასუხობთ...",
}

LOGO_SIZE = 40


def _logo_label() -> Optional[QLabel]:
    """The panel's logo, from assets/logo.png.

    Looked up next to the app first, so the logo can be swapped by replacing
    that one file (no rebuild), then inside a PyInstaller bundle. Missing or
    unreadable = no logo, never an error.
    """
    import sys
    from pathlib import Path
    from config import _HERE

    candidates = [_HERE / "assets" / "logo.png"]
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys._MEIPASS) / "assets" / "logo.png")
    for path in candidates:
        pix = QPixmap(str(path))
        if not pix.isNull():
            label = QLabel()
            label.setPixmap(pix.scaled(
                LOGO_SIZE, LOGO_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            label.setFixedSize(LOGO_SIZE, LOGO_SIZE)
            return label
    return None


# The big button's label follows the state: it starts listening, finishes
# early while listening, and stops a long answer while thinking/speaking.
BUTTON_LABELS = {
    AppState.IDLE:      "🎤  დააჭირეთ და ილაპარაკეთ",
    AppState.LISTENING: "■  დავამთავრე",
    AppState.THINKING:  "■  გაჩერება",
    AppState.SPEAKING:  "■  გაჩერება",
}

AGENT_STATE_LABELS = {
    "planning": "ვგეგმავ…",
    "acting": "ვასრულებ…",
    "verifying": "ვამოწმებ…",
    "replanning": "სხვა გზას ვეძებ…",
    "awaiting_confirm": "გელოდებით: დიახ ან არა",
    "awaiting_private": "გელოდებით: მზად ვარ",
}

STATE_COLORS = {
    AppState.IDLE:      STATE_IDLE,
    AppState.LISTENING: STATE_LISTENING,
    AppState.THINKING:  STATE_THINKING,
    AppState.SPEAKING:  STATE_SPEAKING,
}


class WaveformWidget(QWidget):
    """Animated waveform bars shown while listening."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self._levels = [0.0] * 12
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._decay)

    def set_level(self, rms: float):
        import random
        peak = min(1.0, rms * 3)
        self._levels = [min(1.0, peak * (0.4 + random.random() * 0.6)) for _ in self._levels]
        self.update()

    def _decay(self):
        self._levels = [max(0.0, v * 0.85) for v in self._levels]
        self.update()

    def start(self):
        self._timer.start(50)

    def stop(self):
        self._timer.stop()
        self._levels = [0.0] * 12
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bar_w = max(3, (w - 4 * len(self._levels)) // len(self._levels))
        spacing = (w - bar_w * len(self._levels)) // (len(self._levels) + 1)
        for i, level in enumerate(self._levels):
            x = spacing + i * (bar_w + spacing)
            bar_h = max(4, int(level * (h - 8)))
            y = (h - bar_h) // 2
            alpha = max(80, int(level * 255))
            color = QColor(0, 120, 255, alpha)
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(x, y, bar_w, bar_h, bar_w // 2, bar_w // 2)
        painter.end()


PROVIDER_LABELS = {
    "claude":  "Claude",
    "openai":  "GPT-4o",
    "gemini":  "Gemini",
    "copilot": "Copilot",
    "ollama":  f"Ollama ({cfg.ollama_model})",
}

# Provider model lists are fetched live from each vendor's /models endpoint
# (see ai/model_registry.py for Claude/OpenAI/Gemini and
# ai/github_copilot_provider.py for Copilot). The hardcoded lists below are
# only used as offline fallbacks when no cache exists yet.


def _copilot_model_choices() -> list[tuple[str, str]]:
    """Returns [(model_id, display_label), ...] for the dropdown.
    Free models first, then ascending multiplier. Display shows '(free)' /
    '(1×)' so the user always knows what burns premium quota."""
    try:
        from ai.github_copilot_provider import (
            cached_models, sorted_model_ids, model_label,
        )
    except Exception:
        return [("gpt-4o-mini", "gpt-4o-mini  (free)")]
    out = []
    for mid in sorted_model_ids():
        out.append((mid, model_label(mid)))
    if not out:
        out.append(("gpt-4o-mini", "gpt-4o-mini  (free)"))
    return out


class ProviderBadge(QLabel):
    """Small pill showing active provider."""

    def __init__(self, provider: str, parent=None):
        super().__init__(parent)
        self.set_provider(provider)
        self.setStyleSheet(
            "background: rgba(0,120,255,25); border: 1px solid rgba(0,120,255,100);"
            "border-radius: 8px; color: rgb(140,180,255); font-size: 11px; padding: 2px 8px;"
        )

    def set_provider(self, provider: str):
        self.setText(PROVIDER_LABELS.get(provider, provider))


class CompanionPanel(QWidget):
    """Floating companion control panel — equivalent to CompanionPanelView.swift."""

    on_ask_clicked           = pyqtSignal()        # big button
    on_text_submitted        = pyqtSignal(str)     # typed question
    on_shown                 = pyqtSignal()        # lets the pointer re-raise above us
    on_agent_reply           = pyqtSignal(str)     # "yes" | "no" | "ready"
    on_agent_mode_changed    = pyqtSignal(bool)    # True = „გააკეთე“ (do)
    on_agent_lost            = pyqtSignal()        # „დავიბენი“
    _sig_question            = pyqtSignal(str)
    _sig_error               = pyqtSignal(str)
    on_model_changed         = pyqtSignal(str)
    on_ollama_refresh_requested = pyqtSignal()
    on_document_dropped      = pyqtSignal(str)
    _sig_copilot_code        = pyqtSignal(str, str)   # (user_code, verification_uri)
    _sig_copilot_error       = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._state = AppState.IDLE
        self._agent_running = False
        self._agent_goals = []
        self._agent_goal_idx = 0
        self._agent_steps = []
        self._agent_step_idx = 0
        self._keep_clear = lambda: []      # rects the panel must not cover (stop window)
        self._response_text = ""
        self._ollama_installed: dict[str, list[str]] = {"vision": [], "text": []}
        self._ollama_pick: Optional[str] = None
        self._ollama_polled_at = 0.0
        self._setup_window()
        self._build_ui()
        self._position_bottom_right()
        # Wire internal thread-safe signals → main-thread slots
        self._sig_copilot_code.connect(self._on_copilot_code)
        self._sig_copilot_error.connect(self._on_copilot_error)
        self._sig_question.connect(self._on_question)
        self._sig_error.connect(self._on_error)

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Popping up with an answer must not pull focus out of the app the
        # user is working in.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(PANEL_WIDTH, PANEL_HEIGHT)
        self.setObjectName("panel")
        self.setStyleSheet(PANEL_QSS)
        self.setAcceptDrops(True)   # drag-drop PDFs / DOCX / TXT

    # ── Drag-drop handlers ───────────────────────────────────────────────────
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                self.on_document_dropped.emit(path)
        event.acceptProposedAction()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        # Header
        header = QHBoxLayout()
        logo = _logo_label()
        if logo is not None:
            header.addWidget(logo)
            header.addSpacing(6)
        title = QLabel("კლიპიტაური")
        title.setObjectName("title")
        title.setFont(FONT_TITLE)
        header.addWidget(title)
        header.addStretch()
        provider = cfg.llm_provider()
        self._badge = ProviderBadge(provider)
        header.addWidget(self._badge)

        # Minimize button — hides the panel back to tray
        self._min_btn = QPushButton("—")
        self._min_btn.setFixedSize(24, 24)
        self._min_btn.setStyleSheet(
            "QPushButton { background: rgba(60,60,75,180); color: rgb(220,220,230);"
            "border: none; border-radius: 12px; font-size: 14px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(80,80,95,220); }"
        )
        self._min_btn.setToolTip("პანელის დამალვა (ხელახლა გასახსნელად — ხატულა საათთან)")
        self._min_btn.clicked.connect(self.hide)
        header.addWidget(self._min_btn)
        root.addLayout(header)

        # Mode switch: „მაჩვენე“ (guide) / „გააკეთე“ (do) — synced with the tray
        mode_row = QHBoxLayout()
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_buttons = {}
        for do, text, tip in ((False, strings_ka.MODE_GUIDE, strings_ka.MODE_GUIDE_TIP),
                              (True, strings_ka.MODE_DO, strings_ka.MODE_DO_TIP)):
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("QPushButton { background: rgba(50,50,62,230); color: rgb(215,215,228); border: 1px solid rgba(90,90,110,200); border-radius: 10px; font-size: 18px; font-weight: 700; padding: 6px; }QPushButton:checked { background: rgb(255,225,77); color: black; border-color: rgb(255,225,77); }")
            btn.clicked.connect(lambda _=False, d=do: self._on_mode_clicked(d))
            self._mode_group.addButton(btn)
            mode_row.addWidget(btn)
            self._mode_buttons[do] = btn
        self._mode_buttons[cfg.agent_mode == "do"].setChecked(True)
        root.addLayout(mode_row)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet("color: rgba(60,60,75,180);")
        root.addWidget(div)

        # Status row
        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet(f"color: rgb({STATE_IDLE.red()},{STATE_IDLE.green()},{STATE_IDLE.blue()}); font-size: 10px;")
        self._status_label = QLabel(STATE_LABELS[AppState.IDLE])
        self._status_label.setObjectName("status")
        self._status_label.setFont(FONT_STATUS)
        status_row = QHBoxLayout()
        status_row.addWidget(self._status_dot)
        status_row.addWidget(self._status_label)
        status_row.addStretch()
        root.addLayout(status_row)

        # Waveform
        self._waveform = WaveformWidget()
        self._waveform.setVisible(False)
        root.addWidget(self._waveform)

        # Plan checklist (autopilot runs only): 24 px, high contrast
        self._checklist = QLabel()
        self._checklist.setTextFormat(Qt.TextFormat.RichText)
        self._checklist.setWordWrap(True)
        self._checklist.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._checklist.setStyleSheet("background: rgb(10,10,14); padding: 8px;"
                                      "border-radius: 8px;")
        self._checklist_scroll = QScrollArea()
        self._checklist_scroll.setWidgetResizable(True)
        self._checklist_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._checklist_scroll.setWidget(self._checklist)
        self._checklist_scroll.setVisible(False)
        root.addWidget(self._checklist_scroll, stretch=2)

        # The question being answered (spoken or typed)
        self._question_label = QLabel()
        self._question_label.setWordWrap(True)
        self._question_label.setStyleSheet(
            "color: rgb(150,185,255); font-size: 14px; font-weight: 600; padding: 0 8px;"
        )
        self._question_label.setVisible(False)
        root.addWidget(self._question_label)

        # Response area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._response_label = QLabel()
        self._response_label.setObjectName("response")
        self._response_label.setFont(FONT_RESPONSE)
        self._response_label.setWordWrap(True)
        self._response_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._response_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._response_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scroll.setWidget(self._response_label)
        root.addWidget(scroll, stretch=1)

        # Red banner for a "confirm" action, above დიახ / არა
        self._confirm_banner = QLabel()
        self._confirm_banner.setWordWrap(True)
        self._confirm_banner.setStyleSheet(
            "background: rgb(190,25,25); color: white; border-radius: 8px;"
            "padding: 10px; font-size: 18px; font-weight: 700;"
        )
        self._confirm_banner.setVisible(False)
        root.addWidget(self._confirm_banner)

        # Autopilot answers — shown only while it waits for the person
        agent_row = QHBoxLayout()
        self._agent_buttons = {}
        for value, text, color in (("yes", "დიახ", "rgb(30,150,80)"),
                                    ("no", "არა", "rgb(170,60,60)"),
                                    ("ready", "მზად ვარ", "rgb(0,110,235)")):
            btn = QPushButton(text)
            btn.setFixedHeight(40)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: {color}; color: white; border: none;"
                "border-radius: 8px; font-size: 15px; font-weight: 600; }}"
            )
            btn.clicked.connect(lambda _=False, v=value: self.on_agent_reply.emit(v))
            btn.setVisible(False)
            agent_row.addWidget(btn)
            self._agent_buttons[value] = btn
        root.addLayout(agent_row)

        # Typed question — works even when speech recognition can't
        type_row = QHBoxLayout()
        self._text_input = QLineEdit()
        self._text_input.setPlaceholderText("ან აქ დაწერეთ კითხვა…")
        self._text_input.setStyleSheet(
            "QLineEdit { background: rgba(40,40,50,220); border: 1px solid rgba(80,80,100,200);"
            "border-radius: 8px; color: rgb(235,235,245); padding: 8px; font-size: 14px; }"
            "QLineEdit:focus { border-color: rgba(0,120,255,220); }"
        )
        self._text_input.returnPressed.connect(self._submit_text)
        send_btn = QPushButton("გაგზავნა")
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet(
            "QPushButton { background: rgb(0,110,235); color: white; border: none;"
            "border-radius: 8px; padding: 8px 12px; font-size: 13px; font-weight: 600; }"
            "QPushButton:hover { background: rgb(30,135,255); }"
        )
        send_btn.clicked.connect(self._submit_text)
        type_row.addWidget(self._text_input, stretch=1)
        type_row.addWidget(send_btn)
        root.addLayout(type_row)

        # Talk button — the same as tapping the hotkey
        self._ptt_btn = QPushButton(BUTTON_LABELS[AppState.IDLE])
        self._ptt_btn.setObjectName("hotkey_btn")
        self._ptt_btn.setFont(FONT_LABEL)
        self._ptt_btn.setFixedHeight(48)
        self._ptt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ptt_btn.setToolTip(f"ან თქვით „კლიპიტაური“, ან დააჭირეთ {_hotkey_label()}")
        self._ptt_btn.clicked.connect(self.on_ask_clicked)
        self._lost_btn = QPushButton(strings_ka.LOST_BUTTON)
        self._lost_btn.setToolTip(strings_ka.LOST_TIP)
        self._lost_btn.setFixedHeight(48)
        self._lost_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lost_btn.setStyleSheet(
            "QPushButton { background: rgb(255,170,30); color: black; border: none;"
            "border-radius: 10px; font-size: 17px; font-weight: 700; padding: 0 12px; }"
        )
        self._lost_btn.clicked.connect(self.on_agent_lost)
        self._lost_btn.setVisible(False)
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self._lost_btn)
        bottom_row.addWidget(self._ptt_btn, stretch=1)
        root.addLayout(bottom_row)

        # Footer: model selector + provider info
        footer = QHBoxLayout()
        lbl = QLabel("მოდელი:")
        lbl.setFont(FONT_LABEL)
        lbl.setStyleSheet("color: rgb(100,100,120); font-size: 11px;")
        self._model_combo = QComboBox()
        self._model_combo.setStyleSheet(
            "background: rgba(40,40,50,200); border: 1px solid rgba(60,60,75,180);"
            "border-radius: 6px; color: rgb(200,200,215); padding: 2px 6px; font-size: 11px;"
        )
        self._populate_models()
        self._model_combo.activated.connect(
            lambda _i: self._remember_ollama_pick()
        )
        # Emit the model id (stored in userData), not the display label
        self._model_combo.currentIndexChanged.connect(
            lambda _idx: self.on_model_changed.emit(
                self._model_combo.currentData() or self._model_combo.currentText()
            )
        )
        footer.addWidget(lbl)
        footer.addWidget(self._model_combo, stretch=1)
        root.addLayout(footer)
        self._footer_widgets = [lbl, self._model_combo]   # hidden during a run

    def _populate_models(self):
        self._set_models_for(cfg.llm_provider())

    def _set_models_for(self, provider: str):
        # Remember what the user picked: a background refresh rebuilds this list
        # (daily now), and re-selecting the first entry every time would quietly
        # switch them off the model they chose.
        previous = self._model_combo.currentData() if self._model_combo.count() else None
        # Avoid firing on_model_changed while we rebuild
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        if provider == "copilot":
            for mid, label in _copilot_model_choices():
                self._model_combo.addItem(label, userData=mid)
        elif provider in ("claude", "openai", "gemini"):
            try:
                from ai.model_registry import cached_models
                for m in cached_models(provider):
                    label = m["id"]
                    if not m.get("vision"):
                        label += "  (ეკრანს ვერ ხედავს)"
                    self._model_combo.addItem(label, userData=m["id"])
            except Exception:
                self._model_combo.addItem("ნაგულისხმევი", userData="default")
        else:   # ollama
            self._fill_ollama_models()
            # A pick made in the tray menu wins over what the dropdown held.
            previous = self._ollama_pick or previous
        if previous:
            idx = self._model_combo.findData(previous)
            if idx >= 0:
                self._model_combo.setCurrentIndex(idx)
        self._model_combo.blockSignals(False)
        # Fire once with the new default model id (NOT the display label) so
        # the manager picks it up — important when label != id.
        if self._model_combo.count():
            self.on_model_changed.emit(self._model_combo.currentData() or self._model_combo.currentText())

    def _remember_ollama_pick(self):
        if cfg.llm_provider() == "ollama":
            self._ollama_pick = self._model_combo.currentData()

    def _fill_ollama_models(self):
        """List what is actually installed in Ollama, not just the configured
        model — otherwise a model the user pulls later can never be chosen here
        (the panel's selection is what gets sent with every question)."""
        wanted = cfg.get_ollama_model("vision") or cfg.ollama_model
        shown = set()
        for name in self._ollama_installed.get("vision", []):
            self._model_combo.addItem(name, userData=name)
            shown.add(name)
        for name in self._ollama_installed.get("text", []):
            self._model_combo.addItem(f"{name}  (მხოლოდ ტექსტი)", userData=name)
            shown.add(name)
        if wanted not in shown:
            label = wanted if not shown else f"{wanted}  (არ არის დაყენებული)"
            self._model_combo.insertItem(0, label, userData=wanted)
        idx = self._model_combo.findData(wanted)
        if idx >= 0:
            self._model_combo.setCurrentIndex(idx)

    def set_ollama_models(self, classified: dict):
        """Installed Ollama models arrived (startup, panel open, tray refresh)."""
        self._ollama_installed = {
            "vision": list(classified.get("vision", [])),
            "text":   list(classified.get("text", [])),
        }
        if cfg.llm_provider() == "ollama":
            self._set_models_for("ollama")

    def select_ollama_model(self, name: str):
        """The model was chosen from the tray menu — mirror it here."""
        self._ollama_pick = name
        if cfg.llm_provider() == "ollama":
            self._set_models_for("ollama")

    def showEvent(self, event):
        super().showEvent(event)
        self.on_shown.emit()
        # A model pulled while Clicky was running should appear without a
        # restart; a throttled re-poll on open keeps the list current.
        if cfg.llm_provider() == "ollama":
            now = time.monotonic()
            if now - self._ollama_polled_at > 5:
                self._ollama_polled_at = now
                self.on_ollama_refresh_requested.emit()

    def refresh_for_provider(self, provider: str):
        """Called from outside when the active provider is switched at runtime."""
        self._badge.set_provider(provider)
        self._set_models_for(provider)

    def _position_bottom_right(self):
        # Current size: the panel is larger while the autopilot runs.
        a = self._avail()
        x = a.right() - self.width() - 24
        y = max(a.top() + 16, a.bottom() - self.height() - 20)
        self.move(x, y)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_state(self, state: AppState):
        self._state = state
        color = STATE_COLORS[state]
        self._status_dot.setStyleSheet(
            f"color: rgb({color.red()},{color.green()},{color.blue()}); font-size: 10px;"
        )
        self._status_label.setText(STATE_LABELS[state])
        self._update_ptt_label()
        self._waveform.setVisible(state == AppState.LISTENING)
        if state == AppState.LISTENING:
            self._waveform.start()
        else:
            self._waveform.stop()

    def update_response(self, text: str):
        """Append streaming text chunk."""
        self._response_text = text
        self._response_label.setText(text)

    def append_response_chunk(self, chunk: str):
        self._response_text += chunk
        self._response_label.setText(self._response_text)

    def _submit_text(self):
        text = self._text_input.text().strip()
        if not text:
            return
        if self._state != AppState.IDLE:
            # Busy answering — keep the text so it isn't lost.
            self._status_label.setText("ერთი წუთით — ჯერ წინა კითხვას ვპასუხობ.")
            return
        self._text_input.clear()
        self.on_text_submitted.emit(text)

    def show_question(self, text: str):
        """Thread-safe: a new question starts — show it, clear the old answer."""
        self._sig_question.emit(text)

    def show_error(self, text: str):
        """Thread-safe: show an error in the answer area, not just a toast."""
        self._sig_error.emit(text)

    def _on_question(self, text: str):
        self._question_label.setText(f"თქვენ: {text}")
        self._question_label.setVisible(True)
        self.clear_response()
        if not self.isVisible():
            self.show()

    def _on_error(self, text: str):
        if self._response_text:
            self._response_text += "\n\n"
        self._response_text += f"⚠️ {text}"
        self._response_label.setText(self._response_text)
        if not self.isVisible():
            self.show()

    # ── Autopilot ─────────────────────────────────────────────────────────────

    def set_agent_running(self, running: bool):
        """While the autopilot runs the big button is a stop button, even
        when the app itself is idle (waiting for დიახ / მზად ვარ), and the
        panel grows to show the checklist in large type."""
        self._agent_running = bool(running)
        self._update_ptt_label()
        self._lost_btn.setVisible(self._agent_running)
        self._checklist_scroll.setVisible(self._agent_running)
        for w in self._footer_widgets:
            w.setVisible(not self._agent_running)
        if self._agent_running:
            a = self._avail()
            # At most 80% of the screen height, so there is always room beside
            # it for the stop window and the target.
            self.setFixedSize(RUN_PANEL_WIDTH, min(RUN_PANEL_HEIGHT, int(a.height() * 0.8)))
            self._position_bottom_right()
        else:
            self._confirm_banner.setVisible(False)
            self._agent_steps, self._agent_goals = [], []
            self.setFixedSize(PANEL_WIDTH, PANEL_HEIGHT)
            self._position_bottom_right()

    def _avail(self):
        from PyQt6.QtWidgets import QApplication
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry()

    # mode switch
    def _on_mode_clicked(self, do: bool):
        self.on_agent_mode_changed.emit(do)

    def set_agent_mode(self, do: bool):
        """Mirror a change made elsewhere (tray) without re-emitting."""
        btn = self._mode_buttons[bool(do)]
        if not btn.isChecked():
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

    # checklist
    def show_agent_steps(self, goal_idx: int, steps):
        self._agent_goal_idx = goal_idx
        self._agent_steps = list(steps or [])
        self._agent_step_idx = 0
        self._render_checklist()

    def _render_checklist(self):
        from ui.agent_view import checklist_html
        goal = (self._agent_goals[self._agent_goal_idx].goal
                if self._agent_goal_idx < len(self._agent_goals) else "")
        self._checklist.setText(checklist_html(
            self._agent_steps, self._agent_step_idx, goal,
            self._agent_goal_idx, len(self._agent_goals) or 1))

    # docking
    def set_keep_clear_provider(self, provider):
        self._keep_clear = provider

    def dock_away_from(self, x: float, y: float):
        """During a run: move to the corner opposite the target, never over
        the target and never over the stop window."""
        if not self._agent_running or not self.isVisible():
            return
        from ui.agent_view import panel_dock_position
        a = self._avail()
        keep = [(r.x(), r.y(), r.width(), r.height()) for r in self._keep_clear()]
        g = self.frameGeometry()
        nx, ny = panel_dock_position((a.x(), a.y(), a.width(), a.height()),
                                     (g.width(), g.height()), (x, y), keep)
        if (nx, ny) != (self.x(), self.y()):
            self.move(nx, ny)

    def _update_ptt_label(self):
        self._ptt_btn.setText(strings_ka.AGENT_STOP_BUTTON if self._agent_running
                              else BUTTON_LABELS[self._state])
    # Connected to CompanionManager signals emitted from its worker thread;
    # Qt queues those onto the GUI thread.

    def show_agent_prompt(self, kind: str, question: str):
        """confirm -> red banner + დიახ / არა; private -> მზად ვარ; "" -> hide."""
        shown = {"confirm": ("yes", "no"), "private": ("ready",)}.get(kind, ())
        for value, btn in self._agent_buttons.items():
            btn.setVisible(value in shown)
        if kind == "confirm":
            self._confirm_banner.setText(f"{strings_ka.CONFIRM_BANNER_TITLE}\n{question}")
        self._confirm_banner.setVisible(kind == "confirm")
        if shown and not self.isVisible():
            self.show()

    def show_agent_say(self, text: str):
        if self._response_text:
            self._response_text += "\n"
        self._response_text += f"• {text}"
        self._response_label.setText(self._response_text)
        if not self.isVisible():
            self.show()

    def show_agent_plan(self, goals):
        self._agent_goals = list(goals or [])
        if self._agent_goals:
            self.show_agent_steps(0, self._agent_goals[0].steps)

    def show_agent_step(self, goal_idx: int, step_idx: int, title: str):
        if goal_idx != self._agent_goal_idx and goal_idx < len(self._agent_goals):
            self.show_agent_steps(goal_idx, self._agent_goals[goal_idx].steps)
        self._agent_step_idx = step_idx
        self._render_checklist()

    def set_agent_state(self, state: str, detail: str):
        label = AGENT_STATE_LABELS.get(state)
        if self._agent_steps and state in ("acting", "verifying"):
            from ui.agent_view import progress_text
            label = progress_text(self._agent_step_idx, len(self._agent_steps))
        if label:
            self._status_label.setText(label)
        elif state in ("done", "cancelled", "idle"):
            self._status_label.setText(STATE_LABELS[self._state])

    def set_audio_level(self, rms: float):
        self._waveform.set_level(rms)

    def clear_response(self):
        self._response_text = ""
        self._response_label.setText("")

    def show_copilot_code(self, user_code: str, verification_uri: str):
        """Thread-safe: can be called from any thread. Emits a queued signal
        so the UI update always runs on the Qt main thread."""
        self._sig_copilot_code.emit(user_code, verification_uri)

    def show_copilot_error(self, error: str):
        """Thread-safe version of showing a Copilot login error."""
        self._sig_copilot_error.emit(error)

    # ── Private slots (always run on Qt main thread) ──────────────────────────

    def _on_copilot_code(self, user_code: str, verification_uri: str):
        self.show()   # bring panel to front
        self.raise_()
        self._response_text = (
            "── GitHub Copilot-ში შესვლა ──\n\n"
            f"1.  გახსენით:  {verification_uri}\n\n"
            f"2.  შეიყვანეთ კოდი:\n\n"
            f"        {user_code}\n\n"
            "3.  GitHub-ში დააჭირეთ Authorize-ს.\n\n"
            "ავტორიზაციის შემდეგ კლიპიტაური ავტომატურად შევა."
        )
        self._response_label.setText(self._response_text)
        self._status_label.setText("ველოდები Copilot-ის ავტორიზაციას…")

    def _on_copilot_error(self, error: str):
        self._response_text = f"Copilot-ში შესვლა ვერ მოხერხდა:\n\n{error}"
        self._response_label.setText(self._response_text)

    # ── Mouse drag to reposition ──────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and hasattr(self, '_drag_pos'):
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    # ── Painting: rounded glass background ───────────────────────────────────
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor(18, 18, 22, 235)))
        painter.setPen(QPen(QColor(60, 60, 75, 180), 1))
        painter.drawRoundedRect(self.rect(), PANEL_RADIUS, PANEL_RADIUS)
        painter.end()
