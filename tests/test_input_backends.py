"""pyautogui / pywinauto integration — every library call is faked, so no
real typing, clicking or UI Automation happens."""

import asyncio
from types import SimpleNamespace

import pytest

import agent.executor as ex
from agent import typing_backends as tb
from test_executor import FakeWin, Recorder


# ── typing backends ──────────────────────────────────────────────────────────

class FakePyautogui:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def write(self, text, interval=0.0):
        self.calls.append(("write", text))

    def hotkey(self, *keys):
        self.calls.append(("hotkey", keys))
        if self.fail:
            raise self.fail


class FakeClipboard:
    def __init__(self, content="the person's clipboard"):
        self.content, self.history = content, []

    def paste(self):
        return self.content

    def copy(self, text):
        self.history.append(text)
        self.content = text


def test_plain_ascii_detection():
    assert tb.is_plain_ascii("hello world 123!\n")
    assert not tb.is_plain_ascii("გამარჯობა")
    assert not tb.is_plain_ascii("café")


def test_pyautogui_types_ascii_with_write():
    g = FakePyautogui()
    assert tb.type_with_pyautogui("hello", pyautogui=g, pyperclip=FakeClipboard()) == "pyautogui-write"
    assert g.calls == [("write", "hello")]


def test_pyautogui_pastes_georgian_and_restores_the_clipboard():
    g, clip = FakePyautogui(), FakeClipboard()
    method = tb.type_with_pyautogui("გამარჯობა", pyautogui=g, pyperclip=clip, sleep=lambda s: None)
    assert method == "pyautogui-paste"
    assert g.calls == [("hotkey", ("ctrl", "v"))]
    assert clip.history == ["გამარჯობა", "the person's clipboard"]   # pasted, then put back


def test_clipboard_is_restored_even_if_the_paste_fails():
    g, clip = FakePyautogui(fail=RuntimeError("boom")), FakeClipboard()
    with pytest.raises(RuntimeError):
        tb.type_with_pyautogui("გამარჯობა", pyautogui=g, pyperclip=clip, sleep=lambda s: None)
    assert clip.content == "the person's clipboard"


def test_pywinauto_send_keys_types_literally():
    sent = []
    kb = SimpleNamespace(send_keys=lambda keys, **kw: sent.append((keys, kw)))
    assert tb.type_with_pywinauto("a+b (1) {x} გამარჯობა", keyboard=kb) == "pywinauto-keys"
    keys, kw = sent[0]
    assert keys == "a{+}b {(}1{)} {{}x{}} გამარჯობა"
    assert kw["with_spaces"] and kw["with_newlines"]


@pytest.mark.parametrize("value, expected", [
    ("pyautogui", "pyautogui"), ("PYWINAUTO", "pywinauto"), ("sendinput", "sendinput"),
    ("", "pyautogui"), ("nonsense", "pyautogui"),
])
def test_backend_from_config(monkeypatch, value, expected):
    monkeypatch.setenv("CLICKY_TYPE_BACKEND", value)
    assert tb.configured_backend() == expected


def test_win_type_text_dispatches_and_falls_back(monkeypatch):
    w = ex._Win32()
    used = []
    monkeypatch.setattr(tb, "type_with_pyautogui", lambda t: used.append(("pg", t)) or "pyautogui-write")
    monkeypatch.setattr(tb, "type_with_pywinauto", lambda t: (_ for _ in ()).throw(ImportError("no")))
    monkeypatch.setattr(w, "type_unicode", lambda t: used.append(("si", t)))
    monkeypatch.setenv("CLICKY_TYPE_BACKEND", "pyautogui")
    assert w.type_text("hi") == "pyautogui-write"
    monkeypatch.setenv("CLICKY_TYPE_BACKEND", "pywinauto")
    assert w.type_text("ჰი") == "unicode"                       # package missing -> SendInput
    assert used == [("pg", "hi"), ("si", "ჰი")]


# ── executor typing with the pyautogui backend ───────────────────────────────

