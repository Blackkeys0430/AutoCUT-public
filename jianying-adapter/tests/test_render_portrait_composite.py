"""The derived-visual CLI preserves source colours, timing inputs and outputs."""
import copy
import importlib.util
import io
from pathlib import Path

import pytest
from PIL import Image


module_spec = importlib.util.spec_from_file_location(
    'portrait_composite', Path(__file__).parents[1]/'scripts/render_portrait_composite.py')
portrait = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(portrait)


def job(tmp_path):
    source, model, ffmpeg, background = [tmp_path/name for name in ('source.mp4', 'model.onnx', 'ffmpeg.exe', 'background.png')]
    for path in (source, model, ffmpeg):
        path.write_bytes(b'local fixture')
    Image.new('RGB', (8, 12), 'blue').save(background)
    return {'schema': portrait.SCHEMA, 'source': {'path': str(source), 'start_us': 200000, 'end_us': 1200000},
            'model': {'path': str(model), 'kind': 'u2net', 'provider': 'cpu'},
            'canvas': {'width': 8, 'height': 12, 'fps': 30},
            'foreground': {'scale': 1, 'center_x_px': 4, 'center_y_px': 6},
            'background': {'path': str(background)}, 'output_dir': str(tmp_path/'output'),
            'sample_offsets_us': [0, 500000, 900000], 'ffmpeg': str(ffmpeg)}


@pytest.mark.parametrize('path,value', [
    (('source', 'start_us'), True), (('source', 'end_us'), 200000),
    (('source', 'path'), 'relative.mp4'), (('model', 'provider'), 'automatic'),
    (('model', 'kind'), 'download-something'), (('foreground', 'scale'), float('nan')),
    (('foreground', 'scale'), 0), (('foreground', 'center_x_px'), float('inf')),
    (('canvas', 'width'), 7), (('canvas', 'fps'), False),
    (('sample_offsets_us',), [0, 1000000]), (('sample_offsets_us',), [0, 0]),
    (('output_filename',), '../overwrite.mp4'), (('output_dir',), 'relative'),
])
def test_bad_job_is_rejected_before_output(tmp_path, path, value):
    spec = job(tmp_path)
    target = spec
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        portrait.validate_spec(spec, 'sample')
    assert not (tmp_path/'output').exists()


def test_explicit_interval_and_geometry_are_kept_without_mutating_job(tmp_path):
    spec = job(tmp_path)
    before = copy.deepcopy(spec)
    result = portrait.validate_spec(spec, 'render')
    assert result['source'] == spec['source']
    assert result['foreground'] == spec['foreground']
    assert spec == before


def test_render_requires_background_and_never_reuses_its_target(tmp_path):
    spec = job(tmp_path)
    spec['background'] = None
    with pytest.raises(ValueError, match='background'):
        portrait.validate_spec(spec, 'render')
    portrait.validate_spec(spec, 'sample')
    spec = job(tmp_path)
    output = Path(spec['output_dir']); output.mkdir()
    unrelated = output/'another_composition.mp4'; unrelated.write_bytes(b'preserve')
    portrait.validate_spec(spec, 'render')
    (output/'portrait_composite.mp4').write_bytes(b'old result')
    with pytest.raises(ValueError, match='overwritten'):
        portrait.validate_spec(spec, 'render')
    assert unrelated.read_bytes() == b'preserve'


def test_video_background_has_explicit_time_cover_and_brightness(tmp_path):
    spec = job(tmp_path)
    spec['background'] = {'path': spec['source']['path'], 'kind': 'video', 'start_us': 300000,
                          'fit': 'cover', 'brightness': .4}
    checked = portrait.validate_spec(spec, 'render')
    command = portrait.background_command(checked, 30, 100000)
    assert command[command.index('-ss')+1] == '0.4'
    assert 'force_original_aspect_ratio=increase' in command[command.index('-vf')+1]
    assert checked['background']['brightness'] == .4
    spec['background']['brightness'] = True
    with pytest.raises(ValueError, match='brightness'):
        portrait.validate_spec(spec, 'render')


