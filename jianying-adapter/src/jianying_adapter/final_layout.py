"""Measure final candidate geometry independently of planner character limits."""
import copy
import json
import math
import unicodedata
from pathlib import Path

from .text_layout import union_bbox, measure_reading_glyphs


def _visible_text(track, segment):
    if track.get('attribute', 0) & 1 or segment.get('track_attribute', 0) & 1 or segment.get('visible') is False:
        return False
    return (segment.get('clip') or {}).get('alpha', 1) != 0 or any(
        group.get('property_type') == 'KFTypeAlpha' for group in segment.get('common_keyframes', []))


def _reading_text(text):
    return ''.join(char for char in unicodedata.normalize('NFKC', text).casefold()
                   if not char.isspace() and not unicodedata.category(char).startswith('P'))


def validate_final_text(draft, plan, *, text_unit_to_px=5.0):
    """Mandatory final reading hierarchy and concurrent content deduplication.

    This runs on the assembled tracks, independently of optional fit operations,
    planner text roles or geometry-overlap exemptions.
    """
    config = plan.get('layout_checks', {})
    minimum = config.get('minimum_readable_text_height_px', 56.0)
    ratio = config.get('minimum_emphasis_to_caption_ratio', 1.3)
    errors, rows, duplicates = [], [], []
    if type(minimum) not in (int, float) or not math.isfinite(minimum) or minimum < 56:
        errors.append('minimum_readable_text_height_px must be at least 56')
        minimum = 56.0
    if type(ratio) not in (int, float) or not math.isfinite(ratio) or ratio < 1.3:
        errors.append('minimum_emphasis_to_caption_ratio must be at least 1.3')
        ratio = 1.3
    caption_name = (plan.get('content_gate') or {}).get('ordinary_caption_track_name') or plan.get('ordinary_caption_track_name') or 'JY_ZH_SUBTITLES'
    materials = {item['id']: item for group in draft.get('materials', {}).values() if isinstance(group, list)
                 for item in group if isinstance(item, dict) and item.get('id')}
    groups = {item.get('track_name'): (preset.get('template_id'), preset.get('node_id'))
              for preset in plan.get('presets', []) for item in preset.get('actual_text_tracks', [])}
    for track in draft.get('tracks', []):
        if track.get('type') != 'text': continue
        for segment in track.get('segments', []):
            if not _visible_text(track, segment): continue
            name = track.get('name', track.get('id', ''))
            try:
                material = materials[segment['material_id']]
                payload = json.loads(material['content'])
                value = segment['target_timerange']
                if type(value['start']) is not int or type(value['duration']) is not int or value['duration'] <= 0:
                    raise ValueError('invalid text timerange')
                normalized = _reading_text(payload['text'])
                if not any(char.isalnum() for char in normalized): continue
                # Collect content before measuring so a missing font cannot hide a duplicate.
                row = {'track_name': name, 'segment_id': segment.get('id'), 'text': payload['text'],
                       'normalized': normalized, 'start_us': value['start'], 'end_us': value['start']+value['duration'],
                       'ordinary': name == caption_name, 'segment': segment, 'material': material}
                rows.append(row)
                reading = measure_reading_glyphs(segment, material, text_unit_to_px=text_unit_to_px, materials=materials)
                row['reading'] = reading
                if reading['minimum_height_px'] + .01 < minimum:
                    errors.append('text below minimum readable glyph height: ' + name)
            except (KeyError, ValueError, TypeError, OSError) as exc:
                errors.append(f'text reading {name}: {exc}')
    baseline = max((r['reading']['maximum_height_px'] for r in rows if r['ordinary'] and 'reading' in r), default=0.0)
    reference = config.get('ordinary_caption_baseline')
    if reference is not None:
        try:
            material = materials[reference['material_id']]
            sample = {'target_timerange': {'start': 0, 'duration': 1_000_000},
                      'clip': {'scale': {'y': reference.get('scale_y', 1.0)}}}
            measured = measure_reading_glyphs(sample, material, text_unit_to_px=text_unit_to_px)
            if not measured['informative'] or measured['minimum_height_px'] < minimum:
                raise ValueError('ordinary caption reference is below readable minimum')
            baseline = max(baseline, measured['maximum_height_px'])
        except (KeyError, ValueError, TypeError, OSError) as exc:
            errors.append('ordinary_caption_baseline: ' + str(exc))
    if any(not r['ordinary'] for r in rows) and baseline <= 0:
        errors.append('special text requires an actual ordinary caption or bound ordinary_caption_baseline material')
    for row in rows:
        if not row['ordinary'] and baseline and 'reading' in row:
            row['reading']['emphasis_to_caption_ratio'] = row['reading']['minimum_height_px'] / baseline
            if row['reading']['minimum_height_px'] + .01 < baseline * ratio:
                errors.append(f'special text hierarchy below {ratio:g}: ' + row['track_name'])
    def same_glyph_stack(a, b):
        # Native shadow/outline copies at the same reading location are one glyph,
        # not two captions. An allowed_overlaps reason alone never exempts content.
        if (a['track_name'] == b['track_name'] or not groups.get(a['track_name'])
                or groups.get(a['track_name']) != groups.get(b['track_name'])
                or a['normalized'] != b['normalized']
                or (a['start_us'], a['end_us']) != (b['start_us'], b['end_us'])):
            return False
        sa, sb = a['segment'], b['segment']
        ca, cb = sa.get('clip', {}), sb.get('clip', {})
        if (sa.get('common_keyframes', []) != sb.get('common_keyframes', [])
                or ca.get('scale', {}) != cb.get('scale', {}) or ca.get('rotation', 0) != cb.get('rotation', 0)):
            return False
        styles = lambda r: [(s.get('range'), s.get('size'), s.get('font'))
                            for s in json.loads(r['material']['content']).get('styles', [])]
        if styles(a) != styles(b): return False
        canvas = plan.get('target', {})
        ta, tb = ca.get('transform', {}), cb.get('transform', {})
        return all(abs(ta.get(axis, 0)-tb.get(axis, 0))*canvas.get(dim, default)/2 <= 8
                   for axis, dim, default in [('x', 'width', 1080), ('y', 'height', 1920)])
    for i, a in enumerate(rows):
        for b in rows[i+1:]:
            left, right = max(a['start_us'], b['start_us']), min(a['end_us'], b['end_us'])
            if left >= right: continue
            short, long = sorted([a['normalized'], b['normalized']], key=len)
            if short not in long or same_glyph_stack(a, b): continue
            duplicate = {'tracks': [a['track_name'], b['track_name']],
                         'segment_ids': [a['segment_id'], b['segment_id']],
                         'text': short, 'start_us': left, 'end_us': right}
            duplicates.append(duplicate)
            errors.append('simultaneous duplicate text: ' + short + ' / ' + ' / '.join(duplicate['tracks']))
    return {'ok': not errors, 'errors': errors, 'minimum_readable_text_height_px': minimum,
            'minimum_emphasis_to_caption_ratio': ratio, 'ordinary_caption_height_px': baseline,
            'duplicates': duplicates, 'texts': [{k: v for k, v in r.items() if k not in {'segment', 'material', 'normalized'}} for r in rows],
            'native_visual_verified': False,
            'limitations': 'Measures local font glyphs and supported scale holds. Native animation rendering and undeclared text burned into media still require visual review.'}


