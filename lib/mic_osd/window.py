"""
GTK4 Layer Shell window for mic-osd.

Creates an overlay window at the bottom center of the screen
for displaying audio visualizations.
"""

from __future__ import annotations

import math
import time

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
import cairo

try:
    gi.require_version('Gtk4LayerShell', '1.0')
    LAYER_SHELL_AVAILABLE = True
except ValueError:
    LAYER_SHELL_AVAILABLE = False

from gi.repository import Gtk, Gdk, GLib
from .theme import theme
from .osd_config import MicOSDConfig
from .transcript_preview import PillTranscriptAnimator, PillTranscriptConfig
from .transcript_stability import TranscriptChangeTracker

if LAYER_SHELL_AVAILABLE:
    from gi.repository import Gtk4LayerShell


class OSDWindow(Gtk.Window):
    """
    An overlay window for displaying audio visualizations.
    
    Uses gtk4-layer-shell to create a Wayland layer surface
    that appears above all windows at the bottom of the screen.
    """

    PREVIEW_WORD_LIMIT = 14
    PREVIEW_TIMER_RESERVE = 58
    
    def __init__(
        self,
        visualization,
        width=300,
        height=60,
        transcript_config=None,
        osd_config=None,
    ):
        """
        Initialize the OSD window.
        
        Args:
            visualization: A BaseVisualization instance
            width: Window width in pixels
            height: Window height in pixels
            transcript_config: Validated PillTranscriptConfig for pill previews.
            osd_config: Validated general OSD layout/response settings.
        """
        super().__init__()

        self.visualization = visualization
        self._osd_config = osd_config or MicOSDConfig.load()
        self._is_pill_preview = getattr(visualization, 'preview_mode', None) == 'pill'
        self._is_dynamic_preview = bool(
            getattr(visualization, 'dynamic_preview', False)
        )
        self._pill_transcript_config = (
            transcript_config
            if transcript_config is not None
            else PillTranscriptConfig.load()
        )
        if self._is_pill_preview and self._pill_transcript_config.enabled:
            height = max(
                height,
                int(
                    getattr(self.visualization, 'PILL_HEIGHT', 42)
                    + self._pill_transcript_config.font_size
                    + self._pill_transcript_config.offset_y
                    + 16
                ),
                # Must clear the pill's own preview-mode threshold.
                getattr(self.visualization, 'PREVIEW_HEIGHT_THRESHOLD', 76) + 1,
            )
            width = max(
                width,
                int(min(800.0, self._pill_transcript_config.max_width + 24.0)),
            )

        self._width = width
        self._height = height
        self._meter_height = height
        self._preview_text = ""
        self._preview_committed_length = None
        self._stability_tracker = None
        self._visualizer_state = "recording"
        self._pill_transcript_animator = None
        if self._is_pill_preview:
            self._pill_transcript_animator = PillTranscriptAnimator(
                self._pill_transcript_config
            )
        
        # Layer shell MUST be initialized immediately after window creation
        # and BEFORE any other window configuration
        self._setup_layer_shell()
        self._setup_window()
        self._setup_drawing_area()
    
    def _setup_layer_shell(self):
        """Configure layer shell behavior."""
        if not LAYER_SHELL_AVAILABLE:
            return
        
        # Initialize layer shell - MUST be initialized before other calls
        Gtk4LayerShell.init_for_window(self)
        
        # Set namespace for window rules
        Gtk4LayerShell.set_namespace(self, "mic-osd")
        
        # Put on overlay layer (above everything)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        
        # Anchor to bottom only (centers horizontally)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.BOTTOM, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.LEFT, False)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, False)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, False)
        
        # Margin from bottom
        Gtk4LayerShell.set_margin(
            self,
            Gtk4LayerShell.Edge.BOTTOM,
            self._osd_config.bottom_margin,
        )
        
        # Don't reserve exclusive space
        Gtk4LayerShell.set_exclusive_zone(self, -1)
        
        # No keyboard input
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.NONE)
    
    def _setup_window(self):
        """Configure basic window properties."""
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_default_size(self._width, self._height)
        
        # Make window transparent
        self.add_css_class('mic-osd-window')
    
    def _setup_drawing_area(self):
        """Set up the Cairo drawing area."""
        self.drawing_area = Gtk.DrawingArea()
        self.drawing_area.set_content_width(self._width)
        self.drawing_area.set_content_height(self._height)
        self.drawing_area.set_draw_func(self._on_draw)
        self.set_child(self.drawing_area)
    
    def _on_draw(self, area, cr, width, height):
        """
        Called when the drawing area needs to be redrawn.
        
        Args:
            area: The DrawingArea widget
            cr: Cairo context
            width: Available width
            height: Available height
        """
        # Draw background
        self.visualization.draw_background(cr, width, height)
        
        # Dynamic spectrum previews reserve a compact, stable meter region and
        # let only the transcript grow upward.  A layer surface can change its
        # natural size at runtime; no compositor-specific window rules or
        # Quickshell bridge are required.
        visualization_height = (
            min(height, self._meter_height)
            if self._is_dynamic_preview
            else height
        )
        self.visualization.draw(cr, width, visualization_height)

        if self._is_dynamic_preview:
            self._draw_dynamic_preview_text(cr, width)
        elif self._is_pill_preview:
            self._draw_pill_preview_text(cr, width, height)
        elif getattr(self.visualization, 'show_preview', True):
            self._draw_preview_text(cr, width, height)
    
    def update(self, level: float, samples=None):
        """
        Update the visualization with new audio data.
        
        Args:
            level: Audio level (0.0 to 1.0)
            samples: Raw audio samples (optional)
        """
        self.visualization.update(level, samples)
        self.drawing_area.queue_draw()

    def set_preview_text(self, text: str, committed_length=None):
        """Set compact transcript preview text.

        ``committed_length`` is the number of leading characters the backend
        has finalized, when known; the spectrum preview styles the rest as
        provisional.
        """
        self._preview_text = (text or "").rstrip('\r\n')
        self._preview_committed_length = (
            min(committed_length, len(self._preview_text))
            if isinstance(committed_length, int)
            else None
        )
        if self._pill_transcript_animator is not None:
            self._pill_transcript_animator.set_text(self._preview_text)
        self.drawing_area.queue_draw()

    def set_visualizer_state(self, state: str):
        """Track visualizer state so partial previews only render while recording."""
        previous_state = self._visualizer_state
        self._visualizer_state = (state or "recording").lower()
        if (
            self._pill_transcript_animator is not None
            and previous_state == "recording"
            and self._visualizer_state != "recording"
        ):
            self._pill_transcript_animator.clear()
        self.drawing_area.queue_draw()
    
    def apply_osd_config(self, osd_config: MicOSDConfig):
        """Apply live presentation settings to the mapped layer surface."""
        self._osd_config = osd_config
        if LAYER_SHELL_AVAILABLE:
            Gtk4LayerShell.set_margin(
                self,
                Gtk4LayerShell.Edge.BOTTOM,
                osd_config.bottom_margin,
            )

        tracker = getattr(self, '_stability_tracker', None)
        if tracker is not None:
            tracker.configure(
                osd_config.transcript_change_highlight_ms / 1000.0,
                osd_config.transcript_stability_cues,
            )

        if self._is_dynamic_preview:
            self._width = osd_config.spectrum_width
            self._meter_height = osd_config.spectrum_meter_height
            self.drawing_area.set_content_width(self._width)
            if not self._preview_text or self._visualizer_state != "recording":
                self._set_dynamic_height(self._meter_height)
            self.set_default_size(self._width, self._height)
            self.drawing_area.queue_resize()
        self.drawing_area.queue_draw()

    def _set_dynamic_height(self, target_height: int):
        """Update the GTK natural size; the bottom-anchored surface grows up."""
        target_height = max(self._meter_height, int(target_height))
        if target_height == self._height:
            return
        self._height = target_height
        self.drawing_area.set_content_height(target_height)
        self.set_default_size(self._width, target_height)
        self.drawing_area.queue_resize()

    def _wrap_preview_lines(
        self,
        cr: cairo.Context,
        text: str,
        max_width: float,
    ):
        """Greedy word wrapping with explicit paragraph and long-token support."""
        if not text or max_width <= 0:
            return []

        lines = []
        paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        for paragraph in paragraphs:
            words = paragraph.split()
            if not words:
                if lines and lines[-1] != "":
                    lines.append("")
                continue

            current = ""
            for word in words:
                candidate = f"{current} {word}" if current else word
                if self._text_advance(cr, candidate) <= max_width:
                    current = candidate
                    continue

                if current:
                    lines.append(current)
                    current = ""

                remainder = word
                while remainder and self._text_advance(cr, remainder) > max_width:
                    count = self._bisect_truncate(
                        lambda n: self._text_advance(cr, remainder[:n]),
                        max_width,
                        len(remainder),
                    )
                    count = max(1, count)
                    lines.append(remainder[:count])
                    remainder = remainder[count:]
                current = remainder

            if current:
                lines.append(current)

        while lines and lines[-1] == "":
            lines.pop()
        return lines

    def _font_metrics(self, cr: cairo.Context):
        font_size = self._osd_config.transcript_font_size
        try:
            extents = cr.font_extents()
            ascent = self._text_extent(extents, 'ascent', 0)
            natural_height = self._text_extent(extents, 'height', 2)
        except (AttributeError, TypeError, IndexError):
            ascent = font_size
            natural_height = font_size
        line_height = max(font_size, natural_height) * (
            self._osd_config.transcript_line_spacing
        )
        return ascent, line_height

    def _preview_height_limit(self):
        """Bound the growing preview in logical pixels on its actual monitor.

        Leave desktop space visible and account for the bottom anchor margin.
        Before the surface is mapped, use a conservative fallback.
        """
        try:
            display = self.get_display()
            surface = self.get_surface()
            monitor = display.get_monitor_at_surface(surface) if surface else None
            if monitor is not None:
                height = monitor.get_geometry().height
                return max(
                    self._meter_height,
                    min(int(height * 0.6), height - self._osd_config.bottom_margin - 24),
                )
        except (AttributeError, TypeError):
            pass
        return max(self._meter_height, 720)

    # -- spectrum transcript: stability-aware layout and painting ----------

    # Provisional text keeps this fraction of the configured opacity.
    LIVE_TEXT_ALPHA = 0.72
    # How far provisional text is tinted toward the theme's transcript-live.
    LIVE_TINT = 0.45
    # Left-gutter marker drawn beside lines holding a highlighted revision.
    CHANGE_GUTTER_X = 8.0
    CHANGE_GUTTER_WIDTH = 3.0

    def _transcript_tracker(self) -> TranscriptChangeTracker:
        tracker = getattr(self, '_stability_tracker', None)
        if tracker is None:
            config = self._osd_config
            tracker = TranscriptChangeTracker(
                highlight_seconds=config.transcript_change_highlight_ms / 1000.0,
                enabled=config.transcript_stability_cues,
            )
            self._stability_tracker = tracker
        return tracker

    def _layout_preview_cues(self, cr: cairo.Context, cues, max_width: float):
        """Greedy-wrap word cues into lines of ``(cue_index, x, text)`` pieces.

        Mirrors ``_wrap_preview_lines`` (paragraph breaks, long-token
        splitting) but keeps each word's identity and x offset so words can be
        styled individually.  Empty lists are blank paragraph lines.
        """
        if not cues or max_width <= 0:
            return []

        lines = []
        current = []
        current_text = ""

        for index, cue in enumerate(cues):
            if cue.breaks_before:
                if current:
                    lines.append(current)
                    current, current_text = [], ""
                if cue.breaks_before >= 2 and lines and lines[-1]:
                    lines.append([])

            word = cue.text
            candidate = f"{current_text} {word}" if current_text else word
            if self._text_advance(cr, candidate) <= max_width:
                x = (
                    self._text_advance(cr, current_text + " ")
                    if current_text
                    else 0.0
                )
                current.append((index, x, word))
                current_text = candidate
                continue

            if current:
                lines.append(current)
                current, current_text = [], ""

            remainder = word
            while remainder and self._text_advance(cr, remainder) > max_width:
                count = self._bisect_truncate(
                    lambda n: self._text_advance(cr, remainder[:n]),
                    max_width,
                    len(remainder),
                )
                count = max(1, count)
                lines.append([(index, 0.0, remainder[:count])])
                remainder = remainder[count:]
            if remainder:
                current = [(index, 0.0, remainder)]
                current_text = remainder

        if current:
            lines.append(current)
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def _prefix_truncated_line(self, cr: cairo.Context, line, max_width: float):
        """Prefix the oldest visible line with an ellipsis, dropping its
        leading words if needed so the line still fits."""
        prefix = "… "
        if not line:
            return line
        pieces = list(line)
        while (
            len(pieces) > 1
            and self._text_advance(
                cr, prefix + " ".join(piece[2] for piece in pieces)
            ) > max_width
        ):
            pieces.pop(0)

        rebuilt = []
        text_so_far = prefix
        for position, (index, _x, text) in enumerate(pieces):
            if position == 0:
                rebuilt.append((index, 0.0, prefix + text))
                text_so_far += text
            else:
                x = self._text_advance(cr, text_so_far + " ")
                rebuilt.append((index, x, text))
                text_so_far += " " + text
        return rebuilt

    @staticmethod
    def _cue_style_key(cue):
        # Quantize so runs of similarly styled words are painted together.
        return (
            round(cue.settled * 16) / 16.0,
            round(cue.highlight * 32) / 32.0,
        )

    def _line_runs(self, line, cues):
        """Group a line's pieces into maximal runs sharing one style."""
        runs = []
        for index, x, text in line:
            key = self._cue_style_key(cues[index])
            if runs and runs[-1]["key"] == key:
                runs[-1]["texts"].append(text)
            else:
                runs.append({"key": key, "x": x, "texts": [text]})
        for run in runs:
            run["text"] = " ".join(run["texts"])
        return runs

    @staticmethod
    def _mix(a, b, amount):
        return tuple(a[i] + (b[i] - a[i]) * amount for i in range(3))

    @staticmethod
    def _rounded_rect(cr: cairo.Context, x, y, w, h, r):
        r = max(0.0, min(r, w / 2.0, h / 2.0))
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2.0, 0.0)
        cr.arc(x + w - r, y + h - r, r, 0.0, math.pi / 2.0)
        cr.arc(x + r, y + h - r, r, math.pi / 2.0, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()

    def _draw_dynamic_preview_text(self, cr: cairo.Context, width: int):
        config = self._osd_config
        tracker = self._transcript_tracker()
        if not self._preview_text or self._visualizer_state != "recording":
            tracker.reset()
            self._set_dynamic_height(self._meter_height)
            return

        # Diff against the previous frame's text (a no-op when unchanged).
        tracker.update(
            self._preview_text,
            getattr(self, '_preview_committed_length', None),
        )
        cues = tracker.cues()

        padding_x = 20.0
        padding_bottom = 14.0
        gap = 2.0
        max_width = max(0.0, width - padding_x * 2.0)
        cr.select_font_face(
            config.transcript_font_family,
            cairo.FONT_SLANT_NORMAL,
            cairo.FONT_WEIGHT_NORMAL,
        )
        cr.set_font_size(config.transcript_font_size)
        lines = self._layout_preview_cues(cr, cues, max_width)
        if not lines:
            self._set_dynamic_height(self._meter_height)
            return

        ascent, line_height = self._font_metrics(cr)
        text_top = self._meter_height + gap
        available = self._preview_height_limit() - text_top - padding_bottom
        visible_lines = max(1, int(available // line_height))
        if len(lines) > visible_lines:
            lines = lines[-visible_lines:]
            lines[0] = self._prefix_truncated_line(cr, lines[0], max_width)
        target_height = int(
            math.ceil(text_top + line_height * len(lines) + padding_bottom)
        )
        self._set_dynamic_height(target_height)

        text_color = theme.text
        theme_alpha = text_color[3] if len(text_color) > 3 else 1.0
        base_alpha = config.transcript_opacity * theme_alpha
        final_rgb = tuple(text_color[:3])
        live_rgb = self._mix(final_rgb, theme.transcript_live, self.LIVE_TINT)
        change_rgb = theme.transcript_change

        laid_out = []
        for index, line in enumerate(lines):
            if not line:
                continue
            baseline = text_top + ascent + index * line_height
            laid_out.append((baseline, self._line_runs(line, cues)))

        # Pass 1, behind the text: highlight revised words and mark their
        # lines in the left gutter so distant rewrites catch peripheral vision.
        for baseline, runs in laid_out:
            line_highlight = 0.0
            for run in runs:
                highlight = run["key"][1]
                if highlight <= 0.0:
                    continue
                line_highlight = max(line_highlight, highlight)
                run_width = self._text_advance(cr, run["text"])
                x = padding_x + run["x"]
                cr.set_source_rgba(*change_rgb, 0.22 * highlight * base_alpha)
                self._rounded_rect(
                    cr,
                    x - 4.0,
                    baseline - ascent * 1.05,
                    run_width + 8.0,
                    ascent * 1.4,
                    5.0,
                )
                cr.fill()
                cr.set_source_rgba(*change_rgb, 0.85 * highlight * base_alpha)
                cr.rectangle(x, baseline + ascent * 0.22, run_width, 2.0)
                cr.fill()
            if line_highlight > 0.0:
                cr.set_source_rgba(*change_rgb, 0.9 * line_highlight * base_alpha)
                self._rounded_rect(
                    cr,
                    self.CHANGE_GUTTER_X,
                    baseline - ascent,
                    self.CHANGE_GUTTER_WIDTH,
                    ascent * 1.25,
                    self.CHANGE_GUTTER_WIDTH / 2.0,
                )
                cr.fill()

        # Pass 2: text.  Provisional words are dimmed and tinted toward
        # transcript-live and ease to the final colour when confirmed; revised
        # words take on transcript-change while their highlight decays.
        for baseline, runs in laid_out:
            for run in runs:
                settled, highlight = run["key"]
                rgb = self._mix(live_rgb, final_rgb, settled)
                alpha = base_alpha * (
                    self.LIVE_TEXT_ALPHA + (1.0 - self.LIVE_TEXT_ALPHA) * settled
                )
                if highlight > 0.0:
                    rgb = self._mix(rgb, change_rgb, 0.65 * highlight)
                    alpha += (base_alpha - alpha) * highlight
                x = padding_x + run["x"]
                cr.set_source_rgba(0.0, 0.0, 0.0, 0.52 * alpha)
                cr.move_to(x, baseline + 2.0)
                cr.show_text(run["text"])
                cr.set_source_rgba(rgb[0], rgb[1], rgb[2], alpha)
                cr.move_to(x, baseline)
                cr.show_text(run["text"])

    def _draw_preview_text(self, cr: cairo.Context, width: int, height: int):
        if not self._preview_text or self._visualizer_state != "recording":
            return

        padding = 14
        max_width = max(0, width - padding * 2 - self.PREVIEW_TIMER_RESERVE)

        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(12)
        text = self._ellipsize(cr, self._recent_preview_text(self._preview_text), max_width)
        if not text:
            return

        extents = cr.text_extents(text)
        text_width = self._text_extent(extents, 'width', 2)
        text_height = self._text_extent(extents, 'height', 3)

        y = height - 10
        bg_padding_x = 7
        bg_padding_y = 4
        bg_x = padding - bg_padding_x
        bg_y = y - text_height - bg_padding_y - 1
        bg_w = min(max_width + bg_padding_x * 2, text_width + bg_padding_x * 2)
        bg_h = text_height + bg_padding_y * 2 + 2

        bg = theme.background
        cr.set_source_rgba(bg[0], bg[1], bg[2], 0.88)
        cr.rectangle(bg_x, bg_y, bg_w, bg_h)
        cr.fill()

        text_color = theme.text
        cr.set_source_rgba(text_color[0], text_color[1], text_color[2], 0.96)
        cr.move_to(padding, y)
        cr.show_text(text)

    def _draw_pill_preview_text(
        self,
        cr: cairo.Context,
        width: int,
        height: int,
    ):
        animator = self._pill_transcript_animator
        config = self._pill_transcript_config
        if (
            animator is None
            or not config.enabled
            or self._visualizer_state != "recording"
        ):
            return

        frame = animator.frame(time.monotonic())
        if not frame.words:
            return

        cr.select_font_face(
            config.font_family,
            cairo.FONT_SLANT_NORMAL,
            cairo.FONT_WEIGHT_NORMAL,
        )

        max_width = min(config.max_width, max(0.0, width - 20.0))
        effective_font_size = config.font_size
        cr.set_font_size(effective_font_size)
        previous_texts = frame.previous_words
        current_texts = frame.current_words
        _, previous_total = self._word_layout(cr, previous_texts, width)
        _, current_total = self._word_layout(cr, current_texts, width)

        largest_total = max(previous_total, current_total)
        if largest_total > max_width and largest_total > 0:
            effective_font_size = max(
                8.0,
                effective_font_size * max_width / largest_total,
            )
            cr.set_font_size(effective_font_size)

        previous_texts = tuple(
            self._ellipsize_pill_token(cr, word, max_width)
            for word in previous_texts
        )
        current_texts = tuple(
            self._ellipsize_pill_token(cr, word, max_width)
            for word in current_texts
        )
        previous_positions, _ = self._word_layout(cr, previous_texts, width)
        current_positions, _ = self._word_layout(cr, current_texts, width)

        pill_geometry = getattr(self.visualization, '_pill_geometry', None)
        if callable(pill_geometry):
            _, pill_y, _, _ = pill_geometry(width, height)
        else:
            pill_y = height - 46.0
        baseline = pill_y - config.offset_y

        for word in frame.words:
            if word.alpha <= 0.01:
                continue

            if word.source == "previous":
                if word.index >= len(previous_positions):
                    continue
                x = previous_positions[word.index]
                text = previous_texts[word.index]
            else:
                if word.index >= len(current_positions):
                    continue
                x = current_positions[word.index]
                text = current_texts[word.index]
                if (
                    word.matched_from is not None
                    and word.matched_from < len(previous_positions)
                ):
                    old_x = previous_positions[word.matched_from]
                    x = old_x + (
                        x - old_x
                    ) * word.layout_progress

            y = baseline + word.y_offset

            # A restrained shadow keeps white text readable over bright windows
            # without introducing a visible badge or background rectangle.
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.55 * word.alpha)
            cr.move_to(x, y + 2.0)
            cr.show_text(text)

            cr.set_source_rgba(1.0, 1.0, 1.0, 0.97 * word.alpha)
            cr.move_to(x, y)
            cr.show_text(text)

    def _word_layout(self, cr: cairo.Context, words, width: int):
        if not words:
            return (), 0.0

        # Cairo's ink width for a space is zero. Layout with glyph advances so
        # whitespace and side bearings are preserved between separately drawn
        # words instead of making them visually run together.
        space_width = self._text_advance(cr, " ")
        widths = [self._text_advance(cr, word) for word in words]
        total_width = sum(widths) + space_width * max(0, len(words) - 1)
        x = (width - total_width) / 2.0
        positions = []
        for word_width in widths:
            positions.append(x)
            x += word_width + space_width
        return tuple(positions), total_width

    @staticmethod
    def _bisect_truncate(measure, available: float, length: int) -> int:
        """Longest length in [0, length] fitting `available`."""
        low, high = 0, length
        while low < high:
            mid = (low + high + 1) // 2
            if measure(mid) <= available:
                low = mid
            else:
                high = mid - 1
        return low

    def _ellipsize_pill_token(
        self,
        cr: cairo.Context,
        text: str,
        max_width: float,
    ) -> str:
        if self._text_advance(cr, text) <= max_width:
            return text

        suffix = "…"
        available = max_width - self._text_advance(cr, suffix)
        if available <= 0:
            return ""

        low = self._bisect_truncate(
            lambda n: self._text_advance(cr, text[:n]), available, len(text)
        )
        return text[:low].rstrip() + suffix if low else suffix

    @staticmethod
    def _text_extent(extents, field: str, index: int) -> float:
        if hasattr(extents, field):
            return getattr(extents, field)
        return extents[index]

    def _text_width(self, cr: cairo.Context, text: str) -> float:
        return self._text_extent(cr.text_extents(text), 'width', 2)

    def _text_advance(self, cr: cairo.Context, text: str) -> float:
        extents = cr.text_extents(text)
        advance = self._text_extent(extents, 'x_advance', 4)
        if advance > 0:
            return advance
        return self._text_extent(extents, 'width', 2)

    def _text_height(self, cr: cairo.Context, text: str) -> float:
        return self._text_extent(cr.text_extents(text), 'height', 3)

    def _recent_preview_text(self, text: str) -> str:
        words = text.split()
        if len(words) <= self.PREVIEW_WORD_LIMIT:
            return " ".join(words)
        return "... " + " ".join(words[-self.PREVIEW_WORD_LIMIT:])

    def _ellipsize(self, cr: cairo.Context, text: str, max_width: float) -> str:
        if self._text_width(cr, text) <= max_width:
            return text

        prefix = "... "
        if text.startswith(prefix):
            text = text[len(prefix):]

        available = max_width - self._text_width(cr, prefix)
        if available <= 0:
            return ""

        low = self._bisect_truncate(
            lambda n: self._text_width(cr, text[-n:]) if n else 0.0, available, len(text)
        )

        truncated = text[-low:].lstrip()
        return prefix + truncated if truncated else prefix


def load_css(css_path=None):
    """
    Load CSS styling for the OSD.
    
    Args:
        css_path: Path to CSS file (optional)
    """
    css_provider = Gtk.CssProvider()
    
    default_css = """
    .mic-osd-window {
        background-color: transparent;
    }
    """
    
    if css_path:
        try:
            css_provider.load_from_path(css_path)
        except GLib.Error:
            css_provider.load_from_string(default_css)
    else:
        css_provider.load_from_string(default_css)
    
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        css_provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
