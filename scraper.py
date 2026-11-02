import datetime as dt, html, json, os, re, sys, time
import requests

cfg = json.load(open("config.json", encoding="utf-8"))
try:
    db = json.load(open("data.json", encoding="utf-8"))
except Exception:
    db = {}
db.setdefault("anuncios", {})
PRECIO_ERRONEO = 20000.0
for _a in db["anuncios"].values():
    if _a.get("precio") == PRECIO_ERRONEO:
        _a["precio"] = None
    _a["historial"] = [x for x in _a.get("historial", []) if x[1] not in (PRECIO_ERRONEO, None)]
db["firmas"] = {f"{_a['zona']}|{int(_a['precio'])}|{_a['m2']}|{_a['hab']}": _i
                for _i, _a in db["anuncios"].items() if _a.get("precio") and not _a.get("duplicado_de")}
TG_TOKEN, TG_ID = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_ID")
RISK = re.compile("|".join(cfg["palabras_riesgo"]), re.I)
HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "es-ES,es;q=0.9"}
hoy = dt.date.today().isoformat()
BASE = "https://www.trovimap.com/venta/vivienda/alicante-alacant/{}"
FIN = re.compile(r"Ver más\s*Contactar")
M2 = re.compile(r"(?<!Mín )(\d{2,4}) m²\s+(\d+)(?:\s+(\d+))?")
PRECIO = re.compile(r"(?<!Máx )(?<!Mín )(\d{1,3}(?:\.\d{3})+) €(?!/)")
IDS = re.compile(r"(?<![\d-])(\d{5,7}-\d{7,9})(?![\d-])")


def tg(texto):
    if not (TG_TOKEN and TG_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_ID, "text": texto, "disable_web_page_preview": "true"}, timeout=20)
    except Exception as e:
        print("Telegram falló:", e)


if len(sys.argv) > 1 and sys.argv[1] == "enviar":
    try:
        pendientes = json.load(open("pendientes.json", encoding="utf-8"))
    except Exception:
        pendientes = []
    for m in pendientes:
        tg(m)
        time.sleep(1)
    raise SystemExit


def euros(n):
    return f"{int(n):,}".replace(",", ".") + " €" if n else "precio ?"


def limpia(h):
    h = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


SIN_LEER = {}
H4 = re.compile(r"<h4[^>]*>(.*?)</h4>", re.S)
SOLO_PRECIO = re.compile(r"(\d{1,3}(?:\.\d{3})+) €")
EUR_M2 = re.compile(r"(\d{1,3}(?:\.\d{3})*) €/m²")


def leer_pagina(h, zona):
    partes = re.split(r"Ver más(?:\s|<[^>]*>)*Contactar", html.unescape(h))[:-1]
    out = []
    for p in partes:
        ids = IDS.findall(p)
        # El precio actual es el último titular <h4> con un precio: así no se confunde con
        # otros importes de la página (por ejemplo, los del panel de filtros).
        hs = [m for m in H4.finditer(p) if SOLO_PRECIO.fullmatch(limpia(m.group(1)))]
        if not ids or not hs:
            SIN_LEER[zona] = SIN_LEER.get(zona, 0) + 1
            continue
        precio = float(SOLO_PRECIO.fullmatch(limpia(hs[-1].group(1))).group(1).replace(".", ""))
        resto = limpia(p[hs[-1].end():])
        m = M2.search(resto)
        if not m:
            SIN_LEER[zona] = SIN_LEER.get(zona, 0) + 1
            continue
        mo = re.match(r"(\d{1,3}(?:\.\d{3})+) €(?!/)", resto)
        antes = float(mo.group(1).replace(".", "")) if mo and float(mo.group(1).replace(".", "")) > precio else None
        me = EUR_M2.match(resto)
        if me:
            esperado = float(me.group(1).replace(".", ""))
            if abs(precio / int(m.group(1)) - esperado) > 0.15 * esperado:
                SIN_LEER[zona] = SIN_LEER.get(zona, 0) + 1
                continue
        rest2 = resto[m.end():]
        titulo = re.sub(r"^.*?Compra - (?:Agente Inmobiliario|Particular|\w+)\s*", "", rest2[:200]).strip()[:90] or rest2[:90]
        out.append(dict(id="trovimap-" + ids[-1], fuente="Trovimap", titulo=titulo, precio=precio,
                        precio_antes=antes, m2=int(m.group(1)), hab=int(m.group(2)),
                        banos=int(m.group(3)) if m.group(3) else None, zona=zona,
                        url="https://www.trovimap.com/inmueble/" + ids[-1], texto=resto[:600]))
    if partes and not out:
        raise ValueError("no pude leer precio, m² o enlace en: " + limpia(partes[0])[-300:] + " || " + partes[0][-300:])
    return out


