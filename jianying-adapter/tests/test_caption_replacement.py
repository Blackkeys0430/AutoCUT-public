from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jianying_adapter.candidate_plan import _actual_content_coverage, get_shared_operation_registry
from jianying_adapter.caption_replacement import replace_ordinary_captions_with_presets
from jianying_adapter.shared_operations import _caption_track, shared_operation_handlers
from jianying_adapter.semantic_evidence.content_gate import validate_caption_coverage


def fixture(parts=None):
    parts = parts or ["你永远赚不到认知以外的钱"]
    sentence = "你永远赚不到认知以外的钱"
    names = [f"JY_PRESET_DEMO__NODE__quote_{i}" for i in range(len(parts))]
    tracks, materials = [], []
    for i, (name, text) in enumerate(zip(["JY_ZH_SUBTITLES", *names], [sentence, *parts])):
        tracks.append({"name": name, "type": "text", "segments": [{"id": f"s{i}", "material_id": f"m{i}",
            "target_timerange": {"start": 1_000_000, "duration": 2_000_000}}]})
        materials.append({"id": f"m{i}", "content": json.dumps({"text": text})})
    window = {"id": "quote-replacement", "start_us": 1_000_000, "end_us": 3_000_000,
        "text": sentence, "replacement_for": ["utterance-1"], "complete": True,
        "ordinary_subtitle_replacement": True, "template_id": "DEMO", "node_id": "quote", "track_names": names}
    content = {"final_retained_speech": [{"id": "utterance-1", "start_us": 1_000_000, "end_us": 3_000_000, "text": sentence}],
        "ordinary_subtitles": [{"start_us": 1_000_000, "end_us": 3_000_000, "text": sentence}],
        "preset_replacement_windows": [window]}
    plan = {"content_gate": content, "presets": [{"template_id": "DEMO", "node_id": "quote",
        "actual_text_tracks": [{"track_name": n, "text": t} for n, t in zip(names, parts)]}]}
    return {"tracks": tracks, "materials": {"texts": materials}}, SimpleNamespace(plan=plan), {"window_ids": [window["id"]]}


@pytest.mark.parametrize("parts", [None, ["你永远赚不到", "认知以外的钱"]])
def test_complete_actual_preset_removes_only_ordinary_track_and_passes_final_coverage(parts):
    draft, ctx, spec = fixture(parts)
    # An untouched following sentence is retained.
    next_segment = {"id": "next", "material_id": "next", "target_timerange": {"start": 3_000_000, "duration": 1_000_000}}
    draft["tracks"][0]["segments"].append(next_segment)
    draft["materials"]["texts"].append({"id": "next", "content": json.dumps({"text": "下一句"})})
    ctx.plan["content_gate"]["final_retained_speech"].append({"id": "next", "start_us": 3_000_000, "end_us": 4_000_000, "text": "下一句"})
    preset_before = deepcopy(draft["tracks"][1:])
    material_before = deepcopy(draft["materials"])
    report = replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert report["removed_caption_count"] == 1
    assert draft["tracks"][0]["segments"] == [next_segment]
    assert draft["tracks"][1:] == preset_before
    assert draft["materials"] == material_before
    assert _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])["ok"]
    assert _caption_track(draft) is draft["tracks"][0]


@pytest.mark.parametrize("defect", ["missing_preset", "lost_negation", "late_track", "crossing_caption", "false_speech_ref", "undeclared_track", "wrong_order"])
def test_invalid_handoff_is_atomic(defect):
    draft, ctx, spec = fixture(["你永远赚不到", "认知以外的钱"])
    window = ctx.plan["content_gate"]["preset_replacement_windows"][0]
    if defect == "missing_preset":
        draft["tracks"].pop()
    elif defect == "lost_negation":
        draft["materials"]["texts"][1]["content"] = json.dumps({"text": "你永远赚到"})
    elif defect == "late_track":
        draft["tracks"][2]["segments"][0]["target_timerange"] = {"start": 1_500_000, "duration": 1_500_000}
    elif defect == "crossing_caption":
        draft["tracks"][0]["segments"][0]["target_timerange"]["duration"] = 2_500_000
    elif defect == "false_speech_ref":
        window["replacement_for"] = ["different-sentence"]
    elif defect == "undeclared_track":
        ctx.plan["presets"][0]["actual_text_tracks"].pop()
    elif defect == "wrong_order":
        window["track_names"].reverse()
    before = deepcopy(draft)
    with pytest.raises(ValueError):
        replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft == before


def test_final_gate_rejects_self_attested_window_and_remaining_duplicate():
    draft, ctx, spec = fixture()
    result = _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])
    assert not result["ok"]
    assert any("重复表达" in e for e in result["errors"])
    replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])["ok"]
    draft["tracks"] = draft["tracks"][:1]
    result = _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])
    assert not result["ok"]
    assert any("实际预设轨道" in e for e in result["errors"])
    assert any("没有完整普通字幕" in e for e in result["errors"])


