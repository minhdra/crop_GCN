"""Crop and enhance image-based PDF pages into a scanner-like PDF."""

from __future__ import annotations
from PIL import Image, ImageOps

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import fitz  # PyMuPDF

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

    # Đánh giá chất lượng trên ảnh gốc, trước khi crop làm mất tín hiệu viền rách.
    quality = assess_quality(
        image=original_image,
        min_area_ratio=min_area_ratio,
        blur_threshold=blur_threshold,
        solidity_threshold=solidity_threshold,
    )

    if crop:
        corners = find_document_corners(
            image=original_image,
            min_area_ratio=min_area_ratio,
        )

        if corners is not None:
            image = four_point_crop(original_image, corners)
            cropped = True

    debug_image = (
        generate_debug_image(original_image, min_area_ratio, corners, quality)
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

def _paper_contours(
    image: np.ndarray, min_area_ratio: float
) -> tuple[list[np.ndarray], np.ndarray, float] | None:
    """Tách vùng giấy khỏi nền và trả về các contour hợp lệ (đủ lớn, không
    chạm cả 4 cạnh ảnh), sắp xếp giảm dần theo diện tích, cùng ảnh đã resize
    và tỉ lệ scale đã áp dụng."""
    height, width = image.shape[:2]
    scale = min(1.0, 1400 / max(height, width))
    resized = cv2.resize(image, None, fx=scale, fy=scale) if scale < 1 else image.copy()

    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (9, 9), 0)  # Blur mạnh hơn để giảm nhiễu

    # 1. Dùng Otsu threshold để tự động tìm ngưỡng tách giấy trắng khỏi nền
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 2. Nếu tờ giấy sáng hơn nền, dùng threshold thường; nếu tối hơn, đảo ngược
    # Kiểm tra: lấy mẫu 4 góc ảnh, nếu trung bình < 128 thì nền tối -> giữ nguyên
    # Nếu trung bình > 128 thì nền sáng -> đảo ngược threshold
    corner_brightness = np.mean([
        gray[0, 0], gray[0, -1], gray[-1, 0], gray[-1, -1]
    ])
    if corner_brightness > 128:
        # Nền sáng, giấy tối -> đảo ngược
        thresh = cv2.bitwise_not(thresh)

    # 3. Đóng các lỗ hổng nhỏ trong vùng giấy (do chữ, hình ảnh)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

    # 4. Tìm contour của vùng sáng (tờ giấy)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        return None

    # Sắp xếp theo diện tích giảm dần
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    minimum_area = resized.shape[0] * resized.shape[1] * min_area_ratio
    margin = 5
    valid_contours = []

    for contour in contours:
        if cv2.contourArea(contour) < minimum_area:
            continue

        # Loại bỏ contour chạm cả 4 cạnh (toàn bộ ảnh)
        x, y, w, h = cv2.boundingRect(contour)
        touches_all_borders = (
            x <= margin and
            y <= margin and
            x + w >= resized.shape[1] - 1 - margin and
            y + h >= resized.shape[0] - 1 - margin
        )
        if touches_all_borders:
            continue

        valid_contours.append(contour)

    if not valid_contours:
        return None

    return valid_contours, resized, scale


def find_document_corners(image: np.ndarray, min_area_ratio: float) -> np.ndarray | None:
    """Tìm 4 góc tài liệu bằng cách tách vùng sáng (giấy trắng) khỏi nền tối."""
    prepared = _paper_contours(image, min_area_ratio)
    if prepared is None:
        return None

    contours, _, scale = prepared

    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        approximation = cv2.approxPolyDP(contour, 0.02 * perimeter, True)

        if len(approximation) == 4 and cv2.isContourConvex(approximation):
            return approximation.astype(np.float32) / scale

    return None


def assess_quality(
    image: np.ndarray,
    min_area_ratio: float,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: float = DEFAULT_SOLIDITY_THRESHOLD,
) -> QualityIssues:
    """Đánh giá ảnh bị mờ (độ nét thấp) và tài liệu nghi bị nát/rách (viền
    lồi lõm bất thường). Phải chạy trên ảnh gốc trước khi crop, vì sau khi
    crop bằng four_point_crop, tài liệu luôn là một hình chữ nhật sạch nên
    sẽ mất tín hiệu viền rách."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    is_blurry = blur_score < blur_threshold

    solidity: float | None = None
    prepared = _paper_contours(image, min_area_ratio)
    if prepared is not None:
        contours, _, _ = prepared
        largest_contour = contours[0]
        hull_area = cv2.contourArea(cv2.convexHull(largest_contour))
        if hull_area > 0:
            solidity = cv2.contourArea(largest_contour) / hull_area

    is_damaged = solidity is not None and solidity < solidity_threshold

    return QualityIssues(
        is_blurry=is_blurry,
        is_damaged=is_damaged,
        blur_score=blur_score,
        solidity=solidity,
    )


def generate_debug_image(
    image: np.ndarray,
    min_area_ratio: float,
    corners: np.ndarray | None,
    quality: QualityIssues,
) -> np.ndarray:
    """Vẽ overlay debug lên ảnh gốc (trước crop) để soi vì sao crop/chất
    lượng lại ra kết quả như vậy:
        - Các contour vùng giấy tìm được (viền xanh dương).
        - 4 góc đã chọn để crop, nếu có (viền xanh lá + chấm đỏ ở góc).
        - Chỉ số blur_score/solidity và cờ mờ/nát dạng chữ ở góc trên trái.
    """
    debug_image = image.copy()
    scale_factor = max(image.shape[0], image.shape[1]) / 1500

    prepared = _paper_contours(image, min_area_ratio)
    if prepared is not None:
        contours, _, contour_scale = prepared
        for contour in contours:
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
    denoised = cv2.fastNlMeansDenoising(gray, None, 7, 7, 21)
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
) -> tuple[bool, QualityIssues, Path | None]:
    """Xử lý và lưu một ảnh đầu vào.

    Trả về True nếu đã tìm thấy viền và crop, đánh giá chất lượng (mờ / nghi
    bị nát) của ảnh gốc, và đường dẫn ảnh debug (contour + góc crop) nếu
    debug=True, ngược lại None.
    """
    # Sử dụng load_image_with_exif thay vì cv2.imread
    image = load_image_with_exif(input_path)
    original_image = image

    # Đánh giá chất lượng trên ảnh gốc, trước khi crop làm mất tín hiệu viền rách.
    quality = assess_quality(
        image=original_image,
        min_area_ratio=min_area_ratio,
        blur_threshold=blur_threshold,
        solidity_threshold=solidity_threshold,
    )

    cropped = False
    corners = None
    if crop:
        corners = find_document_corners(original_image, min_area_ratio)
        if corners is not None:
            image = four_point_crop(original_image, corners)
            cropped = True
        # Nếu không tìm thấy viền, giữ nguyên ảnh gốc và chỉ làm rõ

    debug_path: Path | None = None
    if debug:
        debug_image = generate_debug_image(original_image, min_area_ratio, corners, quality)
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
