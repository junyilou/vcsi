#!/usr/bin/env python3

"""Create a video contact sheet.
"""

import os
import shutil
import subprocess
import sys
from argparse import ArgumentTypeError

try:
    from subprocess import DEVNULL
except ImportError:
    DEVNULL = open(os.devnull, 'wb')
import argparse
import json
import math
import re
import tempfile
import textwrap
import threading
from collections import namedtuple
from contextlib import contextmanager
from enum import Enum
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

here = os.path.abspath(os.path.dirname(__file__))

with open(os.path.join(here, "VERSION")) as f:
    VERSION = f.readline().strip()
__version__ = VERSION
__author__ = "Nils Amiet"


class Grid(namedtuple('Grid', ['x', 'y'])):
    def __str__(self):
        return "%sx%s" % (self.x, self.y)


class Frame(namedtuple('Frame', ['filename', 'timestamp'])):
    pass


class Color(namedtuple('Color', ['r', 'g', 'b', 'a'])):
    def to_hex(self, component):
        h = hex(component).replace("0x", "").upper()
        return h if len(h) == 2 else "0" + h

    def __str__(self):
        return "".join([self.to_hex(x) for x in [self.r, self.g, self.b, self.a]])


TimestampPosition = Enum('TimestampPosition', "north south east west ne nw se sw center")

DEFAULT_METADATA_FONT_SIZE = 16
DEFAULT_TIMESTAMP_FONT_SIZE = 12

# Defaults
DEFAULT_METADATA_FONT = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
DEFAULT_TIMESTAMP_FONT = "/usr/share/fonts/TTF/DejaVuSans.ttf"
FALLBACK_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/Library/Fonts/Arial Unicode.ttf"]

# Replace defaults on Windows to support unicode/CJK and multiple fallbacks
if os.name == 'nt':
    DEFAULT_METADATA_FONT = "C:/Windows/Fonts/msgothic.ttc"
    DEFAULT_TIMESTAMP_FONT = "C:/Windows/Fonts/msgothic.ttc"
    FALLBACK_FONTS = [
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/Everson Mono.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/arial.ttf"
    ]

DEFAULT_CONTACT_SHEET_WIDTH = 1500
DEFAULT_START_DELAY_PERCENT = 0
DEFAULT_END_DELAY_PERCENT = 0
DEFAULT_GRID_HORIZONTAL_SPACING = 5
DEFAULT_GRID_VERTICAL_SPACING = DEFAULT_GRID_HORIZONTAL_SPACING
DEFAULT_METADATA_POSITION = "top"
DEFAULT_METADATA_FONT_COLOR = "ffffff"
DEFAULT_BACKGROUND_COLOR = "000000"
DEFAULT_TIMESTAMP_FONT_COLOR = "ffffff"
DEFAULT_TIMESTAMP_BACKGROUND_COLOR = "000000aa"
DEFAULT_TIMESTAMP_BORDER_COLOR = "000000"
DEFAULT_TIMESTAMP_BORDER_SIZE = 1
DEFAULT_METADATA_MARGIN = 10
DEFAULT_METADATA_HORIZONTAL_MARGIN = DEFAULT_METADATA_MARGIN
DEFAULT_METADATA_VERTICAL_MARGIN = DEFAULT_METADATA_MARGIN
DEFAULT_CAPTURE_ALPHA = 255
DEFAULT_GRID_SIZE = Grid(3, 20)
DEFAULT_TIMESTAMP_HORIZONTAL_PADDING = 3
DEFAULT_TIMESTAMP_VERTICAL_PADDING = 3
DEFAULT_TIMESTAMP_HORIZONTAL_MARGIN = 5
DEFAULT_TIMESTAMP_VERTICAL_MARGIN = 5
DEFAULT_IMAGE_QUALITY = 100
DEFAULT_IMAGE_FORMAT = "jpg"
DEFAULT_TIMESTAMP_POSITION = TimestampPosition.se

# 4K / 8K pixel counts.  Above these thresholds vcsi is defensive about
# decoding resources: decode jobs are serialized (unless hardware decoding is
# active) and, on very large videos without hardware decoding, ffmpeg is asked
# for the nearest keyframe instead of an exact frame.  An exact seek on an 8K
# HEVC file can take minutes per frame on a small CPU-only server, which used
# to make the whole machine unresponsive when several files were processed at
# once.
HIGH_RES_PIXELS = 3840 * 2160
EIGHT_K_PIXELS = 7680 * 4320
_HEAVY_THREAD_LOCK = threading.Lock()


def _total_memory_bytes():
    """Best-effort total physical memory, without adding a dependency."""
    try:
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_size > 0:
                return pages * page_size
    except (ValueError, OSError, AttributeError):
        pass
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            pass
    return None


_TOTAL_MEMORY = _total_memory_bytes()


def _low_memory_machine():
    """True for small VPSes (the 1 GiB server is the reason this exists)."""
    return _TOTAL_MEMORY is not None and _TOTAL_MEMORY < 2 * 1024 ** 3


@lru_cache(maxsize=1)
def _available_hwaccels():
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            stdin=DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(
        line.strip() for line in result.stdout.splitlines()
        if line.strip() and not line.lower().startswith("hardware"))


@lru_cache(maxsize=1)
def _nice_prefix():
    if os.name != "nt" and shutil.which("nice"):
        return ("nice", "-n", "10")
    return ()


