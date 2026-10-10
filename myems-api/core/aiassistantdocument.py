import io
import os
import uuid
import zipfile

from docx import Document
from pypdf import PdfReader

import config


class AttachmentValidationError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _safe_filename(filename):
    return os.path.basename(str(filename or '')).strip()


def _validate_file_size(raw_blob):
    if not raw_blob:
        raise AttachmentValidationError('API.EMPTY_AI_ATTACHMENT')
    if len(raw_blob) > config.ai_assistant['max_attachment_bytes']:
        raise AttachmentValidationError('API.AI_ATTACHMENT_TOO_LARGE')


def _extract_pdf(raw_blob):
    try:
        reader = PdfReader(io.BytesIO(raw_blob))
        if len(reader.pages) > config.ai_assistant['max_attachment_pages']:
            raise AttachmentValidationError('API.AI_ATTACHMENT_TOO_MANY_PAGES')
        text = '\n'.join(page.extract_text() or '' for page in reader.pages).strip()
    except AttachmentValidationError:
        raise
    except Exception:
        raise AttachmentValidationError('API.INVALID_AI_ATTACHMENT')
    return text


def _extract_docx(raw_blob):
    try:
        with zipfile.ZipFile(io.BytesIO(raw_blob)) as document_zip:
            names = set(document_zip.namelist())
            if '[Content_Types].xml' not in names or 'word/document.xml' not in names:
                raise AttachmentValidationError('API.INVALID_AI_ATTACHMENT')
            if sum(item.file_size for item in document_zip.infolist()) > \
                    config.ai_assistant['max_attachment_archive_bytes']:
                raise AttachmentValidationError('API.AI_ATTACHMENT_ARCHIVE_TOO_LARGE')
        document = Document(io.BytesIO(raw_blob))
        return '\n'.join(paragraph.text for paragraph in document.paragraphs).strip()
    except AttachmentValidationError:
        raise
    except Exception:
        raise AttachmentValidationError('API.INVALID_AI_ATTACHMENT')


def parse_attachment(raw_blob, filename):
    _validate_file_size(raw_blob)
    original_filename = _safe_filename(filename)
    extension = os.path.splitext(original_filename)[1].lower()

    if extension == '.pdf' and raw_blob.startswith(b'%PDF-'):
        mime_type = 'application/pdf'
        extracted_text = _extract_pdf(raw_blob)
    elif extension == '.docx' and raw_blob.startswith(b'PK\x03\x04'):
        mime_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        extracted_text = _extract_docx(raw_blob)
    else:
        raise AttachmentValidationError('API.UNSUPPORTED_AI_ATTACHMENT')

    if len(extracted_text) > config.ai_assistant['max_attachment_text_length']:
        raise AttachmentValidationError('API.AI_ATTACHMENT_TEXT_TOO_LONG')

    return {
        'original_filename': original_filename,
        'mime_type': mime_type,
        'extracted_text': extracted_text,
    }


def save_attachment(raw_blob):
    storage_filename = str(uuid.uuid4())
    attachment_path = config.ai_assistant['attachment_path']
    os.makedirs(attachment_path, mode=0o700, exist_ok=True)
    destination = os.path.join(attachment_path, storage_filename)
    temporary_destination = destination + '.tmp'
    try:
        with open(temporary_destination, 'xb') as attachment_file:
            attachment_file.write(raw_blob)
        os.replace(temporary_destination, destination)
    except OSError:
        try:
            os.remove(temporary_destination)
        except OSError:
            pass
        raise
    return storage_filename


def attachment_path(storage_filename):
    return os.path.join(config.ai_assistant['attachment_path'], storage_filename)


def remove_attachment(storage_filename):
    try:
        os.remove(attachment_path(storage_filename))
    except FileNotFoundError:
        pass