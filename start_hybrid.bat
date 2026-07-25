@echo off
echo.
echo  ========================================
echo   ALBION HYBRID DESK -- STARTING UP
echo  ========================================
echo.
echo  [1/7] Starting HybridRoundTable ...
start /min "HybridRoundTable" cmd /c "cd /d C:\Users\abc\Desktop\HybridRoundTableAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_hybridroundtable.py"
timeout /t 5 /nobreak >nul
echo  [2/7] Starting CryptoHybrid ...
start /min "CryptoHybrid Dashboard" cmd /c "cd /d C:\Users\abc\Desktop\CryptoHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_btc.py"
start /min "CryptoHybrid Engine" cmd /c "cd /d C:\Users\abc\Desktop\CryptoHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_btc.py"
timeout /t 15 /nobreak >nul
echo  [3/7] Starting FTSEHybrid ...
start /min "FTSEHybrid Dashboard" cmd /c "cd /d C:\Users\abc\Desktop\FTSEHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_ftse.py"
start /min "FTSEHybrid Engine" cmd /c "cd /d C:\Users\abc\Desktop\FTSEHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_ftse.py"
timeout /t 15 /nobreak >nul
echo  [4/7] Starting GoldHybrid ...
start /min "GoldHybrid Dashboard" cmd /c "cd /d C:\Users\abc\Desktop\GoldHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_gold.py"
start /min "GoldHybrid Engine" cmd /c "cd /d C:\Users\abc\Desktop\GoldHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_gold.py"
timeout /t 15 /nobreak >nul
echo  [5/7] Starting USHybrid ...
start /min "USHybrid Dashboard" cmd /c "cd /d C:\Users\abc\Desktop\USHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_us.py"
start /min "USHybrid Engine" cmd /c "cd /d C:\Users\abc\Desktop\USHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_us.py"
timeout /t 15 /nobreak >nul
echo  [6/7] Starting OilHybrid ...
start /min "OilHybrid Dashboard" cmd /c "cd /d C:\Users\abc\Desktop\OilHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_oil.py"
start /min "OilHybrid Engine" cmd /c "cd /d C:\Users\abc\Desktop\OilHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_oil.py"
timeout /t 15 /nobreak >nul
echo  [7/7] Starting NikkeiHybrid ...
start /min "NikkeiHybrid Dashboard" cmd /c "cd /d C:\Users\abc\Desktop\NikkeiHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_nikkei.py"
start /min "NikkeiHybrid Engine" cmd /c "cd /d C:\Users\abc\Desktop\NikkeiHybridAI && C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe watchdog_nikkei.py"
timeout /t 15 /nobreak >nul
echo.
echo  ========================================
echo   HYBRID DESK LAUNCHED
echo   HybridRoundTable: http://localhost:5050
echo  ========================================
echo.
start http://localhost:5050
exit
