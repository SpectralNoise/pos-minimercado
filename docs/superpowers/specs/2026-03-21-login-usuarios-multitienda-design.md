# SFPOS — Login, Usuarios y Multi-Tienda (SaaS)

## Contexto

SFPOS es un POS para mini mercados colombianos. Esta spec diseña el sistema de autenticación, gestión de usuarios y multi-tenancy SaaS: múltiples tiendas en un solo deployment Railway + Turso, con roles superadmin / admin / cajero.

También incluye un botón de "Actualizar inventario" (cambio menor independiente).

---

## Modelo de datos

### Tabla `tiendas`

```sql
CREATE TABLE IF NOT EXISTS tiendas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre     TEXT    NOT NULL,
    slug       TEXT    NOT NULL UNIQUE,
    plan       TEXT    DEFAULT 'basico',
    activo     INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### Tabla `usuarios`

```sql
CREATE TABLE IF NOT EXISTS usuarios (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tienda_id     INTEGER REFERENCES tiendas(id),  -- NULL = superadmin de plataforma
    nombre        TEXT    NOT NULL,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    salt          TEXT    NOT NULL,
    rol           TEXT    NOT NULL DEFAULT 'cajero',  -- 'superadmin' | 'admin' | 'cajero'
    activo        INTEGER DEFAULT 1,
    created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**Notas:**
- `tienda_id = NULL` identifica al superadmin de plataforma.
- `username` es único globalmente.
- `password_hash = SHA-256(salt + password)` con sal aleatoria de 16 bytes hex.

### Migraciones en tablas existentes

Agregar `tienda_id INTEGER` a:
- `productos`
- `ventas`
- `turnos`

Los registros existentes quedan con `tienda_id = NULL`. El superadmin los puede ver; los admins de tienda no.

### Usuario inicial (seed)

Al inicializar la DB, si `usuarios` está vacía:

```python
INSERT INTO usuarios (tienda_id, nombre, username, password_hash, salt, rol)
VALUES (NULL, 'Super Admin', 'admin', <hash('sfpos1234')>, <salt>, 'superadmin')
```

---

## Autenticación — Tokens HMAC

### Generación (POST /api/auth/login)

```
payload = f"{user_id}:{tienda_id}:{rol}:{exp_timestamp}"
signature = HMAC-SHA256(SECRET_KEY, payload)
token = base64(payload) + "." + hex(signature)
```

- `exp_timestamp` = `now + 43200` segundos (12 horas)
- `SECRET_KEY` leída de env var `SECRET_KEY`. Si no está configurada, se genera una aleatoria en cada arranque del servidor (las sesiones no sobreviven reinicios del servidor).

### Validación (middleware en cada endpoint protegido)

1. Leer header `Authorization: Bearer <token>`
2. Separar `payload_b64` y `signature`
3. Recompute HMAC con `SECRET_KEY` — si no coincide → 401
4. Decodificar payload → extraer `user_id`, `tienda_id`, `rol`, `exp`
5. Si `exp < now` → 401 `{"error": "Sesión expirada"}`
6. Retornar `AuthContext(user_id, tienda_id, rol)`

### Función helper en server.py

```python
def require_auth(self) -> AuthContext | None:
    """Lee y valida el token. Retorna AuthContext o envía 401 y retorna None."""
```

Todos los endpoints protegidos llaman `ctx = self.require_auth()` al inicio. Si retorna `None`, el handler retorna inmediatamente (ya se envió el 401).

---

## API endpoints

### Públicos (sin token)

| Método | Ruta | Descripción |
|--------|------|-------------|
| POST | `/api/auth/login` | Login con username + password |
| GET | `/api/auth/me` | Valida token y retorna user+tienda |

**POST /api/auth/login** — body: `{username, password}`

Respuesta 200:
```json
{
  "token": "...",
  "user": {"id": 1, "nombre": "María", "username": "maria", "rol": "cajero"},
  "tienda": {"id": 2, "nombre": "Minimercado La 45", "slug": "la-45"} // null si superadmin
}
```
- 401 si credenciales incorrectas o usuario inactivo.

**GET /api/auth/me** — requiere token

Retorna el mismo objeto `{user, tienda}` sin el token. Útil para restaurar sesión al recargar.

### Protegidos — Gestión de tiendas (solo superadmin)

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/api/tiendas` | Listar todas las tiendas |
| POST | `/api/tiendas` | Crear tienda |
| PUT | `/api/tiendas/:id` | Editar tienda |
| GET | `/api/tiendas/:id/usuarios` | Usuarios de una tienda |
| POST | `/api/tiendas/:id/usuarios` | Crear usuario en una tienda |

### Protegidos — Gestión de usuarios (admin de tienda)

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/api/usuarios` | Usuarios de mi tienda (admin) |
| POST | `/api/usuarios` | Crear usuario en mi tienda (admin, solo rol cajero) |
| PUT | `/api/usuarios/:id` | Editar nombre/rol/activo |
| PUT | `/api/usuarios/:id/password` | Cambiar contraseña |

### Protegidos — Endpoints existentes modificados

Todos los endpoints de `productos`, `ventas`, `turnos` ahora:
1. Llaman `require_auth()`
2. Filtran queries con `WHERE tienda_id = ctx.tienda_id`
3. En INSERT incluyen `tienda_id = ctx.tienda_id`

**Excepción superadmin:** Si `ctx.rol == 'superadmin'`, no filtra por `tienda_id` (ve todo).

---

## Frontend

### Inicialización

Al cargar `index.html`:
1. Leer `sfpos_token` y `sfpos_user` de localStorage.
2. Si no existen o han expirado (comparar `exp` con `Date.now()`) → mostrar pantalla de login.
3. Si existen → llamar `GET /api/auth/me` para validar con el servidor.
   - 200 → continuar con la app, restaurar `currentUser`.
   - 401 → limpiar localStorage → mostrar pantalla de login.

### Pantalla de login

- Ocupa toda la pantalla, oculta la app principal (`#app-root { display: none }`).
- Branding: logo/nombre **SFPOS** con tagline.
- Campos: `username` (texto) + `password` (password) + botón "Ingresar".
- En error: shake de formulario + mensaje inline (no toast).
- Al autenticar: guardar `sfpos_token`, `sfpos_user` en localStorage → ocultar login → inicializar app.

### Estado global nuevo

```javascript
let currentUser = null;
// {id, nombre, username, rol, tienda_id}
// tienda_id === null para superadmin
```

### Flujo por rol al entrar

| Rol | Comportamiento |
|-----|---------------|
| `superadmin` | Muestra panel de plataforma (gestión de tiendas). Oculta toda la UI de cobro/inventario/reportes. |
| `admin` | Entra en modo admin. Muestra tabs Inventario, Reportes, Usuarios. No muestra switcher Cajero/Admin. |
| `cajero` | Entra directo a modo cajero (Cobro). No muestra tabs de admin. |

### Cambios en UI existente

- **Switcher Cajero/Admin** se oculta — el modo lo determina el rol.
- **Barra superior** añade chip con nombre del usuario + botón "Cerrar sesión" (🚪).
- **`abrirCaja()`** pre-rellena `cajero` con `currentUser.nombre` (el campo de texto se convierte en solo-lectura o se elimina del modal).
- **Todos los `fetch()`** se envuelven en una función helper `apiFetch(url, options)` que añade `Authorization: Bearer <token>` automáticamente y maneja 401 redirigiendo al login.

### Panel Superadmin

Pantalla separada `#screen-superadmin` con dos subvistas:

**Vista lista de tiendas:**
- Tabla: Nombre | Slug | Plan | Estado | Usuarios | Fecha | Acciones
- Botón "Nueva tienda" → modal con: nombre, slug, plan

**Vista detalle de tienda:**
- Datos editables de la tienda
- Tabla de usuarios: Nombre | Usuario | Rol | Estado | Acciones
- Botón "Nuevo usuario" → modal: nombre, username, contraseña temporal, rol (admin/cajero)
- Botón desactivar usuario

### Panel Usuarios (admin de tienda)

Nueva pestaña "Usuarios" en el modo admin:
- Lista de usuarios de la tienda con rol y estado
- Admin puede crear cajeros y otros admins de su tienda
- Admin NO puede crear superadmins
- Admin puede cambiar contraseñas y desactivar usuarios

### Logout

- Botón "Cerrar sesión" → `confirm()` → limpiar `sfpos_token` + `sfpos_user` de localStorage → mostrar pantalla de login.
- Si hay caja abierta → advertir antes de cerrar sesión.

### Expiración de sesión

Cualquier `fetch()` que retorne 401 → limpiar localStorage → mostrar pantalla de login con toast "Sesión expirada, ingresa de nuevo".

---

## Botón "Actualizar inventario"

En la barra de herramientas del inventario, junto al botón de búsqueda, agregar:

```html
<button onclick="recargarInventario()">🔄 Actualizar</button>
```

```javascript
async function recargarInventario() {
  await loadData();
  renderInventario();
  showToast('✅ Inventario actualizado');
}
```

---

## Seguridad

- Contraseñas hasheadas con SHA-256 + sal aleatoria. Nunca se almacena la contraseña en texto plano.
- Tokens HMAC-SHA256 con expiración. No se almacenan en servidor.
- `SECRET_KEY` debe configurarse como env var en Railway. Sin ella, las sesiones no sobreviven reinicios del servidor.
- El admin de tienda no puede escalar privilegios (no puede crear superadmins).
- Todos los endpoints protegidos validan el token y filtran por `tienda_id`.

---

## Migración de datos existentes

Los registros existentes en `productos`, `ventas` y `turnos` quedan con `tienda_id = NULL`. Solo el superadmin los verá. El admin de la primera tienda no verá datos históricos a menos que se haga una migración manual asignando `tienda_id` a esos registros.

---

## Fuera de alcance (esta fase)

- Impersonación de tienda por superadmin
- Recuperación de contraseña vía email
- 2FA
- Registro de tiendas por self-service (el superadmin crea las tiendas manualmente)
- Métricas globales de plataforma en el panel superadmin
