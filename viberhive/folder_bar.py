"""Folder controls for the Favorites view."""

import sqlite3
from html import escape

from PyQt6.QtCore import QEvent, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QInputDialog, QLayout, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QWidget,
)

from .store import FolderNameConflict

KEEP_SELECTION = object()
FOLDER_COLORS = (
    "#b7d7fa", "#dfc8fa", "#b8e5cf", "#f5ceb3",
    "#f4c6d7", "#b6e4e8", "#e9dcac", "#c9ccf5",
)


def shortcut_button(parent, name, background, foreground="#232536", hover="#fff"):
    button = QPushButton(parent)
    button.setObjectName("folderShortcut")
    button.setAccessibleName(name)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    button.setStyleSheet(
        f"QPushButton {{ background: {background}; color: {foreground}; padding: 4px 11px; "
        "border: 2px solid transparent; border-radius: 10px; font-weight: 600; }"
        f"QPushButton:hover {{ border-color: {hover}; }}"
        "QPushButton:focus, QPushButton:pressed { border-color: #595d7c; }"
    )
    button.ensurePolished()
    caption = button.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 155)
    button.setText(caption.replace("&", "&&"))
    return button


class FolderShortcuts(QScrollArea):
    folderRequested = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("folderShortcuts")
        self.setAccessibleName("Folder shortcuts")
        self.setToolTip("Scroll to browse folder shortcuts")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWidgetResizable(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QScrollArea#folderShortcuts, QScrollArea#folderShortcuts > QWidget > QWidget "
            "{ background: transparent; }"
        )
        self.setWidget(QWidget())
        self.widget().setAutoFillBackground(False)
        self.widget().installEventFilter(self)
        self.row = QHBoxLayout(self.widget())
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(6)
        self.row.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.row.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.folders = ()

    def set_folders(self, folders):
        folders = (*folders, (0, "Unfiled"))
        if folders == self.folders:
            return
        self.folders = folders
        while self.row.count():
            old = self.row.takeAt(0).widget()
            old.hide()
            old.deleteLater()
        height = 0
        for folder_id, name in folders:
            color = FOLDER_COLORS[(folder_id - 1) % len(FOLDER_COLORS)] if folder_id else "#d0d3dd"
            chip = shortcut_button(self.widget(), name, color)
            chip.setProperty("folderId", folder_id)
            chip.setToolTip(f"<qt>Go to {escape(name)}</qt>")
            chip.installEventFilter(self)
            chip.clicked.connect(lambda _, key=folder_id: self.folderRequested.emit(key))
            self.row.addWidget(chip)
            height = max(height, chip.sizeHint().height())
        self.row.activate()
        self.setFixedHeight(height + 2)

    def wheelEvent(self, event):
        delta = event.pixelDelta()
        if delta.isNull():
            # One standard notch is 120 units, roughly the width of a shortcut.
            delta = event.angleDelta()
        distance = delta.x() if abs(delta.x()) > abs(delta.y()) else delta.y()
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - distance)
        event.accept()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and (watched is self.widget() or isinstance(watched, QPushButton)):
            self.wheelEvent(event)
            return True
        return super().eventFilter(watched, event)


