from __future__ import annotations

"""Content gates for the semantic and subtitle phases.

The module deliberately reads evidence files itself.  A caller cannot make an
empty ``evidence`` object pass by asserting a status or by supplying a Python
callback.  It is a content gate, not a substitute for listening to the audio:
the returned transcript is evidence for a human or an upstream adjudicator.
"""

import hashlib
from difflib import SequenceMatcher
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..transcript import TimedWord, load_timed_words


_HASH_KEYS = ("evidence_sha256", "evidence_hash", "transcript_sha256", "content_sha256")
_SOURCE_KEYS = ("source", "provider", "engine", "model", "source_name")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clean_text(value: Any) -> str:
    text = str(value or "")
    return re.sub(r"\s+", "", text).strip().casefold()


def validate_retake_cuts(cuts: Any, segments: Sequence[Mapping[str, Any]]) -> list[str]:
    """Check declared discarded source ranges against actual retained ranges."""
    errors: list[str] = []
    if not isinstance(cuts, list):
        return ["retake_cuts 必须是列表"]
    for index, cut in enumerate(cuts):
        if not isinstance(cut, Mapping):
            errors.append(f"retake_cuts[{index}] 必须是对象")
            continue
        start, end = cut.get("source_start_us"), cut.get("source_end_us")
        source = cut.get("source_path")
        if (not isinstance(source, str) or not Path(source).is_absolute()
                or type(start) is not int or type(end) is not int
                or start < 0 or end <= start or not str(cut.get("reason") or "").strip()):
            errors.append(f"retake_cuts[{index}] 缺少有效源路径、区间或删除理由")
            continue
        matching = [s for s in segments if os.path.normcase(os.path.abspath(str(s.get("source_path", ""))))
                    == os.path.normcase(os.path.abspath(source))]
        if not matching:
            errors.append(f"retake_cuts[{index}] 源素材未绑定实际保留段")
        for segment in matching:
            if max(start, segment["source_start_us"]) < min(end, segment["source_end_us"]):
                errors.append(f"retake_cuts[{index}] 废弃重录仍在实际保留区间内")
                break
    return errors


def _repeat_difference_errors(records: Sequence[Mapping[str, Any]], detail: Mapping[str, Any]) -> list[str]:
    """Flag repeated speech omitted by the chosen text; never decide to cut it."""
    compact = lambda value: re.sub(r"[^\w]", "", str(value)).casefold()
    final = compact(detail.get("final_text", ""))
    decision = json.loads(Path(detail["path"]).read_text(encoding="utf-8-sig"))
    resolutions = decision.get("repeat_resolutions", [])
    if not isinstance(resolutions, list):
        return ["repeat_resolutions 必须是列表"]
    errors: list[str] = []
    for record in records:
        text = compact(record.get("text", ""))
        for tag, i, j, a, b in SequenceMatcher(None, final, text, autojunk=False).get_opcodes():
            if tag not in {"insert", "replace"}:
                continue
            extra = text[a:b]
            # A repeated prefix/suffix of at least two characters is a review
            # hint, not a linguistic verdict (intentional repetition is valid).
            nearby = final[max(0, i - 12):i] + final[j:j + 12]
            repeated = any(extra[:n] in nearby or extra[-n:] in nearby
                           for n in range(2, min(len(extra), 12) + 1))
            if not repeated:
                continue
            resolved = any(
                isinstance(item, Mapping)
                and str(item.get("evidence_hash", "")).casefold() == str(record["evidence_sha256"]).casefold()
                and compact(item.get("extra_text", "")) == extra
                and item.get("action") in {"asr_artifact", "intentional_repetition"}
                and str(item.get("reason") or "").strip()
                for item in resolutions
            )
            if not resolved:
                errors.append(f"重复语音分歧未裁决: {extra!r}；需剪除时更新实际剪切和粗剪证据；"
                              "识别插入或有意重复则记录 repeat_resolutions，不能仅选流畅文稿放行")
    return errors


def _field(value: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, Mapping):
            candidate = candidate.get("sha256", candidate.get("hash", candidate.get("value")))
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return ""


def _roughcut_hash(value: Mapping[str, Any]) -> str:
    nested = value.get("rough_cut", value.get("roughcut"))
    if isinstance(nested, Mapping):
        return _field(nested, ("sha256", "hash", "rough_cut_sha256"))
    return _field(value, ("rough_cut_sha256", "roughcut_sha256", "source_rough_cut_sha256"))


def _source(value: Mapping[str, Any]) -> str:
    source = _field(value, _SOURCE_KEYS)
    if source:
        return source
    # A nested provenance record is accepted, but its path alone is not used as
    # the independent engine identity when an engine/provider is available.
    provenance = value.get("provenance")
    if isinstance(provenance, Mapping):
        return _field(provenance, _SOURCE_KEYS + ("source_path", "path"))
    return _field(value, ("source_path", "transcript_path"))


def _source_identity(source: str) -> str:
    """Collapse cosmetic provider spelling changes to an engine identity."""
    normalized = re.sub(r"[^a-z0-9]+", "", source.casefold())
    if "whisper" in normalized:
        return "whisper"
    if "funasr" in normalized or "paraformer" in normalized:
        return "funasr"
    return normalized


def _declared_media_sources(value: Mapping[str, Any] | list[Any]) -> tuple[str, ...]:
    """Return media source paths embedded in a raw provider transcript."""
    if not isinstance(value, Mapping):
        return ()
    paths: list[str] = []
    for key in ("source_path", "audio_path", "video_path"):
        candidate = value.get(key)
        if candidate:
            paths.append(str(candidate))
    files = value.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, Mapping):
                continue
            for key in ("source_path", "audio_path", "video_path", "path", "file"):
                candidate = item.get(key)
                if candidate:
                    paths.append(str(candidate))
                    break
    return tuple(paths)


def _same_path(left: str | Path, right: str | Path) -> bool:
    def normalized(value: str | Path) -> str:
        return os.path.normcase(str(Path(value).expanduser().resolve(strict=False)))
    return normalized(left) == normalized(right)


