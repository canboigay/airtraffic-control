"""Spoken sanitize + confirm heuristic."""

from backend.agent import _looks_like_confirm, sanitize_spoken


def test_sanitize_caps_words():
    long = "All five workers are running: A B C D E F G H I J K L M N O P Q R S T U V W X Y Z."
    out = sanitize_spoken(long)
    assert len(out.split()) <= 22
    assert "\n" not in out
    assert "**" not in out


def test_confirm_phrases():
    assert _looks_like_confirm("confirm kill")
    assert _looks_like_confirm("yes")
    assert _looks_like_confirm("Confirm the kill")
    assert not _looks_like_confirm("kill log spam")
    assert not _looks_like_confirm("status")