def _rectangle(value, label):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in value)
            or value[0] >= value[2] or value[1] >= value[3]):
        raise ValueError(label + ' requires a finite positive rectangle')
    return list(value)


def _platform_regions(config, canvas, duration):
    """Map measured player UI from a real screenshot into the video canvas."""
    from PIL import Image
    reference = config.get('platform_ui')
    if reference is None:
        return [], {'status': 'pending_platform_ui_reference'}
    if not isinstance(reference, dict) or not reference.get('platform') or not reference.get('basis'):
        raise ValueError('platform_ui requires platform and measured interface basis')
    path = Path(reference.get('image_path', ''))
    if not path.is_file():
        raise ValueError('platform_ui screenshot is missing')
    with Image.open(path) as image:
        image.load()
        width, height = image.size
    rect = _rectangle(reference.get('video_rect_px'), 'platform_ui.video_rect_px')
    if not (0 <= rect[0] < rect[2] <= width and 0 <= rect[1] < rect[3] <= height):
        raise ValueError('platform_ui video rectangle exceeds screenshot')
    sx, sy = canvas[0] / (rect[2] - rect[0]), canvas[1] / (rect[3] - rect[1])
    if not math.isclose(sx, sy, rel_tol=.01):
        raise ValueError('platform_ui video rectangle aspect differs from target; identify the actual video viewport')
    regions = reference.get('regions')
    if not isinstance(regions, list) or not regions:
        raise ValueError('platform_ui requires measured interface regions')
    rows, seen = [], set()
    for region in regions:
        identifier = region.get('id') if isinstance(region, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError('platform_ui regions require unique ids')
        seen.add(identifier)
        box = _rectangle(region.get('bbox_px'), 'platform_ui region')
        if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
            raise ValueError('platform_ui region exceeds screenshot')
        left, top, right, bottom = max(box[0], rect[0]), max(box[1], rect[1]), min(box[2], rect[2]), min(box[3], rect[3])
        if left < right and top < bottom:
            rows.append({'track_name': 'platform:' + identifier,
                         'bbox': [(left - rect[0]) * sx, (top - rect[1]) * sy,
                                  (right - rect[0]) * sx, (bottom - rect[1]) * sy],
                         'start_us': 0, 'end_us': duration})
    return rows, {'status': 'measured_reference', 'platform': reference['platform'],
                  'image_path': str(path), 'video_rect_px': rect, 'regions': rows,
                  'basis': reference['basis']}


def _decoration_padding(material, *, text_unit_to_px):
    """Conservative local bounds for native stroke and shadow parameters.

    Font-unit conversion remains the existing configurable estimate. Native
    background/effect packages need a supplied visible envelope or are reported
    unchecked; their geometry is never guessed from an effect name.
    """
    payload = json.loads(material['content'])
    padding = 0.0
    for style in payload.get('styles', []):
        em = float(style.get('size', material.get('font_size', 14))) * text_unit_to_px
        for stroke in style.get('strokes', []):
            width = stroke.get('width', 0)
            if type(width) not in (int, float) or not math.isfinite(width) or width < 0:
                raise ValueError('invalid native stroke width')
            padding = max(padding, em * width)
        for shadow in style.get('shadows', []):
            if shadow.get('alpha', 1) <= 0:
                continue
            distance, diffuse = shadow.get('distance', 0), shadow.get('diffuse', 0)
            if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in (distance, diffuse)):
                raise ValueError('invalid native shadow extent')
            # Symmetric envelope also covers rotated shadows and soft edges.
            padding = max(padding, distance * text_unit_to_px + 6 * diffuse * em)
    if material.get('border_color') and material.get('border_alpha', 1) > 0:
        padding = max(padding, float(material.get('border_width', 0))
                      * float(material.get('font_size', 14)) * text_unit_to_px)
    if material.get('has_shadow') and material.get('shadow_alpha', 1) > 0:
        padding = max(padding, float(material.get('shadow_distance', 0)) * text_unit_to_px
                      + float(material.get('shadow_smoothing', 0)) * float(material.get('font_size', 14)) * text_unit_to_px)
    if not math.isfinite(padding) or padding < 0:
        raise ValueError('invalid native text decoration extent')
    return padding