@contextmanager
def _heavy_capture_lock(enabled):
    """Serialize memory-hungry ffmpeg runs across threads and processes."""
    if not enabled:
        yield
        return
    with _HEAVY_THREAD_LOCK:
        if fcntl is None:
            yield
            return
        uid = getattr(os, "getuid", lambda: 0)()
        lock_path = os.path.join(tempfile.gettempdir(), "vcsi-heavy-decode-%s.lock" % uid)
        try:
            lock_file = open(lock_path, "a+")
        except OSError:
            yield
            return
        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError:
                yield
                return
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class Config:
    metadata_font_size = DEFAULT_METADATA_FONT_SIZE
    metadata_font = DEFAULT_METADATA_FONT
    timestamp_font_size = DEFAULT_TIMESTAMP_FONT_SIZE
    timestamp_font = DEFAULT_TIMESTAMP_FONT
    fallback_fonts = FALLBACK_FONTS
    contact_sheet_width = DEFAULT_CONTACT_SHEET_WIDTH
    start_delay_percent = DEFAULT_START_DELAY_PERCENT
    end_delay_percent = DEFAULT_END_DELAY_PERCENT
    grid_horizontal_spacing = DEFAULT_GRID_HORIZONTAL_SPACING
    grid_vertical_spacing = DEFAULT_GRID_VERTICAL_SPACING
    metadata_position = DEFAULT_METADATA_POSITION
    metadata_font_color = DEFAULT_METADATA_FONT_COLOR
    background_color = DEFAULT_BACKGROUND_COLOR
    timestamp_font_color = DEFAULT_TIMESTAMP_FONT_COLOR
    timestamp_background_color = DEFAULT_TIMESTAMP_BACKGROUND_COLOR
    timestamp_border_color = DEFAULT_TIMESTAMP_BORDER_COLOR
    timestamp_border_size = DEFAULT_TIMESTAMP_BORDER_SIZE
    metadata_margin = DEFAULT_METADATA_MARGIN
    metadata_horizontal_margin = DEFAULT_METADATA_HORIZONTAL_MARGIN
    metadata_vertical_margin = DEFAULT_METADATA_VERTICAL_MARGIN
    capture_alpha = DEFAULT_CAPTURE_ALPHA
    grid_size = DEFAULT_GRID_SIZE
    timestamp_horizontal_padding = DEFAULT_TIMESTAMP_HORIZONTAL_PADDING
    timestamp_vertical_padding = DEFAULT_TIMESTAMP_VERTICAL_PADDING
    timestamp_horizontal_margin = DEFAULT_TIMESTAMP_HORIZONTAL_MARGIN
    timestamp_vertical_margin = DEFAULT_TIMESTAMP_VERTICAL_MARGIN
    quality = DEFAULT_IMAGE_QUALITY
    format = DEFAULT_IMAGE_FORMAT
    timestamp_position = DEFAULT_TIMESTAMP_POSITION



