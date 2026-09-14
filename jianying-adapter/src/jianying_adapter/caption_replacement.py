"""Caption handoff and shared reading duties bound to actual native text tracks."""
from __future__ import annotations

import json
import unicodedata
import copy
import uuid
from pathlib import Path
from typing import Any, Mapping

from .semantic_evidence.content_gate import _clean_text, _items, cumulative_sentence_parts, distributed_sentence_parts, load_semantic_evidence, resolve_replacement_speech, validate_caption_coverage


def _timing_text(value: Any) -> str:
    """FunASR character timestamps omit whitespace and Unicode punctuation."""
    return "".join(c for c in str(value or "") if not c.isspace() and not unicodedata.category(c).startswith("P"))


def _replacement_timing(window: Mapping[str, Any], plan: Mapping[str, Any], content: Mapping[str, Any]) -> tuple[str, list[tuple[int, int]]]:
    """Read an existing hash-bound FunASR envelope; never fabricate word timings."""
    semantic = content.get("semantic_evidence")
    if not isinstance(semantic, Mapping):
        gate_path = Path(str(plan.get("semantic_gate") or ""))
        if not gate_path.is_file():
            raise ValueError("顺序替代缺少可读取的当前 semantic_gate")
        gate = json.loads(gate_path.read_text(encoding="utf-8-sig"))
        rough = gate.get("rough_cut", {})
        semantic = {"rough_cut_path": rough.get("path"), "rough_cut_sha256": rough.get("sha256"),
                    "evidence_paths": gate.get("evidence_paths", [])}
    path = Path(str(window.get("timing_evidence_path") or ""))
    declared = semantic.get("evidence_paths", [])
    if not path.is_file() or path.resolve() not in [Path(str(p)).resolve() for p in declared]:
        raise ValueError("顺序替代 timing_evidence_path 必须绑定当前双 ASR 证据之一")
    if not semantic.get("rough_cut_path") or not semantic.get("rough_cut_sha256"):
        raise ValueError("顺序替代缺少当前粗剪路径和哈希绑定")
    record = load_semantic_evidence(path, expected_source_path=semantic["rough_cut_path"])
    if not record["hash_valid"] or record["rough_cut_sha256"].casefold() != str(semantic["rough_cut_sha256"]).casefold():
        raise ValueError("顺序替代 ASR 证据哈希或粗剪绑定不匹配")
    if "funasr" not in record["source"].casefold() and "paraformer" not in record["source"].casefold():
        raise ValueError("顺序替代目前需要已有 FunASR 原生逐字 timestamp")
    raw = json.loads(Path(record["evidence_payload_path"]).read_text(encoding="utf-8-sig"))
    rows = raw.get("result") if isinstance(raw, Mapping) else raw
    if not isinstance(rows, list) or not rows:
        raise ValueError("顺序替代 ASR 缺少原生 result/timestamp")
    text, stamps = "", []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("顺序替代 ASR result 必须是对象")
        chars, times = _timing_text(row.get("text")), row.get("timestamp")
        if not isinstance(times, list) or len(times) != len(chars) or not chars:
            raise ValueError("顺序替代 ASR 字符数与 timestamp 不一致，不能推算字时点")
        for pair in times:
            if not isinstance(pair, list) or len(pair) != 2 or any(type(v) not in (int, float) for v in pair):
                raise ValueError("顺序替代 ASR timestamp 无效")
            start, end = round(pair[0] * 1000), round(pair[1] * 1000)
            if start < 0 or end <= start or (stamps and start < stamps[-1][1]):
                raise ValueError("顺序替代 ASR 字时点无效或倒序")
            stamps.append((start, end))
        text += chars
    return text, stamps


