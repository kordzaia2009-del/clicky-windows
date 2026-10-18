"""
Clicky for Windows — Entry Point.
Boots Qt, spawns overlay+panel+tray, starts ambient mic listener, binds hotkey.
"""

import logging
import os
import sys
from pathlib import Path


def _ensure_std_streams() -> None:
    """Give the process real stdout/stderr before anything tries to write.

    PyInstaller windowed builds (console=False) set both to None. Most code
    survives that — bare print() tolerates it — but anything calling
    .write() directly does not. tqdm is the one that matters: huggingface_hub
    uses it for the download bar when faster-whisper fetches its model on the
    first transcription, so the very first activation died with
    "'NoneType' object has no attribute 'write'". The model then never
    cached, so it failed again on every subsequent attempt, and the toast
    showed only the bare message with no traceback (GitHub issue #15).

    Runs at import time, ahead of the heavy imports below, so library code
    that writes while loading is covered too.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except Exception:
            continue
        setattr(sys, name, stream)
        # Some libraries reach for the __-prefixed originals instead.
        if getattr(sys, f"__{name}__", None) is None:
            setattr(sys, f"__{name}__", stream)


_ensure_std_streams()

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt

from config import cfg
from ui.tray import TrayManager
from ui.panel import CompanionPanel, AppState
from ui.overlay import (
    CursorOverlay, MODE_IDLE, MODE_LISTENING, MODE_THINKING, MODE_SPEAKING
)
from hotkey import GlobalHotkeyMonitor, StopHotkey
from companion_manager import CompanionManager


STATE_TO_CURSOR_MODE = {
    AppState.IDLE:      MODE_IDLE,
    AppState.LISTENING: MODE_LISTENING,
    AppState.THINKING:  MODE_THINKING,
    AppState.SPEAKING:  MODE_SPEAKING,
}


def _copilot_login_flow(tray, panel, manager):
    """Run the GitHub device-flow login in a worker thread so the UI stays live."""
    import asyncio, threading
    from ai.github_copilot_provider import device_login

    def _on_code(user_code: str, verification_uri: str):
        """Called as soon as the device code arrives — display it in the panel."""
        msg = (
            f"GitHub Copilot-ში შესვლა\n\n"
            f"1. გახსენით: {verification_uri}\n"
            f"2. შეიყვანეთ კოდი:  {user_code}\n"
            f"3. დააჭირეთ Authorize-ს — კლიპიტაური ავტომატურად შევა."
        )
        # Show in panel (cross-thread safe via Qt signal)
        panel.show_copilot_code(user_code, verification_uri)
        tray.show_notification("GitHub Copilot — შეიყვანეთ ეს კოდი", user_code)

    def _worker():
        # Sign-in and model discovery are reported separately on purpose: the
        # device flow was succeeding and saving a valid token, then a failing
        # model refresh surfaced as "Copilot-ში შესვლა ვერ მოხერხდა" and sent users off
        # re-authorising something that was never broken.
        try:
            asyncio.run(device_login(on_code=_on_code))
        except Exception as e:
            tray.show_notification("Copilot-ში შესვლა ვერ მოხერხდა", str(e))
            panel.show_copilot_error(str(e))
            return

        tray.show_notification("GitHub Copilot", "შესვლა შესრულდა! მოდელების სია ახლდება…")
        try:
            manager.refresh_copilot_models()
        except Exception as e:
            tray.show_notification(
                "Copilot-ში შესულია — მოდელების სია მიუწვდომელია", str(e)
            )
            panel.show_copilot_error(
                f"შესვლა შესრულდა, მაგრამ მოდელების სია ვერ "
                f"ჩაიტვირთა:\n{e}\n\nსცადეთ: ხატულა → მოდელი → Copilot-ის მოდელების განახლება."
            )

    threading.Thread(target=_worker, daemon=True).start()


def _setup_logging():
    """Rotating runtime log at %LOCALAPPDATA%\\Clicky\\clicky.log — the #1
    ask from bug reports: with no log file, 'stuck on Listening' class issues
    were undiagnosable (GitHub issue #6)."""
    import logging
    from logging.handlers import RotatingFileHandler

    log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Clicky"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "clicky.log", maxBytes=1_000_000, backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        ))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        logging.getLogger("clicky").info(
            "=== Clicky starting (python %s) ===", sys.version.split()[0]
        )
    except Exception:
        pass  # logging must never block startup