class MediaInfo(object):
    """Collect information about a video file
    """

    def __init__(self, path, verbose=False):
        self.probe_media(path)
        self.find_video_stream()
        self.find_audio_stream()
        self.compute_display_resolution()
        self.compute_format()
        self.parse_attributes()

        if verbose:
            print(self.filename)
            print("%sx%s" % (self.sample_width, self.sample_height))
            print("%sx%s" % (self.display_width, self.display_height))
            print(self.duration)
            print(self.size)

    def probe_media(self, path):
        """Probe video file using ffprobe
        """
        ffprobe_command = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "--",
            path
        ]

        try:
            output = subprocess.check_output(ffprobe_command)
            self.ffprobe_dict = json.loads(output.decode("utf-8"))
        except FileNotFoundError:
            error = "Could not find 'ffprobe' executable. Please make sure ffmpeg/ffprobe is installed and is in your PATH."
            error_exit(error)

    def human_readable_size(self, num, suffix='B'):
        """Converts a number of bytes to a human readable format
        """
        for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
            if abs(num) < 1024.0:
                return "%3.1f %s%s" % (num, unit, suffix)
            num /= 1024.0
        return "%.1f %s%s" % (num, 'Yi', suffix)

    def find_video_stream(self):
        """Find the first stream which is a video stream
        """
        for stream in self.ffprobe_dict["streams"]:
            try:
                if stream["codec_type"] == "video":
                    self.video_stream = stream
                    break
            except:
                pass

    def find_audio_stream(self):
        """Find the first stream which is an audio stream
        """
        for stream in self.ffprobe_dict["streams"]:
            try:
                if stream["codec_type"] == "audio":
                    self.audio_stream = stream
                    break
            except:
                pass

    def compute_display_resolution(self):
        """Computes the display resolution.
        Some videos have a sample resolution that differs from the display resolution
        (non-square pixels), thus the proper display resolution has to be computed.
        """
        self.sample_width = int(self.video_stream["width"])
        self.sample_height = int(self.video_stream["height"])

        # videos recorded with a smartphone may have a "rotate" flag
        try:
            # Try to get rotation from the original key
            rotation = int(self.video_stream["tags"]["rotate"])
        except KeyError:
            # If the original key is not present, check the  key for newer iPhones 14+ (iOS 17+)
            side_data_list = self.video_stream.get("side_data_list", [])
            rotation = None

            for side_data in side_data_list:
                if side_data.get("side_data_type") == "Display Matrix":
                    # Extract rotation value
                    rotation_value = side_data.get("rotation")
                    if rotation_value is not None:
                        # Normalize the rotation value to ensure it is either 90, 180, 270, or None
                        rotation = int(rotation_value) % 360
                        if rotation < 0:
                            rotation += 360
                        if rotation not in [0, 90, 180, 270]:
                            rotation = None
                        break

        if rotation in [90, 270]:
            # swap width and height
            self.sample_width, self.sample_height = self.sample_height, self.sample_width

        sample_aspect_ratio = "1:1"
        try:
            sample_aspect_ratio = self.video_stream["sample_aspect_ratio"]
        except KeyError:
            pass

        if sample_aspect_ratio == "1:1":
            self.display_width = self.sample_width
            self.display_height = self.sample_height
        else:
            sample_split = sample_aspect_ratio.split(":")
            sw = int(sample_split[0])
            sh = int(sample_split[1])

            self.display_width = int(self.sample_width * sw / sh)
            self.display_height = int(self.sample_height)

        if self.display_width == 0:
            self.display_width = self.sample_width

        if self.display_height == 0:
            self.display_height = self.sample_height

    def compute_format(self):
        """Compute duration, size and retrieve filename
        """
        format_dict = self.ffprobe_dict["format"]

        try:
            # try getting video stream duration first
            self.duration_seconds = float(self.video_stream["duration"])
        except (KeyError, AttributeError):
            # otherwise fallback to format duration
            self.duration_seconds = float(format_dict["duration"])

        self.duration = MediaInfo.pretty_duration(self.duration_seconds)

        self.filename = os.path.basename(format_dict["filename"])
        self.file_path = os.path.abspath(format_dict["filename"])

        self.size_bytes = int(format_dict["size"])
        self.size = self.human_readable_size(self.size_bytes)

    @staticmethod
    def pretty_duration(
            seconds,
            show_centis=False,
            show_millis=False):
        """Converts seconds to a human readable time format
        """
        hours = int(math.floor(seconds / 3600))
        remaining_seconds = seconds - 3600 * hours

        minutes = math.floor(remaining_seconds / 60)
        remaining_seconds = remaining_seconds - 60 * minutes

        duration = ""

        if hours > 0:
            duration += "%s:" % (int(hours),)

        duration += "%s:%s" % (str(int(minutes)).zfill(2), str(int(math.floor(remaining_seconds))).zfill(2))

        if show_centis or show_millis:
            coeff = 1000 if show_millis else 100
            digits = 3 if show_millis else 2
            centis = math.floor((remaining_seconds - math.floor(remaining_seconds)) * coeff)
            duration += ".%s" % (str(int(centis)).zfill(digits))

        return duration

    @staticmethod
    def parse_duration(seconds):
        hours = int(math.floor(seconds / 3600))
        remaining_seconds = seconds - 3600 * hours

        minutes = math.floor(remaining_seconds / 60)
        remaining_seconds = remaining_seconds - 60 * minutes
        seconds = math.floor(remaining_seconds)

        millis = math.floor((remaining_seconds - math.floor(remaining_seconds)) * 1000)
        centis = math.floor((remaining_seconds - math.floor(remaining_seconds)) * 100)

        return {
            "hours": hours,
            "minutes": minutes,
            "seconds": seconds,
            "centis": centis,
            "millis": millis
        }

    def desired_size(self, width=Config.contact_sheet_width):
        """Computes the height based on a given width and fixed aspect ratio.
        Returns (width, height)
        """
        ratio = width / float(self.display_width)
        desired_height = int(math.floor(self.display_height * ratio))
        return (width, desired_height)

    def parse_attributes(self):
        """Parse multiple media attributes
        """
        # video
        try:
            self.video_codec = self.video_stream["codec_name"]
        except KeyError:
            self.video_codec = None

        try:
            self.video_codec_long = self.video_stream["codec_long_name"]
        except KeyError:
            self.video_codec_long = None

        try:
            self.video_bit_rate = int(self.video_stream["bit_rate"])
        except KeyError:
            self.video_bit_rate = None

        try:
            self.sample_aspect_ratio = self.video_stream["sample_aspect_ratio"]
        except KeyError:
            self.sample_aspect_ratio = None

        try:
            self.display_aspect_ratio = self.video_stream["display_aspect_ratio"]
        except KeyError:
            self.display_aspect_ratio = None

        try:
            self.frame_rate = self.video_stream["avg_frame_rate"]
            splits = self.frame_rate.split("/")

            if len(splits) == 2:
                self.frame_rate = int(splits[0]) / int(splits[1])
            else:
                self.frame_rate = int(self.frame_rate)

            self.frame_rate = round(self.frame_rate, 3)
        except KeyError:
            self.frame_rate = None
        except ZeroDivisionError:
            self.frame_rate = None

        # audio
        try:
            self.audio_codec = self.audio_stream["codec_name"]
        except (KeyError, AttributeError):
            self.audio_codec = None

        try:
            self.audio_codec_long = self.audio_stream["codec_long_name"]
        except (KeyError, AttributeError):
            self.audio_codec_long = None

        try:
            self.audio_sample_rate = int(self.audio_stream["sample_rate"])
        except (KeyError, AttributeError):
            self.audio_sample_rate = None

        try:
            self.audio_bit_rate = int(self.audio_stream["bit_rate"])
        except (KeyError, AttributeError):
            self.audio_bit_rate = None



