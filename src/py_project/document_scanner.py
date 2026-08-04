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

    # 4. Tìm contour của vùng sáng (tờ giấy) qua threshold
    contours_thresh, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # 5. Bổ sung contour theo biên cạnh (Canny). Threshold một ngưỡng dễ thất
    # bại khi ánh sáng không đều / có bóng đổ / nền gần cùng độ sáng với
    # giấy (ảnh chụp điện thoại thực tế); biên cạnh bám theo độ tương phản
    # cục bộ nên bền hơn trong các trường hợp đó.
    #
    # Không có một ngưỡng Canny duy nhất phù hợp mọi ảnh: nền có vân/kết cấu
    # (mặt bàn gỗ, vải...) cần ngưỡng cao để không bắt nhiễu vân nền (vân gỗ
    # dày đặc dễ nối liền viền tài liệu với nền thành một khối duy nhất khi
    # ngưỡng thấp); còn ảnh thiếu sáng/độ tương phản thấp cần tăng tương
    # phản cục bộ (CLAHE) và ngưỡng thấp mới bắt được viền yếu. Nên thử
    # nhiều tổ hợp và để bước lọc phía sau (diện tích, chạm cạnh) loại bỏ
    # ứng viên sai thay vì đoán một ngưỡng "đúng" duy nhất.
    blurred_gray = cv2.GaussianBlur(cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY), (5, 5), 0)
    contrast_boosted = cv2.GaussianBlur(
        cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(
            cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        ),
        (5, 5),
        0,
    )
    median_intensity = float(np.median(contrast_boosted))
    canny_attempts = [
        (blurred_gray, 75, 175),
        (blurred_gray, 120, 240),
        (blurred_gray, 150, 255),
        (
            contrast_boosted,
            int(max(0, 0.67 * median_intensity)),
            int(min(255, 1.33 * median_intensity)),
        ),
    ]

    # Kernel giãn nở lớn để nối liền các đoạn biên bị đứt quãng (do nhiễu
    # JPEG, vân nền, hoặc vùng lóa sáng) thành một đường viền khép kín duy
    # nhất - nếu không, findContours sẽ trả về nhiều mảnh biên rời rạc, nhỏ
    # hơn nhiều so với diện tích tài liệu thật.
    edge_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 21))
    contours_edges: list[np.ndarray] = []
    for source, lower_bound, upper_bound in canny_attempts:
        edges = cv2.dilate(cv2.Canny(source, lower_bound, upper_bound), edge_kernel)
        found, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours_edges.extend(found)

    # 6. Luôn bổ sung ứng viên từ GrabCut. Threshold/Canny đều dựa trên so
    # sánh độ sáng - thất bại (hoặc chỉ bắt được một phần) khi tài liệu
    # nhiều màu (bìa màu + trang trắng của một cuốn sổ/giấy chứng nhận, loại
    # tài liệu chính ở đây) hoặc nền có màu/kết cấu na ná tài liệu. GrabCut
    # tự học phân phối màu foreground/background thay vì dùng một ngưỡng cố
    # định nên bắt được toàn bộ bìa+trang thay vì chỉ phần dễ nhận nhất; đổi
    # lại chậm hơn nhiều lần (hàng trăm ms) nên vì đây là loại tài liệu
    # chính nên chấp nhận chạy mỗi lần thay vì đoán khi nào cần.
    #
    # Lọc trước các contour threshold/Canny hợp lệ (đã qua kiểm tra diện
    # tích/chạm cạnh) để mồi cho GrabCut làm "chắc chắn là tiền cảnh" -
    # giúp mô hình màu hội tụ nhanh hơn nhiều (ít vòng lặp hơn mà vẫn ra
    # cùng kết quả). Chỉ mồi bằng contour ĐÃ LỌC (không phải toàn bộ danh
    # sách thô), vì contour thô đôi khi dính nhầm nền (như vùng lóa sáng) -
    # mồi bằng vùng sai sẽ khiến GrabCut khóa cứng luôn phần sai đó.
    fast_valid_contours, _ = _filter_candidate_contours(
        list(contours_thresh) + contours_edges, resized.shape, min_area_ratio
    )
    grabcut_contours = _grabcut_contours(
        resized, fast_valid_contours[0] if fast_valid_contours else None
    )

    # GrabCut lọc riêng với ngưỡng "đặc hình chữ nhật" lỏng hơn (xem lý do
    # trong _filter_candidate_contours) - không gộp chung vào contours_thresh
    # + contours_edges trước khi lọc như cũ, vì ngưỡng chặt 1.2 áp cho cả 3
    # nguồn sẽ loại đúng contour GrabCut cần dùng để bắt được toàn bộ tài
    # liệu nhiều màu (bìa + trang trắng).
    thresh_edge_contours, thresh_edge_frame_filling = _filter_candidate_contours(
        list(contours_thresh) + contours_edges, resized.shape, min_area_ratio
    )
    grabcut_valid_contours, grabcut_frame_filling = _filter_candidate_contours(
        grabcut_contours, resized.shape, min_area_ratio, rectangularity_ratio=1.5
    )
    valid_contours = thresh_edge_contours + grabcut_valid_contours
    valid_contours.sort(key=cv2.contourArea, reverse=True)

    # Có một contour đủ lớn/đủ đặc hình chữ nhật nhưng bị loại chỉ vì chạm
    # quá nhiều cạnh ảnh (hoặc gần phủ kín khung hình) - đây là dấu hiệu tài
    # liệu thật đã chiếm gần trọn khung hình (ảnh chụp/scan cắt sát mép sẵn,
    # không còn nền xung quanh để so sánh tương phản) chứ không phải nền dính
    # vào tài liệu. Trong tình huống đó, các contour nhỏ hơn tìm được (nếu
    # có) rất dễ chỉ là một chi tiết in bên trong tài liệu (như viền bảng)
    # thay vì mép giấy thật - không đáng tin để crop, thà bỏ qua giữ nguyên
    # ảnh gốc còn hơn cắt nhầm mất nội dung thật ở phần chạm cạnh.
    if thresh_edge_frame_filling or grabcut_frame_filling:
        return None

    if not valid_contours:
        return None

    return valid_contours, resized, scale


