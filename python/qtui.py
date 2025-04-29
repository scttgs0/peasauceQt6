from __future__ import print_function

"""
    Peasauce - interactive disassembler
    Copyright (C) 2012-2017 Richard Tew
    Licensed using the MIT license.
"""

"""

PySide eccentricities:
- After populating the disassembly view, it is not possible to tell it to select a row
  and scroll to it immediately.  Nothing will happen.  It needs to render first, so in
  order to do this, the paint event needs to be caught and the scrolling and selecting
  done there.
- Changing the font used in the TableView will not change the height of the rows, resulting
  in clipping of text.  The font height needs to be obtained and the row height changed
  accordingly.
- Table selection behavior is flakey.  Despite setting both single-selection and row
  selection, various things will do multi-selection of both rows, and bizarre item
  (single cell) selection.

http://doc.qt.digia.com/4.6/richtext-html-subset.html
http://qt-project.org/faq/answer/how_can_i_programatically_find_out_which_rows_items_are_visible_in_my_view
http://qt-project.org/wiki/Signals_and_Slots_in_PySide

"""

import pickle
import logging
import operator
import os
import sys
import time

# mypy-lang support
from typing import Any, List

from PySide6 import QtCore, QtGui, QtWidgets, QtGui
from PySide6.QtGui import QAction, QShortcut

from . import disassemblylib
from . import editor_state
from . import res
from . import util
from . import toolapi


SETTINGS_FILE = "settings.pikl"

APPLICATION_NAME = "PeaSauce"
PROJECT_SUFFIX = "psproj"
PROJECT_FILTER = APPLICATION_NAME +" project (*."+ PROJECT_SUFFIX +")"
SOURCE_CODE_FILTER = "Source code (*.s *.asm)"

ERRMSG_BAD_NEW_PROJECT_OPTIONS = "ERRMSG_BAD_NEW_PROJECT_OPTIONS"

logger = logging.getLogger("UI")


UNCERTAIN_ADDRESS_IDX = 0


class BaseItemModel(QtCore.QAbstractItemModel):
    _header_font = None # type: QtGui.QApplication.font

    def __init__(self, columns, parent):
        self.window = parent

        super(BaseItemModel, self).__init__()

        self._header_data = {}

        self._column_count = len(columns)
        self._column_types = {}
        self._column_alignments = [ None ] * len(columns)
        for i, (column_name, column_type) in enumerate(columns):
            self._column_types[i] = column_type
            self.setHeaderData(i, QtCore.Qt.Horizontal, column_name)
            if column_type is int or column_type is hex:
                self._column_alignments[i] = QtCore.Qt.AlignRight
            else:
                self._column_alignments[i] = QtCore.Qt.AlignLeft

    def _data_ready(self):
        row_count = self.rowCount()
        self.beginInsertRows(QtCore.QModelIndex(), 0, row_count-1)
        self.endInsertRows()

    def _clear_data(self):
        row_count = self.rowCount()
        self.beginRemoveRows(QtCore.QModelIndex(), 0, row_count-1)
        self.endRemoveRows()

    def _begin_row_change(self, row, row_count):
        if row_count < 0:
            self.beginRemoveRows(QtCore.QModelIndex(), row, row+(-row_count)-1)
        elif row_count > 0:
            self.beginInsertRows(QtCore.QModelIndex(), row, row+row_count-1)

    def _end_row_change(self, row, row_count):
        if row_count < 0:
            self.endRemoveRows()
        elif row_count > 0:
            self.endInsertRows()

    def _set_header_font(self, font):
        self._header_font = font

    def _data_changed(self, start_x, start_y, end_x, end_y):
        # NOTE(rmtew): The same start and end indexes does nothing.
        self.dataChanged.emit(self.createIndex(start_x, start_y), self.createIndex(end_x, end_y))

    def setHeaderData(self, section, orientation, data):
        self._header_data[(section, orientation)] = data

    def columnCount(self, parent):
        return self._column_count

    def headerData(self, section, orientation, role):
        if role == QtCore.Qt.DisplayRole:
            # e.g. section = column_index, orientation = QtCore.Qt.Horizontal
            return self._header_data.get((section, orientation))
        elif role == QtCore.Qt.FontRole:
            return self._header_font

    def data(self, index, role):
        if not index.isValid():
            return None

        if role == QtCore.Qt.TextAlignmentRole:
            column = index.column()
            return self._column_alignments[column]
        elif role != QtCore.Qt.DisplayRole:
            return None

        column, row = index.column(), index.row()
        column_type = self._column_types[column]
        value = self._lookup_cell_value(row, column)
        if column_type is hex:
            value = "$%X" % value
        return value

    def parent(self, index):
        return QtCore.QModelIndex()

    def index(self, row, column, parent):
        if not self.hasIndex(row, column, parent):
            return QtCore.QModelIndex()
        return self.createIndex(row, column)


class DisassemblyItemModel(BaseItemModel):
    def __init__(self, columns, parent):
        super(DisassemblyItemModel, self).__init__(columns, parent)

    def rowCount(self, parent=None):
        return self.window.editor_state.get_line_count(self.window.editor_client)

    def _lookup_cell_value(self, row, column):
        return self.window.editor_state.get_file_line(self.window.editor_client, row, column)


class CustomItemModel(BaseItemModel):
    """ The main reason for this subclass is to give custom column alignment. """
    def __init__(self, columns, parent):
        self._row_data = []
        self._sort_column1 = 0 # type: int
        self._sort_column2 = 0 # type: int
        self._sort_order = QtCore.Qt.AscendingOrder

        super(CustomItemModel, self).__init__(columns, parent)

    def _set_row_data(self, row_data, removal_rows=None, addition_rows=None):
        self._row_data = row_data
        if addition_rows:
            self.beginInsertRows(QtCore.QModelIndex(), addition_rows[0], addition_rows[1])
            self.endInsertRows()
        elif removal_rows:
            self.beginRemoveRows(QtCore.QModelIndex(), removal_rows[0], removal_rows[1])
            self.endRemoveRows()

    def _get_row_data(self):
        # If you use this data, remember it may be arbitrarily sorted by column.
        return self._row_data

    def _index_cell_value(self, column, value):
        for i, row in enumerate(self._row_data):
            if row[column] == value:
                return i
        return -1

    def _lookup_cell_value(self, row, column):
        return self._row_data[row][column]

    def _get_sort_column1(self):
        return self._sort_column1

    def _get_sort_column2(self):
        return self._sort_column2

    def rowCount(self, parent=None):
        return len(self._row_data)

    def _sort_list(self, l: List) -> None:
        ix1 = self._sort_column1
        ix2 = self._sort_column2
        if self._sort_order == QtCore.Qt.AscendingOrder:
            l.sort(key=lambda v: (v[ix1], v[ix2]))
        else:
            l.sort(key=lambda v: (v[ix1], v[ix2]), reverse=True)

    def sort(self, column, sort_order):
        if self._sort_column1 == column and self._sort_order == sort_order:
            return

        self._sort_column1 = column
        self._sort_order = sort_order
        if len(self._row_data):
            # The QT docs suggest this is the sequence for sorting.  Sequence has more steps, but not sure they apply.
            self.layoutAboutToBeChanged.emit()
            self._sort_list(self._row_data)
            self.layoutChanged.emit()


def create_table_model(parent, columns, _class=None):
    if _class is None:
        _class = CustomItemModel
    return _class(columns, parent)


class CustomQTableView(QtWidgets.QTableView):
    selection_change_signal = QtCore.Signal(tuple)
    _initial_line_idx = None # type: int
    _selected_indexes = [] # type: List[Any]

    def paintEvent(self, event):
        if self._initial_line_idx is not None:
            # This should be the main window.
            self.parent().scroll_to_line(self._initial_line_idx)
            self._initial_line_idx = None
        super(CustomQTableView, self).paintEvent(event)

    def setFont(self, font):
        result = super(CustomQTableView, self).setFont(font)
        fontMetrics = QtGui.QFontMetrics(font)
        # Whenever the font is changed, resize the row heights to suit.
        self.verticalHeader().setDefaultSectionSize(fontMetrics.lineSpacing() + 2)
        return result

    def selectionChanged(self, selected, deselected):
        # type: (QtGui.QItemSelection, QtGui.QItemSelection) -> None
        super(CustomQTableView, self).selectionChanged(selected, deselected)
        self._selected_indexes = selected.indexes()
        self.selection_change_signal.emit((selected, deselected))


class DisassemblyOperandDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, parent, window):
        self.window = window
        super(DisassemblyOperandDelegate, self).__init__(parent)

    def paint(self, painter, option, index):
        if index.column() == 4:
            options = QtWidgets.QStyleOptionViewItem(option)
            self.initStyleOption(options, index)

            style = QtWidgets.QApplication.style() if options.widget is None else options.widget.style()
            doc = QtGui.QTextDocument()
            doc.setDefaultFont(self.parent().font())
            text = options.text
            if options.state & QtWidgets.QStyle.State_Selected:
                bits = text.split(", ")
                selected_operand_index = self.window.get_selected_operand()
                for i, operand_text in enumerate(bits):
                    if i == selected_operand_index:
                        bits[i] = "<span style='background-color: #a6dafc; color: black;'>&nbsp;&nbsp;&nbsp;"+ operand_text +"&nbsp;&nbsp;&nbsp;</span>"
                    else:
                        bits[i] = "<span>"+ operand_text +"</span>"
                text = ", ".join(bits)
            elif self.window._is_uncertain_data_reference(index.row()):
                text = "<span style='color: white; background-color: black'>"+ text +"</span>";

            doc.setHtml(text)
            doc.setTextWidth(option.rect.width())
            doc.setDocumentMargin(0)
            options.text = ""
            style.drawControl(QtWidgets.QStyle.CE_ItemViewItem, options, painter)
            ctx = QtGui.QAbstractTextDocumentLayout.PaintContext()
            # Ensures that the selection colours are correct.
            if options.state & QtWidgets.QStyle.State_Selected:
                ctx.palette.setColor(QtGui.QPalette.Text, options.palette.color(QtGui.QPalette.Active, QtGui.QPalette.HighlightedText))

            # Errors on PySide 2.2.1, seems to do the same thing as the following attr access.
            #textRect = style.subElementRect(QtWidgets.QStyle.SE_ItemViewItemText, options, options.widget)
            textRect = options.rect
            painter.save()
            painter.translate(textRect.topLeft())
            painter.setClipRect(textRect.translated(-textRect.topLeft()))
            doc.documentLayout().draw(painter, ctx)
            painter.restore()
        else:
            super(DisassemblyOperandDelegate, self).paint(painter, option, index)

    def sizeHint(self, option, index):
        """
        I do not remember why I had this.  Maybe something to do with auto-sizing that did not work?
        columnNumber = index.column()
        if columnNumber == 4:
            options = QtWidgets.QStyleOptionViewItemV4(option)
            self.initStyleOption(options, index)
            doc = QtGui.QTextDocument()
            doc.setHtml(options.text)
            doc.setTextWidth(option.rect.width())
            return QtCore.QSize(doc.idealWidth(), doc.size().height())
        """
        return super(DisassemblyOperandDelegate, self).sizeHint(option, index)

    shflag = False

