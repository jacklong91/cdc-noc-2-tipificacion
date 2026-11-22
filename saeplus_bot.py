# -*- coding: utf-8 -*-
"""Robot local para el panel de tipificacion SAEPLUS.

Instala Playwright y ejecuta `python saeplus_bot.py --servidor`.
Abre http://127.0.0.1:8765/ en el navegador y manten el servidor abierto.
"""

import json
import sys
import time
import re
import threading
import getpass
import webbrowser
import http.client
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime

# ------------------------- CONFIGURACION -------------------------
CONFIG = {
    "URL": "https://fibextelecom.saeplus.com",
    # En modo servidor el panel envia las credenciales en cada ejecucion.
    # En modo manual se leen de saeplus_credenciales.json o se piden por consola.
    "USUARIO": "",                # respaldo opcional (normalmente vacio)
    "CONTRASENA": "",             # respaldo opcional (normalmente vacio)
    "EMPRESA": "WAVE",            # empresa del campo EMPRESA
    "ARCHIVO_COLA": "cola_saeplus.json",
    "PAUSA_ENTRE_CLIENTES": 4,    # segundos de espera entre cada registro
    "MODO_PRUEBA": True,          # True = solo el 1er cliente pendiente (prueba)
    "NAVEGADOR": "chrome",        # "chrome" usa tu Chrome instalado; "chromium" usa el de Playwright
    "CARPETA_ERRORES": "errores_saeplus",
}
# -----------------------------------------------------------------

ESPERA = 20000
BASE = Path(__file__).resolve().parent
COLA = BASE / CONFIG["ARCHIVO_COLA"]
ERR = BASE / CONFIG["CARPETA_ERRORES"]
ERR.mkdir(exist_ok=True)

# Estado compartido con la pagina web (modo --servidor)
ESTADO_WEB = {"corriendo": False, "terminado": False, "procesados": 0,
              "total": 0, "log": [], "items": []}
SERVIDOR_HOST, SERVIDOR_PUERTO = "127.0.0.1", 8765
ORIGEN_LOCAL = f"http://{SERVIDOR_HOST}:{SERVIDOR_PUERTO}"
ESTADO_LOCK = threading.Lock()


def log(msg):
    linea = f"[{datetime.now():%d/%m/%Y %H:%M:%S}] {msg}"
    if sys.stdout is not None:
        print(linea, flush=True)
    with open(BASE / "bot_log.txt", "a", encoding="utf-8") as f:
        f.write(linea + "\n")
    ESTADO_WEB["log"].append(linea)
    if len(ESTADO_WEB["log"]) > 300:
        del ESTADO_WEB["log"][:-300]


def cargar_cola():
    if not COLA.exists():
        sys.exit(f"No existe {COLA}. Exporta la cola desde el panel web (Automatizacion).")
    data = json.loads(COLA.read_text(encoding="utf-8"))
    data.setdefault("items", [])
    return data


def guardar_cola(data):
    COLA.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cargar_credenciales():
    """Credenciales de respaldo para el modo manual; en servidor vienen del panel."""
    ruta = BASE / "saeplus_credenciales.json"
    if ruta.exists():
        try:
            c = json.loads(ruta.read_text(encoding="utf-8"))
            u = c.get("usuario") or CONFIG["USUARIO"]
            p = c.get("contrasena") or c.get("contrasena_saeplus") or CONFIG["CONTRASENA"]
            e = c.get("empresa") or CONFIG["EMPRESA"]
            if u and p:
                log("Credenciales SAEPLUS cargadas desde saeplus_credenciales.json "
                    f"(usuario {u}).")
                return u, p, e
            log("Aviso: el archivo de credenciales esta incompleto.")
        except Exception as ex:
            log(f"Aviso: no pude leer saeplus_credenciales.json ({ex})")
    if CONFIG["USUARIO"] and CONFIG["CONTRASENA"]:
        log("Usando credenciales de respaldo del CONFIG.")
        return CONFIG["USUARIO"], CONFIG["CONTRASENA"], CONFIG["EMPRESA"]
    print("")
    print("=== INGRESO DE CREDENCIALES SAEPLUS (no quedan guardadas) ===")
    try:
        u = input("Usuario SAEPLUS: ").strip()
        p = getpass.getpass("Contrasena SAEPLUS: ").strip()
        e = input("Empresa [WAVE]: ").strip() or "WAVE"
    except EOFError:
        sys.exit("Sin credenciales no se puede continuar.")
    if not u or not p:
        sys.exit("Usuario y contrasena son obligatorios.")
    return u, p, e


