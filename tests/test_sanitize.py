"""surrogate 문자 제거 테스트."""

from app.clients.sanitize import clean_texts, strip_surrogates


def test_strip_surrogates_removes_bad_chars():
    bad = "연차\ud83d규정\udc00입니다"   # 깨진 surrogate 포함
    out = strip_surrogates(bad)
    assert "\ud83d" not in out and "\udc00" not in out
    assert out == "연차규정입니다"


def test_strip_surrogates_keeps_normal_text():
    assert strip_surrogates("정상 텍스트 ABC 123") == "정상 텍스트 ABC 123"


def test_clean_texts_list():
    assert clean_texts(["a\ud800b", "c"]) == ["ab", "c"]
