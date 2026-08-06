"""메일 처리 — 사내 형식(.mysingle) 변환 · Message-ID 중복 · 첨부 분리.

메일은 다른 문서와 다르게 **같은 것이 여러 벌 들어온다**. 같은 메일을 A 사서함과 B
사서함에서 각자 저장하면 파일은 다르지만 메일은 같다. 첨부도 스레드마다 따라온다.
여기서 지키려는 것:
  1) 사내 형식이 표준 메일로 변환돼 그대로 읽힌다.
  2) 같은 메일은 몇 번 올려도 한 건이다(파일이 달라도).
  3) 첨부는 별도 문서로 검색되고, 메일 원본에는 남아 용량을 먹지 않는다.
"""

import base64
import zipfile
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
from app.ingestion.intake import DuplicateError, ext_for
from app.ingestion.mailfile import (
    Attachment,
    attachments_of,
    is_mail_file,
    looks_like_rfc822,
    message_id_of,
    normalize_message_id,
    read_message,
    rebuild_without,
    to_eml,
)
from app.review.service import ReviewService
from app.schemas.enums import FileFormat
from app.schemas.ingestion import IngestionStatus

BODY = ("안녕하세요, 채용 프로세스 개편 관련해 공유드립니다.\r\n"
        "1차 면접은 실무진 2인이 진행하고, 2차는 임원 면접입니다.\r\n"
        "평가표는 공통 양식을 쓰며 점수는 5점 척도입니다. 회신 부탁드립니다.\r\n")


def make_eml(path: Path, *, msgid="M1@corp", received="mx1.corp",
             subject="[공유] 채용 프로세스 개편", body=BODY,
             attachments: list[tuple[str, bytes]] | None = None) -> Path:
    """테스트용 메일 파일. attachments 를 주면 multipart 로 만든다."""
    head = (f"Message-ID: <{msgid}>\r\n"
            f"Received: from mail.corp.local by {received}\r\n"
            f"From: kim@corp.com\r\nTo: a@corp.com, b@corp.com\r\n"
            f"Date: Mon, 3 Aug 2026 10:00:00 +0900\r\nSubject: {subject}\r\n"
            "MIME-Version: 1.0\r\n")
    if not attachments:
        path.write_text(head + "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body,
                        encoding="utf-8")
        return path
    parts = [head + 'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\n'
             "Content-Type: text/plain; charset=utf-8\r\n\r\n" + body]
    for name, data in attachments:
        parts.append(
            "\r\n--B\r\nContent-Type: application/octet-stream\r\n"
            f'Content-Disposition: attachment; filename="{name}"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\n"
            + base64.b64encode(data).decode() + "\r\n")
    parts.append("--B--\r\n")
    path.write_text("".join(parts), encoding="utf-8")
    return path


# ── 1. 사내 형식(.mysingle) 변환 ─────────────────────────────────────────────
def test_mysingle_is_treated_as_mail():
    assert is_mail_file("메일.mysingle") and is_mail_file("메일.eml")
    assert not is_mail_file("보고서.docx")


def test_standard_mail_passes_through_untouched(tmp_path):
    """.mysingle 이 사실 표준 메일이면 내용은 손대지 않고 확장자만 맞춘다."""
    src = make_eml(tmp_path / "회의록.mysingle")
    out = to_eml(src, tmp_path / "conv")
    assert out.suffix == ".eml" and out.stem == "회의록"
    assert out.read_bytes() == src.read_bytes()
    assert message_id_of(out) == "m1@corp"


def test_zipped_mail_is_unwrapped(tmp_path):
    """그룹웨어가 압축해서 내려주는 경우 안에 든 메일을 꺼낸다."""
    inner = make_eml(tmp_path / "inner.eml")
    src = tmp_path / "묶음.mysingle"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("readme.txt", "설명 파일")
        zf.writestr("mail.eml", inner.read_bytes())
    out = to_eml(src, tmp_path / "conv")
    assert message_id_of(out) == "m1@corp"


