#!/usr/bin/env python
#
# ElectrumSys - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import sys
import datetime
from datetime import date
from typing import TYPE_CHECKING, Tuple, Dict
import threading
from enum import IntEnum
from decimal import Decimal

from PyQt5.QtGui import QMouseEvent, QFont, QBrush, QColor
from PyQt5.QtCore import (Qt, QPersistentModelIndex, QModelIndex, QAbstractItemModel,
                          QSortFilterProxyModel, QVariant, QItemSelectionModel, QDate, QPoint)
from PyQt5.QtWidgets import (QMenu, QHeaderView, QLabel, QMessageBox,
                             QPushButton, QComboBox, QVBoxLayout, QCalendarWidget,
                             QGridLayout)

from electrumsys.address_synchronizer import TX_HEIGHT_LOCAL
from electrumsys.i18n import _
from electrumsys.util import (block_explorer_URL, profiler, TxMinedInfo,
                           OrderedDictWithIndex, timestamp_to_datetime,
                           Satoshis, format_time)
from electrumsys.logging import get_logger, Logger

from .util import (read_QIcon, MONOSPACE_FONT, Buttons, CancelButton, OkButton,
                   filename_field, MyTreeView, AcceptFileDragDrop, WindowModalDialog,
                   CloseButton, webopen)

if TYPE_CHECKING:
    from electrumsys.wallet import Abstract_Wallet
    from .main_window import ElectrumSysWindow


_logger = get_logger(__name__)


try:
    from electrumsys.plot import plot_history, NothingToPlotException
except:
    _logger.info("could not import electrumsys.plot. This feature needs matplotlib to be installed.")
    plot_history = None

# note: this list needs to be kept in sync with another in kivy
TX_ICONS = [
    "unconfirmed.png",
    "warning.png",
    "unconfirmed.png",
    "offline_tx.png",
    "clock1.png",
    "clock2.png",
    "clock3.png",
    "clock4.png",
    "clock5.png",
    "confirmed.png",
]

class AssetHistoryColumns(IntEnum):
    STATUS = 0
    SYMBOL = 1
    ASSET = 2
    ADDRESS = 3
    TRANSFER_TYPE = 4
    DESCRIPTION = 5
    AMOUNT = 6
    FIAT_VALUE = 7
    FIAT_ACQ_PRICE = 8
    FIAT_CAP_GAINS = 9
    TXID = 10
    PRECISION = 11

class AssetHistorySortModel(QSortFilterProxyModel):
    def lessThan(self, source_left: QModelIndex, source_right: QModelIndex):
        item1 = self.sourceModel().data(source_left, Qt.UserRole)
        item2 = self.sourceModel().data(source_right, Qt.UserRole)
        if item1 is None or item2 is None:
            raise Exception(f'UserRole not set for column {source_left.column()}')
        v1 = item1.value()
        v2 = item2.value()
        if v1 is None or isinstance(v1, Decimal) and v1.is_nan(): v1 = -float("inf")
        if v2 is None or isinstance(v2, Decimal) and v2.is_nan(): v2 = -float("inf")
        try:
            return v1 < v2
        except:
            return False

def get_item_key(tx_item):
    return tx_item.get('txid') or tx_item['payment_hash']