def _filter_candidate_contours(
    contours: list[np.ndarray],
    image_shape: tuple[int, ...],
    min_area_ratio: float,
    rectangularity_ratio: float = 1.2,
) -> tuple[list[np.ndarray], bool]:
    """Lọc và sắp xếp giảm dần theo diện tích các contour đủ lớn và không
    chạm từ 3 cạnh ảnh trở lên.

    `rectangularity_ratio`: ngưỡng tỉ lệ minAreaRect/diện tích contour tối đa
    được coi là "đặc hình chữ nhật" (xem giải thích chi tiết bên dưới). Mặc
    định 1.2 phù hợp với contour từ threshold/Canny (biên khá sắc nét). Biên
    GrabCut vốn lượn sóng nhẹ hơn nhiều so với biên nhị phân/Canny (viền theo
    phân bố màu học được thay vì so sánh độ sáng cứng), và khi tài liệu có 2
    vùng màu khác nhau kề nhau (bìa màu + trang trắng, ngăn cách bởi khoảng
    hở/gáy sách) tỉ lệ này càng cao hơn nữa dù không phải nhiễu - nên gọi hàm
    này với `rectangularity_ratio` lỏng hơn (~1.5) riêng cho contour nguồn
    GrabCut thay vì áp chung 1.2 cho mọi nguồn.

    Trả về thêm cờ thứ hai: True nếu có contour đủ diện tích tối thiểu và đủ
    đặc hình chữ nhật, nhưng bị loại chỉ vì chạm quá nhiều cạnh ảnh hoặc gần
    phủ kín khung hình - xem chú thích ở nơi gọi hàm này trong
    `_paper_contours`."""
    if not contours:
        return [], False

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
    frame_filling_candidate_found = False

    for contour in contours:
        contour_area = cv2.contourArea(contour)
        if contour_area < minimum_area:
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
        is_rectangular = rect_area <= contour_area * rectangularity_ratio

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

        if contour_area > maximum_area or touched_borders >= 3:
            if is_rectangular:
                frame_filling_candidate_found = True
            continue

        if not is_rectangular:
            continue

        valid_contours.append(contour)

    return valid_contours, frame_filling_candidate_found


