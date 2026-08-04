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


class CaptureIssueModel(BaseModel):
    """Một lý do cụ thể khiến ảnh chụp trực tiếp từ camera bị từ chối."""

    code: str
    message: str


class CaptureQualityCheckResult(BaseModel):
    """Kết quả QC ảnh chụp trực tiếp từ camera (luồng chụp ảnh, không áp
    dụng cho file upload). `passed=False` nghĩa là ảnh bị chặn - frontend
    phải yêu cầu người dùng chụp lại thay vì gửi đi xử lý crop."""

    passed: bool
    issues: list[CaptureIssueModel] = []


class CaptureScanResult(BaseModel):
    """Kết quả QC + xử lý crop gộp trong một lần gọi cho ảnh chụp trực tiếp
    từ camera - chỉ tốn một lượt upload thay vì gọi QC rồi lại upload lần
    nữa để xử lý. `passed=False` nghĩa là ảnh bị chặn ở bước QC, `scan` là
    None; `passed=True` thì `scan` chứa kết quả xử lý như /api/scan."""

    passed: bool
    issues: list[CaptureIssueModel] = []
    scan: ScanResult | None = None
