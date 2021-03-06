# coding: utf8
"""
    weasyprint.draw
    ---------------

    Take an "after layout" box tree and draw it onto a cairo surface.

    :copyright: Copyright 2011-2012 Simon Sapin and contributors, see AUTHORS.
    :license: BSD, see LICENSE for details.

"""

from __future__ import division, unicode_literals

import contextlib
import math
import operator

import cairo

from .formatting_structure import boxes
from .stacking import StackingContext
from .text import show_first_line
from .compat import xrange

# Map values of the image-rendering property to cairo FILTER values:
# Values are normalized to lower case.
IMAGE_RENDERING_TO_FILTER = dict(
    optimizespeed=cairo.FILTER_FAST,
    auto=cairo.FILTER_GOOD,
    optimizequality=cairo.FILTER_BEST,
)


def make_cairo_context(surface, enable_hinting, get_image_from_uri):
    """cairo.Context doesn’t like much when we override __init__,
    and overriding __new__ is ugly.

    """
    context = CairoContext(surface)
    context.enable_hinting = enable_hinting
    context.get_image_from_uri = get_image_from_uri
    return context


class CairoContext(cairo.Context):
    """A ``cairo.Context`` with a few more helper methods."""
    @contextlib.contextmanager
    def stacked(self):
        """Save and restore the context when used with the ``with`` keyword."""
        self.save()
        try:
            yield
        finally:
            self.restore()


def lighten(color, offset):
    """Return a lighter color (or darker, for negative offsets)."""
    return (
        color.red + offset,
        color.green + offset,
        color.blue + offset,
        color.alpha)


def draw_page(page, context):
    """Draw the given PageBox to a Cairo context.

    The context should be scaled so that lengths are in CSS pixels.

    """
    stacking_context = StackingContext.from_page(page)
    draw_box_background(
        context, stacking_context.page, stacking_context.box)
    draw_canvas_background(context, page)
    draw_border(context, page)
    draw_stacking_context(context, stacking_context)


def draw_box_background_and_border(context, page, box):
    draw_box_background(context, page, box)
    if not isinstance(box, boxes.TableBox):
        draw_border(context, box)
    else:
        for column_group in box.column_groups:
            draw_box_background(context, page, column_group)
            for column in column_group.children:
                draw_box_background(context, page, column)
        for row_group in box.children:
            draw_box_background(context, page, row_group)
            for row in row_group.children:
                draw_box_background(context, page, row)
                for cell in row.children:
                    draw_box_background(context, page, cell)
        if box.style.border_collapse == 'separate':
            draw_border(context, box)
            for row_group in box.children:
                for row in row_group.children:
                    for cell in row.children:
                        draw_border(context, cell)
        else:
            draw_collapsed_borders(context, box)


def draw_stacking_context(context, stacking_context):
    """Draw a ``stacking_context`` on ``context``."""
    # See http://www.w3.org/TR/CSS2/zindex.html
    with context.stacked():
        if stacking_context.box.style.clip:
            top, right, bottom, left = stacking_context.box.style.clip
            if top == 'auto':
                top = 0
            if right == 'auto':
                right = 0
            if bottom == 'auto':
                bottom = stacking_context.box.border_height()
            if left == 'auto':
                left = stacking_context.box.border_width()
            context.rectangle(
                stacking_context.box.border_box_x() + right,
                stacking_context.box.border_box_y() + top,
                left - right,
                bottom - top)
            context.clip()

        if stacking_context.box.style.overflow != 'visible':
            context.rectangle(
                *box_rectangle(stacking_context.box, 'padding-box'))
            context.clip()

        if stacking_context.box.style.opacity < 1:
            context.push_group()

        apply_2d_transforms(context, stacking_context.box)

        # Point 1 is done in draw_page

        # Point 2
        if isinstance(stacking_context.box,
                      (boxes.BlockBox, boxes.MarginBox, boxes.InlineBlockBox)):
            # The canvas background was removed by set_canvas_background
            draw_box_background_and_border(
                context, stacking_context.page, stacking_context.box)

        # Point 3
        for child_context in stacking_context.negative_z_contexts:
            draw_stacking_context(context, child_context)

        # Point 4
        for box in stacking_context.block_level_boxes:
            draw_box_background_and_border(
                context, stacking_context.page, box)

        # Point 5
        for child_context in stacking_context.float_contexts:
            draw_stacking_context(context, child_context)

        # Point 6
        if isinstance(stacking_context.box, boxes.InlineBox):
            draw_inline_level(
                context, stacking_context.page, stacking_context.box)

        # Point 7
        for box in [stacking_context.box] + stacking_context.blocks_and_cells:
            marker_box = getattr(box, 'outside_list_marker', None)
            if marker_box:
                draw_inline_level(
                    context, stacking_context.page, marker_box)

            if isinstance(box, boxes.ReplacedBox):
                draw_replacedbox(context, box)
            else:
                for child in box.children:
                    if isinstance(child, boxes.LineBox):
                        # TODO: draw inline tables
                        draw_inline_level(
                            context, stacking_context.page, child)

        # Point 8
        for child_context in stacking_context.zero_z_contexts:
            draw_stacking_context(context, child_context)

        # Point 9
        for child_context in stacking_context.positive_z_contexts:
            draw_stacking_context(context, child_context)

        if stacking_context.box.style.opacity < 1:
            context.pop_group_to_source()
            context.paint_with_alpha(stacking_context.box.style.opacity)