def test_opaque_subject_retains_exact_original_colours():
    source = Image.new('RGB', (2, 2), (78, 46, 19))
    bg = Image.new('RGB', (2, 2), (0, 100, 255))
    result = portrait.composite(source, Image.new('L', (2, 2), 255), bg,
                                {'scale': 1, 'center_x_px': 1, 'center_y_px': 1})
    assert result.tobytes() == source.tobytes()
    result = portrait.composite(source, Image.new('L', (2, 2), 0), bg,
                                {'scale': 1, 'center_x_px': 1, 'center_y_px': 1})
    assert result.tobytes() == bg.tobytes()


def test_premultiplied_resize_does_not_pull_white_background_into_red_edge():
    source = Image.new('RGB', (2, 1)); source.putdata([(255, 0, 0), (255, 255, 255)])
    alpha = Image.new('L', (2, 1)); alpha.putdata([255, 0])
    result = portrait.composite(source, alpha, Image.new('RGB', (4, 2), 'black'),
                                {'scale': 1, 'center_x_px': 2, 'center_y_px': 1})
    assert all(g == 0 and b == 0 for r, g, b in result.getdata())
    assert any(r > 0 for r, g, b in result.getdata())


def test_outside_subject_and_mismatched_alpha_are_rejected():
    source, bg = Image.new('RGB', (2, 2)), Image.new('RGB', (2, 2))
    fg = {'scale': 1, 'center_x_px': 100, 'center_y_px': 100}
    with pytest.raises(ValueError, match='outside'):
        portrait.composite(source, Image.new('L', (2, 2)), bg, fg)
    with pytest.raises(ValueError, match='dimensions'):
        portrait.composite(source, Image.new('L', (1, 2)), bg, fg)


def test_truncated_raw_frame_is_not_silently_encoded():
    assert portrait.read_exact(io.BytesIO(b'abcdef'), 6) == b'abcdef'
    assert portrait.read_exact(io.BytesIO(b''), 6) is None
    with pytest.raises(RuntimeError, match='truncated'):
        portrait.read_exact(io.BytesIO(b'abc'), 6)


def test_explicit_interior_repair_preserves_gaps_outside_region_and_source_rgb(tmp_path):
    spec = job(tmp_path)
    spec['alpha_repair'] = {'kind': 'keep_source_polygons', 'polygons': [[[.25, .5], [.75, .5], [.75, 1], [.25, 1]]]}
    checked = portrait.validate_spec(spec, 'sample')
    alpha = Image.new('L', (9, 9), 17)
    source = Image.new('RGB', alpha.size, (32, 41, 55))
    repaired = portrait.repair_alpha(alpha, checked['alpha_repair'])
    assert repaired.getpixel((4, 8)) == 255  # Missing torso touches the bottom.
    assert repaired.getpixel((0, 8)) == 17  # Arm gap outside ROI is untouched.
    assert repaired.getpixel((4, 3)) == 17  # Hair/face contour is untouched.
    assert alpha.getpixel((4, 8)) == 17  # Original predicted mask is unchanged.
    composite = portrait.composite(source, repaired, Image.new('RGB', alpha.size, 'red'),
                                   {'scale': 1, 'center_x_px': 4.5, 'center_y_px': 4.5})
    assert composite.getpixel((4, 8)) == source.getpixel((4, 8))


@pytest.mark.parametrize('polygon', [[], [[0, 0], [1, 1]], [[0, 0], [1, 1], [2, 1]],
                                    [[0, 0], [float('nan'), 1], [1, 1]],
                                    [[0, 0], [.5, .5], [1, 1]]])
def test_invalid_alpha_repair_regions_are_rejected(tmp_path, polygon):
    spec = job(tmp_path)
    spec['alpha_repair'] = {'kind': 'keep_source_polygons', 'polygons': [polygon]}
    with pytest.raises(ValueError, match='alpha_repair'):
        portrait.validate_spec(spec, 'sample')


def test_alpha_without_explicit_repair_is_unchanged():
    alpha = Image.new('L', (5, 7), 96)
    assert portrait.repair_alpha(alpha) is alpha
