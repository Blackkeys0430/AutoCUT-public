# Copyright (c) 2026 Blackkeys0430 — AutoCUT original project code.
# Origin: https://github.com/Blackkeys0430/AutoCUT-public
# SPDX-License-Identifier: LicenseRef-AutoCUT-Personal-Use-1.0
"""Bind chosen visual techniques to executable shared operations, not taste scores."""
from collections.abc import Mapping
import json
import math
from pathlib import Path
from typing import Any

from .semantic_evidence.content_gate import _items, _time


TECHNIQUE_OPERATIONS = {
    "text_preset": {"add_preset_group", "replace_preset_group", "apply_text_style_variant", "simplify_preset_motion", "retime_preset_text_tracks"},
    "keyframes": {"animate_aroll_transform", "animate_broll_transform", "animate_preset_group_transform"},
    "mask": {"apply_video_mask"},
    "media": {"add_broll"},
    "sound": {"mix_action_audio"},
    "video_effect": {"add_video_effect"},
    "text_animation": {"apply_text_animation"},
}


DECISION_TECHNIQUES = {
    "typography": {"text_preset", "text_animation"},
    "composition": {"mask", "media", "keyframes"},
    "motion": {"keyframes", "video_effect", "text_animation"},
    "broll": {"media"},
    "sound": {"sound"},
}


SUPPORT_OPERATIONS = {'add_broll', 'apply_video_mask'}


def required_visual_minima(state):
    """User requirements belong to current state, independently of plan choices."""
    # All new/full-rebuild tasks use v2. Preserve read-only legacy contracts.
    floor = {'mask': 1, 'video_effect': 3} if state.get('visual_planning_min_version', 1) >= 2 else {}
    if 'required_visual_techniques' not in state:
        return floor
    value = state['required_visual_techniques']
    if (not isinstance(value, Mapping) or not value
            or set(value) - {'mask', 'video_effect'}
            or any(type(count) is not int or count < 1 for count in value.values())):
        raise ValueError('required_visual_techniques 必须为 mask/video_effect 的正整数最低次数；不能用空值或0取消必做项')
    return {key: max(floor.get(key, 0), value.get(key, 0)) for key in floor.keys() | value.keys()}


def _opacity_points(segment):
    """Read native opacity independently of geometry and declared envelopes."""
    start = segment['target_timerange']['start']
    duration = segment['target_timerange']['duration']
    groups = [g for g in segment.get('common_keyframes', []) if g.get('property_type') == 'KFTypeAlpha']
    if len(groups) > 1:
        raise ValueError('duplicate opacity keyframes')
    base = segment.get('clip', {}).get('alpha', 1)
    if type(base) not in (int, float) or not math.isfinite(base) or not 0 <= base <= 1:
        raise ValueError('clip alpha must be finite and between zero and one')
    if not groups:
        return [{'time_us': start, 'value': base}, {'time_us': start+duration, 'value': base}]
    frames = groups[0].get('keyframe_list')
    if not isinstance(frames, list) or not frames:
        raise ValueError('opacity keyframes are empty')
    result, previous = [], -1
    for frame in frames:
        time, values = frame.get('time_offset'), frame.get('values', [])
        if (type(time) not in (int, float) or not math.isfinite(time) or not previous < time <= duration
                or time < 0 or len(values) != 1 or type(values[0]) not in (int, float)
                or not math.isfinite(values[0]) or not 0 <= values[0] <= 1
                or frame.get('curveType', 'Line') != 'Line' or frame.get('graphID')):
            raise ValueError('opacity requires ordered finite native Line points in [0, 1]')
        result.append({'time_us': start+time, 'value': values[0]})
        previous = time
    if result[0]['time_us'] != start or result[0]['value'] != base:
        raise ValueError('opacity must start at clip.alpha at the segment boundary')
    if result[-1]['time_us'] < start+duration:
        result.append({'time_us': start+duration, 'value': result[-1]['value']})
    return result


