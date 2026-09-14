"""Observable viewer needs in the existing CandidatePlan, before effect selection.

This module checks delivery, not beauty. Source observations remain attributed
to the reviewer; native playback and user acceptance are never inferred here.
"""
from collections.abc import Mapping
from pathlib import Path

from .media_crop import crop_rectangle, material_crop, require_content_region
from .media_ledger import SHA256_RE, file_sha256


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _covered(spans, start, end):
    cursor = start
    for left, right in sorted(spans):
        if left > cursor:
            break
        cursor = max(cursor, right)
    return cursor >= end


def _span(item):
    value = item.get('target_timerange') or {}
    start, duration = value.get('start'), value.get('duration')
    return (start, start + duration) if type(start) is int and type(duration) is int and duration > 0 else None


def validate_creative_plan(plan, *, draft=None, plan_path=None, require=False):
    """A need names an object, observable result, interval and actual delivery.

    Existing plans remain readable. New v2 candidate builds call with require
    true; a local revision can transcribe the existing design without choosing
    a new whole-film direction. No additional production-ready state exists.
    """
    brief = plan.get('creative_brief')
    if brief is None:
        return {'ok': not require, 'errors': ['缺少 creative_brief：先确定整片目标和表达，再选择资源'] if require else [],
                'declared': False, 'requirements': [], 'legacy_plan': True,
                'native_visual_verified': False, 'creative_quality_verified': False}
    errors, rows = [], []
    if not isinstance(brief, Mapping):
        return {'ok': False, 'errors': ['creative_brief 必须为对象'], 'requirements': []}
    for field in ('viewer_takeaway', 'progression', 'visual_strategy', 'sound_strategy'):
        if not _text(brief.get(field)):
            errors.append(f'creative_brief 缺少 {field}')
    operations = {op['id']: op for op in plan.get('operations', []) if isinstance(op, Mapping) and _text(op.get('id'))}
    materials = {m['id']: m for group in (draft or {}).get('materials', {}).values() if isinstance(group, list)
                 for m in group if isinstance(m, Mapping) and m.get('id')}
    base = Path(plan_path).parent if plan_path else Path.cwd()
    def path(value):
        item = Path(value)
        return (item if item.is_absolute() else base / item).resolve()
    seen = set()
    source_digests = {}
    for event in plan.get('visual_events', []):
        if not isinstance(event, Mapping):
            continue
        eid = event.get('id')
        if not _text(event.get('expression_role')):
            errors.append(f'{eid}: 缺少当前段在整片中的 expression_role')
        needs = event.get('visual_requirements')
        if not isinstance(needs, list) or not needs:
            errors.append(f'{eid}: 缺少 visual_requirements；写观众须看清什么，不以效果名称代替')
            continue
        for need in needs:
            label = f'{eid}/{need.get("id")}' if isinstance(need, Mapping) else str(eid)
            try:
                if not isinstance(need, Mapping):
                    raise ValueError('画面需求必须为对象')
                if not _text(need.get('id')) or need['id'] in seen:
                    raise ValueError('visual_requirement.id 必须全片唯一')
                seen.add(need['id'])
                if not all(_text(need.get(key)) for key in ('subject', 'observable')):
                    raise ValueError('需要具体 subject 与 observable 观察结果')
                delivery = need.get('delivery')
                if delivery not in {'aroll', 'media', 'text'}:
                    raise ValueError('delivery 必须为 aroll/media/text')
                behavior = need.get('time_behavior', 'still')
                if behavior not in {'still', 'continuous_action', 'graphic_motion'}:
                    raise ValueError('time_behavior 必须为 still/continuous_action/graphic_motion')
                if behavior == 'continuous_action' and delivery == 'text':
                    raise ValueError('连续操作过程不能以文字代替')
                start, end = need.get('start_us', event.get('start_us')), need.get('end_us', event.get('end_us'))
                if (type(start) is not int or type(end) is not int or start >= end
                        or not event['start_us'] <= start < end <= event['end_us']):
                    raise ValueError('观察区间必须在当前视觉事件内')
                refs = need.get('operation_ids', [])
                if not isinstance(refs, list) or any(not _text(ref) or ref not in operations for ref in refs):
                    raise ValueError('operation_ids 必须连接当前实际操作')
                selected_ops = [operations[ref] for ref in refs]
                if delivery == 'media':
                    if not _text(need.get('request_path')) or not path(need['request_path']).is_file():
                        raise ValueError('媒体需求尚未连接实际 request_path')
                    if not selected_ops or any(op.get('kind') != 'add_broll' for op in selected_ops):
                        raise ValueError('媒体需求尚未落实 add_broll，不能以蒙版或字幕代交')
                    for op in selected_ops:
                        binding = op.get('media_asset') or {}
                        if not binding.get('request_path') or path(binding['request_path']) != path(need['request_path']):
                            raise ValueError('当前素材操作绑定了其他画面需求')
                    spans = [(op['start_us'], op['end_us']) for op in selected_ops]
                    if not _covered(spans, start, end):
                        raise ValueError('素材操作没有覆盖所需观察区间')
                observation = need.get('source_observation') or {}
                source_region = None
                if delivery == 'aroll':
                    if (not _text(need.get('track_name')) or not _text(observation.get('source_path'))
                            or not path(observation['source_path']).is_file()
                            or observation.get('scope') not in {'video_frames', 'video_segment'}
                            or not _text(observation.get('observation'))):
                        raise ValueError('沿用主画面须绑定 track_name 和实际源观察 source_observation')
                    left, right = observation.get('start_us'), observation.get('end_us')
                    if type(left) is not int or type(right) is not int or not 0 <= left < right:
                        raise ValueError('source_observation 需要已查看的源区间')
                    if 'content_region' in observation:
                        source_region = crop_rectangle(observation['content_region'])
                        if not observation.get('source_sha256'):
                            raise ValueError('source_observation.content_region 须绑定实际源文件 source_sha256')
                    if 'source_sha256' in observation:
                        expected = observation['source_sha256']
                        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected.lower()):
                            raise ValueError('source_observation.source_sha256 必须是源文件摘要')
                        source_path = path(observation['source_path'])
                        if source_path not in source_digests:
                            source_digests[source_path] = file_sha256(source_path)
                        if source_digests[source_path] != expected.lower():
                            raise ValueError('源文件已变化，source_observation 不可复用')
                if delivery == 'text' and not need.get('track_name') and not selected_ops:
                    raise ValueError('文字需求须连接实际 track_name 或预设操作')
                row = {'event_id': eid, 'requirement_id': need['id'], 'subject': need['subject'],
                       'observable': need['observable'], 'delivery': delivery, 'time_behavior': behavior,
                       'start_us': start, 'end_us': end, 'operation_ids': refs,
                       'source_observation_scope': observation.get('scope'), 'assembled': draft is not None}
                if source_region is not None:
                    row['source_content_region'] = source_region
                    row['source_sha256'] = observation['source_sha256']
                if draft is not None:
                    from .visual_planning import _support_segments, _preset_effect_intervals
                    spans, matching_tracks = [], []
                    for track in draft.get('tracks', []):
                        name = str(track.get('name', ''))
                        prefixes = [f"JY_PRESET_{op.get('new_template_id') or op.get('template_id')}__NODE__{op.get('node_id')}_"
                                    for op in selected_ops if op.get('kind') in {'add_preset_group', 'replace_preset_group'}]
                        if name == need.get('track_name') or any(name.startswith(prefix) for prefix in prefixes):
                            matching_tracks.append(track)
                    if delivery == 'media':
                        spans = [span for op in selected_ops for span in _support_segments(op, draft)]
                        matching_tracks = [t for t in draft.get('tracks', []) if t.get('name') in {op['track_name'] for op in selected_ops}]
                    else:
                        for track in matching_tracks:
                            if track.get('visible') is False or (delivery == 'text' and (track.get('type') != 'text' or track.get('attribute', 0) & 1)):
                                continue
                            if delivery == 'aroll' and track.get('type') != 'video':
                                continue
                            for segment in track.get('segments', []):
                                span, clip = _span(segment), segment.get('clip') or {}
                                if (not span or segment.get('visible') is False or segment.get('track_attribute', 0) & 1
                                        or clip.get('alpha', 1) <= 0 or any(clip.get('scale', {}).get(a, 1) <= 0 for a in ('x', 'y'))):
                                    continue
                                material = materials.get(segment.get('material_id'), {})
                                if delivery == 'aroll':
                                    actual_path = material.get('path') or material.get('media_path')
                                    if not actual_path or path(actual_path) != path(observation['source_path']):
                                        continue
                                    left, right = max(start, span[0]), min(end, span[1])
                                    if left >= right:
                                        continue
                                    source_range = segment.get('source_timerange') or {}
                                    ratio = source_range.get('duration', 0) / (span[1]-span[0])
                                    source_left = source_range.get('start', -1) + (left-span[0])*ratio
                                    source_right = source_range.get('start', -1) + (right-span[0])*ratio
                                    if ratio <= 0 or source_left < observation['start_us'] or source_right > observation['end_us']:
                                        continue
                                    if source_region is not None:
                                        require_content_region(material_crop(material), source_region)
                                elif not material.get('content'):
                                    continue
                                spans.append(span)
                    row['actual_intervals'] = spans
                    if not _covered(spans, start, end):
                        raise ValueError('最终轨道未完整交付所需可见区间或超出已观察源区间')
                    if behavior == 'graphic_motion':
                        effects = _preset_effect_intervals({'tracks': matching_tracks, 'materials': draft.get('materials', {})}, '')
                        if not any(min(right, end) > max(left, start) for left, right in effects['motion']):
                            raise ValueError('所需图形运动未出现实际变化；静态两端关键帧不能代交')
                rows.append(row)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append(f'{label}: {exc}')
    return {'ok': not errors, 'errors': errors, 'declared': True, 'requirements': rows,
            'native_visual_verified': False, 'creative_quality_verified': False}
