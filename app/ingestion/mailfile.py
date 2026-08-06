"""메일 파일 다루기 — 사내 형식(.mysingle) 변환 · Message-ID · 첨부 분리.

세 가지를 여기서 처리한다.

1. **변환** — 그룹웨어에서 받은 `.mysingle` 은 확장자만 다를 뿐 대부분 표준 메일(RFC 822)
   이다. 표준이면 그대로 `.eml` 로 넘기고, 아니면(압축·HTML·평문) 내용을 알아보고
   표준 메일로 감싸서 `.eml` 을 만든다. 이후 단계는 전부 보통 메일로 취급한다.

2. **Message-ID** — 같은 메일을 A 사서함과 B 사서함에서 각자 저장하면 헤더(Received 등)가
   달라 **파일 해시가 어긋난다**. 그러면 같은 메일이 두 건으로 등록된다. 메일에는 발신
   시점에 정해지는 고유값 Message-ID 가 있으므로, 메일은 이 값으로 같고 다름을 본다.

3. **첨부 분리** — 첨부는 본문과 함께 base64 로 들어 있어 용량은 다 차지하면서 검색에는
   전혀 안 잡혔다. 첨부를 꺼내 **별도 문서로 등록**하고, 보관하는 메일 원본에서는 자리
   표시만 남긴다. 같은 첨부가 스레드마다 따라와도 내용 해시가 같아 한 번만 저장된다.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from email import message_from_binary_file, message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path
from typing import Iterable, Optional

from app.ingestion.htmltext import looks_like_html

# 메일로 취급하는 확장자. .mysingle 은 사내 그룹웨어가 내려주는 이름일 뿐 내용은 메일이다.
MAIL_SUFFIXES = (".eml", ".mysingle")

# 헤더 줄 판정: "Name: value" (헤더 이름에는 공백·콜론이 못 온다)
_HEADER_LINE = re.compile(rb"^[A-Za-z][A-Za-z0-9\-_]{1,40}:")
# 이 중 하나라도 있어야 메일로 인정(아무 텍스트나 헤더로 오인하지 않도록)
_MAIL_HEADERS = {b"from", b"to", b"cc", b"subject", b"date", b"message-id",
                 b"received", b"mime-version", b"return-path", b"sender"}

_SNIFF = 8192          # 앞부분만 보고 형식을 판정
_ATTACH_PLACEHOLDER = "[첨부 분리 보관: {name} — 별도 문서로 등록되어 검색됩니다]"


class MailConvertError(ValueError):
    """메일로 해석할 수 없는 파일."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes
    content_type: str

    @property
    def suffix(self) -> str:
        return Path(self.filename).suffix.lower()


# ── 1. 변환 ──────────────────────────────────────────────────────────────────
def is_mail_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in MAIL_SUFFIXES


def looks_like_rfc822(data: bytes) -> bool:
    """앞부분이 메일 헤더 블록인가."""
    head = data[:_SNIFF].lstrip()
    if not head:
        return False
    seen = False
    for raw in head.split(b"\n")[:60]:
        line = raw.rstrip(b"\r")
        if not line:                       # 헤더 블록 끝
            break
        if line[:1] in (b" ", b"\t"):      # 접힌 헤더(이어지는 줄)
            continue
        if not _HEADER_LINE.match(line):
            return False                   # 헤더가 아닌 줄이 섞이면 메일이 아니다
        if line.split(b":", 1)[0].lower() in _MAIL_HEADERS:
            seen = True
    return seen


def _wrap_as_mail(body: str, *, subject: str, html: bool) -> bytes:
    """메일이 아닌 내용(HTML·평문)을 최소한의 표준 메일로 감싼다."""
    msg = EmailMessage()
    msg["Subject"] = subject or "(제목 없음)"
    if html:
        msg.set_content("본문은 HTML 형식입니다.")
        msg.add_alternative(body, subtype="html")
    else:
        msg.set_content(body)
    return msg.as_bytes()