def rellenar_variables(texto, item):
    hoy = datetime.now().strftime("%d/%m/%Y")
    t = texto or ""
    return (t.replace("{nombre}", item.get("nombre", ""))
             .replace("{abonado}", item.get("nro", ""))
             .replace("{cedula}", item.get("cedula", ""))
             .replace("{telefono}", item.get("telefono", ""))
             .replace("{fecha}", hoy))


# ------------------- BUSQUEDA GENERICA DE CAMPOS -------------------
# SAEPLUS es una app AngularJS; los textos en mayusculas estan encima de
# los campos. Buscamos el nodo de texto de la etiqueta y luego el primer
# input/select/textarea (y su boton) dentro del mismo contenedor.
JS_CAMPO = """(etiqueta) => {
  const norm = s => (s||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'')
                     .toUpperCase().replace(/\\s+/g,' ').trim().replace(/:$/, '').trim();
  const objetivo = norm(etiqueta);
  const todos = [...document.querySelectorAll('*')];
  const nodo = todos.find(e => e.children.length === 0 && e.getClientRects().length && norm(e.textContent) === objetivo);
  if (!nodo) return null;
  let cont = nodo.parentElement;
  for (let i = 0; i < 5 && cont; i++) {
    const campo = cont.querySelector('input:not([type=hidden]), select, textarea');
    if (campo) {
      const boton = cont.querySelector('button, a.btn, [role=button], i.fa-search, span.fa-search');
      return { ok: true, campo: campo, boton: boton || null };
    }
    cont = cont.parentElement;
  }
  return null;
}"""

JS_CLIC_RESULTADO = """(identificador) => {
  const buscado = identificador.toUpperCase();
  const filas = [...document.querySelectorAll('table tbody tr')].filter(r =>
    r.getClientRects().length && r.innerText.toUpperCase().split(/[^A-Z0-9]+/).includes(buscado));
  if (filas.length !== 1) return filas.length;
  filas[0].click();
  return 1;
}"""


def campo(p, etiqueta, timeout=8000):
    """Devuelve (campo, boton) ElementHandles junto a la etiqueta, o (None, None)."""
    try:
        handle = p.evaluate_handle(JS_CAMPO, etiqueta)
        if not handle.get_property("ok").json_value():
            return None, None
        campo_h = handle.get_property("campo").as_element()
        boton_h = handle.get_property("boton").as_element()
        if campo_h:
            campo_h.wait_for_element_state("visible", timeout=timeout)
            return campo_h, boton_h
    except Exception:
        pass
    return None, None


def texto_visible(p, texto, timeout=6000):
    try:
        p.get_by_text(re.compile(re.escape(texto), re.I)).first.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def boton_texto(p, texto, timeout=6000):
    try:
        btn = p.get_by_role("button", name=re.compile(re.escape(texto), re.I)).first
        btn.wait_for(state="visible", timeout=timeout)
        return btn
    except Exception:
        return None


