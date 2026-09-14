"""Explicit removal of decorative text entrances/glow, preserving other tracks."""
import copy
import json
import uuid
from pathlib import Path


def simplify_preset_motion(draft, spec, context):
    authorization = spec.get('authorization_source')
    state = json.loads(Path(context.plan['project_state']).read_text('utf-8-sig'))
    if not authorization or not spec.get('design_reason') or not any(
        a.get('option') == 'visual_packaging_revision' and a.get('enabled') is True
        and a.get('source') == authorization for a in state.get('current_authorizations', [])
    ):
        raise ValueError('preset simplification needs current visual authorization and design reason')
    if spec.get('mode') != 'plain_text_no_entrance_or_glow':
        raise ValueError('unsupported preset simplification mode')
    names = spec.get('track_names')
    if not isinstance(names, list) or not names or len(set(names)) != len(names):
        raise ValueError('unique exact text tracks required')
    result = copy.deepcopy(draft)
    animations = {a['id']: a for a in result['materials'].get('material_animations', [])}
    effects = {a['id']: a for a in result['materials'].get('effects', [])}
    rows = []
    for name in names:
        tracks = [t for t in result['tracks'] if t.get('name') == name and t.get('type') == 'text']
        if len(tracks) != 1 or not tracks[0].get('segments'):
            raise ValueError('one populated text track required')
        for segment in tracks[0]['segments']:
            refs = []
            removed = []
            for ref in segment.get('extra_material_refs', []):
                if ref in animations:
                    material = animations[ref]
                    sequence = material.get('animations')
                    if not isinstance(sequence, list) or any(a.get('type') != 'in' for a in sequence):
                        raise ValueError('loop/out/unknown animation requires separate design')
                    # Clone so a shared resource cannot alter an unselected segment.
                    clean = copy.deepcopy(material)
                    clean['id'] = uuid.uuid4().hex
                    clean['animations'] = []
                    result['materials']['material_animations'].append(clean)
                    refs.append(clean['id'])
                    removed.extend(a.get('name', '') for a in sequence)
                elif ref in effects and effects[ref].get('type') == 'bloom' and effects[ref].get('panel_id') == 'text_glow':
                    effect = effects[ref]
                    if any(effect.get(k) for k in ('animations', 'common_keyframes', 'time_range', 'timerange')):
                        raise ValueError('time-varying glow requires separate design')
                    removed.append('static_text_glow')
                else:
                    refs.append(ref)
            segment['extra_material_refs'] = refs
            rows.append({'track_name': name, 'segment_id': segment['id'], 'removed_decorations': removed})
    return result, {'segments': rows, 'text_timing_geometry_audio_preserved': True, 'native_visual_verified': False}
