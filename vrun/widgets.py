# SPDX-License-Identifier: MPL-2.0
import math
from PySide6.QtCore import (QEvent, QLocale, QPoint, QPointF, QRectF, QRegularExpression,
                            QSize, Qt, QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QDoubleValidator, QPainter, QPainterPath, QPen,
                           QRegularExpressionValidator)
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QLineEdit, QListView,
                                QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget)

from . import theme
from .routes import longitude_delta


class ChoiceRow(QStyledItemDelegate):
    height = 34

    def __init__(self, choice, view):
        super().__init__(view)
        self.choice = choice

    def sizeHint(self, option, index):
        return QSize(0, self.height)

    def paint(self, painter, option, index):
        colors = self.choice.popup_colors
        selected = index.row() == self.choice.selected_row()
        focused = option.state & QStyle.State_HasFocus
        hovered = option.state & QStyle.State_MouseOver
        rect = option.rect.adjusted(1, 1, -1, -1)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        if selected or hovered or focused:
            painter.setBrush(QColor(colors["accent"] if selected else colors["field"]))
            painter.setPen(QPen(QColor(colors["ink"])) if focused and not selected else Qt.NoPen)
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        text_color = QColor(colors["accent_text"] if selected else colors["text"])
        painter.setPen(text_color)
        painter.setFont(option.font)
        centered = self.choice.isEditable()
        text_rect = rect.adjusted(22 if centered else 10, 0, -22, 0)
        text = option.fontMetrics.elidedText(str(index.data() or ""), Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | (Qt.AlignHCenter if centered else Qt.AlignLeft), text)
        if selected:
            painter.setPen(QPen(text_color, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            mark = QPointF(rect.right() - 14, rect.center().y())
            painter.drawLine(mark + QPointF(-4, 0), mark + QPointF(-1, 3))
            painter.drawLine(mark + QPointF(-1, 3), mark + QPointF(5, -3))
        painter.restore()


class ChoicePopup(QWidget):
    closed = Signal()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(self.colors["surface"]))
        painter.setPen(QColor(self.colors["border"]))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 11, 11)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.closed.emit()


