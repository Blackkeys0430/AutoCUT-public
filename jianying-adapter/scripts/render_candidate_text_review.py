"""Static layout approximation from a candidate; never native animation evidence.

Static 8.8 common_mask circles/ellipses are clipped in source coordinates before
clip scale/position. Positive normalized centre Y moves upward; width/height
are fractions of source dimensions (see VideoSegment.add_mask). Unknown mask
geometry, feathering, rotation and mask animation fail closed. --frames-dir
optionally saves labelled stills at the draft canvas size; these remain non-native evidence.
"""
import argparse
import io
import json
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont
from jianying_adapter.media_crop import material_crop


def read_source_frame(material, source_us, ffmpeg):
    """Photos remain visible for their whole native timeline interval."""
    if material.get('type') == 'photo':
        with Image.open(material['path']) as source:
            return source.convert('RGBA')
    raw = subprocess.run([str(ffmpeg), '-v', 'error', '-ss', str(source_us/1e6), '-i', material['path'],
                          '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'],
                         capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(raw))


def mask_bbox(mask, source_size):
    """Validate supported static geometry and return source-pixel bounds."""
    if mask.get('type') != 'mask' or mask.get('resource_type') != 'circle':
        raise ValueError('static review supports only common_mask circle/ellipse')
    if any(mask.get(key) for key in ('keyframes', 'common_keyframes', 'keyframe_refs')):
        raise ValueError('mask keyframes are unsupported in static review')
    config = mask.get('config')
    defaults = dict(centerX=0, centerY=0, rotation=0, feather=0, invert=False,
                    roundCorner=0, expansion=0, aspectRatio=1)
    if not isinstance(config, dict) or not {'width', 'height'} <= config.keys():
        raise ValueError('mask requires width/height config')
    if config.keys() - (defaults.keys() | {'width', 'height'}):
        raise ValueError('unknown mask config parameters')
    values = defaults | config
    for key, value in values.items():
        if key == 'invert':
            if value is not False:
                raise ValueError('inverted masks are unsupported in static review')
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'mask {key} must be a finite number')
    for key in ('rotation', 'feather', 'roundCorner', 'expansion'):
        if values[key] != 0:
            raise ValueError(f'nonzero mask {key} is unsupported in static review')
    if values['aspectRatio'] != 1 or values['width'] <= 0 or values['height'] <= 0:
        raise ValueError('unsupported mask aspectRatio or nonpositive size')
    width, height = source_size
    cx, cy = width*(1+values['centerX'])/2, height*(1-values['centerY'])/2
    rx, ry = width*values['width']/2, height*values['height']/2
    return cx-rx, cy-ry, cx+rx, cy+ry


def segment_mask(segment, material_buckets):
    """Resolve only one unambiguous mask in the observed 8.8 material bucket."""
    if segment.get('mask') or segment.get('masks'):
        raise ValueError('inline masks are unsupported in static review')
    refs = segment.get('extra_material_refs', [])
    matches = [(bucket, item) for bucket, items in material_buckets.items() if isinstance(items, list)
               for item in items if isinstance(item, dict) and item.get('id') in refs
               and (bucket in ('common_mask', 'masks', 'mask') or item.get('type') == 'mask')]
    if not matches:
        return None
    if len(matches) != 1 or matches[0][0] != 'common_mask':
        raise ValueError('requires exactly one common_mask material')
    if segment.get('keyframe_refs') or any('mask' in str(group.get('property_type', '')).lower()
            for group in segment.get('common_keyframes', [])):
        raise ValueError('mask keyframes or unresolved keyframe references are unsupported')
    mask = matches[0][1]
    mask_bbox(mask, (1, 1))
    return mask


def composite_video(canvas, source, position, mask=None, alpha=1, source_crop=None):
    """Mask the source first, then transform its pixels and mask together."""
    image = source.convert('RGBA')
    if mask is not None:
        coverage = Image.new('L', image.size, 0)
        ImageDraw.Draw(coverage).ellipse(mask_bbox(mask, image.size), fill=255)
        image.putalpha(ImageChops.multiply(image.getchannel('A'), coverage))
    if source_crop is not None:
        from jianying_adapter.media_crop import crop_rectangle
        left, top, right, bottom = crop_rectangle(source_crop)
        image = image.crop((round(left*image.width), round(top*image.height),
                            round(right*image.width), round(bottom*image.height)))
        if min(image.size) <= 0:
            raise ValueError('source_crop is smaller than one decoded pixel')
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not 0 <= alpha <= 1:
        raise ValueError('clip alpha must be between zero and one')
    if alpha != 1:
        image.putalpha(image.getchannel('A').point(lambda value: round(value*alpha)))
    ratio = min(canvas.width/image.width, canvas.height/image.height)
    size = (round(image.width*ratio*position['sx']), round(image.height*ratio*position['sy']))
    if min(size) <= 0:
        raise ValueError('static review requires positive video scale')
    image = image.resize(size, Image.Resampling.LANCZOS)
    origin = (round(canvas.width/2*(1+position['x'])-image.width/2),
              round(canvas.height/2*(1-position['y'])-image.height/2))
    canvas.paste(image, origin, image)