def test_final_gate_detects_post_operation_text_or_timing_tampering():
    draft, ctx, spec = fixture()
    replace_ordinary_captions_with_presets(draft, spec, ctx)
    draft["materials"]["texts"][1]["content"] = json.dumps({"text": "认知以外的钱"})
    assert not _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])["ok"]


def test_operation_registered_and_repeat_removal_rejected():
    assert shared_operation_handlers()["replace_ordinary_captions_with_presets"] is replace_ordinary_captions_with_presets
    get_shared_operation_registry()
    draft, ctx, spec = fixture()
    replace_ordinary_captions_with_presets(draft, spec, ctx)
    before = deepcopy(draft)
    with pytest.raises(ValueError, match="只能移除完整"):
        replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft == before


def test_speech_id_falls_back_to_original_list_index():
    draft, ctx, spec = fixture()
    content = ctx.plan["content_gate"]
    del content["final_retained_speech"][0]["id"]
    content["final_retained_speech"].insert(0, {"start_us": 0, "end_us": 1_000_000, "text": "前句"})
    content["preset_replacement_windows"][0]["replacement_for"] = ["1"]
    assert replace_ordinary_captions_with_presets(draft, spec, ctx)["removed_caption_count"] == 1


def test_replacement_allows_caption_tail_padding_until_next_speech():
    draft, ctx, spec = fixture()
    content = ctx.plan["content_gate"]
    content["final_retained_speech"][0]["end_us"] = 2_640_000
    # Caption/preset remain visible for another 360 ms in the silent gap.
    content["final_retained_speech"].append({"id": "next", "start_us": 3_000_000, "end_us": 4_000_000, "text": "下一句"})
    draft["tracks"][0]["segments"].append({"id": "next", "material_id": "next", "target_timerange": {"start": 3_000_000, "duration": 1_000_000}})
    draft["materials"]["texts"].append({"id": "next", "content": json.dumps({"text": "下一句"})})
    assert replace_ordinary_captions_with_presets(draft, spec, ctx)["removed_caption_count"] == 1
    assert _actual_content_coverage(draft, ctx.plan, content)["ok"]


def two_sentence_fixture():
    draft, ctx, spec = fixture()
    content = ctx.plan["content_gate"]
    content["final_retained_speech"] = [
        {"id": "a", "start_us": 1_000_000, "end_us": 1_750_000, "text": "你永远赚不到"},
        {"id": "b", "start_us": 2_000_000, "end_us": 2_800_000, "text": "认知以外的钱"},
    ]
    content["preset_replacement_windows"][0]["replacement_for"] = ["a", "b"]
    draft["tracks"][0]["segments"] = [
        {"id": "a", "material_id": "a", "target_timerange": {"start": 1_000_000, "duration": 800_000}},
        {"id": "b", "material_id": "b", "target_timerange": {"start": 2_000_000, "duration": 1_000_000}},
    ]
    draft["materials"]["texts"].extend([
        {"id": "a", "content": json.dumps({"text": "你永远赚不到"})},
        {"id": "b", "content": json.dumps({"text": "认知以外的钱"})},
    ])
    return draft, ctx, spec


def test_one_complete_preset_replaces_adjacent_sentences_with_silent_gaps():
    draft, ctx, spec = two_sentence_fixture()
    assert replace_ordinary_captions_with_presets(draft, spec, ctx)["removed_caption_count"] == 2
    result = _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])
    assert result["ok"], result["errors"]
    assert [row["speech_id"] for row in result["covered"]] == ["a", "b"]


@pytest.mark.parametrize("defect", ["missed_middle", "reversed", "next_speech_in_padding", "missing_sentence_text"])
def test_multiple_sentence_handoff_rejects_missing_or_swallowed_speech(defect):
    draft, ctx, spec = two_sentence_fixture()
    content = ctx.plan["content_gate"]
    if defect == "missed_middle":
        content["final_retained_speech"].insert(1, {"id": "middle", "start_us": 1_800_000, "end_us": 1_900_000, "text": "别漏句"})
    elif defect == "reversed":
        content["preset_replacement_windows"][0]["replacement_for"].reverse()
    elif defect == "next_speech_in_padding":
        content["final_retained_speech"].append({"id": "next", "start_us": 2_900_000, "end_us": 3_500_000, "text": "不能吞掉"})
    elif defect == "missing_sentence_text":
        content["preset_replacement_windows"][0]["text"] = "你永远赚不到"
    before = deepcopy(draft)
    with pytest.raises(ValueError):
        replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft == before


