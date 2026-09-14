from __future__ import annotations

import hashlib
import json
import pytest
from pathlib import Path

from jianying_adapter.semantic_evidence.content_gate import (
    validate_caption_coverage,
    validate_semantic_evidence,
)


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("short,long", [
    ("因为现在非常流行做自媒体", "因为现在现在非常现在非常流行做自媒体"),
    ("不仅能够打破认知", "不仅能够打不仅能够打破认知"),
])
def test_fluent_adjudication_does_not_hide_retake(tmp_path, short, long):
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"rough")
    digest = hashlib.sha256(b"rough").hexdigest()
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    decision = tmp_path / "decision.json"
    for path, source, text in zip(paths, ("whisper", "funasr"), (short, long)):
        _write(path, {"source": source, "rough_cut_sha256": digest,
                      "segments": [{"start": 0, "end": 4, "text": text}],
                      "adjudication_path": str(decision)})
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    _write(decision, {"source": "main_agent", "evidence_hashes": hashes, "final_text": short,
                      "differences": [{"left_hash": hashes[0], "right_hash": hashes[1],
                                       "left_text": short, "right_text": long}]})
    report = validate_semantic_evidence(rough, paths)
    assert not report["ok"]
    assert any("重复语音分歧未裁决" in error for error in report["errors"])
    value = json.loads(decision.read_text(encoding="utf-8"))
    # The gate allows an explicit editorial decision; it must not auto-delete
    # intentional repetition. The reason is an agent judgment, not audio proof.
    from difflib import SequenceMatcher
    value["repeat_resolutions"] = [
        {"evidence_hash": hashes[1], "extra_text": long[a:b],
         "action": "intentional_repetition", "reason": "本测试保留重复以表现强调，未声称剪除"}
        for tag, i, j, a, b in SequenceMatcher(None, short, long, autojunk=False).get_opcodes()
        if tag in {"insert", "replace"}
    ]
    _write(decision, value)
    report = validate_semantic_evidence(rough, paths)
    assert report["ok"], report["errors"]


def test_native_retake_check_catches_kept_audio_after_text_cleanup(tmp_path):
    from jianying_adapter.candidate_plan import validate_actual_retake_cuts
    source = str(tmp_path / "source.mov")
    edit = tmp_path / "edit.json"
    _write(edit, {"retake_cuts": [{"source_path": source, "source_start_us": 10,
                                   "source_end_us": 20, "reason": "废弃重录"}],
                  "timeline_segments": [{"source_path": source, "source_start_us": 20,
                                          "source_end_us": 40}]})
    segment = {"material_id": "m", "source_timerange": {"start": 0, "duration": 40}}
    draft = {"materials": {"videos": [{"id": "m", "path": source}]},
             "tracks": [{"type": "video", "segments": [segment]}]}
    plan = {"edit_plan": str(edit)}
    assert not validate_actual_retake_cuts(draft, plan)["ok"]
    segment["source_timerange"] = {"start": 20, "duration": 20}
    assert validate_actual_retake_cuts(draft, plan)["ok"]
    draft["tracks"].append({"type": "audio", "segments": [
        {"material_id": "m", "source_timerange": {"start": 10, "duration": 10}}]})
    assert not validate_actual_retake_cuts(draft, plan)["ok"]


def _evidence(path: Path, rough_hash: str, source: str, text: str) -> None:
    _write(path, {
        "source": source,
        "rough_cut_sha256": rough_hash,
        "segments": [{"start": 0, "end": 1, "text": text}],
    })


def test_semantic_gate_reads_two_real_files_and_binds_roughcut(tmp_path: Path) -> None:
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"rough")
    digest = hashlib.sha256(b"rough").hexdigest()
    first, second = tmp_path / "whisper.json", tmp_path / "funasr.json"
    _evidence(first, digest, "whisper_large_v3", "甲乙")
    _write(second, {
        "engine": "funasr",
        "rough_cut_sha256": digest,
        "result": [{"text": "甲乙", "sentence_info": {"start": 0, "end": 1000}}],
    })
    report = validate_semantic_evidence(rough, [first, second])
    assert report["ok"], report["errors"]
    assert len(report["evidence"]) == 2
    assert report["evidence"][0]["evidence_sha256"] != report["evidence"][1]["evidence_sha256"]


