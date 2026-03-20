# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

POS (Point-of-Sale) system for Colombian mini markets (mini mercados). The entire application is a **single-file monolith**: `index.html` contains all HTML, CSS, and JavaScript (~2000 lines). There is no build step, no npm, no framework.

## Running the App

```bash
python3 server.py
```

Serves on `https://localhost:5051` (HTTPS with local SSL certs). Access from tablets on the local network via the host machine's LAN IP.

Optional AI product analysis (Claude Vision):
```bash
export ANTHROPIC_API_KEY=your_key
pip3 install anthropic
```

## Architecture

Everything lives in `index.html`. The Python `server.py` is a thin static file server with CORS enabled — it has no business logic.

### Modes

The UI has two modes switchable at runtime:
- **Cajero** (cashier) — tablet-facing checkout interface
- **Admin** — PC-facing inventory/reports interface

### Modules (rendered as tab sections)

| Module | Spanish | Purpose |
|--------|---------|---------|
| Checkout | Cobro | Scan products, manage cart, process cash/transfer payments, print/share receipts |
| Inventory | Inventario | CRUD products, stock tracking, CSV export, AI photo analysis |
| Receipts | Facturación | Receipt history, reconciliation, WhatsApp sharing |
| Reports | Reportes | Sales KPIs and analytics by period (today/week/month) |

### State

All state is global JS variables in `index.html`:

```javascript
let products = [...]      // Product catalog (in-memory, populated from sample data)
let cart = []             // Current checkout cart
let sales = [...]         // Historical transactions (in-memory)
let cajaSession = null    // Active cashier shift data
let currentMode          // 'cajero' | 'admin'
```

Persistence is **not implemented** — data resets on page reload. localStorage is only used for theme and mode preference. SQLite migration is planned but not started.

### Rendering Pattern

No reactive framework. State changes trigger manual re-render calls:
- `renderProducts()` — product grid (filtered by category/search)
- `renderCart()` — checkout items and totals
- `renderInventario()` — admin product table
- `renderReportes()` — analytics dashboard

### Barcode Scanner

Hardware scanner detection uses keystroke velocity: if characters arrive faster than 60ms apart, it's treated as a barcode scan (not keyboard input).

### AI Integration

`index.html` calls the Anthropic Claude API directly from the browser (requires `ANTHROPIC_API_KEY` forwarded from server or injected). Used for product photo analysis: given a product image, it returns name, barcode, category, and emoji suggestion.

### Theming

CSS custom properties (`--bg`, `--surface`, `--text`, etc.) drive light/dark theme switching. The toggle updates `document.documentElement`'s class and saves to localStorage.

## Key Conventions

- **Language:** UI text is Spanish (Colombian locale `es-CO` for date/number formatting)
- **No external dependencies:** pure vanilla JS, no CDN imports, no npm packages
- **Modals:** overlay pattern using `.modal-overlay` CSS class toggled with JS
- **Notifications:** toast system via `showToast(message, type)` function