# ----------------------------- PASOS -----------------------------
def hacer_login(p, creds):
    usuario, contrasena, empresa = creds
    log("Abriendo SAEPLUS...")
    p.goto(CONFIG["URL"], timeout=90000, wait_until="domcontentloaded")
    try:
        p.locator("#login_usuario").wait_for(state="visible", timeout=15000)
    except Exception:
        if texto_visible(p, "Servicio Al Cliente", 5000):
            log("Sesion activa en SAEPLUS.")
            return
        raise RuntimeError("No se pudo reconocer la pantalla de acceso a SAEPLUS")

    p.locator("#login_usuario").fill(usuario)
    p.locator("#pass_usuario").fill(contrasena)
    # SAEPLUS solo carga las empresas al perder el foco del campo clave (blur/Tab)
    p.locator("#pass_usuario").press("Tab")
    try:
        p.wait_for_function("""() => {
          const s = document.querySelector('#empresa_user');
          return (s && [...s.options].some(o => o.value)) ||
            /combinaci[oó]n de usuario.*inv[aá]lida|usuario.*bloquead/i.test(document.body.innerText);
        }""", timeout=30000)
    except Exception:
        raise RuntimeError("SAEPLUS no mostro las empresas tras validar las credenciales; revisa el acceso antes de reintentar")
    if p.get_by_text(re.compile(r"combinaci[oó]n de usuario.*inv[aá]lida|usuario.*bloquead", re.I)).count():
        raise RuntimeError("SAEPLUS rechazo las credenciales o informo un bloqueo; no reintentes sin comprobar la cuenta")
    seleccionada = p.locator("#empresa_user").evaluate("""(s, empresa) => {
      const opcion = [...s.options].find(o => o.text.trim().toUpperCase() === empresa.trim().toUpperCase());
      if (!opcion) return false;
      s.value = opcion.value;
      s.dispatchEvent(new Event('change', {bubbles:true}));
      if (window.jQuery && window.jQuery.fn.selectpicker) window.jQuery(s).selectpicker('refresh');
      return true;
    }""", empresa)
    if not seleccionada:
        raise RuntimeError(f"La empresa {empresa} no aparece entre las opciones de SAEPLUS")
    p.locator("#iniciar").click()
    # SAEPLUS a veces responde con un error de servidor ("parsererror") que se
    # queda en "Cargando...": se detecta y se reporta sin reintentar.
    try:
        estado_login = p.wait_for_function("""() => {
          const t = document.body.innerText;
          if (/parsererror|ERROR DURANTE TRANSACCI/i.test(t)) return 'parsererror';
          const l = document.querySelector('#login_usuario');
          if (!l) return 'ok';
          const cs = getComputedStyle(l);
          if (cs.display === 'none' || cs.visibility === 'hidden') return 'ok';
          return false;
        }""", timeout=45000).json_value()
        if estado_login == "parsererror":
            raise RuntimeError("SAEPLUS devolvio parsererror al iniciar sesion")
    except Exception:
        try:
            p.screenshot(path=str(BASE / "errores_saeplus" / "login_no_confirmado.png"))
        except Exception:
            pass
        estado = p.evaluate("""() => {
          const t = document.body.innerText;
          if (/parsererror|ERROR DURANTE TRANSACCI/i.test(t)) return 'parsererror';
          return 'timeout';
        }""")
        if estado == 'parsererror':
            raise RuntimeError("SAEPLUS devolvio un error de servidor (parsererror) al iniciar sesion; el servicio esta fallando, intentalo mas tarde o reportalo a SAEPLUS")
        raise RuntimeError("SAEPLUS no confirmo el inicio de sesion; revisa errores_saeplus/login_no_confirmado.png o verificacion adicional")
    if not texto_visible(p, "Servicio Al Cliente", 45000):
        try:
            p.screenshot(path=str(BASE / "errores_saeplus" / "sesion_no_verificada.png"))
        except Exception:
            pass
        raise RuntimeError("No se pudo verificar la sesion o se requiere verificacion adicional")
    log("Sesion iniciada y verificada.")


JS_CLIC_MENU = """(t) => {
  const norm = s => (s||'').replace(/\\s+/g,' ').trim().toUpperCase();
  for (const a of document.querySelectorAll('a')) {
    if (norm(a.innerText) !== norm(t)) continue;
    const r = a.getBoundingClientRect();
    const cs = getComputedStyle(a);
    if (r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none') {
      a.click();
      return true;
    }
  }
  return false;
}"""


