"""Keep authored preset sound clips with their visual group and verify readback."""
from __future__ import annotations

import copy
import json
import math
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace


def is_preset_audio(track):
    name = str(track.get('name', ''))
    return (track.get('type') == 'audio' and name.startswith('JY_PRESET_')
            and '__NODE__' in name and '_AUX_AUDIO_' in name)


def apply_preset_gain(draft, spec):
    """Validate all static gains, then attenuate the authored preset cues."""
    from .action_audio import _gain
    gain = _gain(spec.get('preset_gain_db', 0))
    overrides = spec.get('preset_track_gains_db', {})
    if not isinstance(overrides, dict):
        raise ValueError('preset_track_gains_db must be an object of exact audio track names')
    tracks = {}
    for track in draft.get('tracks', []):
        if is_preset_audio(track):
            name = track['name']
            if name in tracks:
                raise ValueError('duplicate native preset audio track: ' + name)
            tracks[name] = track
    track_gains = {}
    for name, db in overrides.items():
        if not isinstance(name, str) or name not in tracks or not tracks[name].get('segments'):
            raise ValueError('preset_track_gains_db requires an existing original preset audio track: ' + str(name))
        track_gains[name] = _gain(db)
    updates = []
    for name, track in tracks.items():
        for segment in track.get('segments', []):
            if (segment.get('keyframe_refs')
                    or any(k.get('property_type') == 'KFTypeVolume' for k in segment.get('common_keyframes', []))):
                raise ValueError('preset bus attenuation requires static cue gain')
            volume = segment.get('volume', 1.0)
            if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not math.isfinite(volume) or volume < 0:
                raise ValueError('original preset audio volume must be finite and nonnegative')
            updates.append((segment, volume * track_gains.get(name, gain)))
    for segment, volume in updates:
        segment['volume'] = volume


def _anchors(draft, prefix):
    animations = {m['id']: m for m in draft.get('materials', {}).get('material_animations', [])}
    result = {}
    for track in draft.get('tracks', []):
        if track.get('type') != 'text' or not str(track.get('name', '')).startswith(prefix):
            continue
        for index, segment in enumerate(track.get('segments', [])):
            start = segment['target_timerange']['start']
            end = start + segment['target_timerange']['duration']
            key = (track['name'], index)
            result[(*key, 'start')] = start
            result[(*key, 'end')] = end
            sequence = [animation for ref in segment.get('extra_material_refs', [])
                        for animation in animations.get(ref, {}).get('animations', [])]
            for ai, animation in enumerate(sequence):
                if type(animation.get('duration')) is int and type(animation.get('start', 0)) is int:
                    onset = start + animation.get('start', 0)
                    result[(*key, f'animation_{ai}_start')] = onset
                    result[(*key, f'animation_{ai}_end')] = onset + animation['duration']
    return result


def sync_preset_audio(before, after, *, prefix, start_us, end_us, bindings=()):
    """Translate each authored cue, preserving its source slice, speed and gain.

    Exact original coincidences identify a cue automatically. An explicit
    binding resolves leads, tails or simultaneous words that are split apart.
    Ambiguous timing is rejected instead of guessing a different pairing.
    """
    old, new = _anchors(before, prefix), _anchors(after, prefix)
    deltas = {key: new[key] - value for key, value in old.items() if key in new}
    if not isinstance(bindings, (list, tuple)):
        raise ValueError('audio_bindings must be a list')
    explicit = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError('audio binding must be an object')
        key = (binding.get('audio_track'), binding.get('segment_index'))
        anchor = (binding.get('text_track'), binding.get('text_segment_index', 0), binding.get('anchor', 'start'))
        if key in explicit or type(key[1]) is not int or key[1] < 0 or anchor not in deltas:
            raise ValueError('audio binding needs a unique real audio clip and text anchor')
        explicit[key] = anchor
    used, reports = set(), []
    for track in after.get('tracks', []):
        if not is_preset_audio(track) or not track['name'].startswith(prefix):
            continue
        for index, segment in enumerate(track.get('segments', [])):
            timerange = segment['target_timerange']
            onset = timerange['start']
            key = (track['name'], index)
            if key in explicit:
                delta = deltas[explicit[key]]
                used.add(key)
            else:
                candidates = {deltas[k] for k, time in old.items() if time == onset and k in deltas}
                if not candidates:
                    candidates = set(deltas.values())
                if not candidates:
                    delta = 0
                elif len(candidates) == 1:
                    delta = candidates.pop()
                else:
                    raise ValueError(f'{track["name"]}[{index}]: ambiguous authored cue; provide audio_bindings')
            translated = onset + delta
            if not start_us <= translated < translated + timerange['duration'] <= end_us:
                raise ValueError('retimed original preset audio exceeds node window')
            timerange['start'] = translated
            reports.append({'track_name': track['name'], 'segment_index': index,
                            'before_start_us': onset, 'after_start_us': translated})
    if set(explicit) != used:
        raise ValueError('audio binding refers to a missing original sound clip')
    return reports