def sequential_fixture(tmp_path):
    draft, ctx, spec = fixture(["你永远赚不到", "认知以外的钱"])
    content = ctx.plan["content_gate"]
    window = content["preset_replacement_windows"][0]
    raw = tmp_path / "funasr.json"
    rough = tmp_path / "rough.mp4"
    rough.write_bytes(b"current rough cut")
    rough_hash = hashlib.sha256(rough.read_bytes()).hexdigest()
    raw.write_text(json.dumps({"source_path": str(rough), "result": [{
        "text": "你永远赚不到，认知以外的钱。", "start": 1000, "end": 3000,
        "timestamp": [[1000 + i * 100, 1100 + i * 100] for i in range(6)] +
                     [[2000 + i * 100, 2100 + i * 100] for i in range(6)]}]}), encoding="utf-8")
    envelope = tmp_path / "evidence.json"
    envelope.write_text(json.dumps({"source": "funasr-paraformer", "evidence_path": str(raw),
        "evidence_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(), "rough_cut_sha256": rough_hash}), encoding="utf-8")
    content["semantic_evidence"] = {"rough_cut_path": str(rough), "rough_cut_sha256": rough_hash, "evidence_paths": [str(envelope)]}
    content["final_retained_speech"][0].update(source_char_range=[0, 12], end_us=2_600_000)
    window["timing_evidence_path"] = str(envelope)
    names = window.pop("track_names")
    window.pop("template_id")
    window.pop("node_id")
    window["replacement_parts"] = [
        {"template_id": "DEMO", "node_id": "quote", "track_names": [names[0]],
         "text": "你永远赚不到", "start_us": 1_000_000, "end_us": 2_000_000, "source_char_range": [0, 6]},
        {"template_id": "OTHER", "node_id": "answer", "track_names": ["JY_PRESET_OTHER__NODE__answer_0"],
         "text": "认知以外的钱", "start_us": 2_000_000, "end_us": 3_000_000, "source_char_range": [6, 12]},
    ]
    ctx.plan["presets"][0]["actual_text_tracks"] = ctx.plan["presets"][0]["actual_text_tracks"][:1]
    ctx.plan["presets"].append({"template_id": "OTHER", "node_id": "answer", "actual_text_tracks": [{"track_name": "JY_PRESET_OTHER__NODE__answer_0", "text": "认知以外的钱"}]})
    draft["tracks"][1]["segments"][0]["target_timerange"]["duration"] = 1_000_000
    draft["tracks"][2]["name"] = "JY_PRESET_OTHER__NODE__answer_0"
    draft["tracks"][2]["segments"][0]["target_timerange"] = {"start": 2_000_000, "duration": 1_000_000}
    return draft, ctx, spec


def test_sequential_native_presets_replace_one_frozen_sentence(tmp_path):
    draft, ctx, spec = sequential_fixture(tmp_path)
    speech_before = deepcopy(ctx.plan["content_gate"]["final_retained_speech"])
    assert replace_ordinary_captions_with_presets(draft, spec, ctx)["removed_caption_count"] == 1
    assert ctx.plan["content_gate"]["final_retained_speech"] == speech_before
    report = _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])
    assert report["ok"], report["errors"]
    # Writer's actual-content check re-reads native timings after construction.
    draft["tracks"][1]["segments"][0]["target_timerange"]["duration"] += 500_000
    assert not _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])["ok"]


@pytest.mark.parametrize("defect", ["gap", "overlap", "guessed_switch", "lost_negation", "reversed_chars", "missing_tail", "raw_changed", "wrong_evidence", "foreign_media", "wrong_sentence_time", "missing_character_time", "actual_lingering_text", "mixed_declaration"])
def test_sequential_handoff_rejects_unbound_or_incomplete_parts_atomically(tmp_path, defect):
    draft, ctx, spec = sequential_fixture(tmp_path)
    content = ctx.plan["content_gate"]
    window = content["preset_replacement_windows"][0]
    parts = window["replacement_parts"]
    if defect == "mixed_declaration":
        window["template_id"] = "DEMO"
    elif defect == "gap":
        parts[1]["start_us"] += 100_000
    elif defect == "overlap":
        parts[1]["start_us"] -= 100_000
    elif defect == "guessed_switch":
        parts[0]["end_us"] = parts[1]["start_us"] = 1_800_000
    elif defect == "lost_negation":
        parts[0]["text"] = "你永远赚到"
    elif defect == "reversed_chars":
        parts[0]["source_char_range"] = [6, 12]
    elif defect == "missing_tail":
        parts[1]["end_us"] -= 100_000
    elif defect == "wrong_evidence":
        window["timing_evidence_path"] = str(tmp_path / "different.json")
    elif defect == "wrong_sentence_time":
        content["final_retained_speech"][0]["end_us"] -= 100_000
    elif defect == "actual_lingering_text":
        draft["tracks"][1]["segments"][0]["target_timerange"]["duration"] += 300_000
    else:
        raw = tmp_path / "funasr.json"
        value = json.loads(raw.read_text(encoding="utf-8"))
        if defect == "foreign_media":
            value["source_path"] = str(tmp_path / "other.mp4")
        elif defect == "missing_character_time":
            value["result"][0]["timestamp"].pop()
        else:
            value["result"][0]["timestamp"][6][0] = 2100
        raw.write_text(json.dumps(value), encoding="utf-8")
        if defect != "raw_changed":
            env_path = tmp_path / "evidence.json"
            envelope = json.loads(env_path.read_text(encoding="utf-8"))
            envelope["evidence_sha256"] = hashlib.sha256(raw.read_bytes()).hexdigest()
            env_path.write_text(json.dumps(envelope), encoding="utf-8")
    before = deepcopy(draft)
    with pytest.raises(ValueError):
        replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft == before