def box_rectangle(box, which_rectangle):
    if which_rectangle == 'border-box':
        return (
            box.border_box_x(),
            box.border_box_y(),
            box.border_width(),
            box.border_height(),
        )
    elif which_rectangle == 'padding-box':
        return (
            box.padding_box_x(),
            box.padding_box_y(),
            box.padding_width(),
            box.padding_height(),
        )
    else:
        assert which_rectangle == 'content-box', which_rectangle
        return (
            box.content_box_x(),
            box.content_box_y(),
            box.width,
            box.height,
        )


def background_positioning_area(page, box, style):
    if style.background_attachment == 'fixed' and box is not page:
        # Initial containing block
        return box_rectangle(page, 'content-box')
    else:
        return box_rectangle(box, box.style.background_origin)


def draw_canvas_background(context, page):
    if not page.children or isinstance(page.children[0], boxes.MarginBox):
        # Skip the canvas background on content-empty pages
        # TODO: remove this when content empty pages still get boxes
        # up to the break point, so that the backgrounds and borders are drawn.
        return
    root_box = page.children[0]
    style = root_box.canvas_background
    draw_background(context, style,
        painting_area=box_rectangle(page, 'padding-box'),
        positioning_area=background_positioning_area(page, root_box, style)
    )


def draw_box_background(context, page, box):
    """Draw the box background color and image to a ``cairo.Context``."""
    if box.style.visibility == 'hidden':
        return
    if box is page:
        painting_area = None
    else:
        painting_area = box_rectangle(box, box.style.background_clip)
    draw_background(
        context, box.style, painting_area,
        positioning_area=background_positioning_area(page, box, box.style))


def percentage(value, refer_to):
    """Return the evaluated percentage value, or the value unchanged."""
    if value.unit == 'px':
        return value.value
    else:
        assert value.unit == '%'
        return refer_to * value.value / 100