def test_html_body_is_wrapped_as_mail(tmp_path):
    """메일 형식이 아니라 본문만 들어 있으면 표준 메일로 감싼다."""
    src = tmp_path / "공지.mysingle"
    src.write_text("<html><body><p>연차는 15일입니다.</p></body></html>", encoding="utf-8")
    out = to_eml(src, tmp_path / "conv")
    msg = read_message(out)
    assert str(msg["Subject"]) == "공지"
    assert "연차는 15일입니다" in out.read_text(encoding="utf-8", errors="replace")


def test_cp949_plain_text_is_readable(tmp_path):
    src = tmp_path / "안내.mysingle"
    src.write_bytes("사내 안내입니다. 연차는 15일.".encode("cp949"))
    out = to_eml(src, tmp_path / "conv")
    assert "연차는 15일" in read_message(out).get_body().get_content()


def test_random_bytes_are_not_mistaken_for_mail():
    assert not looks_like_rfc822(b"\x89PNG\r\n\x1a\n\x00\x00")
    assert not looks_like_rfc822(b"\xec\x95\x88\xeb\x85\x95\xed\x95\x98\xec\x84\xb8\xec\x9a\x94")
    assert looks_like_rfc822(b"Subject: hi\r\nFrom: a@b\r\n\r\nbody")


def test_headerish_text_without_mail_headers_is_rejected():
    """'Note: 어쩌고' 같은 평문을 메일로 오인하면 안 된다."""
    assert not looks_like_rfc822(b"Note: this is a memo\r\nAuthor: kim\r\n\r\nbody")


# ── 2. Message-ID ────────────────────────────────────────────────────────────
def test_normalize_message_id():
    assert normalize_message_id("<ABC@Host>") == "abc@host"
    assert normalize_message_id("  ") is None
    assert normalize_message_id(None) is None


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
    return {"team": team.id, "part": part.id}