def test_sequential_handoff_uses_existing_semantic_gate_file(tmp_path):
    draft, ctx, spec = sequential_fixture(tmp_path)
    semantic = ctx.plan["content_gate"].pop("semantic_evidence")
    gate = tmp_path / "semantic_gate.json"
    gate.write_text(json.dumps({"rough_cut": {"path": semantic["rough_cut_path"], "sha256": semantic["rough_cut_sha256"]},
                               "evidence_paths": semantic["evidence_paths"]}), encoding="utf-8")
    ctx.plan["semantic_gate"] = str(gate)
    assert replace_ordinary_captions_with_presets(draft, spec, ctx)["removed_caption_count"] == 1


def cumulative_fixture():
    texts = ["第一条主线", "这是第一条的说明", "第二条主线"]
    starts, ends = [1_000_000, 2_000_000, 3_500_000], [2_000_000, 3_500_000, 5_000_000]
    speech = [{"id": f"sentence-{i}", "start_us": start, "end_us": end, "text": text}
              for i, (text, start, end) in enumerate(zip(texts, starts, ends))]
    parts, presets, tracks, materials, ordinary = [], [], [], [], []
    for i, item in enumerate(speech):
        name = f"JY_PRESET_DEMO__NODE__sentence-{i}_0"
        part = {**item, "replacement_for": [item["id"]], "template_id": "DEMO",
                "node_id": item["id"], "track_names": [name]}
        if i == 0:
            part["retained_until_us"] = ends[-1]
        parts.append(part)
        presets.append({"template_id": "DEMO", "node_id": item["id"],
                        "actual_text_tracks": [{"track_name": name, "text": item["text"]}]})
        material = {"id": f"m{i}", "content": json.dumps({"text": item["text"]})}
        segment = {"id": f"p{i}", "material_id": material["id"], "target_timerange": {
            "start": starts[i], "duration": part.get("retained_until_us", ends[i]) - starts[i]}}
        tracks.append({"name": name, "type": "text", "segments": [segment]})
        materials.append(material)
        ordinary.append({"id": f"c{i}", "material_id": material["id"],
                         "target_timerange": {"start": starts[i], "duration": ends[i] - starts[i]}})
    window = {"id": "cumulative", "replacement_mode": "cumulative_sentences",
              "start_us": starts[0], "end_us": ends[-1], "text": "".join(texts),
              "replacement_for": [s["id"] for s in speech], "replacement_parts": parts,
              "ordinary_subtitle_replacement": True, "complete": True}
    content = {"final_retained_speech": speech, "ordinary_subtitles": deepcopy(speech),
               "preset_replacement_windows": [window]}
    draft = {"tracks": [{"name": "JY_ZH_SUBTITLES", "type": "text", "segments": ordinary}, *tracks],
             "materials": {"texts": materials}}
    return draft, SimpleNamespace(plan={"content_gate": content, "presets": presets}), {"window_ids": ["cumulative"]}


def test_cumulative_sentences_retain_title_but_release_explanation_without_word_timings():
    draft, ctx, spec = cumulative_fixture()
    content_before = deepcopy(ctx.plan["content_gate"])
    assert replace_ordinary_captions_with_presets(draft, spec, ctx)["removed_caption_count"] == 3
    assert ctx.plan["content_gate"] == content_before
    assert _actual_content_coverage(draft, ctx.plan, ctx.plan["content_gate"])["ok"]
    assert draft["tracks"][1]["segments"][0]["target_timerange"] == {"start": 1_000_000, "duration": 4_000_000}
    assert draft["tracks"][2]["segments"][0]["target_timerange"] == {"start": 2_000_000, "duration": 1_500_000}
    # Candidate and Writer share this actual-content validator: stale plan claims
    # cannot bless a shortened retained title or a lingering explanation.
    for track_index, delta in [(1, -100_000), (2, 100_000), (3, 100_000)]:
        changed = deepcopy(draft)
        changed["tracks"][track_index]["segments"][0]["target_timerange"]["duration"] += delta
        assert not _actual_content_coverage(changed, ctx.plan, ctx.plan["content_gate"])["ok"]