def draw_background(context, style, painting_area, positioning_area):
    """Draw the background color and image to a ``cairo.Context``."""
    bg_color = style.background_color
    bg_image = style.background_image
    if bg_image == 'none':
        image = None
    else:
        image = context.get_image_from_uri(bg_image)
    if bg_color.alpha == 0 and image is None:
        # No background.
        return

    with context.stacked():
        if context.enable_hinting:
            # Prefer crisp edges on background rectangles.
            context.set_antialias(cairo.ANTIALIAS_NONE)

        if painting_area:
            context.rectangle(*painting_area)
            context.clip()
        #else: unrestricted, whole page box

        # Background color
        if bg_color.alpha > 0:
            context.set_source_rgba(*bg_color)
            context.paint()

        # Background image
        if image is None:
            return

        (positioning_x, positioning_y,
            positioning_width, positioning_height) = positioning_area
        context.translate(positioning_x, positioning_y)

        pattern, intrinsic_width, intrinsic_height = image

        bg_size = style.background_size
        if bg_size in ('cover', 'contain'):
            scale_x = scale_y = {'cover': max, 'contain': min}[bg_size](
                positioning_width / intrinsic_width,
                positioning_height / intrinsic_height)
            image_width = intrinsic_width * scale_x
            image_height = intrinsic_height * scale_y
        elif bg_size == ('auto', 'auto'):
            scale_x = scale_y = 1
            image_width = intrinsic_width
            image_height = intrinsic_height
        elif bg_size[0] == 'auto':
            image_height = percentage(bg_size[1], positioning_height)
            scale_x = scale_y = image_height / intrinsic_height
            image_width = intrinsic_width * scale_x
        elif bg_size[1] == 'auto':
            image_width = percentage(bg_size[0], positioning_width)
            scale_x = scale_y = image_width / intrinsic_width
            image_height = intrinsic_height * scale_y
        else:
            image_width = percentage(bg_size[0], positioning_width)
            image_height = percentage(bg_size[1], positioning_height)
            scale_x = image_width / intrinsic_width
            scale_y = image_height / intrinsic_height

        bg_position_x, bg_position_y = style.background_position
        context.translate(
            percentage(bg_position_x, positioning_width - image_width),
            percentage(bg_position_y, positioning_height - image_height),
        )

        bg_repeat = style.background_repeat
        if bg_repeat in ('repeat-x', 'repeat-y'):
            # Get the current clip rectangle. This is the same as
            # painting_area, but in new coordinates after translate()
            clip_x1, clip_y1, clip_x2, clip_y2 = context.clip_extents()
            clip_width = clip_x2 - clip_x1
            clip_height = clip_y2 - clip_y1

            if bg_repeat == 'repeat-x':
                # Limit the drawn area vertically
                clip_y1 = 0  # because of the last context.translate()
                clip_height = image_height
            else:
                # repeat-y
                # Limit the drawn area horizontally
                clip_x1 = 0  # because of the last context.translate()
                clip_width = image_width

            # Second clip for the background image
            context.rectangle(clip_x1, clip_y1, clip_width, clip_height)
            context.clip()

        if bg_repeat == 'no-repeat':
            # The same image/pattern may have been used
            # in a repeating background.
            pattern.set_extend(cairo.EXTEND_NONE)
        else:
            pattern.set_extend(cairo.EXTEND_REPEAT)
        # TODO: de-duplicate this with draw_replacedbox()
        pattern.set_filter(IMAGE_RENDERING_TO_FILTER[style.image_rendering])
        context.scale(scale_x, scale_y)
        context.set_source(pattern)
        context.paint()


def get_rectangle_edges(x, y, width, height):
    """Return the 4 edges of a rectangle as a list.

    Edges are in clock-wise order, starting from the top.

    Each edge is returned as ``(start_point, end_point)`` and each point
    as ``(x, y)`` coordinates.

    """
    # In clock-wise order, starting on top left
    corners = [
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height)]
    # clock-wise order, starting on top right
    shifted_corners = corners[1:] + corners[:1]
    return zip(corners, shifted_corners)


def xy_offset(x, y, offset_x, offset_y, offset):
    """Increment X and Y coordinates by the given offsets."""
    return x + offset_x * offset, y + offset_y * offset


def draw_border(context, box):
    """Draw the box border to a ``cairo.Context``."""
    if box.style.visibility == 'hidden':
        return
    if all(getattr(box, 'border_%s_width' % side) == 0
           for side in ['top', 'right', 'bottom', 'left']):
        # No border, return early.
        return

    for side, border_edge, padding_edge in zip(
        ['top', 'right', 'bottom', 'left'],
        get_rectangle_edges(
            box.border_box_x(), box.border_box_y(),
            box.border_width(), box.border_height(),
        ),
        get_rectangle_edges(
            box.padding_box_x(), box.padding_box_y(),
            box.padding_width(), box.padding_height(),
        ),
    ):
        width = getattr(box, 'border_%s_width' % side)
        if width == 0:
            continue
        color = box.style['border_%s_color' % side]
        if color.alpha == 0:
            continue
        style = box.style['border_%s_style' % side]
        draw_border_segment(context, style, width, color,
                            side, border_edge, padding_edge)