class AssetHistoryModel(QAbstractItemModel, Logger):

    def __init__(self, parent: 'ElectrumSysWindow'):
        QAbstractItemModel.__init__(self, parent)
        Logger.__init__(self)
        self.parent = parent
        self.view = None  # type: AssetHistoryList
        self.transactions = OrderedDictWithIndex()
        self.tx_status_cache = {}  # type: Dict[str, Tuple[int, str]]

    def set_view(self, history_list: 'AssetHistoryList'):
        # FIXME AssetHistoryModel and AssetHistoryList mutually depend on each other.
        # After constructing both, this method needs to be called.
        self.view = history_list  # type: AssetHistoryList
        self.set_visibility_of_columns()

    def columnCount(self, parent: QModelIndex):
        return len(AssetHistoryColumns)

    def rowCount(self, parent: QModelIndex):
        return len(self.transactions)

    def index(self, row: int, column: int, parent: QModelIndex):
        return self.createIndex(row, column)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole) -> QVariant:
        # note: this method is performance-critical.
        # it is called a lot, and so must run extremely fast.
        assert index.isValid()
        col = index.column()
        tx_item = self.transactions.value_from_pos(index.row())
        is_lightning = tx_item.get('lightning', False)
        timestamp = tx_item['timestamp']
        if is_lightning:
            status = 0
            if timestamp is None:
                status_str = 'unconfirmed'
            else:
                status_str = format_time(int(timestamp))
        else:
            tx_hash = tx_item['txid']
            conf = tx_item['confirmations']
            try:
                status, status_str = self.tx_status_cache[tx_hash]
            except KeyError:
                tx_mined_info = self.tx_mined_info_from_tx_item(tx_item)
                status, status_str = self.parent.wallet.get_tx_status(tx_hash, tx_mined_info)

        if role == Qt.UserRole:
            # for sorting
            d = {
                AssetHistoryColumns.STATUS:
                    # respect sort order of self.transactions (wallet.get_full_history)
                    -index.row(),
                AssetHistoryColumns.ASSET:
                    tx_item['asset'] if 'asset' in tx_item else None,
                AssetHistoryColumns.SYMBOL:
                    tx_item['symbol'] if 'symbol' in tx_item else None,  
                AssetHistoryColumns.TRANSFER_TYPE:
                    tx_item['transfer_type'] if 'transfer_type' in tx_item else None,  
                AssetHistoryColumns.PRECISION:
                    tx_item['precision'] if 'precision' in tx_item else None,                                       
                AssetHistoryColumns.DESCRIPTION:
                    tx_item['label'] if 'label' in tx_item else None,
                AssetHistoryColumns.AMOUNT:
                    (tx_item['bc_value'].value if 'bc_value' in tx_item else 0)\
                    + (tx_item['ln_value'].value if 'ln_value' in tx_item else 0),
                AssetHistoryColumns.ADDRESS:
                    tx_item['address'] if 'address' in tx_item else None,
                AssetHistoryColumns.FIAT_VALUE:
                    tx_item['fiat_value'].value if 'fiat_value' in tx_item else None,
                AssetHistoryColumns.FIAT_ACQ_PRICE:
                    tx_item['acquisition_price'].value if 'acquisition_price' in tx_item else None,
                AssetHistoryColumns.FIAT_CAP_GAINS:
                    tx_item['capital_gain'].value if 'capital_gain' in tx_item else None,
                AssetHistoryColumns.TXID: tx_hash if not is_lightning else None,
            }
            return QVariant(d[col])
        if role not in (Qt.DisplayRole, Qt.EditRole):
            if col == AssetHistoryColumns.STATUS and role == Qt.DecorationRole:
                icon = "lightning" if is_lightning else TX_ICONS[status]
                return QVariant(read_QIcon(icon))
            elif col == AssetHistoryColumns.STATUS and role == Qt.ToolTipRole:
                if is_lightning:
                    msg = 'lightning transaction'
                else:  # on-chain
                    if tx_item['height'] == TX_HEIGHT_LOCAL:
                        # note: should we also explain double-spends?
                        msg = _("This transaction is only available on your local machine.\n"
                                "The currently connected server does not know about it.\n"
                                "You can either broadcast it now, or simply remove it.")
                    else:
                        msg = str(conf) + _(" confirmation" + ("s" if conf != 1 else ""))
                return QVariant(msg)
            elif col > AssetHistoryColumns.DESCRIPTION and role == Qt.TextAlignmentRole:
                return QVariant(Qt.AlignRight | Qt.AlignVCenter)
            elif col > AssetHistoryColumns.DESCRIPTION and role == Qt.FontRole:
                monospace_font = QFont(MONOSPACE_FONT)
                return QVariant(monospace_font)
            #elif col == AssetHistoryColumns.DESCRIPTION and role == Qt.DecorationRole and not is_lightning\
            #        and self.parent.wallet.invoices.paid.get(tx_hash):
            #    return QVariant(read_QIcon("seal"))
            elif col in (AssetHistoryColumns.DESCRIPTION, AssetHistoryColumns.AMOUNT) \
                    and role == Qt.ForegroundRole and tx_item['value'].value < 0:
                red_brush = QBrush(QColor("#BC1E1E"))
                return QVariant(red_brush)
            elif col == AssetHistoryColumns.FIAT_VALUE and role == Qt.ForegroundRole \
                    and not tx_item.get('fiat_default') and tx_item.get('fiat_value') is not None:
                blue_brush = QBrush(QColor("#1E1EFF"))
                return QVariant(blue_brush)
            return QVariant()
        if col == AssetHistoryColumns.STATUS:
            return QVariant(status_str)
        elif col == AssetHistoryColumns.DESCRIPTION and 'label' in tx_item:
            return QVariant(tx_item['label'])
        elif col == AssetHistoryColumns.ASSET:
            return QVariant(tx_item['asset'])
        elif col == AssetHistoryColumns.SYMBOL:
            return QVariant(tx_item['symbol']) 
        elif col == AssetHistoryColumns.TRANSFER_TYPE:
            return QVariant(tx_item['transfer_type'])  
        elif col == AssetHistoryColumns.PRECISION:
            return QVariant(tx_item['precision'])                           
        elif col == AssetHistoryColumns.AMOUNT:
            bc_value = tx_item['bc_value'].value if 'bc_value' in tx_item else 0
            ln_value = tx_item['ln_value'].value if 'ln_value' in tx_item else 0
            value = bc_value + ln_value
            precision = tx_item['precision'] if 'precision' in tx_item else 8
            v_str = self.parent.format_amount(value, is_diff=True, whitespaces=True, decimal=precision)
            return QVariant(v_str)
        elif col == AssetHistoryColumns.ADDRESS:
            return QVariant(tx_item['address']) 
        elif col == AssetHistoryColumns.FIAT_VALUE and 'fiat_value' in tx_item:
            value_str = self.parent.fx.format_fiat(tx_item['fiat_value'].value)
            return QVariant(value_str)
        elif col == AssetHistoryColumns.FIAT_ACQ_PRICE and \
                tx_item['value'].value < 0 and 'acquisition_price' in tx_item:
            # fixme: should use is_mine
            acq = tx_item['acquisition_price'].value
            return QVariant(self.parent.fx.format_fiat(acq))
        elif col == AssetHistoryColumns.FIAT_CAP_GAINS and 'capital_gain' in tx_item:
            cg = tx_item['capital_gain'].value
            return QVariant(self.parent.fx.format_fiat(cg))
        elif col == AssetHistoryColumns.TXID:
            return QVariant(tx_hash) if not is_lightning else QVariant('')
        return QVariant()

    def parent(self, index: QModelIndex):
        return QModelIndex()

    def hasChildren(self, index: QModelIndex):
        return not index.isValid()

    def update_label(self, row):
        tx_item = self.transactions.value_from_pos(row)
        tx_item['label'] = self.parent.wallet.get_label(get_item_key(tx_item))
        topLeft = bottomRight = self.createIndex(row, AssetHistoryColumns.DESCRIPTION)
        self.dataChanged.emit(topLeft, bottomRight, [Qt.DisplayRole])
        self.parent.utxo_list.update()

    def get_domain(self):
        """Overridden in address_dialog.py"""
        return self.parent.wallet.get_addresses()

    def should_include_lightning_payments(self) -> bool:
        """Overridden in address_dialog.py"""
        return True

    @profiler
    def refresh(self, reason: str):
        self.logger.info(f"refreshing... reason: {reason}")
        assert self.view, 'view not set'
        if self.view.maybe_defer_update():
            return
        selected = self.view.selectionModel().currentIndex()
        selected_row = None
        if selected:
            selected_row = selected.row()
        fx = self.parent.fx
        if fx: fx.history_used_spot = False
        wallet = self.parent.wallet
        self.set_visibility_of_columns()
        transactions = wallet.get_full_assethistory(
            self.parent.fx,
            include_lightning=self.should_include_lightning_payments())
        if transactions == list(self.transactions.values()):
            return
        old_length = len(self.transactions)
        if old_length != 0:
            self.beginRemoveRows(QModelIndex(), 0, old_length)
            self.transactions.clear()
            self.endRemoveRows()
        self.beginInsertRows(QModelIndex(), 0, len(transactions)-1)
        self.transactions = transactions
        self.endInsertRows()
        if selected_row:
            self.view.selectionModel().select(self.createIndex(selected_row, 0), QItemSelectionModel.Rows | QItemSelectionModel.SelectCurrent)
        self.view.filter()
        # update time filter
        if not self.view.years and self.transactions:
            start_date = date.today()
            end_date = date.today()
            if len(self.transactions) > 0:
                start_date = self.transactions.value_from_pos(0).get('date') or start_date
                end_date = self.transactions.value_from_pos(len(self.transactions) - 1).get('date') or end_date
            self.view.years = [str(i) for i in range(start_date.year, end_date.year + 1)]
            self.view.period_combo.insertItems(1, self.view.years)
        # update tx_status_cache
        self.tx_status_cache.clear()
        for txid, tx_item in self.transactions.items():
            if not tx_item.get('lightning', False):
                tx_mined_info = self.tx_mined_info_from_tx_item(tx_item)
                self.tx_status_cache[txid] = self.parent.wallet.get_tx_status(txid, tx_mined_info)

    def set_visibility_of_columns(self):
        def set_visible(col: int, b: bool):
            self.view.showColumn(col) if b else self.view.hideColumn(col)
        # txid
        set_visible(AssetHistoryColumns.TXID, False)
        set_visible(AssetHistoryColumns.PRECISION, False)
        # fiat
        history = self.parent.fx.show_history()
        cap_gains = self.parent.fx.get_history_capital_gains_config()
        set_visible(AssetHistoryColumns.FIAT_VALUE, history)
        set_visible(AssetHistoryColumns.FIAT_ACQ_PRICE, history and cap_gains)
        set_visible(AssetHistoryColumns.FIAT_CAP_GAINS, history and cap_gains)

    def update_fiat(self, row, idx):
        tx_item = self.transactions.value_from_pos(row)
        key = tx_item['txid']
        fee = tx_item.get('fee')
        value = tx_item['value'].value
        fiat_fields = self.parent.wallet.get_tx_item_fiat(key, value, self.parent.fx, fee.value if fee else None)
        tx_item.update(fiat_fields)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.ForegroundRole])

    def update_tx_mined_status(self, tx_hash: str, tx_mined_info: TxMinedInfo):
        try:
            row = self.transactions.pos_from_key(tx_hash)
            tx_item = self.transactions[tx_hash]
        except KeyError:
            return
        self.tx_status_cache[tx_hash] = self.parent.wallet.get_tx_status(tx_hash, tx_mined_info)
        tx_item.update({
            'confirmations':  tx_mined_info.conf,
            'timestamp':      tx_mined_info.timestamp,
            'txpos_in_block': tx_mined_info.txpos,
            'date':           timestamp_to_datetime(tx_mined_info.timestamp),
        })
        topLeft = self.createIndex(row, 0)
        bottomRight = self.createIndex(row, len(AssetHistoryColumns) - 1)
        self.dataChanged.emit(topLeft, bottomRight)

    def on_fee_histogram(self):
        for tx_hash, tx_item in list(self.transactions.items()):
            if tx_item.get('lightning'):
                continue
            tx_mined_info = self.tx_mined_info_from_tx_item(tx_item)
            if tx_mined_info.conf > 0:
                # note: we could actually break here if we wanted to rely on the order of txns in self.transactions
                continue
            self.update_tx_mined_status(tx_hash, tx_mined_info)

    def headerData(self, section: int, orientation: Qt.Orientation, role: Qt.ItemDataRole):
        assert orientation == Qt.Horizontal
        if role != Qt.DisplayRole:
            return None
        fx = self.parent.fx
        fiat_title = 'n/a fiat value'
        fiat_acq_title = 'n/a fiat acquisition price'
        fiat_cg_title = 'n/a fiat capital gains'
        if fx and fx.show_history():
            fiat_title = '%s '%fx.ccy + _('Value')
            fiat_acq_title = '%s '%fx.ccy + _('Acquisition price')
            fiat_cg_title =  '%s '%fx.ccy + _('Capital Gains')
        return {
            AssetHistoryColumns.STATUS: _('Date'),
            AssetHistoryColumns.ASSET: _('Asset'),
            AssetHistoryColumns.ADDRESS: _('Asset Address'),
            AssetHistoryColumns.SYMBOL: _('Symbol'),
            AssetHistoryColumns.TRANSFER_TYPE: _('Transfer Type'),
            AssetHistoryColumns.DESCRIPTION: _('Description'),
            AssetHistoryColumns.AMOUNT: _('Amount'),
            AssetHistoryColumns.FIAT_VALUE: fiat_title,
            AssetHistoryColumns.FIAT_ACQ_PRICE: fiat_acq_title,
            AssetHistoryColumns.FIAT_CAP_GAINS: fiat_cg_title,
            AssetHistoryColumns.TXID: 'TXID',
            AssetHistoryColumns.PRECISION: 'Precision',
        }[section]

    def flags(self, idx):
        extra_flags = Qt.NoItemFlags # type: Qt.ItemFlag
        if idx.column() in self.view.editable_columns:
            extra_flags |= Qt.ItemIsEditable
        return super().flags(idx) | extra_flags

    @staticmethod
    def tx_mined_info_from_tx_item(tx_item):
        tx_mined_info = TxMinedInfo(height=tx_item['height'],
                                    conf=tx_item['confirmations'],
                                    timestamp=tx_item['timestamp'])
        return tx_mined_info