def ir_a_registrar(p):
    log("Navegando: Servicio Al Cliente -> Registro Saliente -> Registrar")
    for txt in ("Servicio Al Cliente", "Registro Saliente", "Registrar"):
        if not p.evaluate(JS_CLIC_MENU, txt):
            log(f"  (aviso) no pude pulsar '{txt}' - quizas ya estaba en la pantalla")
        p.wait_for_timeout(1800)
    if not texto_visible(p, "BÚSQUEDA DE ABONADOS", 15000):
        raise RuntimeError("No se encontro la pantalla de busqueda de abonados; se detuvo la cola")


def abonado_cargado(p):
    return bool(p.locator("#nro_contrato").input_value().strip())


def buscar_abonado(p, item):
    """Estrategia: cedula -> nro abonado -> busqueda avanzada por nro anterior."""
    ced = (item.get("cedula") or "").strip()
    nro = (item.get("nro") or "").strip()

    if ced:
        log("  Buscando por DOCUMENTO DE IDENTIDAD")
        f, btn = campo(p, "DOCUMENTO DE IDENTIDAD")
        if f:
            f.fill(ced)
            if btn: btn.click()
            else:
                b = boton_texto(p, "BUSCAR")
                if b: b.click()
            p.wait_for_timeout(4000)
            if abonado_cargado(p):
                return True

    if nro.upper().startswith("WM"):
        log("  Buscando por NUMERO DE ABONADO")
        f, btn = campo(p, "NÚMERO DE ABONADO")
        if f:
            f.fill(nro)
            if btn: btn.click()
            p.wait_for_timeout(4000)
            if abonado_cargado(p):
                return True

    anterior = nro if nro and not nro.upper().startswith("WM") else (item.get("anterior") or nro)
    if anterior:
        log("  Búsqueda avanzada por NRO. ABONADO ANTERIOR")
        b = boton_texto(p, "BÚSQUEDA AVANZADA")
        if b:
            b.click()
            p.wait_for_timeout(3000)
            f, _ = campo(p, "NRO. ABONADO ANTERIOR")
            if not f:
                f, _ = campo(p, "NRO. ABONADO ANTERIOR:")
            if f:
                f.fill(anterior)
                buscar = boton_texto(p, "BUSCAR")
                if buscar: buscar.click()
                p.wait_for_timeout(4000)
                for identificador in (anterior, ced, nro):
                    identificador = (identificador or "").strip().upper()
                    if not identificador:
                        continue
                    try:
                        coincidencias = p.evaluate(JS_CLIC_RESULTADO, identificador)
                    except Exception:
                        coincidencias = 0
                    p.wait_for_timeout(2500)
                    if coincidencias == 1 and abonado_cargado(p):
                        return True
                p.wait_for_timeout(3000)
                return abonado_cargado(p)
    return abonado_cargado(p)


def completar_llamada(p, item):
    combos = [("TIPO LLAMADA", item.get("tipo")),
              ("TIPO RESPUESTA", item.get("respuesta")),
              ("DETALLE RESPUESTA", item.get("detalle"))]
    for etiqueta, valor in combos:
        if not valor:
            continue
        f, _ = campo(p, etiqueta)
        if not f:
            raise RuntimeError(f"Campo {etiqueta} no encontrado en SAEPLUS")
        try:
            opciones = p.wait_for_function("""({select, valor}) => {
              const norm = t => (t || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                .toUpperCase().replace(/\\s+/g, ' ').trim();
              const coincidencias = [...select.options].filter(o => norm(o.text) === norm(valor));
              return coincidencias.length ? coincidencias.map(o => ({value: o.value, text: o.text})) : false;
            }""", arg={"select": f, "valor": valor}, timeout=15000).json_value()
        except Exception as e:
            disponibles = f.evaluate("s => [...s.options].map(o => o.text).join(' | ')")
            raise RuntimeError(f"El valor '{valor}' no existe en {etiqueta}. Opciones: {disponibles}") from e
        if len(opciones) != 1:
            raise RuntimeError(f"El valor '{valor}' es ambiguo en {etiqueta}")
        f.select_option(value=opciones[0]["value"], timeout=8000)
        log(f"  {etiqueta}: {opciones[0]['text']}")

    obs = rellenar_variables(item.get("observacion", ""), item)
    if obs:
        f, _ = campo(p, "OBSERVACIÓN")
        if not f:
            f = p.locator("textarea").first
        if f:
            f.fill(obs)
    p.wait_for_timeout(500)


