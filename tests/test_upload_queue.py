"""예약 업로드(웹에서 올려두고 야간에 자동 등록).

중요한 건 세 가지다.
  1) 워커를 여러 개 띄워도 **같은 파일을 둘이 등록하지 않는다**(중복 문서 방지).
  2) 처리 시간대(18:00~08:00 처럼 자정을 넘기는 구간) 계산이 맞는다.
  3) 검토를 생략해도 **권한은 고른 폴더에서** 온다(권한이 새면 안 된다).
"""

from datetime import datetime, time, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base, UploadJob
from app.db.repositories import DocumentRepository, OrgRepository, UploadJobRepository
from app.demo.offline import ExtractiveLLM, HashingEmbedder
from app.manage.schedule import (describe, in_window, next_run_hint, parse_hhmm,
                                 seconds_until, window_from_settings)
from app.review.service import ReviewService
from app.schemas.ingestion import IngestionStatus
from scripts import ingest_worker as worker


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
    part = repo.create_node("ㄴ파트", "part", parent_id=team.id)
    session.commit()
    return {"team": team.id, "ㄴ": part.id}


# ── 대기열 ───────────────────────────────────────────────────────────────────
def test_claim_is_exclusive(session):
    """한 번 선점한 작업은 다시 잡히지 않는다(워커 둘이 같은 파일을 등록하면 중복)."""
    repo = UploadJobRepository(session)
    repo.enqueue(path="/q/a.txt", source_filename="a.txt", uploaded_by="me", batch="b1")
    repo.enqueue(path="/q/b.txt", source_filename="b.txt", uploaded_by="me", batch="b1")
    session.commit()

    first, second, third = repo.claim(), repo.claim(), repo.claim()
    assert [j.source_filename for j in (first, second)] == ["a.txt", "b.txt"]
    assert third is None
    assert first.status == UploadJobRepository.PROCESSING


def test_failure_retries_then_gives_up(session):
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/a.txt", source_filename="a.txt",
                       uploaded_by="me", batch="b1")
    session.commit()

    for _ in range(UploadJobRepository.MAX_ATTEMPTS):
        claimed = repo.claim()
        assert claimed is not None
        repo.fail(claimed.id, "일시 오류")
    assert session.get(UploadJob, job.id).status == UploadJobRepository.FAILED
    assert repo.claim() is None                      # 더 이상 잡히지 않는다


def test_permanent_failure_does_not_retry(session):
    """파일을 못 읽는 실패는 다시 해도 같으므로 바로 실패로 마감한다."""
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/a.hwp", source_filename="a.hwp",
                       uploaded_by="me", batch="b1")
    session.commit()
    repo.claim()
    repo.fail(job.id, "지원하지 않는 형식", retry=False)
    assert session.get(UploadJob, job.id).status == UploadJobRepository.FAILED


def test_retry_failed_puts_jobs_back(session):
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/a.txt", source_filename="a.txt",
                       uploaded_by="me", batch="b1")
    session.commit()
    repo.claim()
    repo.fail(job.id, "오류", retry=False)

    assert repo.retry_failed("me") == 1
    back = session.get(UploadJob, job.id)
    assert (back.status, back.attempts, back.error) == (UploadJobRepository.QUEUED, 0, None)


def test_counts_and_clear_are_scoped_to_uploader(session):
    """남의 예약 현황이 보이거나 남의 기록을 지우면 안 된다."""
    repo = UploadJobRepository(session)
    mine = repo.enqueue(path="/q/a.txt", source_filename="a.txt",
                        uploaded_by="me", batch="b1")
    other = repo.enqueue(path="/q/b.txt", source_filename="b.txt",
                         uploaded_by="you", batch="b2")
    session.commit()
    repo.finish(mine.id, "D1")
    repo.finish(other.id, "D2")

    assert repo.counts("me")[UploadJobRepository.DONE] == 1
    assert repo.counts()[UploadJobRepository.DONE] == 2       # 관리자 = 전체
    assert [j["filename"] for j in repo.list_jobs(uploaded_by="me")] == ["a.txt"]
    assert repo.clear_done("me") == 1
    assert repo.counts()[UploadJobRepository.DONE] == 1       # 남의 것은 남는다


def test_reclaim_stale_returns_dead_workers_jobs(session):
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/a.txt", source_filename="a.txt",
                       uploaded_by="me", batch="b1")
    session.commit()
    repo.claim()
    job.started_at = datetime.now(timezone.utc) - timedelta(hours=2)   # 워커가 죽은 상태
    session.commit()

    assert repo.reclaim_stale(older_than_minutes=30) == 1
    assert session.get(UploadJob, job.id).status == UploadJobRepository.QUEUED


