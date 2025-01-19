import logging
import re
import shlex
import subprocess
import time
from glob import glob
from itertools import pairwise
from pathlib import Path
from typing import List

import numpy as np

_logger = logging.getLogger(__name__)

SILENCE_START_PATTERN = re.compile(r".+silence_start:\s(\d+\.\d+)")
SILENCE_END_PATTERN = re.compile(r".+silence_end:\s(\d+\.\d+)")
HEADER_PATTERN = re.compile(
    r".+\s(?P<hours>\d{2}):(?P<minutes>\d{2}):(?P<seconds>\d{2})\.(?P<ms>\d{2}),.+bitrate:\s(?P<bitrate>\d+)"
)


def get_silent_segments(input_file: Path, min_silence_len: int = 500, noise_thresh: float = 0.001, min_splits: int = 2):
    _logger.info(f"Getting silent segments from file: {input_file}")
    cmd = (
        f"ffmpeg -i {shlex.quote(input_file.as_posix())} -af "
        f"silencedetect=noise={noise_thresh}:d={min_silence_len}ms -f null -"
    )
    process = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, output = process.communicate()
    if not process.returncode == 0:
        msg = f"Getting silent segments failed: {output}"
        _logger.error(msg)
        raise ValueError(msg)
    result = {}
    silent_segments = []
    found_header = False
    found_start = False
    for line1, line2 in pairwise(output.decode("utf-8").splitlines()):
        if not found_header:
            match_header = HEADER_PATTERN.match(line1)
            if not match_header:
                continue
            result["bitrate"] = int(match_header.group("bitrate"))
            hours, minutes, seconds, milliseconds = [
                int(match_header.group(name)) for name in ("hours", "minutes", "seconds", "ms")
            ]
            total_seconds = hours * 3600 + minutes * 60 + seconds + milliseconds / 1000
            result["total_seconds"] = total_seconds
            found_header = True

        match_start = SILENCE_START_PATTERN.match(line1)
        if match_start and not found_start:
            start = float(match_start.group(1))
            found_start = True
        elif found_start and (match_end := SILENCE_END_PATTERN.match(line2)):
            end = float(match_end.group(1))
            silent_segments.append((start, end))
            found_start = False
    if (len(silent_segments) < min_splits - 1) and min_silence_len % 10 == 0:
        shorter_min_silence_len = min_silence_len / 10
        higher_noice_threshold = noise_thresh * 10
        _logger.warning(
            "Found not enough silent segments...Retrying with shorher min silence lenght and higer noise threshold..."
        )
        return get_silent_segments(
            input_file=input_file, min_silence_len=shorter_min_silence_len, noise_thresh=higher_noice_threshold
        )

    result["silent_segments"] = np.array(silent_segments)
    _logger.info("Getting silent segments successfully finished")
    return result


def split_audio_ffmpeg(input_file: Path, output_dir: Path, segments: List[float]):
    suffix = input_file.suffix
    if suffix == ".mpeg":
        suffix = ".m4a"
    output_file = output_dir / f"%03d{suffix}"
    segment_times = ",".join(map(lambda segment: f"{segment:.04f}", segments))
    cmd = (
        rf"ffmpeg -i {shlex.quote(input_file.as_posix())} -f segment -segment_times"
        rf" {segment_times} -c copy {shlex.quote(output_file.as_posix())}"
    )
    run_args = shlex.split(cmd)
    process = subprocess.Popen(run_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while process.poll() is None:
        time.sleep(1)
    _stdout, stderr = process.communicate()
    if process.returncode != 0:
        msg = f"Splitting failed: {stderr}"
        _logger.error(msg)
        raise ValueError(msg)
    _logger.info(f"Splitting successfully finished: {output_file}")
    files = glob(pathname=f"*{suffix}", root_dir=output_dir)
    return [output_dir / file for file in files]


def transcode_ffmpeg(input_file: Path) -> Path:
    _logger.info(f"Transcoding file: {input_file}")
    output_file = input_file.parent / f"{input_file.stem}.mp3"
    cmd = (
        f"ffmpeg -i {shlex.quote(input_file.as_posix())} -c:a libmp3lame -b:a 96k {shlex.quote(output_file.as_posix())}"
    )
    run_args = shlex.split(cmd)
    process = subprocess.Popen(run_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while process.poll() is None:
        time.sleep(1)

    stdout, stderr = process.communicate()
    if process.returncode != 0:
        msg = f"Transcoding failed: {stderr}"
        _logger.error(msg)
        raise ValueError(msg)
    _logger.info(f"Transcoding successfully finished: {output_file}\n{stdout}")
    return output_file


def split_audio(
    input_file: Path, output_dir: Path, max_size_mb: int = 24, min_silence_len: int = 500, noise_thresh: float = 0.001
):
    # get segments and bitrate
    file_size = input_file.stat().st_size * 1024 * 1024  # MB
    if file_size % max_size_mb:
        min_splits = file_size // max_size_mb + 1
    else:
        min_splits = file_size / max_size_mb

    segments = []
    analysis = get_silent_segments(
        input_file, min_silence_len=min_silence_len, noise_thresh=noise_thresh, min_splits=min_splits
    )
    if len(analysis["silent_segments"]):
        splits = analysis["silent_segments"].mean(axis=1)
        current_start = 0
        for split1, split2 in pairwise(splits):
            if (split1 - current_start) * analysis["bitrate"] / 1024 / 8 > max_size_mb:
                segments = get_even_splits(input_file=input_file, analysis=analysis, max_size_mb=max_size_mb)
                break
            # fill up the chunks to the max size
            # If adding this chunk would exceed the size limit, export the current chunk
            elif (split2 - current_start) * analysis["bitrate"] / 1024 / 8 > max_size_mb:
                segments.append(split1)
                current_start = split1
        if any(size > max_size_mb * 1024 for size in get_sizes(segments, analysis)):
            segments = get_even_splits(input_file=input_file, analysis=analysis, max_size_mb=max_size_mb)

    if not len(segments):
        segments = get_even_splits(input_file=input_file, analysis=analysis, max_size_mb=max_size_mb)

    # Export any remaining audio
    split_paths = split_audio_ffmpeg(input_file, output_dir, segments)
    return split_paths


def get_even_splits(input_file: Path, analysis: dict, max_size_mb: int):
    file_size = input_file.stat().st_size / 1024  # kB
    chunksize = max_size_mb * 1024  # kB
    seconds_total = analysis["total_seconds"]
    if file_size % chunksize:
        n_chunks = (file_size // chunksize) + 1
    else:
        n_chunks = file_size / chunksize
    segments = np.linspace(0, int(seconds_total) + 1, int(n_chunks + 1))[1:-1]
    return segments


def get_sizes(segments, analysis):
    current_pos = 0
    for segment in segments:
        length = segment - current_pos
        current_pos = segment
        yield length * analysis["bitrate"] / 8  # kB
