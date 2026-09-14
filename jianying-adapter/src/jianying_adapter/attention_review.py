"""Locate opening and pacing decisions in the existing plan and native timeline.

Diagnostics for the creator, not an aesthetic score or a new Writer gate.
"""
import json


def review_attention(plan, draft=None):
    from .visual_planning import _merge_intervals, _preset_effect_intervals
    events = sorted(plan.get('visual_events', []), key=lambda e: e['start_us'])
    end = max([e['end_us'] for e in events] + [(draft or {}).get('duration', 0)])
    brief = plan.get('creative_brief') or {}
    hook = brief.get('hook') or {}
    opening = next((e for e in events if e.get('id') == hook.get('event_id')), None) if isinstance(hook, dict) else None
    hook_ok = bool(opening and opening['start_us'] == 0 and
                   all(isinstance(hook.get(k), str) and hook[k].strip() for k in ('strategy', 'reason')))
    warnings = [] if hook_ok else ['开场缺少明确Hook：在creative_brief.hook绑定从0开始的事件、策略与停留理由']
    result = {'opening_us': [0, min(3_000_000, end)], 'hook': hook,
              'hook_declared': hook_ok, 'pacing_target_us': [2_000_000, 3_500_000],
              'warnings': warnings, 'review_required': not hook_ok,
              'basis': 'plan_only' if draft is None else 'native_timeline_structure',
              'creative_quality_verified': False}
    if draft is None:
        result['next_action'] = '先设计前3秒停留理由及有效视觉推进；装配后定位实际长停留'
        return result
    end = max([end] + [s.get('target_timerange', {}).get('start', 0) + s.get('target_timerange', {}).get('duration', 0)
                      for t in draft.get('tracks', []) for s in t.get('segments', [])])
    caption = (plan.get('content_gate') or {}).get('ordinary_caption_track_name') or plan.get('ordinary_caption_track_name') or 'JY_ZH_SUBTITLES'
    materials = {m['id']: m for group in draft.get('materials', {}).values() if isinstance(group, list)
                 for m in group if isinstance(m, dict) and m.get('id')}
    points = {0, end}
    reasons = []
    active_tracks = []
    for track in draft.get('tracks', []):
        if track.get('visible') is False or track.get('type') not in {'video', 'text'}:
            continue
        name = track.get('name', '')
        if track.get('type') == 'text' and name == caption:
            continue
        segments = []
        previous = None
        for segment in sorted(track.get('segments', []), key=lambda s: s.get('target_timerange', {}).get('start', 0)):
            if (segment.get('visible') is False or segment.get('track_attribute', 0) & 1
                    or (segment.get('clip') or {}).get('alpha', 1) <= 0):
                continue
            material = materials.get(segment.get('material_id'))
            span = segment.get('target_timerange', {})
            start, duration = span.get('start'), span.get('duration')
            if not material or type(start) is not int or type(duration) is not int or duration <= 0:
                continue
            segments.append(segment)
            if track['type'] == 'text':
                try:
                    content = json.loads(material.get('content', '{}')).get('text', '').strip()
                except (ValueError, AttributeError):
                    content = ''
                signature = (content, segment.get('clip'))
                changed = bool(content) and (previous is None or signature != previous[0] or start != previous[1])
            else:
                # Same-source jump cuts or split-at-the-same-transform are not
                # automatically credited as new visual information.
                signature = (material.get('path'), material.get('crop'), segment.get('clip'))
                changed = previous is None or signature != previous[0] or start != previous[1]
            if changed:
                points.add(start)
                reasons.append({'time_us': start, 'track': name, 'kind': track['type'] + '_change'})
            if track['type'] == 'video' and name != 'JY_ROUGH_CUT_VIDEO':
                points.add(start + duration)
            previous = (signature, start + duration)
        active_tracks.append({**track, 'segments': segments})
    moving = _merge_intervals(_preset_effect_intervals({'tracks': active_tracks, 'materials': draft.get('materials', {})}, '')['motion'])
    boundaries = sorted({max(0, min(end, p)) for p in points} | {max(0, min(end, p)) for span in moving for p in span})
    gaps = []
    for start, stop in zip(boundaries, boundaries[1:]):
        if stop - start <= 3_500_000 or any(left <= start and stop <= right for left, right in moving):
            continue
        active = [e for e in events if min(e['end_us'], stop) > max(e['start_us'], start)]
        gaps.append({'start_us': start, 'end_us': stop, 'event_ids': [e['id'] for e in active],
                     'review_focus': [e.get('review_focus') for e in active],
                     'source_action_observations': [n.get('source_observation') for e in active
                         for n in e.get('visual_requirements', []) if n.get('time_behavior') == 'continuous_action']})
    if gaps:
        warnings.append('存在超过3.5秒未识别到有效视觉推进的区间；核对实际动作/阅读用途或定向调整')
    result.update(change_points=reasons, changing_intervals=moving, long_holds=gaps,
                  review_required=bool(warnings),
                  limitations='普通字幕换句和同源同构图切段不计推进；原片动作须据实际源观察判断，已检测运动不证明幅度或内容有效。')
    return result


def storyboard(plan):
    """One view of the existing design, never a second editable plan."""
    def cell(value):
        return str(value or '').replace('|', '\\|').replace('\n', ' ')
    lines = ['# 当前分镜摘要', '', '| 时间 | 职责与理解任务 | 构图 | 需求 |', '| --- | --- | --- | --- |']
    for event in sorted(plan.get('visual_events', []), key=lambda e: e['start_us']):
        needs = '；'.join(str(n.get('observable', '')) for n in event.get('visual_requirements', []))
        lines.append(f"| {event['start_us']/1e6:.2f}–{event['end_us']/1e6:.2f}s | {cell(event.get('expression_role'))}：{cell(event.get('audience_need'))} | {cell(event.get('composition'))} | {cell(needs)} |")
    return '\n'.join(lines) + '\n'