def transform(segment, relative):
    clip = segment.get('clip', {})
    if any(group.get('property_type') == 'KFTypeRotation'
           for group in segment.get('common_keyframes', [])):
        # The text renderer already draws clip.rotation. Only the same
        # constant inline rotation accepted by final layout may accompany it.
        from jianying_adapter.final_layout import _linear_geometry
        _linear_geometry(segment, 1, 1)
    values = dict(sx=clip.get('scale', {}).get('x', 1), sy=clip.get('scale', {}).get('y', 1),
                  x=clip.get('transform', {}).get('x', 0), y=clip.get('transform', {}).get('y', 0))
    names = {'KFTypeScaleX': 'sx', 'KFTypeScaleY': 'sy', 'KFTypePositionX': 'x', 'KFTypePositionY': 'y'}
    for group in segment.get('common_keyframes', []):
        if group.get('property_type') == 'KFTypeRotation':
            continue  # validated above; draw_text uses unchanged clip.rotation
        if group.get('property_type') == 'KFTypeTextColor':
            # This diagnostic draws the material colour; it is not native
            # colour-animation evidence and colour does not change geometry.
            continue
        key = names.get(group.get('property_type'))
        if not key:
            raise ValueError('unsupported keyframe for static review')
        frames = sorted(group['keyframe_list'], key=lambda f: f['time_offset'])
        value = frames[0]['values'][0]
        for left, right in zip(frames, frames[1:]):
            if relative >= right['time_offset']:
                value = right['values'][0]
            elif relative >= left['time_offset']:
                ratio = (relative-left['time_offset']) / (right['time_offset']-left['time_offset'])
                value = left['values'][0] + ratio*(right['values'][0]-left['values'][0])
                break
        values[key] = value
    # Native/vendor UNIFORM_SCALE is serialized as ScaleX with this lock on;
    # independent X/Y keyframes explicitly turn the lock off.
    properties = {g.get('property_type') for g in segment.get('common_keyframes', [])}
    if (segment.get('uniform_scale') or {}).get('on') is True and 'KFTypeScaleX' in properties:
        if 'KFTypeScaleY' in properties:
            raise ValueError('locked uniform scale has conflicting Y keyframes')
        values['sy'] = values['sx']
    return values


def checked_text_unit_to_px(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('text_unit_to_px must be a finite positive number')
    return value


def approximate_strokes(style, font_pixel_size):
    result = []
    for stroke in style.get('strokes', []):
        solid = stroke.get('content', {}).get('solid', {})
        # Older native rich text omits opaque alpha instead of writing 1.
        color, alpha, width = solid.get('color'), solid.get('alpha', 1), stroke.get('width')
        if (not isinstance(color, list) or len(color) != 3
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in color)
                or type(alpha) not in (int, float) or not math.isfinite(alpha) or not 0 <= alpha <= 1
                or type(width) not in (int, float) or not math.isfinite(width) or not 0 <= width <= .2):
            raise ValueError('static review requires supported native solid strokes')
        if width > 0 and alpha > 0:
            result.append((math.ceil(font_pixel_size*width), tuple(round(c*255) for c in color), round(alpha*255)))
    return sorted(result, reverse=True)