@pytest.mark.parametrize("defect", ["missing_mode", "unknown_mode", "wrong_ref", "reversed", "missing_part",
    "lost_word", "late_start", "early_start", "gap", "overlap", "retention_past_group", "retention_before_end",
    "mixed_char_timing", "late_native", "early_native", "short_native", "extra_native", "wrong_native_text",
    "undeclared_track"])
def test_cumulative_sentence_contract_rejects_invalid_claims_atomically(defect):
    draft, ctx, spec = cumulative_fixture()
    window = ctx.plan["content_gate"]["preset_replacement_windows"][0]
    parts = window["replacement_parts"]
    if defect == "missing_mode":
        window.pop("replacement_mode")
    elif defect == "unknown_mode":
        window["replacement_mode"] = "allow_overflow"
    elif defect == "wrong_ref":
        parts[1]["replacement_for"] = parts[0]["replacement_for"]
    elif defect == "reversed":
        parts.reverse()
    elif defect == "missing_part":
        parts.pop()
    elif defect == "lost_word":
        parts[1]["text"] = "第一条的说明"
    elif defect in ("late_start", "early_start"):
        parts[1]["start_us"] += 100_000 if defect == "late_start" else -100_000
    elif defect in ("gap", "overlap"):
        parts[1]["end_us"] += 100_000 if defect == "overlap" else -100_000
    elif defect == "retention_past_group":
        parts[0]["retained_until_us"] += 1
    elif defect == "retention_before_end":
        parts[0]["retained_until_us"] = parts[0]["end_us"] - 1
    elif defect == "mixed_char_timing":
        parts[0]["source_char_range"] = [0, 5]
    elif defect in ("late_native", "early_native"):
        draft["tracks"][1]["segments"][0]["target_timerange"]["start"] += 100_000 if defect == "late_native" else -100_000
    elif defect == "short_native":
        draft["tracks"][1]["segments"][0]["target_timerange"]["duration"] -= 100_000
    elif defect == "extra_native":
        draft["tracks"][1]["segments"].append(deepcopy(draft["tracks"][1]["segments"][0]))
    elif defect == "wrong_native_text":
        draft["materials"]["texts"][0]["content"] = json.dumps({"text": "另一条主线"})
    elif defect == "undeclared_track":
        ctx.plan["presets"][0]["actual_text_tracks"] = []
    before = deepcopy(draft)
    with pytest.raises(ValueError):
        replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft == before


def test_cumulative_semantic_preflight_and_duplicate_caption_gate():
    draft, ctx, spec = cumulative_fixture()
    content = ctx.plan["content_gate"]
    assert validate_caption_coverage(content["final_retained_speech"], [], content["preset_replacement_windows"])["ok"]
    assert not _actual_content_coverage(draft, ctx.plan, content)["ok"]
    content["preset_replacement_windows"][0]["replacement_parts"][0]["retained_until_us"] += 1
    assert not validate_caption_coverage(content["final_retained_speech"], [], content["preset_replacement_windows"])["ok"]


def distributed_fixture(*, residual=True, qualifier=False):
    ordinary = '哪一条路更适合你' if residual else ''
    first = '不能盲目' if qualifier else '就比如说'
    chunks = [('ordinary' if qualifier else 'omit_filler', first), ('preset', '开店'),
              ('omit_filler', '和'), ('preset', '自媒体')]
    if ordinary: chunks.append(('ordinary', ordinary))
    source = ''.join(text for _, text in chunks)
    draft, ctx, spec = fixture(['开店', '自媒体'])
    window = ctx.plan['content_gate']['preset_replacement_windows'][0]
    names = window.pop('track_names')
    window.pop('template_id'); window.pop('node_id')
    window.update(replacement_mode='distributed_sentence', text=source, text_parts=[])
    cursor, index = 0, 0
    for role, text in chunks:
        part = {'role': role, 'text': text, 'source_range': [cursor, cursor+len(text)]}
        if role == 'preset':
            part.update(template_id='DEMO', node_id='quote', track_name=names[index]); index += 1
        if role == 'omit_filler': part['reason'] = '画面由并列重点和剩余字幕共同表达，省略引导词或并列连接词'
        window['text_parts'].append(part); cursor += len(text)
    content = ctx.plan['content_gate']
    content['final_retained_speech'][0]['text'] = content['ordinary_subtitles'][0]['text'] = source
    for i, material in enumerate(draft['materials']['texts']):
        text = source if i == 0 else ['开店', '自媒体'][i-1]
        material['content'] = json.dumps({'text': text, 'styles': [{'range': [0, len(text)],
            'size': 14 if i == 0 else 21, 'font': {'path': 'C:/Windows/Fonts/msyh.ttc'}}]})
    ctx.plan['target'] = {'width': 1080, 'height': 1920}
    ctx.plan['layout_checks'] = {'ordinary_caption_baseline': {'material_id': 'm0', 'scale_y': 1}}
    return draft, ctx, spec


