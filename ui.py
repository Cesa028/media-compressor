"""Media Compressor - finestra principale: uno o più file, destinazione, opzioni, Comprimi/Annulla."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout, QHBoxLayout,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

import icons
import theme
from common import APP_NAME, IMAGE_EXT, VIDEO_EXT, fmt_size, reveal, settings
from dialogs import AboutDialog, ErrorDialog
from engine import FORMATS, MB, PRESETS, CompressWorker, Item, Job, ScanWorker, clean_name, gpu_available
from theme import SP_1, SP_2, SP_3, SP_4, SP_5, THEME_LABELS, THEMES, titlebar
from widgets import a11y_pass, label, repolish, sep


def write_ui_assets(folder: Path) -> str:
    """Frecce di menu a tendina e campo numerico (usate dal foglio di stile), una coppia per tema."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("dark", "light"):
        color = theme.DARK["muted"] if name == "dark" else theme.LIGHT["muted"]
        (folder / f"chev-{name}.svg").write_text(icons.svg("chevron-down", color, 2.4), encoding="utf-8")
        (folder / f"up-{name}.svg").write_text(icons.svg("chevron-up", color, 2.4), encoding="utf-8")
    return folder.as_posix()


def apply_theme(app: QApplication, pref: str, asset_dir: Path) -> str:
    name = theme.apply(pref)
    app.setPalette(theme.app_palette())
    app.setStyleSheet(theme.stylesheet(write_ui_assets(asset_dir)))
    for w in app.topLevelWidgets():
        if w.isVisible():
            titlebar(w)
    for w in app.allWidgets():
        w.update()
    return name


def read_theme_pref() -> str:
    v = settings().value("theme", "system")
    return v if v in THEMES else "system"


def _patterns(exts):
    return " ".join(f"*{e}" for e in sorted(exts))


FILE_FILTER = (f"Foto e video ({_patterns(IMAGE_EXT | VIDEO_EXT)});;Foto ({_patterns(IMAGE_EXT)});;"
               f"Video ({_patterns(VIDEO_EXT)})")
DROP_EMPTY = "Trascina qui foto o video, anche più di uno\noppure fai clic per sceglierli"
KIND_LABELS = {"photo": "foto", "anim": "animazioni", "video": "video"}
DEFAULT_SUFFIX = "_compresso"


