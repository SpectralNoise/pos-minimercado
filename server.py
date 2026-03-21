#!/usr/bin/env python3
"""
POS Mini Mercado - Servidor local
Puerto 5051 · Sirve index.html, REST API SQLite, y endpoints de IA
"""

import os, json, mimetypes, ssl, sqlite3, base64, io, urllib.request, urllib.error, socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# Cargar .env si existe
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

try:
    import anthropic
    AI_AVAILABLE = True
except ImportError:
    AI_AVAILABLE = False
    print("⚠  anthropic no instalado. Corre: pip3 install anthropic")

from PIL import Image

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TURSO_URL         = os.environ.get("TURSO_URL", "")
TURSO_TOKEN       = os.environ.get("TURSO_TOKEN", "")
BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "pos.db"  # solo se usa en local

# ── Turso (SQLite en la nube) ───────────────────────────────────────
try:
    import libsql_experimental as _libsql
    _LIBSQL_OK = True
except Exception:
    _LIBSQL_OK = False

USE_TURSO = _LIBSQL_OK and bool(TURSO_URL) and bool(TURSO_TOKEN)

# Advertir si las vars están pero la librería no cargó
if (TURSO_URL or TURSO_TOKEN) and not _LIBSQL_OK:
    print("⚠  TURSO_URL/TOKEN están configuradas pero libsql_experimental no cargó — usando SQLite local")
if not TURSO_URL and not TURSO_TOKEN:
    print("ℹ  Sin TURSO_URL/TOKEN — usando SQLite local (datos se pierden en cada deploy de Railway)")

class _DCursor:
    """Cursor wrapper que devuelve dicts en lugar de tuplas."""
    def __init__(self, cur):
        self._c = cur
        self.lastrowid = getattr(cur, 'lastrowid', None)

    def _cols(self):
        d = getattr(self._c, 'description', None)
        return [x[0] for x in d] if d else []

    def _row(self, row):
        if row is None: return None
        cols = self._cols()
        if not cols: return row
        # _Row soporta row["col"] y row[0] para compatibilidad con sqlite3.Row
        class _Row(dict):
            def __getitem__(self, key):
                if isinstance(key, int): return list(self.values())[key]
                return super().__getitem__(key)
        return _Row({cols[i]: row[i] for i in range(len(cols))})

    def execute(self, sql, params=()):
        self._c.execute(sql, params)
        self.lastrowid = getattr(self._c, 'lastrowid', None)
        return self

    def fetchone(self):  return self._row(self._c.fetchone())
    def fetchall(self):  return [self._row(r) for r in self._c.fetchall()]
    def __iter__(self):  return (self._row(r) for r in self._c.fetchall())

class _TursoConn:
    """Conexión Turso compatible con la API de sqlite3 que usa el servidor."""
    def __init__(self):
        url = TURSO_URL.replace('libsql://', '')
        self._conn = _libsql.connect(url, auth_token=TURSO_TOKEN)

    def cursor(self):
        return _DCursor(self._conn.cursor())

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params if params else ())
        dc = _DCursor(cur)
        return dc

    def executemany(self, sql, seq):
        self._conn.executemany(sql, seq)

    def executescript(self, script):
        for stmt in [s.strip() for s in script.split(';') if s.strip()]:
            self._conn.execute(stmt)
        self._conn.commit()

    def commit(self):  self._conn.commit()
    def close(self):   self._conn.close()

