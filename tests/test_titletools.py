"""제목 구성/날짜 정규화 유닛 테스트."""

from datetime import date

from app.ingestion.titletools import compose_title, extract_date


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


def test_length_bounded():
    long = "아주" * 60
    assert len(compose_title(long + ".txt")) <= 60