class FolderBar(QWidget):
    changed = pyqtSignal()
    folderRequested = pyqtSignal(int)

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
        self.strip = FolderShortcuts()
        self.strip.folderRequested.connect(self.folderRequested)
        self.all_folders = shortcut_button(self, "All Folders", "#ffffff", "#000000", "#595d7c")
        self.all_folders.setToolTip("Show all favorites in this collection and clear the search")
        self.all_folders.installEventFilter(self.strip)
        self.all_folders.clicked.connect(self.show_all_folders)
        row.addWidget(self.all_folders)
        row.addWidget(self.strip, 1)
        self.selector = selector
        self.selector.setAccessibleName("Favorites folder")
        self.selector.setToolTip("Show favorites in this folder")
        self.selector.setMinimumWidth(110)
        self.selector.setMaximumWidth(180)
        self.manage = QPushButton("Manage folders")
        self.manage.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(self.manage)
        menu.addAction("New folder…", self.new_folder)
        self.rename_action = menu.addAction("Rename folder…", self.rename_folder)
        self.delete_action = menu.addAction("Delete folder…", self.delete_folder)
        menu.addSeparator()
        self.undo_action = menu.addAction("Undo Merge", self.undo_merge)
        self.undo_action.setEnabled(False)
        menu.aboutToShow.connect(self.update_actions)
        self.manage.setMenu(menu)
        row.addWidget(self.manage)
        row.addWidget(self.selector)
        self.auto = QCheckBox("Auto-Categorize")
        self.auto.setToolTip("Group this collection by folder and file recurring subjects. Turn off for newest first.")
        self.auto.setChecked(self.store.auto_categorize_enabled())
        self.selector.currentIndexChanged.connect(lambda: self.changed.emit())
        self.auto.toggled.connect(self.set_automatic)

    def show_all_folders(self):
        with QSignalBlocker(self.selector):
            self.selector.setCurrentIndex(self.selector.findData(None))
        self.picker.set_search_text("")
        self.strip.horizontalScrollBar().setValue(0)
        self.changed.emit()

    def set_kind(self, kind):
        if kind != self.kind:
            self.selected_folders[self.kind] = self.selector.currentData()
            self.kind = kind
            self.reload(self.selected_folders.get(kind))
            self.strip.horizontalScrollBar().setValue(0)
        else:
            self.reload()

    def reload(self, select=KEEP_SELECTION):
        select = self.selector.currentData() if select is KEEP_SELECTION else select
        folders = self.store.folders(self.kind)
        self.strip.set_folders(folders)
        with QSignalBlocker(self.selector):
            self.selector.clear()
            self.selector.addItem("All folders", None)
            self.selector.addItem("Unfiled", 0)
            for folder_id, name in folders:
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
        try:
            merge = self.store.last_folder_merge(self.kind)
        except sqlite3.Error as exc:
            self.report_error(exc)
            merge = None
        caption = "Undo Merge"
        if merge:
            merge_id, source, destination = merge
            caption += f" ({source}+{destination})".replace("&", "&&")
            self.undo_action.setData(merge_id)
        else:
            self.undo_action.setData(None)
        self.undo_action.setText(caption)
        self.undo_action.setEnabled(merge is not None)

    def report_error(self, exc):
        message = str(exc) if isinstance(exc, ValueError) else "Could not save the folder change."
        self.picker.status.action(message)

    def new_folder(self, *, favorite=None):
        name, ok = QInputDialog.getText(self, "New folder", "Folder name")
        if not ok:
            return
        if favorite is not None:
            return self.move_favorite(favorite, None, new_name=name)
        try:
            folder_id = self.store.create_folder(name, kind=self.kind)
        except (ValueError, sqlite3.Error) as exc:
            return self.report_error(exc)
        self.reload(select=folder_id)
        self.changed.emit()
        self.picker.status.action("Folder created.")

    def rename_folder(self):
        folder_id = self.selector.currentData()
        if not folder_id:
            return
        source_name = self.selector.currentText()
        name, ok = QInputDialog.getText(self, "Rename folder", "Folder name", text=source_name)
        if not ok:
            return
        try:
            self.store.rename_folder(folder_id, name, kind=self.kind)
        except FolderNameConflict as conflict:
            return self.confirm_merge(folder_id, source_name, conflict.folder_id, conflict.folder_name)
        except (ValueError, sqlite3.Error) as exc:
            return self.report_error(exc)
        self.reload()
        self.changed.emit()
        self.picker.status.action("Folder renamed.")

    def confirm_merge(self, source_id, source_name, destination_id, destination_name):
        prompt = QMessageBox(self)
        prompt.setWindowTitle("Merge folders?")
        prompt.setTextFormat(Qt.TextFormat.PlainText)
        prompt.setText(
            f'“{destination_name}” already exists. Move all favorites from “{source_name}” into '
            f'“{destination_name}” and remove the empty “{source_name}” folder?'
        )
        prompt.setInformativeText("You can reverse this change using Undo Merge in Manage folders.")
        merge = prompt.addButton("Merge folders", QMessageBox.ButtonRole.AcceptRole)
        cancel = prompt.addButton(QMessageBox.StandardButton.Cancel)
        prompt.setDefaultButton(cancel)
        prompt.setEscapeButton(cancel)
        prompt.exec()
        if prompt.clickedButton() is not merge:
            return
        try:
            self.store.merge_folders(
                source_id, destination_id, kind=self.kind,
                source_name=source_name, destination_name=destination_name,
            )
        except (ValueError, sqlite3.Error) as exc:
            return self.report_error(exc)
        self.reload(select=destination_id)
        self.changed.emit()
        self.picker.status.action(f'Folders merged into “{destination_name}”.')

    def undo_merge(self):
        merge_id = self.undo_action.data()
        if merge_id is None:
            return
        try:
            restored = self.store.undo_folder_merge(merge_id, kind=self.kind)
        except (ValueError, sqlite3.Error) as exc:
            return self.report_error(exc)
        self.reload(select=restored)
        self.changed.emit()
        self.picker.status.action("Folder merge undone.")

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

    def move_favorite(self, key, folder_id, *, new_name=None):
        try:
            emptied = self.store.move_favorite(key, folder_id, new_name=new_name, kind=self.kind)
        except (ValueError, sqlite3.Error) as exc:
            return self.report_error(exc)
        if new_name is not None:
            self.reload()
        self.changed.emit()
        self.picker.status.action("Favorite moved to the new folder." if new_name is not None else "Favorite moved.")
        if emptied:
            self.offer_delete_empty_folder(*emptied)

    def offer_delete_empty_folder(self, folder_id, name, kind):
        prompt = QMessageBox(self)
        prompt.setWindowTitle("Delete empty folder?")
        prompt.setTextFormat(Qt.TextFormat.PlainText)
        prompt.setText(f'“{name}” is now empty. Delete this folder?')
        delete = prompt.addButton("Delete folder", QMessageBox.ButtonRole.DestructiveRole)
        keep = prompt.addButton("Keep folder", QMessageBox.ButtonRole.RejectRole)
        prompt.setDefaultButton(keep)
        prompt.setEscapeButton(keep)
        prompt.exec()
        if prompt.clickedButton() is not delete:
            return
        try:
            deleted = self.store.delete_folder(folder_id, kind=kind, if_empty_named=name)
        except sqlite3.Error as exc:
            return self.report_error(exc)
        self.reload()
        self.changed.emit()
        self.picker.status.action(f'Deleted empty folder “{name}”.' if deleted else
                                  "The folder changed before confirmation. No folder was deleted.")

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