def distributed_phases_fixture():
    draft, ctx, spec = distributed_fixture()
    window = ctx.plan['content_gate']['preset_replacement_windows'][0]
    # One phrase first appears whole, then splits across two native tracks.
    part = window['text_parts'][1]
    name = part.pop('track_name')
    right_name = name + '_right'
    first = draft['tracks'][1]['segments'][0]
    first['target_timerange']['duration'] = 800_000
    left = deepcopy(first)
    left.update(id='phase-left', material_id='phase-left')
    left['target_timerange'] = dict(start=1_800_000, duration=1_200_000)
    draft['tracks'][1]['segments'].append(left)
    right = deepcopy(left)
    right.update(id='phase-right', material_id='phase-right')
    draft['tracks'].append(dict(name=right_name, type='text', segments=[right]))
    for material_id, text in [('phase-left', '开'), ('phase-right', '店')]:
        draft['materials']['texts'].append(dict(id=material_id, content=json.dumps({'text': text})))
    ctx.plan['presets'][0]['actual_text_tracks'][0] = dict(track_name=name, texts=['开店', '开'])
    ctx.plan['presets'][0]['actual_text_tracks'].append(dict(track_name=right_name, text='店'))
    part['display_phases'] = [
        dict(start_us=1_000_000, end_us=1_800_000,
             tracks=[dict(track_name=name, segment_index=0, text='开店')]),
        dict(start_us=1_800_000, end_us=3_000_000,
             tracks=[dict(track_name=name, segment_index=1, text='开'),
                     dict(track_name=right_name, segment_index=0, text='店')])]
    return draft, ctx, spec


def test_distributed_native_phases_preserve_complete_phrase_and_residual():
    draft, ctx, spec = distributed_phases_fixture()
    before = deepcopy(draft['tracks'][1:])
    replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft['tracks'][1:] == before
    assert _actual_content_coverage(draft, ctx.plan, ctx.plan['content_gate'])['ok']
    assert json.loads(draft['materials']['texts'][-1]['content'])['text'] == '哪一条路更适合你'


@pytest.mark.parametrize('defect', ['missing_phase', 'phase_gap', 'phase_overlap', 'wrong_words',
    'wrong_segment', 'actual_gap', 'actual_overlap', 'hidden', 'missing_ordinary'])
def test_distributed_native_phases_reject_incomplete_actual_display(defect):
    draft, ctx, spec = distributed_phases_fixture()
    phases = ctx.plan['content_gate']['preset_replacement_windows'][0]['text_parts'][1]['display_phases']
    if defect == 'missing_phase': phases.pop()
    elif defect == 'phase_gap': phases[1]['start_us'] += 1
    elif defect == 'phase_overlap': phases[1]['start_us'] -= 1
    elif defect == 'wrong_words': phases[1]['tracks'][1]['text'] = '门'
    elif defect == 'wrong_segment': phases[1]['tracks'][0]['segment_index'] = 0
    elif defect == 'actual_gap': draft['tracks'][1]['segments'][1]['target_timerange']['start'] += 100_000
    elif defect == 'actual_overlap': draft['tracks'][1]['segments'][0]['target_timerange']['duration'] += 100_000
    elif defect == 'hidden': draft['tracks'][-1]['attribute'] = 1
    elif defect == 'missing_ordinary':
        replace_ordinary_captions_with_presets(draft, spec, ctx)
        draft['tracks'][0]['segments'].clear()
        assert not _actual_content_coverage(draft, ctx.plan, ctx.plan['content_gate'])['ok']
        return
    before = deepcopy(draft)
    with pytest.raises(ValueError): replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft == before


