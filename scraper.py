import datetime as dt, html, json, os, re, time
import requests

cfg = json.load(open("config.json", encoding="utf-8"))
try:
    db = json.load(open("data.json", encoding="utf-8"))
except Exception:
    db = {}
db.setdefault("anuncios", {})
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
                      data={"chat_id": TG_ID, "text": texto}, timeout=20)
    except Exception as e:
        print("Telegram falló:", e)


def euros(n):
    return f"{int(n):,}".replace(",", ".") + " €" if n else "precio ?"


def limpia(h):
    h = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()


def leer_pagina(h, zona):
    partes = re.split(r"Ver más(?:\s|<[^>]*>)*Contactar", html.unescape(h))[:-1]
    out = []
    for p in partes:
        ids = IDS.findall(p)
        c = limpia(p)
        m = M2.search(c)
        if not ids or not m:
            continue
        precios = [float(x.replace(".", "")) for x in PRECIO.findall(c[:m.start()])][-2:]
        if not precios:
            continue
        antes = precios[1] if len(precios) == 2 and precios[1] > precios[0] else None
        resto = c[m.end():]
        titulo = re.sub(r"^.*?Compra - (?:Agente Inmobiliario|Particular|\w+)\s*", "", resto[:200]).strip()[:90] or resto[:90]
        out.append(dict(id="trovimap-" + ids[-1], fuente="Trovimap", titulo=titulo, precio=precios[0],
                        precio_antes=antes, m2=int(m.group(1)), hab=int(m.group(2)),
                        banos=int(m.group(3)) if m.group(3) else None, zona=zona,
                        url="https://www.trovimap.com/inmueble/" + ids[-1], texto=c[:600]))
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
for z in cfg["zonas"]:
    try:
        items = leer_zona(z)
        estado[z["nombre"]] = f"{len(items)} leídos"
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
            if not riesgo:
                avisos.append(("🏠 Nuevo", a, None))
        else:
            if a["precio"] != old.get("precio"):
                old["historial"].append([hoy, a["precio"]])
                if a["precio"] < old["precio"] and not riesgo:
                    avisos.append(("📉 Bajada de precio", a, old["precio"]))
            old.update({k: a.get(k) for k in ("precio", "m2", "hab", "banos", "riesgo", "incompleto", "aviso", "url")})
            old["ultima_vez"] = hoy
    time.sleep(3)

for titulo, a, antes in avisos[:20]:
    extra = f" (antes {euros(antes)})" if antes else ""
    aviso = f"\n⚠️ {a['aviso']}" if a.get("aviso") else ""
    tg(f"{titulo}: {euros(a['precio'])}{extra} · {a['m2']} m² · {a['hab']} hab · {a['zona']}\n{a['titulo']}{aviso}\n{a['url']}")
if len(avisos) > 20:
    tg(f"…y {len(avisos) - 20} anuncios más en el panel.")

if estado != db.get("estado_previo"):
    tg("ℹ️ Estado del buscador:\n" + "\n".join(f"{k}: {v}" for k, v in estado.items()))
db["estado_previo"] = estado
db["actualizado"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
json.dump(db, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(estado, len(avisos), "avisos")
