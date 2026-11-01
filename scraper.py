import base64, datetime as dt, json, os, re
import requests

cfg = json.load(open("config.json", encoding="utf-8"))
try:
    db = json.load(open("data.json", encoding="utf-8"))
except Exception:
    db = {}
db.setdefault("anuncios", {})
db.setdefault("estado", {})
TG_TOKEN, TG_ID = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_ID")
RISK = re.compile("|".join(cfg["palabras_riesgo"]), re.I)
HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
hoy = dt.date.today().isoformat()


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


def idealista():
    key, sec = os.getenv("IDEALISTA_KEY"), os.getenv("IDEALISTA_SECRET")
    if not key or not sec:
        return [], "sin clave (opcional)"
    auth = base64.b64encode(f"{key}:{sec}".encode()).decode()
    r = requests.post("https://api.idealista.com/oauth/token", headers={"Authorization": "Basic " + auth},
                      data={"grant_type": "client_credentials", "scope": "read"}, timeout=30)
    r.raise_for_status()
    tok = r.json()["access_token"]
    out = []
    for z in cfg["zonas"]:
        r = requests.post("https://api.idealista.com/3.5/es/search", headers={"Authorization": "Bearer " + tok},
                          data={"country": "es", "operation": "sale", "propertyType": "homes",
                                "center": f"{z['lat']},{z['lon']}", "distance": cfg["radio_m"],
                                "maxPrice": cfg["precio_max"], "minSize": cfg["m2_min"], "bedrooms": "2,3,4",
                                "maxItems": 50, "numPage": 1, "language": "es",
                                "order": "publicationDate", "sort": "desc"}, timeout=30)
        r.raise_for_status()
        for e in r.json().get("elementList", []):
            out.append(dict(id="idealista-" + str(e.get("propertyCode")), fuente="Idealista",
                            titulo=f"{e.get('propertyType', 'Vivienda')} en {e.get('address', '')}",
                            precio=e.get("price"), m2=e.get("size"), hab=e.get("rooms"), banos=e.get("bathrooms"),
                            zona=e.get("municipality") or z["nombre"], url=e.get("url"),
                            lat=e.get("latitude"), lon=e.get("longitude"), texto=e.get("description", "")))
    return out, f"{len(out)} leídos"


def _walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _walk(v)


def pisos_com():
    """Experimental: lee datos estructurados (JSON-LD) de las páginas de listado."""
    out = []
    for z in cfg["zonas"]:
        if not z.get("pisos"):
            continue
        r = requests.get(f"https://www.pisos.com/venta/pisos-{z['pisos']}/", headers=HEAD, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"pisos.com respondió {r.status_code}")
        for blk in re.findall(r'<script[^>]*ld\+json[^>]*>(.*?)</script>', r.text, re.S):
            try:
                data = json.loads(blk)
            except Exception:
                continue
            for d in _walk(data):
                url = d.get("url")
                offers = d.get("offers") if isinstance(d.get("offers"), dict) else {}
                precio = offers.get("price") or d.get("price")
                if not (url and precio and "pisos.com" in str(url)):
                    continue
                size = d.get("floorSize")
                out.append(dict(id="pisos-" + re.sub(r"\W+", "", str(url))[-40:], fuente="Pisos.com",
                                titulo=d.get("name", "Vivienda"), precio=float(str(precio).replace(".", "").replace(",", ".")),
                                m2=size.get("value") if isinstance(size, dict) else size,
                                hab=d.get("numberOfRooms"), banos=d.get("numberOfBathroomsTotal"),
                                zona=z["nombre"], url=url, texto=d.get("description", "")))
    return out, f"{len(out)} leídos"


def evaluar(a):
    for campo, minimo in (("m2", cfg["m2_min"]), ("hab", cfg["hab_min"]), ("banos", cfg["banos_min"])):
        try:
            if a.get(campo) is not None and float(a[campo]) < minimo:
                return False, None
        except (TypeError, ValueError):
            a[campo] = None
    if a.get("precio") and a["precio"] > cfg["precio_max"]:
        return False, None
    m = RISK.search(f"{a.get('titulo', '')} {a.get('texto', '')}")
    return True, (m.group(0) if m else None)


estado, nuevos = {}, []
for nombre, fn in (("Idealista", idealista), ("Pisos.com", pisos_com)):
    try:
        items, msg = fn()
    except Exception as e:
        items, msg = [], f"ERROR: {e}"
    estado[nombre] = msg
    for a in items:
        ok, riesgo = evaluar(a)
        if not ok:
            continue
        a["texto"] = (a.get("texto") or "")[:300]
        a["riesgo"] = riesgo
        a["incompleto"] = any(a.get(k) is None for k in ("precio", "m2", "hab"))
        old = db["anuncios"].get(a["id"])
        if not old:
            a.update(primera_vez=hoy, ultima_vez=hoy, historial=[[hoy, a.get("precio")]])
            db["anuncios"][a["id"]] = a
            if not riesgo:
                nuevos.append(("🏠 Nuevo", a, None))
        else:
            if a.get("precio") and old.get("precio") and a["precio"] != old["precio"]:
                old["historial"].append([hoy, a["precio"]])
                if a["precio"] < old["precio"] and not riesgo:
                    nuevos.append(("📉 Bajada de precio", a, old["precio"]))
            old.update({k: a[k] for k in ("precio", "m2", "hab", "banos", "riesgo", "incompleto", "url") if k in a})
            old["ultima_vez"] = hoy

for titulo, a, antes in nuevos[:20]:
    extra = f" (antes {euros(antes)})" if antes else ""
    tg(f"{titulo}: {euros(a.get('precio'))}{extra} · {a.get('m2') or '?'} m² · {a.get('hab') or '?'} hab · {a['zona']}\n{a.get('url', '')}")
if len(nuevos) > 20:
    tg(f"…y {len(nuevos) - 20} anuncios más en el panel.")

if estado != db.get("estado_previo"):
    tg("ℹ️ Estado del buscador:\n" + "\n".join(f"{k}: {v}" for k, v in estado.items()))
db["estado_previo"] = estado
db["actualizado"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
json.dump(db, open("data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(estado, len(nuevos), "avisos")