def _transcript_value(value: Mapping[str, Any]) -> Mapping[str, Any] | list[Any]:
    for key in ("transcript", "result", "data"):
        nested = value.get(key)
        if isinstance(nested, (Mapping, list)):
            return nested
    return value


def _funasr_words(value: Mapping[str, Any]) -> tuple[TimedWord, ...]:
    """Support the project's FunASR result/sentence_info shape."""
    result = value.get("result")
    if not isinstance(result, list):
        # A few FunASR exports put sentence_info at the root.  Keep this
        # fallback deliberately narrow; arbitrary prose is not timed evidence.
        result = [{"sentence_info": value.get("sentence_info")}]
    words: list[TimedWord] = []
    for sentence in result:
        if not isinstance(sentence, Mapping):
            continue
        info = sentence.get("sentence_info")
        # The project's Paraformer export has result[0].sentence_info as a
        # list of sentence records.  The previous implementation treated the
        # list as one mapping and consequently returned no timings at all.
        if isinstance(info, list):
            records = info
        elif isinstance(info, Mapping):
            # Keep the parent text as a fallback for the compact test/export
            # shape: {text, sentence_info: {start, end}}.
            records = [{**info, "text": info.get("text", sentence.get("text", ""))}]
        else:
            records = [sentence]
        for item in records:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("text", sentence.get("text", ""))).strip()
            start = item.get("start", item.get("start_ms"))
            end = item.get("end", item.get("end_ms"))
            timestamps = item.get("timestamp", sentence.get("timestamp"))
            if start is None or end is None:
                if not isinstance(timestamps, list) or len(timestamps) < 1:
                    continue
                try:
                    start, end = timestamps[0][0], timestamps[-1][1]
                except (TypeError, ValueError, IndexError):
                    continue
            try:
                start_f, end_f = float(start) / 1000.0, float(end) / 1000.0
            except (TypeError, ValueError):
                continue
            if text and end_f > start_f:
                words.append(TimedWord(start_f, end_f, text, source_path=str(value.get("source_path") or "")))
    return tuple(words)


def _timed_items(value: Mapping[str, Any] | list[Any]) -> tuple[TimedWord, ...]:
    parsed = load_timed_words(value)
    if parsed:
        return parsed
    if isinstance(value, Mapping):
        return _funasr_words(value)
    # FunASR's raw result is commonly a top-level list of sentence records.
    return _funasr_words({"result": value})