def create_table_widget(parent, model, multiselect=False):
    # Need a custom table view to get selected row.
    table = CustomQTableView(parent)
    table.setModel(model)
    table.setCornerButtonEnabled(False)
    table.setGridStyle(QtCore.Qt.NoPen)
    table.setSortingEnabled(False)
    if isinstance(model, CustomItemModel):
        table.sortByColumn(model._sort_column1, model._sort_order)
    # Hide row numbers.
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(14)
    # Allow column resizing, but ensure all space is taken up by columns.
    table.horizontalHeader().setStretchLastSection(True)
    #
    # table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
    table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerItem)
    # No selection of individual cells, but rather line specific selection.
    if multiselect:
        table.setSelectionMode(QtWidgets.QAbstractItemView.MultiSelection)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    return table


class QTUIEditorClient(editor_state.ClientAPI, QtCore.QObject):
    prolonged_action_signal = QtCore.Signal(tuple)
    prolonged_action_update_signal = QtCore.Signal(tuple)
    prolonged_action_complete_signal = QtCore.Signal()
    pre_line_change_signal = QtCore.Signal(tuple)
    post_line_change_signal = QtCore.Signal(tuple)
    uncertain_reference_modification_signal = QtCore.Signal(tuple)
    symbol_added_signal = QtCore.Signal(tuple)
    symbol_removed_signal = QtCore.Signal(tuple)

    def __init__(self, *args, **kwargs):
        super(QTUIEditorClient, self).__init__(*args, **kwargs)

        self.file_path = None

    def reset_state(self):
        self.file_path = None
        self.owner_ref().reset_state()

    # TODO: Should this be an internal function?  editor state uses it at the moment.
    def get_load_file(self):
        return open(self.file_path, "rb")

    ## Events related to user direct interaction and required GUIs.
    # It does not appear to be necessary to delegate these to the GUI thread.

    def request_load_file(self):
        # Request the user select a file.
        options = QtWidgets.QFileDialog.Options()
        file_path, open_filter = QtWidgets.QFileDialog.getOpenFileName(self.owner_ref(), "Select a file to disassemble", options=options)
        if not len(file_path):
            return
        # Cover the case of a command-line startup with a current directory file name.
        if os.path.dirname(file_path) == "":
            file_path = os.path.join(os.getcwd(), file_path)
        self.file_path = file_path
        return self.get_load_file(), file_path

    def request_new_project_option_values(self, options):
        result = NewProjectDialog(options, self.file_path, self.owner_ref()).exec_()
        if result != QtWidgets.QDialog.Accepted:
            return ERRMSG_BAD_NEW_PROJECT_OPTIONS
        return options

    def request_load_project_option_values(self, load_options):
        result = LoadProjectDialog(load_options, self.file_path, self.owner_ref()).exec_()
        # if result == QtWidgets.QDialog.Accepted:
        return load_options

    def request_save_project_option_values(self, save_options):
        options = QtWidgets.QFileDialog.Options()
        save_file_path, filter_text = QtWidgets.QFileDialog.getSaveFileName(self.owner_ref(), caption="Save to...", filter=PROJECT_FILTER, options=options)
        if not len(save_file_path):
            return
        result = SaveProjectDialog(save_options, save_file_path, self.owner_ref()).exec_()
        if result != QtWidgets.QDialog.Accepted:
            return
        save_options.save_file_path = save_file_path
        return save_options

    def request_code_save_file(self):
        options = QtWidgets.QFileDialog.Options()
        save_file_path, filter_text = QtWidgets.QFileDialog.getSaveFileName(self.owner_ref(), caption="Export source code to...", filter=SOURCE_CODE_FILTER, options=options)
        if len(save_file_path):
            return open(save_file_path, "w")

    def request_text(self, title_text, prompt_text, default_text=""):
        text, ok = QtWidgets.QInputDialog.getText(self.owner_ref(), title_text, prompt_text, QtWidgets.QLineEdit.Normal, default_text)
        text = text.strip()
        if ok and text != '':
            return text

    def request_address(self, default_address):
        text, ok = QtWidgets.QInputDialog.getText(self.owner_ref(), "Which address?", "Address:", QtWidgets.QLineEdit.Normal, "0x%X" % default_address)
        text = text.strip()
        if ok and text != '':
            return util.str_to_int(text)

    def request_address_selection(self, title_text, body_text, button_text, address_rows, row_keys):
        """
        For now just show a dialog that allows the user to select the given addresses.
        Instructions to click on an address to select it, scrollable list of addresses, cancel button.
        """
        dialog = RowSelectionDialog(self.owner_ref(), title_text, body_text, button_text, address_rows, row_keys)
        ret = dialog.exec_()
        if ret == 1:
            return dialog.selection_key

    def request_label_name(self, default_label_name):
        text, ok = QtWidgets.QInputDialog.getText(self.owner_ref(), "Rename symbol", "New name:", QtWidgets.QLineEdit.Normal, default_label_name)
        text = text.strip()
        if ok and text != default_label_name:
            return text

    def event_tick(self, active_client) -> None:
        global app
        app.processEvents()

    ## Events related to the lifetime of an attempt to load a file.
    # It does not appear to be necessary to delegate these to the GUI thread.

    def event_load_start(self, active_client, file_path):
        # Need this in case loading was started via command-line, and skipped 'request_load_file'.
        self.file_path = file_path
        self.owner_ref().on_file_load_start(file_path)

    def event_load_successful(self, active_client):
        if not active_client:
            self.owner_ref().on_file_opened()

    ## Events related to prolonged actions (display of a progress dialog).
    # It is necessary to delegate these to the GUI thread via slots and signals.

    def event_prolonged_action(self, active_client, title_msg_id, description_msg_id, can_cancel, step_count, abort_callback):
        args = (
            res.strings[title_msg_id],
            res.strings[description_msg_id],
            can_cancel,
            step_count,
            abort_callback
        )
        self.prolonged_action_signal.emit(args)

    def event_prolonged_action_update(self, active_client, message_id, step_number):
        args = res.strings[message_id], step_number
        self.prolonged_action_update_signal.emit(args)

    def event_prolonged_action_complete(self, active_client):
        self.prolonged_action_complete_signal.emit()

    ## Events related to post-load disassembly events.
    # It is necessary to delegate these to the GUI thread via slots and signals.

    def event_pre_line_change(self, active_client, line0, line_count):
        self.pre_line_change_signal.emit((line0, line_count))

    def event_post_line_change(self, active_client, line0, line_count):
        self.post_line_change_signal.emit((line0, line_count))

    def event_uncertain_reference_modification(self, active_client, data_type_from, data_type_to, address, length):
        self.uncertain_reference_modification_signal.emit((data_type_from, data_type_to, address, length))

    def event_symbol_added(self, active_client, symbol_address, symbol_label):
        self.symbol_added_signal.emit((symbol_address, symbol_label))

    def event_symbol_removed(self, active_client, symbol_address, symbol_label):
        self.symbol_removed_signal.emit((symbol_address, symbol_label))