@pytest.fixture
def fake(monkeypatch):
    w = FakeWin()
    monkeypatch.setattr(ex, "win", w)
    monkeypatch.setattr(ex, "HOLD_S", 0.0)
    monkeypatch.setattr(ex, "POLL_S", 0.005)
    monkeypatch.setenv("CLICKY_TYPE_BACKEND", "pyautogui")
    ex.set_dry_run(False)
    ex.reset_cancel()
    yield w
    ex.reset_cancel()


def test_georgian_is_pasted_in_one_go(fake):
    r = ex.execute({"type": "type", "text": "გამარჯობა, როგორ ხართ?"}, None, Recorder().callbacks())
    assert r.status == "done"
    assert [c for c in fake.calls if c[0] == "type"] == [("type", "გამარჯობა, როგორ ხართ?")]


def test_ascii_is_typed_in_chunks(fake):
    ex.execute({"type": "type", "text": "hello world, how are you"}, None, Recorder().callbacks())
    typed = [c[1] for c in fake.calls if c[0] == "type"]
    assert "".join(typed) == "hello world, how are you" and len(typed) > 1


def test_pyautogui_failsafe_counts_as_stop(fake):
    class FailSafeException(Exception):
        pass

    def boom(text):
        raise FailSafeException("mouse in a corner")
    fake.type_text = boom
    r = ex.execute({"type": "type", "text": "hello"}, None, Recorder().callbacks())
    assert r.status == "cancelled"


def test_private_text_never_reaches_pyautogui(fake):
    r = ex.execute({"type": "type", "text": "hunter2", "private": True}, None, Recorder().callbacks())
    assert r.status == "refused" and fake.calls == []


# ── pywinauto controls in the executor ───────────────────────────────────────

class FakePwaControl:
    """Shaped like a pywinauto UIAWrapper."""

    def __init__(self, rect=(100, 200, 180, 230), pid=12345, invokes=True):
        self.rect, self.pid, self.invokes, self.pressed = rect, pid, invokes, []

    def rectangle(self):
        l, t, r, b = self.rect
        return SimpleNamespace(left=l, top=t, right=r, bottom=b)

    def process_id(self):
        return self.pid

    def invoke(self):
        if not self.invokes:
            raise RuntimeError("no invoke pattern")
        self.pressed.append("invoke")


def test_win_reads_pywinauto_rect_and_pid():
    c = FakePwaControl()
    assert ex._Win32().element_rect(c) == (100, 200, 180, 230)
    assert ex._element_pid(c) == 12345


def test_win_invokes_pywinauto_controls():
    c = FakePwaControl()
    assert ex._Win32().uia_activate(c) == "pywinauto-invoke" and c.pressed == ["invoke"]
    assert ex._Win32().uia_activate(FakePwaControl(invokes=False)) is None


def test_click_on_a_pywinauto_control_uses_invoke_not_the_mouse(fake, monkeypatch):
    fake.uia_activate = ex._Win32().uia_activate       # real adapter, fake control
    fake.element_rect = ex._Win32().element_rect
    c = FakePwaControl()
    target = SimpleNamespace(x=140, y=215, bbox=(100, 200, 180, 230), label="Send",
                             source="uia", element=c)
    r = ex.execute({"type": "click", "target": target}, None, Recorder().callbacks())
    assert r.method == "pywinauto-invoke" and c.pressed == ["invoke"]
    assert not any(k[0] in ("set_cursor", "click") for k in fake.calls)


def test_pywinauto_control_covered_by_another_app_is_refused(fake):
    fake.element_rect = ex._Win32().element_rect
    c = FakePwaControl(invokes=False, pid=999)          # falls back to the mouse...
    fake.uia_activate = lambda el: None
    fake.window = (777, "Chrome_WidgetWin_1", "Chrome")  # ...but Chrome is on top
    target = SimpleNamespace(x=140, y=215, bbox=(0, 0, 0, 0), label="OK", source="uia", element=c)
    r = ex.execute({"type": "click", "target": target}, None, Recorder().callbacks())
    assert r.status == "refused" and "covered by another window" in r.message


# ── pywinauto locator ────────────────────────────────────────────────────────