def test_semantic_gate_reads_funasr_sentence_info_list_in_milliseconds(tmp_path: Path) -> None:
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"rough")
    digest = hashlib.sha256(b"rough").hexdigest()
    first, second = tmp_path / "whisper.json", tmp_path / "funasr.json"
    _evidence(first, digest, "whisper", "甲乙")
    _write(second, {
        "engine": "funasr-paraformer-zh",
        "rough_cut_sha256": digest,
        "result": [{
            "text": "甲乙",
            "sentence_info": [
                {"text": "甲", "start": 0, "end": 450, "timestamp": [[0, 450]]},
                {"text": "乙", "start": 450, "end": 1000, "timestamp": [[450, 1000]]},
            ],
        }],
    })
    report = validate_semantic_evidence(rough, [first, second])
    assert report["ok"], report["errors"]
    assert report["evidence"][1]["text"] == "甲乙"


def test_semantic_gate_binds_raw_media_source_and_rejects_engine_aliases(tmp_path: Path) -> None:
    rough = tmp_path / "rough.mp4"
    other = tmp_path / "other.mp4"
    rough.write_bytes(b"rough")
    other.write_bytes(b"other")
    digest = hashlib.sha256(b"rough").hexdigest()
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    _write(first, {
        "source": "whisper-large-v3-turbo", "rough_cut_sha256": digest,
        "transcript_path": str(tmp_path / "raw-a.json"),
    })
    _write(second, {
        "source": "whisper_large_v3", "rough_cut_sha256": digest,
        "transcript_path": str(tmp_path / "raw-b.json"),
    })
    _write(tmp_path / "raw-a.json", {"source_path": str(rough), "segments": [{"start": 0, "end": 1, "text": "甲"}]})
    _write(tmp_path / "raw-b.json", {"source_path": str(other), "segments": [{"start": 0, "end": 1, "text": "乙"}]})
    report = validate_semantic_evidence(rough, [first, second])
    assert not report["ok"]
    assert any("source_path" in error for error in report["errors"])
    assert any("source" in error for error in report["errors"])


def test_semantic_gate_rejects_empty_self_attested_records_and_disagreement_without_source(tmp_path: Path) -> None:
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"rough")
    digest = hashlib.sha256(b"rough").hexdigest()
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    _evidence(first, digest, "same", "甲")
    _write(second, {"source": "same", "rough_cut_sha256": digest, "evidence": [{}]})
    report = validate_semantic_evidence(rough, [first, second])
    assert not report["ok"]
    assert any("source" in error for error in report["errors"])
    assert any("timed transcript" in error for error in report["errors"])


def test_caption_coverage_requires_final_speech_and_allows_only_explicit_replacement() -> None:
    speech = [{"id": "s1", "start": 0, "end": 1, "text": "完整语音"}]
    ordinary = [{"id": "c1", "start": 0, "end": 1, "text": "完整语音"}]
    passed = validate_caption_coverage(speech, ordinary)
    assert passed["ok"], passed["errors"]
    denied = validate_caption_coverage(speech, [])
    assert not denied["ok"]
    allowed = validate_caption_coverage(
        speech,
        [],
        [{
            "id": "p1", "start": 0, "end": 1,
            "text": "完整语音",
            "ordinary_subtitle_replacement": True,
            "complete": True,
            "replacement_for": ["s1"],
        }],
    )
    assert allowed["ok"], allowed["errors"]


def test_caption_coverage_rejects_partial_subtitle_substring() -> None:
    speech = [{"id": "s1", "start": 0, "end": 1, "text": "甲乙丙"}]
    report = validate_caption_coverage(
        speech, [{"id": "c1", "start": 0, "end": 1, "text": "乙"}]
    )
    assert not report["ok"]