class MediaCapture(object):
    """Capture frames of a video
    """

    def __init__(self, path, media_info=None, hwaccel="auto",
                 seek_mode="auto", ffmpeg_threads=0, timeout=180):
        self.path = path
        self.seek_mode = seek_mode
        self.timeout = timeout if timeout and timeout > 0 else None
        self.pixels = 0
        self.video_codec = None
        if media_info is not None:
            self.pixels = int(media_info.display_width) * int(media_info.display_height)
            self.video_codec = media_info.video_codec
        self.hwaccel = self._resolve_hwaccel(hwaccel)
        self.keyframe_seek = self._resolve_keyframe_seek(seek_mode)
        self.ffmpeg_threads = self._resolve_threads(ffmpeg_threads)

    def _resolve_hwaccel(self, requested):
        available = _available_hwaccels()
        if requested == "none":
            return None
        if requested != "auto":
            if requested not in available:
                error_exit("ffmpeg hardware acceleration '{}' is not available "
                           "(available: {})".format(
                               requested, ", ".join(sorted(available)) or "none"))
            return requested
        # VideoToolbox is the only backend we enable automatically.  It is a
        # clear win for 4K HEVC/AV1 and for anything 8K on Apple Silicon, but
        # its per-process setup cost makes it slower for ordinary 1080p/H.264
        # clips.  Linux servers (including the AWS one) have no working
        # /dev/dri device, so they stay on software decoding automatically.
        if sys.platform == "darwin" and "videotoolbox" in available:
            if self.pixels >= EIGHT_K_PIXELS:
                return "videotoolbox"
            if self.pixels >= HIGH_RES_PIXELS and self.video_codec in ("hevc", "av1", "vp9"):
                return "videotoolbox"
        return None

    def _resolve_keyframe_seek(self, requested):
        if requested == "keyframe":
            return True
        if requested == "accurate":
            return False
        # Exact seeking on 8K without a hardware decoder means decoding many
        # frames from the previous keyframe.  Measured on the 2-core server:
        # >180 s per frame with an exact seek versus <2 s for the nearest
        # keyframe.  Use the nearest keyframe in that case (the displayed
        # timestamp is corrected from ffmpeg's showinfo output).
        return self.hwaccel is None and self.pixels >= EIGHT_K_PIXELS

    def _resolve_threads(self, requested):
        if requested and requested > 0:
            return requested
        # One decoder thread keeps peak RSS manageable for 8K (roughly
        # 550 MiB with one thread, 750 MiB with two on the test file) and on
        # the 1 GiB server.  Ordinary clips keep ffmpeg's automatic choice.
        if self.pixels >= EIGHT_K_PIXELS or _low_memory_machine():
            return 1
        return None

    def _is_heavy(self, hwaccel):
        if hwaccel is not None:
            return False
        return (self.pixels >= EIGHT_K_PIXELS or
                (_low_memory_machine() and self.pixels >= HIGH_RES_PIXELS))

    def _build_command(self, time, width, height, out_path, hwaccel, keyframe_seek):
        cmd = list(_nice_prefix()) + [
            "ffmpeg", "-hide_banner", "-nostdin", "-nostats",
            "-loglevel", "info" if keyframe_seek else "error",
        ]
        if self.ffmpeg_threads:
            cmd += ["-threads", str(self.ffmpeg_threads),
                    "-filter_threads", str(self.ffmpeg_threads)]
        if hwaccel is not None:
            cmd += ["-hwaccel", hwaccel]

        if keyframe_seek:
            cmd += ["-skip_frame", "nokey",
                    "-ss", time, "-noaccurate_seek", "-i", self.path]
        else:
            cmd += ["-ss", time, "-i", self.path]

        cmd += ["-frames:v", "1"]
        filters = ["showinfo"] if keyframe_seek else []
        filters.append("scale=%s:%s" % (width, height))
        cmd += ["-vf", ",".join(filters), "-an", "-sn", "-dn", "-y", out_path]
        return cmd

    def make_capture(self, time, width, height, out_path="out.png"):
        """Capture a frame at given time with given width and height using ffmpeg.

        Returns the offset (in seconds) between the requested timestamp and the
        frame ffmpeg actually produced, when that can be determined.  This is
        non-zero only in nearest-keyframe seek mode.
        """
        attempts = []
        if self.hwaccel is not None:
            attempts.append((self.hwaccel, self.keyframe_seek))
            # If hardware decoding rejects this file/profile, fall back to
            # software decoding.  For 8K that fallback also needs the fast seek.
            fallback_keyframe = (self.seek_mode == "auto" and
                                 self.pixels >= EIGHT_K_PIXELS)
            attempts.append((None, self.keyframe_seek or fallback_keyframe))
        else:
            attempts.append((None, self.keyframe_seek))

        seen = set()
        last_error = None
        for hwaccel, keyframe_seek in attempts:
            if (hwaccel, keyframe_seek) in seen:
                continue
            seen.add((hwaccel, keyframe_seek))
            cmd = self._build_command(time, width, height, out_path, hwaccel, keyframe_seek)
            try:
                with _heavy_capture_lock(self._is_heavy(hwaccel)):
                    result = subprocess.run(
                        cmd, stdin=DEVNULL, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", check=True,
                        timeout=self.timeout)
            except FileNotFoundError:
                error_exit("Could not find 'ffmpeg' executable. Please make sure "
                           "ffmpeg/ffprobe is installed and is in your PATH.")
            except subprocess.TimeoutExpired as ex:
                last_error = ex
                if hwaccel is None:
                    break
                continue
            except subprocess.CalledProcessError as ex:
                last_error = ex
                if hwaccel is None:
                    break
                continue

            if keyframe_seek:
                offsets = re.findall(r"pts_time:\s*(-?\d+(?:\.\d+)?)", result.stderr)
                if offsets:
                    offset = float(offsets[-1])
                    # showinfo reports timestamps relative to the seek point.
                    # A keyframe can be up to one GOP away in either direction.
                    if -3600 < offset <= 60:
                        return offset
            return None

        if isinstance(last_error, subprocess.TimeoutExpired):
            raise RuntimeError(
                "ffmpeg timed out after {} seconds while decoding '{}'. "
                "Use --seek-mode keyframe for very large videos, or raise "
                "--ffmpeg-timeout.".format(self.timeout, self.path))
        if last_error is not None:
            error = f"""
                ffmpeg had an error while decoding the file:",
                Command: '{" ".join(last_error.cmd)}',
                Exit code: {last_error.returncode},
                Output: {last_error.stderr}
                """
            raise RuntimeError(error)
        return None

    def compute_avg_color(self, image_path):
        """Computes the average color of an image
        """
        i = Image.open(image_path)
        i = i.convert('P')
        p = i.getcolors()

        # compute avg color
        total_count = 0
        avg_color = 0
        for count, color in p:
            total_count += count
            avg_color += count * color

        avg_color /= total_count

        return avg_color

    def compute_blurriness(self, image_path):
        """Computes the blurriness of an image. Small value means less blurry.

        This used to be based on a numpy FFT.  The variance of the PIL edge
        filter ranks frames almost identically (Spearman 0.94 against the old
        metric on a test clip) and lets us drop the numpy dependency entirely.
        """
        i = Image.open(image_path).convert('L')
        edges = i.filter(ImageFilter.FIND_EDGES)
        variance = ImageStat.Stat(edges).var[0]
        return -variance


