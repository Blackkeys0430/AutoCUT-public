"""Axis-aligned normalized native material crop, shared by authoring and previews."""
import math


def crop_rectangle(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
            or not (0 <= value[0] < value[2] <= 1 and 0 <= value[1] < value[3] <= 1)):
        raise ValueError('source_crop requires [left, top, right, bottom] within 0..1 and nonempty')
    return list(value)


def native_crop(value):
    left, top, right, bottom = crop_rectangle(value)
    return dict(upper_left_x=left, upper_left_y=top, upper_right_x=right, upper_right_y=top,
                lower_left_x=left, lower_left_y=bottom, lower_right_x=right, lower_right_y=bottom)


def material_crop(material):
    crop = material.get('crop')
    if not crop:
        return [0, 0, 1, 1]
    try:
        value = crop_rectangle([crop['upper_left_x'], crop['upper_left_y'],
                                crop['lower_right_x'], crop['lower_right_y']])
        if crop != native_crop(value):
            raise ValueError('only axis-aligned native source_crop is supported')
        return value
    except (KeyError, TypeError) as exc:
        raise ValueError('invalid native source_crop') from exc


def require_content_region(source_crop, content_region):
    crop = crop_rectangle(source_crop)
    protected = crop_rectangle(content_region)
    if not (crop[0] <= protected[0] < protected[2] <= crop[2]
            and crop[1] <= protected[1] < protected[3] <= crop[3]):
        raise ValueError('source_crop cuts the required content_region')
    return protected


def fit_region(source_size, canvas_size, target_rect, *, source_crop=None, content_region=None):
    """Place a selected source region in a pixel rectangle using native min-fit.

    The source crop controls the view inside the window; clip controls its size
    and position on the canvas. No mask resource or tracking is fabricated.
    """
    for value in (source_size, canvas_size):
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in value)):
            raise ValueError('source_size and canvas_size require positive width/height')
    if (not isinstance(target_rect, (list, tuple)) or len(target_rect) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in target_rect)):
        raise ValueError('target_rect requires pixel [left, top, right, bottom]')
    x1, y1, x2, y2 = target_rect
    cw, ch = canvas_size
    if not (0 <= x1 < x2 <= cw and 0 <= y1 < y2 <= ch):
        raise ValueError('target_rect must be nonempty and inside the canvas')
    crop = crop_rectangle(source_crop if source_crop is not None else [0, 0, 1, 1])
    if content_region is not None:
        require_content_region(crop, content_region)
    width = source_size[0] * (crop[2] - crop[0])
    height = source_size[1] * (crop[3] - crop[1])
    # Preserve proportions, letterboxing inside the requested rectangle if needed.
    placed = min((x2-x1) / width, (y2-y1) / height)
    native_fit = min(cw / width, ch / height)
    scale = placed / native_fit
    center_x, center_y = (x1+x2)/2, (y1+y2)/2
    return {'source_crop': crop,
            'clip': {'alpha': 1.0, 'rotation': 0.0,
                     'scale': {'x': scale, 'y': scale},
                     'transform': {'x': 2*center_x/cw-1, 'y': 1-2*center_y/ch}},
            'placed_rect': [center_x-width*placed/2, center_y-height*placed/2,
                            center_x+width*placed/2, center_y+height*placed/2],
            'cropped_resolution': [width, height]}
