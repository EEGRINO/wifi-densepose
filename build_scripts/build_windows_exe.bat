@echo off
REM Сборка одного .exe для Windows (запускать из cmd в каталоге проекта или скопируйте пути).
cd /d "%~dp0..\desktop_app"
python -m pip install -U pip pyinstaller
python -m pip install -r requirements.txt
pyinstaller ruview_desktop.spec
echo.
echo Готово: desktop_app\dist\RuViewDesktop.exe
pause
