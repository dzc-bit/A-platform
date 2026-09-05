"""Knowledge base documents, upload and search routes (mechanically split from app/api.py)."""

from .shared import (  # noqa: F401
    APIRouter,
    Depends,
    ElementTree,
    File,
    Form,
    HTTPException,
    KnowledgeCreate,
    KnowledgeDocument,
    KnowledgeOut,
    KnowledgeReindexOut,
    MAX_CSV_COLUMNS,
    MAX_CSV_ROWS,
    MAX_DOCX_COMPRESSION_RATIO,
    MAX_DOCX_MEMBERS,
    MAX_DOCX_UNCOMPRESSED_BYTES,
    MAX_EXTRACTED_TEXT_CHARS,
    MAX_PDF_PAGES,
    MAX_UPLOAD_BYTES,
    PdfReadError,
    PdfReader,
    PurePosixPath,
    SUPPORTED_DOCUMENT_SUFFIXES,
    SearchRequest,
    SearchResponse,
    Session,
    UploadFile,
    User,
    csv,
    get_current_user,
    get_db,
    index_document,
    io,
    re,
    remove_document,
    require_roles,
    retrieve,
    select,
    status,
    zipfile,
)

router = APIRouter()

@router.get("/knowledge/documents", response_model=list[KnowledgeOut], tags=["knowledge"])
def list_documents(
    current_user: User = Depends(require_roles("admin", "support_agent")), db: Session = Depends(get_db)
) -> list[KnowledgeDocument]:
    del current_user
    return list(db.scalars(select(KnowledgeDocument).order_by(KnowledgeDocument.updated_at.desc())).all())


@router.post("/knowledge/documents", response_model=KnowledgeOut, status_code=status.HTTP_201_CREATED, tags=["knowledge"])
def create_document(
    payload: KnowledgeCreate,
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> KnowledgeDocument:
    del current_user
    document = KnowledgeDocument(title=payload.title.strip(), source=payload.source.strip(), content=payload.content.strip())
    db.add(document)
    db.flush()
    index_document(db, document)
    db.commit()
    db.refresh(document)
    return document


@router.put("/knowledge/documents/{document_id}", response_model=KnowledgeOut, tags=["knowledge"])
def update_document(
    document_id: int,
    payload: KnowledgeCreate,
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> KnowledgeDocument:
    del current_user
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识文档不存在")
    document.title = payload.title.strip()
    document.source = payload.source.strip()
    document.content = payload.content.strip()
    document.status = "indexing"
    db.flush()
    index_document(db, document)
    document.status = "ready"
    db.commit()
    db.refresh(document)
    return document


@router.delete(
    "/knowledge/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["knowledge"],
)
def delete_document(
    document_id: int,
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> None:
    del current_user
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识文档不存在")
    remove_document(db, document)
    db.commit()


@router.post(
    "/knowledge/documents/{document_id}/reindex",
    response_model=KnowledgeReindexOut,
    tags=["knowledge"],
)
def reindex_document(
    document_id: int,
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> KnowledgeReindexOut:
    del current_user
    document = db.get(KnowledgeDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识文档不存在")
    document.status = "indexing"
    db.flush()
    indexed_chunks = index_document(db, document)
    document.status = "ready"
    db.commit()
    db.refresh(document)
    return KnowledgeReindexOut(
        document=KnowledgeOut.model_validate(document),
        status=document.status,
        indexed_chunks=indexed_chunks,
    )


def _safe_uploaded_filename(filename: str | None) -> str:
    raw_name = (filename or "upload.txt").replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", raw_name).strip()
    return cleaned[:255] or "upload.txt"


def _decode_text_payload(payload: bytes) -> str:
    if b"\x00" in payload:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="文本文件不能包含空字节")
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail="文本或 CSV 文件必须使用 UTF-8 或 GB18030 编码",
    )


def _normalize_extracted_text(text: str) -> str:
    # Strip control characters before persisting text that may later be rendered in an admin view.
    normalized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", text)
    normalized = re.sub(r"[\t\r\n ]+", " ", normalized).strip()
    if len(normalized) > MAX_EXTRACTED_TEXT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"解析后的文档不能超过 {MAX_EXTRACTED_TEXT_CHARS} 个字符",
        )
    return normalized


def _extract_csv_text(payload: bytes) -> str:
    source = _decode_text_payload(payload)
    try:
        reader = csv.reader(io.StringIO(source), strict=True)
        rows: list[str] = []
        for row_index, row in enumerate(reader, start=1):
            if row_index > MAX_CSV_ROWS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"CSV 不能超过 {MAX_CSV_ROWS} 行",
                )
            if len(row) > MAX_CSV_COLUMNS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"CSV 每行不能超过 {MAX_CSV_COLUMNS} 列",
                )
            rows.append(" | ".join(cell.strip() for cell in row))
    except csv.Error as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法解析 CSV 文件") from error
    return "\n".join(rows)


