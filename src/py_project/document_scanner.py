"""Crop and enhance image-based PDF pages into a scanner-like PDF."""

from __future__ import annotations
from PIL import Image, ImageOps
import pillow_heif

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import fitz  # PyMuPDF

# Đăng ký decoder HEIF/HEIC cho Pillow - mặc định Pillow không đọc được ảnh
# .heic (định dạng mặc định của iPhone khi chụp ảnh), Image.open() sẽ raise
# UnidentifiedImageError nếu thiếu bước này.
pillow_heif.register_heif_opener()

DEFAULT_BLUR_THRESHOLD = 100.0
DEFAULT_SOLIDITY_THRESHOLD = 0.85


@dataclass
class QualityIssues:
    """Kết quả đánh giá chất lượng ảnh/trang tài liệu."""

    is_blurry: bool
    is_damaged: bool
    blur_score: float
    solidity: float | None


@dataclass
class PdfScanSummary:
    """Tổng kết kết quả xử lý một file PDF nhiều trang."""

    total_pages: int
    cropped_pages: int
    blurry_pages: int
    damaged_pages: int
    blurry_page_numbers: list[int]
    damaged_page_numbers: list[int]
    debug_dir: Path | None = None


@dataclass
class BatchScanSummary:
    """Tổng kết kết quả xử lý hàng loạt ảnh trong một thư mục."""

    total_images: int
    cropped_images: int
    blurry_images: int
    damaged_images: int
    blurry_filenames: list[str]
    damaged_filenames: list[str]
    debug_paths: list[Path] = field(default_factory=list)


def pdf_page_to_bgr(page: fitz.Page, dpi: int) -> np.ndarray:
    """Chuyển một trang PDF thành ảnh OpenCV dạng BGR."""
    scale = dpi / 72
    matrix = fitz.Matrix(scale, scale)

    pixmap = page.get_pixmap(
        matrix=matrix,
        alpha=False,
    )

    image = np.frombuffer(
        pixmap.samples,
        dtype=np.uint8,
    ).reshape(
        pixmap.height,
        pixmap.width,
        pixmap.n,
    )

    if pixmap.n == 4:
        return cv2.cvtColor(image, cv2.COLOR_RGBA2BGR)

    return cv2.cvtColor(image, cv2.COLOR_RGB2BGR)


def process_pdf_page(
    image: np.ndarray,
    min_area_ratio: float,
    mode: str,
    rotation: int,
    sharpness: float,
    crop: bool = True,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: float = DEFAULT_SOLIDITY_THRESHOLD,
    debug: bool = False,
) -> tuple[np.ndarray, bool, QualityIssues, np.ndarray | None]:
    """
    Xử lý một trang PDF.

    Trả về:
        - Ảnh sau xử lý.
        - True nếu tìm thấy viền và đã crop.
        - Đánh giá chất lượng (mờ / nghi bị nát) của trang gốc.
        - Ảnh debug (overlay contour/góc crop) nếu debug=True, ngược lại None.
    """
    original_image = image
    cropped = False
    corners = None

    # Tính một lần, dùng lại cho assess_quality/find_document_corners/
    # generate_debug_image - bước này giờ chạy cả GrabCut nên khá tốn
    # (hàng trăm ms), gọi lặp lại 2-3 lần cho cùng một ảnh sẽ rất lãng phí.
    prepared = _paper_contours(original_image, min_area_ratio)

    # Đánh giá chất lượng trên ảnh gốc, trước khi crop làm mất tín hiệu viền rách.
    quality = assess_quality(
        image=original_image,
        min_area_ratio=min_area_ratio,
        blur_threshold=blur_threshold,
        solidity_threshold=solidity_threshold,
        prepared=prepared,
    )

    if crop:
        corners = find_document_corners(
            image=original_image,
            min_area_ratio=min_area_ratio,
            prepared=prepared,
        )

        if corners is not None:
            image = four_point_crop(original_image, corners)
            cropped = True

    debug_image = (
        generate_debug_image(original_image, min_area_ratio, corners, quality, prepared=prepared)
        if debug
        else None
    )

    # Không tìm thấy viền thì vẫn giữ nguyên trang,
    # chỉ làm rõ thay vì báo lỗi và bỏ qua trang.
    image = enhance_for_scan(
        image=image,
        mode=mode,
        sharpness=sharpness,
    )

    image = rotate(
        image=image,
        degrees=rotation,
    )

    return image, cropped, quality, debug_image


def encode_pdf_page(
    image: np.ndarray,
    mode: str,
    jpeg_quality: int,
) -> bytes:
    """Mã hóa ảnh để đưa vào PDF đầu ra."""
    if mode == "bw":
        success, buffer = cv2.imencode(
            ".png",
            image,
            [cv2.IMWRITE_PNG_COMPRESSION, 4],
        )
    else:
        success, buffer = cv2.imencode(
            ".jpg",
            image,
            [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality],
        )

    if not success:
        raise ValueError("Không thể mã hóa ảnh của trang PDF.")

    return buffer.tobytes()


