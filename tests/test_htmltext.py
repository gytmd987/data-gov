"""HTML 본문 처리 — 회사 메일은 대부분 HTML 이라 마크업을 걷어내야 한다.

걷어내지 않으면 AI 요약이 "본문은 이메일 클라이언트 렌더링 코드로 채워져 있으나…"
처럼 내용과 무관한 말이 된다.
"""

from email.message import EmailMessage

import pytest

from app.ingestion.htmltext import html_to_text, looks_like_html, normalize, to_text
from app.ingestion.parsers.email_parser import EmailParser
from app.schemas.enums import ChunkType

_CORP_MAIL = """<!DOCTYPE html>
<html><head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<style>body{font-family:'Malgun Gothic';font-size:9pt}.hdr{color:#036}</style>
<title>근태 공유</title></head>
<body>
<!--[if mso]><table><tr><td><![endif]-->
<div class="hdr"><p>안녕하세요, 인사팀입니다.</p></div>
<p>3월 근태 마감 일정을 공유드립니다.</p>
<table>
  <tr><th>구분</th><th>일정</th></tr>
  <tr><td>근태 입력 마감</td><td>3월 25일</td></tr>
  <tr><td>팀장 승인 마감</td><td>3월 27일</td></tr>
</table>
<ul><li>미입력 시 자동으로 정상근무 처리됩니다.</li>
    <li>문의: 인사팀 내선 1234</li></ul>
<p>감사합니다.&nbsp;</p>
<script>trackOpen();</script>
</body></html>"""


def test_markup_is_removed_and_content_kept():
    text = html_to_text(_CORP_MAIL)
    assert "안녕하세요, 인사팀입니다." in text
    assert "3월 근태 마감 일정을 공유드립니다." in text
    assert "근태 입력 마감" in text and "3월 25일" in text
    assert "미입력 시 자동으로 정상근무 처리됩니다." in text


@pytest.mark.parametrize("junk", ["<style", "font-family", "trackOpen", "<div",
                                  "<!DOCTYPE", "[if mso]", "&nbsp;", "charset"])
def test_no_markup_or_code_survives(junk):
    assert junk not in html_to_text(_CORP_MAIL)


def test_entities_are_decoded():
    assert html_to_text("<p>연차 &amp; 반차 &lt;규정&gt;</p>") == "연차 & 반차 <규정>"


def test_blocks_become_line_breaks():
    text = html_to_text("<p>첫째 줄</p><p>둘째 줄</p>")
    assert text.splitlines() == ["첫째 줄", "둘째 줄"]


def test_list_items_are_marked():
    assert "- 첫째" in html_to_text("<ul><li>첫째</li><li>둘째</li></ul>")


def test_broken_html_still_yields_text():
    assert "연차 규정" in html_to_text("<p>연차 규정<div><span>미종료 태그")


def test_looks_like_html_detection():
    assert looks_like_html(_CORP_MAIL)
    assert looks_like_html("<div><p>가</p><br><span>나</span></div>")
    assert not looks_like_html("연차는 15일이며 5 < 10 인 경우도 있다.")
    assert not looks_like_html("")


def test_to_text_passes_plain_through():
    assert to_text("연차는  15일입니다.\n\n\n신청은 인사팀.") == \
        "연차는 15일입니다.\n신청은 인사팀."


def test_normalize_keeps_table_cell_separator():
    assert "\t" in normalize("구분\t일정")


# ── .eml 통합 ───────────────────────────────────────────────────────────────
def _eml(tmp_path, *, plain: str | None, html: str | None, subject="근태 마감 공유"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "hr@company.com"
    msg["To"] = "all@company.com"
    if plain is not None:
        msg.set_content(plain)
    if html is not None:
        if plain is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    p = tmp_path / "mail.eml"
    p.write_bytes(msg.as_bytes())
    return str(p)


def _body(result) -> str:
    return "\n".join(e.text for e in result.elements)


def test_html_only_email_is_readable(tmp_path):
    res = EmailParser().parse(_eml(tmp_path, plain=None, html=_CORP_MAIL))
    body = _body(res)
    assert "3월 근태 마감 일정" in body
    assert "<div" not in body and "font-family" not in body


def test_stub_plain_part_falls_back_to_html(tmp_path):
    """본문은 HTML 에만 있고 text/plain 은 한 줄짜리 안내인 흔한 회사 메일."""
    res = EmailParser().parse(_eml(
        tmp_path, plain="본 메일은 HTML 형식입니다.", html=_CORP_MAIL))
    body = _body(res)
    assert "근태 입력 마감" in body, "껍데기 plain 을 쓰는 바람에 본문을 놓쳤다"


def test_real_plain_part_is_preferred(tmp_path):
    """text/plain 에 제대로 된 본문이 있으면 그대로 쓴다(HTML 변환 불필요)."""
    plain = ("안녕하세요, 인사팀입니다.\n3월 근태 마감 일정을 공유드립니다.\n"
             "근태 입력 마감은 3월 25일, 팀장 승인 마감은 3월 27일입니다.\n"
             "미입력 시 자동으로 정상근무 처리되니 기한 내 입력 부탁드립니다.\n감사합니다.")
    body = _body(EmailParser().parse(_eml(tmp_path, plain=plain, html=_CORP_MAIL)))
    assert "기한 내 입력 부탁드립니다" in body


def test_headers_and_subject_are_kept(tmp_path):
    res = EmailParser().parse(_eml(tmp_path, plain=None, html=_CORP_MAIL))
    body = _body(res)
    assert "근태 마감 공유" in body and "hr@company.com" in body
    assert res.elements[0].element_type == ChunkType.TEXT
