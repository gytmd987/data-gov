"""Django 설정 — 얇은 웹 계층.

도메인 데이터는 SQLAlchemy(app.db)가 담당하고, Django DB는 auth/세션 전용이다.
기본은 로컬 sqlite 파일(web/django.sqlite3); 운영에서 Postgres를 쓰려면
DJANGO_DB=postgres 로 지정하면 기존 .env의 Postgres에 django_* 테이블만 추가된다.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # web/
REPO_ROOT = BASE_DIR.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "web.chat",
    "web.console",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "web.hrrag.urls"
WSGI_APPLICATION = "web.hrrag.wsgi.application"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "web.authz.admin_context",
    ]},
}]

if os.environ.get("DJANGO_DB") == "postgres":
    from app.config import settings as _s
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _s.postgres_db, "USER": _s.postgres_user,
        "PASSWORD": _s.postgres_password, "HOST": "localhost",
        "PORT": _s.postgres_port,
    }}
else:
    DATABASES = {"default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "django.sqlite3",
    }}

LANGUAGE_CODE = "ko-kr"
TIME_ZONE = "Asia/Seoul"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

# ── 웹 업로드 개수 제한 ──────────────────────────────────────────────────────
# 업로드 경로는 두 가지이고 한도가 다르다.
#  · 즉시 처리 — 요청 안에서 파일당 파싱+AI 자동 채움을 순차로 돌린다(문서당 수십 초).
#    많이 올리면 요청이 몇십 분씩 걸려 브라우저·프록시에서 끊기므로 낮게 잡는다.
#  · 예약 처리 — 요청은 파일 저장만 하고(수 초) 야간 워커가 등록한다. 많이 받아도 된다.
# DATA_UPLOAD_MAX_NUMBER_FILES 는 Django 가 요청을 파싱할 때 보는 값이라 뷰보다 앞선다.
# 그래서 하드 한도는 예약 기준으로 두고, 즉시 처리 한도는 뷰에서 따로 막는다.
UPLOAD_WARN_FILES = 20            # 즉시 처리에서 이보다 많으면 화면에서 경고
UPLOAD_SYNC_MAX_FILES = 50        # 즉시 처리 한도(뷰에서 검사)
DATA_UPLOAD_MAX_NUMBER_FILES = 500   # 한 요청의 하드 한도(넘으면 Django 가 거부)

# 업로드 파일을 메모리에 들고 있는 상한. Django 기본은 2.5MB 라, 2.4MB 짜리 파일
# 여러 개가 한 요청에 오면 그만큼이 통째로 RAM 에 올라간다(500개면 1.2GB).
# 우리는 받자마자 디스크에 쓰므로 메모리에 들고 있을 이유가 없다 → 낮게 잡는다.
FILE_UPLOAD_MAX_MEMORY_SIZE = 512 * 1024

# 로그를 표준 출력으로 — systemd 로 띄우면 `journalctl -u hr-web` 에 그대로 남는다.
# 이게 없으면 업로드 실패 같은 예외가 어디에도 안 남아 관리자가 원인을 못 찾는다.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # 요청 처리 중 터진 예외는 항상 남긴다(DEBUG=0 이어도)
        "django.request": {"handlers": ["console"], "level": "ERROR",
                           "propagate": False},
    },
}

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
