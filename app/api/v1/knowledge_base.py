"""Knowledge Base API endpoints for document upload and RAG search.

Provides endpoints for users to upload documents (PDF, Markdown, API
specs) that are indexed for semantic search and injected into the
coder agent's context automatically.

Enabled via ENABLE_KNOWLEDGE_BASE Feature Flag (default false).
Documents are isolated per-user (shared across all tasks).
"""

from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from app.api.v1.auth import get_current_user
from app.core.config import settings
from app.core.limiter import limiter
from app.core.logging import logger
from app.models.user import User
from app.services.knowledge_base_service import knowledge_base_service
from app.utils.errors import safe_500
from app.utils.sanitization import sanitize_string

router = APIRouter()


class DocumentUploadResponse(BaseModel):
    """Response model for document upload."""

    doc_id: str = Field(..., description="Document UUID for management")
    filename: str = Field(..., description="Original filename")
    chunk_count: int = Field(..., description="Number of chunks indexed")
    status: str = Field(..., description="Indexing status (indexed, empty_content, etc.)")


class DocumentInfo(BaseModel):
    """Response model for document listing."""

    doc_id: str = Field(..., description="Document UUID")
    filename: str = Field(..., description="Original filename")
    chunk_count: int = Field(..., description="Number of indexed chunks")


class DocumentDeleteResponse(BaseModel):
    """Response model for document deletion."""

    doc_id: str = Field(..., description="Document UUID that was deleted")
    deleted_count: int = Field(..., description="Number of chunks removed")


class SearchResult(BaseModel):
    """Response model for a single knowledge base search result."""

    content: str = Field(..., description="Matching text passage")
    score: float = Field(..., description="Relevance score (0.0 - 1.0)")
    filename: str = Field(..., description="Source document filename")
    doc_id: str = Field(..., description="Document UUID")


@router.post("/upload", response_model=DocumentUploadResponse)
@limiter.limit("10 per minute")
async def upload_document(
    request: Request,
    file: UploadFile = File(..., description="Document file to upload"),
    user: User = Depends(get_current_user),
):
    """Upload a document to the user's knowledge base.

    Supported formats: md, txt, pdf, json, yaml, yml.
    Max file size: KB_MAX_FILE_SIZE (default 10MB).
    PDF parsing requires PyMuPDF (install via: uv pip install pymupdf).
    """
    try:
        if not settings.ENABLE_KNOWLEDGE_BASE:
            raise HTTPException(
                status_code=404,
                detail="Knowledge base is disabled (ENABLE_KNOWLEDGE_BASE=false)",
            )

        filename = sanitize_string(file.filename or "untitled")

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in settings.KB_ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported file type: .{ext}. Allowed: {settings.KB_ALLOWED_EXTENSIONS}",
            )

        content = await file.read()
        if not content:
            raise HTTPException(status_code=422, detail="Empty file")

        if len(content) > settings.KB_MAX_FILE_SIZE:
            max_mb = settings.KB_MAX_FILE_SIZE / (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max size: {max_mb:.1f}MB",
            )

        logger.info(
            "kb_upload_request_received",
            user_id=user.id,
            filename=filename,
            file_size=len(content),
        )

        result = await knowledge_base_service.upload_document(user_id=user.id, filename=filename, content=content)

        if result.get("status") == "extraction_failed":
            raise HTTPException(
                status_code=422,
                detail="Failed to extract text from file. If PDF, ensure PyMuPDF is installed.",
            )

        if result.get("status") == "empty_content":
            raise HTTPException(
                status_code=422,
                detail="File content is empty after extraction",
            )

        logger.info(
            "kb_upload_completed",
            user_id=user.id,
            doc_id=result["doc_id"],
            chunk_count=result["chunk_count"],
        )

        return DocumentUploadResponse(
            doc_id=result["doc_id"],
            filename=result["filename"],
            chunk_count=result["chunk_count"],
            status=result["status"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("kb_upload_failed", user_id=user.id, error=str(e))
        raise safe_500(e, context="kb_upload")


@router.get("/documents", response_model=List[DocumentInfo])
@limiter.limit("20 per minute")
async def list_documents(
    request: Request,
    user: User = Depends(get_current_user),
):
    """List all documents in the user's knowledge base."""
    try:
        if not settings.ENABLE_KNOWLEDGE_BASE:
            raise HTTPException(
                status_code=404,
                detail="Knowledge base is disabled (ENABLE_KNOWLEDGE_BASE=false)",
            )

        docs = await knowledge_base_service.list_documents(str(user.id))
        logger.info("kb_list_documents", user_id=user.id, doc_count=len(docs))
        return [
            DocumentInfo(
                doc_id=d["doc_id"],
                filename=d["filename"],
                chunk_count=d["chunk_count"],
            )
            for d in docs
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("kb_list_failed", user_id=user.id, error=str(e))
        raise safe_500(e, context="kb_list_documents")


@router.delete("/documents/{doc_id}", response_model=DocumentDeleteResponse)
@limiter.limit("10 per minute")
async def delete_document(
    request: Request,
    doc_id: str,
    user: User = Depends(get_current_user),
):
    """Delete a document and all its chunks from the knowledge base."""
    try:
        if not settings.ENABLE_KNOWLEDGE_BASE:
            raise HTTPException(
                status_code=404,
                detail="Knowledge base is disabled (ENABLE_KNOWLEDGE_BASE=false)",
            )

        sanitized_doc_id = sanitize_string(doc_id)
        result = await knowledge_base_service.delete_document(str(user.id), sanitized_doc_id)

        if result.get("deleted_count", 0) == 0:
            raise HTTPException(
                status_code=404,
                detail="Document not found or already deleted",
            )

        logger.info(
            "kb_document_deleted",
            user_id=user.id,
            doc_id=sanitized_doc_id,
            deleted_count=result["deleted_count"],
        )

        return DocumentDeleteResponse(
            doc_id=sanitized_doc_id,
            deleted_count=result["deleted_count"],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("kb_delete_failed", user_id=user.id, doc_id=doc_id, error=str(e))
        raise safe_500(e, context="kb_delete_document")


@router.get("/search", response_model=List[SearchResult])
@limiter.limit("30 per minute")
async def search_knowledge_base(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500, description="Search query"),
    limit: int = Query(5, ge=1, le=10, description="Max results"),
    user: User = Depends(get_current_user),
):
    """Search the user's knowledge base for relevant passages.

    Returns semantic search results ranked by relevance score.
    """
    try:
        if not settings.ENABLE_KNOWLEDGE_BASE:
            raise HTTPException(
                status_code=404,
                detail="Knowledge base is disabled (ENABLE_KNOWLEDGE_BASE=false)",
            )

        query = sanitize_string(q)
        results = await knowledge_base_service.search(
            user_id=str(user.id),
            query=query,
            limit=limit,
        )
        logger.info("kb_search_api", user_id=user.id, query=query, result_count=len(results))
        return [
            SearchResult(
                content=r["content"],
                score=r["score"],
                filename=r["filename"],
                doc_id=r["doc_id"],
            )
            for r in results
        ]

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("kb_search_failed", user_id=user.id, error=str(e))
        raise safe_500(e, context="kb_search")
