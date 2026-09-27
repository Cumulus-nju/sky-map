@echo off
chcp 65001 >nul
title 天光云影 · 生成打卡点地图
cd /d "%~dp0"
echo.
echo   ============================================
echo    生成「校园天空打卡点地图」
echo   ============================================
echo.
python map_build.py --csv
echo.
echo   在线版地图： data\exports\校园天空打卡点地图_在线版.html
echo   （把 data\exports 整个文件夹打包发给别人，或直接发到公众号）
echo.
echo   如需断网也能看的单文件版，另运行：
echo       python map_build.py --offline
echo.
pause