@pytest.fixture
def service(session, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    return ReviewService(session=session, llm=ExtractiveLLM(), llm_model="offline",
                         embedder=HashingEmbedder(),
                         indexer=QdrantIndexer(collection="c", vector_size=EMBED_DIM,
                                               client=QdrantClient(location=":memory:")))


def _register(service, path, node_id):
    doc_id = service.start_ingestion(str(path), ingested_by="me@corp.com",
                                     folder_node_id=node_id)
    service.confirm_without_review(doc_id)
    return doc_id


def test_same_mail_from_two_mailboxes_is_one_document(session, org, service, tmp_path):
    """A 사서함 사본과 B 사서함 사본 — 파일 해시는 다르지만 같은 메일이다."""
    a = make_eml(tmp_path / "a.eml", received="mx1.a")
    b = make_eml(tmp_path / "b.eml", received="mx2.b")   # Received 만 다름
    assert a.read_bytes() != b.read_bytes()

    _register(service, a, org["part"])
    with pytest.raises(DuplicateError) as e:
        service.start_ingestion(str(b), ingested_by="other@corp.com",
                                folder_node_id=org["part"])
    assert e.value.reason == "같은 메일"
    assert len(DocumentRepository(session).list_documents()) == 1


def test_mysingle_and_eml_of_the_same_mail_are_one_document(session, org, service,
                                                            tmp_path):
    """형식이 달라도(.mysingle/.eml) 같은 메일이면 한 건이다."""
    _register(service, make_eml(tmp_path / "메일.mysingle"), org["part"])
    with pytest.raises(DuplicateError):
        service.start_ingestion(str(make_eml(tmp_path / "메일.eml")),
                                ingested_by="me@corp.com", folder_node_id=org["part"])


def test_reply_in_thread_is_a_separate_document(session, org, service, tmp_path):
    """답장은 다른 메일이다(Message-ID 가 다르다) — 합쳐 버리면 안 된다."""
    _register(service, make_eml(tmp_path / "원본.eml", msgid="M1@corp"), org["part"])
    _register(service, make_eml(tmp_path / "답장.eml", msgid="M2@corp",
                                subject="RE: [공유] 채용 프로세스 개편"), org["part"])
    assert len(DocumentRepository(session).list_documents()) == 2


def test_message_id_is_stored_for_mail_only(session, org, service, tmp_path):
    mail = _register(service, make_eml(tmp_path / "메일.eml"), org["part"])
    txt = tmp_path / "일반문서.txt"
    txt.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
    other = _register(service, txt, org["part"])

    assert session.get(Document, mail).message_id == "m1@corp"
    assert session.get(Document, other).message_id is None


def test_mail_without_message_id_still_registers(session, org, service, tmp_path):
    """Message-ID 가 없는 메일도 있다 — 그때는 파일 해시만으로 판단한다."""
    src = tmp_path / "헤더없음.eml"
    src.write_text("From: kim@corp.com\r\nSubject: 안내\r\n\r\n" + BODY, encoding="utf-8")
    doc_id = _register(service, src, org["part"])
    assert session.get(Document, doc_id).message_id is None


# ── 3. 첨부 분리 ─────────────────────────────────────────────────────────────
def test_attachments_are_listed_but_inline_images_are_not(tmp_path):
    src = make_eml(tmp_path / "m.eml", attachments=[("자료.txt", b"payload")])
    # 서명 로고(인라인 이미지)를 하나 끼워 넣는다
    raw = src.read_text(encoding="utf-8").replace(
        "--B--", "--B\r\nContent-Type: image/png\r\nContent-ID: <logo>\r\n"
        "Content-Disposition: inline; filename=\"logo.png\"\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode(b"\x89PNG").decode() + "\r\n--B--")
    src.write_text(raw, encoding="utf-8")

    names = [a.filename for a in attachments_of(read_message(src))]
    assert names == ["자료.txt"]          # 로고는 본문 그림 — 문서로 만들지 않는다


def test_attachment_path_traversal_is_stripped(tmp_path):
    src = make_eml(tmp_path / "m.eml", attachments=[("../../etc/passwd", b"x")])
    assert attachments_of(read_message(src))[0].filename == "passwd"


def test_rebuild_without_removes_only_named_attachments(tmp_path):
    big, keep = b"A" * 200_000, b"B" * 50_000
    src = make_eml(tmp_path / "m.eml",
                   attachments=[("보고서.txt", big), ("설계.hwp", keep)])
    before = src.stat().st_size
    slim = rebuild_without(src, ["보고서.txt"])

    assert len(slim) < before / 2                 # 떼어낸 만큼 작아졌다
    left = [a.filename for a in attachments_of(read_message(tmp_path / "m.eml"))]
    assert left == ["보고서.txt", "설계.hwp"]      # 원본 파일은 그대로
    from app.ingestion.mailfile import parse_bytes
    assert [a.filename for a in attachments_of(parse_bytes(slim))] == ["설계.hwp"]


def test_attachment_becomes_its_own_document(session, org, service, tmp_path):
    """첨부 내용이 검색되도록 별도 문서로 등록되고, 메일과 연관으로 이어진다."""
    src = make_eml(tmp_path / "자료송부.eml", attachments=[
        ("연차규정.txt", "연차 휴가는 15일이며 인사팀에 신청한다.".encode())])
    mail_id = _register(service, src, org["part"])

    repo = DocumentRepository(session)
    docs = {d["filename"]: d for d in repo.list_documents()}
    assert set(docs) == {"자료송부.eml", "연차규정.txt"}
    child = docs["연차규정.txt"]["doc_id"]
    assert repo.get_status(child) == IngestionStatus.INDEXED.value   # 같이 등록된다

    links = RelationRepository(session).related_ids(mail_id)
    assert any(l["doc_id"] == child and l["reason"] == "메일 첨부" for l in links)


def test_attachment_inherits_mail_folder_permissions(session, org, service, tmp_path):
    """첨부는 메일과 같은 자리에서 왔으므로 권한도 같아야 한다."""
    src = make_eml(tmp_path / "송부.eml", attachments=[
        ("평가지침.txt", "평가는 5점 척도로 하며 인사팀이 취합한다.".encode())])
    _register(service, src, org["part"])

    repo = DocumentRepository(session)
    child = next(d for d in repo.list_documents() if d["filename"] == "평가지침.txt")
    doc = repo.get(child["doc_id"])
    assert doc.governance.author_node_id == org["part"]
    assert doc.governance.access_selections == [f"node:{org['part']}"]


def test_stored_mail_no_longer_carries_the_attachment(session, org, service, tmp_path):
    """보관하는 메일에서는 첨부를 덜어낸다 — 스레드마다 사본이 쌓이지 않게."""
    payload = b"C" * 300_000
    src = make_eml(tmp_path / "송부.eml", attachments=[("자료.txt", payload)])
    mail_id = _register(service, src, org["part"])

    stored = Path(DocumentRepository(session).get_original_path(mail_id))
    assert stored.suffix == ".eml"
    assert stored.stat().st_size < src.stat().st_size / 4
    assert not attachments_of(read_message(stored))


def test_unsupported_attachment_stays_inside_the_mail(session, org, service, tmp_path):
    """등록 못 하는 형식(.hwp)은 메일에서 빼지 않는다 — 원본이 사라지면 안 된다."""
    src = make_eml(tmp_path / "기안.eml", attachments=[("기안문.hwp", b"HWP" * 1000)])
    mail_id = _register(service, src, org["part"])

    stored = Path(DocumentRepository(session).get_original_path(mail_id))
    assert [a.filename for a in attachments_of(read_message(stored))] == ["기안문.hwp"]
    assert len(DocumentRepository(session).list_documents()) == 1


def test_same_attachment_in_two_mails_is_stored_once(session, org, service, tmp_path):
    """스레드마다 따라오는 첨부 — 내용이 같으면 문서는 하나, 둘 다 연결된다."""
    data = "연차 휴가는 15일이며 인사팀에 신청한다.".encode()
    first = make_eml(tmp_path / "원본.eml", msgid="M1@corp",
                     attachments=[("연차규정.txt", data)])
    second = make_eml(tmp_path / "전달.eml", msgid="M2@corp", subject="FW: 공유",
                      attachments=[("연차규정.txt", data)])
    m1 = _register(service, first, org["part"])
    m2 = _register(service, second, org["part"])

    repo = DocumentRepository(session)
    children = [d for d in repo.list_documents() if d["filename"] == "연차규정.txt"]
    assert len(children) == 1                       # 두 번째는 중복으로 걸러졌다
    child = children[0]["doc_id"]
    rel = RelationRepository(session)
    assert any(l["doc_id"] == child for l in rel.related_ids(m1))
    assert any(l["doc_id"] == child for l in rel.related_ids(m2))   # 그래도 연결은 된다


def test_attached_mail_is_not_expanded_again(session, org, service, tmp_path):
    """첨부가 또 메일이면 거기서 멈춘다(스레드가 통째로 딸려 오면 끝없이 번진다)."""
    inner = make_eml(tmp_path / "inner.eml", msgid="INNER@corp",
                     attachments=[("속지.txt", b"deep")])
    src = make_eml(tmp_path / "겉메일.eml", msgid="OUTER@corp",
                   attachments=[("전달된메일.eml", inner.read_bytes())])
    _register(service, src, org["part"])

    names = {d["filename"] for d in DocumentRepository(session).list_documents()}
    assert names == {"겉메일.eml", "전달된메일.eml"}   # 속지.txt 까지는 안 판다


# ── 4. 저장 확장자 ───────────────────────────────────────────────────────────
def test_ext_for_uses_real_extensions():
    assert ext_for(FileFormat.EMAIL) == ".eml"      # 'email' 이면 더블클릭이 안 된다
    assert ext_for(FileFormat.DOCX) == ".docx"


def test_stored_mail_keeps_eml_extension(session, org, service, tmp_path):
    mail_id = _register(service, make_eml(tmp_path / "회의록.mysingle"), org["part"])
    stored = Path(DocumentRepository(session).get_original_path(mail_id))
    assert stored.suffix == ".eml" and stored.exists()


def test_attachment_dataclass_suffix():
    assert Attachment("보고서.XLSX", b"", "application/octet-stream").suffix == ".xlsx"