def _raw_reference(value: Mapping[str, Any], envelope_path: Path) -> Path | None:
    """Resolve a raw transcript referenced by a small evidence envelope."""
    candidates: list[Any] = []
    for key in ("evidence_path", "raw_evidence_path", "transcript_path", "raw_path"):
        if value.get(key):
            candidates.append(value.get(key))
    for key in ("evidence", "transcript", "raw_evidence"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            for name in ("path", "source_path", "file", "evidence_path", "transcript_path"):
                if nested.get(name):
                    candidates.append(nested.get(name))
    for candidate in candidates:
        path = Path(str(candidate))
        if not path.is_absolute():
            path = envelope_path.parent / path
        if path.is_file():
            return path.resolve()
    return None


def _canonical_hash(value: Mapping[str, Any] | list[Any]) -> str:
    payload = value.get("transcript", value) if isinstance(value, Mapping) else value
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def load_semantic_evidence(
    path: str | Path,
    *,
    expected_source_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read one evidence file and return its real digest and timed content."""
    source_path = Path(path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    value = json.loads(source_path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, Mapping):
        raise ValueError(f"semantic evidence root must be an object: {source_path}")
    raw_path = _raw_reference(value, source_path)
    raw_value: Mapping[str, Any] | list[Any] | None = None
    raw_file_hash = ""
    raw_canonical_hash = ""
    if raw_path is not None:
        try:
            raw_value = json.loads(raw_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(f"raw semantic evidence 不是有效 JSON: {raw_path}") from error
        if not isinstance(raw_value, (Mapping, list)):
            raise ValueError(f"raw semantic evidence root must be object or array: {raw_path}")
        if expected_source_path is not None:
            media_sources = _declared_media_sources(raw_value)
            mismatches = [item for item in media_sources if not _same_path(item, expected_source_path)]
            if mismatches:
                raise ValueError(
                    f"raw semantic evidence source_path 与当前粗剪不匹配: {mismatches[0]}"
                )
        transcript_value = _transcript_value(raw_value) if isinstance(raw_value, Mapping) else raw_value
        raw_file_hash = _digest(raw_path)
        raw_canonical_hash = _canonical_hash(transcript_value)
    else:
        transcript_value = _transcript_value(value)
    words = _timed_items(transcript_value)
    declared_hash = _field(value, _HASH_KEYS)
    actual_hash = _digest(source_path)
    canonical_hash = _canonical_hash(transcript_value)
    evidence_hash = declared_hash or raw_file_hash or actual_hash
    hash_valid = bool(evidence_hash)
    valid_hashes = {actual_hash, canonical_hash, raw_file_hash, raw_canonical_hash} - {""}
    if declared_hash and declared_hash.casefold() not in {item.casefold() for item in valid_hashes}:
        hash_valid = False
    source = _source(value)
    rough_cut_sha256 = _roughcut_hash(value)
    if not words:
        raise ValueError(f"semantic evidence has no timed transcript items: {source_path}")
    value_for_validation = dict(value)
    # Internal provenance used only to resolve a relative adjudication path;
    # it is omitted from the public evidence report below.
    value_for_validation["_envelope_dir"] = str(source_path.resolve().parent)
    return {
        "path": str(source_path.resolve()),
        "value": value_for_validation,
        "words": words,
        "text": "".join(_clean_text(word.text) for word in words),
        "source": source,
        "evidence_sha256": evidence_hash,
        "actual_file_sha256": actual_hash,
        "evidence_payload_path": str(raw_path) if raw_path is not None else str(source_path.resolve()),
        "evidence_payload_sha256": raw_file_hash or actual_hash,
        "canonical_transcript_sha256": canonical_hash,
        "hash_valid": hash_valid,
        "rough_cut_sha256": rough_cut_sha256,
    }


def _adjudication_path(values: Sequence[Mapping[str, Any]]) -> Path | None:
    for value in values:
        for key in ("adjudication_path", "adjudication_record_path"):
            candidate = value.get(key)
            if candidate:
                path = Path(str(candidate))
                if not path.is_absolute():
                    # Evidence envelopes are normally beside their adjudication
                    # record.  The caller may also provide an absolute path.
                    for base in (value.get("_envelope_dir"),):
                        if base:
                            path = Path(str(base)) / path
                            break
                if path.is_file():
                    return path.resolve()
        record = value.get("adjudication", value.get("adjudication_record"))
        if isinstance(record, Mapping):
            candidate = record.get("source_path", record.get("record_path", record.get("path")))
            if candidate:
                path = Path(str(candidate))
                if not path.is_absolute() and value.get("_envelope_dir"):
                    path = Path(str(value["_envelope_dir"])) / path
                if path.is_file():
                    return path.resolve()
    return None


def _decision_hashes(decision: Mapping[str, Any]) -> set[str]:
    hashes = decision.get("evidence_hashes", decision.get("transcript_hashes", decision.get("input_hashes", [])))
    if isinstance(hashes, str):
        hashes = [hashes]
    if not isinstance(hashes, list):
        return set()
    return {str(item).strip().casefold() for item in hashes if str(item).strip()}


def _decision_text_bindings(decision: Mapping[str, Any]) -> dict[str, str]:
    """Extract the explicit hash -> source transcript text binding.

    A structured edit decision must carry both source hashes and the source
    text that each hash names.  This deliberately does not infer text from a
    free-form rationale or from the replacement itself.
    """
    bindings: dict[str, str] = {}
    for key in ("evidence_texts", "input_texts", "transcript_texts", "inputs"):
        candidate = decision.get(key)
        if isinstance(candidate, Mapping):
            for raw_hash, value in candidate.items():
                text = value.get("text", value.get("original_text", value.get("value", ""))) if isinstance(value, Mapping) else value
                if str(raw_hash).strip() and str(text).strip():
                    bindings[str(raw_hash).strip().casefold()] = _clean_text(text)
        elif isinstance(candidate, list):
            for item in candidate:
                if not isinstance(item, Mapping):
                    continue
                raw_hash = _field(item, ("hash", "sha256", "evidence_hash", "transcript_hash"))
                text = item.get("text", item.get("original_text", item.get("value", "")))
                if raw_hash and str(text).strip():
                    bindings[raw_hash.casefold()] = _clean_text(text)
    # The pair records used by the legacy schema are also an explicit binding
    # when a producer puts the text there.
    differences = decision.get("differences", decision.get("disagreements", []))
    if isinstance(differences, Mapping):
        differences = [differences]
    if isinstance(differences, list):
        for item in differences:
            if not isinstance(item, Mapping):
                continue
            for hash_keys, text_keys in (
                (("left_hash", "first_hash"), ("left_text", "first_text")),
                (("right_hash", "second_hash"), ("right_text", "second_text")),
            ):
                raw_hash = _field(item, hash_keys)
                text = item.get(text_keys[0], item.get(text_keys[1], ""))
                if raw_hash and str(text).strip():
                    bindings[raw_hash.casefold()] = _clean_text(text)
    return bindings


def _resolve_decision_path(value: Any, decision_path: Path) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        path = decision_path.parent / path
    return path.resolve(strict=False)


def _document_text(path: Path) -> str:
    """Read checkable text from a supported reference document."""
    suffix = path.suffix.casefold()
    if suffix in {".txt", ".md", ".markdown"}:
        return path.read_text(encoding="utf-8-sig")
    if suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if suffix == ".docx":
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ET.fromstring(xml)
        return "".join(node.text or "" for node in root.iter() if node.tag.casefold().endswith("}t"))
    return ""


def _basis_quote(basis: Mapping[str, Any]) -> tuple[str, str]:
    quote = _clean_text(basis.get("quote", basis.get("quoted_text", basis.get("excerpt", ""))))
    context = _clean_text(
        basis.get(
            "replacement_context",
            basis.get("context", basis.get("supporting_text", basis.get("replacement_quote", ""))),
        )
    )
    return quote, context


def _span_values(value: Any, fallback: Mapping[str, Any]) -> tuple[int, int] | None:
    if isinstance(value, Mapping):
        start_value = value.get("start", value.get("start_index"))
        end_value = value.get("end", value.get("end_index"))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start_value, end_value = value
    else:
        start_value = fallback.get("start", fallback.get("start_index"))
        end_value = fallback.get("end", fallback.get("end_index"))
    if isinstance(start_value, bool) or isinstance(end_value, bool):
        return None
    try:
        start, end = int(start_value), int(end_value)
    except (TypeError, ValueError):
        return None
    return start, end


def _validate_edit_basis(
    basis: Any,
    *,
    decision_path: Path,
    required_hashes: set[str],
    record_texts: Mapping[str, str],
    replacement: str,
) -> bool:
    """Validate an edit's typed, checkable provenance.

    ``user_audio_qa`` and arbitrary prose are intentionally not accepted as
    word-level transcription evidence.  Documents and current-state records
    must be real, hash-bound files; a dual-ASR citation must name both current
    evidence hashes.
    """
    if not isinstance(basis, Mapping):
        return False
    kind = str(
        basis.get("type", basis.get("kind", basis.get("source_type", basis.get("basis_type", ""))))
    ).strip().casefold()
    kind = re.sub(r"[^a-z0-9]+", "_", kind).strip("_")
    if not kind or kind in {"user_audio_qa", "audio_qa", "qa", "prose", "rationale"}:
        return False
    reference = str(
        basis.get("reference", basis.get("ref", basis.get("claim", basis.get("source", "")))) or ""
    ).strip()

    if kind in {"document", "doc", "reference_document", "reference_doc"}:
        source_path = _resolve_decision_path(
            basis.get("path", basis.get("source_path", basis.get("document_path", basis.get("file")))), decision_path
        )
        expected_hash = _field(basis, ("sha256", "source_sha256", "hash", "content_sha256"))
        quote, context = _basis_quote(basis)
        if source_path is None or not source_path.is_file() or not expected_hash or not quote or not context:
            return False
        if not reference:
            reference = str(source_path)
        try:
            if _digest(source_path).casefold() != expected_hash.casefold():
                return False
            source_text = _clean_text(_document_text(source_path))
        except OSError:
            return False
        except (ValueError, UnicodeError, zipfile.BadZipFile, ET.ParseError, KeyError):
            return False
        if (
            quote not in source_text
            or context not in source_text
            or replacement not in quote
            or replacement not in context
        ):
            return False
    elif kind in {"current_state", "confirmed_term", "confirmed_terms", "project_state"}:
        state_path = _resolve_decision_path(
            basis.get(
                "path", basis.get("state_path", basis.get("current_state_path", basis.get("source_path")))
            ), decision_path
        )
        expected_hash = _field(
            basis,
            (
                "confirmed_sha256", "confirmed_terms_sha256", "subtree_sha256",
                "sha256", "source_sha256", "hash", "content_sha256",
            ),
        )
        term = _clean_text(basis.get("term", basis.get("value", basis.get("text", ""))))
        if state_path is None or not state_path.is_file() or not expected_hash or not term:
            return False
        if not reference:
            reference = str(state_path)
        try:
            state_value = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(state_value, Mapping):
            return False
        if "confirmed_terms" in state_value:
            confirmed_subtree = state_value["confirmed_terms"]
        elif "confirmed" in state_value:
            confirmed_subtree = state_value["confirmed"]
        else:
            return False
        try:
            file_hash = _digest(state_path).casefold()
            subtree_hash = _canonical_hash(confirmed_subtree).casefold()
        except (OSError, TypeError, ValueError):
            return False
        if expected_hash.casefold() not in {file_hash, subtree_hash}:
            return False
        confirmed_text = _clean_text(json.dumps(confirmed_subtree, ensure_ascii=False, sort_keys=True))
        if term not in confirmed_text:
            return False
        _quote, context = _basis_quote(basis)
        if context:
            if context not in confirmed_text or replacement not in context:
                return False
        elif replacement != term:
            return False
    elif kind in {"cross_asr", "dual_asr", "double_asr", "asr_cross_check", "two_asr"}:
        cited = basis.get(
            "evidence_hashes", basis.get("input_hashes", basis.get("source_hashes", basis.get("hashes", [])))
        )
        if isinstance(cited, str):
            cited = [cited]
        cited_set = {str(item).strip().casefold() for item in cited} if isinstance(cited, list) else set()
        source_hash = _field(basis, ("source_hash", "source_evidence_hash", "evidence_hash", "transcript_hash"))
        quote, _context = _basis_quote(basis)
        span = _span_values(basis.get("source_span", basis.get("span")), basis)
        if (
            cited_set != required_hashes
            or not reference
            or source_hash.casefold() not in required_hashes
            or not quote
            or span is None
        ):
            return False
        start, end = span
        source_text = _clean_text(record_texts[source_hash.casefold()])
        if start < 0 or end <= start or end > len(source_text) or source_text[start:end] != quote:
            return False
        # The quote must contain the corrected wording and surrounding context;
        # a bare replacement word is not independent ASR evidence.
        if not replacement or replacement not in quote or len(quote) <= len(replacement):
            return False
    else:
        return False

    # If a typed source records the supported term explicitly, bind it to the
    # actual replacement.  Omitted optional term fields remain valid because
    # the source file/hash is still independently checkable.
    supported = basis.get("replacement")
    if supported is not None and str(supported).strip() and _clean_text(supported) != replacement:
        return False
    return True


def _validate_structured_adjudication(
    decision: Mapping[str, Any],
    path: Path,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Validate and reconstruct a hash-bound, per-edit adjudication."""
    required_hashes = {str(item.get("evidence_sha256", "")).casefold() for item in records}
    if len(required_hashes) != 2 or "" in required_hashes:
        return None
    if _decision_hashes(decision) != required_hashes:
        return None
    source = _field(decision, ("source", "decided_by", "reviewer", "record_source"))
    if not source or _source_identity(source) in {"useraudioqa", "audioqa"}:
        return None
    bindings = _decision_text_bindings(decision)
    if set(bindings) != required_hashes:
        return None
    record_texts = {str(item.get("evidence_sha256", "")).casefold(): str(item.get("text", "")) for item in records}
    if any(bindings[item] != _clean_text(record_texts[item]) for item in required_hashes):
        return None

    chosen_hash = _field(decision, ("chosen_hash", "baseline_hash", "selected_hash", "chosen_transcript_hash"))
    chosen_hash = chosen_hash.casefold()
    if chosen_hash not in required_hashes:
        return None
    baseline = record_texts[chosen_hash]
    declared_baseline = decision.get("baseline_text", decision.get("chosen_text", ""))
    if declared_baseline and _clean_text(declared_baseline) != _clean_text(baseline):
        return None
    edits = decision.get("edits", decision.get("text_edits", decision.get("transcript_edits")))
    if not isinstance(edits, list) or not edits:
        return None

    parsed: list[tuple[int, int, str, str]] = []
    for edit in edits:
        if not isinstance(edit, Mapping):
            return None
        position = edit.get("position", edit.get("span"))
        if isinstance(position, Mapping):
            start_value = position.get("start", position.get("start_index"))
            end_value = position.get("end", position.get("end_index"))
        else:
            start_value = edit.get("start", edit.get("start_index"))
            end_value = edit.get("end", edit.get("end_index"))
        if isinstance(start_value, bool) or isinstance(end_value, bool):
            return None
        try:
            start, end = int(start_value), int(end_value)
        except (TypeError, ValueError):
            return None
        original = _clean_text(edit.get("original", edit.get("original_text", edit.get("source_text", ""))))
        replacement = _clean_text(edit.get("replacement", edit.get("replacement_text", edit.get("new_text", ""))))
        if start < 0 or end <= start or end > len(baseline) or not original:
            return None
        if baseline[start:end] != original:
            return None
        if not _validate_edit_basis(
            edit.get("basis", edit.get("evidence", edit.get("provenance"))),
            decision_path=path,
            required_hashes=required_hashes,
            record_texts=record_texts,
            replacement=replacement,
        ):
            return None
        parsed.append((start, end, original, replacement))
    parsed.sort(key=lambda item: (item[0], item[1]))
    if any(left[1] > right[0] for left, right in zip(parsed, parsed[1:])):
        return None
    rebuilt = baseline
    for start, end, _original, replacement in reversed(parsed):
        rebuilt = rebuilt[:start] + replacement + rebuilt[end:]
    final_value = decision.get("final_text")
    if final_value is None:
        return None
    final_text = _clean_text(final_value)
    if final_text != rebuilt:
        return None
    return {
        "path": str(path.resolve()),
        "final_text": final_text,
        "chosen_hash": chosen_hash,
        "edit_count": len(parsed),
        "schema": "structured_edits_v1",
    }


def _validate_adjudication_detail(values: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Read and bind a real adjudication record; prose strings do not count."""
    path = _adjudication_path(values)
    if path is None:
        return None
    try:
        decision = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(decision, Mapping):
        return None
    if any(key in decision for key in ("edits", "text_edits", "transcript_edits")):
        return _validate_structured_adjudication(decision, path, records)
    source = _field(decision, ("source", "decided_by", "reviewer", "record_source"))
    final_text = _clean_text(decision.get("final_text", decision.get("selected_text", decision.get("text", ""))))
    hashes = decision.get("evidence_hashes", decision.get("transcript_hashes", decision.get("input_hashes", [])))
    if isinstance(hashes, str):
        hashes = [hashes]
    hash_set = {str(item).strip().casefold() for item in hashes} if isinstance(hashes, list) else set()
    required_hashes = {str(item.get("evidence_sha256", "")).casefold() for item in records}
    texts = {str(item.get("text", "")).casefold() for item in records}
    # A reviewer/source label by itself is not an adjudication.  The record
    # must carry both input hashes and a structured description of the actual
    # disagreement, tied back to those hashes and transcript texts.
    differences = decision.get("differences", decision.get("disagreements"))
    if isinstance(differences, Mapping):
        differences = [differences]
    if not isinstance(differences, list) or not differences:
        return None
    if not source or not final_text or hash_set != required_hashes or final_text not in texts:
        return None
    valid_pairs = False
    for difference in differences:
        if not isinstance(difference, Mapping):
            continue
        left_hash = str(difference.get("left_hash", difference.get("first_hash", ""))).strip().casefold()
        right_hash = str(difference.get("right_hash", difference.get("second_hash", ""))).strip().casefold()
        left_text = _clean_text(difference.get("left_text", difference.get("first_text", "")))
        right_text = _clean_text(difference.get("right_text", difference.get("second_text", "")))
        if (
            left_hash in required_hashes and right_hash in required_hashes
            and left_hash != right_hash
            and left_text in texts and right_text in texts
            and left_text != right_text
        ):
            valid_pairs = True
            break
    if not valid_pairs:
        return None
    if _source_identity(source) in {"useraudioqa", "audioqa"}:
        return None
    selected_hash = str(decision.get("selected_hash", decision.get("selected_transcript_hash", ""))).strip().casefold()
    if selected_hash and selected_hash not in required_hashes:
        return None
    return {
        "path": str(path.resolve()),
        "final_text": final_text,
        "chosen_hash": selected_hash,
        "edit_count": 0,
        "schema": "whole_transcript_v1",
    }


def _validate_adjudication(values: Sequence[Mapping[str, Any]], records: Sequence[Mapping[str, Any]]) -> str:
    """Compatibility wrapper returning the historical adjudication path."""
    detail = _validate_adjudication_detail(values, records)
    return str(detail["path"]) if detail else ""


def validate_semantic_evidence(
    rough_cut_path: str | Path,
    evidence_paths: Sequence[str | Path],
    *,
    expected_rough_cut_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate two distinct, current-rough-cut-bound semantic evidences."""
    errors: list[str] = []
    if len(evidence_paths) != 2:
        errors.append("必须提供恰好两路独立语义证据文件")
    rough_path = Path(rough_cut_path)
    actual_rough = _digest(rough_path) if rough_path.is_file() else ""
    expected_rough = expected_rough_cut_sha256 or actual_rough
    if not expected_rough:
        errors.append("当前粗剪文件或其 SHA256 缺失")
    if expected_rough_cut_sha256 and actual_rough and expected_rough.casefold() != actual_rough.casefold():
        errors.append("调用方提供的 expected_rough_cut_sha256 与当前粗剪实际 SHA256 不匹配")
    records: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for index, path in enumerate(evidence_paths, 1):
        resolved_path = Path(path).resolve()
        path_key = str(resolved_path).casefold()
        if path_key in seen_paths:
            errors.append(f"第 {index} 路证据与其他 evidence_path 重复")
        seen_paths.add(path_key)
        try:
            records.append(load_semantic_evidence(path, expected_source_path=rough_path))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"第 {index} 路证据无效（source 或 timed transcript 不可用）: {error}")
    for index, record in enumerate(records, 1):
        if not record["source"]:
            errors.append(f"第 {index} 路证据 source 不能为空")
        if not record["evidence_sha256"] or not record["hash_valid"]:
            errors.append(f"第 {index} 路证据 hash 缺失或与文件/转写内容不符")
        if not record["rough_cut_sha256"]:
            errors.append(f"第 {index} 路证据未绑定当前粗剪 SHA256")
        elif record["rough_cut_sha256"].casefold() != actual_rough.casefold():
            errors.append(f"第 {index} 路证据绑定的粗剪 SHA256 不匹配当前粗剪")
    if len(records) == 2:
        if _source_identity(records[0]["source"]) == _source_identity(records[1]["source"]):
            errors.append("两路语义证据 source 必须不同")
        if records[0]["evidence_sha256"].casefold() == records[1]["evidence_sha256"].casefold():
            errors.append("两路语义证据 hash 必须不同")
        if records[0]["evidence_payload_path"].casefold() == records[1]["evidence_payload_path"].casefold():
            errors.append("两路语义证据必须来自不同的实际转写文件")
        divergent = records[0]["text"] != records[1]["text"]
        explicit_divergence = any(
            bool(item.get("value", {}).get("divergence"))
            or bool(item.get("value", {}).get("disagreement"))
            or bool(item.get("value", {}).get("disagreements"))
            for item in records
        )
        adjudication_detail = _validate_adjudication_detail(
            [item["value"] for item in records], records
        )
        adjudication_source = str(adjudication_detail["path"]) if adjudication_detail else ""
        if (divergent or explicit_divergence) and not adjudication_detail:
            errors.append("两路证据存在分歧，但没有 adjudication source")
        if adjudication_detail:
            errors.extend(_repeat_difference_errors(records, adjudication_detail))
    else:
        adjudication_detail = None
        adjudication_source = ""
    return {
        "ok": not errors,
        "errors": errors,
        "rough_cut_sha256": expected_rough,
        "evidence": [
            {key: value for key, value in item.items() if key not in {"value", "words"}}
            for item in records
        ],
        "adjudication_source": adjudication_source if len(records) == 2 else "",
        "adjudication_final_text": (
            str(adjudication_detail.get("final_text", ""))
            if adjudication_detail and len(records) == 2 else ""
        ),
        "adjudication": (
            {key: value for key, value in adjudication_detail.items() if key != "path"}
            | {"path": adjudication_source}
            if adjudication_detail and len(records) == 2 else {}
        ),
    }


def _time(item: Mapping[str, Any], kind: str) -> float | None:
    for key in ((f"{kind}_us",), (kind,)):
        for name in key:
            if item.get(name) is not None:
                try:
                    value = float(item[name])
                except (TypeError, ValueError):
                    return None
                return value / 1_000_000.0 if name.endswith("_us") else value
    return None


def _items(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        for key in ("items", "segments", "captions", "speech", "final_retained_speech", "ordinary_subtitles"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, Mapping)]
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def resolve_replacement_speech(
    window: Mapping[str, Any], speech: list[Mapping[str, Any]], *, tolerance_seconds: float = .04,
) -> list[int]:
    """Bind a replacement to consecutive full sentences, allowing silent padding."""
    label = str(window.get("id") or "替代窗口")
    refs = window.get("replacement_for")
    if not isinstance(refs, list) or not refs or len({str(ref) for ref in refs}) != len(refs):
        raise ValueError(f"{label}: replacement_for 必须按原顺序引用不重复的完整语音句 id")
    indices = []
    for ref in refs:
        matches = [i for i, item in enumerate(speech) if str(item.get("id", item.get("speech_id", i))) == str(ref)]
        if len(matches) != 1:
            raise ValueError(f"{label}: replacement_for 未唯一匹配语音句 {ref}")
        indices.append(matches[0])
    if indices != list(range(indices[0], indices[-1] + 1)):
        raise ValueError(f"{label}: 只能按原顺序替代相邻完整语音句，不能漏句")
    start, end = _time(window, "start"), _time(window, "end")
    if start is None or end is None or start < 0 or end <= start:
        raise ValueError(f"{label}: 替代窗口起止时间无效")
    joined = []
    previous_start = -1.0
    for i, item in enumerate(speech):
        ustart, uend = _time(item, "start"), _time(item, "end")
        if ustart is None or uend is None or uend <= ustart:
            raise ValueError(f"{label}: 原语音时间无效")
        if i in indices:
            if ustart < previous_start or start > ustart + tolerance_seconds or end < uend - tolerance_seconds:
                raise ValueError(f"{label}: 替代窗口必须完整包含所引语音句")
            previous_start = ustart
            joined.append(str(item.get("text", item.get("transcript", ""))))
        elif min(end, uend) > max(start, ustart):
            raise ValueError(f"{label}: 窗口延伸覆盖了未引用语音句，不能吞掉其他句")
    explicit = window.get("ordinary_subtitle_replacement") is True or window.get("allows_ordinary_subtitle_replacement") is True
    complete = window.get("complete") is True or str(window.get("coverage", "")).casefold() == "complete"
    if not explicit or not complete or _clean_text(window.get("text", window.get("content", ""))) != _clean_text("".join(joined)):
        raise ValueError(f"{label}: 完整替代声明或拼接原句文字不匹配")
    mode = window.get("replacement_mode")
    if mode not in (None, "sequential_phrases", "cumulative_sentences", "distributed_sentence"):
        raise ValueError(f"{label}: replacement_mode 不受支持")
    if mode == "cumulative_sentences":
        cumulative_sentence_parts(window, speech, indices)
    if mode == "distributed_sentence":
        if len(indices) != 1:
            raise ValueError(f"{label}: 分工字幕每个窗口绑定一条完整语音句")
        distributed_sentence_parts(window)
    return indices


def distributed_display_phases(part: Mapping[str, Any], window: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Declare continuous layouts of the same assigned phrase, not new speech."""
    phases = part.get('display_phases')
    if (not isinstance(phases, list) or not phases or any(key in part for key in
            ('track_name', 'reveal_start_us', 'source_char_range'))):
        raise ValueError('display_phases 需要非空阶段列表，不能混用单轨或 reveal 绑定')
    cursor, end = window.get('start_us'), window.get('end_us')
    if type(cursor) is not int or type(end) is not int or end <= cursor:
        raise ValueError('display_phases 需要有效父句窗口')
    used = set()
    for phase in phases:
        if not isinstance(phase, Mapping) or set(phase) != {'start_us', 'end_us', 'tracks'}:
            raise ValueError('显示阶段只声明 start_us/end_us/tracks')
        start, stop, tracks = phase['start_us'], phase['end_us'], phase['tracks']
        if (type(start) is not int or type(stop) is not int or start != cursor
                or not start < stop <= end or not isinstance(tracks, list) or not tracks):
            raise ValueError('显示阶段必须无缝且不重叠地覆盖父句窗口')
        names, text = set(), []
        for row in tracks:
            if (not isinstance(row, Mapping) or set(row) != {'track_name', 'segment_index', 'text'}
                    or not isinstance(row['track_name'], str) or not row['track_name'].strip()
                    or type(row['segment_index']) is not int or row['segment_index'] < 0
                    or not isinstance(row['text'], str) or not row['text']):
                raise ValueError('显示阶段须精确声明 track_name/segment_index/text')
            key = (row['track_name'], row['segment_index'])
            if key in used or row['track_name'] in names:
                raise ValueError('显示阶段文字片段不可重复绑定')
            used.add(key); names.add(row['track_name']); text.append(row['text'])
        if _clean_text(''.join(text)) != _clean_text(part.get('text')):
            raise ValueError('每个显示阶段须完整拼回同一分配原文')
        cursor = stop
    if cursor != end:
        raise ValueError('显示阶段没有完整覆盖父句窗口')
    return phases


def distributed_sentence_parts(window: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Assign each original character once to a preset, residual caption or filler.

    Character offsets refer to the whitespace-free source sentence, not invented
    word timestamps. Presets may explicitly reveal within the sentence when
    their existing FunASR evidence is validated by the actual-track binder.
    """
    source = _clean_text(window.get('text'))
    parts = window.get('text_parts')
    if (not isinstance(parts, list) or not parts or any(key in window for key in
            ('template_id', 'node_id', 'track_names', 'replacement_parts'))):
        raise ValueError('分工字幕需要 text_parts，不能混用整句/逐字预设绑定')
    reveals = any(isinstance(p, Mapping) and 'reveal_start_us' in p for p in parts)
    if reveals != ('timing_evidence_path' in window):
        raise ValueError('分工 reveal_start_us 必须与窗口 timing_evidence_path 同时声明')
    cursor, presets = 0, 0
    for index, part in enumerate(parts):
        if not isinstance(part, Mapping):
            raise ValueError('text_parts 条目必须是对象')
        span, role = part.get('source_range'), part.get('role')
        if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                or span[0] != cursor or not cursor < span[1] <= len(source)):
            raise ValueError('text_parts 必须按原顺序完整且不重复地分配原句字符')
        text = source[span[0]:span[1]]
        if _clean_text(part.get('text')) != text:
            raise ValueError('text_parts 文字与原句 source_range 不符')
        if any(key in part for key in ('start_us', 'end_us')) or ('source_char_range' in part and 'reveal_start_us' not in part):
            raise ValueError('分工字幕共用整句时间；逐字切换必须使用已有 ASR 顺序替代')
        if 'reveal_start_us' in part:
            reveal = part['reveal_start_us']
            start, end = window.get('start_us'), window.get('end_us')
            if (role != 'preset' or type(reveal) is not int or type(start) is not int
                    or type(end) is not int or not start <= reveal < end):
                raise ValueError('reveal_start_us 仅允许 preset 在父句窗口内声明整数起点')
            global_span = part.get('source_char_range')
            if (not isinstance(global_span, list) or len(global_span) != 2
                    or any(type(n) is not int for n in global_span) or not 0 <= global_span[0] < global_span[1]):
                raise ValueError('分工 reveal 必须显式绑定已有 FunASR source_char_range')
        if role == 'preset':
            presets += 1
            binding_keys = ('template_id', 'node_id') if 'display_phases' in part else ('template_id', 'node_id', 'track_name')
            if any(not isinstance(part.get(key), str) or not part[key].strip()
                   for key in binding_keys):
                raise ValueError('分工重点文字须绑定 template_id/node_id/track_name')
            if 'display_phases' in part:
                distributed_display_phases(part, window)
        elif role == 'ordinary':
            if any(key in part for key in ('template_id', 'node_id', 'track_name', 'display_phases')):
                raise ValueError('普通字幕由唯一字幕轨承接，不得伪装成预设轨')
        elif role == 'omit_filler':
            # Restrict omissions to actual discourse fillers. Negation, conditions,
            # numbers and qualifiers cannot be waived by a reason or complete=true.
            fillers = {'嗯', '呃', '啊', '比如', '比如说', '就比如说'}
            conjunction = text in {'和', '与', '以及'} and 0 < index < len(parts)-1 and all(
                isinstance(parts[i], Mapping) and parts[i].get('role') == 'preset' for i in (index-1, index+1))
            punctuation = bool(text) and all(char in '，。！？、；：,.!?;:' for char in text)
            if (text not in fillers and not conjunction and not punctuation) or not str(part.get('reason', '')).strip():
                raise ValueError('只能省略明确语气/引导词或并列连接词，否定、条件、数字和限定必须显示')
            if any(key in part for key in ('template_id', 'node_id', 'track_name', 'display_phases')):
                raise ValueError('省略项不能同时绑定可见文字')
        else:
            raise ValueError('text_parts.role 必须为 preset/ordinary/omit_filler')
        cursor = span[1]
    if cursor != len(source) or not presets:
        raise ValueError('分工字幕须完整分配原句并有实际重点文字')
    return parts


def cumulative_sentence_parts(
    window: Mapping[str, Any], speech: list[Mapping[str, Any]], indices: list[int],
) -> list[Mapping[str, Any]]:
    """Bind whole-sentence reveals and optional retention to frozen sentence times.

    ``end_us`` is caption responsibility; ``retained_until_us`` is display only.
    This path never derives sub-sentence timing from text length or ASR words.
    """
    parts = window.get("replacement_parts")
    if not isinstance(parts, list) or len(parts) != len(indices) or len(parts) < 2:
        raise ValueError("累计整句展示须为每个相邻完整语音句声明一个 part，至少两句")
    if any(key in window for key in ("template_id", "node_id", "track_names", "timing_evidence_path", "source_char_range")):
        raise ValueError("累计整句展示不能混用单预设或逐字时间绑定")
    window_start, window_end = window.get("start_us"), window.get("end_us")
    if type(window_start) is not int or type(window_end) is not int:
        raise ValueError("累计整句展示需要整数窗口 start_us/end_us")
    starts = [round(_time(speech[i], "start") * 1_000_000) for i in indices]
    ends = [round(_time(speech[i], "end") * 1_000_000) for i in indices]
    if window_start != starts[0]:
        raise ValueError("累计整句展示窗口必须从第一完整语音句起点开始")
    for offset, (part, index) in enumerate(zip(parts, indices)):
        if not isinstance(part, Mapping):
            raise ValueError("累计整句 replacement_parts 条目必须为对象")
        item = speech[index]
        ref = str(item.get("id", item.get("speech_id", index)))
        refs = part.get("replacement_for")
        if not isinstance(refs, list) or len(refs) != 1 or str(refs[0]) != ref:
            raise ValueError("累计整句 part 必须按原顺序唯一引用对应完整语音句")
        if "source_char_range" in part or "timing_evidence_path" in part:
            raise ValueError("累计整句 part 不接受逐字切分或推算时间")
        if _clean_text(part.get("text")) != _clean_text(item.get("text", item.get("transcript", ""))):
            raise ValueError("累计整句 part 文字必须保留对应完整原句")
        start, end = part.get("start_us"), part.get("end_us")
        expected_end = starts[offset + 1] if offset + 1 < len(parts) else window_end
        if (type(start) is not int or type(end) is not int or start != starts[offset]
                or end != expected_end or end <= start or end < ends[offset]):
            raise ValueError("累计整句 part 须从冻结句起点到下一句起点，末句到组终点，完整覆盖讲话")
        retained = part.get("retained_until_us", end)
        if type(retained) is not int or retained < end or retained > window_end:
            raise ValueError("retained_until_us 只能在原句责任终点与组终点之间")
    return parts


def validate_caption_coverage(
    final_retained_speech: Any,
    ordinary_subtitles: Any,
    preset_replacement_windows: Any = None,
    *,
    tolerance_seconds: float = 0.04,
) -> dict[str, Any]:
    """Check every retained speech item has text/timing coverage.

    A preset can cover a missing ordinary caption only with an explicit,
    complete replacement window and explicit ``replacement_for`` references.
    A base subtitle file being unchanged is never considered coverage.
    """
    errors: list[str] = []
    speech = _items(final_retained_speech)
    captions = _items(ordinary_subtitles)
    windows = _items(preset_replacement_windows)
    if not speech:
        errors.append("final_retained_speech 不能为空")
    if not captions and not windows:
        errors.append("普通字幕和合法预设替代窗口均为空")
    resolved_windows = []
    for window in windows:
        try:
            resolved_windows.append((window, resolve_replacement_speech(window, speech, tolerance_seconds=tolerance_seconds)))
        except ValueError as error:
            errors.append(str(error))
    covered: list[dict[str, Any]] = []
    for index, utterance in enumerate(speech):
        start, end = _time(utterance, "start"), _time(utterance, "end")
        text = _clean_text(utterance.get("text", utterance.get("transcript", "")))
        item_id = str(utterance.get("id", utterance.get("speech_id", index))).strip()
        if start is None or end is None or end <= start or not text:
            errors.append(f"保留语音项 {item_id or index} 缺少有效 text/start/end")
            continue
        match = None
        # A speech item may be split over several ordinary subtitle rows.  A
        # match is valid only when the rows collectively cover the entire time
        # span and their complete normalized text equals the speech text.
        caption_parts: list[tuple[float, float, str, Mapping[str, Any]]] = []
        for caption in captions:
            cstart, cend = _time(caption, "start"), _time(caption, "end")
            ctext = _clean_text(caption.get("text", caption.get("content", "")))
            if cstart is None or cend is None or cend <= cstart or not ctext:
                continue
            # A caption must overlap this utterance with positive duration.
            # The tolerance is used below for tiny coverage gaps, never to
            # pull a merely boundary-touching previous/next caption into it.
            if min(cend, end) > max(cstart, start):
                caption_parts.append((max(cstart, start), min(cend, end), ctext, caption))
        caption_parts.sort(key=lambda item: (item[0], item[1]))
        if caption_parts:
            assembled = ""
            cursor = start
            used: list[Mapping[str, Any]] = []
            contiguous = True
            for cstart, cend, ctext, caption in caption_parts:
                if cend < cursor - tolerance_seconds:
                    continue
                if cstart > cursor + tolerance_seconds:
                    contiguous = False
                    break
                assembled += ctext
                cursor = max(cursor, cend)
                used.append(caption)
            if contiguous and cursor >= end - tolerance_seconds and assembled == text:
                match = {
                    "kind": "ordinary_subtitle",
                    "id": used[0].get("id", "") if used else "",
                    "ids": [caption.get("id", "") for caption in used],
                }
        if match is None:
            for window, indices in resolved_windows:
                if index in indices:
                    match = {"kind": "explicit_preset_replacement", "id": window.get("id", "")}
                    break
        if match is None:
            errors.append(f"保留语音项 {item_id or index} 没有完整普通字幕或合法预设替代窗口")
        else:
            covered.append({"speech_id": item_id, **match})
    return {"ok": not errors, "errors": errors, "covered": covered, "speech_count": len(speech)}


def validate_content_gate(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run semantic evidence and final-speech caption coverage gates."""
    errors: list[str] = []
    semantic = payload.get("semantic_evidence")
    if isinstance(semantic, Mapping) and semantic.get("rough_cut_path") and semantic.get("evidence_paths"):
        semantic_report = validate_semantic_evidence(
            semantic["rough_cut_path"], semantic["evidence_paths"],
            expected_rough_cut_sha256=semantic.get("rough_cut_sha256"),
        )
    else:
        semantic_report = {"ok": False, "errors": ["semantic_evidence 必须提供 rough_cut_path 与两路 evidence_paths"]}
    errors.extend(semantic_report.get("errors", []))
    coverage = validate_caption_coverage(
        payload.get("final_retained_speech", []),
        payload.get("ordinary_subtitles", []),
        payload.get("preset_replacement_windows", []),
    )
    errors.extend(coverage.get("errors", []))
    return {"ok": not errors, "errors": errors, "semantic_evidence": semantic_report, "caption_coverage": coverage}
