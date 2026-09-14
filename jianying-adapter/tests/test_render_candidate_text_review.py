"""Pure geometry checks for explicitly non-native candidate review stills."""
import importlib.util
import copy
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageFont
import pytest


spec = importlib.util.spec_from_file_location(
    'candidate_review', Path(__file__).parents[1] / 'scripts' / 'render_candidate_text_review.py')
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


@pytest.mark.parametrize('locked, expected_y', [(True, .85), (False, 1)])
def test_native_x_scale_keyframe_honors_uniform_lock(locked, expected_y):
    from jianying_adapter.final_layout import _linear_geometry
    segment = {'target_timerange': {'start': 0, 'duration': 1000000},
               'uniform_scale': {'on': locked, 'value': 1.0},
               'common_keyframes': [{'property_type': 'KFTypeScaleX', 'keyframe_list': [
                   {'time_offset': 0, 'values': [1.2], 'curveType': 'Line'},
                   {'time_offset': 800000, 'values': [.5], 'curveType': 'Line'}]}]}
    before = copy.deepcopy(segment)
    position = review.transform(segment, 400000)
    assert position['sx'] == pytest.approx(.85)
    assert position['sy'] == pytest.approx(expected_y)
    boxes = _linear_geometry(segment, 100, 200)
    first, last = boxes[0]['bbox'], boxes[-1]['bbox']
    assert first[2]-first[0] == pytest.approx(120)
    assert first[3]-first[1] == pytest.approx(240 if locked else 200)
    assert last[2]-last[0] == pytest.approx(50)
    assert last[3]-last[1] == pytest.approx(100 if locked else 200)
    assert segment == before


def test_locked_uniform_scale_rejects_conflicting_y_curve():
    from jianying_adapter.final_layout import _linear_geometry
    segment = {'target_timerange': {'start': 0, 'duration': 1000000},
               'uniform_scale': {'on': True, 'value': 1.0},
               'common_keyframes': [{'property_type': prop, 'keyframe_list': [
                   {'time_offset': 0, 'values': [1.2], 'curveType': 'Line'}]}
                   for prop in ['KFTypeScaleX', 'KFTypeScaleY']]}
    with pytest.raises(ValueError, match='conflicting Y'):
        review.transform(segment, 0)
    with pytest.raises(ValueError, match='conflicting Y'):
        _linear_geometry(segment, 100, 200)


@pytest.mark.parametrize('rotation', [0, 30])
def test_static_review_preserves_fixed_rotation_keyframe_contract(rotation):
    segment = {'target_timerange': {'start': 0, 'duration': 1000},
               'clip': {'rotation': rotation}, 'common_keyframes': [
                   {'property_type': 'KFTypeRotation', 'keyframe_list': [
                       {'time_offset': 0, 'values': [rotation]},
                       {'time_offset': 1000, 'values': [rotation]}]}]}
    before = copy.deepcopy(segment)
    assert review.transform(segment, 500) == {'sx': 1, 'sy': 1, 'x': 0, 'y': 0}
    assert segment == before


@pytest.mark.parametrize('times,values,property_name', [
    ([0, 1000], [0, 1], 'KFTypeRotation'),
    ([0, 1000], [1, 1], 'KFTypeRotation'),
    ([-1, 1000], [0, 0], 'KFTypeRotation'),
    ([0, 1001], [0, 0], 'KFTypeRotation'),
    ([0, 1000], [0, 0], 'KFTypeUnknown'),
])
def test_static_review_rejects_changing_or_unsupported_rotation(times, values, property_name):
    segment = {'target_timerange': {'start': 0, 'duration': 1000},
               'clip': {'rotation': 0}, 'common_keyframes': [
                   {'property_type': property_name, 'keyframe_list': [
                       {'time_offset': time, 'values': [value]} for time, value in zip(times, values)]}]}
    with pytest.raises(ValueError):
        review.transform(segment, 500)


