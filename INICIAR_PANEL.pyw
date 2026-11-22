from saeplus_bot import ejecutar_servidor

try:
    ejecutar_servidor(abrir=True)
except Exception as error:
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        None, str(error), "CDC NOC 2: no se pudo iniciar", 16)
