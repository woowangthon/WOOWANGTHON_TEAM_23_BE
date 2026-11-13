import difflib
import re
import zipfile
from io import BytesIO

from pptx import Presentation
from pypdf import PdfReader

CHUNK_CHARS = 2000
# PPTX는 zip이라 압축을 풀면 크기가 크게 늘 수 있음. 여는 것만으로 메모리가 넘치지 않게 막음
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024


class UnsupportedFile(Exception):
    pass


class TooManyPages(Exception):
    pass


def kind_of(name: str, content_type: str | None) -> str:
    lower = name.lower()
    if lower.endswith(".pdf") or content_type == "application/pdf":
        return "pdf"
    if lower.endswith(".pptx") or content_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        return "pptx"
    raise UnsupportedFile(name)


def extract_pages(kind: str, data: bytes, max_pages: int) -> list[str]:
    """쪽(슬라이드)마다 글자를 뽑음. 번호는 목록 순서 + 1. 글자를 뽑기 전에 쪽수와 압축 해제 크기를 먼저 확인함"""
    try:
        if kind == "pdf":
            pages = PdfReader(BytesIO(data)).pages
            if len(pages) > max_pages:
                raise TooManyPages(len(pages))
            return [(page.extract_text() or "").strip() for page in pages]
        with zipfile.ZipFile(BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED_BYTES:
                raise UnsupportedFile("압축 해제 크기 초과")
        prs = Presentation(BytesIO(data))
        if len(prs.slides) > max_pages:
            raise TooManyPages(len(prs.slides))
        slides = []
        for slide in prs.slides:
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    texts.append(shape.text_frame.text)
                elif shape.has_table:
                    texts += [cell.text for row in shape.table.rows for cell in row.cells]
            slides.append("\n".join(t.strip() for t in texts if t.strip()))
        return slides
    except (TooManyPages, UnsupportedFile):
        raise
    except Exception as e:
        raise UnsupportedFile(str(e)) from e


def split_chunks(pages: list[str]) -> list[str]:
    text = "\n".join(p for p in pages if p)
    pieces = [text[i : i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
    # 마지막 조각이 너무 짧으면 문제를 만들기 어려워 앞 조각에 붙임
    if len(pieces) > 1 and len(pieces[-1]) < CHUNK_CHARS // 4:
        last = pieces.pop()
        pieces[-1] += last
    return pieces


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def find_page(evidence: str, pages: list[str]) -> int | None:
    """근거 문장이 있는 쪽 번호. 추출 잡음으로 완전히 같지 않을 수 있어 80% 이상 겹치면 인정함"""
    e = squash(evidence)
    if not e:
        return None
    squashed = [squash(p) for p in pages]
    for n, p in enumerate(squashed, 1):
        if e in p:
            return n
    best, best_ratio = None, 0.8
    for n, p in enumerate(squashed, 1):
        blocks = difflib.SequenceMatcher(None, p, e, autojunk=False).get_matching_blocks()
        ratio = sum(b.size for b in blocks) / len(e)
        if ratio >= best_ratio:
            best, best_ratio = n, ratio
    return best
