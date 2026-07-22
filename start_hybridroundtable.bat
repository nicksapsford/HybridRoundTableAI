@echo off
title HybridRoundTable A.I. - Port 5050
cd /d C:\Users\abc\Desktop\HybridRoundTableAI
start /min "HybridRoundTable A.I." cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_hybridroundtable.py
timeout /t 5 /nobreak >nul
start http://localhost:5050