def main():
    _setup_logging()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Clicky")
    app.setApplicationDisplayName("კლიპიტაური")

    # ── Core components ───────────────────────────────────────────────────────
    manager = CompanionManager()
    panel   = CompanionPanel()
    overlay = CursorOverlay()
    # Shown only while the autopilot runs; keeps clear of the panel.
    from ui.stop_window import StopWindow
    # iPhone-style status island at the top (its × stops what is running)
    from ui.island import IslandController, IslandWindow
    island = IslandWindow()
    island_ctl = IslandController(island)

    def _island_rects():
        return [island.frameGeometry()] if island.isVisible() else []

    stop_window = StopWindow(
        avoid_rects=lambda: ([panel.frameGeometry()] if panel.isVisible() else [])
        + _island_rects() + overlay.occupied_rects()
    )
    tray    = TrayManager()

    # ── Wire signals ──────────────────────────────────────────────────────────

    # State changes → Panel + Tray + Cursor
    def _on_state(state: AppState):
        panel.set_state(state)
        tray.set_state_icon(state.name.lower())
        overlay.set_mode(STATE_TO_CURSOR_MODE.get(state, MODE_IDLE))
        island_ctl.on_app_state(state)

    manager.sig_state_changed.connect(_on_state)

    # Response streaming
    manager.sig_response_chunk.connect(panel.append_response_chunk)

    # Recognised speech → large on-screen caption, shown before the answer,
    # and the question line at the top of the panel
    # The blue „თქვენ თქვით:“ box can be switched off in the startup settings.
    _ui_prefs = {"said_caption": True}
    _startup = {"dlg": None}        # the startup settings window, while open

    def _on_transcript(text: str):
        if _ui_prefs["said_caption"]:
            overlay.show_caption(text)
    manager.sig_transcript.connect(_on_transcript)
    manager.sig_transcript.connect(panel.show_question)

    # Panel input: the big button and typed questions
    panel.on_ask_clicked.connect(manager.on_panel_ask)
    panel.on_text_submitted.connect(manager.ask_text)
    # The pointer is drawn above everything the app shows, panel included
    panel.on_shown.connect(overlay.keep_on_top)

    # Audio level → cursor waveform (+ panel meter)
    manager.sig_audio_level.connect(panel.set_audio_level)
    manager.sig_audio_level.connect(overlay.set_audio_level)

    # Pointing directives
    manager.sig_point_at.connect(overlay.point_at)
    manager.sig_point_hold.connect(overlay.set_point_hold)
    manager.sig_point_release.connect(overlay.release_point)
    manager.sig_point_retarget.connect(overlay.retarget)

    # Whiteboard annotations (legacy per-shape signals)
    manager.sig_arrow.connect(overlay.add_arrow)
    manager.sig_circle.connect(overlay.add_circle)
    manager.sig_underline.connect(overlay.add_underline)
    manager.sig_label.connect(overlay.add_text)
    # Teaching drawings — generic shape channel with progressive animation
    manager.sig_draw.connect(overlay.add_shape)
    manager.sig_clear_drawings.connect(overlay.clear_annotations)

    # Errors. The toast names the log file because it only has room for the
    # message itself — issue #15 was reported with no traceback simply because
    # nobody knew a log existed.
    _log_hint = Path(
        os.environ.get("LOCALAPPDATA", Path.home())
    ) / "Clicky" / "clicky.log"
    manager.sig_error.connect(panel.show_error)
    manager.sig_error.connect(
        lambda e: tray.show_notification(
            "კლიპიტაური — შეცდომა", f"{e}\n\nდეტალები: {_log_hint}"
        )
    )

    # Panel → Manager
    panel.on_model_changed.connect(manager.set_model)

    def _on_doc_dropped(path: str):
        ok = manager.attach_document(path)
        tray.show_notification(
            "დოკუმენტი მიმაგრებულია" if ok else "მიმაგრება ვერ მოხერხდა",
            f"{path}\nახლა შეგიძლიათ კლიპიტაურის ჰკითხოთ მის შესახებ." if ok else
            "ფაილის წაკითხვა ვერ მოხერხდა."
        )
    panel.on_document_dropped.connect(_on_doc_dropped)

    # Tray → UI / Manager
    tray.on_show_panel.connect(panel.show)
    tray.on_hide_panel.connect(panel.hide)
    tray.on_toggle_search.connect(manager.set_web_search)
    tray.on_toggle_wake_word.connect(manager.set_wake_word)
    tray.on_toggle_slow_mode.connect(manager.set_slow_mode)
    tray.on_toggle_slow_mode.connect(overlay.set_slow_mode)
    tray.on_toggle_quiz_mode.connect(manager.set_quiz_mode)
    tray.on_toggle_privacy.connect(manager.set_privacy_guard)
    tray.on_toggle_code_mode.connect(manager.set_code_mode_auto)
    tray.on_toggle_multilang.connect(manager.set_multilang)
    tray.on_toggle_journal.connect(manager.set_journal)
    tray.on_toggle_ocr.connect(manager.set_ocr_enabled)
    tray.on_toggle_agent_mode.connect(manager.set_agent_mode)
    tray.on_toggle_agent_dry_run.connect(manager.set_agent_dry_run)

    # Autopilot progress -> panel; panel answers -> autopilot
    manager.sig_agent_plan.connect(panel.show_agent_plan)
    manager.sig_agent_step.connect(panel.show_agent_step)
    manager.sig_agent_say.connect(panel.show_agent_say)
    manager.sig_agent_confirm_request.connect(panel.show_agent_prompt)
    manager.sig_agent_state.connect(panel.set_agent_state)
    panel.on_agent_reply.connect(manager.agent_reply)
    # Stopping a run: the panel button turns into a stop button and the
    # separate stop window appears for as long as the autopilot runs.
    manager.sig_agent_running.connect(panel.set_agent_running)
    manager.sig_agent_running.connect(stop_window.set_running)
    manager.sig_agent_running.connect(overlay.set_agent_running)
    # Before each action: the panel docks opposite the target first (keeping
    # the stop window uncovered), then the stop window gets out of the way.
    panel.set_keep_clear_provider(
        lambda: ([stop_window.frameGeometry()] if stop_window.isVisible() else [])
        + _island_rects() + overlay.occupied_rects()
    )
    manager.sig_agent_target.connect(panel.dock_away_from)
    manager.sig_agent_target.connect(island.avoid_target)
    manager.sig_agent_target.connect(stop_window.avoid_target)
    stop_window.on_stop.connect(manager.cancel_agent)

    # Mode switch — panel and tray stay in sync through the manager
    panel.on_agent_mode_changed.connect(manager.set_agent_mode)
    manager.sig_agent_mode.connect(panel.set_agent_mode)
    manager.sig_agent_mode.connect(tray.set_agent_mode)

    # Checklist, „დავიბენი“, and the autopilot's words as the big caption
    manager.sig_agent_steps.connect(panel.show_agent_steps)
    panel.on_agent_lost.connect(manager.agent_request_replan)
    # Everything spoken comes out of the mascot's mouth as a speech bubble
    # (this replaces the autopilot's bottom caption, which said the same).
    manager.sig_speaking.connect(overlay.on_speaking)

    # Island: what is happening right now, in Georgian
    manager.sig_agent_running.connect(island_ctl.on_agent_running)
    manager.sig_agent_state.connect(island_ctl.on_agent_state)
    manager.sig_agent_action.connect(island_ctl.on_agent_action)
    manager.sig_point_at.connect(island_ctl.on_point)
    manager.sig_audio_level.connect(island_ctl.on_level)
    island.on_close.connect(manager.dismiss_from_island)

    # Our own windows stay out of screenshots (the model and the autopilot's
    # settle check see the real screen), except while a lesson is recorded,
    # where the pointer must be in the video. CLICKY_HIDE_OVERLAY_FROM_CAPTURE=0
    # turns this off.
    from screen.capture import exclude_from_capture
    _hide_own = os.getenv("CLICKY_HIDE_OVERLAY_FROM_CAPTURE", "1").strip().lower()         not in ("0", "false", "no")

    def _set_own_capture(recording: bool):
        if not _hide_own:
            return
        for w in (overlay, island, stop_window):
            try:
                exclude_from_capture(int(w.winId()), not recording)
            except Exception:
                logging.getLogger("clicky").debug("capture exclusion failed", exc_info=True)
    _set_own_capture(False)
    manager.sig_recording_state.connect(lambda on, _path: _set_own_capture(bool(on)))

    # Lesson recording
    def _record_start():
        out = manager.start_recording()
        tray.show_notification(
            "გაკვეთილის ჩაწერა",
            f"ჩაიწერება აქ:\n{out}" if out else
            "ვერ მოხერხდა — დააყენეთ imageio[ffmpeg]: pip install imageio imageio-ffmpeg"
        )
    def _record_stop():
        out = manager.stop_recording()
        if out:
            tray.show_notification("გაკვეთილი შენახულია", out)
    tray.on_record_start.connect(_record_start)
    tray.on_record_stop.connect(_record_stop)
    manager.sig_recording_state.connect(
        lambda on, _path: tray.set_recording_state(on)
    )

    # Workflow capture
    def _wf_start():
        ok = manager.workflow_start()
        tray.show_notification(
            "მოქმედებების ჩაწერა",
            "იწერება თქვენი დაწკაპუნებები და კლავიშები. დასრულებისას შეაჩერეთ ხატულიდან."
            if ok else "დააყენეთ pynput: pip install pynput"
        )
    def _wf_stop():
        summary = manager.workflow_stop()
        if summary:
            tray.show_notification(
                "მოქმედებები ჩაწერილია",
                "კლიპიტაურის გადაეცა. ჰკითხეთ: „რა გავაკეთე ახლა?“"
            )
            # Stash as an attached doc so the next question sees it
            manager._attached_docs.append(("recorded_workflow.txt", summary))
    tray.on_workflow_start.connect(_wf_start)
    tray.on_workflow_stop.connect(_wf_stop)

    # Live collab
    tray.on_collab_start.connect(manager.collab_start_host)
    def _collab_join():
        from PyQt6.QtWidgets import QInputDialog
        code, ok = QInputDialog.getText(None, "სესიაში შესვლა",
                                        "შეიყვანეთ სესიის 6-სიმბოლოიანი კოდი:")
        if ok and code:
            manager.collab_join(code.strip())
    tray.on_collab_join.connect(_collab_join)

    # Journal folder
    def _open_journal():
        import os, subprocess
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "Clicky")
        try:
            os.startfile(path)
        except Exception:
            subprocess.Popen(["explorer", path])
    tray.on_journal_open.connect(_open_journal)

    # Attach document (drag-drop alternative — file picker)
    def _attach_doc():
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            None, "დოკუმენტის მიმაგრება კლიპიტაურისთვის",
            "", "დოკუმენტები (*.pdf *.docx *.txt *.md *.csv)"
        )
        if path:
            ok = manager.attach_document(path)
            tray.show_notification(
                "დოკუმენტი მიმაგრებულია",
                f"{path}\nახლა შეგიძლიათ კლიპიტაურის ჰკითხოთ მის შესახებ." if ok else
                "ფაილის წაკითხვა ვერ მოხერხდა."
            )
    tray.on_attach_doc.connect(_attach_doc)

    def _switch(name: str):
        manager.set_active_provider(name)
        panel.refresh_for_provider(name)       # repopulate model dropdown + badge
        tray.rebuild_menu()                    # tick mark moves to new provider
        tray.show_notification("კლიპიტაური", f"გადართულია: {name}")

    tray.on_switch_provider.connect(_switch)
    tray.on_stop.connect(manager.stop)
    tray.on_copilot_login.connect(lambda: _copilot_login_flow(tray, panel, manager))
    tray.on_copilot_refresh.connect(manager.refresh_copilot_models)

    # When the live model list arrives, repopulate the panel + show a toast
    def _on_copilot_models_done(count: int):
        if cfg.llm_provider() == "copilot":
            panel.refresh_for_provider("copilot")
        tray.show_notification(
            "GitHub Copilot",
            f"ჩაიტვირთა {count} მოდელი. უფასო მოდელები მონიშნულია "
            f"მოდელის სიაში."
        )
    manager.sig_copilot_models_done.connect(_on_copilot_models_done)

    # Live model auto-refresh for Claude / OpenAI / Gemini (30-day cache).
    # Repopulate the panel whenever a refresh lands.
    def _on_models_refreshed(provider: str, count: int):
        if cfg.llm_provider() == provider:
            panel.refresh_for_provider(provider)
    manager.sig_models_refreshed.connect(_on_models_refreshed)

    # ── Ollama multi-model wiring ─────────────────────────────────────────
    tray.on_ollama_set_model.connect(manager.set_ollama_model)
    tray.on_ollama_pull.connect(manager.pull_ollama_model)
    tray.on_ollama_refresh.connect(manager.refresh_ollama_models)
    tray.on_set_mic_device.connect(manager.set_mic_device)
    tray.on_set_response_language.connect(manager.set_response_language)
    tray.on_set_stt_language.connect(manager.set_stt_language)
    tray.on_set_custom_instructions.connect(manager.set_custom_instructions)

    # When the installed-model list arrives, push it into the tray submenu
    manager.sig_ollama_models.connect(tray.set_ollama_models)
    manager.sig_ollama_models.connect(panel.set_ollama_models)
    manager.sig_ollama_selected.connect(panel.select_ollama_model)
    panel.on_ollama_refresh_requested.connect(manager.refresh_ollama_models)

    # Surface pull progress as tray toasts so students see download status
    def _on_ollama_pull_status(name: str, status: str):
        tray.show_notification("Ollama", status)
    manager.sig_ollama_pull_status.connect(_on_ollama_pull_status)

    # First-run: poll Ollama if it's the active provider so the menu
    # actually shows installed models from the start.
    if cfg.llm_provider() == "ollama":
        manager.refresh_ollama_models()

    # Setup wizard (re-run) + diagnostics
    def _run_setup_again():
        from ui.setup_wizard import SetupWizard
        wiz = SetupWizard()
        wiz.show()
        _setup_keepalive[0] = wiz
    tray.on_run_setup.connect(_run_setup_again)

    def _open_api_keys():
        from ui.api_keys_dialog import ApiKeysDialog
        dlg = ApiKeysDialog()

        def _applied():
            # A new key can change which provider is active, so refresh every
            # surface that renders the provider name or model list.
            provider = cfg.llm_provider()
            manager.set_active_provider(provider)
            panel.refresh_for_provider(provider)
            tray.rebuild_menu()
            if _startup["dlg"] is not None:          # keep its model list in step
                _startup["dlg"].set_models(panel.model_choices(), panel.current_model())
            tray.show_notification("კლიპიტაური", f"API გასაღებები შენახულია — გამოიყენება {provider}")

        dlg.keys_saved.connect(_applied)
        dlg.show()
        _setup_keepalive[1] = dlg   # keep a reference so Qt doesn't GC it
    tray.on_api_keys.connect(_open_api_keys)

    def _save_diagnostics():
        import datetime, json, platform, traceback
        from ai import ollama_bootstrap as ob
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        out = Path(base) / "Clicky" / f"diagnostics-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
        try:
            providers_d = cfg.describe()
        except Exception:
            providers_d = {}
        report = []
        report.append(f"Clicky diagnostics — {datetime.datetime.now().isoformat()}")
        report.append(f"Python: {sys.version.split()[0]}")
        report.append(f"Platform: {platform.platform()}")
        report.append(f"Active LLM: {providers_d.get('llm', '?')}")
        report.append(f"STT: {providers_d.get('stt', '?')}  TTS: {providers_d.get('tts', '?')}")
        report.append("")
        report.append("─── Ollama ───")
        try:
            report.append(f"Host: {cfg.ollama_host}")
            report.append(f"Text model:   {cfg.ollama_text_model}")
            report.append(f"Vision model: {cfg.ollama_vision_model}")
            report.append(f"Binary on PATH: {ob.is_ollama_installed()}")
            report.append(f"Server reachable: {ob.is_ollama_running()}")
            if ob.is_ollama_running():
                report.append(f"Installed models: {ob.list_installed_models()}")
        except Exception:
            report.append(traceback.format_exc())
        report.append("")
        report.append("─── GitHub Copilot ───")
        try:
            from ai.github_copilot_provider import is_authenticated, _token_path
            report.append(f"Token file: {_token_path()}  exists={_token_path().exists()}")
            report.append(f"Authenticated: {is_authenticated()}")
        except Exception:
            report.append(traceback.format_exc())
        try:
            out.write_text("\n".join(report), encoding="utf-8")
            tray.show_notification("დიაგნოსტიკა შენახულია", str(out))
            try:
                os.startfile(str(out))
            except Exception:
                pass
        except Exception as e:
            tray.show_notification("დიაგნოსტიკა ვერ მოხერხდა", str(e))
    tray.on_diagnostics.connect(_save_diagnostics)

    tray.on_quit.connect(lambda: (tray.hide_icon(), manager.shutdown(), app.quit()))

    # ── Global hotkey ─────────────────────────────────────────────────────────
    hotkey = GlobalHotkeyMonitor(
        on_press=manager.on_hotkey_press,
        on_release=manager.on_hotkey_release,
    )
    hotkey.start()

    # Esc = cancel current generation (kills Ollama ramble mid-stream), and
    # latch the executor's cancel_event so any real input action stops too.
    from agent.executor import cancel_event as executor_cancel_event
    stop_key = StopHotkey(on_stop=manager.stop, key="esc",
                          cancel_event=executor_cancel_event)
    stop_key.start()

    # ── Show UI + start listener ──────────────────────────────────────────────
    overlay.show()        # persistent overlay (cursor follow)
    # No chat panel on screen: the mascot (bottom-right) talks in a speech
    # bubble and the island shows what is happening. Tray -> „პანელის
    # ჩვენება“ still opens the panel.
    manager.start()        # begin ambient mic + wake-word scanning

    # ── Startup settings: model / API key, chat panel, the blue box ─────────
    # Last time's choices apply at once; the window opens on every start so
    # they can be changed before the first question.
    from ui import startup_settings as startup

    def _apply_startup(s, save: bool = True):
        _ui_prefs["said_caption"] = s.show_said_caption
        if s.model and not panel.select_model(s.model):
            logging.getLogger("clicky").info("startup: model %r is not offered now", s.model)
        # On: the panel is shown and comes back with each question.
        panel.auto_show = s.show_chat_panel
        if s.show_chat_panel:
            panel.show()
        else:
            panel.hide()
        if save:
            startup.save_settings(s)

    def _startup_closed(s):
        _startup["dlg"] = None
        _apply_startup(s)

    _saved = startup.load_settings()
    _apply_startup(_saved, save=False)
    if startup.enabled():
        _dlg = startup.StartupSettingsDialog(
            _saved, panel.model_choices() or startup.model_choices(cfg.llm_provider()),
            provider=cfg.llm_provider())
        _dlg.applied.connect(_startup_closed)
        _dlg.api_keys_requested.connect(_open_api_keys)
        _startup["dlg"] = _dlg
        _dlg.show()
        _dlg.raise_()
        _dlg.activateWindow()

    providers = cfg.describe()
    _hk = "+".join(p.strip().capitalize() for p in cfg.hotkey.split("+"))
    tray.show_notification(
        "კლიპიტაური ჩართულია",
        (f"თქვით „კლიპიტაური“ ან დააჭირეთ {_hk}"
         if cfg.ambient_mic() else
         f"დააჭირეთ {_hk}, დასვით კითხვა — როცა გაჩერდებით, "
         f"კლიპიტაური გიპასუხებთ"),
    )

    # ── First-run setup wizard ────────────────────────────────────────────────
    # Show the Ollama install / model pull walkthrough on the first launch.
    # If everything is already wired up, the helper is a no-op.
    try:
        from ui.setup_wizard import maybe_show_setup_wizard, SetupWizard

        # Force-show via env var (handy for testing).
        if os.environ.get("CLICKY_FORCE_SETUP", "").strip() in ("1", "true", "yes"):
            wiz = SetupWizard()
            wiz.show()
            _setup_keepalive[0] = wiz
        else:
            wiz = maybe_show_setup_wizard()
            if wiz is not None:
                _setup_keepalive[0] = wiz   # keep a reference so it isn't GC'd
    except Exception as e:
        print(f"[setup-wizard] skipped: {e}")

    sys.exit(app.exec())


# Module-level slots keeping references to the setup wizard [0] and the API
# keys dialog [1] alive while Qt is running (PyQt will GC them otherwise and
# the dialog will vanish).
_setup_keepalive: list = [None, None]


if __name__ == "__main__":
    main()