def _sequential_parts(window: Mapping[str, Any], plan: Mapping[str, Any], content: Mapping[str, Any], speech: list[Mapping[str, Any]], indices: list[int]) -> list[Mapping[str, Any]]:
    parts = window.get("replacement_parts")
    if not isinstance(parts, list) or len(parts) < 2:
        raise ValueError("replacement_parts 必须包含至少两个顺序短语")
    if any(key in window for key in ("template_id", "node_id", "track_names")):
        raise ValueError("顺序替代不能同时声明窗口顶层的单预设绑定")
    text, stamps = _replacement_timing(window, plan, content)
    # The window keeps its original full-sentence refs. Character ranges only
    # bind presentation slices; they never replace semantic adjudication.
    ranges = [speech[i].get("source_char_range") for i in indices]
    if any(not isinstance(r, list) or len(r) != 2 or any(type(n) is not int for n in r) or r[0] < 0 or r[1] <= r[0] or r[1] > len(text) for r in ranges):
        raise ValueError("顺序替代语音句缺少有效的全局 source_char_range")
    for i, (item_index, span) in enumerate(zip(indices, ranges)):
        item = speech[item_index]
        if (i and span[0] != ranges[i - 1][1]) or _timing_text(item.get("text")) != text[span[0]:span[1]]:
            raise ValueError("顺序替代语音句字符范围未绑定 ASR 完整原文")
        ustart = item.get("start_us", round(float(item.get("start", 0)) * 1_000_000))
        uend = item.get("end_us", round(float(item.get("end", 0)) * 1_000_000))
        if abs(ustart - stamps[span[0]][0]) > 40_000 or abs(uend - stamps[span[1] - 1][1]) > 40_000:
            raise ValueError("顺序替代语音句时间未绑定 ASR 字时点")
    cursor, char_cursor = window["start_us"], ranges[0][0]
    assembled = []
    for i, part in enumerate(parts):
        if not isinstance(part, Mapping):
            raise ValueError("replacement_parts 条目必须是对象")
        start, end, span = part.get("start_us"), part.get("end_us"), part.get("source_char_range")
        if type(start) is not int or type(end) is not int or start != cursor or end <= start or end > window["end_us"]:
            raise ValueError("顺序短语必须按声明顺序无缝覆盖整句窗口，不能重叠或留空")
        if not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span) or span[0] != char_cursor or span[1] <= span[0] or span[1] > ranges[-1][1]:
            raise ValueError("顺序短语 source_char_range 必须按原顺序连续且不重复")
        if _timing_text(part.get("text")) != text[span[0]:span[1]]:
            raise ValueError("顺序短语文字与 ASR 字符范围不符")
        if i and abs(start - stamps[span[0]][0]) > 40_000:
            raise ValueError("顺序短语切换未对齐下一短语首字的实际 ASR 时间")
        if start > stamps[span[0]][0] + 40_000 or end < stamps[span[1] - 1][1] - 40_000:
            raise ValueError("顺序短语没有覆盖其实际讲话时间")
        assembled.append(str(part.get("text", "")))
        cursor, char_cursor = end, span[1]
    if cursor != window["end_us"] or char_cursor != ranges[-1][1] or _clean_text("".join(assembled)) != _clean_text(window.get("text")):
        raise ValueError("顺序短语未完整覆盖原句文字与字幕窗口")
    return parts


def caption_items(draft: Mapping[str, Any], track_name: str) -> list[dict[str, Any]]:
    tracks = [t for t in draft.get("tracks", []) if t.get("type") == "text" and t.get("name") == track_name]
    if len(tracks) > 1:
        raise ValueError(f"字幕轨道名不唯一: {track_name}")
    materials = {m.get("id"): m for m in draft.get("materials", {}).get("texts", [])}
    rows = []
    for segment in (tracks[0].get("segments", []) if tracks else []):
        material = materials.get(segment.get("material_id"), {})
        payload = material.get("content")
        payload = json.loads(payload) if isinstance(payload, str) else {}
        timerange = segment.get("target_timerange", {})
        start = timerange.get("start")
        duration = timerange.get("duration")
        if type(start) is not int or type(duration) is not int or duration <= 0:
            raise ValueError(f"字幕轨道时间无效: {track_name}")
        rows.append({"id": segment.get("id"), "start_us": start, "end_us": start + duration,
                     "text": payload.get("text", material.get("text", "")),
                     "visible": not (tracks[0].get('attribute', 0) & 1 or segment.get('track_attribute', 0) & 1
                         or segment.get('visible') is False or (segment.get('clip') or {}).get('alpha', 1) <= 0)})
    return rows


