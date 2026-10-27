from argparse import ArgumentTypeError
from unittest.mock import MagicMock, Mock, PropertyMock, patch

import pytest

from vcsi.vcsi import (Color, Grid, TimestampPosition, cleanup,
                       compute_timestamp_position, draw_metadata,
                       max_line_length, mxn_type, save_image)


def test_grid_default():
    test_grid = mxn_type('4x4')
    assert test_grid.x == 4
    assert test_grid.y == 4


def test_grid_equality():
    assert Grid(4, 4) == Grid(4, 4)
    assert Grid(4, 4) != Grid(3, 4)


def test_grid_columns_integer():
    pytest.raises(ArgumentTypeError, mxn_type, 'ax4')
    pytest.raises(ArgumentTypeError, mxn_type, '4.1x4')


def test_grid_columns_positive():
    pytest.raises(ArgumentTypeError, mxn_type, '-1x4')
    pytest.raises(ArgumentTypeError, mxn_type, '0x4')


def test_grid_rows_integer():
    pytest.raises(ArgumentTypeError, mxn_type, '4xa')
    pytest.raises(ArgumentTypeError, mxn_type, '4x4.1')


def test_grid_rows_positive():
    pytest.raises(ArgumentTypeError, mxn_type, '4x-1')
    pytest.raises(ArgumentTypeError, mxn_type, '4x0')


def test_grid_format():
    pytest.raises(ArgumentTypeError, mxn_type, '')
    pytest.raises(ArgumentTypeError, mxn_type, '4xx4')
    pytest.raises(ArgumentTypeError, mxn_type, '4x1x4')
    pytest.raises(ArgumentTypeError, mxn_type, '4')


@patch("vcsi.vcsi.os")
def test_cleanup(mocked_os):
    mocked_os.unlink.side_effect = lambda x: True
    frames = [Mock()]
    frames[0].filename = "frame1"
    cleanup(frames)

    mocked_os.unlink.assert_called_once_with("frame1")


@patch("vcsi.vcsi.Image")
def test_save_image(mocked_Image):
    args = PropertyMock()
    output_path = "whatever"
    assert save_image(args, mocked_Image, output_path) is True

    mocked_Image.convert().save.assert_called_once()


def test_compute_timestamp_position():
    args = PropertyMock()
    args.timestamp_horizontal_margin = 10
    args.timestamp_vertical_margin = 10
    w, h = 1000, 500
    text_size = (10, 10)
    desired_size = (20, 20)
    rectangle_hpadding, rectangle_vpadding = 5, 5

    args.timestamp_position = TimestampPosition.west
    assert (((1010, 500.0), (1030, 520.0)) ==
            compute_timestamp_position(args, w, h, text_size, desired_size,
                                       rectangle_hpadding, rectangle_vpadding))

    args.timestamp_position = TimestampPosition.north
    assert (((1000, 510.0), (1020, 530.0)) ==
            compute_timestamp_position(args, w, h, text_size, desired_size,
                                       rectangle_hpadding, rectangle_vpadding))

    args.timestamp_position = None
    assert (((990, 490), (1010, 510)) ==
            compute_timestamp_position(args, w, h, text_size, desired_size,
                                       rectangle_hpadding, rectangle_vpadding))


def test_max_line_length():
    media_info = PropertyMock()
    metadata_font = PropertyMock()
    metadata_font.getlength.return_value = 40
    header_margin = 100
    width = 1000

    text = "A long line of text"
    assert max_line_length(media_info, metadata_font, header_margin, width, text) == 19

    text = "A long line of text with a few more words"
    assert max_line_length(media_info, metadata_font, header_margin, width, text) == 41

    text = "A really long line of text with a lots more words" * 100
    assert max_line_length(media_info, metadata_font, header_margin, width, text) == 4900

    text = None
    filename = PropertyMock()
    type(media_info).filename = filename
    assert max_line_length(media_info, metadata_font, header_margin, width, text) == 0
    filename.assert_called_once()


def test_draw_metadata():
    args = Mock()
    draw = Mock()
    header_lines = MagicMock()
    draw.text.return_value = 0
    args.metadata_vertical_margin = 0
    header_lines.__iter__ = Mock(return_value=iter(['text1', 'text2']))

    assert draw_metadata(draw, args,
                         header_lines=header_lines,
                         header_line_height=0,
                         start_height=0) == 0
    draw.text.assert_called()


def test_grid():
    assert "2x2" == Grid(2, 2).__str__()
    assert "10x0" == Grid(10, 0).__str__()


def test_color():
    assert "FFFFFFFF" == Color(255, 255, 255, 255).__str__()
