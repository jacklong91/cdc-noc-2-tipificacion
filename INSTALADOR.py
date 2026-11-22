# -*- coding: utf-8 -*-
"""Instalador/actualizador portátil de CDC NOC 2 (solo stdlib).

Uso:
  INSTALADOR.exe            instala o reinstala y abre el panel
  INSTALADOR.exe --actualizar   solo actualiza los archivos desde GitHub
"""

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = "jacklong91/cdc-noc-2-tipificacion"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/main.zip"
TITULO = "CDC NOC 2"
INSTALAR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "CDC-NOC-2"
SOLO_ACTUALIZAR = "--actualizar" in sys.argv


def aviso(texto, error=False):
    if os.environ.get("CDC_NOC2_SILENCIOSO"):
        print(texto)
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, texto, TITULO, 16 if error else 64)
    except Exception:
        print(texto)


def ejecutar(comando, **kwargs):
    return subprocess.run(comando, check=False, **kwargs)


def ruta_python():
    intentos = ((["py", "-3", "-c", "import sys;print(sys.executable)"], True),
                (["python", "-c", "import sys;print(sys.executable)"], False))
    for args, usa_launcher in intentos:
        try:
            r = ejecutar(args, capture_output=True, text=True, timeout=25)
            exe = Path(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else None
            if exe and exe.exists():
                return exe, usa_launcher
        except Exception:
            continue
    return None, False


def lanzador_sin_consola(exe, usa_launcher):
    if usa_launcher:
        local = os.environ.get("LOCALAPPDATA", "")
        candidatos = [Path(local) / "Programs" / "Python" / "Launcher" / "pyw.exe",
                      exe.parent.parent / "Launcher" / "pyw.exe"]
        for candidato in candidatos:
            if candidato.exists():
                return candidato, "-3"
    candidato = exe.with_name("pythonw.exe")
    return (candidato if candidato.exists() else exe), ""


def bajar_codigo(destino):
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "codigo.zip"
        aviso("Descargando la última versión desde GitHub…")
        urllib.request.urlretrieve(ZIP_URL, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        carpetas = [p for p in Path(tmp).iterdir() if p.is_dir() and p.name.startswith("cdc-noc-2-tipificacion-")]
        if not carpetas:
            raise RuntimeError("El ZIP de GitHub no tiene la estructura esperada")
        origen = carpetas[0]
        destino.mkdir(parents=True, exist_ok=True)
        for item in origen.iterdir():
            objetivo = destino / item.name
            if item.is_dir():
                shutil.copytree(item, objetivo, dirs_exist_ok=True)
            else:
                shutil.copy2(item, objetivo)


def preparar_python():
    exe, usa_launcher = ruta_python()
    if exe:
        return exe, usa_launcher
    aviso("No se encontró Python. Se intentará instalar Python 3.12 con winget; si no aparece el asistente, instálalo desde python.org y vuelve a ejecutar este instalador.", True)
    try:
        ejecutar(["winget", "install", "-e", "--id", "Python.Python.3.12",
                  "--accept-package-agreements", "--accept-source-agreements"], timeout=600)
    except Exception:
        pass
    exe, usa_launcher = ruta_python()
    if not exe:
        raise RuntimeError("Python no está instalado. Instálalo desde python.org y vuelve a ejecutar el instalador.")
    return exe, usa_launcher


def instalar_dependencias(exe):
    aviso("Instalando dependencias (primera vez puede tardar unos minutos)…")
    r = ejecutar([str(exe), "-m", "pip", "install", "-r", "requirements.txt"],
                 cwd=str(INSTALAR), timeout=900)
    if r.returncode != 0:
        raise RuntimeError("No se pudieron instalar las dependencias de Python")
    aviso("Instalando el navegador de Playwright (Chromium)…")
    r = ejecutar([str(exe), "-m", "playwright", "install", "chromium"], cwd=str(INSTALAR), timeout=900)
    if r.returncode != 0:
        raise RuntimeError("No se pudo instalar Chromium para Playwright")


def configurar_windows(pyw, prefijo, actualizador=""):
    ps = f'''
$pyw = '{pyw}'
$prefix = '{prefijo}'
$install = '{INSTALAR}'
$actualizador = '{actualizador}'
$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$startup = [Environment]::GetFolderPath('Startup')

$sc = $ws.CreateShortcut((Join-Path $desktop 'CDC NOC 2.lnk'))
$sc.TargetPath = $pyw
$sc.Arguments = $prefix + ' "' + (Join-Path $install 'INICIAR_PANEL.pyw') + '"'
$sc.WorkingDirectory = $install
$sc.Description = 'CDC NOC 2 - Tipificacion automatica SAEPLUS'
$sc.Save()

$sr = $ws.CreateShortcut((Join-Path $startup 'CDC NOC 2 - Robot local.lnk'))
$sr.TargetPath = $pyw
$sr.Arguments = $prefix + ' "' + (Join-Path $install 'saeplus_bot.py') + '" --servidor'
$sr.WorkingDirectory = $install
$sr.WindowStyle = 7
$sr.Save()

if ($actualizador -ne '') {{
  $su = $ws.CreateShortcut((Join-Path $desktop 'Actualizar CDC NOC 2.lnk'))
  $su.TargetPath = $actualizador
  $su.Arguments = '--actualizar'
  $su.WorkingDirectory = $install
  $su.Description = 'Actualizar CDC NOC 2 desde GitHub'
  $su.Save()
}}

$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\\Classes\\cdcnoc2')
$key.SetValue('', 'URL:CDC NOC 2 Robot')
$key.SetValue('URL Protocol', '')
$cmd = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\\Classes\\cdcnoc2\\shell\\open\\command')
$cmd.SetValue('', '"' + $pyw + '" ' + $prefix + ' "' + (Join-Path $install 'saeplus_bot.py') + '" --servidor')
'''
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as f:
        f.write(ps)
        script = f.name
    try:
        r = ejecutar(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
                     capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(r.stderr or "No se pudieron crear los accesos directos")
    finally:
        os.unlink(script)


def main():
    try:
        bajar_codigo(INSTALAR)
        exe, usa_launcher = preparar_python()
        if not SOLO_ACTUALIZAR:
            instalar_dependencias(exe)
        pyw, prefijo = lanzador_sin_consola(exe, usa_launcher)
        actualizador = sys.executable if getattr(sys, "frozen", False) else ""
        configurar_windows(pyw, prefijo, actualizador)
        if not SOLO_ACTUALIZAR:
            comando = [str(pyw)] + ([prefijo] if prefijo else []) + [str(INSTALAR / "INICIAR_PANEL.pyw")]
            subprocess.Popen(comando, cwd=str(INSTALAR), creationflags=subprocess.DETACHED_PROCESS)
            aviso("CDC NOC 2 instalado. El panel se abrió en tu navegador.\n\n"
                  "Accesos creados:\n- Escritorio: CDC NOC 2\n- Inicio: robot local automático\n\n"
                  f"Archivos en: {INSTALAR}")
        else:
            aviso("CDC NOC 2 actualizado a la última versión.")
    except Exception as error:
        aviso(f"No se pudo completar la instalación:\n{error}", True)
        sys.exit(1)


if __name__ == "__main__":
    main()
