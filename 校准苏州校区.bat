@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   ============================================
echo    苏州校区 地理配准校准台
echo   ============================================
echo.
echo   浏览器会自动打开 http://localhost:8502
echo   拖动左侧滑块，把红色建筑轮廓拖到真实校园上，
echo   对上之后点「保存配准参数」。
echo.
echo   关掉这个黑窗口就等于关闭校准台。
echo.
python -m streamlit run "tools\苏州配准校准台.py" --server.port 8502 --server.headless false
pause