def distributed_reveal_fixture(tmp_path):
    draft, ctx, spec = distributed_fixture(qualifier=True)
    content = ctx.plan['content_gate']
    window = content['preset_replacement_windows'][0]
    source = window['text']
    rough = tmp_path / 'rough.mp4'
    rough.write_bytes(b'current distributed rough')
    rough_hash = hashlib.sha256(rough.read_bytes()).hexdigest()
    raw = tmp_path / 'funasr.json'
    raw.write_text(json.dumps({'source_path': str(rough), 'result': [{'text': source,
        'timestamp': [[1000+i*100, 1100+i*100] for i in range(len(source))]}]}), encoding='utf-8')
    envelope = tmp_path / 'evidence.json'
    envelope.write_text(json.dumps({'source': 'funasr-paraformer', 'evidence_path': str(raw),
        'evidence_sha256': hashlib.sha256(raw.read_bytes()).hexdigest(), 'rough_cut_sha256': rough_hash}), encoding='utf-8')
    content['semantic_evidence'] = dict(rough_cut_path=str(rough), rough_cut_sha256=rough_hash,
                                       evidence_paths=[str(envelope)])
    content['final_retained_speech'][0].update(source_char_range=[0, len(source)],
                                              end_us=1_000_000+len(source)*100_000)
    window['timing_evidence_path'] = str(envelope)
    for part in window['text_parts']:
        if part['role'] != 'preset': continue
        reveal = 1_000_000+part['source_range'][0]*100_000
        part['reveal_start_us'] = reveal
        part['source_char_range'] = list(part['source_range'])
        track = next(t for t in draft['tracks'] if t['name'] == part['track_name'])
        track['segments'][0]['target_timerange'] = dict(start=reveal, duration=window['end_us']-reveal)
    return draft, ctx, spec, raw


def test_distributed_reveals_use_existing_funasr_and_preserve_residual_sentence(tmp_path):
    draft, ctx, spec, _ = distributed_reveal_fixture(tmp_path)
    content = ctx.plan['content_gate']
    before_speech = deepcopy(content['final_retained_speech'])
    replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert content['final_retained_speech'] == before_speech
    assert draft['tracks'][0]['segments'][0]['target_timerange'] == dict(start=1_000_000, duration=2_000_000)
    report = _actual_content_coverage(draft, ctx.plan, content)
    assert report['ok'], report
    draft['tracks'][2]['segments'][0]['target_timerange']['start'] += 100_000
    assert not _actual_content_coverage(draft, ctx.plan, content)['ok']


def test_distributed_reveal_allows_early_display_and_adjudicated_parent_asr_difference(tmp_path):
    draft, ctx, spec, raw = distributed_reveal_fixture(tmp_path)
    content = ctx.plan['content_gate']
    content['final_retained_speech'][0].pop('source_char_range')
    window = content['preset_replacement_windows'][0]
    payload = json.loads(raw.read_text(encoding='utf-8'))
    row = payload['result'][0]
    row['text'] = row['text'].replace('自媒体', '自媒体体')
    row['timestamp'].append([row['timestamp'][-1][1], row['timestamp'][-1][1]+100])
    raw.write_text(json.dumps(payload), encoding='utf-8')
    envelope = Path(window['timing_evidence_path'])
    record = json.loads(envelope.read_text(encoding='utf-8'))
    record['evidence_sha256'] = hashlib.sha256(raw.read_bytes()).hexdigest()
    envelope.write_text(json.dumps(record), encoding='utf-8')
    part = window['text_parts'][1]
    part['reveal_start_us'] -= 100_000
    timing = draft['tracks'][1]['segments'][0]['target_timerange']
    timing['start'] -= 100_000
    timing['duration'] += 100_000
    replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert _actual_content_coverage(draft, ctx.plan, content)['ok']


@pytest.mark.parametrize('defect', ['late_reveal', 'no_evidence', 'changed_evidence', 'wrong_global_span',
    'foreign_phrase_time', 'ordinary_reveal', 'actual_early', 'actual_late', 'early_end', 'missing_field'])
def test_distributed_reveal_rejects_unbound_or_incomplete_handoff(tmp_path, defect):
    draft, ctx, spec, raw = distributed_reveal_fixture(tmp_path)
    content = ctx.plan['content_gate']
    window = content['preset_replacement_windows'][0]
    part = window['text_parts'][1]
    if defect == 'late_reveal': part['reveal_start_us'] += 40_001
    elif defect == 'no_evidence': del window['timing_evidence_path']
    elif defect == 'changed_evidence': raw.write_text('{}', encoding='utf-8')
    elif defect == 'wrong_global_span': part['source_char_range'][0] += 1
    elif defect == 'foreign_phrase_time':
        payload = json.loads(raw.read_text(encoding='utf-8'))
        payload['result'][0]['timestamp'] = [[a+5000, b+5000] for a, b in payload['result'][0]['timestamp']]
        raw.write_text(json.dumps(payload), encoding='utf-8')
        envelope = Path(window['timing_evidence_path'])
        record = json.loads(envelope.read_text(encoding='utf-8'))
        record['evidence_sha256'] = hashlib.sha256(raw.read_bytes()).hexdigest()
        envelope.write_text(json.dumps(record), encoding='utf-8')
    elif defect == 'ordinary_reveal': window['text_parts'][0]['reveal_start_us'] = 1_000_000
    elif defect == 'actual_early': draft['tracks'][1]['segments'][0]['target_timerange']['start'] -= 100_000
    elif defect == 'actual_late': draft['tracks'][1]['segments'][0]['target_timerange']['start'] += 100_000
    elif defect == 'early_end': draft['tracks'][1]['segments'][0]['target_timerange']['duration'] -= 100_000
    elif defect == 'missing_field':
        del window['timing_evidence_path']
        for item in window['text_parts']:
            item.pop('reveal_start_us', None)
            item.pop('source_char_range', None)
    before = deepcopy(draft)
    with pytest.raises(ValueError): replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft == before


