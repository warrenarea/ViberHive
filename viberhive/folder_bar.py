"""Folder controls for the Favorites view."""

import sqlite3

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QInputDialog, QMenu, QMessageBox, QPushButton,
    QSizePolicy, QWidget,
)

KEEP_SELECTION = object()


class FolderBar(QWidget):
    changed = pyqtSignal()

    def __init__(self, picker, selector):
        super().__init__(picker)
        self.picker = picker
        self.store = picker.store
        self.kind = "gif"
        self.selected_folders = {}
        self.pending_auto = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.selector = selector
        self.selector.setAccessibleName("Favorites folder")
        self.selector.setToolTip("Show favorites in this folder")
        self.selector.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.selector.setMinimumWidth(110)
        self.selector.setMaximumWidth(240)
        row.addWidget(self.selector, 1)
        self.manage = QPushButton("Manage folders")
        self.manage.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(self.manage)
        menu.addAction("New folder…", self.new_folder)
        self.rename_action = menu.addAction("Rename folder…", self.rename_folder)
        self.delete_action = menu.addAction("Delete folder…", self.delete_folder)
        menu.aboutToShow.connect(self.update_actions)
        self.manage.setMenu(menu)
        row.addWidget(self.manage)
        row.addStretch(2)
        self.options = QHBoxLayout()
        self.options.setSpacing(26)
        row.addLayout(self.options)
        self.auto = QCheckBox("Auto-Categorize")
        self.auto.setToolTip("Group this collection by folder and file recurring subjects. Turn off for newest first.")
        self.auto.setChecked(self.store.auto_categorize_enabled())
        self.options.addWidget(self.auto)
        self.selector.currentIndexChanged.connect(lambda: self.changed.emit())
        self.auto.toggled.connect(self.set_automatic)

    def set_kind(self, kind):
        if kind != self.kind:
            self.selected_folders[self.kind] = self.selector.currentData()
            self.kind = kind
            self.reload(self.selected_folders.get(kind))
        else:
            self.reload()

    def reload(self, select=KEEP_SELECTION):
        select = self.selector.currentData() if select is KEEP_SELECTION else select
        with QSignalBlocker(self.selector):
            self.selector.clear()
            self.selector.addItem("All folders", None)
            self.selector.addItem("Unfiled", 0)
            for folder_id, name in self.store.folders(self.kind):
                self.selector.addItem(name, folder_id)
            self.selector.setCurrentIndex(max(0, self.selector.findData(select)))
        with QSignalBlocker(self.auto):
            current = self.pending_auto[self.kind] if self.kind in self.pending_auto else self.store.auto_categorize_enabled(self.kind)
            self.auto.setChecked(current)
            self.auto.setEnabled(self.kind not in self.pending_auto)

    def update_actions(self):
        selected = bool(self.selector.currentData())
        self.rename_action.setEnabled(selected)
        self.delete_action.setEnabled(selected)

    def report_error(self, exc):
        message = str(exc) if isinstance(exc, ValueError) else "Could not save the folder change."
        self.picker.status.action(message)

    def new_folder(self, *, favorite=None):
        name, ok = QInputDialog.getText(self, "New folder", "Folder name")
        if not ok:
            return
        try:
            folder_id = self.store.create_folder(name, kind=self.kind, favorite=favorite)
        except (ValueError, sqlite3.Error) as exc:
            return self.report_error(exc)
        self.reload(select=folder_id if favorite is None else KEEP_SELECTION)
        self.changed.emit()
        self.picker.status.action("Folder created." if favorite is None else "Favorite moved to the new folder.")

    def rename_folder(self):
        folder_id = self.selector.currentData()
        if not folder_id:
            return
        name, ok = QInputDialog.getText(self, "Rename folder", "Folder name", text=self.selector.currentText())
        if not ok:
            return
        try:
            self.store.rename_folder(folder_id, name, kind=self.kind)
        except (ValueError, sqlite3.Error) as exc:
            return self.report_error(exc)
        self.reload()
        self.changed.emit()
        self.picker.status.action("Folder renamed.")

    def delete_folder(self):
        folder_id = self.selector.currentData()
        if not folder_id:
            return
        prompt = QMessageBox(self)
        prompt.setWindowTitle("Delete folder")
        prompt.setTextFormat(Qt.TextFormat.PlainText)
        prompt.setText(f'Delete “{self.selector.currentText()}”? Its favorites will appear in Unfiled.')
        buttons = QMessageBox.StandardButton
        prompt.setStandardButtons(buttons.Yes | buttons.Cancel)
        prompt.setDefaultButton(buttons.Cancel)
        if prompt.exec() != buttons.Yes:
            return
        try:
            self.store.delete_folder(folder_id, kind=self.kind)
        except sqlite3.Error as exc:
            return self.report_error(exc)
        self.reload(select=0)
        self.changed.emit()
        self.picker.status.action("Folder deleted. Its favorites are in Unfiled.")

    def move_menu(self, menu, item):
        sub = menu.addMenu("Move to folder")
        current = self.store.folder_for(item.key)
        for folder_id, name in [(None, "Unfiled"), *self.store.folders(item.kind)]:
            action = sub.addAction(name.replace("&", "&&"))
            action.setCheckable(True)
            action.setChecked(current == folder_id)
            action.triggered.connect(lambda _, target=folder_id: self.move_favorite(item.key, target))
        sub.addSeparator()
        sub.addAction("New folder…", lambda: self.new_folder(favorite=item.key))

    def move_favorite(self, key, folder_id):
        try:
            self.store.move_favorite(key, folder_id)
        except (ValueError, sqlite3.Error) as exc:
            return self.report_error(exc)
        self.changed.emit()
        self.picker.status.action("Favorite moved.")

    def set_automatic(self, enabled):
        kind = self.kind
        self.pending_auto[kind] = enabled
        noun = "GIF" if kind == "gif" else "clip"
        self.auto.setEnabled(False)
        if enabled:
            self.picker.status.action(f"Organizing saved {noun}s…", 0)
        else:
            self.reload(None)
            self.changed.emit()

        def complete(count, error):
            self.pending_auto.pop(kind, None)
            if self.kind != kind:
                return
            self.reload()
            self.changed.emit()
            if error:
                self.picker.status.action(error)
                return
            message = (
                f"Auto-Categorize is on. Filed {count} saved {noun}{'s' if count != 1 else ''}."
                if enabled else "Auto-Categorize is off. Showing newest first."
            )
            self.picker.status.action(message)

        self.picker.jobs.run("action", lambda: self.store.set_auto_categorize(enabled, kind), complete)
