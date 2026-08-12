"""채팅 응답이 어디서 느린지 **단계별로** 재 본다.

    python -m scripts.bench_chat "연차는 며칠인가요?" --user hong@company.com

"느리다"만으로는 고칠 수 없다. 한 번의 질문을 다음으로 쪼개 실제 초를 잰다.

    임베딩 → Qdrant 검색 → 리랭킹 → (판단 호출) → 답변 생성

특히 **첫 글자까지 걸린 시간(TTFT)** 과 **생성 토큰 수**를 본다. 사용자가 체감하는
지연은 총 시간보다 TTFT 에 가깝고, 총 시간은 대체로 생성 토큰 수에 비례한다.
추론(<think>)이 켜져 있으면 눈에 안 보이는 토큰을 수백~수천 개 만들면서 TTFT 만
길어지므로, 여기서 그게 드러난다.
"""

from __future__ import annotations

import argparse
import sys
import time
from contextlib import contextmanager

from app.config import settings


class Timer:
    def __init__(self) -> None:
        self.marks: list[tuple[str, float]] = []

    @contextmanager
    def step(self, name: str):
        began = time.monotonic()
        try:
            yield
        finally:
            self.marks.append((name, time.monotonic() - began))

    def report(self, total: float) -> None:
        print("\n── 단계별 소요 ─────────────────────────────────────────")
        for name, secs in self.marks:
            share = (secs / total * 100) if total else 0
            bar = "█" * max(1, round(share / 4))
            print(f"  {name:<22} {secs:6.2f}초  {share:5.1f}%  {bar}")
        print(f"  {'합계':<22} {total:6.2f}초")


def _components(session):
    from web import bridge
    return bridge.get_search_pipeline(session)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="채팅 응답 속도 진단")
    ap.add_argument("question", nargs="?", default="연차는 며칠인가요?")
    ap.add_argument("--user", required=True, help="권한 기준이 될 사용자 이메일")
    ap.add_argument("--plan", action="store_true", help="판단 루프도 포함해서 잰다")
    ap.add_argument("--repeat", type=int, default=1, help="여러 번 재서 평균")
    args = ap.parse_args(argv)

    from app.db.repositories import UserRepository
    from app.review.factory import new_session
    from app.search.access import AccessPolicy
    from app.search.rerank import rerank_chunks

    session = new_session()
    try:
        user = UserRepository(session).get_user_context(args.user)
        if user is None:
            print(f"❌ '{args.user}' 사용자를 찾을 수 없습니다.")
            return 1
        pipe = _components(session)

        print(f"질문   : {args.question}")
        print(f"모델   : {settings.vllm_model}")
        print(f"설정   : max_tokens={settings.vllm_max_tokens} · "
              f"추론={settings.vllm_thinking} · 판단루프={'켬' if args.plan else '끔'}")

        for run in range(1, args.repeat + 1):
            if args.repeat > 1:
                print(f"\n===== {run}회차 =====")
            _one_run(pipe, session, user, args, rerank_chunks, AccessPolicy)
    finally:
        session.close()
    return 0