def pulsar_registrar(p):
    p.locator("#registrar").click(timeout=8000)
    confirmacion = p.locator(".modal:visible").filter(has_text="¿Seguro que desea enviar este formulario?")
    confirmacion.locator("button").filter(has_text=re.compile(r"^S[IÍ]$", re.I)).click(timeout=8000)
    log("  Envio confirmado en SAEPLUS.")


def limpiar_formulario(p):
    p.locator("#salir").click(timeout=8000)
    p.wait_for_function("() => !document.querySelector('#nro_contrato').value", timeout=10000)


def registrar_cliente(p, item):
    nombre = item.get("nombre") or item.get("nro") or item.get("cedula")
    log(f"Cliente: {nombre}")
    limpiar_formulario(p)
    p.wait_for_timeout(1000)

    if not buscar_abonado(p, item):
        item["status"] = "NO ENCONTRADO"
        item["log"] = "SAEPLUS no devolvio datos del abonado"
        p.screenshot(path=str(ERR / f"no_encontrado_{item['id']}.png"))
        log(f"  NO ENCONTRADO en SAEPLUS: {nombre}")
        return

    abonado = p.locator("#nro_contrato").input_value().strip()
    texto = p.locator("body").inner_text()
    cliente = re.search(r"CLIENTE:\s*(.*?)\s*ESTATUS:", texto, re.S)
    documento = re.search(r"DOCUMENTO:\s*(\S+)", texto)
    if not abonado or not cliente:
        raise RuntimeError("No se pudo verificar la identidad del abonado antes de registrar")
    cedula = re.sub(r"\D", "", item.get("cedula", ""))
    if item.get("cedula") and (not cedula or not documento
            or re.sub(r"\D", "", documento.group(1)) != cedula):
        raise RuntimeError("El documento del abonado cargado no coincide con la cola; no se registro")
    nro = item.get("nro", "").strip().upper()
    if not cedula and nro.startswith("WM") and abonado.upper() != nro:
        raise RuntimeError("El numero del abonado cargado no coincide con la cola; no se registro")
    item["abonado_saeplus"] = abonado
    item["cliente_saeplus"] = cliente.group(1).strip()
    log("  Abonado cargado y verificado. Completando datos de la llamada...")
    completar_llamada(p, item)
    esperado = {
        "abonado": abonado,
        "cliente": cliente.group(1).strip(),
        "observacion": p.locator("#obser_lla").input_value(),
        "detalle": p.locator("#id_drl option:checked").inner_text(),
        "respuesta": p.locator("#id_trl option:checked").inner_text(),
    }
    anteriores = p.locator("#datagrid_llamadas tbody tr").evaluate_all(
        "rows => rows.map(r => [...r.cells].map(e => e.textContent.trim()).join('|'))")
    item["status"] = "REVISAR"
    item["log"] = "REGISTRAR enviado; confirmar en SAEPLUS antes de volver a ejecutarlo"
    pulsar_registrar(p)
    try:   # SAEPLUS no refresca la grilla solo: hay que pulsar BUSCAR de "llamadas registradas"
        p.get_by_role("button", name=re.compile(r"^BUSCAR$", re.I)).last.click(timeout=8000)
    except Exception:
        pass
    try:
        registro = p.wait_for_function("""({anteriores, esperado}) => {
          const norm = t => (t || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
            .toUpperCase().replace(/\\s+/g, ' ').trim();
          for (const fila of document.querySelectorAll('#datagrid_llamadas tbody tr')) {
            const crudo = [...fila.cells].map(e => e.textContent.trim()).join('|');
            if (anteriores.includes(crudo)) continue;
            const c = [...fila.cells].map(e => norm(e.textContent));
            if (c.length < 7) continue;
            const cliente = norm(esperado.cliente);
            const todo = c.join(' ');
            const identidad = (cliente && c[5].includes(cliente)) || todo.includes(norm(esperado.abonado));
            if (!identidad) continue;
            if (esperado.detalle && c[3] !== norm(esperado.detalle)) continue;
            if (esperado.respuesta && c[4] !== norm(esperado.respuesta)) continue;
            if (esperado.observacion && !c[6].includes(norm(esperado.observacion).slice(0, 80))) continue;
            return {firma: crudo, fecha: c[1], hora: c[2]};
          }
          return false;
        }""", arg={"anteriores": anteriores, "esperado": esperado}, timeout=20000).json_value()
    except Exception:
        item["log"] = "REGISTRAR enviado; no se confirmó una fila nueva. Revisar SAEPLUS antes de repetir"
        p.screenshot(path=str(ERR / f"revisar_{item['id']}.png"))
        log(f"  REVISAR: {nombre}")
        return
    item["status"] = "PROCESADO"
    item["registro_saeplus"] = f"REG {registro['fecha']} {registro['hora']}"
    item["log"] = f"Registro verificado en SAEPLUS ({item['registro_saeplus']}) para {abonado}"
    log(f"  PROCESADO: {nombre} - {item['log']}")

