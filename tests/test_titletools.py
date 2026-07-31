"""제목 구성/날짜 정규화 유닛 테스트."""

from datetime import date

from app.ingestion.titletools import compose_title, extract_date, to_iso


def test_extract_various_date_formats():
    assert extract_date("2025-07-28 인사평가")[0] == date(2025, 7, 28)
    assert extract_date("인사평가_20250728")[0] == date(2025, 7, 28)
    assert extract_date("보고서 250728 최종")[0] == date(2025, 7, 28)
    assert extract_date("2025년 7월 28일 회의록")[0] == date(2025, 7, 28)
    assert extract_date("25.07.28 지침")[0] == date(2025, 7, 28)
    assert extract_date("(25-0728) 이미정규화")[0] == date(2025, 7, 28)
    assert extract_date("연차규정 최종본")[0] is None       # 날짜 아님
    assert extract_date("코드 99999999")[0] is None          # 유효 날짜 아님


def test_date_moved_to_front_and_normalized():
    # 파일명 중간/끝의 날짜를 앞으로 옮기고 (YY-MMDD)로 통일
    assert compose_title("인사평가결과_2025-07-28.txt") == "(25-0728) 인사평가결과"
    assert compose_title("2025.07.28 채용공고.pdf") == "(25-0728) 채용공고"
    assert compose_title("채용공고 250728.docx") == "(25-0728) 채용공고"


def test_filename_preserved_when_no_date():
    assert compose_title("연차 휴가 규정.txt") == "연차 휴가 규정"


def test_ai_date_used_when_filename_has_none():
    assert compose_title("회의록.txt", ai_date="2025-07-28") == "(25-0728) 회의록"


def test_uninformative_filename_uses_ai_title():
    assert compose_title("새문서.txt", ai_title="Q3 예산 보고") == "Q3 예산 보고"
    assert compose_title("새문서.txt", ai_title="Q3 예산", ai_date="2025-07-28") == "(25-0728) Q3 예산"


def test_attachment_names_use_ai_title():
    """'붙임1', '별첨2' 같은 이름은 내용을 모르니 AI 제안 제목을 쓴다."""
    assert compose_title("붙임1.xlsx", ai_title="2024년 임직원 명단") == "2024년 임직원 명단"
    assert compose_title("별첨 2.docx", ai_title="급여 지급 기준") == "급여 지급 기준"
    assert compose_title("첨부.pdf", ai_title="채용 공고") == "채용 공고"


def test_length_bounded():
    long = "아주" * 60
    assert len(compose_title(long + ".txt")) <= 60


def test_long_title_cut_at_word_boundary():
    """단어 중간에서 자르지 않는다."""
    name = "인사 제도 개편 관련 주요 논의 사항 정리 및 향후 추진 계획 보고 자료입니다"
    title = compose_title(name + ".docx")
    assert len(title) <= 60
    assert not title.endswith(" ")
    assert title in name          # 원본의 접두사여야(중간 글자 조작 없음)


# ── 잡음 제거 ────────────────────────────────────────────────────────────────
def test_version_and_status_suffixes_removed():
    """끝에 붙은 버전·상태 꼬리표는 제거한다(버전 정리가 동작하려면 필수)."""
    assert compose_title("연차규정_최종_v3(수정).docx") == "연차규정"
    assert compose_title("급여지침 final.docx") == "급여지침"
    assert compose_title("평가표_rev2.xlsx") == "평가표"
    assert compose_title("조직개편 v0.9 초안.pptx") == "조직개편"
    assert compose_title("취업규칙 (1).docx") == "취업규칙"


def test_meaningful_words_at_front_are_kept():
    """'최종'이 제목 앞·중간에 있으면 진짜 제목이므로 지우지 않는다."""
    assert compose_title("최종 평가 지침.docx") == "최종 평가 지침"
    assert compose_title("초안 작성 가이드.docx") == "초안 작성 가이드"


def test_copy_prefix_removed():
    assert compose_title("사본 - 복사본 - 급여규정_v2 (1).docx") == "급여규정"
    assert compose_title("Copy of 인사규정.docx") == "인사규정"


def test_department_prefix_and_parentheses_preserved():
    """부서 말머리와 괄호 표기는 의미가 있으므로 보존한다."""
    assert compose_title("[인사팀] 평가 결과 보고(안)_최종본.docx") == "[인사팀] 평가 결과 보고(안)"
    assert compose_title("조직개편(안).pptx") == "조직개편(안)"
    assert compose_title("[People팀] 채용 계획.docx") == "[People팀] 채용 계획"


def test_decorations_removed():
    assert compose_title("★★★취업규칙 개정★★★.docx") == "취업규칙 개정"


def test_filename_that_is_only_noise_falls_back_to_ai_title():
    assert compose_title("최종본.docx", ai_title="복리후생 안내") == "복리후생 안내"


def test_identifier_number_is_not_treated_as_date():
    """사번 같은 6자리 숫자를 날짜로 오인해 지워버리면 안 된다."""
    assert compose_title("사번 250728 인사기록.docx") == "사번 250728 인사기록"
    assert extract_date("사번 250728")[0] is None
    assert extract_date("문서번호 240115")[0] is None
    # 앞에 식별번호 단서가 없으면 그대로 날짜로 본다(기존 동작 유지)
    assert extract_date("보고서 250728 최종")[0] == date(2025, 7, 28)


def test_versions_of_same_doc_share_a_title():
    """잡음을 걷어내야 신·구 버전이 같은 제목으로 묶여 '이전 버전 후보'로 잡힌다."""
    from app.ingestion.titletools import strip_date_prefix
    a = compose_title("연차규정_v1.docx", ai_date="2024-01-01")
    b = compose_title("사본 - 연차규정_최종본.docx", ai_date="2024-06-01")
    assert a != b                                        # 날짜가 다르므로 제목은 다름
    assert strip_date_prefix(a) == strip_date_prefix(b)  # 날짜를 빼면 같은 문서


def test_to_iso_accepts_localized_and_common_formats():
    # 화면이 한국어 로케일로 렌더한 날짜를 그대로 저장해도 깨지지 않아야 한다
    assert to_iso("2026년 6월 30일") == "2026-06-30"
    assert to_iso("2026-06-30") == "2026-06-30"
    assert to_iso("2026.06.30") == "2026-06-30"
    assert to_iso("20260630") == "2026-06-30"
    assert to_iso(date(2026, 6, 30)) == "2026-06-30"
    assert to_iso("") is None and to_iso(None) is None
    assert to_iso("아무 날짜 아님") is None
