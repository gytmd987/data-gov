"""한글 표시 라벨 필터 — config/system.yaml 의 labels 맵 사용.

폼 전송 값은 영문 enum 원값을 유지하고, 화면에 보이는 글자만 바꾼다.
"""

from django import template

from app import system_config

register = template.Library()


@register.filter
def klabel(value):
    if value is None or value == "":
        return ""
    return system_config.label(value)


@register.filter
def klabel_join(values, sep=", "):
    return sep.join(system_config.label(v) for v in (values or []))