def test_caption_coverage_accepts_contiguous_speech_without_boundary_bleed() -> None:
    speech = [
        {"id": "s1", "start": 0, "end": 1, "text": "甲"},
        {"id": "s2", "start": 1, "end": 2, "text": "乙"},
    ]
    ordinary = [
        {"id": "c1", "start": 0, "end": 1, "text": "甲"},
        {"id": "c2", "start": 1, "end": 2, "text": "乙"},
    ]
    report = validate_caption_coverage(speech, ordinary)
    assert report["ok"], report["errors"]


def test_caption_coverage_accepts_one_speech_split_across_subtitles() -> None:
    speech = [{"id": "s1", "start": 0, "end": 2, "text": "甲乙丙"}]
    ordinary = [
        {"id": "c1", "start": 0, "end": 0.7, "text": "甲"},
        {"id": "c2", "start": 0.7, "end": 2, "text": "乙丙"},
    ]
    report = validate_caption_coverage(speech, ordinary)
    assert report["ok"], report["errors"]


def test_adjudication_requires_hash_bound_structured_difference(tmp_path: Path) -> None:
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"rough")
    digest = hashlib.sha256(b"rough").hexdigest()
    first, second, decision = (tmp_path / name for name in ("a.json", "b.json", "decision.json"))
    # Bind the same record from both evidence envelopes.
    _write(first, {"source": "whisper", "rough_cut_sha256": digest,
                   "segments": [{"start": 0, "end": 1, "text": "甲乙"}],
                   "adjudication_path": str(decision)})
    _write(second, {"source": "funasr", "rough_cut_sha256": digest,
                    "segments": [{"start": 0, "end": 1, "text": "甲丙"}],
                    "adjudication_path": str(decision)})
    first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
    second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
    # The envelopes use their file hashes when no declared transcript hash is
    # supplied, so the adjudication must reference those actual hashes.
    _write(decision, {
        "source": "human_review",
        "evidence_hashes": [first_hash, second_hash],
        "selected_text": "甲乙",
        "differences": [{
            "left_hash": first_hash,
            "right_hash": second_hash,
            "left_text": "甲乙",
            "right_text": "甲丙",
        }],
    })
    report = validate_semantic_evidence(rough, [first, second])
    assert report["ok"], report["errors"]

    _write(decision, {"source": "human_review", "selected_text": "甲乙"})
    report = validate_semantic_evidence(rough, [first, second])
    assert not report["ok"]
    assert any("adjudication" in error or "分歧" in error for error in report["errors"])


def _structured_pair(tmp_path: Path) -> tuple[Path, list[Path], str, str]:
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"rough")
    rough_hash = hashlib.sha256(b"rough").hexdigest()
    first, second, decision = (tmp_path / name for name in ("a.json", "b.json", "decision.json"))
    _evidence(first, rough_hash, "whisper", "甲乙")
    _evidence(second, rough_hash, "funasr", "甲丙")
    for path in (first, second):
        value = json.loads(path.read_text(encoding="utf-8"))
        value["adjudication_path"] = str(decision)
        _write(path, value)
    first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
    second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
    _write(decision, {
        "source": "transcript_adjudicator",
        "evidence_hashes": [first_hash, second_hash],
        "evidence_texts": {first_hash: "甲乙", second_hash: "甲丙"},
        "chosen_hash": first_hash,
        "baseline_text": "甲乙",
        "edits": [{
            "position": {"start": 1, "end": 2},
            "original": "乙",
            "replacement": "丙",
            "basis": {
                "type": "cross_asr",
                "reference": "双ASR交叉证据",
                "source_hash": second_hash,
                "source_span": {"start": 0, "end": 2},
                "quote": "甲丙",
                "evidence_hashes": [first_hash, second_hash],
            },
        }],
        "final_text": "甲丙",
    })
    return rough, [first, second], first_hash, second_hash