def scan_pdf(
    input_path: Path,
    output_path: Path,
    min_area_ratio: float = 0.2,
    mode: str = "scan",
    rotation: int = 0,
    sharpness: float = 0.7,
    dpi: int = 200,
    crop: bool = True,
    jpeg_quality: int = 92,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: float = DEFAULT_SOLIDITY_THRESHOLD,
    debug: bool = False,
) -> PdfScanSummary:
    """
    Crop và làm rõ toàn bộ các trang trong PDF.

    Trả về tổng số trang, số trang đã crop, số trang nghi bị mờ và số trang
    nghi bị nát/rách. Nếu debug=True, còn lưu ảnh debug từng trang (contour +
    góc crop) vào thư mục `<output_stem>_debug/` bên cạnh file PDF đầu ra.
    """
    if input_path.suffix.lower() != ".pdf":
        raise ValueError("File đầu vào phải có phần mở rộng .pdf")

    if output_path.suffix.lower() != ".pdf":
        raise ValueError("File đầu ra phải có phần mở rộng .pdf")

    if not input_path.is_file():
        raise ValueError(f"Không tìm thấy file PDF: {input_path}")

    if input_path.resolve() == output_path.resolve():
        raise ValueError("File PDF đầu ra phải khác file PDF đầu vào.")

    try:
        source_pdf = fitz.open(str(input_path))
    except Exception as error:
        raise ValueError(
            f"Không thể mở file PDF: {input_path}"
        ) from error

    if source_pdf.needs_pass:
        source_pdf.close()
        raise ValueError(
            "File PDF đang được bảo vệ bằng mật khẩu."
        )

    total_pages = source_pdf.page_count

    if total_pages == 0:
        source_pdf.close()
        raise ValueError("File PDF không có trang nào.")

    output_pdf = fitz.open()
    cropped_pages = 0
    blurry_pages = 0
    damaged_pages = 0
    blurry_page_numbers: list[int] = []
    damaged_page_numbers: list[int] = []
    debug_dir = output_path.with_name(f"{output_path.stem}_debug") if debug else None

    try:
        for page_number in range(total_pages):
            source_page = source_pdf.load_page(page_number)

            # Chuyển trang PDF thành ảnh.
            image = pdf_page_to_bgr(
                page=source_page,
                dpi=dpi,
            )

            # Crop và làm rõ.
            result, cropped, quality, debug_image = process_pdf_page(
                image=image,
                min_area_ratio=min_area_ratio,
                mode=mode,
                rotation=rotation,
                sharpness=sharpness,
                crop=crop,
                blur_threshold=blur_threshold,
                solidity_threshold=solidity_threshold,
                debug=debug,
            )

            if cropped:
                cropped_pages += 1
            if quality.is_blurry:
                blurry_pages += 1
                blurry_page_numbers.append(page_number + 1)
            if quality.is_damaged:
                damaged_pages += 1
                damaged_page_numbers.append(page_number + 1)

            if debug_image is not None and debug_dir is not None:
                debug_dir.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(
                    str(debug_dir / f"page_{page_number + 1:04d}.jpg"),
                    debug_image,
                )

            image_bytes = encode_pdf_page(
                image=result,
                mode=mode,
                jpeg_quality=jpeg_quality,
            )

            image_height, image_width = result.shape[:2]

            # Chiều rộng gần tương đương khổ A4.
            output_width = 595.0
            output_height = (
                output_width * image_height / image_width
            )

            output_page = output_pdf.new_page(
                width=output_width,
                height=output_height,
            )

            output_page.insert_image(
                output_page.rect,
                stream=image_bytes,
            )

            print(
                f"Đã xử lý trang "
                f"{page_number + 1}/{total_pages}"
            )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        output_pdf.save(
            str(output_path),
            garbage=4,
            deflate=True,
        )

    finally:
        output_pdf.close()
        source_pdf.close()

    return PdfScanSummary(
        total_pages=total_pages,
        cropped_pages=cropped_pages,
        blurry_pages=blurry_pages,
        damaged_pages=damaged_pages,
        blurry_page_numbers=blurry_page_numbers,
        damaged_page_numbers=damaged_page_numbers,
        debug_dir=debug_dir,
    )


def order_corners(corners: np.ndarray) -> np.ndarray:
    """Return four points in top-left, top-right, bottom-right, bottom-left order."""
    points = corners.reshape(4, 2).astype(np.float32)
    ordered = np.empty((4, 2), dtype=np.float32)
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[np.argmin(sums)]
    ordered[2] = points[np.argmax(sums)]
    ordered[1] = points[np.argmin(differences)]
    ordered[3] = points[np.argmax(differences)]
    return ordered

_GRABCUT_MARGIN_RATIO = 0.05

# Sentinel để phân biệt "chưa truyền `prepared`, cần tự tính" với "đã tính
# rồi và kết quả thật sự là None (không tìm được vùng giấy hợp lệ)". Nếu
# dùng mặc định `None` cho cả hai trường hợp, khi ảnh không crop được (kết
# quả tính lần đầu là None), mọi hàm dùng lại `prepared=None` sẽ tưởng nhầm
# là "chưa tính" và chạy lại GrabCut từ đầu - lãng phí gấp 3-4 lần (chạy lại
# trong assess_quality, find_document_corners, generate_debug_image), là lý
# do các ảnh không crop được lại chậm hơn hẳn ảnh crop được.
_PREPARED_NOT_COMPUTED = object()


def _paper_contours(
    image: np.ndarray, min_area_ratio: float
) -> tuple[list[np.ndarray], np.ndarray, float] | None:
    """Tách vùng giấy khỏi nền bằng GrabCut (rect khởi tạo trừ margin quanh
    ảnh) và trả về contour hợp lệ (đủ lớn, không chạm cả 4 cạnh ảnh), cùng
    ảnh đã resize và tỉ lệ scale đã áp dụng.

    Dùng GrabCut thay vì dò cạnh (Canny/threshold) vì tài liệu không phẳng
    (sổ/sách đang mở, có nếp gấp) khiến viền ngoài không tạo thành 1 đường
    cạnh liền mạch - Canny dò theo gradient sáng/tối nên bị vỡ contour thành
    nhiều mảnh tại chỗ gấp. GrabCut phân loại từng pixel là tiền cảnh/nền
    theo phân bố màu, không phụ thuộc cạnh có liền mạch hay không, nên vẫn
    ra 1 vùng tài liệu liền khối."""
    height, width = image.shape[:2]
    scale = min(1.0, 1000 / max(height, width))
    resized = cv2.resize(image, None, fx=scale, fy=scale) if scale < 1 else image.copy()

    contour = _grabcut_foreground_contour(resized, _GRABCUT_MARGIN_RATIO)
    if contour is None:
        return None

    valid_contours = _filter_candidate_contours([contour], resized.shape, min_area_ratio)

    if not valid_contours:
        return None

    return valid_contours, resized, scale