def grid_desired_size(
        grid,
        media_info,
        width=Config.contact_sheet_width,
        horizontal_margin=Config.grid_horizontal_spacing):
    """Computes the size of the images placed on a mxn grid with given fixed width.
    Returns (width, height)
    """
    desired_width = (width - (grid.x - 1) * horizontal_margin) / grid.x
    desired_width = int(math.floor(desired_width))

    return media_info.desired_size(width=desired_width)


def total_delay_seconds(media_info, args):
    """Computes the total seconds to skip (beginning + ending).
    """
    start_delay_seconds = math.floor(media_info.duration_seconds * args.start_delay_percent / 100)
    end_delay_seconds = math.floor(media_info.duration_seconds * args.end_delay_percent / 100)
    delay = start_delay_seconds + end_delay_seconds
    return delay


def timestamp_generator(media_info, args):
    """Generate `num_samples` uniformly distributed timestamps."""
    delay = total_delay_seconds(media_info, args)
    capture_interval = (media_info.duration_seconds - delay) / (args.num_samples + 1)
    start_delay_seconds = math.floor(
        media_info.duration_seconds * args.start_delay_percent / 100)
    time = start_delay_seconds + capture_interval

    for _ in range(args.num_samples):
        yield time, MediaInfo.pretty_duration(time, show_millis=True)
        time += capture_interval


def capture_frames(media_info, media_capture, args):
    """Capture the grid's frames.  The lite build always keeps every frame;
    there is no sampling/selection step."""
    desired_size = grid_desired_size(
        args.grid,
        media_info,
        width=args.vcs_width,
        horizontal_margin=args.grid_horizontal_spacing)

    frames = []
    for i, (timestamp, pretty_time) in enumerate(timestamp_generator(media_info, args)):
        print("Sampling... {}/{}".format(i + 1, args.num_samples), end="\r")
        fd, filename = tempfile.mkstemp(suffix=".bmp")
        try:
            offset = media_capture.make_capture(
                pretty_time, desired_size[0], desired_size[1], filename)
            if offset is not None:
                timestamp = max(0.0, timestamp + offset)
        except BaseException:
            try:
                os.unlink(filename)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        frames.append(Frame(filename=filename, timestamp=timestamp))
    print()

    frames.sort(key=lambda frame: frame.timestamp)
    return frames


def draw_metadata(
        draw,
        args,
        header_line_height=None,
        header_lines=None,
        header_font=None,
        header_font_color=None,
        start_height=None):
    """Draw metadata header
    """
    h = start_height
    h += args.metadata_vertical_margin

    for line in header_lines:
        draw.text((args.metadata_horizontal_margin, h), line, font=header_font, fill=header_font_color)
        h += header_line_height

    h += args.metadata_vertical_margin

    return h


def max_line_length(
        media_info,
        metadata_font,
        header_margin,
        width=Config.contact_sheet_width,
        text=None):
    """Find the number of characters that fit in width with given font.
    """
    if text is None:
        text = media_info.filename

    max_width = width - 2 * header_margin

    max_length = 0
    for i in range(len(text) + 1):
        text_chunk = text[:i]
        text_width = 0 if len(text_chunk) == 0 else metadata_font.getlength(text_chunk)

        max_length = i
        if text_width > max_width:
            break

    return max_length


