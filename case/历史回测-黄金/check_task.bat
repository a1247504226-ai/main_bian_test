@echo off
echo ============================================================
echo   Task status: GoldTrendStrategy
echo ============================================================
schtasks /Query /TN "GoldTrendStrategy" /V /FO LIST
echo.
echo ------------------------------------------------------------
echo If the task info is shown above, the task EXISTS.
echo.
echo To see it in the GUI:
echo   1) Press Win+R, type:  taskschd.msc  , press Enter
echo   2) In left tree click: Task Scheduler Library  (the ROOT)
echo   3) Press F5 to refresh
echo   4) Look for "GoldTrendStrategy" in the middle list
echo ------------------------------------------------------------
pause
