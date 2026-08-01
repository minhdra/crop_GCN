"""FastAPI cho công cụ crop và làm rõ PDF / ảnh theo kiểu scanner.

Chạy server:
    uvicorn py_project.api:app --reload

Sau đó xem tài liệu API tại http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Literal, List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from py_project.document_scanner import (
    DEFAULT_BLUR_THRESHOLD,
    DEFAULT_SOLIDITY_THRESHOLD,
    QualityIssues,
    scan_image,
    scan_pdf,
)
from py_project.schemas import BatchScanResult, ScanResult

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".heic"}

# Thư mục lưu tạm file input/output của từng lần xử lý (dùng cho /api/download, /api/view).
STORAGE_DIR = Path(tempfile.gettempdir()) / "py_project_scan_results"
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Thư mục lưu lâu dài một bản sao kết quả để đối soát nhanh trên ổ đĩa,
# ngoài luồng gọi API. Có thể đổi qua biến môi trường SCAN_OUTPUT_DIR.
OUTPUT_DIR = Path(
    os.environ.get(
        "SCAN_OUTPUT_DIR",
        str(Path(__file__).resolve().parent.parent.parent / "output_scans"),
    )
)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"

app = FastAPI(
    title="Document Scanner API",
    description="Crop và làm rõ PDF / ảnh theo kiểu scanner.",
    version="1.0.0",
)

VALID_ROTATIONS = (0, 90, 180, 270)

ModeParam = Annotated[
    Literal["color", "scan", "bw"],
    Form(
        description=(
            "Chế độ xử lý màu ảnh đầu ra: 'color' giữ nguyên màu, "
            "'scan' tăng tương phản kiểu máy scan, 'bw' chuyển đen trắng."
        )
    ),
]
RotateParam = Annotated[
    int,
    Form(description="Góc xoay ảnh/trang theo chiều kim đồng hồ, tính bằng độ. Chỉ nhận 0, 90, 180 hoặc 270."),
]
SharpnessParam = Annotated[
    float,
    Form(ge=0, le=3, description="Độ làm nét ảnh (unsharp mask). 0 = không làm nét, giá trị càng cao càng nét."),
]
MinAreaRatioParam = Annotated[
    float,
    Form(
        gt=0,
        lt=1,
        description=(
            "Tỷ lệ diện tích tối thiểu (0-1) của vùng giấy so với toàn bộ ảnh "
            "để được nhận diện là tài liệu cần crop."
        ),
    ),
]
DpiParam = Annotated[
    int,
    Form(ge=72, le=600, description="Độ phân giải (DPI) khi render trang PDF thành ảnh trước khi xử lý. Chỉ áp dụng cho file PDF."),
]
JpegQualityParam = Annotated[
    int,
    Form(ge=1, le=100, description="Chất lượng nén JPEG của ảnh/trang đầu ra (1-100), giá trị càng cao ảnh càng nét nhưng dung lượng càng lớn."),
]
BlurThresholdParam = Annotated[
    float,
    Form(
        ge=0,
        description=(
            "Ngưỡng điểm độ nét (Laplacian variance) để coi ảnh là bị mờ; "
            "điểm thấp hơn ngưỡng này sẽ bị đánh dấu is_blurry."
        ),
    ),
]
SolidityThresholdParam = Annotated[
    float,
    Form(
        gt=0,
        le=1,
        description=(
            "Ngưỡng độ đặc (solidity) của viền giấy để nghi ngờ tài liệu bị nát/rách; "
            "solidity thấp hơn ngưỡng này sẽ bị đánh dấu is_damaged."
        ),
    ),
]


def _validate_rotate(rotate: int) -> None:
    if rotate not in VALID_ROTATIONS:
        raise HTTPException(
            status_code=422,
            detail=f"rotate phải là một trong {VALID_ROTATIONS}",
        )


def _image_quality_warnings(quality: QualityIssues) -> list[str]:
    warnings = []
    if quality.is_blurry:
        warnings.append("Ảnh bị mờ")
    if quality.is_damaged:
        warnings.append("Tài liệu nghi bị nát/rách")
    return warnings


def _pdf_quality_warnings(blurry_page_numbers: list[int], damaged_page_numbers: list[int]) -> list[str]:
    warnings = []
    if blurry_page_numbers:
        pages = ", ".join(str(n) for n in blurry_page_numbers)
        warnings.append(f"Trang {pages} bị mờ")
    if damaged_page_numbers:
        pages = ", ".join(str(n) for n in damaged_page_numbers)
        warnings.append(f"Trang {pages} nghi bị nát/rách")
    return warnings


def _persist_output_copy(job_id: str, original_name: str, output_path: Path) -> Path:
    """Sao chép file kết quả sang OUTPUT_DIR để đối soát nhanh trên ổ đĩa."""
    safe_stem = Path(original_name).name.rsplit(".", 1)[0] or "output"
    persisted_path = OUTPUT_DIR / f"{job_id}_{safe_stem}{output_path.suffix}"
    shutil.copyfile(output_path, persisted_path)
    return persisted_path


def _process_one(
    upload: UploadFile,
    mode: str,
    rotate: int,
    sharpness: float,
    min_area_ratio: float,
    crop: bool,
    dpi: int,
    jpeg_quality: int,
    blur_threshold: float,
    solidity_threshold: float,
    debug: bool,
) -> ScanResult:
    """Lưu file upload, xử lý (PDF hoặc ảnh) và trả về trạng thái kết quả."""
    original_name = upload.filename or "unknown"
    suffix = Path(original_name).suffix.lower()

    if suffix not in IMAGE_EXTENSIONS and suffix != ".pdf":
        return ScanResult(
            filename=original_name,
            status="error",
            message=f"Định dạng file không được hỗ trợ: {suffix or '(không có)'}",
        )

    job_id = uuid.uuid4().hex
    job_dir = STORAGE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_path = job_dir / f"input{suffix}"

    try:
        with input_path.open("wb") as buffer:
            shutil.copyfileobj(upload.file, buffer)

        if suffix == ".pdf":
            output_path = job_dir / "output.pdf"
            summary = scan_pdf(
                input_path=input_path,
                output_path=output_path,
                min_area_ratio=min_area_ratio,
                mode=mode,
                rotation=rotate,
                sharpness=sharpness,
                dpi=dpi,
                crop=crop,
                jpeg_quality=jpeg_quality,
                blur_threshold=blur_threshold,
                solidity_threshold=solidity_threshold,
                debug=debug,
            )
            warnings = _pdf_quality_warnings(
                summary.blurry_page_numbers, summary.damaged_page_numbers
            )
            persisted_path = _persist_output_copy(job_id, original_name, output_path)
            debug_page_count = None
            if summary.debug_dir is not None and summary.debug_dir.is_dir():
                debug_page_count = len(list(summary.debug_dir.glob("page_*.jpg")))
            return ScanResult(
                filename=original_name,
                status="warning" if warnings else "success",
                warnings=warnings,
                total_pages=summary.total_pages,
                cropped_pages=summary.cropped_pages,
                blurry_pages=summary.blurry_pages,
                damaged_pages=summary.damaged_pages,
                blurry_page_numbers=summary.blurry_page_numbers,
                damaged_page_numbers=summary.damaged_page_numbers,
                download_url=f"/api/download/{job_id}",
                view_url=f"/api/view/{job_id}",
                saved_path=str(persisted_path),
                debug_url=f"/api/debug/{job_id}" if debug_page_count else None,
                debug_page_count=debug_page_count,
            )

        output_path = job_dir / f"output{suffix}"
        cropped, quality, debug_path = scan_image(
            input_path=input_path,
            output_path=output_path,
            min_area_ratio=min_area_ratio,
            mode=mode,
            rotation=rotate,
            sharpness=sharpness,
            crop=crop,
            blur_threshold=blur_threshold,
            solidity_threshold=solidity_threshold,
            debug=debug,
        )
        warnings = _image_quality_warnings(quality)
        persisted_path = _persist_output_copy(job_id, original_name, output_path)
        return ScanResult(
            filename=original_name,
            status="warning" if warnings else "success",
            warnings=warnings,
            cropped=cropped,
            is_blurry=quality.is_blurry,
            is_damaged=quality.is_damaged,
            download_url=f"/api/download/{job_id}",
            view_url=f"/api/view/{job_id}",
            saved_path=str(persisted_path),
            debug_url=f"/api/debug/{job_id}" if debug_path is not None else None,
        )
    except ValueError as error:
        shutil.rmtree(job_dir, ignore_errors=True)
        message = str(error).replace(str(input_path), original_name)
        return ScanResult(
            filename=original_name,
            status="error",
            message=message,
        )
    finally:
        upload.file.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/scan", response_model=ScanResult)
async def scan_single_file(
    file: Annotated[UploadFile, File(description="File PDF hoặc ảnh cần xử lý")],
    mode: ModeParam = "scan",
    rotate: RotateParam = 0,
    sharpness: SharpnessParam = 0.7,
    min_area_ratio: MinAreaRatioParam = 0.2,
    crop: Annotated[
        bool,
        Form(description="Có tự động phát hiện và crop viền giấy hay không. False = giữ nguyên khung ảnh gốc."),
    ] = True,
    dpi: DpiParam = 200,
    jpeg_quality: JpegQualityParam = 92,
    blur_threshold: BlurThresholdParam = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: SolidityThresholdParam = DEFAULT_SOLIDITY_THRESHOLD,
    debug: Annotated[
        bool,
        Form(
            description=(
                "Nếu True, sinh thêm ảnh debug (contour vùng giấy, góc crop, blur_score/solidity) "
                "để xem qua /api/debug/{job_id}."
            )
        ),
    ] = False,
) -> ScanResult:
    """Xử lý một file (PDF hoặc ảnh) và trả về trạng thái cùng đường dẫn tải kết quả."""
    _validate_rotate(rotate)
    return _process_one(
        upload=file,
        mode=mode,
        rotate=rotate,
        sharpness=sharpness,
        min_area_ratio=min_area_ratio,
        crop=crop,
        dpi=dpi,
        jpeg_quality=jpeg_quality,
        blur_threshold=blur_threshold,
        solidity_threshold=solidity_threshold,
        debug=debug,
    )


@app.post("/api/scan/batch", response_model=BatchScanResult)
async def scan_multiple_files(
    # QUAN TRỌNG: Dùng List từ typing, không dùng list thường
    files: List[UploadFile] = File(description="Danh sách file PDF/ảnh cần xử lý"),
    mode: ModeParam = "scan",
    rotate: RotateParam = 0,
    sharpness: SharpnessParam = 0.7,
    min_area_ratio: MinAreaRatioParam = 0.2,
    crop: Annotated[
        bool,
        Form(description="Có tự động phát hiện và crop viền giấy hay không. False = giữ nguyên khung ảnh gốc."),
    ] = True,
    dpi: DpiParam = 200,
    jpeg_quality: JpegQualityParam = 92,
    blur_threshold: BlurThresholdParam = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: SolidityThresholdParam = DEFAULT_SOLIDITY_THRESHOLD,
    debug: Annotated[
        bool,
        Form(
            description=(
                "Nếu True, sinh thêm ảnh debug (contour vùng giấy, góc crop, blur_score/solidity) "
                "để xem qua /api/debug/{job_id}."
            )
        ),
    ] = False,
) -> BatchScanResult:
    """Xử lý một danh sách file, mỗi file trả về trạng thái riêng."""
    _validate_rotate(rotate)
    results = [
        _process_one(
            upload=upload,
            mode=mode,
            rotate=rotate,
            sharpness=sharpness,
            min_area_ratio=min_area_ratio,
            crop=crop,
            dpi=dpi,
            jpeg_quality=jpeg_quality,
            blur_threshold=blur_threshold,
            solidity_threshold=solidity_threshold,
            debug=debug,
        )
        for upload in files
    ]
    return BatchScanResult(results=results)


JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _resolve_job_output_file(job_id: str) -> Path:
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy kết quả với job_id này.")

    job_dir = STORAGE_DIR / job_id

    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Không tìm thấy kết quả với job_id này.")

    output_files = list(job_dir.glob("output.*"))
    if not output_files:
        raise HTTPException(status_code=404, detail="Không tìm thấy file kết quả.")

    return output_files[0]


@app.get("/api/download/{job_id}")
async def download_result(job_id: str) -> FileResponse:
    """Tải file kết quả (buộc trình duyệt lưu về máy) theo job_id trả về từ /api/scan."""
    output_path = _resolve_job_output_file(job_id)
    return FileResponse(
        path=output_path,
        filename=output_path.name,
        content_disposition_type="attachment",
    )


@app.get("/api/view/{job_id}")
async def view_result(job_id: str) -> FileResponse:
    """Xem trực tiếp file kết quả (ảnh/PDF hiển thị inline trên trình duyệt), dùng làm link ảnh để đối soát nhanh."""
    output_path = _resolve_job_output_file(job_id)
    return FileResponse(
        path=output_path,
        filename=output_path.name,
        content_disposition_type="inline",
    )


@app.get("/api/debug/{job_id}")
async def view_debug_image(job_id: str, page: int = 1) -> FileResponse:
    """Xem ảnh debug (contour vùng giấy + góc crop + blur_score/solidity) để
    soi vì sao crop/chất lượng ra kết quả như vậy. Chỉ có nếu gọi /api/scan
    hoặc /api/scan/batch với debug=true. Với PDF nhiều trang, dùng ?page=N
    (mặc định trang 1)."""
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy kết quả với job_id này.")

    job_dir = STORAGE_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Không tìm thấy kết quả với job_id này.")

    image_debug_files = list(job_dir.glob("output_debug.*"))
    if image_debug_files:
        debug_path = image_debug_files[0]
    else:
        debug_dir = job_dir / "output_debug"
        pages = sorted(debug_dir.glob("page_*.jpg")) if debug_dir.is_dir() else []
        if not pages or page < 1 or page > len(pages):
            raise HTTPException(
                status_code=404,
                detail="Không có ảnh debug. Gọi /api/scan hoặc /api/scan/batch với debug=true trước.",
            )
        debug_path = pages[page - 1]

    return FileResponse(
        path=debug_path,
        filename=debug_path.name,
        content_disposition_type="inline",
    )


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Chạy server FastAPI bằng uvicorn (dùng cho lệnh script)."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
