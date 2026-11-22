@echo off
cd /d "%~dp0"
if exist "%USERPROFILE%\Desktop\Actualizar CDC NOC 2.lnk" (
  start "" "%USERPROFILE%\Desktop\Actualizar CDC NOC 2.lnk"
) else (
  py -3 INSTALADOR.py --actualizar
)