class ChoiceBox(QComboBox):
    """Keep the control's model and text editing while painting a separate popup."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMaxVisibleItems(10)
        self._popup = ChoicePopup(self, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self._popup.setAttribute(Qt.WA_TranslucentBackground)
        layout = QVBoxLayout(self._popup)
        layout.setContentsMargins(6, 6, 6, 6)
        self._choices = QListView(self._popup)
        self._choices.setObjectName("choiceRows")
        self._choices.setMouseTracking(True)
        self._choices.setUniformItemSizes(True)
        self._choices.setVerticalScrollMode(QAbstractItemView.ScrollPerItem)
        self._choices.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._choices.setTextElideMode(Qt.ElideRight)
        self._choices.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._choices.setItemDelegate(ChoiceRow(self, self._choices))
        layout.addWidget(self._choices)
        self._choices.installEventFilter(self)
        self._choices.clicked.connect(self._commit)
        self._popup.closed.connect(self._popup_closed)
        self.set_popup_colors(theme.colors("system"))

    def set_popup_colors(self, colors):
        self.popup_colors = colors
        self._popup.colors = colors
        self._popup.setStyleSheet(f"""
            QListView#choiceRows {{ background: transparent; border: none; outline: none; }}
            QScrollBar:vertical {{ background: {colors['field']}; width: 7px; border-radius: 3px; }}
            QScrollBar::handle:vertical {{
                background: {colors['track']}; min-height: 26px; border-radius: 3px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {colors['muted']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        self._popup.update()
        self._choices.viewport().update()

    def view(self):
        return self._choices

    def selected_row(self):
        return self.findText(self.currentText()) if self.isEditable() else self.currentIndex()

    def showPopup(self):
        if not self.isEnabled() or not self.count() or self._popup.isVisible():
            return
        self._choices.setModel(self.model())
        self._choices.setRootIndex(self.rootModelIndex())
        self._choices.setModelColumn(self.modelColumn())
        row = self.selected_row()
        index = self.model().index(max(0, row), self.modelColumn(), self.rootModelIndex())
        self._choices.setCurrentIndex(index)
        top = self.mapToGlobal(QPoint(0, 0))
        screen = self.screen().availableGeometry()
        height = min(self.count(), self.maxVisibleItems()) * ChoiceRow.height + 12
        below = screen.bottom() - (top.y() + self.height()) - 4
        above = top.y() - screen.top() - 4
        open_below = below >= height or below >= above
        rows_that_fit = max(1, ((below if open_below else above) - 12) // ChoiceRow.height)
        height = min(height, rows_that_fit * ChoiceRow.height + 12)
        width = min(self.width(), screen.width())
        x = max(screen.left(), min(top.x(), screen.right() - width + 1))
        y = top.y() + self.height() + 4 if open_below else top.y() - height - 4
        self._popup.setGeometry(x, y, width, height)
        self.setProperty("popupOpen", True)
        self.style().unpolish(self)
        self.style().polish(self)
        self._popup.show()
        self._choices.setFocus(Qt.PopupFocusReason)
        self._choices.scrollTo(index, QAbstractItemView.PositionAtCenter)

    def hidePopup(self):
        self._popup.hide()
        super().hidePopup()

    def _popup_closed(self):
        super().hidePopup()
        self.setProperty("popupOpen", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _commit(self, index):
        if not index.isValid():
            return
        self.setCurrentIndex(index.row())
        if self.isEditable() and self.currentText() != self.itemText(index.row()):
            self.setEditText(self.itemText(index.row()))
        self.hidePopup()
        self.activated.emit(index.row())
        self.textActivated.emit(self.currentText())

    def wheelEvent(self, event):
        event.ignore()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F4 or event.key() == Qt.Key_Down and event.modifiers() & Qt.AltModifier:
            self.showPopup()
            event.accept()
        elif not self.isEditable() and event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.showPopup()
            event.accept()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._choices and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Escape, Qt.Key_F4):
                self.hidePopup()
                return True
            if event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
                self.hidePopup()
                self.focusNextPrevChild(event.key() == Qt.Key_Tab and not event.modifiers() & Qt.ShiftModifier)
                return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                self._commit(self._choices.currentIndex())
                return True
        return super().eventFilter(watched, event)


class SpeedInput(QLineEdit):
    valueChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        validator = QDoubleValidator(0.1, 15.0, 2, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        validator.setLocale(QLocale.c())
        self.setValidator(validator)
        self.setMaxLength(8)
        self.setTextMargins(0, 0, 40, 0)
        self.unit_color = QColor("#71839c")
        self.textChanged.connect(lambda _: self.valueChanged.emit(self.value()))

    def value(self):
        try:
            return float(self.text())
        except ValueError:
            return math.nan

    def setValue(self, value):
        self.setText(f"{value:.2f}")

    def wheelEvent(self, event):
        event.ignore()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(self.unit_color)
        painter.drawText(self.rect().adjusted(0, 0, -12, 0), Qt.AlignRight | Qt.AlignVCenter, "m/s")


class PaceInput(QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9]{1,3}:[0-5][0-9]"), self))
        self.setMaxLength(6)
        self.setPlaceholderText("分:秒")

    def speed(self):
        if not self.hasAcceptableInput():
            return math.nan
        minutes, seconds = map(int, self.text().split(":"))
        total = minutes * 60 + seconds
        return 1000 / total if total else math.nan


class DistanceInput(ChoiceBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setCompleter(None)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setFixedWidth(105)
        self.setMaxVisibleItems(6)
        self.addItems([str(value) for value in range(1, 21)])
        validator = QDoubleValidator(0, 1000, 6, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        locale = QLocale.c()
        locale.setNumberOptions(QLocale.RejectGroupSeparator)
        validator.setLocale(locale)
        self.lineEdit().setValidator(validator)
        self.lineEdit().setMaxLength(12)
        self.lineEdit().setPlaceholderText("如 2.5")
        self.lineEdit().setAlignment(Qt.AlignCenter)

class RoutePreview(QWidget):
    """Draw simulated playback progress, not an inferred live phone GPS position."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.route = None
        self.show_closing_segment = False
        self.colors = {}
        self.fraction = 0.0
        self.target_fraction = 0.0
        self.running = False
        self.phase = 0
        self.setMinimumHeight(116)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(220)
        self.animation.valueChanged.connect(self._animate)
        self.pulse = QTimer(self)
        self.pulse.setInterval(50)
        self.pulse.timeout.connect(self._pulse)

    def set_route(self, route, *, show_closing_segment=False):
        self.animation.stop()
        self.route = route
        self.show_closing_segment = show_closing_segment
        self.fraction = self.target_fraction = 0.0
        self.update()

    def set_running(self, running):
        self.running = running
        if running:
            self.pulse.start()
        else:
            self.pulse.stop()
            self.animation.stop()
            self.fraction = self.target_fraction
        self.update()

    def set_progress(self, fraction, animate=True):
        fraction = max(0.0, min(1.0, float(fraction)))
        self.animation.stop()
        if self.route and self.route.closed and fraction < self.target_fraction - 0.01:
            self.fraction = 0.0
        self.target_fraction = fraction
        if animate and self.running:
            self.animation.setStartValue(self.fraction)
            self.animation.setEndValue(fraction)
            self.animation.start()
        else:
            self.fraction = fraction
            self.update()

    def _animate(self, value):
        self.fraction = float(value)
        self.update()

    def _pulse(self):
        self.phase = (self.phase + 1) % 40
        self.update()

    def paintEvent(self, event):
        if not self.colors:
            return
        p = self.colors
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p["field"]))
        painter.drawRoundedRect(self.rect(), 9, 9)
        painter.setPen(QPen(QColor(p["grid"]), 1))
        for x in range(16, self.width(), 26):
            for y in range(14, self.height(), 26):
                painter.drawPoint(x, y)
        if not self.route:
            return
        mapped = self._mapped_points(painter)
        full = QPainterPath(mapped[0])
        track_points = mapped[1:-1] if self.show_closing_segment else mapped[1:]
        for point in track_points:
            full.lineTo(point)
        painter.setBrush(Qt.NoBrush)
        self._paint_track(painter, full)
        self._paint_progress(painter, mapped)

    def _mapped_points(self, painter):
        points = self.route.points
        latitude = sum(point.lat for point in points) / len(points)
        longitude_scale = math.cos(math.radians(latitude))
        longitude = 0.0
        coords = [(0.0, -points[0].lat)]
        for previous, point in zip(points, points[1:]):
            longitude += longitude_delta(previous.lng, point.lng)
            coords.append((longitude * longitude_scale, -point.lat))
        min_x, max_x = min(x for x, _ in coords), max(x for x, _ in coords)
        min_y, max_y = min(y for _, y in coords), max(y for _, y in coords)
        scale = max(1e-9, min((self.width()-50)/max(max_x-min_x, 1e-9),
                             (self.height()-40)/max(max_y-min_y, 1e-9)))
        return [QPointF(self.width()/2+(x-(min_x+max_x)/2)*scale,
                       self.height()/2+(y-(min_y+max_y)/2)*scale) for x, y in coords]

    def _paint_track(self, painter, full):
        painter.setPen(QPen(QColor(self.colors["track"]), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(full)

    def _paint_progress(self, painter, mapped):
        p = self.colors
        if self.show_closing_segment:
            painter.setPen(QPen(QColor(p["ink"]), 2, Qt.DashLine))
            painter.drawLine(mapped[-2], mapped[0])
            painter.setBrush(QColor(p["surface"]))
            painter.drawEllipse(mapped[-2], 4, 4)
        elif not self.route.closed:
            painter.setPen(QPen(QColor(p["ink"]), 2))
            painter.setBrush(QColor(p["surface"]))
            painter.drawEllipse(mapped[-1], 4, 4)
        travelled = self.route.length_m * self.fraction
        completed = QPainterPath(mapped[0])
        position = mapped[0]
        for start, end, distance in zip(mapped, mapped[1:], self.route.segment_lengths):
            if travelled >= distance:
                completed.lineTo(end)
                position = end
                travelled -= distance
            else:
                ratio = travelled / distance if distance else 0
                position = start + (end-start)*ratio
                completed.lineTo(position)
                break
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(p["ink"]), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(completed)
        painter.setPen(QPen(QColor(p["success"]), 2))
        painter.setBrush(QColor(p["surface"]))
        painter.drawEllipse(mapped[0], 4, 4)
        glow = QColor(p["ink"])
        glow.setAlpha(38 if not self.running else 32 + int(20*(1+math.sin(self.phase/40*math.tau))))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(position, 12, 12)
        painter.setPen(QPen(QColor(p["surface"]), 2))
        painter.setBrush(QColor(p["ink"]))
        painter.drawEllipse(position, 6, 6)