class MainWindow(QtWidgets.QMainWindow):
    _settings = None # type: dict

    loaded_signal = QtCore.Signal(int)
    log_signal = QtCore.Signal(tuple)

    _progress_dialog = None # type: QtWidgets.QProgressDialog
    _progress_dialog_steps = 0 # type: int

    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)

        self.editor_client = QTUIEditorClient(self)
        self.editor_client.prolonged_action_signal.connect(self.show_progress_dialog)
        self.editor_client.prolonged_action_update_signal.connect(self.update_progress_dialog)
        self.editor_client.prolonged_action_complete_signal.connect(self.close_progress_dialog)
        self.editor_client.pre_line_change_signal.connect(self.on_pre_line_change)
        self.editor_client.post_line_change_signal.connect(self.on_post_line_change)
        self.editor_client.uncertain_reference_modification_signal.connect(self.on_uncertain_reference_modification)
        self.editor_client.symbol_added_signal.connect(self.on_disassembly_symbol_added)
        self.editor_client.symbol_removed_signal.connect(self.on_disassembly_symbol_removed)

        self.editor_state = editor_state.EditorState()
        self.editor_state.register_client(self.editor_client)

        self.toolapiob = toolapi.ToolAPI(self.editor_state)

        self.tracked_models = []

        ## GENERATE THE UI

        self.setWindowTitle(APPLICATION_NAME)

        self.list_model = create_table_model(self, [ ("Address", int), ("Data", str), ("Label", str), ("Instruction", str), ("Operands", str), ("Template", str) ], _class=DisassemblyItemModel)
        self.tracked_models.append(self.list_model)
        self.list_model._column_alignments[0] = QtCore.Qt.AlignRight
        self.list_table = create_table_widget(self, self.list_model)
        self.list_table.setColumnWidth(4, 200)
        self.list_table.setItemDelegateForColumn(4, DisassemblyOperandDelegate(self.list_table, self))
        self.list_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.list_table.selection_change_signal.connect(self.list_table_selection_change_event)
        self.setCentralWidget(self.list_table)

        self.create_menus()
        self.create_dock_windows()
        self.create_shortcuts()

        # Override the default behaviour of using the same font for the table header, that the table itself uses.
        # TODO: Maybe rethink this, as it looks a bit disparate to use different fonts for both.
        default_header_font = QtWidgets.QApplication.font(self.list_table.horizontalHeader())
        self.list_model._set_header_font(default_header_font)

        ## RESTORE SAVED SETTINGS

        # Restore the user selected font for the table view.
        self.font_info = self._get_setting("font-info")
        if self.font_info is not None:
            font = QtGui.QFont()
            if font.fromString(self.font_info):
                self.list_table.setFont(font)
                self.uncertain_code_references_table.setFont(font)
                self.uncertain_data_references_table.setFont(font)
                self.orphaned_blocks_table.setFont(font)
                self.symbols_table.setFont(font)

        # Restore the layout of the main window and the dock windows.
        window_geometry = self._get_setting("window-geometry")
        if window_geometry is not None:
            self.restoreGeometry(window_geometry)

        window_state = self._get_setting("window-state")
        if window_state is not None:
            self.restoreState(window_state)

        ## INITIALISE APPLICATION STATE

        # State related to having something loaded.
        self.view_address_stack = []

    def closeEvent(self, event):
        """ Intercept the window close event and anything which needs to happen first. """

        # Needed to allow the script to exit (ensures the editor state worker thread is exited).
        self.editor_state.on_app_exit()

        # Persist window layout.
        self._set_setting("window-geometry", self.saveGeometry())
        self._set_setting("window-state", self.saveState())

        # Let the window close.
        event.accept()

    def list_table_selection_change_event(self, result):
        """
        The goal of this method is to notify the editor state when the user changes the selected line number.
        At this time, it assumes single row selection, as that is what our code above configures.
        """
        selected_indexes = result[0].indexes()
        if len(selected_indexes) > 0:
            # Multiple cells in the same row are selected.
            self.editor_state.set_line_number(self.editor_client, selected_indexes[0].row())

        # If the line has operands, set the selection to the first one.
        self.set_selected_operand(0)

    def create_dock_windows(self):
        dock = QtWidgets.QDockWidget("Log", self)
        dock.setAllowedAreas(QtCore.Qt.BottomDockWidgetArea)
        self.log_model = create_table_model(self, [ ("Time", str), ("Level", str), ("System", str), ("Description", str), ])
        self.tracked_models.append(self.log_model)
        self.log_table = create_table_widget(dock, self.log_model)
        self.log_table.setAlternatingRowColors(True) # Non-standard
        dock.setWidget(self.log_table)
        self.addDockWidget(QtCore.Qt.BottomDockWidgetArea, dock)
        self.viewMenu.addAction(dock.toggleViewAction())
        dock.setObjectName("dock-log") # State/geometry persistence requirement.

        dock = QtWidgets.QDockWidget("Symbol List", self)
        dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        self.symbols_model = create_table_model(self, [ ("Address", hex), ("Symbol", str), ])
        self.tracked_models.append(self.symbols_model)
        self.symbols_table = create_table_widget(dock, self.symbols_model)
        self.symbols_table.setSortingEnabled(True) # Non-standard
        dock.setWidget(self.symbols_table)
        dock.setFocusProxy(self.symbols_table)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self.viewMenu.addAction(dock.toggleViewAction())
        dock.setObjectName("dock-symbols") # State/geometry persistence requirement.
        # Double-click on a row to scroll the view to the address for that row.
        def symbols_doubleClicked(index):
            row_index = index.row()
            new_address = self.symbols_model._lookup_cell_value(row_index, 0)
            self.scroll_to_address(new_address)
        self.symbols_table.doubleClicked.connect(symbols_doubleClicked)

        # The "Uncertain Code References" list is currently hidden by default.
        dock = QtWidgets.QDockWidget("Uncertain Code References", self)
        dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        self.uncertain_code_references_model = create_table_model(self, [ ("Address", hex), ("Value", hex), ("Source Code", str), ])
        self.tracked_models.append(self.uncertain_code_references_model)
        self.uncertain_code_references_table = create_table_widget(dock, self.uncertain_code_references_model, multiselect=True)
        self.uncertain_code_references_table.setSortingEnabled(True) # Non-standard
        dock.setWidget(self.uncertain_code_references_table)
        dock.setFocusProxy(self.uncertain_code_references_table)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self.viewMenu.addAction(dock.toggleViewAction())
        dock.setObjectName("dock-uncertain-code-references") # State/geometry persistence requirement.
        dock.hide()
        # Double-click on a row to scroll the view to the address for that row.
        def uncertain_code_references_doubleClicked(index):
            row_index = index.row()
            new_address = self.uncertain_code_references_model._lookup_cell_value(row_index, UNCERTAIN_ADDRESS_IDX)
            self.scroll_to_address(new_address)
        self.uncertain_code_references_table.doubleClicked.connect(uncertain_code_references_doubleClicked)
        def uncertain_code_references_customContextMenuRequested(pos):
            relocate_action = QtWidgets.QAction("Create label", self, statusTip="Specify selected rows should use labels in place of their absolute addresses", triggered=lambda:self._create_labels_for_selected_rows(self.uncertain_code_references_table, UNCERTAIN_ADDRESS_IDX))
            menu = QtWidgets.QMenu(self)
            menu.addAction(relocate_action)
            menu.exec_(self.uncertain_code_references_table.mapToGlobal(pos))
        self.uncertain_code_references_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.uncertain_code_references_table.customContextMenuRequested.connect(uncertain_code_references_customContextMenuRequested)

        # The "Uncertain Data References" list is currently hidden by default.
        dock = QtWidgets.QDockWidget("Uncertain Data References", self)
        dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        self.uncertain_data_references_model = create_table_model(self, [ ("Address", hex), ("Value", hex), ("Source Code", str), ])
        self.tracked_models.append(self.uncertain_data_references_model)
        self.uncertain_data_references_table = create_table_widget(dock, self.uncertain_data_references_model, multiselect=True)
        self.uncertain_data_references_table.setSortingEnabled(True) # Non-standard
        dock.setWidget(self.uncertain_data_references_table)
        dock.setFocusProxy(self.uncertain_data_references_table)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self.viewMenu.addAction(dock.toggleViewAction())
        dock.setObjectName("dock-uncertain-data-references") # State/geometry persistence requirement.
        dock.hide()
        # Double-click on a row to scroll the view to the address for that row.
        def uncertain_data_references_doubleClicked(index):
            row_index = index.row()
            new_address = self.uncertain_data_references_model._lookup_cell_value(row_index, UNCERTAIN_ADDRESS_IDX)
            self.scroll_to_address(new_address)
        self.uncertain_data_references_table.doubleClicked.connect(uncertain_data_references_doubleClicked)
        def uncertain_data_references_customContextMenuRequested(pos):
            relocate_action = QtWidgets.QAction("Apply labelisation", self, statusTip="Specify selected rows should use labels in place of their absolute addresses", triggered=lambda:self._create_labels_for_selected_rows(self.uncertain_data_references_table, UNCERTAIN_ADDRESS_IDX))
            menu = QtWidgets.QMenu(self)
            menu.addAction(relocate_action)
            menu.exec_(self.uncertain_data_references_table.mapToGlobal(pos))
        self.uncertain_data_references_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.uncertain_data_references_table.customContextMenuRequested.connect(uncertain_data_references_customContextMenuRequested)

        dock = QtWidgets.QDockWidget("Segment List", self)
        dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        model = create_table_model(self, [ ("#", int), ("Type", str), ("Memory", int), ("Disk", int), ("Relocs", int), ("Symbols", int), ])
        self.tracked_models.append(model)
        self.segments_table = create_table_widget(dock, model)
        dock.setWidget(self.segments_table)
        dock.setFocusProxy(self.segments_table)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self.viewMenu.addAction(dock.toggleViewAction())
        dock.setObjectName("dock-segments") # State/geometry persistence requirement.

        dock = QtWidgets.QDockWidget("Orphaned Blocks", self)
        dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        model = create_table_model(self, [ ("Address", hex), ("Length", int), ("Source Code", str), ])
        self.tracked_models.append(model)
        self.orphaned_blocks_table = create_table_widget(dock, model)
        dock.setWidget(self.orphaned_blocks_table)
        dock.setFocusProxy(self.orphaned_blocks_table)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self.viewMenu.addAction(dock.toggleViewAction())
        dock.setObjectName("dock-orphaned-blocks") # State/geometry persistence requirement.

    def create_menus(self):
        self.open_action = QAction("&Open file", self, shortcut="Ctrl+O", statusTip="Disassemble a new file", triggered=self.menu_file_open)
        self.save_project_action = QAction("&Save project", self, statusTip="Save currently loaded project", triggered=self.interaction_request_save_project)
        #self.save_project_as_action = QAction("Save project as..", self, statusTip="Save currently loaded project under a specified name", triggered=self.interaction_request_save_project_as)
        self.export_source_action = QAction("&Export source", self, statusTip="Export source code", triggered=self.interaction_request_export_source)
        self.quit_action = QAction("&Quit", self, shortcut="Ctrl+Q", statusTip="Quit the application", triggered=self.menu_file_quit)

        self.edit_undo_action = QAction("Undo", self, shortcut="Ctrl+Z", statusTip="Undo the last action", triggered=self.interaction_undo_last_action)
        self.edit_redo_action = QAction("Redo", self, shortcut="Ctrl+Y", statusTip="Redo the last action", triggered=self.interaction_redo_from_action_stack)

        self.edit_datatype_submenu_action = QAction("Change address datatype", self, statusTip="Change data type at current address")
        self.edit_set_datatype_code_action = QAction("Code", self, shortcut="Alt+C", statusTip="Change data type to code", triggered=self.interaction_set_datatype_code)
        self.edit_set_datatype_32bit_action = QAction("32 bit", self, shortcut="Alt+3", statusTip="Change data type to 32 bit", triggered=self.interaction_set_datatype_32bit)
        self.edit_set_datatype_16bit_action = QAction("16 bit", self, shortcut="Alt+2", statusTip="Change data type to 16 bit", triggered=self.interaction_set_datatype_16bit)
        self.edit_set_datatype_8bit_action = QAction("8 bit", self, shortcut="Alt+1", statusTip="Change data type to 8 bit", triggered=self.interaction_set_datatype_8bit)
        self.edit_set_datatype_ascii_action = QAction("ASCII", self, shortcut="Alt+A", statusTip="Change data type to ascii", triggered=self.interaction_set_datatype_ascii)

        self.edit_numericbase_submenu_action = QAction("Operand numeric base", self, statusTip="Change numeric base of selected operand")
        self.edit_set_numericbase_decimal_action = QAction("Decimal", self, statusTip="Change numeric base to decimal", triggered=lambda: None)
        self.edit_set_numericbase_hexadecimal_action = QAction("Hexadecimal", self, statusTip="Change numeric base to hexadecimal", triggered=lambda: None)
        self.edit_set_numericbase_binary_action = QAction("Binary", self, statusTip="Change numeric base to binary", triggered=lambda: None)

        self.search_find = QAction("Find..", self, statusTip="Find some specific text", triggered=self.menu_search_find)
        self.goto_address_action = QAction("Go to address", self, shortcut="Ctrl+G", statusTip="View a specific address", triggered=self.menu_search_goto_address)
        if True:
            self.search_previous_submenu_action = QAction("Previous", self, statusTip="Previous navigation options")
            self.search_previous_code_block_action = QAction("Go to previous code block", self, shortcut="Ctrl+Shift+C", statusTip="View previous code block", triggered=self.menu_search_goto_previous_code_block)
            self.search_previous_data_block_action = QAction("Go to previous data block", self, shortcut="Ctrl+Shift+D", statusTip="View previous data block", triggered=self.menu_search_goto_previous_data_block)
            self.search_previous_text_match_action = QAction("Go to previous text match", self, shortcut="Ctrl+Shift+F", statusTip="View previous text match", triggered=self.menu_search_goto_previous_text_match)
        if True:
            self.search_next_submenu_action = QAction("Next", self, statusTip="Next navigation options")
            self.search_next_code_block_action = QAction("Go to next code block", self, shortcut="Ctrl+C", statusTip="View next code block", triggered=self.menu_search_goto_next_code_block)
            self.search_next_data_block_action = QAction("Go to next data block", self, shortcut="Ctrl+D", statusTip="View next data block", triggered=self.menu_search_goto_next_data_block)
            self.search_next_text_match_action = QAction("Go to next text match", self, shortcut="Ctrl+F", statusTip="View next text match", triggered=self.menu_search_goto_next_text_match)
        self.choose_font_action = QAction("Select disassembly font", self, statusTip="Change the font used in the disassembly view", triggered=self.menu_settings_choose_font)

        self.file_menu = self.menuBar().addMenu("&File")
        self.file_menu.addAction(self.open_action)
        self.file_menu.addAction(self.save_project_action)
        #self.file_menu.addAction(self.save_project_as_action)
        self.file_menu.addAction(self.export_source_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.quit_action)

        self.edit_menu = self.menuBar().addMenu("&Edit")
        self.edit_menu.addAction(self.edit_undo_action)
        self.edit_menu.addAction(self.edit_redo_action)
        self.edit_menu.addAction(self.edit_datatype_submenu_action)
        self.edit_menu.addAction(self.edit_numericbase_submenu_action)
        if True:
            self.edit_datatype_submenu = QtWidgets.QMenu(self.edit_menu)
            self.edit_datatype_submenu.addAction(self.edit_set_datatype_code_action)
            self.edit_datatype_submenu.addAction(self.edit_set_datatype_32bit_action)
            self.edit_datatype_submenu.addAction(self.edit_set_datatype_16bit_action)
            self.edit_datatype_submenu.addAction(self.edit_set_datatype_8bit_action)
            self.edit_datatype_submenu.addAction(self.edit_set_datatype_ascii_action)
            self.edit_datatype_submenu_action.setMenu(self.edit_datatype_submenu)
        if True:
            self.edit_numericbase_submenu = QtWidgets.QMenu(self.edit_menu)
            self.edit_numericbase_submenu.addAction(self.edit_set_numericbase_decimal_action)
            self.edit_set_numericbase_decimal_action.setEnabled(False)
            self.edit_numericbase_submenu.addAction(self.edit_set_numericbase_hexadecimal_action)
            self.edit_set_numericbase_hexadecimal_action.setEnabled(False)
            self.edit_numericbase_submenu.addAction(self.edit_set_numericbase_binary_action)
            self.edit_set_numericbase_binary_action.setEnabled(False)
            self.edit_numericbase_submenu_action.setMenu(self.edit_numericbase_submenu)

        self.search_menu = self.menuBar().addMenu("&Search")
        self.search_menu.addAction(self.search_find)
        self.search_menu.addAction(self.goto_address_action)
        self.search_menu.addAction(self.search_previous_submenu_action)
        if True:
            self.search_previous_submenu = QtWidgets.QMenu(self.search_menu)
            self.search_previous_submenu.addAction(self.search_previous_data_block_action)
            self.search_previous_submenu.addAction(self.search_previous_code_block_action)
            self.search_previous_submenu.addAction(self.search_previous_text_match_action)
            self.search_previous_submenu_action.setMenu(self.search_previous_submenu)
        self.search_menu.addAction(self.search_next_submenu_action)
        if True:
            self.search_next_submenu = QtWidgets.QMenu(self.search_menu)
            self.search_next_submenu.addAction(self.search_next_data_block_action)
            self.search_next_submenu.addAction(self.search_next_code_block_action)
            self.search_next_submenu.addAction(self.search_next_text_match_action)
            self.search_next_submenu_action.setMenu(self.search_next_submenu)

        self.viewMenu = self.menuBar().addMenu("&View")

        self.settings_menu = self.menuBar().addMenu("Settings")
        self.settings_menu.addAction(self.choose_font_action)

    def create_shortcuts(self):
        ## Main disassembly list table.
        # Place the current location on the browsing stack, and go to the address of the referenced symbol.
        QShortcut(QtGui.QKeySequence(self.tr("Ctrl+Right")), self.list_table, self.interaction_view_push_symbol).setContext(QtCore.Qt.WidgetShortcut)
        # Go back in the browsing stack.
        QShortcut(QtGui.QKeySequence(self.tr("Ctrl+Left")), self.list_table, self.interaction_view_pop_symbol).setContext(QtCore.Qt.WidgetShortcut)
        # Display referring addresses.
        QShortcut(QtGui.QKeySequence(self.tr("Shift+Ctrl+Right")), self.list_table, self.interaction_view_referring_symbols).setContext(QtCore.Qt.WidgetShortcut)
        # Edit the name of a label.
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Return), self.list_table, self.interaction_rename_symbol).setContext(QtCore.Qt.WidgetShortcut)
        # ...
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_1), self.list_table, self.interaction_select_operand_1).setContext(QtCore.Qt.WidgetShortcut)
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_2), self.list_table, self.interaction_select_operand_2).setContext(QtCore.Qt.WidgetShortcut)
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_3), self.list_table, self.interaction_select_operand_3).setContext(QtCore.Qt.WidgetShortcut)
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Left), self.list_table, self.interaction_select_operand_previous).setContext(QtCore.Qt.WidgetShortcut)
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Right), self.list_table, self.interaction_select_operand_next).setContext(QtCore.Qt.WidgetShortcut)

        ## Uncertain code references list table.
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Return), self.uncertain_code_references_table, self.interaction_uncertain_code_references_view_push_symbol).setContext(QtCore.Qt.WidgetShortcut)
        ## Uncertain data references list table.
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Return), self.uncertain_data_references_table, self.interaction_uncertain_data_references_view_push_symbol).setContext(QtCore.Qt.WidgetShortcut)
        QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Space), self.uncertain_data_references_table, self.interaction_show_row_contextmenu).setContext(QtCore.Qt.WidgetShortcut)

    def reset_all(self):
        self.reset_ui()
        self.reset_state()

    def reset_ui(self):
        for model in self.tracked_models:
            model._clear_data()

    def reset_state(self):
        """ Called to clear out all state related to loaded data. """
        self.setWindowTitle(APPLICATION_NAME)

    def menu_file_open(self):
        if self.editor_state.in_loaded_state(self.editor_client):
            ret = QtWidgets.QMessageBox.question(self, "Abandon work?", "You have existing work loaded, do you wish to abandon it?", QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel)
            if ret != QtWidgets.QMessageBox.Ok:
                return
            self.reset_all()
        elif not self.editor_state.in_initial_state(self.editor_client):
            return

        self.attempt_open_file()

    def menu_file_quit(self):
        if QtWidgets.QMessageBox.question(self, "Quit..", "Are you sure you wish to quit?", QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel):
            self.close()

    def menu_search_find(self):
        errmsg = self.editor_state.search_text(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
        else:
            self.scroll_to_line(self.editor_state.get_line_number(self.editor_client))

    def menu_search_goto_address(self):
        errmsg = self.editor_state.goto_address(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
        else:
            self.scroll_to_line(self.editor_state.get_line_number(self.editor_client))

    def menu_search_goto_previous_code_block(self):
        errmsg = self.editor_state.goto_previous_code_block(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
        else:
            self.scroll_to_line(self.editor_state.get_line_number(self.editor_client))

    def menu_search_goto_previous_data_block(self):
        errmsg = self.editor_state.goto_previous_data_block(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
        else:
            self.scroll_to_line(self.editor_state.get_line_number(self.editor_client))

    def menu_search_goto_previous_text_match(self):
        errmsg = self.editor_state.goto_previous_text_match(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
        else:
            self.scroll_to_line(self.editor_state.get_line_number(self.editor_client))

    def menu_search_goto_next_code_block(self):
        errmsg = self.editor_state.goto_next_code_block(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
        else:
            self.scroll_to_line(self.editor_state.get_line_number(self.editor_client))

    def menu_search_goto_next_data_block(self):
        errmsg = self.editor_state.goto_next_data_block(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
        else:
            self.scroll_to_line(self.editor_state.get_line_number(self.editor_client))

    def menu_search_goto_next_text_match(self):
        errmsg = self.editor_state.goto_next_text_match(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
        else:
            self.scroll_to_line(self.editor_state.get_line_number(self.editor_client))

    def menu_settings_choose_font(self):
        font, ok = QtWidgets.QFontDialog.getFont(self.list_table.font(), self)
        if font and ok:
            self.list_table.setFont(font)
            self.uncertain_code_references_table.setFont(font)
            self.uncertain_data_references_table.setFont(font)
            self.orphaned_blocks_table.setFont(font)
            self._set_setting("font-info", font.toString())

    ## INTERACTION FUNCTIONS

    def interaction_request_save_project(self):
        errmsg = self.editor_state.save_project(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Unable to save project", errmsg)

    def interaction_request_export_source(self):
        errmsg = self.editor_state.export_source_code(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Unable to export source", errmsg)

    def interaction_rename_symbol(self):
        errmsg = self.editor_state.set_label_name(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)

    def set_selected_operand(self, operand_index):
        line_index = self.editor_state.set_selected_operand(self.editor_client, operand_index)
        if line_index is not None:
            self.list_model._data_changed(0, line_index, 5, line_index)
            return True
        return False

    def get_selected_operand(self):
        return self.editor_state.get_selected_operand(self.editor_client)

    def interaction_select_operand_1(self):
        self.set_selected_operand(0)

    def interaction_select_operand_2(self):
        self.set_selected_operand(1)

    def interaction_select_operand_3(self):
        self.set_selected_operand(2)

    def interaction_select_operand_previous(self):
        line_index = self.editor_state.select_next_operand(self.editor_client)
        if line_index is not None:
            self.list_model._data_changed(0, line_index, 5, line_index)

    def interaction_select_operand_next(self):
        line_index = self.editor_state.select_previous_operand(self.editor_client)
        if line_index is not None:
            self.list_model._data_changed(0, line_index, 5, line_index)

    def interaction_show_row_contextmenu(self):
        # Get the currently selected row.
        if len(self.uncertain_data_references_table._selected_indexes):
            y = self.uncertain_data_references_table.rowViewportPosition(self.uncertain_data_references_table._selected_indexes[0].row())
            x = self.uncertain_data_references_table.columnWidth(0) / 2
            self.uncertain_data_references_table.customContextMenuRequested.emit(QtCore.QPoint(x, y))

    def interaction_uncertain_code_references_view_push_symbol(self):
        if not self.editor_state.in_loaded_state(self.editor_client):
            return

        # Place current address on the stack.
        current_address = self.editor_state.get_address(self.editor_client)
        if current_address is None:
            return

        # View selected uncertain code reference address.
        row_idx = self.uncertain_code_references_table.currentIndex().row()
        address = self.uncertain_code_references_model._lookup_cell_value(row_idx, UNCERTAIN_ADDRESS_IDX)
        self.functionality_view_push_address(current_address, address)
        self.list_table.setFocus()

    def interaction_uncertain_data_references_view_push_symbol(self):
        if not self.editor_state.in_loaded_state(self.editor_client):
            return

        # Place current address on the stack.
        current_address = self.editor_state.get_address(self.editor_client)
        if current_address is None:
            return

        # View selected uncertain code reference address.
        row_idx = self.uncertain_data_references_table.currentIndex().row()
        address = self.uncertain_data_references_model._lookup_cell_value(row_idx, UNCERTAIN_ADDRESS_IDX)
        self.functionality_view_push_address(current_address, address)
        self.list_table.setFocus()

    def interaction_view_push_symbol(self):
        errmsg = self.editor_state.push_address(self.editor_client)
        if type(errmsg) is str:
            # Fallback behaviour, select any uncertain data reference in the custom window.
            if errmsg == editor_state.ERRMSG_NO_IDENTIFIABLE_DESTINATION:
                errmsg = self.editor_state.if_uncertain_data_reference_address(self.editor_client)
                if errmsg is None:
                    address = self.editor_state.get_address(self.editor_client)
                    new_line_idx = self.uncertain_data_references_model._index_cell_value(0, address)
                    if new_line_idx > -1:
                        self.uncertain_data_references_table.selectRow(new_line_idx)
                        index = self.uncertain_data_references_model.index(new_line_idx, 2, QtCore.QModelIndex())
                        self.uncertain_data_references_table.scrollTo(index, QtWidgets.QAbstractItemView.PositionAtCenter)
                        dockWidget = self.uncertain_data_references_table.parentWidget()
                        if dockWidget.isHidden():
                            dockWidget.show()
                        if isinstance(dockWidget, QtWidgets.QDockWidget):
                            dockWidget.setFocus()
                        return
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
            return
        line_index = self.editor_state.get_line_number(self.editor_client)
        self.scroll_to_line(line_index, True)

    def interaction_view_pop_symbol(self):
        errmsg = self.editor_state.pop_address(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
            return
        line_idx = self.editor_state.get_line_number(self.editor_client)
        self.scroll_to_line(line_idx, True)

    def interaction_view_referring_symbols(self):
        errmsg = self.editor_state.goto_referring_address(self.editor_client)
        if errmsg is False:
            return
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Error", errmsg)
            return
        line_idx = self.editor_state.get_line_number(self.editor_client)
        self.scroll_to_line(line_idx, True)

    def interaction_undo_last_action(self):
        pass

    def interaction_redo_from_action_stack(self):
        pass

    def interaction_set_datatype_code(self):
        # May change current line number due to following references above in the file.
        #address = self.editor_state.get_address(self.editor_client)
        errmsg = self.editor_state.set_datatype_code(self.editor_client)
        #self.scroll_to_line(self.editor_state.get_line_number(self.editor_client), True)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Unable to change block datatype", errmsg)

    def interaction_set_datatype_32bit(self):
        errmsg = self.editor_state.set_datatype_32bit(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Unable to change block datatype", errmsg)

    def interaction_set_datatype_16bit(self):
        errmsg = self.editor_state.set_datatype_16bit(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Unable to change block datatype", errmsg)

    def interaction_set_datatype_8bit(self):
        errmsg = self.editor_state.set_datatype_8bit(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Unable to change block datatype", errmsg)

    def interaction_set_datatype_ascii(self):
        errmsg = self.editor_state.set_datatype_ascii(self.editor_client)
        if type(errmsg) is str:
            QtWidgets.QMessageBox.information(self, "Unable to change block datatype", errmsg)

    ## MISCELLANEIA

    def scroll_to_address(self, new_address):
        new_line_idx = self.editor_state.get_line_number_for_address(self.editor_client, new_address)
        logger.debug("scroll_to_address: line=%d address=$%X", new_line_idx, new_address)
        self.scroll_to_line(new_line_idx, True)

    def scroll_to_line(self, new_line_idx, other=False):
        if not other:
            logger.debug("scroll_to_line line=%d", new_line_idx)
        index = self.list_model.index(new_line_idx, 0, QtCore.QModelIndex())
        #self.list_table.selectionModel().setCurrentIndex(index, QtCore.QItemSelectionModel.ClearAndSelect)
        self.list_table.scrollTo(index, QtWidgets.QAbstractItemView.PositionAtCenter)
        self.list_table.setFocus()
        # NOTE(rmtew): The table is single row selection, but leaves rows selected resulting in multiple rows!
        self.list_table.clearSelection()
        self.list_table.selectRow(new_line_idx)

    def functionality_view_push_address(self, current_address, address):
        self.view_address_stack.append(current_address)
        next_line_number = self.editor_state.get_line_number_for_address(self.editor_client, address)
        if next_line_number is not None:
            self.scroll_to_line(next_line_number)
            logger.info("view push symbol going to address %06X / line number %d." % (address, next_line_number))
        else:
            logger.error("view push symbol for address %06X unable to resolve line number." % address)

    def get_current_address(self):
        # Place current address on the stack.
        current_address = self.editor_state.get_address(self.editor_client)
        # Whether a non-disassembly "readability" line was selected.
        if current_address is None:
            logger.debug("Failed to get current address, no address for line.")
            return
        return current_address

    def _set_setting(self, setting_name, setting_value):
        # type: (str, Any) -> None
        self._settings[setting_name] = setting_value
        with open(SETTINGS_FILE, "wb") as f:
            pickle.dump(self._settings, f)

    def _get_setting(self, setting_name, default_value=None):
        # type: (str, Any) -> Any
        if self._settings is None:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "rb") as f:
                    self._settings = pickle.load(f)
            else:
                self._settings = {}
        return self._settings.get(setting_name, default_value)

    def attempt_open_file(self, file_path=None):
        result = self.editor_state.load_file(self.editor_client)
        # Cancelled?
        if result is None:
            return
        # Error message?
        if type(result) is str:
            QtWidgets.QMessageBox.information(self, "Unable to open file", result)
            return

        # This isn't really good enough, as long loading files may conflict with cancellation and subsequent load attempts.
        if not self.editor_state.in_loaded_state(self.editor_client):
            return

        # Successfully completed.
        self.on_file_opened()

    def on_file_load_start(self, file_path):
        self.setWindowTitle("%s - %s" % (APPLICATION_NAME, os.path.basename(file_path)))

    def on_file_opened(self):
        self.list_table._initial_line_idx = self.editor_state.get_line_number(self.editor_client)

        ## Populate the disassembly view with the loaded data.
        self.list_model._data_ready()

        ## SYMBOLS

        row_data = list(self.editor_state.get_symbols(self.editor_client))
        self.symbols_model._sort_list(row_data)
        self.symbols_model._set_row_data(row_data, addition_rows=(0, len(row_data)-1))
        self.symbols_table.resizeColumnsToContents()
        self.symbols_table.horizontalHeader().setStretchLastSection(True)

        ## UNCERTAIN REFERENCES

        results = self.editor_state.get_uncertain_code_references(self.editor_client)
        self.uncertain_code_references_model._set_row_data(results, addition_rows=(0, len(results)-1))
        self.uncertain_code_references_table.resizeColumnsToContents()
        self.uncertain_code_references_table.horizontalHeader().setStretchLastSection(True)

        results = self.editor_state.get_uncertain_data_references(self.editor_client)
        self.uncertain_data_references_model._set_row_data(results, addition_rows=(0, len(results)-1))
        self.uncertain_data_references_table.resizeColumnsToContents()
        self.uncertain_data_references_table.horizontalHeader().setStretchLastSection(True)

        ## DONE LOADING ##

        self.loaded_signal.emit(0)

    def on_pre_line_change(self, args):
        line0, line_count = args
        if line_count == 0:
            pass
        else:
            self.list_model._begin_row_change(line0, line_count)

    def on_post_line_change(self, args):
        line0, line_count = args
        if line_count == 0:
            #for i in range(self.list_model._column_count):
            #    self.list_table.update(self.list_model.createIndex(line0, i))
            self.list_model._data_changed(0, line0, 5, line0)
        else:
            self.list_model._end_row_change(line0, line_count)

    def on_disassembly_symbol_added(self, args):
        symbol_address, symbol_label = args
        logger.info("on_disassembly_symbol_added: %x %s", symbol_address, symbol_label)

        self._add_rows_to_model(self.symbols_model, [ (symbol_address, symbol_label), ])

        self.symbols_table.resizeColumnsToContents()
        self.symbols_table.horizontalHeader().setStretchLastSection(True)

    def on_disassembly_symbol_removed(self, args):
        # TODO: When these events are actually sent, remove this note indicating the case is otherwise.
        symbol_address, symbol_label = args
        logger.info("on_disassembly_symbol_removed: UNTESTED %x %s", symbol_address, symbol_label)

        self._remove_address_range_from_model(self.symbols_model, symbol_address, 1)

        self.symbols_table.resizeColumnsToContents()
        self.symbols_table.horizontalHeader().setStretchLastSection(True)

    def on_uncertain_reference_modification(self, args):
        data_type_from, data_type_to, address, length = args
        logger.info("on_uncertain_reference_modification: %s %s %x %d", data_type_from, data_type_to, address, length)
        if data_type_from == "CODE":
            from_model = self.uncertain_code_references_model
        else:
            from_model = self.uncertain_data_references_model
        self._remove_address_range_from_model(from_model, address, length)

        addition_rows = self.editor_state.get_uncertain_references_by_address(self.editor_client, address)
        if len(addition_rows):
            if data_type_to == "CODE":
                to_model = self.uncertain_code_references_model
            else:
                to_model = self.uncertain_data_references_model
            self._add_rows_to_model(to_model, addition_rows)

    def _remove_address_range_from_model(self, from_model, address, length):
        from_row_data = from_model._get_row_data()
        # Bundle addresses to remove into contiguous batches.
        removal_idx0 = removal_idxN = None
        batches = []
        for i, entry in enumerate(from_row_data):
            if entry[0] >= address and entry[0] < address + length:
                if removal_idx0 is None:
                    removal_idx0 = i
                else:
                    removal_idxN = i
            elif removal_idx0 is not None:
                batches.append((removal_idx0, removal_idx0 if removal_idxN is None else removal_idxN))
                removal_idx0 = removal_idxN = None
        else:
            if removal_idx0 is not None:
                batches.append((removal_idx0, removal_idx0 if removal_idxN is None else removal_idxN))
        # Clip out from the end backwards, so indexes do not change due to removal of preceding data.
        batches.reverse()
        for idx0, idxN in batches:
            from_row_data[idx0:idxN+1] = []
            from_model._set_row_data(from_row_data, removal_rows=(idx0, idxN))

    def _add_rows_to_model(self, to_model, addition_rows):
        to_row_data = to_model._get_row_data()
        # Ensure we do not break the original reference ordering.
        addition_rows = addition_rows[:]
        # Sort the rows to be added in the same ordering as the model rows.
        to_model._sort_list(addition_rows)

        if to_model._sort_order == QtCore.Qt.AscendingOrder:
            op = operator.le
        else:
            op = operator.ge
        sort_column1 = to_model._sort_column1
        to_index = from_index = 0
        insert_ranges = []
        while to_index < len(to_row_data) and from_index < len(addition_rows):
            insert_row = addition_rows[from_index]
            if op(insert_row[sort_column1], to_row_data[to_index][sort_column1]):
                doesNotExistAlready = insert_row != to_row_data[to_index]
                if doesNotExistAlready:
                    to_row_data.insert(to_index, insert_row)
                if len(insert_ranges) and insert_ranges[-1][1] == to_index-1:
                    insert_ranges[-1][1] = to_index
                else:
                    if len(insert_ranges):
                        to_model._set_row_data(to_row_data, addition_rows=(insert_ranges[-1][0], insert_ranges[-1][1]))
                    if doesNotExistAlready:
                        insert_ranges.append([ to_index, to_index ])
                from_index += 1
            to_index += 1
        if len(insert_ranges):
            to_model._set_row_data(to_row_data, addition_rows=(insert_ranges[-1][0], insert_ranges[-1][1]))

    def show_progress_dialog(self, args):
        title, description, can_cancel, step_count, abort_callback = args

        # Display a modal dialog.
        d = self._progress_dialog = QtWidgets.QProgressDialog(self)
        self._progress_dialog_steps = step_count
        if can_cancel:
            d.setCancelButtonText("&Cancel")
        else:
            d.setCancelButtonText("")
        d.setWindowTitle(title)
        d.setLabelText(description)
        d.setAutoClose(True)
        d.setWindowModality(QtCore.Qt.WindowModal)
        d.setRange(0, step_count)
        d.setMinimumDuration(1000)
        d.setValue(0)

        # Register to hear if the cancel button is pressed.
        def canceled():
            abort_callback()
        d.canceled.connect(canceled)

        # Non-blocking.
        d.show()

    def update_progress_dialog(self, args):
        message, step_number = args
        d = self._progress_dialog
        d.setLabelText(message)
        d.setValue(step_number)

    def close_progress_dialog(self):
        d = self._progress_dialog
        # Trigger the auto-close behaviour.
        d.setValue(self._progress_dialog_steps)

        self._progress_dialog = None
        self._progress_dialog_steps = 0

    def _is_uncertain_data_reference(self, line_number):
        address = self.editor_state.get_address_for_line_number(self.editor_client, line_number)
        return self.uncertain_data_references_model._index_cell_value(0, address) > -1

    def _get_rows_from_indices(self, indices):
        # Whether the selection model is per-row (rather than per-cell) or not, we get all
        # selected cells.  So use a set generator expression to get a unique set of rows.
        return { indice.row() for indice in indices }

    def _create_labels_for_selected_rows(self, table, address_row_idx):
        model = table.model()
        selected_row_indices = self._get_rows_from_indices(table.selectedIndexes())
        addresses = set()
        for row_idx in selected_row_indices:
            addresses.add(model._lookup_cell_value(row_idx, address_row_idx))
        for address in addresses:
            self.scroll_to_address(address)
            self.editor_state.add_label_for_value(self.editor_client)


## Option dialogs.

class ClickableLabel(QtWidgets.QLabel):
    clicked = QtCore.Signal()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()

def _make_inputdata_options(dialog, group_title, keep_input_data=True):
    """
    This uses two workarounds resulting in a second-rate solution:
    - Radio button text does not wrap, so the workaround is that no text is
      given to the radio buttons.  Instead the text is displayed using labels.
    - Labels cannot be clicked on to get the related radio buttons to depress.
      Instead, a custom label class is created that responds to mouse press
      events.  This leaves gaps in the clickable areas.
    """
    dialog.inputdata_do_radio = QtGui.QRadioButton()
    dialog.inputdata_dont_radio = QtGui.QRadioButton()
    inputdata_do_short_label = ClickableLabel("Saved work SHOULD contain source/input file data.")
    inputdata_do_short_label.clicked.connect(dialog.inputdata_do_radio.click)
    inputdata_do_long_label = ClickableLabel("When you load your saved work, you WILL NOT need to provide the source/input file.")
    inputdata_do_long_label.clicked.connect(dialog.inputdata_do_radio.click)
    inputdata_dont_short_label = ClickableLabel("Saved work SHOULD NOT contain source/input file data.")
    inputdata_dont_short_label.clicked.connect(dialog.inputdata_dont_radio.click)
    inputdata_dont_long_label = ClickableLabel("When you load your saved work, you WILL need to provide the source/input file.")
    inputdata_dont_long_label.clicked.connect(dialog.inputdata_dont_radio.click)
    if keep_input_data:
        dialog.inputdata_do_radio.setChecked(True)
    else:
        dialog.inputdata_dont_radio.setChecked(True)
    inputdata_groupbox = QtWidgets.QGroupBox(group_title)
    inputdata_layout = QtWidgets.QGridLayout()
    inputdata_layout.addWidget(dialog.inputdata_do_radio, 0, 0)
    inputdata_layout.addWidget(inputdata_do_short_label, 0, 1)
    inputdata_layout.addWidget(inputdata_do_long_label, 1, 1)
    inputdata_layout.addWidget(dialog.inputdata_dont_radio, 2, 0)
    inputdata_layout.addWidget(inputdata_dont_short_label, 2, 1)
    inputdata_layout.addWidget(inputdata_dont_long_label, 3, 1)
    inputdata_groupbox.setLayout(inputdata_layout)
    return inputdata_groupbox

def _set_default_font(widget):
    font = QtGui.QFont()
    if not font.fromString("Arial,8,-1,5,50,0,0,0,0,0"):
        font = QtWidgets.QApplication.font()
    widget.setFont(font)


class LoadProjectDialog(QtWidgets.QDialog):
    """
    Dialog shown when the user loads an existing saved project.
    """
    def __init__(self, load_options, file_path, parent=None):
        super(LoadProjectDialog, self).__init__(parent)

        self.load_options = load_options
        _set_default_font(self)

        self.setWindowTitle("Load Project")
        self.setWindowModality(QtCore.Qt.WindowModal)

        ## Information layout.
        problem_groupbox = QtWidgets.QGroupBox("Problem")
        problem_label1 = QtWidgets.QLabel("This project does not include the original data.")
        problem_label2 = QtWidgets.QLabel("Perhaps whomever created the project opted to exclude it.")
        problem_label3 = QtWidgets.QLabel("Perhaps Peasource's cached copy was somehow deleted.")
        problem_label4 = QtWidgets.QLabel("In any case, you need to locate and provide it.")
        problem_layout = QtWidgets.QVBoxLayout()
        problem_layout.addWidget(problem_label1)
        problem_layout.addSpacing(10)
        problem_layout.addWidget(problem_label2)
        problem_layout.addWidget(problem_label3)
        problem_layout.addSpacing(10)
        problem_layout.addWidget(problem_label4)
        problem_groupbox.setLayout(problem_layout)

        original_filesize = load_options.input_file_filesize
        original_filename = load_options.input_file_filename
        original_checksum = load_options.input_file_checksum

        filespec_groupbox = QtWidgets.QGroupBox("Original file")
        filespec_layout = QtWidgets.QGridLayout()
        filename_key_label = QtWidgets.QLabel("Name:")
        filename_value_label = QtWidgets.QLabel(original_filename)
        filesize_key_label = QtWidgets.QLabel("Size:")
        filesize_value_label = QtWidgets.QLabel("%d bytes" % original_filesize)
        filechecksum_key_label = QtWidgets.QLabel("Checksum:")
        filechecksum_value_label = QtWidgets.QLabel("".join("%X" % ord(c) for c in original_checksum))
        filespec_layout.addWidget(filename_key_label, 0, 0, 1, 1)
        filespec_layout.addWidget(filename_value_label, 0, 1, 1, 19)
        filespec_layout.addWidget(filesize_key_label, 1, 0, 1, 1)
        filespec_layout.addWidget(filesize_value_label, 1, 1, 1, 19)
        filespec_layout.addWidget(filechecksum_key_label, 2, 0, 1, 1)
        filespec_layout.addWidget(filechecksum_value_label, 2, 1, 1, 19)
        filespec_groupbox.setLayout(filespec_layout)

        filelocation_groupbox = QtWidgets.QGroupBox("File location")
        filelocation_layout = QtWidgets.QVBoxLayout()
        path_layout = QtWidgets.QHBoxLayout()
        path_lineedit = QtWidgets.QLineEdit()
        path_button = QtGui.QToolButton(self) # A button that stays minimally sized.
        path_button.setText("...")
        path_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextOnly)
        path_layout.addWidget(path_lineedit)
        path_layout.addWidget(path_button)
        valid_size_checkbox = QtWidgets.QCheckBox("Size", self)
        valid_size_checkbox.setChecked(False)
        valid_size_checkbox.setEnabled(False)
        valid_checksum_checkbox = QtWidgets.QCheckBox("Checksum", self)
        valid_checksum_checkbox.setChecked(False)
        valid_checksum_checkbox.setEnabled(False)
        validity_layout = QtWidgets.QHBoxLayout()
        validity_layout.addWidget(QtWidgets.QLabel("Validity:"))
        validity_layout.addWidget(valid_size_checkbox, alignment=QtCore.Qt.AlignLeft)
        validity_layout.addWidget(valid_checksum_checkbox, alignment=QtCore.Qt.AlignLeft)
        filelocation_layout.addLayout(path_layout)
        filelocation_layout.addLayout(validity_layout)
        filelocation_groupbox.setLayout(filelocation_layout)

        # The algorithm used to enable the load button is:
        # - Wait 2 seconds after the last text change, or when return pressed.
        # - Check if given path is a file of the correct size.
        # -

        self.validation_attempt = 0
        self.validation_attempt_text = None
        self.validation_key = None

        def validate_file_path(validation_attempt, file_path):
            # Maybe the user kept typing, if so they're not finished.
            if self.validation_attempt != validation_attempt:
                return
            path_lineedit.setEnabled(False)
            if os.path.isfile(file_path):
                if os.path.getsize(file_path) == original_filesize:
                    valid_size_checkbox.setChecked(True)
                with open(file_path, "rb") as input_file:
                    file_checksum = util.calculate_file_checksum(input_file)
                if file_checksum == original_checksum:
                    valid_checksum_checkbox.setChecked(True)
                if valid_size_checkbox.isChecked() and valid_checksum_checkbox.isChecked():
                    load_button.setEnabled(True)
                    self.valid_file_path = file_path
            path_lineedit.setEnabled(True)

        def _reset_widgets():
            self.valid_file_path = None
            valid_size_checkbox.setChecked(False)
            valid_checksum_checkbox.setChecked(False)
            load_button.setEnabled(False)
        def on_path_lineedit_textChanged(new_text):
            if self.validation_attempt_text != new_text:
                _reset_widgets()
                self.validation_attempt_text = new_text
                self.validation_attempt += 1
                QtCore.QTimer.singleShot(2000, lambda n=self.validation_attempt: validate_file_path(n, new_text))
        def on_path_lineedit_returnPressed():
            if self.validation_attempt_text != path_lineedit.text():
                _reset_widgets()
                self.validation_attempt_text = path_lineedit.text()
                self.validation_attempt += 1
                validate_file_path(self.validation_attempt, path_lineedit.text())

        path_lineedit.textChanged.connect(on_path_lineedit_textChanged)
        path_lineedit.returnPressed.connect(on_path_lineedit_returnPressed)

        def on_path_button_clicked():
            options = QtWidgets.QFileDialog.Options()
            file_path, open_filter = QtWidgets.QFileDialog.getOpenFileName(self, "Locate original file..", options=options)
            if not len(file_path):
                return
            path_lineedit.setText(file_path)
        path_button.clicked.connect(on_path_button_clicked)

        ## Buttons layout.
        load_button = QtWidgets.QPushButton("Load")
        load_button.setEnabled(False)
        load_button.clicked.connect(self.accept)
        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        buttons_layout = QtWidgets.QHBoxLayout()
        buttons_layout.addWidget(load_button, QtCore.Qt.AlignRight)
        buttons_layout.addWidget(cancel_button, QtCore.Qt.AlignRight)

        ## Outer layout.
        information_layout = QtWidgets.QVBoxLayout()
        information_layout.addWidget(problem_groupbox)
        information_layout.addWidget(filespec_groupbox)
        information_layout.addWidget(filelocation_groupbox)
        information_layout.addLayout(buttons_layout)
        self.setLayout(information_layout)

    def accept(self):
        self.load_options.loader_file_path = self.valid_file_path
        return super(LoadProjectDialog, self).accept()

class SaveProjectDialog(QtWidgets.QDialog):
    """
    Dialog shown when the user saves the currently loaded project.
    """
    def __init__(self, save_options, file_path, parent=None):
        super(SaveProjectDialog, self).__init__(parent)

        self.save_options = save_options
        _set_default_font(self)

        self.setWindowTitle("Save Project")
        self.setWindowModality(QtCore.Qt.WindowModal)

        ## File options layout.
        inputdata_groupbox = _make_inputdata_options(self, "File Options", save_options.cache_input_file)

        ## Buttons layout.
        save_button = QtWidgets.QPushButton("Save")
        save_button.clicked.connect(self.accept)
        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        buttons_layout = QtWidgets.QHBoxLayout()
        buttons_layout.addWidget(save_button, QtCore.Qt.AlignRight)
        buttons_layout.addWidget(cancel_button, QtCore.Qt.AlignRight)

        ## Outer layout.
        outer_vertical_layout = QtWidgets.QVBoxLayout()
        outer_vertical_layout.addWidget(inputdata_groupbox)
        outer_vertical_layout.addLayout(buttons_layout)
        self.setLayout(outer_vertical_layout)

    def accept(self):
        self.save_options.cache_input_file = self.inputdata_do_radio.isChecked()
        return super(SaveProjectDialog, self).accept()


class NewProjectDialog(QtWidgets.QDialog):
    """
    Dialog shown when the user loads a file for disassembling (not a saved project).
    """
    def __init__(self, new_options, file_path, parent=None):
        super(NewProjectDialog, self).__init__(parent)

        _set_default_font(self)

        self.new_options = new_options
        dir_path, file_name = os.path.split(file_path)

        ## Options / information layouts.
        # File groupbox.
        file_name_key_label = QtWidgets.QLabel("Name:")
        file_name_value_label = QtWidgets.QLabel(file_name)
        file_size_key_label = QtWidgets.QLabel("Size:")
        file_size_value_label = QtWidgets.QLabel("%d bytes" % os.path.getsize(file_path))
        file_hline = QtWidgets.QFrame()
        file_hline.setFrameShape(QtWidgets.QFrame.HLine)
        file_hline.setFrameShadow(QtWidgets.QFrame.Sunken)
        file_hline.setLineWidth(0)
        file_hline.setMidLineWidth(1)

        file_type_key_label = QtWidgets.QLabel("Type:")
        s = res.get_string_by_id(res.FILE_FORMAT_KEY, new_options.loader_filetype)
        file_type_value_label = QtWidgets.QLabel(s)
        file_arch_key_label = QtWidgets.QLabel("Architecture:")
        self.file_arch_value_combobox = file_arch_value_combobox = QtWidgets.QComboBox(self)
        if new_options.is_binary_file:
            # List all supported processor options, for user to choose.
            for processor_id in disassemblylib.get_processor_ids():
                s = res.get_string_by_id(res.PROCESSOR_KEY, processor_id)
                file_arch_value_combobox.addItem(s, processor_id)
            file_arch_value_combobox.setEnabled(True)
        else:
            # Fixed processor defined by the file format.
            s = res.get_string_by_id(res.PROCESSOR_KEY, new_options.loader_processor)
            file_arch_value_combobox.addItem(s, new_options.loader_processor)
            file_arch_value_combobox.setEnabled(False)

        information_groupbox = QtWidgets.QGroupBox("File Information")
        information_layout = QtWidgets.QGridLayout()
        information_layout.addWidget(file_name_key_label, 0, 0)
        information_layout.addWidget(file_name_value_label, 0, 1)
        information_layout.addWidget(file_size_key_label, 1, 0)
        information_layout.addWidget(file_size_value_label, 1, 1)
        information_layout.addWidget(file_hline, 2, 0, 1, 2)
        information_layout.addWidget(file_type_key_label, 3, 0)
        information_layout.addWidget(file_type_value_label, 3, 1)
        information_layout.addWidget(file_arch_key_label, 4, 0)
        information_layout.addWidget(file_arch_value_combobox, 4, 1)
        information_groupbox.setLayout(information_layout)

        # Processing groupbox.
        processing_loadaddress_key_label = QtWidgets.QLabel("Load address:")
        self.processing_loadaddress_value_textedit = processing_loadaddress_value_textedit = QtWidgets.QLineEdit("0x%X" % self.new_options.loader_load_address)
        processing_loadaddress_value_textedit.setEnabled(new_options.is_binary_file)
        processing_entryaddress_key_label = QtWidgets.QLabel("Entrypoint address:")
        self.processing_entryaddress_value_textedit = processing_entryaddress_value_textedit = QtWidgets.QLineEdit("0x%X" % self.new_options.loader_entrypoint_offset)
        processing_entryaddress_value_textedit.setEnabled(new_options.is_binary_file)
        processing_hline1 = QtWidgets.QFrame()
        processing_hline1.setFrameShape(QtWidgets.QFrame.HLine)
        processing_hline1.setFrameShadow(QtWidgets.QFrame.Sunken)
        processing_hline1.setLineWidth(0)
        processing_hline1.setMidLineWidth(0)

        processing_groupbox = QtWidgets.QGroupBox("Processing")
        processing_layout = QtWidgets.QGridLayout()
        processing_layout.addWidget(processing_loadaddress_key_label, 0, 0)
        processing_layout.addWidget(processing_loadaddress_value_textedit, 0, 1)
        processing_layout.addWidget(processing_entryaddress_key_label, 1, 0)
        processing_layout.addWidget(processing_entryaddress_value_textedit, 1, 1)
        fill_row_count = information_layout.rowCount() - processing_layout.rowCount() # Need grid spacing to be equal.
        processing_layout.addWidget(processing_hline1, 2, 0, fill_row_count, 2)
        processing_groupbox.setLayout(processing_layout)

        # Gather together complete options layout.
        options_layout = QtWidgets.QHBoxLayout()
        options_layout.addWidget(information_groupbox)
        options_layout.addWidget(processing_groupbox)

        ## Buttons layout.
        create_button = QtWidgets.QPushButton("Create")
        create_button.clicked.connect(self.accept)
        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)

        buttons_layout = QtWidgets.QHBoxLayout()
        buttons_layout.addWidget(create_button, QtCore.Qt.AlignRight)
        buttons_layout.addWidget(cancel_button, QtCore.Qt.AlignRight)

        ## Outer layout.
        outer_vertical_layout = QtWidgets.QVBoxLayout()
        outer_vertical_layout.addLayout(options_layout)
        outer_vertical_layout.addLayout(buttons_layout)
        self.setLayout(outer_vertical_layout)

        self.setWindowTitle("New Project")
        self.setWindowModality(QtCore.Qt.WindowModal)

    def accept(self):
        if self.new_options.is_binary_file:
            idx = self.file_arch_value_combobox.currentIndex()
            self.new_options.processor_id = self.file_arch_value_combobox.itemData(idx)
            self.new_options.loader_load_address = util.str_to_int(self.processing_loadaddress_value_textedit.text())
            self.new_options.loader_entrypoint_offset = util.str_to_int(self.processing_entryaddress_value_textedit.text()) - self.new_options.loader_load_address
        return super(NewProjectDialog, self).accept()


class RowSelectionDialog(QtWidgets.QDialog):
    selection_key = None # type: int

    def __init__(self, parent, title_text, body_text, button_text, rows, row_keys):
        super(RowSelectionDialog, self).__init__(parent)

        self._row_keys = row_keys

        _set_default_font(self)

        self.setWindowTitle(title_text)
        self.setWindowModality(QtCore.Qt.WindowModal)

        label_widget = QtWidgets.QLabel(body_text, self)

        class Model(QtCore.QAbstractItemModel):
            def __init__(self, parent, rows):
                super(Model, self).__init__(parent)
                self._rows = rows
                self.beginInsertRows(QtCore.QModelIndex(), 0, len(rows)-1)
                self.endInsertRows()
            def data(self, index, role=QtCore.Qt.DisplayRole):
                if not index.isValid():
                    return None
                if role == QtCore.Qt.DisplayRole:
                    return self._rows[index.row()][index.column()]
                return None
            def columnCount(self, parent):
                if parent.isValid():
                    return 0
                if len(self._rows):
                    return len(self._rows[0])
                return 0
            def rowCount(self, parent):
                if parent.isValid():
                    return 0
                return len(self._rows)
            def parent(self, index):
                return QtCore.QModelIndex()
            def index(self, row, column, parent):
                if not self.hasIndex(row, column, parent):
                    return QtCore.QModelIndex()
                return self.createIndex(row, column)

        table = self.table_widget = CustomQTableView(self)
        self.table_model = Model(self, rows)
        table.setModel(self.table_model)
        table.setCornerButtonEnabled(False)
        #table.setGridStyle(QtCore.Qt.DashLine)
        table.setSortingEnabled(False)
        # Hide row numbers and column names.
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.setFont(self.parent().list_table.font())
        # No selection of individual cells, but rather line specific selection.
        table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerItem)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        # Adjust the row data display.
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        # Ensure the first row is selected.
        index = self.table_model.index(0, 0, QtCore.QModelIndex())
        table.scrollTo(index, QtWidgets.QAbstractItemView.PositionAtCenter)
        table.selectionModel().setCurrentIndex(index, QtCore.QItemSelectionModel.Select)

        button_widget = QtWidgets.QPushButton(button_text, self)
        button_widget.clicked.connect(self.accept)

        outer_vertical_layout = QtWidgets.QVBoxLayout()
        outer_vertical_layout.addWidget(label_widget)
        outer_vertical_layout.addWidget(self.table_widget)
        outer_vertical_layout.addWidget(button_widget)
        self.setLayout(outer_vertical_layout)

    def accept(self):
        # TODO: Should be selected row index from table, used to look up row_keys.
        row_idx = self.table_widget.currentIndex().row()
        self.selection_key = self._row_keys[row_idx]
        return super(RowSelectionDialog, self).accept()



## General script startup code.

def _initialise_logging(window):
    def _ui_thread_logging(t):
        timestamp, level_name, logger_name, message = t
        table = window.log_table
        model = window.log_model
        row_index = model.rowCount()
        model.insertRows(row_index, 1, QtCore.QModelIndex())
        model.setData(model.index(row_index, 0, QtCore.QModelIndex()), time.ctime(timestamp))
        model.setData(model.index(row_index, 1, QtCore.QModelIndex()), level_name)
        model.setData(model.index(row_index, 2, QtCore.QModelIndex()), logger_name)
        model.setData(model.index(row_index, 3, QtCore.QModelIndex()), message)
        #table.resizeColumnsToContents()
        #table.horizontalHeader().setStretchLastSection(True)
        #table.scrollTo(model.index(row_index, 0, QtCore.QModelIndex()), QtWidgets.QAbstractItemView.PositionAtBottom)
        table.scrollToBottom()

    window.log_signal.connect(_ui_thread_logging)

    class LogHandler(logging.Handler):
        def emit(self, record):
            msg = self.format(record)
            # These logging events may be happening in the worker thread, ensure they only get displayed in the UI thread.
            print(msg)
            return
            window.log_signal.emit((record.created, record.levelname, record.name, msg))

    handler = LogHandler()
    handler.setLevel(logging.DEBUG)

    root_logger = logging.root
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)
    root_logger.debug("Logging redirected to standard output as inter-thread logging is slow.")


def run():
    global app
    app = QtWidgets.QApplication(sys.argv)

    window = MainWindow()
    # The window needs to be created so we can connect to its signal.
    _initialise_logging(window)
    window.show()

    # Do our own argument handling.  The documentation for QApplication says that
    # QT will remove it's own arguments from argc, but this does not apply when
    # it is used in PySide.
    def _arg_file_load():
        """
        Initial very simple argument parsing.  There are three cases:
        - Load an executable file.
        - Load a project file, with optional input file path.
        - Load a binary file, specifying arch, load address and entrypoint address.
        See the syntax strings printed out on error, for more detail.
        """
        file_name = None
        input_file_name = None
        processor_id = None
        load_address = None
        entrypoint_address = None
        error_text = None
        if len(sys.argv) > 1:
            if len(sys.argv) > 1:
                file_name = sys.argv[1]
                if len(sys.argv) == 5:
                    processor_name = sys.argv[2]
                    processor_id = loaderlib.constants.lookup_processor_id_by_name(processor_name)
                    if processor_id not in disassemblylib.get_processor_ids():
                        error_text = "arch: not recognised"
                    else:
                        try:
                            load_address = util.str_to_int(sys.argv[3])
                            try:
                                entrypoint_address = util.str_to_int(sys.argv[4])
                            except ValueError:
                                error_text = "entrypoint address: unable to extract valid value"
                        except ValueError:
                            error_text = "load address: unable to extract valid value"
                elif len(sys.argv) == 3:
                    if not file_name.endswith("."+ PROJECT_SUFFIX):
                        error_text = "expected project file: "+ file_name
                    else:
                        input_file_name = sys.argv[2]
            if error_text is None and file_name is not None:
                if processor_id is not None:
                    error_text = window.toolapiob.load_binary_file(file_name, processor_id, load_address, entrypoint_address-load_address, input_file_name)
                else:
                    error_text = window.toolapiob.load_file(file_name, input_file_name)
        if type(error_text) is str:
            print("error:", error_text)
            print("%s: <executable file>" % sys.argv[0])
            print("%s: <project file> <input file>" % sys.argv[0])
            print("%s: <binary file> <arch name> <load address> <entry address>" % sys.argv[0])
            print()
            print("Addresses should use a leading '$' or '0x' to indicate they are hex, or base 16.")
            return False
        return True
    if _arg_file_load():
        # Run successfully.
        sys.exit(app.exec_())
        return
    # Close window and exit (if only it worked...).
    window.close()
    app.quit()

if __name__ == '__main__':
    run()
