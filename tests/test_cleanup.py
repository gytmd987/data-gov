"""폴더 정리 추천 — 그룹 묶기 / 정기 문서 구분 / 권한 범위."""

from app.manage.cleanup import CROWDED_GROUP, build_groups


def _doc(doc_id, title, eff=None, filename=None):
    return {"doc_id": doc_id, "title": title,
            "filename": filename or f"{title}.docx",
            "owner": "a@x.com", "effective_date": eff}


def test_links_two_similar_docs_into_one_group():
    docs = [_doc("d1", "연차규정 v2", "2025-03-01"),
            _doc("d2", "연차규정", "2024-01-01")]
    cands = {"d1": [{"doc_id": "d2", "score": 0.93, "ai_relation": "revision"}]}

    groups = build_groups(docs, cands)

    assert len(groups) == 1
    assert [d["doc_id"] for d in groups[0].docs] == ["d1", "d2"]   # 최신본이 앞
    assert groups[0].suggested_keep == "d1"
    assert "개정판" in groups[0].reason


def test_transitive_links_form_a_single_group():
    """A↔B, B↔C 면 A·B·C 가 한 묶음 — 따로 뜨면 사람이 두 번 정리하게 된다."""
    docs = [_doc(x, f"규정 {x}") for x in ("a", "b", "c")]
    cands = {"a": [{"doc_id": "b", "score": 0.91}],
             "b": [{"doc_id": "c", "score": 0.90}]}

    groups = build_groups(docs, cands)

    assert len(groups) == 1
    assert {d["doc_id"] for d in groups[0].docs} == {"a", "b", "c"}


def test_below_threshold_is_not_grouped():
    docs = [_doc("d1", "연차규정"), _doc("d2", "출장규정")]
    cands = {"d1": [{"doc_id": "d2", "score": 0.62}]}

    assert build_groups(docs, cands) == []


def test_ignores_candidates_outside_the_given_list():
    """호출자가 권한·폴더로 걸러 넘긴 목록 밖 문서는 절대 묶이지 않는다."""
    docs = [_doc("mine", "연차규정")]
    cands = {"mine": [{"doc_id": "secret", "score": 0.99}]}

    assert build_groups(docs, cands) == []


def test_periodic_docs_are_flagged_and_pushed_down():
    """날짜만 다른 월간 보고서는 개정판이 아니다 — 경고를 달고 아래로 내린다."""
    periodic = [_doc("m1", "(25-0131) 월간보고", "2025-01-31"),
                _doc("m2", "(25-0228) 월간보고", "2025-02-28")]
    revision = [_doc("r1", "연차규정 개정", "2025-03-01"),
                _doc("r2", "연차규정", "2024-01-01")]
    docs = periodic + revision
    cands = {"m1": [{"doc_id": "m2", "score": 0.97}],
             "r1": [{"doc_id": "r2", "score": 0.95, "ai_relation": "revision"}]}

    groups = build_groups(docs, cands)

    assert len(groups) == 2
    assert not groups[0].periodic          # 개정판이 위
    assert groups[1].periodic
    assert "정기 문서" in groups[1].reason


def test_periodic_detection_handles_dates_left_inside_the_title():
    """제목 정규화를 안 거친 문서(일괄 반입 등)도 정기 문서로 잡혀야 한다."""
    docs = [_doc("m1", "월간보고_20250131", "2025-01-31"),
            _doc("m2", "월간보고_20250228", "2025-02-28")]
    cands = {"m1": [{"doc_id": "m2", "score": 0.96}]}

    assert build_groups(docs, cands)[0].periodic


def test_same_title_without_dates_is_not_periodic():
    """날짜가 없으면 정기 문서가 아니다 — 그냥 같은 문서의 복사본일 수 있다."""
    docs = [_doc("d1", "연차규정"), _doc("d2", "연차규정")]
    cands = {"d1": [{"doc_id": "d2", "score": 0.99}]}

    assert not build_groups(docs, cands)[0].periodic


def test_crowded_group_gets_a_warning():
    n = CROWDED_GROUP + 1
    docs = [_doc(f"d{i}", f"양식 {i}") for i in range(n)]
    cands = {"d0": [{"doc_id": f"d{i}", "score": 0.9} for i in range(1, n)]}

    group = build_groups(docs, cands)[0]

    assert group.size == n
    assert "한 묶음" in group.reason


def test_newest_effective_date_is_suggested_regardless_of_input_order():
    docs = [_doc("old", "연차규정", "2023-01-01"),
            _doc("new", "연차규정 최종", "2025-06-01")]
    cands = {"old": [{"doc_id": "new", "score": 0.94}]}

    assert build_groups(docs, cands)[0].suggested_keep == "new"


def test_missing_effective_date_falls_back_to_list_order():
    """작성일이 없으면 목록 순서(최근 수정순)를 따른다 — 화면과 어긋나지 않게."""
    docs = [_doc("recent", "연차규정"), _doc("stale", "연차규정 사본")]
    cands = {"recent": [{"doc_id": "stale", "score": 0.94}]}

    assert build_groups(docs, cands)[0].suggested_keep == "recent"


def test_scores_are_reported_for_the_whole_group():
    docs = [_doc(x, f"규정 {x}") for x in ("a", "b", "c")]
    cands = {"a": [{"doc_id": "b", "score": 0.91}],
             "b": [{"doc_id": "c", "score": 0.97}]}

    group = build_groups(docs, cands)[0]

    assert group.min_score == 0.91
    assert group.max_score == 0.97
