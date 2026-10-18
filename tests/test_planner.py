"""agent.planner: JSON parsing, validation, retry and fallback. No network —
every model call goes to FakeLLM below."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.planner import (
    Goal, PlanParseError, PlanResult, Step,
    extract_json_object, parse_plan, plan, strip_fences,
)
from ai.base_provider import BaseLLMProvider


def _step(i, title="გახსენით ბრაუზერი", done="ბრაუზერი გახსნილია", private=False):
    return {"id": i, "title": title, "done_when": done, "private": private}


def _plan_json(**overrides):
    data = {
        "is_task": True,
        "goals": [
            {"goal": "მეილის გახსნა", "steps": [
                _step(1),
                _step(2, "გადადით Gmail-ის საიტზე", "ეკრანზე ჩანს Gmail"),
                _step(3, "შეიყვანეთ პაროლი", "ანგარიში გახსნილია", private=True),
            ]},
            {"goal": "პირველი წერილის გახსნა", "steps": [
                _step(1, "იპოვეთ ზედა წერილი", "წერილი ნაპოვნია"),
                _step(2, "დააჭირეთ წერილს", "წერილი გაიხსნა"),
                _step(3, "წაიკითხეთ წერილი", "ტექსტი ჩანს ეკრანზე"),
            ]},
        ],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class FakeLLM(BaseLLMProvider):
    """Replays scripted replies (str, or an Exception to raise) and records
    every call. Each reply is streamed in small chunks, like a real provider."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def stream_response(self, user_text, screenshots_b64, history,
                              system_prompt, model=None):
        self.calls.append(SimpleNamespace(
            user_text=user_text, images=list(screenshots_b64),
            history=list(history), system_prompt=system_prompt, model=model,
        ))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        for i in range(0, len(reply), 7):
            yield reply[i:i + 7]

    async def health_check(self):
        return True


def run(coro):
    return asyncio.run(coro)


# ── strip_fences / extract_json_object ───────────────────────────────────────

@pytest.mark.parametrize("raw", [
    '{"is_task": false, "goals": []}',
    '```json\n{"is_task": false, "goals": []}\n```',
    '```\n{"is_task": false, "goals": []}\n```',
    '  ```JSON\n{"is_task": false, "goals": []}```  ',
])
def test_strip_fences(raw):
    assert json.loads(strip_fences(raw)) == {"is_task": False, "goals": []}


def test_extract_ignores_prose_around_the_object():
    raw = 'Here is the plan:\n{"is_task": false, "goals": []}\nHope it helps!'
    assert extract_json_object(raw) == {"is_task": False, "goals": []}


@pytest.mark.parametrize("raw", ["", "sorry, I can't", "[1, 2, 3]",
                                 '{"is_task": true, "goals": [',  # cut off
                                 "{'is_task': True}"])            # not JSON
def test_extract_rejects_non_objects(raw):
    with pytest.raises(PlanParseError):
        extract_json_object(raw)


# ── parse_plan: valid replies ────────────────────────────────────────────────

def test_parse_valid_plan_builds_dataclasses():
    result = parse_plan("```json\n" + _plan_json() + "\n```")
    assert isinstance(result, PlanResult) and result.is_task and result.error is None
    assert [g.goal for g in result.goals] == ["მეილის გახსნა", "პირველი წერილის გახსნა"]
    assert all(isinstance(g, Goal) for g in result.goals)
    first = result.goals[0].steps
    assert all(isinstance(s, Step) for s in first)
    assert [s.id for s in first] == [1, 2, 3]
    assert [s.private for s in first] == [False, False, True]


def test_parse_question_has_no_goals():
    assert parse_plan('{"is_task": false, "goals": []}') == PlanResult(is_task=False)


def test_parse_non_task_drops_any_goals():
    result = parse_plan(_plan_json(is_task=False))
    assert result.is_task is False and result.goals == []


def test_parse_missing_private_defaults_to_false():
    data = json.loads(_plan_json())
    del data["goals"][1]["steps"][0]["private"]
    assert parse_plan(json.dumps(data)).goals[1].steps[0].private is False


def test_parse_accepts_brand_names_inside_georgian_text():
    data = json.loads(_plan_json())
    data["goals"][0]["steps"][1]["title"] = "გახსენით Google Chrome"
    assert parse_plan(json.dumps(data)).is_task


# ── parse_plan: invalid replies ──────────────────────────────────────────────

