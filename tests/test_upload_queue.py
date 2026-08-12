"""예약 업로드(웹에서 올려두고 야간에 자동 등록).

중요한 건 세 가지다.
  1) 워커를 여러 개 띄워도 **같은 파일을 둘이 등록하지 않는다**(중복 문서 방지).
  2) 처리 시간대(18:00~08:00 처럼 자정을 넘기는 구간) 계산이 맞는다.
  3) 검토를 생략해도 **권한은 고른 폴더에서** 온다(권한이 새면 안 된다).
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base, UploadJob
from app.db.repositories import DocumentRepository, OrgRepository, UploadJobRepository
from app.demo.offline import ExtractiveLLM, HashingEmbedder
from app.manage.schedule import (DEFAULT_TZ, clock_report, describe, in_window,
                                 next_at, next_run_hint, now_local, parse_hhmm,
                                 seconds_until, to_local, tz_label, tz_of,
                                 window_from_settings)
from app.review.service import ReviewService
from app.schemas.ingestion import IngestionStatus
from scripts import ingest_worker as worker


def _kst(*args) -> datetime:
    """업무 시간대(KST) 기준 시각 — 서버 시계가 UTC 여도 흔들리지 않게."""
    return datetime(*args, tzinfo=ZoneInfo(DEFAULT_TZ))


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
    now = _kst(2026, 1, 5, 12, 0)
    assert seconds_until(now, time(18, 0)) == 6 * 3600
    assert seconds_until(now, time(9, 0)) == 21 * 3600      # 오늘은 지났으니 내일


def test_hint_only_shown_outside_window():
    start, end = time(18, 0), time(8, 0)
    assert next_run_hint(_kst(2026, 1, 5, 20, 0), start, end) is None
    hint = next_run_hint(_kst(2026, 1, 5, 16, 30), start, end)
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


def test_rejects_out_of_range_time():
    assert parse_hhmm("25:00", time(18, 0)) == time(18, 0)
    assert parse_hhmm("12:99", time(18, 0)) == time(18, 0)


# ── 시간대: 서버 시계가 UTC 여도 KST 로 해석해야 한다 ────────────────────────
def test_now_local_follows_configured_timezone():
    """서버가 UTC 로 돌아도 '지금'은 업무 시간대 기준이어야 한다.

    이게 틀리면 18:00~08:00 설정이 KST 03:00~17:00 = 업무시간에 돌아간다.
    """
    class _S:
        schedule_timezone = "Asia/Seoul"

    utc_now = datetime.now(timezone.utc)
    local = now_local(_S())
    assert local.tzinfo is not None
    assert abs((local - utc_now).total_seconds()) < 5      # 같은 순간
    assert local.utcoffset() == timedelta(hours=9)         # 표기만 KST


def test_naive_time_is_treated_as_utc():
    naive = datetime(2026, 1, 5, 0, 0)                     # 00:00 UTC
    assert to_local(naive).hour == 9                       # = 09:00 KST


def test_bad_timezone_falls_back_to_default():
    class _S:
        schedule_timezone = "Mars/Olympus"
    assert str(tz_of(_S())) == DEFAULT_TZ


def test_timezone_fallback_never_raises(monkeypatch):
    """폐쇄망 컨테이너에 tzdata 가 없어도 화면이 죽으면 안 된다."""
    import app.manage.schedule as sched

    def _no_tzdata(_name):
        raise sched.ZoneInfoNotFoundError("tzdata 없음")

    monkeypatch.setattr(sched, "ZoneInfo", _no_tzdata)
    assert tz_of(None) is timezone.utc          # UTC 로 떨어질 뿐 예외는 없다


def test_tz_label_shows_offset():
    class _S:
        schedule_timezone = "Asia/Seoul"
    assert tz_label(_S()) == "KST(UTC+09:00)"


def test_clock_report_has_what_diagnosis_needs():
    """시각이 이상할 때 원인을 좁히려면 UTC·로컬·시스템 시계가 다 있어야 한다."""
    rep = clock_report(None)
    assert set(rep) >= {"utc", "local", "tz", "tz_label", "system_naive", "system_tz"}
    assert rep["utc"].tzinfo is not None
    assert abs((rep["local"] - rep["utc"]).total_seconds()) < 1   # 같은 순간
    assert rep["tz"] == DEFAULT_TZ


def test_next_at_returns_utc_for_local_time():
    """KST 18:00 은 UTC 09:00 이다 — 저장은 UTC 로 한다."""
    class _S:
        schedule_timezone = "Asia/Seoul"

    when = next_at(time(18, 0), _S(), after=_kst(2026, 1, 5, 12, 0))
    assert when.utcoffset() == timedelta(0)                # UTC 로 나온다
    assert when == datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    assert to_local(when, _S()).hour == 18                 # 되돌리면 18시


def test_next_at_rolls_to_tomorrow_when_past():
    class _S:
        schedule_timezone = "Asia/Seoul"
    when = next_at(time(9, 0), _S(), after=_kst(2026, 1, 5, 12, 0))
    assert to_local(when, _S()).date() == datetime(2026, 1, 6).date()


# ── 예약 시작 시각 ───────────────────────────────────────────────────────────
def test_job_is_not_claimed_before_its_start_time(session):
    repo = UploadJobRepository(session)
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    repo.enqueue(path="/q/a.txt", source_filename="a.txt", uploaded_by="me",
                 batch="b1", start_after=later)
    session.commit()

    assert repo.claim() is None                            # 아직 시작 시각 전
    assert repo.claim(now=later + timedelta(minutes=1)) is not None


def test_jobs_without_start_time_are_claimed_immediately(session):
    """예전에 쌓인 작업(start_after 없음)도 그대로 처리돼야 한다."""
    repo = UploadJobRepository(session)
    repo.enqueue(path="/q/a.txt", source_filename="a.txt", uploaded_by="me", batch="b1")
    session.commit()
    assert repo.claim() is not None


def test_outside_window_only_urgent_jobs_are_claimed(session):
    """시간대 밖에서는 '지금 바로'로 올린 것만 집는다."""
    repo = UploadJobRepository(session)
    repo.enqueue(path="/q/normal.txt", source_filename="normal.txt",
                 uploaded_by="me", batch="b1")
    repo.enqueue(path="/q/urgent.txt", source_filename="urgent.txt",
                 uploaded_by="me", batch="b1", bypass_window=True)
    session.commit()

    job = repo.claim(window_open=False)
    assert job is not None and job.source_filename == "urgent.txt"
    assert repo.claim(window_open=False) is None           # 일반 건은 안 집는다
    assert repo.claim(window_open=True) is not None        # 시간대 안에서는 집는다


def test_claimable_counts_respect_window_and_start_time(session):
    repo = UploadJobRepository(session)
    repo.enqueue(path="/q/a.txt", source_filename="a.txt", uploaded_by="me", batch="b")
    repo.enqueue(path="/q/b.txt", source_filename="b.txt", uploaded_by="me", batch="b",
                 bypass_window=True)
    repo.enqueue(path="/q/c.txt", source_filename="c.txt", uploaded_by="me", batch="b",
                 start_after=datetime.now(timezone.utc) + timedelta(hours=5))
    session.commit()

    assert repo.claimable() == 2                           # c 는 아직 시작 전
    assert repo.claimable(window_open=False) == 1          # b(지금 바로)만


def test_earliest_start_is_reported(session):
    repo = UploadJobRepository(session)
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    late = datetime.now(timezone.utc) + timedelta(hours=9)
    repo.enqueue(path="/q/a.txt", source_filename="a.txt", uploaded_by="me",
                 batch="b", start_after=late)
    repo.enqueue(path="/q/b.txt", source_filename="b.txt", uploaded_by="me",
                 batch="b", start_after=soon)
    session.commit()
    got = repo.earliest_start("me")
    assert abs((got.replace(tzinfo=timezone.utc) - soon).total_seconds()) < 2


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


def test_worker_stops_at_window_end_and_resumes_later(session, org, service, tmp_path,
                                                      monkeypatch):
    """08:00 이 되면 새 작업을 더 집지 않는다 — 남은 건 다음 시간대에 이어서.

    지금까지 등록한 문서는 한 건씩 커밋되므로 그대로 남는다.
    """
    for i in range(4):
        src = tmp_path / f"문서{i}.txt"
        src.write_text(f"연차 규정 {i}. 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
        _queued(session, src, org["ㄴ"])

    # 2건 처리한 시점에 시간대가 끝나도록 시계를 조작한다
    calls = {"n": 0}
    inside = _kst(2026, 1, 6, 3, 0)      # 시간대 안(새벽 3시)
    outside = _kst(2026, 1, 6, 9, 0)     # 시간대 밖(오전 9시)

    def fake_now(_settings=None):
        calls["n"] += 1
        return inside if calls["n"] <= 2 else outside

    monkeypatch.setattr(worker, "now_local", fake_now)
    stats = worker.drain(workers=1, window=(time(18, 0), time(8, 0)))

    assert stats["done"] == 2, f"시간대가 끝났는데 계속 처리함: {stats}"
    assert stats["paused"] == 1
    repo = UploadJobRepository(session)
    assert repo.counts()[UploadJobRepository.QUEUED] == 2   # 남은 건 대기열에 그대로
    assert len(DocumentRepository(session).list_documents()) == 2   # 등록분은 남는다

    # 다음 시간대에 이어서 처리
    monkeypatch.setattr(worker, "now_local", lambda _s=None: inside)
    again = worker.drain(workers=1, window=(time(18, 0), time(8, 0)))
    assert again["done"] == 2
    assert repo.counts()[UploadJobRepository.QUEUED] == 0


def test_urgent_job_runs_even_outside_the_window(session, org, service, tmp_path,
                                                 monkeypatch):
    """'지금 바로'로 올린 건 업무시간이어도 처리된다."""
    src = tmp_path / "급한자료.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
    UploadJobRepository(session).enqueue(
        path=str(src), source_filename=src.name, uploaded_by="me@x.kr",
        batch="b1", folder_node_id=org["ㄴ"], bypass_window=True)
    session.commit()

    monkeypatch.setattr(worker, "now_local", lambda _s=None: _kst(2026, 1, 6, 14, 0))
    stats = worker.drain(workers=1, window=(time(18, 0), time(8, 0)))
    assert stats["done"] == 1, f"지금 바로인데 처리 안 됨: {stats}"


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


# ── 취소 ─────────────────────────────────────────────────────────────────────
# 깨진 파일이 계속 실패해도 대기열에서 뺄 방법이 없으면, 워커가 매번 같은 것에 걸리고
# 화면의 실패 건수도 안 줄어든다.
def test_cancel_removes_the_job_and_reports_the_file_to_delete(session):
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/b1/000/깨진파일.hwp", source_filename="깨진파일.hwp",
                       uploaded_by="me", batch="b1")
    session.commit()

    paths = repo.cancel([job.id])

    assert paths == ["/q/b1/000/깨진파일.hwp"]      # 호출자가 이 파일을 지운다
    assert repo.counts()[UploadJobRepository.QUEUED] == 0


def test_cancel_works_on_failed_jobs_too(session):
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/a.txt", source_filename="a.txt",
                       uploaded_by="me", batch="b1")
    session.commit()
    repo.claim()
    repo.fail(job.id, "읽지 못했습니다", retry=False)

    assert repo.cancel([job.id]) == ["/q/a.txt"]
    assert repo.counts()[UploadJobRepository.FAILED] == 0


def test_cancel_leaves_a_job_a_worker_is_processing(session):
    """지금 워커가 쓰고 있는 파일을 지우면 그쪽이 이상하게 실패한다."""
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/a.txt", source_filename="a.txt",
                       uploaded_by="me", batch="b1")
    session.commit()
    repo.claim()                                   # → processing

    assert repo.cancel([job.id]) == []
    assert repo.counts()[UploadJobRepository.PROCESSING] == 1


def test_cancel_cannot_touch_someone_elses_job(session):
    repo = UploadJobRepository(session)
    other = repo.enqueue(path="/q/b.txt", source_filename="b.txt",
                         uploaded_by="you", batch="b2")
    session.commit()

    assert repo.cancel([other.id], uploaded_by="me") == []
    assert repo.counts()[UploadJobRepository.QUEUED] == 1


def test_cancel_failed_is_scoped_to_the_uploader(session):
    repo = UploadJobRepository(session)
    mine = repo.enqueue(path="/q/a.txt", source_filename="a.txt",
                        uploaded_by="me", batch="b1")
    other = repo.enqueue(path="/q/b.txt", source_filename="b.txt",
                         uploaded_by="you", batch="b2")
    session.commit()
    for job in (mine, other):
        repo.claim()
        repo.fail(job.id, "깨짐", retry=False)

    assert repo.cancel_failed("me") == ["/q/a.txt"]
    assert repo.counts()[UploadJobRepository.FAILED] == 1     # 남의 것은 남는다


def test_list_queued_shows_waiting_jobs_oldest_first(session):
    repo = UploadJobRepository(session)
    repo.enqueue(path="/q/a.txt", source_filename="a.txt", uploaded_by="me", batch="b1")
    repo.enqueue(path="/q/b.txt", source_filename="b.txt", uploaded_by="me", batch="b1")
    done = repo.enqueue(path="/q/c.txt", source_filename="c.txt",
                        uploaded_by="me", batch="b1")
    session.commit()
    repo.finish(done.id, "D1")

    rows = repo.list_queued("me")

    assert [r["filename"] for r in rows] == ["a.txt", "b.txt"]   # 끝난 건 빠진다


def test_discard_staged_removes_the_file_and_its_empty_folders(tmp_path):
    from app.manage.schedule import discard_staged

    staged = tmp_path / "batch1" / "000" / "문서.txt"
    staged.parent.mkdir(parents=True)
    staged.write_text("내용", encoding="utf-8")

    discard_staged(str(staged))

    assert not staged.exists()
    assert not staged.parent.exists()          # 번호 폴더
    assert not staged.parent.parent.exists()   # 배치 폴더


def test_discard_staged_keeps_a_folder_that_still_has_files(tmp_path):
    from app.manage.schedule import discard_staged

    keep = tmp_path / "batch1" / "001" / "남길것.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("내용", encoding="utf-8")
    gone = tmp_path / "batch1" / "000" / "지울것.txt"
    gone.parent.mkdir(parents=True)
    gone.write_text("내용", encoding="utf-8")

    discard_staged(str(gone))

    assert not gone.exists() and keep.exists()
    assert keep.parent.parent.exists()         # 배치 폴더는 남는다


def test_discard_staged_is_quiet_when_the_file_is_already_gone(tmp_path):
    from app.manage.schedule import discard_staged

    discard_staged(str(tmp_path / "없는파일.txt"))   # 예외가 나면 안 된다
    discard_staged("")


def test_run_now_makes_a_scheduled_job_claimable_immediately(session):
    """급한 문서를 밤까지 기다리지 않게 — 예약 시각·시간대를 모두 건너뛴다."""
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/a.txt", source_filename="a.txt", uploaded_by="me",
                       batch="b1", start_after=_kst(2099, 1, 1, 18, 0))
    session.commit()
    assert repo.claimable() == 0                    # 예약 시각이 한참 뒤

    assert repo.run_now([job.id]) == 1

    assert repo.claimable() == 1
    assert repo.claimable(window_open=False) == 1   # 시간대 밖에서도 집힌다
    assert repo.claim().source_filename == "a.txt"


def test_run_now_cannot_touch_someone_elses_job(session):
    repo = UploadJobRepository(session)
    other = repo.enqueue(path="/q/b.txt", source_filename="b.txt", uploaded_by="you",
                         batch="b2", start_after=_kst(2099, 1, 1, 18, 0))
    session.commit()

    assert repo.run_now([other.id], uploaded_by="me") == 0
    assert repo.claimable() == 0                    # 그대로 예약 상태


def test_run_now_ignores_jobs_that_are_not_waiting(session):
    """처리 중이거나 이미 끝난 건은 대상이 아니다."""
    repo = UploadJobRepository(session)
    job = repo.enqueue(path="/q/a.txt", source_filename="a.txt",
                       uploaded_by="me", batch="b1")
    session.commit()
    repo.claim()                                    # → processing

    assert repo.run_now([job.id]) == 0
