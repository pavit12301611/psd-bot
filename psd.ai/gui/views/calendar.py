"""Calendar workspace screen: month navigation, agenda, event editing, CalDAV sync."""

from __future__ import annotations

import calendar as py_calendar
import datetime as dt
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget, QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from gui.theme import current_theme
from gui.views.base import View, card, muted
from gui.widgets.dialogs import confirm, form_dialog
from gui.workers import run


class CalendarView(View):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            "Calendar",
            "Your events, local or CalDAV-synced. The agent can add and move events too.",
            parent,
        )
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.calendar.selectionChanged.connect(self._day_changed)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        nav = QHBoxLayout()
        self.month_label = QLabel("")
        self.month_label.setObjectName("Accent")
        nav.addWidget(self.month_label, 1)
        today = QPushButton("Today")
        today.setObjectName("Ghost")
        today.clicked.connect(lambda: self.calendar.setSelectedDate(QDate.currentDate()))
        nav.addWidget(today)
        left_layout.addLayout(nav)
        left_layout.addWidget(self.calendar)

        self.agenda_label = QLabel("Agenda")
        self.agenda_label.setObjectName("Accent")
        left_layout.addWidget(self.agenda_label)
        self.agenda = QListWidget()
        self.agenda.currentItemChanged.connect(self._on_event_selected)
        left_layout.addWidget(self.agenda, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self.detail = card("")
        self.detail_layout = self.detail.layout_  # type: ignore[attr-defined]
        self.detail_title = QLabel("Select an event")
        self.detail_title.setObjectName("PageTitle")
        self.detail_title.setWordWrap(True)
        self.detail_layout.addWidget(self.detail_title)
        self.detail_body = QLabel("")
        self.detail_body.setObjectName("Muted")
        self.detail_body.setWordWrap(True)
        self.detail_body.setTextFormat(Qt.RichText)
        self.detail_layout.addWidget(self.detail_body)
        buttons = QHBoxLayout()
        edit_button = QPushButton("Edit")
        edit_button.clicked.connect(self._edit_event)
        buttons.addWidget(edit_button)
        delete_button = QPushButton("Delete")
        delete_button.setObjectName("Danger")
        delete_button.clicked.connect(self._delete_event)
        buttons.addWidget(delete_button)
        buttons.addStretch(1)
        self.detail_layout.addLayout(buttons)
        right_layout.addWidget(self.detail)

        self.month_events = card("This month")
        month_layout = self.month_events.layout_  # type: ignore[attr-defined]
        self.month_list = QListWidget()
        self.month_list.setMaximumHeight(240)
        self.month_list.currentItemChanged.connect(self._on_event_selected)
        month_layout.addWidget(self.month_list)
        right_layout.addWidget(self.month_events, 1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 620])
        self.set_content(splitter)

        new_button = QPushButton("+ New event")
        new_button.setObjectName("Primary")
        new_button.clicked.connect(self._new_event)
        self.add_action(new_button)
        sync_button = QPushButton("Sync")
        sync_button.setToolTip("Pull/push CalDAV if configured")
        sync_button.clicked.connect(self._sync)
        self.add_action(sync_button)
        self.cal_box = QComboBox()
        self.cal_box.setMinimumWidth(160)
        self.cal_box.currentIndexChanged.connect(lambda _i: self.refresh())
        self.add_action(self.cal_box)
        self.status_label = QLabel("")
        self.status_label.setObjectName("MetaLine")
        self.add_action(self.status_label)

        self._events: List[Dict[str, Any]] = []
        self._selected: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        self.run(self._load_all, on_done=self._loaded)

    def _load_all(self) -> Dict[str, Any]:
        calendars = []
        try:
            calendars = self.api.calendars() or []
        except Exception:  # noqa: BLE001
            calendars = []
        selected = self.calendar.selectedDate().toPython()
        start = selected.replace(day=1)
        last_day = py_calendar.monthrange(selected.year, selected.month)[1]
        end = selected.replace(day=last_day, hour=23, minute=59)
        href = self.cal_box.currentData() or ""
        events = self.api.calendar_events(start.isoformat(), end.isoformat(), href)
        return {"calendars": calendars, "events": events}

    def _loaded(self, payload: Dict[str, Any]) -> None:
        calendars = payload.get("calendars") or []
        current_href = self.cal_box.currentData()
        self.cal_box.blockSignals(True)
        self.cal_box.clear()
        self.cal_box.addItem("All calendars", "")
        for entry in calendars:
            self.cal_box.addItem(entry.get("name") or entry.get("href") or "calendar",
                                 entry.get("href") or "")
        index = self.cal_box.findData(current_href or "")
        self.cal_box.setCurrentIndex(max(0, index))
        self.cal_box.blockSignals(False)

        self._events = payload.get("events") or []
        self._render_agenda()
        self._render_month()
        self._mark_days()
        self.status_label.setText(f"{len(self._events)} events this month")

    def _render_agenda(self) -> None:
        selected = self.calendar.selectedDate().toPython()
        self.agenda_label.setText(f"On {selected:%a %d %b %Y}")
        day_events = [
            event for event in self._events
            if self._event_date(event) == selected.date()
        ]
        day_events.sort(key=lambda e: self._event_start(e) or dt.datetime.min)
        self.agenda.clear()
        for event in day_events:
            start = self._event_start(event)
            label = f"{start:%H:%M}  {event.get('summary') or '(no title)'}" if start and not event.get("all_day") else f"{event.get('summary') or '(no title)'}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, event)
            self.agenda.addItem(item)

    def _render_month(self) -> None:
        self.month_list.clear()
        events = sorted(self._events, key=lambda e: self._event_start(e) or dt.datetime.min)
        for event in events:
            start = self._event_start(event)
            when = f"{start:%d %b %H:%M}" if start else "?"
            item = QListWidgetItem(f"{when}  ·  {event.get('summary') or '(no title)'}")
            item.setData(Qt.UserRole, event)
            self.month_list.addItem(item)

    def _mark_days(self) -> None:
        theme = current_theme()
        marked = QTextCharFormat()
        marked.setBackground(QColor(theme.accent_soft))
        marked.setForeground(QColor(theme.text))
        plain = QTextCharFormat()
        dates = {self._event_date(event) for event in self._events}
        first = self.calendar.selectedDate().toPython().replace(day=1)
        last_day = py_calendar.monthrange(first.year, first.month)[1]
        for day in range(1, last_day + 1):
            date = QDate(first.year, first.month, day)
            self.calendar.setDateTextFormat(date, marked if first.replace(day=day).date() in dates else plain)

    @staticmethod
    def _event_start(event: Dict[str, Any]) -> Optional[dt.datetime]:
        for key in ("dtstart", "start", "dt_start"):
            value = event.get(key)
            if isinstance(value, dict):
                value = value.get("dt") or value.get("value")
            if not value:
                continue
            return CalendarView._parse(value)
        return None

    @staticmethod
    def _parse(value: Any) -> Optional[dt.datetime]:
        if isinstance(value, dt.datetime):
            return value
        if isinstance(value, dt.date):
            return dt.datetime(value.year, value.month, value.day)
        text = str(value)
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(text.replace("Z", "+0000")[:19], fmt.replace("%z", ""))
            except ValueError:
                continue
        try:
            return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _event_date(self, event: Dict[str, Any]) -> Optional[dt.date]:
        start = self._event_start(event)
        return start.date() if start else None

    # -- interactions ------------------------------------------------------ #
    def _day_changed(self) -> None:
        self._render_agenda()

    def _on_event_selected(self, current: Optional[QListWidgetItem], _prev) -> None:
        if current is None:
            return
        event = current.data(Qt.UserRole) or {}
        self._selected = event
        start = self._event_start(event)
        end = self._parse(event.get("dtend") or event.get("end") or "")
        lines = []
        if start:
            lines.append(f"<b>When:</b> {start:%a %d %b %Y %H:%M}"
                         + (f" – {end:%H:%M}" if end else ""))
        if event.get("location"):
            lines.append(f"<b>Where:</b> {event['location']}")
        if event.get("calendar"):
            lines.append(f"<b>Calendar:</b> {event['calendar']}")
        if event.get("description"):
            lines.append(f"<br>{event['description']}")
        self.detail_title.setText(event.get("summary") or "(no title)")
        self.detail_body.setText("<br>".join(lines) or "No details.")

    def _new_event(self) -> None:
        selected = self.calendar.selectedDate().toPython()
        values = form_dialog(self, "New event", [
            ("summary", "Title", "text", ""),
            ("date", "Date (YYYY-MM-DD)", "text", f"{selected:%Y-%m-%d}"),
            ("time", "Start time (HH:MM)", "text", "09:00"),
            ("duration", "Duration (minutes)", "number", 60),
            ("all_day", "All-day event", "check", False),
            ("location", "Location", "text", ""),
            ("description", "Description", "multiline", ""),
        ])
        if not values or not values.get("summary"):
            return
        try:
            day = dt.date.fromisoformat(values.get("date") or f"{selected:%Y-%m-%d}")
        except ValueError:
            day = selected.date()
        if values.get("all_day"):
            start = dt.datetime(day.year, day.month, day.day)
            end = start + dt.timedelta(days=1)
        else:
            hour, minute = (values.get("time") or "09:00").split(":")[:2]
            start = dt.datetime(day.year, day.month, day.day, int(hour), int(minute))
            end = start + dt.timedelta(minutes=int(values.get("duration") or 60))
        run(self.api.create_event, values["summary"], start.isoformat(),
            dtend=end.isoformat(), all_day=bool(values.get("all_day")),
            location=values.get("location") or "",
            description=values.get("description") or "",
            on_done=lambda _r: (self.toast("Event created", "success"), self.refresh()),
            on_error=self.error)

    def _edit_event(self) -> None:
        event = self._selected
        uid = event.get("uid") or event.get("id")
        if not uid:
            return
        start = self._event_start(event)
        values = form_dialog(self, "Edit event", [
            ("summary", "Title", "text", event.get("summary") or ""),
            ("dtstart", "Start (ISO)", "text", start.isoformat() if start else ""),
            ("location", "Location", "text", event.get("location") or ""),
            ("description", "Description", "multiline", event.get("description") or ""),
        ])
        if not values:
            return
        run(self.api.update_event, uid, summary=values.get("summary"),
            dtstart=values.get("dtstart") or None, location=values.get("location"),
            description=values.get("description"),
            on_done=lambda _r: (self.toast("Event updated", "success"), self.refresh()),
            on_error=self.error)

    def _delete_event(self) -> None:
        event = self._selected
        uid = event.get("uid") or event.get("id")
        if not uid:
            return
        if not confirm(self, "Delete event?", event.get("summary") or "",
                       yes="Delete", destructive=True):
            return
        run(self.api.delete_event, uid,
            on_done=lambda _r: self.refresh(), on_error=self.error)

    def _sync(self) -> None:
        run(self.api.sync_calendar,
            on_done=lambda _r: (self.toast("Calendar synced", "success"), self.refresh()),
            on_error=self.error)