def prepare_metadata_text_lines(media_info, header_font, header_margin, width):
    """Prepare the metadata header text and return a list containing each line."""
    template = """{}
        File size: {}
        Duration: {}
        Dimensions: {}x{}""".format(
        media_info.filename, media_info.size, media_info.duration,
        media_info.sample_width, media_info.sample_height)
    template_lines = [x.strip() for x in template.split("\n") if x.strip()]

    header_lines = []
    for line in template_lines:
        remaining_chars = line
        while remaining_chars:
            max_metadata_line_length = max_line_length(
                media_info, header_font, header_margin,
                width=width, text=remaining_chars)
            wraps = textwrap.wrap(remaining_chars, max_metadata_line_length)
            header_lines.append(wraps[0])
            remaining_chars = remaining_chars[len(wraps[0]):].strip()

    return header_lines


def compute_timestamp_position(args, w, h, text_size, desired_size, rectangle_hpadding, rectangle_vpadding):
    """Compute the (x,y) position of the upper left and bottom right points of the rectangle surrounding timestamp text.
    """
    position = args.timestamp_position

    x_offset = 0
    if position in [TimestampPosition.west, TimestampPosition.nw, TimestampPosition.sw]:
        x_offset = args.timestamp_horizontal_margin
    elif position in [TimestampPosition.north, TimestampPosition.center, TimestampPosition.south]:
        x_offset = (desired_size[0] / 2) - (text_size[0] / 2) - rectangle_hpadding
    else:
        x_offset = desired_size[0] - text_size[0] - args.timestamp_horizontal_margin - 2 * rectangle_hpadding

    y_offset = 0
    if position in [TimestampPosition.nw, TimestampPosition.north, TimestampPosition.ne]:
        y_offset = args.timestamp_vertical_margin
    elif position in [TimestampPosition.west, TimestampPosition.center, TimestampPosition.east]:
        y_offset = (desired_size[1] / 2) - (text_size[1] / 2) - rectangle_vpadding
    else:
        y_offset = desired_size[1] - text_size[1] - args.timestamp_vertical_margin - 2 * rectangle_vpadding

    upper_left = (
        w + x_offset,
        h + y_offset
    )

    bottom_right = (
        upper_left[0] + text_size[0] + 2 * rectangle_hpadding,
        upper_left[1] + text_size[1] + 2 * rectangle_vpadding
    )

    return upper_left, bottom_right


def load_font(args, font_path, font_size, default_font_path):
    """Loads given font and defaults to fallback fonts if that fails."""
    fonts = [font_path] + FALLBACK_FONTS
    if font_path == default_font_path:
        for font in fonts:
            if os.path.exists(font):
                try:
                    return ImageFont.truetype(font, font_size)
                except OSError:
                    pass
        print("Falling back to default font.")
        return ImageFont.load_default()
    else:
        try:
            return ImageFont.truetype(font_path, font_size)
        except OSError:
            error_exit("Cannot load font: {}".format(font_path))


