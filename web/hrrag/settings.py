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
# 업로드는 요청 안에서 파일당 파싱+AI 자동 채움을 순차로 돌리므로(문서당 수십 초),
# 한 번에 많이 올리면 요청이 몇십 분씩 걸려 브라우저·프록시에서 끊긴다.
# 대량 반입은 웹이 아니라 `python -m scripts.bulk_ingest` 를 쓴다(docs/일괄반입.md).
UPLOAD_WARN_FILES = 20        # 이보다 많으면 화면에서 경고(계속은 가능)
DATA_UPLOAD_MAX_NUMBER_FILES = 50   # 하드 한도. 넘으면 Django 가 요청을 거부한다.

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
