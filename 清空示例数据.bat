@echo off
chcp 65001 >nul
title 天光云影 · 清空示例数据
cd /d "%~dp0"
echo.
echo   ============================================
echo    清空示例投稿数据，准备正式征稿
echo   ============================================
echo.
echo   会删除：投稿记录、投稿照片、缩略图、已生成的地图
echo   会保留：三校区底图数据（不用重新下载）
echo.
python reset_data.py
pause
