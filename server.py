#!/usr/bin/env python3
"""
POS Mini Mercado - Servidor local
Puerto 5051 · Sirve index.html, REST API SQLite, y endpoints de IA
"""

import os, json, mimetypes, ssl, sqlite3, base64, io, urllib.request, urllib.error, urllib.parse, socketserver, struct
import hmac as _hmac, hashlib, time, secrets  # secrets usado en init_db (seed) y _create_usuario
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from collections import namedtuple

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
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TURSO_URL         = os.environ.get("TURSO_URL", "")
TURSO_TOKEN       = os.environ.get("TURSO_TOKEN", "")

SECRET_KEY           = os.environ.get("SECRET_KEY", "")
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.environ.get("GOOGLE_REDIRECT_URI", "")

_WEAK_DEFAULT_KEY = "dev-secret-key-cambia-en-produccion"
if not SECRET_KEY:
    raise RuntimeError(
        "[AESSPOS] SECRET_KEY no configurada.\n"
        "  En Railway: Variables → agregar SECRET_KEY con un valor aleatorio largo.\n"
        "  En local: agregar SECRET_KEY=<valor> en el archivo .env"
    )
_IS_CLOUD = bool(os.environ.get("PORT") or os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"))
if SECRET_KEY == _WEAK_DEFAULT_KEY:
    if _IS_CLOUD:
        raise RuntimeError(
            "[AESSPOS] SECRET_KEY usa el valor por defecto inseguro.\n"
            "  Genera una clave: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "  Y agrégala en Railway/Render → Variables de entorno."
        )
    else:
        print("  [AESSPOS] ⚠  SECRET_KEY usa el valor por defecto de .env — cámbiala antes de pasar a producción.")

AuthContext = namedtuple("AuthContext", ["user_id", "tienda_id", "rol"])

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
        self._conn = _libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

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

# ── TOTP (RFC 6238 — stdlib only) ──────────────────────────────────
def _totp_generate_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")

def _totp_code(secret_b32, t=None):
    if t is None:
        t = int(time.time()) // 30
    secret_b32 = secret_b32.upper()
    padding = (8 - len(secret_b32) % 8) % 8
    key = base64.b32decode(secret_b32 + "=" * padding)
    msg = struct.pack(">Q", t)
    h = _hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0xf
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7fffffff
    return str(code % 1_000_000).zfill(6)

def _totp_verify(secret_b32, code, window=1):
    t = int(time.time()) // 30
    code = str(code).strip().zfill(6)
    for i in range(-window, window + 1):
        if _hmac.compare_digest(_totp_code(secret_b32, t + i), code):
            return True
    return False

def _generate_recovery_codes():
    codes = []
    for _ in range(10):
        codes.append("-".join(secrets.token_hex(3).upper() for _ in range(3)))
    return codes

def _hash_recovery_code(code):
    return hashlib.sha256(code.strip().upper().replace("-", "").encode()).hexdigest()

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 260000).hex()


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
        CREATE TABLE IF NOT EXISTS turnos (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            caja_id          TEXT     DEFAULT 'caja-1',
            cajero           TEXT     NOT NULL,
            estado           TEXT     DEFAULT 'abierto',
            monto_inicial    REAL     DEFAULT 0,
            apertura_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            cierre_at        DATETIME DEFAULT NULL,
            efectivo_ventas  REAL     DEFAULT 0,
            transferencias   REAL     DEFAULT 0,
            total_ventas     REAL     DEFAULT 0,
            monto_contado    REAL     DEFAULT NULL,
            diferencia       REAL     DEFAULT NULL,
            num_tx           INTEGER  DEFAULT 0,
            resumen_ia       TEXT     DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS tiendas (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre     TEXT    NOT NULL,
            slug       TEXT    NOT NULL UNIQUE,
            plan       TEXT    DEFAULT 'basico',
            activo     INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS usuarios (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tienda_id     INTEGER REFERENCES tiendas(id) ON DELETE RESTRICT,
            nombre        TEXT    NOT NULL,
            username      TEXT    NOT NULL,
            password_hash TEXT    NOT NULL,
            salt          TEXT    NOT NULL,
            rol           TEXT    NOT NULL DEFAULT 'cajero'
                          CHECK (rol IN ('superadmin','admin','cajero')),
            activo        INTEGER DEFAULT 1,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tienda_id, username)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_usuarios_superadmin_username
            ON usuarios(username) WHERE tienda_id IS NULL;
    """)
    # Migración: añadir thumbnail si no existe
    try:
        conn.execute("ALTER TABLE productos ADD COLUMN thumbnail TEXT DEFAULT NULL")
        conn.commit()
    except (sqlite3.OperationalError, ValueError) as e:
        if "duplicate column name" not in str(e):
            raise
    # Migración: añadir turno_id en ventas si no existe
    try:
        conn.execute("ALTER TABLE ventas ADD COLUMN turno_id INTEGER REFERENCES turnos(id)")
        conn.commit()
    except (sqlite3.OperationalError, ValueError) as e:
        if "duplicate column name" not in str(e):
            raise
    # Migración: añadir tienda_id a productos, ventas, turnos
    for table in ('productos', 'ventas', 'turnos'):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tienda_id INTEGER REFERENCES tiendas(id)")
            conn.commit()
        except (sqlite3.OperationalError, ValueError) as e:
            if "duplicate column name" not in str(e):
                raise
    # Migración: tipo de negocio en tiendas
    try:
        conn.execute("ALTER TABLE tiendas ADD COLUMN tipo TEXT DEFAULT NULL")
        conn.commit()
    except (sqlite3.OperationalError, ValueError) as e:
        if "duplicate column name" not in str(e):
            raise
    # Migración: precio de compra en productos (para margen de ganancia)
    try:
        conn.execute("ALTER TABLE productos ADD COLUMN precio_compra REAL DEFAULT NULL")
        conn.commit()
    except (sqlite3.OperationalError, ValueError) as e:
        if "duplicate column name" not in str(e):
            raise
    # Migración: reasignar productos/ventas sin tienda_id a la primera tienda (datos pre-multitienda)
    primera = conn.execute("SELECT id FROM tiendas ORDER BY id LIMIT 1").fetchone()
    if primera:
        for table in ('productos', 'ventas'):
            conn.execute(f"UPDATE {table} SET tienda_id=? WHERE tienda_id IS NULL", (primera["id"],))
        conn.commit()
    # Migraciones 2FA / Google OAuth
    for col, definition in [
        ('google_email',  'TEXT DEFAULT NULL'),
        ('totp_secret',   'TEXT DEFAULT NULL'),
        ('totp_enabled',  'INTEGER DEFAULT 0'),
    ]:
        try:
            conn.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {definition}")
            conn.commit()
        except (sqlite3.OperationalError, ValueError) as e:
            if "duplicate column name" not in str(e):
                raise
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recovery_codes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
            code_hash  TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    # Migración: anulación de ventas
    for col, definition in [
        ('status',            "TEXT DEFAULT 'completada'"),
        ('anulada_at',        'DATETIME DEFAULT NULL'),
        ('motivo_anulacion',  'TEXT DEFAULT NULL'),
    ]:
        try:
            conn.execute(f"ALTER TABLE ventas ADD COLUMN {col} {definition}")
            conn.commit()
        except (sqlite3.OperationalError, ValueError) as e:
            if "duplicate column name" not in str(e):
                raise
    # Migración: ajuste de stock (entradas de inventario)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ajustes_stock (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL REFERENCES productos(id),
            tienda_id   INTEGER REFERENCES tiendas(id),
            delta       INTEGER NOT NULL,
            motivo      TEXT,
            usuario     TEXT,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    # Seed superadmin (solo si no hay usuarios)
    if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        _pw = secrets.token_urlsafe(12)
        _salt = secrets.token_hex(16)
        _hash = _hash_password(_pw, _salt)
        conn.execute(
            "INSERT INTO usuarios (tienda_id, nombre, username, password_hash, salt, rol) VALUES (?,?,?,?,?,?)",
            (None, "Super Admin", "admin", _hash, _salt, "superadmin")
        )
        conn.commit()
        print(f"\n  [AESSPOS] ✅ Superadmin creado")
        print(f"  [AESSPOS]    Usuario:    admin")
        print(f"  [AESSPOS]    Contraseña: {_pw}")
        print(f"  [AESSPOS]    ⚠  Guarda esta contraseña — no se volverá a mostrar.\n")
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

    def _cors_origin(self):
        """Permite solo orígenes del mismo host. Nunca devuelve '*'."""
        origin = self.headers.get("Origin", "")
        if not origin:
            return None  # misma origen, no se necesita header CORS
        host = self.headers.get("Host", "")
        if origin in (f"http://{host}", f"https://{host}"):
            return origin
        return None  # origen externo desconocido → no agregar header

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        cors = self._cors_origin()
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, msg, status=400):
        if status >= 500:
            print(f"  [ERROR {status}] {msg}")
            self.send_json({"error": "Error interno del servidor"}, status)
        else:
            self.send_json({"error": msg}, status)

    def require_auth(self):
        """Valida el token Bearer. Retorna AuthContext o envía 401 y retorna None."""
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            self.send_error_json("No autenticado", 401); return None
        token = header[7:]
        try:
            payload_b64, signature = token.rsplit(".", 1)
            payload = base64.b64decode(payload_b64).decode()
            expected = _hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not _hmac.compare_digest(expected, signature):
                self.send_error_json("Token inválido", 401); return None
            user_id_s, tienda_str, rol, exp_s = payload.split(":", 3)
            if int(exp_s) < int(time.time()):
                self.send_error_json("Sesión expirada", 401); return None
            tienda_id = None if tienda_str == "null" else int(tienda_str)
            conn = get_db()
            try:
                u = conn.execute("SELECT activo FROM usuarios WHERE id=?", (int(user_id_s),)).fetchone()
                if not u or not u["activo"]:
                    self.send_error_json("Usuario inactivo", 401); return None
                if tienda_id is not None:
                    t = conn.execute("SELECT activo FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
                    if not t or not t["activo"]:
                        self.send_error_json("Tienda inactiva", 401); return None
            finally:
                conn.close()
            return AuthContext(int(user_id_s), tienda_id, rol)
        except Exception:
            self.send_error_json("Token inválido", 401); return None

    def _tienda_is_pro(self, tienda_id):
        """Consulta la DB y retorna True si la tienda tiene plan 'pro'."""
        if tienda_id is None:
            return False
        conn = get_db()
        try:
            t = conn.execute("SELECT plan FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
            return bool(t and t["plan"] == "pro")
        finally:
            conn.close()

    def require_pro(self, ctx):
        """Verifica que la tienda tenga plan 'pro'. Superadmin siempre pasa.
        Retorna True si tiene acceso, False si envió error 403."""
        if ctx.rol == 'superadmin':
            return True
        if not self._tienda_is_pro(ctx.tienda_id):
            self.send_error_json("Esta función requiere el plan Pro", 403); return False
        return True

    def _make_partial_token(self, user_id):
        """Token de 5 min para el paso de 2FA."""
        exp = int(time.time()) + 300
        payload = f"partial:{user_id}:{exp}"
        sig = base64.urlsafe_b64encode(
            _hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest()
        ).decode().rstrip("=")
        return base64.urlsafe_b64encode(f"{payload}.{sig}".encode()).decode()

    def _verify_partial_token(self, token):
        try:
            decoded = base64.urlsafe_b64decode(token + "==").decode()
            payload, sig = decoded.rsplit(".", 1)
            expected = base64.urlsafe_b64encode(
                _hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest()
            ).decode().rstrip("=")
            if not _hmac.compare_digest(sig, expected):
                return None
            prefix, user_id_s, exp_s = payload.split(":", 2)
            if prefix != "partial" or int(exp_s) < int(time.time()):
                return None
            return int(user_id_s)
        except Exception:
            return None

    def _get_redirect_uri(self):
        if GOOGLE_REDIRECT_URI:
            return GOOGLE_REDIRECT_URI
        host = self.headers.get("Host", "localhost:5051")
        scheme = "https" if any(x in host for x in ("railway", "aesspos")) else "http"
        return f"{scheme}://{host}/api/auth/google/callback"

    def _redirect_login_error(self, msg):
        self.send_response(302)
        self.send_header("Location", f"/?auth_error={urllib.parse.quote(msg)}")
        self.end_headers()

    def _auth_google_redirect(self):
        if not GOOGLE_CLIENT_ID:
            self.send_error_json("Google OAuth no configurado", 501); return
        params = urllib.parse.urlencode({
            "client_id":     GOOGLE_CLIENT_ID,
            "redirect_uri":  self._get_redirect_uri(),
            "response_type": "code",
            "scope":         "openid email profile",
            "state":         base64.urlsafe_b64encode(secrets.token_bytes(16)).decode().rstrip("="),
        })
        self.send_response(302)
        self.send_header("Location", f"https://accounts.google.com/o/oauth2/v2/auth?{params}")
        self.end_headers()

    def _auth_google_callback(self):
        if not GOOGLE_CLIENT_ID:
            self._redirect_login_error("Google OAuth no configurado"); return
        try:
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            code = qs.get("code", [""])[0]
            if not code:
                self._redirect_login_error("Google: código de autorización no recibido"); return
            # Intercambiar código por tokens
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=urllib.parse.urlencode({
                    "code": code, "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": self._get_redirect_uri(),
                    "grant_type": "authorization_code",
                }).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                token_resp = json.loads(r.read())
            # Decodificar id_token (JWT) para obtener el email
            id_token = token_resp.get("id_token", "")
            parts = id_token.split(".")
            if len(parts) < 2:
                self._redirect_login_error("Token de Google inválido"); return
            jwt_payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
            google_email = jwt_payload.get("email", "").lower()
            if not google_email:
                self._redirect_login_error("No se pudo obtener email de Google"); return
            # Buscar superadmin vinculado a este email
            conn = get_db()
            try:
                user = conn.execute(
                    "SELECT * FROM usuarios WHERE google_email=? AND tienda_id IS NULL AND rol='superadmin'",
                    (google_email,)
                ).fetchone()
            finally:
                conn.close()
            if not user:
                self._redirect_login_error(
                    f"El email {google_email} no está vinculado a ninguna cuenta superadmin"
                ); return
            if not user["activo"]:
                self._redirect_login_error("Cuenta inactiva"); return
            # 2FA
            if user["totp_enabled"]:
                partial = self._make_partial_token(user["id"])
                self.send_response(302)
                self.send_header("Location", f"/?partial_token={urllib.parse.quote(partial)}")
                self.end_headers()
            else:
                token, exp = self._make_token(user["id"], None, "superadmin")
                user_json = urllib.parse.quote(json.dumps({
                    "id": user["id"], "nombre": user["nombre"],
                    "username": user["username"], "rol": "superadmin", "tienda_id": None,
                    "totp_enabled": bool(user["totp_enabled"]), "google_email": user["google_email"],
                }))
                self.send_response(302)
                self.send_header("Location", f"/?cb_token={urllib.parse.quote(token)}&cb_exp={exp}&cb_user={user_json}")
                self.end_headers()
        except Exception as e:
            self._redirect_login_error(f"Error: {str(e)}")

    def _auth_totp_verify(self):
        try:
            body = self.read_json_body()
            partial_token   = body.get("partial_token", "")
            code            = body.get("code", "").strip()
            recovery_code   = body.get("recovery_code", "").strip().upper()
            user_id = self._verify_partial_token(partial_token)
            if not user_id:
                self.send_error_json("Token expirado o inválido. Vuelve a iniciar sesión.", 401); return
            conn = get_db()
            try:
                user = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
                if not user or not user["activo"]:
                    self.send_error_json("Usuario no encontrado", 401); return
                if recovery_code:
                    code_hash = _hash_recovery_code(recovery_code)
                    rc = conn.execute(
                        "SELECT id FROM recovery_codes WHERE user_id=? AND code_hash=?",
                        (user_id, code_hash)
                    ).fetchone()
                    if not rc:
                        self.send_error_json("Código de recuperación inválido", 401); return
                    conn.execute("DELETE FROM recovery_codes WHERE id=?", (rc["id"],))
                    conn.commit()
                elif code:
                    if not user["totp_secret"] or not _totp_verify(user["totp_secret"], code):
                        self.send_error_json("Código incorrecto", 401); return
                else:
                    self.send_error_json("Falta código", 400); return
                tienda = None
                if user["tienda_id"] is not None:
                    t = conn.execute("SELECT * FROM tiendas WHERE id=?", (user["tienda_id"],)).fetchone()
                    if t:
                        tienda = {"id": t["id"], "nombre": t["nombre"], "slug": t["slug"], "plan": t["plan"], "tipo": t["tipo"]}
                token, exp = self._make_token(user["id"], user["tienda_id"], user["rol"])
                self.send_json({
                    "token": token, "exp": exp,
                    "user": {"id": user["id"], "nombre": user["nombre"], "username": user["username"],
                             "rol": user["rol"], "tienda_id": user["tienda_id"],
                             "totp_enabled": bool(user["totp_enabled"]), "google_email": user["google_email"]},
                    "tienda": tienda
                })
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _auth_2fa_setup(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Solo el superadmin puede configurar 2FA", 403); return
        secret = _totp_generate_secret()
        conn = get_db()
        try:
            user = conn.execute("SELECT username FROM usuarios WHERE id=?", (ctx.user_id,)).fetchone()
            conn.execute("UPDATE usuarios SET totp_secret=? WHERE id=?", (secret, ctx.user_id))
            conn.commit()
            uri = f"otpauth://totp/AESSPOS:{user['username']}?secret={secret}&issuer=AESSPOS"
            self.send_json({"secret": secret, "uri": uri})
        finally:
            conn.close()

    def _auth_2fa_confirm(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Solo el superadmin puede configurar 2FA", 403); return
        try:
            body = self.read_json_body()
            code = body.get("code", "").strip()
            conn = get_db()
            try:
                user = conn.execute("SELECT * FROM usuarios WHERE id=?", (ctx.user_id,)).fetchone()
                if not user["totp_secret"]:
                    self.send_error_json("Primero genera el secreto con /api/auth/2fa/setup", 400); return
                if not _totp_verify(user["totp_secret"], code):
                    self.send_error_json("Código incorrecto. Verifica que la hora de tu dispositivo sea correcta.", 401); return
                conn.execute("UPDATE usuarios SET totp_enabled=1 WHERE id=?", (ctx.user_id,))
                codes = _generate_recovery_codes()
                conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (ctx.user_id,))
                for c in codes:
                    conn.execute("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?,?)",
                                 (ctx.user_id, _hash_recovery_code(c)))
                conn.commit()
                self.send_json({"ok": True, "recovery_codes": codes})
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _auth_2fa_disable(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Solo el superadmin puede configurar 2FA", 403); return
        try:
            body = self.read_json_body()
            code = body.get("code", "").strip()
            conn = get_db()
            try:
                user = conn.execute("SELECT * FROM usuarios WHERE id=?", (ctx.user_id,)).fetchone()
                if not user["totp_enabled"]:
                    self.send_error_json("El 2FA no está activo", 400); return
                if not _totp_verify(user["totp_secret"], code):
                    self.send_error_json("Código incorrecto", 401); return
                conn.execute("UPDATE usuarios SET totp_enabled=0, totp_secret=NULL WHERE id=?", (ctx.user_id,))
                conn.execute("DELETE FROM recovery_codes WHERE user_id=?", (ctx.user_id,))
                conn.commit()
                self.send_json({"ok": True})
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _auth_2fa_link_google(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            google_email = body.get("google_email", "").strip().lower()
            if not google_email or "@" not in google_email:
                self.send_error_json("Email inválido", 400); return
            conn = get_db()
            try:
                conn.execute("UPDATE usuarios SET google_email=? WHERE id=?", (google_email, ctx.user_id))
                conn.commit()
                self.send_json({"ok": True})
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _auth_unlink_google(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Acceso denegado", 403); return
        try:
            conn = get_db()
            try:
                conn.execute("UPDATE usuarios SET google_email=NULL WHERE id=?", (ctx.user_id,))
                conn.commit()
                self.send_json({"ok": True})
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _make_token(self, user_id, tienda_id, rol):
        """Genera token HMAC-SHA256 con 12h de expiración."""
        exp = int(time.time()) + 43200
        tienda_str = "null" if tienda_id is None else str(tienda_id)
        payload = f"{user_id}:{tienda_str}:{rol}:{exp}"
        payload_b64 = base64.b64encode(payload.encode()).decode()
        sig = _hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload_b64}.{sig}", exp

    def _auth_login(self):
        try:
            body = self.read_json_body()
            username = body.get("username", "").strip().lower()
            password = body.get("password", "")
            tienda_slug = body.get("tienda_slug", "").strip()
            if not username or not password:
                self.send_error_json("Faltan credenciales", 400); return
            conn = get_db()
            try:
                # Superadmin: tienda_id IS NULL (solo desde la raíz, sin slug)
                user = None
                if not tienda_slug:
                    user = conn.execute(
                        "SELECT * FROM usuarios WHERE username=? AND tienda_id IS NULL", (username,)
                    ).fetchone()
                if not user:
                    if tienda_slug:
                        # Login con slug → buscar exactamente en esa tienda
                        tienda_row = conn.execute(
                            "SELECT id FROM tiendas WHERE slug=? AND activo=1", (tienda_slug,)
                        ).fetchone()
                        if not tienda_row:
                            self.send_error_json("Tienda no encontrada", 404); return
                        user = conn.execute(
                            "SELECT * FROM usuarios WHERE username=? AND tienda_id=?",
                            (username, tienda_row["id"])
                        ).fetchone()
                    else:
                        user = conn.execute(
                            "SELECT * FROM usuarios WHERE username=? AND tienda_id IS NOT NULL ORDER BY created_at ASC LIMIT 1",
                            (username,)
                        ).fetchone()
                if not user:
                    self.send_error_json("Credenciales incorrectas", 401); return
                if not user["activo"]:
                    self.send_error_json("Usuario inactivo", 401); return
                expected_hash = _hash_password(password, user["salt"])
                if not _hmac.compare_digest(expected_hash, user["password_hash"]):
                    self.send_error_json("Credenciales incorrectas", 401); return
                # 2FA — si está activo, devolver partial token
                if user["totp_enabled"]:
                    partial = self._make_partial_token(user["id"])
                    self.send_json({"needs_2fa": True, "partial_token": partial}); return
                # Verificar tienda activa (si aplica)
                tienda = None
                if user["tienda_id"] is not None:
                    tienda_row = conn.execute("SELECT * FROM tiendas WHERE id=?", (user["tienda_id"],)).fetchone()
                    if not tienda_row or not tienda_row["activo"]:
                        self.send_error_json("Tienda inactiva", 401); return
                    tienda = {"id": tienda_row["id"], "nombre": tienda_row["nombre"], "slug": tienda_row["slug"], "plan": tienda_row["plan"], "tipo": tienda_row.get("tipo")}
                token, exp = self._make_token(user["id"], user["tienda_id"], user["rol"])
                self.send_json({
                    "token": token, "exp": exp,
                    "user": {"id": user["id"], "nombre": user["nombre"], "username": user["username"],
                             "rol": user["rol"], "tienda_id": user["tienda_id"]},
                    "tienda": tienda
                })
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _auth_me(self):
        ctx = self.require_auth()
        if not ctx: return
        try:
            conn = get_db()
            try:
                user = conn.execute("SELECT * FROM usuarios WHERE id=?", (ctx.user_id,)).fetchone()
                if not user:
                    self.send_error_json("Usuario no encontrado", 404); return
                tienda = None
                if ctx.tienda_id is not None:
                    t = conn.execute("SELECT * FROM tiendas WHERE id=?", (ctx.tienda_id,)).fetchone()
                    if t:
                        tienda = {"id": t["id"], "nombre": t["nombre"], "slug": t["slug"], "plan": t["plan"], "tipo": t["tipo"]}
                rc_count = conn.execute(
                    "SELECT COUNT(*) FROM recovery_codes WHERE user_id=?", (user["id"],)
                ).fetchone()[0]
                self.send_json({
                    "user": {
                        "id": user["id"],
                        "nombre": user["nombre"],
                        "username": user["username"],
                        "rol": user["rol"],
                        "tienda_id": user["tienda_id"],
                        "totp_enabled": bool(user["totp_enabled"]),
                        "google_email": user["google_email"],
                        "recovery_codes_remaining": rc_count,
                    },
                    "tienda": tienda
                })
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def read_json_body(self, max_size=2_000_000):
        """Lee y parsea el body JSON. Rechaza payloads > 2 MB para prevenir DoS."""
        length = int(self.headers.get("Content-Length", 0))
        if length > max_size:
            self.send_error_json("Payload demasiado grande", 413)
            return None
        data = b""
        while len(data) < length:
            chunk = self.rfile.read(length - len(data))
            if not chunk:
                break
            data += chunk
        if not data:
            return {}
        return json.loads(data)

    def do_OPTIONS(self):
        self.send_response(200)
        cors = self._cors_origin()
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ── GET ────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html") or path.startswith("/t/"):
            self._serve_file(BASE_DIR / "index.html", "text/html")
        elif path == "/api/status":
            self.send_json({"status": "ok"})
        elif path == "/api/auth/me":
            self._auth_me()
        elif path == "/api/auth/google":
            self._auth_google_redirect()
        elif path.startswith("/api/auth/google/callback"):
            self._auth_google_callback()
        elif path == "/api/tiendas":
            self._list_tiendas()
        elif path.startswith("/api/tiendas/by-slug/"):
            slug = path.split("/api/tiendas/by-slug/")[1]
            self._get_tienda_by_slug(slug)
        elif path == "/api/usuarios":
            self._list_usuarios()
        elif path.startswith("/api/tiendas/") and path.endswith("/usuarios"):
            parts = path.split("/")
            if len(parts) == 5 and parts[3].isdigit():
                self._list_tienda_usuarios(int(parts[3]))
            else:
                self.send_error_json("Ruta no encontrada", 404)
        elif path == "/api/productos":
            ctx = self.require_auth()
            if not ctx: return
            conn = get_db()
            try:
                if ctx.rol == 'superadmin':
                    rows = conn.execute(
                        "SELECT id,emoji,name,barcode,cat,price,precio_compra,stock,alert,"
                        "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                        "FROM productos ORDER BY name"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id,emoji,name,barcode,cat,price,precio_compra,stock,alert,"
                        "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                        "FROM productos WHERE tienda_id=? ORDER BY name",
                        (ctx.tienda_id,)
                    ).fetchall()
                self.send_json([row_to_dict(r) for r in rows])
            finally:
                conn.close()
        elif path == "/api/productos/thumbnails":
            # Carga batch de thumbnails — opcionalmente filtrada por ?ids=1,2,3
            ctx = self.require_auth()
            if not ctx: return
            # Parsear ids opcionales del query string
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            ids_param = params.get("ids", [None])[0]
            id_list = [int(x) for x in ids_param.split(",") if x.strip().isdigit()] if ids_param else None
            conn = get_db()
            try:
                if id_list is not None:
                    placeholders = ",".join("?" * len(id_list))
                    if ctx.rol == 'superadmin':
                        rows = conn.execute(
                            f"SELECT id, thumbnail FROM productos "
                            f"WHERE id IN ({placeholders}) AND thumbnail IS NOT NULL AND thumbnail != ''",
                            id_list
                        ).fetchall()
                    else:
                        rows = conn.execute(
                            f"SELECT id, thumbnail FROM productos "
                            f"WHERE id IN ({placeholders}) AND tienda_id=? AND thumbnail IS NOT NULL AND thumbnail != ''",
                            id_list + [ctx.tienda_id]
                        ).fetchall()
                elif ctx.rol == 'superadmin':
                    rows = conn.execute(
                        "SELECT id, thumbnail FROM productos "
                        "WHERE thumbnail IS NOT NULL AND thumbnail != '' ORDER BY id"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id, thumbnail FROM productos "
                        "WHERE tienda_id=? AND thumbnail IS NOT NULL AND thumbnail != '' ORDER BY id",
                        (ctx.tienda_id,)
                    ).fetchall()
                self.send_json({str(r["id"]): r["thumbnail"] for r in rows})
            finally:
                conn.close()
        elif path.startswith("/api/productos/") and path.endswith("/thumbnail"):
            ctx = self.require_auth()
            if not ctx: return
            parts = path.split("/")
            if len(parts) == 5:
                prod_id = parts[3]
                if not prod_id.isdigit():
                    self.send_response(404); self.end_headers(); return
                conn = get_db()
                try:
                    if ctx.rol == 'superadmin':
                        row = conn.execute(
                            "SELECT thumbnail FROM productos WHERE id=?", (prod_id,)
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT thumbnail FROM productos WHERE id=? AND tienda_id=?",
                            (prod_id, ctx.tienda_id)
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
            ctx = self.require_auth()
            if not ctx: return
            barcode = path.split("/api/lookup-barcode/")[1]
            self._lookup_barcode(barcode, ctx)
        elif path == "/api/admin/diagnostico":
            ctx = self.require_auth()
            if not ctx: return
            if ctx.rol != 'superadmin':
                self.send_error_json("Solo superadmin", 403); return
            conn = get_db()
            try:
                tiendas = conn.execute("SELECT id, nombre, plan FROM tiendas ORDER BY id").fetchall()
                result = []
                for t in tiendas:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM productos WHERE tienda_id=?", (t["id"],)
                    ).fetchone()[0]
                    null_cnt = conn.execute(
                        "SELECT COUNT(*) FROM productos WHERE tienda_id IS NULL"
                    ).fetchone()[0]
                    result.append({"id": t["id"], "nombre": t["nombre"], "plan": t["plan"],
                                   "productos": cnt, "sin_tienda": null_cnt})
                self.send_json({"tiendas": result})
            finally:
                conn.close()
        elif path == "/api/sync":
            self._get_sync()
        elif path == "/api/turnos/activo":
            self._get_turno_activo()
        elif path == "/api/turnos":
            self._list_turnos()
        elif path == "/api/ventas":
            ctx = self.require_auth()
            if not ctx: return
            conn = get_db()
            try:
                if ctx.rol == 'superadmin':
                    ventas = conn.execute("SELECT * FROM ventas ORDER BY created_at DESC LIMIT 2000").fetchall()
                else:
                    ventas = conn.execute(
                        "SELECT * FROM ventas WHERE tienda_id=? ORDER BY created_at DESC LIMIT 2000",
                        (ctx.tienda_id,)
                    ).fetchall()
                result = []
                for v in ventas:
                    vd = row_to_dict(v)
                    items = conn.execute("SELECT * FROM items_venta WHERE venta_id=?", (v["id"],)).fetchall()
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
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(500)
            self.end_headers()

    # ── POST ───────────────────────────────────────────────────────
    def do_POST(self):
        if self.path == "/api/auth/login":
            self._auth_login(); return
        elif self.path == "/api/auth/totp-verify":
            self._auth_totp_verify(); return
        elif self.path == "/api/auth/2fa/setup":
            self._auth_2fa_setup(); return
        elif self.path == "/api/auth/2fa/confirm":
            self._auth_2fa_confirm(); return
        elif self.path == "/api/auth/2fa/disable":
            self._auth_2fa_disable(); return
        elif self.path == "/api/auth/2fa/link-google":
            self._auth_2fa_link_google(); return
        elif self.path == "/api/productos":
            self._create_producto()
        elif self.path == "/api/ventas":
            self._create_venta()
        elif self.path == "/analyze-product":
            ctx = self.require_auth()
            if not ctx: return
            if not self.require_pro(ctx): return
            self._handle_analyze_product()
        elif self.path == "/api/facturas/analizar":
            ctx = self.require_auth()
            if not ctx: return
            if not self.require_pro(ctx): return
            self._handle_analizar_factura(ctx)
        elif self.path == "/api/facturas/aplicar":
            ctx = self.require_auth()
            if not ctx: return
            if not self.require_pro(ctx): return
            self._handle_aplicar_factura(ctx)
        elif self.path == "/api/ai-cierre":
            ctx = self.require_auth()
            if not ctx: return
            if not self.require_pro(ctx): return
            self._handle_ai_cierre()
        elif self.path == "/api/ai-pedido":
            ctx = self.require_auth()
            if not ctx: return
            if not self.require_pro(ctx): return
            self._handle_ai_pedido()
        elif self.path == "/api/admin/reasignar-productos":
            ctx = self.require_auth()
            if not ctx: return
            if ctx.rol != 'superadmin':
                self.send_error_json("Solo superadmin", 403); return
            try:
                body = self.read_json_body()
                desde = body.get("desde_tienda_id")
                hacia = int(body["hacia_tienda_id"])
                conn = get_db()
                try:
                    if desde is None:
                        # Reasignar productos sin tienda
                        r = conn.execute(
                            "UPDATE productos SET tienda_id=? WHERE tienda_id IS NULL", (hacia,)
                        )
                    else:
                        r = conn.execute(
                            "UPDATE productos SET tienda_id=? WHERE tienda_id=?", (hacia, int(desde))
                        )
                    conn.commit()
                    self.send_json({"ok": True, "actualizados": r.rowcount})
                finally:
                    conn.close()
            except Exception as e:
                self.send_error_json(str(e), 500)
        elif self.path == "/api/turnos":
            self._open_turno()
        elif self.path == "/api/tiendas":
            self._create_tienda()
        elif self.path == "/api/usuarios":
            self._create_usuario()
        elif self.path.startswith("/api/tiendas/") and self.path.endswith("/usuarios"):
            parts = self.path.split("/")
            if len(parts) == 5 and parts[3].isdigit():
                self._create_tienda_usuario(int(parts[3]))
            else:
                self.send_error_json("Ruta no encontrada", 404)
        elif self.path == "/api/admin/compress-thumbnails":
            self._compress_all_thumbnails()
        elif self.path == "/api/ajustes-stock":
            self._crear_ajuste_stock()
        else:
            # POST /api/ventas/:id/anular
            parts = self.path.split("/")
            if len(parts) == 5 and parts[2] == "ventas" and parts[4] == "anular" and parts[3].isdigit():
                self._anular_venta(int(parts[3]))
            else:
                self.send_error_json("Ruta no encontrada", 404)

    # ── PUT ────────────────────────────────────────────────────────
    def do_PUT(self):
        parts = self.path.split("/")
        if len(parts) == 4 and parts[2] == "productos":
            self._update_producto(parts[3])
        elif len(parts) == 5 and parts[2] == "turnos" and parts[4] == "cierre":
            self._close_turno(int(parts[3]))
        elif len(parts) == 4 and parts[2] == "tiendas" and parts[3].isdigit():
            self._update_tienda(int(parts[3]))
        elif len(parts) == 4 and parts[2] == "usuarios" and parts[3].isdigit():
            self._update_usuario(int(parts[3]))
        elif len(parts) == 5 and parts[2] == "usuarios" and parts[4] == "password" and parts[3].isdigit():
            self._update_usuario_password(int(parts[3]))
        else:
            self.send_error_json("Ruta no encontrada", 404)

    # ── DELETE ─────────────────────────────────────────────────────
    def do_DELETE(self):
        parts = self.path.split("/")
        if self.path == "/api/auth/2fa/link-google":
            self._auth_unlink_google()
        elif len(parts) == 4 and parts[2] == "productos":
            self._delete_producto(parts[3])
        elif len(parts) == 4 and parts[2] == "tiendas" and parts[3].isdigit():
            self._delete_tienda(int(parts[3]))
        elif len(parts) == 4 and parts[2] == "usuarios" and parts[3].isdigit():
            self._delete_usuario(int(parts[3]))
        else:
            self.send_error_json("Ruta no encontrada", 404)

    # ── CRUD PRODUCTOS ─────────────────────────────────────────────
    def _compress_all_thumbnails(self):
        """Endpoint de mantenimiento: recomprime todas las imágenes existentes a 80×80@70."""
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Solo superadmin", 403); return
        if not PIL_AVAILABLE:
            self.send_error_json("Pillow no disponible en este servidor", 500); return
        try:
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT id, thumbnail FROM productos WHERE thumbnail IS NOT NULL AND thumbnail != ''"
                ).fetchall()
                updated = 0
                skipped = 0
                for row in rows:
                    compressed = self._compress_thumbnail(row["thumbnail"])
                    # Solo actualizar si la compresión redujo el tamaño
                    if compressed and len(compressed) < len(row["thumbnail"]):
                        conn.execute("UPDATE productos SET thumbnail=? WHERE id=?",
                                     (compressed, row["id"]))
                        updated += 1
                    else:
                        skipped += 1
                conn.commit()
                self.send_json({"ok": True, "updated": updated, "skipped": skipped,
                                "total": len(rows)})
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _compress_thumbnail(self, data_url):
        """Comprime cualquier imagen a 80×80 JPEG @70 antes de guardarla."""
        if not data_url or not PIL_AVAILABLE:
            return data_url
        try:
            header, b64 = data_url.split(",", 1)
            img_bytes = base64.b64decode(b64)
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            img.thumbnail((80, 80), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
        except Exception:
            return data_url  # si falla, guardar original

    def _create_producto(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol not in ('admin', 'superadmin'):
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            tienda_id = None if ctx.rol == 'superadmin' else ctx.tienda_id
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "INSERT INTO productos (emoji,name,barcode,cat,price,precio_compra,stock,alert,thumbnail,tienda_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                 body.get("cat","General"), body.get("price",0),
                 body.get("precio_compra") or None,
                 body.get("stock",0), body.get("alert",5),
                 self._compress_thumbnail(body.get("thumbnail")), tienda_id)
            )
            row = conn.execute(
                "SELECT id,emoji,name,barcode,cat,price,precio_compra,stock,alert,thumbnail "
                "FROM productos WHERE id=?", (c.lastrowid,)
            ).fetchone()
            conn.commit(); conn.close()
            self.send_json(row_to_dict(row), 201)
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _update_producto(self, prod_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol not in ('admin', 'superadmin'):
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            conn = get_db()
            tienda_filter = "" if ctx.rol == 'superadmin' else " AND tienda_id=?"
            tienda_params = () if ctx.rol == 'superadmin' else (ctx.tienda_id,)
            pc = body.get("precio_compra") or None
            if "thumbnail" in body:
                conn.execute(
                    f"UPDATE productos SET emoji=?,name=?,barcode=?,cat=?,price=?,precio_compra=?,stock=?,alert=?,thumbnail=? WHERE id=?{tienda_filter}",
                    (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                     body.get("cat","General"), body.get("price",0), pc,
                     body.get("stock",0), body.get("alert",5),
                     self._compress_thumbnail(body.get("thumbnail")), prod_id) + tienda_params
                )
            else:
                conn.execute(
                    f"UPDATE productos SET emoji=?,name=?,barcode=?,cat=?,price=?,precio_compra=?,stock=?,alert=? WHERE id=?{tienda_filter}",
                    (body.get("emoji","📦"), body["name"], body.get("barcode",""),
                     body.get("cat","General"), body.get("price",0), pc,
                     body.get("stock",0), body.get("alert",5), prod_id) + tienda_params
                )
            if tienda_params:  # non-superadmin: verificar que el producto pertenece a esta tienda
                row = conn.execute(
                    "SELECT id,emoji,name,barcode,cat,price,precio_compra,stock,alert,thumbnail "
                    "FROM productos WHERE id=? AND tienda_id=?", (prod_id, ctx.tienda_id)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id,emoji,name,barcode,cat,price,precio_compra,stock,alert,thumbnail "
                    "FROM productos WHERE id=?", (prod_id,)
                ).fetchone()
            conn.commit(); conn.close()
            self.send_json(row_to_dict(row) if row else {}, 200 if row else 404)
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _delete_producto(self, prod_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol not in ('admin', 'superadmin'):
            self.send_error_json("Acceso denegado", 403); return
        try:
            conn = get_db()
            c = conn.cursor()
            if ctx.rol == 'superadmin':
                c.execute("DELETE FROM productos WHERE id=?", (prod_id,))
            else:
                c.execute("DELETE FROM productos WHERE id=? AND tienda_id=?", (prod_id, ctx.tienda_id))
            if c.rowcount == 0:
                conn.close()
                self.send_error_json("Producto no encontrado", 404); return
            conn.commit(); conn.close()
            self.send_json({"ok": True})
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── VENTAS ─────────────────────────────────────────────────────
    def _create_venta(self):
        ctx = self.require_auth()
        if not ctx: return
        try:
            body = self.read_json_body()
            tienda_id = None if ctx.rol == 'superadmin' else ctx.tienda_id
            conn = get_db()
            c = conn.cursor()
            c.execute(
                "INSERT INTO ventas (total, metodo, recibido, turno_id, tienda_id) VALUES (?,?,?,?,?)",
                (body["total"], body.get("metodo","efectivo"),
                 body.get("recibido", body["total"]),
                 body.get("turno_id"), tienda_id)
            )
            venta_id = c.lastrowid
            for item in body.get("items", []):
                c.execute(
                    "INSERT INTO items_venta (venta_id,producto_id,name,emoji,qty,price) VALUES (?,?,?,?,?,?)",
                    (venta_id, item.get("id"), item.get("name",""), item.get("emoji",""),
                     item.get("qty",1), item.get("price",0))
                )
                if tienda_id is None:
                    c.execute(
                        "UPDATE productos SET stock = MAX(0, stock - ?) WHERE id=?",
                        (item.get("qty",1), item.get("id"))
                    )
                else:
                    c.execute(
                        "UPDATE productos SET stock = MAX(0, stock - ?) WHERE id=? AND tienda_id=?",
                        (item.get("qty",1), item.get("id"), tienda_id)
                    )
            row = conn.execute("SELECT * FROM ventas WHERE id=?", (venta_id,)).fetchone()
            conn.commit(); conn.close()
            self.send_json({"ok": True, "venta": row_to_dict(row)}, 201)
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── ANULACIÓN DE VENTAS ────────────────────────────────────────
    def _anular_venta(self, venta_id):
        """Soft-delete de una venta: restaura stock y la marca como anulada."""
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol == 'cajero':
            self.send_error_json("Solo admin puede anular ventas", 403); return
        try:
            body = self.read_json_body()
            motivo = (body.get("motivo") or "").strip()
            if not motivo:
                self.send_error_json("Falta el motivo de anulación", 400); return
            conn = get_db()
            try:
                venta = conn.execute(
                    "SELECT * FROM ventas WHERE id=? AND tienda_id=?",
                    (venta_id, ctx.tienda_id)
                ).fetchone()
                if not venta:
                    self.send_error_json("Venta no encontrada", 404); return
                if venta["status"] == "anulada":
                    self.send_error_json("Esta venta ya fue anulada", 409); return
                # Restaurar stock de cada ítem
                items = conn.execute(
                    "SELECT * FROM items_venta WHERE venta_id=?", (venta_id,)
                ).fetchall()
                for item in items:
                    if item["producto_id"]:
                        conn.execute(
                            "UPDATE productos SET stock = stock + ? WHERE id=? AND tienda_id=?",
                            (item["qty"], item["producto_id"], ctx.tienda_id)
                        )
                # Marcar como anulada
                conn.execute(
                    """UPDATE ventas SET status='anulada', anulada_at=CURRENT_TIMESTAMP,
                       motivo_anulacion=? WHERE id=?""",
                    (motivo, venta_id)
                )
                conn.commit()
                venta_actualizada = conn.execute(
                    "SELECT * FROM ventas WHERE id=?", (venta_id,)
                ).fetchone()
                self.send_json({"ok": True, "venta": row_to_dict(venta_actualizada)})
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── AJUSTE DE STOCK (ENTRADA DE INVENTARIO) ────────────────────
    def _crear_ajuste_stock(self):
        """Ajusta el stock de uno o varios productos (entrada de mercancía)."""
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol == 'cajero':
            self.send_error_json("Solo admin puede ajustar stock", 403); return
        try:
            body = self.read_json_body()
            ajustes = body.get("ajustes", [])  # [{producto_id, delta, motivo}]
            if not ajustes:
                self.send_error_json("Sin ajustes", 400); return
            conn = get_db()
            try:
                productos_actualizados = []
                for aj in ajustes:
                    prod_id = int(aj.get("producto_id", 0))
                    delta   = int(aj.get("delta", 0))
                    motivo  = (aj.get("motivo") or "Entrada de inventario").strip()
                    usuario = ctx.user_id
                    if delta == 0:
                        continue
                    # Verificar que el producto pertenece a la tienda
                    prod = conn.execute(
                        "SELECT id, stock FROM productos WHERE id=? AND tienda_id=?",
                        (prod_id, ctx.tienda_id)
                    ).fetchone()
                    if not prod:
                        continue
                    conn.execute(
                        "UPDATE productos SET stock = MAX(0, stock + ?) WHERE id=? AND tienda_id=?",
                        (delta, prod_id, ctx.tienda_id)
                    )
                    conn.execute(
                        "INSERT INTO ajustes_stock (producto_id, tienda_id, delta, motivo, usuario) VALUES (?,?,?,?,?)",
                        (prod_id, ctx.tienda_id, delta, motivo, usuario)
                    )
                    nuevo_stock = conn.execute(
                        "SELECT stock FROM productos WHERE id=?", (prod_id,)
                    ).fetchone()["stock"]
                    productos_actualizados.append({"id": prod_id, "stock": nuevo_stock})
                conn.commit()
                self.send_json({"ok": True, "actualizados": productos_actualizados})
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── ANÁLISIS IA ────────────────────────────────────────────────
    # ── ANÁLISIS DE FACTURA DE PROVEEDOR CON IA ─────────────────────
    def _handle_analizar_factura(self, ctx):
        """Recibe imagen de factura, llama a Claude Vision, retorna items cruzados con inventario."""
        if not AI_AVAILABLE:
            self.send_error_json("SDK de Anthropic no instalado (pip3 install anthropic)"); return
        if not ANTHROPIC_API_KEY:
            self.send_error_json("ANTHROPIC_API_KEY no configurada"); return
        try:
            body       = self.read_json_body()
            image_b64  = body.get("image")
            media_type = body.get("mediaType", "image/jpeg")
            if not image_b64:
                self.send_error_json("Falta el campo 'image'"); return

            prompt = """Eres un experto en facturas de proveedores colombianos de consumo masivo.

Analiza CUIDADOSAMENTE esta imagen de factura y extrae TODOS los productos.

Para cada producto devuelve:
- "barcode": código de barras / código del producto (string, puede estar en columna "Código", "Cod Barra", etc.)
- "name": nombre completo del producto tal como aparece en la factura
- "qty": cantidad comprada (número entero — columna "Cant" o similar)
- "price_unit": precio unitario final con IVA incluido (columna "P. Final", "P.U.A Iva", "Valor unitario" o similar)
- "price_total": total de la línea (columna "Total" o "Valor total")

Reglas importantes:
1. El "price_unit" es el precio POR UNIDAD/CAJA comprada, NO el total de la línea
2. Si "P. Final" o "P.U.A Iva" no está disponible, calcula: price_total / qty
3. Los precios son en pesos colombianos, sin símbolo $, con puntos como separador de miles
4. El barcode puede ser vacío string "" si no aparece código
5. Extrae TODOS los productos visibles, sin omitir ninguno

Responde ÚNICAMENTE con este JSON, sin texto adicional:
{
  "proveedor": "nombre del proveedor si es legible",
  "fecha": "fecha de la factura si es visible",
  "items": [
    {"barcode": "7702117008216", "name": "ALMENDRA FRANCESA X 50GR ITALO", "qty": 4, "price_unit": 2677, "price_total": 10710},
    ...
  ]
}"""

            client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            message = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text",  "text": prompt}
                ]}],
            )
            raw = message.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"): raw = raw[4:]
            factura = json.loads(raw.strip())
            items   = factura.get("items", [])

            # Cruzar con inventario de la tienda
            conn = get_db()
            try:
                if ctx.rol == 'superadmin':
                    rows = conn.execute(
                        "SELECT id,name,barcode,price,stock,cat,emoji FROM productos ORDER BY name"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id,name,barcode,price,stock,cat,emoji FROM productos "
                        "WHERE tienda_id=? ORDER BY name", (ctx.tienda_id,)
                    ).fetchall()
            finally:
                conn.close()

            inv_by_barcode = {r["barcode"]: dict(r) for r in rows if r["barcode"]}
            inv_by_id      = {r["id"]: dict(r) for r in rows}

            enriched = []
            for item in items:
                bc = str(item.get("barcode","")).strip()
                match = inv_by_barcode.get(bc)
                enriched.append({
                    "barcode":     bc,
                    "name":        item.get("name",""),
                    "qty":         int(item.get("qty") or 1),
                    "price_unit":  float(item.get("price_unit") or 0),
                    "price_total": float(item.get("price_total") or 0),
                    "status":      "update" if match else "new",
                    "prod_id":     match["id"] if match else None,
                    "prod_name":   match["name"] if match else None,
                    "stock_actual": match["stock"] if match else None,
                    "precio_venta": match["price"] if match else None,
                })

            self.send_json({
                "ok":       True,
                "proveedor": factura.get("proveedor",""),
                "fecha":    factura.get("fecha",""),
                "items":    enriched,
            })
        except json.JSONDecodeError as e:
            self.send_error_json(f"IA devolvió formato inesperado: {e}")
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _handle_aplicar_factura(self, ctx):
        """Aplica los cambios de la factura al inventario: actualiza stock y crea productos nuevos."""
        if ctx.rol not in ('admin', 'superadmin'):
            self.send_error_json("Solo admin puede aplicar facturas", 403); return
        try:
            body  = self.read_json_body()
            items = body.get("items", [])
            conn  = get_db()
            tienda_id = None if ctx.rol == 'superadmin' else ctx.tienda_id
            creados    = 0
            actualizados = 0
            try:
                for item in items:
                    if not item.get("incluir", True):
                        continue
                    prod_id    = item.get("prod_id")
                    qty        = int(item.get("qty") or 0)
                    price_unit = float(item.get("price_unit") or 0)
                    if item.get("status") == "update" and prod_id:
                        # Actualizar stock y precio de compra
                        conn.execute(
                            "UPDATE productos SET stock = stock + ?, precio_compra = ? WHERE id = ?",
                            (qty, price_unit if price_unit > 0 else None, prod_id)
                        )
                        actualizados += 1
                    elif item.get("status") == "new":
                        name = item.get("name","").strip()[:120]
                        if not name: continue
                        bc  = str(item.get("barcode","")).strip()[:60]
                        cat = item.get("cat","Otro") or "Otro"
                        emoji = item.get("emoji","📦") or "📦"
                        # Precio de venta sugerido: compra + 30%
                        precio_venta = round(price_unit * 1.30) if price_unit > 0 else 0
                        conn.execute(
                            "INSERT INTO productos "
                            "(emoji,name,barcode,cat,price,precio_compra,stock,alert,tienda_id) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (emoji, name, bc, cat, precio_venta,
                             price_unit if price_unit > 0 else None,
                             qty, 5, tienda_id)
                        )
                        creados += 1
                conn.commit()
            finally:
                conn.close()
            self.send_json({
                "ok": True,
                "actualizados": actualizados,
                "creados": creados,
                "msg": f"✅ {actualizados} productos actualizados, {creados} creados"
            })
        except Exception as e:
            self.send_error_json(str(e), 500)

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

    def _get_sync(self):
        """Retorna productos + ventas + turno activo en una sola petición (F4)."""
        ctx = self.require_auth()
        if not ctx: return
        conn = get_db()
        try:
            # ── Productos ────────────────────────────────────────────────
            if ctx.rol == 'superadmin':
                prod_rows = conn.execute(
                    "SELECT id,emoji,name,barcode,cat,price,precio_compra,stock,alert,"
                    "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                    "FROM productos ORDER BY name"
                ).fetchall()
            else:
                prod_rows = conn.execute(
                    "SELECT id,emoji,name,barcode,cat,price,precio_compra,stock,alert,"
                    "(thumbnail IS NOT NULL AND thumbnail != '') AS has_thumbnail "
                    "FROM productos WHERE tienda_id=? ORDER BY name",
                    (ctx.tienda_id,)
                ).fetchall()
            productos = [row_to_dict(r) for r in prod_rows]

            # ── Ventas (últimas 2000 — ~40 días para tienda activa) ──────────
            if ctx.rol == 'superadmin':
                venta_rows = conn.execute(
                    "SELECT * FROM ventas ORDER BY created_at DESC LIMIT 2000"
                ).fetchall()
            else:
                venta_rows = conn.execute(
                    "SELECT * FROM ventas WHERE tienda_id=? ORDER BY created_at DESC LIMIT 2000",
                    (ctx.tienda_id,)
                ).fetchall()
            ventas = []
            for v in venta_rows:
                vd = row_to_dict(v)
                items = conn.execute(
                    "SELECT * FROM items_venta WHERE venta_id=?", (v["id"],)
                ).fetchall()
                vd["items"] = [row_to_dict(i) for i in items]
                ventas.append(vd)

            # ── Turno activo ─────────────────────────────────────────────
            if ctx.rol == 'superadmin':
                turno = None  # superadmin no tiene turno
            else:
                turno_row = conn.execute(
                    "SELECT * FROM turnos WHERE tienda_id=? AND estado='abierto' "
                    "ORDER BY apertura_at DESC LIMIT 1",
                    (ctx.tienda_id,)
                ).fetchone()
                turno = row_to_dict(turno_row) if turno_row else None

            self.send_json({"productos": productos, "ventas": ventas, "turno": turno})
        finally:
            conn.close()

    # ── TURNOS ─────────────────────────────────────────────────────
    def _get_turno_activo(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.tienda_id is None:  # superadmin no opera cajas
            self.send_json(None); return
        try:
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT * FROM turnos WHERE estado='abierto' AND tienda_id=? ORDER BY apertura_at DESC LIMIT 1",
                    (ctx.tienda_id,)
                ).fetchone()
                self.send_json(row_to_dict(row) if row else None)
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _list_turnos(self):
        ctx = self.require_auth()
        if not ctx: return
        try:
            conn = get_db()
            try:
                if ctx.rol == 'superadmin':
                    rows = conn.execute("SELECT * FROM turnos ORDER BY apertura_at DESC LIMIT 120").fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM turnos WHERE tienda_id=? ORDER BY apertura_at DESC LIMIT 120",
                        (ctx.tienda_id,)
                    ).fetchall()
                self.send_json([row_to_dict(r) for r in rows])
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _open_turno(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.tienda_id is None:
            self.send_error_json("Superadmin no puede operar cajas directamente", 403); return
        try:
            body = self.read_json_body()
            cajero = body.get("cajero", "").strip()
            if not cajero:
                self.send_error_json("Falta el nombre del cajero"); return
            conn = get_db()
            try:
                activo = conn.execute(
                    "SELECT * FROM turnos WHERE estado='abierto' AND tienda_id=? ORDER BY apertura_at DESC LIMIT 1",
                    (ctx.tienda_id,)
                ).fetchone()
                if activo:
                    self.send_json({"error": "Ya existe un turno abierto", "turno_activo": row_to_dict(activo)}, 409)
                    return
                c = conn.cursor()
                c.execute(
                    "INSERT INTO turnos (cajero, monto_inicial, caja_id, tienda_id) VALUES (?,?,?,?)",
                    (cajero, body.get("monto_inicial", 0), body.get("caja_id", "caja-1"), ctx.tienda_id)
                )
                turno = conn.execute("SELECT * FROM turnos WHERE id=?", (c.lastrowid,)).fetchone()
                conn.commit()
                self.send_json(row_to_dict(turno), 201)
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _close_turno(self, turno_id):
        ctx = self.require_auth()
        if not ctx: return
        try:
            body = self.read_json_body()
            conn = get_db()
            try:
                # Calcular totales reales desde ventas — el cliente no es fuente de verdad
                totals = conn.execute("""
                    SELECT
                        COALESCE(SUM(CASE WHEN metodo='efectivo' THEN total ELSE 0 END), 0) AS efectivo_ventas,
                        COALESCE(SUM(CASE WHEN metodo='transferencia' THEN total ELSE 0 END), 0) AS transferencias,
                        COALESCE(SUM(total), 0) AS total_ventas,
                        COUNT(*) AS num_tx
                    FROM ventas WHERE turno_id=?
                """, (turno_id,)).fetchone()

                tienda_filter = "" if ctx.rol == 'superadmin' else " AND tienda_id=?"
                params_filter = () if ctx.rol == 'superadmin' else (ctx.tienda_id,)
                conn.execute(
                    f"""UPDATE turnos SET estado='cerrado', cierre_at=CURRENT_TIMESTAMP,
                        efectivo_ventas=?, transferencias=?, total_ventas=?, num_tx=?,
                        monto_contado=?, diferencia=?, resumen_ia=?
                       WHERE id=? AND estado='abierto'{tienda_filter}""",
                    (totals["efectivo_ventas"], totals["transferencias"],
                     totals["total_ventas"], totals["num_tx"],
                     body.get("monto_contado"), body.get("diferencia"),
                     body.get("resumen_ia"), turno_id) + params_filter
                )
                turno = conn.execute("SELECT * FROM turnos WHERE id=?", (turno_id,)).fetchone()
                if not turno or turno["estado"] != "cerrado":
                    self.send_error_json("Turno no encontrado o ya cerrado", 409); return
                conn.commit()
                self.send_json(row_to_dict(turno))
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    # ── LOOKUP BARCODE (Open Food Facts) ───────────────────────────
    def _lookup_barcode(self, barcode, ctx=None):
        # MercadoLibre (precios sugeridos) solo para plan Pro
        is_pro = ctx is None or ctx.rol == 'superadmin' or self._tienda_is_pro(ctx.tienda_id)
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
                # OpenFoodFacts no encontró el producto; MercadoLibre solo en plan Pro
                if is_pro:
                    meli  = self._meli_lookup(barcode)
                    ps    = {k: meli[k] for k in ("min","max","medio","n")} if meli else None
                    thumb = meli.get("thumbnail") if meli else None
                else:
                    ps = None; thumb = None
                self.send_json({"found": False, "precio_sugerido": ps, "thumbnail": thumb}); return

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
                    img.thumbnail((80, 80), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=70)
                    thumbnail = f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"
                except Exception:
                    pass

            # Mercado Libre: precio + imagen real (solo plan Pro)
            precio_sugerido = None
            if is_pro:
                meli = self._meli_lookup(name or barcode)
                precio_sugerido = {k: meli[k] for k in ("min","max","medio","n")} if meli else None
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
                        img.thumbnail((80, 80), Image.LANCZOS)
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=70)
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

    # ── TIENDAS (superadmin) ────────────────────────────────────────
    def _get_tienda_by_slug(self, slug):
        """Endpoint público — devuelve id+nombre+slug de la tienda (sin datos sensibles)."""
        if not slug:
            self.send_error_json("Slug requerido", 400); return
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id, nombre, slug FROM tiendas WHERE slug=? AND activo=1", (slug,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            self.send_error_json("Tienda no encontrada", 404); return
        self.send_json({"id": row["id"], "nombre": row["nombre"], "slug": row["slug"]})

    def _list_tiendas(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Acceso denegado", 403); return
        try:
            conn = get_db()
            try:
                rows = conn.execute("SELECT * FROM tiendas ORDER BY created_at DESC").fetchall()
                result = []
                for t in rows:
                    d = row_to_dict(t)
                    d["num_usuarios"] = conn.execute(
                        "SELECT COUNT(*) FROM usuarios WHERE tienda_id=?", (t["id"],)
                    ).fetchone()[0]
                    result.append(d)
                self.send_json(result)
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _create_tienda(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            nombre = body.get("nombre","").strip()
            slug = body.get("slug","").strip()
            if not nombre or not slug:
                self.send_error_json("Faltan nombre o slug"); return
            conn = get_db()
            try:
                c = conn.cursor()
                c.execute(
                    "INSERT INTO tiendas (nombre, slug, plan, tipo) VALUES (?,?,?,?)",
                    (nombre, slug, body.get("plan","basico"), body.get("tipo") or None)
                )
                tienda = conn.execute("SELECT * FROM tiendas WHERE id=?", (c.lastrowid,)).fetchone()
                conn.commit()
                self.send_json(row_to_dict(tienda), 201)
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _update_tienda(self, tienda_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            nombre = body.get("nombre", "").strip()
            if not nombre:
                self.send_error_json("Falta nombre", 400); return
            conn = get_db()
            try:
                tienda = conn.execute("SELECT * FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
                if not tienda:
                    self.send_error_json("Tienda no encontrada", 404); return
                conn.execute(
                    "UPDATE tiendas SET nombre=?, plan=?, activo=?, tipo=? WHERE id=?",
                    (nombre, body.get("plan", tienda["plan"]), int(body.get("activo", tienda["activo"])),
                     body.get("tipo", tienda["tipo"]), tienda_id)
                )
                tienda = conn.execute("SELECT * FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
                conn.commit()
                self.send_json(row_to_dict(tienda))
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _delete_tienda(self, tienda_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Acceso denegado", 403); return
        conn = get_db()
        try:
            t = conn.execute("SELECT id FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
            if not t:
                self.send_error_json("Tienda no encontrada", 404); return
            conn.execute("DELETE FROM usuarios WHERE tienda_id=?", (tienda_id,))
            conn.execute("DELETE FROM productos WHERE tienda_id=?", (tienda_id,))
            conn.execute("DELETE FROM tiendas WHERE id=?", (tienda_id,))
            conn.commit()
            self.send_json({"ok": True})
        except Exception as e:
            self.send_error_json(str(e), 500)
        finally:
            conn.close()

    def _delete_usuario(self, user_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol not in ('admin', 'superadmin'):
            self.send_error_json("Acceso denegado", 403); return
        conn = get_db()
        try:
            target = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
            if not target:
                self.send_error_json("Usuario no encontrado", 404); return
            if ctx.rol == 'admin' and target["tienda_id"] != ctx.tienda_id:
                self.send_error_json("Acceso denegado", 403); return
            if user_id == ctx.user_id:
                self.send_error_json("No puedes eliminarte a ti mismo", 400); return
            conn.execute("DELETE FROM usuarios WHERE id=?", (user_id,))
            conn.commit()
            self.send_json({"ok": True})
        except Exception as e:
            self.send_error_json(str(e), 500)
        finally:
            conn.close()

    def _list_tienda_usuarios(self, tienda_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Acceso denegado", 403); return
        try:
            conn = get_db()
            try:
                rows = conn.execute(
                    "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE tienda_id=? ORDER BY nombre",
                    (tienda_id,)
                ).fetchall()
                self.send_json([row_to_dict(r) for r in rows])
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _create_tienda_usuario(self, tienda_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol != 'superadmin':
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            nombre = body.get("nombre","").strip()
            username = body.get("username","").strip().lower()
            password = body.get("password","")
            rol = body.get("rol","cajero")
            if not nombre or not username or not password:
                self.send_error_json("Faltan campos requeridos"); return
            if len(password) < 6:
                self.send_error_json("La contraseña debe tener al menos 6 caracteres", 400); return
            if rol not in ('admin','cajero'):
                self.send_error_json("Rol inválido", 400); return
            salt = secrets.token_hex(16)
            password_hash = _hash_password(password, salt)
            conn = get_db()
            try:
                tienda_check = conn.execute("SELECT id FROM tiendas WHERE id=?", (tienda_id,)).fetchone()
                if not tienda_check:
                    self.send_error_json("Tienda no encontrada", 404); return
                c = conn.cursor()
                c.execute(
                    "INSERT INTO usuarios (tienda_id,nombre,username,password_hash,salt,rol) VALUES (?,?,?,?,?,?)",
                    (tienda_id, nombre, username, password_hash, salt, rol)
                )
                conn.commit()
                user = conn.execute(
                    "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE id=?",
                    (c.lastrowid,)
                ).fetchone()
                self.send_json(row_to_dict(user), 201)
            finally:
                conn.close()
        except sqlite3.IntegrityError:
            self.send_error_json("El nombre de usuario ya existe en esta tienda", 409)
        except Exception as e:
            self.send_error_json(str(e), 500)


    # ── USUARIOS ────────────────────────────────────────────────────
    def _list_usuarios(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol not in ('admin','superadmin'):
            self.send_error_json("Acceso denegado", 403); return
        try:
            conn = get_db()
            try:
                if ctx.rol == 'superadmin':
                    rows = conn.execute(
                        "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios ORDER BY nombre"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE tienda_id=? ORDER BY nombre",
                        (ctx.tienda_id,)
                    ).fetchall()
                self.send_json([row_to_dict(r) for r in rows])
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _create_usuario(self):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol not in ('admin','superadmin'):
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            nombre = body.get("nombre","").strip()
            username = body.get("username","").strip().lower()
            password = body.get("password","")
            rol = body.get("rol","cajero")
            if not nombre or not username or not password:
                self.send_error_json("Faltan campos requeridos"); return
            if len(password) < 6:
                self.send_error_json("La contraseña debe tener al menos 6 caracteres", 400); return
            # Admin no puede crear superadmins
            if rol == 'superadmin' and ctx.rol != 'superadmin':
                self.send_error_json("No puedes crear usuarios superadmin", 403); return
            if rol not in ('admin','cajero','superadmin'):
                self.send_error_json("Rol inválido", 400); return
            # Admin siempre crea en su propia tienda
            tienda_id = ctx.tienda_id if ctx.rol != 'superadmin' else body.get("tienda_id")
            if rol != 'superadmin' and tienda_id is None:
                self.send_error_json("Se requiere tienda_id para roles admin o cajero", 400); return
            salt = secrets.token_hex(16)
            password_hash = _hash_password(password, salt)
            conn = get_db()
            try:
                c = conn.cursor()
                c.execute(
                    "INSERT INTO usuarios (tienda_id,nombre,username,password_hash,salt,rol) VALUES (?,?,?,?,?,?)",
                    (tienda_id, nombre, username, password_hash, salt, rol)
                )
                conn.commit()
                user = conn.execute(
                    "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE id=?",
                    (c.lastrowid,)
                ).fetchone()
                self.send_json(row_to_dict(user), 201)
            finally:
                conn.close()
        except sqlite3.IntegrityError:
            self.send_error_json("El nombre de usuario ya existe en esta tienda", 409)
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _update_usuario(self, user_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol not in ('admin','superadmin'):
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            conn = get_db()
            try:
                target = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
                if not target:
                    self.send_error_json("Usuario no encontrado", 404); return
                # Admin solo puede modificar usuarios de su tienda
                if ctx.rol == 'admin' and target["tienda_id"] != ctx.tienda_id:
                    self.send_error_json("Acceso denegado", 403); return
                # Bloquear auto-escalación de rol
                if "rol" in body and body["rol"] == "superadmin" and ctx.rol != 'superadmin':
                    self.send_error_json("No puedes asignar rol superadmin", 403); return
                # Bloquear auto-cambio de rol propio
                if "rol" in body and user_id == ctx.user_id:
                    self.send_error_json("No puedes cambiar tu propio rol", 403); return
                nuevo_rol = body.get("rol", target["rol"])
                nuevo_nombre = body.get("nombre", target["nombre"])
                nuevo_activo = int(body.get("activo", target["activo"]))
                conn.execute(
                    "UPDATE usuarios SET nombre=?, rol=?, activo=? WHERE id=?",
                    (nuevo_nombre, nuevo_rol, nuevo_activo, user_id)
                )
                conn.commit()
                user = conn.execute(
                    "SELECT id,tienda_id,nombre,username,rol,activo,created_at FROM usuarios WHERE id=?",
                    (user_id,)
                ).fetchone()
                self.send_json(row_to_dict(user))
            finally:
                conn.close()
        except Exception as e:
            self.send_error_json(str(e), 500)

    def _update_usuario_password(self, user_id):
        ctx = self.require_auth()
        if not ctx: return
        if ctx.rol not in ('admin','superadmin'):
            self.send_error_json("Acceso denegado", 403); return
        try:
            body = self.read_json_body()
            new_password = body.get("password","")
            if not new_password:
                self.send_error_json("Falta la nueva contraseña"); return
            if len(new_password) < 6:
                self.send_error_json("La contraseña debe tener al menos 6 caracteres", 400); return
            conn = get_db()
            try:
                target = conn.execute("SELECT * FROM usuarios WHERE id=?", (user_id,)).fetchone()
                if not target:
                    self.send_error_json("Usuario no encontrado", 404); return
                if ctx.rol == 'admin' and target["tienda_id"] != ctx.tienda_id:
                    self.send_error_json("Acceso denegado", 403); return
                salt = secrets.token_hex(16)
                password_hash = _hash_password(new_password, salt)
                conn.execute(
                    "UPDATE usuarios SET password_hash=?, salt=? WHERE id=?",
                    (password_hash, salt, user_id)
                )
                conn.commit()
                self.send_json({"ok": True})
            finally:
                conn.close()
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
