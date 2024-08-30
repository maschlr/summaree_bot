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
BITRATE_PATTERN = re.compile(r".+?(\d+) kb\/s.*")


def get_silent_segments(input_file: Path, min_silence_len: int = 500, noise_thresh: float = 0.001):
    _logger.info(f"Getting silent segments from file: {input_file}")
    cmd = f"ffmpeg -i {input_file} -af silencedetect=noise={noise_thresh}:d={min_silence_len}ms -f null -"
    process = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, output = process.communicate()
    if not process.returncode == 0:
        msg = f"Getting silent segments failed: {output}"
        _logger.error(msg)
        raise ValueError(msg)
    result = {"silent_segments": None, "bitrate": None}
    silent_segments = []
    found_bitrate = False
    for line1, line2 in pairwise(output.decode("utf-8").splitlines()):
        if not found_bitrate:
            match_bitrate = BITRATE_PATTERN.match(line1)
            if not match_bitrate:
                continue
            result["bitrate"] = int(match_bitrate.group(1))
            found_bitrate = True

        match_start = SILENCE_START_PATTERN.match(line1)
        if match_start:
            start = float(match_start.group(1))
        else:
            continue
        match_end = SILENCE_END_PATTERN.match(line2)
        if match_end:
            end = float(match_end.group(1))
            _logger.info(f"Silent segment: {start} - {end}")
            silent_segments.append((start, end))

    result["silent_segments"] = np.array(silent_segments)
    _logger.info("Getting silent segments successfully finished")
    return result


def split_audio_ffmpeg(input_file: Path, output_dir: Path, segments: List[float]):
    suffix = input_file.suffix
    output_file = output_dir / f"{input_file.stem}_%03d{suffix}"
    segment_times = ",".join(map(str, segments))
    cmd = rf"ffmpeg -i {str(input_file)} -f segment -segment_times {segment_times} -c copy {str(output_file)}"
    run_args = shlex.split(cmd)
    process = subprocess.Popen(run_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while process.poll() is None:
        time.sleep(1)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        msg = f"Splitting failed: {stderr}"
        _logger.error(msg)
        raise ValueError(msg)
    _logger.info(f"Splitting successfully finished: {output_file}\n{stdout}")
    files = glob(pathname=f"{input_file.stem}_*{suffix}", root_dir=output_dir)
    return [output_dir / file for file in files]


def transcode_ffmpeg(input_file: Path) -> Path:
    _logger.info(f"Transcoding file: {input_file}")
    output_file = input_file.parent / f"{input_file.stem}.mp3"
    cmd = f"ffmpeg -i {input_file} -c:a libmp3lame -b:a 96k {output_file}"
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
    analysis = get_silent_segments(input_file, min_silence_len=min_silence_len, noise_thresh=noise_thresh)
    splits = analysis["silent_segments"].mean(axis=1)
    # load audio
    segments = []
    current_start = 0
    for split1, split2 in pairwise(splits):
        # fill up the chunks to the max size
        # If adding this chunk would exceed the size limit, export the current chunk
        if (split2 - current_start) * analysis["bitrate"] / 1024 / 8 > max_size_mb:
            segments.append(split1)
            current_start = split1

    # Export any remaining audio
    split_paths = split_audio_ffmpeg(input_file, output_dir, segments)
    return split_paths
