import numpy as np

from scripts.sla import calibration_patterns as cp


def test_rect_pattern_fills_only_the_rectangle():
    frame = cp.rect_pattern(100, 60, (10, 20, 30, 15))
    assert frame.shape == (60, 100, 4)
    white = frame[..., 0] == 255
    assert white.sum() == 30 * 15
    assert white[20, 10] and white[34, 39]
    assert not white[19, 10] and not white[20, 40]
    assert (frame[..., 3] == 255).all()


def test_rect_pattern_clips_to_the_frame():
    frame = cp.rect_pattern(100, 60, (-10, -10, 30, 30))
    assert (frame[..., 0] == 255).sum() == 20 * 20
    assert (frame[..., 0] == 255).sum() == 400


def test_rect_pattern_fully_outside_is_black():
    frame = cp.rect_pattern(100, 60, (200, 200, 10, 10))
    assert not frame[..., :3].any()


def test_focus_pattern_marks_corners_and_edges():
    frame = cp.focus_pattern(320, 180)
    assert frame.shape == (180, 320, 4)
    for y, x in ((0, 0), (0, 319), (179, 0), (179, 319)):
        assert frame[y, x, 0] == 255
    assert (frame[0, :, 0] == 255).all()
    assert frame[5, 160, 0] == 255 and frame[175, 160, 0] == 255
    assert frame[..., 3].min() == 255
    # Fine detail, not just solid regions.
    assert np.unique(frame[..., 0]).tolist() == [0, 255]