PRODUCTOS_INICIALES = [
    ('🍚', 'Arroz 500g',         '7702001001001', 'Granos',      2100,  48, 10),
    ('🛢️', 'Aceite 1L',           '7702001001002', 'Aceites',     8500,   5,  8),
    ('🍬', 'Azúcar 500g',         '7702001001003', 'Granos',      1800,  32, 10),
    ('🥛', 'Leche entera 1L',     '7702001001004', 'Lácteos',     3200,  18,  6),
    ('🧂', 'Sal 500g',            '7702001001005', 'Condimentos',  900,  25,  8),
    ('🫒', 'Aceite girasol 3L',   '7702001001006', 'Aceites',    22000,   7,  4),
    ('🥫', 'Atún en lata',        '7702001001007', 'Enlatados',   4200,   2,  5),
    ('🍅', 'Salsa de tomate',     '7702001001008', 'Enlatados',   3500,  14,  6),
    ('🧃', 'Jugo Hit 1L',         '7702001001009', 'Bebidas',     3800,  22,  8),
    ('🥤', 'Gaseosa 1.5L',        '7702001001010', 'Bebidas',     4500,  35, 10),
    ('💧', 'Agua 600ml',          '7702001001011', 'Bebidas',     1500,  60, 12),
    ('🍫', 'Chocolate Jet',       '7702001001012', 'Dulces',      2800,   0,  5),
    ('🍪', 'Galletas Ducales',    '7702001001013', 'Dulces',      2200,  18,  6),
    ('🧻', 'Papel higiénico x4',  '7702001001014', 'Aseo',        8900,  12,  4),
    ('🧼', 'Jabón Rey x3',        '7702001001015', 'Aseo',        4200,   3,  5),
    ('🥚', 'Huevos x12',          '7702001001016', 'Frescos',    14000,   8,  4),
    ('🧈', 'Margarina 500g',      '7702001001017', 'Lácteos',     5600,   9,  4),
    ('☕', 'Café Colcafé 200g',   '7702001001018', 'Bebidas',    12500,   6,  4),
    ('🍝', 'Espaguetis 500g',     '7702001001019', 'Granos',      2600,  20,  8),
    ('🥣', 'Avena Quaker 400g',   '7702001001020', 'Granos',      4800,  11,  5),
]


# ─── BASE DE DATOS ─────────────────────────────────────────────────

