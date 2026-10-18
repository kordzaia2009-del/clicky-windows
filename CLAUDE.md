# CLAUDE.md — კლიპიტაური (Clicky for Windows, Georgian elderly edition)

Handoff notes so a new Claude Code session (on any computer) can continue this work.

## What this is
A fork of Clicky for Windows (repo: https://github.com/kordzaia2009-del/clicky-windows),
adapted for **elderly Georgian users** and renamed **კლიპიტაური**. The user speaks
Georgian, and the app looks at the screen and answers by voice. In **"do" mode** an
**autopilot** carries out the task itself, using real clicks and typing.

- **Georgian throughout:**
  - UI strings: `ui/strings_ka.py`;
  - prompts;
  - STT: Google web speech `ka-GE`;
  - TTS: Edge `ka-GE-EkaNeural`, wrapped in `audio/tts/reporting.py` (`ReportingTTS`) so the overlay knows which line is being spoken.
- **Visuals:**
  - a toon mascot in the bottom-right corner (`ui/mascot.py`, painted by `ui/overlay.py`), with talk/blink frames and a speech bubble whose words appear in time with the voice;
  - a 🍇 grape pointer that flies with a golden trail;
  - an iPhone-style "island" notification at the top (`ui/island.py`), where × stops the run;
  - a stop window (`ui/stop_window.py`);
  - the old chat panel is no longer shown at startup.

## Architecture (key files)
- `main.py`: wires up the Qt app, overlay, island, stop window and tray. Logging goes to `%LOCALAPPDATA%\Clicky\clicky.log` (rotating, INFO).
- `companion_manager.py`: the brain. It runs an asyncio loop on a worker thread and talks to the UI through PyQt signals.
  - `_agent_deps` builds the autopilot's dependencies. Its `decide` closure makes the model call for each step.
  - `_agent_locate` is the locator chain: **pywinauto → hybrid (UIA → OCR) → Computer Use → universal vision**.
  - `_agent_intercept` routes "do" mode into the autopilot. Note that **every question** in "do" mode first runs `plan_request`, which is an extra planner call.
- **AI providers:**
  - `ai/provider_factory.py` picks the provider;
  - every provider implements `stream_response(user_text, screenshots_b64, history, system_prompt, model)`;
  - `ClaudeProvider` sends an `anthropic-workspace-id` header (taken from `.env`).
- **`agent/`:**
  - `planner.py`: `plan()` returns `PlanResult` / `Goal` / `Step`;
  - `autopilot.py`: the state machine. It uses `AutopilotDeps`, `Located` and `Settings`. `_settle` waits until the screen stops changing, compared with `image_diff`;
  - `executor.py`: `execute()` returns `ActionResult`. It works in physical pixels and is DPI aware. It tries UIA/pywinauto Invoke first, then falls back to SendInput. It also supports `cancel_event` and dry run;
  - `safety.py`: `classify()` is the gate. Confirm words such as "Send" make the run wait for a spoken დიახ ("yes");
  - `typing_backends.py`:
    - ASCII is typed with pyautogui `write`;
    - Georgian is pasted from the clipboard, and the clipboard is restored afterwards;
    - pywinauto `send_keys` is another option;
    - the backend is chosen with the env var `CLICKY_TYPE_BACKEND` (`pyautogui` | `pywinauto` | `sendinput`).
- **Locators:**
  - `ai/pwa_locator.py` finds pywinauto controls by name (turned off with `CLICKY_USE_PYWINAUTO=0`);
  - `ai/hybrid_pointer.py` runs `find_target`;
  - `ai/element_locator.py` uses Computer Use;
  - `ai/universal_locator.py` is the universal vision fallback.
- `screen/capture.py`: captures every monitor with mss and makes a 1280-wide JPEG for the model.
- `audio/playback.py`: exposes `is_playing`, `speaking_level`, `playback_progress` and `last_start_time`.
- `timing.py`: `mark()` / `timed()` / `since_ms()` helpers for the `timing:` logs (see the current task below).

## Running & testing
- Offline tests (fast, no windows, no real input):
  `venv\Scripts\python.exe -X utf8 -m pytest tests/ -q`.
  Last result: **230 passed, 1 failed**. The failure is the known flaky `test_visuals.py::test_overlay_bubble_reveals_words_with_the_voice`: if alpha is 0 on the first paint, `_say_rect` stays None. It is unrelated to recent changes and still unfixed.
- Run the app (the **user** does this, not Claude): `venv\Scripts\python.exe main.py`
- Read timings after a run: `findstr "timing:" "%LOCALAPPDATA%\Clicky\clicky.log"`
- API keys live in `.env`, which is ignored by git. `.env.example` is the template with empty values. On a new computer, copy `.env.example` to `.env` and fill it in.
- `venv/` is the real virtualenv. `venvvenv/` is a stray folder: **never commit it**.

## Working rules from the user (MUST follow)
- **Never open Notepad, a browser, `main.py` or any other program yourself**, and don't build tests that open programs.
- No long live checks. Only run the fast offline pytest suite.
- Anything that needs a live screen: don't try it. Give the user **one exact command** and say what to look for. The user runs it themselves.
- Keep reports **short**.
- When asked to measure, measure first and **don't change behaviour**. Optimize only when asked.
- **Never touch or force-push the `autopilot` branch.** It is kept stable for the demo.
- This machine has no git identity set. Commit with
  `git -c user.name=giorgibrotato -c user.email=giorgibrotato@gmail.com commit ...`
  and don't change the global config.
- Commit or push only when asked. Don't commit `venvvenv/`, `.env` or keys.

## Branches (on origin)
- `main`: upstream base.
- `georgian-elderly-adaptation`: the Georgian UI, prompt, STT and TTS adaptation.
- `autopilot` (8cc492b): planner, executor, state machine, safety, stop window, elderly-friendly panel and README. **Frozen for the demo.**
- `autopilot-visuals` (**current**, 6df4417): adds the mascot, island, grape pointer, speech bubble, hidden panel, pyautogui/pywinauto backends and the `timing:` logs.

## Current task / where we stopped
The user said **"the autopilot is too slow — measure first, don't optimize yet."**
Done and pushed (commit 6df4417): `timing:` log lines for every phase.
- Model calls (first token and total):
  - `planner.model_call`;
  - `autopilot.model_call`;
  - `do_mode.plan_request` (the extra plan on every question).
- Planning steps:
  - `autopilot.planning_screenshot`;
  - `autopilot.planner`;
  - `autopilot.decide`.
- Screen capture:
  - `capture.monitor` (grab and encode times, JPEG size);
  - `capture.all_screens`.
- Locators:
  - `locator.pywinauto`;
  - `locator.uia`;
  - `locator.ocr`;
  - `locator.computer_use`;
  - `locator.universal`;
  - `locator.total` (with `answered=` naming the tier that found the target).
- Executor and settle:
  - `executor.pointer_wait` (the flight plus the 400 ms hold);
  - `executor.input`;
  - `executor.total`;
  - `autopilot.settle` (samples taken, timed out or not).
- TTS:
  - `tts.decode`;
  - `tts.playback`;
  - `tts.speak` (`start_speaking_ms` and total).

**Next step:** the user runs a real task and pastes the `timing:` lines. Analyse where the time goes, rank the bottlenecks, and propose optimizations. Implement them only once the user approves. Likely suspects to check against the data:
- the extra `plan_request` on every do-mode question;
- slow model time to first token;
- vision locator fallbacks (Computer Use / universal);
- settle timeouts;
- TTS synthesis before the voice starts.