def test_source_crop_square_preserves_pixels_without_stretch():
    source = Image.new('RGB', (320, 180), 'red')
    source.paste('blue', (70, 0, 250, 180))
    canvas = Image.new('RGB', (1080, 1920), 'black')
    review.composite_video(canvas, source, dict(sx=.4, sy=.4, x=0, y=0),
                           source_crop=[.21875, 0, .78125, 1])
    assert canvas.getbbox() == (324, 744, 756, 1176)
    assert canvas.getpixel((540, 960)) == (0, 0, 255)


def test_native_photo_is_visible_after_its_first_frame(tmp_path, monkeypatch):
    path = tmp_path / 'cover.png'
    Image.new('RGB', (31, 19), '#b34422').save(path)
    def no_video_seek(*args, **kwargs):
        raise AssertionError('static photo must not be seeked as video')
    monkeypatch.setattr(review.subprocess, 'run', no_video_seek)
    result = review.read_source_frame({'type': 'photo', 'path': str(path)}, 8_500_000, 'unused')
    assert result.size == (31, 19)
    assert result.getpixel((10, 10)) == (179, 68, 34, 255)


@pytest.mark.parametrize('alignment', [0, 1, 2])
@pytest.mark.parametrize('canvas_size', [(1080, 1920), (1920, 1080)])
def test_multiline_alignment_uses_one_block_anchor(monkeypatch, alignment, canvas_size):
    font = ImageFont.load_default()
    monkeypatch.setattr(review.ImageFont, 'truetype', lambda *_args, **_kwargs: font)
    material = {'alignment': alignment, 'letter_spacing': .6, 'content': json.dumps({
        'text': 'AAA\nA', 'styles': [{'range': [0, 5], 'size': 15,
                                   'font': {'path': 'bound-test-font'}}]})}
    position = dict(sx=1, sy=1, x=.2, y=.1)
    canvas = Image.new('RGB', canvas_size, 'black')
    result = review.draw_text(canvas, material, position)
    bbox = result['approx_bbox']
    assert (bbox[0]+bbox[2])/2 == pytest.approx(canvas.width*.6)
    assert (bbox[1]+bbox[3])/2 == pytest.approx(canvas.height*.45)
    # Inspect actual bright glyph pixels separately from the dark shadow.
    pixels = canvas.convert('L').point(lambda value: 255 if value > 127 else 0)
    middle = round(canvas.height*.45)
    upper = pixels.crop((0, 0, canvas.width, middle)).getbbox()
    lower = pixels.crop((0, middle, canvas.width, canvas.height)).getbbox()
    assert upper and lower
    if alignment == 0:
        assert lower[0] == upper[0]
    elif alignment == 2:
        assert lower[2] == pytest.approx(upper[2], abs=1)
    else:
        assert lower[0]+lower[2] == pytest.approx(upper[0]+upper[2], abs=1)
    # Omitting alignment keeps the previous centered rendering unchanged.
    if alignment == 1:
        material.pop('alignment')
        default = Image.new('RGB', canvas.size, 'black')
        assert review.draw_text(default, material, position) == result
        assert ImageChops.difference(canvas, default).getbbox() is None


@pytest.mark.parametrize('alignment', [-1, 3, 0.0, True, 'left', None])
def test_invalid_text_alignment_rejected_before_drawing(alignment):
    canvas = Image.new('RGB', (1080, 1920), 'black')
    material = {'alignment': alignment, 'content': json.dumps({'text': 'A', 'styles': []})}
    with pytest.raises(ValueError, match='alignment'):
        review.draw_text(canvas, material, dict(sx=1, sy=1, x=0, y=0))
    assert canvas.getbbox() is None