def _declared_geometry(region, *, start_us, end_us):
    if not region.get('basis'):
        raise ValueError('visible region requires its observed basis')
    if 'geometry_keyframes' not in region:
        box = _rectangle(region.get('bbox'), 'visible region')
        return [{'time_us': start_us, 'bbox': box}, {'time_us': end_us, 'bbox': box}]
    points = region['geometry_keyframes']
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError('visible region geometry requires at least two points')
    previous = start_us - 1
    result = []
    for point in points:
        time = point.get('time_us')
        if type(time) is not int or not previous < time <= end_us:
            raise ValueError('visible region geometry times must increase inside the interval')
        result.append({'time_us': time, 'bbox': _rectangle(point.get('bbox'), 'visible region point')})
        previous = time
    if result[0]['time_us'] != start_us or result[-1]['time_us'] != end_us:
        raise ValueError('visible region geometry must cover the whole visible interval')
    return result


def _linear_geometry(segment, width, height, *, kind='text', canvas=(1080, 1920)):
    """Piecewise-affine boxes, retaining simultaneous position/scale values."""
    names = {'KFTypePositionX': 'x', 'KFTypePositionY': 'y',
             'KFTypeScaleX': 'sx', 'KFTypeScaleY': 'sy', 'KFTypeRotation': 'rotation'}
    clip = segment.get('clip', {})
    base = dict(x=clip.get('transform', {}).get('x', 0),
                y=clip.get('transform', {}).get('y', 0),
                sx=clip.get('scale', {}).get('x', 1),
                sy=clip.get('scale', {}).get('y', 1))
    rotation = clip.get('rotation', 0)
    if kind == 'B-roll' and rotation != 0:
        raise ValueError('B-roll geometry does not support rotation')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in [*base.values(), rotation]):
        raise ValueError(f'{kind} transform must contain finite numbers')
    if base['sx'] <= 0 or base['sy'] <= 0 or segment.get('keyframe_refs'):
        raise ValueError(f'{kind} geometry requires positive scale and resolved keyframes')
    curves = {}
    duration = segment['target_timerange']['duration']
    times = {0, duration}
    for group in segment.get('common_keyframes', []):
        # Native text colour changes do not alter the glyph box. Keep them in
        # the draft while measuring position and scale independently.
        if group.get('property_type') == ('KFTypeTextColor' if kind == 'text' else 'KFTypeVolume'):
            continue
        axis = names.get(group.get('property_type'))
        if axis is None or axis in curves or not group.get('keyframe_list'):
            raise ValueError(f'unsupported or duplicate {kind} geometry keyframe property')
        if axis == 'rotation' and group.get('material_id'):
            raise ValueError('constant rotation requires resolved inline keyframes')
        frames = []
        for frame in group['keyframe_list']:
            time = frame.get('time_offset')
            values = frame.get('values', [])
            if (frame.get('curveType', 'Line') != 'Line' or frame.get('graphID')
                    or type(time) not in (int, float) or not math.isfinite(time)
                    or len(values) != 1 or type(values[0]) not in (int, float)
                    or not math.isfinite(values[0]) or (axis in ('sx', 'sy') and values[0] <= 0)):
                raise ValueError(f'{kind} geometry requires finite linear position/positive-scale keyframes')
            if kind == 'B-roll' and not 0 <= time <= duration:
                raise ValueError('B-roll geometry keyframe time exceeds its segment')
            if axis == 'rotation' and (not 0 <= time <= duration
                    or values[0] != rotation or frame.get('string_value')
                    or (frames and time <= frames[-1][0])):
                raise ValueError('rotation keyframes must remain constant at clip.rotation with valid ordered times')
            frames.append((time, values[0]))
            if 0 < time < duration:
                times.add(time)
        frames.sort()
        if len({time for time, _ in frames}) != len(frames):
            raise ValueError(f'duplicate {kind} geometry keyframe time')
        curves[axis] = frames

    # VisualSegment.add_keyframe serializes UNIFORM_SCALE as ScaleX while
    # retaining uniform_scale.on; independent X/Y animation disables it.
    if (segment.get('uniform_scale') or {}).get('on') is True and 'sx' in curves:
        if 'sy' in curves:
            raise ValueError('locked uniform scale has conflicting Y keyframes')
        curves['sy'] = curves['sx']

    def value_at(axis, time):
        frames = curves.get(axis)
        if not frames:
            return base[axis]
        if time <= frames[0][0]:
            return frames[0][1]
        for (start, left), (end, right) in zip(frames, frames[1:]):
            if time <= end:
                return left+(right-left)*(time-start)/(end-start)
        return frames[-1][1]

    # Positive scale and fixed rotation make each box edge affine between
    # the union of all keyframe times.
    cosine, sine = abs(math.cos(math.radians(rotation))), abs(math.sin(math.radians(rotation)))
    start = segment['target_timerange']['start']
    points = []
    for time in sorted(times):
        x, y = canvas[0]/2*(1+value_at('x', time)), canvas[1]/2*(1-value_at('y', time))
        w, h = width*value_at('sx', time), height*value_at('sy', time)
        half_w, half_h = (w*cosine+h*sine)/2, (w*sine+h*cosine)/2
        points.append({'time_us': start+time, 'bbox': [x-half_w, y-half_h, x+half_w, y+half_h]})
    return points