def _display_text(value: str) -> str:
    # Layout punctuation may differ; signs and percentages may not disappear.
    return ''.join(char for char in unicodedata.normalize('NFKC', value).casefold()
                   if not char.isspace() and (not unicodedata.category(char).startswith('P') or char in '+-%'))


def _distributed_reveal_starts(window, plan, content, parts):
    """Bind only the revealed phrase to existing global FunASR character times.

    Parent sentence semantics stay under the existing adjudicated speech gate;
    a raw ASR repeated word must not force a different final sentence boundary.
    """
    if not any('reveal_start_us' in part for part in parts):
        return {}
    text, stamps = _replacement_timing(window, plan, content)
    starts = {}
    for index, part in enumerate(parts):
        if 'reveal_start_us' not in part:
            continue
        left, right = part['source_char_range']
        phrase = _timing_text(part['text'])
        if right > len(text) or not phrase or text[left:right] != phrase:
            raise ValueError('分工 reveal source_char_range 未严格绑定 FunASR 短语原文')
        first_start = stamps[left][0]
        if not window['start_us'] <= first_start < window['end_us']:
            raise ValueError('分工 reveal 对应短语首字不在父句窗口内')
        if part['reveal_start_us'] > first_start + 40_000:
            raise ValueError('分工 reveal_start_us 晚于对应首字实际口述时间')
        starts[index] = (part['reveal_start_us'], first_start)
    return starts


def _bind_display_phases(draft, plan, window, part):
    from .semantic_evidence.content_gate import distributed_display_phases
    selections = [p for p in plan.get('presets', [])
                  if p.get('template_id') == part['template_id'] and p.get('node_id') == part['node_id']]
    if len(selections) != 1:
        raise ValueError('显示阶段须绑定唯一已选预设')
    declared = {t.get('track_name') for t in selections[0].get('actual_text_tracks', [])}
    prefix = f"JY_PRESET_{part['template_id']}__NODE__{part['node_id']}_"
    names = set()
    for phase in distributed_display_phases(part, window):
        for entry in phase['tracks']:
            name, index = entry['track_name'], entry['segment_index']
            if name not in declared or not name.startswith(prefix):
                raise ValueError('显示阶段轨道未绑定所选真实预设: ' + name)
            rows = caption_items(draft, name)
            if index >= len(rows):
                raise ValueError('显示阶段 segment_index 不存在: ' + name)
            row = rows[index]
            overlaps = [r for r in rows if min(phase['end_us'], r['end_us']) > max(phase['start_us'], r['start_us'])]
            if (len(overlaps) != 1 or overlaps[0] is not row or not row['visible']
                    or row['start_us'] != phase['start_us']
                    or row['end_us'] != phase['end_us']
                    or _display_text(row['text']) != _display_text(entry['text'])):
                raise ValueError('实际显示阶段文字、片段或可见区间不匹配: ' + name)
            names.add(name)
    return names