@pytest.mark.parametrize('omit_opaque_alpha', [False, True])
def test_native_solid_stroke_is_visible_in_static_review_and_does_not_mutate_style(omit_opaque_alpha):
    font = Path('C:/Windows/Fonts/arial.ttf')
    if not font.is_file():
        pytest.skip('Windows fixture font unavailable')
    style = {'range': [0, 2], 'size': 20, 'font': {'path': str(font)}}
    material = {'content': json.dumps({'text': 'OO', 'styles': [style]})}
    position = dict(sx=1, sy=1, x=0, y=0)
    plain = Image.new('RGB', (400, 300), 'white')
    plain_report = review.draw_text(plain, material, position)
    style['strokes'] = [{'content': {'solid': {'color': [0, 0, 0], 'alpha': 1}}, 'width': .036}]
    if omit_opaque_alpha:
        del style['strokes'][0]['content']['solid']['alpha']
    material['content'] = json.dumps({'text': 'OO', 'styles': [style]})
    before = copy.deepcopy(material)
    outlined = Image.new('RGB', plain.size, 'white')
    report = review.draw_text(outlined, material, position)
    assert report['approx_stroke_px'] == 4  # ceil(20 * 5 * .036)
    assert report['approx_bbox'][0] == plain_report['approx_bbox'][0]-4
    assert report['approx_bbox'][2] == plain_report['approx_bbox'][2]+4
    assert ImageChops.difference(plain, outlined).getbbox() is not None
    assert sum(count for count, pixel in outlined.getcolors(outlined.width*outlined.height) if pixel == (0, 0, 0)) > 100
    assert material == before
    style['strokes'][0]['content']['solid']['alpha'] = 0
    material['content'] = json.dumps({'text': 'OO', 'styles': [style]})
    transparent = Image.new('RGB', plain.size, 'white')
    assert review.draw_text(transparent, material, position)['approx_stroke_px'] == 0
    assert ImageChops.difference(plain, transparent).getbbox() is None


@pytest.mark.parametrize('rotation', [-20, 20])
def test_fixed_text_rotation_changes_glyph_slope_around_block_anchor(rotation):
    font = Path('C:/Windows/Fonts/arial.ttf')
    if not font.is_file():
        pytest.skip('Windows fixture font unavailable')
    material = {'content': json.dumps({'text': 'MMMMMM', 'styles': [
        {'range': [0, 6], 'size': 10, 'font': {'path': str(font)}}]})}
    position = dict(sx=1, sy=1, x=.2, y=-.2)
    canvas = Image.new('RGB', (600, 400), '#204080')
    before = copy.deepcopy(material)
    report = review.draw_text(canvas, material, position, rotation=rotation)
    box = report['approx_bbox']
    assert (box[0]+box[2])/2 == pytest.approx(360)
    assert (box[1]+box[3])/2 == pytest.approx(240)
    # White text forms a wide line; clockwise rotation slopes downward on screen.
    coords = [(x, y) for y in range(canvas.height) for x in range(canvas.width)
              if min(canvas.getpixel((x, y))) > 200]
    mean_x = sum(x for x, _ in coords)/len(coords)
    mean_y = sum(y for _, y in coords)/len(coords)
    covariance = sum((x-mean_x)*(y-mean_y) for x, y in coords)/len(coords)
    assert covariance*rotation > 0
    assert mean_x == pytest.approx(360, abs=15)
    assert mean_y == pytest.approx(240, abs=15)
    assert canvas.getpixel((0, 0)) == (32, 64, 128)
    assert material == before


def circle(**config):
    return {'id': 'mask', 'type': 'mask', 'resource_type': 'circle',
            'config': {'width': .4, 'height': .2, **config}}


def test_positive_center_y_moves_up_in_source_pixels():
    assert review.mask_bbox(circle(centerY=.4), (100, 200)) == pytest.approx((30, 40, 70, 80))


def test_source_mask_follows_nonuniform_scale_and_position():
    canvas = Image.new('RGB', (200, 400), 'black')
    source = Image.new('RGB', (100, 200), 'red')
    review.composite_video(canvas, source, {'sx': .5, 'sy': .25, 'x': .3, 'y': -.2},
                           circle(centerY=.4))
    # Mask centre: source (50,60), transformed to (130,220).
    # The source-space circle becomes an ellipse of radius (20,10).
    assert canvas.getpixel((130, 220)) == (255, 0, 0)
    assert canvas.getpixel((147, 220))[0] > 240
    assert canvas.getpixel((130, 228))[0] > 240
    assert canvas.getpixel((130, 237)) == (0, 0, 0)
    assert canvas.getpixel((158, 220)) == (0, 0, 0)
    assert canvas.getpixel((130, 260)) == (0, 0, 0)


