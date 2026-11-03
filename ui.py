"""Small UI kit for app.py: colours derived from the system palette, toolbar icons,
disclosure sections and segmented controls.

The window chrome stays neutral and follows the system appearance, so the only saturated colour
on screen is the thermal image itself (and the red "recording" signal).
"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget

ICON_PX = 48                            # drawn large, QIcon scales down; the drawing grid is 24 x 24


def blend(fg, bg, a):
    return QColor(round(fg.red() * a + bg.red() * (1 - a)), round(fg.green() * a + bg.green() * (1 - a)),
                  round(fg.blue() * a + bg.blue() * (1 - a)))


def contrast(a, b):
    """WCAG contrast ratio between two opaque colours"""
    def lum(c):
        ch = [x / 255 for x in (c.red(), c.green(), c.blue())]
        r, g, b_ = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(pal):
    return pal.color(QPalette.Window).lightness() < 128


def secondary(pal):
    """secondary label colour: quieter than body text, still above 4.5:1"""
    return blend(pal.color(QPalette.WindowText), pal.color(QPalette.Window), 0.72)


def separator(pal):
    return blend(pal.color(QPalette.WindowText), pal.color(QPalette.Window), 0.28)


def signal(pal):
    """recording / problem state; always shown together with text or a shape, never colour alone"""
    return QColor('#FF6B60') if is_dark(pal) else QColor('#B3261E')


def scheme_key(pal):
    return tuple(pal.color(r).rgb() for r in (QPalette.Window, QPalette.WindowText, QPalette.Button, QPalette.Highlight))


def stylesheet(pal):
    """Only the custom pieces are styled; every other widget keeps the native look of the platform."""
    win, text = pal.color(QPalette.Window), pal.color(QPalette.WindowText)
    sep, hover = separator(pal).name(), blend(text, win, 0.10).name()
    selected = blend(text, win, 0.22).name()
    focus = pal.color(QPalette.Highlight).name()
    return f"""
    QLabel[role="secondary"] {{ color: {secondary(pal).name()}; }}
    QFrame#banner {{ background: {blend(text, win, 0.08).name()}; border-bottom: 1px solid {sep}; }}
    QFrame[role="separator"] {{ color: {sep}; }}
    QToolButton[role="disclosure"] {{ font-weight: 600; background: transparent; border: 1px solid transparent;
                                      border-radius: 5px; padding: 3px 2px; }}
    QToolButton[role="disclosure"]:hover {{ background: {hover}; }}
    QToolButton[role="disclosure"]:focus {{ border: 1px solid {focus}; }}
    QPushButton[segment] {{ background: transparent; color: {text.name()}; border: 1px solid {sep};
                            padding: 3px 10px; min-height: 20px; }}
    QPushButton[segment]:hover {{ background: {hover}; }}
    QPushButton[segment]:checked {{ background: {selected}; font-weight: 600; }}
    QPushButton[segment]:focus {{ border: 1px solid {focus}; }}
    QPushButton[segment="first"] {{ border-top-left-radius: 5px; border-bottom-left-radius: 5px; border-right: none; }}
    QPushButton[segment="last"] {{ border-top-right-radius: 5px; border-bottom-right-radius: 5px; }}
    QPushButton[segment="first"]:focus {{ border-right: 1px solid {focus}; }}
    """


# ------------------------------------------------------------------------------------- icons
def _draw(name, p):
    def stroke(path):
        p.drawPath(path)

    if name == 'connect':                       # plug
        p.drawLine(QPointF(9, 3), QPointF(9, 8))
        p.drawLine(QPointF(15, 3), QPointF(15, 8))
        body = QPainterPath(QPointF(6, 8))
        body.lineTo(18, 8)
        body.lineTo(18, 12)
        body.cubicTo(18, 15.5, 15.5, 17, 12, 17)
        body.cubicTo(8.5, 17, 6, 15.5, 6, 12)
        body.closeSubpath()
        stroke(body)
        p.drawLine(QPointF(12, 17), QPointF(12, 21.5))
    elif name == 'shutter':                     # lens with the closed flat-field flag
        p.drawEllipse(QPointF(12, 12), 9, 9)
        p.drawEllipse(QPointF(12, 12), 3.2, 3.2)
    elif name == 'snapshot':                    # camera body
        body = QPainterPath(QPointF(3, 8.5))
        body.lineTo(7.5, 8.5)
        body.lineTo(9, 5.5)
        body.lineTo(15, 5.5)
        body.lineTo(16.5, 8.5)
        body.lineTo(21, 8.5)
        body.lineTo(21, 19)
        body.lineTo(3, 19)
        body.closeSubpath()
        stroke(body)
        p.drawEllipse(QPointF(12, 13.2), 3.3, 3.3)
    elif name == 'record':
        p.setBrush(p.pen().color())
        p.drawEllipse(QPointF(12, 12), 6.5, 6.5)
    elif name == 'stop':
        p.setBrush(p.pen().color())
        p.drawRoundedRect(QRectF(6, 6, 12, 12), 2, 2)
    elif name == 'stream':                      # broadcast arcs
        p.setBrush(p.pen().color())
        p.drawEllipse(QPointF(12, 12), 1.8, 1.8)
        p.setBrush(Qt.NoBrush)
        for r in (5.5, 9.5):
            box = QRectF(12 - r, 12 - r, 2 * r, 2 * r)
            p.drawArc(box, -45 * 16, 90 * 16)
            p.drawArc(box, 135 * 16, 90 * 16)
    elif name == 'inspector':                   # window with a trailing pane
        p.drawRoundedRect(QRectF(3, 5, 18, 14), 2.5, 2.5)
        p.drawLine(QPointF(15, 5), QPointF(15, 19))
    else:
        raise ValueError(name)


def icon(name, pal, color=None):
    """toolbar icon in the current text colour (redrawn when the system appearance changes)"""
    fg = color or pal.color(QPalette.ButtonText)
    out = QIcon()
    for mode, c in ((QIcon.Normal, fg), (QIcon.Disabled, blend(fg, pal.color(QPalette.Window), 0.38))):
        pm = QPixmap(ICON_PX, ICON_PX)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(ICON_PX / 24, ICON_PX / 24)
        p.setPen(QPen(c, 1.7, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        try:
            _draw(name, p)
        finally:
            p.end()
        out.addPixmap(pm, mode)
    return out


# ------------------------------------------------------------------------------------- widgets
class Section(QWidget):
    """Titled disclosure section of the inspector: the triangle shows / hides the body."""

    def __init__(self, title, expanded=True):
        super().__init__()
        self.title = title
        self.header = QToolButton()
        self.header.setText(title)
        self.header.setProperty('role', 'disclosure')
        self.header.setCheckable(True)
        self.header.setAutoRaise(True)
        self.header.setFocusPolicy(Qt.StrongFocus)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.setMinimumHeight(28)
        self.body = QWidget()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        v.addWidget(self.header)
        v.addWidget(self.body)
        self.header.toggled.connect(self._toggled)
        self.set_expanded(expanded)
        self._toggled(expanded)

    def _toggled(self, on):
        self.header.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.body.setVisible(on)

    def set_expanded(self, on):
        self.header.setChecked(bool(on))

    def expanded(self):
        return self.header.isChecked()


def segmented(*labels):
    """Single-choice segmented control -> (container widget, [checkable buttons])"""
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(0)
    group = QButtonGroup(w)
    buttons = []
    for i, label in enumerate(labels):
        b = QPushButton(label)
        b.setCheckable(True)
        b.setProperty('segment', 'first' if i == 0 else 'last' if i == len(labels) - 1 else 'middle')
        group.addButton(b)
        h.addWidget(b, 1)
        buttons.append(b)
    buttons[0].setChecked(True)
    return w, buttons