def _bind_distributed_sentence(draft, plan, content, window, ordinary, *, final):
    parts = distributed_sentence_parts(window)
    start, end = window['start_us'], window['end_us']
    reveals = _distributed_reveal_starts(window, plan, content, parts)
    by_track, residual, phase_tracks = {}, [], set()
    for index, part in enumerate(parts):
        if part['role'] == 'ordinary':
            residual.append(part['text'])
        elif part['role'] == 'preset':
            if 'display_phases' in part:
                names = _bind_display_phases(draft, plan, window, part)
                if names & phase_tracks:
                    raise ValueError('显示阶段轨道不可重复分配不同短语')
                phase_tracks.update(names)
                continue
            name = part['track_name']
            selections = [p for p in plan.get('presets', [])
                          if p.get('template_id') == part['template_id'] and p.get('node_id') == part['node_id']]
            prefix = f"JY_PRESET_{part['template_id']}__NODE__{part['node_id']}_"
            if (len(selections) != 1 or not name.startswith(prefix) or name not in
                    {t.get('track_name') for t in selections[0].get('actual_text_tracks', [])}):
                raise ValueError('分工字幕重点轨未绑定所选真实预设: ' + name)
            reveal, first_start = reveals.get(index, (start, None))
            by_track.setdefault(name, []).append((part['text'], reveal, index in reveals, first_start))
    if phase_tracks & set(by_track):
        raise ValueError('显示阶段不能与单轨分工重复绑定')
    for name, chunks in by_track.items():
        starts = {chunk[1] for chunk in chunks}
        if len(starts) != 1:
            raise ValueError('同一分工重点轨不能绑定不同 reveal 起点: ' + name)
        expected_start = next(iter(starts))
        explicit_reveal = any(chunk[2] for chunk in chunks)
        rows = [r for r in caption_items(draft, name) if min(end, r['end_us']) > max(start, r['start_us'])]
        if (len(rows) != 1 or rows[0]['start_us'] > expected_start + 40_000 or rows[0]['end_us'] < end - 40_000
                or (explicit_reveal and (abs(rows[0]['start_us']-expected_start) > 40_000
                                        or abs(rows[0]['end_us']-end) > 40_000))
                or any(chunk[3] is not None and rows[0]['start_us'] > chunk[3]+40_000 for chunk in chunks)
                or _display_text(rows[0]['text']) != _display_text(''.join(chunk[0] for chunk in chunks))):
            raise ValueError('实际分工重点文字/时间未完整承载所分配内容: ' + name)
        track = next(t for t in draft['tracks'] if t.get('name') == name)
        segment = next(s for s in track['segments'] if s.get('id') == rows[0]['id'])
        if (track.get('attribute', 0) & 1 or segment.get('visible') is False
                or segment.get('track_attribute', 0) & 1 or (segment.get('clip') or {}).get('alpha', 1) <= 0):
            raise ValueError('分工重点文字不可隐藏: ' + name)
    residual_text = ''.join(residual)
    if final:
        rows = [r for r in ordinary if min(end, r['end_us']) > max(start, r['start_us'])]
        if not residual_text and rows:
            raise ValueError('分工窗口无需普通字幕，但实际仍有普通字幕')
        if residual_text and (len(rows) != 1 or not rows[0]['visible'] or rows[0]['start_us'] > start + 40_000 or rows[0]['end_us'] < end - 40_000
                              or _display_text(rows[0]['text']) != _display_text(residual_text)):
            raise ValueError('实际普通字幕没有准确承载分工后的剩余内容，或仍有重复文字')
    return residual_text