class MainWindow(QMainWindow):
    def __init__(self, tmp: Path):
        super().__init__()
        self.tmp = tmp
        self.settings = settings()
        self.theme_pref = read_theme_pref()
        self.items: list[Item] = []
        self.results: dict[int, Path] = {}
        self.errors: dict[int, tuple[str, str]] = {}
        self.work: CompressWorker | None = None
        self.scan: ScanWorker | None = None
        self.was_cancelled = False
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.app_icon())
        self.setMinimumWidth(600)          # l'altezza minima la decide il contenuto
        self.setAcceptDrops(True)
        self._build()
        self._shortcuts()
        geo = self.settings.value("geometry")
        if not (geo and self.restoreGeometry(geo)):
            self.resize(660, 700)
        self._show_items()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()            # niente riquadro di focus acceso all'avvio

    # ------------------------------------------------------------------ UI
    def _build(self):
        central = QWidget(objectName="leftPane")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(SP_5, SP_4, SP_5, SP_4)
        root.setSpacing(SP_2)

        head = QHBoxLayout()
        title = QVBoxLayout()
        title.setSpacing(0)
        title.addWidget(label(APP_NAME, "appTitle"))
        title.addWidget(label("Rendi più leggeri foto e video", "appSub"))
        head.addLayout(title, 1)
        self.btn_theme = QPushButton(icons.icon("monitor", "text2"), "", objectName="ghost")
        self.btn_theme.setToolTip("Tema dell'interfaccia")
        self.btn_theme.clicked.connect(self._theme_menu)
        self.btn_about = QPushButton(icons.icon("info", "text2"), "", objectName="ghost")
        self.btn_about.setToolTip("Informazioni su Media Compressor  (F1)")
        self.btn_about.clicked.connect(lambda: AboutDialog(self).exec())
        for b in (self.btn_theme, self.btn_about):
            b.setIconSize(QSize(18, 18))
            head.addWidget(b, 0, Qt.AlignTop)
        root.addLayout(head)

        # un file: riquadro di rilascio; più file: elenco con lo stato di ciascuno
        self.drop = QPushButton(DROP_EMPTY, objectName="drop")
        self.drop.setIcon(icons.icon("image-plus", "drop_icon", 1.6))
        self.drop.setIconSize(QSize(32, 32))
        self.drop.setMinimumHeight(84)
        self.drop.setAccessibleName("Scegli le foto o i video da comprimere")
        self.drop.clicked.connect(self.pick_input)
        root.addWidget(self.drop)
        self.list = QListWidget(objectName="queue")
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setAccessibleName("File da comprimere")
        self.list.setMinimumHeight(110)
        self.list.setTextElideMode(Qt.ElideMiddle)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.itemSelectionChanged.connect(self._update_state)
        root.addWidget(self.list, 1)
        list_bar = QHBoxLayout()
        self.btn_add = QPushButton(icons.icon("plus", "accent_text"), "  Aggiungi file", objectName="accentSoft")
        self.btn_add.setToolTip("Aggiunge altri file all'elenco  (Ctrl+Shift+O)")
        self.btn_add.clicked.connect(self.add_files)
        self.btn_remove = QPushButton(icons.icon("x", "muted"), "  Togli selezionati", objectName="ghost")
        self.btn_remove.setToolTip("Toglie dall'elenco i file selezionati  (Canc)")
        self.btn_remove.clicked.connect(self.remove_selected)
        list_bar.addWidget(self.btn_add)
        list_bar.addWidget(self.btn_remove)
        list_bar.addStretch()
        root.addLayout(list_bar)

        self.in_edit = QLineEdit(placeholderText="Nessun file scelto")
        self.in_edit.editingFinished.connect(self._input_typed)
        self.in_box = QWidget()
        in_lay = QVBoxLayout(self.in_box)
        in_lay.setContentsMargins(0, 0, 0, 0)
        in_lay.setSpacing(SP_2)
        self.btn_in = self._select_button(in_lay, "Percorso &file", self.in_edit, self.pick_input, "Ctrl+O")
        root.addWidget(self.in_box)
        self.out_edit = QLineEdit(placeholderText="La stessa cartella del file originale")
        self.out_edit.setText(str(self.settings.value("out_dir", "")))
        self.out_edit.textChanged.connect(self._update_state)
        self.btn_out = self._select_button(root, "Percorso f&inale", self.out_edit, self.pick_output, "Ctrl+D")

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(SP_3)
        self.grid.setVerticalSpacing(SP_1)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0, 100_000)
        self.size_spin.setDecimals(2)
        self.size_spin.setSuffix(" MB")
        self.size_spin.setSpecialValueText("Nessun limite")
        self.size_spin.setValue(float(self.settings.value("target_mb", 0)))
        self.size_spin.setToolTip("Peso massimo di ogni file finale. Con 0 conta solo la qualità scelta.")
        self.quality_cb = QComboBox()
        self.quality_cb.addItems(list(PRESETS))
        q = self.settings.value("quality", "Media")
        self.quality_cb.setCurrentText(q if q in PRESETS else "Media")
        self.quality_cb.setToolTip("Con un peso desiderato: più è alta, prima si riduce la risoluzione\n"
                                   "invece di sgranare l'immagine.")
        self.size_lab = label("&Peso desiderato", "fieldLabel")
        for col, (lab, w) in enumerate(((self.size_lab, self.size_spin),
                                        (label("&Qualità", "fieldLabel"), self.quality_cb))):
            lab.setBuddy(w)
            w.setAccessibleName(lab.text().replace("&", ""))
            self.grid.addWidget(lab, 0, col)
            self.grid.addWidget(w, 1, col)
        self.formats: dict[str, tuple] = {}
        for kind in FORMATS:
            lab, combo = label("", "fieldLabel"), QComboBox()
            combo.addItems(list(FORMATS[kind]))
            saved = self.settings.value(f"format_{kind}", "")
            if saved in FORMATS[kind]:
                combo.setCurrentText(saved)
            lab.setBuddy(combo)
            combo.setToolTip("Formato del file finale" +
                             (". MKV conserva anche i sottotitoli a immagini." if kind == "video" else ""))
            combo.activated.connect(lambda _, k=kind, c=combo: self.settings.setValue(f"format_{k}", c.currentText()))
            combo.currentIndexChanged.connect(self._update_state)
            self.formats[kind] = (lab, combo)
        for col in range(3):
            self.grid.setColumnStretch(col, 1)
        root.addLayout(self.grid)

        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._update_state)
        self.name_lab = label("&Nome finale", "fieldLabel")
        self.name_lab.setBuddy(self.name_edit)
        root.addWidget(self.name_lab)
        root.addWidget(self.name_edit)

        self.gpu_check = QCheckBox("Usa la scheda video NVIDIA per i video (molto più veloce)")
        self.gpu_check.setToolTip("Funziona solo con schede NVIDIA. Codifica molte volte più veloce, con una qualità appena\n"
                                  "inferiore a parità di peso. Togli la spunta per la massima qualità.")
        self.gpu_check.setChecked(self.settings.value("gpu", "true") == "true")
        root.addWidget(self.gpu_check)
        self.filler = QWidget()
        root.addWidget(self.filler, 1)
        root.addWidget(sep())

        self.progress = QProgressBar(objectName="overall")
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setAccessibleName("Avanzamento della compressione")
        root.addWidget(self.progress)
        self.status = label("", "statusText", True)
        self.status.setMinimumHeight(34)
        self.status.setTextFormat(Qt.PlainText)
        root.addWidget(self.status)

        bar = QHBoxLayout()
        bar.setSpacing(SP_2)
        self.btn_reveal = QPushButton(icons.icon("folder"), "  Mostra nella cartella")
        self.btn_reveal.clicked.connect(lambda: reveal(list(self.results.values())[-1]))
        self.btn_copy = QPushButton(icons.icon("copy"), "  Copia percorso", objectName="ghost")
        self.btn_copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText("\n".join(map(str, self.results.values()))))
        self.btn_errors = QPushButton(icons.icon("alert", "warn"), "  Dettagli errori", objectName="ghost")
        self.btn_errors.clicked.connect(self._show_errors)
        self.btn_go = QPushButton("Comprimi", objectName="primary")
        self.btn_go.clicked.connect(lambda: self.cancel() if self.working() else self.go())
        for b in (self.btn_reveal, self.btn_copy, self.btn_errors):
            bar.addWidget(b)
        bar.addStretch()
        bar.addWidget(self.btn_go)
        root.addLayout(bar)
        for b in self.findChildren(QPushButton):
            b.setCursor(Qt.PointingHandCursor)
        a11y_pass(self)

    def _select_button(self, root, text, edit, fn, key) -> QPushButton:
        lab = label(text, "fieldLabel")
        lab.setBuddy(edit)
        edit.setAccessibleName(text.replace("&", ""))
        btn = QPushButton("Seleziona")
        btn.setToolTip(f"{text.replace('&', '')}: scegli  ({key})")
        btn.setAccessibleName(f"Seleziona {text.replace('&', '').lower()}")
        btn.clicked.connect(fn)
        row = QHBoxLayout()
        row.setSpacing(SP_2)
        row.addWidget(edit, 1)
        row.addWidget(btn)
        root.addWidget(lab)
        root.addLayout(row)
        return btn

    def _shortcuts(self):
        for k, fn in (("Ctrl+O", self.pick_input), ("Ctrl+Shift+O", self.add_files), ("Ctrl+D", self.pick_output),
                      ("Ctrl+Return", self.go), ("Ctrl+Enter", self.go), ("Esc", self.cancel),
                      ("F1", lambda: AboutDialog(self).exec())):
            QShortcut(QKeySequence(k), self, activated=fn)
        QShortcut(QKeySequence("Delete"), self.list, activated=self.remove_selected, context=Qt.WidgetShortcut)

    def showEvent(self, e):
        titlebar(self)
        self._theme_icon()
        super().showEvent(e)

    def closeEvent(self, e):
        if self.scan and self.scan.isRunning():
            self.scan.requestInterruption()
            self.scan.wait(10000)
        if self.working():
            self.work.stop()
            self.work.wait(10000)
        self.settings.setValue("geometry", self.saveGeometry())
        self._save_options()
        super().closeEvent(e)

    def _save_options(self):
        self.settings.setValue("out_dir", self.out_edit.text().strip())
        self.settings.setValue("target_mb", self.size_spin.value())
        self.settings.setValue("quality", self.quality_cb.currentText())
        self.settings.setValue("gpu", "true" if self.gpu_check.isChecked() else "false")

    # ------------------------------------------------------------------ tema
    def _theme_icon(self):
        self.btn_theme.setIcon(icons.icon({"dark": "moon", "light": "sun"}.get(self.theme_pref, "monitor"), "text2"))

    def _theme_menu(self):
        m = QMenu(self)
        g = QActionGroup(m)
        for key in THEMES:
            a = QAction(THEME_LABELS[key], m, checkable=True, checked=key == self.theme_pref)
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            g.addAction(a)
            m.addAction(a)
        m.exec(self.btn_theme.mapToGlobal(self.btn_theme.rect().bottomLeft()))

    def set_theme(self, pref: str):
        self.theme_pref = pref
        self.settings.setValue("theme", pref)
        apply_theme(QApplication.instance(), pref, self.tmp / "ui")
        self._theme_icon()

    # ------------------------------------------------------------------ scelta file e cartella
    def _last_dir(self) -> str:
        return str(self.settings.value("last_dir", str(Path.home() / "Pictures")))

    def _ask_files(self) -> list[str]:
        return QFileDialog.getOpenFileNames(self, "Scegli foto o video", self._last_dir(), FILE_FILTER)[0]

    def pick_input(self):
        if not self.busy() and (files := self._ask_files()):
            self.load(files)

    def add_files(self):
        if not self.busy() and (files := self._ask_files()):
            self.load(files, add=True)

    def pick_output(self):
        if self.busy():
            return
        d = QFileDialog.getExistingDirectory(self, "Scegli la cartella finale",
                                             self.out_edit.text() or self._last_dir())
        if d:
            self.out_edit.setText(str(Path(d)))

    def _input_typed(self):
        if len(self.items) > 1:
            return
        text = self.in_edit.text().strip().strip('"')
        if text != (str(self.items[0].path) if self.items else ""):
            if text:
                self.load([text])
            elif not self.busy():
                self._set_items([])

    def load(self, paths, add=False):
        """Legge i file in un thread; `add` li accoda a quelli già presenti."""
        if self.busy():
            return
        self.scan = ScanWorker([Path(p) for p in paths])
        self.scan.scanned.connect(lambda items, errors: self._scanned(items, errors, add))
        self.scan.finished.connect(self._update_state)
        self.scan.start()
        self.status.setText("Leggo i file…")
        self._update_state()

    def _scanned(self, items: list, errors: list, add: bool):
        if items:
            if add:
                known = {i.path.resolve() for i in self.items}
                items = self.items + [i for i in items if i.path.resolve() not in known]
            self.settings.setValue("last_dir", str(items[-1].path.parent))
            self._set_items(items)
        else:
            self.status.setText("")
        if errors:
            msg = errors[0][1] if len(errors) == 1 else f"{len(errors)} file non si possono usare e sono stati saltati."
            ErrorDialog(self, "Non posso usare questo file" if len(errors) == 1 else "Alcuni file sono stati saltati",
                        msg, "\n".join(f"{p}: {m}" for p, m in errors)).exec()

    def remove_selected(self):
        if self.busy():
            return
        gone = {self.list.row(it) for it in self.list.selectedItems()}
        if gone:
            self._set_items([it for i, it in enumerate(self.items) if i not in gone])

    def _set_items(self, items: list):
        was_multi, had = len(self.items) > 1, bool(self.items)
        old_path = self.items[0].path if self.items else None
        self.items = items
        self.results, self.errors = {}, {}
        multi = len(items) > 1
        # il nome scritto dall'utente resta finché ha ancora senso (stesso file, o stessa coda)
        if not items:
            self.name_edit.clear()
        elif multi and not (had and was_multi):
            self.name_edit.setText(DEFAULT_SUFFIX)
        elif not multi and (was_multi or not had or old_path != items[0].path):
            self.name_edit.setText(items[0].path.stem + DEFAULT_SUFFIX)
        self.progress.setValue(0)
        self.status.setText("")
        self._show_items()

    def _show_items(self):
        n, multi = len(self.items), len(self.items) > 1
        self.drop.setVisible(not multi)
        self.drop.setText(f"{self.items[0].path.name}\n{self.items[0].details}  ·  {fmt_size(self.items[0].size / MB)}"
                          if n == 1 else DROP_EMPTY)
        self.list.setVisible(multi)
        self.btn_add.setVisible(bool(n))
        self.btn_remove.setVisible(multi)
        self.filler.setVisible(not multi)
        self.list.clear()
        for item in self.items:
            row = QListWidgetItem(icons.icon("image" if item.kind != "video" else "clapperboard"),
                                  f"{item.path.name}  ·  {fmt_size(item.size / MB)}")
            row.setToolTip(f"{item.path}\n{item.details}")
            row.setSizeHint(QSize(0, 34))
            self.list.addItem(row)
        self.in_box.setVisible(not multi)
        self.in_edit.setText(str(self.items[0].path) if n == 1 else "")
        self.size_lab.setText("&Peso massimo per file" if multi else "&Peso desiderato")
        self.name_lab.setText("Aggiunta al &nome di ogni file" if multi else "&Nome finale")
        self.name_edit.setAccessibleName(self.name_lab.text().replace("&", ""))
        self.name_edit.setPlaceholderText("es. _compresso" if multi else "Nome del file finale")

        kinds = [k for k in FORMATS if any(i.kind == k for i in self.items)] or ["photo"]
        for lab, combo in self.formats.values():
            for w in (lab, combo):
                self.grid.removeWidget(w)
                w.hide()
        # il primo formato sta accanto a peso e qualità; gli altri (code miste) sulla riga sotto
        for i, kind in enumerate(kinds):
            lab, combo = self.formats[kind]
            row, col = (0, 2) if i == 0 else (2, i - 1)
            lab.setText("Forma&to" if len(kinds) == 1 else f"Formato {KIND_LABELS[kind]}")
            combo.setAccessibleName(lab.text().replace("&", ""))
            lab.setContentsMargins(0, SP_2 if row else 0, 0, 0)
            self.grid.addWidget(lab, row, col)
            self.grid.addWidget(combo, row + 1, col)
            lab.show()
            combo.show()
        self.gpu_check.setVisible("video" in kinds)
        if "video" in kinds and not gpu_available():
            self.gpu_check.setText("Accelerazione solo con NVIDIA: non rilevata, uso il processore")
        self._update_state()

    # ------------------------------------------------------------------ compressione
    def working(self) -> bool:
        return bool(self.work and self.work.isRunning())

    def busy(self) -> bool:
        return self.working() or bool(self.scan and self.scan.isRunning())

    def _out_dir(self, item: Item) -> Path:
        text = self.out_edit.text().strip().strip('"')
        return Path(text) if text else item.path.parent

    def _name(self, item: Item) -> str:
        fmt = self.formats[item.kind][1].currentText()
        typed = self.name_edit.text()
        if len(self.items) > 1:
            typed = item.path.stem + re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", typed)
        return clean_name(typed, FORMATS[item.kind][fmt])

    def _problem(self) -> str:
        if not self.items:
            return "Scegli prima una foto o un video"
        for item in self.items:
            out = self._out_dir(item)
            if not out.is_absolute() or not out.is_dir():
                return "La cartella finale non esiste"
        if len(self.items) == 1 and not self._name(self.items[0]):
            return "Scrivi un nome per il file finale"
        return ""

    def go(self):
        if self.busy():
            return
        self.size_spin.interpretText()
        missing = [str(i.path) for i in self.items if not i.path.is_file()]
        if missing:
            ErrorDialog(self, "File non trovato", "Alcuni file sono stati spostati o eliminati. Sceglili di nuovo.",
                        "\n".join(missing)).exec()
            return
        if self._problem():
            self.status.setText(self._problem() + ".")
            return
        self._save_options()
        self.results, self.errors, self.was_cancelled = {}, {}, False
        gpu = self.gpu_check.isChecked() and self.gpu_check.isEnabled() and self.gpu_check.isVisibleTo(self)
        jobs = [Job(i.path, self._out_dir(i), self._name(i), self.formats[i.kind][1].currentText(),
                    self.quality_cb.currentText(), self.size_spin.value(), i.kind, gpu) for i in self.items]
        for row, item in enumerate(self.items):
            self.list.item(row).setText(f"{item.path.name}  ·  in attesa")
        self.work = CompressWorker(jobs)
        self.work.progress.connect(self._progress)
        self.work.item_done.connect(self._item_done)
        self.work.item_failed.connect(self._item_failed)
        self.work.cancelled.connect(self._cancelled)
        self.work.finished.connect(self._finished)
        self.progress.setValue(0)
        self.work.start()
        self._update_state()

    def cancel(self):
        if self.working():
            self.status.setText("Annullo…")
            self.work.stop()

    def _progress(self, part: float, text: str):
        if not self.work.stop_requested:
            self.progress.setValue(int(part * 1000))
            self.status.setText(text)

    def _change(self, before: int, after: int) -> str:
        if after < before:
            return f"{round((1 - after / before) * 100)}% in meno"
        return "più pesante dell'originale: prova una qualità più bassa o un peso desiderato"

    def _item_done(self, index: int, final: Path, note: str):
        self.results[index] = final
        item, after = self.items[index], final.stat().st_size
        sizes = f"{fmt_size(item.size / MB)} → {fmt_size(after / MB)}"
        row = self.list.item(index)
        row.setIcon(icons.icon("circle-check", "ok"))
        row.setText(f"{item.path.name}  ·  {sizes}")
        row.setToolTip(f"{final}\n{self._change(item.size, after)}  ·  {note}")
        if len(self.items) == 1:
            self.status.setText(f"Fatto: {final.name}\n{sizes} ({self._change(item.size, after)})  ·  {note}")

    def _item_failed(self, index: int, message: str, detail: str):
        self.errors[index] = (message, detail)
        row = self.list.item(index)
        row.setIcon(icons.icon("alert", "err"))
        row.setText(f"{self.items[index].path.name}  ·  errore: {message}")
        row.setToolTip(message)

    def _cancelled(self):
        self.was_cancelled = True

    def _finished(self):
        ok, bad, multi = len(self.results), len(self.errors), len(self.items) > 1
        self.progress.setValue(0 if self.was_cancelled or not ok else 1000)
        if self.was_cancelled:
            done = f" I {ok} file già pronti restano." if ok else " Nessun file creato."
            self.status.setText("Compressione annullata." + done)
            for row, item in enumerate(self.items):
                if row not in self.results and row not in self.errors:
                    self.list.item(row).setText(f"{item.path.name}  ·  non compresso")
        elif multi:
            before = sum(self.items[i].size for i in self.results)
            after = sum(p.stat().st_size for p in self.results.values() if p.exists())
            text = f"Fatto: {ok} file compressi" + (f", {bad} con errore" if bad else "")
            if ok:
                text += f"\n{fmt_size(before / MB)} → {fmt_size(after / MB)} ({self._change(before, after)})"
            self.status.setText(text)
        elif bad:
            self.status.setText(self.errors[0][0])
        self._update_state()
        if bad and not multi:
            self._show_errors()

    def _show_errors(self):
        message = (next(iter(self.errors.values()))[0] if len(self.errors) == 1
                   else f"{len(self.errors)} file non sono stati compressi.")
        detail = "\n\n".join(f"{self.items[i].path.name}: {m}\n{d}" for i, (m, d) in self.errors.items())
        ErrorDialog(self, "Compressione non riuscita", message, detail).exec()

    # ------------------------------------------------------------------ stato dei controlli
    def _update_state(self, *_):
        if not hasattr(self, "btn_go"):
            return
        busy, working = self.busy(), self.working()
        for w in (self.drop, self.in_edit, self.btn_in, self.btn_add, self.out_edit, self.btn_out, self.size_spin,
                  self.quality_cb, self.name_edit):
            w.setEnabled(not busy)
        # prima controlla che ci sia un video, così la scheda non viene provata per niente
        self.gpu_check.setEnabled(not busy and self.gpu_check.isVisibleTo(self) and gpu_available())
        self.btn_remove.setEnabled(not busy and bool(self.list.selectedItems()))
        for _lab, combo in self.formats.values():
            combo.setEnabled(bool(self.items) and not busy)
        for b in (self.btn_reveal, self.btn_copy):
            b.setVisible(bool(self.results) and not busy)
        self.btn_copy.setText("  Copia percorsi" if len(self.results) > 1 else "  Copia percorso")
        self.btn_errors.setVisible(bool(self.errors) and len(self.items) > 1 and not busy)
        problem = "" if busy else self._problem()
        self.btn_go.setText("Annulla" if working else "Comprimi")
        self.btn_go.setObjectName("danger" if working else "primary")
        repolish(self.btn_go)
        self.btn_go.setEnabled(working or (not busy and not problem))
        self.btn_go.setToolTip("Interrompi  (Esc)" if working else problem or "Comprimi  (Ctrl+Invio)")
        if self.items and problem:
            self.status.setText(problem + ".")
        elif self.status.text().endswith(("non esiste.", "file finale.")):
            self.status.setText("")

    # ------------------------------------------------------------------ drag & drop
    def _drag_state(self, on: bool):
        for w in (self.drop, self.list):
            w.setProperty("drag", "true" if on else "false")
            repolish(w)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and not self.busy():
            self._drag_state(True)
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drag_state(False)

    def dropEvent(self, e):
        self._drag_state(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        e.acceptProposedAction()
        if paths:
            self.load(paths, add=len(self.items) > 1)      # con una coda aperta si aggiunge, altrimenti si sostituisce
