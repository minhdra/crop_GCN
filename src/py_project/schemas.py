"""Pydantic response models for the document scanner API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ScanResult(BaseModel):
    """Kết quả xử lý một file (PDF hoặc ảnh).

    status là "warning" khi xử lý thành công nhưng phát hiện vấn đề chất
    lượng (mờ / nghi bị nát), chi tiết nằm trong `warnings`.
    """

    filename: str
    status: Literal["success", "warning", "error"]
    message: str | None = None
    warnings: list[str] = []
    total_pages: int | None = None
    cropped_pages: int | None = None
    blurry_pages: int | None = None
    damaged_pages: int | None = None
    blurry_page_numbers: list[int] | None = None
    damaged_page_numbers: list[int] | None = None
    cropped: bool | None = None
    is_blurry: bool | None = None
    is_damaged: bool | None = None
    download_url: str | None = None
    view_url: str | None = None
    saved_path: str | None = None
    debug_url: str | None = None
    debug_page_count: int | None = None
    processing_time_seconds: float | None = None


class BatchScanResult(BaseModel):
    """Kết quả xử lý một danh sách file."""

    results: list[ScanResult]