def _support_segments(operation, draft):
    """Read the selected visible media/mask, not the presence of an operation label."""
    name = operation.get('track_name')
    tracks = [t for t in draft.get('tracks', []) if t.get('type') == 'video' and t.get('name') == name]
    # SDK video Track.export_json uses attribute=1 for mute. Silent B-roll
    # remains visible; visibility must not be inferred from its audio switch.
    if len(tracks) != 1 or tracks[0].get('visible') is False:
        return []
    materials = {m['id']: m for group in draft.get('materials', {}).values() if isinstance(group, list)
                 for m in group if isinstance(m, Mapping) and m.get('id')}
    intervals = []
    for segment in tracks[0].get('segments', []):
        if operation.get('segment_id') and segment.get('id') != operation['segment_id']:
            continue
        clip = segment.get('clip') or {}
        scale = clip.get('scale') or {}
        if (segment.get('visible') is False or segment.get('track_attribute', 0) & 1
                or scale.get('x', 1) <= 0 or scale.get('y', 1) <= 0):
            continue
        media = materials.get(segment.get('material_id'), {})
        source = media.get('path') or media.get('media_path')
        if not isinstance(source, str) or not source or not Path(source).is_file():
            continue
        if operation['kind'] == 'add_broll':
            if Path(source).resolve() != Path(operation.get('source_path', '')).resolve():
                continue
        else:
            masks = [materials.get(ref, {}) for ref in segment.get('extra_material_refs', [])
                     if materials.get(ref, {}).get('type') == 'mask']
            if (len(masks) != 1 or masks[0].get('resource_type') != operation.get('shape')
                    or not masks[0].get('resource_id') or not masks[0].get('path') or not Path(masks[0]['path']).is_dir()
                    or 'enable_video_mask' in segment or segment.get('enable_adjust_mask') is not False):
                continue
            dimensions = masks[0].get('config', {})
            if any(type(dimensions.get(axis)) not in (int, float) or not math.isfinite(dimensions[axis])
                   or dimensions[axis] <= 0 for axis in ('width', 'height')):
                continue
        timerange = segment.get('target_timerange', {})
        start, duration = timerange.get('start'), timerange.get('duration')
        if type(start) is int and type(duration) is int and start >= 0 and duration > 0:
            try:
                points = _opacity_points(segment)
            except (ValueError, KeyError, TypeError):
                continue
            intervals.extend((a['time_us'], b['time_us']) for a, b in zip(points, points[1:])
                             if max(a['value'], b['value']) > 0)
    return intervals


def validate_required_visual_techniques(plan, *, state=None, draft=None, plan_path=None, effect_report=None):
    """Enforce requested minima at preflight, assembly and Writer readback.

    Counts are distinct operations bound to a current event, never permissions,
    text animations or unreferenced materials. Native appearance remains unverified.
    """
    errors, rows = [], {}
    result = {'ok': True, 'errors': errors, 'requirements': rows,
              'basis': 'plan' if draft is None else 'native_structure',
              'native_visual_verified': False, 'creative_quality_verified': False}
    try:
        if state is None:
            value = plan.get('project_state')
            if value is None:
                return result  # Pure/legacy callers without a current project.
            path = Path(value)
            if not path.is_absolute():
                path = (Path(plan_path).parent if plan_path else Path.cwd()) / path
            state = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(state, Mapping):
            raise ValueError('project_state 必须为对象')
        minima = required_visual_minima(state)
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f'必做画面检查无法读取当前要求: {exc}')
        result['ok'] = False
        return result
    if not minima:
        return result
    operations = {op['id']: op for op in plan.get('operations', [])
                  if isinstance(op, Mapping) and isinstance(op.get('id'), str)}
    for kind, minimum in minima.items():
        bindings = {}
        for event in plan.get('visual_events', []):
            if not isinstance(event, Mapping):
                continue
            start, end = event.get('start_us'), event.get('end_us')
            if (type(start) is not int or type(end) is not int or not 0 <= start < end
                    or not all(isinstance(event.get(k), str) and event[k].strip()
                               for k in ('id', 'audience_need', 'composition'))):
                continue
            for technique in event.get('techniques', []):
                if not isinstance(technique, Mapping) or technique.get('kind') != kind:
                    continue
                for ref in technique.get('operation_ids', []):
                    if isinstance(ref, str) and operations.get(ref, {}).get('kind') in TECHNIQUE_OPERATIONS[kind]:
                        bindings.setdefault(ref, []).append(event)
        actual = []
        if draft is not None and kind == 'video_effect' and bindings and effect_report is None:
            from .video_effects import validate_video_effects
            effect_report = validate_video_effects(plan, draft, plan_path=plan_path)
        checked_effects = {row['id']: row for row in (effect_report or {}).get('effects', [])}
        for ref, events in bindings.items():
            op = operations[ref]
            spans = []
            try:
                if draft is None:
                    actual.append({'operation_id': ref, 'event_ids': [e['id'] for e in events]})
                    continue
                if kind == 'mask':
                    from .video_mask import _reference_mask
                    reference, _ = _reference_mask(op.get('native_reference'), op.get('shape'))
                    spans = _support_segments(op, draft)
                    masks = {m['id']: m for m in draft.get('materials', {}).get('common_mask', [])}
                    targets = [s for t in draft.get('tracks', []) if t.get('name') == op.get('track_name')
                               for s in t.get('segments', []) if s.get('id') == op.get('segment_id')]
                    attached = [masks[r] for s in targets for r in s.get('extra_material_refs', []) if r in masks]
                    if len(attached) != 1 or any(attached[0].get(k) != reference.get(k)
                                                  for k in ('type', 'resource_type', 'resource_id', 'path')):
                        spans = []
                elif (effect_report or {}).get('ok') and ref in checked_effects:
                    interval = checked_effects[ref]['interval_us']
                    media = {m['id']: m for m in draft.get('materials', {}).get('videos', [])}
                    for track in draft.get('tracks', []):
                        if track.get('type') != 'video' or (op['scope'] == 'segment' and track.get('name') != op['track_name']):
                            continue
                        for segment in track.get('segments', []):
                            if op['scope'] == 'segment' and segment.get('id') != op['segment_id']:
                                continue
                            source = media.get(segment.get('material_id'), {}).get('path')
                            if not source:
                                continue
                            visible = _support_segments({'kind': 'add_broll', 'track_name': track.get('name'),
                                'segment_id': segment.get('id'), 'source_path': source}, draft)
                            spans.extend((max(a, interval[0]), min(b, interval[1])) for a, b in visible
                                         if max(a, interval[0]) < min(b, interval[1]))
                overlap = [(max(a, e['start_us']), min(b, e['end_us'])) for a, b in spans for e in events
                           if max(a, e['start_us']) < min(b, e['end_us'])]
                if overlap:
                    actual.append({'operation_id': ref, 'event_ids': [e['id'] for e in events],
                                   'intervals_us': _merge_intervals(overlap)})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f'必做画面 {kind}/{ref} 回读失败: {exc}')
        rows[kind] = {'minimum': minimum, 'count': len(actual), 'operations': actual}
        if len(actual) < minimum:
            stage = '已绑定计划' if draft is None else '实际草稿'
            errors.append(f'必做画面 {kind}: {stage}仅 {len(actual)} 处，当前项目至少要求 {minimum} 处；不能省略或用其他技法代替')
    result['ok'] = not errors
    return result