def draw_border_segment(context, style, width, color, side,
                        border_edge, padding_edge):
    with context.stacked():
        context.set_source_rgba(*color)
        x_offset, y_offset = {
            'top': (0, 1), 'bottom': (0, -1), 'left': (1, 0), 'right': (-1, 0),
        }[side]

        if context.enable_hinting and (
                # Borders smaller than 1 device unit would disappear
                # without anti-aliasing.
                math.hypot(*context.user_to_device(width, 0)) >= 1 and
                math.hypot(*context.user_to_device(0, width)) >= 1):
            # Avoid an artefact in the corner joining two solid borders
            # of the same color.
            context.set_antialias(cairo.ANTIALIAS_NONE)

        if style not in ('dotted', 'dashed'):
            # Clip on the trapezoid shape
            """
            Clip on the trapezoid formed by the border edge (longer)
            and the padding edge (shorter).

            This is on the top side:

              +---------------+    <= border edge      ^
               \             /                         |
                \           /                          |  top border width
                 \         /                           |
                  +-------+        <= padding edge     v

              <-->         <-->    <=  left and right border widths

            """
            border_start, border_stop = border_edge
            padding_start, padding_stop = padding_edge
            context.move_to(*border_start)
            for point in [border_stop, padding_stop,
                          padding_start, border_start]:
                context.line_to(*point)
            context.clip()

        if style == 'solid':
            # Fill the whole trapezoid
            context.paint()
        elif style in ('inset', 'outset'):
            do_lighten = (side in ('top', 'left')) ^ (style == 'inset')
            factor = 1 if do_lighten else -1
            context.set_source_rgba(*lighten(color, 0.5 * factor))
            context.paint()
        elif style in ('groove', 'ridge'):
            # TODO: these would look better with more color stops
            """
            Divide the width in 2 and stroke lines in different colors
              +-------------+
              1\           /2
              1'\         / 2'
                 +-------+
            """
            do_lighten = (side in ('top', 'left')) ^ (style == 'groove')
            factor = 1 if do_lighten else -1
            context.set_line_width(width / 2)
            (x1, y1), (x2, y2) = border_edge
            # from the border edge to the center of the first line
            x1, y1 = xy_offset(x1, y1, x_offset, y_offset, width / 4)
            x2, y2 = xy_offset(x2, y2, x_offset, y_offset, width / 4)
            context.move_to(x1, y1)
            context.line_to(x2, y2)
            context.set_source_rgba(*lighten(color, 0.5 * factor))
            context.stroke()
            # Between the centers of both lines. 1/4 + 1/4 = 1/2
            x1, y1 = xy_offset(x1, y1, x_offset, y_offset, width / 2)
            x2, y2 = xy_offset(x2, y2, x_offset, y_offset, width / 2)
            context.move_to(x1, y1)
            context.line_to(x2, y2)
            context.set_source_rgba(*lighten(color, -0.5 * factor))
            context.stroke()
        elif style == 'double':
            """
            Divide the width in 3 and stroke both outer lines
              +---------------+
              1\             /2
                \           /
              1' \         /  2'
                  +-------+
            """
            context.set_line_width(width / 3)
            (x1, y1), (x2, y2) = border_edge
            # from the border edge to the center of the first line
            x1, y1 = xy_offset(x1, y1, x_offset, y_offset, width / 6)
            x2, y2 = xy_offset(x2, y2, x_offset, y_offset, width / 6)
            context.move_to(x1, y1)
            context.line_to(x2, y2)
            context.stroke()
            # Between the centers of both lines. 1/6 + 1/3 + 1/6 = 2/3
            x1, y1 = xy_offset(x1, y1, x_offset, y_offset, 2 * width / 3)
            x2, y2 = xy_offset(x2, y2, x_offset, y_offset, 2 * width / 3)
            context.move_to(x1, y1)
            context.line_to(x2, y2)
            context.stroke()
        else:
            assert style in ('dotted', 'dashed')
            (x1, y1), (x2, y2) = border_edge
            if style == 'dotted':
                # Half-way from the extremities of the border and padding
                # edges.
                (px1, py1), (px2, py2) = padding_edge
                x1 = (x1 + px1) / 2
                x2 = (x2 + px2) / 2
                y1 = (y1 + py1) / 2
                y2 = (y2 + py2) / 2
                """
                  +---------------+
                   \             /
                    1           2
                     \         /
                      +-------+
                """
            else:  # dashed
                # From the border edge to the middle:
                x1, y1 = xy_offset(x1, y1, x_offset, y_offset, width / 2)
                x2, y2 = xy_offset(x2, y2, x_offset, y_offset, width / 2)
                """
                  +---------------+
                   \             /
                  1 \           / 2
                     \         /
                      +-------+
                """
            length = ((x2 - x1)**2 + (y2 - y1)**2) ** 0.5
            dash = 2 * width
            if style == 'dotted':
                if context.user_to_device_distance(width, 0)[0] > 3:
                    # Round so that dash is a divisor of length,
                    # but not in the dots are too small.
                    dash = length / round(length / dash)
                context.set_line_cap(cairo.LINE_CAP_ROUND)
                context.set_dash([0, dash])
            else:  # dashed
                # Round so that 2*dash is a divisor of length
                dash = length / (2 * round(length / (2 * dash)))
                context.set_dash([dash])
            # Stroke along the line in === above, as wide as the border
            context.move_to(x1, y1)
            context.line_to(x2, y2)
            context.set_line_width(width)
            context.stroke()