def _filter_candidate_contours(
    contours: list[np.ndarray], image_shape: tuple[int, ...], min_area_ratio: float
) -> list[np.ndarray]:
    """Lọc và sắp xếp giảm dần theo diện tích các contour đủ lớn và không
    chạm từ 3 cạnh ảnh trở lên."""
    if not contours:
        return []

    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    image_area = image_shape[0] * image_shape[1]
    minimum_area = image_area * min_area_ratio
    # Nếu contour lớn gần bằng cả ảnh, gần như chắc chắn đó là nền/toàn khung
    # hình bị dính vào nhau (thường gặp khi ảnh quá ít tương phản), không
    # phải tài liệu có viền/lề - thà bỏ qua để giữ nguyên ảnh gốc còn hơn
    # crop nhầm vào nền.
    maximum_area = image_area * 0.92
    margin = 5
    valid_contours = []

    for contour in contours:
        contour_area = cv2.contourArea(contour)
        if contour_area < minimum_area or contour_area > maximum_area:
            continue

        # Loại bỏ contour chạm từ 3 cạnh ảnh trở lên - tài liệu thật luôn có
        # lề nhìn thấy được ít nhất ở 2 phía đối diện; chạm gần hết các cạnh
        # thường là nền/toàn khung hình bị dính vào vùng tách được.
        x, y, w, h = cv2.boundingRect(contour)
        touched_borders = sum([
            x <= margin,
            y <= margin,
            x + w >= image_shape[1] - 1 - margin,
            y + h >= image_shape[0] - 1 - margin,
        ])
        if touched_borders >= 3:
            continue

        # Loại bỏ contour không "đặc" theo hình chữ nhật: với một tài liệu
        # hình chữ nhật thật (dù xoay/nghiêng), hình chữ nhật xoay nhỏ nhất
        # bao quanh nó (minAreaRect) gần như trùng khít diện tích - tỉ lệ
        # diện tích minAreaRect / diện tích contour xấp xỉ 1.0. Contour bị
        # dính thêm "cành" nhiễu nền (như vết nứt/vân gỗ nối vào viền giấy)
        # sẽ kéo minAreaRect phình to hẳn ra so với diện tích thật, tỉ lệ
        # cao hơn rõ rệt - dấu hiệu contour không đáng tin, dù diện tích và
        # vị trí (không chạm cạnh) trông có vẻ hợp lệ.
        rect_area = cv2.contourArea(cv2.boxPoints(cv2.minAreaRect(contour)).astype(np.float32))
        if rect_area > contour_area * 1.2:
            continue

        valid_contours.append(contour)

    return valid_contours


# GrabCut chậm hẳn (quan sát được vài chục giây, có ảnh tới ~90s) khi chạy
# trực tiếp trên ảnh full-size đã resize (tối đa 1000px) - nền có màu/kết
# cấu khiến bước phân cụm màu ban đầu (GMM) hội tụ rất chậm. Chạy trên bản
# thu nhỏ hơn nữa (tối đa 450px) giữ thời gian ở mức vài giây mà kết quả
# contour không đổi đáng kể, vì đây là dịch vụ xử lý theo request (API).
_GRABCUT_WORKING_SIZE = 450