def _validate_supporting_visuals(plan, events, operations, errors, *, draft=None):
    """Keep necessary asset work open and bind supporting compositions to execution."""
    supported = []
    for event in events:
        if not isinstance(event, Mapping): continue
        label = event.get('id')
        support = event.get('supporting_visual')
        if not isinstance(support, Mapping):
            errors.append(f'visual_event {label}: 缺少 supporting_visual 辅助画面安排')
            continue
        status, refs = support.get('status'), support.get('operation_ids')
        if status not in ('ready', 'needs_asset', 'not_needed'):
            errors.append(f'visual_event {label}: supporting_visual.status 必须为 ready/needs_asset/not_needed')
        if not isinstance(support.get('purpose'), str) or not support['purpose'].strip():
            errors.append(f'visual_event {label}: 辅助画面缺少信息/构图职责 purpose')
        if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs) or len(refs) != len(set(refs)):
            errors.append(f'visual_event {label}: 辅助画面 operation_ids 必须为不重复字符串列表')
            refs = []
        linked = {ref for technique in event.get('techniques', []) if isinstance(technique, Mapping) and isinstance(technique.get('operation_ids'), list)
                  for ref in technique.get('operation_ids', []) if isinstance(ref, str)
                  and operations.get(ref, {}).get('kind') in SUPPORT_OPERATIONS}
        if status == 'needs_asset':
            search = support.get('asset_search')
            if (not isinstance(search, Mapping) or not isinstance(search.get('queries'), list)
                    or not search['queries'] or any(not isinstance(q, str) or not q.strip() for q in search['queries'])
                    or not isinstance(search.get('next_action'), str) or not search['next_action'].strip()):
                errors.append(f'visual_event {label}: 缺素材须记录 asset_search.queries 和 next_action，继续查找或制作')
            errors.append(f'visual_event {label}: 辅助画面待补素材，不能作为完整包装放行')
        elif status == 'not_needed':
            if refs or linked or event.get('primary_visual') in {'media', 'relationship'}:
                errors.append(f'visual_event {label}: 辅助画面 not_needed 与当前资料/关系构图矛盾')
        elif status == 'ready':
            if not refs or set(refs) != linked:
                errors.append(f'visual_event {label}: 辅助画面须绑定本事件全部实际素材/蒙版操作')
                continue
            valid = True
            for ref in refs:
                operation = operations.get(ref, {})
                kind = operation.get('kind')
                if kind not in SUPPORT_OPERATIONS or not operation.get('track_name'):
                    errors.append(f'visual_event {label}: 辅助画面不支持或未定位操作 {ref}')
                    valid = False
                    continue
                reference = operation.get('native_reference')
                resource = (operation.get('source_path') if kind == 'add_broll'
                            else reference.get('path') if isinstance(reference, Mapping) else None)
                if not isinstance(resource, str) or not Path(resource).is_absolute() or not Path(resource).is_file():
                    errors.append(f'visual_event {label}: 辅助画面资源未落地 {ref}；须查找/制作后绑定绝对路径')
                    valid = False
                if kind == 'apply_video_mask' and not operation.get('segment_id'):
                    errors.append(f'visual_event {label}: 蒙版未绑定实际 segment_id')
                    valid = False
            if draft is not None and valid:
                start, end = event.get('start_us'), event.get('end_us')
                cursor = start
                if type(start) is not int or type(end) is not int or start >= end:
                    valid = False
                else:
                    intervals = []
                    for ref in refs:
                        spans = _support_segments(operations[ref], draft)
                        if not any(min(right, end) > max(left, start) for left, right in spans):
                            errors.append(f'visual_event {label}: 已选辅助画面操作未实际出现 {ref}')
                            valid = False
                        intervals.extend(spans)
                    for left, right in sorted(intervals):
                        if left <= cursor < right: cursor = right
                    if cursor < end and not plan.get('creative_brief'):
                        errors.append(f'visual_event {label}: 实际辅助画面/蒙版缺失、隐藏或未覆盖事件时段')
                        valid = False
            if valid: supported.append(label)
    if not supported:
        errors.append('v2 完整包装缺少已落地的辅助画面与构图；不能以全片人物加字幕或缺素材省略')
    return supported