class AssetHistoryList(MyTreeView, AcceptFileDragDrop):
    filter_columns = [AssetHistoryColumns.STATUS,
                      AssetHistoryColumns.ASSET,
                      AssetHistoryColumns.SYMBOL,
                      AssetHistoryColumns.DESCRIPTION,
                      AssetHistoryColumns.AMOUNT,
                      AssetHistoryColumns.TXID]

    def tx_item_from_proxy_row(self, proxy_row):
        hm_idx = self.model().mapToSource(self.model().index(proxy_row, 0))
        return self.hm.transactions.value_from_pos(hm_idx.row())

    def should_hide(self, proxy_row):
        if self.start_timestamp and self.end_timestamp:
            tx_item = self.tx_item_from_proxy_row(proxy_row)
            date = tx_item['date']
            if date:
                in_interval = self.start_timestamp <= date <= self.end_timestamp
                if not in_interval:
                    return True
            return False

    def __init__(self, parent, model: AssetHistoryModel):
        super().__init__(parent, self.create_menu, stretch_column=AssetHistoryColumns.DESCRIPTION)
        self.config = parent.config
        self.hm = model
        self.proxy = AssetHistorySortModel(self)
        self.proxy.setSourceModel(model)
        self.setModel(self.proxy)
        AcceptFileDragDrop.__init__(self, ".txn")
        self.setSortingEnabled(True)
        self.start_timestamp = None
        self.end_timestamp = None
        self.years = []
        self.wallet = self.parent.wallet  # type: Abstract_Wallet
        self.sortByColumn(AssetHistoryColumns.STATUS, Qt.AscendingOrder)
        self.editable_columns |= {AssetHistoryColumns.FIAT_VALUE}
        self.create_toolbar_buttons()
        self.create_paging()
        self.header().setStretchLastSection(False)
        for col in AssetHistoryColumns:
            sm = QHeaderView.Stretch if col == self.stretch_column else QHeaderView.ResizeToContents
            self.header().setSectionResizeMode(col, sm)

    def update(self):
        self.hm.refresh('AssetHistoryList.update()')
        self.current_page_label.setText(str(self.parent.wallet.asset_synchronizer.current_page))
        self.total_pages_label.setText(str(self.parent.wallet.asset_synchronizer.total_pages))

    def format_date(self, d):
        return str(datetime.date(d.year, d.month, d.day)) if d else _('None')

    def on_combo(self, x):
        s = self.period_combo.itemText(x)
        x = s == _('Custom')
        self.start_button.setEnabled(x)
        self.end_button.setEnabled(x)
        if s == _('All'):
            self.start_timestamp = None
            self.end_timestamp = None
            self.start_button.setText("-")
            self.end_button.setText("-")
        else:
            try:
                year = int(s)
            except:
                return
            self.start_timestamp = start_date = datetime.datetime(year, 1, 1)
            self.end_timestamp = end_date = datetime.datetime(year+1, 1, 1)
            self.start_button.setText(_('From') + ' ' + self.format_date(start_date))
            self.end_button.setText(_('To') + ' ' + self.format_date(end_date))
        self.hide_rows()

    def on_page_size_combo(self, x):
        s = self.page_size_combo.itemText(x)
        self.wallet.network.run_from_another_thread(self.wallet.asset_synchronizer.change_results_per_page(int(s), self.update))
        self.hide_rows()

    def create_toolbar_buttons(self):
        self.period_combo = QComboBox()
        self.start_button = QPushButton('-')
        self.start_button.pressed.connect(self.select_start_date)
        self.start_button.setEnabled(False)
        self.end_button = QPushButton('-')
        self.end_button.pressed.connect(self.select_end_date)
        self.end_button.setEnabled(False)
        self.period_combo.addItems([_('All'), _('Custom')])
        self.period_combo.activated.connect(self.on_combo)

    def get_toolbar_buttons(self):
        return self.period_combo, self.start_button, self.end_button

    def create_paging(self):
        self.page_size_combo = QComboBox()
        self.page_size_combo.setFixedWidth(100)
        self.prev_button = QPushButton('<')
        self.prev_button.setFixedWidth(75)
        self.prev_button.pressed.connect(self.select_prev_page)
        self.prev_button.setEnabled(True)
        self.next_button = QPushButton('>')
        self.next_button.setFixedWidth(75)
        self.next_button.pressed.connect(self.select_next_page)
        self.next_button.setEnabled(True)
        self.page_size_combo.addItems(["25","50","100"])
        self.page_size_combo.activated.connect(self.on_page_size_combo)
        self.current_page_label = QLabel(str(self.wallet.asset_synchronizer.current_page))
        self.page_divider = QLabel("/")
        self.total_pages_label = QLabel(str(self.wallet.asset_synchronizer.total_pages))

    def get_paging_buttons(self):
        return [self.page_size_combo, self.prev_button, self.next_button]

    def get_page_info_labels(self):
        return [self.current_page_label, self.page_divider, self.total_pages_label]

    def on_hide_toolbar(self):
        self.start_timestamp = None
        self.end_timestamp = None
        self.hide_rows()

    def save_toolbar_state(self, state, config):
        config.set_key('show_toolbar_assethistory', state)

    def select_start_date(self):
        self.start_timestamp = self.select_date(self.start_button)
        self.hide_rows()

    def select_end_date(self):
        self.end_timestamp = self.select_date(self.end_button)
        self.hide_rows()

    def select_next_page(self):
        self.wallet.network.run_from_another_thread(self.wallet.asset_synchronizer.increase_page(self.update))
        self.hide_rows()

    def select_prev_page(self):
        self.wallet.network.run_from_another_thread(self.wallet.asset_synchronizer.decrease_page(self.update))
        self.hide_rows()

    def select_date(self, button):
        d = WindowModalDialog(self, _("Select date"))
        d.setMinimumSize(600, 150)
        d.date = None
        vbox = QVBoxLayout()
        def on_date(date):
            d.date = date
        cal = QCalendarWidget()
        cal.setGridVisible(True)
        cal.clicked[QDate].connect(on_date)
        vbox.addWidget(cal)
        vbox.addLayout(Buttons(OkButton(d), CancelButton(d)))
        d.setLayout(vbox)
        if d.exec_():
            if d.date is None:
                return None
            date = d.date.toPyDate()
            button.setText(self.format_date(date))
            return datetime.datetime(date.year, date.month, date.day)


    def plot_history_dialog(self):
        if plot_history is None:
            self.parent.show_message(
                _("Can't plot history.") + '\n' +
                _("Perhaps some dependencies are missing...") + " (matplotlib?)")
            return
        try:
            plt = plot_history(list(self.hm.transactions.values()))
            plt.show()
        except NothingToPlotException as e:
            self.parent.show_message(str(e))

    def on_edited(self, index, user_role, text):
        index = self.model().mapToSource(index)
        row, column = index.row(), index.column()
        tx_item = self.hm.transactions.value_from_pos(row)
        key = get_item_key(tx_item)
        if column == AssetHistoryColumns.DESCRIPTION:
            if self.wallet.set_label(key, text): #changed
                self.hm.update_label(row)
                self.parent.update_completions()
        elif column == AssetHistoryColumns.FIAT_VALUE:
            self.wallet.set_fiat_value(key, self.parent.fx.ccy, text, self.parent.fx, tx_item['value'].value)
            value = tx_item['value'].value
            if value is not None:
                self.hm.update_fiat(row, index)
        else:
            assert False

    def add_copy_menu(self, menu, idx):
        cc = menu.addMenu(_("Copy"))
        for column in AssetHistoryColumns:
            if self.isColumnHidden(column):
                continue
            column_title = self.hm.headerData(column, Qt.Horizontal, Qt.DisplayRole)
            idx2 = idx.sibling(idx.row(), column)
            column_data = (self.hm.data(idx2, Qt.DisplayRole).value() or '').strip()
            cc.addAction(
                column_title,
                lambda text=column_data, title=column_title:
                self.place_text_on_clipboard(text, title=title))
        return cc

    def create_menu(self, position: QPoint):
        org_idx: QModelIndex = self.indexAt(position)
        idx = self.proxy.mapToSource(org_idx)
        if not idx.isValid():
            # can happen e.g. before list is populated for the first time
            return
        tx_item = self.hm.transactions.value_from_pos(idx.row())
        if tx_item.get('lightning') and tx_item['type'] == 'payment':
            menu = QMenu()
            menu.addAction(_("View Payment"), lambda: self.parent.show_lightning_transaction(tx_item))
            cc = self.add_copy_menu(menu, idx)
            cc.addAction(_("Payment Hash"), lambda: self.place_text_on_clipboard(tx_item['payment_hash'], title="Payment Hash"))
            cc.addAction(_("Preimage"), lambda: self.place_text_on_clipboard(tx_item['preimage'], title="Preimage"))
            menu.exec_(self.viewport().mapToGlobal(position))
            return
        tx_hash = tx_item['txid']
        asset_guid = tx_item['asset']
        asset_URL = block_explorer_URL(self.config, 'asset', asset_guid)
        tx_URL = block_explorer_URL(self.config, 'tx', tx_hash)
        menu = QMenu()
       
        cc = self.add_copy_menu(menu, idx)
        cc.addAction(_("Transaction ID"), lambda: self.place_text_on_clipboard(tx_hash, title="TXID"))
        for c in self.editable_columns:
            if self.isColumnHidden(c): continue
            label = self.hm.headerData(c, Qt.Horizontal, Qt.DisplayRole)
            # TODO use siblingAtColumn when min Qt version is >=5.11
            persistent = QPersistentModelIndex(org_idx.sibling(org_idx.row(), c))
            menu.addAction(_("Edit {}").format(label), lambda p=persistent: self.edit(QModelIndex(p)))
        channel_id = tx_item.get('channel_id')
        if channel_id:
            menu.addAction(_("View Channel"), lambda: self.parent.show_channel(bytes.fromhex(channel_id)))
        if tx_URL:
            menu.addAction(_("View on block explorer"), lambda: webopen(tx_URL))
        if asset_URL:
            menu.addAction(_("View Asset on block explorer"), lambda: webopen(asset_URL))            
        menu.exec_(self.viewport().mapToGlobal(position))


    def export_history_dialog(self):
        d = WindowModalDialog(self, _('Export Asset History'))
        d.setMinimumSize(400, 200)
        vbox = QVBoxLayout(d)
        defaultname = os.path.expanduser('~/electrumsys-assethistory.csv')
        select_msg = _('Select file to export your wallet transactions to')
        hbox, filename_e, csv_button = filename_field(self, self.config, defaultname, select_msg)
        vbox.addLayout(hbox)
        vbox.addStretch(1)
        hbox = Buttons(CancelButton(d), OkButton(d, _('Export')))
        vbox.addLayout(hbox)
        #run_hook('export_history_dialog', self, hbox)
        self.update()
        if not d.exec_():
            return
        filename = filename_e.text()
        if not filename:
            return
        try:
            self.do_export_history(filename, csv_button.isChecked())
        except (IOError, os.error) as reason:
            export_error_label = _("ElectrumSys was unable to produce a transaction export.")
            self.parent.show_critical(export_error_label + "\n" + str(reason), title=_("Unable to export history"))
            return
        self.parent.show_message(_("Your wallet history has been successfully exported."))

    def do_export_history(self, file_name, is_csv):
        hist = self.wallet.get_detailed_assethistory(fx=self.parent.fx)
        txns = hist['transactions']
        for item in txns:
            bc_value = item['bc_value'].value if 'bc_value' in item else 0
            precision = item['precision'] if 'precision' in item else 8
            item['bc_value'] = self.parent.format_amount(bc_value, is_diff=True, decimal=precision)
        lines = []
        if is_csv:
            for item in txns:
                lines.append([item['txid'],
                              item.get('symbol', ''),
                              item.get('asset', ''),
                              item.get('address', ''),
                              item.get('label', ''),
                              item['confirmations'],
                              item['bc_value'],
                              item.get('fiat_value', ''),
                              item.get('fee', ''),
                              item.get('fiat_fee', ''),
                              item['date']])
        with open(file_name, "w+", encoding='utf-8') as f:
            if is_csv:
                import csv
                transaction = csv.writer(f, lineterminator='\n')
                transaction.writerow(["transaction_hash",
                                      "symbol",
                                      "asset",
                                      "address",
                                      "label",
                                      "confirmations",
                                      "value",
                                      "fiat_value",
                                      "fee",
                                      "fiat_fee",
                                      "timestamp"])
                for line in lines:
                    transaction.writerow(line)
            else:
                from electrumsys.util import json_encode
                f.write(json_encode(txns))

    def text_txid_from_coordinate(self, row, col):
        idx = self.model().mapToSource(self.model().index(row, col))
        tx_item = self.hm.transactions.value_from_pos(idx.row())
        return self.hm.data(idx, Qt.DisplayRole).value(), get_item_key(tx_item)
