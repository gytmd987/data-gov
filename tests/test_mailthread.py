"""메일 스레드 — 인용문 제거 + 헤더 기반 답장·전달 관계.

답장은 원문을 통째로 인용해서 온다. 5단계 스레드면 마지막 메일 하나에 앞의 네 통이
다 들어 있어, 그대로 색인하면 같은 문장이 다섯 벌 쌓인다.

인용문을 걷어내도 **관계는 잃지 않는다**. 답장·전달 관계는 인용문이 아니라 헤더
(In-Reply-To / References)에 들어 있기 때문이다. 여기서 그 두 가지를 함께 확인한다.
"""

from email.message import EmailMessage
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.qdrant_indexer import QdrantIndexer
from app.db.models import Base, Document
from app.db.repositories import DocumentRepository, OrgRepository, RelationRepository
from app.demo.offline import EMBED_DIM, ExtractiveLLM, HashingEmbedder
from app.ingestion.htmltext import html_to_text
from app.ingestion.mailfile import thread_of
from app.ingestion.mailquote import looks_quoted, strip_quotes
from app.ingestion.parsers.email_parser import EmailParser
from app.review.service import ReviewService

ORIGINAL = ("안녕하세요, 채용 프로세스 개편 관련해 공유드립니다.\n"
            "1차 면접은 실무진 2인이 진행하고, 2차는 임원 면접입니다.\n"
            "평가표는 공통 양식을 사용하며 점수는 5점 척도입니다.")
NEW_WORDS = "확인했습니다. 2차 면접 일정만 조율 부탁드립니다."


# ── 1. 인용문 제거 규칙 ──────────────────────────────────────────────────────
def test_quote_marker_lines_are_dropped():
    text = NEW_WORDS + "\n\n> " + ORIGINAL.replace("\n", "\n> ")
    got = strip_quotes(text)
    assert got == NEW_WORDS
    assert "1차 면접은" not in got


def test_nested_quotes_are_dropped():
    text = NEW_WORDS + "\n>> 더 앞의 메일 인용\n> 바로 앞 메일 인용"
    assert strip_quotes(text) == NEW_WORDS


@pytest.mark.parametrize("separator", [
    "-----Original Message-----",
    "----- Forwarded message -----",
    "-------- 원본 메시지 --------",
    "보낸 사람: 김실무 <kim@corp.com>",
    "From: 김실무 <kim@corp.com> Sent: Monday, August 3, 2026",
    "2026년 8월 3일 (월) 오전 10:00, 김실무 <kim@corp.com>님이 작성:",
    "On Mon, Aug 3, 2026 at 10:00 AM, Kim wrote:",
])
def test_separator_cuts_everything_below(separator):
    text = f"{NEW_WORDS}\n\n{separator}\n{ORIGINAL}"
    got = strip_quotes(text)
    assert got == NEW_WORDS, f"구분선을 못 잡음: {separator}"


def test_signature_is_dropped():
    text = f"{NEW_WORDS}\n\n--\n김대리 / People팀\nTel. 02-000-0000"
    got = strip_quotes(text)
    assert got == NEW_WORDS and "02-000-0000" not in got


def test_original_mail_is_untouched():
    """스레드 첫 메일은 인용문이 없다 — 아무것도 잘리면 안 된다."""
    assert strip_quotes(ORIGINAL) == ORIGINAL


def test_body_is_kept_when_everything_would_be_cut():
    """인용만 있고 새로 쓴 말이 없는 전달 메일 — 본문이 통째로 사라지면 안 된다."""
    text = "> " + ORIGINAL.replace("\n", "\n> ")
    assert "1차 면접은" in strip_quotes(text)


def test_one_word_reply_keeps_the_quote():
    """'넵.' 한 줄만 남으면 문맥이 사라지므로 원문을 그대로 쓴다."""
    text = "넵.\n\n> " + ORIGINAL.replace("\n", "\n> ")
    assert "1차 면접은" in strip_quotes(text)


def test_looks_quoted_detection():
    assert looks_quoted("답장\n> 인용")
    assert looks_quoted(f"답장\n-----Original Message-----\n{ORIGINAL}")
    assert not looks_quoted(ORIGINAL)


def test_markdown_style_text_is_not_mistaken_for_a_quote():
    """본문에 '--' 가 들어간 표나 구분선은 서명이 아니다(줄 전체가 '--' 일 때만)."""
    text = "예산은 다음과 같습니다.\n항목 -- 금액\n인건비 -- 1000만원"
    assert strip_quotes(text) == text


# ── 2. HTML 메일의 인용 블록 ─────────────────────────────────────────────────
def test_blockquote_is_dropped_from_html():
    html = (f"<div><p>{NEW_WORDS}</p></div>"
            f"<blockquote><p>{ORIGINAL}</p></blockquote>")
    assert html_to_text(html, drop_quotes=True) == NEW_WORDS
    assert "1차 면접은" in html_to_text(html)      # 기본값은 그대로 둔다