def _validate_design_decisions(plan, events, operations, errors):
    decisions = plan.get("design_decisions")
    if not isinstance(decisions, Mapping):
        errors.append("v2 缺少 design_decisions 五领域判断")
        decisions = {}
    by_id = {event["id"]: event for event in events
             if isinstance(event, Mapping) and isinstance(event.get("id"), str)}
    def kinds(event):
        value = event.get("techniques")
        return {item.get("kind") for item in (value if isinstance(value, list) else [])
                if isinstance(item, Mapping) and isinstance(item.get("kind"), str)}
    def imports_preset(event):
        return any(operations.get(ref, {}).get('kind') in {'add_preset_group', 'replace_preset_group'}
                   for technique in event.get('techniques', []) if isinstance(technique, Mapping)
                   for ref in technique.get('operation_ids', []) if isinstance(ref, str))
    selected = set().union(*(kinds(event) for event in by_id.values()))
    # Executable choices also count: omitting their event declaration cannot
    # make an existing effect/media operation into a truthful omit decision.
    for kind, operation_kinds in TECHNIQUE_OPERATIONS.items():
        if any(op.get("kind") in operation_kinds for op in operations.values()
               if isinstance(op.get("kind"), str)):
            selected.add(kind)
    for domain, techniques in DECISION_TECHNIQUES.items():
        decision = decisions.get(domain)
        if not isinstance(decision, Mapping):
            errors.append(f"design_decisions 缺少 {domain} 判断")
            continue
        choice = decision.get("decision")
        if choice not in ("use", "omit"):
            errors.append(f"design_decisions.{domain}: decision 必须为 use/omit")
        if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
            errors.append(f"design_decisions.{domain}: 缺少 reason")
        refs = decision.get("event_ids")
        if not isinstance(refs, list):
            errors.append(f"design_decisions.{domain}: event_ids 必须为列表")
            refs = []
        valid = []
        for ref in refs:
            if not isinstance(ref, str) or ref not in by_id:
                errors.append(f"design_decisions.{domain}: 未知 event_id {ref!r}")
            else:
                valid.append(by_id[ref])
        if choice == "use":
            if not valid:
                errors.append(f"design_decisions.{domain}: use 必须引用有效事件")
            elif (domain not in {"typography", "composition"}
                  and not any(kinds(event) & techniques for event in valid)
                  and not (domain in {"motion", "sound"} and any(imports_preset(event) for event in valid))):
                errors.append(f"design_decisions.{domain}: use 缺少对应技术及执行操作")
        # A video effect can be static (blur/vignette). It may support a
        # declared motion choice, but its presence alone does not prove motion.
        elif choice == "omit" and selected & (techniques - {"video_effect"} if domain == "motion" else techniques):
            errors.append(f"design_decisions.{domain}: 已选技术与 omit 矛盾")


def _preset_effect_intervals(draft, prefix):
    """Read actual native dependencies and enabled clips, not catalog labels."""
    materials = {item['id']: item for group in draft.get('materials', {}).values() if isinstance(group, list)
                 for item in group if isinstance(item, Mapping) and item.get('id')}
    result = {'motion': [], 'sound': []}
    for track in draft.get('tracks', []):
        if (not str(track.get('name', '')).startswith(prefix) or track.get('visible') is False
                or (track.get('type') != 'video' and track.get('attribute', 0) & 1)):
            continue
        for segment in track.get('segments', []):
            if segment.get('visible') is False or segment.get('track_attribute', 0) & 1:
                continue
            start = segment.get('target_timerange', {}).get('start', 0)
            duration = segment.get('target_timerange', {}).get('duration', 0)
            if duration <= 0:
                continue
            if track.get('type') == 'audio':
                material = materials.get(segment.get('material_id'), {})
                if material.get('path') and Path(material['path']).is_file() and segment.get('volume', 1) > 0:
                    result['sound'].append((start, start + duration))
                continue
            clip = segment.get('clip') or {}
            if clip.get('alpha', 1) <= 0 or any(clip.get('scale', {}).get(a, 1) <= 0 for a in ('x', 'y')):
                continue
            if track.get('type') == 'video':
                material = materials.get(segment.get('material_id'), {})
                if not material.get('path') or not Path(material['path']).is_file():
                    continue
            for ref in segment.get('extra_material_refs', []):
                for animation in materials.get(ref, {}).get('animations', []):
                    length = animation.get('duration', 0)
                    offset = animation.get('start', 0)
                    if (type(length) is int and length > 0 and type(offset) is int and offset >= 0
                            and offset + length <= duration
                            and any(animation.get(k) for k in ('resource_id', 'effect_id', 'path'))):
                        motion_end = start + duration if animation.get('type') == 'loop' else start + offset + length
                        result['motion'].append((start + offset, motion_end))
            result['motion'].extend(_keyframe_intervals(segment))
    return result


