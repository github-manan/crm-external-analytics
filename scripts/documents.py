#!/usr/bin/env python3
"""Upload-a-document support for the chatbot - lets a user attach a file
and ask questions about its contents, alongside (not instead of) the
normal CRM tools.

Supports .txt, .csv, .pdf, .xlsx - all via libraries already present on
this machine (pypdf, openpyxl) or the standard library, so this adds no
new dependency. .docx is NOT supported yet - python-docx isn't installed,
and adding a dependency for a format nobody has asked for yet isn't
justified; this is a disclosed gap (a clear error, not a silent failure),
not an oversight.

Different trust model from every other tool in this app: a CRM tool's
result is pre-tested and correct by construction - that's the whole
"controlled query layer" design. An uploaded document's content is
whatever the user attached - arbitrary, unverified text. The model reads
it directly rather than calling a verified function, so there is no
"headline" guarantee for anything it says about an uploaded file. This is
explicitly a weaker guarantee than every other answer in this app, and
chatbot.py's prompt says so to the model, and the UI should say so to the
user too.

Uploads are held in memory only, per uploader (never written to disk,
never shared between users - checked by username, not just by having the
upload_id). They vanish on logout or server restart - same simplification
already accepted for login sessions in auth.py, not a new risk.
"""

import io
import os
import secrets

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_CHARS = 12_000  # keeps the extracted text a sane size for the model's context window
SUPPORTED_EXTENSIONS = (".txt", ".csv", ".pdf", ".xlsx")

# upload_id -> {"username", "filename", "text", "truncated"}
UPLOADS = {}


def _extract_pdf(content_bytes):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content_bytes))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_xlsx(content_bytes):
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content_bytes), data_only=True, read_only=True)
    parts = []
    for sheet in wb.worksheets:
        parts.append(f"--- Sheet: {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            parts.append(", ".join("" if v is None else str(v) for v in row))
    return "\n".join(parts)


def extract_text(filename, content_bytes):
    """Returns (text, truncated). Raises ValueError for an unsupported extension."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".txt", ".csv"):
        text = content_bytes.decode("utf-8", errors="replace")
    elif ext == ".pdf":
        text = _extract_pdf(content_bytes)
    elif ext == ".xlsx":
        text = _extract_xlsx(content_bytes)
    else:
        raise ValueError(
            f"'{ext or 'unknown'}' files aren't supported yet - only "
            f"{', '.join(SUPPORTED_EXTENSIONS)}."
        )
    truncated = len(text) > MAX_CHARS
    return text[:MAX_CHARS], truncated


def store_upload(username, filename, content_bytes):
    """Extracts and stores one upload, scoped to username. Returns the
    public record (upload_id, filename, char_count, truncated)."""
    text, truncated = extract_text(filename, content_bytes)
    upload_id = secrets.token_hex(16)
    UPLOADS[upload_id] = {
        "username": username, "filename": filename, "text": text, "truncated": truncated,
    }
    return {
        "upload_id": upload_id, "filename": filename,
        "char_count": len(text), "truncated": truncated,
    }


def get_upload(upload_id, username):
    """Returns the upload record only if it belongs to this username -
    the same ownership check everywhere else in this app uses, applied to
    uploads too. Returns None if missing or owned by someone else."""
    record = UPLOADS.get(upload_id)
    if not record or record["username"] != username:
        return None
    return record