def test_gmail_quote_container_is_dropped():
    html = (f"<div>{NEW_WORDS}</div>"
            f'<div class="gmail_quote"><div>2026년 8월 3일 작성:</div>'
            f"<div>{ORIGINAL}</div></div>")
    assert "1차 면접은" not in html_to_text(html, drop_quotes=True)


def test_outlook_reply_header_container_is_dropped():
    html = (f"<div>{NEW_WORDS}</div>"
            f'<div id="divRplyFwdMsg">보낸 사람: 김실무</div>'
            f"<div>{ORIGINAL}</div>")
    assert NEW_WORDS in html_to_text(html, drop_quotes=True)


def test_unclosed_tags_do_not_swallow_the_body():
    """메일 HTML 은 닫는 태그가 자주 빠진다 — 그래도 본문이 사라지면 안 된다."""
    html = (f"<div><blockquote><p>{ORIGINAL}</div>"
            f"<p>{NEW_WORDS}")
    got = html_to_text(html, drop_quotes=True)
    assert NEW_WORDS in got, "인용 블록이 안 닫혔다고 뒤 본문까지 삼켰다"


# ── 3. .eml 통합 ─────────────────────────────────────────────────────────────
def _reply_eml(tmp_path, *, plain=None, html=None, name="reply.eml"):
    msg = EmailMessage()
    msg["Subject"] = "RE: [공유] 채용 프로세스 개편"
    msg["From"] = "lee@corp.com"
    msg["To"] = "kim@corp.com"
    msg["Message-ID"] = "<M2@corp>"
    msg["In-Reply-To"] = "<M1@corp>"
    msg["References"] = "<M1@corp>"
    if plain is not None:
        msg.set_content(plain)
    if html is not None:
        if plain is None:
            msg.set_content(html, subtype="html")
        else:
            msg.add_alternative(html, subtype="html")
    p = tmp_path / name
    p.write_bytes(msg.as_bytes())
    return str(p)


def test_reply_email_indexes_only_the_new_words(tmp_path):
    path = _reply_eml(tmp_path, plain=NEW_WORDS + "\n\n> "
                      + ORIGINAL.replace("\n", "\n> "))
    body = "\n".join(e.text for e in EmailParser().parse(path).elements)
    assert NEW_WORDS in body
    assert "1차 면접은" not in body, "인용된 원문이 그대로 색인됐다"


def test_html_reply_indexes_only_the_new_words(tmp_path):
    html = (f"<div><p>{NEW_WORDS}</p></div>"
            f"<blockquote><p>{ORIGINAL}</p></blockquote>")
    body = "\n".join(e.text for e in EmailParser().parse(
        _reply_eml(tmp_path, html=html)).elements)
    assert NEW_WORDS in body and "1차 면접은" not in body


def test_html_reply_does_not_beat_plain_just_by_quote_length(tmp_path):
    """인용이 잔뜩 붙은 HTML 이 길이만으로 본문 자리를 뺏으면 안 된다."""
    plain = NEW_WORDS + " 회신 부탁드립니다."
    html = f"<p>{NEW_WORDS}</p><blockquote><p>{ORIGINAL * 5}</p></blockquote>"
    body = "\n".join(e.text for e in EmailParser().parse(
        _reply_eml(tmp_path, plain=plain, html=html)).elements)
    assert "회신 부탁드립니다" in body