def _one_run(pipe, session, user, args, rerank_chunks, AccessPolicy) -> None:
    timer = Timer()
    began = time.monotonic()
    policy = AccessPolicy.for_user(user)

    with timer.step("검색(임베딩+Qdrant)"):
        candidates = pipe.retriever.retrieve(args.question, policy, top_n=pipe.top_n)
    sizes = sorted((len(c.text or "") for c in candidates), reverse=True)
    with timer.step(f"리랭킹({len(candidates)}개)"):
        reranked = rerank_chunks(pipe.reranker, args.question, candidates,
                                 top_k=pipe.top_k)
    reranked = [c for c in reranked if policy.allows(c.payload)]

    steps = 0
    if args.plan:
        with timer.step("판단 호출(도구 선택)"):
            from app.search.agent import run_agent
            from app.search.tools import ToolBox, ToolResult
            box = ToolBox(session=session, policy=policy,
                          visibility=_vis(user), retrieve=pipe._retrieve_ranked,
                          today=policy.today)
            seed = ToolResult(text="\n".join(
                f"- {c.title}: {(c.text or '')[:300]}" for c in reranked),
                chunks=list(reranked))
            walker = run_agent(pipe.llm, args.question, box, prefetch=seed)
            while True:
                try:
                    next(walker)
                    steps += 1
                except StopIteration:
                    break

    # 답변 생성 — 첫 글자까지(TTFT)와 전체를 따로 잰다
    from app.search.answer import build_answer_prompt
    prompt = build_answer_prompt(args.question, reranked)
    first_at, chars = None, 0
    gen_began = time.monotonic()
    for piece in pipe.llm.stream_text(prompt):
        if first_at is None:
            first_at = time.monotonic() - gen_began
        chars += len(piece)
    gen_took = time.monotonic() - gen_began
    timer.marks.append(("답변 첫 글자까지", first_at or gen_took))
    timer.marks.append(("답변 나머지", gen_took - (first_at or gen_took)))

    total = time.monotonic() - began
    timer.report(total)

    print(f"\n  근거 조각 {len(reranked)}개 · 프롬프트 {len(prompt):,}자 · "
          f"답변 {chars:,}자" + (f" · 도구 호출 {steps}회" if args.plan else ""))
    if sizes:
        print(f"  후보 청크 길이: 최대 {sizes[0]:,}자 · 중앙값 {sizes[len(sizes)//2]:,}자 "
              f"· 합계 {sum(sizes):,}자")
    _advise(first_at or 0.0, gen_took, chars, total, timer, sizes)


def _vis(user):
    from app.search.access import Visibility
    return Visibility(read_tokens=frozenset(user.groups))


def _advise(ttft: float, gen: float, chars: int, total: float,
            timer: Timer, sizes: list[int]) -> None:
    """숫자만 보면 뭘 고쳐야 할지 모른다 — 해석과 다음 조치를 붙인다."""
    print("\n── 해석 ────────────────────────────────────────────────")
    took = dict(timer.marks)
    rerank = next((v for k, v in took.items() if k.startswith("리랭킹")), 0.0)

    if rerank > 2:
        print(f"  ⚠️  리랭킹이 {rerank:.1f}초입니다. 후보 수 × 글자 수에 거의 비례합니다.")
        if sizes and sizes[0] > settings.rerank_max_chars * 2:
            print(f"      가장 긴 후보가 {sizes[0]:,}자입니다 — 표(엑셀·워드 표)는 헤더를"
                  " 지키려고 쪼개지 않아 청크 하나가 아주 커집니다.")
            print(f"      RERANK_MAX_CHARS(지금 {settings.rerank_max_chars})로 잘라 보내고,"
                  f" RERANK_TOP_N(지금 {settings.rerank_top_n})을 줄이면 그만큼 빨라집니다.")
        print("      그래도 느리면 **리랭커가 CPU 로 돌고 있을 수 있습니다** — 확인:")
        print("        docker compose ps · docker compose logs reranker | head -20")
        print("        (로그에 cuda/gpu 언급이 없으면 CPU 폴백입니다)")

    if ttft > 3:
        print(f"  ⚠️  첫 글자까지 {ttft:.1f}초. 이 동안 화면은 멈춰 보입니다.")
        print(f"      프롬프트가 길수록 여기가 길어집니다 — ANSWER_MAX_CHARS"
              f"(지금 {settings.answer_max_chars})로 근거를 잘라 보세요.")
        if settings.vllm_thinking != "off":
            print("      추론(<think>)이 켜져 있어도 길어집니다. VLLM_THINKING=off 로"
                  " 재 봐서 차이가 없으면 이 모델은 추론을 안 쓰는 것이니 되돌리세요.")

    speed = chars / gen if gen else 0
    print(f"  생성 속도 약 {speed:.0f}자/초 · 답변 {chars:,}자")
    if chars > 1200:
        print("  ⚠️  답변이 깁니다. VLLM_MAX_TOKENS 를 줄이면 그만큼 빨라집니다"
              f"(지금 {settings.vllm_max_tokens}).")
    if total < 5:
        print("  ✅ 총 5초 미만 — 무난합니다.")
    elif total < 12:
        print("  △ 총 5~12초 — 쓸 만하지만 더 줄일 수 있습니다.")
    else:
        print("  ❌ 총 12초 초과 — 위에서 제일 큰 항목부터 손보세요.")


if __name__ == "__main__":
    sys.exit(main())