class Node:
    def __init__(self, name, ctype="Pane", rect=(0, 0, 50, 20), children=(), password=False):
        self.name, self._children, self.rect = name, list(children), rect
        self.element_info = SimpleNamespace(control_type=ctype,
                                            element=SimpleNamespace(CurrentIsPassword=password))

    def window_text(self):
        return self.name

    def rectangle(self):
        l, t, r, b = self.rect
        return SimpleNamespace(left=l, top=t, right=r, bottom=b)

    def children(self):
        return self._children


def test_best_match_finds_the_named_control_breadth_first():
    from ai.pwa_locator import best_match
    send = Node("Send", "Button", (300, 400, 380, 430))
    tree = Node("Mail", children=[Node("Toolbar", children=[send, Node("Delete", "Button")]),
                                  Node("Message body", "Document")])
    f = best_match("Send", [tree])
    assert f.wrapper is send and f.center == (340, 415) and not f.is_password


def test_best_match_ignores_hidden_and_weak_matches():
    from ai.pwa_locator import best_match
    hidden = Node("Send", "Button", (0, 0, 0, 0))
    assert best_match("Send", [Node("Root", children=[hidden, Node("Settings")])]) is None


def test_best_match_reports_password_fields():
    from ai.pwa_locator import best_match
    pw = Node("Password", "Edit", (10, 10, 200, 40), password=True)
    assert best_match("Password", [Node("Login", children=[pw])]).is_password


def test_best_match_stops_at_the_node_cap():
    from ai.pwa_locator import best_match
    chain = Node("target", "Button", (1, 1, 9, 9))
    for i in range(50):
        chain = Node(f"n{i}", children=[chain])
    assert best_match("target", [chain], max_nodes=10) is None


def test_find_control_uses_the_foreground_window_and_taskbar(monkeypatch):
    from ai import pwa_locator
    chrome_btn = Node("Google Chrome", "Button", (300, 690, 340, 720))
    taskbar = Node("Taskbar", children=[chrome_btn])
    app = Node("Untitled - Notepad", children=[Node("Text editor", "Document")])
    desktop = SimpleNamespace(window=lambda **kw: SimpleNamespace(
        wrapper_object=lambda: taskbar if kw.get("class_name") == "Shell_TrayWnd" else app))
    monkeypatch.setattr(pwa_locator.ctypes.windll.user32, "GetForegroundWindow", lambda: 1234)
    assert pwa_locator.find_control("Google Chrome", desktop=desktop).wrapper is chrome_btn
    monkeypatch.setenv("CLICKY_USE_PYWINAUTO", "0")
    assert pwa_locator.find_control("Google Chrome", desktop=desktop) is None


# ── autopilot hands the control to the executor ──────────────────────────────

def test_autopilot_passes_the_found_control_to_the_executor():
    from agent import autopilot as ap
    from agent.planner import Goal, PlanResult
    from test_autopilot import Env, TASK, act, done, steps
    # "Inbox", not "Send": Send is a confirm word and would (rightly) wait for დიახ
    env = Env([TASK(Goal("ა", steps("ა", "ბ", "გ")))], [act("click", "Inbox"), done(), done(), done()])
    control = FakePwaControl()
    env.locate = lambda label, shot: ap.Located(140, 215, label, control, "pywinauto")
    asyncio.run(env.machine().handle("გახსენი შემოსულები"))
    target = env.executed[0]["target"]
    assert target.element is control and target.source == "uia" and (target.x, target.y) == (140, 215)


def test_manager_locator_tries_pywinauto_first(monkeypatch):
    import companion_manager as cm
    from ai import pwa_locator
    control = FakePwaControl()
    monkeypatch.setattr(pwa_locator, "find_control",
                        lambda label: pwa_locator.Found(control, "Send", (100, 200, 180, 230), False))
    m = cm.CompanionManager.__new__(cm.CompanionManager)
    loc = cm.CompanionManager._agent_locate(m, "Send", None)
    assert loc.source == "pywinauto" and loc.element is control and (loc.px, loc.py) == (140, 215)