def draw_collapsed_borders(context, table):
    row_heights = [row.height for row_group in table.children
                              for row in row_group.children]
    column_widths = table.column_widths
    if not (row_heights and column_widths):
        # One of the list is empty: don’t bother with empty tables
        return
    row_positions = [row.position_y for row_group in table.children
                                    for row in row_group.children]
    column_positions = table.column_positions
    grid_height = len(row_heights)
    grid_width = len(column_widths)
    assert grid_width == len(column_positions)
    # Add the end of the last column, but make a copy from the table attr.
    column_positions += [column_positions[-1] + column_widths[-1]]
    # Add the end of the last row. No copy here, we own this list
    row_positions.append(row_positions[-1] + row_heights[-1])
    vertical_borders, horizontal_borders = table.collapsed_border_grid
    skipped_rows = table.skipped_rows

    segments = []

    def half_max_width(border_list, yx_pairs, vertical=True):
        result = 0
        for y, x in yx_pairs:
            if (
                (0 <= y < grid_height and 0 <= x <= grid_width)
                if vertical else
                (0 <= y <= grid_height and 0 <= x < grid_width)
            ):
                _, (_, width, _) = border_list[skipped_rows + y][x]
                result = max(result, width)
        return result / 2

    def add_vertical(x, y):
        score, (style, width, color) = vertical_borders[skipped_rows + y][x]
        if width == 0 or color.alpha == 0:
            return
        half_width = width / 2
        pos_x = column_positions[x]
        pos_y_1 = row_positions[y] - half_max_width(horizontal_borders, [
            (y, x - 1), (y, x)], vertical=False)
        pos_y_2 = row_positions[y + 1] + half_max_width(horizontal_borders, [
            (y + 1, x - 1), (y + 1, x)], vertical=False)
        edge_1 = (pos_x - half_width, pos_y_1), (pos_x - half_width, pos_y_2)
        edge_2 = (pos_x + half_width, pos_y_1), (pos_x + half_width, pos_y_2)
        segments.append((score, style, width, color, 'left', edge_1, edge_2))

    def add_horizontal(x, y):
        score, (style, width, color) = horizontal_borders[skipped_rows + y][x]
        if width == 0 or color.alpha == 0:
            return
        half_width = width / 2
        pos_y = row_positions[y]
        # TODO: change signs for rtl when we support rtl tables?
        pos_x_1 = column_positions[x] - half_max_width(vertical_borders, [
            (y - 1, x), (y, x)])
        pos_x_2 = column_positions[x + 1] + half_max_width(vertical_borders, [
            (y - 1, x + 1), (y, x + 1)])
        edge_1 = (pos_x_1, pos_y - half_width), (pos_x_2, pos_y - half_width)
        edge_2 = (pos_x_1, pos_y + half_width), (pos_x_2, pos_y + half_width)
        segments.append((score, style, width, color, 'top', edge_1, edge_2))

    for x in xrange(grid_width):
        add_horizontal(x, 0)
    for y in xrange(grid_height):
        add_vertical(0, y)
        for x in xrange(grid_width):
            add_vertical(x + 1, y)
            add_horizontal(x, y + 1)

    # Sort bigger scores last (painted later, on top)
    # Since the number of different scores is expected to be small compared
    # to the number of segments, there should be little changes and Timsort
    # should be closer to O(n) than O(n * log(n))
    segments.sort(key=operator.itemgetter(0))

    for segment in segments:
        draw_border_segment(context, *segment[1:])