def compose_contact_sheet(
        media_info,
        frames,
        args):
    """Creates a video contact sheet with the media information in a header
    and the selected frames arranged on a mxn grid with optional timestamps
    """
    desired_size = grid_desired_size(
        args.grid,
        media_info,
        width=args.vcs_width,
        horizontal_margin=args.grid_horizontal_spacing)
    width = args.grid.x * (desired_size[0] + args.grid_horizontal_spacing) - args.grid_horizontal_spacing
    height = args.grid.y * (desired_size[1] + args.grid_vertical_spacing) - args.grid_vertical_spacing

    header_font = load_font(args, args.metadata_font, args.metadata_font_size, Config.metadata_font)
    timestamp_font = load_font(args, args.timestamp_font, args.timestamp_font_size, Config.timestamp_font)

    header_lines = prepare_metadata_text_lines(
        media_info,
        header_font,
        args.metadata_horizontal_margin,
        width)

    line_spacing_coefficient = 1.2
    header_line_height = int(args.metadata_font_size * line_spacing_coefficient)
    header_height = 2 * args.metadata_margin + len(header_lines) * header_line_height

    if args.metadata_position == "hidden":
        header_height = 0

    final_image_width = width
    final_image_height = height + header_height
    transparent = (255, 255, 255, 0)

    image = Image.new("RGBA", (final_image_width, final_image_height), args.background_color)
    image_capture_layer = Image.new("RGBA", (final_image_width, final_image_height), transparent)
    image_header_text_layer = Image.new("RGBA", (final_image_width, final_image_height), transparent)
    image_timestamp_layer = Image.new("RGBA", (final_image_width, final_image_height), transparent)
    image_timestamp_text_layer = Image.new("RGBA", (final_image_width, final_image_height), transparent)

    draw_header_text_layer = ImageDraw.Draw(image_header_text_layer)
    draw_timestamp_layer = ImageDraw.Draw(image_timestamp_layer)
    draw_timestamp_text_layer = ImageDraw.Draw(image_timestamp_text_layer)
    h = 0

    def draw_metadata_helper():
        """Draw metadata with fixed arguments
        """
        return draw_metadata(
            draw_header_text_layer,
            args,
            header_line_height=header_line_height,
            header_lines=header_lines,
            header_font=header_font,
            header_font_color=args.metadata_font_color,
            start_height=h)

    # draw metadata
    if args.metadata_position == "top":
        h = draw_metadata_helper()

    # draw capture grid
    w = 0
    frames = sorted(frames, key=lambda x: x.timestamp)
    for i, frame in enumerate(frames):
        f = Image.open(frame.filename)
        f.putalpha(args.capture_alpha)
        image_capture_layer.paste(f, (w, h))

        # show timestamp
        if args.show_timestamp:
            timestamp_time = MediaInfo.pretty_duration(frame.timestamp, show_centis=True)
            timestamp_duration = MediaInfo.pretty_duration(media_info.duration_seconds, show_centis=True)
            parsed_time = MediaInfo.parse_duration(frame.timestamp)
            parsed_duration = MediaInfo.parse_duration(media_info.duration_seconds)
            timestamp_args = {
                "TIME": timestamp_time,
                "DURATION": timestamp_duration,
                "THUMBNAIL_NUMBER": i + 1,
                "H": str(parsed_time["hours"]).zfill(2),
                "M": str(parsed_time["minutes"]).zfill(2),
                "S": str(parsed_time["seconds"]).zfill(2),
                "c": str(parsed_time["centis"]).zfill(2),
                "m": str(parsed_time["millis"]).zfill(3),
                "dH": str(parsed_duration["hours"]).zfill(2),
                "dM": str(parsed_duration["minutes"]).zfill(2),
                "dS": str(parsed_duration["seconds"]).zfill(2),
                "dc": str(parsed_duration["centis"]).zfill(2),
                "dm": str(parsed_duration["millis"]).zfill(3)
            }
            timestamp_text = args.timestamp_format.format(**timestamp_args)
            text_bbox = timestamp_font.getbbox(timestamp_text)
            (left, top, right, bottom) = text_bbox
            text_width = abs(right - left)
            text_height = abs(top - bottom)
            text_size = (text_width, text_height)

            # draw rectangle
            rectangle_hpadding = args.timestamp_horizontal_padding
            rectangle_vpadding = args.timestamp_vertical_padding

            upper_left, bottom_right = compute_timestamp_position(args, w, h, text_size, desired_size,
                                                                  rectangle_hpadding, rectangle_vpadding)

            if not args.timestamp_border_mode:
                draw_timestamp_layer.rectangle(
                    [upper_left, bottom_right],
                    fill=args.timestamp_background_color
                )
            else:
                offset_factor = args.timestamp_border_size
                offsets = [
                    (1, 0),
                    (-1, 0),
                    (0, 1),
                    (0, -1),
                    (1, 1),
                    (1, -1),
                    (-1, 1),
                    (-1, -1)
                ]

                final_offsets = []
                for offset_counter in range(1, offset_factor + 1):
                    final_offsets += [(x[0] * offset_counter, x[1] * offset_counter) for x in offsets]

                for offset in final_offsets:
                    # draw border first
                    draw_timestamp_text_layer.text(
                        (
                            upper_left[0] + rectangle_hpadding + offset[0],
                            upper_left[1] + rectangle_vpadding + offset[1]
                        ),
                        timestamp_text,
                        font=timestamp_font,
                        fill=args.timestamp_border_color,
                        anchor="lt"
                    )

            # draw timestamp
            draw_timestamp_text_layer.text(
                (
                    upper_left[0] + rectangle_hpadding,
                    upper_left[1] + rectangle_vpadding
                ),
                timestamp_text,
                font=timestamp_font,
                fill=args.timestamp_font_color,
                anchor="lt"
            )

        # update x position for next frame
        w += desired_size[0] + args.grid_horizontal_spacing

        # update y position
        if (i + 1) % args.grid.x == 0:
            h += desired_size[1] + args.grid_vertical_spacing

        # update x position
        if (i + 1) % args.grid.x == 0:
            w = 0

    # draw metadata
    if args.metadata_position == "bottom":
        h -= args.grid_vertical_spacing
        h = draw_metadata_helper()

    # alpha blend
    out_image = Image.alpha_composite(image, image_capture_layer)
    out_image = Image.alpha_composite(out_image, image_header_text_layer)
    out_image = Image.alpha_composite(out_image, image_timestamp_layer)
    out_image = Image.alpha_composite(out_image, image_timestamp_text_layer)

    return out_image


def save_image(args, image, output_path):
    """Save the image to `output_path`."""
    image = image.convert("RGB")
    try:
        image.save(output_path, optimize=True, quality=args.image_quality)
        return True
    except KeyError:
        return False


def cleanup(frames):
    """Delete temporary captures."""
    for frame in frames:
        try:
            os.unlink(frame.filename)
        except OSError:
            pass


def mxn_type(string):
    """Parse an "mxn" grid specification."""
    try:
        split = string.split("x")
        assert len(split) == 2
        m = int(split[0])
        n = int(split[1])
        assert m > 0 and n > 0
        return Grid(m, n)
    except (IndexError, ValueError, AssertionError):
        raise argparse.ArgumentTypeError(
            "Grid must be of the form mxn, where m is the number of columns "
            "and n is the number of rows.")


def error(message):
    """Print an error message."""
    print("[ERROR] %s" % (message,))


def error_exit(message):
    """Print an error message and exit"""
    error(message)
    sys.exit(-1)


