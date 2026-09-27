@echo off
chcp 65001 >nul
title 天光云影 · 打卡点地图网站
cd /d "%~dp0"
echo.
echo   ============================================
echo    天光云影 · 校园天空摄影大赛
echo    投稿 + 打卡点地图（同一个网站）
echo   ============================================
echo.
echo   启动中，请稍等几秒……
echo.
echo   然后手动打开浏览器，访问：
echo.
echo       首页（同学投稿）  http://localhost:8501
echo       打卡点地图        http://localhost:8501/打卡点地图
echo.
echo   同一个 Wi-Fi 下的手机也能看/投稿，地址见下面 Network URL。
echo.
echo   关闭这个黑窗口就结束；已提交的作品不会丢。
echo.
python -m streamlit run app.py --server.port 8501
echo.
echo   网站已关闭。
pause