def test_structured_adjudication_rebuilds_mixed_transcript_and_returns_final_text(tmp_path: Path) -> None:
    rough, evidences, _first_hash, _second_hash = _structured_pair(tmp_path)
    report = validate_semantic_evidence(rough, evidences)
    assert report["ok"], report["errors"]
    assert report["adjudication_final_text"] == "甲丙"
    assert report["adjudication"]["schema"] == "structured_edits_v1"
    assert report["adjudication"]["edit_count"] == 1


def test_structured_adjudication_rejects_undeclared_extra_change(tmp_path: Path) -> None:
    rough, evidences, first_hash, second_hash = _structured_pair(tmp_path)
    decision = tmp_path / "decision.json"
    _write(decision, {
        "source": "transcript_adjudicator",
        "evidence_hashes": [first_hash, second_hash],
        "evidence_texts": {first_hash: "甲乙", second_hash: "甲丙"},
        "chosen_hash": first_hash,
        "edits": [{
            "start": 1, "end": 2, "original": "乙", "replacement": "丙",
            "basis": {"type": "cross_asr", "reference": "双ASR", "source_hash": second_hash,
                       "source_span": {"start": 0, "end": 2}, "quote": "甲丙",
                       "evidence_hashes": [first_hash, second_hash]},
        }],
        "final_text": "甲戊",
    })
    # The helper already binds both envelopes to the decision path.  Keep
    # those files unchanged so their hashes remain the current input hashes.
    for path in evidences:
        assert path.is_file()
    assert not validate_semantic_evidence(rough, evidences)["ok"]


def test_structured_adjudication_rejects_bad_hash_original_or_missing_basis(tmp_path: Path) -> None:
    rough, evidences, first_hash, second_hash = _structured_pair(tmp_path)
    decision = tmp_path / "decision.json"
    base = {
        "source": "transcript_adjudicator",
        "evidence_hashes": [first_hash, second_hash],
        "evidence_texts": {first_hash: "甲乙", second_hash: "甲丙"},
        "chosen_hash": first_hash,
        "edits": [{
            "start": 1, "end": 2, "original": "乙", "replacement": "丙",
            "basis": {"type": "cross_asr", "reference": "双ASR", "source_hash": second_hash,
                       "source_span": {"start": 0, "end": 2}, "quote": "甲丙",
                       "evidence_hashes": [first_hash, second_hash]},
        }],
        "final_text": "甲丙",
    }
    cases = []
    wrong_hash = dict(base, chosen_hash="stale-hash")
    cases.append(wrong_hash)
    wrong_original = dict(base, edits=[dict(base["edits"][0], original="甲")])
    cases.append(wrong_original)
    missing_basis = dict(base, edits=[dict(base["edits"][0], basis=None)])
    cases.append(missing_basis)
    for index, value in enumerate(cases):
        case = tmp_path / f"decision-{index}.json"
        _write(case, value)
        for path in evidences:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["adjudication_path"] = str(case)
            # Rebinding would change the evidence hash.  Use the original
            # helper decision path and replace its JSON content per case.
            original_decision = tmp_path / "decision.json"
            original_decision.write_text(case.read_text(encoding="utf-8"), encoding="utf-8")
        report = validate_semantic_evidence(rough, evidences)
        assert not report["ok"], (index, report)


def test_structured_adjudication_rejects_missing_or_audio_qa_basis_source(tmp_path: Path) -> None:
    rough, evidences, first_hash, second_hash = _structured_pair(tmp_path)
    decision = tmp_path / "decision.json"
    base = {
        "source": "transcript_adjudicator",
        "evidence_hashes": [first_hash, second_hash],
        "evidence_texts": {first_hash: "甲乙", second_hash: "甲丙"},
        "chosen_hash": first_hash,
        "edits": [{
            "start": 1, "end": 2, "original": "乙", "replacement": "丙",
            "basis": {"type": "user_audio_qa", "reference": "人工听感"},
        }],
        "final_text": "甲丙",
    }
    _write(decision, base)
    # The helper already points the envelopes at decision.json.
    report = validate_semantic_evidence(rough, evidences)
    assert not report["ok"]