def _mutate(fn):
    data = json.loads(_plan_json())
    fn(data)
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.parametrize("raw, message", [
    (_mutate(lambda d: d.update(is_task="true")), "is_task"),
    (_mutate(lambda d: d.update(goals=[])), "non-empty list"),
    (_mutate(lambda d: d.pop("goals")), "non-empty list"),
    (_mutate(lambda d: d["goals"][0]["steps"].clear()), "needs 1-6"),
    (_mutate(lambda d: d["goals"][0]["steps"].extend([_step(i) for i in range(4, 8)])), "needs 1-6"),
    (_mutate(lambda d: d["goals"][0]["steps"][0].update(title="Open the browser")), "Georgian"),
    (_mutate(lambda d: d["goals"][0].update(goal="")), "non-empty string"),
    (_mutate(lambda d: d["goals"][0]["steps"][0].update(title="დააჭირეთ (840, 610)")), "coordinates"),
    (_mutate(lambda d: d["goals"][0]["steps"][0].update(done_when="ღილაკი 1280x720 ეკრანზე")), "coordinates"),
    (_mutate(lambda d: d["goals"][0]["steps"][0].update(title="დააჭირეთ 20 პიქსელით მარჯვნივ")), "coordinates"),
    (_mutate(lambda d: d["goals"][0]["steps"][0].update(private="no")), "private"),
    (_mutate(lambda d: d["goals"][0]["steps"][1].update(id=1)), "repeats id"),
    (_mutate(lambda d: d["goals"][0]["steps"][0].update(id=True)), "positive integer"),
    (_mutate(lambda d: d["goals"][0]["steps"][0].update(id="1")), "positive integer"),
])
def test_parse_rejects_invalid_plans(raw, message):
    with pytest.raises(PlanParseError, match=message):
        parse_plan(raw)


# ── plan(): one call, retry, fallback ────────────────────────────────────────

def test_plan_makes_one_call_and_sends_the_screenshot():
    llm = FakeLLM("```json\n" + _plan_json() + "\n```")
    shot = SimpleNamespace(base64_jpeg="SCREEN")
    result = run(plan("გახსენი მეილი და პირველი წერილი", shot, llm=llm, model="m-1"))
    assert result.is_task and len(result.goals) == 2
    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call.user_text == "გახსენი მეილი და პირველი წერილი"
    assert call.images == ["SCREEN"] and call.history == [] and call.model == "m-1"
    assert "JSON" in call.system_prompt


def test_plan_retries_once_with_the_error_then_succeeds():
    llm = FakeLLM("not json at all", _plan_json())
    result = run(plan("გახსენი მეილი", SimpleNamespace(base64_jpeg="SCREEN"), llm=llm))
    assert result.is_task and result.error is None
    assert len(llm.calls) == 2
    retry = llm.calls[1]
    assert [m.role for m in retry.history] == ["user", "assistant"]
    assert retry.history[1].content == "not json at all"
    assert "no JSON object" in retry.user_text
    assert retry.images == []          # the screenshot is not uploaded twice


def test_plan_falls_back_after_two_bad_replies():
    llm = FakeLLM("still not json", _mutate(lambda d: d["goals"][0]["steps"].clear()))
    result = run(plan("გახსენი მეილი", llm=llm))
    assert result.is_task is False and result.goals == []
    assert "invalid plan after retry" in result.error and "needs 1-6" in result.error
    assert len(llm.calls) == 2


def test_plan_falls_back_when_the_provider_fails():
    llm = FakeLLM(RuntimeError("credit balance is too low"))
    result = run(plan("გახსენი მეილი", llm=llm))
    assert result == PlanResult(is_task=False, error="model call failed: credit balance is too low")
    assert len(llm.calls) == 1         # a failed call is not a bad reply: no retry


def test_plan_question_returns_no_goals():
    llm = FakeLLM('{"is_task": false, "goals": []}')
    result = run(plan("რა ამინდია დღეს?", llm=llm))
    assert result == PlanResult(is_task=False)


def test_plan_empty_request_makes_no_call():
    llm = FakeLLM()
    assert run(plan("   ", llm=llm)).is_task is False
    assert llm.calls == []


def test_a_one_step_goal_is_accepted():
    data = json.loads(_plan_json())
    del data["goals"][0]["steps"][1:]
    result = parse_plan(json.dumps(data, ensure_ascii=False))
    assert result.is_task and len(result.goals[0].steps) == 1
