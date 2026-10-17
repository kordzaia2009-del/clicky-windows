import pytest

from agent.safety import (
    CONFIRM, PRIVATE, SAFE, classify, matches_keyword, normalize_level, stricter,
)


@pytest.mark.parametrize("value, level", [
    ("safe", SAFE), ("CONFIRM", CONFIRM), (" private ", PRIVATE),
    (None, CONFIRM), ("", CONFIRM), ("dangerous", CONFIRM),
])
def test_normalize_level_defaults_to_confirm(value, level):
    assert normalize_level(value) == level


def test_stricter():
    assert stricter(SAFE, CONFIRM) == CONFIRM
    assert stricter(PRIVATE, CONFIRM) == PRIVATE
    assert stricter(SAFE, SAFE) == SAFE


@pytest.mark.parametrize("text, hit", [
    ("გაგზავნა", "გაგზავნა"),
    ("გაგზავნე წერილი", "გაგზავნა"),        # inflected Georgian
    ("წაშალე", None),                        # different stem: not in the list
    ("წაშლა ყველა", "წაშლა"),
    ("Send", "Send"), ("Send now", "Send"),
    ("Payment details", "Pay"),
    ("display settings", None),              # "pay" inside a word
    ("Delete account", "Delete"),
    ("Inbox", None), ("", None),
])
def test_matches_keyword(text, hit):
    assert matches_keyword(text, ("გაგზავნა", "წაშლა", "Send", "Pay", "Delete")) == hit


def test_safe_click_stays_safe():
    d = classify("safe", {"type": "click"}, "Inbox")
    assert d.level == SAFE


def test_keyword_on_target_forces_confirm_even_if_model_says_safe():
    d = classify("safe", {"type": "click"}, "გაგზავნა")
    assert d.level == CONFIRM and any("გაგზავნა" in r for r in d.reasons)


def test_keyword_in_typed_text_forces_confirm():
    assert classify("safe", {"type": "type", "text": "Confirm"}, "Search").level == CONFIRM


def test_model_confirm_is_kept():
    assert classify("confirm", {"type": "click"}, "Next").level == CONFIRM


def test_typing_into_password_field_is_private():
    d = classify("safe", {"type": "type", "text": "x"}, "Email", target_is_private_field=True)
    assert d.level == PRIVATE


@pytest.mark.parametrize("label", ["Password", "პაროლი", "Card number", "SMS code", "CVV"])
def test_typing_into_private_labels_is_private(label):
    assert classify("safe", {"type": "type", "text": "x"}, label).level == PRIVATE


def test_clicking_a_password_field_is_not_private():
    assert classify("safe", {"type": "click"}, "Password").level == SAFE


def test_model_private_wins():
    assert classify("private", {"type": "click"}, "Next").level == PRIVATE


def test_reasons_never_contain_the_typed_text():
    d = classify("safe", {"type": "type", "text": "secret-Send-123"}, "Message")
    assert d.level == CONFIRM
    assert all("secret" not in r for r in d.reasons)


def test_editable_keyword_list():
    assert classify("safe", {"type": "click"}, "Archive", keywords=["Archive"]).level == CONFIRM
    assert classify("safe", {"type": "click"}, "Send", keywords=["Archive"]).level == SAFE