def _grabcut_foreground_contour(image: np.ndarray, margin_ratio: float) -> np.ndarray | None:
    """Tách tài liệu khỏi nền bằng GrabCut (khởi tạo bằng rect trừ margin
    quanh ảnh, coi phần ngoài rect là nền chắc chắn), trả về contour lớn
    nhất tách được (toạ độ theo khung hình truyền vào, chưa quy đổi về ảnh
    gốc)."""
    frame_h, frame_w = image.shape[:2]
    grabcut_scale = min(1.0, _GRABCUT_WORKING_SIZE / max(frame_h, frame_w))
    small = (
        cv2.resize(image, None, fx=grabcut_scale, fy=grabcut_scale)
        if grabcut_scale < 1
        else image
    )
    small_h, small_w = small.shape[:2]
    margin_x = int(small_w * margin_ratio)
    margin_y = int(small_h * margin_ratio)
    rect = (margin_x, margin_y, small_w - 2 * margin_x, small_h - 2 * margin_y)
    if rect[2] <= 0 or rect[3] <= 0:
        return None

    mask = np.zeros((small_h, small_w), np.uint8)
    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(small, mask, rect, bgd_model, fgd_model, 5, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None

    fg_mask = np.where(
        (mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0
    ).astype(np.uint8)
    # Nối các mảnh nhỏ rời rạc do nếp gấp/nhiễu màu ở biên tài liệu.
    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))

    contours, _ = cv2.findContours(fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    if grabcut_scale < 1:
        contour = (contour / grabcut_scale).astype(np.float32)
    return contour


def _approximate_quad(contour: np.ndarray) -> np.ndarray | None:
    """Xấp xỉ contour thành đa giác lồi 4 điểm. Thử nhiều mức epsilon từ chặt
    đến lỏng vì viền giấy thực tế (bo tròn nhẹ, mờ, có bóng) thường không xẹp
    gọn về đúng 4 góc ngay ở epsilon đầu tiên."""
    perimeter = cv2.arcLength(contour, True)
    for epsilon_ratio in (0.01, 0.02, 0.03, 0.04, 0.05, 0.08):
        approximation = cv2.approxPolyDP(contour, epsilon_ratio * perimeter, True)
        if len(approximation) == 4 and cv2.isContourConvex(approximation):
            return approximation.reshape(4, 2).astype(np.float32)
    return None


def find_document_corners(
    image: np.ndarray,
    min_area_ratio: float,
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = _PREPARED_NOT_COMPUTED,
) -> np.ndarray | None:
    """Tìm 4 góc tài liệu từ contour tách được bằng GrabCut (`_paper_contours`).
    Xấp xỉ contour thành đúng 4 điểm; nếu viền quá nhiễu/mờ để xấp xỉ gọn,
    dùng hình chữ nhật xoay nhỏ nhất bao quanh contour làm phương án dự
    phòng - vẫn lật thẳng được tài liệu bị nghiêng thay vì bỏ qua không
    crop.

    `prepared` cho phép truyền lại kết quả `_paper_contours` đã tính sẵn
    (từ assess_quality/generate_debug_image trên cùng ảnh) để khỏi tính lại
    - bước này chạy GrabCut nên khá tốn (hàng trăm ms đến vài giây), không
    nên lặp lại 2-3 lần cho mỗi ảnh/trang. Nếu `prepared` được truyền vào là
    None (nghĩa là đã tính rồi và không tìm được vùng giấy), KHÔNG tính lại
    - trả về None ngay."""
    if prepared is _PREPARED_NOT_COMPUTED:
        prepared = _paper_contours(image, min_area_ratio)
    if prepared is None:
        return None

    contours, _, scale = prepared
    contour = contours[0]

    quad = _approximate_quad(contour)
    if quad is None:
        quad = cv2.boxPoints(cv2.minAreaRect(contour)).astype(np.float32)

    return quad / scale


def assess_quality(
    image: np.ndarray,
    min_area_ratio: float,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: float = DEFAULT_SOLIDITY_THRESHOLD,
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = _PREPARED_NOT_COMPUTED,
) -> QualityIssues:
    """Đánh giá ảnh bị mờ (độ nét thấp) và tài liệu nghi bị nát/rách (viền
    lồi lõm bất thường). Phải chạy trên ảnh gốc trước khi crop, vì sau khi
    crop bằng four_point_crop, tài liệu luôn là một hình chữ nhật sạch nên
    sẽ mất tín hiệu viền rách.

    `prepared`: xem ghi chú ở find_document_corners."""
    # Phương sai Laplacian phụ thuộc rất nhiều vào độ phân giải ảnh đưa
    # vào: cùng một ảnh, chỉ resize khác đi, điểm số có thể lệch hàng chục
    # đến hàng trăm lần (ảnh càng nhỏ, biên vốn mềm bị "nén" lại thành biên
    # cứng hơn so với lưới điểm ảnh mới, đẩy điểm số lên rất cao dù ảnh
    # không hề nét hơn) - dùng ngưỡng cố định trên ảnh chưa chuẩn hóa kích
    # thước sẽ báo sai tùy theo độ phân giải ảnh đầu vào (ảnh chụp điện
    # thoại độ phân giải cao/thấp khác nhau, hoặc PDF render ở DPI khác
    # nhau). Chuẩn hóa về cùng một chiều dài cạnh dài trước khi tính điểm
    # để threshold có ý nghĩa nhất quán bất kể ảnh gốc to hay nhỏ.
    height, width = image.shape[:2]
    normalization_scale = 1200 / max(height, width)
    normalized = (
        cv2.resize(image, None, fx=normalization_scale, fy=normalization_scale)
        if normalization_scale < 1
        else image
    )
    gray = cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY)
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    is_blurry = blur_score < blur_threshold

    solidity: float | None = None
    if prepared is _PREPARED_NOT_COMPUTED:
        prepared = _paper_contours(image, min_area_ratio)
    if prepared is not None:
        contours, _, _ = prepared
        # Ưu tiên contour xấp xỉ được thành tứ giác sạch (giống hình tài
        # liệu thật) để tính solidity; contour lớn nhất trong danh sách đôi
        # khi chỉ là biên bị giãn nở/nối đứt quãng lồi lõm do chính bước xử
        # lý ảnh tạo ra (không phải do tài liệu thật bị rách), dùng nó sẽ
        # báo nhầm is_damaged.
        reference_contour = next(
            (contour for contour in contours if _approximate_quad(contour) is not None),
            contours[0],
        )
        hull_area = cv2.contourArea(cv2.convexHull(reference_contour))
        if hull_area > 0:
            solidity = cv2.contourArea(reference_contour) / hull_area

    is_damaged = solidity is not None and solidity < solidity_threshold

    return QualityIssues(
        is_blurry=is_blurry,
        is_damaged=is_damaged,
        blur_score=blur_score,
        solidity=solidity,
    )


# --- QC ảnh chụp trực tiếp từ camera (chặn cứng, yêu cầu chụp lại) ---------
#
# Khác với assess_quality (chỉ gắn cờ cảnh báo, vẫn cho xử lý), các heuristic
# dưới đây dùng cho luồng chụp ảnh trực tiếp (không áp dụng cho file upload):
# nếu vi phạm, ảnh bị chặn hoàn toàn và người dùng phải chụp lại. Đây vẫn là
# các heuristic xấp xỉ (giống blur/solidity ở trên), có thể cần tinh chỉnh
# ngưỡng theo dữ liệu thực tế.

DEFAULT_MIN_CAPTURE_RESOLUTION = 500
DEFAULT_SKIN_COVERAGE_THRESHOLD = 0.10
DEFAULT_CLUTTER_EDGE_RATIO_THRESHOLD = 0.20

# Không có tiêu chí "chỉ là tài liệu" (nghi chụp lại từ màn hình thiết bị
# khác) ở đây: đã thử hai hướng heuristic phổ biến (đỉnh phổ tần số trên ảnh
# xám, và lệch màu giữa các kênh RGB kiểu chroma) và cả hai đều không đủ tin
# cậy để chặn cứng - hướng đầu nhận nhầm giấy kẻ ô ly/bảng biểu (rất phổ biến
# với tài liệu thật) thành chụp màn hình với điểm số còn cao hơn cả trường
# hợp mô phỏng chụp màn hình; hướng sau chính xác hơn trên ảnh gốc nhưng tín
# hiệu gần như bị nén JPEG (chroma subsampling) xóa sạch - trong khi ảnh chụp
# từ camera trình duyệt luôn được nén JPEG trước khi gửi lên. Để bổ sung tiêu
# chí này sau, cần tập ảnh mẫu chụp màn hình thật để hiệu chỉnh thay vì đoán
# ngưỡng trên ảnh tổng hợp.

# Ngưỡng màu da trong không gian YCrCb - ổn định hơn RGB/HSV trước thay đổi
# ánh sáng vì tách riêng độ chói (Y) khỏi thông tin màu (Cr, Cb).
_SKIN_YCRCB_LOWER = np.array([0, 133, 77], dtype=np.uint8)
_SKIN_YCRCB_UPPER = np.array([255, 173, 127], dtype=np.uint8)


@dataclass
class CaptureIssue:
    """Một lý do cụ thể khiến ảnh chụp bị từ chối, kèm thông báo tiếng Việt
    hiển thị trực tiếp cho người dùng để họ biết cách chụp lại cho đúng."""

    code: str
    message: str


