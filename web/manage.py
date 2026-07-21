#!/usr/bin/env python
"""Django 관리 유틸리티. repo 루트를 sys.path에 추가해 app.* 임포트를 보장한다."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo 루트

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web.hrrag.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