def _text_geometry(segment, material, *, canvas=(1080, 1920), text_unit_to_px=5.0):
    from .shared_operations import _measure_segment
    unit = copy.deepcopy(segment)
    unit['common_keyframes'] = []
    unit['clip'] = {'scale': {'x': 1, 'y': 1}, 'transform': {'x': 0, 'y': 0}, 'rotation': 0}
    left, top, right, bottom = _measure_segment(unit, material, canvas=canvas, text_unit_to_px=text_unit_to_px)['bbox']
    padding = _decoration_padding(material, text_unit_to_px=text_unit_to_px)
    return _linear_geometry(segment, right-left + 2*padding, bottom-top + 2*padding, canvas=canvas)


def _broll_geometry(segment, material, materials, *, canvas=(1080, 1920)):
    from .media_crop import material_crop
    width, height = material.get('width', 0), material.get('height', 0)
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (width, height)):
        raise ValueError('B-roll dimensions missing or invalid')
    if segment.get('animations'):
        raise ValueError('B-roll geometry cannot measure native animation')
    for reference in segment.get('extra_material_refs', []):
        if materials.get(reference, {}).get('animations'):
            raise ValueError('B-roll geometry cannot measure native animation')
    uniform = segment.get('uniform_scale') or {}
    if uniform.get('on') and uniform.get('value', 1) != 1:
        raise ValueError('B-roll geometry cannot compose non-default uniform_scale')
    left, top, right, bottom = material_crop(material)
    width, height = width*(right-left), height*(bottom-top)
    fit = min(canvas[0]/width, canvas[1]/height)
    return _linear_geometry(segment, width*fit, height*fit, kind='B-roll', canvas=canvas)