def leer_zona(z):
    out = []
    for pag in range(1, cfg["max_paginas"] + 1):
        params = {"maxPrice": cfg["precio_max"], "minBeds": cfg["hab_min"], "minLivingArea": cfg["m2_min"]}
        if pag > 1:
            params["page"] = pag
        r = requests.get(BASE.format(z["trovimap"]), params=params, headers=HEAD, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Trovimap respondió {r.status_code}")
        tanda = leer_pagina(r.text, z["nombre"])
        out += tanda
        if len(tanda) < 15:
            break
        time.sleep(3)
    return out


def evaluar(a):
    if a["precio"] > cfg["precio_max"] or a["m2"] < cfg["m2_min"] or a["hab"] < cfg["hab_min"]:
        return False, None
    if a["banos"] is not None and a["banos"] < cfg["banos_min"]:
        return False, None
    m = RISK.search(f"{a['titulo']} {a['texto']}")
    return True, (m.group(0) if m else None)


estado, avisos = {}, []
vistas = set(db.get("zonas_base", []))
firmas = db.setdefault("firmas", {})
for z in cfg["zonas"]:
    base = z["nombre"] not in vistas
    primeros = []
    try:
        items = leer_zona(z)
        estado[z["nombre"]] = f"{len(items)} leídos" + (f", {SIN_LEER[z['nombre']]} sin leer bien" if SIN_LEER.get(z["nombre"]) else "") + (" (primera lectura completa)" if base and items else "")
        if items:
            vistas.add(z["nombre"])
    except Exception as e:
        items, estado[z["nombre"]] = [], f"ERROR: {e}"
    for a in items:
        ok, riesgo = evaluar(a)
        if not ok:
            continue
        a["riesgo"] = riesgo
        a["incompleto"] = a["banos"] is None
        if a["precio"] / a["m2"] < cfg["eur_m2_sospechoso"]:
            a["aviso"] = f"muy barato por m² ({int(a['precio'] / a['m2'])} €/m²): comprueba por qué"
        old = db["anuncios"].get(a["id"])
        if not old:
            h = [[hoy, a["precio"]]]
            if a["precio_antes"]:
                h.insert(0, [hoy, a["precio_antes"]])
            a.update(primera_vez=hoy, ultima_vez=hoy, historial=h)
            db["anuncios"][a["id"]] = a
            firma = f"{a['zona']}|{int(a['precio'])}|{a['m2']}|{a['hab']}"
            repetido = firmas.get(firma)
            if repetido:
                a["duplicado_de"] = repetido
            else:
                firmas[firma] = a["id"]
            if not riesgo and not repetido:
                (primeros if base else avisos).append(("base" if base else "nuevo", a, None))
        else:
            if a["precio"] != old.get("precio"):
                old["historial"].append([hoy, a["precio"]])
                if old.get("precio") and not riesgo:
                    avisos.append(("baja" if a["precio"] < old["precio"] else "sube", a, old["precio"]))
            old.update({k: a.get(k) for k in ("precio", "m2", "hab", "banos", "riesgo", "incompleto", "aviso", "url")})
            old["ultima_vez"] = hoy
    if base and primeros:
        avisos += sorted(primeros, key=lambda x: x[1]["precio"])[:cfg.get("primera_lectura_max", 0)]
    time.sleep(3)

def linea(a, antes):
    t = euros(a["precio"]) + (f" (antes {euros(antes)})" if antes else "")
    t += f" · {a['m2']} m² · {a['hab']} hab" + (f" · {a['banos']} baño{'s' if a['banos'] != 1 else ''}" if a["banos"] else "")
    t += f"\n{a['titulo']}"
    if a.get("aviso"):
        t += f"\n⚠️ {a['aviso']}"
    return t + f"\n{a['url']}"


mensajes = []


def enviar_bloques():
    for z in cfg["zonas"]:
        mios = [x for x in avisos if x[1]["zona"] == z["nombre"]]
        if not mios:
            continue
        partes = [f"📍 {z['nombre']}: {len(mios)} anuncio{'s' if len(mios) != 1 else ''}, de más barato a más caro"]
        for etiqueta, tipo in (("🏠 Nuevos", "nuevo"), ("📉 Bajadas de precio", "baja"), ("📈 Subidas de precio", "sube"), ("🔎 Primera lectura de la zona: los más baratos", "base")):
            lista = sorted((x for x in mios if x[0] == tipo), key=lambda x: x[1]["precio"])
            if lista:
                partes.append(etiqueta)
                partes += [linea(a, antes) for _, a, antes in lista]
        msg = ""
        for p in partes:
            if msg and len(msg) + len(p) > 3800:
                mensajes.append(msg)
                msg = f"📍 {z['nombre']} (continuación)"
            msg = (msg + "\n\n" + p) if msg else p
        mensajes.append(msg)


enviar_bloques()

resumen = {k: ("error" if v.startswith("ERROR") else "ok") for k, v in estado.items()}
if resumen != db.get("estado_previo") or any("primera" in v for v in estado.values()):
    mensajes.append("ℹ️ Estado del buscador:\n" + "\n".join(f"{k}: {v}" for k, v in estado.items()))
db["estado_previo"] = resumen
db["zonas_base"] = sorted(vistas)
db["actualizado"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
json.dump(db, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump(mensajes, open("pendientes.json", "w", encoding="utf-8"), ensure_ascii=False)
print(estado, len(avisos), "avisos")