def diagnostico(p):
    log("MODO DIAGNOSTICO: volcando estructura de la pantalla...")
    ir_a_registrar(p)
    p.wait_for_timeout(3000)
    info = p.evaluate("""() => {
      const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
      const ins = [...document.querySelectorAll('input:not([type=hidden])')]
        .filter(vis).map(e => ({tipo:e.type||'text', id:e.id, name:e.name,
          placeholder:e.placeholder, etiqueta:(e.closest('div')||{}).innerText?.slice(0,60)||''}));
      const sels = [...document.querySelectorAll('select')].filter(vis)
        .map(e => ({id:e.id, name:e.name, opciones:[...e.options].slice(0,40).map(o=>o.text)}));
      const btns = [...document.querySelectorAll('button, a.btn')].filter(vis)
        .map(e => e.innerText.trim()).filter(t => t);
      const links = [...document.querySelectorAll('a')].filter(vis)
        .map(e => e.innerText.trim()).filter(t => t && t.length < 40).slice(0, 60);
      return {inputs: ins, selects: sels, botones: btns, enlaces: links};
    }""")
    (BASE / "diagnosticos.txt").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    p.screenshot(path=str(BASE / "diagnostico_registrar.png"), full_page=True)
    log("Listo: revisa diagnosticos.txt y diagnostico_registrar.png")
    input("Pulsa Enter para cerrar el navegador...")



# ------------------- MODO SERVIDOR (pagina web) -------------------
def procesar_cola_web(items, creds):
    """Ejecuta la cola que envia la pagina web y reporta el avance en ESTADO_WEB."""
    with ESTADO_LOCK:
        ESTADO_WEB.update(corriendo=True, terminado=False, procesados=0,
                          total=len(items), items=list(items), log=[])
    log(f"[web] Iniciando tipificacion automatica de {len(items)} cliente(s)...")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            navegador = pw.chromium.launch(
                channel=CONFIG["NAVEGADOR"] if CONFIG["NAVEGADOR"] == "chrome" else None,
                headless=True)
            ctx = navegador.new_context(viewport={"width": 1600, "height": 950})
            p = ctx.new_page()
            p.on("dialog", lambda d: d.accept())
            hacer_login(p, creds)
            ir_a_registrar(p)
            for item in items:
                try:
                    registrar_cliente(p, item)
                except Exception as e:
                    item["status"] = "REVISAR" if item.get("status") == "REVISAR" else "ERROR"
                    item["log"] = str(e)[:300]
                    log(f"  ERROR en {item.get('nombre')}: {e}")
                    try:
                        p.screenshot(path=str(ERR / f"error_{item['id']}.png"))
                    except Exception:
                        pass
                    try:
                        limpiar_formulario(p)
                    except Exception:
                        pass
                item["ts"] = datetime.now().isoformat(timespec="seconds")
                with ESTADO_LOCK:
                    ESTADO_WEB["procesados"] += 1
                    ESTADO_WEB["items"] = list(items)
                time.sleep(CONFIG["PAUSA_ENTRE_CLIENTES"])
            navegador.close()
    except Exception as e:
        log(f"[web] Error grave durante la ejecucion: {e}")
        for item in items:
            if item.get("status") == "PENDIENTE":
                item.update(status="ERROR", log=str(e)[:300],
                            ts=datetime.now().isoformat(timespec="seconds"))
    with ESTADO_LOCK:
        ESTADO_WEB["items"] = list(items)
        ESTADO_WEB.update(corriendo=False, terminado=True)
    resumen = {}
    for i in items:
        resumen[i["status"]] = resumen.get(i["status"], 0) + 1
    log(f"[web] FIN. Resultados: {resumen}")


