#!/usr/bin/env python3
"""
POS Mini Mercado - Servidor local
Puerto 5051 · Sirve index.html, REST API SQLite, y endpoints de IA
"""

import os, json, mimetypes, ssl, sqlite3, base64, io
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

try:
    from rembg import remove as rembg_remove
    from PIL import Image
    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BASE_DIR = Path(__file__).parent
DB_PATH  = BASE_DIR / "pos.db"

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
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS productos (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            emoji   TEXT    DEFAULT '📦',
            name    TEXT    NOT NULL,
            barcode TEXT    DEFAULT '',
            cat     TEXT    DEFAULT 'General',
            price   REAL    DEFAULT 0,
            stock   INTEGER DEFAULT 0,
            alert   INTEGER DEFAULT 5
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
        elif path == "/api/productos":
            conn = get_db()
            rows = conn.execute("SELECT * FROM productos ORDER BY name").fetchall()
            conn.close()
            self.send_json([row_to_dict(r) for r in rows])
        elif path == "/api/ventas":
            conn = get_db()
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
            conn.close()
            self.send_json(result)
        else:
            file_path = BASE_DIR / path.lstrip("/")
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
        elif self.path == "/remove-bg":
            self._handle_remove_bg()
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
                "INSERT INTO productos (emoji,name,barcode,cat,price,stock,alert) VALUES (?,?,?,?,?,?,?)",
                (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                 body.get("cat","General"), body.get("price",0),
                 body.get("stock",0), body.get("alert",5))
            )
            row = conn.execute("SELECT * FROM productos WHERE id=?", (c.lastrowid,)).fetchone()
            conn.commit(); conn.close()
            self.send_json(row_to_dict(row), 201)
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _update_producto(self, prod_id):
        try:
            body = self.read_json_body()
            conn = get_db()
            conn.execute(
                "UPDATE productos SET emoji=?,name=?,barcode=?,cat=?,price=?,stock=?,alert=? WHERE id=?",
                (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                 body.get("cat","General"), body.get("price",0),
                 body.get("stock",0), body.get("alert",5), prod_id)
            )
            row = conn.execute("SELECT * FROM productos WHERE id=?", (prod_id,)).fetchone()
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
            prompt = """Analiza esta imagen de un producto de mini mercado / tienda de abarrotes.
Extrae la siguiente información y responde ÚNICAMENTE con JSON válido, sin texto adicional:

{
  "nombre": "nombre completo del producto con presentación (ej: Arroz Diana 500g)",
  "categoria": "una de: Granos, Bebidas, Lácteos, Aceites, Enlatados, Dulces, Aseo, Frescos, Condimentos, Otro",
  "descripcion": "descripción breve del producto en 1 oración",
  "codigo_barras": "código de barras si es visible, si no deja vacío",
  "emoji": "un emoji que represente el producto",
  "peso_volumen": "peso o volumen del producto si aparece (ej: 500g, 1L)"
}

Si no puedes identificar algún campo con certeza, deja el valor como cadena vacía "".
Responde SOLO el JSON."""

            message = client.messages.create(
                model="claude-haiku-4-5-20251001",
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

    # ── QUITAR FONDO ───────────────────────────────────────────────
    def _handle_remove_bg(self):
        if not REMBG_AVAILABLE:
            self.send_error_json("rembg no instalado. Corre: pip3 install rembg Pillow"); return
        try:
            body   = self.read_json_body()
            b64    = body.get("image", "")
            if not b64:
                self.send_error_json("Falta el campo 'image'"); return
            # Decodificar imagen
            img_bytes = base64.b64decode(b64)
            # Quitar fondo
            out_bytes = rembg_remove(img_bytes)
            # Redimensionar a 256×256 con fondo transparente (PNG)
            img = Image.open(io.BytesIO(out_bytes)).convert("RGBA")
            img.thumbnail((256, 256), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            result_b64 = base64.b64encode(buf.getvalue()).decode()
            self.send_json({"ok": True, "image": result_b64, "mediaType": "image/png"})
        except Exception as e:
            self.send_error_json(str(e), 500)


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
  ║  Base de datos: SQLite ({DB_PATH.name}){'  ' if len(DB_PATH.name)<8 else ''}║
  ║  IA: {'✅ Configurada' if ANTHROPIC_API_KEY else '⚠  Falta ANTHROPIC_API_KEY'}{'                 ' if ANTHROPIC_API_KEY else '        '}║
  ╚══════════════════════════════════════════╝
""")
    server = HTTPServer(("0.0.0.0", port), Handler)
    if https:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Servidor detenido.")