def _box_at(row, time):
    points = row.get('geometry_keyframes')
    if not points:
        return row['bbox']
    if time <= points[0]['time_us']:
        return points[0]['bbox']
    for left, right in zip(points, points[1:]):
        if time <= right['time_us']:
            ratio = (time-left['time_us'])/(right['time_us']-left['time_us'])
            return [a+(b-a)*ratio for a, b in zip(left['bbox'], right['bbox'])]
    return points[-1]['bbox']


def _simultaneous_overlap(first, second):
    """Solve four strict affine inequalities on each shared time interval.

    Endpoint-only or arbitrary-time sampling misses two clips crossing between
    samples. Intersecting the positive intervals of all four edge differences
    proves whether overlap exists anywhere in each piecewise-linear interval.
    """
    start, end = max(first['start_us'], second['start_us']), min(first['end_us'], second['end_us'])
    if start >= end:
        return False
    times = sorted({start, end, *(point['time_us'] for row in (first, second)
                                for point in row.get('geometry_keyframes', [])
                                if start < point['time_us'] < end)})

    def differences(a, b):
        return (a[2]-b[0], b[2]-a[0], a[3]-b[1], b[3]-a[1])

    for left, right in zip(times, times[1:]):
        lower, upper = 0.0, 1.0
        d0 = differences(_box_at(first, left), _box_at(second, left))
        d1 = differences(_box_at(first, right), _box_at(second, right))
        for a, b in zip(d0, d1):
            if a <= 0 and b <= 0:
                lower, upper = 1, 0
                break
            if a <= 0:
                lower = max(lower, -a/(b-a))
            elif b <= 0:
                upper = min(upper, a/(a-b))
        if lower < upper:
            return True
    return False


