"""Phát hiện 4 góc tài liệu trong ảnh chụp và cắt thẳng theo phối cảnh.

Dùng cho ảnh chụp trực tiếp (điện thoại, camera) chứ không phải PDF: tách
tài liệu khỏi nền bằng GrabCut (phân vùng theo màu/tiền cảnh), tìm tứ giác
bao quanh vùng tách được, rồi áp phép biến đổi phối cảnh (perspective
transform) để tài liệu trở thành hình chữ nhật thẳng — giống hiệu ứng "crop
kiểu scan" của các app quét tài liệu.

Dùng GrabCut thay vì dò cạnh (Canny) vì tài liệu không phẳng (sổ/sách đang
mở, có nếp gấp) khiến viền ngoài không tạo thành 1 đường cạnh liền mạch —
Canny dò theo gradient sáng/tối nên bị vỡ contour thành nhiều mảnh tại chỗ
gấp. GrabCut phân loại từng pixel là tiền cảnh/nền theo phân bố màu, không
phụ thuộc cạnh có liền mạch hay không, nên vẫn ra 1 vùng tài liệu liền khối.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps


def load_image_with_exif(image_path: Path) -> np.ndarray:
    """Đọc ảnh và tự động xoay theo thẻ EXIF (sửa lỗi ảnh mobile bị xoay ngang)."""
    pil_image = Image.open(image_path)
    pil_image = ImageOps.exif_transpose(pil_image)

    if pil_image.mode != "RGB":
        pil_image = pil_image.convert("RGB")

    return cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)


def order_corners(corners: np.ndarray) -> np.ndarray:
    """Sắp xếp 4 điểm theo thứ tự trên-trái, trên-phải, dưới-phải, dưới-trái."""
    points = corners.reshape(4, 2).astype(np.float32)
    ordered = np.empty((4, 2), dtype=np.float32)
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[np.argmin(sums)]
    ordered[2] = points[np.argmax(sums)]
    ordered[1] = points[np.argmin(differences)]
    ordered[3] = points[np.argmax(differences)]
    return ordered


def _grabcut_foreground_contour(image: np.ndarray, margin_ratio: float) -> np.ndarray | None:
    """Tách tài liệu khỏi nền bằng GrabCut, trả về contour lớn nhất tách được
    (toạ độ theo khung hình đã resize, chưa quy đổi về ảnh gốc)."""
    frame_h, frame_w = image.shape[:2]
    margin_x = int(frame_w * margin_ratio)
    margin_y = int(frame_h * margin_ratio)
    rect = (margin_x, margin_y, frame_w - 2 * margin_x, frame_h - 2 * margin_y)
    if rect[2] <= 0 or rect[3] <= 0:
        return None

    mask = np.zeros((frame_h, frame_w), np.uint8)
    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(image, mask, rect, bgd_model, fgd_model, 5, cv2.GC_INIT_WITH_RECT)
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
    return max(contours, key=cv2.contourArea)


def find_document_corners(
    image: np.ndarray, margin_ratio: float = 0.05
) -> np.ndarray | None:
    """Tìm tứ giác bao quanh tài liệu trong ảnh (đã tách khỏi nền bằng GrabCut)."""
    height, width = image.shape[:2]
    scale = min(1.0, 1000 / max(height, width))
    resized = cv2.resize(image, None, fx=scale, fy=scale) if scale < 1 else image.copy()

    contour = _grabcut_foreground_contour(resized, margin_ratio)
    if contour is None:
        return None

    # Xấp xỉ contour về đúng 4 điểm; nếu không khớp thẳng (thường do viền
    # cong/gấp), dùng hình chữ nhật xoay nhỏ nhất bao quanh làm xấp xỉ.
    perimeter = cv2.arcLength(contour, True)
    approximation = None
    for epsilon_ratio in (0.01, 0.02, 0.03, 0.05, 0.08):
        candidate = cv2.approxPolyDP(contour, epsilon_ratio * perimeter, True)
        if len(candidate) == 4 and cv2.isContourConvex(candidate):
            approximation = candidate
            break

    if approximation is None:
        rect = cv2.minAreaRect(contour)
        approximation = cv2.boxPoints(rect)

    return approximation.reshape(4, 2).astype(np.float32) / scale


def four_point_crop(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Áp phép biến đổi phối cảnh để tài liệu trở thành hình chữ nhật thẳng."""
    top_left, top_right, bottom_right, bottom_left = order_corners(corners)
    output_width = round(
        max(np.linalg.norm(bottom_right - bottom_left), np.linalg.norm(top_right - top_left))
    )
    output_height = round(
        max(np.linalg.norm(top_right - bottom_right), np.linalg.norm(top_left - bottom_left))
    )
    if output_width < 2 or output_height < 2:
        raise ValueError("Vùng tìm được quá nhỏ để crop.")

    destination = np.array(
        [[0, 0], [output_width - 1, 0], [output_width - 1, output_height - 1], [0, output_height - 1]],
        dtype=np.float32,
    )
    transform = cv2.getPerspectiveTransform(
        np.array([top_left, top_right, bottom_right, bottom_left]), destination
    )
    return cv2.warpPerspective(image, transform, (output_width, output_height))


def detect_and_crop(
    input_path: Path, output_path: Path, margin_ratio: float = 0.05
) -> bool:
    """Phát hiện tài liệu trong 1 ảnh, cắt thẳng theo phối cảnh và lưu ra file.

    Trả về True nếu tìm thấy 4 góc và đã crop; False nếu giữ nguyên ảnh gốc.
    """
    if not input_path.is_file():
        raise ValueError(f"Không tìm thấy file ảnh: {input_path}")

    image = load_image_with_exif(input_path)
    corners = find_document_corners(image, margin_ratio=margin_ratio)

    cropped = False
    if corners is not None:
        image = four_point_crop(image, corners)
        cropped = True

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), image):
        raise ValueError(f"Không thể ghi ảnh: {output_path}")

    return cropped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phát hiện tài liệu trong ảnh chụp bằng GrabCut và cắt thẳng theo phối cảnh."
    )
    parser.add_argument("input", type=Path, help="Đường dẫn ảnh đầu vào")
    parser.add_argument("output", type=Path, help="Đường dẫn ảnh đầu ra")
    parser.add_argument(
        "--margin-ratio",
        type=float,
        default=0.05,
        help="Tỉ lệ margin ban đầu cho rect khởi tạo GrabCut (0.02-0.15 tuỳ ảnh)",
    )
    args = parser.parse_args()

    if not 0 < args.margin_ratio < 0.4:
        parser.error("--margin-ratio phải nằm trong khoảng 0 đến 0.4")

    try:
        cropped = detect_and_crop(args.input, args.output, margin_ratio=args.margin_ratio)
    except ValueError as error:
        parser.exit(1, f"Lỗi: {error}\n")
        return

    print(f"Đã lưu ảnh: {args.output}")
    print(f"Tìm thấy viền và crop: {'Có' if cropped else 'Không (giữ nguyên ảnh gốc)'}")


if __name__ == "__main__":
    main()