def _grabcut_contours(
    resized: np.ndarray, seed_contour: np.ndarray | None = None
) -> list[np.ndarray]:
    """Tách vùng tiền cảnh (tài liệu) khỏi hậu cảnh bằng GrabCut. Mặc định
    coi lề ngoài 3% mỗi cạnh là nền, phần còn lại là "có thể tiền cảnh"
    (giả định tài liệu nằm gần giữa khung hình) - đủ lỏng để GrabCut tự học
    và mở rộng ra các phần màu khác nhau của cùng một tài liệu (như bìa màu
    + trang trắng của một cuốn sổ).

    Nếu có `seed_contour` (một contour đã qua lọc diện tích/chạm cạnh từ
    threshold/Canny, không phải contour thô), đánh dấu thêm phần lõi của nó
    là "chắc chắn tiền cảnh" để mồi cho mô hình màu - giúp hội tụ nhanh hơn
    nhiều (ít vòng lặp hơn mà vẫn ra cùng kết quả). Chỉ dùng contour đã lọc
    làm mồi, vì contour thô đôi khi dính nhầm nền (ví dụ vùng lóa sáng) -
    mồi bằng vùng sai sẽ khiến GrabCut khóa cứng luôn phần sai đó.

    Chạy trên ảnh thu nhỏ hơn nữa vì GrabCut khá chậm (hàng trăm ms) - và
    với ảnh nền vân dày đặc (gỗ nhiều vân...), bước phân cụm màu ban đầu có
    thể chậm hơn hẳn mức bình thường (quan sát được tới hơn 1s ở 600px),
    không liên quan tới số vòng lặp; hạ kích thước là cách giảm rủi ro đó."""
    height, width = resized.shape[:2]
    grabcut_scale = min(1.0, 450 / max(height, width))
    small = (
        cv2.resize(resized, None, fx=grabcut_scale, fy=grabcut_scale)
        if grabcut_scale < 1
        else resized
    )
    small_height, small_width = small.shape[:2]

    # GC_BGD (nền chắc chắn) chứ không phải GC_PR_BGD (nền "có thể") - viền
    # ngoài phải là ràng buộc cứng giống cách GC_INIT_WITH_RECT coi phần
    # ngoài rect là nền chắc chắn. Dùng GC_PR_BGD (mềm) khiến GrabCut có thể
    # lấn cả viền nền mỏng vào tài liệu khi tài liệu chiếm gần hết khung
    # hình (viền nền còn lại quá ít để mô hình màu học tin cậy).
    mask = np.full((small_height, small_width), cv2.GC_BGD, np.uint8)
    margin_y = max(1, round(small_height * 0.03))
    margin_x = max(1, round(small_width * 0.03))
    mask[margin_y : small_height - margin_y, margin_x : small_width - margin_x] = cv2.GC_PR_FGD

    if seed_contour is not None:
        seed_mask = np.zeros((small_height, small_width), np.uint8)
        scaled_seed = (seed_contour * grabcut_scale).astype(np.int32)
        cv2.drawContours(seed_mask, [scaled_seed], -1, 255, -1)
        seed_mask = cv2.erode(seed_mask, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
        mask[seed_mask > 0] = cv2.GC_FGD

    background_model = np.zeros((1, 65), np.float64)
    foreground_model = np.zeros((1, 65), np.float64)

    try:
        cv2.grabCut(
            small, mask, None, background_model, foreground_model, 1, cv2.GC_INIT_WITH_MASK
        )
    except cv2.error:
        return []

    foreground = np.where(
        (mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0
    ).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    foreground = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(foreground, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if grabcut_scale < 1:
        contours = [(contour / grabcut_scale).astype(np.float32) for contour in contours]

    return list(contours)


def _approximate_quad(contour: np.ndarray) -> np.ndarray | None:
    """Xấp xỉ contour thành đa giác lồi 4 điểm. Thử nhiều mức epsilon từ chặt
    đến lỏng vì viền giấy thực tế (bo tròn nhẹ, mờ, có bóng) thường không xẹp
    gọn về đúng 4 góc ngay ở epsilon đầu tiên.

    Với contour có phần khuyết ở giữa (ví dụ 2 vùng tài liệu nối bởi gáy
    sách), approxPolyDP ép về đúng 4 điểm đôi khi vẫn ra một tứ giác lồi
    "hợp lệ" về mặt hình học nhưng lại là một hình cánh diều/thang méo cắt
    chéo qua phần khuyết đó, thay vì đi qua đúng 4 góc thật - diện tích có
    thể vẫn gần đúng dù các điểm hoàn toàn sai vị trí. Vì vậy chỉ nhận một
    xấp xỉ nếu bản thân nó cũng đủ "đặc hình chữ nhật" (minAreaRect của
    chính 4 điểm đó gần khớp diện tích của chúng) - một tứ giác lồi bị méo
    thành cánh diều sẽ có minAreaRect phình to rõ rệt so với diện tích thật
    của nó, giống hệt dấu hiệu dùng để lọc contour nhiễu ở nơi khác."""
    perimeter = cv2.arcLength(contour, True)
    for epsilon_ratio in (0.01, 0.02, 0.03, 0.04, 0.05, 0.08):
        approximation = cv2.approxPolyDP(contour, epsilon_ratio * perimeter, True)
        if len(approximation) != 4 or not cv2.isContourConvex(approximation):
            continue
        quad_area = cv2.contourArea(approximation)
        rect_area = cv2.contourArea(cv2.boxPoints(cv2.minAreaRect(approximation)).astype(np.float32))
        if quad_area > 0 and rect_area <= quad_area * 1.2:
            return approximation.reshape(4, 2).astype(np.float32)
    return None


def _band_mask(
    shape: tuple[int, ...], point_a: np.ndarray, point_b: np.ndarray, width: int, extend: float = 0.15
) -> np.ndarray:
    """Tạo mask hình chữ nhật dài (dải băng) bao quanh đoạn thẳng point_a-
    point_b, rộng `width` mỗi bên và kéo dài thêm `extend` tỉ lệ chiều dài ở
    hai đầu - dùng để giới hạn vùng tìm biên chỉ quanh một cạnh cụ thể của
    tứ giác, tránh bắt nhầm biên của cạnh khác hoặc hoa văn ở xa."""
    direction = point_b - point_a
    length = np.linalg.norm(direction)
    if length < 1e-6:
        return np.zeros(shape[:2], np.uint8)
    unit = direction / length
    normal = np.array([-unit[1], unit[0]])
    point_a_ext = point_a - unit * length * extend
    point_b_ext = point_b + unit * length * extend
    polygon = np.array(
        [
            point_a_ext + normal * width,
            point_b_ext + normal * width,
            point_b_ext - normal * width,
            point_a_ext - normal * width,
        ],
        dtype=np.int32,
    )
    mask = np.zeros(shape[:2], np.uint8)
    cv2.fillPoly(mask, [polygon], 255)
    return mask


def _best_line_near(
    edge_map: np.ndarray, point_a: np.ndarray, point_b: np.ndarray
) -> tuple[np.ndarray, np.ndarray] | None:
    """Tìm đoạn thẳng Hough dài nhất, gần song song với point_a-point_b
    (lệch góc dưới 8°), trong ảnh biên đã giới hạn theo dải quanh cạnh này."""
    segment_length = np.linalg.norm(point_b - point_a)
    if segment_length < 1:
        return None
    original_angle = np.degrees(np.arctan2(point_b[1] - point_a[1], point_b[0] - point_a[0])) % 180
    lines = cv2.HoughLinesP(
        edge_map, 1, np.pi / 360, threshold=15,
        minLineLength=segment_length * 0.2, maxLineGap=40,
    )
    if lines is None:
        return None

    best: tuple[float, np.ndarray, np.ndarray] | None = None
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1)) % 180
        angle_diff = min(abs(angle - original_angle), 180 - abs(angle - original_angle))
        if angle_diff > 8:
            continue
        length = np.hypot(x2 - x1, y2 - y1)
        if best is None or length > best[0]:
            best = (length, np.array([x1, y1], dtype=np.float64), np.array([x2, y2], dtype=np.float64))

    return None if best is None else (best[1], best[2])


def _line_intersection(
    line_a: tuple[np.ndarray, np.ndarray], line_b: tuple[np.ndarray, np.ndarray]
) -> np.ndarray | None:
    """Giao điểm của 2 đường thẳng (mở rộng vô hạn) đi qua line_a, line_b."""
    point_a1, point_a2 = line_a
    point_b1, point_b2 = line_b
    direction_a = point_a2 - point_a1
    direction_b = point_b2 - point_b1
    denominator = direction_a[0] * direction_b[1] - direction_a[1] * direction_b[0]
    if abs(denominator) < 1e-6:
        return None
    t = (
        (point_b1[0] - point_a1[0]) * direction_b[1] - (point_b1[1] - point_a1[1]) * direction_b[0]
    ) / denominator
    return point_a1 + t * direction_a


def _snap_quad_to_edges(image: np.ndarray, quad: np.ndarray, band_width: int = 30) -> np.ndarray:
    """Tinh chỉnh 4 cạnh của tứ giác bằng cách dò đường thẳng mạnh nhất bám
    sát mỗi cạnh (trong một dải hẹp quanh cạnh đó của ảnh gốc) và kéo cạnh
    về đúng đường viền thật.

    Contour/GrabCut hoạt động trên ảnh đã thu nhỏ và làm mịn (morphology,
    blur) nên biên tìm được có thể lệch vài % so với viền tài liệu thật -
    nhất là khi nền có màu/kết cấu gần giống tài liệu (GrabCut khi đó dễ
    lấn nhẹ ra nền). Bước này chạy trên ảnh gốc (chưa thu nhỏ) để bám biên
    chính xác hơn, giống cách CamScanner tinh chỉnh cạnh sau khi khoanh
    vùng thô.

    Có kiểm tra an toàn: nếu kết quả tinh chỉnh làm tứ giác phình/co bất
    thường hoặc không còn lồi (dấu hiệu bám nhầm đường thẳng khác như chữ,
    hoa văn), giữ nguyên tứ giác gốc thay vì dùng kết quả sai."""
    ordered = order_corners(quad).astype(np.float64)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edge_map = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 50, 150)

    refined_lines: list[tuple[np.ndarray, np.ndarray]] = []
    for i in range(4):
        point_a, point_b = ordered[i], ordered[(i + 1) % 4]
        mask = _band_mask(image.shape, point_a, point_b, band_width)
        band_edges = cv2.bitwise_and(edge_map, mask)
        line = _best_line_near(band_edges, point_a, point_b)
        refined_lines.append(line if line is not None else (point_a, point_b))

    new_corners = []
    for i in range(4):
        corner = _line_intersection(refined_lines[i - 1], refined_lines[i])
        new_corners.append(ordered[i] if corner is None else corner)
    new_corners = np.array(new_corners, dtype=np.float32)

    original_area = cv2.contourArea(ordered.astype(np.float32))
    new_area = cv2.contourArea(new_corners)
    if original_area <= 0 or not (0.75 <= new_area / original_area <= 1.15):
        return quad.astype(np.float32)
    if not cv2.isContourConvex(new_corners.reshape(-1, 1, 2).astype(np.int32)):
        return quad.astype(np.float32)

    return new_corners