def bind_replacement_windows(
    draft: Mapping[str, Any], plan: Mapping[str, Any], content: Mapping[str, Any],
    *, require_absent_captions: bool = True,
) -> dict[str, Any]:
    """Reject self-attested replacements unless actual declared tracks carry the sentence.

    Supports complete simultaneous text, explicit sequential phrase parts
    bound to original ASR character timestamps, or cumulative whole sentences
    bound to frozen sentence starts. Native animation/readability
    QA remains a separate gate; no word timing is inferred.
    """
    windows = content.get("preset_replacement_windows", [])
    if not isinstance(windows, list):
        return {"ok": False, "errors": ["preset_replacement_windows 必须为列表"], "windows": []}
    errors, valid, seen, spans = [], [], set(), []
    speech = _items(content.get("final_retained_speech"))
    caption_name = str(content.get("ordinary_caption_track_name") or plan.get("ordinary_caption_track_name") or "JY_ZH_SUBTITLES")
    try:
        ordinary = caption_items(draft, caption_name)
    except (ValueError, TypeError) as error:
        return {"ok": False, "errors": [str(error)], "windows": []}
    for window in windows:
        try:
            if not isinstance(window, Mapping):
                raise ValueError("替代窗口必须是对象")
            window_id = window.get("id")
            if not isinstance(window_id, str) or not window_id or window_id in seen:
                raise ValueError("替代窗口 id 必须唯一且非空")
            seen.add(window_id)
            start, end = window.get("start_us"), window.get("end_us")
            if type(start) is not int or type(end) is not int or start < 0 or end <= start:
                raise ValueError(f"{window_id}: 需要整数 start_us/end_us")
            if any(min(end, b) > max(start, a) for a, b in spans):
                raise ValueError(f"{window_id}: 替代窗口不能重叠")
            spans.append((start, end))
            indices = resolve_replacement_speech(window, speech)
            if window.get('replacement_mode') == 'distributed_sentence':
                _bind_distributed_sentence(draft, plan, content, window, ordinary, final=require_absent_captions)
                valid.append(dict(window))
                continue
            sequential = "replacement_parts" in window
            cumulative = window.get("replacement_mode") == "cumulative_sentences"
            parts = (cumulative_sentence_parts(window, speech, indices) if cumulative else
                     _sequential_parts(window, plan, content, speech, indices) if sequential else [window])
            for part in parts:
                start = part["start_us"]
                end = part.get("retained_until_us", part["end_us"]) if cumulative else part["end_us"]
                if not cumulative and "retained_until_us" in part:
                    raise ValueError(f"{window_id}: 只有显式累计整句模式可声明 retained_until_us")
                selections = [p for p in plan.get("presets", []) if p.get("template_id") == part.get("template_id") and p.get("node_id") == part.get("node_id")]
                if len(selections) != 1 or not part.get("template_id") or not part.get("node_id"):
                    raise ValueError(f"{window_id}: 替代预设必须在 CandidatePlan 中唯一声明")
                names = part.get("track_names")
                if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
                    raise ValueError(f"{window_id}: track_names 必须是有序、不重复的实际预设文字轨道")
                prefix = f"JY_PRESET_{part['template_id']}__NODE__{part['node_id']}_"
                declared = {t.get("track_name") for t in selections[0].get("actual_text_tracks", [])}
                actual_text = []
                for name in names:
                    if name not in declared or not name.startswith(prefix):
                        raise ValueError(f"{window_id}: 替代轨道未绑定所选预设: {name}")
                    rows = caption_items(draft, name)
                    overlapping = [r for r in rows if min(end, r["end_us"]) > max(start, r["start_us"])]
                    if len(overlapping) != 1 or overlapping[0]["start_us"] > start + 40_000 or overlapping[0]["end_us"] < end - 40_000:
                        raise ValueError(f"{window_id}: 实际预设轨道没有完整覆盖整句: {name}")
                    if sequential and (abs(overlapping[0]["start_us"] - start) > 40_000 or abs(overlapping[0]["end_us"] - end) > 40_000):
                        raise ValueError(f"{window_id}: 实际轨道越过声明显示起止边界: {name}")
                    actual_text.append(str(overlapping[0]["text"]))
                if _clean_text("".join(actual_text)) != _clean_text(part.get("text")):
                    raise ValueError(f"{window_id}: 实际预设文字不是完整原句或短语")
            start, end = window["start_us"], window["end_us"]
            if require_absent_captions and any(min(end, r["end_us"]) > max(start, r["start_us"]) for r in ordinary):
                raise ValueError(f"{window_id}: 完整预设替代窗口仍有普通字幕，重复表达")
            valid.append(dict(window))
        except (OSError, ValueError, TypeError, KeyError) as error:
            errors.append(str(error))
    return {"ok": not errors, "errors": errors, "windows": valid,
            "native_visual_verified": False}