@pytest.mark.parametrize('config', [{'unknown': 1}, {'rotation': 10}, {'feather': .1},
                                    {'invert': True}, {'height': float('nan')}, {'width': 0}])
def test_unsupported_mask_config_rejected(config):
    with pytest.raises(ValueError):
        review.mask_bbox(circle(**config), (100, 200))


def test_unknown_shape_and_mask_animation_rejected():
    mask = circle()
    mask['resource_type'] = 'rectangle'
    with pytest.raises(ValueError, match='circle/ellipse'):
        review.mask_bbox(mask, (100, 200))
    segment = {'extra_material_refs': ['mask'], 'common_keyframes': [
        {'property_type': 'KFTypeMaskPositionY'}]}
    with pytest.raises(ValueError, match='keyframes'):
        review.segment_mask(segment, {'common_mask': [circle()]})
    segment['common_keyframes'] = []
    segment['keyframe_refs'] = ['unresolved']
    with pytest.raises(ValueError, match='keyframe'):
        review.segment_mask(segment, {'common_mask': [circle()]})


def test_wrong_mask_bucket_rejected_and_unmasked_video_preserved():
    segment = {'extra_material_refs': ['mask']}
    with pytest.raises(ValueError, match='common_mask'):
        review.segment_mask(segment, {'masks': [circle()]})
    canvas = Image.new('RGB', (100, 200), 'black')
    review.composite_video(canvas, Image.new('RGB', (100, 200), 'red'),
                           dict(sx=1, sy=1, x=0, y=0))
    assert canvas.getpixel((0, 0)) == (255, 0, 0)
    assert canvas.getpixel((99, 199)) == (255, 0, 0)