PANEL_HTML = None


def panel_html():
    global PANEL_HTML
    if PANEL_HTML is None:
        html_path = BASE / "cdc-noc-2-auto.html"
        if not html_path.exists():
            raise FileNotFoundError(f"No se encontro {html_path}")
        PANEL_HTML = html_path.read_text(encoding="utf-8")
    return PANEL_HTML


class _Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json({"ok": True, "app": "cdc-noc-2", "version": 2})
            return
        if self.path in ("/", "/index.html", "/xlsx.full.min.js"):
            es_libreria = self.path == "/xlsx.full.min.js"
            try:
                body = (BASE / "xlsx.full.min.js").read_bytes() if es_libreria else panel_html().encode("utf-8")
            except FileNotFoundError as e:
                self._json({"ok": False, "msg": str(e)}, 500)
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/javascript; charset=utf-8" if es_libreria else "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/status":
            with ESTADO_LOCK:
                estado = {"corriendo": ESTADO_WEB["corriendo"],
                          "terminado": ESTADO_WEB["terminado"],
                          "procesados": ESTADO_WEB["procesados"],
                          "total": ESTADO_WEB["total"],
                          "log": ESTADO_WEB["log"][-60:]}
            self._json(estado)
            return
        if self.path == "/results":
            with ESTADO_LOCK:
                self._json({"items": list(ESTADO_WEB.get("items", []))})
            return
        self._json({"ok": False, "msg": "ruta desconocida"}, 404)

    def do_POST(self):
        if self.path != "/run":
            self._json({"ok": False, "msg": "ruta desconocida"}, 404)
            return
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self._json({"ok": False, "msg": "Solo se aceptan conexiones locales"}, 403)
            return
        try:
            largo = int(self.headers.get("Content-Length", 0))
            if largo <= 0 or largo > 5_000_000:
                self._json({"ok": False, "msg": "Solicitud invalida"}, 400)
                return
            payload = json.loads(self.rfile.read(largo) or b"{}")
        except Exception:
            self._json({"ok": False, "msg": "JSON invalido"}, 400)
            return
        if not isinstance(payload, dict):
            self._json({"ok": False, "msg": "Solicitud invalida"}, 400)
            return
        items = payload.get("items")
        c = payload.get("credenciales")
        if not isinstance(items, list) or not items or not isinstance(c, dict):
            self._json({"ok": False, "msg": "Cola o credenciales invalidas"}, 400)
            return
        for item in items:
            if (not isinstance(item, dict) or item.get("status") != "PENDIENTE"
                    or not isinstance(item.get("id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item["id"])
                    or any(not isinstance(v, str) for v in item.values())
                     or any(not item.get(k) for k in ("tipo", "respuesta", "detalle", "observacion"))
                    or not (item.get("cedula") or item.get("nro"))):
                self._json({"ok": False, "msg": "La cola contiene un cliente invalido o no pendiente"}, 400)
                return
        if len({item["id"] for item in items}) != len(items):
            self._json({"ok": False, "msg": "La cola contiene clientes duplicados"}, 400)
            return
        creds = (c.get("usuario"), c.get("contrasena"), c.get("empresa") or CONFIG["EMPRESA"])
        if any(not isinstance(v, str) or not v for v in creds):
            self._json({"ok": False, "msg": "Faltan credenciales SAEPLUS"}, 400)
            return
        with ESTADO_LOCK:
            if ESTADO_WEB["corriendo"]:
                self._json({"ok": False, "msg": "Ya hay una ejecucion en curso"}, 409)
                return
            ESTADO_WEB.update(corriendo=True, terminado=False, procesados=0,
                              total=len(items), items=list(items), log=[])
        threading.Thread(target=procesar_cola_web, args=(items, creds),
                         daemon=True).start()
        self._json({"ok": True, "total": len(items)})

    def log_message(self, *args):
        pass


class _ServidorLocal(ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def ejecutar_servidor(abrir=False):
    try:
        srv = _ServidorLocal((SERVIDOR_HOST, SERVIDOR_PUERTO), _Handler)
    except OSError as error:
        if not abrir:
            raise
        conexion = http.client.HTTPConnection(SERVIDOR_HOST, SERVIDOR_PUERTO, timeout=3)
        try:
            conexion.request("GET", "/health")
            respuesta = conexion.getresponse()
            estado = json.loads(respuesta.read())
            if respuesta.status != 200 or estado.get("app") != "cdc-noc-2" or estado.get("version") != 2:
                raise RuntimeError("El puerto 8765 esta ocupado por otro servidor. Cierra el servidor anterior antes de abrir CDC NOC 2.") from error
        finally:
            conexion.close()
        webbrowser.open(ORIGEN_LOCAL + "/")
        return
    with srv:
        log(f"Servidor del robot en {ORIGEN_LOCAL}/")
        log("TIPIFICAR ejecuta el bot en segundo plano; consulta el avance en el panel.")
        if abrir:
            webbrowser.open(ORIGEN_LOCAL + "/")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log("Servidor detenido.")


# ------------------------------ MAIN ------------------------------
def main():
    if "--servidor" in sys.argv or "--abrir" in sys.argv:
        ejecutar_servidor(abrir="--abrir" in sys.argv)
        return
    data = cargar_cola()
    pendientes = [i for i in data["items"] if i.get("status") == "PENDIENTE"]
    if not pendientes:
        sys.exit("No hay clientes pendientes en la cola.")
    if CONFIG["MODO_PRUEBA"]:
        pendientes = pendientes[:1]
        log("MODO PRUEBA: solo se procesara 1 cliente.")

    creds = cargar_credenciales()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(
            channel=CONFIG["NAVEGADOR"] if CONFIG["NAVEGADOR"] == "chrome" else None,
            headless=False)
        ctx = navegador.new_context(viewport={"width": 1600, "height": 950})
        p = ctx.new_page()
        p.on("dialog", lambda d: d.accept())

        hacer_login(p, creds)
        if "--diagnostico" in sys.argv:
            diagnostico(p)
            navegador.close()
            return
        ir_a_registrar(p)

        for item in pendientes:
            try:
                registrar_cliente(p, item)
            except Exception as e:
                item["status"] = "REVISAR" if item.get("status") == "REVISAR" else "ERROR"
                item["log"] = str(e)[:300]
                log(f"  ERROR en {item.get('nombre')}: {e}")
                try:
                    p.screenshot(path=str(ERR / f"error_{item['id']}.png"))
                except Exception:
                    pass
                try:   # por si el formulario quedo a medias
                    limpiar_formulario(p)
                except Exception:
                    pass
            item["ts"] = datetime.now().isoformat(timespec="seconds")
            guardar_cola(data)          # guardado despues de CADA cliente
            time.sleep(CONFIG["PAUSA_ENTRE_CLIENTES"])

        navegador.close()

    resumen = {}
    for i in data["items"]:
        resumen[i["status"]] = resumen.get(i["status"], 0) + 1
    log(f"FIN. Resultados: {resumen}")
    log("Importa el JSON actualizado en el panel web para ver el progreso.")


if __name__ == "__main__":
    main()
