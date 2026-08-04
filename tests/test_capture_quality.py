"""QC ảnh chụp trực tiếp từ camera (assess_capture_quality) - luồng chụp ảnh
riêng, chặn cứng khác với assess_quality (chỉ cảnh báo) dùng cho upload."""

import numpy as np
import cv2

from py_project.document_scanner import assess_capture_quality


def _make_document_photo(width=1600, height=1200):
    """Ảnh tài liệu "tốt": nền xám đơn sắc, giấy sáng màu ở giữa với vài
    dòng "chữ" giả (đường kẻ đen), đủ nét và đủ độ phân giải."""
    image = np.full((height, width, 3), (90, 90, 90), dtype=np.uint8)
    x0, y0 = int(width * 0.15), int(height * 0.12)
    x1, y1 = int(width * 0.85), int(height * 0.88)
    cv2.rectangle(image, (x0, y0), (x1, y1), (235, 235, 235), -1)
    for i in range(20):
        y = y0 + 40 + i * 28
        if y > y1 - 20:
            break
        cv2.line(image, (x0 + 40, y), (x0 + 300, y), (20, 20, 20), 2)
    return image


def test_good_document_photo_passes():
    result = assess_capture_quality(_make_document_photo())
    assert result.passed is True
    assert result.issues == []


def test_low_resolution_is_rejected():
    small = cv2.resize(_make_document_photo(), (400, 300))
    result = assess_capture_quality(small)
    assert result.passed is False
    assert any(issue.code == "low_resolution" for issue in result.issues)


def test_blurry_photo_is_rejected():
    blurry = cv2.GaussianBlur(_make_document_photo(), (0, 0), 15)
    result = assess_capture_quality(blurry)
    assert result.passed is False
    assert any(issue.code == "blurry" for issue in result.issues)


def test_hand_covering_document_is_rejected():
    image = _make_document_photo()
    height, width = image.shape[:2]
    # Vệt màu da tay (BGR) phủ một phần lớn vùng tài liệu.
    cv2.ellipse(
        image, (int(width * 0.75), int(height * 0.5)), (180, 350), 20, 0, 360,
        (120, 170, 230), -1,
    )
    result = assess_capture_quality(image)
    assert result.passed is False
    assert any(issue.code == "hand_covering" for issue in result.issues)


def test_small_incidental_skin_touch_does_not_trigger_hand_covering():
    # Một chấm nhỏ màu da ở góc (kiểu ngón tay giữ mép giấy) không nên bị
    # chặn - chỉ chặn khi che một phần đáng kể tài liệu.
    image = _make_document_photo()
    height, width = image.shape[:2]
    x0, y0 = int(width * 0.15), int(height * 0.12)
    cv2.circle(image, (x0 + 15, y0 + 15), 12, (120, 170, 230), -1)
    result = assess_capture_quality(image)
    assert not any(issue.code == "hand_covering" for issue in result.issues)


def test_no_clear_document_in_frame_is_rejected():
    rng = np.random.default_rng(0)
    noise_image = rng.integers(0, 255, size=(1200, 1600, 3), dtype=np.uint8)
    result = assess_capture_quality(noise_image)
    assert result.passed is False
    assert any(issue.code == "no_document_detected" for issue in result.issues)


def test_thresholds_are_tunable():
    # Ngưỡng skin_coverage_threshold cực thấp phải khiến ngay cả tài liệu
    # sạch (skin_coverage_ratio == 0) cũng bị chặn do 0 > ngưỡng âm.
    result = assess_capture_quality(
        _make_document_photo(), skin_coverage_threshold=-1.0
    )
    assert result.passed is False
    assert any(issue.code == "hand_covering" for issue in result.issues)