@pytest.mark.parametrize('residual', [True, False])
def test_distributed_sentence_lets_ordinary_caption_yield_without_losing_speech(residual):
    from jianying_adapter.final_layout import validate_final_text
    draft, ctx, spec = distributed_fixture(residual=residual)
    content = ctx.plan['content_gate']
    before_speech = deepcopy(content['final_retained_speech'])
    presets_before = deepcopy(draft['tracks'][1:])
    assert validate_caption_coverage(content['final_retained_speech'], [], content['preset_replacement_windows'])['ok']
    assert not _actual_content_coverage(draft, ctx.plan, content)['ok']
    report = shared_operation_handlers()['replace_ordinary_captions_with_presets'](draft, spec, ctx)
    assert report['residual_caption_count'] == int(residual)
    assert draft['tracks'][1:] == presets_before
    assert content['final_retained_speech'] == before_speech
    assert _actual_content_coverage(draft, ctx.plan, content)['ok']
    if Path('C:/Windows/Fonts/msyh.ttc').is_file():
        result = validate_final_text(draft, ctx.plan)
        assert result['ok'], result['errors']
    if residual:
        actual = json.loads(draft['materials']['texts'][-1]['content'])
        assert actual['text'] == '哪一条路更适合你'


@pytest.mark.parametrize('defect', ['lost_negation', 'range_gap', 'range_overlap', 'wrong_text',
    'unbound_track', 'late_preset', 'hidden_preset', 'missing_preset', 'invented_word_time', 'crossing_caption'])
def test_distributed_handoff_rejects_invalid_assignments_atomically(defect):
    draft, ctx, spec = distributed_fixture(qualifier=True)
    window = ctx.plan['content_gate']['preset_replacement_windows'][0]
    parts = window['text_parts']
    if defect == 'lost_negation': parts[0].update(role='omit_filler', reason='不能为去重删掉否定')
    elif defect == 'range_gap': parts[1]['source_range'][0] += 1
    elif defect == 'range_overlap': parts[1]['source_range'][0] -= 1
    elif defect == 'wrong_text': parts[1]['text'] = '选项目'
    elif defect == 'unbound_track': parts[1]['track_name'] = 'invented'
    elif defect == 'late_preset': draft['tracks'][1]['segments'][0]['target_timerange']['start'] += 100_000
    elif defect == 'hidden_preset': draft['tracks'][1]['attribute'] = 1
    elif defect == 'missing_preset': draft['tracks'].pop()
    elif defect == 'invented_word_time': parts[1]['start_us'] = 1_400_000
    elif defect == 'crossing_caption': draft['tracks'][0]['segments'][0]['target_timerange']['duration'] += 100_000
    before = deepcopy(draft)
    with pytest.raises(ValueError): replace_ordinary_captions_with_presets(draft, spec, ctx)
    assert draft == before


@pytest.mark.parametrize('defect', ['lost_qualifier', 'duplicate_caption', 'changed_preset', 'shortened_preset'])
def test_writer_content_readback_rechecks_distributed_text(defect):
    draft, ctx, spec = distributed_fixture(qualifier=True)
    replace_ordinary_captions_with_presets(draft, spec, ctx)
    content = ctx.plan['content_gate']
    assert _actual_content_coverage(draft, ctx.plan, content)['ok']
    if defect == 'lost_qualifier':
        payload = json.loads(draft['materials']['texts'][-1]['content'])
        payload['text'] = '哪一条路更适合你'
        draft['materials']['texts'][-1]['content'] = json.dumps(payload)
    elif defect == 'duplicate_caption':
        draft['tracks'][0]['segments'].append(deepcopy(draft['tracks'][0]['segments'][0]))
    elif defect == 'changed_preset': draft['materials']['texts'][1]['content'] = json.dumps({'text': '选项目'})
    else: draft['tracks'][1]['segments'][0]['target_timerange']['duration'] -= 100_000
    assert not _actual_content_coverage(draft, ctx.plan, content)['ok']