@dataclass
class CaptureQualityResult:
    """Kết quả QC một ảnh vừa chụp từ camera. `passed=False` nghĩa là ảnh bị
    chặn, không xử lý crop - frontend phải yêu cầu người dùng chụp lại.

    `prepared`: kết quả `_paper_contours` đã tính trong lúc QC - cho phép
    caller (ví dụ endpoint gộp QC + crop) truyền lại cho `scan_image`/
    `find_document_corners` khi `passed=True`, khỏi phải chạy lại GrabCut
    (tốn nhất trong toàn bộ pipeline) lần thứ hai trên cùng một ảnh."""

    passed: bool
    issues: list[CaptureIssue] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = None


def _skin_mask(image: np.ndarray) -> np.ndarray:
    ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)
    return cv2.inRange(ycrcb, _SKIN_YCRCB_LOWER, _SKIN_YCRCB_UPPER)


def _skin_coverage_ratio(
    resized: np.ndarray, document_contour: np.ndarray
) -> float:
    """Tỉ lệ diện tích vùng tài liệu bị che bởi vùng màu da tay - dùng để
    phát hiện ngón tay/bàn tay đè lên tài liệu lúc chụp."""
    mask = _skin_mask(resized)
    document_mask = np.zeros(mask.shape, dtype=np.uint8)
    cv2.drawContours(
        document_mask, [document_contour.astype(np.int32)], -1, 255, thickness=cv2.FILLED
    )
    document_area = cv2.countNonZero(document_mask)
    if document_area == 0:
        return 0.0
    skin_in_document = cv2.countNonZero(cv2.bitwise_and(mask, document_mask))
    return skin_in_document / document_area


def _background_clutter_ratio(
    resized: np.ndarray, document_contour: np.ndarray
) -> float:
    """Mật độ biên (edge) ở phần nền quanh tài liệu (ngoài contour). Nền
    phẳng/đơn sắc cho mật độ biên thấp; nền nhiều vật thể, hoa văn, hay lộn
    xộn cho mật độ biên cao hẳn - dùng làm tín hiệu "quá nhiều thứ gây
    nhiễu"."""
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    document_mask = np.zeros(edges.shape, dtype=np.uint8)
    cv2.drawContours(
        document_mask, [document_contour.astype(np.int32)], -1, 255, thickness=cv2.FILLED
    )
    background_mask = cv2.bitwise_not(document_mask)
    background_area = cv2.countNonZero(background_mask)
    if background_area == 0:
        return 0.0
    background_edges = cv2.countNonZero(cv2.bitwise_and(edges, background_mask))
    return background_edges / background_area


def assess_capture_quality(
    image: np.ndarray,
    min_area_ratio: float = 0.2,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    min_resolution: int = DEFAULT_MIN_CAPTURE_RESOLUTION,
    skin_coverage_threshold: float = DEFAULT_SKIN_COVERAGE_THRESHOLD,
    clutter_edge_ratio_threshold: float = DEFAULT_CLUTTER_EDGE_RATIO_THRESHOLD,
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = _PREPARED_NOT_COMPUTED,
) -> CaptureQualityResult:
    """QC ảnh vừa chụp trực tiếp từ camera trước khi xử lý crop. Kiểm tra
    (mỗi mục có thể tự thêm/bớt sau này, không phụ thuộc lẫn nhau):
        - Không nhận diện được tài liệu rõ ràng trong khung hình.
        - Độ phân giải quá thấp hoặc ảnh bị mờ, không đọc được chữ.
        - Tay che một phần tài liệu.
        - Nền xung quanh quá nhiều thứ gây nhiễu.

    `passed=False` nếu có bất kỳ vi phạm nào - frontend phải yêu cầu người
    dùng chụp lại thay vì tiếp tục xử lý.

    `prepared`: cho phép truyền lại kết quả `_paper_contours` đã tính sẵn (ví
    dụ khi gọi liền sau đó để crop luôn ảnh đạt QC) để khỏi chạy lại GrabCut
    - xem ghi chú ở `find_document_corners`."""
    issues: list[CaptureIssue] = []
    scores: dict[str, float] = {}

    height, width = image.shape[:2]
    long_side = max(height, width)
    scores["resolution_px"] = float(long_side)
    if long_side < min_resolution:
        issues.append(
            CaptureIssue(
                code="low_resolution",
                message=(
                    "Độ phân giải ảnh quá thấp, không nhìn rõ chữ. "
                    "Vui lòng lại gần hơn hoặc chụp lại ở độ phân giải cao hơn."
                ),
            )
        )

    if prepared is _PREPARED_NOT_COMPUTED:
        prepared = _paper_contours(image, min_area_ratio)
    quality = assess_quality(
        image=image,
        min_area_ratio=min_area_ratio,
        blur_threshold=blur_threshold,
        prepared=prepared,
    )
    scores["blur_score"] = quality.blur_score
    if quality.is_blurry:
        issues.append(
            CaptureIssue(
                code="blurry",
                message=(
                    "Ảnh bị mờ, không đọc được chữ. "
                    "Vui lòng giữ máy chụp ổn định, đủ sáng rồi chụp lại."
                ),
            )
        )

    if prepared is None:
        issues.append(
            CaptureIssue(
                code="no_document_detected",
                message=(
                    "Không nhận diện được tài liệu rõ ràng trong khung hình. "
                    "Vui lòng đặt tài liệu trên nền phẳng, đơn sắc, đủ ánh sáng, "
                    "chụp thẳng góc và lấy trọn tài liệu vào khung hình rồi chụp lại."
                ),
            )
        )
    else:
        contours, resized, _scale = prepared
        document_contour = next(
            (contour for contour in contours if _approximate_quad(contour) is not None),
            contours[0],
        )

        skin_ratio = _skin_coverage_ratio(resized, document_contour)
        scores["skin_coverage_ratio"] = skin_ratio
        if skin_ratio > skin_coverage_threshold:
            issues.append(
                CaptureIssue(
                    code="hand_covering",
                    message=(
                        "Phát hiện tay che một phần tài liệu. "
                        "Vui lòng bỏ tay ra khỏi vùng tài liệu rồi chụp lại."
                    ),
                )
            )

        clutter_ratio = _background_clutter_ratio(resized, document_contour)
        scores["background_clutter_ratio"] = clutter_ratio
        if clutter_ratio > clutter_edge_ratio_threshold:
            issues.append(
                CaptureIssue(
                    code="cluttered_background",
                    message=(
                        "Nền xung quanh có quá nhiều vật/chi tiết gây nhiễu. "
                        "Vui lòng đặt tài liệu lên nền phẳng, đơn sắc rồi chụp lại."
                    ),
                )
            )

    return CaptureQualityResult(passed=not issues, issues=issues, scores=scores, prepared=prepared)