def draw_replacedbox(context, box):
    """Draw the given :class:`boxes.ReplacedBox` to a ``cairo.context``."""
    if box.style.visibility == 'hidden':
        return

    x, y = box.content_box_x(), box.content_box_y()
    width, height = box.width, box.height
    pattern, intrinsic_width, intrinsic_height = box.replacement
    scale_width = width / intrinsic_width
    scale_height = height / intrinsic_height
    # Draw nothing for width:0 or height:0
    if scale_width != 0 and scale_height != 0:
        with context.stacked():
            context.translate(x, y)
            context.rectangle(0, 0, width, height)
            context.clip()
            context.scale(scale_width, scale_height)
            # The same image/pattern may have been used in a
            # repeating background.
            pattern.set_extend(cairo.EXTEND_NONE)
            pattern.set_filter(IMAGE_RENDERING_TO_FILTER[
                box.style.image_rendering])
            context.set_source(pattern)
            context.paint()
    # Make sure `pattern` is garbage collected. If a surface for a SVG image
    # is still alive by the time we call show_page(), cairo will rasterize
    # the image instead writing vectors.
    # Use a unique string that can be traced back here.
    # Replaced boxes are atomic, so they should only ever be drawn once.
    # Use an object incompatible with the usual 3-tuple to cause an exception
    # if this box is used more than that.
    box.replacement = 'Removed to work around cairo’s behavior'


def draw_inline_level(context, page, box):
    if isinstance(box, StackingContext):
        stacking_context = box
        assert isinstance(stacking_context.box, boxes.InlineBlockBox)
        draw_stacking_context(context, stacking_context)
    else:
        draw_box_background(context, page, box)
        draw_border(context, box)
        if isinstance(box, (boxes.InlineBox, boxes.LineBox)):
            for child in box.children:
                if isinstance(child, boxes.TextBox):
                    draw_text(context, child)
                else:
                    draw_inline_level(context, page, child)
        elif isinstance(box, boxes.InlineReplacedBox):
            draw_replacedbox(context, box)
        else:
            assert isinstance(box, boxes.TextBox)
            # Should only happen for list markers
            draw_text(context, box)


def draw_text(context, textbox):
    """Draw ``textbox`` to a ``cairo.Context`` from ``PangoCairo.Context``."""
    # Pango crashes with font-size: 0
    assert textbox.style.font_size

    if textbox.style.visibility == 'hidden':
        return

    context.move_to(textbox.position_x, textbox.position_y + textbox.baseline)
    context.set_source_rgba(*textbox.style.color)
    show_first_line(context, textbox.pango_layout, context.enable_hinting)
    values = textbox.style.text_decoration
    if 'overline' in values:
        draw_text_decoration(context, textbox,
            textbox.baseline - 0.15 * textbox.style.font_size)
    elif 'underline' in values:
        draw_text_decoration(context, textbox,
            textbox.baseline + 0.15 * textbox.style.font_size)
    elif 'line-through' in values:
        draw_text_decoration(context, textbox, textbox.height * 0.5)


def draw_text_decoration(context, textbox, offset_y):
    """Draw text-decoration of ``textbox`` to a ``cairo.Context``."""
    with context.stacked():
        if context.enable_hinting:
            context.set_antialias(cairo.ANTIALIAS_NONE)
        context.set_source_rgba(*textbox.style.color)
        context.set_line_width(1)  # TODO: make this proportional to font_size?
        context.move_to(textbox.position_x, textbox.position_y + offset_y)
        context.rel_line_to(textbox.width, 0)
        context.stroke()


def apply_2d_transforms(context, box):
    # "Transforms apply to block-level and atomic inline-level elements,
    #  but do not apply to elements which may be split into
    #  multiple inline-level boxes."
    # http://www.w3.org/TR/css3-2d-transforms/#introduction
    if box.style.transform and not isinstance(box, boxes.InlineBox):
        border_width = box.border_width()
        border_height = box.border_height()
        origin_x, origin_y = box.style.transform_origin
        origin_x = percentage(origin_x, border_width)
        origin_y = percentage(origin_y, border_height)
        origin_x += box.border_box_x()
        origin_y += box.border_box_y()

        context.translate(origin_x, origin_y)
        for name, args in box.style.transform:
            if name == 'scale':
                context.scale(*args)
            elif name == 'rotate':
                context.rotate(args)
            elif name == 'translate':
                translate_x, translate_y = args
                context.translate(
                    percentage(translate_x, border_width),
                    percentage(translate_y, border_height),
                )
            else:
                if name == 'skewx':
                    args = (1, 0, math.tan(args), 1, 0, 0)
                elif name == 'skewy':
                    args = (1, math.tan(args), 0, 1, 0, 0)
                else:
                    assert name == 'matrix'
                context.transform(cairo.Matrix(*args))
        context.translate(-origin_x, -origin_y)
