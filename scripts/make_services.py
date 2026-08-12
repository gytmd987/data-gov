"""systemd 유닛 파일을 만들어 준다 — 웹 + 예약 처리 워커.

    python -m scripts.make_services            # 화면에 보여주기만(안전)
    python -m scripts.make_services --write    # /etc/systemd/system 에 쓰기(sudo 필요)

이 시스템은 **항상 떠 있어야 하는 프로그램이 둘**이다.
  - 웹  (hr-web)            : 사람이 보는 화면·채팅
  - 워커(hr-ingest-worker)  : 예약 업로드 처리. 없으면 예약이 영원히 '대기 중'

`systemctl enable` 은 **유닛 파일이 이미 있어야** 동작한다("Unit ... does not exist").
그래서 파일을 먼저 만들어야 하는데, 경로(프로젝트 위치·가상환경·실행 계정)는 서버마다
달라서 문서에 적힌 예시를 그대로 쓰면 안 맞는다. 여기서는 **지금 이 환경을 그대로
읽어** 유닛 파일을 만든다.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

UNIT_DIR = Path("/etc/systemd/system")

_TEMPLATE = """[Unit]
Description={description}
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={workdir}
Environment=PYTHONUNBUFFERED=1
ExecStart={exec_start}
Restart=always
RestartSec={restart_sec}

[Install]
WantedBy=multi-user.target
"""


def _gunicorn(python: Path) -> str:
    """같은 가상환경의 gunicorn 을 쓴다(없으면 python -m 으로 우회)."""
    candidate = python.parent / "gunicorn"
    return str(candidate) if candidate.exists() else f"{python} -m gunicorn"


def units(workdir: Path, python: Path, user: str, port: int,
          workers: int, threads: int) -> dict[str, str]:
    web = (f"{_gunicorn(python)} web.hrrag.wsgi:application "
           f"--bind 0.0.0.0:{port} --worker-class gthread "
           f"--workers {workers} --threads {threads} --timeout 300")
    return {
        "hr-web.service": _TEMPLATE.format(
            description="HR RAG 웹", user=user, workdir=workdir,
            exec_start=web, restart_sec=5),
        "hr-ingest-worker.service": _TEMPLATE.format(
            description="HR RAG 예약 업로드 처리 워커", user=user, workdir=workdir,
            exec_start=f"{python} -m scripts.ingest_worker", restart_sec=10),
    }


def _guess_python() -> Path:
    return Path(sys.executable).resolve()


def _warn_if_odd(workdir: Path, python: Path) -> list[str]:
    """그대로 쓰면 안 될 것 같은 상황을 미리 알려 준다."""
    notes = []
    if not (workdir / "web" / "hrrag" / "wsgi.py").exists():
        notes.append(f"⚠️  {workdir} 가 프로젝트 폴더가 아닌 것 같습니다"
                     "(web/hrrag/wsgi.py 가 없습니다). --workdir 로 지정하세요.")
    if _gunicorn(python).endswith("-m gunicorn"):
        notes.append("⚠️  gunicorn 이 설치돼 있지 않은 것 같습니다: "
                     f"{python} -m pip install gunicorn")
    if getpass.getuser() == "root":
        notes.append("⚠️  지금 root 로 실행 중입니다. --user 로 서비스 계정을 "
                     "지정하는 편이 안전합니다(예: --user hrrag).")
    return notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="systemd 유닛 파일 생성")
    ap.add_argument("--workdir", type=Path, default=Path.cwd(),
                    help="프로젝트 폴더(기본: 지금 폴더)")
    ap.add_argument("--python", type=Path, default=_guess_python(),
                    help="쓸 파이썬(기본: 지금 실행 중인 것)")
    ap.add_argument("--user", default=getpass.getuser(), help="서비스 실행 계정")
    ap.add_argument("--port", type=int, default=8500)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--threads", type=int, default=8,
                    help="스트리밍 답변이 연결을 오래 붙잡으므로 스레드가 필요하다")
    ap.add_argument("--write", action="store_true",
                    help=f"{UNIT_DIR} 에 실제로 쓴다(sudo 필요)")
    args = ap.parse_args(argv)

    workdir = args.workdir.resolve()
    files = units(workdir, args.python.resolve(), args.user,
                  args.port, args.workers, args.threads)

    for note in _warn_if_odd(workdir, args.python.resolve()):
        print(note)
    print()

    if not args.write:
        for name, body in files.items():
            print(f"── {UNIT_DIR / name} " + "─" * 30)
            print(body)
        print("실제로 만들려면(관리자 권한 필요):")
        print(f"  sudo {args.python} -m scripts.make_services --write "
              f"--workdir {workdir} --user {args.user}")
        print("\n만든 뒤:")
        print("  sudo systemctl daemon-reload")
        print("  sudo systemctl enable --now hr-web hr-ingest-worker")
        print("  sudo systemctl status hr-web hr-ingest-worker")
        return 0

    if os.geteuid() != 0:
        print(f"❌ {UNIT_DIR} 에 쓰려면 관리자 권한이 필요합니다. sudo 를 붙여 주세요.")
        return 1
    for name, body in files.items():
        path = UNIT_DIR / name
        path.write_text(body, encoding="utf-8")
        print(f"  OK: {path}")
    print("\n이제 다음을 실행하세요:")
    print("  sudo systemctl daemon-reload")
    print("  sudo systemctl enable --now hr-web hr-ingest-worker")
    print("  sudo systemctl status hr-web hr-ingest-worker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