def _from_zip(path: Path) -> Optional[bytes]:
    """압축 안에 들어 있는 메일 본체를 찾는다(그룹웨어가 묶어 주는 경우)."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            # 확장자가 메일인 것 먼저, 없으면 내용으로 판정
            ordered = sorted(names, key=lambda n: 0 if is_mail_file(n) else 1)
            for name in ordered:
                data = zf.read(name)
                if looks_like_rfc822(data):
                    return data
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    return None


def _decode_text(data: bytes) -> str:
    """사내 메일은 utf-8 아니면 cp949 다."""
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def to_eml(src: str | Path, out_dir: str | Path) -> Path:
    """어떤 메일 파일이든 표준 `.eml` 로 만들어 경로를 돌려준다.

    이미 표준 메일이면 확장자만 `.eml` 로 맞춰 복사한다(내용은 손대지 않는다).
    """
    src = Path(src)
    out = Path(out_dir) / f"{src.stem}.eml"
    out.parent.mkdir(parents=True, exist_ok=True)

    data = src.read_bytes()
    if looks_like_rfc822(data):
        out.write_bytes(data)
        return out

    inner = _from_zip(src)
    if inner is not None:
        out.write_bytes(inner)
        return out

    text = _decode_text(data).strip()
    if not text:
        raise MailConvertError(f"메일로 읽을 수 없습니다(내용 없음): {src.name}")
    out.write_bytes(_wrap_as_mail(text, subject=src.stem,
                                  html=looks_like_html(text)))
    return out


# ── 2. Message-ID ────────────────────────────────────────────────────────────
def normalize_message_id(value: Optional[str]) -> Optional[str]:
    """`<abc@host>` → `abc@host`. 비어 있으면 None."""
    mid = (value or "").strip()
    if mid.startswith("<") and mid.endswith(">"):
        mid = mid[1:-1]
    mid = mid.strip().lower()
    return mid[:512] or None


def read_message(path: str | Path) -> EmailMessage:
    with open(path, "rb") as f:
        return message_from_binary_file(f, policy=default_policy)


def message_id_of(path: str | Path) -> Optional[str]:
    """메일 파일의 Message-ID. 메일이 아니거나 없으면 None."""
    try:
        return normalize_message_id(str(read_message(path)["Message-ID"] or ""))
    except Exception:                       # noqa: BLE001 — 판정 실패는 '없음'으로
        return None


# ── 2-1. 스레드(답장·전달 관계) ──────────────────────────────────────────────
@dataclass(frozen=True)
class ThreadInfo:
    """이 메일이 스레드 어디에 있는지.

    - message_id : 이 메일
    - in_reply_to: 바로 위 메일(무엇에 대한 답장인가)
    - root       : 스레드의 첫 메일. References 의 맨 앞이며, 없으면 자기 자신이다.
    """

    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    root: Optional[str] = None


def thread_of(msg: EmailMessage) -> ThreadInfo:
    """헤더에서 스레드 정보를 읽는다.

    답장·전달 관계는 **인용문이 아니라 헤더에 있다**(In-Reply-To / References).
    그래서 본문의 인용문을 걷어내도 관계는 그대로 남는다.
    """
    mid = normalize_message_id(str(msg["Message-ID"] or ""))
    irt = normalize_message_id(str(msg["In-Reply-To"] or ""))
    refs = [normalize_message_id(r) for r in
            re.findall(r"<[^>]+>", str(msg["References"] or ""))]
    refs = [r for r in refs if r]
    # 스레드 뿌리 = References 의 맨 앞. 그게 없으면 답장 대상, 그것도 없으면 자기 자신.
    return ThreadInfo(message_id=mid, in_reply_to=irt,
                      root=(refs[0] if refs else irt) or mid)


def thread_of_file(path: str | Path) -> ThreadInfo:
    try:
        return thread_of(read_message(path))
    except Exception:                       # noqa: BLE001 — 메일이 아니면 빈 정보
        return ThreadInfo()


# ── 3. 첨부 분리 ─────────────────────────────────────────────────────────────
def _attachment_name(part, index: int) -> str:
    name = (part.get_filename() or "").strip()
    if not name:
        ext = {"application/pdf": ".pdf", "text/plain": ".txt"}.get(
            part.get_content_type(), "")
        name = f"첨부{index}{ext}"
    return Path(name).name                  # 경로 성분 제거(디렉터리 탈출 방지)


def _is_attachment(part) -> bool:
    """본문이 아니라 '첨부'인가.

    서명에 붙은 로고 같은 **인라인 이미지는 제외**한다. 본문의 일부로 보이는 그림이라
    별도 문서로 만들 이유가 없고, 메일마다 딸려 와 문서 목록만 어지럽힌다.
    """
    if part.get_content_maintype() == "multipart":
        return False
    disposition = str(part.get("Content-Disposition") or "").lower()
    if "attachment" in disposition:
        return True
    if not part.get_filename():
        return False
    # filename 은 있는데 inline 인 경우 — 이미지면 본문 그림으로 본다
    return not (part.get("Content-ID") or part.get_content_maintype() == "image")


def attachments_of(msg: EmailMessage) -> list[Attachment]:
    out: list[Attachment] = []
    for i, part in enumerate(msg.walk(), start=1):
        if not _is_attachment(part):
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        out.append(Attachment(filename=_attachment_name(part, i), data=data,
                              content_type=part.get_content_type()))
    return out


def strip_attachments(msg: EmailMessage, names: Iterable[str]) -> bytes:
    """지정한 첨부를 자리 표시 문구로 바꾼 메일 바이트를 돌려준다.

    **따로 보관된 첨부만** 지운다(names). 등록하지 못한 첨부는 메일 안에 그대로 둬서
    원본이 사라지는 일이 없게 한다.
    """
    targets = set(names)
    if not targets:
        return msg.as_bytes()
    for i, part in enumerate(msg.walk(), start=1):
        if not _is_attachment(part):
            continue
        name = _attachment_name(part, i)
        if name not in targets:
            continue
        part.clear_content()
        part.set_content(_ATTACH_PLACEHOLDER.format(name=name))
    return msg.as_bytes()


def split_attachments(path: str | Path) -> tuple[EmailMessage, list[Attachment]]:
    """메일을 읽어 (메시지, 첨부 목록) 으로 나눈다."""
    msg = read_message(path)
    return msg, attachments_of(msg)


def rebuild_without(path: str | Path, names: Iterable[str]) -> bytes:
    """파일을 다시 읽어 지정 첨부만 덜어낸 메일 바이트를 만든다."""
    return strip_attachments(read_message(path), names)


def parse_bytes(data: bytes) -> EmailMessage:
    return message_from_bytes(data, policy=default_policy)
