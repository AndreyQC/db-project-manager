"""GUI entry point (db-pm-gui)."""

from __future__ import annotations

import sys

import darkdetect
from PySide6.QtWidgets import QApplication

from db_project_manager.infrastructure.config.app_config import load_cfg
from db_project_manager.presentation.gui.main_window import MainWindow


def main() -> int:
    app = QApplication(sys.argv)

    if darkdetect.isDark():
        app.setStyle("Fusion")
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPalette

        palette = app.palette()
        dark = Qt.GlobalColor.darkGray
        white = Qt.GlobalColor.white
        palette.setColor(QPalette.ColorRole.Window, dark)
        palette.setColor(QPalette.ColorRole.WindowText, white)
        palette.setColor(QPalette.ColorRole.Base, dark)
        palette.setColor(QPalette.ColorRole.AlternateBase, dark)
        palette.setColor(QPalette.ColorRole.Text, white)
        palette.setColor(QPalette.ColorRole.Button, dark)
        palette.setColor(QPalette.ColorRole.ButtonText, white)
        palette.setColor(QPalette.ColorRole.ToolTipBase, dark)
        palette.setColor(QPalette.ColorRole.ToolTipText, white)
        app.setPalette(palette)

    cfg = load_cfg()
    window = MainWindow(cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