def validate_final_layout(draft, plan, *, preview=False):
    # Only undeclared/v0 contracts skip geometry; both current versions use it.
    version = plan.get('project_format', {}).get('visual_planning_version')
    if version is None or (type(version) is int and version == 0):
        return {'ok': True, 'status': 'not_enabled_legacy_contract', 'native_visual_verified': False}
    if type(version) is not int or version not in (1, 2):
        return {'ok': False, 'errors': ['visual_planning_version 必须为 1 或 2'],
                'native_visual_verified': False}
    target = plan.get('target', {})
    canvas = (target.get('width'), target.get('height'))
    if any(type(v) is not int for v in canvas) or canvas not in ((1080, 1920), (1920, 1080)):
        return {'ok': False, 'errors': ['final layout requires 1080x1920 or 1920x1080 canvas']}
    actual = draft.get('canvas_config')
    if actual is not None and (actual.get('width'), actual.get('height')) != canvas:
        return {'ok': False, 'errors': ['final draft canvas does not match target canvas']}
    config = plan.get('layout_checks', {})
    text_unit_to_px = config.get('text_unit_to_px', 5.0)
    if type(text_unit_to_px) not in (int, float) or not math.isfinite(text_unit_to_px) or text_unit_to_px <= 0:
        return {'ok': False, 'errors': ['text_unit_to_px must be a finite positive number'], 'native_visual_verified': False}
    safe = config.get('safe_area_px', [0, 0, *canvas])
    if len(safe) != 4 or any(type(x) not in (int, float) or not math.isfinite(x) for x in safe) or not (0 <= safe[0] < safe[2] <= canvas[0] and 0 <= safe[1] < safe[3] <= canvas[1]):
        return {'ok': False, 'errors': ['invalid final layout safe_area_px']}
    materials = {m['id']: m for group in draft['materials'].values() if isinstance(group, list)
                 for m in group if isinstance(m, dict) and 'id' in m}
    text_quality = validate_final_text(draft, plan, text_unit_to_px=text_unit_to_px)
    errors, text_rows, media_rows = list(text_quality['errors']), [], []
    unchecked = []
    visible_bounds = config.get('visible_bounds', [])
    if not isinstance(visible_bounds, list):
        return {'ok': False, 'errors': ['visible_bounds must be a list']}
    bound_lookup = {}
    for bound in visible_bounds:
        if not isinstance(bound, dict):
            errors.append('visible_bounds item must be an object')
            continue
        key = (bound.get('track_name'), bound.get('segment_id'))
        if not all(isinstance(value, str) and value for value in key) or key in bound_lookup:
            errors.append('visible_bounds needs unique exact track_name/segment_id')
        bound_lookup[key] = bound
    used_bounds = set()
    for track in draft['tracks']:
        name = track.get('name', track.get('id', ''))
        if track.get('visible') is False:
            continue
        for segment in track.get('segments', []):
            if (segment.get('visible') is False or segment.get('track_attribute', 0) & 1
                    or (segment.get('clip') or {}).get('alpha', 1) <= 0):
                continue
            timerange = segment['target_timerange']
            row = {'track_name': name, 'segment_id': segment.get('id'), 'start_us': timerange['start'],
                   'end_us': timerange['start']+timerange['duration']}
            material = materials.get(segment['material_id'], {})
            if track.get('type') == 'text':
                try:
                    payload = json.loads(material['content'])
                    for style in payload['styles']:
                        if not Path(style.get('font', {}).get('path', '')).is_file():
                            raise ValueError('final text font is not bound to a local file')
                    points = _text_geometry(segment, material, canvas=canvas, text_unit_to_px=text_unit_to_px)
                    bound_key = (name, segment.get('id'))
                    if bound_key in bound_lookup:
                        declared_points = _declared_geometry(bound_lookup[bound_key], start_us=row['start_us'], end_us=row['end_us'])
                        original_row = {**row, 'geometry_keyframes': points}
                        declared_row = {**row, 'geometry_keyframes': declared_points}
                        times = sorted({point['time_us'] for point in points + declared_points})
                        points = [{'time_us': time, 'bbox': list(union_bbox([_box_at(original_row, time), _box_at(declared_row, time)]))}
                                  for time in times]
                        used_bounds.add(bound_key)
                    else:
                        if material.get('background_color') and material.get('background_alpha', 1) > 0:
                            unchecked.append({'track_name': name, 'segment_id': segment.get('id'), 'part': 'native_text_background'})
                        if material.get('text_effects') or any(materials.get(ref, {}).get('type') in {'text_effect', 'text_shape'}
                                                              for ref in segment.get('extra_material_refs', [])):
                            unchecked.append({'track_name': name, 'segment_id': segment.get('id'), 'part': 'native_text_effect'})
                        if any(materials.get(ref, {}).get('animations') for ref in segment.get('extra_material_refs', [])):
                            unchecked.append({'track_name': name, 'segment_id': segment.get('id'), 'part': 'native_animation_envelope'})
                    box = list(union_bbox(point['bbox'] for point in points))
                    row.update(text=payload['text'], bbox=box, geometry_keyframes=points)
                    text_rows.append(row)
                    if box[0] < safe[0] or box[1] < safe[1] or box[2] > safe[2] or box[3] > safe[3]:
                        errors.append(f'{name}: measured text outside safe area {box}')
                except (KeyError, ValueError, OSError, TypeError) as exc:
                    errors.append(f'{name}: final text measurement failed: {exc}')
            elif track.get('type') == 'video' and name.startswith('JY_BROLL_'):
                try:
                    bound_key = (name, segment.get('id'))
                    if bound_key in bound_lookup:
                        points = _declared_geometry(bound_lookup[bound_key], start_us=row['start_us'], end_us=row['end_us'])
                        used_bounds.add(bound_key)
                    else:
                        points = _broll_geometry(segment, material, materials, canvas=canvas)
                    row.update(bbox=list(union_bbox(point['bbox'] for point in points)), geometry_keyframes=points)
                    media_rows.append(row)
                except (KeyError, ValueError, OSError, TypeError) as exc:
                    errors.append(f'{name}: final B-roll measurement failed: {exc}')
            elif track.get('type') == 'video':
                bound_key = (name, segment.get('id'))
                if bound_key in bound_lookup:
                    try:
                        points = _declared_geometry(bound_lookup[bound_key], start_us=row['start_us'], end_us=row['end_us'])
                        row.update(bbox=list(union_bbox(p['bbox'] for p in points)), geometry_keyframes=points)
                        media_rows.append(row)
                        used_bounds.add(bound_key)
                    except (KeyError, TypeError, ValueError) as exc:
                        errors.append(f'{name}: visible video bounds failed: {exc}')
                elif name != 'JY_ROUGH_CUT_VIDEO':
                    unchecked.append({'track_name': name, 'segment_id': segment.get('id'), 'part': 'auxiliary_video_visible_bounds'})
    protected = config.get('protected_regions', [])
    for region in protected:
        try:
            if not region.get('basis') or not region.get('id'):
                raise ValueError('protected region needs id and visual basis')
            start, end = region['start_us'], region['end_us']
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise ValueError('protected region requires positive integer interval')
            points = _declared_geometry(region, start_us=start, end_us=end)
            media_rows.append({'track_name': 'protected:'+region['id'], 'bbox': list(union_bbox(p['bbox'] for p in points)),
                               'geometry_keyframes': points, 'start_us': start, 'end_us': end})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append('protected region: ' + str(exc))
    if bound_lookup.keys() != used_bounds:
        errors.append('visible_bounds contains an unresolved track/segment')
    duration = max([draft.get('duration', 0)] + [row['end_us'] for row in text_rows + media_rows])
    try:
        platform_rows, platform_report = _platform_regions(config, canvas, duration)
        if version == 2 and platform_report['status'] == 'pending_platform_ui_reference':
            if preview:
                platform_report['deferred_to_user_review'] = True
                unchecked.append({'part': 'platform_ui_reference_missing'})
            else:
                errors.append('v2 layout requires platform_ui measured screenshot and exclusion regions')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        platform_rows, platform_report = [], {'status': 'invalid_reference'}
        errors.append('platform_ui: ' + str(exc))
    platform_collisions = []
    for row in text_rows + media_rows:
        for zone in platform_rows:
            if _simultaneous_overlap(row, zone):
                platform_collisions.append([row['track_name'], zone['track_name']])
                errors.append('platform UI overlaps visible content: ' + row['track_name'] + ' / ' + zone['track_name'])
    for track in draft['tracks']:
        if track.get('name') != 'JY_ROUGH_CUT_VIDEO':
            continue
        for segment in track.get('segments', []):
            if (track['name'], segment.get('id')) in used_bounds:
                continue
            start = segment['target_timerange']['start']
            end = start + segment['target_timerange']['duration']
            cursor = start
            for region in sorted(protected, key=lambda r: r.get('start_us', 0)):
                if region.get('start_us', end) <= cursor < region.get('end_us', 0):
                    cursor = region['end_us']
            if cursor < end:
                unchecked.append({'track_name': track['name'], 'segment_id': segment.get('id'),
                                  'part': 'subject_visible_regions_not_fully_declared'})
    if platform_report['status'] == 'measured_reference':
        platform_report['status'] = 'violations_found' if platform_collisions else ('partial_geometry' if unchecked else 'passed_supported_geometry')
    platform_report.update(collisions=platform_collisions, unchecked=unchecked)
    allowed = set()
    for pair in config.get('allowed_overlaps', []):
        if len(pair.get('tracks', [])) != 2 or not pair.get('reason'):
            errors.append('allowed overlap requires two exact tracks and reason')
        else:
            allowed.add(frozenset(pair['tracks']))
    pairs, collisions = 0, []
    visible_rows = text_rows + media_rows
    for i, a in enumerate(visible_rows):
        for b in visible_rows[i+1:]:
            # Protected areas describe content, rather than additional layers.
            if a['track_name'].startswith('protected:') and b['track_name'].startswith('protected:'):
                continue
            if min(a['end_us'], b['end_us']) <= max(a['start_us'], b['start_us']):
                continue
            pairs += 1
            if _simultaneous_overlap(a, b):
                pair = [a['track_name'], b['track_name']]
                if frozenset(pair) not in allowed:
                    collisions.append(pair)
                    errors.append('unplanned measured overlap: '+' / '.join(pair))
    return {'ok': not errors, 'errors': errors, 'status': 'measured_piecewise_linear_geometry', 'safe_area_px': safe,
            'text_quality': text_quality,
            'text_unit_to_px': text_unit_to_px,
            'text_segments': text_rows, 'media_and_protected_regions': media_rows,
            'checked_visible_pairs': pairs, 'collisions': collisions,
            'collision_method': 'continuous_piecewise_linear_shared_visibility',
            'subject_check': 'declared_regions_only' if protected else 'pending_no_regions',
            'platform_ui_check': platform_report,
            'visible_extents_method': 'font_measurement_plus_conservative_stroke_shadow_and_declared_envelopes',
            'native_visual_verified': False,
            'limitations': 'Text-unit conversion and native stroke/shadow padding are conservative estimates. Continuous collision checks cover supported linear geometry and supplied visible envelopes; unmeasured native backgrounds/effects/animation and subject regions are listed explicitly. No native audiovisual verification.'}