def _keyframe_intervals(segment):
    """Only changing, valid visual keyframes create motion intervals."""
    span = segment.get('target_timerange') or {}
    start, duration = span.get('start'), span.get('duration')
    if type(start) is not int or type(duration) is not int or duration <= 0:
        return []
    intervals = []
    for group in segment.get('common_keyframes', []):
        if group.get('property_type') not in {'KFTypePositionX', 'KFTypePositionY', 'KFTypeScaleX',
                'KFTypeScaleY', 'KFTypeRotation', 'KFTypeAlpha', 'UNIFORM_SCALE'}:
            continue
        frames = group.get('keyframe_list') or []
        if (any(type(f.get('time_offset')) is not int or not 0 <= f['time_offset'] <= duration
                or not isinstance(f.get('values'), list) or not f['values']
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in f['values']) for f in frames)
                or any(a['time_offset'] >= b['time_offset'] for a, b in zip(frames, frames[1:]))):
            continue
        intervals.extend((start+a['time_offset'], start+b['time_offset']) for a, b in zip(frames, frames[1:])
                         if a['values'] != b['values'])
    return intervals


def _motion_operation_intervals(operation, draft):
    template = operation.get('new_template_id') or operation.get('template_id')
    prefix = f"JY_PRESET_{template}__NODE__{operation.get('node_id')}_"
    selected = []
    for track in draft.get('tracks', []):
        if not (track.get('name') == operation.get('track_name') or
                operation.get('kind') == 'animate_preset_group_transform' and str(track.get('name', '')).startswith(prefix)):
            continue
        selected.append({**track, 'segments': [{**segment, 'extra_material_refs': []} for segment in track.get('segments', [])
                        if not operation.get('segment_id') or segment.get('id') == operation['segment_id']]})
    return _preset_effect_intervals({'tracks': selected, 'materials': draft.get('materials', {})}, '')['motion']


def _merge_intervals(intervals):
    merged = []
    for left, right in sorted(intervals):
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return merged