def _validate_docx_archive(document_zip: zipfile.ZipFile) -> zipfile.ZipInfo:
    infos = document_zip.infolist()
    if len(infos) > MAX_DOCX_MEMBERS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 包含过多文件")

    total_uncompressed = 0
    document_xml_infos: list[zipfile.ZipInfo] = []
    for info in infos:
        archive_path = PurePosixPath(info.filename)
        if info.flag_bits & 0x1:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持加密的 DOCX 文件")
        if "\\" in info.filename or archive_path.is_absolute() or ".." in archive_path.parts:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 文件结构无效")
        if info.file_size < 0 or info.file_size > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 解压后的内容过大")
        if info.file_size and (not info.compress_size or info.file_size / info.compress_size > MAX_DOCX_COMPRESSION_RATIO):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 压缩比例异常")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 解压后的内容过大")
        if info.filename == "word/document.xml":
            document_xml_infos.append(info)

    if len(document_xml_infos) != 1:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX 缺少正文内容")
    return document_xml_infos[0]


def _extract_docx_text(payload: bytes) -> str:
    if not zipfile.is_zipfile(io.BytesIO(payload)):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法解析 DOCX 文件")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as document_zip:
            document_xml = document_zip.read(_validate_docx_archive(document_zip))
        if b"<!DOCTYPE" in document_xml.upper() or b"<!ENTITY" in document_xml.upper():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="DOCX XML 不允许包含实体声明")
        root = ElementTree.fromstring(document_xml)
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法解析 DOCX 文件") from error
    text_nodes = [node.text or "" for node in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")]
    return " ".join(text_nodes)


def _extract_pdf_text(payload: bytes) -> str:
    if not payload.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="PDF 文件头无效")
    try:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="不支持加密的 PDF 文件")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"PDF 不能超过 {MAX_PDF_PAGES} 页",
            )
        text_parts: list[str] = []
        text_size = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            text_size += len(page_text)
            if text_size > MAX_EXTRACTED_TEXT_CHARS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"解析后的文档不能超过 {MAX_EXTRACTED_TEXT_CHARS} 个字符",
                )
            text_parts.append(page_text)
        return "\n".join(text_parts)
    except HTTPException:
        raise
    except (PdfReadError, KeyError, OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法解析 PDF 文件") from error


def _extract_document_text(filename: str, payload: bytes) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".doc":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="不支持旧版 .doc 文件，请转换为 DOCX、PDF 或 CSV 后重试",
        )
    if suffix not in SUPPORTED_DOCUMENT_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="仅支持 TXT、Markdown、CSV、PDF 和 DOCX 文件",
        )
    if suffix == ".docx":
        text = _extract_docx_text(payload)
    elif suffix == ".pdf":
        text = _extract_pdf_text(payload)
    elif suffix == ".csv":
        text = _extract_csv_text(payload)
    else:
        text = _decode_text_payload(payload)
    return _normalize_extracted_text(text)


@router.post("/knowledge/upload", response_model=KnowledgeOut, status_code=status.HTTP_201_CREATED, tags=["knowledge"])
@router.post(
    "/admin/knowledge/upload",
    response_model=KnowledgeOut,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
    tags=["knowledge"],
)
async def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    current_user: User = Depends(require_roles("admin", "support_agent")),
    db: Session = Depends(get_db),
) -> KnowledgeDocument:
    del current_user
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="上传文件为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="单个文档不能超过 5MB")
    filename = _safe_uploaded_filename(file.filename)
    text = _extract_document_text(filename, content)
    if len(text) < 20:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="文档有效文本不足 20 个字符")
    document_title = (title or "").strip()[:255] or filename
    document = KnowledgeDocument(title=document_title, source=f"上传文件：{filename}"[:255], content=text)
    db.add(document)
    db.flush()
    index_document(db, document)
    db.commit()
    db.refresh(document)
    return document


@router.post("/knowledge/search", response_model=SearchResponse, tags=["knowledge"])
def search_knowledge(
    payload: SearchRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SearchResponse:
    del current_user
    hits = retrieve(db, payload.query, top_k=payload.top_k)
    return SearchResponse(
        results=[
            {"document_id": hit.document_id, "title": hit.title, "excerpt": hit.excerpt, "score": hit.score}
            for hit in hits
        ]
    )
