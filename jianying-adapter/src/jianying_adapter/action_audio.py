# Copyright (c) 2026 Blackkeys0430 — AutoCUT original project code.
# Origin: https://github.com/Blackkeys0430/AutoCUT-public
# SPDX-License-Identifier: LicenseRef-AutoCUT-Personal-Use-1.0
"""Action-bound local sound effects through the shared candidate pipeline."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import sys
import uuid
from pathlib import Path

from .preset_audio import is_preset_audio, apply_preset_gain, validate_preset_audio


def _us(value):
    if type(value) is not int or value < 0:
        raise ValueError('audio time must be nonnegative integer microseconds')
    return value


def _gain(db, *, maximum_db=0):
    if isinstance(db, bool) or not isinstance(db, (int, float)) or not math.isfinite(db) or not -60 <= db <= maximum_db:
        raise ValueError(f'audio gain must be finite dB between -60 and {maximum_db}')
    return 10 ** (db / 20)


def _native():
    root = Path(__file__).resolve().parents[2] / 'vendor'
    for folder in ('python-deps', 'pyJianYingDraft-source'):
        path = str(root / folder)
        if path not in sys.path:
            sys.path.insert(0, path)
    import pyJianYingDraft as native
    return native


def _check_binding(event, plan):
    matches = [v for v in plan.get('visual_events', []) if v.get('id') == event.get('visual_event_id')]
    if len(matches) != 1 or not event.get('reason'):
        raise ValueError('sound requires one visual_event and a reason')
    visual = matches[0]
    action_time = _us(event['action_time_us'])
    if not visual['start_us'] <= action_time < visual['end_us']:
        raise ValueError('sound action time outside visual event')
    operation_ids = {x for t in visual['techniques'] if t['kind'] != 'sound' for x in t['operation_ids']}
    if event.get('action_operation_id') not in operation_ids:
        raise ValueError('sound must bind a declared visual operation')
    start, duration, anchor = _us(event['start_us']), _us(event['duration_us']), _us(event['anchor_offset_us'])
    if not duration or anchor >= duration or start + anchor != action_time:
        raise ValueError('sound onset anchor does not match action time')
    fi, fo = _us(event['fade_in_us']), _us(event['fade_out_us'])
    if fi + fo > duration:
        raise ValueError('audio fades exceed duration')
    _us(event['source_start_us'])
    _gain(event['gain_db'])


def _resolve_event(event, plan, draft, *, plan_path=None):
    """New anchors read compiled native motion; legacy events retain their contract."""
    if 'action_anchor' not in event:
        return event
    from .text_animation import resolve_text_intro_anchor
    action_time = resolve_text_intro_anchor(plan, draft, event.get('action_operation_id'), event['action_anchor'], plan_path=plan_path)
    start = action_time - _us(event['anchor_offset_us'])
    _us(start)
    for key, actual in (('action_time_us', action_time), ('start_us', start)):
        if key in event and _us(event[key]) != actual:
            raise ValueError('sound explicit ' + key + ' differs from compiled text entrance anchor')
    return {**event, 'action_time_us': action_time, 'start_us': start}


def _dialogue_baseline(plan, *, plan_path=None):
    """Read the existing base contract so Writer can distinguish silence from drift."""
    if not plan.get('base_draft'):
        return None  # Legacy callers have the original unity-only contract.
    path = Path(plan['base_draft'])
    if not path.is_absolute() and plan_path is not None:
        path = Path(plan_path).resolve().parent / path
    base = json.loads(path.read_text('utf-8-sig'))
    tracks = [t for t in base['tracks'] if t.get('name') == 'JY_ROUGH_CUT_VIDEO' and t.get('type') == 'video']
    if len(tracks) != 1 or not tracks[0].get('segments'):
        raise ValueError('dialogue baseline requires unique rough-cut video track')
    rows = []
    for segment in tracks[0]['segments']:
        volume = segment.get('volume')
        if (type(volume) not in (int, float) or volume not in (0, 1)
                or any(k.get('property_type') == 'KFTypeVolume' for k in segment.get('common_keyframes', []))):
            raise ValueError('existing dialogue gain/automation conflicts with explicit mix')
        timerange = segment['target_timerange']
        start, duration = _us(timerange['start']), _us(timerange['duration'])
        if not duration:
            raise ValueError('dialogue baseline duration must be positive')
        rows.append((start, start + duration, volume))
    return sorted(rows)


def _baseline_volume(segment, baseline):
    if baseline is None:
        return 1.0
    timerange = segment['target_timerange']
    start, duration = _us(timerange['start']), _us(timerange['duration'])
    end, cursor, volumes = start + duration, start, set()
    for left, right, volume in baseline:
        if right <= start or left >= end:
            continue
        if max(left, start) != cursor:
            raise ValueError('dialogue baseline has a gap or overlap')
        cursor = min(right, end)
        volumes.add(volume)
    if not duration or cursor != end or len(volumes) != 1:
        raise ValueError('dialogue segment must preserve one base silence/voice interval')
    return volumes.pop()


def mix_action_audio(draft, spec, context):
    source = spec.get('authorization_source')
    if not source or source != context.plan.get('audio_authorization_source'):
        raise ValueError('audio authorization mismatch')
    state = json.loads(Path(context.plan['project_state']).read_text('utf-8-sig'))
    if not any(a.get('option') == 'action_sfx' and a.get('enabled') is True and a.get('source') == source
               for a in state.get('current_authorizations', [])):
        raise ValueError('missing current action_sfx authorization')
    if any(t.get('type') == 'audio' and t.get('segments') and not is_preset_audio(t) for t in draft['tracks']):
        raise ValueError('audio mix requires no existing added audio; avoid double mixing')
    tracks = [t for t in draft['tracks'] if t.get('name') == 'JY_ROUGH_CUT_VIDEO' and t.get('type') == 'video']
    if len(tracks) != 1 or not tracks[0].get('segments'):
        raise ValueError('audio mix requires unique rough-cut video track')
    baseline = _dialogue_baseline(context.plan, plan_path=getattr(context, 'plan_path', None))
    for segment in tracks[0]['segments']:
        volume = segment.get('volume')
        if (type(volume) not in (int, float) or volume not in (0, 1)
                or volume != _baseline_volume(segment, baseline)
                or any(k.get('property_type') == 'KFTypeVolume' for k in segment.get('common_keyframes', []))):
            raise ValueError('existing dialogue gain/automation conflicts with explicit mix')
    # Quiet source speech may need positive gain. This remains a static native
    # volume multiplier; actual mixed peak/headroom is checked by the audition
    # renderer, rather than treating every volume above unity as clipping.
    dialogue_gain = _gain(spec['dialogue_gain_db'], maximum_db=12)
    events = spec.get('events')
    if (not isinstance(events, list) or any(not isinstance(e, dict) for e in events)
            or len({e.get('id') for e in events}) != len(events)
            or (not events and not any(is_preset_audio(t) and t.get('segments') for t in draft['tracks']))):
        raise ValueError('audio mix requires unique sound events')
    if any(not isinstance(e.get('id'), str) or not e['id'].strip() for e in events):
        raise ValueError('sound event id required')
    duration = max(s['target_timerange']['start'] + s['target_timerange']['duration'] for s in tracks[0]['segments'])
    native = _native()
    prepared = []
    native_audio = {m['id']: m for m in draft['materials'].get('audios', [])}
    for event in events:
        event = _resolve_event(event, context.plan, draft, plan_path=getattr(context, 'plan_path', None))
        _check_binding(event, context.plan)
        path = Path(event['source_path'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest().lower() != str(event.get('sha256')).lower():
            raise ValueError('sound source missing or hash mismatch')
        if event['start_us'] + event['duration_us'] > duration:
            raise ValueError('sound extends beyond rough-cut duration')
        for track in draft['tracks']:
            if not is_preset_audio(track):
                continue
            for original in track.get('segments', []):
                if (original.get('target_timerange', {}).get('start') != event['start_us']
                        or original.get('source_timerange', {}).get('start') != event['source_start_us']):
                    continue
                original_path = Path(native_audio.get(original.get('material_id'), {}).get('path', ''))
                if (original_path.is_file()
                        and hashlib.sha256(original_path.read_bytes()).hexdigest().lower() == str(event['sha256']).lower()):
                    raise ValueError('action sound duplicates an original preset cue')
        material = native.AudioMaterial(str(path.resolve()))
        segment = native.AudioSegment(material, native.trange(event['start_us'], event['duration_us']),
                                      source_timerange=native.trange(event['source_start_us'], event['duration_us']),
                                      volume=_gain(event['gain_db']))
        segment.add_fade(event['fade_in_us'], event['fade_out_us'])
        prepared.append((event, material, segment))
    result = copy.deepcopy(draft)
    apply_preset_gain(result, spec)
    for segment in next(t for t in result['tracks'] if t.get('name') == 'JY_ROUGH_CUT_VIDEO')['segments']:
        segment['volume'] *= dialogue_gain
    for event, material, segment in prepared:
        result['materials'].setdefault('audios', []).append(material.export_json())
        result['materials'].setdefault('audio_fades', []).append(segment.fade.export_json())
        result['materials'].setdefault('speeds', []).append(segment.speed.export_json())
        result['tracks'].append({'id': uuid.uuid4().hex, 'name': 'JY_SFX_' + event['id'], 'type': 'audio',
                                 'attribute': 0, 'flag': 0, 'segments': [segment.export_json()]})
    return result, {'events_written': len(events), 'dialogue_gain_db': spec['dialogue_gain_db'],
                    'preset_gain_db': spec.get('preset_gain_db', 0),
                    'preset_track_gains_db': copy.deepcopy(spec.get('preset_track_gains_db', {})),
                    'original_preset_audio_preserved': sum(len(t.get('segments', [])) for t in draft['tracks'] if is_preset_audio(t)),
                    'resolved_actions': [{'id': event['id'], 'action_anchor': event.get('action_anchor'),
                                          'action_time_us': event['action_time_us'], 'start_us': event['start_us']}
                                         for event, _, _ in prepared],
                    'native_listening_verified': False}


def validate_action_audio(plan, draft, *, plan_path=None):
    """Also used after the native Writer round trip; never infer sound from a label."""
    specs = [o for o in plan.get('operations', []) if o.get('kind') == 'mix_action_audio']
    sfx = [t for t in draft.get('tracks', []) if str(t.get('name', '')).startswith('JY_SFX_')]
    if not specs:
        return {'ok': not sfx, 'errors': ['undeclared action audio'] if sfx else [], 'declared': False}
    errors = []
    if len(specs) != 1:
        return {'ok': False, 'errors': ['exactly one action mix is supported'], 'declared': True}
    spec = specs[0]
    if any(t.get('type') == 'audio' and t.get('segments') and not is_preset_audio(t)
           and not str(t.get('name', '')).startswith('JY_SFX_') for t in draft['tracks']):
        errors.append('unplanned additional audio in action mix')
    resolved_events = []
    for event in spec['events']:
        try:
            event = _resolve_event(event, plan, draft, plan_path=plan_path)
            _check_binding(event, plan)
            resolved_events.append(event)
        except (ValueError, KeyError, TypeError, OSError, LookupError) as exc:
            errors.append('invalid sound binding: ' + str(exc))
    expected = {'JY_SFX_' + e['id']: e for e in resolved_events}
    if sorted(t.get('name') for t in sfx) != sorted(expected):
        errors.append('action audio track set mismatch')
    mats = {m['id']: m for m in draft['materials'].get('audios', [])}
    fades = {m['id']: m for m in draft['materials'].get('audio_fades', [])}
    for track in sfx:
        event = expected.get(track['name'])
        if not event or len(track.get('segments', [])) != 1:
            errors.append('action audio requires exact one-segment tracks')
            continue
        seg = track['segments'][0]
        mat = mats.get(seg.get('material_id'), {})
        fade = [fades[r] for r in seg.get('extra_material_refs', []) if r in fades]
        # SDK Track exports mute as attribute=1. Keep the generated audio
        # segment enabled and bound to the SDK's local-audio material type.
        if (track.get('type') != 'audio' or track.get('attribute') != 0
                or seg.get('visible') is not True or seg.get('track_attribute') != 0
                or mat.get('type') != 'extract_music'):
            errors.append('action audio muted, disabled or invalid audio type: ' + track['name'])
        valid = (seg.get('target_timerange') == {'start': event['start_us'], 'duration': event['duration_us']}
                 and seg.get('source_timerange') == {'start': event['source_start_us'], 'duration': event['duration_us']}
                 and seg.get('speed') == 1.0 and not seg.get('reverse')
                 and not any(k.get('property_type') == 'KFTypeVolume' for k in seg.get('common_keyframes', []))
                 and math.isclose(seg.get('volume', -1), _gain(event['gain_db']), rel_tol=1e-7)
                 and str(mat.get('path', '')).replace('\\', '/').casefold() == str(event['source_path']).replace('\\', '/').casefold()
                 and len(fade) == 1 and fade[0].get('fade_in_duration') == event['fade_in_us']
                 and fade[0].get('fade_out_duration') == event['fade_out_us'])
        if not valid:
            errors.append('action audio differs: ' + track['name'])
        path = Path(str(mat.get('path', '')))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest().lower() != str(event.get('sha256')).lower():
            errors.append('action audio source changed: ' + track['name'])
    ar = [t for t in draft['tracks'] if t.get('name') == 'JY_ROUGH_CUT_VIDEO']
    try:
        baseline = _dialogue_baseline(plan, plan_path=plan_path)
        gain = _gain(spec['dialogue_gain_db'], maximum_db=12)
        dialogue_ok = len(ar) == 1 and bool(ar[0].get('segments')) and all(
            not any(k.get('property_type') == 'KFTypeVolume' for k in s.get('common_keyframes', []))
            and math.isclose(s.get('volume', -1), _baseline_volume(s, baseline) * gain, rel_tol=1e-7)
            for s in ar[0]['segments'])
    except (ValueError, KeyError, TypeError, OSError):
        dialogue_ok = False
    if not dialogue_ok:
        errors.append('dialogue gain differs from action mix')
    return {'ok': not errors, 'errors': errors, 'declared': True, 'sound_event_count': len(expected),
            'resolved_actions': [{'id': e['id'], 'action_anchor': e.get('action_anchor'),
                                  'action_time_us': e['action_time_us'], 'start_us': e['start_us']} for e in resolved_events],
            'native_listening_verified': False}


def needs_audio_measurement(draft):
    """Measure added sound against the actual audible dialogue bus."""
    audible = [t for t in draft.get('tracks', [])
               if t.get('type') in ('video', 'audio') and t.get('attribute', 0) != 1
               and any(s.get('volume', 0) > 0 for s in t.get('segments', []))]
    return (any(t.get('name') == 'JY_ROUGH_CUT_VIDEO' for t in audible)
            and any(t.get('name') != 'JY_ROUGH_CUT_VIDEO' for t in audible))


def audio_measurement_signature(plan, draft, *, plan_path):
    """Bind reuse to audible inputs, excluding visual styling and generated IDs."""
    materials = {m['id']: m for group in draft.get('materials', {}).values()
                 if isinstance(group, list) for m in group if isinstance(m, dict) and 'id' in m}
    files, rows = {}, []
    for track in draft.get('tracks', []):
        if track.get('type') not in ('video', 'audio') or track.get('attribute', 0) == 1:
            continue
        for segment in track.get('segments', []):
            if segment.get('volume', 0) == 0:
                continue
            path = Path(materials[segment['material_id']]['path'])
            if not path.is_absolute():
                path = Path(plan_path).resolve().parent / path
            path = path.resolve()
            name = str(path)
            if name not in files:
                stat = path.stat()
                files[name] = [name, stat.st_size, stat.st_mtime_ns]
            audio_refs = [materials[ref] for ref in segment.get('extra_material_refs', [])
                          if ref in materials and any(ref == item.get('id') for kind in ('audio_fades', 'speeds', 'audio_effects')
                                                       for item in draft.get('materials', {}).get(kind, []))]
            rows.append({
                'bus': 'voice' if track.get('name') == 'JY_ROUGH_CUT_VIDEO' else 'background',
                'file': files[name],
                'segment': {key: segment.get(key) for key in ('source_timerange', 'target_timerange', 'volume', 'speed', 'reverse', 'keyframe_refs')},
                'volume_keyframes': [k for k in segment.get('common_keyframes', []) if k.get('property_type') == 'KFTypeVolume'],
                'audio_refs': [{k: v for k, v in ref.items() if k != 'id'} for ref in audio_refs],
            })
    payload = {'window_us': 10000, 'checks': plan.get('audio_checks', {}),
               'duration_us': max((s['target_timerange']['start'] + s['target_timerange']['duration']
                                   for t in draft['tracks'] for s in t.get('segments', [])), default=0),
               'sources': sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def measure_candidate_audio(plan, draft, *, plan_path):
    """Reuse the existing mix meter, keeping only its report after measurement."""
    if not needs_audio_measurement(draft):
        return {'ok': True, 'status': 'not_required', 'errors': []}
    ffmpeg = os.environ.get('REVIEW_FFMPEG') or shutil.which('ffmpeg')
    try:
        signature = audio_measurement_signature(plan, draft, plan_path=plan_path)
        # Reuse only the current task's existing report; never scan old trials.
        state_value = plan.get('project_state')
        if state_value:
            state_path = Path(state_value)
            if not state_path.is_absolute():
                state_path = Path(plan_path).resolve().parent / state_path
            state = json.loads(state_path.read_text('utf-8-sig'))
            prior_path = state.get('artifacts', {}).get('structure_report')
            if prior_path:
                prior_path = Path(prior_path)
                if not prior_path.is_absolute():
                    prior_path = state_path.parent / prior_path
                try:
                    prior = json.loads(prior_path.read_text('utf-8-sig'))
                    cached = prior.get('final_validation', {}).get('audio_measurement')
                    if (prior.get('ok') is True and isinstance(cached, dict)
                            and cached.get('input_signature') == signature
                            and not validate_audio_measurement(draft, cached)):
                        return cached
                except (OSError, ValueError, AttributeError):
                    pass  # Missing/old report is a cache miss, not a media error.
        if not ffmpeg:
            raise ValueError('FFmpeg unavailable; set REVIEW_FFMPEG or PATH')
        with tempfile.TemporaryDirectory(prefix='.audio-check-', dir=Path(plan_path).resolve().parent) as temp:
            report = render_candidate_audio(plan, draft, ffmpeg, Path(temp) / 'mix.wav', plan_path=plan_path)
        errors = []
        if not report['headroom_at_least_1db']:
            errors.append('混音峰值不足 1 dB 余量；调整实际音量后重建')
        balance = report['voice_balance']
        if balance['intervals']:
            errors.append('音效与同期人声音量存在风险；按 voice_balance.intervals 调整后重建')
        if balance['status'] != 'measured_no_risk_at_threshold' and not balance['intervals']:
            errors.append('无法测得有效同期人声，不能认定音量平衡通过')
        return {**report, 'ok': not errors, 'status': 'measured', 'errors': errors, 'input_signature': signature}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return {'ok': False, 'status': 'measurement_failed', 'errors': [f'音量测量失败: {exc}']}


def validate_audio_measurement(draft, report, *, plan=None, plan_path=None):
    if not needs_audio_measurement(draft):
        return []
    if (not isinstance(report, dict) or report.get('ok') is not True
            or report.get('status') != 'measured'
            or report.get('headroom_at_least_1db') is not True
            or (report.get('voice_balance') or {}).get('window_us') != 10000
            or (report.get('voice_balance') or {}).get('status') != 'measured_no_risk_at_threshold'
            or (report.get('voice_balance') or {}).get('intervals')):
        return ['候选缺少通过的实际音量测量；需重新装配，不能仅用结构检查放行']
    if plan is not None:
        try:
            if report.get('input_signature') != audio_measurement_signature(plan, draft, plan_path=plan_path):
                return ['音量测量依赖已变化，需重新装配测量']
        except (OSError, ValueError, KeyError) as exc:
            return [f'无法核对音量测量依赖: {exc}']
    return []


def tempo_filters(speed):
    """Keep each atempo stage in its pitch-preserving [0.5, 2] range."""
    values = []
    while speed > 2:
        values.append(2.0)
        speed /= 2
    while speed < .5:
        values.append(.5)
        speed *= 2
    if speed != 1:
        values.append(speed)
    return ''.join(f',atempo={value:.17g}' for value in values)


def render_candidate_audio(plan, draft, ffmpeg, output, *, plan_path=None):
    check = validate_action_audio(plan, draft, plan_path=plan_path)
    if not check['ok']:
        raise ValueError(check['errors'])
    original = validate_preset_audio(plan, draft, plan_path=plan_path)
    if not original['ok']:
        raise ValueError(original['errors'])
    output = Path(output)
    if output.exists():
        raise ValueError('audition output already exists')
    materials = {m['id']: m for kind in ('videos', 'audios') for m in draft['materials'].get(kind, [])}
    fades = {m['id']: m for m in draft['materials'].get('audio_fades', [])}
    speed_mats = {m['id']: m for m in draft['materials'].get('speeds', [])}
    audio_effects = {m['id'] for m in draft['materials'].get('audio_effects', [])}
    command = [ffmpeg, '-v', 'error', '-nostdin']
    filters = []
    constant_speeds = []
    count = 0
    voice, background = [], []
    for track in draft['tracks']:
        if track['type'] not in ('video', 'audio'):
            continue
        # pyJianYingDraft Track.export_json encodes mute as attribute=1.
        if track.get('attribute', 0) == 1:
            continue
        for seg in track.get('segments', []):
            if seg.get('volume', 0) == 0:
                continue
            speed = seg.get('speed')
            if (type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0
                    or seg.get('reverse') or any(k.get('property_type') == 'KFTypeVolume' for k in seg.get('common_keyframes', []))):
                raise ValueError('audition requires finite positive constant speed and static gain')
            refs = seg.get('extra_material_refs', [])
            if set(refs) & audio_effects or any(speed_mats[r].get('curve_speed') or speed_mats[r].get('mode', 0) != 0
                                              for r in refs if r in speed_mats):
                raise ValueError('audio effects/curve speed not supported in audition')
            source, target = seg['source_timerange'], seg['target_timerange']
            if source['duration'] <= 0 or target['duration'] <= 0 or abs(source['duration'] / speed - target['duration']) > 2:
                raise ValueError('source duration / constant speed and target duration differ')
            duration = target['duration'] / 1e6
            source_duration = source['duration'] / 1e6
            command += ['-ss', str(source['start'] / 1e6), '-t', str(source_duration), '-i', materials[seg['material_id']]['path']]
            # Read the authored source slice, then change tempo. Never extend a
            # cue to hide a duration mismatch; fades use the target-time axis.
            chain = (f'[{count}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,asetpts=PTS-STARTPTS'
                     + tempo_filters(speed) + f',atrim=duration={duration},volume={seg["volume"]}')
            if speed != 1:
                constant_speeds.append({'track': track.get('name'), 'segment_id': seg.get('id'), 'speed': speed,
                                        'source_duration_us': source['duration'], 'target_duration_us': target['duration']})
            sf = [fades[r] for r in refs if r in fades]
            if len(sf) > 1:
                raise ValueError('multiple fades on one audio source')
            if sf:
                fi, fo = sf[0]['fade_in_duration']/1e6, sf[0]['fade_out_duration']/1e6
                if fi: chain += f',afade=t=in:st=0:d={fi}'
                if fo: chain += f',afade=t=out:st={duration-fo}:d={fo}'
            delay = round(target['start'] * 48000 / 1e6)
            chain += f',adelay={delay}S:all=1[a{count}]'
            filters.append(chain)
            (voice if track.get('name') == 'JY_ROUGH_CUT_VIDEO' else background).append(count)
            count += 1
    if not count:
        raise ValueError('no audible segments')
    end = max(s['target_timerange']['start']+s['target_timerange']['duration'] for t in draft['tracks'] for s in t['segments']) / 1e6
    output.parent.mkdir(parents=True, exist_ok=True)
    balance = {'status': 'unavailable_without_dialogue_and_background', 'intervals': []}
    with tempfile.TemporaryDirectory(prefix='.audio-meter-', dir=output.parent) as temp:
        extra_outputs = []
        if voice and background:
            for name, indices in [('voice', voice), ('background', background)]:
                filters.append(''.join(f'[a{i}]' for i in indices) +
                    f'amix=inputs={len(indices)}:normalize=0:dropout_transition=0,apad,atrim=duration={end},asplit=2[{name}mix][{name}meter]')
                extra_outputs += ['-map', f'[{name}meter]', '-c:a', 'pcm_f32le', str(Path(temp)/(name+'.wav'))]
            mix_inputs = '[voicemix][backgroundmix]'
            mix_count = 2
        else:
            mix_inputs, mix_count = ''.join(f'[a{i}]' for i in range(count)), count
        filters.append(mix_inputs + f'amix=inputs={mix_count}:normalize=0:dropout_transition=0,apad,atrim=duration={end}[mix]')
        # Meter the same native gains/fades/slices used in the mix. No hidden
        # limiter or ducking is applied only to the offline preview.
        subprocess.run(command + ['-filter_complex', ';'.join(filters), '-map', '[mix]', '-c:a', 'pcm_f32le', str(output)] + extra_outputs,
                       check=True, capture_output=True)
        if voice and background:
            balance = measure_balance(ffmpeg, Path(temp)/'voice.wav', Path(temp)/'background.wav', plan.get('audio_checks', {}))
    import numpy as np
    raw = subprocess.run([ffmpeg, '-v','error','-i',str(output),'-ar','192000','-f','f32le','-'], check=True, capture_output=True).stdout
    samples = np.frombuffer(raw, dtype='<f4')
    peak = float(np.max(np.abs(samples)))
    return {'ok': peak <= 10**(-1/20) and not balance['intervals'],
            'audio_sources':count, 'duration_seconds':end, 'oversampled_peak_dbfs':20*math.log10(max(peak,1e-12)),
            'voice_balance': balance,
            'constant_speed_sources': constant_speeds,
            'original_preset_audio': original,
            'headroom_at_least_1db':peak <= 10**(-1/20), 'native_listening_verified':False,
            'scope':'offline mix using native source ranges, constant atempo speed, gain and target-time fades; not native playback evidence'}


def measure_balance(ffmpeg, voice, background, config):
    """10ms RMS comparison also exposes short cues hidden by 100ms averaging."""
    import numpy as np
    margin, floor = config.get('background_margin_db', 6.0), config.get('dialogue_floor_dbfs', -45.0)
    if (type(margin) not in (int, float) or not math.isfinite(margin) or not 0 <= margin <= 30
            or type(floor) not in (int, float) or not math.isfinite(floor) or not -80 <= floor < 0):
        raise ValueError('invalid audio measurement thresholds')
    def levels(path):
        raw = subprocess.run([ffmpeg, '-v', 'error', '-i', str(path), '-ar', '48000', '-ac', '2', '-f', 'f32le', '-'],
                             check=True, capture_output=True).stdout
        samples = np.frombuffer(raw, dtype='<f4')
        size = 480 * 2
        return [20 * math.log10(max(float(np.sqrt(np.mean(samples[i:i+size].astype('float64') ** 2))), 1e-12))
                for i in range(0, len(samples), size)]
    speech, other = levels(voice), levels(background)
    intervals, active = [], 0
    for i, (v, b) in enumerate(zip(speech, other)):
        if v < floor:
            continue
        active += 1
        if b - v <= -margin:
            continue
        if intervals and intervals[-1]['end_us'] == i * 10000:
            intervals[-1]['end_us'] += 10000
            intervals[-1]['worst_background_minus_dialogue_db'] = max(intervals[-1]['worst_background_minus_dialogue_db'], b-v)
        else:
            intervals.append({'start_us': i*10000, 'end_us': (i+1)*10000,
                              'worst_background_minus_dialogue_db': b-v})
    return {'status': 'risk_found' if intervals else ('measured_no_risk_at_threshold' if active else 'no_active_dialogue'),
            'intervals': intervals, 'background_margin_db': margin, 'dialogue_floor_dbfs': floor,
            'active_windows': active, 'window_us': 10000,
            'basis': 'actual native timeline gains; dialogue-track amplitude estimates speech activity',
            'intelligibility_verified': False}