def test_cross_asr_requires_real_source_span_quote_and_replacement_context(tmp_path: Path) -> None:
    rough, evidences, first_hash, second_hash = _structured_pair(tmp_path)
    decision = tmp_path / "decision.json"
    value = {
        "source": "transcript_adjudicator",
        "evidence_hashes": [first_hash, second_hash],
        "evidence_texts": {first_hash: "甲乙", second_hash: "甲丙"},
        "chosen_hash": first_hash,
        "edits": [{
            "start": 1, "end": 2, "original": "乙", "replacement": "戊",
            "basis": {
                "type": "cross_asr", "reference": "双ASR", "source_hash": second_hash,
                "source_span": {"start": 0, "end": 2}, "quote": "甲丙",
                "evidence_hashes": [first_hash, second_hash],
            },
        }],
        "final_text": "甲戊",
    }
    _write(decision, value)
    assert not validate_semantic_evidence(rough, evidences)["ok"]


def test_document_basis_requires_real_quote_and_replacement_context(tmp_path: Path) -> None:
    rough, evidences, first_hash, second_hash = _structured_pair(tmp_path)
    document = tmp_path / "terms.md"
    document.write_text("已确认术语：甲丙。", encoding="utf-8")
    document_hash = hashlib.sha256(document.read_bytes()).hexdigest()
    decision = tmp_path / "decision.json"
    value = {
        "source": "transcript_adjudicator",
        "evidence_hashes": [first_hash, second_hash],
        "evidence_texts": {first_hash: "甲乙", second_hash: "甲丙"},
        "chosen_hash": first_hash,
        "edits": [{
            "start": 1, "end": 2, "original": "乙", "replacement": "丙",
            "basis": {
                "type": "document", "reference": "术语文档", "path": str(document),
                "sha256": document_hash, "quote": "甲丙", "replacement_context": "甲丙",
            },
        }],
        "final_text": "甲丙",
    }
    _write(decision, value)
    report = validate_semantic_evidence(rough, evidences)
    assert report["ok"], report["errors"]

    value["edits"][0]["basis"]["quote"] = "虚构甲丙"
    _write(decision, value)
    assert not validate_semantic_evidence(rough, evidences)["ok"]


def test_current_state_basis_reads_confirmed_subtree_only_and_accepts_subtree_hash(tmp_path: Path) -> None:
    rough, evidences, first_hash, second_hash = _structured_pair(tmp_path)
    state = tmp_path / "project_state.json"
    state_value = {"confirmed": ["术语：甲丙"], "pending": ["戊"]}
    _write(state, state_value)
    confirmed_hash = hashlib.sha256(
        json.dumps(state_value["confirmed"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    decision = tmp_path / "decision.json"
    value = {
        "source": "transcript_adjudicator",
        "evidence_hashes": [first_hash, second_hash],
        "evidence_texts": {first_hash: "甲乙", second_hash: "甲丙"},
        "chosen_hash": first_hash,
        "edits": [{
            "start": 1, "end": 2, "original": "乙", "replacement": "丙",
            "basis": {
                "type": "current_state", "reference": "confirmed", "path": str(state),
                "confirmed_terms_sha256": confirmed_hash, "term": "丙", "context": "术语：甲丙",
            },
        }],
        "final_text": "甲丙",
    }
    _write(decision, value)
    report = validate_semantic_evidence(rough, evidences)
    assert report["ok"], report["errors"]

    value["edits"][0]["basis"]["term"] = "戊"
    value["edits"][0]["basis"]["context"] = "戊"
    _write(decision, value)
    assert not validate_semantic_evidence(rough, evidences)["ok"]