def generate_debug_image(
    image: np.ndarray,
    min_area_ratio: float,
    corners: np.ndarray | None,
    quality: QualityIssues,
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = _PREPARED_NOT_COMPUTED,
) -> np.ndarray:
    """Vẽ overlay debug lên ảnh gốc (trước crop) để soi vì sao crop/chất
    lượng lại ra kết quả như vậy:
        - Các contour vùng giấy tìm được (viền xanh dương).
        - 4 góc đã chọn để crop, nếu có (viền xanh lá + chấm đỏ ở góc).
        - Chỉ số blur_score/solidity và cờ mờ/nát dạng chữ ở góc trên trái.

    `prepared`: xem ghi chú ở find_document_corners.
    """
    debug_image = image.copy()
    scale_factor = max(image.shape[0], image.shape[1]) / 1500

    if prepared is _PREPARED_NOT_COMPUTED:
        prepared = _paper_contours(image, min_area_ratio)
    if prepared is not None:
        contours, _, contour_scale = prepared
        # Chỉ vẽ vài contour lớn nhất; danh sách giờ gộp cả contour từ
        # threshold lẫn Canny nên có thể khá dài, vẽ hết sẽ rối overlay.
        for contour in contours[:5]:
            scaled_contour = (contour / contour_scale).astype(np.int32)
            cv2.drawContours(
                debug_image, [scaled_contour], -1, (255, 128, 0), max(1, round(2 * scale_factor))
            )

    if corners is not None:
        points = corners.reshape(4, 2).astype(np.int32)
        cv2.polylines(
            debug_image, [points], isClosed=True, color=(0, 255, 0),
            thickness=max(1, round(3 * scale_factor)),
        )
        for x, y in points:
            cv2.circle(debug_image, (int(x), int(y)), max(3, round(8 * scale_factor)), (0, 0, 255), -1)

    lines = [
        f"crop: {'OK' if corners is not None else 'khong tim thay vien'}",
        f"blur_score={quality.blur_score:.1f} ({'MO' if quality.is_blurry else 'ok'})",
        f"solidity={quality.solidity:.3f}" if quality.solidity is not None else "solidity=n/a",
        f"nghi nat/rach: {'CO' if quality.is_damaged else 'khong'}",
    ]
    font_scale = 0.8 * scale_factor
    thickness = max(1, round(2 * scale_factor))
    line_height = round(40 * scale_factor)
    for i, line in enumerate(lines):
        y = line_height * (i + 1)
        cv2.putText(
            debug_image, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale,
            (0, 0, 0), thickness + 2, cv2.LINE_AA,
        )
        cv2.putText(
            debug_image, line, (10, y), cv2.FONT_HERSHEY_SIMPLEX, font_scale,
            (0, 255, 255), thickness, cv2.LINE_AA,
        )

    return debug_image


def four_point_crop(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Apply a perspective transform so the document becomes rectangular."""
    top_left, top_right, bottom_right, bottom_left = order_corners(corners)
    output_width = round(max(np.linalg.norm(bottom_right - bottom_left), np.linalg.norm(top_right - top_left)))
    output_height = round(max(np.linalg.norm(top_right - bottom_right), np.linalg.norm(top_left - bottom_left)))
    if output_width < 2 or output_height < 2:
        raise ValueError("Vùng tìm được quá nhỏ để crop.")
    destination = np.array(
        [[0, 0], [output_width - 1, 0], [output_width - 1, output_height - 1], [0, output_height - 1]],
        dtype=np.float32,
    )
    transform = cv2.getPerspectiveTransform(np.array([top_left, top_right, bottom_right, bottom_left]), destination)
    return cv2.warpPerspective(image, transform, (output_width, output_height))


def enhance_for_scan(image: np.ndarray, mode: str, sharpness: float) -> np.ndarray:
    """Flatten uneven lighting and produce a readable scanner-like result."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Denoise then improve local contrast. This keeps stamps and faint text,
    # unlike a hard threshold which can make a photocopied document unreadable.
    # bilateralFilter is ~25x faster than fastNlMeansDenoising at this
    # resolution and preserves text edges just as well for scanned documents.
    denoised = cv2.bilateralFilter(gray, 9, 50, 50)
    enhanced = cv2.createCLAHE(clipLimit=1.4, tileGridSize=(16, 16)).apply(denoised)
    if sharpness:
        # Unsharp masking increases the contrast on character edges without
        # changing the document's overall brightness.
        softened = cv2.GaussianBlur(enhanced, (0, 0), 1.2)
        enhanced = cv2.addWeighted(enhanced, 1 + sharpness, softened, -sharpness, 0)
    if mode == "bw":
        return cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
        )
    if mode == "scan":
        return enhanced
    return image


def rotate(image: np.ndarray, degrees: int) -> np.ndarray:
    rotations = {
        0: image,
        90: cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE),
        180: cv2.rotate(image, cv2.ROTATE_180),
        270: cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE),
    }
    return rotations[degrees]


# def scan_image(
#     input_path: Path, output_path: Path, min_area_ratio: float, mode: str, rotation: int, sharpness: float
# ) -> None:
#     image = cv2.imread(str(input_path))
#     if image is None:
#         raise ValueError(f"Không thể đọc ảnh: {input_path}")
#     corners = find_document_corners(image, min_area_ratio)
#     if corners is None:
#         raise ValueError("Không tìm được viền bốn góc của ảnh/tài liệu để crop.")
#     result = four_point_crop(image, corners)
#     result = rotate(enhance_for_scan(result, mode, sharpness), rotation)
#     output_path.parent.mkdir(parents=True, exist_ok=True)
#     if not cv2.imwrite(str(output_path), result):
#         raise ValueError(f"Không thể ghi ảnh: {output_path}")