def summarize_visual_sequence(plan, draft):
    """Read final timeline distribution for creative review, without taste gates.

    Simultaneous supporting images occupy one interval. A mask is reported
    separately from supplementary media. Native structure is not playback.
    """
    from .preset_registry import describe_visual_form, visual_form_key, compare_expression_features

    operations = [op for op in plan.get('operations', []) if isinstance(op, Mapping)]
    tracks = draft.get('tracks', [])
    materials = {m.get('id'): m for group in draft.get('materials', {}).values() if isinstance(group, list)
                 for m in group if isinstance(m, Mapping) and m.get('id')}
    instances, groups = [], set()
    for op in operations:
        if op.get('kind') not in ('add_preset_group', 'replace_preset_group'):
            continue
        template, node = op.get('new_template_id') or op.get('template_id'), op.get('node_id')
        key = (template, node)
        if key in groups:
            continue
        groups.add(key)
        prefix = f'JY_PRESET_{template}__NODE__{node}_'
        selected_tracks = [t for t in tracks if str(t.get('name', '')).startswith(prefix)]
        spans = []
        for track in selected_tracks:
            if track.get('type') != 'text' or track.get('visible') is False:
                continue
            for segment in track.get('segments', []):
                span = segment.get('target_timerange') or {}
                left, duration = span.get('start'), span.get('duration')
                if (segment.get('material_id') in materials and segment.get('visible') is not False
                        and not segment.get('track_attribute', 0) & 1 and (segment.get('clip') or {}).get('alpha', 1) > 0
                        and type(left) is int and type(duration) is int and left >= 0 and duration > 0):
                    spans.append((left, left + duration))
        if spans:
            instances.append({'template_id': template, 'node_id': node, 'intervals': _merge_intervals(spans),
                              'visual_features': describe_visual_form({'tracks': selected_tracks, 'materials': draft.get('materials', {})})})
    media_by_operation = {op['id']: _support_segments(op, draft) for op in operations
                          if op.get('id') and op.get('kind') == 'add_broll'}
    masks_by_operation = {op['id']: _support_segments(op, draft) for op in operations
                          if op.get('id') and op.get('kind') == 'apply_video_mask'}
    media = _merge_intervals([span for spans in media_by_operation.values() for span in spans])
    masks = _merge_intervals([span for spans in masks_by_operation.values() for span in spans])
    motion_operations = [{'operation_id': op['id'], 'kind': op['kind'],
                          'changing_intervals': _merge_intervals(_motion_operation_intervals(op, draft))}
                         for op in operations if op.get('id') and op.get('kind') in TECHNIQUE_OPERATIONS['keyframes']]
    for row in motion_operations:
        row['actual_motion'] = bool(row['changing_intervals'])
    audio_features = describe_visual_form({'tracks': [t for t in tracks if t.get('type') == 'audio'],
                                          'materials': draft.get('materials', {})})
    caption_name = (plan.get('content_gate') or {}).get('ordinary_caption_track_name') or plan.get('ordinary_caption_track_name') or 'JY_ZH_SUBTITLES'
    captions = describe_visual_form({'tracks': [t for t in tracks if t.get('name') == caption_name],
                                    'materials': draft.get('materials', {})})
    from .media_crop import material_crop
    video_layers = []
    for track in tracks:
        if track.get('type') != 'video':
            continue
        for segment in track.get('segments', []):
            material = materials.get(segment.get('material_id'), {})
            span = segment.get('target_timerange') or {}
            source = material.get('path') or material.get('media_path')
            try:
                crop = material_crop(material)
            except ValueError:
                crop = None
            video_layers.append({'track_name': track.get('name'), 'segment_id': segment.get('id'),
                'start_us': span.get('start'), 'duration_us': span.get('duration'),
                'source_path': source, 'source_available': bool(source and Path(source).is_file()),
                'source_type': material.get('type'), 'source_timerange': segment.get('source_timerange'),
                'source_crop': crop, 'clip': segment.get('clip'),
                'hidden': track.get('visible') is False or segment.get('visible') is False or bool(segment.get('track_attribute', 0) & 1),
                'mask_refs': [ref for ref in segment.get('extra_material_refs', []) if materials.get(ref, {}).get('type') == 'mask']})
    rows, repeated, same_primary_runs = [], [], []
    for event in sorted(plan.get('visual_events', []), key=lambda e: (e['start_us'], e['end_us'], e['id'])):
        start, end = event['start_us'], event['end_us']
        def intersects(spans):
            return any(min(right, end) > max(left, start) for left, right in spans)
        active = [instance for instance in instances if intersects(instance['intervals'])]
        row = {'event_id': event['id'], 'start_us': start, 'end_us': end,
               'audience_need': event.get('audience_need'), 'expression_role': event.get('expression_role'),
               'planned_primary_visual': event.get('primary_visual'), 'planned_composition': event.get('composition'),
               'transition': event.get('transition', {}),
               'template_ids': sorted({str(i['template_id']) for i in active}),
               'preset_forms': [i['visual_features'] for i in active],
               'changing_motion_operation_ids': [m['operation_id'] for m in motion_operations if intersects(m['changing_intervals'])],
               'supporting_media_intervals': _merge_intervals([(max(left, start), min(right, end)) for left, right in media if min(right, end) > max(left, start)]),
               'mask_operation_ids': [ref for ref, spans in masks_by_operation.items() if intersects(spans)]}
        row['supporting_media_duration_us'] = sum(b - a for a, b in row['supporting_media_intervals'])
        if rows:
            previous = rows[-1]
            previous_forms = {visual_form_key(f) for f in previous['preset_forms']} - {None}
            current_forms = {visual_form_key(f) for f in row['preset_forms']} - {None}
            row['compared_with_previous'] = [compare_expression_features(a, b)
                                            for a in previous['preset_forms'] for b in row['preset_forms']]
            if previous_forms and previous_forms == current_forms and previous['end_us'] == start:
                repeated.append({'event_ids': [previous['event_id'], row['event_id']],
                                 'transition_intent': row['transition'].get('intent'),
                                 'reason': '相邻事件的文字布局、信息顺序和进退场机制相同；结合当前内容判断是否需要调整'})
        if (same_primary_runs and same_primary_runs[-1]['primary_visual'] == event.get('primary_visual')
                and same_primary_runs[-1]['end_us'] == start):
            same_primary_runs[-1]['end_us'] = end
            same_primary_runs[-1]['event_ids'].append(event['id'])
        else:
            same_primary_runs.append({'primary_visual': event.get('primary_visual'), 'start_us': start,
                                      'end_us': end, 'event_ids': [event['id']]})
        rows.append(row)
    end_us = max([s.get('target_timerange', {}).get('start', 0) + s.get('target_timerange', {}).get('duration', 0)
                  for t in tracks for s in t.get('segments', [])] or [0])
    no_media, cursor = [], 0
    for start, end in media:
        if start > cursor:
            no_media.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < end_us:
        no_media.append([cursor, end_us])
    from .attention_review import review_attention
    return {'basis': 'assembled_native_structure', 'events': rows, 'preset_instances': instances,
            'attention_review': review_attention(plan, draft),
            'preset_instance_count': len(instances),
            'distinct_template_count': len({i['template_id'] for i in instances}),
            'supporting_media_intervals': media, 'supporting_media_duration_us': sum(b - a for a, b in media),
            'mask_intervals': masks, 'without_supporting_media_intervals': no_media,
            'same_planned_primary_runs': same_primary_runs, 'adjacent_repeated_preset_forms': repeated,
            'motion_operations': motion_operations,
            'video_layers': video_layers,
            'ordinary_caption_features': captions,
            'audio_sources': audio_features['audio_sources'],
            'reused_audio_sources': [a for a in audio_features['audio_sources'] if len(a['segments']) > 1],
            'native_visual_verified': False, 'creative_quality_verified': False,
            'interpretation': '数量和时段用于定位实际分布，不代表视觉密度评分；无辅助媒体不等于静止或缺乏设计，规划主视觉不等于机器已判断实际主次'}