@pytest.mark.parametrize('size', [(1080, 1920), (1920, 1080)])
@pytest.mark.parametrize('text_unit_to_px', [None, 10.8])
def test_review_main_keeps_canvas_aspect_and_lower_caption(tmp_path, monkeypatch, size, text_unit_to_px):
    font = Path('C:/Windows/Fonts/msyh.ttc')
    if not font.exists():
        pytest.skip('Windows layout fixture font unavailable')
    source = tmp_path/'source.png'
    Image.new('RGB', size, '#204080').save(source)
    draft_path, output, report_path = (tmp_path/name for name in ('draft.json', 'sheet.jpg', 'review.json'))
    frames = tmp_path/'frames'
    material = {'id': 'text', 'content': json.dumps({'text': '下部字幕', 'styles': [
        {'range': [0, 4], 'size': 14, 'font': {'path': str(font)}}]})}
    draft = {'canvas_config': dict(width=size[0], height=size[1]), 'materials': {
        'videos': [{'id': 'image', 'type': 'photo', 'path': str(source)}], 'texts': [material]}, 'tracks': [
        {'type': 'video', 'segments': [{'material_id': 'image', 'render_index': 0,
            'target_timerange': {'start': 0, 'duration': 1_000_000}, 'source_timerange': {'start': 0, 'duration': 1_000_000}}]},
        {'type': 'text', 'segments': [{'material_id': 'text', 'render_index': 1,
            'target_timerange': {'start': 0, 'duration': 1_000_000}, 'clip': {'transform': {'x': 0, 'y': -.7}}}]}]}
    draft_path.write_text(json.dumps(draft), 'utf-8')
    original_bytes = draft_path.read_bytes()
    monkeypatch.setattr(sys, 'argv', ['review', '--draft', str(draft_path), '--ffmpeg', 'unused',
        '--output', str(output), '--report', str(report_path), '--frames-dir', str(frames), '--times', '.5']
        + ([] if text_unit_to_px is None else ['--text-unit-to-px', str(text_unit_to_px)]))
    review.main()
    result = json.loads(report_path.read_text('utf-8'))
    assert result['canvas'] == draft['canvas_config'] and result['native_visual_verified'] is False
    selected_unit = 5.0 if text_unit_to_px is None else text_unit_to_px
    assert result['text_unit_to_px'] == selected_unit
    assert 'not a universal native formula' in result['limitations']
    assert draft_path.read_bytes() == original_bytes
    box = result['frames'][0]['text'][0]['approx_bbox']
    expected_font = ImageFont.truetype(str(font), round(14*selected_unit))
    assert box[2]-box[0] == pytest.approx(sum(expected_font.getlength(c) for c in '下部字幕'))
    assert (box[0]+box[2])/2 == pytest.approx(size[0]/2)
    assert (box[1]+box[3])/2 == pytest.approx(size[1]*.85)
    with Image.open(result['frames'][0]['still_path']) as still:
        assert still.size == size and still.getpixel((0, size[1]-1)) == (32, 64, 128)
    with Image.open(output) as sheet:
        assert sheet.size == (size[0], size[1]//4+30)


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf'])
def test_review_cli_rejects_invalid_text_unit_before_reading_draft(tmp_path, monkeypatch, value):
    output = tmp_path/'sheet.jpg'
    monkeypatch.setattr(sys, 'argv', ['review', '--draft', str(tmp_path/'missing.json'), '--ffmpeg', 'unused',
        '--output', str(output), '--report', str(tmp_path/'review.json'), '--times', '.5', '--text-unit-to-px', value])
    with pytest.raises(ValueError, match='text_unit_to_px'):
        review.main()
    assert not output.exists()


def test_review_main_refuses_unsupported_canvas(tmp_path, monkeypatch):
    draft = tmp_path/'draft.json'
    draft.write_text(json.dumps({'canvas_config': {'width': 1080, 'height': 608}}), 'utf-8')
    monkeypatch.setattr(sys, 'argv', ['review', '--draft', str(draft), '--ffmpeg', 'unused',
        '--output', str(tmp_path/'sheet.jpg'), '--report', str(tmp_path/'report.json'), '--times', '.5'])
    with pytest.raises(ValueError, match='1080x1920 or 1920x1080'):
        review.main()
    assert not (tmp_path/'sheet.jpg').exists()


@pytest.mark.parametrize('carrier', ['material', 'track'])
def test_native_effects_require_explicit_omission_and_label(tmp_path, monkeypatch, carrier):
    draft_path = tmp_path/'draft.json'
    draft = {'canvas_config': {'width': 1080, 'height': 1920}, 'materials': {}, 'tracks': []}
    if carrier == 'material':
        draft['materials']['video_effects'] = [{'id': 'blur', 'type': 'video_effect'}]
    else:
        draft['tracks'] = [{'type': 'effect', 'segments': [{'material_id': 'blur'}]}]
    draft_path.write_text(json.dumps(draft), 'utf-8')
    args = ['review', '--draft', str(draft_path), '--ffmpeg', 'unused',
            '--output', str(tmp_path/'sheet.jpg'), '--report', str(tmp_path/'report.json'), '--times', '.5']
    monkeypatch.setattr(sys, 'argv', args)
    with pytest.raises(ValueError, match='native video effects'):
        review.main()
    assert not (tmp_path/'sheet.jpg').exists()
    labels = []
    original = review.ImageDraw.ImageDraw.text
    def record_label(self, xy, text, *args, **kwargs):
        labels.append(text)
        return original(self, xy, text, *args, **kwargs)
    monkeypatch.setattr(review.ImageDraw.ImageDraw, 'text', record_label)
    monkeypatch.setattr(sys, 'argv', args + ['--ignore-video-effects-for-layout'])
    review.main()
    result = json.loads((tmp_path/'report.json').read_text('utf-8'))
    assert result['native_video_effects_omitted'] is True
    assert result['native_visual_verified'] is False
    assert any('EFFECTS OMITTED' in label for label in labels)
