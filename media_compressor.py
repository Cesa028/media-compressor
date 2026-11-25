"""
Media Compressor — comprime foto e video scegliendo peso, qualità, formato e nome del risultato.
Requisiti (solo per eseguire il sorgente): Python 3.10+, PySide6, Pillow, pi-heif, ffmpeg.
La versione installata include già tutto.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

if importlib.util.find_spec("PySide6") is None:
    _msg = (f"PySide6 non è installato per questo Python:\n{sys.executable}\n\n"
            f"Installalo con:\n\"{sys.executable}\" -m pip install PySide6 Pillow pi-heif")
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, _msg, "Media Compressor", 0x10)
    print(_msg, file=sys.stderr)
    sys.exit(1)

from PySide6.QtWidgets import QApplication

import icons
from common import APP_NAME, APP_VERSION, log, setup_logging
from dialogs import ErrorDialog
from ui import MainWindow, apply_theme, read_theme_pref


def _excepthook(exc_type, exc, tb):
    """Nessun traceback nudo all'utente: messaggio chiaro, dettaglio copiabile, log su file."""
    detail = "".join(traceback.format_exception(exc_type, exc, tb))
    log.error("Errore non gestito:\n%s", detail)
    try:
        ErrorDialog(QApplication.activeWindow(), "Qualcosa è andato storto",
                    "Si è verificato un errore imprevisto. Puoi continuare a usare il programma; "
                    "se il problema si ripete, copia i dettagli e inviali a chi mantiene l'app.", detail).exec()
    except Exception:  # noqa: BLE001
        print(detail, file=sys.stderr)


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MediaCompressor.App")
        except Exception:  # noqa: BLE001
            pass
    setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")
    app.setWindowIcon(icons.app_icon())
    tmp = Path(tempfile.mkdtemp(prefix="mediacompressor_"))
    app.aboutToQuit.connect(lambda: shutil.rmtree(tmp, ignore_errors=True))
    apply_theme(app, read_theme_pref(), tmp / "ui")
    sys.excepthook = _excepthook
    log.info("Avvio %s %s", APP_NAME, APP_VERSION)
    w = MainWindow(tmp)
    w.show()
    if len(sys.argv) > 1:
        w.load(sys.argv[1:])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