def validate_visual_execution(plan, draft, *, plan_path=None):
    """Complete deferred bundle checks after assembly and on Writer readback."""
    if plan.get('project_format', {}).get('visual_planning_version') != 2:
        return {'ok': True, 'errors': [], 'declared': False,
                'native_visual_verified': False, 'creative_quality_verified': False}
    operations = {op['id']: op for op in plan.get('operations', []) if isinstance(op, Mapping) and op.get('id')}
    features = {}
    for identifier, op in operations.items():
        if op.get('kind') in {'add_preset_group', 'replace_preset_group'}:
            template = op.get('new_template_id') or op.get('template_id')
            prefix = f"JY_PRESET_{template}__NODE__{op.get('node_id')}_"
            features[identifier] = _preset_effect_intervals(draft, prefix)
    actual_by_event = {}
    for event in plan.get('visual_events', []):
        ids = {ref for technique in event.get('techniques', []) for ref in technique.get('operation_ids', [])}
        actual_by_event[event['id']] = {
            domain for domain in ('motion', 'sound')
            if any(min(right, event['end_us']) > max(left, event['start_us'])
                   for ref in ids for left, right in features.get(ref, {}).get(domain, []))}
    errors = []
    from .creative_plan import validate_creative_plan
    creative = validate_creative_plan(plan, draft=draft, plan_path=plan_path)
    errors.extend(creative['errors'])
    supporting = _validate_supporting_visuals(plan, plan.get('visual_events', []), operations, errors, draft=draft)
    decisions = plan.get('design_decisions', {})
    for domain in ('motion', 'sound'):
        decision = decisions.get(domain, {})
        if decision.get('decision') == 'omit' and any(row[domain] for row in features.values()):
            errors.append(f'design_decisions.{domain}: 实际导入的预设原配效果与 omit 矛盾')
        elif decision.get('decision') == 'use':
            refs = set(decision.get('event_ids', []))
            events = [event for event in plan.get('visual_events', []) if event.get('id') in refs]
            direct = any(technique.get('kind') in (DECISION_TECHNIQUES[domain] - {'keyframes'} if domain == 'motion' else DECISION_TECHNIQUES[domain])
                         for event in events for technique in event.get('techniques', []))
            if domain == 'motion':
                direct = direct or any(min(right, event['end_us']) > max(left, event['start_us'])
                    for event in events for technique in event.get('techniques', []) if technique.get('kind') == 'keyframes'
                    for ref in technique.get('operation_ids', []) for left, right in _motion_operation_intervals(operations.get(ref, {}), draft))
            if not direct and not any(domain in actual_by_event.get(ref, set()) for ref in refs):
                errors.append(f'design_decisions.{domain}: 所引用事件未实际导入所需预设动画或原配音轨')
    return {'ok': not errors, 'errors': errors, 'declared': True,
            'supporting_visual_events': supporting,
            'creative_delivery': creative,
            'sequence_summary': summarize_visual_sequence(plan, draft),
            'preset_effects_by_event': {key: sorted(value) for key, value in actual_by_event.items()},
            'native_visual_verified': False, 'creative_quality_verified': False}


def _validate_speech_events(events, content_gate, errors):
    speech = _items(content_gate.get("final_retained_speech")) if isinstance(content_gate, Mapping) else []
    if not speech:
        errors.append("v2 缺少 content_gate.final_retained_speech")
    indexed = {}
    for index, item in enumerate(speech):
        ref = str(item.get("id", item.get("speech_id", index))).strip()
        start, end = _time(item, "start"), _time(item, "end")
        if not ref or ref in indexed:
            errors.append(f"final_retained_speech 引用不唯一: {ref!r}")
            continue
        if start is None or end is None or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            errors.append(f"final_retained_speech {ref}: 起止时间无效")
            continue
        indexed[ref] = (round(start * 1_000_000), round(end * 1_000_000))
    coverage = {ref: [] for ref in indexed}
    for event in events:
        if not isinstance(event, Mapping):
            continue
        label = event.get("id")
        refs = event.get("speech_ids")
        if not isinstance(refs, list):
            errors.append(f"visual_event {label}: v2 缺少 speech_ids 列表，无口述事件须显式为空列表")
            refs = []
        for ref in refs:
            key = str(ref) if type(ref) in (str, int) else None
            if key not in indexed:
                errors.append(f"visual_event {label}: 未知 speech_id {ref!r}")
                continue
            start, end = event.get("start_us"), event.get("end_us")
            if type(start) is int and type(end) is int and 0 <= start < end:
                left, right = indexed[key]
                if min(end, right) > max(start, left):
                    coverage[key].append((max(start, left), min(end, right)))
        handbook = event.get("handbook_refs")
        if not isinstance(handbook, list) or not handbook or any(not isinstance(ref, str) or not ref.strip() for ref in handbook):
            errors.append(f"visual_event {label}: v2 缺少非空 handbook_refs 字符串列表")
        if not isinstance(event.get("review_focus"), str) or not event["review_focus"].strip():
            errors.append(f"visual_event {label}: v2 缺少 review_focus 待观察结果")
    for ref, (start, end) in indexed.items():
        cursor = start
        for left, right in sorted(coverage[ref]):
            if left > cursor:
                break
            cursor = max(cursor, right)
        if cursor < end:
            errors.append(f"final_retained_speech {ref}: visual_events 未完整覆盖语音时段")


