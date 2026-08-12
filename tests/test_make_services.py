"""systemd 유닛 생성 — 경로가 서버마다 달라서, 지금 환경을 읽어 만들어야 한다."""

from pathlib import Path

from scripts.make_services import main, units


def _units(tmp_path, python=None, user="hrrag"):
    return units(workdir=tmp_path, python=python or Path("/opt/app/.venv/bin/python"),
                 user=user, port=8500, workers=4, threads=8)


def test_makes_both_units_web_and_worker(tmp_path):
    """워커가 빠지면 예약 업로드가 영원히 '대기 중'이 된다 — 둘 다 있어야 한다."""
    made = _units(tmp_path)
    assert set(made) == {"hr-web.service", "hr-ingest-worker.service"}


def test_web_unit_uses_threads_so_streaming_does_not_block_others(tmp_path):
    web = _units(tmp_path)["hr-web.service"]
    assert "--worker-class gthread" in web and "--threads 8" in web
    assert "web.hrrag.wsgi:application" in web


def test_worker_unit_runs_the_ingest_worker(tmp_path):
    worker = _units(tmp_path)["hr-ingest-worker.service"]
    assert "-m scripts.ingest_worker" in worker


def test_units_use_the_given_paths_and_account(tmp_path):
    made = _units(tmp_path, user="hrrag")
    for body in made.values():
        assert f"WorkingDirectory={tmp_path}" in body
        assert "User=hrrag" in body
        assert "Restart=always" in body          # 죽어도 다시 뜬다


def test_prefers_the_gunicorn_next_to_the_python(tmp_path):
    """가상환경 안의 gunicorn 을 써야 한다 — 시스템 것을 쓰면 패키지가 안 맞는다."""
    venv = tmp_path / "bin"
    venv.mkdir()
    (venv / "python").write_text("")
    (venv / "gunicorn").write_text("")

    web = _units(tmp_path, python=venv / "python")["hr-web.service"]

    assert f"ExecStart={venv / 'gunicorn'} web.hrrag.wsgi" in web


def test_falls_back_to_module_form_when_gunicorn_is_missing(tmp_path):
    venv = tmp_path / "bin"
    venv.mkdir()
    (venv / "python").write_text("")

    web = _units(tmp_path, python=venv / "python")["hr-web.service"]

    assert f"{venv / 'python'} -m gunicorn" in web


# ── 화면 출력 ────────────────────────────────────────────────────────────────
def test_dry_run_prints_the_units_and_the_next_commands(tmp_path, capsys):
    """--write 없이는 아무것도 안 쓰고 보여주기만 한다(실수로 덮어쓰지 않게)."""
    assert main(["--workdir", str(tmp_path), "--user", "hrrag"]) == 0

    out = capsys.readouterr().out
    assert "hr-web.service" in out and "hr-ingest-worker.service" in out
    assert "systemctl daemon-reload" in out
    assert "systemctl enable --now hr-web hr-ingest-worker" in out


def test_warns_when_the_folder_is_not_the_project(tmp_path, capsys):
    main(["--workdir", str(tmp_path)])
    assert "프로젝트 폴더가 아닌 것 같습니다" in capsys.readouterr().out


def test_no_folder_warning_inside_the_real_project(tmp_path, capsys):
    (tmp_path / "web" / "hrrag").mkdir(parents=True)
    (tmp_path / "web" / "hrrag" / "wsgi.py").write_text("")

    main(["--workdir", str(tmp_path)])

    assert "프로젝트 폴더가 아닌 것 같습니다" not in capsys.readouterr().out