# ── 4. 헤더가 알려주는 스레드 관계 ───────────────────────────────────────────
def _thread_msg(mid, subject, irt=None, refs=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{mid}>"
    if irt:
        msg["In-Reply-To"] = f"<{irt}>"
    if refs:
        msg["References"] = " ".join(f"<{r}>" for r in refs)
    msg.set_content("본문")
    return msg


def test_thread_info_from_headers():
    info = thread_of(_thread_msg("M3@corp", "RE: RE: 공유", irt="M2@corp",
                                 refs=["M1@corp", "M2@corp"]))
    assert info.message_id == "m3@corp"
    assert info.in_reply_to == "m2@corp"
    assert info.root == "m1@corp"


def test_first_mail_is_its_own_thread_root():
    info = thread_of(_thread_msg("M1@corp", "[공유] 채용 프로세스 개편"))
    assert info.root == "m1@corp" and info.in_reply_to is None


def test_reply_without_references_uses_in_reply_to_as_root():
    info = thread_of(_thread_msg("M2@corp", "RE: 공유", irt="M1@corp"))
    assert info.root == "m1@corp"


# ── 5. 스레드 연결(적재까지) ─────────────────────────────────────────────────
@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def org(session):
    repo = OrgRepository(session)
    team = repo.create_node("People팀", "team")
    part = repo.create_node("인사파트", "part", parent_id=team.id)
    session.commit()
    return {"part": part.id}


@pytest.fixture
def service(session, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    return ReviewService(session=session, llm=ExtractiveLLM(), llm_model="offline",
                         embedder=HashingEmbedder(),
                         indexer=QdrantIndexer(collection="c", vector_size=EMBED_DIM,
                                               client=QdrantClient(location=":memory:")))


def _write(tmp_path, name, mid, subject, irt=None, refs=None, body=None):
    msg = _thread_msg(mid, subject, irt=irt, refs=refs)
    msg.set_content(body or f"{subject} 관련 내용입니다. 인사팀 확인 부탁드립니다.")
    p = Path(tmp_path) / name
    p.write_bytes(msg.as_bytes())
    return str(p)


def _register(service, path, node_id):
    doc_id = service.start_ingestion(path, ingested_by="me@corp.com",
                                     folder_node_id=node_id)
    service.confirm_without_review(doc_id)
    return doc_id


def _links(session, doc_id):
    return {l["doc_id"]: l["reason"] for l in RelationRepository(session).related_ids(doc_id)}


def test_reply_is_linked_to_the_mail_it_answers(session, org, service, tmp_path):
    first = _register(service, _write(tmp_path, "1.eml", "M1@corp", "채용 개편 공유"),
                      org["part"])
    reply = _register(service, _write(tmp_path, "2.eml", "M2@corp", "RE 채용 개편 공유",
                                      irt="M1@corp", refs=["M1@corp"]), org["part"])
    assert _links(session, reply)[first] == "메일 답장"


def test_whole_thread_is_linked_together(session, org, service, tmp_path):
    ids = [
        _register(service, _write(tmp_path, "1.eml", "M1@corp", "채용 개편 공유"),
                  org["part"]),
        _register(service, _write(tmp_path, "2.eml", "M2@corp", "RE 채용 개편 공유",
                                  irt="M1@corp", refs=["M1@corp"]), org["part"]),
        _register(service, _write(tmp_path, "3.eml", "M3@corp", "RE RE 채용 개편 공유",
                                  irt="M2@corp", refs=["M1@corp", "M2@corp"]),
                  org["part"]),
    ]
    for doc_id in ids:                       # 셋이 서로 다 이어져 있다
        assert set(_links(session, doc_id)) == set(ids) - {doc_id}


def test_thread_links_regardless_of_upload_order(session, org, service, tmp_path):
    """마지막 답장을 먼저 올리고 원본을 나중에 올려도 이어져야 한다."""
    reply = _register(service, _write(tmp_path, "2.eml", "M2@corp", "RE 채용 개편",
                                      irt="M1@corp", refs=["M1@corp"]), org["part"])
    first = _register(service, _write(tmp_path, "1.eml", "M1@corp", "채용 개편"),
                      org["part"])
    assert _links(session, first)[reply] == "메일 답장"


def test_thread_links_even_if_a_middle_mail_is_missing(session, org, service, tmp_path):
    """가운데 답장을 아무도 안 올려도 뿌리가 같으면 묶인다."""
    first = _register(service, _write(tmp_path, "1.eml", "M1@corp", "채용 개편"),
                      org["part"])
    third = _register(service, _write(tmp_path, "3.eml", "M3@corp", "RE RE 채용 개편",
                                      irt="M2@corp", refs=["M1@corp", "M2@corp"]),
                      org["part"])
    assert _links(session, first)[third] == "메일 스레드"   # 직접 답장은 아니다


def test_unrelated_mails_are_not_linked_as_a_thread(session, org, service, tmp_path):
    a = _register(service, _write(tmp_path, "a.eml", "A@corp", "출장 정산 안내"),
                  org["part"])
    b = _register(service, _write(tmp_path, "b.eml", "B@corp", "교육 신청 안내"),
                  org["part"])
    assert _links(session, a).get(b) not in ("메일 답장", "메일 스레드")


def test_thread_fields_are_stored(session, org, service, tmp_path):
    _register(service, _write(tmp_path, "2.eml", "M2@corp", "RE 채용 개편",
                              irt="M1@corp", refs=["M1@corp"]), org["part"])
    row = session.execute(
        Document.__table__.select().where(Document.source_filename == "2.eml")).first()
    assert row.message_id == "m2@corp"
    assert row.in_reply_to == "m1@corp"
    assert row.thread_root == "m1@corp"


def test_non_mail_documents_have_no_thread_fields(session, org, service, tmp_path):
    txt = Path(tmp_path) / "규정.txt"
    txt.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
    doc_id = _register(service, str(txt), org["part"])
    row = session.get(Document, doc_id)
    assert (row.message_id, row.in_reply_to, row.thread_root) == (None, None, None)


def test_reply_body_is_shorter_than_the_raw_mail(session, org, service, tmp_path):
    """실제 적재에서도 인용문이 빠지는지 — 청크 길이로 확인."""
    path = _write(tmp_path, "2.eml", "M2@corp", "RE 채용 개편", irt="M1@corp",
                  refs=["M1@corp"],
                  body=NEW_WORDS + "\n\n> " + ORIGINAL.replace("\n", "\n> "))
    doc_id = _register(service, path, org["part"])
    text = "\n".join(c["text"] for c in DocumentRepository(session).chunks_of(doc_id))
    assert NEW_WORDS in text and "1차 면접은" not in text