def main():
    """Program entry point.  This is the deliberately small vcsi-lite CLI:
    only the options used by vcsi_ui are exposed."""
    parser = argparse.ArgumentParser(
        description="Create a video contact sheet (vcsi-lite)")
    parser.add_argument("filenames", nargs="+")
    parser.add_argument(
        "-o", "--output",
        help="Output file or existing directory. Defaults to <video>.jpg.",
        dest="output_path")
    parser.add_argument(
        "-g", "--grid",
        help="Display frames on a mxn grid (for example 3x20).",
        dest="grid", type=mxn_type, default=Config.grid_size)
    parser.add_argument(
        "-t", "--show-timestamp",
        action="store_true",
        help="Display a timestamp on each frame.",
        dest="show_timestamp")
    parser.add_argument(
        "--start-delay-percent",
        help="Do not capture frames in the first n percent of the video.",
        dest="start_delay_percent", type=int, default=Config.start_delay_percent)
    parser.add_argument(
        "--end-delay-percent",
        help="Do not capture frames in the last n percent of the video.",
        dest="end_delay_percent", type=int, default=Config.end_delay_percent)
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Skip the file if the output image already exists.",
        dest="no_overwrite")
    parser.add_argument(
        "--hwaccel",
        choices=("auto", "none", "videotoolbox"),
        default="auto",
        help="ffmpeg hardware decoding. auto = VideoToolbox for large "
             "HEVC/AV1/VP9 or 8K videos on macOS, software elsewhere.",
        dest="hwaccel")
    parser.add_argument(
        "--seek-mode",
        choices=("auto", "accurate", "keyframe"),
        default="auto",
        help="accurate = exact timestamp; keyframe = nearest keyframe (much "
             "faster on huge videos); auto = keyframe for software-decoded 8K.",
        dest="seek_mode")
    parser.add_argument(
        "--ffmpeg-threads",
        type=int,
        default=0,
        metavar="N",
        help="Decoder threads per ffmpeg call. 0 = automatic.",
        dest="ffmpeg_threads")
    parser.add_argument(
        "--ffmpeg-timeout",
        type=int,
        default=180,
        metavar="SECONDS",
        help="Abort a single frame extraction after this many seconds. "
             "0 disables the timeout.",
        dest="ffmpeg_timeout")
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s version {version}".format(version=__version__))

    # Internal defaults.  These are intentionally not command-line options:
    # vcsi_ui uses the upstream defaults for everything except the options
    # added above.
    parser.set_defaults(
        background_color=Color(0x00, 0x00, 0x00, 0xFF),
        capture_alpha=Config.capture_alpha,
        grid_horizontal_spacing=Config.grid_horizontal_spacing,
        grid_vertical_spacing=Config.grid_vertical_spacing,
        image_format=Config.format,
        image_quality=Config.quality,
        metadata_font=Config.metadata_font,
        metadata_font_color=Color(0xFF, 0xFF, 0xFF, 0xFF),
        metadata_font_size=Config.metadata_font_size,
        metadata_horizontal_margin=Config.metadata_horizontal_margin,
        metadata_margin=Config.metadata_margin,
        metadata_position=Config.metadata_position,
        metadata_vertical_margin=Config.metadata_vertical_margin,
        num_samples=None,
        timestamp_background_color=Color(0x00, 0x00, 0x00, 0xAA),
        timestamp_border_color=Color(0x00, 0x00, 0x00, 0xFF),
        timestamp_border_mode=False,
        timestamp_border_size=Config.timestamp_border_size,
        timestamp_font=Config.timestamp_font,
        timestamp_font_color=Color(0xFF, 0xFF, 0xFF, 0xFF),
        timestamp_font_size=Config.timestamp_font_size,
        timestamp_format="{TIME}",
        timestamp_horizontal_margin=Config.timestamp_horizontal_margin,
        timestamp_horizontal_padding=Config.timestamp_horizontal_padding,
        timestamp_position=Config.timestamp_position,
        timestamp_vertical_margin=Config.timestamp_vertical_margin,
        timestamp_vertical_padding=Config.timestamp_vertical_padding,
        vcs_width=Config.contact_sheet_width,
    )

    args = parser.parse_args()
    for path in args.filenames:
        process_file(path, args)


def process_file(path, args):
    """Generate a video contact sheet for the file at given path."""
    if not os.path.isfile(path):
        error_exit("File does not exist: {}".format(path))

    output_path = args.output_path
    if not output_path:
        output_path = path + "." + args.image_format
    elif os.path.isdir(output_path):
        output_path = os.path.join(output_path, os.path.basename(path) + "." + args.image_format)

    if args.no_overwrite and os.path.exists(output_path):
        print("[INFO] contact-sheet already exists, skipping: {}".format(output_path))
        return

    print("Processing {}...".format(path))

    media_info = MediaInfo(path)
    media_capture = MediaCapture(
        path,
        media_info=media_info,
        hwaccel=args.hwaccel,
        seek_mode=args.seek_mode,
        ffmpeg_threads=args.ffmpeg_threads,
        timeout=args.ffmpeg_timeout,
    )

    args.num_samples = args.grid.x * args.grid.y
    frames = capture_frames(media_info, media_capture, args)

    print("Composing contact sheet...")
    image = compose_contact_sheet(media_info, frames, args)

    is_save_successful = save_image(args, image, output_path)

    print("Cleaning up temporary files...")
    cleanup(frames)

    if not is_save_successful:
        error_exit("Unsupported image format: %s." % (args.image_format,))


if __name__ == "__main__":
    main()