def _expand_quad(quad: np.ndarray, image_shape: tuple[int, ...], margin_ratio: float = 0.015) -> np.ndarray:
    """Nới tứ giác ra ngoài một chút (từ tâm) theo `margin_ratio`, chừa viền
    an toàn quanh tài liệu thay vì cắt sát đúng mép đã dò được - phòng
    trường hợp bám biên hơi lẹm vào nội dung. Giới hạn lại trong biên ảnh
    nếu tài liệu đã nằm sát cạnh ảnh."""
    center = quad.mean(axis=0)
    expanded = center + (quad - center) * (1 + margin_ratio)
    height, width = image_shape[:2]
    expanded[:, 0] = np.clip(expanded[:, 0], 0, width - 1)
    expanded[:, 1] = np.clip(expanded[:, 1], 0, height - 1)
    return expanded.astype(np.float32)


def find_document_corners(
    image: np.ndarray,
    min_area_ratio: float,
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = None,
) -> np.ndarray | None:
    """Tìm 4 góc tài liệu. Ưu tiên góc từ contour xấp xỉ đúng thành hình tứ
    giác; nếu viền quá nhiễu/mờ để xấp xỉ gọn, dùng hình chữ nhật xoay nhỏ
    nhất bao quanh contour lớn nhất tìm được làm phương án dự phòng - vẫn
    lật thẳng được tài liệu bị nghiêng thay vì bỏ qua không crop, giống cách
    CamScanner luôn cố đưa ra một khung crop tốt nhất có thể. Sau đó tinh
    chỉnh lại từng cạnh bằng cách bám đường thẳng mạnh nhất gần đó trên ảnh
    gốc, sửa phần dư biên nhỏ còn sót lại từ bước phát hiện contour/GrabCut,
    rồi nới nhẹ tứ giác ra ngoài một chút để chừa viền an toàn quanh tài
    liệu thay vì cắt sát đúng mép.

    `prepared` cho phép truyền lại kết quả `_paper_contours` đã tính sẵn
    (từ assess_quality/generate_debug_image trên cùng ảnh) để khỏi tính lại
    - bước này giờ chạy cả GrabCut nên khá tốn (hàng trăm ms), không nên
    lặp lại 2-3 lần cho mỗi ảnh/trang."""
    if prepared is None:
        prepared = _paper_contours(image, min_area_ratio)
    if prepared is None:
        return None

    contours, _, scale = prepared

    # Luôn bám theo contour lớn nhất (đã qua lọc diện tích/chạm cạnh/độ đặc
    # ở _filter_candidate_contours) thay vì dò tiếp các contour nhỏ hơn khi
    # nó không xấp xỉ gọn thành tứ giác - contour lớn nhất là ứng viên đúng
    # nhất cho toàn bộ tài liệu (ví dụ bìa màu + trang trắng của cùng một
    # cuốn sổ); một contour nhỏ hơn dù xấp xỉ "sạch" hơn (như chỉ vùng viền
    # bảng trên 1 trang) vẫn chỉ là một phần của tài liệu thật, dùng nó sẽ
    # crop hụt mất phần còn lại.
    largest_contour = contours[0]
    candidate = _approximate_quad(largest_contour)
    if candidate is not None:
        quad = candidate / scale
    else:
        box = cv2.boxPoints(cv2.minAreaRect(largest_contour))
        quad = box.astype(np.float32) / scale

    refined = _snap_quad_to_edges(image, quad)
    return _expand_quad(refined, image.shape)


def assess_quality(
    image: np.ndarray,
    min_area_ratio: float,
    blur_threshold: float = DEFAULT_BLUR_THRESHOLD,
    solidity_threshold: float = DEFAULT_SOLIDITY_THRESHOLD,
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = None,
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
    if prepared is None:
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


def generate_debug_image(
    image: np.ndarray,
    min_area_ratio: float,
    corners: np.ndarray | None,
    quality: QualityIssues,
    prepared: tuple[list[np.ndarray], np.ndarray, float] | None = None,
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

    if prepared is None:
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
) -> tuple[bool, QualityIssues, Path | None]:
    """Xử lý và lưu một ảnh đầu vào.

    Trả về True nếu đã tìm thấy viền và crop, đánh giá chất lượng (mờ / nghi
    bị nát) của ảnh gốc, và đường dẫn ảnh debug (contour + góc crop) nếu
    debug=True, ngược lại None.
    """
    # Sử dụng load_image_with_exif thay vì cv2.imread
    image = load_image_with_exif(input_path)
    original_image = image

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
