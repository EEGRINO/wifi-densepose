# AppImage для RuView Desktop (опционально)

Полноценный AppImage для PyQt6 требует упаковки Qt-плагинов и библиотек системы. Рекомендуемые пути:

1. **Простой вариант (CachyOS / Arch):** используйте `build_scripts/run_cachyos.sh` — без AppImage, через venv.
2. **AppImage:** установите [linuxdeploy](https://github.com/linuxdeploy/linuxdeploy) и [linuxdeploy-plugin-qt](https://github.com/linuxdeploy/linuxdeploy-plugin-qt), соберите `onedir` через PyInstaller, затем оберните результат в AppImage по официальной инструкции Qt.

Если нужен готовый рецепт под ваш CI — добавьте шаг `pyinstaller ruview_desktop.spec --onedir`, затем `linuxdeploy --appdir AppDir ...`.