def validate_visual_events(plan: Mapping[str, Any], *, content_gate: Any = None) -> dict[str, Any]:
    events = plan.get("visual_events")
    project_format = plan.get("project_format")
    version = project_format.get("visual_planning_version") if isinstance(project_format, Mapping) else None
    if events is None and version is None:
        return {"ok": True, "errors": [], "declared": False,
                "native_visual_verified": False, "creative_quality_verified": False}
    errors: list[str] = []
    if type(version) is not int or version not in (1, 2):
        errors.append("visual_planning_version 必须为 1 或 2")
    if not isinstance(events, list) or not events:
        return {"ok": False, "errors": errors + ["缺少 visual_events 画面安排"], "declared": True,
                "native_visual_verified": False, "creative_quality_verified": False}
    raw_operations = plan.get("operations")
    operations = {o["id"]: o for o in (raw_operations if isinstance(raw_operations, list) else []) if isinstance(o, Mapping) and isinstance(o.get("id"), str)}
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping):
            errors.append("visual_events 项必须是对象")
            continue
        label = event.get("id")
        if not isinstance(label, str) or not label.strip() or label in seen:
            errors.append("visual_event.id 必须是唯一非空字符串")
            label = "<invalid>"
        seen.add(label)
        for field in ("audience_need", "composition"):
            if not isinstance(event.get(field), str) or not event[field].strip():
                errors.append(f"visual_event {label}: 缺少 {field}")
        if 'expression_role' in event and (not isinstance(event['expression_role'], str) or not event['expression_role'].strip()):
            errors.append(f'visual_event {label}: expression_role 须说明当前表达职责')
        if 'transition' in event:
            transition = event['transition']
            if (not isinstance(transition, Mapping) or transition.get('intent') not in ('hold', 'change')
                    or not isinstance(transition.get('reason'), str) or not transition['reason'].strip()):
                errors.append(f'visual_event {label}: transition 需要 hold/change 及当前内容理由 reason')
        if 'preset_request' in event and (not isinstance(event['preset_request'], Mapping) or not event['preset_request'].get('intent')):
            errors.append(f'visual_event {label}: preset_request 需要当前表达用途 intent')
        if not isinstance(event.get("primary_visual"), str) or event["primary_visual"] not in {"person", "media", "relationship", "text"}:
            errors.append(f"visual_event {label}: primary_visual 不正确")
        start, end = event.get("start_us"), event.get("end_us")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            errors.append(f"visual_event {label}: 需要有效起止时点")
        techniques = event.get("techniques")
        if not isinstance(techniques, list):
            errors.append(f"visual_event {label}: techniques 必须显式为列表，保留人物原样可用空列表")
            continue
        for technique in techniques:
            if not isinstance(technique, Mapping) or not isinstance(technique.get("kind"), str) or technique["kind"] not in TECHNIQUE_OPERATIONS:
                errors.append(f"visual_event {label}: 不支持的技术，先接通共享入口，不得静默降级")
                continue
            kind = technique["kind"]
            ids = technique.get("operation_ids")
            if not isinstance(ids, list) or not ids:
                errors.append(f"visual_event {label}: 已选 {kind} 缺少执行操作")
                continue
            for operation_id in ids:
                operation = operations.get(operation_id) if isinstance(operation_id, str) else None
                if operation is None or operation.get("kind") not in TECHNIQUE_OPERATIONS[kind]:
                    errors.append(f"visual_event {label}: {kind} 未连接匹配操作 {operation_id!r}")
                elif kind == "video_effect" and operation.get("visual_event_id") != label:
                    errors.append(f"visual_event {label}: video_effect 指向其他画面事件")
                elif kind == "text_animation" and operation.get("visual_event_id") != label:
                    errors.append(f"visual_event {label}: text_animation 指向其他画面事件")
    if version == 2:
        _validate_speech_events(events, content_gate if content_gate is not None else plan.get("content_gate"), errors)
        if not plan.get('creative_brief') or 'design_decisions' in plan:
            _validate_design_decisions(plan, events, operations, errors)
        _validate_supporting_visuals(plan, events, operations, errors)
    return {"ok": not errors, "errors": errors, "declared": True,
            "native_visual_verified": False, "creative_quality_verified": False}