def replace_ordinary_captions_with_presets(draft: dict[str, Any], spec: Mapping[str, Any], context: Any) -> Mapping[str, Any]:
    """Remove whole caption segments only after verifying an actual full replacement."""
    plan = context.plan
    content = plan.get("content_gate")
    if not isinstance(content, Mapping):
        path = Path(str(plan.get("semantic_gate") or ""))
        path = path if path.is_absolute() else Path(context.plan_path).parent / path
        content = json.loads(path.read_text(encoding="utf-8")).get("content_gate", {})
    ids = spec.get("window_ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("window_ids 必须为非空、唯一窗口 id 列表")
    windows = [w for w in content.get("preset_replacement_windows", []) if w.get("id") in ids]
    if len(windows) != len(ids):
        raise ValueError("window_ids 未唯一匹配 content_gate.preset_replacement_windows")
    result = bind_replacement_windows(draft, plan, {**content, "preset_replacement_windows": windows}, require_absent_captions=False)
    if not result["ok"]:
        raise ValueError("; ".join(result["errors"]))
    caption_name = str(content.get("ordinary_caption_track_name") or plan.get("ordinary_caption_track_name") or "JY_ZH_SUBTITLES")
    rows = caption_items(draft, caption_name)
    remove_indexes, replacements, new_materials = set(), [], []
    materials = {m['id']: m for m in draft['materials'].get('texts', [])}
    track = next(t for t in draft['tracks'] if t.get('type') == 'text' and t.get('name') == caption_name)
    for window in windows:
        start, end = window["start_us"], window["end_us"]
        overlaps = [(i, r) for i, r in enumerate(rows) if min(end, r["end_us"]) > max(start, r["start_us"])]
        if not overlaps or any(r["start_us"] < start or r["end_us"] > end for _, r in overlaps):
            raise ValueError(f"{window['id']}: 只能移除完整普通字幕段，不能切掉跨窗口句子")
        speech = _items(content.get("final_retained_speech"))
        indices = resolve_replacement_speech(window, speech)
        source_check = validate_caption_coverage([speech[i] for i in indices], [r for _, r in overlaps])
        removed_text = "".join(str(r["text"]) for _, r in sorted(overlaps, key=lambda pair: pair[1]["start_us"]))
        if not source_check["ok"] or _clean_text(removed_text) != _clean_text(window["text"]):
            raise ValueError(f"{window['id']}: 原普通字幕没有完整承载本句，拒绝移除")
        if window.get('replacement_mode') == 'distributed_sentence':
            residual = _bind_distributed_sentence(draft, plan, content, window, rows, final=False)
            if residual:
                segment = copy.deepcopy(track['segments'][overlaps[0][0]])
                material = copy.deepcopy(materials[segment['material_id']])
                payload = json.loads(material['content'])
                styles = payload.get('styles', [])
                if len(styles) != 1:
                    raise ValueError('分工普通字幕需要一个明确样式，先用既有字幕样式入口统一')
                payload['text'] = residual
                styles[0]['range'] = [0, len(residual)]
                material['id'] = str(uuid.uuid4()).upper()
                material['content'] = json.dumps(payload, ensure_ascii=False)
                if 'text' in material: material['text'] = residual
                segment.update(id=str(uuid.uuid4()).upper(), material_id=material['id'],
                               target_timerange={'start': start, 'duration': end-start})
                replacements.append(segment)
                new_materials.append(material)
        remove_indexes.update(i for i, _ in overlaps)
    # All checks precede mutation; keep materials for recovery and shared references.
    track["segments"] = sorted([s for i, s in enumerate(track.get("segments", [])) if i not in remove_indexes] + replacements,
                               key=lambda segment: segment['target_timerange']['start'])
    draft['materials']['texts'].extend(new_materials)
    return {"window_ids": ids, "removed_caption_count": len(remove_indexes),
            "residual_caption_count": len(replacements),
            "removed_caption_ids": [rows[i]["id"] for i in sorted(remove_indexes)],
            "native_visual_verified": False}