def get_db():
    if USE_TURSO:
        return _TursoConn()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS productos (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            emoji     TEXT    DEFAULT '📦',
            name      TEXT    NOT NULL,
            barcode   TEXT    DEFAULT '',
            cat       TEXT    DEFAULT 'General',
            price     REAL    DEFAULT 0,
            stock     INTEGER DEFAULT 0,
            alert     INTEGER DEFAULT 5,
            thumbnail TEXT    DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS ventas (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            total      REAL    NOT NULL,
            metodo     TEXT    DEFAULT 'efectivo',
            recibido   REAL    DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS items_venta (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            venta_id    INTEGER REFERENCES ventas(id),
            producto_id INTEGER,
            name        TEXT,
            emoji       TEXT,
            qty         INTEGER,
            price       REAL
        );
    """)
    # Migración: añadir thumbnail si no existe
    try:
        conn.execute("ALTER TABLE productos ADD COLUMN thumbnail TEXT DEFAULT NULL")
        conn.commit()
    except (sqlite3.OperationalError, ValueError) as e:
        if "duplicate column name" not in str(e):
            raise
    if conn.execute("SELECT COUNT(*) FROM productos").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO productos (emoji,name,barcode,cat,price,stock,alert) VALUES (?,?,?,?,?,?,?)",
            PRODUCTOS_INICIALES
        )
    conn.commit()
    conn.close()

def row_to_dict(row):
    return dict(row)


# ─── SERVIDOR HTTP ─────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  {self.command} {self.path} → {args[1] if len(args)>1 else ''}")

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, status=400):
        self.send_json({"error": msg}, status)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        data = b""
        while len(data) < length:
            chunk = self.rfile.read(length - len(data))
            if not chunk:
                break
            data += chunk
        return json.loads(data)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._serve_file(BASE_DIR / "index.html", "text/html")
        elif path == "/api/status":
            self._json({"db": "turso" if USE_TURSO else "sqlite", "turso_url": bool(TURSO_URL), "libsql_ok": _LIBSQL_OK})
        elif path == "/api/productos":
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT id,emoji,name,barcode,cat,price,stock,alert,"
                    "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                    "FROM productos ORDER BY name"
                ).fetchall()
                self.send_json([row_to_dict(r) for r in rows])
            finally:
                conn.close()
        elif path.startswith("/api/productos/") and path.endswith("/thumbnail"):
            parts = path.split("/")
            if len(parts) == 5:
                prod_id = parts[3]
                conn = get_db()
                try:
                    row = conn.execute(
                        "SELECT thumbnail FROM productos WHERE id=?", (prod_id,)
                    ).fetchone()
                finally:
                    conn.close()
                if row and row["thumbnail"]:
                    thumb = row["thumbnail"]
                    # thumbnail almacenado como "data:image/jpeg;base64,..."
                    if thumb.startswith("data:"):
                        header, b64data = thumb.split(",", 1)
                        mime = header.split(":")[1].split(";")[0]
                        img_bytes = base64.b64decode(b64data)
                    else:
                        mime = "image/jpeg"
                        img_bytes = base64.b64decode(thumb)
                    self.send_response(200)
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", len(img_bytes))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.end_headers()
                    self.wfile.write(img_bytes)
                else:
                    self.send_response(404); self.end_headers()
            else:
                self.send_response(404); self.end_headers()
        elif path.startswith("/api/lookup-barcode/"):
            barcode = path.split("/api/lookup-barcode/")[1]
            self._lookup_barcode(barcode)
        elif path == "/api/ventas":
            conn = get_db()
            try:
                ventas = conn.execute(
                    "SELECT * FROM ventas ORDER BY created_at DESC LIMIT 500"
                ).fetchall()
                result = []
                for v in ventas:
                    vd = row_to_dict(v)
                    items = conn.execute(
                        "SELECT * FROM items_venta WHERE venta_id=?", (v["id"],)
                    ).fetchall()
                    vd["items"] = [row_to_dict(i) for i in items]
                    result.append(vd)
                self.send_json(result)
            finally:
                conn.close()
        else:
            file_path = (BASE_DIR / path.lstrip("/")).resolve()
            # Bloquear path traversal y archivos sensibles
            if not str(file_path).startswith(str(BASE_DIR.resolve())):
                self.send_response(403); self.end_headers(); return
            BLOCKED = {"pos.db", ".env", "key.pem", "cert.pem", "rootCA.pem"}
            if file_path.name in BLOCKED:
                self.send_response(403); self.end_headers(); return
            if file_path.exists() and file_path.is_file():
                mime, _ = mimetypes.guess_type(str(file_path))
                self._serve_file(file_path, mime or "application/octet-stream")
            else:
                self.send_response(404)
                self.end_headers()

    def _serve_file(self, path, content_type):
        try:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(500)
            self.end_headers()

    # ── POST ───────────────────────────────────────────────────────
    def do_POST(self):
        if self.path == "/api/productos":
            self._create_producto()
        elif self.path == "/api/ventas":
            self._create_venta()
        elif self.path == "/analyze-product":
            self._handle_analyze_product()
        elif self.path == "/api/ai-cierre":
            self._handle_ai_cierre()
        elif self.path == "/api/ai-pedido":
            self._handle_ai_pedido()
        else:
            self.send_error_json("Ruta no encontrada", 404)

    # ── PUT ────────────────────────────────────────────────────────
    def do_PUT(self):
        parts = self.path.split("/")
        if len(parts) == 4 and parts[2] == "productos":
            self._update_producto(parts[3])
        else:
            self.send_error_json("Ruta no encontrada", 404)

    # ── DELETE ─────────────────────────────────────────────────────
    def do_DELETE(self):
        parts = self.path.split("/")
        if len(parts) == 4 and parts[2] == "productos":
            self._delete_producto(parts[3])
        else:
            self.send_error_json("Ruta no encontrada", 404)

    # ── CRUD PRODUCTOS ─────────────────────────────────────────────
    def _create_producto(self):
        try:
            body = self.read_json_body()
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "INSERT INTO productos (emoji,name,barcode,cat,price,stock,alert,thumbnail) VALUES (?,?,?,?,?,?,?,?)",
                (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                 body.get("cat","General"), body.get("price",0),
                 body.get("stock",0), body.get("alert",5), body.get("thumbnail"))
            )
            row = conn.execute(
                "SELECT id,emoji,name,barcode,cat,price,stock,alert,"
                "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                "FROM productos WHERE id=?", (c.lastrowid,)
            ).fetchone()
            conn.commit(); conn.close()
            self.send_json(row_to_dict(row), 201)
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _update_producto(self, prod_id):
        try:
            body = self.read_json_body()
            conn = get_db()
            if "thumbnail" in body:
                conn.execute(
                    "UPDATE productos SET emoji=?,name=?,barcode=?,cat=?,price=?,stock=?,alert=?,thumbnail=? WHERE id=?",
                    (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                     body.get("cat","General"), body.get("price",0),
                     body.get("stock",0), body.get("alert",5), body.get("thumbnail"), prod_id)
                )
            else:
                conn.execute(
                    "UPDATE productos SET emoji=?,name=?,barcode=?,cat=?,price=?,stock=?,alert=? WHERE id=?",
                    (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                     body.get("cat","General"), body.get("price",0),
                     body.get("stock",0), body.get("alert",5), prod_id)
                )
            # Devolver has_thumbnail en vez del blob
            row = conn.execute(
                "SELECT id,emoji,name,barcode,cat,price,stock,alert,"
                "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                "FROM productos WHERE id=?", (prod_id,)
            ).fetchone()
            conn.commit(); conn.close()
            self.send_json(row_to_dict(row) if row else {}, 200 if row else 404)
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _delete_producto(self, prod_id):
        try:
            conn = get_db()
            conn.execute("DELETE FROM productos WHERE id=?", (prod_id,))
            conn.commit(); conn.close()
            self.send_json({"ok": True})
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── VENTAS ─────────────────────────────────────────────────────
    def _create_venta(self):
        try:
            body = self.read_json_body()
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "INSERT INTO ventas (total, metodo, recibido) VALUES (?,?,?)",
                (body["total"], body.get("metodo","efectivo"), body.get("recibido", body["total"]))
            )
            venta_id = c.lastrowid
            for item in body.get("items", []):
                c.execute(
                    "INSERT INTO items_venta (venta_id,producto_id,name,emoji,qty,price) VALUES (?,?,?,?,?,?)",
                    (venta_id, item.get("id"), item.get("name",""), item.get("emoji",""),
                     item.get("qty",1), item.get("price",0))
                )
                # Descontar stock
                c.execute(
                    "UPDATE productos SET stock = MAX(0, stock - ?) WHERE id=?",
                    (item.get("qty",1), item.get("id"))
                )
            row = conn.execute("SELECT * FROM ventas WHERE id=?", (venta_id,)).fetchone()
            conn.commit(); conn.close()
            self.send_json({"ok": True, "venta": row_to_dict(row)}, 201)
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── ANÁLISIS IA ────────────────────────────────────────────────
    def _handle_analyze_product(self):
        if not AI_AVAILABLE:
            self.send_error_json("SDK de Anthropic no instalado"); return
        if not ANTHROPIC_API_KEY:
            self.send_error_json("ANTHROPIC_API_KEY no configurada"); return
        try:
            body = self.read_json_body()
            image_b64  = body.get("image")
            media_type = body.get("mediaType", "image/jpeg")
            if not image_b64:
                self.send_error_json("Falta el campo 'image'"); return

            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            prompt = """Eres un lector experto de empaques de productos de supermercado colombiano.

TAREA PRINCIPAL: Lee con precisión el texto impreso en el empaque de la imagen.

Reglas estrictas:
1. El "nombre" debe ser EXACTAMENTE el texto del empaque: marca + producto + variedad + gramaje/volumen.
   Ejemplos correctos: "Arroz Diana Extra 500g", "Leche Entera Alquería 1L", "Coca-Cola 1.5L"
   Ejemplos INCORRECTOS: "arroz", "leche", "gaseosa"
2. Si el texto es difícil de leer, intenta de todas formas — transcribe lo que puedas ver.
3. El código de barras: si ves números debajo de las barras, transcríbelos exactamente.

Responde ÚNICAMENTE con este JSON válido, sin texto adicional:
{
  "nombre": "texto exacto del empaque: marca + producto + gramaje",
  "categoria": "una de: Granos, Bebidas, Lácteos, Aceites, Enlatados, Dulces, Aseo, Frescos, Condimentos, Otro",
  "descripcion": "descripción breve en 1 oración",
  "codigo_barras": "números del código de barras si son visibles, si no vacío",
  "emoji": "emoji que represente el producto"
}"""

            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": prompt}
                ]}],
            )
            raw = message.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
            self.send_json({"ok": True, "product": json.loads(raw.strip())})
        except json.JSONDecodeError as e:
            self.send_error_json(f"Respuesta inesperada de la IA: {e}")
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── RESUMEN IA DE CIERRE DE CAJA ───────────────────────────────
    def _handle_ai_cierre(self):
        if not AI_AVAILABLE or not ANTHROPIC_API_KEY:
            self.send_error_json("IA no configurada"); return
        try:
            body = self.read_json_body()
            cajero   = body.get("cajero", "el cajero")
            duracion = body.get("duracion_min", 0)
            total    = body.get("total", 0)
            efectivo = body.get("efectivo", 0)
            transf   = body.get("transf", 0)
            tx       = body.get("tx_count", 0)
            top      = body.get("top_productos", [])

            horas = duracion // 60
            mins  = duracion % 60
            dur_txt = f"{horas}h {mins}min" if horas else f"{mins} minutos"
            top_txt = ", ".join([f"{p['name']} ({p['qty']} uds)" for p in top[:5]]) or "sin datos"

            prompt = f"""Eres el asistente del sistema POS de un mini mercado colombiano.
Genera un resumen breve y amigable del turno en máximo 3 oraciones, en español colombiano informal.
Datos del turno:
- Cajero: {cajero}
- Duración: {dur_txt}
- Transacciones: {tx}
- Total vendido: ${int(total):,} COP
- Efectivo: ${int(efectivo):,} | Transferencias: ${int(transf):,}
- Productos más vendidos: {top_txt}
Sé positivo, menciona el nombre del cajero, y destaca algo interesante de los números."""

            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            self.send_json({"ok": True, "resumen": msg.content[0].text.strip()})
        except Exception as e:
            self.send_error_json(str(e), 500)


    # ── PEDIDO INTELIGENTE ─────────────────────────────────────────
    def _handle_ai_pedido(self):
        if not AI_AVAILABLE or not ANTHROPIC_API_KEY:
            self.send_error_json("IA no configurada"); return
        try:
            body     = self.read_json_body()
            criticos = body.get("criticos", [])   # stock < 3 días
            bajos    = body.get("bajos", [])       # 3-7 días
            sin_rot  = body.get("sin_rotacion", [])

            def fmt_prod(p):
                dias = f"{p['dias']} días" if p.get('dias') else "sin ventas"
                return f"- {p['name']}: stock {p['stock']} uds, velocidad {dias}"

            crit_txt = "\n".join([fmt_prod(p) for p in criticos]) or "ninguno"
            bajo_txt = "\n".join([fmt_prod(p) for p in bajos])     or "ninguno"
            rot_txt  = ", ".join([p['name'] for p in sin_rot[:5]]) or "ninguno"

            prompt = f"""Eres el asistente de compras de un mini mercado colombiano.
Genera una lista de pedido concreta y priorizada en español informal.
Máximo 8 líneas. Sé directo y práctico.

CRÍTICOS (se acaban en menos de 3 días):
{crit_txt}

STOCK BAJO (3-7 días):
{bajo_txt}

Sin rotación (más de 15 días sin venderse):
{rot_txt}

Para cada producto crítico sugiere una cantidad a pedir (aprox 2 semanas de stock).
Al final, un consejo breve sobre los productos sin rotación."""

            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            self.send_json({"ok": True, "pedido": msg.content[0].text.strip()})
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── LOOKUP BARCODE (Open Food Facts) ───────────────────────────
    def _lookup_barcode(self, barcode):
        OFF_CATS = {
            'en:beverages':'Bebidas','en:drinks':'Bebidas','en:sodas':'Bebidas',
            'en:waters':'Bebidas','en:juices':'Bebidas','en:coffees':'Bebidas',
            'en:dairy':'Lácteos','en:milks':'Lácteos','en:cheeses':'Lácteos',
            'en:yogurts':'Lácteos',
            'en:cereals-and-their-products':'Granos','en:rice':'Granos',
            'en:legumes':'Granos','en:pastas':'Granos','en:flours':'Granos',
            'en:oatmeals':'Granos',
            'en:oils-and-fats':'Aceites','en:cooking-oils':'Aceites',
            'en:canned-foods':'Enlatados','en:canned-fish':'Enlatados',
            'en:canned-vegetables':'Enlatados',
            'en:snacks':'Dulces','en:chocolates':'Dulces',
            'en:candies':'Dulces','en:biscuits':'Dulces','en:cookies':'Dulces',
            'en:cleaning-products':'Aseo','en:hygiene':'Aseo',
            'en:fresh-foods':'Frescos','en:eggs':'Frescos','en:meats':'Frescos',
            'en:condiments':'Condimentos','en:sauces':'Condimentos',
            'en:spices':'Condimentos','en:salts':'Condimentos',
        }
        CAT_EMOJIS = {
            'Bebidas':'🥤','Lácteos':'🥛','Granos':'🌾','Aceites':'🛢️',
            'Enlatados':'🥫','Dulces':'🍬','Aseo':'🧼','Frescos':'🥚',
            'Condimentos':'🧂','Otro':'📦',
        }
        try:
            url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}?fields=product_name,product_name_es,categories_tags,image_front_small_url,brands,quantity"
            req = urllib.request.Request(url, headers={"User-Agent": "POS-MiniMercado/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read())

            if data.get("status") != 1:
                self.send_json({"found": False}); return

            p = data["product"]
            name = (p.get("product_name_es") or p.get("product_name") or "").strip()
            brand = p.get("brands","").split(",")[0].strip()
            qty   = p.get("quantity","").strip()
            if brand and brand.lower() not in name.lower():
                name = f"{name} {brand}".strip() if name else brand
            if qty and qty not in name:
                name = f"{name} {qty}".strip()

            # Categoría
            tags = p.get("categories_tags", [])
            cat  = "Otro"
            for tag in tags:
                if tag in OFF_CATS:
                    cat = OFF_CATS[tag]; break

            emoji = CAT_EMOJIS.get(cat, "📦")

            # Miniatura
            thumbnail = None
            img_url = p.get("image_front_small_url","")
            if img_url:
                try:
                    with urllib.request.urlopen(img_url, timeout=5) as ir:
                        img_bytes = ir.read()
                    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                    img.thumbnail((128, 128), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    thumbnail = f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
                except Exception:
                    pass

            # Mercado Libre: precio + imagen real
            meli = self._meli_lookup(name or barcode)
            precio_sugerido = {k: meli[k] for k in ("min","max","medio","n")} if meli else None
            # Preferir imagen de MELI (foto real) sobre la de Open Food Facts
            if meli and meli.get("thumbnail"):
                thumbnail = meli["thumbnail"]

            self.send_json({
                "found": True,
                "product": {
                    "name":             name or "Producto sin nombre",
                    "cat":              cat,
                    "emoji":            emoji,
                    "barcode":          barcode,
                    "thumbnail":        thumbnail,
                    "precio_sugerido":  precio_sugerido,
                }
            })
        except urllib.error.URLError:
            self.send_json({"found": False, "error": "Sin conexión a Open Food Facts"})
        except Exception as e:
            self.send_json({"found": False, "error": str(e)})

    def _meli_lookup(self, query):
        """Consulta Mercado Libre Colombia: precios reales + imagen del primer resultado."""
        try:
            q = urllib.request.quote(query)
            url = f"https://api.mercadolibre.com/sites/MCO/search?q={q}&limit=10&condition=new"
            req = urllib.request.Request(url, headers={"User-Agent": "POS-MiniMercado/1.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
            items = data.get("results", [])
            if not items:
                return None

            # Precios (descarta outliers)
            precios = [i["price"] for i in items if i.get("price", 0) > 0]
            if not precios:
                return None
            precios.sort()
            corte = max(1, len(precios) // 5)
            precios = precios[corte:-corte] if len(precios) > 4 else precios

            # Imagen: usar el thumbnail del primer resultado con precio válido
            thumbnail = None
            for item in items:
                img_url = item.get("thumbnail", "")
                if img_url:
                    # MELI devuelve URLs con -I (pequeña), cambiar a -O (mayor calidad)
                    img_url = img_url.replace("-I.jpg", "-O.jpg").replace("-I.webp", "-O.jpg")
                    try:
                        with urllib.request.urlopen(img_url, timeout=4) as ir:
                            img_bytes = ir.read()
                        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                        img.thumbnail((256, 256), Image.LANCZOS)
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=88)
                        thumbnail = f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
                        break
                    except Exception:
                        continue

            return {
                "min":      round(min(precios)),
                "max":      round(max(precios)),
                "medio":    round(sum(precios) / len(precios)),
                "n":        len(items),
                "thumbnail": thumbnail,
            }
        except Exception:
            return None


# ─── INICIO ────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    # En cloud (Railway/Render) usar PORT env var; localmente 5051
    IS_CLOUD = bool(os.environ.get("PORT") or os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"))
    port = int(os.environ.get("PORT", 5051))

    # SSL solo en local (cloud maneja HTTPS en su propia capa)
    cert  = BASE_DIR / "cert.pem"
    key   = BASE_DIR / "key.pem"
    https = cert.exists() and key.exists() and not IS_CLOUD
    proto = "https" if https else "http"

    print(f"""
  ╔══════════════════════════════════════════╗
  ║   POS Mini Mercado                       ║
  ╠══════════════════════════════════════════╣
  ║  Entorno: {'☁  Cloud' if IS_CLOUD else '🏠 Local'}{'                      ' if IS_CLOUD else '                       '}║
  ║  {proto}://{'0.0.0.0' if IS_CLOUD else 'localhost'}:{port}{'           ' if IS_CLOUD else '            '}║
  ║                                          ║
  ║  Base de datos: {'Turso ☁' if USE_TURSO else f'SQLite ({DB_PATH.name})'}{'       ' if USE_TURSO else ('  ' if len(DB_PATH.name)<8 else '')}║
  ║  IA: {'✅ Configurada' if ANTHROPIC_API_KEY else '⚠  Falta ANTHROPIC_API_KEY'}{'                 ' if ANTHROPIC_API_KEY else '        '}║
  ╚══════════════════════════════════════════╝
""")
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if https:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Servidor detenido.")