def _without_local_ids(value):
    if isinstance(value, dict):
        return {key: _without_local_ids(item) for key, item in value.items()
                if key not in {'id', 'local_material_id', 'origin_material_id', 'material_id'}}
    if isinstance(value, list):
        return [_without_local_ids(item) for item in value]
    return value


@lru_cache(maxsize=128)
def _audio_duration(path, modified_ns, size):
    # Cache only for the lifetime of this process and the current file version.
    from .action_audio import _native
    return _native().AudioMaterial(path).duration


def audio_rows(draft):
    """ID-independent native facts; track names keep separate uses separate."""
    materials = {m['id']: m for group in draft.get('materials', {}).values() if isinstance(group, list)
                 for m in group if isinstance(m, dict) and m.get('id')}
    rows = {}
    for track in draft.get('tracks', []):
        if not is_preset_audio(track):
            continue
        name = track['name']
        if name in rows:
            raise ValueError('duplicate native preset audio track: ' + name)
        clips = []
        for segment in track.get('segments', []):
            material = materials.get(segment.get('material_id'))
            if material is None or not Path(material.get('path', '')).is_file():
                raise ValueError('original preset audio source missing: ' + name)
            path = Path(material['path'])
            stat = path.stat()
            duration = _audio_duration(str(path.resolve()), stat.st_mtime_ns, stat.st_size)
            source = segment.get('source_timerange') or {'start': 0, 'duration': segment['target_timerange']['duration']}
            declared_duration = material.get('duration', duration)
            if (duration <= 0 or not 0 <= source['start'] < duration or source['duration'] <= 0
                    or source['start'] + source['duration'] > max(duration, declared_duration) + 50_000):
                raise ValueError('original preset audio source slice exceeds actual media: ' + name)
            facts = _without_local_ids(segment)
            # Some authored cloud SFX reserve a longer native clip than their
            # decoded sound. Preserve that silent tail instead of shortening
            # the author's timeline or claiming unavailable samples were heard.
            facts['source_media_duration_us'] = duration
            facts['source_tail_padding_us'] = max(0, source['start'] + source['duration'] - duration)
            # Native materialization may renumber visual render bookkeeping.
            for key in ('extra_material_refs', 'render_index', 'track_render_index'):
                facts.pop(key, None)
            facts['material'] = _without_local_ids(material)
            # The same local source can be spelled with plan-relative '..'
            # components; compare the resolved file, not its path spelling.
            facts['material']['path'] = str(path.resolve())
            if material.get('music_id') == material.get('id'):
                facts['material'].pop('music_id', None)
            dependencies = []
            for ref in segment.get('extra_material_refs', []):
                if ref not in materials:
                    raise ValueError('original preset audio has a dangling dependency: ' + name)
                dependencies.append(_without_local_ids(materials[ref]))
            facts['dependencies'] = dependencies
            clips.append(facts)
        rows[name] = {'attribute': track.get('attribute', 0), 'flag': track.get('flag', 0), 'segments': clips}
    return rows


