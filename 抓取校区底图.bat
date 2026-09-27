@echo off
chcp 65001 >nul
title 天光云影 · 抓取校区底图
cd /d "%~dp0"
echo.
echo   抓取南京大学三校区 OSM 建筑轮廓 / 步道 / 机位 POI
echo   （只需在首次部署或想更新底图时运行，需要联网）
echo.
python -u fetch_base.py
pause
