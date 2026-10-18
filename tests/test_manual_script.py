"""The tiny manual executor script, with the executor's Win32 layer faked:
no real input, no windows, no waiting."""

import importlib.util
from pathlib import Path

import pytest

import agent.executor as ex
from test_executor import FakeWin

_path = Path(__file__).resolve().parent.parent / "scripts" / "executor_manual_test.py"
_spec = importlib.util.spec_from_file_location("executor_manual_test", _path)
mt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mt)


@pytest.fixture
def fake(monkeypatch):
    w = FakeWin()
    monkeypatch.setattr(ex, "win", w)
    monkeypatch.setattr(ex, "HOLD_S", 0.0)
    monkeypatch.setenv("CLICKY_TYPE_BACKEND", "sendinput")
    ex.reset_cancel()
    yield w
    ex.set_dry_run(False)


def _run(argv, fake):
    said, slept = [], []
    results = mt.run(mt.build_parser().parse_args(argv), ex, sleep=slept.append, say=said.append)
    return results, said, slept


def test_dry_run_by_default_sends_nothing(fake):
    results, said, slept = _run(["--x", "800", "--y", "400"], fake)
    assert slept == [3.0]
    assert [(r.type, r.status) for r in results] == [("type", "dry_run"), ("click", "dry_run")]
    assert fake.inputs() == []
    assert "DRY RUN" in said[0]


def test_real_types_georgian_then_clicks_at_x_y(fake):
    results, said, _ = _run(["--x", "800", "--y", "400", "--real"], fake)
    assert [r.status for r in results] == ["done", "done"]
    calls = fake.inputs()
    typed = "".join(c[1] for c in calls if c[0] == "type")   # sent in chunks
    assert typed == "გამარჯობა"
    assert calls[-2:] == [("set_cursor", 800, 400), ("click", 1)]   # type first, then click
    assert all(c[0] == "type" for c in calls[:-2])
    assert any("off by (0, 0)" in s for s in said)


def test_x_and_y_are_required():
    with pytest.raises(SystemExit):
        mt.build_parser().parse_args(["--x", "5"])


def test_the_script_never_launches_a_program():
    source = _path.read_text(encoding="utf-8")
    for forbidden in ("subprocess", "Popen", "os.startfile", "notepad", "ShellExecute",
                      "find_target", "planner", "QApplication"):
        assert forbidden not in source, forbidden