def rebuild_preset_groups(plan, draft, *, plan_path=None):
    """Re-read selected native sources and replay their imports/timing in memory.

    Audio and inherited video-effect checks share this source reconstruction.
    There is no rendering, download, live draft write or persistent cache here.
    """
    operations = [op for op in plan.get('operations', [])
                  if op.get('kind') in {'add_preset_group', 'replace_preset_group'}
                  and (op.get('template_id') or op.get('new_template_id')) and op.get('node_id')]
    # Older plans can describe already-imported text without a source registry.
    # This compatibility case may not contain or claim inherited audio.
    if (plan.get('project_format', {}).get('visual_planning_version') != 2
            and not plan.get('preset_registry') and not any(op.get('registry') for op in operations)):
        operations = []
    if not operations:
        return None
    from .shared_operations import _append_native_group
    from .preset_timing import retime_preset_text_tracks
    expected = {'duration': draft.get('duration', 0), 'materials': {}, 'tracks': [],
                'canvas_config': copy.deepcopy(draft.get('canvas_config', plan.get('target', {})))}
    if not expected['duration']:
        expected['duration'] = max((selection.get('end_us', 0) for selection in plan.get('presets', [])), default=0)
    context = SimpleNamespace(plan=plan, plan_path=Path(plan_path or 'candidate-plan.json').resolve())
    final_operations = {}
    for index, op in enumerate(plan.get('operations', [])):
        if op not in operations:
            continue
        node = op.get('node_id')
        if op.get('kind') == 'replace_preset_group':
            final_operations.pop((op.get('old_template_id'), node), None)
        key = (op.get('new_template_id') or op.get('template_id'), node)
        final_operations[key] = (index, op)
    for _, op in final_operations.values():
        spec = dict(op)
        if op.get('kind') == 'replace_preset_group':
            spec['template_id'] = op.get('new_template_id') or op.get('template_id')
        expected, _ = _append_native_group(expected, spec, context)
    for index, op in enumerate(plan.get('operations', [])):
        imported = final_operations.get((op.get('template_id'), op.get('node_id')))
        if op.get('kind') == 'retime_preset_text_tracks' and imported and index > imported[0]:
            expected, _ = retime_preset_text_tracks(expected, op, context)
    return expected


def validate_preset_audio(plan, draft, *, plan_path=None):
    """Re-read selected sources, so a missing or doubled cue cannot pass Writer."""
    try:
        expected = rebuild_preset_groups(plan, draft, plan_path=plan_path)
        if expected is None:
            unexpected = any(is_preset_audio(track) for track in draft.get('tracks', []))
            return {'ok': not unexpected, 'errors': ['undeclared original preset audio'] if unexpected else [],
                    'declared': False, 'native_listening_verified': False}
        mixes = [op for op in plan.get('operations', []) if op.get('kind') == 'mix_action_audio']
        if len(mixes) > 1:
            raise ValueError('exactly one action mix is supported')
        for mix in mixes:
            apply_preset_gain(expected, mix)
        wanted, actual = audio_rows(expected), audio_rows(draft)
        errors = []
        if wanted.keys() != actual.keys():
            errors.append('original preset audio track set differs from selected sources')
        for name in wanted.keys() & actual.keys():
            if wanted[name] != actual[name]:
                errors.append('original preset audio source, clip, timing or gain differs: ' + name)
        return {'ok': not errors, 'errors': errors, 'declared': True,
                'track_count': len(actual), 'segment_count': sum(len(row['segments']) for row in actual.values()),
                'source_tail_padding_us': sum(clip['source_tail_padding_us'] for row in actual.values() for clip in row['segments']),
                'native_listening_verified': False}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {'ok': False, 'errors': ['original preset audio verification failed: ' + str(exc)],
                'declared': True, 'native_listening_verified': False}