def draw_text(canvas, material, position, *, text_unit_to_px=5.0, rotation=0):
    checked_text_unit_to_px(text_unit_to_px)
    if type(rotation) not in (int, float) or not math.isfinite(rotation):
        raise ValueError('text rotation must be a finite number')
    payload = json.loads(material['content'])
    text, styles = payload['text'], payload['styles']
    alignment = material.get('alignment', 1)
    if type(alignment) is not int or alignment not in (0, 1, 2):
        raise ValueError('static review alignment must be an integer in {0,1,2}')
    if position['sx'] != position['sy']:
        raise ValueError('nonuniform text scale not supported')
    lines, index = [], 0
    spacing = material.get('letter_spacing', 0)*text_unit_to_px*position['sx']
    for line in text.split('\n'):
        glyphs = []
        for char in line:
            style = next(s for s in styles if s['range'][0] <= index < s['range'][1])
            font_pixels = round(style['size']*text_unit_to_px*position['sx'])
            font = ImageFont.truetype(style['font']['path'], font_pixels)
            color = style.get('fill', {}).get('content', {}).get('solid', {}).get('color', [1, 1, 1])
            glyphs.append((char, font, tuple(round(c*255) for c in color), font.getlength(char),
                           approximate_strokes(style, font_pixels)))
            index += 1
        index += 1
        ascent = max((f.getmetrics()[0] for _, f, _, _, _ in glyphs), default=0)
        descent = max((f.getmetrics()[1] for _, f, _, _, _ in glyphs), default=0)
        width = sum(w for _, _, _, w, _ in glyphs) + max(0, len(glyphs)-1)*spacing
        lines.append((glyphs, width, ascent, descent))
    cx, cy = canvas.width/2*(1+position['x']), canvas.height/2*(1-position['y'])
    # The segment position anchors the whole block. Alignment only changes
    # shorter lines inside that block; it never moves the block itself.
    block_width = max(w for _, w, _, _ in lines)
    block_left = cx-block_width/2
    height = sum(a+d for _, _, a, d in lines)
    if rotation:
        # Rotate only the complete local text block around its own anchor.
        # Jianying uses clockwise degrees; Pillow uses counterclockwise degrees.
        padding = 4 + max((stroke[0] for glyphs, _, _, _ in lines
                           for _, _, _, _, strokes in glyphs for stroke in strokes), default=0)
        layer = Image.new('RGBA', (max(1, math.ceil(block_width)+2*padding),
                                   max(1, math.ceil(height)+2*padding)))
        local = draw_text(layer, material, dict(position, x=0, y=0), text_unit_to_px=text_unit_to_px)
        layer = layer.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True)
        origin = (round(cx-layer.width/2), round(cy-layer.height/2))
        if canvas.mode == 'RGBA':
            canvas.alpha_composite(layer, dest=origin)
        else:
            canvas.paste(layer, origin, layer)
        radians = math.radians(rotation)
        box = local['approx_bbox']
        width, block_height = box[2]-box[0], box[3]-box[1]
        rotated_width = abs(width*math.cos(radians))+abs(block_height*math.sin(radians))
        rotated_height = abs(width*math.sin(radians))+abs(block_height*math.cos(radians))
        return dict(local, approx_bbox=[cx-rotated_width/2, cy-rotated_height/2,
                                       cx+rotated_width/2, cy+rotated_height/2], rotation=rotation)
    y = cy-height/2
    draw = ImageDraw.Draw(canvas)
    stroke_draw = ImageDraw.Draw(canvas, 'RGBA')
    max_stroke = 0
    for glyphs, width, ascent, descent in lines:
        x = (block_left if alignment == 0 else
             cx-width/2 if alignment == 1 else block_left+block_width-width)
        for char, font, color, advance, strokes in glyphs:
            draw.text((x+1, y+ascent+2), char, font=font, fill=(25, 25, 25), anchor='ls')
            for width_px, stroke_color, alpha in strokes:
                stroke_draw.text((x, y+ascent), char, font=font, fill=(0, 0, 0, 0), anchor='ls',
                                 stroke_width=width_px, stroke_fill=(*stroke_color, alpha))
                max_stroke = max(max_stroke, width_px)
            draw.text((x, y+ascent), char, font=font, fill=color, anchor='ls')
            x += advance+spacing
        y += ascent+descent
    return {'text': text, 'approx_bbox': [block_left-max_stroke, cy-height/2-max_stroke,
                                         block_left+block_width+max_stroke, cy+height/2+max_stroke],
            'approx_stroke_px': max_stroke}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('draft', 'ffmpeg', 'output', 'report'):
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--times', required=True, nargs='+', type=float)
    parser.add_argument('--frames-dir', type=Path)
    parser.add_argument('--text-unit-to-px', type=float, default=5.0,
                        help='Explicit text-unit pixel estimate; use the same value as layout_checks.text_unit_to_px.')
    parser.add_argument('--ignore-video-effects-for-layout', action='store_true',
                        help='Explicit layout-only diagnostic; native video effects are omitted and labelled.')
    args = parser.parse_args()
    checked_text_unit_to_px(args.text_unit_to_px)
    draft = json.loads(args.draft.read_text('utf-8-sig'))
    has_effects = bool(draft.get('materials', {}).get('video_effects')) or any(
        t.get('type') == 'effect' and t.get('segments') for t in draft.get('tracks', []))
    if has_effects and not args.ignore_video_effects_for_layout:
        raise ValueError('native video effects cannot be rendered here; use native playback, or explicitly '
                         '--ignore-video-effects-for-layout for a labelled layout-only diagnostic')
    effect_label = ' / EFFECTS OMITTED' if has_effects else ''
    config = draft.get('canvas_config', {'width': 1080, 'height': 1920})
    canvas_size = (config.get('width'), config.get('height'))
    if any(type(v) is not int for v in canvas_size) or canvas_size not in ((1080, 1920), (1920, 1080)):
        raise ValueError('static review requires 1080x1920 or 1920x1080 canvas')
    thumbnail_size = tuple(value//4 for value in canvas_size)
    materials = {m['id']: m for values in draft['materials'].values() if isinstance(values, list)
                 for m in values if isinstance(m, dict) and 'id' in m}
    # Validate all video masks, even if requested samples happen to miss them.
    for track in draft['tracks']:
        if track.get('type') == 'video':
            for segment in track.get('segments', []):
                segment_mask(segment, draft['materials'])
    if args.frames_dir:
        args.frames_dir.mkdir(parents=True, exist_ok=True)
    frames, report = [], []
    for seconds in args.times:
        time = round(seconds*1e6)
        canvas = Image.new('RGB', canvas_size, '#151515')
        active = [(s.get('render_index', 0), t, s) for t in draft['tracks'] if t.get('type') in ('video', 'text')
                  for s in t.get('segments', []) if s['target_timerange']['start'] <= time < sum(s['target_timerange'].values())]
        rows = []
        for _, track, segment in sorted(active, key=lambda item: item[0]):
            material = materials[segment['material_id']]
            relative = time-segment['target_timerange']['start']
            position = transform(segment, relative)
            rotation = (segment.get('clip') or {}).get('rotation', 0)
            if (track['type'] != 'text' and rotation) or segment.get('reverse') or segment.get('speed', 1) != 1:
                raise ValueError('unsupported rotation/reverse/speed')
            if track['type'] == 'text':
                rows.append(draw_text(canvas, material, position, text_unit_to_px=args.text_unit_to_px,
                                      rotation=rotation))
            else:
                source = segment['source_timerange']['start']+relative
                image = read_source_frame(material, source, args.ffmpeg)
                composite_video(canvas, image, position, segment_mask(segment, draft['materials']),
                                segment.get('clip', {}).get('alpha', 1), material_crop(material))
        frames.append(canvas.resize(thumbnail_size))
        entry = {'seconds': seconds, 'text': rows}
        if args.frames_dir:
            frame_path = args.frames_dir / f'{len(frames):03d}_{seconds:.3f}s_STATIC_APPROX.png'
            ImageDraw.Draw(canvas).text((12, 12), f'{seconds:.3f}s - STATIC APPROX / NON-NATIVE{effect_label}', fill='white',
                                        stroke_width=2, stroke_fill='black')
            canvas.save(frame_path)
            entry['still_path'] = str(frame_path.resolve())
        report.append(entry)
    cell_width, cell_height = thumbnail_size[0], thumbnail_size[1]+30
    sheet = Image.new('RGB', (cell_width*4, cell_height*((len(frames)+3)//4)), '#202020')
    for i, frame in enumerate(frames):
        x, y = (i%4)*cell_width, (i//4)*cell_height
        sheet.paste(frame, (x, y+30))
        ImageDraw.Draw(sheet).text((x+8, y+8), f'{args.times[i]:.2f}s - STATIC APPROX{effect_label}', fill='white')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(args.output)
    args.report.write_text(json.dumps({'native_visual_verified': False, 'native_video_effects_omitted': has_effects, 'text_unit_to_px': args.text_unit_to_px, 'stroke_width_approximation': 'ceil(font_pixel_size * native_stroke.width); not a verified native pixel formula', 'canvas': {'width': canvas_size[0], 'height': canvas_size[1]}, 'limitations': 'Static approximation; the selected text-unit pixel conversion is an estimate, not a universal native formula. Native video effects are not rendered. Solid text strokes use an approximate font-relative width. Ignores native entrances/glow and text colour animation, uses approximate font baseline, shadows and line spacing; no audio evaluation.', 'frames': report}, ensure_ascii=False, indent=2), 'utf-8')


if __name__ == '__main__':
    main()
