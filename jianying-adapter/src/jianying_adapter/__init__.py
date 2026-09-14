# Copyright (c) 2026 Blackkeys0430 — AutoCUT original project code.
# Origin: https://github.com/Blackkeys0430/AutoCUT-public
# SPDX-License-Identifier: LicenseRef-AutoCUT-Personal-Use-1.0
"""AutoCUT by Blackkeys0430 — local Jianying draft adapter primitives.

Project origin: https://github.com/Blackkeys0430/AutoCUT-public
Third-party components retain their own attribution and licenses.
"""

from .identity import AUTHOR as __author__, PROJECT_URL as __url__
from .codec import DraftCodec, JsonCodec, LazyJianying11DllProvider
from .hashing import file_sha256, object_sha256, tree_fingerprint
from .subtitle_layout import (
    KeywordRange,
    SubtitleInput,
    SubtitleLayoutConfig,
    measure_case,
    render_preview,
)
from .subtitles import SubtitlePlan, build_bilingual_subtitles, load_subtitle_plan
from .cut_safety import SafeCut, clamp_fade_duration, safe_audio_fade, snap_cut_to_words
from .transcript import TimedWord, join_word_text, load_timed_words, load_timed_words_file, pack_transcript, pack_transcript_file
from .writer import apply_subtitle_plan

__all__ = [
    "DraftCodec",
    "JsonCodec",
    "LazyJianying11DllProvider",
    "KeywordRange",
    "SubtitleInput",
    "SubtitleLayoutConfig",
    "SubtitlePlan",
    "SafeCut",
    "TimedWord",
    "apply_subtitle_plan",
    "build_bilingual_subtitles",
    "file_sha256",
    "measure_case",
    "load_subtitle_plan",
    "load_timed_words",
    "load_timed_words_file",
    "join_word_text",
    "object_sha256",
    "render_preview",
    "pack_transcript",
    "pack_transcript_file",
    "clamp_fade_duration",
    "safe_audio_fade",
    "snap_cut_to_words",
    "tree_fingerprint",
]

__version__ = "0.1.0"
