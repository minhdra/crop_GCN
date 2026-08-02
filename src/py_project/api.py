"""FastAPI cho công cụ crop và làm rõ PDF / ảnh theo kiểu scanner.

Chạy server:
    uvicorn py_project.api:app --reload

Sau đó xem tài liệu API tại http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator, Literal, List

import anyio.to_thread
import cv2
from dotenv import load_dotenv
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

# Nạp file .env ở thư mục làm việc hiện tại (nếu có) - chỉ điền các biến
# CHƯA được set sẵn trong môi trường, không ghi đè biến đã export/đã đặt
# qua Docker (xem docker-compose.yml). Cho phép chạy `uvicorn`/`scan-api`
# cục bộ cũng đọc được cấu hình từ .env giống như khi chạy qua Docker.
load_dotenv()

# Mặc định OpenCV tự dùng thread pool nội bộ (parallel_for_) bằng số CPU
# NÓ PHÁT HIỆN ĐƯỢC (thường là số core của máy host/VM, không phải số core
# cgroup thực sự cấp cho container). Với nhiều worker process
# (WEB_CONCURRENCY) và nhiều job đồng thời/process (SCAN_MAX_CONCURRENT_JOBS)
# cùng gọi OpenCV, mỗi job lại tự giành nhiều thread -> tổng số thread tranh
# CPU vượt xa số core thật, gây tranh chấp (oversubscription) và làm request
# chậm hẳn khi có tải đồng thời, dù CPU vẫn "bận 100%". Giới hạn về 1 để mỗi
# job dùng đúng 1 core, nhường việc chạy song song cho tầng
# process/threadpool (đã kiểm soát bằng WEB_CONCURRENCY *
# SCAN_MAX_CONCURRENT_JOBS) thay vì để OpenCV tự nhân đôi song song bên
# trong. Chỉnh qua CV2_NUM_THREADS nếu cần thử nghiệm giá trị khác.
cv2.setNumThreads(int(os.environ.get("CV2_NUM_THREADS", "1")))

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

# Số job xử lý (scan_image/scan_pdf) chạy đồng thời tối đa trên MỖI process.
# Request vượt quá số này CHỜ (xếp hàng) tối đa SCAN_QUEUE_TIMEOUT_SECONDS
# để có slot trống, thay vì bị từ chối ngay - vì client gọi vào (app chụp
# ảnh) không tự động retry khi gặp 503, nên từ chối ngay = ảnh bị mất thẳng
# với người dùng. Chỉ trả 503 nếu chờ quá lâu (hàng đợi thật sự quá tải).
# Với nhiều worker process (xem WEB_CONCURRENCY), tổng số job đồng thời
# thực tế = WEB_CONCURRENCY * SCAN_MAX_CONCURRENT_JOBS.
MAX_CONCURRENT_JOBS = int(os.environ.get("SCAN_MAX_CONCURRENT_JOBS", "4"))
_JOB_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_JOBS)

# Thời gian tối đa (giây) một request chờ có slot xử lý trống trước khi bị
# từ chối bằng 503. Nên nhỏ hơn timeout phía client để client biết chắc
# request đã thất bại thay vì bị client tự hủy giữa chừng trong lúc server
# vẫn đang xử lý.
QUEUE_TIMEOUT_SECONDS = float(os.environ.get("SCAN_QUEUE_TIMEOUT_SECONDS", "20"))

# Số request tối đa được giữ (đa số ở trạng thái chờ semaphore, gần như
# không tốn CPU) trong threadpool của MỖI process cùng lúc, để chịu được
# burst > MAX_CONCURRENT_JOBS mà không bị chặn ở tầng threadpool (nơi
# KHÔNG áp dụng SCAN_QUEUE_TIMEOUT_SECONDS). Nên đặt lớn hơn hẳn burst tối
# đa dự kiến từ client.
MAX_QUEUED_REQUESTS = int(os.environ.get("SCAN_MAX_QUEUED_REQUESTS", "40"))

# Kích thước file upload tối đa (MB) cho mỗi file, chặn sớm trước khi tốn
# CPU xử lý ảnh/PDF quá khổ.
MAX_UPLOAD_BYTES = int(os.environ.get("SCAN_MAX_UPLOAD_MB", "50")) * 1024 * 1024
_UPLOAD_COPY_CHUNK_SIZE = 1024 * 1024

# STORAGE_DIR và OUTPUT_DIR trước đây không có cơ chế dọn dẹp nào - mỗi lần
# gọi API thành công đều lưu vĩnh viễn, khiến cả hai phình to vô hạn theo
# thời gian chạy server. STORAGE_DIR chỉ cần giữ đủ lâu để người dùng tải/
# xem lại qua /api/download, /api/view, /api/debug ngay sau khi xử lý;
# OUTPUT_DIR (đối soát lâu dài) giữ lâu hơn. Cả hai đều chỉnh được qua biến
# môi trường nếu nhu cầu đối soát cần giữ lâu hơn/ngắn hơn mặc định.
STORAGE_TTL_SECONDS = int(os.environ.get("SCAN_STORAGE_TTL_HOURS", "24")) * 3600
OUTPUT_TTL_SECONDS = int(os.environ.get("SCAN_OUTPUT_TTL_DAYS", "30")) * 86400
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("SCAN_CLEANUP_INTERVAL_MINUTES", "60")) * 60


def _cleanup_expired_files() -> None:
    """Xóa job tạm (STORAGE_DIR) quá SCAN_STORAGE_TTL_HOURS và bản lưu lâu
    dài (OUTPUT_DIR) quá SCAN_OUTPUT_TTL_DAYS. Dựa trên thời gian sửa đổi
    (mtime) - job dir/file chỉ được ghi một lần lúc xử lý nên mtime phản
    ánh đúng thời điểm hoàn tất."""
    now = time.time()

    for job_dir in STORAGE_DIR.iterdir():
        try:
            if job_dir.is_dir() and now - job_dir.stat().st_mtime > STORAGE_TTL_SECONDS:
                shutil.rmtree(job_dir, ignore_errors=True)
        except OSError:
            continue

    for output_file in OUTPUT_DIR.iterdir():
        try:
            if output_file.is_file() and now - output_file.stat().st_mtime > OUTPUT_TTL_SECONDS:
                output_file.unlink(missing_ok=True)
        except OSError:
            continue


async def _periodic_cleanup_loop() -> None:
    """Chạy _cleanup_expired_files định kỳ suốt vòng đời server (không chỉ
    lúc khởi động), để dọn rác ngay cả khi server chạy liên tục lâu ngày
    không restart."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        await anyio.to_thread.run_sync(_cleanup_expired_files)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Đặt số luồng threadpool (nơi FastAPI chạy các route `def` đồng bộ)
    theo MAX_QUEUED_REQUESTS - lớn hơn MAX_CONCURRENT_JOBS vì đa số luồng
    chỉ đang chờ _JOB_SEMAPHORE (xem QUEUE_TIMEOUT_SECONDS), không xử lý
    CPU. Đồng thời dọn file/job hết hạn ngay khi khởi động và định kỳ
    trong lúc chạy."""
    anyio.to_thread.current_default_thread_limiter().total_tokens = max(
        MAX_QUEUED_REQUESTS, MAX_CONCURRENT_JOBS
    )
    _cleanup_expired_files()
    cleanup_task = asyncio.create_task(_periodic_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(
    title="Document Scanner API",
    description="Crop và làm rõ PDF / ảnh theo kiểu scanner.",
    version="1.0.0",
    lifespan=_lifespan,
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


def _copy_upload_with_size_limit(upload: UploadFile, destination: Path) -> None:
    """Sao chép file upload vào đĩa theo chunk, chặn sớm nếu vượt MAX_UPLOAD_BYTES."""
    total_bytes = 0
    with destination.open("wb") as buffer:
        while True:
            chunk = upload.file.read(_UPLOAD_COPY_CHUNK_SIZE)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > MAX_UPLOAD_BYTES:
                raise ValueError(
                    f"File vượt quá kích thước tối đa cho phép "
                    f"({MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."
                )
            buffer.write(chunk)


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

    # Giới hạn số job xử lý đồng thời trên process này; nếu đang quá tải thì
    # CHỜ tối đa QUEUE_TIMEOUT_SECONDS cho có slot trống (client không tự
    # retry khi gặp 503 - xếp hàng có giới hạn thời gian an toàn hơn từ
    # chối ngay). Chỉ từ chối (503) nếu chờ quá lâu, tức hàng đợi thật sự
    # quá tải kéo dài chứ không phải một burst ngắn.
    if not _JOB_SEMAPHORE.acquire(timeout=QUEUE_TIMEOUT_SECONDS):
        shutil.rmtree(job_dir, ignore_errors=True)
        upload.file.close()
        raise HTTPException(
            status_code=503,
            detail="Server đang xử lý quá nhiều yêu cầu, vui lòng thử lại sau.",
            headers={"Retry-After": str(int(QUEUE_TIMEOUT_SECONDS))},
        )

    try:
        _copy_upload_with_size_limit(upload, input_path)

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
        _JOB_SEMAPHORE.release()
        upload.file.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/scan", response_model=ScanResult)
def scan_single_file(
    file: Annotated[UploadFile, File(description="File PDF hoặc ảnh cần xử lý")],
    mode: ModeParam = "color",
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
    """Xử lý một file (PDF hoặc ảnh) và trả về trạng thái cùng đường dẫn tải kết quả.

    Hàm này khai báo `def` (không phải `async def`) vì `_process_one` gọi
    OpenCV/PyMuPDF đồng bộ, không hỗ trợ await; FastAPI tự chạy các route
    `def` trong threadpool riêng nên không chiếm event loop chính.
    """
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
def scan_multiple_files(
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
def download_result(job_id: str) -> FileResponse:
    """Tải file kết quả (buộc trình duyệt lưu về máy) theo job_id trả về từ /api/scan."""
    output_path = _resolve_job_output_file(job_id)
    return FileResponse(
        path=output_path,
        filename=output_path.name,
        content_disposition_type="attachment",
    )


@app.get("/api/view/{job_id}")
def view_result(job_id: str) -> FileResponse:
    """Xem trực tiếp file kết quả (ảnh/PDF hiển thị inline trên trình duyệt), dùng làm link ảnh để đối soát nhanh."""
    output_path = _resolve_job_output_file(job_id)
    return FileResponse(
        path=output_path,
        filename=output_path.name,
        content_disposition_type="inline",
    )


@app.get("/api/debug/{job_id}")
def view_debug_image(job_id: str, page: int = 1) -> FileResponse:
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
