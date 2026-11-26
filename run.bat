@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist .venv (
    echo [1/2] Dang tao moi truong Python...
    py -3.12 -m venv .venv || py -m venv .venv
)
call .venv\Scripts\activate.bat
echo [2/2] Kiem tra thu vien...
python -m pip install -q --index-url https://pypi.org/simple -r requirements.txt
echo.
echo  Mo trinh duyet: http://127.0.0.1:8000
echo  (Giu cua so nay mo de video hen gio duoc dang)
echo.
start "" http://127.0.0.1:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