# ── 처리 시간대 ──────────────────────────────────────────────────────────────
def test_overnight_window_wraps_midnight():
    start, end = time(18, 0), time(8, 0)
    inside = [datetime(2026, 1, 5, h) for h in (18, 21, 23)]
    inside += [datetime(2026, 1, 6, h) for h in (0, 3, 7)]
    for now in inside:
        assert in_window(now, start, end), now
    for now in (datetime(2026, 1, 6, 8), datetime(2026, 1, 6, 12),
                datetime(2026, 1, 6, 17, 59)):
        assert not in_window(now, start, end), now


def test_daytime_window_does_not_wrap():
    start, end = time(9, 0), time(18, 0)
    assert in_window(datetime(2026, 1, 5, 12), start, end)
    assert not in_window(datetime(2026, 1, 5, 20), start, end)


def test_equal_bounds_mean_always_on():
    assert in_window(datetime(2026, 1, 5, 3), time(0, 0), time(0, 0))


def test_seconds_until_next_start():
    now = datetime(2026, 1, 5, 12, 0)
    assert seconds_until(now, time(18, 0)) == 6 * 3600
    assert seconds_until(now, time(9, 0)) == 21 * 3600      # 오늘은 지났으니 내일


def test_hint_only_shown_outside_window():
    start, end = time(18, 0), time(8, 0)
    assert next_run_hint(datetime(2026, 1, 5, 20), start, end) is None
    hint = next_run_hint(datetime(2026, 1, 5, 16, 30), start, end)
    assert "18:00" in hint and "1시간 30분" in hint


def test_parse_and_describe():
    assert parse_hhmm("07:30", time(0, 0)) == time(7, 30)
    assert parse_hhmm("이상한값", time(18, 0)) == time(18, 0)
    assert describe(time(18, 0), time(8, 0)).endswith("(익일)")
    assert describe(time(9, 0), time(18, 0)) == "09:00~18:00"


def test_window_from_settings_uses_defaults():
    class _S:
        pass
    assert window_from_settings(_S()) == (time(18, 0), time(8, 0))


# ── 워커(오프라인 fake 로 실제 등록까지) ─────────────────────────────────────
class _NoIndexer:
    client = None
    collection = None

    def upsert(self, *a, **k):
        return 0

    def set_doc_payload(self, *a, **k):
        pass

    def delete_doc(self, *a, **k):
        pass


@pytest.fixture
def service(session, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    svc = ReviewService(session=session, llm=ExtractiveLLM(), llm_model="offline",
                        embedder=HashingEmbedder(), indexer=_NoIndexer())
    monkeypatch.setattr(worker, "_service", lambda: svc)
    return svc


def _queued(session, path, node_id, uploader="me@x.kr"):
    repo = UploadJobRepository(session)
    job = repo.enqueue(path=str(path), source_filename=path.name,
                       uploaded_by=uploader, batch="b1", folder_node_id=node_id)
    session.commit()
    return job


def test_worker_registers_with_folder_permissions(session, org, service, tmp_path):
    """검토를 건너뛰어도 작성부서·열람 권한은 **고른 폴더**에서 온다."""
    src = tmp_path / "연차규정.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
    _queued(session, src, org["ㄴ"])

    stats = worker.drain(workers=1)
    assert stats["done"] == 1

    repo = DocumentRepository(session)
    doc_id = repo.list_documents()[0]["doc_id"]
    assert repo.get_status(doc_id) == IngestionStatus.INDEXED.value
    doc = repo.get(doc_id)
    assert doc.governance.author_node_id == org["ㄴ"]
    assert doc.governance.access_selections == [f"node:{org['ㄴ']}"]
    assert not src.exists()                       # 등록됐으면 대기 파일은 지운다


def test_worker_marks_job_done_with_doc_id(session, org, service, tmp_path):
    src = tmp_path / "규정.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
    job = _queued(session, src, org["ㄴ"])

    worker.drain(workers=1)
    row = session.get(UploadJob, job.id)
    assert row.status == UploadJobRepository.DONE
    assert row.doc_id and row.error is None


def test_missing_queue_file_fails_without_retry(session, org, service, tmp_path):
    job = _queued(session, tmp_path / "없는파일.txt", org["ㄴ"])
    stats = worker.drain(workers=1)
    assert stats["failed"] == 1
    row = session.get(UploadJob, job.id)
    assert row.status == UploadJobRepository.FAILED      # 다시 해도 같으므로 재시도 안 함
    assert "대기 파일이 없습니다" in row.error


def test_duplicate_is_skipped_not_failed(session, org, service, tmp_path):
    """같은 내용을 두 번 예약해도 실패가 아니라 건너뛴다(재시도해도 소용없다)."""
    body = "연차 휴가는 15일이며 인사팀에 신청한다."
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text(body, encoding="utf-8")
    second.write_text(body, encoding="utf-8")
    _queued(session, first, org["ㄴ"])
    _queued(session, second, org["ㄴ"])

    stats = worker.drain(workers=1)
    assert (stats["done"], stats["skipped"], stats["failed"]) == (1, 1, 0)
    assert len(DocumentRepository(session).list_documents()) == 1