def load_image_with_exif(image_path: Path) -> np.ndarray:
    """Đọc ảnh và tự động xoay theo thẻ EXIF (sửa lỗi ảnh mobile bị xoay ngang)."""
    pil_img = Image.open(image_path)
    pil_img = ImageOps.exif_transpose(pil_img) # Tự động xoay về đúng hướng
    
    if pil_img.mode == 'RGBA':
        np_img = np.array(pil_img)
        return cv2.cvtColor(np_img, cv2.COLOR_RGBA2BGR)
    elif pil_img.mode == 'RGB':
        np_img = np.array(pil_img)
        return cv2.cvtColor(np_img, cv2.COLOR_RGB2BGR)
    else:
        pil_img = pil_img.convert('RGB')
        np_img = np.array(pil_img)
        return cv2.cvtColor(np_img, cv2.COLOR_RGB2BGR)

# Cập nhật lại scan_image
def scan_image(
    input_path: Path,
    output_path: Path,
    min_area_ratio: float,
    mode: str,
    rotation: int,
    sharpness: float,
    crop: bool = True,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: float = DEFAULT_SOLIDITY_THRESHOLD,
    debug: bool = False,
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = _PREPARED_NOT_COMPUTED,
) -> tuple[bool, QualityIssues, Path | None]:
    """Xử lý và lưu một ảnh đầu vào.

    Trả về True nếu đã tìm thấy viền và crop, đánh giá chất lượng (mờ / nghi
    bị nát) của ảnh gốc, và đường dẫn ảnh debug (contour + góc crop) nếu
    debug=True, ngược lại None.

    `prepared`: cho phép truyền lại kết quả `_paper_contours` đã tính sẵn từ
    bên ngoài (ví dụ đã chạy QC ảnh chụp camera ngay trước đó trên cùng
    ảnh) để khỏi chạy lại GrabCut - xem ghi chú ở `find_document_corners`.
    """
    # Sử dụng load_image_with_exif thay vì cv2.imread
    image = load_image_with_exif(input_path)
    original_image = image

    # Tính một lần, dùng lại cho assess_quality/find_document_corners/
    # generate_debug_image - bước này giờ chạy cả GrabCut nên khá tốn
    # (hàng trăm ms), gọi lặp lại 2-3 lần cho cùng một ảnh sẽ rất lãng phí.
    if prepared is _PREPARED_NOT_COMPUTED:
        prepared = _paper_contours(original_image, min_area_ratio)

    # Đánh giá chất lượng trên ảnh gốc, trước khi crop làm mất tín hiệu viền rách.
    quality = assess_quality(
        image=original_image,
        min_area_ratio=min_area_ratio,
        blur_threshold=blur_threshold,
        solidity_threshold=solidity_threshold,
        prepared=prepared,
    )

    cropped = False
    corners = None
    if crop:
        corners = find_document_corners(original_image, min_area_ratio, prepared=prepared)
        if corners is not None:
            image = four_point_crop(original_image, corners)
            cropped = True
        # Nếu không tìm thấy viền, giữ nguyên ảnh gốc và chỉ làm rõ

    debug_path: Path | None = None
    if debug:
        debug_image = generate_debug_image(
            original_image, min_area_ratio, corners, quality, prepared=prepared
        )
        debug_path = output_path.with_name(f"{output_path.stem}_debug{output_path.suffix}")
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(debug_path), debug_image):
            raise ValueError(f"Không thể ghi ảnh debug: {debug_path}")

    result = enhance_for_scan(image, mode, sharpness)
    result = rotate(result, rotation)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), result):
        raise ValueError(f"Không thể ghi ảnh: {output_path}")

    return cropped, quality, debug_path

def scan_images(
    input_dir: Path,
    output_dir: Path,
    min_area_ratio: float = 0.2,
    mode: str = "scan",
    rotation: int = 0,
    sharpness: float = 0.7,
    crop: bool = True,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: float = DEFAULT_SOLIDITY_THRESHOLD,
    debug: bool = False,
) -> BatchScanSummary:
    """Xử lý hàng loạt ảnh trong một thư mục."""
    extensions = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".heic"}
    image_paths = sorted([
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    ])

    if not image_paths:
        raise ValueError(f"Không tìm thấy file ảnh nào trong thư mục: {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    cropped_count = 0
    blurry_count = 0
    damaged_count = 0
    blurry_filenames: list[str] = []
    damaged_filenames: list[str] = []
    debug_paths: list[Path] = []

    for i, img_path in enumerate(image_paths):
        out_path = output_dir / img_path.name
        # Tránh ghi đè nếu thư mục input và output trùng nhau
        if out_path.resolve() == img_path.resolve():
            out_path = output_dir / f"scan_{img_path.name}"

        cropped, quality, debug_path = scan_image(
            input_path=img_path,
            output_path=out_path,
            min_area_ratio=min_area_ratio,
            mode=mode,
            rotation=rotation,
            sharpness=sharpness,
            crop=crop,
            blur_threshold=blur_threshold,
            solidity_threshold=solidity_threshold,
            debug=debug,
        )
        if cropped:
            cropped_count += 1
        if quality.is_blurry:
            blurry_count += 1
            blurry_filenames.append(img_path.name)
        if quality.is_damaged:
            damaged_count += 1
            damaged_filenames.append(img_path.name)
        if debug_path is not None:
            debug_paths.append(debug_path)

        print(f"Đã xử lý ảnh {i + 1}/{len(image_paths)}: {img_path.name}")

    return BatchScanSummary(
        total_images=len(image_paths),
        cropped_images=cropped_count,
        blurry_images=blurry_count,
        damaged_images=damaged_count,
        blurry_filenames=blurry_filenames,
        damaged_filenames=damaged_filenames,
        debug_paths=debug_paths,
    )

def main() -> None:
    """CLI nhận PDF, ảnh lẻ, hoặc thư mục ảnh và xuất kết quả đã xử lý."""
    parser = argparse.ArgumentParser(
        description="Crop và làm rõ PDF / Ảnh theo kiểu scanner."
    )
    parser.add_argument("input", type=Path, help="Đường dẫn file PDF, ảnh lẻ, hoặc thư mục chứa ảnh")
    parser.add_argument("output", type=Path, help="Đường dẫn file PDF, ảnh lẻ, hoặc thư mục đầu ra")
    
    # ... (Giữ nguyên các parser.add_argument cho --mode, --rotate, --sharpness, --min-area-ratio, --no-crop) ...
    parser.add_argument("--mode", choices=("color", "scan", "bw"), default="scan", help="color: giữ màu; scan: ảnh xám; bw: đen trắng")
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0, help="Xoay trang")
    parser.add_argument("--sharpness", type=float, default=0.7, help="Mức làm sắc nét")
    parser.add_argument("--min-area-ratio", type=float, default=0.2, help="Tỉ lệ diện tích tối thiểu")
    parser.add_argument("--no-crop", action="store_true", help="Không tìm viền, chỉ làm rõ")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Xuất thêm ảnh debug (contour vùng giấy, góc crop, blur_score/solidity) để soi vì sao crop/chất lượng ra như vậy",
    )

    # Các tham số riêng cho PDF (sẽ bỏ qua nếu xử lý ảnh)
    parser.add_argument("--dpi", type=int, default=200, help="DPI khi chuyển PDF thành ảnh")
    parser.add_argument("--jpeg-quality", type=int, default=92, help="Chất lượng JPEG trong PDF")

    args = parser.parse_args()

    # Validate tham số chung
    if not 0 < args.min_area_ratio < 1:
        parser.error("--min-area-ratio phải nằm trong khoảng 0 đến 1")
    if not 0 <= args.sharpness <= 3:
        parser.error("--sharpness phải nằm trong khoảng 0 đến 3")

    try:
        # 1. Nếu đầu vào là Thư mục -> Xử lý hàng loạt ảnh
        if args.input.is_dir():
            if args.output.suffix.lower() == ".pdf":
                parser.error("Đầu ra không thể là file PDF khi đầu vào là thư mục ảnh.")
            summary = scan_images(
                input_dir=args.input,
                output_dir=args.output,
                min_area_ratio=args.min_area_ratio,
                mode=args.mode,
                rotation=args.rotate,
                sharpness=args.sharpness,
                crop=not args.no_crop,
                debug=args.debug,
            )
            print(f"\nĐã lưu vào thư mục: {args.output}")
            print(f"Tổng số ảnh: {summary.total_images}")
            print(f"Số ảnh tìm thấy viền và crop: {summary.cropped_images}")
            print(f"Số ảnh giữ nguyên: {summary.total_images - summary.cropped_images}")
            print(f"Số ảnh nghi bị mờ: {summary.blurry_images}")
            if summary.blurry_filenames:
                print(f"  -> {', '.join(summary.blurry_filenames)}")
            print(f"Số ảnh nghi bị nát/rách: {summary.damaged_images}")
            if summary.damaged_filenames:
                print(f"  -> {', '.join(summary.damaged_filenames)}")
            if summary.debug_paths:
                print(f"Ảnh debug: {len(summary.debug_paths)} file, cạnh mỗi ảnh gốc (hậu tố _debug)")

        # 2. Nếu đầu vào là File PDF
        elif args.input.suffix.lower() == ".pdf":
            if args.output.suffix.lower() != ".pdf":
                parser.error("File đầu ra phải có phần mở rộng .pdf")
            if not 72 <= args.dpi <= 600:
                parser.error("--dpi phải nằm trong khoảng 72 đến 600")
            if not 1 <= args.jpeg_quality <= 100:
                parser.error("--jpeg-quality phải nằm trong khoảng 1 đến 100")

            summary = scan_pdf(
                input_path=args.input,
                output_path=args.output,
                min_area_ratio=args.min_area_ratio,
                mode=args.mode,
                rotation=args.rotate,
                sharpness=args.sharpness,
                dpi=args.dpi,
                crop=not args.no_crop,
                jpeg_quality=args.jpeg_quality,
                debug=args.debug,
            )
            print(f"\nĐã lưu PDF: {args.output}")
            print(f"Tổng số trang: {summary.total_pages}")
            print(f"Số trang tìm thấy viền và crop: {summary.cropped_pages}")
            print(f"Số trang giữ nguyên toàn trang: {summary.total_pages - summary.cropped_pages}")
            print(f"Số trang nghi bị mờ: {summary.blurry_pages}")
            if summary.blurry_page_numbers:
                print(f"  -> Trang {', '.join(map(str, summary.blurry_page_numbers))}")
            print(f"Số trang nghi bị nát/rách: {summary.damaged_pages}")
            if summary.damaged_page_numbers:
                print(f"  -> Trang {', '.join(map(str, summary.damaged_page_numbers))}")
            if summary.debug_dir is not None:
                print(f"Ảnh debug từng trang: {summary.debug_dir}")

        # 3. Nếu đầu vào là File Ảnh lẻ
        elif args.input.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".heic"}:
            if args.output.suffix.lower() == ".pdf":
                parser.error("Đầu ra không thể là file PDF khi đầu vào là ảnh lẻ. (Hãy dùng thư mục hoặc chuyển ảnh sang PDF bằng công cụ khác)")

            cropped, quality, debug_path = scan_image(
                input_path=args.input,
                output_path=args.output,
                min_area_ratio=args.min_area_ratio,
                mode=args.mode,
                rotation=args.rotate,
                sharpness=args.sharpness,
                crop=not args.no_crop,
                debug=args.debug,
            )
            print(f"\nĐã lưu ảnh: {args.output}")
            print(f"Tìm thấy viền và crop: {'Có' if cropped else 'Không (giữ nguyên ảnh gốc)'}")
            print(f"Nghi bị mờ: {'Có' if quality.is_blurry else 'Không'}")
            print(f"Nghi bị nát/rách: {'Có' if quality.is_damaged else 'Không'}")
            if debug_path is not None:
                print(f"Ảnh debug: {debug_path}")

        else:
            parser.error("Định dạng file đầu vào không được hỗ trợ. Vui lòng chọn PDF, ảnh, hoặc thư mục.")

    except ValueError as error:
        parser.exit(1, f"Lỗi: {error}\n")


if __name__ == "__main__":
    main()
